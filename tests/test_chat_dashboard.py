import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from ems_voice.provider import ServiceError
from ems_voice.review import CHECKS
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

    def test_chat_hold_spell_back_correction_and_review_are_saved_once(self):
        self.assertEqual([tab.label for tab in self.app.tabs], ["Chat", "Phone Call"])
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
