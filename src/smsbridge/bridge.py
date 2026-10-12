"""One inbound text, end to end: ask the agent, compose the reply, send it, watch delivery.

Failure policy - the customer always hears back, and never sees an error:

* A transient agent failure (connection error, HTTP 5xx/429, empty or malformed answer) is
  retried once after a short pause, then answered with a neutral fallback message.
* A timeout is *not* retried: the task is genuinely too long, and a second full wait would
  only double the delay before the same outcome. The sender gets a "taking too long" note.
* An explicit empty reply (or the `NO_REPLY` token) means the agent chose silence: nothing is
  sent. Silence is only ever a decision, never the side effect of a failure.

A timeout ends the wait, not the turn. Cancelling a call to a remote agent only hangs up the
bridge's end: the agent usually keeps working, and if the sender's next text reached it then,
it would run two turns of one conversation at once - the lost update the scheduler exists to
prevent. So a timed-out call stays open, and the turn (its worker and its sender) ends only when
the agent answers, or `agent_turn_limit` seconds after the call started, when the call is
closed. The late answer is dropped: the sender already has the timeout note. A backend whose
turn really does end when its call is cancelled (`cancel_ends_turn = True`, such as the echo
and OpenAI-compatible backends) is cancelled at the timeout instead.
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
    """The agent did not answer in time. `call` is that call, still running, if it was kept open."""

    def __init__(self, call: asyncio.Future[object] | None = None) -> None:
        super().__init__("the agent did not answer in time")
        self.call = call


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
                 agent_timeout: float = 120.0, agent_turn_limit: float = 600.0,
                 retry_backoff: float = 1.0, status_callback_url: str | None = None,
                 timeout_reply: str = DEFAULT_TIMEOUT_REPLY,
                 failure_reply: str = DEFAULT_FAILURE_REPLY,
                 silent_tokens: Iterable[str] = SILENT_TOKENS) -> None:
        self.agent = agent
        self.gateway = gateway
        self.composer = composer
        self.delivery = delivery
        self.agent_timeout = agent_timeout
        self.agent_turn_limit = agent_turn_limit
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
        still_running: asyncio.Future[object] | None = None
        try:
            raw, outcome = await self._ask(request), "replied"
        except AgentTimeout as timeout:
            log.warning("agent timed out after %.0fs for %s", self.agent_timeout, who)
            raw, outcome, still_running = self.timeout_reply, "agent_timeout", timeout.call
        except Exception as err:  # AgentError, or a bug in a custom backend
            log.warning("agent failed for %s: %s", who, err)
            raw, outcome = self.failure_reply, "agent_failed"
        try:
            return await self._respond(message.sender, raw, outcome, started)
        finally:
            if still_running is not None:
                await self._outlast(still_running, who)

    async def _respond(self, to: str, raw: str, outcome: str, started: float) -> HandleResult:
        who = mask_number(to)
        if outcome == "replied" and raw.strip().upper() in self._silent | {""}:
            log.info("agent chose not to reply to %s", who)
            return self._done(HandleResult("no_reply"))
        sent = await self._deliver(to, raw)
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
                reply = await self._call(request)  # AgentTimeout is never retried
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

    async def _call(self, request: AgentRequest) -> object:
        """One call to the agent; raises AgentTimeout once it outlasts `agent_timeout`."""
        call = asyncio.ensure_future(self.agent.reply(request))
        if await _finished(call, self.agent_timeout):
            return call.result()
        if getattr(self.agent, "cancel_ends_turn", False):
            await _cancel(call)
            raise AgentTimeout
        raise AgentTimeout(call)  # kept open: see module docstring

    async def _outlast(self, call: asyncio.Future[object], who: str) -> None:
        """Hold the turn until a timed-out call ends, so the sender's next text cannot reach
        the agent while it is still working on this one."""
        if await _finished(call, max(0.0, self.agent_turn_limit - self.agent_timeout)):
            if not call.cancelled():
                call.exception()  # a late failure changes nothing now; mark it as seen
            log.info("the agent finished a timed-out turn for %s; its late result was dropped",
                     who)
            return
        self._counts["agent_cut_off"] += 1
        log.warning("closed an agent call for %s still running after %.0fs; the agent may not "
                    "have stopped", who, self.agent_turn_limit)
        await _cancel(call)

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
                "agent_cut_off", "segments_sent")
        return {key: self._counts[key] for key in keys}


async def _finished(call: asyncio.Future[object], seconds: float) -> bool:
    """Wait up to `seconds` for `call` to end. If this task is cancelled meanwhile, so is it."""
    try:
        await asyncio.wait({call}, timeout=seconds)
    except asyncio.CancelledError:
        call.cancel()
        raise
    return call.done()


async def _cancel(call: asyncio.Future[object]) -> None:
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)  # let it unwind and close its connection
