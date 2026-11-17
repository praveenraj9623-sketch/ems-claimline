// Run with: node --test tests/voice_component.test.mjs
// Browser APIs are simulated; this checks transport and playback, not a real microphone.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../claimline/voice/orb.js", import.meta.url), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
let instance = 0;

async function mount({ playback = "complete", error = "", delayed = false } = {}) {
  const elements = Object.fromEntries(["wrap", "label", "hint", "err", "start", "end"].map((key) =>
    [key, { isConnected: true, hidden: false, textContent: "", dataset: {}, style: { setProperty() {} } }]));
  const parentElement = {
    querySelector: (selector) => elements[selector.slice(1)],
    querySelectorAll: () => [],
  };
  let stopped = false;
  const track = { stop() { stopped = true; } };
  const utterances = [], requests = [], responses = [];
  class Recognition { start() {} abort() {} }
  class AudioContext {
    createAnalyser() { return { fftSize: 512, getByteTimeDomainData() {} }; }
    createMediaStreamSource() { return { connect() {} }; }
    close() {}
  }
  const synth = {
    getVoices: () => [],
    speak(utterance) {
      utterances.push(utterance);
      if (playback !== "pending") queueMicrotask(() =>
        playback === "complete" ? utterance.onend() : utterance.onerror());
    },
    cancel() { utterances.at(-1)?.onerror?.(); },
  };
  globalThis.window = { SpeechRecognition: Recognition, AudioContext, speechSynthesis: synth };
  globalThis.speechSynthesis = synth;
  globalThis.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
  Object.defineProperty(globalThis, "navigator", { configurable: true,
    value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } } });
  globalThis.requestAnimationFrame = () => 1;
  globalThis.fetch = () => { throw new Error("Voice must not fetch a localhost API"); };
  const render = (await import("data:text/javascript;base64," + Buffer.from(source).toString("base64") + "#" + (++instance))).default;
  const callId = "a".repeat(32);
  let cleanup;
  function rerender(response) {
    cleanup?.();
    cleanup = render({ parentElement, data: { response }, setStateValue });
  }
  function setStateValue(key, request) {
    if (key !== "request") return;
    requests.push(request);
    const response = error ? { id: request.id, error } : { id: request.id, data: {
      id: callId, phase: "active", finished: request.action === "end",
      reply: { says: [{ text: "Hello. What is the claim status?" }], expect: "text", playback_turn: 1, end: false },
    } };
    if (delayed) responses.push(response); else queueMicrotask(() => rerender(response));
  }
  rerender(null);
  return { elements, requests, responses, utterances, rerender,
    get state() { return window.__claimlineVoice; },
    get stopped() { return stopped; },
    close() {
      elements.wrap.isConnected = false;
      clearTimeout(window.__claimlineVoice.idle);
      clearTimeout(window.__claimlineVoice.kill);
      cleanup();
    },
  };
}

test("Streamlit messages start, acknowledge speech, and end without HTTP", async () => {
  const app = await mount();
  await app.elements.start.onclick();
  assert.deepEqual(app.requests.map((r) => r.action), ["start", "played"]);
  assert.equal(app.requests[0].call_id, null);
  assert.equal(app.requests[1].call_id, "a".repeat(32));
  assert.equal(app.requests[1].body.turn, 1);
  assert.equal(app.state.listening, true);
  await app.elements.end.onclick();
  assert.equal(app.requests.at(-1).action, "end");
  assert.equal(app.stopped, true);
  app.close();
});

test("speech errors end the call without crediting playback", async () => {
  const app = await mount({ playback: "error" });
  await app.elements.start.onclick();
  await tick();
  assert.deepEqual(app.requests.map((r) => r.action), ["start", "end"]);
  assert.match(app.elements.err.textContent, /playback stopped/i);
  assert.equal(app.stopped, true);
  app.close();
});

test("ending during speech never acknowledges the interrupted turn", async () => {
  const app = await mount({ playback: "pending" });
  const starting = app.elements.start.onclick();
  await tick();
  assert.equal(app.state.speaking, true);
  await app.elements.end.onclick();
  await starting;
  assert.deepEqual(app.requests.map((r) => r.action), ["start", "end"]);
  app.close();
});

test("server errors are displayed and microphone resources are released", async () => {
  const app = await mount({ error: "The call could not be started." });
  await app.elements.start.onclick();
  assert.equal(app.elements.err.textContent, "The call could not be started.");
  assert.equal(app.stopped, true);
  assert.equal(app.elements.start.disabled, false);
  assert.deepEqual(app.requests.map((r) => r.action), ["start"]);
  app.close();
});

test("a new call waits for the previous end request to finish", async () => {
  const app = await mount({ delayed: true });
  const starting = app.elements.start.onclick();
  await tick();
  app.rerender(app.responses.shift()); // start
  await tick();
  app.rerender(app.responses.shift()); // played
  await starting;
  const ending = app.elements.end.onclick();
  const restarting = app.elements.start.onclick();
  await tick();
  assert.deepEqual(app.requests.map((r) => r.action), ["start", "played", "end"]);
  app.rerender(app.responses.shift());
  await ending;
  await tick();
  assert.equal(app.requests.at(-1).action, "start");
  app.rerender(app.responses.shift());
  await tick();
  app.rerender(app.responses.shift());
  await restarting;
  const finalEnd = app.elements.end.onclick();
  await tick();
  app.rerender(app.responses.shift());
  await finalEnd;
  app.close();
});
