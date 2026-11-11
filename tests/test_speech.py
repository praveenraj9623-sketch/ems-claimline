import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from ems_voice.speech import DeepgramSpeech


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
