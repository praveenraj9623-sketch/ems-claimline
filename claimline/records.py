"""Call records: transcript, claim result, scorecard. Written atomically, one folder per call."""

import json
import os
import tempfile
from pathlib import Path

from .extract import KEYS
from .review import CHECKS


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class CallStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, call_id: str, *, transcript, result, scorecard, detail, meta) -> Path:
        folder = self.root / call_id
        exact_result = {key: result.get(key) for key in KEYS}
        exact_card = {key: bool(scorecard[key]) for key in CHECKS}
        write_json(folder / "transcript.json", transcript)
        write_json(folder / "claim_result.json", exact_result)
        write_json(folder / "review.json", exact_card)
        write_json(folder / "review_detail.json", detail)
        write_json(folder / "meta.json", meta)
        return folder
