"""EMS ClaimLine dashboard for text, browser voice, and phone calls."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from ems_voice.huskyvoice import (
    END_STATES, FAILED_STATES, UNCONFIRMED, HuskyError, HuskySettings, HuskyVoice, call_failure,
    parse_transcript,
)
from ems_voice.outbound import Attempt, DialSettings, OutboundCalls, place_call
from ems_voice.phone_records import PhoneRecords
from ems_voice.provider import Config, GroqClient, ServiceError, local_settings
from ems_voice.records import read_output, review_saved, save_outputs
from ems_voice.review import inspect_transcript
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


HUSKY_STATUSES = {
    "pending": "Queued", "queued": "Queued", "scheduled": "Queued", "initiated": "Dialing",
    "ringing": "Ringing", "in_progress": "Connected", "completed": "Call ended", "failed": "Call failed",
    "disallowed": "Call blocked",
    "cancelled": "Canceled", "canceled": "Canceled", "no_answer": "No answer", "busy": "Line busy",
    "unconfirmed": "Dial request unconfirmed", "request_failed": "Call request failed",
    "cleared": "Ready",
}
POLL_SECONDS = 5


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


@dataclass
class HuskyAttempt:
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "unconfirmed"
    call_id: str = ""
    error: str = ""
    poll_error: str = ""
    provider_error: str = ""
    transcript_error: str = ""
    next_poll: float = 0
    poll_until: float = field(default_factory=lambda: time.monotonic() + 600)

    @property
    def active(self) -> bool:
        return self.status not in END_STATES and self.status not in {"request_failed", "cleared"}


def dashboard_settings() -> dict[str, str]:
    local = local_settings(ROOT)
    try:
        secrets = {name: value for name, value in st.secrets.items() if isinstance(value, str)}
    except StreamlitSecretNotFoundError:
        secrets = {}
    secrets.update({name: value for name, value in local.items() if value.strip() or name not in secrets})
    return secrets


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
                review_saved(folder, reviewer, mode="huskyvoice" if mode == "huskyvoice" else "groq")
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
                        review_saved(folder, reviewer, retry=True,
                                     mode="huskyvoice" if mode == "huskyvoice" else "groq")
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


def chat_panel(settings: dict[str, str], mode: str = "chat") -> None:
    session_key = f"ems_{mode}"
    if session_key not in st.session_state:
        st.session_state[session_key] = ChatSession()
    session: ChatSession = st.session_state[session_key]
    voice_mode = mode == "browser"
    provider = GroqClient(Config.from_settings(settings))
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
    show_voice_record(directory / attempt.sid, attempt.status, mode="phone")


def show_voice_record(folder: Path, status: str, *, mode: str, reviewer: GroqClient | None = None) -> None:
    result = read_output(folder, "claim_result.json")
    transcript = read_output(folder, "transcript.json")
    call_error = read_output(folder, "call_error.json")
    if call_error:
        st.error(call_error["error"])
    if transcript is None or result is None:
        if status == "completed":
            st.info("Waiting for the voice service to finish saving the call.")
        return
    show_results(folder, result, mode=mode, reviewer=reviewer)
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


def save_husky_transcript(folder: Path, transcript: list[dict]) -> None:
    call, _ = inspect_transcript(transcript, hosted=True)
    save_outputs(folder, call, transcript)


def poll_husky_call(client: HuskyVoice, attempt: HuskyAttempt, folder: Path) -> None:
    attempt.next_poll = time.monotonic() + POLL_SECONDS
    try:
        data, attempt.provider_error = client.status(attempt.call_id)
    except HuskyError as error:
        attempt.poll_error = str(error)
        attempt.provider_error = error.details
        if not error.retryable:
            attempt.poll_until = 0
        return
    status = client.status_name(data)
    if not status:
        attempt.poll_error = "HuskyVoice returned an unreadable call status. Try refreshing."
        return
    if attempt.poll_error and not attempt.poll_until:
        attempt.poll_until = time.monotonic() + 600
    attempt.poll_error = ""
    if status in END_STATES and attempt.active:
        attempt.poll_until = time.monotonic() + 30  # Allow time for transcript processing.
    attempt.status = status
    attempt.error = call_failure(data, status) if status in FAILED_STATES else ""
    if status not in END_STATES or (folder / "transcript.json").exists():
        return
    try:
        transcript = client.transcript(data)
    except ValueError as error:
        attempt.transcript_error = str(error)
    else:
        attempt.transcript_error = ""
        if transcript:
            save_husky_transcript(folder, transcript)
        elif status != "completed":
            attempt.poll_until = 0


@st.fragment(run_every=POLL_SECONDS)
def huskyvoice_panel() -> None:
    values = dashboard_settings()
    try:
        settings = HuskySettings.load(values)
    except ValueError as error:
        settings = None
        st.warning(str(error))
    attempt: HuskyAttempt | None = st.session_state.get("husky_attempt")
    folder = ROOT / "huskyvoice_calls" / attempt.id if attempt else None
    if settings and attempt and attempt.call_id and folder:
        now = time.monotonic()
        auto_poll = (attempt.status != "cleared" and attempt.next_poll <= now < attempt.poll_until
                     and not (folder / "transcript.json").exists())
        if st.button("Refresh call status", key="husky_refresh") or auto_poll:
            poll_husky_call(HuskyVoice(settings), attempt, folder)

    with st.container(border=True):
        st.metric("Call status", HUSKY_STATUSES.get(attempt.status, "Call status pending") if attempt else "Ready")
        if settings:
            st.caption(f"HuskyVoice · Mobile ending in {settings.to_number[-4:]}")
        if st.button("Call my phone", type="primary", width="stretch",
                     key=f"husky_dial_{attempt.id if attempt else 'first'}",
                     disabled=settings is None or bool(attempt and attempt.active)) and settings:
            attempt = HuskyAttempt(error=UNCONFIRMED)
            st.session_state.husky_attempt = attempt
            try:
                with st.spinner("Requesting a call from HuskyVoice…"):
                    attempt.call_id, attempt.status = HuskyVoice(settings).start(attempt.id)
            except HuskyError as error:
                attempt.error = str(error)
                attempt.provider_error = error.details
                attempt.status = "unconfirmed" if error.unconfirmed else "request_failed"
            else:
                attempt.error = ""
                attempt.next_poll = time.monotonic() + POLL_SECONDS
            st.rerun()
        if attempt and attempt.status == "ringing":
            st.info("Answer the incoming call on your mobile.")

    if not attempt or folder is None:
        return
    if attempt.error:
        st.error(attempt.error)
    if attempt.call_id and attempt.status in FAILED_STATES:
        st.text(f"HuskyVoice call ID: {attempt.call_id}")
    if attempt.poll_error:
        st.error(attempt.poll_error)
    if details := getattr(attempt, "provider_error", ""):
        operation = "call status" if attempt.call_id else "create call"
        st.text(f"HuskyVoice {operation} error: {details}", width="stretch")
    if (folder / "transcript.json").exists():
        show_voice_record(folder, attempt.status, mode="huskyvoice",
                          reviewer=GroqClient(Config.from_settings(values)))
        return

    unknown_end = attempt.active and (
        not attempt.call_id or bool(attempt.poll_error) or time.monotonic() >= attempt.poll_until
    )
    if not (attempt.status in END_STATES or attempt.status == "cleared" or unknown_end):
        return
    if unknown_end:
        st.info("Check All Calls in HuskyVoice before importing or starting another call.")
        if st.button("I checked that no call is active", key="husky_release"):
            attempt.status = "cleared"
            attempt.error = attempt.poll_error = attempt.provider_error = ""
            st.rerun()
    failed = attempt.status in FAILED_STATES
    with st.expander("Import a transcript") if failed else st.container():
        if failed:
            st.caption("If you spoke with Sarah, check the attempt in HuskyVoice → All Calls → Insights for a transcript.")
        else:
            st.info(attempt.transcript_error or "HuskyVoice has not returned a transcript through the API. "
                    "It may still be processing or unavailable on your trial. You can import it from All Calls → Insights.")
        with st.form(f"husky_import_{attempt.id}", clear_on_submit=True):
            raw = st.text_area("Paste the call transcript", key="husky_transcript",
                               help="Use transcript JSON or text with Sarah: and Representative: speaker labels.")
            if st.form_submit_button("Import and review"):
                try:
                    transcript = parse_transcript(raw, api_key=values.get("HUSKYVOICE_API_KEY", "").strip(),
                                                  to_number=values.get("CALL_TO_NUMBER", "").strip())
                    if not transcript:
                        raise ValueError("Paste at least one spoken turn from the call.")
                except ValueError as error:
                    st.error(str(error))
                else:
                    save_husky_transcript(folder, transcript)
                    if unknown_end:
                        attempt.status = "cleared"
                        attempt.error = attempt.poll_error = attempt.provider_error = ""
                    st.rerun()


st.set_page_config(page_title="EMS ClaimLine", page_icon=":material/phone_in_talk:", layout="centered")
st.title("EMS ClaimLine")
st.caption(f"Cedar Vale EMS · Synthetic claim {CLAIM['claim_number']} · Service date {CLAIM['date_of_service']}")
settings = dashboard_settings()
chat_tab, book_tab = st.tabs(["Chat", "Book a call"])
with chat_tab:
    chat_panel(settings)
with book_tab:
    choice = st.radio("How would you like to speak with the agent?", ("Call my phone", "Talk here"),
                      horizontal=True, key="call_choice")
    if choice == "Call my phone":
        st.caption("Answer the incoming call on your registered mobile number.")
        if settings.get("VOICE_TRANSPORT", "").strip().lower() == "twilio":
            twilio_panel()
        else:
            huskyvoice_panel()
    else:
        with st.expander("Voice options"):
            recorded = st.toggle("Use recorded replies", key="recorded_voice")
        if recorded:
            chat_panel(settings, "browser")
        else:
            from ems_voice.browser_component import live_voice

            live_voice(key="live_voice", data={"socketUrl": "ws://127.0.0.1:5001/voice"})
