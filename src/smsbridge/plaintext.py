"""Turn an agent's Markdown into text that reads well on a phone.

Chat models answer in Markdown. On SMS the markers are noise (and every `*` costs a septet),
so they are removed while the words, list structure and link targets are kept. The rules are
deliberately conservative: `snake_case`, `2*3*4` and lone symbols are left alone.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^[ \t]*(?:```|~~~).*(?:\n|$)", re.MULTILINE)
_RULE = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*(?:\n|$)", re.MULTILINE)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_QUOTE = re.compile(r"^[ \t]{0,3}(?:>[ \t]?)+", re.MULTILINE)
_BULLET = re.compile(r"^([ \t]*)[*+\u2022][ \t]+", re.MULTILINE)
_IMAGE = re.compile(r'!\[([^\]]*)\]\(\s*(\S+?)(?:\s+"[^"]*")?\s*\)')
_LINK = re.compile(r'\[([^\]]+)\]\(\s*(\S+?)(?:\s+"[^"]*")?\s*\)')
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_STRIKE = re.compile(r"[ \t]?~~[^~\n]+~~")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1")
_BOLD_ITALIC = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])")
_ITALIC_UNDERSCORE = re.compile(r"(?<!\w)_(?=\S)([^_\n]*?\S)_(?!\w)")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUNS = re.compile(r"\n{3,}")


def _link(match: re.Match[str]) -> str:
    text, url = match.group(1).strip(), match.group(2)
    if not text or text in (url, url.split("://", 1)[-1]):
        return url
    return f"{text} ({url})"


def markdown_to_sms(text: str) -> str:
    if not text.strip():
        return ""
    text = _FENCE.sub("", text)
    text = _RULE.sub("", text)
    text = _HEADING.sub(r"\1", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub(r"\1- ", text)
    text = _IMAGE.sub(_link, text)
    text = _LINK.sub(_link, text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _STRIKE.sub("", text)
    text = _INLINE_CODE.sub(r"\2", text)
    text = _BOLD_ITALIC.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDERSCORE.sub(r"\1", text)
    text = _TRAILING_SPACE.sub("", text)
    return _BLANK_RUNS.sub("\n\n", text).strip()
