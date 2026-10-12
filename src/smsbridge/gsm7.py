"""GSM-7 vs UCS-2: encoding detection, segment counting and lookalike normalisation.

An SMS travels as GSM-7 (160 septets, or 153 per part once it has to be split) only if
*every* character is in the GSM 03.38 alphabet. A single character outside it - a curly
apostrophe, a non-breaking hyphen, a tab - silently switches the whole message to UCS-2:
70 UTF-16 code units, or 67 per part. The text looks identical on the handset and costs two
to three times as many segments; long UCS-2 messages are also more likely to be dropped.

Counting rules implemented here (they match how carriers pack concatenated messages):

* GSM-7 extension characters (``^ { } \\ [ ~ ] | €`` and form feed) are sent as an escape
  plus a character, so they cost two septets - and the pair is never split across parts.
* In UCS-2, characters outside the Basic Multilingual Plane (most emoji) are surrogate pairs
  that cost two units, and a pair is never split across parts either.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# GSM 03.38 default alphabet, in code-point order (0x00-0x7F). 0x1B is the escape to the
# extension table, not a printable character.
_BASIC_TABLE = (
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\x1b\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./"
    "0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmno"
    "pqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)
BASIC = frozenset(_BASIC_TABLE) - {"\x1b"}
EXTENDED = frozenset("\f^{}\\[~]|\u20ac")
_ALPHABET = BASIC | EXTENDED

GSM7 = "GSM-7"
UCS2 = "UCS-2"
SINGLE_PART_CAPACITY = {GSM7: 160, UCS2: 70}
MULTI_PART_CAPACITY = {GSM7: 153, UCS2: 67}

MODES = ("off", "lookalikes", "strict")

# Typographic lookalikes -> GSM-7 text with the same meaning. Applied only when the whole
# message then fits GSM-7 (see `normalise`).
LOOKALIKES: dict[str, str] = {
    # apostrophes, single quotes, primes
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u2035": "'",
    "\u00b4": "'", "`": "'", "\u02bc": "'", "\u02b9": "'", "\u2039": "'", "\u203a": "'",
    # double quotes
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"', "\u2036": '"',
    "\u00ab": '"', "\u00bb": '"',
    # hyphens and dashes (U+2011 is the non-breaking hyphen word processors love)
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2212": "-", "\u2043": "-", "\ufe58": "-", "\ufe63": "-", "\uff0d": "-",
    # spaces (no-break, thin, figure, ideographic, ...) and tab
    "\t": " ", "\u00a0": " ", "\u1680": " ", "\u2000": " ", "\u2001": " ", "\u2002": " ",
    "\u2003": " ", "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ", "\u3000": " ",
    # invisible characters: soft hyphen, zero-width space/joiners, direction marks, BOM
    "\u00ad": "", "\u200b": "", "\u200c": "", "\u200d": "", "\u200e": "", "\u200f": "",
    "\u2060": "", "\ufeff": "",
    # punctuation
    "\u2026": "...", "\u2024": ".", "\u2025": "..", "\u2022": "-", "\u2023": "-",
    "\u25e6": "-", "\u2219": "-", "\u2044": "/",
    # arrows
    "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u21d2": "=>", "\u21d0": "<=",
    "\u21d4": "<=>",
    # maths
    "\u00d7": "x", "\u00f7": "/", "\u2264": "<=", "\u2265": ">=", "\u2260": "!=",
    "\u00b1": "+/-",
    # symbols, fractions, ligatures
    "\u2122": "TM", "\u00a9": "(c)", "\u00ae": "(R)", "\u00bd": "1/2", "\u00bc": "1/4",
    "\u00be": "3/4", "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl",
}
_TRANSLATION = {ord(source): target for source, target in LOOKALIKES.items()}

# Letters that Unicode decomposition does not reduce to a GSM-7 base letter.
_STRICT_EXTRAS = {
    "\u0141": "L", "\u0142": "l", "\u0110": "D", "\u0111": "d", "\u00d0": "D", "\u00f0": "d",
    "\u00de": "Th", "\u00fe": "th", "\u0152": "OE", "\u0153": "oe", "\u0131": "i",
    "\u0126": "H", "\u0127": "h", "\u0166": "T", "\u0167": "t",
}
_SPACE_RUNS = re.compile(r" {2,}")


@dataclass(frozen=True)
class SegmentInfo:
    encoding: str
    units: int  # septets for GSM-7, UTF-16 code units for UCS-2
    segments: int
    offenders: tuple[str, ...] = ()  # the characters that forced UCS-2, in order

    @property
    def capacity(self) -> int:
        """Units available per segment at this length (160/153 or 70/67)."""
        table = SINGLE_PART_CAPACITY if self.segments <= 1 else MULTI_PART_CAPACITY
        return table[self.encoding]


def non_gsm7(text: str) -> tuple[str, ...]:
    """Each character of `text` outside the GSM-7 alphabet, once, in order of appearance."""
    return tuple(dict.fromkeys(ch for ch in text if ch not in _ALPHABET))


def is_gsm7(text: str) -> bool:
    return all(ch in _ALPHABET for ch in text)


def segment_info(text: str) -> SegmentInfo:
    offenders = non_gsm7(text)
    if offenders:
        encoding = UCS2
        costs = [2 if ord(ch) > 0xFFFF else 1 for ch in text]
    else:
        encoding = GSM7
        costs = [2 if ch in EXTENDED else 1 for ch in text]
    segments = _count_segments(costs, SINGLE_PART_CAPACITY[encoding],
                               MULTI_PART_CAPACITY[encoding])
    return SegmentInfo(encoding, sum(costs), segments, offenders)


def _count_segments(costs: list[int], single: int, multi: int) -> int:
    total = sum(costs)
    if total == 0:
        return 0
    if total <= single:
        return 1
    segments, used = 1, 0
    for cost in costs:
        if used + cost > multi:  # an escape pair / surrogate pair never straddles two parts
            segments += 1
            used = 0
        used += cost
    return segments


def normalise(text: str, mode: str = "lookalikes") -> str:
    """Make `text` cheaper to send without changing what it says.

    * ``off``        - unchanged.
    * ``lookalikes`` - replace typographic lookalikes (curly quotes, dashes, odd spaces,
      invisible characters, ...) *if that makes the whole message GSM-7*. A message that
      needs UCS-2 anyway (emoji, non-Latin scripts) is left exactly as written: rewriting it
      gains nothing and can even make it longer ("…" is one UTF-16 unit, "..." is three).
    * ``strict``     - always produce GSM-7: also transliterate accented letters to their base
      letter and drop anything that has no GSM-7 equivalent (emoji, other scripts). Lossy.
    """
    if mode not in MODES:
        raise ValueError(f"unknown GSM mode {mode!r}; expected one of {', '.join(MODES)}")
    if mode == "off" or not text:
        return text
    mapped = text.replace("\r\n", "\n").translate(_TRANSLATION)
    if mode == "lookalikes":
        return mapped if is_gsm7(mapped) else text
    return _transliterate(mapped)


def _transliterate(text: str) -> str:
    out: list[str] = []
    dropped = False
    for ch in text:
        if ch in _ALPHABET:
            out.append(ch)
            continue
        replacement = _strict_char(ch)
        dropped = dropped or not replacement
        out.append(replacement)
    result = "".join(out)
    return _SPACE_RUNS.sub(" ", result) if dropped else result


def _strict_char(ch: str) -> str:
    if ch in _STRICT_EXTRAS:
        return _STRICT_EXTRAS[ch]
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    base = base.translate(_TRANSLATION)
    return base if base and is_gsm7(base) else ""
