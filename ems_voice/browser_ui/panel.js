function microphoneProcessor() {
  class ClaimlineMic extends AudioWorkletProcessor {
    constructor() {
      super();
      this.packet = new ArrayBuffer(640);
      this.view = new DataView(this.packet);
      this.offset = 0;
      this.energy = 0;
    }

    process(inputs) {
      const channel = inputs[0]?.[0];
      if (!channel) return true;
      for (const sample of channel) {
        const value = Math.max(-1, Math.min(1, sample));
        this.view.setInt16(this.offset, value * (value < 0 ? 32768 : 32767), true);
        this.energy += value * value;
        this.offset += 2;
        if (this.offset === this.packet.byteLength) {
          const level = Math.sqrt(this.energy / (this.offset / 2));
          this.port.postMessage({ frame: this.packet, level }, [this.packet]);
          this.packet = new ArrayBuffer(640);
          this.view = new DataView(this.packet);
          this.offset = 0;
          this.energy = 0;
        }
      }
      return true;
    }
  }
  registerProcessor("claimline-mic", ClaimlineMic);
}

function decodeSample(value) {
  const code = (~value) & 255;
  const magnitude = ((((code & 15) << 3) + 132) << ((code >> 4) & 7)) - 132;
  return (code & 128 ? -magnitude : magnitude) / 32768;
}

function sendControl(call, message) {
  if (call.socket?.readyState === WebSocket.OPEN) {
    call.socket.send(JSON.stringify(message));
  }
}

function clearAudio(call) {
  for (const clip of call.clips) {
    clip.source.onended = null;
    clip.source.stop();
    clip.source.disconnect();
  }
  call.clips.clear();
  call.lastClip = null;
  call.nextPlayback = 0;
}

function playAudio(call, data) {
  if (call.ending) return;
  const bytes = new Uint8Array(data);
  const buffer = call.context.createBuffer(1, bytes.length, 8000);
  const samples = buffer.getChannelData(0);
  for (let index = 0; index < bytes.length; index += 1) {
    samples[index] = decodeSample(bytes[index]);
  }
  const source = call.context.createBufferSource();
  const clip = { source, marks: [] };
  source.buffer = buffer;
  source.connect(call.context.destination);
  call.clips.add(clip);
  call.lastClip = clip;
  source.onended = () => {
    source.disconnect();
    if (!call.clips.delete(clip)) return;
    for (const name of clip.marks) sendControl(call, { event: "mark", name });
  };
  const openingDelay = Math.max(0, (call.greetingAt - performance.now()) / 1000);
  const start = Math.max(call.context.currentTime + Math.max(.04, openingDelay), call.nextPlayback);
  source.start(start);
  call.nextPlayback = start + buffer.duration;
}

function stopMicrophone(call) {
  if (call.input) {
    call.input.port.onmessage = null;
    call.input.disconnect();
  }
  call.stream?.getTracks().forEach(track => track.stop());
  clearAudio(call);
  if (call.context && call.context.state !== "closed") {
    void call.context.close().catch(() => {});
  }
}

function renderTranscript(container, turns) {
  const nodes = [];
  for (const turn of turns) {
    const line = document.createElement("p");
    const speaker = document.createElement("strong");
    speaker.textContent = turn.speaker === "agent" ? "EMS billing agent" : "Insurance representative";
    line.append(speaker, document.createElement("br"), document.createTextNode(turn.text));
    nodes.push(line);
  }
  container.replaceChildren(...nodes);
  container.hidden = nodes.length === 0;
  container.scrollTop = container.scrollHeight;
}

function renderResults(container, data) {
  const nodes = [];
  for (const [title, value] of [["Claim result", data.claim], ["AI reviewer", data.review]]) {
    if (!value) continue;
    const heading = document.createElement("h3");
    heading.textContent = title;
    const result = document.createElement("pre");
    result.textContent = JSON.stringify(value, null, 2);
    nodes.push(heading, result);
  }
  if (data.review_error?.error) {
    const error = document.createElement("p");
    error.textContent = "Reviewer: " + data.review_error.error;
    nodes.push(error);
  }
  container.replaceChildren(...nodes);
  container.hidden = nodes.length === 0;
}

