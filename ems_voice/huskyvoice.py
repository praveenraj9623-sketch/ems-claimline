"""HuskyVoice outbound calls and transcript import."""

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

CALLS_URL = "https://api.huskyvoice.ai/v1/calls"
FAILED_STATES = {"failed", "disallowed", "no_answer", "busy"}
END_STATES = {"completed", "cancelled", "canceled"} | FAILED_STATES
CALL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}")
NO_CALLER = "HuskyVoice has no outbound caller number available for the configured agent. In HuskyVoice, open Phone Numbers and assign an existing number."
UNCONFIRMED = ("HuskyVoice did not confirm the dial request. Check All Calls in HuskyVoice "
               "before placing another call.")
ROLES = {"agent": "agent", "assistant": "agent", "sarah": "agent",
         "user": "representative", "customer": "representative",
         "representative": "representative", "insurance representative": "representative"}
SPEAKER_LINE = re.compile(r"^\s*(" + "|".join(ROLES) + r")\s*:\s*(.*)$", re.I)


@dataclass(frozen=True)
class HuskySettings:
    api_key: str = field(repr=False)
    to_number: str = field(repr=False)
    agent_id: str

    @classmethod
    def load(cls, values: dict[str, str]) -> "HuskySettings":
        key = values.get("HUSKYVOICE_API_KEY", "").strip()
        number = values.get("CALL_TO_NUMBER", "").strip()
        agent = values.get("HUSKYVOICE_AGENT_ID", "").strip()
        if not re.fullmatch(r"[!-~]+", key):
            raise ValueError("Set HUSKYVOICE_API_KEY in .env or Streamlit secrets.")
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
            raise ValueError("Set CALL_TO_NUMBER in international format with + and the country code.")
        if not CALL_ID.fullmatch(agent):
            raise ValueError("Set HUSKYVOICE_AGENT_ID in .env or Streamlit secrets to the agent ID from HuskyVoice.")
        return cls(key, number, agent)


def redact(text: str, api_key: str, to_number: str) -> str:
    if api_key:
        text = text.replace(api_key, "[API key]")
    digits = re.sub(r"[^0-9]", "", to_number)
    if len(digits) >= 7:
        number = r"[\s().-]*".join(digits)
        text = re.sub(rf"(?<!\d)\+?{number}(?!\d)", "[phone number]", text)
    return text


class HuskyError(Exception):
    def __init__(self, message: str, *, unconfirmed: bool = False, retryable: bool = False, details: str = ""):
        super().__init__(message)
        self.unconfirmed = unconfirmed
        self.retryable = retryable
        self.details = details


def error_text(payload: object, *, include_message: bool = False, depth: int = 0) -> str:
    """Read error messages without rendering the response body or request metadata."""
    if depth > 4:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        parts = (error_text(item, include_message=True, depth=depth + 1) for item in payload[:3])
        return " · ".join(part for part in parts if part)
    if not isinstance(payload, dict):
        return ""
    fields: tuple[str, ...] = ("error", "error_message", "failure_reason", "detail", "errors", "data")
    if include_message:
        fields += ("message", "msg", "reason", "code")
    for name in fields:
        nested = error_text(payload.get(name), include_message=include_message or name != "data", depth=depth + 1)
        if nested:
            return nested
    return ""


def sanitize_error(text: str, settings: HuskySettings) -> str:
    text = redact(text, settings.api_key, settings.to_number)
    text = re.sub(r"\b(?:https?://|sips?:)[^\s<>\"]+", "[URL]", text, flags=re.I)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", text)
    text = re.sub(r"(?<!\w)\+?\d(?:[\s().-]*\d){6,}(?!\w)", "[phone number]", text)
    text = re.sub(r"\b(?:bearer\s+|(?:x-api-key|api[_ -]?key|token|secret)\s*[:=]\s*)[^\s,;]+",
                  "[credential]", text, flags=re.I)
    return " ".join(text.split())[:500]


def missing_caller_number(payload: object) -> bool:
    # Use provider details only to select fixed messages. They may echo request data.
    message = re.sub(r"[_-]", " ", str(payload).lower())
    return bool(re.search(
        r"(?:no |missing |assign (?:a |an )?|without (?:a |an )?)"
        r"(?:(?:outbound|assigned|valid|agent|configured) )*(?:phone|caller|calling|from) number"
        r"|(?:phone|caller|calling|from) number.{0,40}(?:not assigned|not configured|is required|missing)"
        r"|no (?:assigned )?number (?:is )?assigned|no assigned number"
        r"|no outbound (?:phone )?numbers? available for (?:the )?agent"
        r"|does not have.{0,40}(?:phone|caller|calling|from) number", message
    ))


