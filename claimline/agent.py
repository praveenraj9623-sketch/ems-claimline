"""The caller: a billing-office agent that phones an insurer about one claim.

The conversation logic is a deterministic state machine. The agent can only ever
speak values the representative actually said (see extract.py), so it cannot invent
data. Speech transports (Twilio, browser, typed chat) all drive this one class.
"""

import re
from dataclasses import dataclass, field
from html import escape

from .extract import CUES, KEYS, Found, Ingest, ingest, unrelated_number_context

CLAIM = {
    "claim_number": "TEST-48213",
    "date_of_service": "03/14/2026",
    "date_of_service_spoken": "March 14, 2026",
    "patient_name": "Alex Morgan",
    "agency": "Cedar Vale EMS",
    "agent_name": "Sarah",
}
SPELLING = ["T", "E", "S", "T", "dash", "four", "eight", "two", "one", "three"]
SPELLED_TEXT = "T. E. S. T. Dash. Four. Eight. Two. One. Three."
HOLD_REPLY = "Of course, I'll wait."
GOODBYE = "Thank you for your help. Have a good day."
READBACK_PREFIX = "Let me read back what I captured."
MAX_READBACKS = 8

LABELS = {
    "claim_status": "Claim status",
    "paid_amount": "Paid amount",
    "check_or_eft_number": "Check or EFT number",
    "payment_date": "Payment date",
}
QUESTIONS = {
    "claim_status": "What is the current claim status?",
    "paid_amount": "What amount was paid?",
    "check_or_eft_number": "What is the check or EFT number?",
    "payment_date": "On what date was the payment made?",
}

_HOLD_STRONG = re.compile(
    r"\b(?:please hold|hold please|hold on|hang on|hold the (?:line|call|phone)|hold for (?:a|one|just)|"
    r"(?:can|could|will|would) you (?:please )?(?:hold|wait|bear)|put(?:ting)? you on hold|plac(?:e|ing) you on hold|"
    r"(?:one|a|just a|just one) (?:moment|second|minute|sec)|give me (?:a|one|just a) (?:moment|second|minute|sec)|"
    r"bear with me|stay on the line|wait a (?:moment|second|minute)|let me (?:check|pull|look|see|verify|find|get|bring|review)|"
    r"hold (?:hold|one|a|just)|just hold)\b", re.I)
_ON_HOLD_STATUS = re.compile(r"\b(?:claim|status|payment|account|it(?:'s| is| was)|been|currently|placed|marked)\b[^.?!]{0,12}\b(?:on|under) hold\b", re.I)
RESUME = re.compile(
    r"\b(?:i(?:'m| am) back|thanks? (?:you )?for (?:holding|waiting|your patience)|sorry for the wait|back on the line|"
    r"thanks? for your patience|thank you for waiting)\b", re.I)
NOISE = re.compile(r"(?:music|silence|no speech|beep|thank you for watching|you|uh+|um+|hmm+|mm+|ok(?:ay)?)?", re.I)
SPELL = re.compile(
    r"\b(?:slow(?:ly|er)?|spell|one (?:character|letter|digit|number) at a time|letter by letter|"
    r"character by character|digit by digit|each (?:character|letter|digit))\b", re.I)
REPEAT = re.compile(
    r"\b(?:repeat|say (?:it|that|this)(?: one)? again|once more|come again|pardon|what was (?:that|the claim)|"
    r"(?:didn't|did not|couldn't|could not) (?:catch|get|hear)|sorry,? what)\b", re.I)
CLAIM_TALK = re.compile(r"\b(?:claim|id|reference)\b|\bnumber\b", re.I)
ASK_CLAIM = re.compile(
    r"(?:what(?:'s| is| was)|(?:give|read|say|confirm|verify|repeat|provide)(?: me| us)?(?: the| that| your)?)"
    r"[^.?!]{0,25}\bclaim (?:number|id)\b|\bclaim (?:number|id) (?:again|please)\b|\bwhich claim\b", re.I)
IDENTITY = re.compile(r"\b(?:patient(?:'s)? name|name of the patient|who is the patient|date of service|dos\b)", re.I)
END_CALL = re.compile(
    r"\b(?:that(?:'s| is) (?:all|everything|it)(?: i (?:have|can (?:share|provide|give)))?|i (?:don't|do not) have (?:any )?more|"
    r"nothing (?:else|more)|no other (?:information|details)|we can end the call|i have to go|i need to go|i must go|i have to leave)\b", re.I)
