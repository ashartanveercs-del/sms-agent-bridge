import pytest

from smsbridge.phone import compliance_keyword, mask_number, normalise_number, session_id_for


def test_numbers_are_normalised_to_digits_with_an_optional_plus():
    assert normalise_number(" +1 (555) 000-0199 ") == "+15550000199"
    assert normalise_number("55501") == "55501"  # short codes have no plus
    assert normalise_number("") == ""
    assert normalise_number("not a number") == ""


def test_masking_keeps_only_the_last_four_digits():
    assert mask_number("+15550000199") == "+*******0199"
    assert mask_number("55501") == "*5501"
    assert mask_number("") == "?"


@pytest.mark.parametrize(("body", "kind"), [
    ("STOP", "opt-out"), ("  stop. ", "opt-out"), ("Unsubscribe", "opt-out"),
    ("cancel", "opt-out"), ("END!", "opt-out"), ("quit", "opt-out"), ("stopall", "opt-out"),
    ("revoke", "opt-out"), ("OPTOUT", "opt-out"),
    ("START", "opt-in"), ("unstop", "opt-in"),
    ("help", "help"), ("INFO", "help"),
])
def test_compliance_keywords_are_recognised(body, kind):
    assert compliance_keyword(body) == kind


@pytest.mark.parametrize("body", [
    "please stop the delivery",
    "Can you help me with my invoice?",
    "yes",  # an opt-in keyword on some senders, but far more often a plain answer
    "",
])
def test_ordinary_messages_are_not_keywords(body):
    assert compliance_keyword(body) is None


def test_every_sender_gets_its_own_agent_session():
    assert session_id_for("+15550000199") == "sms:+15550000199"
    assert session_id_for("+15550000199") != session_id_for("+15550000198")
