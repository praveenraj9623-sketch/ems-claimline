"""Local Streamlit dashboard for the EMS assessment phone call."""

import json
import time
from pathlib import Path

import streamlit as st

from ems_voice.outbound import Attempt, DialSettings, OutboundCalls, place_call
from ems_voice.provider import local_settings

ROOT = Path(__file__).resolve().parent
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


@st.cache_resource
def call_store(directory: Path) -> OutboundCalls:
    return OutboundCalls(directory)


def read_output(directory: Path, name: str):
    try:
        return json.loads((directory / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def show_outputs(attempt: Attempt, directory: Path) -> None:
    if not attempt.sid:
        return
    folder = directory / attempt.sid
    result = read_output(folder, "claim_result.json")
    review = read_output(folder, "review.json")
    call_error = read_output(folder, "call_error.json")
    review_error = read_output(folder, "review_error.json")
    if call_error:
        st.error(call_error["error"])
    if result is None:
        if attempt.status == "completed":
            st.info("Waiting for the voice service to finish saving the call.")
        return

    result_card, review_card = st.columns(2, gap="medium")
    with result_card.container(border=True):
        st.subheader("Claim result")
        st.json(result)
    with review_card.container(border=True):
        st.subheader("AI reviewer")
        if review is not None:
            st.json(review)
        elif review_error:
            st.error(review_error["error"])
            st.code(f".\\.venv\\Scripts\\python.exe -m ems_voice.phone retry-review {attempt.sid}",
                    language="powershell")
        else:
            st.info("Review in progress.")


@st.fragment(run_every="2s")
def dashboard():
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
        show_outputs(attempt, directory)


st.set_page_config(page_title="EMS Claim Desk", page_icon=":material/phone_in_talk:", layout="centered")
st.title("EMS Claim Desk")
st.caption("Cedar Vale EMS · Synthetic claim TEST-48213 · Service date 03/14/2026")
dashboard()
