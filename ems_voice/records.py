"""Private result files and the shared post-conversation review."""

import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

from .fields import CORE_KEYS
from .provider import GroqClient, ServiceError
from .review import review_transcript
from .workflow import Call

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


def read_output(directory: Path, name: str):
    try:
        return json.loads((directory / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_outputs(directory: Path, call: Call, transcript: list[dict], *, call_error: str | None = None) -> None:
    transcript_file = directory / "transcript.json"
    if transcript_file.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    result = call.result()
    write_json(directory / "claim_result.json", {key: result.get(key) for key in CORE_KEYS})
    if call_error:
        write_json(directory / "call_error.json", {"error": call_error})
    # The transcript is written last and signals that both review inputs are ready.
    write_json(transcript_file, transcript)


def review_saved(directory: Path, provider: GroqClient, *, retry: bool = False,
                 read_back_finished: bool = True) -> None:
    output = directory / "review.json"
    error = directory / "review_error.json"
    if output.exists() or (error.exists() and not retry):
        return
    try:
        transcript = read_output(directory, "transcript.json")
        result = read_output(directory, "claim_result.json")
        if transcript is None or result is None:
            raise ServiceError("The conversation files are unavailable. Check the saved record and retry.")
        scorecard = review_transcript(transcript, "groq", provider).scorecard
        if scorecard["all_four_facts_collected"]:
            scorecard["all_four_facts_collected"] = all(result[key] is not None for key in CORE_KEYS)
        if not read_back_finished:
            scorecard["read_back_done"] = False
    except Exception as exc:
        message = str(exc) if isinstance(exc, ServiceError) else "The conversation review could not be completed."
        write_json(error, {"retryable": True, "error": message})
        LOGGER.warning("Review failed for %s.", directory.name)
    else:
        write_json(output, scorecard)
        error.unlink(missing_ok=True)
