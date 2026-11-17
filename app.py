"""EMS ClaimLine: Streamlit front end (iOS glass design).

Run with:  streamlit run app.py
Browser Voice uses Streamlit's connection. The process also serves Twilio webhooks on PORT (default 8000).
"""

import streamlit as st

from claimline import ui
from claimline.agent import CLAIM
from claimline.config import Settings
from claimline.launch import start_background
from claimline.server import ApiError
from claimline.voice_component import dispatch_voice_request, load_orb, orb_asset_version

MODES = ("Phone call", "Voice", "Type")
POLL_SECONDS = 1.5

st.set_page_config(page_title="EMS ClaimLine", page_icon="📞", layout="centered", initial_sidebar_state="collapsed")
st.markdown(ui.style_tag(), unsafe_allow_html=True)


@st.cache_resource(show_spinner=False)
def runtime():
    settings = Settings.load(ui_port=st.get_option("server.port"))
    service, problem = start_background(settings)
    return settings, service, problem


@st.cache_resource(show_spinner=False)
def voice_orb(asset_version):
    """Cache registration by frontend content so deployments replace old JavaScript."""
    return load_orb()


settings, service, problem = runtime()
state_store = st.session_state
state_store.setdefault("view_id", None)    # the call whose conversation is on screen
state_store.setdefault("chat_id", None)    # an open typed chat
state_store.setdefault("voice_call_id", None)  # owned by this browser's Streamlit session
state_store.setdefault("voice_request_id", None)
state_store.setdefault("voice_response", None)
state_store.setdefault("notice", "")


def current_state():
    """State of the call on screen, or None. Forgets ids the server no longer knows (after a restart)."""
    call_id = state_store.view_id
    if not call_id:
        return None
    try:
        return service.state(service.get(call_id))
    except ApiError:
        state_store.view_id = state_store.chat_id = None
        return None


def phone_panel(info, current):
    active = bool(current and current["kind"] == "phone" and not current["finished"])
    status = current["status"] if current and current["kind"] == "phone" else "created"
    st.markdown(f'<div class="cl-h">Call my phone</div>{ui.status_pill(status)}', unsafe_allow_html=True)
    hint = (f"The agent will call your mobile {info['to_hint']}. Pick up and talk as the insurance representative."
            if info["to_hint"] else "The agent calls your registered mobile. Pick up and talk as the insurance representative.")
    st.markdown(f'<p class="cl-hint">{ui.esc(hint)}</p>', unsafe_allow_html=True)
    if info["phone_missing"]:
        st.markdown(ui.alert("Phone calling needs these values in .env: " + ", ".join(info["phone_missing"])), unsafe_allow_html=True)
    elif not info["public_url"]:
        st.markdown(ui.alert(f"No public URL yet. Run “ngrok http {settings.port}” in another window "
                             "(or set PUBLIC_BASE_URL in .env), then press the button."), unsafe_allow_html=True)
    if st.button("Call my phone", type="primary", width="stretch", key="dial",
                 disabled=bool(info["phone_missing"]) or active):
        try:
            state_store.view_id = service.place_call()["id"]
            state_store.notice = ""
        except ApiError as error:
            state_store.notice = str(error)
        st.rerun()
    if current and current["kind"] == "phone" and current["error"]:
        st.markdown(ui.alert(current["error"], "error"), unsafe_allow_html=True)


def voice_panel():
    st.markdown('<div class="cl-h">Talk here</div>', unsafe_allow_html=True)
    result = voice_orb(orb_asset_version())(key="voice_orb", data={"response": state_store.voice_response},
                         default={"call_id": None, "request": None},
                         on_call_id_change=lambda: None, on_request_change=lambda: None)
    request = getattr(result, "request", None)
    if isinstance(request, dict) and request.get("id") != state_store.voice_request_id:
        state_store.voice_request_id = request.get("id")
        try:
            data = dispatch_voice_request(service, request, state_store.voice_call_id)
            if request.get("action") == "start":
                state_store.voice_call_id = state_store.view_id = data["id"]
            state_store.voice_response = {"id": request.get("id"), "data": data}
        except ApiError as error:
            state_store.voice_response = {"id": request.get("id"), "error": str(error)}
        st.rerun()  # deliver the response through the component's data
    call_id = result.call_id if result is not None else None
    if call_id and call_id == state_store.voice_call_id and call_id != state_store.view_id:
        state_store.view_id = call_id


