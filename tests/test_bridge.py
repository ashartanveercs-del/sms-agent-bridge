import asyncio

from fakes import FakeGateway, ScriptedAgent

from smsbridge.agents import AgentError, Media
from smsbridge.bridge import DEFAULT_FAILURE_REPLY, DEFAULT_TIMEOUT_REPLY, Bridge, InboundMessage
from smsbridge.delivery import DeliveryMonitor
from smsbridge.outbound import OutboundComposer
from smsbridge.twilio_gateway import SendError

SENDER = "+15550000101"
CALLBACK = "https://sms.example.com/twilio/status"


def make_bridge(agent, gateway: FakeGateway | None = None, *, gsm_mode: str = "lookalikes",
                **kwargs):
    gateway = gateway or FakeGateway()
    delivery = DeliveryMonitor(gateway, status_callback_url=CALLBACK, recheck_delay=0)
    kwargs.setdefault("agent_timeout", 1.0)
    bridge = Bridge(agent=agent, gateway=gateway, composer=OutboundComposer(gsm_mode=gsm_mode),
                    delivery=delivery, retry_backoff=0, status_callback_url=CALLBACK, **kwargs)
    return bridge, gateway, delivery


def inbound(body: str = "Is invoice 42 paid?", media=()) -> InboundMessage:
    return InboundMessage(sid="SM1", sender=SENDER, body=body, media=tuple(media))


async def test_a_reply_is_composed_sent_and_tracked():
    agent = ScriptedAgent("Yes \u2014 invoice **42** is paid.")
    bridge, gw, delivery = make_bridge(agent)

    result = await bridge.handle(inbound())

    assert gw.bodies_to(SENDER) == ["Yes - invoice 42 is paid."]
    assert gw.sent[0]["status_callback"] == CALLBACK
    assert delivery.get(gw.sent[0]["sid"]) is not None
    assert (result.outcome, result.parts, result.segments) == ("replied", 1, 1)
    request = agent.requests[0]
    assert (request.session_id, request.sender, request.text, request.message_sid) == \
        ("sms:+15550000101", SENDER, "Is invoice 42 paid?", "SM1")


async def test_an_explicit_empty_reply_sends_nothing():
    for silent in ("", "NO_REPLY", "  no_reply  "):
        bridge, gw, _ = make_bridge(ScriptedAgent(silent))
        result = await bridge.handle(inbound())
        assert gw.sent == []
        assert result.outcome == "no_reply"


async def test_a_transient_agent_failure_is_retried_once():
    agent = ScriptedAgent(AgentError("upstream 502"), "Here you go.")
    bridge, gw, _ = make_bridge(agent)
    result = await bridge.handle(inbound())
    assert len(agent.requests) == 2
    assert gw.bodies_to(SENDER) == ["Here you go."]
    assert result.outcome == "replied"


async def test_two_failures_send_the_fallback_and_never_the_error():
    agent = ScriptedAgent(AgentError("crashed in /srv/agent/run.py"), AgentError("again"))
    bridge, gw, _ = make_bridge(agent, failure_reply="Sorry, I hit a snag. Please resend.")
    result = await bridge.handle(inbound())
    assert gw.bodies_to(SENDER) == ["Sorry, I hit a snag. Please resend."]
    assert result.outcome == "agent_failed"


async def test_a_non_retryable_failure_is_not_retried():
    agent = ScriptedAgent(AgentError("bad credentials", retryable=False))
    bridge, gw, _ = make_bridge(agent)
    await bridge.handle(inbound())
    assert len(agent.requests) == 1
    assert gw.bodies_to(SENDER) == [DEFAULT_FAILURE_REPLY]


async def test_a_timeout_is_not_retried_and_gets_its_own_reply():
    agent = ScriptedAgent("too late", delay=0.5)
    bridge, gw, _ = make_bridge(agent, agent_timeout=0.05,
                                timeout_reply="That is taking a while - please try again.")
    result = await bridge.handle(inbound())
    assert len(agent.requests) == 1
    assert gw.bodies_to(SENDER) == ["That is taking a while - please try again."]
    assert result.outcome == "agent_timeout"


class SlowAgent:
    """Works on a turn until `finish` is set, whether or not the bridge is still waiting."""

    def __init__(self, *, cancel_ends_turn: bool | None = None) -> None:
        self.finish = asyncio.Event()
        self.cancelled = False
        if cancel_ends_turn is not None:
            self.cancel_ends_turn = cancel_ends_turn

    async def reply(self, request) -> str:
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "a late answer"

    async def close(self) -> None:
        return None


async def test_a_timed_out_call_stays_open_until_the_agent_finishes():
    agent = SlowAgent()
    bridge, gw, _ = make_bridge(agent, agent_timeout=0.05)
    turn = asyncio.create_task(bridge.handle(inbound()))

    await gw.wait_for_sent(1)                    # the sender hears back at the timeout...
    assert gw.bodies_to(SENDER) == [DEFAULT_TIMEOUT_REPLY]
    await asyncio.sleep(0.05)
    assert not turn.done()                       # ...but the turn holds its sender
    agent.finish.set()

    result = await asyncio.wait_for(turn, 1)
    assert result.outcome == "agent_timeout"
    assert not agent.cancelled
    assert gw.bodies_to(SENDER) == [DEFAULT_TIMEOUT_REPLY]  # the late answer is dropped
    assert bridge.stats()["agent_cut_off"] == 0


