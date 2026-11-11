"""A single Twilio media stream, with independent listening and playback."""

import asyncio
import base64
import binascii
import copy
import logging
import re
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from .phone_records import PhoneCall, PhoneRecords
from .provider import GroqClient, ServiceError
from .speech import DeepgramSpeech, SpeechError
from .workflow import CLAIM, END, MUSIC, READBACK_PREFIX, SLOW_ID, YES, Call, Turn, append_agent, clean, read_back, reply

LOGGER = logging.getLogger(__name__)
STREAM_SID = re.compile(r"MZ[0-9a-fA-F]{32}\Z")
NOISE = re.compile(r"^\W*(?:(?:background )?noise|applause|static|inaudible|unintelligible)\W*$", re.I)
FORMAT = {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
SPELL_PAUSE = b"\xff" * 2400


class TurnBuffer:
    """Combine stable Deepgram segments and ignore repeated endpoint events."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.final_end = -1.0
        self.interim_end = -1.0
        self.interim_start = -1.0

    def take(self) -> str:
        text = " ".join(self.parts)
        self.parts.clear()
        return text

    def event(self, message: dict) -> tuple[bool, str]:
        kind = message.get("type")
        if kind == "UtteranceEnd":
            end = message.get("last_word_end", -1)
            return False, self.take() if end >= 0 and end >= max(self.final_end, self.interim_end) - .02 else ""
        if kind != "Results":
            return False, ""
        alternatives = message.get("channel", {}).get("alternatives", [])
        best = alternatives[0] if alternatives else {}
        text = clean(best.get("transcript", ""))
        words = best.get("words", [])
        start = message.get("start", 0)
        segment_end = start + message.get("duration", 0)
        end = words[-1]["end"] if words else segment_end
        fresh = end > self.final_end + .001
        recognized = (fresh and any(char.isalnum() for char in text) and not MUSIC.fullmatch(text)
                      and not NOISE.fullmatch(text) and best.get("confidence", 1) >= .55)
        if recognized:
            if message.get("is_final"):
                self.parts.append(text)
                self.final_end = end
                if abs(start - self.interim_start) < .05 or segment_end >= self.interim_end:
                    self.interim_end = -1.0
            else:
                self.interim_start = start
                self.interim_end = end
        # An old duplicate Results can never flush words from a newer utterance.
        complete = message.get("speech_final") and fresh and end >= self.interim_end - .02
        return bool(recognized), self.take() if complete else ""


@dataclass
class Playback:
    turn: int
    phrases: list[str]
    sent: set[int] = field(default_factory=set)
    acknowledged: set[int] = field(default_factory=set)
    audio_sent: bool = False

    def mark(self, index: int) -> str:
        return f"turn-{self.turn}-phrase-{index}"


class MediaCall:
    def __init__(self, socket: WebSocket, sid: str, state: PhoneCall, records: PhoneRecords,
                 speech: DeepgramSpeech, extractor: GroqClient):
        self.socket, self.sid, self.state, self.records = socket, sid, state, records
        self.speech, self.extractor = speech, extractor
        self.stream_sid = ""
        self.buffer = TurnBuffer()
        self.pending: asyncio.Queue[str] = asyncio.Queue()
        self.stop = asyncio.Event()
        self.terminal = asyncio.Event()
        self.output_lock = asyncio.Lock()
        self.playback: Playback | None = None
        self.play_task: asyncio.Task | None = None
        self.caller_speaking = False
        self.stopping = False
        self.last_audio = 0.0

    async def send(self, event: str, **message) -> None:
        await asyncio.wait_for(self.socket.send_json({"event": event, "streamSid": self.stream_sid, **message}), 4)

    async def start(self) -> None:
        async with asyncio.timeout(8):
            message = await self.socket.receive_json()
            if message.get("event") == "connected":
                message = await self.socket.receive_json()
        start = message.get("start", {})
        sid = start.get("streamSid", "")
        if (message.get("event") != "start" or start.get("callSid") != self.sid
                or not STREAM_SID.fullmatch(sid) or message.get("streamSid") != sid
                or start.get("mediaFormat") != FORMAT or "inbound" not in start.get("tracks", [])):
            raise ValueError("Invalid Twilio start frame.")
        self.stream_sid = sid
        self.state.stream_started = True
        self.records.save(self.sid, self.state)

    def speak(self, turn: int) -> None:
        text = self.state.call.transcript[turn - 1].text
        phrases = re.split(r"(?<=[.?!])\s+", text)
        playback = Playback(turn, phrases)
        self.playback = playback
        self.play_task = asyncio.create_task(self.stream_playback(playback, text == SLOW_ID))

    async def send_audio(self, playback: Playback, audio: bytes) -> None:
        async with self.output_lock:
            if self.playback is playback and not self.stopping:
                playback.audio_sent = True
                await self.send("media", media={"payload": base64.b64encode(audio).decode("ascii")})

    async def stream_playback(self, playback: Playback, spell: bool) -> None:
        try:
            async with self.speech.synthesizer() as synthesize:
                for index, phrase in enumerate(playback.phrases):
                    text = phrase.replace(CLAIM["claim_number"], "T E S T dash four eight two one three")
                    audio_received = False
                    async with asyncio.timeout(15):
                        async for audio in synthesize(text):
                            if audio:
                                audio_received = True
                                await self.send_audio(playback, audio)
                    if not audio_received:
                        raise SpeechError("Deepgram returned empty audio.")
                    if spell and index < len(playback.phrases) - 1:
                        await self.send_audio(playback, SPELL_PAUSE)
                    async with self.output_lock:
                        if self.playback is not playback or self.stopping:
                            return
                        playback.sent.add(index)
                        await self.send("mark", mark={"name": playback.mark(index)})
        except Exception:
            self.state.error = "Agent audio failed or timed out. Check Deepgram synthesis and retry the call."
            self.stop.set()

    async def interrupt(self) -> None:
        async with self.output_lock:
            playback = self.playback
            if playback is None:
                return
            # Retire these marks before clear. Twilio echoes all outstanding marks on clear.
            self.playback = None
            if self.play_task is not None:
                self.play_task.cancel()
            if playback.audio_sent and not self.stopping:
                await self.send("clear")

    async def mark(self, name: str) -> None:
        async with self.output_lock:
            playback = self.playback
            if playback is None:
                return
            index = next((index for index in playback.sent if playback.mark(index) == name), None)
            if index is None or index in playback.acknowledged:
                return
            playback.acknowledged.add(index)
            count = 0
            while count in playback.acknowledged:
                count += 1
            if count == len(playback.phrases):
                self.state.played.append(playback.turn)
                self.state.partial.pop(playback.turn, None)
                self.playback = None
                if self.state.call.phase == "ended":
                    self.stop.set()
            elif count:
                self.state.partial[playback.turn] = " ".join(playback.phrases[:count])
            self.records.save(self.sid, self.state)

    async def receive_media(self, recognizer) -> None:
        while True:
            message = await self.socket.receive_json()
            if message.get("streamSid") != self.stream_sid:
                continue
            match message.get("event"):
                case "media":
                    media = message.get("media", {})
                    if media.get("track") == "inbound":
                        try:
                            audio = base64.b64decode(media.get("payload", ""), validate=True)
                        except (ValueError, TypeError, binascii.Error):
                            continue
                        if audio:
                            await asyncio.wait_for(recognizer.audio(audio), 4)
                            self.last_audio = asyncio.get_running_loop().time()
                case "mark":
                    await self.mark(message.get("mark", {}).get("name", ""))
                case "stop":
                    return

    def enqueue(self, text: str) -> None:
        if text:
            self.state.pending.append(text)
            self.records.save(self.sid, self.state)
            self.pending.put_nowait(text)

    async def transcribe(self, recognizer) -> None:
        async for message in recognizer.events():
            recognized, utterance = self.buffer.event(message)
            if recognized and not self.stopping:
                self.caller_speaking = True
                await self.interrupt()
            if utterance:
                self.caller_speaking = False
                self.enqueue(utterance)

    def advance(self, call: Call, played: set[int], text: str) -> Call:
        if call.phase == "ended":
            call.phase = "confirming"
        phase = call.held_phase if call.phase == "hold" else call.phase
        if phase == "confirming" and (YES.fullmatch(text) or END.fullmatch(text)):
            latest = next((number for number, turn in reversed(list(enumerate(call.transcript, 1)))
                           if turn.speaker == "agent" and turn.text.startswith(READBACK_PREFIX)), None)
            if latest not in played:
                call.transcript.append(Turn("representative", text))
                read_back(call)
                return call
        if len(text) > 1600:
            call.transcript.append(Turn("representative", text))
            append_agent(call, "Could you repeat that one detail at a time?")
            return call
        try:
            reply(call, text, self.extractor)
        except ServiceError:
            call.transcript.append(Turn("representative", text))
            append_agent(call, "I couldn't capture that reply. Could you say it again?")
            LOGGER.warning("Claim extraction failed for %s.", self.sid)
        return call

    async def conversation(self) -> None:
        while True:
            text = await self.pending.get()
            try:
                previous = len(self.state.call.transcript)
                snapshot = copy.deepcopy(self.state.call)
                self.state.call = await asyncio.to_thread(self.advance, snapshot, set(self.state.played), text)
                self.state.pending.pop(0)
                self.records.save(self.sid, self.state)
                call = self.state.call
                if (not self.stopping and not self.caller_speaking and self.pending.empty()
                        and len(call.transcript) > previous and call.transcript[-1].speaker == "agent"):
                    await self.interrupt()
                    if not self.caller_speaking and self.pending.empty():
                        self.speak(len(call.transcript))
            finally:
                self.pending.task_done()

    async def keep_alive(self, recognizer) -> None:
        while True:
            await asyncio.sleep(3)
            if asyncio.get_running_loop().time() - self.last_audio >= 3:
                await asyncio.wait_for(recognizer.control("KeepAlive"), 4)

    async def terminal_grace(self) -> None:
        await self.terminal.wait()
        # Status and media are independent transports. Let final marks and stop arrive first.
        await asyncio.sleep(2)

    async def run(self) -> None:
        await self.start()
        async with self.speech.listen() as recognizer:
            reader = asyncio.create_task(self.receive_media(recognizer))
            stt = asyncio.create_task(self.transcribe(recognizer))
            worker = asyncio.create_task(self.conversation())
            keepalive = asyncio.create_task(self.keep_alive(recognizer))
            terminal = asyncio.create_task(self.terminal_grace())
            done = asyncio.create_task(self.stop.wait())
            self.last_audio = asyncio.get_running_loop().time()
            self.speak(1)
            try:
                completed, _ = await asyncio.wait((reader, stt, worker, keepalive, terminal, done),
                                                  return_when=asyncio.FIRST_COMPLETED)
                for task in completed:
                    await task
                if stt in completed and not self.stop.is_set():
                    raise SpeechError("Deepgram closed the live stream.")
            except WebSocketDisconnect:
                pass
            finally:
                self.stopping = True
                for finishing_task in (reader, keepalive, terminal, done, self.play_task):
                    if finishing_task is not None:
                        finishing_task.cancel()
                await asyncio.gather(reader, keepalive, terminal, done, return_exceptions=True)
                if self.play_task is not None:
                    await asyncio.gather(self.play_task, return_exceptions=True)
                try:
                    if not stt.done():
                        await asyncio.wait_for(recognizer.control("CloseStream"), 1)
                        await asyncio.wait_for(stt, 2)
                except Exception:
                    pass  # Keep stable segments if the provider cannot flush its tail.
                finally:
                    if not stt.done():
                        stt.cancel()
                    await asyncio.gather(stt, return_exceptions=True)
                self.enqueue(self.buffer.take())
                try:
                    await asyncio.wait_for(self.pending.join(), 5)
                except TimeoutError:
                    pass  # Unprocessed words remain in the persisted pending transcript.
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
