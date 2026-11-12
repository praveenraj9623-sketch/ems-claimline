"""Exact transcript evidence and the configurable claim fields."""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

STATUS = re.compile(r"\b(?:not yet paid|not paid|unpaid|partially paid|paid|denied|rejected|pending|processing|in progress|in process|approved|closed|received|not found)\b", re.I)
AMOUNT = re.compile(r"(?<![\w.,+\-−])(?:USD\s*)?\$?\s*(?:\d{1,3}(?:,\d{3})+|\d{1,9})(?:\.\d{1,2})?(?:\s*(?:US dollars|dollars|USD))?(?![\w,.]\d|[\w])", re.I)
SPOKEN_YEAR = r"(?:19|20)[0-9]{2}"
DATE_GAP = r"(?:[.,]\s*|\s+)"
SPOKEN_DATE = re.compile(
    rf"{SPOKEN_YEAR}{DATE_GAP}(?:[0-9]{{1,2}}{DATE_GAP}[0-9]{{1,2}}|[0-9]{{3,4}})(?!\d|{DATE_GAP}\d)"
)
DATE = re.compile(rf"(?<!\d)(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{4}}|{SPOKEN_DATE.pattern}|"
                  r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})(?!\d)", re.I)
DIGIT_WORDS = dict(zip("zero one two three four five six seven eight nine".split(), "0123456789"))
DIGIT_WORDS["oh"] = "0"
DIGIT = r"(?:[0-9]|" + "|".join(DIGIT_WORDS) + r")"
SPOKEN_REFERENCE = re.compile(rf"{DIGIT}(?:\s+{DIGIT}){{1,29}}", re.I)
REFERENCE = re.compile(
    rf"(?<![\w-])(?:{SPOKEN_REFERENCE.pattern}|(?:EFT|CHK|ACH)\s+\d{{2,15}}|"
    r"[A-Z]\d{1,3}\s+\d{2,12}\s+[A-Z]{2,3}(?=[.,;!?]|$)|[A-Z0-9][A-Z0-9-]{1,29})(?![\w-])", re.I
)
NEGATED = re.compile(r"(?:\b(?:not|no|never|isn't|wasn't|weren't|don't|didn't|cannot|can't)\b|\b(?:might be|could be|probably|maybe|for example|for instance|hypothetically|guess|pretend|assume|make up)\b)[^.;!?]{0,24}$", re.I)
UNKNOWN = re.compile(r"\b(?:unknown|unavailable|not available|not provided|not issued|cannot share|can't share|don't have|do not have|no payment|no check|no EFT|no reference|none|no amount|no date)\b", re.I)


def normalize_status(value):
    status = value.lower()
    aliases = {"processing": "pending", "in progress": "pending", "in process": "pending",
               "not yet paid": "unpaid", "not paid": "unpaid"}
    return aliases.get(status, status)


def normalize_amount(value):
    number = re.sub(r"(?i)(?:USD|US dollars|dollars|[\s$,])", "", value)
    amount = Decimal(number)
    if not amount.is_finite() or not Decimal(0) <= amount <= Decimal("999999999.99"):
        raise ValueError("Invalid amount")
    return float(amount)


def normalize_date(value):
    if SPOKEN_DATE.fullmatch(value):
        year, *parts = re.split(DATE_GAP, value)
        if len(parts) == 2:
            return datetime(int(year), int(parts[0]), int(parts[1])).date().isoformat()
        digits = parts[0]
        dates = set()
        for split in range(max(1, len(digits) - 2), 3):
            try:
                dates.add(datetime(int(year), int(digits[:split]), int(digits[split:])).date().isoformat())
            except ValueError:
                continue
        if len(dates) == 1:
            return dates.pop()
        raise ValueError("Invalid or ambiguous spoken date")
    cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b|,", "", value, flags=re.I)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError("Invalid date")


def normalize_reference(value):
    if SPOKEN_REFERENCE.fullmatch(value):
        value = "".join(DIGIT_WORDS.get(digit, digit) for digit in value.lower().split())
    cleaned = re.sub(r"[\s-]", "", value)
    if not (2 <= len(cleaned) <= 30 and cleaned.isascii() and cleaned.isalnum()
            and any(char.isdigit() for char in cleaned)):
        raise ValueError("Invalid payment reference")
    if cleaned.upper() in {"TEST48213", "03142026", "20260314"}:
        raise ValueError("Claim identifiers are not payment references")
    return value.upper()


