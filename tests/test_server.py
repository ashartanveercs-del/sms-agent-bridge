import asyncio

import pytest
from fakes import AUTH_TOKEN, FakeGateway, ScriptedAgent
from twilio.request_validator import RequestValidator

from smsbridge.agents import EchoAgent, OpenAIChatAgent, WebhookAgent
from smsbridge.config import Settings
from smsbridge.server import DELIVERY, GATEWAY, SCHEDULER, build_agent, create_app, settle
from smsbridge.twilio_gateway import DryRunGateway

BASE_URL = "https://sms.example.com"
SENDER = "+15550000101"


def make_settings(**overrides) -> Settings:
    values = dict(twilio_auth_token=AUTH_TOKEN, public_base_url=BASE_URL, agent_backend="echo",
                  twilio_account_sid="ACtest", twilio_from_number="+15550000000",
                  status_recheck_seconds=0, rate_limit_messages=0)
    values.update(overrides)
    return Settings(**values)


def signed(path: str, params: dict, base: str = BASE_URL) -> dict[str, str]:
    return {"X-Twilio-Signature": RequestValidator(AUTH_TOKEN).compute_signature(base + path,
                                                                                 params)}


def sms(body: str = "hello", sender: str = SENDER, sid: str = "SM1", **extra) -> dict:
    return {"MessageSid": sid, "AccountSid": "ACtest", "From": sender, "To": "+15550000000",
            "Body": body, "NumMedia": "0", **extra}


async def post_signed(client, params: dict, path: str = "/twilio/sms"):
    return await client.post(path, data=params, headers=signed(path, params))


async def settled(app) -> None:
    await asyncio.wait_for(settle(app), 5)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def agent() -> ScriptedAgent:
    return ScriptedAgent()


@pytest.fixture
async def client(aiohttp_client, gateway, agent):
    return await aiohttp_client(create_app(make_settings(), gateway=gateway, agent=agent))


async def test_health_check_reports_queue_and_delivery_state(client):
    response = await client.get("/healthz")
    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert {"queue", "replies", "delivery"} <= body.keys()


async def test_unsigned_and_forged_webhooks_are_rejected(client, agent):
    params = sms()
    assert (await client.post("/twilio/sms", data=params)).status == 403
    bad = {"X-Twilio-Signature": "bm90IGEgcmVhbCBzaWduYXR1cmU="}
    assert (await client.post("/twilio/sms", data=params, headers=bad)).status == 403
    # signed for the server's internal address rather than the public URL Twilio calls
    internal = signed("/twilio/sms", params, base="http://127.0.0.1:8080")
    assert (await client.post("/twilio/sms", data=params, headers=internal)).status == 403
    await settled(client.app)
    assert agent.requests == []


async def test_a_message_is_acknowledged_at_once_and_answered_in_the_background(
        aiohttp_client, gateway):
    agent = ScriptedAgent("Your invoice is ready.", delay=0.2)
    client = await aiohttp_client(create_app(make_settings(), gateway=gateway, agent=agent))

    response = await post_signed(client, sms())

    assert response.status == 200
    assert response.content_type == "text/xml"
    assert "<Response/>" in await response.text()
    assert gateway.sent == []  # Twilio got its answer before the agent finished
    await settled(client.app)
    assert gateway.bodies_to(SENDER) == ["Your invoice is ready."]


async def test_the_signature_covers_the_query_string_of_the_public_url(client, gateway):
    params = sms()
    response = await post_signed(client, params, path="/twilio/sms?source=main")
    assert response.status == 200
    await settled(client.app)
    assert len(gateway.sent) == 1


async def test_a_twilio_retry_of_the_same_message_is_processed_once(client, agent, gateway):
    params = sms(sid="SMretry")
    first = await post_signed(client, params)
    second = await client.post("/twilio/sms", data=params,
                               headers={**signed("/twilio/sms", params),
                                        "I-Twilio-Idempotency-Token": "retry-1"})
    assert (first.status, second.status) == (200, 200)
    await settled(client.app)
    assert len(agent.requests) == 1
    assert len(gateway.sent) == 1


async def test_a_failed_hand_off_releases_the_sid_so_the_retry_is_not_lost(client, agent):
    scheduler = client.app[SCHEDULER]
    real_submit = scheduler.submit

    def broken_once(key, item):
        scheduler.submit = real_submit
        raise RuntimeError("queue unavailable")

    scheduler.submit = broken_once
    params = sms(sid="SMflaky")
    assert (await post_signed(client, params)).status == 503   # Twilio retries / falls back
    assert (await post_signed(client, params)).status == 200
    await settled(client.app)
    assert len(agent.requests) == 1


@pytest.mark.parametrize("params", [
    sms(body="STOP"),
    sms(body=" unsubscribe "),
    sms(body="Assistance please", OptOutType="HELP"),
], ids=["stop", "unsubscribe", "advanced-opt-out"])
async def test_compliance_keywords_never_reach_the_agent(client, agent, gateway, params):
    assert (await post_signed(client, params)).status == 200
    await settled(client.app)
    assert agent.requests == []
    assert gateway.sent == []


