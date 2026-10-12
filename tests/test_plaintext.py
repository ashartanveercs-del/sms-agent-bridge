from smsbridge.plaintext import markdown_to_sms


def test_plain_text_passes_through_untouched():
    text = "Your table for 4 is booked at 7pm. Reply C to cancel."
    assert markdown_to_sms(text) == text


def test_emphasis_and_inline_code_markers_are_removed():
    md = "**Total:** __$42__ due *today* or _tomorrow_ - run `pay now` ***or not***"
    assert markdown_to_sms(md) == "Total: $42 due today or tomorrow - run pay now or not"


def test_snake_case_and_arithmetic_are_not_mistaken_for_emphasis():
    text = "use invoice_id and customer_ref, total 2*3*4 units"
    assert markdown_to_sms(text) == text


def test_struck_through_text_is_dropped():
    assert markdown_to_sms("Price: ~~$50~~ $40") == "Price: $40"


def test_headings_quotes_and_rules_are_flattened():
    md = "# Receipt\n\n> Thanks for your order\n\n---\n\n## Items ##\nCoffee"
    assert markdown_to_sms(md) == "Receipt\n\nThanks for your order\n\nItems\nCoffee"


def test_bullets_become_plain_dashes():
    md = "* one\n+ two\n- three\n  * nested\n\u2022 four\n1. numbered"
    assert markdown_to_sms(md) == "- one\n- two\n- three\n  - nested\n- four\n1. numbered"


def test_links_and_images_keep_their_urls():
    assert (markdown_to_sms("See [the invoice](https://example.com/inv/42).")
            == "See the invoice (https://example.com/inv/42).")
    assert markdown_to_sms("[https://example.com](https://example.com)") == "https://example.com"
    assert markdown_to_sms("Help: <https://example.com/help>") == "Help: https://example.com/help"
    assert (markdown_to_sms('![receipt](https://example.com/r.png "scan")')
            == "receipt (https://example.com/r.png)")


def test_code_fences_are_removed_but_the_code_is_kept():
    md = "Run this:\n```bash\npay --invoice 42\n```\nDone."
    assert markdown_to_sms(md) == "Run this:\npay --invoice 42\nDone."


def test_blank_lines_and_trailing_spaces_are_collapsed():
    assert markdown_to_sms("a  \n\n\n\nb\n") == "a\n\nb"


def test_empty_input_stays_empty():
    assert markdown_to_sms("") == ""
    assert markdown_to_sms("  \n ") == ""
