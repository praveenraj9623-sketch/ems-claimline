"""Loopback-only transport for continuous browser microphone calls."""

import argparse
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .media_call import VoiceConversation
from .phone_records import PhoneCall, PhoneRecords
from .provider import Config, GroqClient, local_settings
from .records import read_output, review_saved
from .speech import DEFAULT_TTS_MODEL, DeepgramSpeech
from .workflow import Call, latest_readback

ROOT = Path(__file__).resolve().parents[1]
PORT = 5001
HOSTS = frozenset((f"127.0.0.1:{PORT}", f"localhost:{PORT}"))
ORIGINS = frozenset(("http://127.0.0.1:8501", "http://localhost:8501"))
LOGGER = logging.getLogger(__name__)


class BrowserCall(VoiceConversation):
    def __init__(self, socket: WebSocket, sid: str, state: PhoneCall, records: PhoneRecords,
                 speech: DeepgramSpeech, extractor: GroqClient):
        super().__init__(sid, state, records, speech, extractor)
        self.socket = socket

    async def send(self, event: str, **data) -> None:
        if event == "media":
            await asyncio.wait_for(self.socket.send_bytes(data["audio"]), 4)
        else:
            await asyncio.wait_for(self.socket.send_json({"event": event, **data}), 4)

    async def publish_transcript(self) -> None:
        await self.send("transcript", turns=self.state.call.records(), phase=self.state.call.phase)

    async def receive_media(self, recognizer) -> None:
        while True:
            message = await self.socket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            audio = message.get("bytes")
            if audio is not None:
                if not 0 < len(audio) <= 8192 or len(audio) % 2:
                    raise ValueError("Invalid microphone frame.")
                await self.forward_audio(recognizer, audio)
                continue
            control = json.loads(message.get("text") or "{}")
            if not isinstance(control, dict):
                raise ValueError("Invalid browser control frame.")
            if control.get("event") == "end":
                return
            if control.get("event") == "mark" and isinstance(control.get("name"), str):
                await self.mark(control["name"])


def create_app(records: PhoneRecords, speech: DeepgramSpeech, extractor: GroqClient,
               reviewer: GroqClient) -> FastAPI:
    active = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        records.recover_exports()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ready"}

    @app.websocket("/voice")
    async def voice(socket: WebSocket) -> None:
        nonlocal active
        if socket.headers.get("host") not in HOSTS or socket.headers.get("origin") not in ORIGINS:
            await socket.close(code=1008)
            return
        await socket.accept()
        if active:
            await socket.send_json({"event": "error", "message": "A live voice call is already open on this PC."})
            await socket.close()
            return
        active = True
        sid = uuid4().hex
        state = PhoneCall(Call("groq"))
        session = BrowserCall(socket, sid, state, records, speech, extractor)
        try:
            try:
                await session.converse()
            except WebSocketDisconnect:
                pass
            except Exception:
                state.error = "The live audio connection failed. Check the Deepgram configuration and try again."
                LOGGER.warning("Browser voice session %s failed.", sid)
            records.finish(sid, state)
            with suppress(RuntimeError, WebSocketDisconnect, TimeoutError):
                await session.send("finishing")
            directory = records.root / sid
            await asyncio.to_thread(review_saved, directory, reviewer,
                                    read_back_finished=latest_readback(state.call) in state.played)
            with suppress(RuntimeError, WebSocketDisconnect, TimeoutError):
                await session.send("result", turns=read_output(directory, "transcript.json"),
                                   claim=read_output(directory, "claim_result.json"),
                                   review=read_output(directory, "review.json"),
                                   review_error=read_output(directory, "review_error.json"),
                                   error=state.error)
        finally:
            active = False
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    settings = local_settings(ROOT)
    config = Config.load(ROOT)
    key = settings.get("DEEPGRAM_API_KEY", "").strip()
    if not config.api_key or not key:
        parser.error("Set GROQ_API_KEY and DEEPGRAM_API_KEY in the project's .env file.")
    speech = DeepgramSpeech(key, settings.get("DEEPGRAM_STT_MODEL", "").strip() or "nova-3",
                            settings.get("DEEPGRAM_TTS_MODEL", "").strip() or DEFAULT_TTS_MODEL,
                            stt_encoding="linear16", stt_sample_rate=16000)
    records = PhoneRecords(ROOT / "browser_calls", re.compile(r"[0-9a-f]{32}\Z"))
    app = create_app(records, speech, GroqClient(config, timeout=4), GroqClient(config))
    uvicorn.run(app, host="127.0.0.1", port=PORT, ws="websockets-sansio",
                ws_max_size=65536, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
