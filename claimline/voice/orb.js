// Live microphone call. Runs in the browser: speech recognition + speech synthesis, talking to the local ClaimLine API.
// Call state lives on window so a Streamlit re-render never drops an active call.
const S = (window.__claimlineVoice = window.__claimlineVoice || {
  active: false, listening: false, speaking: false, rec: null, buffer: "", interim: "", timer: null, idle: null,
  expect: "text", phase: "active", stream: null, ctx: null, analyser: null, level: 0, state: "idle", raf: 0,
  tick: 0, id: null, api: "", ui: null, kill: null, setStateValue: null,
});
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const PAUSE_MS = { text: 1700, number: 2700, date: 3800, confirm: 1500 };
const LABELS = {
  idle: ["Ready when you are", "Use Chrome or Edge and allow the microphone."],
  listening: ["Listening…", "Speak when you are ready."],
  hearing: ["Hearing you…", "Pause for a moment when you are done."],
  thinking: ["Thinking…", " "],
  speaking: ["Agent is speaking…", "Please wait for the agent to finish."],
  hold: ["On hold", "Take your time. Speak again when you are back."],
};

function alive() { return S.ui && S.ui.wrap.isConnected; }
function setState(state) {
  S.state = state;
  if (!alive()) return;
  S.ui.wrap.dataset.state = state;
  const [label, hint] = LABELS[state];
  S.ui.label.textContent = label; S.ui.hint.textContent = hint;
}
function showError(text) { if (alive()) { S.ui.err.textContent = text || ""; S.ui.err.hidden = !text; } }
function syncButtons() { if (alive()) { S.ui.start.hidden = S.active; S.ui.end.hidden = !S.active; } }

async function api(path, body) {
  const res = await fetch(S.api + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed (" + res.status + ")");
  return data;
}

function animate() {
  S.tick += 1;
  if (S.analyser) {
    const data = new Uint8Array(S.analyser.fftSize); S.analyser.getByteTimeDomainData(data);
    let sum = 0; for (const v of data) sum += ((v - 128) / 128) ** 2;
    S.level = S.level * 0.7 + Math.sqrt(sum / data.length) * 0.3;
  }
  const mic = S.state === "listening" || S.state === "hearing";
  if (S.state === "listening" && S.level > 0.05) setState("hearing");
  let orb = 0;
  if (mic) orb = Math.min(1, S.level * 8);
  else if (S.state === "speaking") orb = 0.35 + 0.3 * Math.abs(Math.sin(S.tick / 7));
  else if (S.state === "thinking") orb = 0.12 + 0.1 * Math.sin(S.tick / 9);
  else if (S.state === "hold") orb = 0.06 + 0.06 * Math.sin(S.tick / 40);
  if (alive()) {
    S.ui.wrap.style.setProperty("--level", orb.toFixed(3));
    S.ui.bars.forEach((bar, i) => {
      let h = 6;
      if (mic) h = 6 + Math.min(1, S.level * 9) * 28 * (0.45 + 0.55 * Math.abs(Math.sin(S.tick / 4 + i * 0.9)));
      else if (S.state === "speaking") h = 8 + 22 * Math.abs(Math.sin(S.tick / 6 + i * 0.7));
      else if (S.state === "thinking") h = 8 + 8 * Math.abs(Math.sin(S.tick / 10 + i * 0.4));
      bar.style.height = h.toFixed(1) + "px";
    });
  }
  S.raf = requestAnimationFrame(animate);
}

function pickVoice() {
  const voices = speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en"));
  return voices.find((v) => /Google US English|Jenny|Aria|Samantha|Zira/i.test(v.name)) || voices.find((v) => v.lang === "en-US") || voices[0];
}
function speakText(text) {
  return new Promise((resolve) => {
    const sentences = text.match(/[^.!?]+[.!?]*/g) || [text]; let i = 0;
    const next = () => {
      if (!S.active) return resolve(false);
      if (i >= sentences.length) return resolve(true);
      const u = new SpeechSynthesisUtterance(sentences[i++].trim());
      u.voice = pickVoice() || null; u.lang = "en-US"; u.rate = 0.98;
      u.onend = next; u.onerror = () => resolve(false); speechSynthesis.speak(u);
    };
    next();
  });
}
async function speakSpell(chars) {
  for (const c of chars) {
    if (!S.active) return false;
    const completed = await new Promise((done) => {
      const u = new SpeechSynthesisUtterance(c);
      u.voice = pickVoice() || null; u.lang = "en-US"; u.rate = 0.8;
      u.onend = () => done(true); u.onerror = () => done(false); speechSynthesis.speak(u);
    });
    if (!completed) return false;
    await new Promise((r) => setTimeout(r, 450));
  }
  return true;
}
async function speakReply(reply) {
  if (!reply.says.length) return true;
  stopListening(); S.speaking = true; setState("speaking");
  try {
    for (const say of reply.says) {
      if (!S.active) return false;
      const completed = await (say.spell ? speakSpell(say.spell) : speakText(say.text));
      if (!completed) return false;
    }
    return true;
  } finally {
    S.speaking = false;
  }
}

function startListening() {
  if (!S.active || S.listening) return;
  S.buffer = ""; S.interim = ""; S.listening = true;
  setState(S.phase === "hold" ? "hold" : "listening");
  const rec = new SR(); S.rec = rec; rec.lang = "en-US"; rec.continuous = true; rec.interimResults = true;
  rec.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) S.buffer += " " + r[0].transcript; else interim += r[0].transcript;
    }
    S.interim = interim; heard();
  };
  rec.onend = () => { if (S.active && S.listening && S.rec === rec) { try { rec.start(); } catch (e) { /* already running */ } } };
  rec.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") fail("Microphone access was blocked. Allow it in the address bar and start again.");
    else if (e.error === "network") fail("Speech recognition needs an internet connection in this browser.");
  };
  try { rec.start(); } catch (e) { /* already running */ }
  clearTimeout(S.idle); S.idle = setTimeout(onIdle, 14000);
}
function stopListening() {
  S.listening = false; clearTimeout(S.timer); clearTimeout(S.idle);
  const rec = S.rec; S.rec = null;
  if (rec) { rec.onend = null; try { rec.abort(); } catch (e) { /* ignore */ } }
}
function heard() {
  if (S.phase !== "hold") setState("hearing");
  clearTimeout(S.timer); clearTimeout(S.idle);
  S.timer = setTimeout(flush, PAUSE_MS[S.expect] || PAUSE_MS.text);
}
async function flush() {
  const text = (S.buffer + " " + S.interim).replace(/\s+/g, " ").trim();
  if (!text || !S.active) return;
  stopListening(); setState("thinking");
  try { await handle(await api("/api/web/" + S.id + "/say", { text })); } catch (e) { fail(e.message); }
}
async function onIdle() {
  if (!S.active || !S.listening) return;
  stopListening();
  try { await handle(await api("/api/web/" + S.id + "/silence", {})); } catch (e) { fail(e.message); }
}
async function handle(s) {
  const callId = s.id;
  S.expect = s.reply.expect; S.phase = s.phase;
  const completed = await speakReply(s.reply);
  if (!S.active || S.id !== callId) return;
  if (!completed) throw new Error("Speech playback stopped before completion. Please start a new voice call.");
  if (s.reply.playback_turn) await api("/api/web/" + callId + "/played", { turn: s.reply.playback_turn });
  if (!S.active || S.id !== callId) return;
  if (s.reply.end) return endVoice();
  setState(s.phase === "hold" ? "hold" : "listening"); startListening();
}
function fail(message) { showError(message); endVoice(true); }

