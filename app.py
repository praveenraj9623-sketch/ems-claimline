"""EMS ClaimLine dashboard for text, browser voice, and Twilio calls."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import streamlit as st

from ems_voice.outbound import Attempt, DialSettings, OutboundCalls, place_call
from ems_voice.phone_records import PhoneRecords
from ems_voice.provider import Config, GroqClient, ServiceError, local_settings
from ems_voice.records import read_output, review_saved, save_outputs
from ems_voice.speech import DEFAULT_TTS_MODEL, SpeechError, browser_audio
from ems_voice.workflow import CLAIM, Call, reply

ROOT = Path(__file__).resolve().parent
GREETING_DELAY_SECONDS = 2.0
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
    recording: bytes | None = None
    speak_reply: bool = False
    audio: dict[int, bytes] = field(default_factory=dict)
    autoplay: int | None = None
    audio_error: str = ""
    audio_retry: int | None = None


@st.cache_resource
def call_store(directory: Path) -> OutboundCalls:
    return OutboundCalls(directory)


def show_thread(turns: list[dict], session: ChatSession | None = None) -> None:
    for turn in turns:
        agent = turn["speaker"] == "agent"
        name = "EMS billing agent" if agent else "Insurance representative"
        avatar = ":material/support_agent:" if agent else ":material/person:"
        with st.chat_message(name, avatar=avatar):
            st.caption(name + (" · Partial playback" if turn.get("playback") == "partial" else ""))
            st.text(turn["text"], width="stretch")
            if session is not None and turn["turn"] in session.audio:
                st.audio(session.audio[turn["turn"]], format="audio/mpeg", autoplay=session.autoplay == turn["turn"])
    if session is not None:
        session.autoplay = None


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
                        records = PhoneRecords(folder.parent)
                        records.queue_retry(folder.name)
                    except ValueError:
                        pass  # The backend may already have completed the retry.
                st.rerun()
        else:
            st.info("Review in progress.")


def synthesize_turn(session: ChatSession, turn: int, settings: dict) -> None:
    try:
        key = settings.get("DEEPGRAM_API_KEY", "").strip()
        if not key:
            raise SpeechError("Set DEEPGRAM_API_KEY to enable spoken replies. The text reply is available.")
        model = settings.get("DEEPGRAM_TTS_MODEL", "").strip() or DEFAULT_TTS_MODEL
        session.audio[turn] = browser_audio(key, model, session.call.transcript[turn - 1].text)
    except SpeechError as error:
        session.audio_error, session.audio_retry = str(error), turn
    else:
        session.audio_error, session.audio_retry = "", None
        session.autoplay = turn


def chat_panel(mode: str = "chat") -> None:
    session_key = f"ems_{mode}"
    if session_key not in st.session_state:
        st.session_state[session_key] = ChatSession()
    session: ChatSession = st.session_state[session_key]
    voice_mode = mode == "browser"
    settings = local_settings(ROOT)
    provider = GroqClient(Config.load(ROOT))
    if voice_mode:
        st.caption("Record a reply or type one. The agent responds in text and audio.")
    if voice_mode and len(session.call.transcript) == 1 and not session.audio and not session.audio_error:
        greeting_at = time.monotonic() + GREETING_DELAY_SECONDS
        with st.spinner("Preparing the greeting…"):
            synthesize_turn(session, 1, settings)
            if session.autoplay == 1:
                remaining = greeting_at - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
    if session.call.phase == "hold":
        st.info("On hold. Send a message when you’re back.")
    with st.container(height=380, border=True, key=f"thread_{session.id}", autoscroll=True):
        show_thread(session.call.records(), session)
    if session.audio_error:
        st.warning(session.audio_error)
        if session.audio_retry is not None and settings.get("DEEPGRAM_API_KEY", "").strip():
            if st.button("Retry audio", key=f"retry_audio_{session.id}"):
                with st.spinner("Preparing the audio…"):
                    synthesize_turn(session, session.audio_retry, settings)
                st.rerun()
    if session.call.phase == "ended":
        st.success("Conversation complete.")
        folder = ROOT / "chat_calls" / session.id
        save_outputs(folder, session.call, session.call.records())
        show_results(folder, read_output(folder, "claim_result.json"), mode=mode, reviewer=provider)
        if st.button("New conversation" if voice_mode else "New chat", key=f"new_{mode}"):
            st.session_state[session_key] = ChatSession()
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
        if session.pending or session.recording is not None:
            retry = st.button("Retry reply", key=f"retry_reply_{session.id}", disabled=not provider.enabled)

    prompt = st.chat_input("Reply as the insurance representative", key=f"chat_{session.id}",
                           max_chars=1600, accept_audio=True, max_upload_size=3, disabled=not provider.enabled)
    if prompt is None and not retry:
        return
    if prompt is not None:
        session.pending = prompt.text.strip()
        session.recording = prompt.audio.getvalue() if prompt.audio is not None else None
        session.speak_reply = voice_mode or session.recording is not None

    previous_count = len(session.call.transcript)
    try:
        if session.recording is not None:
            with st.spinner("Transcribing your recording…"):
                transcript = provider.transcribe(session.recording)
            session.pending = " ".join(filter(None, (session.pending, transcript)))
            session.recording = None
        with st.spinner("Preparing the agent’s reply…"):
            reply(session.call, session.pending, provider)
    except ValueError as error:
        session.error, session.pending, session.recording = str(error), "", None
    except ServiceError as error:
        session.error = str(error)
    else:
        session.error = session.pending = ""
        session.audio_error, session.audio_retry = "", None
        turn = len(session.call.transcript)
        if session.speak_reply and turn > previous_count and session.call.transcript[-1].speaker == "agent":
            with st.spinner("Preparing the spoken reply…"):
                synthesize_turn(session, turn, settings)
    st.rerun()


def show_phone_record(attempt: Attempt, directory: Path) -> None:
    if not attempt.sid:
        return
    folder = directory / attempt.sid
    result = read_output(folder, "claim_result.json")
    transcript = read_output(folder, "transcript.json")
    call_error = read_output(folder, "call_error.json")
    if call_error:
        st.error(call_error["error"])
    if transcript is None or result is None:
        if attempt.status == "completed":
            st.info("Waiting for the voice service to finish saving the call.")
        return
    show_results(folder, result, mode="phone")
    with st.expander("Phone transcript", expanded=True):
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


st.set_page_config(page_title="EMS ClaimLine", page_icon=":material/phone_in_talk:", layout="centered")
st.title("EMS ClaimLine")
st.caption(f"Cedar Vale EMS · Synthetic claim {CLAIM['claim_number']} · Service date {CLAIM['date_of_service']}")
chat_tab, book_tab = st.tabs(["Chat", "Book a call"])
with chat_tab:
    chat_panel()
with book_tab:
    choice = st.radio("How would you like to speak with the agent?", ("Call my phone", "Talk here"),
                      horizontal=True, key="call_choice")
    if choice == "Call my phone":
        st.caption("Answer the incoming call on your registered mobile number.")
        twilio_panel()
    else:
        with st.expander("Voice options"):
            recorded = st.toggle("Use recorded replies", key="recorded_voice")
        if recorded:
            chat_panel("browser")
        else:
            from ems_voice.browser_component import live_voice

            live_voice(key="live_voice", data={"socketUrl": "ws://127.0.0.1:5001/voice"})