def call_failure(data: dict, status: str) -> str:
    """Translate known provider errors without exposing the original payload."""
    error = data.get("error") or data.get("failure_reason") or data.get("error_message")
    if missing_caller_number(error):
        return NO_CALLER
    reason = re.sub(r"[_-]", " ", str(error).lower())
    if re.search(r"\b(?:insufficient|no|out of|not enough) (?:call(?:ing)? )?credits?\b|\bcredits? exhausted\b", reason):
        return "HuskyVoice reports insufficient calling credits for this attempt."
    if re.search(r"\b(?:unsupported|blocked|restricted) (?:destination|country|region)\b"
                 r"|\b(?:destination|country|region)\b.{0,35}\b(?:unsupported|blocked|not allowed|not supported)\b", reason):
        return "HuskyVoice reports that this destination is restricted. Check the agent’s outbound calling settings."
    if status == "busy" or re.search(r"\b(?:user|line|destination) busy\b", reason):
        return "HuskyVoice reports that the destination was busy."
    if status == "no_answer" or re.search(r"\b(?:no answer|did not answer)\b", reason):
        return "HuskyVoice reports that the call was unanswered."
    if re.search(r"\b(?:invalid|unverified) (?:destination|recipient|user number)\b", reason):
        return "HuskyVoice could not dial the destination number. Check the number and its verification status."
    if sip := re.search(r"\bsip(?:\s+(?:status|code|response))?\s*[:=]?\s*([3-6][0-9]{2})\b", reason):
        code = sip[1]
        explanations = {
            "403": "The calling provider rejected the call.",
            "404": "The calling provider could not find a route to the destination.",
            "408": "The call timed out.",
            "480": "The destination was temporarily unavailable.",
            "486": "The destination was busy.",
            "603": "The call was declined.",
        }
        return f"HuskyVoice reported SIP {code}. " + explanations.get(
            code, "Check the call in HuskyVoice → All Calls for details.")
    if status == "disallowed":
        return "HuskyVoice blocked this outbound call. Check the attempt in HuskyVoice → All Calls."
    return "HuskyVoice could not complete the call. Check the attempt in HuskyVoice → All Calls."


