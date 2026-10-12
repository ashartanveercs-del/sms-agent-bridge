"""Idempotent intake: each Twilio MessageSid is handled at most once.

The same inbound message can reach the webhook more than once: Twilio retries a webhook on
connection failures by default (and on timeouts or 5xx if the URL asks it to), and a
Fallback URL receives the message again when the primary URL fails. Every copy carries the
same MessageSid, so the SID is the idempotency key.

Two details matter:

* `claim()` is check-and-record in one step, so two near-simultaneous copies cannot both
  pass. If the hand-off *after* a claim fails, the caller must `release()` the SID and answer
  with an error, so Twilio's retry (or the fallback URL) can still deliver the message
  instead of it being silently dropped as a "duplicate".
* Memory is bounded by evicting the oldest SID one at a time. Clearing the whole set when it
  fills up would open a window in which every retry in flight is processed a second time.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class DedupeCache:
    def __init__(self, *, ttl_seconds: float = 24 * 3600, max_entries: int = 50_000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._expires: OrderedDict[str, float] = OrderedDict()  # insertion = expiry order

    def claim(self, key: str) -> bool:
        """Record `key`. True if it is new, False if it was already seen within the TTL."""
        if not key:
            return True  # nothing to deduplicate on
        now = self._clock()
        self._expire(now)
        if key in self._expires:
            return False
        self._expires[key] = now + self._ttl
        while len(self._expires) > self._max:
            self._expires.popitem(last=False)
        return True

    def release(self, key: str) -> None:
        """Forget `key`, so a retry of the same message is accepted again."""
        self._expires.pop(key, None)

    def _expire(self, now: float) -> None:
        while self._expires:
            oldest, expires_at = next(iter(self._expires.items()))
            if expires_at > now:
                return
            del self._expires[oldest]

    def __contains__(self, key: object) -> bool:
        self._expire(self._clock())
        return key in self._expires

    def __len__(self) -> int:
        return len(self._expires)
