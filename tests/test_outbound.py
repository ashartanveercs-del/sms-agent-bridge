import pytest

from smsbridge.outbound import MAX_BODY_UNITS, OutboundComposer, split_text, utf16_units
from smsbridge.sanitize import DEFAULT_FALLBACK


def composer(**kwargs) -> OutboundComposer:
    return OutboundComposer(**kwargs)


def test_short_reply_becomes_one_gsm7_part():
    plan = composer().compose("Your order **#4411** ships Monday \u2014 we\u2019ll text you.")
    assert [p.body for p in plan.parts] == ["Your order #4411 ships Monday - we'll text you."]
    assert plan.parts[0].info.encoding == "GSM-7"
    assert plan.segments == 1


def test_pipeline_sanitises_then_flattens_markdown_then_normalises():
    raw = ("Done \u2014 here\u2019s why:\n```\nTraceback (most recent call last):\n"
           '  File "/srv/x.py", line 1\nOSError: boom\n```\nAnything else?')
    plan = composer().compose(raw)
    assert plan.text == "Done - here's why:\nAnything else?"
    assert plan.removed_lines == 3


def test_empty_and_blank_replies_produce_no_parts():
    assert composer().compose("").empty
    assert composer().compose("   \n").empty


def test_a_reply_made_only_of_internals_becomes_the_fallback():
    plan = composer().compose("ValueError: boom")
    assert plan.text == DEFAULT_FALLBACK
    assert plan.fallback_used


def test_long_replies_split_on_paragraphs_under_the_twilio_limit():
    paragraphs = [f"Paragraph {i}. " + "word " * 150 for i in range(4)]
    plan = composer(max_parts=5).compose("\n\n".join(paragraphs))
    assert len(plan.parts) == 2
    assert plan.parts[0].body.startswith("Paragraph 0.")
    assert plan.parts[1].body.startswith("Paragraph 2.")
    assert all(utf16_units(p.body) <= MAX_BODY_UNITS for p in plan.parts)
    assert not plan.truncated


def test_split_prefers_sentence_boundaries_and_loses_nothing():
    text = ("This is a sentence. " * 100).strip()
    parts = split_text(text, limit=1600)
    assert len(parts) == 2
    assert parts[0].endswith(".")
    assert " ".join(parts) == text


def test_split_hard_cuts_an_unbroken_token():
    assert [len(p) for p in split_text("x" * 3500, limit=1600)] == [1600, 1600, 300]


def test_split_measures_utf16_units_so_emoji_heavy_text_still_fits():
    parts = split_text("\U0001f600" * 1000, limit=1600)  # 2000 UTF-16 units
    assert [len(p) for p in parts] == [800, 200]


def test_parts_beyond_the_cap_are_dropped_and_the_last_kept_part_ends_with_an_ellipsis():
    text = "\n\n".join(f"Section {i}. " + "detail " * 200 for i in range(6))
    plan = composer(max_parts=2).compose(text)
    assert len(plan.parts) == 2
    assert plan.truncated
    assert plan.parts[-1].body.endswith("...")
    assert all(utf16_units(p.body) <= MAX_BODY_UNITS for p in plan.parts)


def test_strict_mode_applies_to_every_part():
    plan = composer(gsm_mode="strict").compose(
        "Thanks \U0001f64f see you at caf\u00e9 \u0141\u00f3d\u017a")
    assert plan.text == "Thanks see you at caf\u00e9 Lodz"
    assert all(p.info.encoding == "GSM-7" for p in plan.parts)


def test_segment_totals_are_reported():
    plan = composer().compose("a" * 200)
    assert plan.segments == 2
    assert plan.parts[0].info.units == 200


def test_invalid_settings_are_rejected():
    with pytest.raises(ValueError):
        composer(gsm_mode="fancy")
    with pytest.raises(ValueError):
        composer(max_parts=0)
