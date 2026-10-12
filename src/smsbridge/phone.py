"""Phone-number helpers and carrier compliance keywords."""

from __future__ import annotations

_DIGITS = frozenset("0123456789")

# On a Twilio long code these words, sent as the whole message, opt the sender out (or back
# in, or ask for help) at the platform level - Twilio answers them itself. An agent reply to
# an opt-out would be refused anyway (error 21610), so the bridge never runs a turn for them.
# "YES" is also an opt-in word on long codes but is left out on purpose: it is far more often
# an ordinary answer to the agent's question.
_KEYWORDS = {
    **dict.fromkeys(("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE",
                     "OPTOUT"), "opt-out"),
    **dict.fromkeys(("START", "UNSTOP"), "opt-in"),
    **dict.fromkeys(("HELP", "INFO"), "help"),
}


def normalise_number(raw: str) -> str:
    """'+1 (555) 000-0199' -> '+15550000199'. Short codes keep their bare digits."""
    raw = (raw or "").strip()
    digits = "".join(ch for ch in raw if ch in _DIGITS)
    if not digits:
        return ""
    return "+" + digits if raw.startswith("+") else digits


def mask_number(number: str) -> str:
    """For logs: '+15550000199' -> '+*******0199'."""
    digits = "".join(ch for ch in number or "" if ch in _DIGITS)
    if not digits:
        return "?"
    shown = digits[-4:]
    return ("+" if number.startswith("+") else "") + "*" * (len(digits) - len(shown)) + shown


def compliance_keyword(body: str) -> str | None:
    """'opt-out', 'opt-in' or 'help' when the whole message is one of those keywords."""
    word = (body or "").strip().rstrip(".!").strip().upper()
    return _KEYWORDS.get(word)


def session_id_for(number: str) -> str:
    """The agent session key for a sender: one isolated conversation per phone number."""
    return f"sms:{number}"
