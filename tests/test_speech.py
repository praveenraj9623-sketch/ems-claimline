import json
import unittest
import wave
from contextlib import asynccontextmanager
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from ems_voice.provider import Config, GroqClient
from ems_voice.speech import DeepgramSpeech, browser_audio


class BrowserSpeechTests(unittest.TestCase):
    def test_transcription_uses_groq_whisper_multipart(self):
        recording = BytesIO()
        with wave.open(recording, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x01" * 160)
        client = GroqClient(Config("test-key", "extract", "review"))
        with patch.object(client, "post", return_value={"text": " Please hold. "}) as post:
            self.assertEqual(client.transcribe(recording.getvalue()), "Please hold.")
            with self.assertRaises(ValueError):
                client.transcribe(b"not a WAV")
        post.assert_called_once()
        endpoint, body, content_type = post.call_args.args
        self.assertEqual(endpoint, "audio/transcriptions")
        self.assertIn(b"whisper-large-v3-turbo", body)
        self.assertIn(recording.getvalue(), body)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))

    def test_browser_uses_mp3_without_changing_phone_audio_format(self):
        def speak(request):
            self.assertEqual(request.url.path, "/v1/speak")
            self.assertEqual(dict(request.url.params), {"model": "test-voice", "encoding": "mp3"})
            self.assertEqual(json.loads(request.content), {"text": "Please hold."})
            return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=b"ID3 test")

        with httpx.Client(transport=httpx.MockTransport(speak)) as client:
            with patch("ems_voice.speech.httpx.stream", side_effect=client.stream):
                self.assertEqual(browser_audio("test-key", "test-voice", "Please hold."), b"ID3 test")


class SpeechTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesis_streams_headerless_telephone_audio(self):
        requests = []
        raw_audio = b"\x81\x82" * 900

        def speak(request):
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "audio/mulaw"}, content=raw_audio)

        client = httpx.AsyncClient(transport=httpx.MockTransport(speak))
        speech = DeepgramSpeech("test-key", "nova-3", "aura-2-thalia-en")
        with patch("ems_voice.speech.httpx.AsyncClient", return_value=client):
            async with speech.synthesizer() as audio:
                chunks = [chunk async for chunk in audio("Hello.")]
        self.assertEqual(list(map(len, chunks)), [800, 800, 200])
        self.assertEqual(b"".join(chunks), raw_audio)
        self.assertEqual(requests[0].url.path, "/v1/speak")
        self.assertEqual(dict(requests[0].url.params), {
            "model": "aura-2-thalia-en", "encoding": "mulaw", "sample_rate": "8000", "container": "none",
        })
        self.assertEqual(json.loads(requests[0].content), {"text": "Hello."})

    async def test_transcription_uses_binary_audio_and_text_controls(self):
        sent = []

        class Connection:
            async def send(self, packet):
                sent.append(packet)

        @asynccontextmanager
        async def connect(url, **kwargs):
            address = urlsplit(url)
            self.assertEqual((address.scheme, address.netloc, address.path),
                             ("wss", "api.deepgram.com", "/v1/listen"))
            params = parse_qs(address.query)
            self.assertEqual([params[key][0] for key in ("encoding", "sample_rate", "interim_results")],
                             ["mulaw", "8000", "true"])
            self.assertEqual(kwargs["additional_headers"]["Authorization"], "Token test-key")
            yield Connection()

        speech = DeepgramSpeech("test-key", "nova-3", "aura-2-thalia-en")
        with patch("ems_voice.speech.connect", connect):
            async with speech.listen() as recognizer:
                await recognizer.audio(b"\xff" * 160)
                await recognizer.control("KeepAlive")
                await recognizer.control("CloseStream")
        self.assertEqual(sent[0], b"\xff" * 160)
        self.assertEqual([json.loads(packet) for packet in sent[1:]],
                         [{"type": "KeepAlive"}, {"type": "CloseStream"}])
