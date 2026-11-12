"""Deepgram speech services for phone audio and browser playback."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect

SPEAK_URL = "https://api.deepgram.com/v1/speak"
DEFAULT_TTS_MODEL = "aura-2-thalia-en"
SPEAK_TIMEOUT = httpx.Timeout(7, connect=4)
MAX_BROWSER_AUDIO_BYTES = 2_000_000


class SpeechError(Exception):
    """A speech provider or transport failed during the call."""


def browser_audio(api_key: str, voice: str, text: str) -> bytes:
    """Synthesize a browser-playable MP3 without changing the phone audio format."""
    try:
        with httpx.stream("POST", SPEAK_URL, params={"model": voice, "encoding": "mp3"},
                          json={"text": text}, headers={"Authorization": f"Token {api_key}"},
                          timeout=SPEAK_TIMEOUT) as response:
            if response.is_error:
                raise SpeechError(f"Deepgram synthesis returned HTTP {response.status_code}.")
            if not response.headers.get("Content-Type", "").startswith("audio/"):
                raise SpeechError("Deepgram synthesis did not return audio.")
            audio = bytearray()
            for chunk in response.iter_bytes(16_384):
                audio.extend(chunk)
                if len(audio) > MAX_BROWSER_AUDIO_BYTES:
                    raise SpeechError("The spoken reply exceeded the audio size limit.")
    except httpx.HTTPError:
        raise SpeechError("Deepgram could not be reached. Retry the audio when the connection is available.") from None
    if not audio:
        raise SpeechError("Deepgram returned an empty spoken reply.")
    return bytes(audio)


class LiveTranscription:
    def __init__(self, connection: ClientConnection):
        self.connection = connection

    async def audio(self, data: bytes) -> None:
        await self.connection.send(data)

    async def control(self, event: str) -> None:
        await self.connection.send(json.dumps({"type": event}))

    async def events(self) -> AsyncIterator[dict]:
        async for packet in self.connection:
            message = json.loads(packet)
            if isinstance(message, dict):
                if message.get("type") == "Error":
                    raise SpeechError("Deepgram rejected the live audio stream.")
                yield message


class DeepgramSpeech:
    def __init__(self, api_key: str, stt_model: str, voice: str, *,
                 stt_encoding: str = "mulaw", stt_sample_rate: int = 8000):
        self.headers = {"Authorization": f"Token {api_key}"}
        self.stt_query = urlencode({
            "model": stt_model, "language": "en-US", "encoding": stt_encoding,
            "sample_rate": stt_sample_rate, "channels": 1, "interim_results": "true",
            "smart_format": "true", "endpointing": 500, "utterance_end_ms": 1100,
        })
        self.tts_params = {"model": voice, "encoding": "mulaw", "sample_rate": 8000, "container": "none"}

    @asynccontextmanager
    async def listen(self):
        async with connect(f"wss://api.deepgram.com/v1/listen?{self.stt_query}",
                           additional_headers=self.headers, open_timeout=5, close_timeout=1) as connection:
            yield LiveTranscription(connection)

    @asynccontextmanager
    async def synthesizer(self):
        async with httpx.AsyncClient(headers=self.headers, timeout=SPEAK_TIMEOUT) as client:
            async def audio(text: str) -> AsyncIterator[bytes]:
                async with client.stream("POST", SPEAK_URL,
                                         params=self.tts_params, json={"text": text}) as response:
                    if response.is_error:
                        raise SpeechError(f"Deepgram synthesis returned HTTP {response.status_code}.")
                    if "audio/" not in response.headers.get("Content-Type", ""):
                        raise SpeechError("Deepgram synthesis did not return audio.")
                    # About 100 ms per frame. The REST response contains headerless mu-law.
                    async for chunk in response.aiter_bytes(800):
                        yield chunk

            yield audio
