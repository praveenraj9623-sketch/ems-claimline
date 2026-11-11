"use strict";

const join = document.getElementById("join");
const end = document.getElementById("end");
const status = document.getElementById("status");
const invitation = new URLSearchParams(location.hash.slice(1));
const id = invitation.get("call");
const code = invitation.get("code");
const topic = "ems-voice";
const decoder = new TextDecoder();
const encoder = new TextEncoder();

let room;
let audio;
let microphone;
let nextSample = 0;
let lastSource;
let finished = false;
let spent = false;
let greetingTimer;
const sources = new Set();
const completed = new WeakSet();
const marks = new WeakMap();

function send(event, name) {
  return room.localParticipant.publishData(encoder.encode(JSON.stringify({event, name})), {
    reliable: true, topic, destinationIdentities: ["agent"],
  });
}

function clearAudio() {
  for (const source of sources) {
    source.onended = null;
    source.stop();
    source.disconnect();
  }
  sources.clear();
  lastSource = undefined;
  nextSample = 0;
}

function queueAudio(encoded) {
  const bytes = atob(encoded);
  const buffer = audio.createBuffer(1, bytes.length, 8000);
  const samples = buffer.getChannelData(0);
  for (let i = 0; i < bytes.length; i++) {
    const codeword = (~bytes.charCodeAt(i)) & 255;
    const magnitude = (((codeword & 15) * 8 + 132) << ((codeword >> 4) & 7)) - 132;
    samples[i] = ((codeword & 128) ? -magnitude : magnitude) / 32768;
  }
  const source = audio.createBufferSource();
  source.buffer = buffer;
  source.connect(audio.destination);
  if (nextSample <= audio.currentTime) nextSample = audio.currentTime + .06;
  source.start(nextSample);
  nextSample += buffer.duration;
  sources.add(source);
  lastSource = source;
  source.onended = () => {
    sources.delete(source);
    source.disconnect();
    completed.add(source);
    const mark = marks.get(source);
    if (mark && !finished) void send("mark", mark).catch(() => {});
  };
}

async function closeCall(message, notify = false) {
  if (finished) return;
  finished = true;
  clearTimeout(greetingTimer);
  end.hidden = true;
  status.textContent = message;
  clearAudio();
  if (notify && room?.state === LivekitClient.ConnectionState.Connected) {
    await Promise.race([send("end").catch(() => {}), new Promise(resolve => setTimeout(resolve, 350))]);
  }
  microphone?.stop();
  await room?.disconnect().catch(() => {});
  await audio?.close().catch(() => {});
}

function dataReceived(payload, participant, _kind, channel) {
  if (finished || participant?.identity !== "agent" || channel !== topic) return;
  const message = JSON.parse(decoder.decode(payload));
  switch (message.event) {
    case "media":
      clearTimeout(greetingTimer);
      status.textContent = "Connected. Speak naturally when you’re ready.";
      queueAudio(message.audio);
      break;
    case "mark":
      if (lastSource && completed.has(lastSource)) void send("mark", message.name).catch(() => {});
      else if (lastSource) marks.set(lastSource, message.name);
      break;
    case "clear":
      clearAudio();
      break;
    case "end":
      void closeCall("The call ended. Check the EMS dashboard for your results.");
      break;
  }
}

end.addEventListener("click", () => {
  void closeCall("Call ended. Check the EMS dashboard for your results.", true);
});

join.addEventListener("click", async () => {
  join.disabled = true;
  status.textContent = "Waiting for microphone permission…";
  try {
    finished = false;
    const {Room, RoomEvent, Track, createLocalAudioTrack} = LivekitClient;
    audio = new (window.AudioContext || window.webkitAudioContext)();
    await audio.resume();
    microphone = await createLocalAudioTrack({echoCancellation: true, noiseSuppression: true});
    status.textContent = "Connecting to the billing agent…";
    const response = await fetch("/live/join/" + id, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({code}),
      signal: AbortSignal.timeout(12000),
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(typeof body.detail === "string" ? body.detail : "The voice service could not start.");
    }
    spent = true;
    const credentials = await response.json();
    history.replaceState(null, "", location.pathname);
    room = new Room();
    room.on(RoomEvent.DataReceived, dataReceived);
    room.on(RoomEvent.Disconnected, () => {
      void closeCall("Disconnected. Check the EMS dashboard for results or create a new link.");
    });
    room.on(RoomEvent.Reconnecting, () => {
      void closeCall("The connection was interrupted. Check your Wi-Fi and create a new link.");
    });
    await room.connect(credentials.url, credentials.token, {maxRetries: 0, peerConnectionTimeout: 12000});
    if (finished) return;
    await room.localParticipant.publishTrack(microphone, {source: Track.Source.Microphone});
    if (finished) return;
    join.hidden = true;
    end.hidden = false;
    if (!lastSource) {
      status.textContent = "Connected. Waiting for the billing agent…";
      greetingTimer = setTimeout(() => {
        void closeCall("No greeting arrived. Check Deepgram and the PC’s voice service, then create a new link.", true);
      }, 25000);
    }
  } catch (error) {
    let message;
    if (!spent && (error.name === "NotAllowedError" || error.name === "NotFoundError")) {
      message = "Allow microphone access in your browser settings, then try again.";
    } else if (spent) {
      message = "Could not connect audio. Use the same Wi-Fi as the PC and check the Windows firewall, then create a new link.";
    } else {
      message = error.message || "Could not start the call. Check the EMS dashboard and try again.";
    }
    await closeCall(message);
    join.disabled = spent;
  }
});

if (!/^[0-9a-f]{32}$/.test(id || "") || !/^[\w-]{43}$/.test(code || "")) {
  join.disabled = true;
  status.textContent = "Open a new call link from the EMS dashboard.";
} else if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
  join.disabled = true;
  status.textContent = "Open the HTTPS link from the EMS dashboard to enable your microphone.";
} else if (!window.LivekitClient) {
  join.disabled = true;
  status.textContent = "The voice client did not load. Restart the voice service and reload this page.";
}
