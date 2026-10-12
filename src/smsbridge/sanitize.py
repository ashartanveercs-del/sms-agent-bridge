"""Keep agent internals out of text messages.

Agents fail in ways that leak: a tool raises and the traceback ends up in the reply, a model
quotes the file it just wrote, a log line or an API error blob is pasted verbatim, a key from
the environment gets echoed back. This module is the single outbound choke point that stops
that from reaching a customer's phone.

Policy:

* Lines that are diagnostics (tracebacks, stack frames, exception lines, tool-failure notes,
  log lines, raw JSON error bodies, internal file paths) are dropped whole; the rest of the
  reply is kept.
* Credential-shaped strings inside an otherwise normal line are masked in place.
* If nothing meaningful survives, the reply becomes a neutral fallback - the customer gets a
  polite message, never silence and never the error.
* An empty reply stays empty: an agent choosing not to answer is not an error.

The rules are anchored and specific on purpose. "Your payment failed" or a URL that happens to
contain `/var/` are ordinary sentences, and the tests pin both sides of that line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_FALLBACK = "Sorry - something went wrong on my side. Please try again in a moment."
REDACTED = "[redacted]"

_TRACEBACK = re.compile(r"^\s*Traceback \(most recent call last\):")
# The last line of a traceback: an exception name, optionally followed by ": message".
_TRACEBACK_TAIL = re.compile(r"^\s*[A-Za-z_][\w.]*\s*(?::.*)?$")

_LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("chained-exception", re.compile(
        r"^\s*(?:During handling of the above exception|"
        r"The above exception was the direct cause)")),
    ("python-frame", re.compile(r'^\s*File "[^"]+", line \d+')),
    ("js-frame", re.compile(
        r"^\s*at\s+(?:\S+\s+)?\(?(?:file://|node:|[A-Za-z]:\\|/)[^\s)]*:\d+(?::\d+)?\)?\s*$")),
    ("exception", re.compile(r"^\s*(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception)(?:\s*:|\s*$)")),
    ("error-prefix", re.compile(
        r"^\s*(?:error|fatal(?:\s+error)?|panic|unhandled\s+(?:exception|error|rejection)|"
        r"internal server error)\s*[:!\]]", re.IGNORECASE)),
    ("log-line", re.compile(
        r"^\s*(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\S*\s+"
        r"(?:ERROR|CRITICAL|FATAL|WARNING|DEBUG)\b|"
        r"\[(?:ERROR|CRITICAL|FATAL|WARN|WARNING|DEBUG|TRACE)\])")),
    ("errno", re.compile(
        r"\[Errno -?\d+\]|\b(?:ENOENT|EACCES|EPERM|ECONNREFUSED|ECONNRESET|ETIMEDOUT|"
        r"EADDRINUSE|ENOTFOUND|EPIPE)\b")),
    ("tool-failure", re.compile(
        r"^\s*(?:tool|command|exec|subprocess|function call)\b.{0,80}?"
        r"\b(?:failed|errored|error|crashed|timed out|exited with|non-zero exit)",
        re.IGNORECASE)),
    ("tool-tag", re.compile(
        r"^\s*\[(?:tool|exec|command|function)\b[^\]]*\].{0,80}?"
        r"\b(?:failed|errored|error|timed out)", re.IGNORECASE)),
    ("json-error", re.compile(
        r'^\s*\{.*"(?:error|errors|exception|traceback|stack|stacktrace)"\s*:', re.IGNORECASE)),
    # Absolute paths under well-known system roots, Windows drive paths, home-relative paths
    # and file:// URIs. The look-behind skips URL paths such as https://example.com/var/x.
    ("internal-path", re.compile(
        r"(?<![\w.:/~-])/(?:home|root|var|srv|opt|usr|etc|tmp|app|mnt|proc|sys|run|Users|"
        r"workspace|data)/\S+"
        r"|(?<!\w)[A-Za-z]:\\\S+"
        r"|(?<![\w/])~/\S+"
        r"|\bfile://\S+")),
)

_SECRETS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),                        # OpenAI-style keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                           # AWS access key ids
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),                 # GitHub tokens
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),                # Slack tokens
    re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"),           # JWTs
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"),  # auth headers
    re.compile(                                                     # FOO_API_KEY=..., etc.
        r"\b(?:[A-Z0-9]+_)*(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN)(?:_[A-Z0-9]+)*"
        r"\s*=\s*\S+"),
)

_BLANK_RUNS = re.compile(r"\n{3,}")
_MEANINGFUL = re.compile(r"\w")


@dataclass(frozen=True)
class SanitizeResult:
    text: str
    removed_lines: int = 0
    redactions: int = 0
    fallback_used: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.removed_lines or self.redactions or self.fallback_used)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


class Sanitizer:
    def __init__(self, *, fallback: str = DEFAULT_FALLBACK,
                 extra_patterns: Iterable[str | re.Pattern[str]] = ()) -> None:
        self.fallback = fallback
        self._extra = tuple(re.compile(p) if isinstance(p, str) else p for p in extra_patterns)

    def is_diagnostic(self, line: str) -> bool:
        return (any(rule.search(line) for _, rule in _LINE_RULES)
                or any(rule.search(line) for rule in self._extra))

    def clean(self, text: str) -> SanitizeResult:
        if not text or not text.strip():
            return SanitizeResult("")  # choosing not to reply is not an error

        kept: list[str] = []
        removed = redactions = 0
        block_indent: int | None = None  # indentation of an open "Traceback ..." block
        for line in text.splitlines():
            if block_indent is not None:
                indent = _indent(line)
                if not line.strip() or indent > block_indent:
                    removed += bool(line.strip())
                    continue
                opened_at, block_indent = block_indent, None
                if indent == opened_at and _TRACEBACK_TAIL.match(line):
                    removed += 1  # "KeyError: 'x'" closes the block
                    continue
                # anything else ends the block and is judged on its own below
            if _TRACEBACK.match(line):
                block_indent = _indent(line)
                removed += 1
                continue
            if self.is_diagnostic(line):
                removed += 1
                continue
            for pattern in _SECRETS:
                line, count = pattern.subn(REDACTED, line)
                redactions += count
            kept.append(line.rstrip())

        out = _BLANK_RUNS.sub("\n\n", "\n".join(kept)).strip()
        if removed and not _MEANINGFUL.search(out):
            return SanitizeResult(self.fallback, removed, redactions, fallback_used=True)
        return SanitizeResult(out, removed, redactions)
