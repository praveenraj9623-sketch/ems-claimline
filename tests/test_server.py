import json
import tempfile
import threading
import unittest
from dataclasses import replace
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from xml.etree import ElementTree
from unittest.mock import patch

from claimline.config import Settings
from claimline.groq import GroqClient
from claimline.server import Service, create_server
from claimline.twilio_api import compute_signature

PUBLIC = "https://demo.ngrok.app"
TOKEN = "secret-token"


class FakeTwilio(BaseHTTPRequestHandler):
    calls = []
    fail_code = None

    def do_POST(self):
        body = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        type(self).calls.append((self.path, body, self.headers.get("Authorization")))
        if type(self).fail_code:
            payload, status = {"code": type(self).fail_code, "message": "nope"}, 400
        else:
            payload, status = {"sid": "CA" + "1" * 32}, 201
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class ServerCase(unittest.TestCase):
    trial_mode = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        FakeTwilio.calls, FakeTwilio.fail_code = [], None
        self.twilio_http = HTTPServer(("127.0.0.1", 0), FakeTwilio)
        threading.Thread(target=self.twilio_http.serve_forever, daemon=True).start()
        self.settings = Settings(
            twilio_account_sid="AC" + "0" * 32, twilio_auth_token=TOKEN, twilio_from_number="+15005550006",
            call_to_number="+919876543210", public_base_url=PUBLIC, port=0, calls_dir=Path(self.tmp.name),
            twilio_api_base=f"http://127.0.0.1:{self.twilio_http.server_address[1]}",
            twilio_trial_mode=self.trial_mode,
            twilio_trial_webhook_token="private-trial-token" if self.trial_mode else "")
        service = Service(self.settings, groq=GroqClient(""), finalize_delay=0.05)
        service._preflight = lambda base: None
        self.server, self.service = create_server(self.settings, service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.twilio_http.shutdown()
        self.server.server_close()
        self.twilio_http.server_close()
        self.tmp.cleanup()

    def api(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def twilio(self, path, params, *, sign=True):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        form = {k: [v] for k, v in params.items()}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if sign:
            headers["X-Twilio-Signature"] = compute_signature(TOKEN, PUBLIC + path, form)
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(params).encode(),
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


class ChatApi(ServerCase):
    def test_browser_voice_requires_playback_acknowledgement(self):
        _, state = self.api("/api/web/start", {"mode": "voice"})
        call_id = state["id"]
        for message in ("paid", "240 dollars", "EFT 9134", "March 20, 2026"):
            playback_turn = state["reply"]["playback_turn"]
            status, _ = self.api(f"/api/web/{call_id}/played", {"turn": playback_turn})
            self.assertEqual(status, 200)
            _, state = self.api(f"/api/web/{call_id}/say", {"text": message})
        readback_turn = state["reply"]["playback_turn"]
        self.assertEqual(state["transcript"][-1]["playback"], "pending")
        _, state = self.api(f"/api/web/{call_id}/end", {})
        self.assertFalse(state["scorecard"]["read_back_done"])
        self.assertEqual(state["transcript"][readback_turn - 1]["playback"], "unconfirmed")
        status, _ = self.api(f"/api/web/{call_id}/played", {"turn": readback_turn})
        self.assertEqual(status, 409)

    def test_browser_voice_completed_readback_is_credited(self):
        _, state = self.api("/api/web/start", {"mode": "voice"})
        call_id = state["id"]
        for message in ("paid", "240 dollars", "EFT 9134", "March 20, 2026", "yes"):
            self.api(f"/api/web/{call_id}/played", {"turn": state["reply"]["playback_turn"]})
            _, state = self.api(f"/api/web/{call_id}/say", {"text": message})
        self.api(f"/api/web/{call_id}/played", {"turn": state["reply"]["playback_turn"]})
        _, state = self.api(f"/api/web/{call_id}/end", {})
        self.assertTrue(all(state["scorecard"].values()))

    def test_invalid_playback_acknowledgements_are_rejected(self):
        _, state = self.api("/api/web/start", {"mode": "voice"})
        call_id = state["id"]
        self.api(f"/api/web/{call_id}/say", {"text": "paid"})
        for turn in (None, True, "1", 0, -1, 999, 2):
            with self.subTest(turn=turn):
                status, _ = self.api(f"/api/web/{call_id}/played", {"turn": turn})
                self.assertEqual(status, 400)

    def test_full_chat_call_saves_exact_json_files(self):
        status, s = self.api("/api/web/start", {"mode": "chat"})
        self.assertEqual(status, 200)
        cid = s["id"]
        for text in ["paid", "$240", "EFT 9134", "March 20, 2026"]:
            _, s = self.api(f"/api/web/{cid}/say", {"text": text})
        self.assertIn("read back", s["reply"]["says"][0]["text"])
        _, s = self.api(f"/api/web/{cid}/say", {"text": "yes"})
        self.assertTrue(s["reply"]["end"])
        _, s = self.api(f"/api/web/{cid}/end", {})
        self.assertTrue(s["finished"])
        folder = Path(self.tmp.name) / cid
        result = json.loads((folder / "claim_result.json").read_text())
        card = json.loads((folder / "review.json").read_text())
        self.assertEqual(list(result), ["claim_status", "paid_amount", "check_or_eft_number", "payment_date"])
        self.assertEqual(result["payment_date"], "2026-03-20")
        self.assertEqual(list(card), ["introduced_as_billing_office", "claim_number_stated",
                                      "all_four_facts_collected", "read_back_done", "no_invented_data"])
        self.assertTrue(all(card.values()))

    def test_input_validation_and_isolation(self):
        self.assertEqual(self.api("/api/web/start", {"mode": "chat"})[0], 200)
        self.assertEqual(self.api("/api/web/" + "a" * 32 + "/say", {"text": "hi"})[0], 404)
        _, s = self.api("/api/web/start", {"mode": "chat"})
        self.assertEqual(self.api(f"/api/web/{s['id']}/say", {"text": ""})[0], 400)
        req = urllib.request.Request(self.base + "/api/web/start", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 415)

    def test_dashboard_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"EMS ClaimLine", r.read())

    def test_tunnel_only_exposes_twilio_webhooks(self):
        req = urllib.request.Request(self.base + "/api/config", headers={"Host": "demo.ngrok.app"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 404)


class PhoneCall(ServerCase):
    def test_holding_beyond_the_legacy_timeout_stays_silent(self):
        self.service.settings = replace(self.settings, max_hold_seconds=1)
        _, body = self.api("/api/phone/call", {})
        call_id = body["id"]
        self.twilio(f"/twilio/answer?sid={call_id}", {"CallSid": "CAhold"})
        self.twilio(f"/twilio/turn?sid={call_id}", {"CallSid": "CAhold", "SpeechResult": "Please hold"})
        with patch("claimline.server.time.time", return_value=4_000_000_000):
            for _ in range(20):
                _, xml = self.twilio(f"/twilio/turn?sid={call_id}", {"CallSid": "CAhold"})
                self.assertNotIn("<Say", xml)
                self.assertNotIn("<Hangup", xml)
        session = self.service.get(call_id)
        self.assertEqual(session.agent.phase, "hold")
        self.assertFalse(session.finalized)
        self.assertEqual(sum("I'll wait" in t["text"] for t in session.agent.transcript), 1)

    def test_hangup_during_readback_is_not_credited(self):
        _, body = self.api("/api/phone/call", {})
        call_id = body["id"]
        _, xml = self.twilio(f"/twilio/answer?sid={call_id}", {"CallSid": "CAinterrupted"})
        for speech in ("paid", "240 dollars", "EFT 9134", "March 20, 2026"):
            action = urllib.parse.urlsplit(ElementTree.fromstring(xml).find("Gather").attrib["action"])
            _, xml = self.twilio(action.path + "?" + action.query,
                                  {"CallSid": "CAinterrupted", "SpeechResult": speech})
        self.assertIn("read back", xml)
        self.twilio(f"/twilio/status?sid={call_id}",
                    {"CallSid": "CAinterrupted", "CallStatus": "completed"})
        _, state = self.api(f"/api/calls/{call_id}")
        self.assertTrue(state["scorecard"]["all_four_facts_collected"])
        self.assertFalse(state["scorecard"]["read_back_done"])
        self.assertEqual(state["transcript"][-1]["playback"], "unconfirmed")

    def test_hangup_during_introduction_is_not_credited(self):
        _, body = self.api("/api/phone/call", {})
        call_id = body["id"]
        self.twilio(f"/twilio/answer?sid={call_id}", {"CallSid": "CAopening"})
        self.twilio(f"/twilio/status?sid={call_id}", {"CallSid": "CAopening", "CallStatus": "completed"})
        _, state = self.api(f"/api/calls/{call_id}")
        self.assertFalse(state["scorecard"]["introduced_as_billing_office"])
        self.assertFalse(state["scorecard"]["claim_number_stated"])

    def test_places_call_with_correct_twilio_request(self):
        status, body = self.api("/api/phone/call", {})
        self.assertEqual(status, 200, body)
        path, form, auth = FakeTwilio.calls[0]
        self.assertTrue(path.endswith("/Calls.json"))
        self.assertEqual(form["To"], ["+919876543210"])
        self.assertEqual(form["From"], ["+15005550006"])
        self.assertTrue(form["Url"][0].startswith(PUBLIC + "/twilio/answer?sid="))
        self.assertIn("completed", form["StatusCallbackEvent"])
        self.assertTrue(auth.startswith("Basic "))
        self.assertEqual(self.api("/api/phone/call", {})[0], 409)  # one call at a time

    def test_twilio_error_is_explained(self):
        FakeTwilio.fail_code = 21219
        status, body = self.api("/api/phone/call", {})
        self.assertEqual(status, 502)
        self.assertIn("verified", body["error"])

    def test_missing_settings_are_named(self):
        self.service.settings = Settings(port=0, calls_dir=Path(self.tmp.name))
        status, body = self.api("/api/phone/call", {})
        self.assertEqual(status, 400)
        self.assertIn("TWILIO_ACCOUNT_SID", body["error"])

    def test_rejects_unsigned_webhooks(self):
        self.assertEqual(self.twilio("/twilio/answer?sid=" + "b" * 32, {"CallSid": "CA1"}, sign=False)[0], 403)

    def test_full_phone_conversation(self):
        _, body = self.api("/api/phone/call", {})
        sid = body["id"]
        turn = f"/twilio/turn?sid={sid}"
        _, xml = self.twilio(f"/twilio/answer?sid={sid}", {"CallSid": "CA1", "CallStatus": "in-progress"})
        self.assertIn("<Gather", xml)
        self.assertIn("billing office of Cedar Vale EMS", xml)
        self.assertIn('speechModel="phone_call"', xml)
        self.assertIn("TEST", xml)

        def say(text):
            nonlocal xml
            action = urllib.parse.urlsplit(ElementTree.fromstring(xml).find("Gather").attrib["action"])
            xml = self.twilio(action.path + "?" + action.query, {"CallSid": "CA1", "SpeechResult": text})[1]
            return xml

        self.assertIn("What amount was paid?", say("The status is paid"))
        hold = say("Please hold")
        self.assertIn("ll wait", hold)
        quiet = say("")
        self.assertNotIn("<Say", quiet)            # silent while on hold
        self.assertNotIn("<Hangup", quiet)
        self.assertIn('timeout="30"', quiet)
        spelled = say("I'm back, could you say the claim number slowly?")
        self.assertEqual(spelled.count('<break time="650ms"/>'), 10)
        self.assertIn("What amount was paid?", spelled)
        say("two hundred forty dollars")
        say("EFT nine one three four")
        self.assertNotIn("<Say", say("2026"))      # waits for the rest of the date
        readback = say("0320")
        self.assertIn("March 20, 2026", readback)
        self.assertIn("Is that correct?", readback)
        bye = say("Yes, correct")
        self.assertIn("<Hangup/>", bye)
        self.twilio(f"/twilio/status?sid={sid}", {"CallSid": "CA1", "CallStatus": "completed"})
        _, state = self.api(f"/api/calls/{sid}")
        self.assertTrue(state["finished"])
        self.assertEqual(state["result"], {"claim_status": "paid", "paid_amount": 240.0,
                                           "check_or_eft_number": "9134", "payment_date": "2026-03-20"})
        self.assertTrue(all(state["scorecard"].values()))
        self.assertTrue((Path(self.tmp.name) / sid / "review.json").exists())

    def test_hangup_midway_is_scored_truthfully(self):
        _, body = self.api("/api/phone/call", {})
        sid = body["id"]
        self.twilio(f"/twilio/answer?sid={sid}", {"CallSid": "CA2"})
        self.twilio(f"/twilio/turn?sid={sid}", {"CallSid": "CA2", "SpeechResult": "paid"})
        self.twilio(f"/twilio/status?sid={sid}", {"CallSid": "CA2", "CallStatus": "completed"})
        _, state = self.api(f"/api/calls/{sid}")
        self.assertFalse(state["scorecard"]["read_back_done"])
        self.assertFalse(state["scorecard"]["all_four_facts_collected"])

    def test_unanswered_call_makes_no_scorecard(self):
        _, body = self.api("/api/phone/call", {})
        sid = body["id"]
        self.twilio(f"/twilio/status?sid={sid}", {"CallSid": "CA3", "CallStatus": "no-answer"})
        _, state = self.api(f"/api/calls/{sid}")
        self.assertTrue(state["finished"])
        self.assertIsNone(state["scorecard"])

    def test_retried_webhook_does_not_repeat_the_agent(self):
        _, body = self.api("/api/phone/call", {})
        sid = body["id"]
        a = self.twilio(f"/twilio/answer?sid={sid}", {"CallSid": "CA4"})[1]
        b = self.twilio(f"/twilio/answer?sid={sid}", {"CallSid": "CA4"})[1]
        self.assertEqual(a, b)
        x = self.twilio(f"/twilio/turn?sid={sid}", {"CallSid": "CA4", "SpeechResult": "paid"})[1]
        y = self.twilio(f"/twilio/turn?sid={sid}", {"CallSid": "CA4", "SpeechResult": "paid"})[1]
        self.assertEqual(x, y)
        _, state = self.api(f"/api/calls/{sid}")
        self.assertEqual(sum(t["speaker"] == "representative" for t in state["transcript"]), 1)


class TrialPhoneCall(ServerCase):
    trial_mode = True

    def test_trial_proxy_conversation_and_completion(self):
        status, body = self.api("/api/phone/call", {})
        self.assertEqual(status, 200, body)
        form = FakeTwilio.calls[0][1]
        self.assertEqual(set(form), {"To", "From", "Url", "StatusCallback"})
        answer = urllib.parse.urlsplit(form["Url"][0])
        status_url = urllib.parse.urlsplit(form["StatusCallback"][0])
        self.assertIn("trial_token=private-trial-token", answer.query)
        self.assertIn("trial_token=private-trial-token", status_url.query)
        params = {"CallSid": "CAtrial", "CallStatus": "in-progress"}
        status, xml = self.twilio(answer.path + "?" + answer.query, params, sign=False)
        self.assertEqual(status, 200, xml)
        self.assertIn("billing office of Cedar Vale EMS", xml)
        for speech in ("paid", "two hundred forty dollars", "EFT nine one three four", "March 20, 2026", "yes"):
            tree = ElementTree.fromstring(xml)
            action = tree.find("Gather").attrib["action"]
            self.assertEqual(tree.find("Redirect").text, action)
            parsed = urllib.parse.urlsplit(action)
            self.assertIn("trial_token=private-trial-token", parsed.query)
            status, xml = self.twilio(parsed.path + "?" + parsed.query,
                                      {"CallSid": "CAtrial", "SpeechResult": speech}, sign=False)
            self.assertEqual(status, 200, xml)
        self.assertIn("<Hangup/>", xml)
        status, _ = self.twilio(status_url.path + "?" + status_url.query,
                                {"CallSid": "CAtrial", "CallStatus": "completed"}, sign=False)
        self.assertEqual(status, 204)
        _, state = self.api(f"/api/calls/{body['id']}")
        self.assertTrue(state["finished"])
        self.assertEqual(state["result"], {"claim_status": "paid", "paid_amount": 240.0,
                                           "check_or_eft_number": "9134", "payment_date": "2026-03-20"})
        self.assertTrue(all(state["scorecard"].values()))

    def test_trial_proxy_rejects_missing_wrong_and_duplicate_tokens(self):
        for query in ("", "?trial_token=wrong", "?trial_token=private-trial-token&trial_token=wrong",
                      "?trial_token=%E2%98%83"):
            with self.subTest(query=query):
                status, _ = self.twilio("/twilio/answer" + query, {"CallSid": "CAtrial"}, sign=False)
                self.assertEqual(status, 403)
        self.assertEqual(len(self.service.sessions), 0)

    def test_token_requires_trial_mode_and_signed_callbacks_still_work(self):
        path = "/twilio/answer?trial_token=private-trial-token"
        self.service.settings = replace(self.settings, twilio_trial_mode=False)
        self.assertEqual(self.twilio(path, {"CallSid": "CAtrial"}, sign=False)[0], 403)
        self.service.settings = self.settings
        self.assertEqual(self.twilio("/twilio/answer", {"CallSid": "CAtrial"})[0], 200)
        self.service.settings = replace(self.settings, twilio_trial_webhook_token="")
        self.assertEqual(self.twilio("/twilio/answer", {"CallSid": "CAtrial"}, sign=False)[0], 403)


if __name__ == "__main__":
    unittest.main()
