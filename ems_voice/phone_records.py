"""Private call state, exports, and post-call review jobs for one local server."""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event

from .fields import CORE_KEYS
from .provider import GroqClient, ServiceError
from .review import review_transcript
from .workflow import Call, Fact, Turn

CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
LOGGER = logging.getLogger(__name__)


def write_json(path: Path, data) -> None:
    temp = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(temp.name)
    try:
        with temp:
            json.dump(data, temp, indent=2, ensure_ascii=False)
            temp.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class PhoneCall:
    call: Call
    played: list[int] = field(default_factory=list)
    partial: dict[int, str] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    stream_started: bool = False
    finished: bool = False
    error: str | None = None


class PhoneRecords:
    def __init__(self, root: Path):
        self.root = root

    def load(self, sid: str) -> PhoneCall | None:
        path = self.root / sid / "_state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        call = state.pop("call")
        call["transcript"] = [Turn(**turn) for turn in call["transcript"]]
        call["facts"] = {key: Fact(**fact) for key, fact in call["facts"].items()}
        for previous_key in ("responses", "expected", "gathers"):
            state.pop(previous_key, None)
        state["partial"] = {int(number): text for number, text in state.get("partial", {}).items()}
        return PhoneCall(Call(**call), **state)

    def save(self, sid: str, state: PhoneCall) -> None:
        directory = self.root / sid
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "_state.json", asdict(state))

    def finish(self, sid: str, state: PhoneCall) -> None:
        if not state.finished:
            state.finished = True
            self.save(sid, state)

        directory = self.root / sid
        transcript = directory / "transcript.json"
        if transcript.exists():
            return
        result = state.call.result()
        write_json(directory / "claim_result.json", {key: result.get(key) for key in CORE_KEYS})
        if state.error:
            write_json(directory / "call_error.json", {"error": state.error})
        played = set(state.played)
        actual = []
        for turn in state.call.records():
            if turn["speaker"] == "representative" or turn["turn"] in played:
                actual.append(turn)
            elif turn["turn"] in state.partial:
                actual.append({**turn, "text": state.partial[turn["turn"]], "playback": "partial"})
        actual.extend({"speaker": "representative", "text": text} for text in state.pending)
        for number, turn in enumerate(actual, 1):
            turn["turn"] = number
        # Publishing the transcript last makes it the review worker's readiness signal.
        write_json(transcript, actual)

    def recover_exports(self) -> None:
        for path in self.root.glob("CA*/_state.json"):
            sid = path.parent.name
            if CALL_SID.fullmatch(sid):
                state = self.load(sid)
                if state and (state.finished or state.stream_started):
                    self.finish(sid, state)

    def queue_retry(self, sid: str) -> None:
        if not CALL_SID.fullmatch(sid):
            raise ValueError("Enter a Twilio CallSid (CA followed by 32 hexadecimal characters).")
        directory = self.root / sid
        if not (directory / "review_error.json").is_file():
            raise ValueError("There is no failed review to retry for this call.")
        (directory / "review_retry.request").touch()

    def review_pending(self, provider: GroqClient) -> None:
        for transcript in self.root.glob("CA*/transcript.json"):
            directory = transcript.parent
            if not CALL_SID.fullmatch(directory.name):
                continue
            output = directory / "review.json"
            error = directory / "review_error.json"
            retry = directory / "review_retry.request"
            if output.exists() or (error.exists() and not retry.exists()):
                continue
            error.unlink(missing_ok=True)
            retry.unlink(missing_ok=True)
            try:
                records = json.loads(transcript.read_text(encoding="utf-8"))
                scorecard = review_transcript(records, "groq", provider).scorecard
                if scorecard["all_four_facts_collected"]:
                    # The judge receives only the transcript. Also require the workflow to have captured each fact.
                    result = json.loads((directory / "claim_result.json").read_text(encoding="utf-8"))
                    scorecard["all_four_facts_collected"] = all(result[key] is not None for key in CORE_KEYS)
            except Exception as exc:
                message = str(exc) if isinstance(exc, ServiceError) else "The call review could not be completed."
                write_json(error, {"retryable": True, "error": message})
                LOGGER.warning("Review failed for %s. See review_error.json and use retry-review.", directory.name)
            else:
                write_json(output, scorecard)

    def review_loop(self, provider: GroqClient, stop: Event) -> None:
        while not stop.is_set():
            try:
                self.review_pending(provider)
            except OSError:
                LOGGER.exception("Could not access the local review files. Will retry.")
            stop.wait(1)
