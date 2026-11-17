"""Minimal Groq chat client (standard library only) with retries and clear errors."""

import json
import time
import urllib.error
import urllib.request

BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"


class GroqError(Exception):
    pass


class GroqClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, base_url: str = BASE_URL,
                 timeout: float = 25, attempts: int = 3, sleep=time.sleep):
        self.api_key, self.model = (api_key or "").strip(), model or DEFAULT_MODEL
        self.base_url, self.timeout, self.sleep = base_url.rstrip("/"), timeout, sleep
        self.attempts = max(1, attempts)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "User-Agent": "claimline/1.0"})
        last = "Groq could not be reached."
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read(2_000_000))
            except urllib.error.HTTPError as error:
                body = error.read(2000).decode("utf-8", "replace")
                error.close()
                if error.code in (401, 403):
                    raise GroqError("Groq rejected the API key. Check GROQ_API_KEY.") from None
                if error.code == 400:
                    raise GroqError(f"Groq rejected the request (400): {body[:300]}") from None
                last = f"Groq returned HTTP {error.code}."
            except (urllib.error.URLError, TimeoutError, OSError):
                last = "Groq could not be reached. Check the internet connection."
            except ValueError:
                last = "Groq returned an unreadable response."
            if attempt < self.attempts - 1:
                self.sleep(0.8 * (attempt + 1))
        raise GroqError(last)

    def json_object(self, system: str, user: str, schema: dict, name: str = "result") -> dict:
        """Ask for a JSON object matching schema. Falls back to plain JSON mode if the model
        does not support strict schemas."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        base = {"model": self.model, "messages": messages, "temperature": 0, "max_completion_tokens": 1500}
        attempts = (
            {"response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}},
            {"response_format": {"type": "json_object"}},
        )
        error = None
        for extra in attempts:
            try:
                body = self._post({**base, **extra})
                content = body["choices"][0]["message"]["content"]
                data = json.loads(content)
                if isinstance(data, dict):
                    return data
                error = GroqError("Groq returned JSON that was not an object.")
            except GroqError as exc:
                error = exc
                if "400" not in str(exc):
                    raise
            except (KeyError, IndexError, TypeError, ValueError):
                error = GroqError("Groq returned an unreadable structured answer.")
        raise error or GroqError("Groq did not return a result.")
