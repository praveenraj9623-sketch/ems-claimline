"""Self-hosted LiveKit voice rooms for the mobile-browser demo."""

import argparse
import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from livekit import api, rtc
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .media_call import VoiceConversation
from .phone_records import BROWSER_CALL_ID, PhoneCall, PhoneRecords
from .provider import Config, GroqClient, local_settings
from .speech import DeepgramSpeech
from .workflow import Call

ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / "voice_web"
CONTROL = "http://127.0.0.1:5000"
LIVEKIT = "ws://127.0.0.1:7880"
KEY_FILE = Path(".livekit") / "dashboard.key"
LOGGER = logging.getLogger(__name__)
TOPIC = "ems-voice"
PHONE = "representative"


@dataclass
class Invite:
    id: str
    code: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires: float = field(default_factory=lambda: monotonic() + 600)
    status: str = "waiting"
    message: str = ""
    task: asyncio.Task | None = None


class JoinRequest(BaseModel):
    code: str = Field(min_length=43, max_length=43)


class BrowserCall(VoiceConversation):
    def __init__(self, sid, state, records, speech, extractor, room):
        super().__init__(sid, state, records, speech, extractor)
        self.room = room
        self.microphone = asyncio.get_running_loop().create_future()
        self.incoming = asyncio.Queue(maxsize=64)
        self.disconnected = asyncio.Event()
        room.on("track_subscribed", self.track_subscribed)
        room.on("data_received", self.data_received)
        room.on("participant_disconnected", self.participant_disconnected)
        room.on("disconnected", lambda _: self.disconnected.set())

    def track_subscribed(self, track, publication, participant):
        if (participant.identity == PHONE and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
                and not self.microphone.done()):
            self.microphone.set_result(track)

    def data_received(self, packet):
        if (packet.topic != TOPIC or packet.participant is None or packet.participant.identity != PHONE
                or len(packet.data) > 128 or self.incoming.full()):
            return
        try:
            data = json.loads(packet.data)
        except (ValueError, UnicodeError):
            return
        if isinstance(data, dict):
            self.incoming.put_nowait(data)

    def participant_disconnected(self, participant):
        if participant.identity == PHONE:
            self.disconnected.set()

    async def send(self, event, **message):
        data = {"event": event}
        if event == "media":
            data["audio"] = base64.b64encode(message["audio"]).decode("ascii")
        elif event == "mark":
            data["name"] = message["name"]
        await asyncio.wait_for(self.room.local_participant.publish_data(
            json.dumps(data), reliable=True, destination_identities=[PHONE], topic=TOPIC), 4)

    async def receive_media(self, recognizer):
        stream = rtc.AudioStream(self.microphone.result(), capacity=30, sample_rate=8000,
                                 num_channels=1, frame_size_ms=20)
        try:
            async for event in stream:
                await self.forward_audio(recognizer, bytes(event.frame.data))
        finally:
            await stream.aclose()

    async def wait_for_disconnect(self):
        async def receive_controls():
            while True:
                message = await self.incoming.get()
                if message.get("event") == "end":
                    return
                if message.get("event") == "mark" and isinstance(message.get("name"), str):
                    await self.mark(message["name"])

        control = asyncio.create_task(receive_controls())
        leaving = asyncio.create_task(self.disconnected.wait())
        try:
            complete, _ = await asyncio.wait((control, leaving), return_when=asyncio.FIRST_COMPLETED)
            for task in complete:
                await task
        finally:
            control.cancel()
            leaving.cancel()
            await asyncio.gather(control, leaving, return_exceptions=True)


def token(key, secret, sid, identity):
    phone = identity == PHONE
    grants = api.VideoGrants(room_join=True, room=sid, can_publish=phone,
                             can_publish_sources=["microphone"] if phone else [],
                             can_subscribe=not phone, can_publish_data=True)
    return (api.AccessToken(key, secret).with_identity(identity).with_grants(grants)
            .with_ttl(timedelta(minutes=2)).to_jwt())


