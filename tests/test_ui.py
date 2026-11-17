import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from claimline import ui
from claimline.config import Settings
from claimline.groq import GroqClient
from claimline.server import Service

ROOT = Path(__file__).resolve().parents[1]


class DesignHelpers(unittest.TestCase):
    def test_transcript_escapes_everything_said(self):
        html = ui.transcript([{"turn": 1, "speaker": "representative", "text": "<script>alert(1)</script> & <b>"}])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_alert_and_json_are_escaped(self):
        self.assertNotIn("<img", ui.alert("<img src=x onerror=1>"))
        self.assertNotIn("<img", ui.json_block({"claim_status": "<img src=x>"}))

    def test_rows_show_missing_values_and_checks(self):
        rows = ui.result_rows({"claim_status": "paid", "paid_amount": 240.0, "check_or_eft_number": None, "payment_date": None})
        self.assertIn("$240.00", rows)
        self.assertEqual(rows.count("Not provided"), 2)
        score = ui.score_rows({"read_back_done": False, "no_invented_data": True})
        self.assertIn("cl-badge no", score)
        self.assertIn("cl-badge ok", score)

    def test_glass_design_tokens_present(self):
        for token in ("backdrop-filter", "saturate", "SF Pro", "border-radius", "stBaseButton-primary", "st-key-glass"):
            self.assertIn(token, ui.CSS)

    def test_status_pill_known_and_unknown(self):
        self.assertIn("Ringing", ui.status_pill("ringing"))
        self.assertIn("Weird", ui.status_pill("weird"))


class SessionState(dict):
    __getattr__ = dict.get

    def __setattr__(self, k, v):
        self[k] = v


class Rerun(Exception):
    pass


def make_streamlit(mode, *, clicks=(), typed="", form=None, orb_call_id=None, state=None):
    st = MagicMock(name="streamlit")
    st.session_state = state if state is not None else SessionState()
    st.markdown_calls = []
    st.markdown = lambda body, **k: st.markdown_calls.append(body)
    st.cache_resource = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
    st.fragment = lambda *a, **k: (lambda f: f)
    st.segmented_control = lambda *a, **k: mode
    st.button = lambda label, **k: label in clicks
    st.text_input = lambda *a, **k: typed
    st.form_submit_button = lambda label, **k: form == label
    st.columns = lambda n: [SimpleNamespace(form_submit_button=lambda label, **k: form == label) for _ in range(n)]
    st.rerun = MagicMock(side_effect=Rerun)
    orb = MagicMock(return_value=SimpleNamespace(call_id=orb_call_id))
    st.components.v2.component = MagicMock(return_value=orb)
    return st


