"""Mount the local live-call controls in Streamlit."""

from pathlib import Path

import streamlit as st

ASSETS = Path(__file__).with_name("browser_ui")

live_voice = st.components.v2.component(
    "claimline_live_voice",
    html=(ASSETS / "panel.html").read_text(encoding="utf-8"),
    css=(ASSETS / "panel.css").read_text(encoding="utf-8"),
    js=(ASSETS / "panel.js").read_text(encoding="utf-8"),
)
