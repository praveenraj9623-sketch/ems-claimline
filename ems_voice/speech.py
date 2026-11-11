"""Deepgram live transcription and streaming 8 kHz telephone synthesis."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect


class SpeechError(Exception):
    """A speech provider or transport failed during the call."""


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
    def __init__(self, api_key: str, stt_model: str, voice: str):
        self.headers = {"Authorization": f"Token {api_key}"}
        self.stt_query = urlencode({
            "model": stt_model, "language": "en-US", "encoding": "mulaw",
            "sample_rate": 8000, "channels": 1, "interim_results": "true",
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
        async with httpx.AsyncClient(headers=self.headers, timeout=httpx.Timeout(7, connect=4)) as client:
            async def audio(text: str) -> AsyncIterator[bytes]:
                async with client.stream("POST", "https://api.deepgram.com/v1/speak",
                                         params=self.tts_params, json={"text": text}) as response:
                    if response.is_error:
                        raise SpeechError(f"Deepgram synthesis returned HTTP {response.status_code}.")
                    if "audio/" not in response.headers.get("Content-Type", ""):
                        raise SpeechError("Deepgram synthesis did not return audio.")
                    # About 100 ms per frame. The REST response contains headerless mu-law.
                    async for chunk in response.aiter_bytes(800):
                        yield chunk

            yield audio
