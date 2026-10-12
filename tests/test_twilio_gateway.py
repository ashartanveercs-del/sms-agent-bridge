import asyncio
import logging

import pytest
from fakes import StubTwilioClient
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from smsbridge.twilio_gateway import (
    NO_RESEND_ERROR_CODES,
    DryRunGateway,
    SendError,
    TwilioGateway,
)

TOKEN = "test-auth-token"
URL = "https://sms.example.com/twilio/sms"
PARAMS = {"From": "+15550000101", "To": "+15550000000", "Body": "hi", "MessageSid": "SM1"}
CALLBACK = "https://sms.example.com/twilio/status"


def gateway(client: StubTwilioClient | None = None, **kwargs) -> TwilioGateway:
    if "messaging_service_sid" not in kwargs:
        kwargs.setdefault("from_number", "+15550000000")
    return TwilioGateway("ACtest", TOKEN, client=client or StubTwilioClient(), **kwargs)


def sign(url: str, params: dict, token: str = TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, params)


def test_a_correctly_signed_request_is_accepted():
    assert gateway().is_valid_request(URL, PARAMS, sign(URL, PARAMS))


def test_tampered_misaddressed_or_unsigned_requests_are_rejected():
    gw, signature = gateway(), sign(URL, PARAMS)
    assert not gw.is_valid_request(URL, {**PARAMS, "Body": "hi!"}, signature)
    assert not gw.is_valid_request("https://other.example.com/twilio/sms", PARAMS, signature)
    assert not gw.is_valid_request(URL, PARAMS, "")


def test_a_signature_made_with_another_auth_token_is_rejected():
    assert not gateway().is_valid_request(URL, PARAMS, sign(URL, PARAMS, token="other-token"))


async def test_send_uses_the_from_number_and_asks_for_status_callbacks():
    client = StubTwilioClient()
    sent = await gateway(client).send("+15550000101", "hello", status_callback=CALLBACK)
    assert sent.sid.startswith("SM") and len(sent.sid) == 34
    assert sent.status == "queued"
    assert client.created == [{"to": "+15550000101", "body": "hello", "from_": "+15550000000",
                               "status_callback": CALLBACK}]


async def test_send_through_a_messaging_service():
    client = StubTwilioClient()
    await gateway(client, messaging_service_sid="MGtest").send("+15550000101", "hello")
    assert client.created == [{"to": "+15550000101", "body": "hello",
                               "messaging_service_sid": "MGtest"}]


async def test_api_errors_carry_the_twilio_error_code():
    error = TwilioRestException(400, "/Messages.json", "unsubscribed recipient", code=21610)
    with pytest.raises(SendError) as err:
        await gateway(StubTwilioClient(fail_with=error)).send("+15550000101", "hi")
    assert (err.value.code, err.value.status) == (21610, 400)


async def test_transport_errors_become_send_errors():
    with pytest.raises(SendError) as err:
        await gateway(StubTwilioClient(fail_with=ConnectionError("down"))).send("+1555", "hi")
    assert err.value.code is None


def test_the_sdk_client_is_built_with_an_http_timeout():
    # the SDK's default is no timeout at all: one dead connection would hang a thread forever
    gw = TwilioGateway("ACtest", TOKEN, from_number="+15550000000", api_timeout=7)
    assert gw._client.http_client.timeout == 7


async def test_a_send_that_hangs_gives_up_at_the_deadline():
    client = StubTwilioClient()
    client.hang.add("+15550000101")
    try:
        with pytest.raises(SendError, match="did not answer"):
            await asyncio.wait_for(
                gateway(client, api_timeout=0.1).send("+15550000101", "hi"), 2)
    finally:
        client.release.set()


async def test_a_status_lookup_that_hangs_gives_up_at_the_deadline():
    client = StubTwilioClient()
    client.hang.add("SM9")
    try:
        with pytest.raises(SendError, match="did not answer"):
            await asyncio.wait_for(gateway(client, api_timeout=0.1).fetch_status("SM9"), 2)
    finally:
        client.release.set()


async def test_fetch_status_reads_status_and_error_code():
    client = StubTwilioClient()
    client.statuses["SM9"] = ("undelivered", 30003)
    status = await gateway(client).fetch_status("SM9")
    assert (status.sid, status.status, status.error_code) == ("SM9", "undelivered", 30003)


def test_a_sender_is_required():
    with pytest.raises(ValueError):
        TwilioGateway("ACtest", TOKEN, client=StubTwilioClient())


def test_errors_where_a_resend_cannot_help_are_listed():
    # opted out, not a mobile number, landline, carrier content filter, unregistered sender
    assert {21610, 30630, 21614, 30006, 30007, 30034} <= set(NO_RESEND_ERROR_CODES)
    assert 30003 not in NO_RESEND_ERROR_CODES  # handset off: a later attempt may well work


async def test_dry_run_logs_the_message_instead_of_sending_it(caplog):
    gw = DryRunGateway(TOKEN)
    with caplog.at_level(logging.INFO, logger="smsbridge.twilio_gateway"):
        sent = await gw.send("+15550000101", "hello there", status_callback=CALLBACK)
    assert sent.sid.startswith("SM") and len(sent.sid) == 34
    assert "hello there" in caplog.text
    assert "+*******0101" in caplog.text and "+15550000101" not in caplog.text
    assert (await gw.fetch_status(sent.sid)).status == "delivered"
    assert gw.sent == [{"sid": sent.sid, "to": "+15550000101", "body": "hello there"}]
    assert gw.is_valid_request(URL, PARAMS, sign(URL, PARAMS))
