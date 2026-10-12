import asyncio

import pytest
from fakes import FakeGateway

from smsbridge.delivery import DeliveryMonitor
from smsbridge.twilio_gateway import SendError

TO = "+15550000101"
CALLBACK = "https://sms.example.com/twilio/status"


def monitor(gateway: FakeGateway, **kwargs) -> DeliveryMonitor:
    kwargs.setdefault("recheck_delay", 0)  # re-check off unless a test turns it on
    return DeliveryMonitor(gateway, status_callback_url=CALLBACK, **kwargs)


async def test_delivered_messages_are_left_alone():
    gw = FakeGateway()
    dm = monitor(gw)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "sent")
    dm.observe("SM1", "delivered")
    await dm.flush()
    assert dm.get("SM1").outcome == "delivered"
    assert gw.sent == []


async def test_an_undelivered_message_is_resent_exactly_once():
    gw = FakeGateway()
    dm = monitor(gw)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "undelivered", 30003)
    await dm.flush()

    assert [(m["to"], m["body"], m["status_callback"]) for m in gw.sent] == \
        [(TO, "hello", CALLBACK)]
    original = dm.get("SM1")
    assert original.outcome == "resent"
    resend = dm.get(original.resend_sid)
    assert (resend.attempt, resend.original_sid) == (2, "SM1")


async def test_duplicate_and_out_of_order_reports_do_not_cause_a_second_resend():
    gw = FakeGateway()
    dm = monitor(gw)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "undelivered", 30003)
    dm.observe("SM1", "undelivered", 30003)   # the same callback delivered twice
    dm.observe("SM1", "failed", 30008)        # a conflicting late report
    dm.observe("SM1", "sent")                 # an earlier status arriving last
    await dm.flush()
    assert len(gw.sent) == 1
    assert dm.get("SM1").status == "undelivered"


async def test_callback_and_recheck_reporting_the_same_failure_resend_once():
    gw = FakeGateway()
    gw.statuses["SM1"] = ("undelivered", 30003)
    gw.hold_fetches = True
    dm = monitor(gw, recheck_delay=0.001)
    dm.track("SM1", TO, "hello")

    await asyncio.wait_for(gw.fetch_started.wait(), 1)   # the re-check is mid-lookup...
    dm.observe("SM1", "undelivered", 30003)             # ...when the callback lands
    gw.release_fetches.set()
    await dm.flush()

    assert len(gw.sent) == 1


async def test_the_recheck_catches_a_failure_whose_callback_never_arrived():
    gw = FakeGateway()
    gw.statuses["SM1"] = ("failed", 30008)
    dm = monitor(gw, recheck_delay=0.001)
    dm.track("SM1", TO, "hello")
    await dm.flush()
    assert gw.fetches[0] == "SM1"
    assert [m["body"] for m in gw.sent] == ["hello"]


async def test_the_recheck_skips_messages_whose_outcome_is_already_known():
    gw = FakeGateway()
    dm = monitor(gw, recheck_delay=0.001)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "delivered")
    await dm.flush()
    assert gw.fetches == []


async def test_a_message_still_in_flight_at_the_recheck_is_unconfirmed_not_resent():
    gw = FakeGateway()
    gw.statuses["SM1"] = ("sent", None)
    dm = monitor(gw, recheck_delay=0.001)
    dm.track("SM1", TO, "hello")
    await dm.flush()
    assert dm.get("SM1").outcome == "unconfirmed"
    assert gw.sent == []


@pytest.mark.parametrize("code", [21610, 30006, 30007, 30034])
async def test_no_resend_when_a_resend_cannot_help(code):
    gw = FakeGateway()
    dm = monitor(gw)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "undelivered", code)
    await dm.flush()
    assert gw.sent == []
    assert dm.get("SM1").outcome == "failed"


async def test_a_failed_resend_is_never_resent_again():
    gw = FakeGateway()
    dm = monitor(gw)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "undelivered", 30003)
    await dm.flush()
    resend_sid = dm.get("SM1").resend_sid
    dm.observe(resend_sid, "undelivered", 30003)
    await dm.flush()
    assert len(gw.sent) == 1
    assert dm.get(resend_sid).outcome == "failed"


async def test_a_resend_that_twilio_refuses_is_recorded_as_failed():
    gw = FakeGateway()
    gw.send_errors.append(SendError("refused", code=21610, status=400))
    dm = monitor(gw)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "failed", 30008)
    await dm.flush()
    assert dm.get("SM1").outcome == "failed"
    assert gw.sent == []


async def test_resending_can_be_switched_off():
    gw = FakeGateway()
    dm = monitor(gw, resend=False)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "undelivered", 30003)
    await dm.flush()
    assert gw.sent == []
    assert dm.get("SM1").outcome == "failed"


async def test_a_status_that_arrives_before_tracking_starts_is_not_lost():
    # Twilio can post a callback before our send() call has even returned
    gw = FakeGateway()
    dm = monitor(gw)
    dm.observe("SM1", "undelivered", 30003)
    dm.track("SM1", TO, "hello")
    await dm.flush()
    assert len(gw.sent) == 1


async def test_the_resend_suffix_is_added_only_when_it_still_fits():
    gw = FakeGateway()
    dm = monitor(gw, resend_suffix=" (resent)", max_part_units=20)
    dm.track("SM1", TO, "short")
    dm.track("SM2", TO, "x" * 18)
    dm.observe("SM1", "undelivered", 30003)
    dm.observe("SM2", "undelivered", 30003)
    await dm.flush()
    assert sorted(m["body"] for m in gw.sent) == ["short (resent)", "x" * 18]


async def test_unknown_statuses_are_ignored():
    gw = FakeGateway()
    dm = monitor(gw)
    dm.track("SM1", TO, "hello")
    dm.observe("SM1", "teleported")
    assert dm.get("SM1").status == "queued"


async def test_stats_count_outcomes():
    gw = FakeGateway()
    dm = monitor(gw)
    for sid in ("SM1", "SM2", "SM3"):
        dm.track(sid, TO, "hello")
    dm.observe("SM1", "delivered")
    dm.observe("SM2", "undelivered", 30006)
    dm.observe("SM3", "undelivered", 30003)
    await dm.flush()
    assert dm.stats() == {"tracked": 4, "delivered": 1, "failed": 1, "resent": 1,
                          "unconfirmed": 0}


async def test_close_cancels_pending_rechecks():
    gw = FakeGateway()
    dm = monitor(gw, recheck_delay=30)
    dm.track("SM1", TO, "hello")
    await asyncio.wait_for(dm.close(), 1)
    assert gw.fetches == []
