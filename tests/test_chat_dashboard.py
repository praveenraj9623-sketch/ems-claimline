import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from ems_voice.provider import ServiceError
from ems_voice.review import CHECKS
from ems_voice.speech import SpeechError
from ems_voice.workflow import HOLD_REPLY, OPENING, SLOW_ID
from test_assessment import EXPECTED, PAID
from test_phone import GroqStub

APP = Path(__file__).resolve().parents[1] / "app.py"


class ChatDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        path = self.root / "app.py"
        path.write_bytes(APP.read_bytes())
        self.app = AppTest.from_file(str(path), default_timeout=8)
        self.provider = GroqStub()
        self.fail_reply = False
        self.enterContext(patch.dict(os.environ, {
            "GROQ_API_KEY": "offline-chat-key", "GROQ_MODEL": "extract", "GROQ_REVIEW_MODEL": "review",
            "DEEPGRAM_API_KEY": "",
            "TWILIO_ACCOUNT_SID": "", "TWILIO_FROM_NUMBER": "", "CALL_TO_NUMBER": "",
        }))
        self.enterContext(patch("ems_voice.provider.GroqClient.structured", side_effect=self.structured))
        self.app.run()
        self.assertFalse(self.app.exception)

    def structured(self, model, name, *args):
        if name == "claim_facts" and self.fail_reply:
            raise ServiceError("Temporary extraction failure.")
        return self.provider.structured(model, name, *args)

    def send(self, text):
        self.app.chat_input[0].set_value(text).run()
        self.assertFalse(self.app.exception)

    def button(self, label):
        return next(button for button in self.app.button if button.label == label)

    def record(self):
        folder, = (self.root / "chat_calls").iterdir()
        return folder

    def recorded_voice(self):
        self.app.radio[0].set_value("Talk here").run()
        self.app.toggle[0].set_value(True).run()

    def test_chat_hold_spell_back_correction_and_review_are_saved_once(self):
        self.assertEqual([tab.label for tab in self.app.tabs], ["Chat", "Book a call"])
        self.assertEqual([item.value for item in self.app.text], [OPENING])
        self.send("Please hold.")
        self.send("Please hold.")
        self.assertEqual([item.value for item in self.app.text].count(HOLD_REPLY), 1)
        self.assertTrue(any("On hold" in item.value for item in self.app.info))
        self.send("Could you say the claim number slowly?")
        self.assertIn(SLOW_ID, [item.value for item in self.app.text])
        self.send(PAID)
        self.send("No, the paid amount was $250.25.")
        self.send("Yes.")
        expected = {**EXPECTED, "paid_amount": 250.25}
        self.assertEqual([json.loads(item.value) for item in self.app.json],
                         [expected, dict.fromkeys(CHECKS, True)])
        directory = self.record()
        result = directory / "claim_result.json"
        updated = result.stat().st_mtime_ns
        transcript = json.loads((directory / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(self.provider.reviews[0]["transcript"], transcript)
        self.app.run()
        self.assertEqual(result.stat().st_mtime_ns, updated)
        self.assertEqual(len(self.provider.reviews), 1)
        self.button("New chat").click().run()
        self.assertEqual([item.value for item in self.app.text], [OPENING])
        self.assertFalse(self.app.json)
        self.assertFalse((self.root / "phone_calls" / directory.name).exists())

    def test_missing_fact_and_failed_review_can_retry_without_losing_the_result(self):
        self.provider.review_failure = True
        self.send("The claim was paid for $240 on March 20, 2026.")
        self.send("I cannot share it.")
        self.send("I cannot share it.")
        self.send("Yes.")
        result = {**EXPECTED, "check_or_eft_number": None}
        self.assertEqual([json.loads(item.value) for item in self.app.json], [result])
        self.assertIn("Temporary review failure", self.app.error[0].value)
        folder = self.record()
        self.assertTrue((folder / "transcript.json").is_file())
        self.app.run()
        self.assertEqual(len(self.provider.reviews), 1)
        self.provider.review_failure = False
        self.button("Retry review").click().run()
        review = {**dict.fromkeys(CHECKS, True), "all_four_facts_collected": False}
        self.assertEqual([json.loads(item.value) for item in self.app.json], [result, review])
        self.assertEqual(len(self.provider.reviews), 2)
        self.assertFalse((folder / "review_error.json").exists())

    def test_failed_turn_can_retry_and_other_browser_session_has_its_own_thread(self):
        self.fail_reply = True
        self.send(PAID)
        self.assertIn("Temporary extraction failure", self.app.error[0].value)
        self.fail_reply = False
        self.button("Retry reply").click().run()
        self.assertEqual([item.value for item in self.app.text].count(PAID), 1)
        self.assertFalse(self.app.error)
        separate = AppTest.from_file(str(self.root / "app.py"), default_timeout=8).run()
        self.assertEqual([item.value for item in separate.text], [OPENING])
        self.assertEqual(len(self.provider.extractions), 1)

    def test_talk_here_autoplays_the_greeting_once_per_conversation(self):
        with (patch.dict(os.environ, {"DEEPGRAM_API_KEY": "local-test"}),
              patch("ems_voice.speech.browser_audio", return_value=b"ID3 greeting") as speak):
            self.recorded_voice()
            self.assertFalse(self.app.exception)
            self.assertNotIn("Play greeting", [button.label for button in self.app.button])
            greeting, = self.app.get("audio")
            self.assertTrue(greeting.proto.autoplay)

            self.app.run()
            self.app.radio[0].set_value("Call my phone").run()
            self.recorded_voice()
            greeting, = self.app.get("audio")
            self.assertFalse(greeting.proto.autoplay)
        speak.assert_called_once()
        self.assertEqual(speak.call_args.args[2], OPENING)

    def test_talk_here_accepts_a_recording_and_renders_text_and_audio(self):
        self.recorded_voice()
        self.assertNotIn("Call my phone", [button.label for button in self.app.button])
        self.assertFalse(self.app.metric)
        self.assertEqual(len(self.app.chat_input), 2)
        native_chat_input = st.chat_input
        audio = b"recorded reply"
        delivered = False

        def submit_recording(*args, **kwargs):
            nonlocal delivered
            result = native_chat_input(*args, **kwargs)
            self.assertTrue(kwargs["accept_audio"])
            browser = self.app.session_state["ems_browser"]
            if not delivered and kwargs["key"] == f"chat_{browser.id}":
                delivered = True
                return SimpleNamespace(text="", audio=BytesIO(audio))
            return result

        with (patch.dict(os.environ, {"DEEPGRAM_API_KEY": "local-test"}),
              patch("streamlit.chat_input", side_effect=submit_recording),
              patch("ems_voice.provider.GroqClient.transcribe", return_value=PAID) as transcribe,
              patch("ems_voice.speech.browser_audio", return_value=b"ID3 test reply") as speak):
            self.app.run()
            self.assertEqual(len(self.app.get("audio")), 1)
            self.app.chat_input[1].set_value("Yes.").run()
        self.assertFalse(self.app.exception)
        transcribe.assert_called_once_with(audio)
        self.assertEqual(speak.call_count, 2)
        self.assertEqual([item.value for item in self.app.text].count(PAID), 1)
        self.assertEqual(len(self.app.get("audio")), 2)
        self.assertEqual([json.loads(item.value) for item in self.app.json],
                         [EXPECTED, dict.fromkeys(CHECKS, True)])
        self.assertFalse(self.app.session_state["ems_chat"].call.facts)

    def test_audio_failure_can_retry_without_submitting_another_turn(self):
        self.recorded_voice()
        with (patch.dict(os.environ, {"DEEPGRAM_API_KEY": "local-test"}),
              patch("ems_voice.speech.browser_audio",
                    side_effect=[SpeechError("Audio temporarily unavailable."), b"ID3 test reply"]) as speak):
            self.app.chat_input[1].set_value(PAID).run()
            self.assertFalse(self.app.exception)
            self.assertIn("Audio temporarily unavailable.", [warning.value for warning in self.app.warning])
            self.assertEqual(len(self.app.get("audio")), 0)
            self.button("Retry audio").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(speak.call_count, 2)
        self.assertEqual(len(self.provider.extractions), 1)
        self.assertEqual(len(self.app.get("audio")), 1)