async def test_senders_outside_the_allowlist_are_ignored(aiohttp_client, gateway, agent):
    settings = make_settings(allowed_senders=frozenset({"+15550000102"}))
    client = await aiohttp_client(create_app(settings, gateway=gateway, agent=agent))
    await post_signed(client, sms(sender="+15550000101", sid="SMa"))
    await post_signed(client, sms(sender="+15550000102", sid="SMb"))
    await settled(client.app)
    assert [r.sender for r in agent.requests] == ["+15550000102"]


async def test_messages_without_a_usable_sender_are_ignored(client, agent):
    assert (await post_signed(client, sms(sender="unknown"))).status == 200
    await settled(client.app)
    assert agent.requests == []


async def test_a_rate_limited_sender_gets_one_notice_and_no_extra_agent_turns(
        aiohttp_client, gateway, agent):
    settings = make_settings(rate_limit_messages=1, busy_reply="Slow down a little, please.")
    client = await aiohttp_client(create_app(settings, gateway=gateway, agent=agent))
    for n in range(4):
        assert (await post_signed(client, sms(body=f"msg {n}", sid=f"SM{n}"))).status == 200
    await settled(client.app)
    assert len(agent.requests) == 1
    assert sorted(gateway.bodies_to(SENDER)) == ["Slow down a little, please.", "echo: msg 0"]


async def test_a_full_backlog_gets_a_single_busy_notice(aiohttp_client, gateway):
    gate = asyncio.Event()
    agent = ScriptedAgent(gate=gate)
    settings = make_settings(max_queued_per_sender=1, busy_reply="One moment, please.")
    client = await aiohttp_client(create_app(settings, gateway=gateway, agent=agent))

    await post_signed(client, sms(body="first", sid="SM1"))
    await asyncio.sleep(0.02)                      # "first" is now in flight
    for n in range(2, 5):                          # one waits, the rest overflow
        await post_signed(client, sms(body=f"msg {n}", sid=f"SM{n}"))
    gate.set()
    await settled(client.app)

    assert [r.text for r in agent.requests] == ["first", "msg 2"]
    assert gateway.bodies_to(SENDER).count("One moment, please.") == 1


async def test_status_callbacks_drive_exactly_one_resend(client, gateway):
    await post_signed(client, sms(body="ping"))
    await settled(client.app)
    [reply] = gateway.sent
    callback = {"MessageSid": reply["sid"], "MessageStatus": "undelivered", "ErrorCode": "30003",
                "AccountSid": "ACtest"}

    first = await post_signed(client, callback, path="/twilio/status")
    duplicate = await post_signed(client, callback, path="/twilio/status")
    await settled(client.app)

    assert (first.status, duplicate.status) == (204, 204)
    assert [m["body"] for m in gateway.sent] == ["echo: ping", "echo: ping"]
    assert client.app[DELIVERY].get(reply["sid"]).outcome == "resent"


async def test_unsigned_status_callbacks_are_rejected(client):
    response = await client.post("/twilio/status",
                                 data={"MessageSid": "SM1", "MessageStatus": "failed"})
    assert response.status == 403


async def test_attachments_are_forwarded_to_the_agent(client, agent):
    params = sms(body="", NumMedia="1", MediaUrl0="https://example.com/media/1",
                 MediaContentType0="image/jpeg")
    await post_signed(client, params)
    await settled(client.app)
    [media] = agent.requests[0].media
    assert (media.url, media.content_type) == ("https://example.com/media/1", "image/jpeg")


async def test_signature_checks_can_be_disabled_for_local_experiments(aiohttp_client, gateway,
                                                                      agent):
    settings = make_settings(validate_signatures=False)
    client = await aiohttp_client(create_app(settings, gateway=gateway, agent=agent))
    assert (await client.post("/twilio/sms", data=sms())).status == 200
    await settled(client.app)
    assert len(agent.requests) == 1


async def test_shutdown_closes_the_agent_backend(aiohttp_client, gateway, agent):
    client = await aiohttp_client(create_app(make_settings(), gateway=gateway, agent=agent))
    await client.close()
    assert agent.closed


def test_dry_run_builds_a_logging_gateway():
    app = create_app(make_settings(dry_run=True, twilio_account_sid=None), agent=EchoAgent())
    assert isinstance(app[GATEWAY], DryRunGateway)


def test_the_agent_backend_follows_the_settings():
    assert isinstance(build_agent(make_settings()), EchoAgent)
    webhook = make_settings(agent_backend="webhook", agent_webhook_url="https://agent.example.com/x")
    assert isinstance(build_agent(webhook), WebhookAgent)
    openai = make_settings(agent_backend="openai", openai_api_key="key", openai_model="model")
    assert isinstance(build_agent(openai), OpenAIChatAgent)
