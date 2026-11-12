import base64
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from requests.exceptions import ReadTimeout
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError
from twilio.http.response import Response as TwilioResponse
from twilio.request_validator import RequestValidator

from ems_voice.outbound import OutboundCalls
from ems_voice.phone import create_app
from ems_voice.phone_records import PhoneRecords
from ems_voice.review import CHECKS
from test_assessment import EXPECTED, PAID
from test_phone import AGENT_AUDIO, FORMAT, HTTPS, OTHER_SID, SID, STREAM, TOKEN, WSS, FakeSpeech, GroqStub, result

ACCOUNT = "AC" + "a" * 32
FROM = "+12025550131"
TO = "+12025550132"
APP = Path(__file__).resolve().parents[1] / "app.py"


class OutboundDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / "phone_calls"
        self.calls = OutboundCalls(self.directory)
        self.speech = FakeSpeech()
        self.provider = GroqStub()
        app = create_app(HTTPS, TOKEN, PhoneRecords(self.directory), self.speech, self.provider, self.provider,
                         outbound=self.calls, account_sid=ACCOUNT)
        self.backend = self.enterContext(TestClient(app))
        (self.root / "app.py").write_bytes(APP.read_bytes())
        self.app = AppTest.from_file(str(self.root / "app.py"), default_timeout=10)
        self.enterContext(patch.dict(os.environ, {
            "VOICE_TRANSPORT": "twilio", "VOICE_PUBLIC_URL": HTTPS, "TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_AUTH_TOKEN": TOKEN,
            "TWILIO_FROM_NUMBER": FROM, "CALL_TO_NUMBER": TO,
        }))
        self.api_requests = []
        self.failure = None
        self.status_during_create = False
        self.enterContext(patch("ems_voice.outbound.httpx.post", side_effect=self.preflight))
        self.enterContext(patch("ems_voice.outbound.TwilioHttpClient.request", autospec=True, side_effect=self.dial))

    def preflight(self, url, *, data, headers, timeout):
        self.assertTrue(url.startswith(HTTPS + "/voice/outbound/"))
        self.assertTrue(url.endswith("/ready"))
        return self.backend.post(urlsplit(url).path, data=data, headers=headers)

    def dial(self, http_client, method, url, **kwargs):
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Calls.json")
        self.assertEqual(kwargs["auth"], (ACCOUNT, TOKEN))
        data = kwargs["data"]
        self.api_requests.append(data)
        if self.failure == "timeout":
            raise ReadTimeout()
        if self.failure == "unverified":
            return TwilioResponse(400, json.dumps({"code": 21219, "message": "Unverified phone number."}))
        if self.failure == "blocked":
            message = f"Outbound dialing blocked for {TO} on account {ACCOUNT} with {TOKEN}."
            return TwilioResponse(400, json.dumps({"code": 21203, "message": message}))
        if self.failure == "trial":
            message = "Invalid or disallowed parameters provided - trial accounts have limited parameter access."
            return TwilioResponse(400, json.dumps({"message": message}))
        if self.status_during_create:
            self.assertEqual(self.progress("ringing", 1).status_code, 204)
        return TwilioResponse(201, json.dumps({"sid": SID, "status": "queued", "account_sid": ACCOUNT}))

    def post(self, url, **fields):
        form = {"AccountSid": ACCOUNT, "Direction": "outbound-api", "CallSid": SID, **fields}
        signature = RequestValidator(TOKEN).compute_signature(url, form)
        return self.backend.post(urlsplit(url).path, data=form, headers={"X-Twilio-Signature": signature})

    def progress(self, state, sequence, **fields):
        return self.post(self.api_requests[-1]["StatusCallback"], CallStatus=state, SequenceNumber=str(sequence), **fields)

    def click(self):
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertEqual([button.label for button in self.app.button], ["Call my phone"])
        self.app.button[0].click().run()
        self.assertFalse(self.app.exception)

    def frame(self, socket, event, **fields):
        socket.send_json({"event": event, "streamSid": STREAM, **fields})

    def listen(self, socket, phrases):
        for _ in range(phrases):
            message = socket.receive_json()
            self.assertEqual(message["event"], "media")
            self.assertEqual(base64.b64decode(message["media"]["payload"]), AGENT_AUDIO)
            mark = socket.receive_json()
            self.assertEqual(mark["event"], "mark")
            self.frame(socket, "mark", mark=mark["mark"])

    def speak(self, socket, text, sequence):
        sample = bytes([sequence]) * 160
        self.speech.script[sample] = [result(text, sequence)]
        self.frame(socket, "media", media={"track": "inbound", "payload": base64.b64encode(sample).decode()})

    def wait_for(self, filename):
        deadline = monotonic() + 5
        path = self.directory / SID / filename
        while not path.is_file() and monotonic() < deadline:
            sleep(.02)
        self.assertTrue(path.is_file())

    def test_click_uses_twilio_calls_api_then_answer_media_and_dashboard_outputs(self):
        self.status_during_create = True
        self.click()
        payload, = self.api_requests
        self.assertEqual((payload["To"], payload["From"]), (TO, FROM))
        self.assertEqual((payload["Method"], payload["StatusCallbackMethod"]), ("POST", "POST"))
        self.assertEqual(payload["StatusCallbackEvent"], ["initiated", "ringing", "answered", "completed"])
        self.assertTrue(payload["Url"].endswith("/answer"))
        self.assertEqual(self.app.metric[0].value, "Ringing")
        self.assertTrue(self.app.button[0].disabled)
        self.app.run()
        with self.assertRaises(AppTestError):
            self.app.button[0].click()
        self.assertEqual(len(self.api_requests), 1)
        self.assertEqual(self.progress("initiated", 0).status_code, 204)
        self.assertEqual(self.calls.latest().status, "ringing")
        self.assertEqual(self.post(payload["Url"], AccountSid="AC" + "b" * 32).status_code, 403)
        self.assertEqual(self.post(payload["Url"], CallSid=OTHER_SID).status_code, 404)

        answer = self.post(payload["Url"], CallStatus="in-progress")
        self.assertEqual(answer.status_code, 200)
        stream_url = ET.fromstring(answer.text).find("Connect/Stream").get("url")
        self.assertEqual(stream_url, f"{WSS}/voice/media/{SID}")
        self.assertEqual(self.progress("in-progress", 2).status_code, 204)
        self.app.run()
        self.assertEqual(self.app.metric[0].value, "Connected")
        signature = RequestValidator(TOKEN).compute_signature(stream_url, {})
        with self.backend.websocket_connect(urlsplit(stream_url).path,
                                            headers={"x-twilio-signature": signature}) as socket:
            socket.send_json({"event": "connected"})
            self.frame(socket, "start", start={"callSid": SID, "streamSid": STREAM,
                                               "mediaFormat": FORMAT, "tracks": ["inbound"]})
            self.listen(socket, 4)
            self.speak(socket, PAID, 1)
            self.listen(socket, 6)
            self.speak(socket, "Yes.", 2)
            self.listen(socket, 2)
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
        self.assertIn("Cedar Vale EMS", self.speech.spoken[0])
        self.assertEqual(self.progress("completed", 3).status_code, 204)
        self.assertEqual(self.progress("completed", 3).status_code, 204)
        self.assertEqual(self.progress("ringing", 1).status_code, 204)
        self.wait_for("review.json")
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.metric[0].value, "Call ended")
        self.assertEqual([json.loads(element.value) for element in self.app.json],
                         [EXPECTED, dict.fromkeys(CHECKS, True)])
        self.assertEqual([tab.label for tab in self.app.tabs], ["Chat", "Book a call"])
        self.assertIn("Phone transcript", [item.label for item in self.app.expander])
        self.assertIn(PAID, [item.value for item in self.app.text])
        self.assertEqual(len(self.provider.reviews), 1)
        self.assertEqual(len(self.api_requests), 1)

    def test_twilio_rejection_is_actionable_and_does_not_disclose_provider_message(self):
        self.failure = "unverified"
        self.click()
        self.assertEqual(self.app.metric[0].value, "Call request failed")
        self.assertIn("Verify your mobile number", self.app.error[0].value)
        self.assertEqual(len(self.api_requests), 1)
        self.assertFalse(self.app.button[0].disabled)
        self.app.run()
        self.assertEqual(len(self.api_requests), 1)

    def test_unmapped_twilio_rejection_shows_reason_without_private_values(self):
        self.failure = "blocked"
        self.click()
        error = self.app.error[0].value
        self.assertIn("21203", error)
        self.assertIn("Outbound dialing blocked", error)
        for private in (TO, ACCOUNT, TOKEN):
            self.assertNotIn(private, error)

    def test_trial_rejection_explains_the_media_streams_restriction(self):
        self.failure = "trial"
        self.click()
        error = self.app.error[0].value
        self.assertIn("400", error)
        self.assertIn("blocks Media Streams", error)
        self.assertIn("Upgrade the account", error)
        self.assertEqual(len(self.api_requests), 1)

    def test_no_answer_shows_twilio_status_and_allows_another_click(self):
        self.click()
        self.assertEqual(self.progress("no-answer", 2).status_code, 204)
        self.app.run()
        self.assertEqual(self.app.metric[0].value, "No answer")
        self.assertIn("did not answer", self.app.error[0].value)
        self.assertFalse(self.app.button[0].disabled)
        self.assertFalse(self.app.json)

    def test_no_dial_when_public_backend_is_unreachable(self):
        import httpx

        with patch("ems_voice.outbound.httpx.post", side_effect=httpx.ConnectError("ngrok unavailable")):
            self.click()
        self.assertIn("backend is unreachable", self.app.error[0].value)
        self.assertEqual(self.api_requests, [])

    def test_uncertain_request_blocks_redial_and_accepts_late_callback(self):
        self.failure = "timeout"
        self.click()
        self.assertEqual(self.app.metric[0].value, "Dial request unconfirmed")
        self.assertTrue(self.app.button[0].disabled)
        with self.assertRaises(AppTestError):
            self.app.button[0].click()
        self.assertEqual(len(self.api_requests), 1)
        self.assertEqual(self.progress("ringing", 1).status_code, 204)
        self.app.run()
        self.assertEqual(self.app.metric[0].value, "Ringing")

    def test_claim_remains_visible_when_review_fails(self):
        self.provider.review_failure = True
        self.click()
        answer = self.post(self.api_requests[0]["Url"])
        stream_url = ET.fromstring(answer.text).find("Connect/Stream").get("url")
        signature = RequestValidator(TOKEN).compute_signature(stream_url, {})
        with self.backend.websocket_connect(urlsplit(stream_url).path,
                                            headers={"x-twilio-signature": signature}) as socket:
            self.frame(socket, "start", start={"callSid": SID, "streamSid": STREAM,
                                               "mediaFormat": FORMAT, "tracks": ["inbound"]})
            self.listen(socket, 4)
            self.speak(socket, PAID, 1)
            self.listen(socket, 6)
            self.frame(socket, "stop")
        self.assertEqual(self.progress("completed", 3).status_code, 204)
        self.wait_for("review_error.json")
        self.app.run()
        self.assertEqual([json.loads(element.value) for element in self.app.json], [EXPECTED])
        self.assertIn("Temporary review failure", self.app.error[0].value)
        self.provider.review_failure = False
        next(button for button in self.app.button if button.label == "Retry review").click().run()
        self.wait_for("review.json")
        self.app.run()
        self.assertEqual([json.loads(element.value) for element in self.app.json],
                         [EXPECTED, dict.fromkeys(CHECKS, True)])
        self.assertEqual(len(self.provider.reviews), 2)


class ReservationTests(unittest.TestCase):
    def test_two_dashboard_sessions_can_reserve_only_one_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            first, second = OutboundCalls(Path(root)), OutboundCalls(Path(root))
            with ThreadPoolExecutor(2) as pool:
                attempted = list(pool.map(lambda store: store.reserve(None), (first, second)))
            self.assertEqual(sum(attempt is not None for attempt in attempted), 1)
            current = first.latest()
            first.request_error(current.id, "Rejected")
            self.assertIsNone(second.reserve(None))
            self.assertIsNotNone(second.reserve(current.id))