def normalize_text(value):
    return value.strip()


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    question: str
    pattern: re.Pattern
    cues: re.Pattern
    normalize: Callable


FIELDS = (
    Field("claim_status", "Claim status", "What is the current claim status?", STATUS,
          re.compile(r"\b(?:claim|status|it\s+(?:is|was)|currently)\b", re.I), normalize_status),
    Field("paid_amount", "Paid amount", "What amount was paid?", AMOUNT,
          re.compile(r"\b(?:amount|paid|payment\s+of|issued\s+for)\b|\$|\bdollars\b", re.I), normalize_amount),
    Field("check_or_eft_number", "Check or EFT number", "What is the check or EFT number?", REFERENCE,
          re.compile(r"\b(?:check|EFT|payment\s+(?:reference|number)|reference(?:\s+number)?)\b", re.I), normalize_reference),
    Field("payment_date", "Payment date", "On what date was the payment made?", DATE,
          re.compile(r"\b(?:date\s*[:=]|(?:payment\s+date|date\s+paid|paid[^;!?]{0,50}\bon|issued\s+on|posted\s+on|payment\s+was\s+made\s+on)\b)", re.I), normalize_date),
)
CORE_KEYS = ("claim_status", "paid_amount", "check_or_eft_number", "payment_date")


def candidate(field, text, focus=None):
    """Return a validated value and a literal quote from this representative turn."""
    key = field.key
    if key == "paid_amount" and re.search(r"\b(?:EUR|INR|GBP|euros|rupees|pounds)\b|[€₹£]", text, re.I):
        return None
    date_spans = [match.span() for match in DATE.finditer(text)]
    matches = []
    for match in field.pattern.finditer(text):
        quote = match.group().strip()
        before = text[:match.start()]
        after = text[match.end():]
        clause = re.split(r"[;!?]|\.(?=\s|$)", before)[-1]
        # A standalone "No," can reject a read-back before stating a correction.
        clause = re.sub(r"^\s*no,\s+", "", clause, flags=re.I)
        tail = re.split(r"[;!?]|\.(?=\s|$)", after)[0]
        vicinity = clause[-85:] + " " + quote + tail[:26]
        standalone = text.strip(" .!?") == quote
        if NEGATED.search(clause) or re.match(r"\s*(?:is wrong|was wrong|is incorrect)\b", after, re.I):
            continue
        if re.search(r"\b(?:for example|for instance|hypothetically|pretend|make up)\b", clause, re.I):
            continue
        follows_reference = re.search(
            r"\b(?:check|EFT|ACH|payment reference|reference)\s*(?:(?:number|no\.?|is|was)\b[ .]*|[:#-]\s*)*\s*$",
            clause, re.I,
        )
        if key == "check_or_eft_number":
            has_prefix = re.match(r"(?:EFT|CHK|CHECK|ACH)[ -]?[0-9]", quote, re.I)
            if not (has_prefix or follows_reference or (focus == key and standalone)):
                continue
        elif not field.cues.search(vicinity) and not (focus == key and (standalone or key == "payment_date")):
            continue
        if key == "paid_amount":
            if any(start < match.end() and match.start() < end for start, end in date_spans):
                continue
            if re.match(r"\s*(?:/|[-–:]\s*\d|percent\b|%|\d|or so\b)", after, re.I):
                continue
            if follows_reference or re.search(r"(?:[$\d/.,+−-]|\b(?:on|service date|TEST)\s*[:#-]?)\s*$", clause, re.I):
                continue
            if re.search(r"\b(?:billed|charged|allowed|requested|estimated|adjustment|about|around|roughly|approximately)\b[^.;!?]{0,26}$", clause, re.I):
                continue
        if key == "payment_date" and re.search(r"\b(?:date of service|service date|birth date|received on)\b[^,;]{0,20}$", clause, re.I):
            continue
        try:
            value = field.normalize(quote)
        except (ValueError, ArithmeticError):
            continue
        alternative = re.match(r"\s+(?:or|to)\s+", after, re.I)
        if alternative and (other := field.pattern.match(after, alternative.end())):
            try:
                if field.normalize(other.group().strip()) != value:
                    return None
            except (ValueError, ArithmeticError):
                pass
        matches.append((value, quote))
    if not matches:
        return None
    unique = {value for value, _ in matches}
    if len(unique) > 1 and not re.search(r"\b(?:correction|actually|correct(?:ed)?|instead|sorry)\b", text, re.I):
        return None
    return matches[-1]
