"""Per-sender admission control.

A sliding-window limit bounds how many agent turns one number can trigger - protection
against abuse, runaway model cost, and two automated systems texting each other in a loop.
Over the limit, the message is acknowledged to Twilio (so it is not retried) but no agent
turn runs; the sender gets at most one short notice per window instead of silence.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    notify: bool = False  # True for the first rejection in a window: tell the sender once


class Cooldown:
    """`ready(key)` is True at most once per `period_seconds` for each key."""

    def __init__(self, *, period_seconds: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10_000) -> None:
        self._period = period_seconds
        self._clock = clock
        self._max = max_keys
        self._last: OrderedDict[str, float] = OrderedDict()

    def ready(self, key: str) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._period:
            return False
        self._last[key] = now
        self._last.move_to_end(key)
        while len(self._last) > self._max:
            self._last.popitem(last=False)
        return True


class SlidingWindowLimiter:
    def __init__(self, *, limit: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic, max_senders: int = 10_000) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._max = max_senders
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._notices = Cooldown(period_seconds=window_seconds, clock=clock,
                                 max_keys=max_senders)

    def check(self, key: str) -> RateDecision:
        if self.limit <= 0:
            return RateDecision(True)
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        self._hits.move_to_end(key)
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            # rejected messages are not recorded, so they never extend the block
            return RateDecision(False, notify=self._notices.ready(key))
        hits.append(now)
        while len(self._hits) > self._max:
            self._hits.popitem(last=False)
        return RateDecision(True)

    def tracked_senders(self) -> int:
        return len(self._hits)