async def test_a_call_still_running_at_the_turn_limit_is_closed():
    agent = SlowAgent()
    bridge, gw, _ = make_bridge(agent, agent_timeout=0.05, agent_turn_limit=0.2)
    result = await asyncio.wait_for(bridge.handle(inbound()), 1)
    assert result.outcome == "agent_timeout"
    assert agent.cancelled
    assert gw.bodies_to(SENDER) == [DEFAULT_TIMEOUT_REPLY]
    assert bridge.stats()["agent_cut_off"] == 1


async def test_a_backend_whose_turn_ends_on_cancel_is_cancelled_at_the_timeout():
    agent = SlowAgent(cancel_ends_turn=True)
    bridge, gw, _ = make_bridge(agent, agent_timeout=0.05)
    result = await asyncio.wait_for(bridge.handle(inbound()), 0.5)
    assert result.outcome == "agent_timeout"
    assert agent.cancelled
    assert gw.bodies_to(SENDER) == [DEFAULT_TIMEOUT_REPLY]
    assert bridge.stats()["agent_cut_off"] == 0


async def test_a_crashing_custom_backend_still_gets_the_fallback():
    bridge, gw, _ = make_bridge(ScriptedAgent(KeyError("session")))
    result = await bridge.handle(inbound())
    assert gw.bodies_to(SENDER) == [DEFAULT_FAILURE_REPLY]
    assert result.outcome == "agent_failed"


async def test_a_backend_that_returns_something_other_than_text_gets_the_fallback():
    bridge, gw, _ = make_bridge(ScriptedAgent(None))
    result = await bridge.handle(inbound())
    assert gw.bodies_to(SENDER) == [DEFAULT_FAILURE_REPLY]
    assert result.outcome == "agent_failed"


async def test_an_unexpected_gateway_error_is_contained_and_reported():
    gw = FakeGateway()
    gw.send_errors.append(RuntimeError("bug in a custom gateway"))
    bridge, gw, _ = make_bridge(ScriptedAgent("hello"), gateway=gw)
    result = await bridge.handle(inbound())
    assert result.outcome == "send_failed"


async def test_a_failing_notice_is_logged_not_raised():
    gw = FakeGateway()
    gw.send_errors.append(RuntimeError("bug in a custom gateway"))
    bridge, gw, _ = make_bridge(ScriptedAgent(), gateway=gw)
    await bridge.notify(SENDER, "One moment please.")  # must not raise
    assert gw.sent == []


async def test_internal_errors_inside_a_reply_are_stripped_before_sending():
    agent = ScriptedAgent("Done.\nTool calendar failed: HTTP 500 from upstream\nAnything else?")
    bridge, gw, _ = make_bridge(agent)
    await bridge.handle(inbound())
    assert gw.bodies_to(SENDER) == ["Done.\nAnything else?"]


async def test_strict_mode_never_turns_a_real_reply_into_silence_or_punctuation():
    # Russian ("Hello", "Your order 1001 is ready!"), Chinese ("Hello") and a thumbs-up:
    # none of it can be GSM-7 without losing the words, so it goes out as written (UCS-2)
    for reply in ("Привет", "\U0001f44d", "你好",
                  "Ваш заказ 1001 "
                  "готов!"):
        bridge, gw, _ = make_bridge(ScriptedAgent(reply), gsm_mode="strict")
        result = await bridge.handle(inbound())
        assert gw.bodies_to(SENDER) == [reply]
        assert result.outcome == "replied"


async def test_long_replies_go_out_as_ordered_parts():
    agent = ScriptedAgent("\n\n".join(f"Part {n}. " + "word " * 250 for n in range(3)))
    bridge, gw, _ = make_bridge(agent)
    result = await bridge.handle(inbound())
    assert [body.split(".")[0] for body in gw.bodies_to(SENDER)] == ["Part 0", "Part 1", "Part 2"]
    assert result.parts == 3


async def test_a_refused_send_stops_the_remaining_parts():
    gw = FakeGateway()
    gw.send_errors.append(SendError("unsubscribed", code=21610, status=400))
    agent = ScriptedAgent("\n\n".join(f"Part {n}. " + "word " * 250 for n in range(3)))
    bridge, gw, _ = make_bridge(agent, gateway=gw)
    result = await bridge.handle(inbound())
    assert gw.sent == []
    assert result.outcome == "send_failed"


async def test_attachments_are_passed_to_the_agent():
    media = (Media("https://example.com/m/1", "image/jpeg"),)
    agent = ScriptedAgent("Nice receipt.")
    bridge, _, _ = make_bridge(agent)
    await bridge.handle(inbound(body="", media=media))
    assert agent.requests[0].media == media


async def test_notices_use_the_same_outbound_path():
    bridge, gw, delivery = make_bridge(ScriptedAgent())
    await bridge.notify(SENDER, "You\u2019re sending messages faster than I can answer.")
    assert gw.bodies_to(SENDER) == ["You're sending messages faster than I can answer."]
    assert delivery.get(gw.sent[0]["sid"]) is not None


async def test_stats_count_outcomes_and_segments():
    bridge, _, _ = make_bridge(ScriptedAgent("a" * 200, "", AgentError("x", retryable=False)))
    for _ in range(3):
        await bridge.handle(inbound())
    stats = bridge.stats()
    assert (stats["replied"], stats["no_reply"], stats["agent_failed"]) == (1, 1, 1)
    assert stats["segments_sent"] == 3  # 200 GSM-7 chars = 2 segments, fallback = 1
