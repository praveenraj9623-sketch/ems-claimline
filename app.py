"""Local chat and phone dashboard for the EMS assessment."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import streamlit as st

from ems_voice.outbound import Attempt, DialSettings, OutboundCalls, place_call
from ems_voice.phone_records import BROWSER_CALL_ID, CALL_SID, PhoneRecords
from ems_voice.provider import Config, GroqClient, ServiceError, local_settings
from ems_voice.records import read_output, review_saved, save_outputs
from ems_voice.workflow import CLAIM, Call, reply

ROOT = Path(__file__).resolve().parent
BROWSER_CALLS = ROOT / "browser_calls"
VOICE_BACKEND = "http://127.0.0.1:5000"
STATUSES = {
    "requesting": "Requesting call",
    "unconfirmed": "Dial request unconfirmed",
    "queued": "Queued by Twilio",
    "initiated": "Dialing",
    "ringing": "Ringing",
    "in-progress": "Connected",
    "completed": "Call ended",
    "no-answer": "No answer",
    "busy": "Line busy",
    "canceled": "Canceled",
    "failed": "Call failed",
    "request-failed": "Call request failed",
    "released": "Call attempt released",
}
FAILURES = {
    "no-answer": "Your mobile did not answer. Click again when you’re ready to pick up.",
    "busy": "The destination was busy. Make sure your mobile can receive a call, then try again.",
    "canceled": "Twilio canceled the call before it completed. Check the Twilio Calls Log.",
    "failed": "Twilio could not connect the call. Check the Calls Log for the error code and verify your account’s outbound calling permissions.",
}


@dataclass
class ChatSession:
    id: str = field(default_factory=lambda: uuid4().hex)
    call: Call = field(default_factory=lambda: Call("groq"))
    pending: str = ""
    error: str = ""


@st.cache_resource
def call_store(directory: Path) -> OutboundCalls:
    return OutboundCalls(directory)


def show_thread(turns: list[dict]) -> None:
    for turn in turns:
        agent = turn["speaker"] == "agent"
        name = "EMS billing agent" if agent else "Insurance representative"
        avatar = ":material/support_agent:" if agent else ":material/person:"
        with st.chat_message(name, avatar=avatar):
            st.caption(name + (" · Partial playback" if turn.get("playback") == "partial" else ""))
            st.text(turn["text"], width="stretch")


def show_results(folder: Path, result: dict, *, mode: str, reviewer: GroqClient | None = None) -> None:
    result_card, review_card = st.columns(2, gap="medium")
    with result_card.container(border=True):
        st.subheader("Claim result")
        st.json(result)
    with review_card.container(border=True):
        st.subheader("AI reviewer")
        review = read_output(folder, "review.json")
        review_error = read_output(folder, "review_error.json")
        if reviewer is not None and review is None and review_error is None:
            with st.spinner("Reviewing the conversation…"):
                review_saved(folder, reviewer)
            review = read_output(folder, "review.json")
            review_error = read_output(folder, "review_error.json")
        if review is not None:
            st.json(review)
        elif (folder / "review_retry.request").exists():
            st.info("Review retry queued.")
        elif review_error:
            st.error(review_error["error"])
            if st.button("Retry review", key=f"{mode}_review_{folder.name}"):
                if reviewer is not None:
                    with st.spinner("Reviewing the conversation…"):
                        review_saved(folder, reviewer, retry=True)
                else:
                    try:
                        records = PhoneRecords(folder.parent, BROWSER_CALL_ID if mode == "browser" else CALL_SID)
                        records.queue_retry(folder.name)
                    except ValueError:
                        pass  # The backend may already have completed the retry.
                st.rerun()
        else:
            st.info("Review in progress.")


def chat_panel() -> None:
    if "ems_chat" not in st.session_state:
        st.session_state.ems_chat = ChatSession()
    session: ChatSession = st.session_state.ems_chat
    provider = GroqClient(Config.load(ROOT))
    if session.call.phase == "hold":
        st.info("On hold. Send a message when you’re back.")
    with st.container(height=380, border=True, key=f"thread_{session.id}", autoscroll=True):
        show_thread(session.call.records())
    if session.call.phase == "ended":
        st.success("Conversation complete.")
        folder = ROOT / "chat_calls" / session.id
        save_outputs(folder, session.call, session.call.records())
        show_results(folder, read_output(folder, "claim_result.json"), mode="chat", reviewer=provider)
        if st.button("New chat", key="new_chat"):
            st.session_state.ems_chat = ChatSession()
            st.rerun()
        return

    if not provider.enabled:
        st.warning("Set GROQ_API_KEY in your local .env file to send chat replies.")
    retry = False
    if session.error:
        st.error(session.error)
        if session.pending:
            st.caption("Unsent reply")
            st.text(session.pending, width="stretch")
            retry = st.button("Retry reply", key="retry_chat_reply", disabled=not provider.enabled)

    prompt = st.chat_input("Reply as the insurance representative", key=f"chat_{session.id}",
                           max_chars=1600, disabled=not provider.enabled)
    message = session.pending if retry else prompt
    if isinstance(message, str) and message.strip():
        try:
            with st.spinner("Preparing the agent’s reply…"):
                reply(session.call, message, provider)
        except ServiceError as error:
            session.error, session.pending = str(error), message
        else:
            session.error = session.pending = ""
        st.rerun()


def show_phone_record(attempt: Attempt, directory: Path) -> None:
    if not attempt.sid:
        return
    show_voice_record(directory / attempt.sid, attempt.status, mode="phone")


def show_voice_record(folder: Path, status: str, *, mode: str) -> None:
    result = read_output(folder, "claim_result.json")
    transcript = read_output(folder, "transcript.json")
    call_error = read_output(folder, "call_error.json")
    if call_error:
        st.error(call_error["error"])
    if transcript is None or result is None:
        if status == "completed":
            st.info("Waiting for the voice service to finish saving the call.")
        return
    show_results(folder, result, mode=mode)
    with st.expander("Phone transcript" if mode == "phone" else "Voice transcript", expanded=True):
        if transcript:
            with st.container(height=320, border=False):
                show_thread(transcript)
        else:
            st.info("No speech was captured on this call.")


@st.fragment(run_every="2s")
def twilio_panel() -> None:
    directory = ROOT / "phone_calls"
    calls = call_store(directory)
    attempt = calls.latest()
    try:
        settings = DialSettings.from_env(local_settings(ROOT))
    except ValueError as error:
        settings = None
        st.warning(str(error))

    with st.container(border=True):
        st.metric("Call status", STATUSES[attempt.status] if attempt else "Ready")
        if settings:
            st.caption(f"Mobile ending in {settings.to_number[-4:]}")
        if st.button("Call my phone", type="primary", width="stretch",
                     key=f"dial_after_{attempt.id if attempt else 'first'}",
                     disabled=settings is None or bool(attempt and attempt.active)):
            if settings:
                with st.spinner("Requesting one call from Twilio…"):
                    place_call(calls, settings, attempt.id if attempt else None)
                st.rerun()
        if attempt and attempt.status == "ringing":
            st.info("Answer the incoming call on your mobile.")

    if attempt:
        if attempt.status in FAILURES:
            st.error(FAILURES[attempt.status])
        if attempt.message and attempt.status != "released":
            st.error(attempt.message)
        if attempt.active and (attempt.status == "unconfirmed" or time.time() - attempt.updated_at > 60):
            st.warning("If the Twilio Calls Log confirms there is no active call, release this attempt from PowerShell.")
            st.code(f".\\.venv\\Scripts\\python.exe -m ems_voice.phone release-call {attempt.id}",
                    language="powershell")
        show_phone_record(attempt, directory)


def dashboard_request(method: str, sid: str) -> dict:
    from ems_voice.live_voice import KEY_FILE

    try:
        secret = (ROOT / KEY_FILE).read_text(encoding="ascii")
        with httpx.Client(timeout=2, trust_env=False) as client:
            response = client.request(method, f"{VOICE_BACKEND}/live/dashboard/{sid}",
                                      headers={"x-ems-dashboard": secret})
            response.raise_for_status()
            return response.json()
    except FileNotFoundError as exc:
        raise ValueError("Start the LiveKit voice service with Start-LiveKit.ps1.") from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError("The voice link is no longer available. Create a new link.") from exc
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise ValueError("The local voice service is unavailable. Check its terminal and restart it.") from exc


@st.fragment(run_every="2s")
def browser_voice_panel() -> None:
    sid, link = st.session_state.get("voice_invite", ("", ""))
    status, message = "ready", ""
    if sid:
        try:
            state = dashboard_request("GET", sid)
            status, message = state["status"], state["message"]
        except ValueError as error:
            status, message = "expired", str(error)

    labels = {"ready": "Ready", "waiting": "Waiting for you to join", "connecting": "Connecting",
              "connected": "Connected", "completed": "Call ended", "failed": "Call failed",
              "expired": "Link unavailable"}
    with st.container(border=True):
        st.metric("Call status", labels[status])
        st.caption("Join on your phone from the same Wi-Fi as this PC.")
        if st.button("Create voice link", type="primary", width="stretch", key="create_voice_link",
                     disabled=status in {"connecting", "connected"}):
            try:
                sid = sid if status == "waiting" else uuid4().hex
                created = dashboard_request("PUT", sid)
                st.session_state.voice_invite = sid, created["link"]
            except ValueError as error:
                st.error(str(error))
            else:
                st.rerun()
        if status == "waiting":
            st.link_button("Open voice link", link, width="stretch")
            st.code(link, language=None)
            st.caption("Copy the link to your phone. It expires in 10 minutes and works once.")
        if message:
            st.error(message)
    if sid and status in {"completed", "failed", "expired"}:
        show_voice_record(BROWSER_CALLS / sid, status, mode="browser")


st.set_page_config(page_title="EMS Claim Desk", page_icon=":material/phone_in_talk:", layout="centered")
st.title("EMS Claim Desk")
st.caption(f"Cedar Vale EMS · Synthetic claim {CLAIM['claim_number']} · Service date {CLAIM['date_of_service']}")
chat_tab, phone_tab = st.tabs(["Chat", "Phone Call"])
with chat_tab:
    chat_panel()
with phone_tab:
    if local_settings(ROOT).get("VOICE_TRANSPORT", "").lower() == "twilio":
        twilio_panel()
    else:
        browser_voice_panel()
