"""Per-sender FIFO queues, drained fairly by a bounded pool of workers.

Three guarantees, all needed at once:

1. **Per-sender order.** A sender's messages are handled one at a time, in arrival order.
   This is about correctness, not just tidiness: an agent turn reads the conversation
   history and then appends to it. Two turns for the same person running concurrently both
   read the same history and the second one loses the first exchange (a lost update), or
   the replies arrive out of order.
2. **Parallelism across senders.** Different people are served at the same time, up to
   `workers` agent turns in flight - a slow conversation never blocks anyone else.
3. **Fairness.** A worker takes *one* message from a sender, then that sender goes to the
   back of the rotation. Someone who sends twenty texts in a row gets one turn per lap, so a
   chatty sender (or a misbehaving bot texting in a loop) cannot starve everyone else.

Mechanics: each sender has a deque; `_ready` is a FIFO of senders that have queued work and
are not in flight - each sender appears in it at most once. A worker pops a sender, marks it
in flight, handles one message, and re-queues the sender at the back if more is waiting. A
message that arrives while its sender is in flight just waits in the deque; the worker that
finishes the current turn puts the sender back in the rotation.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class QueueFull(Exception):
    """The sender's backlog (scope "sender") or the whole backlog (scope "total") is full."""

    def __init__(self, scope: str) -> None:
        super().__init__(f"queue full ({scope})")
        self.scope = scope


class SchedulerClosed(RuntimeError):
    pass


class FairScheduler(Generic[T]):
    def __init__(self, handler: Callable[[T], Awaitable[object]], *, workers: int = 8,
                 max_pending_per_sender: int = 20, max_pending_total: int = 1000) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if max_pending_per_sender < 1 or max_pending_total < 1:
            raise ValueError("queue limits must be at least 1")
        self._handler = handler
        self.workers = workers
        self.max_pending_per_sender = max_pending_per_sender
        self.max_pending_total = max_pending_total

        self._queues: dict[str, deque[T]] = {}
        self._ready: asyncio.Queue[str] = asyncio.Queue()
        self._in_flight: set[str] = set()
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self.processed = 0
        self.failed = 0
        self.rejected = 0

    def start(self) -> None:
        """Spawn the worker tasks (call from inside the running event loop)."""
        if self._closed:
            raise SchedulerClosed("scheduler is closed")
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._work(), name=f"sms-worker-{n}")
                           for n in range(self.workers)]

    def submit(self, key: str, item: T) -> int:
        """Queue `item` behind `key`'s earlier messages; returns that sender's backlog."""
        if self._closed:
            raise SchedulerClosed("scheduler is shutting down")
        queue = self._queues.get(key)
        if queue is not None and len(queue) >= self.max_pending_per_sender:
            self.rejected += 1
            raise QueueFull("sender")
        if self._pending >= self.max_pending_total:
            self.rejected += 1
            raise QueueFull("total")
        if queue is None:
            queue = self._queues[key] = deque()
        queue.append(item)
        self._pending += 1
        self._idle.clear()
        if len(queue) == 1 and key not in self._in_flight:
            self._ready.put_nowait(key)  # an idle sender joins the back of the rotation
        return len(queue)

    async def _work(self) -> None:
        while True:
            key = await self._ready.get()
            queue = self._queues.get(key)
            if not queue or key in self._in_flight:
                log.error("scheduler invariant broken for a sender; skipping a stale entry")
                continue  # never let a bookkeeping bug kill a worker
            item = queue.popleft()
            self._pending -= 1
            self._in_flight.add(key)
            try:
                await self._handler(item)
                self.processed += 1
            except Exception:
                self.failed += 1
                log.exception("message handler failed; the worker carries on")
            finally:
                self._in_flight.discard(key)
                if queue:
                    self._ready.put_nowait(key)  # more waiting: back of the line
                else:
                    del self._queues[key]
                if not self._pending and not self._in_flight:
                    self._idle.set()

    async def drain(self) -> None:
        """Wait until nothing is queued or in flight (bound it with `asyncio.timeout`)."""
        await self._idle.wait()

    async def close(self, grace_seconds: float = 10.0) -> None:
        """Stop intake, let queued work finish for `grace_seconds`, then cancel the rest."""
        self._closed = True
        if self._tasks:
            try:
                await asyncio.wait_for(self._idle.wait(), grace_seconds)
            except TimeoutError:
                log.warning("shutting down: abandoning %d queued and %d in-flight messages",
                            self._pending, len(self._in_flight))
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def stats(self) -> dict[str, int]:
        return {"workers": self.workers, "pending": self._pending,
                "in_flight": len(self._in_flight), "senders": len(self._queues),
                "processed": self.processed, "failed": self.failed, "rejected": self.rejected}
