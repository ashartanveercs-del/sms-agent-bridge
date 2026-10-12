from fakes import FakeGateway, ScriptedAgent

from smsbridge.agents import AgentError, Media
from smsbridge.bridge import DEFAULT_FAILURE_REPLY, Bridge, InboundMessage
from smsbridge.delivery import DeliveryMonitor
from smsbridge.outbound import OutboundComposer
from smsbridge.twilio_gateway import SendError

SENDER = "+15550000101"
CALLBACK = "https://sms.example.com/twilio/status"


def make_bridge(agent, gateway: FakeGateway | None = None, **kwargs):
    gateway = gateway or FakeGateway()
    delivery = DeliveryMonitor(gateway, status_callback_url=CALLBACK, recheck_delay=0)
    kwargs.setdefault("agent_timeout", 1.0)
    bridge = Bridge(agent=agent, gateway=gateway, composer=OutboundComposer(), delivery=delivery,
                    retry_backoff=0, status_callback_url=CALLBACK, **kwargs)
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