def chat_panel(current):
    st.markdown('<div class="cl-h">Type your replies</div>', unsafe_allow_html=True)
    chat_id = state_store.chat_id
    if chat_id and (not current or current["id"] != chat_id or current["finished"]):
        state_store.chat_id = chat_id = None
    if not chat_id:
        st.markdown('<p class="cl-hint">Start a chat and answer as the insurance representative.</p>', unsafe_allow_html=True)
        if st.button("Start chat", type="primary", width="stretch", key="chat_start"):
            started = service.web_start("chat")
            state_store.chat_id = state_store.view_id = started["id"]
            st.rerun()
        return
    with st.form("chat_form", clear_on_submit=True, border=False):
        text = st.text_input("Reply", placeholder="Reply as the insurance representative…", label_visibility="collapsed")
        send_col, silent_col = st.columns(2)
        send = send_col.form_submit_button("Send", type="primary", width="stretch")
        silent = silent_col.form_submit_button("Stay silent", width="stretch")
    if st.button("End call", key="chat_end", width="stretch"):
        service.web_end(chat_id)
        state_store.chat_id = None
        st.rerun()
    if send or silent:
        try:
            if silent:
                reply = service.web_silence(chat_id)
            elif text.strip():
                reply = service.web_say(chat_id, text)
            else:
                return
            if reply["reply"]["end"]:
                service.web_end(chat_id)
                state_store.chat_id = None
        except ApiError as error:
            state_store.notice = str(error)
        st.rerun()


def conversation():
    current = current_state()
    if not current:
        return
    polling = current["kind"] in ("phone", "voice") and not current["finished"]

    @st.fragment(run_every=POLL_SECONDS if polling else None)
    def live():
        now = current_state()
        if not now:
            return
        st.markdown(f'<div class="cl-glass"><div class="cl-h">Conversation</div>'
                    f'{ui.transcript(now["transcript"], now["phase"])}</div>', unsafe_allow_html=True)
        if now.get("reviewing"):
            st.markdown('<p class="cl-hint">Reviewing the call…</p>', unsafe_allow_html=True)
        elif now["finished"]:
            if now["scorecard"]:
                st.markdown(ui.results_cards(now["result"], now["scorecard"], now["review_method"]), unsafe_allow_html=True)
            else:
                st.markdown(ui.alert("The call ended before the agent could speak, so there is nothing to review."),
                            unsafe_allow_html=True)
            if polling:
                st.rerun()  # leave polling mode once the call has finished

    live()


# ------------------------------------------------------------------ page
st.markdown(ui.hero(CLAIM), unsafe_allow_html=True)
if problem:
    st.markdown(ui.alert(problem, "error"), unsafe_allow_html=True)
mode = st.segmented_control("Mode", MODES, default=MODES[0], key="mode", label_visibility="collapsed") or MODES[0]
if mode != "Voice" and state_store.voice_call_id:
    try:
        session = service.get(state_store.voice_call_id)
        if not session.finalized:
            service.web_end(session.id)
    except ApiError:
        pass  # the runtime may have restarted
    state_store.voice_call_id = None
info = service.config_info()
current = current_state()
with st.container(key="glass_action"):
    if mode == "Phone call":
        phone_panel(info, current)
    elif mode == "Voice":
        voice_panel()
    else:
        chat_panel(current)
    if state_store.notice:
        st.markdown(ui.alert(state_store.notice, "error"), unsafe_allow_html=True)
conversation()