YES = re.compile(
    r"\b(?:yes|yeah|yep|yup|correct|right|confirm(?:ed)?|affirmative|exactly|perfect|sounds good|all good|that'?s it|you got it|spot on|good)\b", re.I)
NO = re.compile(
    r"\b(?:no|nope|incorrect|wrong|not (?:right|correct|quite)|negative|change|actually|wait|mistake)\b", re.I)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("’", "'")).strip()


def is_hold_request(low: str) -> bool:
    if _ON_HOLD_STATUS.search(low) and not _HOLD_STRONG.search(low):
        return False
    if _HOLD_STRONG.search(low):
        return True
    return bool(re.search(r"\bhold\b(?!\s+(?:music|status|code))", low)) and len(low.split()) <= 8


@dataclass
class Say:
    text: str
    ssml: str | None = None          # richer markup for phone voices (optional)
    spell: list | None = None        # characters to speak one at a time (browser voice)
    interruptible: bool = True


@dataclass
class Reply:
    says: list = field(default_factory=list)
    end: bool = False       # hang up after speaking
    wait: bool = False      # on hold: stay silent and keep listening
    quiet: bool = False     # waiting for the rest of a split answer: stay silent
    expect: str = "text"    # text | number | date | confirm (helps the transport pick pause lengths)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.says)


def money(value) -> str:
    return f"${value:,.2f}"


def spoken_value(key, value) -> str:
    if key == "paid_amount":
        return money(value)
    if key == "payment_date":
        y, m, d = (int(x) for x in value.split("-"))
        month = ("January February March April May June July August September October November December").split()[m - 1]
        return f"{month} {d}, {y}"
    return str(value)


def _ssml_value(key, value) -> str:
    text = escape(spoken_value(key, value))
    return f'<say-as interpret-as="characters">{text}</say-as>' if key == "check_or_eft_number" else text


OPENING_TEXT = (
    f"Hello, this is {CLAIM['agent_name']}, calling from the billing office of {CLAIM['agency']}. "
    f"I'm calling about claim {CLAIM['claim_number']} for {CLAIM['patient_name']}. "
    f"The date of service is {CLAIM['date_of_service_spoken']}. {QUESTIONS['claim_status']}"
)
OPENING_SSML = (
    f"Hello, this is {CLAIM['agent_name']}, calling from the billing office of {CLAIM['agency']}. "
    'I\'m calling about claim <say-as interpret-as="characters">TEST</say-as><break time="250ms"/>'
    '<say-as interpret-as="digits">48213</say-as> '
    f"for {CLAIM['patient_name']}. The date of service is {CLAIM['date_of_service_spoken']}. "
    f"{QUESTIONS['claim_status']}"
)


def spell_say() -> Say:
    parts = "".join(f'{escape(c)}<break time="650ms"/>' for c in SPELLING)
    return Say(SPELLED_TEXT, ssml=f'<prosody rate="slow">{parts}</prosody>', spell=list(SPELLING), interruptible=False)


