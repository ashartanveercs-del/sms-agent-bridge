"""One inbound text, end to end: ask the agent, compose the reply, send it, watch delivery.

Failure policy - the customer always hears back, and never sees an error:

* A transient agent failure (connection error, HTTP 5xx/429, empty or malformed answer) is
  retried once after a short pause, then answered with a neutral fallback message.
* A timeout is *not* retried: the task is genuinely too long, and a second full wait would
  only double the delay before the same outcome. The sender gets a "taking too long" note.
* An explicit empty reply (or the `NO_REPLY` token) means the agent chose silence: nothing is
  sent. Silence is only ever a decision, never the side effect of a failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from .agents import AgentBackend, AgentError, AgentRequest, Media
from .delivery import DeliveryMonitor
from .outbound import OutboundComposer
from .phone import mask_number, session_id_for
from .twilio_gateway import MessagingGateway, SendError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_REPLY = ("Sorry - that is taking longer than I can manage in one reply. "
                         "Please try again in a little while.")
DEFAULT_FAILURE_REPLY = ("Sorry - I hit a problem on my side and couldn't answer that. "
                         "Please send it again.")
SILENT_TOKENS = ("NO_REPLY",)


class AgentTimeout(Exception):
    pass


@dataclass(frozen=True)
class InboundMessage:
    sid: str
    sender: str
    body: str
    media: tuple[Media, ...] = ()


@dataclass(frozen=True)
class HandleResult:
    outcome: str  # replied | no_reply | agent_timeout | agent_failed | send_failed
    parts: int = 0
    segments: int = 0


class Bridge:
    def __init__(self, *, agent: AgentBackend, gateway: MessagingGateway,
                 composer: OutboundComposer, delivery: DeliveryMonitor,
                 agent_timeout: float = 120.0, retry_backoff: float = 1.0,
                 status_callback_url: str | None = None,
                 timeout_reply: str = DEFAULT_TIMEOUT_REPLY,
                 failure_reply: str = DEFAULT_FAILURE_REPLY,
                 silent_tokens: Iterable[str] = SILENT_TOKENS) -> None:
        self.agent = agent
        self.gateway = gateway
        self.composer = composer
        self.delivery = delivery
        self.agent_timeout = agent_timeout
        self.retry_backoff = retry_backoff
        self.status_callback_url = status_callback_url
        self.timeout_reply = timeout_reply
        self.failure_reply = failure_reply
        self._silent = {token.strip().upper() for token in silent_tokens}
        self._counts: Counter[str] = Counter()

    async def handle(self, message: InboundMessage) -> HandleResult:
        who = mask_number(message.sender)
        request = AgentRequest(session_id=session_id_for(message.sender), sender=message.sender,
                               text=message.body, message_sid=message.sid, media=message.media)
        started = time.monotonic()
        try:
            raw, outcome = await self._ask(request), "replied"
        except AgentTimeout:
            log.warning("agent timed out after %.0fs for %s", self.agent_timeout, who)
            raw, outcome = self.timeout_reply, "agent_timeout"
        except Exception as err:  # AgentError, or a bug in a custom backend
            log.warning("agent failed for %s: %s", who, err)
            raw, outcome = self.failure_reply, "agent_failed"

        if outcome == "replied" and raw.strip().upper() in self._silent | {""}:
            log.info("agent chose not to reply to %s", who)
            return self._done(HandleResult("no_reply"))
        sent = await self._deliver(message.sender, raw)
        if sent is None:
            return self._done(HandleResult("send_failed"))
        parts, segments = sent
        if not parts:
            return self._done(HandleResult("no_reply"))
        log.info("%s to %s: %d part(s), %d segment(s), %.1fs", outcome, who, parts, segments,
                 time.monotonic() - started)
        return self._done(HandleResult(outcome, parts, segments))

    async def notify(self, to: str, text: str) -> None:
        """Send a bridge-generated notice (busy, rate limited) through the same outbound path."""
        try:
            await self._deliver(to, text)
        except Exception:
            log.exception("could not send a notice to %s", mask_number(to))

    async def _ask(self, request: AgentRequest) -> str:
        for attempt in (1, 2):
            try:
                reply = await asyncio.wait_for(self.agent.reply(request), self.agent_timeout)
            except TimeoutError as err:
                raise AgentTimeout from err  # never retried: see module docstring
            except AgentError as err:
                if attempt == 2 or not err.retryable:
                    raise
                log.info("agent error (%s); retrying once", err)
                await asyncio.sleep(self.retry_backoff)
                continue
            if not isinstance(reply, str):
                raise AgentError(f"agent returned {type(reply).__name__}, not text",
                                 retryable=False)
            return reply
        raise AssertionError("unreachable")

    async def _deliver(self, to: str, raw: str) -> tuple[int, int] | None:
        plan = self.composer.compose(raw)
        if plan.removed_lines or plan.redactions:
            log.warning("sanitised a reply to %s: dropped %d line(s), masked %d value(s)",
                        mask_number(to), plan.removed_lines, plan.redactions)
        for number, part in enumerate(plan.parts, start=1):
            try:
                sent = await self.gateway.send(to, part.body,
                                               status_callback=self.status_callback_url)
            except SendError as err:
                log.error("sending part %d/%d to %s failed: %s", number, len(plan.parts),
                          mask_number(to), err)
                return None
            except Exception:
                log.exception("unexpected error sending to %s", mask_number(to))
                return None
            self.delivery.track(sent.sid, to, part.body, status=sent.status)
            self._counts["segments_sent"] += part.info.segments
        return len(plan.parts), plan.segments

    def _done(self, result: HandleResult) -> HandleResult:
        self._counts[result.outcome] += 1
        return result

    def stats(self) -> dict[str, int]:
        keys = ("replied", "no_reply", "agent_timeout", "agent_failed", "send_failed",
                "segments_sent")
        return {key: self._counts[key] for key in keys}
