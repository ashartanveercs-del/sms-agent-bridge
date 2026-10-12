"""Delivery verification: Twilio accepting a message is not the phone receiving it.

`messages.create` succeeds as soon as Twilio *queues* the message. Carrier failures - handset
off, content filtered, unregistered sender - surface minutes later as an `undelivered` or
`failed` status. This module watches every outbound message through two independent channels:

* **status callbacks** that Twilio POSTs to `/twilio/status`, and
* **one delayed re-check** of the Message resource, which covers callbacks that were lost,
  never configured, or never sent (Twilio sends no callback for the initial status).

On a delivery failure the message is resent **exactly once**:

* The first final status wins. Reports that do not move a message forward - a duplicate
  callback, an earlier status arriving late, the re-check confirming what a callback already
  said - are ignored, so two observers seeing the same failure cannot trigger two resends.
  The check-and-mark happens synchronously, with no `await` in between.
* A resend is tracked like any message but never resent itself.
* Errors where a resend cannot help (an opt-out, a landline, carrier filtering, an
  unregistered sender) are not resent at all.
* A message still in flight at re-check time is marked unconfirmed and left alone: resending
  something that is merely slow would deliver it twice.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from .outbound import MAX_BODY_UNITS, utf16_units
from .phone import mask_number
from .twilio_gateway import NO_RESEND_ERROR_CODES, MessagingGateway, SendError

log = logging.getLogger(__name__)

# Position in a message's life. A report is applied only if it moves the message forward.
_RANK = {"accepted": 0, "scheduled": 0, "queued": 1, "sending": 2, "sent": 3,
         "delivered": 4, "undelivered": 4, "failed": 4, "canceled": 4, "read": 5}
DELIVERED = frozenset({"delivered", "read"})
FAILED = frozenset({"undelivered", "failed"})
FINAL = DELIVERED | FAILED | {"canceled"}


@dataclass
class Delivery:
    sid: str
    to: str
    body: str
    attempt: int = 1  # 2 = the one resend
    status: str = "queued"
    error_code: int | None = None
    original_sid: str | None = None
    resend_sid: str | None = None
    # pending | delivered | failed | resending | resent | unconfirmed | canceled
    outcome: str = "pending"


class DeliveryMonitor:
    def __init__(self, gateway: MessagingGateway, *, status_callback_url: str | None = None,
                 recheck_delay: float = 30.0, resend: bool = True, resend_suffix: str = "",
                 max_part_units: int = MAX_BODY_UNITS, max_tracked: int = 10_000) -> None:
        self.gateway = gateway
        self.status_callback_url = status_callback_url
        self.recheck_delay = recheck_delay
        self.resend = resend
        self.resend_suffix = resend_suffix
        self.max_part_units = max_part_units
        self.max_tracked = max_tracked
        self._records: OrderedDict[str, Delivery] = OrderedDict()
        self._early: OrderedDict[str, tuple[str, int | None]] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._counts: Counter[str] = Counter()

    # -- intake ---------------------------------------------------------------------------

    def track(self, sid: str, to: str, body: str, *, status: str = "queued", attempt: int = 1,
              original_sid: str | None = None) -> Delivery:
        record = Delivery(sid, to, body, attempt, status.lower(), original_sid=original_sid)
        self._records[sid] = record
        while len(self._records) > self.max_tracked:
            self._records.popitem(last=False)
        early = self._early.pop(sid, None)
        if early is not None:  # the callback beat our send() call back from Twilio
            self._apply(record, *early, source="callback")
        if self.recheck_delay > 0 and record.status not in FINAL:
            self._spawn(self._recheck(record))
        return record

    def observe(self, sid: str, status: str, error_code: int | None = None, *,
                source: str = "callback") -> None:
        """Apply a status report. Synchronous on purpose: see the module docstring."""
        record = self._records.get(sid)
        if record is None:
            self._early[sid] = (status.lower(), error_code)
            while len(self._early) > 1000:
                self._early.popitem(last=False)
            return
        self._apply(record, status.lower(), error_code, source)

    def _apply(self, record: Delivery, status: str, error_code: int | None,
               source: str) -> None:
        rank = _RANK.get(status)
        if rank is None or rank <= _RANK.get(record.status, -1):
            return  # unknown, duplicate, or stale: the first final status wins
        record.status = status
        if error_code is not None:
            record.error_code = error_code
        if status in DELIVERED:
            self._finish(record, "delivered")
        elif status == "canceled":
            self._finish(record, "canceled")
        elif status in FAILED:
            self._on_failure(record, source)

    # -- failure handling -----------------------------------------------------------------

    def _on_failure(self, record: Delivery, source: str) -> None:
        code = record.error_code
        reason = NO_RESEND_ERROR_CODES.get(code) if code is not None else None
        log.warning("delivery %s: %s to %s, error %s (seen via %s)", record.status,
                    record.sid, mask_number(record.to), code, source)
        if record.attempt > 1:
            log.warning("the resend of %s failed too; giving up", record.original_sid)
            self._finish(record, "failed")
        elif not self.resend:
            self._finish(record, "failed")
        elif reason is not None:
            log.info("not resending %s: %s", record.sid, reason)
            self._finish(record, "failed")
        else:
            record.outcome = "resending"
            self._spawn(self._resend(record))

    async def _resend(self, record: Delivery) -> None:
        body = record.body
        if self.resend_suffix and utf16_units(body + self.resend_suffix) <= self.max_part_units:
            body += self.resend_suffix
        try:
            sent = await self.gateway.send(record.to, body,
                                           status_callback=self.status_callback_url)
        except SendError as err:
            log.warning("resend of %s refused: %s", record.sid, err)
            self._finish(record, "failed")
            return
        record.resend_sid = sent.sid
        self._finish(record, "resent")
        log.info("resent %s as %s", record.sid, sent.sid)
        self.track(sent.sid, record.to, body, status=sent.status, attempt=2,
                   original_sid=record.sid)

    async def _recheck(self, record: Delivery) -> None:
        await asyncio.sleep(self.recheck_delay)
        if record.status in FINAL:
            return  # a callback already told us
        try:
            report = await self.gateway.fetch_status(record.sid)
        except SendError as err:
            log.warning("status re-check of %s failed: %s", record.sid, err)
            self._finish(record, "unconfirmed")
            return
        self._apply(record, report.status.lower(), report.error_code, source="re-check")
        if record.status not in FINAL:
            log.info("%s still %s after %.0fs; leaving it alone", record.sid, record.status,
                     self.recheck_delay)
            self._finish(record, "unconfirmed")

    # -- bookkeeping ----------------------------------------------------------------------

    def _finish(self, record: Delivery, outcome: str) -> None:
        record.outcome = outcome
        self._counts[outcome] += 1

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def busy(self) -> bool:
        """True while a re-check or resend is still pending."""
        return bool(self._tasks)

    async def flush(self) -> None:
        """Wait for every pending re-check and resend (including ones they start)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def get(self, sid: str) -> Delivery | None:
        return self._records.get(sid)

    def stats(self) -> dict[str, int]:
        return {"tracked": len(self._records), "delivered": self._counts["delivered"],
                "failed": self._counts["failed"], "resent": self._counts["resent"],
                "unconfirmed": self._counts["unconfirmed"]}
