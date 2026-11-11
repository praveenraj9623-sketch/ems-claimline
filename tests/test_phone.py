import asyncio
import base64
import json
import tempfile
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from ems_voice.fields import CORE_KEYS, FIELDS, candidate
from ems_voice.media_call import FORMAT, SPELL_PAUSE, TurnBuffer
from ems_voice.phone import create_app
from ems_voice.phone_records import PhoneRecords
from ems_voice.provider import Config, ServiceError
from ems_voice.review import CHECKS, practice_review
from ems_voice.workflow import GOODBYE, HOLD_REPLY, OPENING, READBACK_PREFIX, SLOW_ID
from test_assessment import EXPECTED, PAID

HTTPS = "https://voice.example.test"
WSS = "wss://voice.example.test"
TOKEN = "offline-test-token"
SID = "CA" + "1" * 32
OTHER_SID = "CA" + "2" * 32
STREAM = "MZ" + "3" * 32
AGENT_AUDIO = b"\xfa\xfb" * 80


def result(text, time, *, final=True, end=True, confidence=.99):
    return {"type": "Results", "start": time, "duration": .75, "is_final": final, "speech_final": end,
            "channel": {"alternatives": [{"transcript": text, "confidence": confidence,
                                          "words": [{"end": time + .5}] if text else []}]}}


class FakeTranscription:
    def __init__(self, speech):
        self.speech = speech
        self.queue = asyncio.Queue()

    async def audio(self, payload):
        self.speech.received.append(payload)
        for event in self.speech.script[payload]:
            await self.queue.put(event)

    async def control(self, event):
        self.speech.controls.append(event)
        if event == "CloseStream":
            for result in self.speech.flush:
                await self.queue.put(result)
            await self.queue.put(None)

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event


class FakeSpeech:
    def __init__(self):
        self.script = {}
        self.received = []
        self.controls = []
        self.spoken = []
        self.flush = []
        self.empty_audio = False
        self.block_after_audio = None
        self.canceled = []

    @asynccontextmanager
    async def listen(self):
        yield FakeTranscription(self)

    @asynccontextmanager
    async def synthesizer(self):
        async def audio(text):
            self.spoken.append(text)
            if not self.empty_audio:
                yield AGENT_AUDIO
                if text == self.block_after_audio:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.canceled.append(text)
        yield audio


class GroqStub:
    config = Config("stub-key", "extract", "review")

    def __init__(self):
        self.extractions = []
        self.reviews = []
        self.review_failure = False
        self.optimistic_readback = False

    def structured(self, model, name, instructions, data, schema):
        if name == "claim_facts":
            assert model == self.config.extract_model
            self.extractions.append(data)
            return {field.key: found[1] if (found := candidate(field, data["latest_representative_turn"],
                                                              data["focus"])) else None for field in FIELDS}
        assert model == self.config.review_model and name == "call_review"
        self.reviews.append(data)
        if self.review_failure:
            raise ServiceError("Temporary review failure.")
        review = practice_review(data["transcript"])
        output = {key: {"passed": review.scorecard[key], "turns": review.evidence[key]} for key in CHECKS}
        if self.optimistic_readback:
            turns = [row["turn"] for row in data["transcript"] if row.get("playback") == "partial"]
            output["read_back_done"] = {"passed": True, "turns": turns}
        return output


class PhoneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.speech = FakeSpeech()
        self.provider = GroqStub()
        self.store = PhoneRecords(self.root)
        self.client = TestClient(create_app(HTTPS, TOKEN, self.store, self.speech, self.provider, self.provider))
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.__enter__()
        self.audio_sequence = 0

    def post(self, path, sid=SID, *, origin=HTTPS, **kwargs):
        data = {"CallSid": sid, "Direction": "inbound", **kwargs}
        signature = RequestValidator(TOKEN).compute_signature(origin + path, data)
        return self.client.post(path, data=data, headers={"X-Twilio-Signature": signature,
                                                        "X-Forwarded-Host": "ignored.example"})

    def incoming(self, sid=SID):
        response = self.post("/voice/incoming", sid)
        self.assertEqual(response.status_code, 200)
        tree = ET.fromstring(response.content)
        self.assertEqual([element.tag for element in tree], ["Connect", "Hangup"])
        url = tree.find("./Connect/Stream").get("url")
        self.assertEqual(url, WSS + f"/voice/media/{sid}")
        return urlsplit(url).path

    def socket_headers(self, path, origin=HTTPS, suffix=""):
        return {"x-twilio-signature": RequestValidator(TOKEN).compute_signature(origin + path + suffix, {})}

    @contextmanager
    def stream(self, sid=SID, *, origin=HTTPS, suffix=""):
        path = self.incoming(sid)
        with self.client.websocket_connect(path, headers=self.socket_headers(path, origin, suffix)) as socket:
            socket.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
            self.frame(socket, "start", start={"callSid": sid, "streamSid": STREAM, "mediaFormat": FORMAT,
                                                "tracks": ["inbound"]})
            yield socket

    def frame(self, socket, event, **extra):
        socket.send_json({"event": event, "streamSid": STREAM, **extra})

    def caller(self, socket, text="", *, events=None):
        self.audio_sequence += 1
        sample = bytes([self.audio_sequence]) * 160
        self.speech.script[sample] = [result(text, self.audio_sequence)] if events is None else events
        self.frame(socket, "media", media={"track": "inbound", "payload": base64.b64encode(sample).decode()})
        return sample

    def hear(self, socket, phrases, *, acknowledge=None):
        marks, audio = [], []
        for _ in range(phrases):
            while (message := socket.receive_json())["event"] == "media":
                self.assertEqual(message["streamSid"], STREAM)
                audio.append(base64.b64decode(message["media"]["payload"]))
            self.assertEqual(message["event"], "mark")
            marks.append(message["mark"]["name"])
            if acknowledge is None or len(marks) <= acknowledge:
                self.frame(socket, "mark", mark=message["mark"])
        self.assertGreaterEqual(len(audio), phrases)
        self.assertTrue(all(chunk in (AGENT_AUDIO, SPELL_PAUSE) for chunk in audio))
        return marks, audio

    def status(self, sid=SID, status="completed"):
        self.assertEqual(self.post("/voice/status", sid, CallStatus=status).status_code, 204)

    def read(self, name, sid=SID):
        path = self.root / sid / name
        deadline = monotonic() + 6
        while not path.is_file() and monotonic() < deadline:
            sleep(.02)
        self.assertTrue(path.is_file(), f"Missing {name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_complete_call_with_correction_and_exact_json(self):
        with self.stream() as socket:
            self.hear(socket, 4)
            incoming_audio = self.caller(socket, PAID)
            self.hear(socket, 6)
            self.caller(socket, "No, the paid amount was $250.25.")
            self.hear(socket, 6)
            self.caller(socket, "Yes, that's correct.")
            self.hear(socket, 2)
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
        self.assertIn(incoming_audio, self.speech.received)
        self.assertIn("T E S T dash four eight two one three", self.speech.spoken[1])
        self.assertIn("Payment date is March 20, 2026.", self.speech.spoken)
        self.assertEqual(self.read("claim_result.json"), {**EXPECTED, "paid_amount": 250.25})
        self.assertEqual(tuple(self.read("claim_result.json")), CORE_KEYS)
        self.assertEqual(self.read("review.json"), dict.fromkeys(CHECKS, True))
        self.assertEqual(tuple(self.read("review.json")), tuple(CHECKS))
        transcript = self.read("transcript.json")
        self.assertEqual(transcript[0]["text"], OPENING)
        self.assertEqual(transcript[-1]["text"], GOODBYE)
        self.assertEqual(self.provider.reviews[0]["transcript"], transcript)
        self.assertEqual(set(self.provider.reviews[0]), {"fixed_claim", "rubric", "transcript"})

    def test_hold_is_silent_through_noise_and_resumes(self):
        with self.stream() as socket:
            self.hear(socket, 4)
            self.caller(socket, "Please hold.")
            self.hear(socket, 1)
            self.caller(socket, "Please hold.")
            self.caller(socket, events=[{"type": "SpeechStarted"}, result("", 3),
                                        result("[background noise]", 4), result("voice", 5, confidence=.15),
                                        {"type": "UtteranceEnd", "last_word_end": 6.0}])
            self.caller(socket, events=[result("I am back.", 8)])
            self.hear(socket, 2)
            self.caller(socket, events=[result(PAID, 9)])
            self.hear(socket, 6)
            self.frame(socket, "stop")
        transcript = self.read("transcript.json")
        agent_text = [row["text"] for row in transcript if row["speaker"] == "agent"]
        self.assertEqual(agent_text[1:3], [HOLD_REPLY, "Thank you. I’m ready when you are."])
        self.assertEqual(len(agent_text), 4)
        self.assertEqual(len(self.provider.extractions), 1)
        self.assertEqual(self.read("claim_result.json"), EXPECTED)

    def test_spell_back_uses_separate_phrases_and_pauses(self):
        with self.stream(origin=WSS, suffix="/") as socket:
            self.hear(socket, 4)
            self.caller(socket, "Could you say the claim number slowly?")
            _, audio = self.hear(socket, 10)
            self.assertEqual(audio.count(SPELL_PAUSE), 9)
            self.assertEqual(" ".join(self.speech.spoken[-10:]), SLOW_ID)
            self.frame(socket, "stop")
        self.assertEqual(self.read("transcript.json")[-1]["text"], SLOW_ID)

    def test_interruption_keeps_only_played_phrases_and_ignores_cleared_marks(self):
        self.provider.optimistic_readback = True
        with self.stream() as socket:
            self.hear(socket, 4)
            self.caller(socket, PAID)
            readback, _ = self.hear(socket, 6, acknowledge=2)
            self.caller(socket, events=[result("No, the paid amount", 3, final=False, end=False)])
            self.assertEqual(socket.receive_json(), {"event": "clear", "streamSid": STREAM})
            for name in reversed(readback):
                self.frame(socket, "mark", mark={"name": name})
            self.frame(socket, "stop")
        self.assertEqual(self.read("claim_result.json"), EXPECTED)
        self.assertEqual(self.read("transcript.json")[-1], {
            "turn": 3, "speaker": "agent", "text": READBACK_PREFIX + "Claim status is paid.", "playback": "partial",
        })
        self.assertFalse(self.read("review.json")["read_back_done"])

    def test_confirmation_during_readback_requires_a_complete_replay(self):
        with self.stream() as socket:
            self.hear(socket, 4)
            self.caller(socket, PAID)
            marks, _ = self.hear(socket, 6, acknowledge=0)
            self.caller(socket, "Yes.")
            self.assertEqual(socket.receive_json()["event"], "clear")
            for name in marks:
                self.frame(socket, "mark", mark={"name": name})
            self.hear(socket, 6)
            self.caller(socket, "Yes.")
            self.hear(socket, 2)
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
        self.assertTrue(self.read("review.json")["read_back_done"])

    def test_barge_in_cancels_in_flight_synthesis(self):
        self.speech.block_after_audio = "Claim status is paid."
        with self.stream() as socket:
            self.hear(socket, 4)
            self.caller(socket, PAID)
            self.hear(socket, 1)
            self.assertEqual(socket.receive_json()["event"], "media")
            self.caller(socket, events=[result("No, wait", 3, final=False, end=False)])
            self.assertEqual(socket.receive_json()["event"], "clear")
            self.frame(socket, "stop")
        self.assertFalse(self.read("review.json")["read_back_done"])
        self.assertEqual(self.speech.canceled, ["Claim status is paid."])

    def test_missing_fields_are_retried_then_saved_as_null(self):
        with self.stream() as socket:
            self.hear(socket, 4)
            self.caller(socket, "The claim is pending.")
            for question in ("What amount was paid?", "What is the check or EFT number?",
                             "On what date was the payment made?"):
                for _ in range(2):
                    self.hear(socket, 1)
                    self.assertEqual(self.speech.spoken[-1], question)
                    self.caller(socket, "That information is unavailable.")
            self.hear(socket, 4)
            self.caller(socket, "Correct.")
            self.hear(socket, 2)
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
        self.assertEqual(self.read("claim_result.json"), {**dict.fromkeys(CORE_KEYS), "claim_status": "pending"})
        self.assertEqual(self.read("review.json"), {**dict.fromkeys(CHECKS, True), "all_four_facts_collected": False})

    def test_early_disconnect_flushes_final_speech_without_inventing_playback(self):
        self.speech.flush = [result(PAID, 3, end=False)]
        with self.stream() as socket:
            self.hear(socket, 4)
            socket.close()
        self.assertEqual(self.read("claim_result.json"), EXPECTED)
        self.assertEqual([row["speaker"] for row in self.read("transcript.json")], ["agent", "representative"])
        self.assertIn("CloseStream", self.speech.controls)
        self.assertFalse(self.read("review.json")["read_back_done"])

    def test_duplicate_callbacks_and_streams_finalize_and_review_once(self):
        path = self.incoming()
        self.assertEqual(self.incoming(), path)
        self.status(status="in-progress")
        with self.stream() as socket:
            self.hear(socket, 4)
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(path, headers=self.socket_headers(path)):
                    self.fail("Duplicate stream accepted")
            self.caller(socket, PAID)
            self.hear(socket, 6)
            self.status()
            self.status()
            self.frame(socket, "stop")
        first = self.read("review.json")
        result_path = self.root / SID / "claim_result.json"
        timestamp = result_path.stat().st_mtime_ns
        self.status()
        self.status()
        self.assertEqual(self.read("review.json"), first)
        self.assertEqual(self.read("claim_result.json"), EXPECTED)
        self.assertEqual(result_path.stat().st_mtime_ns, timestamp)
        self.assertEqual(len(self.provider.reviews), 1)
        self.assertIsNone(ET.fromstring(self.post("/voice/incoming").content).find("Connect"))

    def test_terminal_status_before_stream_saves_an_empty_call(self):
        self.incoming()
        self.status()
        self.status()
        self.assertEqual(self.read("claim_result.json"), dict.fromkeys(CORE_KEYS))
        self.assertEqual(self.read("transcript.json"), [])
        self.assertEqual(self.read("review.json"), {**dict.fromkeys(CHECKS, False), "no_invented_data": True})

    def test_review_failure_is_retryable_without_rewriting_result(self):
        self.provider.review_failure = True
        with self.stream() as socket:
            self.hear(socket, 4)
            self.caller(socket, PAID)
            self.hear(socket, 6)
            self.frame(socket, "stop")
        self.assertTrue(self.read("review_error.json")["retryable"])
        self.assertFalse((self.root / SID / "review.json").exists())
        self.assertEqual(self.read("claim_result.json"), EXPECTED)
        self.provider.review_failure = False
        self.store.queue_retry(SID)
        self.assertTrue(self.read("review.json")["read_back_done"])
        self.assertEqual(len(self.provider.reviews), 2)
        self.assertEqual(self.read("claim_result.json"), EXPECTED)

    def test_synthesis_failure_saves_call_without_crediting_greeting(self):
        self.speech.empty_audio = True
        with self.stream() as socket:
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
        self.assertIn("audio failed", self.read("call_error.json")["error"])
        self.assertEqual(self.read("transcript.json"), [])

    def test_http_and_socket_signatures_bind_to_public_target(self):
        path = "/voice/incoming?example=March%2014%2f2026&extra=%2B"
        self.assertEqual(self.post(path, origin="http://localhost").status_code, 403)
        self.assertEqual(self.post(path, Direction="outbound-api").status_code, 403)
        self.assertEqual(self.post(path, sid="../somewhere").status_code, 400)
        self.assertEqual(self.post(path).status_code, 200)
        media_path = f"/voice/media/{SID}"
        for headers in ({}, self.socket_headers(f"/voice/media/{OTHER_SID}"),
                        self.socket_headers(media_path, origin="http://testserver")):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(media_path, headers=headers):
                    self.fail("Invalid signature accepted")
        self.assertEqual(self.client.get(f"/phone_calls/{SID}/claim_result.json").status_code, 404)


class EndpointTests(unittest.TestCase):
    def test_finals_are_combined_once_and_old_endpoints_are_ignored(self):
        buffer = TurnBuffer()
        self.assertEqual(buffer.event(result("It was paid", 1, end=False)), (True, ""))
        self.assertEqual(buffer.event(result("for 240 dollars", 2, final=False, end=False)), (True, ""))
        self.assertEqual(buffer.event({"type": "UtteranceEnd", "last_word_end": 1.5}), (False, ""))
        self.assertEqual(buffer.event(result("for $240.", 2)), (True, "It was paid for $240."))
        self.assertEqual(buffer.event(result("for $240.", 2)), (False, ""))
        self.assertEqual(buffer.event({"type": "UtteranceEnd", "last_word_end": 2.5}), (False, ""))
        self.assertEqual(buffer.event(result("Correct.", 3, end=False)), (True, ""))
        self.assertEqual(buffer.event({"type": "UtteranceEnd", "last_word_end": 3.5}), (False, "Correct."))
