"""Mount the live-microphone orb as a Streamlit Components v2 component."""

from pathlib import Path

ASSETS = Path(__file__).with_name("voice")


def load_orb():
    import streamlit as st

    return st.components.v2.component(
        "claimline_voice_orb",
        html=(ASSETS / "orb.html").read_text(encoding="utf-8"),
        css=(ASSETS / "orb.css").read_text(encoding="utf-8"),
        js=(ASSETS / "orb.js").read_text(encoding="utf-8"),
    )
