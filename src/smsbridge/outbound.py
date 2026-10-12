"""From an agent's raw reply to SMS-ready message parts.

    raw reply -> sanitise -> Markdown to plain text -> GSM-7 normalisation -> split -> count

Order matters. Sanitising first means a traceback inside a code fence is removed before the
fence markers are; normalising before splitting means every part is measured exactly as it
will be sent.

Twilio rejects a body over 1,600 characters (error 21617), so a long reply is split into
several messages at the most natural boundary available - paragraph, line, sentence, word -
and never mid-word unless a single token is longer than a whole message. Lengths are measured
in UTF-16 code units, which is never less than the character count, so emoji-heavy text still
fits.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gsm7 import MODES, SegmentInfo, normalise, segment_info
from .plaintext import markdown_to_sms
from .sanitize import Sanitizer

MAX_BODY_UNITS = 1600
ELLIPSIS = "..."


@dataclass(frozen=True)
class OutboundPart:
    body: str
    info: SegmentInfo


@dataclass(frozen=True)
class OutboundPlan:
    parts: tuple[OutboundPart, ...]
    removed_lines: int = 0
    redactions: int = 0
    fallback_used: bool = False
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.parts

    @property
    def segments(self) -> int:
        return sum(part.info.segments for part in self.parts)

    @property
    def text(self) -> str:
        return "\n\n".join(part.body for part in self.parts)


def utf16_units(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _fit(text: str, limit: int) -> int:
    """Length of the longest prefix of `text` that is at most `limit` UTF-16 units."""
    used = 0
    for index, ch in enumerate(text):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > limit:
            return index
    return len(text)


def _cut_point(text: str, end: int) -> int:
    """Best place to cut `text[:end]`: paragraph, line, sentence, word - else `end`."""
    window = text[:end]
    floor = end // 2  # a "natural" boundary that leaves a tiny first part isn't worth it
    for separator in ("\n\n", "\n"):
        index = window.rfind(separator)
        if index >= floor:
            return index + len(separator)
    sentence = max(window.rfind(mark) for mark in (". ", "! ", "? "))
    if sentence >= floor:
        return sentence + 1
    space = window.rfind(" ")
    if space >= floor:
        return space + 1
    return end


def split_text(text: str, limit: int = MAX_BODY_UNITS) -> list[str]:
    parts: list[str] = []
    rest = text.strip()
    while rest:
        if utf16_units(rest) <= limit:
            parts.append(rest)
            break
        cut = _cut_point(rest, _fit(rest, limit))
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return parts


def _with_ellipsis(text: str, limit: int) -> str:
    budget = limit - len(ELLIPSIS)
    if utf16_units(text) > budget:
        end = _fit(text, budget)
        space = text.rfind(" ", 0, end)
        text = text[:space if space >= end // 2 else end]
    return text.rstrip() + ELLIPSIS


class OutboundComposer:
    def __init__(self, *, sanitizer: Sanitizer | None = None, gsm_mode: str = "lookalikes",
                 max_parts: int = 3, max_part_units: int = MAX_BODY_UNITS) -> None:
        if gsm_mode not in MODES:
            raise ValueError(f"gsm_mode must be one of {', '.join(MODES)}")
        if max_parts < 1:
            raise ValueError("max_parts must be at least 1")
        if not 20 <= max_part_units <= MAX_BODY_UNITS:
            raise ValueError(f"max_part_units must be between 20 and {MAX_BODY_UNITS}")
        self.sanitizer = sanitizer or Sanitizer()
        self.gsm_mode = gsm_mode
        self.max_parts = max_parts
        self.max_part_units = max_part_units

    def compose(self, raw: str) -> OutboundPlan:
        cleaned = self.sanitizer.clean(raw or "")
        text = normalise(markdown_to_sms(cleaned.text), self.gsm_mode).strip()
        pieces = split_text(text, self.max_part_units) if text else []
        truncated = len(pieces) > self.max_parts
        if truncated:
            pieces = pieces[:self.max_parts]
            pieces[-1] = _with_ellipsis(pieces[-1], self.max_part_units)
        return OutboundPlan(
            parts=tuple(OutboundPart(piece, segment_info(piece)) for piece in pieces),
            removed_lines=cleaned.removed_lines,
            redactions=cleaned.redactions,
            fallback_used=cleaned.fallback_used,
            truncated=truncated,
        )