class AppSmokeTest(unittest.TestCase):
    """Runs app.py top to bottom against a stand-in for Streamlit (the real one is not installable in CI here)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(port=0, calls_dir=Path(self.tmp.name))
        self.service = Service(self.settings, groq=GroqClient(""))
        self.addCleanup(self.tmp.cleanup)

    def run_app(self, st, *, problem=""):
        with patch.dict(sys.modules, {"streamlit": st}), \
                patch("claimline.launch.start_background", lambda s: (self.service, problem)), \
                patch("claimline.config.Settings.load", classmethod(lambda cls, *a, **k: self.settings)):
            try:
                runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
            except Rerun:
                return "rerun"
        return "ok"

    def page(self, st):
        return " ".join(st.markdown_calls)

    def test_runtime_uses_the_running_streamlit_port(self):
        st = make_streamlit("Voice")
        st.get_option.return_value = 8502
        with patch.dict(sys.modules, {"streamlit": st}), \
                patch("claimline.launch.start_background", return_value=(self.service, "")), \
                patch("claimline.config.Settings.load", return_value=self.settings) as load:
            runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
        load.assert_called_once_with(ui_port=8502)

    def test_phone_mode_without_twilio_settings(self):
        st = make_streamlit("Phone call")
        self.assertEqual(self.run_app(st), "ok")
        page = self.page(st)
        self.assertIn("ClaimLine", page)
        self.assertIn("TEST-48213", page)
        self.assertIn("TWILIO_ACCOUNT_SID", page)       # tells you what is missing
        self.assertTrue(st.session_state.get("view_id") is None)

    def test_phone_button_places_call(self):
        self.service.settings = Settings(
            twilio_account_sid="AC" + "0" * 32, twilio_auth_token="t", twilio_from_number="+15005550006",
            call_to_number="+919876543210", public_base_url="https://x.ngrok.app", port=0, calls_dir=Path(self.tmp.name))
        self.settings = self.service.settings
        self.service.public_base = "https://x.ngrok.app"
        self.service._preflight = lambda base: None
        self.service.twilio = SimpleNamespace(place_call=lambda **k: "CA" + "9" * 32)
        st = make_streamlit("Phone call", clicks=("Call my phone",))
        self.assertEqual(self.run_app(st), "rerun")
        sid = st.session_state["view_id"]
        self.assertEqual(self.service.state(self.service.get(sid))["status"], "queued")
        st2 = make_streamlit("Phone call", state=st.session_state)
        self.run_app(st2)
        self.assertIn("Requesting the call", self.page(st2))

    def test_voice_mode_mounts_orb_and_shows_live_transcript(self):
        started = self.service.web_start("voice")
        st = make_streamlit("Voice", orb_call_id=started["id"])
        self.run_app(st)
        kwargs = st.components.v2.component.return_value.call_args.kwargs
        self.assertEqual(kwargs["data"], {"api": "http://127.0.0.1:0"})
        self.assertEqual(st.session_state["view_id"], started["id"])
        self.assertIn("billing office of Cedar Vale EMS", self.page(st))

    def test_voice_mode_reports_port_conflict(self):
        st = make_streamlit("Voice")
        self.run_app(st, problem="Port 8000 is already in use")
        self.assertIn("Port 8000 is already in use", self.page(st))

    def test_type_mode_full_chat_through_the_ui(self):
        state = SessionState()
        st = make_streamlit("Type", clicks=("Start chat",), state=state)
        self.assertEqual(self.run_app(st), "rerun")
        chat_id = state["chat_id"]
        self.assertTrue(chat_id)
        for text in ["paid", "240 dollars", "EFT 9134", "March 20 2026", "yes"]:
            st = make_streamlit("Type", typed=text, form="Send", state=state)
            outcome = self.run_app(st)
            self.assertEqual(outcome, "rerun" if text else "ok")
        self.assertIsNone(state["chat_id"])             # call ended after the goodbye
        done = make_streamlit("Type", state=state)
        self.run_app(done)
        page = self.page(done)
        self.assertIn("Claim result", page)
        self.assertIn("AI reviewer", page)
        self.assertIn("$240.00", page)
        self.assertNotIn("cl-badge no", page)

    def test_stay_silent_button_and_unknown_call_recovery(self):
        state = SessionState()
        st = make_streamlit("Type", clicks=("Start chat",), state=state)
        self.run_app(st)
        st = make_streamlit("Type", form="Stay silent", state=state)
        self.assertEqual(self.run_app(st), "rerun")
        state["view_id"] = "f" * 32                     # an id the server does not know
        st = make_streamlit("Type", state=state)
        self.assertEqual(self.run_app(st), "ok")
        self.assertIsNone(state["view_id"])


class CorsAndLaunch(unittest.TestCase):
    def test_cors_allows_only_the_streamlit_origin(self):
        import json
        import threading
        import urllib.error
        import urllib.request
        from claimline.server import create_server
        with tempfile.TemporaryDirectory() as d:
            settings = Settings(port=0, calls_dir=Path(d))
            server, _ = create_server(settings)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                def call(origin, method="OPTIONS", path="/api/web/start"):
                    req = urllib.request.Request(base + path, method=method, headers={"Origin": origin,
                                                 "Access-Control-Request-Method": "POST"})
                    try:
                        with urllib.request.urlopen(req) as r:
                            return r.status, dict(r.headers)
                    except urllib.error.HTTPError as e:
                        return e.code, dict(e.headers)
                status, headers = call("http://localhost:8501")
                self.assertEqual(status, 204)
                self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:8501")
                self.assertEqual(call("https://evil.example")[0], 403)
                status, headers = call("http://127.0.0.1:8501", "GET", "/api/config")
                self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:8501")
                status, headers = call("https://evil.example", "GET", "/api/config")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
            finally:
                server.shutdown()
                server.server_close()

    def test_background_server_reports_busy_port(self):
        from claimline.launch import start_background
        with tempfile.TemporaryDirectory() as d:
            first, problem = start_background(Settings(port=0, calls_dir=Path(d)))
            self.assertEqual(problem, "")
            import socket
            busy = socket.socket()
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            try:
                _, problem = start_background(Settings(port=busy.getsockname()[1], calls_dir=Path(d)))
                self.assertIn("already in use", problem)
            finally:
                busy.close()


if __name__ == "__main__":
    unittest.main()