export default function ({ parentElement, data }) {
  const start = parentElement.querySelector("#voice-start");
  const end = parentElement.querySelector("#voice-end");
  const meter = parentElement.querySelector("#voice-meter");
  const status = parentElement.querySelector("#voice-status");
  const transcript = parentElement.querySelector("#voice-transcript");
  const results = parentElement.querySelector("#voice-results");
  let active = null;

  function updateListening(call, level = 0) {
    if (call.ending) {
      meter.hidden = true;
      meter.classList.remove("is-listening");
      return;
    }
    if (!call.phase) return;
    const now = performance.now();
    if (level >= .012) call.lastVoice = now;
    const listening = now - call.lastVoice < 230;
    meter.hidden = false;
    meter.classList.toggle("is-listening", listening);
    if (listening) meter.style.setProperty("--mic-level", String(Math.min(1, .45 + level * 6)));
    const label = listening ? "Listening…" :
      call.phase === "hold" ? "On hold · speak when ready" : "Live · microphone on";
    if (status.textContent !== label) status.textContent = label;
  }

  function finish(call, message) {
    if (active !== call) return;
    active = null;
    call.ending = true;
    updateListening(call);
    stopMicrophone(call);
    call.socket?.close();
    status.textContent = message;
    start.disabled = false;
    end.disabled = true;
  }

  function receive(call, event) {
    if (active !== call) return;
    if (event.data instanceof ArrayBuffer) {
      playAudio(call, event.data);
      return;
    }
    const message = JSON.parse(event.data);
    switch (message.event) {
      case "mark":
        if (call.ending) return;
        if (call.lastClip && call.clips.has(call.lastClip)) call.lastClip.marks.push(message.name);
        else sendControl(call, { event: "mark", name: message.name });
        break;
      case "clear":
        clearAudio(call);
        break;
      case "transcript":
        if (call.ending) return;
        renderTranscript(transcript, message.turns);
        call.phase = message.phase;
        updateListening(call);
        break;
      case "finishing":
        call.ending = true;
        updateListening(call);
        stopMicrophone(call);
        end.disabled = true;
        status.textContent = "Call ended · reviewing transcript…";
        break;
      case "result":
        renderTranscript(transcript, message.turns ?? []);
        renderResults(results, message);
        finish(call, message.error || "Call complete");
        break;
      case "error":
        finish(call, message.message);
        break;
    }
  }

  start.onclick = async () => {
    if (active) return;
    const call = { greetingAt: performance.now() + 2000, clips: new Set(), nextPlayback: 0,
      lastVoice: -Infinity, ending: false };
    active = call;
    start.disabled = true;
    end.disabled = true;
    renderTranscript(transcript, []);
    renderResults(results, {});
    status.textContent = "Connecting microphone…";
    try {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
        throw new Error("Open the local app in Chrome or Edge to use the microphone.");
      }
      call.context = new AudioContext({ sampleRate: 16000 });
      await call.context.resume();
      if (call.context.sampleRate !== 16000) throw new Error("This browser could not start 16 kHz audio.");
      call.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      if (active !== call) {
        stopMicrophone(call);
        return;
      }
      const script = new Blob(["(" + microphoneProcessor.toString() + ")()"], { type: "text/javascript" });
      const url = URL.createObjectURL(script);
      try {
        await call.context.audioWorklet.addModule(url);
      } finally {
        URL.revokeObjectURL(url);
      }
      if (active !== call) return;

      call.socket = new WebSocket(data.socketUrl);
      call.socket.binaryType = "arraybuffer";
      call.socket.onmessage = event => receive(call, event);
      call.socket.onerror = () => finish(call, "Live voice service unavailable. Start it on port 5001, then retry.");
      call.socket.onclose = () => finish(call, "Connection ended. You can start a new call.");
      call.socket.onopen = () => {
        if (active !== call) return;
        const input = new AudioWorkletNode(call.context, "claimline-mic");
        const microphone = call.context.createMediaStreamSource(call.stream);
        const mute = call.context.createGain();
        mute.gain.value = 0;
        call.input = input;
        input.port.onmessage = ({ data: { frame, level } }) => {
          if (!call.ending && call.socket.readyState === WebSocket.OPEN && call.socket.bufferedAmount < 65536) {
            call.socket.send(frame);
            updateListening(call, level);
          }
        };
        microphone.connect(input).connect(mute).connect(call.context.destination);
        end.disabled = false;
        status.textContent = "Starting live voice…";
      };
    } catch (error) {
      finish(call, error.name === "NotAllowedError" ? "Allow microphone access, then try again." : error.message);
    }
  };

  end.onclick = () => {
    if (!active || active.ending) return;
    active.ending = true;
    updateListening(active);
    sendControl(active, { event: "end" });
    stopMicrophone(active);
    end.disabled = true;
    status.textContent = "Ending call…";
  };

  return () => {
    if (active) finish(active, "Call ended");
    start.onclick = null;
    end.onclick = null;
  };
}
