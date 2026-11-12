"""Signed answer, status, and media endpoints for Twilio Programmable Voice."""

import argparse
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qsl, urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from starlette.datastructures import FormData, UploadFile
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from .media_call import MediaCall
from .outbound import ACCOUNT_SID, OUTBOUND_PATH, TERMINAL_STATUSES, TWILIO_STATUSES, OutboundCalls
from .phone_records import CALL_SID, PhoneCall, PhoneRecords
from .provider import Config, GroqClient, local_settings
from .speech import DEFAULT_TTS_MODEL, DeepgramSpeech
from .workflow import Call

ROOT = Path(__file__).resolve().parents[1]
PORT = 5000
LOGGER = logging.getLogger(__name__)


def request_target(scope: Mapping) -> str:
    path = scope.get("raw_path") or scope["path"].encode("ascii")
    query = scope.get("query_string", b"")
    return (path + (b"?" + query if query else b"")).decode("ascii")


def create_app(public_url: str, auth_token: str, records: PhoneRecords, speech: DeepgramSpeech,
               extractor: GroqClient, reviewer: GroqClient, *,
               outbound: OutboundCalls | None = None, account_sid: str = "") -> FastAPI:
    validator = RequestValidator(auth_token)
    wss_origin = "wss://" + public_url.removeprefix("https://")
    active: dict[str, MediaCall] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        records.recover_exports()
        stop = Event()
        worker = Thread(target=records.review_loop, args=(reviewer, stop), daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=1)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    async def signed_form(request: Request) -> FormData:
        if request.headers.get("content-type", "").partition(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(403)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 64 * 1024:
                raise HTTPException(413)
            body.extend(chunk)
        try:
            params: list[tuple[str, str | UploadFile]] = [
                *parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=250),
            ]
            form = FormData(params)
            target = request_target(request.scope)
        except (UnicodeError, ValueError):
            raise HTTPException(400) from None
        if not validator.validate(public_url + target, form, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(403)
        return form

    def call_sid(form: FormData, direction: str) -> str:
        sid = str(form.get("CallSid", ""))
        if not CALL_SID.fullmatch(sid):
            raise HTTPException(400)
        if form.get("Direction", "inbound") != direction:
            raise HTTPException(403)
        return sid

    def outbound_account(form: FormData) -> OutboundCalls:
        if outbound is None or not account_sid or form.get("AccountSid") != account_sid:
            raise HTTPException(403)
        return outbound

    def connect_call(sid: str, *, ended: bool = False) -> Response:
        response = VoiceResponse()
        if not ended:
            state = records.load(sid)
            if state is None:
                state = PhoneCall(Call("groq"))
                records.save(sid, state)
            if not state.finished:
                response.connect().stream(url=f"{wss_origin}/voice/media/{sid}")
        response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/voice/incoming")
    async def incoming(request: Request) -> Response:
        return connect_call(call_sid(await signed_form(request), "inbound"))

    @app.post("/voice/status")
    async def status(request: Request) -> Response:
        form = await signed_form(request)
        sid = call_sid(form, "inbound")
        if form.get("CallStatus") in TERMINAL_STATUSES:
            if sid in active:
                active[sid].terminal.set()
            else:
                state = records.load(sid)
                if state is None and form.get("Direction") == "inbound":
                    state = PhoneCall(Call("groq", transcript=[]))
                if state is not None:
                    records.finish(sid, state)
        return Response(status_code=204)

    @app.post(OUTBOUND_PATH + "/{attempt}/ready")
    async def outbound_ready(request: Request, attempt: str) -> Response:
        calls = outbound_account(await signed_form(request))
        if not calls.ready(attempt):
            raise HTTPException(404)
        return Response(status_code=204)

    @app.post(OUTBOUND_PATH + "/{attempt}/answer")
    async def outbound_answer(request: Request, attempt: str) -> Response:
        form = await signed_form(request)
        calls = outbound_account(form)
        sid = call_sid(form, "outbound-api")
        state = calls.observe(attempt, sid, "in-progress")
        if state is None or state.sid != sid:
            raise HTTPException(404)
        return connect_call(sid, ended=not state.active)

    @app.post(OUTBOUND_PATH + "/{attempt}/status")
    async def outbound_status(request: Request, attempt: str) -> Response:
        form = await signed_form(request)
        calls = outbound_account(form)
        sid = call_sid(form, "outbound-api")
        status = str(form.get("CallStatus", ""))
        if status not in TWILIO_STATUSES:
            raise HTTPException(400)
        raw_sequence = str(form.get("SequenceNumber", ""))
        sequence = int(raw_sequence) if raw_sequence.isascii() and raw_sequence.isdecimal() and len(raw_sequence) <= 10 else -1
        attempt_state = calls.observe(attempt, sid, status, sequence)
        if attempt_state is None or attempt_state.sid != sid:
            raise HTTPException(404)
        if status in TERMINAL_STATUSES and attempt_state.status == status:
            if sid in active:
                active[sid].terminal.set()
            else:
                state = records.load(sid)
                if state is None and status == "completed":
                    state = PhoneCall(Call("groq", transcript=[]))
                if state is not None:
                    if not state.finished and not state.stream_started and status == "completed":
                        state.error = "Twilio ended the call before live audio opened. Check the Media Stream in Twilio Debugger."
                    records.finish(sid, state)
        return Response(status_code=204)

    @app.websocket("/voice/media/{sid}")
    async def media(socket: WebSocket, sid: str) -> None:
        # Use the public handshake URL, including Twilio's WSS and trailing-slash variants.
        expected_path = f"/voice/media/{sid}"
        target = request_target(socket.scope)
        signature = socket.headers.get("x-twilio-signature", "")
        if (not CALL_SID.fullmatch(sid) or target != expected_path or not any(
                validator.validate(origin + target + suffix, {}, signature)
                for origin in (public_url, wss_origin) for suffix in ("", "/"))):
            await socket.close(code=1008)
            return
        state = records.load(sid)
        if state is None or state.finished or state.stream_started or sid in active:
            await socket.close(code=1008)
            return
        session = MediaCall(socket, sid, state, records, speech, extractor)
        active[sid] = session
        try:
            await socket.accept()
            await session.run()
        except WebSocketDisconnect:
            pass
        except Exception:
            state.error = "The live audio stream failed or timed out. Check Twilio and Deepgram, then redial."
            LOGGER.warning("Live audio stream failed for %s.", sid)
        finally:
            try:
                records.finish(sid, state)
            finally:
                active.pop(sid, None)
                with suppress(RuntimeError, WebSocketDisconnect):
                    await socket.close()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the EMS phone assessment service.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Serve phone calls and run post-call reviews.")
    retry = commands.add_parser("retry-review", help="Queue a failed review for the running service.")
    retry.add_argument("call_sid")
    release = commands.add_parser("release-call", help="Release a stuck outbound attempt after checking Twilio.")
    release.add_argument("attempt_id")
    args = parser.parse_args()
    records = PhoneRecords(ROOT / "phone_calls")

    if args.command == "retry-review":
        try:
            records.queue_retry(args.call_sid)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Review queued for {args.call_sid}.")
        return
    if args.command == "release-call":
        try:
            OutboundCalls(records.root).release(args.attempt_id)
        except ValueError as exc:
            parser.error(str(exc))
        print("Call attempt released.")
        return

    settings = local_settings(ROOT)
    config = Config.load(ROOT)
    public_url = settings.get("VOICE_PUBLIC_URL", "").strip().rstrip("/")
    auth_token = settings.get("TWILIO_AUTH_TOKEN", "").strip()
    deepgram_key = settings.get("DEEPGRAM_API_KEY", "").strip()
    try:
        origin = urlsplit(public_url)
        valid_origin = (origin.scheme == "https" and origin.hostname and not origin.username
                        and not origin.path and not origin.query and not origin.fragment)
    except ValueError:
        valid_origin = False
    if not valid_origin or "your-ngrok" in public_url:
        parser.error("Set VOICE_PUBLIC_URL to your tunnel's public HTTPS origin in .env.")
    if not auth_token or not config.api_key or not deepgram_key:
        parser.error("Set TWILIO_AUTH_TOKEN, DEEPGRAM_API_KEY and GROQ_API_KEY in .env or the local environment.")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    speech = DeepgramSpeech(deepgram_key, settings.get("DEEPGRAM_STT_MODEL", "nova-3"),
                            settings.get("DEEPGRAM_TTS_MODEL", "").strip() or DEFAULT_TTS_MODEL)
    account_sid = settings.get("TWILIO_ACCOUNT_SID", "").strip()
    outbound = OutboundCalls(records.root) if ACCOUNT_SID.fullmatch(account_sid) else None
    app = create_app(public_url, auth_token, records, speech, GroqClient(config, timeout=4), GroqClient(config),
                     outbound=outbound, account_sid=account_sid)
    uvicorn.run(app, host="127.0.0.1", port=PORT, ws="websockets-sansio", ws_max_size=65536,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
