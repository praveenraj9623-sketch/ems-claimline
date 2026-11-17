"""Turn spoken number words into digits so speech-to-text output can be parsed reliably.

Examples (each result is a string of digit "atoms" separated by spaces):

    "nine one three four"            -> "9 1 3 4"
    "two hundred forty dollars"      -> "240 dollars"
    "twenty twenty six zero three twenty" -> "20 26 0 3 20"
    "march twentieth"                -> "march 20th"

Atoms are intentionally kept apart: the date and reference parsers decide how to
group them, because "twenty twenty six" is a year but "twenty six" can be a day.
"""

import re

ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * (i + 2) for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())}
ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}
NUMBER_WORDS = set(ONES) | set(TENS) | set(ORDINALS) | {"hundred", "thousand", "oh", "and"}


def ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _read_below_100(words, i):
    """Return (value, next_index, is_ordinal) or None."""
    w = words[i]
    if w in ORDINALS:
        return ORDINALS[w], i + 1, True
    if w in TENS:
        value = TENS[w]
        if i + 1 < len(words):
            nxt = words[i + 1]
            if nxt in ONES and 1 <= ONES[nxt] <= 9:
                return value + ONES[nxt], i + 2, False
            if nxt in ORDINALS and 1 <= ORDINALS[nxt] <= 9:
                return value + ORDINALS[nxt], i + 2, True
        return value, i + 1, False
    if w in ONES and w != "zero":
        return ONES[w], i + 1, False
    return None


def _read_below_1000(words, i):
    if words[i] in ONES and 1 <= ONES[words[i]] <= 9 and i + 1 < len(words) and words[i + 1] == "hundred":
        value, j = ONES[words[i]] * 100, i + 2
        if j < len(words) and words[j] == "and":
            j += 1
        rest = _read_below_100(words, j) if j < len(words) else None
        if rest and not rest[2]:
            return value + rest[0], rest[1], False
        if rest and rest[2]:
            return value + rest[0], rest[1], True
        return value, j if j == i + 2 else i + 2, False
    return _read_below_100(words, i)


def _read_number(words, i):
    """Read one 'atom': a standalone English number. Returns (text, next_index)."""
    w = words[i]
    if w in ("zero", "oh"):
        return "0", i + 1
    head = _read_below_1000(words, i)
    if head is None:
        return None
    value, j, ordinal = head
    if not ordinal and j < len(words) and words[j] == "thousand":
        value, j = value * 1000, j + 1
        k = j + 1 if j < len(words) and words[j] == "and" else j
        tail = _read_below_1000(words, k) if k < len(words) else None
        if tail:
            value, j, ordinal = value + tail[0], tail[1], tail[2]
    return (f"{value}{ordinal_suffix(value)}" if ordinal else str(value)), j


def spoken_to_digits(text: str) -> str:
    """Replace runs of number words with digit atoms. Other text is left untouched."""
    text = text.replace("’", "'")
    text = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", text)  # twenty-six -> twenty six
    parts = re.split(r"([A-Za-z']+)", text)
    out, i = [], 0
    # parts alternates: non-word, word, non-word, word ...
    tokens = [(p, i % 2 == 1) for i, p in enumerate(parts)]
    idx = 0
    while idx < len(tokens):
        piece, is_word = tokens[idx]
        if not is_word or piece.lower() not in NUMBER_WORDS or piece.lower() == "and":
            out.append(piece)
            idx += 1
            continue
        # collect a run of number words separated only by spaces/commas
        run_idx, words = [idx], [piece.lower()]
        j = idx + 1
        while j + 1 < len(tokens):
            gap, nxt = tokens[j][0], tokens[j + 1][0]
            if (re.fullmatch(r"[ ,]*", gap) and tokens[j + 1][1] and nxt.lower() in NUMBER_WORDS):
                words.append(nxt.lower())
                run_idx.append(j + 1)
                j += 2
            else:
                break
        atoms, k = [], 0
        while k < len(words):
            if words[k] in ("and", "hundred", "thousand"):
                k += 1
                continue
            got = _read_number(words, k)
            if got is None:
                k += 1
                continue
            atoms.append(got[0])
            k = got[1]
        if words == ["oh"] or not atoms:
            out.extend(tokens[x][0] for x in range(idx, j))  # a lone "oh" is an interjection
        else:
            out.append(" ".join(atoms))
        idx = j
    return "".join(out)
