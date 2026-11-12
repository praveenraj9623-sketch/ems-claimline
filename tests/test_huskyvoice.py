import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx
from streamlit.testing.v1 import AppTest

from ems_voice.huskyvoice import CALLS_URL, HuskySettings, HuskyVoice
from ems_voice.review import CHECKS
from ems_voice.workflow import OPENING
from test_assessment import EXPECTED, PAID

APP = Path(__file__).resolve().parents[1] / "app.py"
AGENT_ID = "configured_agent_mock"
KEY = "fake-husky-key"
NUMBER = "+12025550107"
CALL_ID = "call_mock_123"
SIGNED_URL = "https://mock-transcripts.s3.us-east-1.amazonaws.com/transcript.json?signature=offline"
TRANSCRIPT = [
    {"speaker": "agent", "text": OPENING.replace("TEST-48213", "Test 48213")},
    {"speaker": "user", "text": "Please hold."},
    {"speaker": "agent", "text": "Of course, I’ll wait."},
    {"speaker": "user", "text": "I’m back. Could you say the claim number slowly?"},
    {"speaker": "agent", "text": "T E S T dash four eight two one three."},
    {"speaker": "user", "text": PAID},
    {"speaker": "agent", "text": (
        "Let me read back what I captured. Claim status is paid. Paid amount is $240.00. "
        "Check or EFT number is EFT-9134. Payment date is March 20, 2026. Is that correct?"
    )},
    {"speaker": "user", "text": f"Yes, that's correct. You reached me at {NUMBER}."},
]


class HuskyDashboardTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.records = root / "huskyvoice_calls"
        path = root / "app.py"
        path.write_bytes(APP.read_bytes())
        (root / ".env").write_text(f"HUSKYVOICE_API_KEY={KEY}\nCALL_TO_NUMBER={NUMBER}\nHUSKYVOICE_AGENT_ID={AGENT_ID}\n", encoding="utf-8")
        self.app = AppTest.from_file(str(path), default_timeout=8)
        self.enterContext(patch.dict(os.environ, {"VOICE_TRANSPORT": "", "GROQ_API_KEY": "offline-groq"}, clear=False))
        self.responses = []
        self.requests = []
        self.reviews = []
        self.transport = self.enterContext(httpx.Client(transport=httpx.MockTransport(self.respond)))
        self.enterContext(patch("ems_voice.huskyvoice.httpx.request", side_effect=self.transport.request))
        self.enterContext(patch("ems_voice.provider.GroqClient.structured", side_effect=self.review))

    def respond(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected HuskyVoice request")
        status, data = self.responses.pop(0)
        if isinstance(data, httpx.HTTPError):
            raise data
        return httpx.Response(status, json=data)

    def review(self, model, name, instructions, payload, schema):
        self.assertEqual(name, "call_review")
        self.reviews.append(payload)
        return {
            "introduced_as_billing_office": {"passed": True, "turns": [1]},
            "claim_number_stated": {"passed": True, "turns": [1, 5]},
            "all_four_facts_collected": {"passed": True, "turns": [6]},
            "read_back_done": {"passed": True, "turns": [7]},
            "no_invented_data": {"passed": True, "turns": [6, 7]},
        }

    def button(self, label):
        return next(button for button in self.app.button if button.label == label)

    def dial(self):
        self.button("Call my phone").click().run()
        self.assertFalse(self.app.exception)

    def refresh(self):
        self.button("Refresh call status").click().run()
        self.assertFalse(self.app.exception)

    def assert_saved_review(self):
        folder, = self.records.iterdir()
        transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(transcript, self.reviews[0]["transcript"])
        self.assertEqual(transcript[0]["text"], TRANSCRIPT[0]["text"])
        self.assertEqual(transcript[-1]["speaker"], "representative")
        self.assertNotIn(NUMBER, json.dumps(transcript))
        self.assertEqual(json.loads((folder / "claim_result.json").read_bytes()), EXPECTED)
        review = json.loads((folder / "review.json").read_bytes())
        self.assertEqual(set(review), set(CHECKS))
        self.assertTrue(all(review.values()), review)

    def test_one_post_per_click_tracks_status_and_reviews_actual_transcript(self):
        self.responses = [
            (201, {"data": {"call_id": CALL_ID, "status": "scheduled"}}),
            (200, {"data": {"status": "ringing", "call_to": NUMBER}}),
            (200, {"data": {"status": "completed", "call_transcript": None}}),
            (200, {"data": {"status": "completed", "call_transcript": TRANSCRIPT}}),
        ]
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertEqual([tab.label for tab in self.app.tabs], ["Chat", "Book a call"])
        self.assertEqual(list(self.app.radio[0].options), ["Call my phone", "Talk here"])
        self.assertFalse(self.requests)
        self.dial()
        self.assertEqual(len(self.requests), 1)
        create = self.requests[0]
        self.assertEqual((create.method, str(create.url)), ("POST", CALLS_URL))
        self.assertEqual(create.headers["x-api-key"], KEY)
        self.assertEqual(json.loads(create.content), {"agent_id": AGENT_ID, "user_number": NUMBER})
        self.assertEqual(UUID(create.headers["idempotency-key"]).version, 4)
        self.assertTrue(self.button("Call my phone").disabled)
        self.app.run()
        self.assertEqual(len(self.requests), 1)

        self.app.session_state["husky_attempt"].next_poll = 0
        self.app.run()
        self.assertEqual(self.app.metric[0].value, "Ringing")
        self.refresh()
        self.assertFalse(self.reviews)
        self.assertTrue(any("unavailable on your trial" in item.value for item in self.app.info))
        self.refresh()
        self.app.run()
        self.assertEqual([r.method for r in self.requests], ["POST", "GET", "GET", "GET"])
        self.assertTrue(all(str(r.url) == f"{CALLS_URL}/{CALL_ID}" for r in self.requests[1:]))
        self.assertTrue(all(r.headers["x-api-key"] == KEY for r in self.requests[1:]))
        self.assertEqual(len(self.reviews), 1)
        self.assert_saved_review()
        self.assertNotIn(NUMBER, str(self.app))
        self.assertNotIn(KEY, str(self.app))

    def test_cloud_secrets_and_manual_import_when_trial_has_no_transcript(self):
        self.app.secrets["HUSKYVOICE_API_KEY"] = KEY
        self.app.secrets["CALL_TO_NUMBER"] = NUMBER
        self.app.secrets["HUSKYVOICE_AGENT_ID"] = AGENT_ID
        (self.records.parent / ".env").write_text("HUSKYVOICE_API_KEY=\nCALL_TO_NUMBER=\n", encoding="utf-8")
        self.responses = [(201, {"data": {"call_id": CALL_ID}}), (200, {"data": {"status": "completed"}})]
        self.app.run()
        self.dial()
        self.refresh()
        self.assertFalse(self.reviews)
        self.assertFalse(self.records.exists())
        self.app.text_area[0].set_value("Summary: the call went well.")
        self.button("Import and review").click().run()
        self.assertTrue(any("speaker label" in item.value for item in self.app.error))
        self.assertFalse(self.reviews)
        self.app.text_area[0].set_value("\n".join(
            f"{'Sarah' if turn['speaker'] == 'agent' else 'Representative'}: {turn['text']}" for turn in TRANSCRIPT
        ))
        self.button("Import and review").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.reviews), 1)
        self.assert_saved_review()

    def test_safe_errors_and_a_distinct_key_for_each_new_click(self):
        self.responses = [
            (401, {"error": f"Invalid key {KEY}"}),
            (403, {"error": f"Missing scope for {NUMBER}"}),
            (400, {"error": {"code": "NO_ASSIGNED_PHONE_NUMBER", "message": NUMBER}}),
            (None, httpx.ReadTimeout(f"uncertain response for {NUMBER}")),
        ]
        self.app.run()
        for message in ("rejected the API key", "calls:create", "no outbound caller number available", "did not confirm"):
            self.dial()
            errors = " ".join(item.value for item in self.app.error)
            self.assertIn(message, errors)
            self.assertNotIn(NUMBER, errors)
            self.assertNotIn(KEY, errors)
        self.assertTrue(self.button("Call my phone").disabled)
        self.app.run()
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(len({r.headers["Idempotency-Key"] for r in self.requests}), 4)
        self.button("I checked that no call is active").click().run()
        self.assertFalse(self.button("Call my phone").disabled)
        self.assertEqual(len(self.requests), 4)

    def test_failed_call_shows_missing_outbound_number_without_echoing_provider_data(self):
        self.responses = [
            (201, {"data": {"call_id": CALL_ID, "status": "scheduled"}}),
            (200, {"data": {"call_id": CALL_ID, "status": "failed", "call_duration_seconds": None,
                            "error": f"No outbound numbers available for agent {AGENT_ID} ({NUMBER}) {KEY}"}}),
        ]
        self.app.run()
        self.dial()
        self.refresh()
        self.assertEqual(self.app.metric[0].value, "Call failed")
        self.assertIn("no outbound caller number available for the configured agent", self.app.error[0].value)
        self.assertNotIn(NUMBER, str(self.app))
        self.assertNotIn(KEY, str(self.app))
        self.assertFalse(any("unavailable on your trial" in item.value for item in self.app.info))
        self.assertFalse(self.reviews)
        self.assertEqual([request.method for request in self.requests], ["POST", "GET"])

    def test_missing_agent_id_disables_dial(self):
        (self.records.parent / ".env").write_text(
            f"HUSKYVOICE_API_KEY={KEY}\nCALL_TO_NUMBER={NUMBER}\n", encoding="utf-8"
        )
        self.app.run()
        self.assertTrue(self.button("Call my phone").disabled)
        self.assertTrue(any("HUSKYVOICE_AGENT_ID" in item.value for item in self.app.warning))
        self.assertFalse(self.requests)

    def test_no_call_id_shows_sanitized_create_error(self):
        self.responses = [
            (422, {"error": {"message": f"Destination was rejected: {NUMBER}; x-api-key={KEY}"}}),
            (200, {"data": {}, "error": {"message": "Provider queue is paused."}}),
        ]
        self.app.run()
        for reason in ("HTTP 422", "Provider queue is paused."):
            self.dial()
            message = next(item.value for item in self.app.text if item.value.startswith("HuskyVoice create call error"))
            self.assertIn(reason, message)
            self.assertNotIn(KEY, message)
            self.assertNotIn(NUMBER, message)
            self.assertFalse(any(b.label == "Refresh call status" for b in self.app.button))
        self.assertEqual([request.method for request in self.requests], ["POST", "POST"])

    def test_refresh_shows_latest_get_error_and_clears_it_after_success(self):
        self.responses = [
            (201, {"data": {"call_id": CALL_ID, "status": "queued"}}),
            (200, {"data": {"call_id": CALL_ID, "status": "failed", "error": f"Carrier rejected {NUMBER}."}}),
            (503, {"error": {"message": f"Status service unavailable for x-api-key={KEY}"}}),
            (200, {"data": {"call_id": CALL_ID, "status": "completed", "call_transcript": TRANSCRIPT}}),
        ]
        self.app.run()
        self.dial()
        self.refresh()
        detail = next(item.value for item in self.app.text if item.value.startswith("HuskyVoice call status error"))
        self.assertIn("Carrier rejected", detail)
        self.assertNotIn(NUMBER, detail)
        self.refresh()
        detail = next(item.value for item in self.app.text if item.value.startswith("HuskyVoice call status error"))
        self.assertIn("HTTP 503", detail)
        self.assertIn("Status service unavailable", detail)
        self.assertNotIn(KEY, detail)
        self.refresh()
        self.assertFalse(any("HuskyVoice call status error" in item.value for item in self.app.text))
        self.assertFalse(self.app.error)
        self.assertEqual([request.method for request in self.requests], ["POST", "GET", "GET", "GET"])

    def test_status_permission_error_keeps_original_attempt(self):
        self.responses = [(201, {"data": {"call_id": CALL_ID}}), (403, {"error": "Forbidden"})]
        self.app.run()
        self.dial()
        self.refresh()
        self.assertTrue(any("calls:read" in item.value for item in self.app.error))
        self.assertTrue(self.button("Call my phone").disabled)
        self.assertEqual(len(self.requests), 2)

    def test_signed_transcript_download_uses_no_api_key(self):
        settings = HuskySettings(KEY, NUMBER, AGENT_ID)
        with patch("ems_voice.huskyvoice.httpx.get", side_effect=self.transport.get):
            self.responses = [(200, TRANSCRIPT)]
            turns = HuskyVoice(settings).transcript({"call_transcript": SIGNED_URL})
            self.assertEqual(len(turns), len(TRANSCRIPT))
            self.assertNotIn("x-api-key", self.requests[0].headers)
            with self.assertRaisesRegex(ValueError, "transcript link is unavailable"):
                HuskyVoice(settings).transcript({"call_transcript": "https://127.0.0.1/transcript.json"})
            self.assertEqual(len(self.requests), 1)