def create_app(public_url, admin, key, secret, records, speech, extractor, reviewer, *,
               rtc_url=LIVEKIT, room_factory=rtc.Room):
    invites: dict[str, Invite] = {}

    @asynccontextmanager
    async def lifespan(_app):
        records.recover_exports()
        stop = threading.Event()
        review = threading.Thread(target=records.review_loop, args=(reviewer, stop), daemon=True)
        review.start()
        try:
            yield
        finally:
            pending = [invite.task for invite in invites.values() if invite.task and not invite.task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            stop.set()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(request):
        supplied = request.headers.get("x-ems-dashboard", "").encode()
        if not secrets.compare_digest(supplied, admin.encode()):
            raise HTTPException(404)

    @app.put("/live/dashboard/{sid}")
    async def create_invite(sid: str, request: Request):
        authorize(request)
        if not BROWSER_CALL_ID.fullmatch(sid):
            raise HTTPException(404)
        for old_id, invite in list(invites.items()):
            if invite.expires <= monotonic() and (invite.task is None or invite.task.done()):
                del invites[old_id]
        invite = invites.get(sid)
        if invite is None:
            invite = invites[sid] = Invite(sid)
        return {"link": f"{public_url}/live#call={sid}&code={invite.code}"}

    @app.get("/live/dashboard/{sid}")
    async def status(sid: str, request: Request):
        authorize(request)
        invite = invites.get(sid)
        if invite is None or (invite.status == "waiting" and invite.expires <= monotonic()):
            raise HTTPException(404)
        return {"status": invite.status, "message": invite.message}

    async def run_room(invite, ready):
        room = room_factory()
        state = PhoneCall(Call("groq"))
        try:
            session = BrowserCall(invite.id, state, records, speech, extractor, room)
            await room.connect(rtc_url, token(key, secret, invite.id, "agent"),
                               rtc.RoomOptions(connect_timeout=6))
            ready.set_result(True)
            async with asyncio.timeout(45):
                await session.microphone
            invite.status = "connected"
            await session.converse()
            if state.error is None and state.call.phase == "active" and not state.played:
                state.error = "The browser disconnected before the greeting played. Check the microphone and network."
        except asyncio.CancelledError:
            state.error = "The voice service shut down during the call."
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_result(False)
                state.error = "Could not connect to the local LiveKit server. Restart the voice service."
            elif not state.stream_started:
                state.error = "The phone microphone did not connect. Check microphone access and the Wi-Fi connection."
            else:
                state.error = "Live audio failed. Check the network and Deepgram settings, then start a new session."
            LOGGER.warning("Browser voice session failed (%s).", type(exc).__name__)
        finally:
            if not ready.done():
                ready.set_result(False)
            try:
                records.finish(invite.id, state)
            finally:
                invite.status = "failed" if state.error else "completed"
                invite.message = state.error or ""
                if room.isconnected():
                    with suppress(Exception):
                        await session.send("end")
                        await room.disconnect()

    @app.post("/live/join/{sid}")
    async def join(sid: str, payload: JoinRequest):
        invite = invites.get(sid)
        if (invite is None or invite.expires <= monotonic()
                or not secrets.compare_digest(invite.code.encode(), payload.code.encode())):
            raise HTTPException(404, "This link expired or is invalid. Create a new link on the dashboard.")
        if invite.status != "waiting":
            raise HTTPException(409, "This link was already used. Create a new link on the dashboard.")
        invite.status = "connecting"
        ready = asyncio.get_running_loop().create_future()
        invite.task = asyncio.create_task(run_room(invite, ready))
        if not await asyncio.shield(ready):
            raise HTTPException(503, "LiveKit could not start the session. Check the voice service terminal.")
        ws_url = re.sub(r"^http", "ws", public_url)
        return JSONResponse({"url": ws_url, "token": token(key, secret, sid, PHONE)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/live")
    async def page():
        return FileResponse(ASSETS / "index.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "microphone=(self), camera=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                       f"connect-src 'self' {re.sub(r'^http', 'ws', public_url)}; frame-ancestors 'none'",
        })

    @app.get("/live/client.js")
    async def javascript():
        return FileResponse(ASSETS / "client.js", media_type="application/javascript")

    @app.get("/live/livekit.js")
    async def sdk():
        return FileResponse(ROOT / "node_modules/livekit-client/dist/livekit-client.umd.js",
                            media_type="application/javascript")

    @app.websocket("/rtc")
    @app.websocket("/rtc/v1")
    async def relay(socket: WebSocket):
        target = f"{rtc_url}{socket.url.path}?{socket.url.query}"
        try:
            async with connect(target, open_timeout=5, close_timeout=1, proxy=None) as upstream:
                await socket.accept()

                async def upload():
                    while True:
                        packet = await socket.receive()
                        if packet["type"] == "websocket.disconnect":
                            return
                        await upstream.send(packet.get("bytes") if packet.get("bytes") is not None else packet["text"])

                async def download():
                    async for packet in upstream:
                        if isinstance(packet, bytes):
                            await socket.send_bytes(packet)
                        else:
                            await socket.send_text(packet)

                tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, WebSocketException, TimeoutError, WebSocketDisconnect):
            pass
        finally:
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    @app.get("/rtc/validate")
    @app.get("/rtc/v1/validate")
    async def validate(request: Request):
        url = re.sub(r"^ws", "http", rtc_url) + request.url.path
        try:
            async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
                response = await client.get(url, params=request.query_params)
            return Response(status_code=response.status_code)
        except httpx.HTTPError:
            return Response(status_code=503)

    return app


def main():
    parser = argparse.ArgumentParser(description="Start self-hosted LiveKit and the EMS browser voice service.")
    parser.add_argument("--lan-ip", required=True, type=ipaddress.IPv4Address, help="IPv4 address of the Windows PC.")
    parser.add_argument("--local", action="store_true", help="Use localhost for a test on this PC.")
    args = parser.parse_args()
    settings = local_settings(ROOT)
    config = Config.load(ROOT)
    public_url = CONTROL if args.local else settings.get("VOICE_PUBLIC_URL", "").rstrip("/")
    try:
        url = urlsplit(public_url)
        valid = (url.scheme == "https" and url.hostname and not url.username and not url.path
                 and not url.query and not url.fragment and "your-ngrok" not in url.hostname
                 and re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", public_url))
    except ValueError:
        valid = False
    if not args.local and not valid:
        parser.error("Set VOICE_PUBLIC_URL to the HTTPS forwarding origin printed by ngrok, or use --local.")
    deepgram = settings.get("DEEPGRAM_API_KEY", "").strip()
    if not deepgram or not config.api_key:
        parser.error("Set DEEPGRAM_API_KEY and GROQ_API_KEY in .env.")
    if not (ROOT / "node_modules/livekit-client/dist/livekit-client.umd.js").is_file():
        parser.error("Run npm.cmd ci in the project folder to install the browser client.")
    server = ROOT / ".livekit" / "livekit-server.exe"
    if not server.is_file():
        parser.error("Run .\\Start-LiveKit.ps1 to install and launch the Windows LiveKit server.")
    if not args.local and (args.lan_ip.is_loopback or args.lan_ip.is_unspecified):
        parser.error("Use the PC's Wi-Fi IPv4 address for a mobile demo.")

    admin, key, secret = secrets.token_urlsafe(32), secrets.token_hex(12), secrets.token_hex(32)
    server_config = {"port": 7880, "bind_addresses": ["127.0.0.1"],
                     "rtc": {"node_ip": str(args.lan_ip), "udp_port": 7882, "tcp_port": 7881,
                             "use_external_ip": False, "enable_loopback_candidate": args.local},
                     "keys": {key: secret}, "logging": {"level": "error"}}
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    key_file = ROOT / KEY_FILE
    worker = subprocess.Popen([str(server)], env={**os.environ, "LIVEKIT_CONFIG": json.dumps(server_config)})
    try:
        with httpx.Client(timeout=.4, trust_env=False) as client:
            for _ in range(25):
                if worker.poll() is not None:
                    parser.error("LiveKit exited. Check for an existing server on ports 7880–7882.")
                try:
                    if client.get("http://127.0.0.1:7880/").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                threading.Event().wait(.2)
            else:
                parser.error("LiveKit did not start. Check port 7880 and the terminal output.")
        key_file.write_text(admin, encoding="ascii")
        records = PhoneRecords(ROOT / "browser_calls", BROWSER_CALL_ID)
        speech = DeepgramSpeech(deepgram, settings.get("DEEPGRAM_STT_MODEL", "nova-3"),
                                settings.get("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en"), stt_encoding="linear16")
        app = create_app(public_url, admin, key, secret, records, speech, GroqClient(config, timeout=4),
                         GroqClient(config))
        uvicorn.run(app, host="127.0.0.1", port=5000, access_log=False, proxy_headers=False,
                    ws="websockets-sansio", ws_max_size=65536)
    finally:
        if key_file.exists() and key_file.read_text(encoding="ascii") == admin:
            key_file.unlink(missing_ok=True)
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)


if __name__ == "__main__":
    main()