class HuskyVoice:
    def __init__(self, settings: HuskySettings):
        self.settings = settings

    def request(
        self, method: str, url: str, *, attempt_id: str = "", body: dict | None = None
    ) -> tuple[dict, str]:
        creating = method == "POST"
        headers = {"x-api-key": self.settings.api_key}
        if attempt_id:
            headers["Idempotency-Key"] = attempt_id
        try:
            response = httpx.request(method, url, headers=headers, timeout=8, follow_redirects=False, json=body)
        except (httpx.HTTPError, ValueError):
            raise HuskyError(UNCONFIRMED if creating else "HuskyVoice is unreachable. Try refreshing the call status.",
                             unconfirmed=creating, retryable=not creating) from None
        try:
            payload = response.json()
        except ValueError:
            payload = response.text if response.headers.get("content-type", "").split(";")[0] == "text/plain" else None
        data = payload.get("data", payload) if isinstance(payload, dict) else None
        status = self.status_name(data) if isinstance(data, dict) else ""
        details = sanitize_error(error_text(
            payload, include_message=creating or not response.is_success or not status or status in FAILED_STATES
        ), self.settings)
        if not response.is_success:
            raise self.http_error(response.status_code, creating, payload, details)
        if not isinstance(data, dict):
            raise HuskyError(UNCONFIRMED if creating else "HuskyVoice returned an unreadable call status.",
                             unconfirmed=creating, retryable=not creating, details=details)
        return data, details

    @staticmethod
    def http_error(code: int, creating: bool, payload: object, details: str) -> HuskyError:
        unconfirmed = retryable = False
        if code == 401:
            message = "HuskyVoice rejected the API key. Check HUSKYVOICE_API_KEY in your settings."
        elif code in (400, 403, 404, 409, 422) and missing_caller_number(payload):
            message = NO_CALLER
        elif code == 403:
            scope = "calls:create" if creating else "calls:read"
            message = f"HuskyVoice denied access. Check that the API key has the {scope} permission."
        elif code == 404:
            message = ("HuskyVoice could not find the configured agent." if creating else
                       "HuskyVoice could not find the call. Try refreshing the call status.")
            retryable = not creating
        elif code == 429:
            message = "HuskyVoice’s rate limit was reached. Wait before trying again."
        elif code >= 500 or code in (408, 409) or 300 <= code < 400:
            message = (UNCONFIRMED if creating else
                       "HuskyVoice could not return the call status. Try again shortly.")
            unconfirmed, retryable = creating, not creating
        else:
            message = ("HuskyVoice rejected the call request. Check the agent’s outbound setup and CALL_TO_NUMBER."
                       if creating else "HuskyVoice rejected the status request.")
        response_details = f"HTTP {code} · {details}" if details else f"HTTP {code}"
        return HuskyError(message, unconfirmed=unconfirmed, retryable=retryable, details=response_details)

    def start(self, attempt_id: str) -> tuple[str, str]:
        data, details = self.request("POST", CALLS_URL, attempt_id=attempt_id,
                                     body={"agent_id": self.settings.agent_id, "user_number": self.settings.to_number})
        call_id = data.get("call_id")
        if (not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id)
                or redact(call_id, self.settings.api_key, self.settings.to_number) != call_id):
            raise HuskyError(UNCONFIRMED, unconfirmed=True,
                             details=details or "The create-call response did not contain a valid call ID.")
        return call_id, self.status_name(data, default="queued")

    def status(self, call_id: str) -> tuple[dict, str]:
        data, details = self.request("GET", f"{CALLS_URL}/{call_id}")
        if data.get("call_id", call_id) != call_id:
            raise HuskyError("HuskyVoice returned a status for a different call. Try refreshing.", retryable=True)
        return data, details

    @staticmethod
    def status_name(data: dict, default: str = "") -> str:
        status = data.get("status")
        if isinstance(status, str) and status.strip():
            return status.strip().lower().replace("-", "_").replace(" ", "_")
        return default

    def transcript(self, data: dict) -> list[dict]:
        raw = data.get("call_transcript") or data.get("transcript")
        if isinstance(raw, str) and raw.startswith(("http://", "https://")):
            # Download only from the documented provider or S3 origins. Signed links use
            # their own credentials and never receive the HuskyVoice API key.
            try:
                url = urlsplit(raw)
                host = url.hostname or ""
                s3 = re.fullmatch(r"(?:[a-z0-9.-]+\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host)
                if (url.scheme != "https" or url.username or url.password or url.port not in (None, 443)
                        or not (host.endswith(".huskyvoice.ai") or s3)):
                    raise ValueError
                response = httpx.get(raw, timeout=8, follow_redirects=False)
                response.raise_for_status()
                raw = response.json()
            except (ValueError, httpx.HTTPError):
                raise ValueError("HuskyVoice’s transcript link is unavailable. Refresh or import the transcript.") from None
        return parse_transcript(raw, api_key=self.settings.api_key, to_number=self.settings.to_number)


def parse_transcript(raw: object, *, api_key: str = "", to_number: str = "") -> list[dict]:
    """Accept provider JSON, saved EMS transcript JSON or speaker-labelled plain text."""
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith(("[", "{")):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise ValueError("The transcript JSON could not be read.") from None
        else:
            lines: list[dict] = []
            for line in raw.splitlines():
                if match := SPEAKER_LINE.fullmatch(line):
                    lines.append({"speaker": match[1], "text": match[2]})
                elif line.strip():
                    if not lines:
                        raise ValueError("Start the transcript with a speaker label such as Sarah: or Representative:.")
                    lines[-1]["text"] += "\n" + line
            raw = lines
    if isinstance(raw, dict):
        raw = raw.get("data", raw)
        if isinstance(raw, dict):
            raw = raw.get("call_transcript") or raw.get("transcript")
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise ValueError("Use transcript JSON or text with a speaker label on each turn.")

    turns: list[dict] = []
    for item in raw:
        speaker = item.get("speaker", "") if isinstance(item, dict) else ""
        role = ROLES.get(speaker.strip().lower()) if isinstance(speaker, str) else None
        content = item.get("text") if isinstance(item, dict) else None
        if not role or not isinstance(content, str):
            raise ValueError("The transcript needs readable turns labeled as Sarah and the insurance representative.")
        if content.strip():
            text = redact(content.strip(), api_key, to_number)
            turn = {"turn": len(turns) + 1, "speaker": role, "text": text}
            if item.get("playback") == "partial":
                turn["playback"] = "partial"
            turns.append(turn)
    return turns
