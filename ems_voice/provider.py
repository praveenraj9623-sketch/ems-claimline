"""Standard-library transport for Groq structured output and transcription."""

import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = "https://api.groq.com/openai/v1/"
USER_AGENT = "EMS-Voice-Assessment/1.0"
MAX_RECORDING_BYTES = 3_000_000


class ServiceError(Exception):
    pass


def local_settings(root):
    values = {}
    path = Path(root) / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    extract_model: str
    review_model: str
    stt_model: str = "whisper-large-v3-turbo"

    @classmethod
    def load(cls, root):
        return cls.from_settings(local_settings(root))

    @classmethod
    def from_settings(cls, values):
        def get(name, default=""):
            return values.get(name, default).strip()

        model = get("GROQ_MODEL", "openai/gpt-oss-20b")
        return cls(get("GROQ_API_KEY"), model, get("GROQ_REVIEW_MODEL", model),
                   get("GROQ_STT_MODEL", "whisper-large-v3-turbo"))


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


class GroqClient:
    def __init__(self, config, opener=urlopen, sleep=time.sleep, *, timeout=15):
        self.config = config
        self.opener = opener
        self.sleep = sleep
        self.timeout = timeout

    @property
    def enabled(self):
        return bool(self.config.api_key)

    def post(self, endpoint, payload, content_type):
        if not self.enabled:
            raise ServiceError("Add GROQ_API_KEY to .env, then restart the app.")
        request = Request(BASE_URL + endpoint, data=payload, headers={
            "Authorization": "Bearer " + self.config.api_key,
            "Content-Type": content_type,
            "User-Agent": USER_AGENT,
        }, method="POST")
        for attempt in range(2):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ServiceError("Groq returned a response larger than the app can process.")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError
                return body
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After", "") if error.headers else ""
                error.close()
                if status in (401, 403):
                    raise ServiceError("Groq rejected the key or model permissions. Check your local settings.") from None
                if status not in (429, 500, 502, 503, 504):
                    raise ServiceError("Groq rejected the request. Check the configured model.") from None
                delay = 0.6 * (2 ** attempt) + random.uniform(0, 0.25)
                if retry_after.isdecimal():
                    delay = max(delay, min(int(retry_after), 3600))
                if attempt == 0 and delay <= 3:
                    self.sleep(delay)
                    continue
                message = "Groq rate limit reached." if status == 429 else "Groq is temporarily unavailable."
                raise ServiceError(message + " Wait, then retry.") from None
            except (TimeoutError, URLError, OSError):
                raise ServiceError("Groq could not be reached. Check the connection and retry.") from None
            except (ValueError, UnicodeError):
                raise ServiceError("Groq returned an unreadable response. Please retry.") from None

    def structured(self, model, name, instructions, data, schema):
        request = {"model": model, "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ], "max_completion_tokens": 1800, "response_format": {"type": "json_schema", "json_schema": {
            "name": name, "strict": True, "schema": schema,
        }}}
        body = self.post("chat/completions", json.dumps(request).encode(), "application/json")
        try:
            choice = body["choices"][0]
            if choice["finish_reason"] != "stop":
                raise ValueError
            result = json.loads(choice["message"]["content"])
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (KeyError, IndexError, TypeError, ValueError):
            raise ServiceError("Groq returned an incomplete or malformed structured response. Please retry.") from None

    def transcribe(self, wav: bytes) -> str:
        if len(wav) < 44 or len(wav) > MAX_RECORDING_BYTES:
            raise ValueError("Record a short audio reply of up to 3 MB.")
        if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            raise ValueError("The microphone recording must be a WAV file.")

        boundary = uuid4().hex
        parts = []
        for name, value in (("model", self.config.stt_model), ("response_format", "json"), ("language", "en")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.extend([
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="reply.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode(),
            wav,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        data = self.post("audio/transcriptions", b"".join(parts), f"multipart/form-data; boundary={boundary}")
        transcript = data.get("text")
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("No speech was recognized. Record again or type a reply.")
        return transcript.strip()
