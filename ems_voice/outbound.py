"""Place one configured outbound call and share its progress across local processes."""

import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from requests.exceptions import RequestException
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .phone_records import CALL_SID

ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}\Z")
E164 = re.compile(r"\+[1-9][0-9]{6,14}\Z")
TERMINAL_STATUSES = {"completed", "canceled", "failed", "busy", "no-answer"}
PROGRESS = {status: rank for rank, status in enumerate(
    ("requesting", "unconfirmed", "queued", "initiated", "ringing", "in-progress")
)}
PROGRESS.update(dict.fromkeys(TERMINAL_STATUSES, 6))
TWILIO_STATUSES = set(PROGRESS) - {"requesting", "unconfirmed"}
OUTBOUND_PATH = "/voice/outbound"
UNCONFIRMED = ("Twilio did not confirm the dial request. Check your Twilio Calls Log for an active call "
               "before releasing this attempt or trying again.")


@dataclass(frozen=True)
class DialSettings:
    origin: str
    account_sid: str
    auth_token: str = field(repr=False)
    from_number: str = field(repr=False)
    to_number: str = field(repr=False)

    @classmethod
    def from_env(cls, values: dict[str, str]) -> "DialSettings":
        names = ("VOICE_PUBLIC_URL", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "CALL_TO_NUMBER")
        settings = cls(*(values.get(name, "").strip() for name in names))
        missing = [name for name in names if not values.get(name, "").strip()]
        if missing:
            raise ValueError("Set " + ", ".join(missing) + " in your local .env file.")
        try:
            origin = urlsplit(settings.origin.rstrip("/"))
            valid = (origin.scheme == "https" and origin.hostname and not origin.username
                     and not origin.path and not origin.query and not origin.fragment
                     and "your-ngrok" not in settings.origin)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Set VOICE_PUBLIC_URL to your tunnel’s HTTPS origin, without a path.")
        if not ACCOUNT_SID.fullmatch(settings.account_sid):
            raise ValueError("Set TWILIO_ACCOUNT_SID to the Account SID beginning with AC in Twilio Console.")
        if not E164.fullmatch(settings.from_number) or not E164.fullmatch(settings.to_number):
            raise ValueError("Set TWILIO_FROM_NUMBER and CALL_TO_NUMBER in international format, including + and country code.")
        if settings.from_number == settings.to_number:
            raise ValueError("Set CALL_TO_NUMBER to your mobile and TWILIO_FROM_NUMBER to your Twilio voice number.")
        return cls(settings.origin.rstrip("/"), settings.account_sid, settings.auth_token,
                   settings.from_number, settings.to_number)


@dataclass(frozen=True)
class Attempt:
    id: str
    sid: str | None
    status: str
    sequence: int
    updated_at: float
    message: str | None

    @property
    def active(self) -> bool:
        return self.status in PROGRESS and self.status not in TERMINAL_STATUSES


