"""Mount the live-microphone orb as a Streamlit Components v2 component."""

from hashlib import sha256
from pathlib import Path

from .server import ApiError, Service

ASSETS = Path(__file__).with_name("voice")


def orb_asset_version() -> str:
    """Invalidate Streamlit's cached component when its frontend files change."""
    return sha256(b"\0".join((ASSETS / name).read_bytes() for name in ("orb.html", "orb.css", "orb.js"))).hexdigest()


def dispatch_voice_request(service: Service, request: dict, owned_call_id: str | None) -> dict:
    """Handle a component message within its owning Streamlit session.

    Browser Voice uses Streamlit's connection, so no browser request to localhost
    or separately exposed HTTP port is needed. Call ids from other users are rejected.
    """
    if not isinstance(request, dict) or not isinstance(request.get("id"), str) or not 1 <= len(request["id"]) <= 128:
        raise ApiError("Invalid voice request.")
    action, body = request.get("action"), request.get("body", {})
    if action not in ("start", "say", "silence", "played", "end") or not isinstance(body, dict):
        raise ApiError("Invalid voice action.")
    if action == "start":
        if owned_call_id:
            try:
                previous = service.get(owned_call_id)
            except ApiError:
                previous = None  # the runtime may have restarted
            if previous and previous.kind == "voice" and not previous.finalized:
                service.web_end(owned_call_id)
        return service.web_start("voice")
    call_id = request.get("call_id")
    if not owned_call_id or call_id != owned_call_id or service.get(owned_call_id).kind != "voice":
        raise ApiError("This voice call does not belong to this browser session.", 403)
    if action == "say":
        return service.web_say(call_id, body.get("text"))
    if action == "silence":
        return service.web_silence(call_id)
    if action == "played":
        return service.web_played(call_id, body.get("turn"))
    return service.web_end(call_id)


def load_orb():
    import streamlit as st

    return st.components.v2.component(
        "claimline_voice_orb",
        html=(ASSETS / "orb.html").read_text(encoding="utf-8"),
        css=(ASSETS / "orb.css").read_text(encoding="utf-8"),
        js=(ASSETS / "orb.js").read_text(encoding="utf-8"),
    )
