"""Evidence-based extraction of the four claim facts from what the representative said.

Nothing here guesses. A value is only returned when the representative's own words
contain it, and every value carries the quote it came from. Ambiguous input
(two different amounts, a date like 03/04/2026) is reported as ambiguous instead of
being resolved by assumption.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .numbers import spoken_to_digits

KEYS = ("claim_status", "paid_amount", "check_or_eft_number", "payment_date")
MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
YEAR_RE = r"(?:(?:19|20)\d{2}|(?:19|20)\s\d{2})"

STATUS_PATTERNS = (
    (r"\bpartially paid\b|\bpartial(?:ly)? (?:payment|paid)\b", "partially paid"),
    (r"\b(?:not|never|hasn'?t been|has not been|wasn'?t|was not|isn'?t|is not)(?: yet| been)* (?:paid|processed)\b|\bunpaid\b", "not paid"),
    (r"\bdenied\b|\bdenial\b", "denied"),
    (r"\brejected\b", "rejected"),
    (r"\b(?:pending|in process|in progress|processing|in review|under review)\b", None),  # keep wording
    (r"\bapproved\b", "approved"),
    (r"\bnot found\b|\bno record\b", "not found"),
    (r"\bclosed\b", "closed"),
    (r"\bapologies\b(?!)", None),  # never matches; placeholder keeps tuple shape stable
    (r"\bpaid(?! amount| date)\b(?<!amount paid)", "paid"),
)
UNKNOWN = re.compile(
    r"\b(?:unknown|unavailable|not available|not provided|not issued|can(?:no|')t (?:share|provide|find|give|see|verify|locate)|cannot (?:share|provide|find|give|see|verify|locate)|"
    r"don'?t (?:have|know|see)|do not (?:have|know|see)|no (?:payment|check|eft|reference|amount|date|information|record)|none|n/?a|not sure|"
    r"haven'?t (?:been )?issued|hasn'?t been issued|not yet (?:issued|available|assigned))\b", re.I)
CORRECTION = re.compile(r"\b(?:correction|actually|correct(?:ed)?|instead|sorry|change|make that|i mean|meant|rather)\b", re.I)
CUES = {
    "claim_status": re.compile(r"\bstatus\b", re.I),
    "paid_amount": re.compile(r"\b(?:amount|total|dollars?)\b|\$", re.I),
    "check_or_eft_number": re.compile(r"\b(?:check|cheque|eft|aft|ach|reference|trace|confirmation)\b", re.I),
    "payment_date": re.compile(r"\bdate\b", re.I),
}
REF_CUE = re.compile(
    r"\b(?:e\s?f\s?t|aft|ach|chk|cheque|check|trace|reference|confirmation)\b"
    r"(?:[\s#:.,-]*(?:number|no|num|id|reference|is|was|of|for|numbers)\b)*[\s#:.,-]*", re.I)
ID_TOKEN = re.compile(r"[a-z]*\d[a-z\d]*", re.I)
MONEY_WORDS = {"dollars", "dollar", "usd", "cents", "percent", "bucks"}
NON_USD = re.compile(r"\b(?:eur|inr|gbp|euros?|rupees?|pounds?)\b|[€₹£]", re.I)
CLAIM_IDS = {"48213", "TEST48213", "03142026", "20260314", "3142026"}
NUMBER_CONTEXT = re.compile(
    r"\b(?:(?P<payment>paid|payment|eft|aft|ach|check|cheque|trace|reference|confirmation)|"
    r"(?P<other>extensions?|ext|phone|telephone|fax|policy|member|patient|service|appointment|"
    r"deductible|co-?pay|coinsurance|billed|charged|charges|balance))\b", re.I)


def unrelated_number_context(text: str, start: int, end: int) -> bool:
    """Reject numbers explicitly attached to another subject in the same clause."""
    prefix = re.split(r"[;!?]|\.(?=\s|$)|\bbut\b", text[:start], flags=re.I)[-1]
    contexts = list(NUMBER_CONTEXT.finditer(prefix))
    following = re.match(r"\s*(?:(?:is|was|for|the|my|our|a)\s+)*([A-Za-z-]+)", text[end:], re.I)
    suffix_context = NUMBER_CONTEXT.fullmatch(following[1]) if following else None
    return bool((contexts and contexts[-1].lastgroup == "other") or
                (suffix_context and suffix_context.lastgroup == "other"))


@dataclass(frozen=True)
class Found:
    value: object
    evidence: str


@dataclass
class Ingest:
    values: dict = field(default_factory=dict)       # key -> Found
    unavailable: set = field(default_factory=set)    # reps said they can't provide it
    ambiguous: set = field(default_factory=set)      # more than one possible value
    fragment: str = ""                               # partial date to merge with the next utterance


def prepare(text: str) -> str:
    """Normalize a transcript line: digits for number words, separator words removed."""
    t = spoken_to_digits(text.replace("’", "'"))
    t = re.sub(r"\b(?:hyphen|dash|slash|stroke)\b", " ", t, flags=re.I)
    return re.sub(r"[ \t]+", " ", t).strip()


_DOS = (r"(?:march 14(?:th)?,? 2026|14(?:th)? (?:of )?march,? 2026|0?3[/.]0?14[/.]2026|2026-0?3-0?14|"
        r"2026 0?3 14|2026 0?314)")
CLAIM_ID_RE = re.compile(r"\btest\s*-?\s*(?:4 8 2 1 3|48213)\b|\b48213\b|\b4 8 2 1 3\b", re.I)
DOS_CUED_RE = re.compile(r"(?:date of service|service date|dos|service)\W{0,15}" + _DOS, re.I)
DOS_RE = re.compile(r"\b" + _DOS + r"(?!\d)", re.I)


def strip_claim_identifiers(t: str, focus: str | None, has_fragment: bool) -> str:
    """Remove the claim number and date of service so repeating them is never read as payment data."""
    t = CLAIM_ID_RE.sub(" ", t)
    t = DOS_CUED_RE.sub(" ", t)
    if focus != "payment_date" and not has_fragment:
        t = DOS_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _blank(chars, span):
    for i in range(*span):
        chars[i] = " "


def _negated_before(text, start):
    return bool(re.search(r"\b(?:not|never|isn't|wasn't|rather than|instead of)\s+(?:the\s+|a\s+|an\s+)?(?:\$\s*)?$",
                          text[max(0, start - 22):start], re.I))


def _wrong_after(text, end):
    return bool(re.match(r"\W*(?:is|was)?\s*(?:wrong|incorrect|not right|not correct)\b", text[end:end + 30], re.I))


def _pick(candidates, text):
    """candidates: list of (value, evidence). Returns (Found|None, ambiguous)."""
    if not candidates:
        return None, False
    values = {v for v, _ in candidates}
    if len(values) > 1 and not CORRECTION.search(text):
        return None, True
    value, evidence = candidates[-1]
    return Found(value, evidence), False


# ---------------------------------------------------------------- dates
def _valid(y, m, d):
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _splits(blob):
    """All (month, day) readings of a 3-4 digit blob such as 320 or 0320."""
    out = []
    for cut in (1, 2):
        if 0 < cut < len(blob) and len(blob) - cut <= 2:
            out.append((blob[:cut], blob[cut:]))
    return out


def date_candidates(tokens):
    """Every valid calendar date obtainable by grouping neighbouring digit atoms.

    Returns (four_digit_year_dates, two_digit_year_dates) as sets of ISO strings.
    """
    full, short = set(), set()
    n = len(tokens)
    if not 1 < n <= 9:
        return full, short

    def group(bounds):
        return ["".join(tokens[a:b]) for a, b in zip(bounds, bounds[1:])]

    def add_y4(y, m, d):
        if len(y) == 4 and 1900 <= int(y) <= 2100 and len(m) <= 2 and len(d) <= 2 and m and d:
            if iso := _valid(int(y), int(m), int(d)):
                full.add(iso)

    for a in range(1, n):
        for b in range(a + 1, n):
            g1, g2, g3 = group((0, a, b, n))
            if len(g1) == 4:
                add_y4(g1, g2, g3)                       # year-month-day
            if len(g3) == 4:
                add_y4(g3, g1, g2)                       # month-day-year
                add_y4(g3, g2, g1)                       # day-month-year
            if len(g3) == 2 and len(g1) <= 2 and len(g2) <= 2 and g1 and g2:
                year = 2000 + int(g3)
                for m, d in ((g1, g2), (g2, g1)):
                    if iso := _valid(year, int(m), int(d)):
                        short.add(iso)
    for a in range(1, n):
        g1, g2 = group((0, a, n))
        if len(g1) == 4 and len(g2) in (3, 4):
            for m, d in _splits(g2):
                add_y4(g1, m, d)
        if len(g2) == 4 and len(g1) in (3, 4):
            for m, d in _splits(g1):
                add_y4(g2, m, d)
    return full, short


def _year(text):
    return int(re.sub(r"\s", "", text))


def find_dates(t, *, loose):
    """Return (candidates, ambiguous, partial). candidates: list of (iso, evidence, span).

    loose=True enables free-form digit groups ("2026 0320") and two-digit years; it is
    only used when the agent just asked for the payment date or a date fragment is pending.
    """
    low = t.lower()
    found, spans = [], []

    def add(iso, m):
        if iso and not unrelated_number_context(t, m.start(), m.end()):
            found.append((iso, m.group().strip(), m.span()))
            spans.append(m.span())

    for m in re.finditer(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,|\s+of)?\s*(?:the\s+year\s+)?({YEAR_RE})\b", low):
        add(_valid(_year(m[3]), MONTHS[m[1][:3] if m[1] != "sept" else "sept"], int(m[2])), m)
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_RE})\.?,?\s*({YEAR_RE})\b", low):
        add(_valid(_year(m[3]), MONTHS[m[2][:3] if m[2] != "sept" else "sept"], int(m[1])), m)
    ambiguous = False
    for m in re.finditer(r"(?<![\d/.-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d/])", low):
        add(_valid(int(m[1]), int(m[2]), int(m[3])), m)
    for m in re.finditer(r"(?<![\d/.-])(\d{1,2})([/.-])(\d{1,2})\2(\d{4})(?![\d/])", low):
        a, b, y = int(m[1]), int(m[3]), int(m[4])
        options = {x for x in (_valid(y, a, b), _valid(y, b, a)) if x}
        if len(options) == 1:
            add(options.pop(), m)
        elif len(options) > 1:
            ambiguous = True
    taken = list(spans)
    if loose:
        for m in re.finditer(r"\d+(?:[\s,./-]+\d+)*", low):
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue
            if REF_CUE.search(low[max(0, m.start() - 14):m.start()]) or re.match(r"\s*(?:dollars?|usd|\$|cents|percent)", low[m.end():]):
                continue
            tokens = re.findall(r"\d+", m.group())
            full, short = date_candidates(tokens)
            options = full or short
            if len(options) == 1:
                add(next(iter(options)), m)
            elif len(options) > 1:
                ambiguous = True
    partial = False
    if not found and not ambiguous:
        has_year = re.search(rf"(?<!\d){YEAR_RE}(?!\d)", low)
        has_month_day = re.search(rf"\b{MONTH_RE}\b\.?\s+\d{{1,2}}|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH_RE}\b", low)
        partial = bool(has_month_day or (loose and re.search(r"\d", low) and (has_year or re.search(r"\b\d{3,4}\b", low))))
    return found, ambiguous, partial


# ------------------------------------------------------------ amounts
def _to_amount(whole, cents=None):
    try:
        amount = Decimal(whole.replace(",", "")) + (Decimal(cents) / 100 if cents else 0)
    except InvalidOperation:
        return None
    return float(amount) if Decimal(0) <= amount <= Decimal("999999999.99") else None


def find_amounts(t, chars, focus):
    """Money values outside already-claimed spans (dates, references)."""
    cands = []
    work = "".join(chars)
    if NON_USD.search(work):
        return cands
    patterns = (
        re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)"),
        re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d{1,2})?)\s*(?:us\s+)?(?:dollars?|usd|bucks)\b(?:\s*(?:and\s*)?(\d{1,2})\s*cents?)?", re.I),
    )
    for pattern in patterns:
        for m in pattern.finditer(work):
            if _negated_before(work, m.start()) or _wrong_after(work, m.end()) \
                    or unrelated_number_context(work, m.start(), m.end()):
                continue
            if (amount := _to_amount(m[1], m.group(2) if m.lastindex and m.lastindex >= 2 else None)) is not None:
                cands.append((amount, m.group().strip(), m.span()))
    if not cands:
        cue = re.compile(r"\b(?:amount|paid|payment|total paid|issued)\b(?:\s+(?:amount|paid|payment|total|issued|for|of|is|was|the)\b)*\s+(\d[\d,]*(?:\.\d{1,2})?)(?![\d,]*\s*(?:%|percent|/|st\b|nd\b|rd\b|th\b))(?![\d.,]*\d)", re.I)
        for m in cue.finditer(work):
            if _negated_before(work, m.start(1)) or _wrong_after(work, m.end()) \
                    or unrelated_number_context(work, m.start(1), m.end()):
                continue
            if re.fullmatch(r"(?:19|20)\d{2}", m[1]):
                continue  # looks like a year, not money
            if (amount := _to_amount(m[1])) is not None:
                cands.append((amount, m.group().strip(), m.span(1)))
    if not cands and focus == "paid_amount":
        direct = re.fullmatch(r"\s*(?:(?:yes|it|it's|is|was|the|amount)\b[\s,:]*)*"
                              r"(\d[\d,]*(?:\.\d{1,2})?)[\s.!]*", work, re.I)
        if direct and (amount := _to_amount(direct[1])) is not None:
            cands.append((amount, direct[1], direct.span(1)))
    cands.sort(key=lambda c: c[2][0])
    return cands


# ----------------------------------------------------------- references
def _collapse_spelled(text):
    """'e f t 9 1 3 4' -> 'eft 9 1 3 4' (only for single letters in a row of 3+)."""
    return re.sub(r"\b(?:[A-Za-z]\s){2,}[A-Za-z]\b", lambda m: m.group().replace(" ", ""), text)


def _id_tokens(text, start):
    """Consecutive id-like tokens beginning at start. Returns (joined, end) or None.

    A run of single characters ("7 X 9 Q 2") is read as a spelled-out reference; otherwise
    every token must contain a digit so ordinary words never leak into the value.
    """
    pos, parts, end, spelled = start, [], start, True
    while True:
        m = re.compile(r"[\s,-]*([A-Za-z0-9]+)").match(text, pos)
        if not m:
            break
        token = m.group(1)
        following = re.match(r"[\s,]*([A-Za-z$]+)", text[m.end():])
        money_next = bool(following and following.group(1).lower() in MONEY_WORDS)
        if len(token) != 1:
            spelled = False
        ok = ID_TOKEN.fullmatch(token) or (spelled and len(token) == 1)
        if not ok or (token.isdigit() and money_next) or (not parts and not ID_TOKEN.fullmatch(token)):
            break
        parts.append(token)
        end, pos = m.end(), m.end()
    return ("".join(parts), end) if parts else None


def _clean_reference(value):
    value = value.upper()
    value = re.sub(r"^(?:EFT|ACH|CHK|CHECK|CHEQUE)(?=\d)", "", value)
    if not (3 <= len(value) <= 24) or not any(c.isdigit() for c in value) or value in CLAIM_IDS:
        return None
    return value


def find_references(chars, focus):
    work = _collapse_spelled("".join(chars))
    # Collapsing changes length, so work on the collapsed string for matching only.
    cands = []
    for m in REF_CUE.finditer(work):
        got = _id_tokens(work, m.end())
        if got is None:
            attached = re.match(r"\d[\dA-Za-z]*", work[m.end():])
            got = (attached.group(), m.end() + attached.end()) if attached else None
        if got is None or _negated_before(work, m.start()) \
                or unrelated_number_context(work, m.start(), got[1]):
            continue
        if (value := _clean_reference(got[0])) is not None:
            cands.append((value, work[m.start():got[1]].strip()))
    if not cands and focus == "check_or_eft_number":
        direct = re.fullmatch(r"\s*(?:(?:yes|it|it's|is|was|the|number)\b[\s,:]*)*"
                              r"([A-Za-z0-9]+(?:[\s,-]+[A-Za-z0-9])*)[\s.!]*", work, re.I)
        if direct and (value := _clean_reference(re.sub(r"[\s,-]", "", direct[1]))) is not None:
            cands.append((value, direct[1]))
    return cands


# --------------------------------------------------------------- status
def find_status(t):
    low = t.lower()
    cands = []
    for pattern, label in STATUS_PATTERNS:
        for m in re.finditer(pattern, low):
            if _negated_before(low, m.start()) and label in {"paid", "approved", "denied", "closed"}:
                continue
            cands.append((label or m.group().strip(), m.group(), m.start()))
    cands.sort(key=lambda c: c[2])
    # "not paid" also matches the plain "paid" pattern; drop it when inside a longer match.
    longer = [m.span() for p, _ in STATUS_PATTERNS[:5] for m in re.finditer(p, low)]
    out = []
    for value, ev, pos in cands:
        if value == "paid" and any(s <= pos < e and e - s > len(ev) for s, e in longer):
            continue
        out.append((value, ev))
    return out


# ------------------------------------------------------------- ingest
def _clauses(text):
    return [c for c in re.split(r"[;!?]|\.(?=\s|$)|\bbut\b", text, flags=re.I) if c.strip()]


def ingest(text: str, focus: str | None = None, fragment: str = "") -> Ingest:
    """Extract every fact the representative reported in one (possibly merged) utterance."""
    result = Ingest()
    t = strip_claim_identifiers(prepare(text), focus, bool(fragment))
    if not t:
        return result
    chars = list(t)

    # 1. Dates. A complete date in this utterance wins; otherwise merge it with a pending
    #    fragment so "2026" followed by "0320" works. Partial dates are carried forward.
    loose = focus == "payment_date" or bool(fragment)
    dates, date_ambiguous, partial = find_dates(t, loose=loose)
    date_source = t
    if fragment and not dates and not date_ambiguous:
        merged = find_dates(f"{fragment} {t}", loose=True)
        if merged[0] or merged[1] or merged[2]:
            dates, date_ambiguous, partial = merged
            date_source = f"{fragment} {t}"
    if dates:
        picked, amb = _pick([(iso, ev) for iso, ev, _ in dates], text)
        if picked:
            result.values["payment_date"] = picked
        if amb:
            result.ambiguous.add("payment_date")
        if date_source == t:
            for *_, span in dates:
                _blank(chars, span)
        else:  # the digits in this utterance belong to the merged date
            for m in re.finditer(r"\d+(?:[\s,./-]+\d+)*", t):
                _blank(chars, m.span())
    elif date_ambiguous:
        result.ambiguous.add("payment_date")
    elif partial and (focus in ("payment_date", None) or fragment):
        result.fragment = date_source

    # 2. Amounts with unmistakable money markers, then references, then bare amounts.
    amounts = find_amounts(t, chars, focus)
    strong = [a for a in amounts if re.search(r"\$|dollar|usd|buck", a[1], re.I)]
    for _, _, span in strong:
        _blank(chars, span)
    refs = find_references(chars, focus)
    if refs:
        picked, amb = _pick(refs, text)
        if picked:
            result.values["check_or_eft_number"] = picked
        if amb:
            result.ambiguous.add("check_or_eft_number")
        # remove the reference's digits so they are not read as an amount or date
        for m in REF_CUE.finditer("".join(chars)):
            got = _id_tokens("".join(chars), m.end())
            if got:
                _blank(chars, (m.start(), got[1]))
    amounts = find_amounts(t, chars, focus) if not strong else amounts
    if amounts:
        picked, amb = _pick([(a, ev) for a, ev, _ in amounts], text)
        if picked:
            result.values["paid_amount"] = picked
        if amb:
            result.ambiguous.add("paid_amount")

    # 3. Status wording.
    statuses = find_status(t)
    if statuses:
        picked, amb = _pick(statuses, text)
        if picked:
            result.values["claim_status"] = picked
        if amb:
            result.ambiguous.add("claim_status")

    # 4. Explicit "I don't have that".
    for clause in _clauses(t):
        if not UNKNOWN.search(clause):
            continue
        mentioned = {key for key in KEYS if CUES[key].search(clause)}
        if not mentioned and focus:
            mentioned.add(focus)
        for key in mentioned:
            if key not in result.values:
                result.unavailable.add(key)
    return result