class OutboundCalls:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "_outbound.sqlite3"
        with closing(self.connect()) as database:
            database.execute("""
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, sid TEXT UNIQUE, status TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT -1, updated_at REAL NOT NULL, message TEXT
                )
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5, isolation_level=None)

    def latest(self) -> Attempt | None:
        with closing(self.connect()) as database:
            row = database.execute("SELECT * FROM attempts ORDER BY rowid DESC LIMIT 1").fetchone()
            return Attempt(*row) if row else None

    def reserve(self, after: str | None) -> str | None:
        with closing(self.connect()) as database, database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM attempts ORDER BY rowid DESC LIMIT 1").fetchone()
            latest = Attempt(*row) if row else None
            if (latest.id if latest else None) != after or (latest and latest.active):
                return None
            attempt = uuid4().hex
            database.execute("INSERT INTO attempts (id, status, updated_at) VALUES (?, 'requesting', ?)",
                             (attempt, time.time()))
            return attempt

    def ready(self, attempt: str) -> bool:
        with closing(self.connect()) as database:
            return database.execute("SELECT 1 FROM attempts WHERE id = ? AND status = 'requesting'",
                                    (attempt,)).fetchone() is not None

    def observe(self, attempt: str, sid: str, status: str, sequence: int = -1) -> Attempt | None:
        """Apply signed Twilio progress in order, even if it arrives before Calls.create returns."""
        with closing(self.connect()) as database, database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM attempts WHERE id = ?", (attempt,)).fetchone()
            if row is None:
                return None
            previous = Attempt(*row)
            if previous.sid not in (None, sid) or (not previous.active and not previous.sid):
                return None
            if (not previous.active or PROGRESS[status] < PROGRESS[previous.status]
                    or (sequence >= 0 and sequence <= previous.sequence)):
                return previous
            updated = Attempt(attempt, sid, status, max(sequence, previous.sequence), time.time(), None)
            try:
                database.execute(
                    "UPDATE attempts SET sid=?, status=?, sequence=?, updated_at=?, message=NULL WHERE id=?",
                    (sid, status, updated.sequence, updated.updated_at, attempt),
                )
            except sqlite3.IntegrityError:
                return None
            return updated

    def request_error(self, attempt: str, message: str, *, uncertain: bool = False) -> None:
        status = "unconfirmed" if uncertain else "request-failed"
        with closing(self.connect()) as database:
            database.execute("""
                UPDATE attempts SET status=?, updated_at=?, message=?
                WHERE id=? AND sid IS NULL AND status='requesting'
            """, (status, time.time(), message, attempt))

    def release(self, attempt: str) -> None:
        with closing(self.connect()) as database, database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM attempts ORDER BY rowid DESC LIMIT 1").fetchone()
            latest = Attempt(*row) if row else None
            if not latest or latest.id != attempt or not latest.active:
                raise ValueError("There is no matching active call attempt.")
            if latest.status == "requesting" and time.time() - latest.updated_at < 30:
                raise ValueError("The dial request is still being sent. Check again in 30 seconds.")
            database.execute("UPDATE attempts SET status='released', updated_at=?, message=? WHERE id=?",
                             (time.time(), "Call attempt released after checking the Twilio Calls Log.", attempt))


def twilio_error(error: TwilioRestException) -> str:
    guidance = {
        20003: "Check TWILIO_ACCOUNT_SID and the primary TWILIO_AUTH_TOKEN for that account.",
        21210: "Use a voice-capable Twilio number from this account as TWILIO_FROM_NUMBER.",
        21211: "Check CALL_TO_NUMBER and include its country code.",
        21212: "Check TWILIO_FROM_NUMBER and include its country code.",
        21215: "Enable the destination country in your Twilio Voice Geographic Permissions.",
        21219: "Verify your mobile number in Twilio before calling from a trial account.",
        20429: "Twilio is rate-limiting requests. Wait a moment before clicking again.",
    }
    code = error.code if isinstance(error.code, int) else None
    detail = guidance.get(code or 0, "Check your Twilio Calls Log and Debugger for details.")
    return f"Twilio rejected the dial request (code {code or error.status}). {detail}"


def place_call(calls: OutboundCalls, settings: DialSettings, after: str | None) -> None:
    attempt = calls.reserve(after)
    if attempt is None:
        return
    base = f"{settings.origin}{OUTBOUND_PATH}/{attempt}"
    readiness_url = base + "/ready"
    form = {"AccountSid": settings.account_sid}
    headers = {
        "X-Twilio-Signature": RequestValidator(settings.auth_token).compute_signature(readiness_url, form),
        "ngrok-skip-browser-warning": "1",
    }
    try:
        response = httpx.post(readiness_url, data=form, headers=headers, timeout=4)
        if response.status_code != 204:
            calls.request_error(attempt, "The public voice backend did not recognize this call request. "
                                "Start or restart the voice backend and check the tunnel, shared project folder, "
                                "and Twilio credentials.")
            return
    except httpx.HTTPError:
        calls.request_error(attempt, "The public voice backend is unreachable. Start the voice service and "
                            "ngrok, and check VOICE_PUBLIC_URL.")
        return

    client = Client(settings.account_sid, settings.auth_token,
                    http_client=TwilioHttpClient(timeout=8, max_retries=0))
    try:
        call = client.calls.create(
            to=settings.to_number, from_=settings.from_number, url=base + "/answer", method="POST",
            status_callback=base + "/status", status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
    except TwilioRestException as error:
        if 400 <= error.status < 500 and error.status != 408:
            calls.request_error(attempt, twilio_error(error))
        else:
            calls.request_error(attempt, UNCONFIRMED, uncertain=True)
    except (RequestException, ValueError):
        calls.request_error(attempt, UNCONFIRMED, uncertain=True)
    else:
        if (not call.sid or not CALL_SID.fullmatch(call.sid)
                or calls.observe(attempt, call.sid, call.status if call.status in TWILIO_STATUSES else "queued") is None):
            calls.request_error(attempt, UNCONFIRMED, uncertain=True)