class Agent:
    def __init__(self, *, quiet_partials: bool = False, span_picker=None, track_playback: bool = False):
        """quiet_partials: stay silent after half a date ("2026") and wait for the rest.
        Live speech transports enable this because speech recognizers split slow dictation.
        span_picker: optional callable(key, text) -> verbatim quote or None (AI assist).
        track_playback: speech transports confirm each spoken turn after playback."""
        self.quiet_partials = quiet_partials
        self.span_picker = span_picker
        self.track_playback = track_playback
        self.transcript: list[dict] = []
        self.facts: dict[str, Found] = {}
        self.fact_turn: dict[str, int] = {}
        self.unavailable: set[str] = set()
        self.attempts = {key: 0 for key in KEYS}
        self.phase = "new"            # new | active | hold | confirming | ended
        self.held_phase = "active"
        self.focus: str | None = None
        self.fragment = ""
        self.last_question = ""
        self.silent_count = 0
        self.readbacks = 0
        self.confirm_failures = 0
        self.confirmed = False

    # ------------------------------------------------------------ output
    def _log(self, speaker, text):
        turn = {"turn": len(self.transcript) + 1, "speaker": speaker, "text": text}
        if self.track_playback and speaker == "agent":
            turn["playback"] = "pending"
        self.transcript.append(turn)

    def _speak(self, says, **flags) -> Reply:
        reply = Reply(says=list(says), **flags)
        if says:
            self._log("agent", reply.text)
        if not flags.get("expect"):
            reply.expect = "date" if (self.focus == "payment_date" or self.fragment) else \
                "number" if self.focus in ("paid_amount", "check_or_eft_number") else \
                "confirm" if self.phase == "confirming" else "text"
        return reply

    def result(self) -> dict:
        return {key: (self.facts[key].value if key in self.facts else None) for key in KEYS}

    # ------------------------------------------------------------- flow
    def start(self) -> Reply:
        if self.phase != "new":
            raise RuntimeError("The call has already started.")
        self.phase, self.focus = "active", "claim_status"
        self.attempts["claim_status"] = 1
        self.last_question = QUESTIONS["claim_status"]
        return self._speak([Say(OPENING_TEXT, ssml=OPENING_SSML, interruptible=False)])

    def _ask(self, key, prefix="") -> Reply:
        self.focus = key
        self.attempts[key] += 1
        self.last_question = QUESTIONS[key]
        text = f"{prefix} {QUESTIONS[key]}".strip()
        return self._speak([Say(text)])

    def _reask_prefix(self, key, res: Ingest | None) -> str:
        if res is None:
            return "Sorry, I didn't catch that."
        if key in res.unavailable:
            return "Understood. If you're able to look it up, could you double-check?"
        if key == "payment_date" and (res.fragment or "payment_date" in res.ambiguous):
            if "payment_date" in res.ambiguous:
                return "I want to be sure I record the right day. Please say the month by name, like March 20, 2026."
            return "I only caught part of the date. Please give the month, day and year."
        if key in res.ambiguous:
            return "I heard more than one value, so I want to be sure."
        return "Sorry, I didn't catch that."

    def _advance(self, res: Ingest | None = None, prefix: str = "") -> Reply:
        for key in KEYS:
            if key in self.facts or self.attempts[key] >= 2:
                continue
            if self.attempts[key] == 0:
                return self._ask(key, prefix)
            return self._ask(key, (prefix + " " + self._reask_prefix(key, res)).strip())
        return self._read_back()

    def _read_back(self, closing=False) -> Reply:
        self.readbacks += 1
        self.phase, self.focus, self.fragment = "confirming", None, ""
        parts, ssml = [], []
        for key in KEYS:
            if key in self.facts:
                value = self.facts[key].value
                parts.append(f"{LABELS[key]} is {spoken_value(key, value)}.")
                ssml.append(f"{LABELS[key]} is {_ssml_value(key, value)}.")
            else:
                parts.append(f"{LABELS[key]} was not provided.")
                ssml.append(f"{LABELS[key]} was not provided.")
        spoken = " ".join(parts)
        marked = ' <break time="250ms"/>'.join(ssml)
        says = [Say(f"{READBACK_PREFIX} {spoken}",
                    ssml=f'{READBACK_PREFIX} <break time="300ms"/>{marked}',
                    interruptible=False)]
        if closing:
            self.phase = "ended"
            says.append(Say("I haven't been able to confirm these with you, so I'll note them as unconfirmed. "
                            "Thank you for your time. Goodbye.", interruptible=False))
            return self._speak(says, end=True)
        self.last_question = "Is that correct?"
        says.append(Say("Is that correct?"))
        return self._speak(says, expect="confirm")

    def _goodbye(self) -> Reply:
        self.phase, self.confirmed = "ended", True
        return self._speak([Say(GOODBYE, interruptible=False)], end=True)

    # ------------------------------------------------------- ingestion
    def _ingest(self, text) -> tuple[Ingest, set]:
        focus = self.focus if self.phase != "confirming" else None
        res = ingest(text, focus, self.fragment)
        if self.span_picker and focus and focus not in res.values and not res.unavailable \
                and not res.fragment and len(text.split()) >= 3:
            try:
                quote = self.span_picker(focus, text)
            except Exception:
                quote = None
            match = re.search(re.escape(quote), text, re.I) if isinstance(quote, str) and quote.strip() else None
            if match and not unrelated_number_context(text, match.start(), match.end()) \
                    and (focus == "claim_status" or CUES[focus].search(quote)):
                extra = ingest(quote, focus, self.fragment)
                if focus in extra.values:
                    res.values[focus] = extra.values[focus]
        changed, turn = set(), len(self.transcript)
        for key, found in res.values.items():
            old = self.facts.get(key)
            if old is None or old.value != found.value:
                changed.add(key)
            self.facts[key] = found
            self.fact_turn[key] = turn
            self.unavailable.discard(key)
        for key in res.unavailable:
            if key not in res.values:
                if key in self.facts and key != focus:
                    del self.facts[key]
                    changed.add(key)
                self.unavailable.add(key)
        self.fragment = res.fragment
        return res, changed

    # ---------------------------------------------------------- hearing
    def hear(self, message: str) -> Reply:
        text = clean(message)
        if not text:
            return self.silence()
        if self.phase in ("new", "ended"):
            return Reply(end=self.phase == "ended")
        self.silent_count = 0
        self._log("representative", text)
        low = text.lower()

        if is_hold_request(low) and not RESUME.search(low):
            self._ingest(text)
            if self.phase != "hold":
                self.held_phase, self.phase = self.phase, "hold"
                return self._speak([Say(HOLD_REPLY)], wait=True)
            return Reply(wait=True)  # already waiting: say nothing, never repeat ourselves

        resumed = False
        if self.phase == "hold":
            if NOISE.fullmatch(low.strip(" .!?,")) :
                return Reply(wait=True)
            self.phase, resumed = self.held_phase, True

        res, changed = self._ingest(text)
        thanks = "Thank you." if resumed else ""

        if SPELL.search(low) or ASK_CLAIM.search(low) or (REPEAT.search(low) and CLAIM_TALK.search(low)) \
                or (REPEAT.search(low) and sum(t["speaker"] == "agent" for t in self.transcript) == 1):
            says = [spell_say()]
            if self.last_question:
                says.append(Say(self.last_question))
            return self._speak(says)
        if REPEAT.search(low):
            return self._speak([Say(f"Of course. {self.last_question}".strip())])
        if IDENTITY.search(low) and not res.values:
            return self._speak([Say(f"The patient is {CLAIM['patient_name']}, and the date of service is "
                                    f"{CLAIM['date_of_service_spoken']}. {self.last_question}".strip())])

        if self.phase == "confirming":
            return self._on_confirmation(text, low, res, changed)

        if END_CALL.search(low):
            return self._read_back()
        if res.fragment and self.quiet_partials and not res.ambiguous:
            return Reply(quiet=True, expect="date")
        if resumed and not res.values and not res.unavailable and not res.ambiguous and not res.fragment:
            return self._speak([Say(f"{thanks} {self.last_question}".strip())])
        return self._advance(res, thanks)

    def _on_confirmation(self, text, low, res, changed) -> Reply:
        if changed:
            return self._read_back()
        if self.readbacks >= MAX_READBACKS:
            return self._goodbye()
        if res.ambiguous or res.fragment:
            self.fragment = ""
            return self._speak([Say("I want to make sure I get that right. Which detail should I correct, "
                                    "and what is the right value?")])
        if NO.search(low) and not (YES.search(low) and not re.search(r"\b(?:no|wrong|incorrect|not)\b", low)):
            self.confirm_failures = 0
            return self._speak([Say("Which detail should I correct?")], expect="text")
        if YES.search(low) or END_CALL.search(low):
            return self._goodbye()
        self.confirm_failures += 1
        if self.confirm_failures >= 3:
            return self._goodbye()
        return self._speak([Say("Sorry, I didn't catch that. Is the read-back correct, or which detail should I change?")])

    def silence(self) -> Reply:
        """The representative said nothing for a while."""
        if self.phase in ("new",):
            return Reply()
        if self.phase == "ended":
            return Reply(end=True)
        if self.phase == "hold":
            return Reply(wait=True)
        self.silent_count += 1
        if self.phase == "confirming":
            if self.silent_count >= 2:
                self.phase = "ended"
                return self._speak([Say("I haven't heard back, so I'll note the read-back as unconfirmed. "
                                        "Thank you for your time. Goodbye.", interruptible=False)], end=True)
            return self._speak([Say("Are you still there? Is that correct?")])
        if self.fragment:
            res = Ingest(fragment=self.fragment)
            self.fragment = ""
            return self._advance(res)
        if self.silent_count >= 3:
            return self._read_back()
        return self._advance(None, "Are you still there?")

    def give_up(self) -> Reply:
        """Hold timed out or the line went quiet for good: read back, then close politely."""
        if self.phase in ("new", "ended"):
            return Reply(end=self.phase == "ended")
        return self._read_back(closing=True)
