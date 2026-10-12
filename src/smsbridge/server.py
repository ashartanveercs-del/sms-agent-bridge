"""HTTP entry points.

POST /twilio/sms     inbound message webhook (signature-checked) -> queued agent turn
POST /twilio/status  outbound status callback (signature-checked) -> delivery monitor
GET  /healthz        liveness plus queue, reply and delivery counters

The inbound handler never waits for the agent. Twilio gives a webhook 15 seconds and agent
turns routinely take longer, so the message is validated, de-duplicated, admitted and queued,
and Twilio gets an empty TwiML response within milliseconds. The reply is sent later through
the REST API by a worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine
from typing import Any

from aiohttp import web
from multidict import MultiDict

from .agents import AgentBackend, EchoAgent, Media, OpenAIChatAgent, WebhookAgent
from .bridge import Bridge, InboundMessage
from .config import Settings
from .dedupe import DedupeCache
from .delivery import DeliveryMonitor
from .outbound import OutboundComposer
from .phone import compliance_keyword, mask_number, normalise_number
from .ratelimit import Cooldown, SlidingWindowLimiter
from .sanitize import Sanitizer
from .scheduler import FairScheduler, QueueFull
from .twilio_gateway import DryRunGateway, MessagingGateway, TwilioGateway, parse_error_code

log = logging.getLogger(__name__)

SCHEDULER = web.AppKey("scheduler", FairScheduler)
DELIVERY = web.AppKey("delivery", DeliveryMonitor)
BRIDGE = web.AppKey("bridge", Bridge)
GATEWAY: web.AppKey[Any] = web.AppKey("gateway")
NOTICES: web.AppKey[set[asyncio.Task[None]]] = web.AppKey("notices")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
MAX_MEDIA = 10


def build_agent(settings: Settings) -> AgentBackend:
    if settings.agent_backend == "webhook":
        return WebhookAgent(settings.agent_webhook_url or "", token=settings.agent_webhook_token)
    if settings.agent_backend == "openai":
        return OpenAIChatAgent(base_url=settings.openai_base_url,
                               api_key=settings.openai_api_key or "",
                               model=settings.openai_model or "",
                               system_prompt=settings.system_prompt,
                               history_turns=settings.history_turns)
    return EchoAgent()


def build_gateway(settings: Settings) -> MessagingGateway:
    if settings.dry_run:
        return DryRunGateway(settings.twilio_auth_token)
    return TwilioGateway(settings.twilio_account_sid or "", settings.twilio_auth_token,
                         from_number=settings.twilio_from_number,
                         messaging_service_sid=settings.twilio_messaging_service_sid)


def _ack() -> web.Response:
    return web.Response(text=EMPTY_TWIML, content_type="text/xml")


def _media(form: MultiDict[str]) -> tuple[Media, ...]:
    try:
        count = min(int(form.get("NumMedia") or 0), MAX_MEDIA)
    except ValueError:
        count = 0
    return tuple(Media(form[f"MediaUrl{n}"], form.get(f"MediaContentType{n}", ""))
                 for n in range(count) if form.get(f"MediaUrl{n}"))


def create_app(settings: Settings, *, gateway: MessagingGateway | None = None,
               agent: AgentBackend | None = None) -> web.Application:
    gateway = gateway if gateway is not None else build_gateway(settings)
    agent = agent if agent is not None else build_agent(settings)
    delivery = DeliveryMonitor(
        gateway, status_callback_url=settings.status_callback_url,
        recheck_delay=settings.status_recheck_seconds, resend=settings.resend_on_failure,
        resend_suffix=settings.resend_suffix)
    composer = OutboundComposer(sanitizer=Sanitizer(fallback=settings.failure_reply),
                                gsm_mode=settings.gsm_mode, max_parts=settings.max_reply_parts)
    bridge = Bridge(agent=agent, gateway=gateway, composer=composer, delivery=delivery,
                    agent_timeout=settings.agent_timeout_seconds,
                    status_callback_url=settings.status_callback_url,
                    timeout_reply=settings.timeout_reply, failure_reply=settings.failure_reply)
    scheduler: FairScheduler[InboundMessage] = FairScheduler(
        bridge.handle, workers=settings.workers,
        max_pending_per_sender=settings.max_queued_per_sender,
        max_pending_total=settings.max_queued_total)
    dedupe = DedupeCache()
    limiter = SlidingWindowLimiter(limit=settings.rate_limit_messages,
                                   window_seconds=settings.rate_limit_window_seconds)
    busy_notices = Cooldown(period_seconds=settings.rate_limit_window_seconds)
    notices: set[asyncio.Task[None]] = set()

    def send_notice(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        notices.add(task)
        task.add_done_callback(notices.discard)

    async def verified_form(request: web.Request) -> MultiDict[str] | None:
        posted = await request.post()
        form: MultiDict[str] = MultiDict(
            (key, value) for key, value in posted.items() if isinstance(value, str))
        if not settings.validate_signatures:
            return form
        # Twilio signs the URL *it* called; behind a proxy or tunnel that is the public URL,
        # not whatever host and scheme this process sees.
        url = settings.public_base_url + request.path_qs
        if gateway.is_valid_request(url, form, request.headers.get("X-Twilio-Signature", "")):
            return form
        log.warning("rejected a request to %s: missing or invalid Twilio signature",
                    request.path)
        return None

    async def inbound(request: web.Request) -> web.Response:
        form = await verified_form(request)
        if form is None:
            return web.Response(status=403, text="invalid signature")
        sender = normalise_number(form.get("From", ""))
        sid = form.get("MessageSid") or form.get("SmsMessageSid") or ""
        who = mask_number(sender)
        if not sender:
            log.info("ignored %s: no usable sender number", sid or "a message")
            return _ack()
        if settings.allowed_senders and sender not in settings.allowed_senders:
            log.info("ignored a message from %s: not in ALLOWED_SENDERS", who)
            return _ack()
        keyword = form.get("OptOutType") or compliance_keyword(form.get("Body", ""))
        if keyword:
            log.info("%s sent a compliance keyword (%s); Twilio answers those, no agent turn",
                     who, keyword.lower())
            return _ack()
        if not dedupe.claim(sid):
            log.info("ignored a repeat delivery of %s (retry token %s)", sid,
                     request.headers.get("I-Twilio-Idempotency-Token", "-"))
            return _ack()
        decision = limiter.check(sender)
        if not decision.allowed:
            log.warning("rate limit: dropped a message from %s", who)
            if decision.notify:
                send_notice(bridge.notify(sender, settings.busy_reply))
            return _ack()
        message = InboundMessage(sid=sid, sender=sender, body=form.get("Body", ""),
                                 media=_media(form))
        try:
            backlog = scheduler.submit(sender, message)
        except QueueFull as full:
            log.warning("backlog full (%s): dropped a message from %s", full.scope, who)
            if busy_notices.ready(sender):
                send_notice(bridge.notify(sender, settings.busy_reply))
            return _ack()
        except Exception:
            dedupe.release(sid)  # let Twilio's retry or fallback URL deliver it again
            log.exception("could not queue a message from %s", who)
            return web.Response(status=503, text="temporarily unavailable")
        log.info("queued %s from %s (backlog %d)", sid, who, backlog)
        return _ack()

    async def status(request: web.Request) -> web.Response:
        form = await verified_form(request)
        if form is None:
            return web.Response(status=403, text="invalid signature")
        sid = form.get("MessageSid") or form.get("SmsSid") or ""
        state = form.get("MessageStatus") or form.get("SmsStatus") or ""
        if sid and state:
            delivery.observe(sid, state, parse_error_code(form.get("ErrorCode")))
        return web.Response(status=204)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "queue": scheduler.stats(),
                                  "replies": bridge.stats(), "delivery": delivery.stats()})

    async def lifecycle(app: web.Application) -> AsyncIterator[None]:
        scheduler.start()
        yield
        await scheduler.close(grace_seconds=10)
        for task in list(notices):
            task.cancel()
        await asyncio.gather(*notices, return_exceptions=True)
        await delivery.close()
        await agent.close()

    app = web.Application()
    app[SCHEDULER] = scheduler
    app[DELIVERY] = delivery
    app[BRIDGE] = bridge
    app[GATEWAY] = gateway
    app[NOTICES] = notices
    app.cleanup_ctx.append(lifecycle)
    app.router.add_post("/twilio/sms", inbound)
    app.router.add_post("/twilio/status", status)
    app.router.add_get("/healthz", health)
    return app


async def settle(app: web.Application) -> None:
    """Wait until queued turns, notices and delivery follow-ups have all finished.

    Unbounded: wrap it in `asyncio.timeout(...)` when a deadline matters.
    """
    while True:
        await app[SCHEDULER].drain()
        await asyncio.gather(*list(app[NOTICES]), return_exceptions=True)
        await app[DELIVERY].flush()
        stats = app[SCHEDULER].stats()
        if not (stats["pending"] or stats["in_flight"] or app[NOTICES] or app[DELIVERY].busy):
            return
