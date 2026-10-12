import pytest

from smsbridge.sanitize import DEFAULT_FALLBACK, Sanitizer

S = Sanitizer()

# Credential-shaped strings are assembled at runtime so the repository itself never contains
# anything a secret scanner would flag.
FAKE_OPENAI_KEY = "sk-" + "test" + "x" * 24
FAKE_BEARER = "Bearer " + "y" * 32
FAKE_AWS_KEY = "AKIA" + "Z" * 16
FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12


def test_clean_text_is_untouched():
    text = "Your invoice #4411 is paid. Receipt: https://example.com/r/4411"
    result = S.clean(text)
    assert result.text == text
    assert not result.changed


def test_python_traceback_block_is_removed_entirely():
    text = (
        "I couldn't look that up.\n"
        "Traceback (most recent call last):\n"
        '  File "/srv/agent/tools.py", line 88, in lookup\n'
        "    row = db.fetch(order_id)\n"
        "          ^^^^^^^^^^^^^^^^^^\n"
        "KeyError: 'order_id'\n"
        "Want me to try again?"
    )
    result = S.clean(text)
    assert result.text == "I couldn't look that up.\nWant me to try again?"
    assert result.removed_lines == 5


def test_chained_tracebacks_are_removed():
    text = (
        "Traceback (most recent call last):\n"
        '  File "a.py", line 1, in <module>\n'
        "ValueError: bad\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "b.py", line 2, in <module>\n'
        "RuntimeError: worse\n"
        "Sorry about that."
    )
    assert S.clean(text).text == "Sorry about that."


def test_an_indented_traceback_does_not_swallow_the_next_sentence():
    text = (
        "    Traceback (most recent call last):\n"
        '      File "x.py", line 1, in <module>\n'
        "    OSError: boom\n"
        "Sorry, try again."
    )
    assert S.clean(text).text == "Sorry, try again."


def test_stack_frames_from_other_runtimes_are_removed():
    text = (
        "Here is what I found:\n"
        "    at Object.<anonymous> (/app/server.js:10:5)\n"
        "    at processTicksAndRejections (node:internal/process/task_queues:95:5)\n"
        "Your balance is $12."
    )
    assert S.clean(text).text == "Here is what I found:\nYour balance is $12."


@pytest.mark.parametrize("line", [
    "ValueError: invalid literal for int() with base 10: 'abc'",
    "requests.exceptions.ConnectionError: HTTPSConnectionPool(host='api', port=443)",
    "java.lang.NullPointerException",
    "Error: ENOENT: no such file or directory, open 'memory.json'",
    'FATAL: password authentication failed for user "agent"',
    "panic: runtime error: index out of range [3] with length 3",
    "[Errno 111] Connection refused",
    "Tool web_search failed: upstream returned 502",
    "tool `read_file` errored after 3 attempts",
    "Command failed with exit code 1",
    "[tool:calendar] failed - quota exceeded",
    '{"error": {"type": "rate_limit_error", "message": "Too many requests"}}',
    "2026-01-05 10:22:31,442 ERROR agent.loop: step 4 crashed",
    "[ERROR] memory store unavailable",
    "Saved to /home/agent/workspace/notes.md",
    r"Wrote C:\Users\agent\AppData\Local\notes.txt",
    "Loaded ~/.config/agent/settings.toml",
])
def test_internal_error_lines_are_removed(line):
    result = S.clean(f"Thanks for your patience.\n{line}\nYour order ships Monday.")
    assert result.text == "Thanks for your patience.\nYour order ships Monday."
    assert result.removed_lines == 1


@pytest.mark.parametrize("text", [
    "Your payment failed - please update your card at https://example.com/billing",
    "The upload failed because the file was over 10 MB.",
    "Download it from https://example.com/var/files/report.pdf",
    "We're open 9-5, Mon/Tue/Wed, and 24/7 online.",
    "Error rates were low this quarter.",
    "Exception: orders over $500 need a manager's approval.",
    "Use the TCP/IP settings and the and/or operator.",
    "Python raises an exception when you divide by zero.",
    "The command center opens at 9.",
    "Router admin page: http://192.168.0.1/login",
    "Use invoice_id INV-2041 when you call.",
])
def test_ordinary_sentences_are_not_mistaken_for_errors(text):
    result = S.clean(text)
    assert result.text == text
    assert not result.changed


def test_credentials_are_masked_but_the_sentence_is_kept():
    text = f"Use key {FAKE_OPENAI_KEY} and header {FAKE_BEARER} please"
    result = S.clean(text)
    assert result.text == "Use key [redacted] and header [redacted] please"
    assert result.redactions == 2
    assert result.removed_lines == 0


@pytest.mark.parametrize("secret", [FAKE_AWS_KEY, FAKE_JWT, "OPENAI_API_KEY=abc123def456",
                                    "DB_PASSWORD=hunter2", "gh" + "p_" + "q" * 36])
def test_other_credential_shapes_are_masked(secret):
    result = S.clean(f"value: {secret} end")
    assert result.text == "value: [redacted] end"


def test_reply_made_only_of_internals_becomes_the_fallback():
    result = S.clean('Traceback (most recent call last):\n  File "x.py", line 1\nOSError: boom')
    assert result.text == DEFAULT_FALLBACK
    assert result.fallback_used


def test_empty_reply_stays_empty_because_silence_is_a_valid_answer():
    assert S.clean("").text == ""
    result = S.clean("  \n ")
    assert result.text == ""
    assert not result.fallback_used


def test_blank_lines_left_behind_are_collapsed():
    assert S.clean("Hi.\n\nValueError: x\n\nBye.").text == "Hi.\n\nBye."


def test_custom_fallback_and_extra_patterns():
    custom = Sanitizer(fallback="One moment please.", extra_patterns=[r"(?i)internal ticket #\d+"])
    assert custom.clean("See internal ticket #99 for details").text == "One moment please."
    assert custom.clean("All good").text == "All good"


def test_default_fallback_is_plain_gsm_text():
    from smsbridge.gsm7 import is_gsm7

    assert is_gsm7(DEFAULT_FALLBACK)
