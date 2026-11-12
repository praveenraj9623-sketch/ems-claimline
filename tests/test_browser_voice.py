import re
import tempfile
import unittest
from pathlib import Path
from time import sleep

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from ems_voice.browser_voice import create_app
from ems_voice.phone_records import PhoneRecords
from ems_voice.review import CHECKS
from ems_voice.workflow import HOLD_REPLY, OPENING, SLOW_ID
from test_assessment import EXPECTED, PAID
from test_phone import AGENT_AUDIO, FakeSpeech, GroqStub, result


class BrowserVoiceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.speech = FakeSpeech()
        self.provider = GroqStub()
        records = PhoneRecords(self.root, re.compile(r"[0-9a-f]{32}\Z"))
        self.client = self.enterContext(TestClient(create_app(records, self.speech, self.provider, self.provider)))
        self.headers = {"host": "127.0.0.1:5001", "origin": "http://127.0.0.1:8501"}
        self.sequence = 0

    def send_audio(self, socket, text):
        self.sequence += 1
        pcm = bytes([self.sequence]) * 640
        self.speech.script[pcm] = [result(text, self.sequence)]
        socket.send_bytes(pcm)

    def speak(self, socket, text):
        self.send_audio(socket, text)
        update = socket.receive_json()
        self.assertEqual(update["event"], "transcript")
        return update

    def hear(self, socket, count, *, spell=False):
        for index in range(count):
            self.assertEqual(socket.receive_bytes(), AGENT_AUDIO)
            if spell and index < count - 1:
                self.assertEqual(socket.receive_bytes(), b"\xff" * 2400)
            mark = socket.receive_json()
            self.assertEqual(mark["event"], "mark")
            socket.send_json(mark)

    def test_live_microphone_hold_spell_back_readback_and_review(self):
        with self.client.websocket_connect("/voice", headers=self.headers) as socket:
            self.assertEqual(socket.receive_json()["turns"][0]["text"], OPENING)
            self.hear(socket, 4)
            hold = self.speak(socket, "Please hold.")
            self.assertEqual(hold["phase"], "hold")
            self.assertEqual(hold["turns"][-1]["text"], HOLD_REPLY)
            self.hear(socket, 1)

            repeated = self.speak(socket, "Please hold.")
            self.assertEqual(repeated["phase"], "hold")
            self.assertEqual(repeated["turns"][-1]["speaker"], "representative")
            spelling = self.speak(socket, "Could you say the claim number slowly?")
            self.assertEqual(spelling["turns"][-1]["text"], SLOW_ID)
            self.hear(socket, len(SLOW_ID.split()), spell=True)

            self.speak(socket, PAID)
            self.hear(socket, 6)
            self.speak(socket, "Yes. Correct.")
            self.hear(socket, 2)
            self.assertEqual(socket.receive_json()["event"], "finishing")
            output = socket.receive_json()
        self.assertEqual(output["claim"], EXPECTED)
        self.assertEqual(output["review"], dict.fromkeys(CHECKS, True))
        self.assertEqual(self.speech.spoken.count(HOLD_REPLY), 1)
        self.assertEqual(len(self.speech.received), 5)
        self.assertEqual(len(list(self.root.glob("*/review.json"))), 1)

    def test_early_end_exports_only_confirmed_playback(self):
        with self.client.websocket_connect("/voice", headers=self.headers) as socket:
            socket.receive_json()
            self.hear(socket, 4)
            self.speak(socket, PAID)
            self.assertEqual(socket.receive_bytes(), AGENT_AUDIO)
            socket.send_json({"event": "end"})
            message = socket.receive()
            while message.get("bytes") is not None or '"finishing"' not in message.get("text", ""):
                self.assertNotEqual(message["type"], "websocket.close")
                message = socket.receive()
            output = socket.receive_json()
        self.assertEqual(output["claim"], EXPECTED)
        self.assertFalse(output["review"]["read_back_done"])
        self.assertFalse(any("Let me read back" in row["text"] for row in output["turns"]))

    def test_rejects_an_unrelated_website_before_opening_speech(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/voice", headers={**self.headers, "origin": "https://example.org"}):
                self.fail("Unexpectedly accepted an untrusted origin")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_payment_date_survives_separate_speech_endpoints(self):
        for segments in (("2026.", "320."), ("2026.", "03.", "20.")):
            with self.subTest(segments=segments):
                self.assert_spoken_date(segments)

    def assert_spoken_date(self, segments):
        with self.client.websocket_connect("/voice", headers=self.headers) as socket:
            socket.receive_json()
            self.hear(socket, 4)
            self.speak(socket, "The claim was paid for $240. The EFT number is EFT-9134.")
            self.hear(socket, 1)
            for segment in segments[:-1]:
                self.send_audio(socket, segment)
                sleep(.65)
            update = self.speak(socket, segments[-1])
            self.assertEqual(update["turns"][-2]["text"], " ".join(segments).replace(". ", " "))
            self.assertIn("Payment date is March 20, 2026.", update["turns"][-1]["text"])
            self.hear(socket, 6)
            self.speak(socket, "Yes, that is correct.")
            self.hear(socket, 2)
            self.assertEqual(socket.receive_json()["event"], "finishing")
            output = socket.receive_json()
        self.assertEqual(output["claim"], EXPECTED)
        self.assertEqual(output["review"], dict.fromkeys(CHECKS, True))
