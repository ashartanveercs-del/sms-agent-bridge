import pytest

from smsbridge import gsm7
from smsbridge.gsm7 import is_gsm7, normalise, segment_info


def test_alphabet_sizes_match_gsm_03_38():
    # 128 basic positions minus the escape code, plus the 10 extension-table characters
    assert len(gsm7.BASIC) == 127
    assert len(gsm7.EXTENDED) == 10
    assert "\x1b" not in gsm7.BASIC


def test_plain_ascii_and_gsm_symbols_are_gsm7():
    assert is_gsm7("Hello @ home, it's \u00a35 or \u20ac5 - ok? {yes}")
    assert not is_gsm7("It\u2019s fine")          # right single quotation mark
    assert not is_gsm7("re\u2011send")            # non-breaking hyphen
    assert not is_gsm7("tab\tseparated")          # tab is not in the GSM alphabet
    assert not is_gsm7("back`tick")


@pytest.mark.parametrize(("length", "segments"), [(1, 1), (160, 1), (161, 2), (306, 2), (307, 3)])
def test_gsm7_segment_boundaries(length, segments):
    info = segment_info("a" * length)
    assert info.encoding == "GSM-7"
    assert info.units == length
    assert info.segments == segments


def test_extension_characters_cost_two_septets():
    assert segment_info("\u20ac" * 80).segments == 1          # 160 septets
    info = segment_info("\u20ac" * 81)                        # 162 septets
    assert (info.units, info.segments) == (162, 2)


def test_an_escape_sequence_is_never_split_across_segments():
    # 306 septets would fit in two 153-septet segments, but the euro sign sits on the
    # boundary and its escape + character must travel in the same segment.
    text = "a" * 152 + "\u20ac" + "a" * 152
    info = segment_info(text)
    assert info.units == 306
    assert info.segments == 3


@pytest.mark.parametrize(("length", "segments"), [(70, 1), (71, 2), (134, 2), (135, 3)])
def test_ucs2_segment_boundaries(length, segments):
    info = segment_info("\u0142" * length)                   # 'ł' is not in GSM-7
    assert info.encoding == "UCS-2"
    assert info.segments == segments


def test_emoji_count_as_two_utf16_units_and_are_never_split():
    assert segment_info("\U0001f600" * 35).segments == 1     # exactly 70 units
    text = "a" * 66 + "\U0001f600" + "a" * 66                # 134 units, pair on the boundary
    info = segment_info(text)
    assert info.units == 134
    assert info.segments == 3


def test_one_lookalike_character_triples_the_segment_count():
    text = ("Your invoice is ready. Reply PAY to settle it today or call us to set up a plan - "
            "we're open until 6pm on weekdays and noon Sat\u2011Sun. Thanks!")
    assert len(text) == 140
    before = segment_info(text)
    after = segment_info(normalise(text))
    assert (before.encoding, before.segments) == ("UCS-2", 3)
    assert (after.encoding, after.segments) == ("GSM-7", 1)
    assert before.offenders == ("\u2011",)


def test_offenders_lists_each_non_gsm_character_once_in_order():
    info = segment_info("\u201cHi\u201d \u2014 it\u2019s \u201cme\u201d")
    assert info.offenders == ("\u201c", "\u201d", "\u2014", "\u2019")
    assert segment_info("plain").offenders == ()


def test_empty_text_has_no_segments():
    info = segment_info("")
    assert (info.encoding, info.units, info.segments) == ("GSM-7", 0, 0)


def test_lookalike_normalisation_maps_typography_to_gsm7():
    text = ("It\u2019s \u201cdone\u201d \u2014 see\u2026 a\u00a0b\u2009c "
            "re\u2011send x\u200by\ttab\r\nnext")
    assert normalise(text) == "It's \"done\" - see... a b c re-send xy tab\nnext"


def test_lookalike_mode_keeps_meaningful_unicode():
    # emoji and accented letters outside GSM-7 carry meaning; lookalike mode keeps them,
    # so the message is (knowingly) sent as UCS-2
    text = "Gracias \u00e1 todos \U0001f389"
    assert normalise(text) == text
    assert segment_info(normalise(text)).encoding == "UCS-2"


def test_strict_mode_transliterates_a_latin_reply_and_drops_its_emoji():
    text = "Caf\u00e9 cr\u00e8me \u00e1 \u0142 \U0001f600 done \u2013 \u00bd price"
    out = normalise(text, mode="strict")
    assert out == "Caf\u00e9 cr\u00e8me a l done - 1/2 price"
    assert is_gsm7(out)


@pytest.mark.parametrize("text", [
    "\u0412\u0430\u0448 \u0437\u0430\u043a\u0430\u0437 1001 \u0433\u043e\u0442\u043e\u0432!",
    "\u039a\u03b1\u03bb\u03b7\u03bc\u03ad\u03c1\u03b1",             # Greek
    "\u4f60\u597d",                                                     # Chinese
    "Order \u0661\u0662\u0663 has shipped",                             # Arabic-Indic digits
    "Your order is ready. "
    "\u0412\u0430\u0448 \u0437\u0430\u043a\u0430\u0437 \u0433\u043e\u0442\u043e\u0432.",
], ids=["cyrillic", "greek", "chinese", "foreign-digits", "mixed-scripts"])
def test_strict_mode_never_drops_a_letter_or_a_digit(text):
    # transliterating would lose words or numbers, so the reply is sent as written (UCS-2)
    assert normalise(text, mode="strict") == text


@pytest.mark.parametrize("text", ["\U0001f44d", "\U0001f389\U0001f389", "!!! \U0001f389"])
def test_strict_mode_never_reduces_a_reply_to_nothing(text):
    assert normalise(text, mode="strict") == text


def test_off_mode_changes_nothing():
    text = "It\u2019s \u201cdone\u201d"
    assert normalise(text, mode="off") == text


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        normalise("x", mode="aggressive")


def test_every_lookalike_replacement_is_itself_gsm7():
    for source, replacement in gsm7.LOOKALIKES.items():
        assert not is_gsm7(source), source
        assert is_gsm7(replacement), (source, replacement)


@pytest.mark.parametrize("mode", ["lookalikes", "strict"])
def test_normalisation_is_idempotent(mode):
    text = "\u201cQuote\u201d \u2014 caf\u00e9 \u0142 \U0001f600\u2026\t\u00a0end"
    once = normalise(text, mode=mode)
    assert normalise(once, mode=mode) == once
