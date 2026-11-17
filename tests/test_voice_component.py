"""Hosted browser Voice uses session-owned Streamlit messages, without HTTP."""

import tempfile
import unittest
from pathlib import Path

from claimline.config import Settings
from claimline.groq import GroqClient
from claimline.server import ApiError, Service
from claimline.voice_component import dispatch_voice_request


class VoiceRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(Settings(calls_dir=Path(self.tmp.name)), groq=GroqClient(""))
        self.call_id = None
        self.sequence = 0

    def send(self, action, **body):
        self.sequence += 1
        state = dispatch_voice_request(self.service, {"id": str(self.sequence), "action": action,
                                       "call_id": self.call_id, "body": body}, self.call_id)
        if action == "start":
            self.call_id = state["id"]
        return state

    def test_complete_call_playback_hold_correction_and_review(self):
        state = self.send("start")
        for text in ["Please hold", "I'm back. The claim is paid", "$450", "EFT 9421",
                     "May 2 2026", "No, the amount is $550", "Everything is correct"]:
            self.send("played", turn=state["reply"]["playback_turn"])
            state = self.send("say", text=text)
        self.send("played", turn=state["reply"]["playback_turn"])
        state = self.send("end")
        self.assertTrue(state["finished"])
        self.assertEqual(state["result"], {"claim_status": "paid", "paid_amount": 550.0,
                         "check_or_eft_number": "9421", "payment_date": "2026-05-02"})
        self.assertEqual(set(state["scorecard"]), {"introduced_as_billing_office", "claim_number_stated",
                         "all_four_facts_collected", "read_back_done", "no_invented_data"})
        self.assertTrue(all(state["scorecard"].values()))

    def test_missing_values_stay_null_and_unplayed_readback_is_not_credited(self):
        state = self.send("start")
        self.send("played", turn=state["reply"]["playback_turn"])
        self.send("say", text="paid")
        state = self.send("end")
        self.assertIsNone(state["result"]["paid_amount"])
        self.assertIsNone(state["result"]["check_or_eft_number"])
        self.assertIsNone(state["result"]["payment_date"])
        self.assertFalse(state["scorecard"]["all_four_facts_collected"])
        self.assertFalse(state["scorecard"]["read_back_done"])

    def test_silence_on_hold_does_not_repeat_or_end(self):
        self.send("start")
        self.send("say", text="Please hold")
        for _ in range(3):
            state = self.send("silence")
            self.assertEqual(state["reply"]["says"], [])
            self.assertFalse(state["reply"]["end"])

    def test_requests_cannot_access_another_browser_or_phone_session(self):
        other = self.service.web_start("voice")
        for action in ("say", "silence", "played", "end"):
            with self.subTest(action=action), self.assertRaises(ApiError) as error:
                dispatch_voice_request(self.service, {"id": "x", "action": action, "call_id": other["id"],
                                       "body": {"text": "paid", "turn": 1}}, None)
            self.assertEqual(error.exception.status, 403)
        self.assertFalse(self.service.get(other["id"]).finalized)
        phone = self.service.create("phone")
        with self.assertRaises(ApiError):
            dispatch_voice_request(self.service, {"id": "x", "action": "end", "call_id": phone.id}, phone.id)
        self.assertFalse(phone.finalized)

    def test_invalid_messages_and_values_are_rejected(self):
        self.send("start")
        for request in (None, [], {}, {"id": 5}, {"id": ""}, {"id": "x", "action": "dial"},
                        {"id": "x", "action": "start", "body": []}):
            with self.subTest(request=request), self.assertRaises(ApiError):
                dispatch_voice_request(self.service, request, self.call_id)
        for action, body in [("say", {"text": 42}), ("played", {"turn": True}), ("played", {"turn": 999})]:
            with self.subTest(action=action), self.assertRaises(ApiError):
                self.send(action, **body)

    def test_restart_closes_previous_owned_call_and_recovers_from_runtime_restart(self):
        self.send("start")
        previous = self.service.get(self.call_id)
        self.send("start")
        self.assertTrue(previous.finalized)
        self.assertNotEqual(previous.id, self.call_id)
        self.service.sessions.clear()  # equivalent to a restarted cached service
        state = self.send("start")
        self.assertEqual(state["kind"], "voice")
        self.assertFalse(state["finished"])


if __name__ == "__main__":
    unittest.main()
