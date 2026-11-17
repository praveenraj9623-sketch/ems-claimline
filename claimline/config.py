"""Settings from the environment and an optional .env file (no third-party packages)."""

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env_file(path: Path) -> dict:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            value = value.strip()
            if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].strip()
            values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    groq_review_model: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    call_to_number: str = ""
    public_base_url: str = ""
    tts_voice: str = "Polly.Joanna-Neural"
    port: int = 8000
    host: str = "127.0.0.1"
    max_hold_seconds: int = 300  # Legacy setting accepted for compatibility. Holds now wait until resumed.
    calls_dir: Path = ROOT / "calls"
    twilio_api_base: str = "https://api.twilio.com"
    validate_twilio_signatures: bool = True
    twilio_trial_mode: bool = False
    twilio_trial_webhook_token: str = ""
    ui_origins: tuple = ("http://127.0.0.1:8501", "http://localhost:8501")

    @classmethod
    def load(cls, root: Path = ROOT, env: dict | None = None) -> "Settings":
        values = read_env_file(root / ".env")
        values.update(os.environ if env is None else env)

        def get(name, default="", *aliases):
            for key in (name, *aliases):
                if values.get(key, "").strip():
                    return values[key].strip()
            return default

        return cls(
            groq_api_key=get("GROQ_API_KEY"),
            groq_model=get("GROQ_MODEL", cls.groq_model),
            groq_review_model=get("GROQ_REVIEW_MODEL"),
            twilio_account_sid=get("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=get("TWILIO_AUTH_TOKEN"),
            twilio_from_number=get("TWILIO_FROM_NUMBER"),
            call_to_number=get("CALL_TO_NUMBER"),
            public_base_url=get("PUBLIC_BASE_URL", "", "VOICE_PUBLIC_URL").rstrip("/"),
            tts_voice=get("TTS_VOICE", cls.tts_voice),
            port=int(get("PORT", "8000")),
            host=get("HOST", "127.0.0.1"),
            max_hold_seconds=int(get("MAX_HOLD_SECONDS", "300")),
            calls_dir=Path(get("CALLS_DIR", str(root / "calls"))),
            twilio_api_base=get("TWILIO_API_BASE", cls.twilio_api_base).rstrip("/"),
            validate_twilio_signatures=get("VALIDATE_TWILIO_SIGNATURES", "true").lower() != "false",
            twilio_trial_mode=get("TWILIO_TRIAL_MODE", "false").lower() == "true",
            twilio_trial_webhook_token=get("TWILIO_TRIAL_WEBHOOK_TOKEN"),
            ui_origins=cls._origins(get("UI_ORIGINS"), get("STREAMLIT_PORT", "8501")),
        )

    @staticmethod
    def _origins(custom: str, ui_port: str) -> tuple:
        if custom:
            return tuple(o.strip().rstrip("/") for o in custom.split(",") if o.strip())
        return (f"http://127.0.0.1:{ui_port}", f"http://localhost:{ui_port}")

    @property
    def phone_ready(self) -> list:
        """Names of missing settings needed to place a phone call (empty = ready)."""
        need = {"TWILIO_ACCOUNT_SID": self.twilio_account_sid, "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
                "TWILIO_FROM_NUMBER": self.twilio_from_number, "CALL_TO_NUMBER": self.call_to_number}
        if self.twilio_trial_mode:
            need["TWILIO_TRIAL_WEBHOOK_TOKEN"] = self.twilio_trial_webhook_token
        return [name for name, value in need.items() if not value]


def detect_ngrok_url(timeout: float = 1.5) -> str:
    """Find a running ngrok tunnel's public https URL, or ''."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=timeout) as response:
            for tunnel in json.loads(response.read()).get("tunnels", []):
                url = tunnel.get("public_url", "")
                if url.startswith("https://"):
                    return url.rstrip("/")
    except Exception:
        pass
    return ""


def valid_phone(number: str) -> bool:
    return bool(re.fullmatch(r"\+[1-9]\d{6,14}", number or ""))