async function startVoice() {
  showError("");
  if (!SR || !window.speechSynthesis) { showError("This browser has no speech recognition. Open the app in Chrome or Edge, or use the Type mode."); return; }
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) { showError("Microphone access is needed for a voice call. Allow it and try again."); return; }
  S.ctx = new (window.AudioContext || window.webkitAudioContext)();
  S.analyser = S.ctx.createAnalyser(); S.analyser.fftSize = 512;
  S.ctx.createMediaStreamSource(S.stream).connect(S.analyser);
  try {
    const s = await api("/api/web/start", { mode: "voice" });
    S.id = s.id; S.active = true; S.phase = "active"; syncButtons();
    if (S.setStateValue) S.setStateValue("call_id", s.id);
    await handle(s);
  } catch (e) {
    showError(/fetch|network/i.test(e.message) ? "Could not reach ClaimLine at " + S.api + ". Is the app still running?" : e.message);
    endVoice(true);
  }
}
async function endVoice(failed) {
  const id = S.id, wasActive = S.active;
  S.active = false; stopListening(); speechSynthesis.cancel();
  if (S.stream) S.stream.getTracks().forEach((t) => t.stop());
  if (S.ctx) S.ctx.close();
  S.stream = S.ctx = S.analyser = null; S.level = 0;
  setState("idle"); syncButtons();
  if (wasActive && id) { try { await api("/api/web/" + id + "/end", {}); } catch (e) { if (!failed) showError(e.message); } }
}

export default function (component) {
  const { parentElement, data, setStateValue } = component;
  clearTimeout(S.kill);
  S.api = (data && data.api) || S.api; S.setStateValue = setStateValue;
  const q = (s) => parentElement.querySelector(s);
  S.ui = { wrap: q(".wrap"), label: q(".label"), hint: q(".hint"), err: q(".err"), start: q(".start"), end: q(".end"),
           bars: [...parentElement.querySelectorAll(".bars i")] };
  S.ui.start.onclick = startVoice; S.ui.end.onclick = () => endVoice();
  setState(S.state); syncButtons();
  if (!S.raf) animate();
  return () => {
    // A re-render calls this and then the function above again. If no new render arrives, the panel was removed.
    S.kill = setTimeout(() => { if (S.active && !alive()) endVoice(true); }, 800);
  };
}
