"""Private call state, exports, and post-call review jobs for one local server."""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event

from .provider import GroqClient
from .records import review_saved, save_outputs, write_json
from .workflow import Call, Fact, Turn, latest_readback

CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
LOGGER = logging.getLogger(__name__)


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
    def __init__(self, root: Path, id_pattern: re.Pattern = CALL_SID):
        self.root = root
        self.id_pattern = id_pattern

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
        if (directory / "transcript.json").exists():
            return
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
        save_outputs(directory, state.call, actual, call_error=state.error)

    def recover_exports(self) -> None:
        for path in self.root.glob("*/_state.json"):
            sid = path.parent.name
            if self.id_pattern.fullmatch(sid):
                state = self.load(sid)
                if state and (state.finished or state.stream_started):
                    self.finish(sid, state)

    def queue_retry(self, sid: str) -> None:
        if not self.id_pattern.fullmatch(sid):
            raise ValueError("Enter a valid call identifier.")
        directory = self.root / sid
        if not (directory / "review_error.json").is_file():
            raise ValueError("There is no failed review to retry for this call.")
        (directory / "review_retry.request").touch()

    def review_pending(self, provider: GroqClient) -> None:
        for transcript in self.root.glob("*/transcript.json"):
            directory = transcript.parent
            if not self.id_pattern.fullmatch(directory.name):
                continue
            output = directory / "review.json"
            error = directory / "review_error.json"
            retry = directory / "review_retry.request"
            if output.exists() or (error.exists() and not retry.exists()):
                continue
            state = self.load(directory.name)
            latest = latest_readback(state.call) if state else None
            finished = state is not None and latest is not None and latest in state.played
            review_saved(directory, provider, retry=retry.exists(), read_back_finished=finished)
            retry.unlink(missing_ok=True)

    def review_loop(self, provider: GroqClient, stop: Event) -> None:
        while not stop.is_set():
            try:
                self.review_pending(provider)
            except OSError:
                LOGGER.exception("Could not access the local review files. Will retry.")
            stop.wait(1)
