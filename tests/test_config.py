import pytest

from smsbridge.config import ConfigError, Settings

BASE = {
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-auth-token",
    "TWILIO_FROM_NUMBER": "+1 555 000 0000",
    "PUBLIC_BASE_URL": "https://sms.example.com/",
    "AGENT_BACKEND": "echo",
}


def without(*names: str) -> dict[str, str]:
    return {key: value for key, value in BASE.items() if key not in names}


def test_minimal_settings_and_defaults():
    s = Settings.from_env(BASE)
    assert s.twilio_from_number == "+15550000000"
    assert s.public_base_url == "https://sms.example.com"
    assert s.inbound_url == "https://sms.example.com/twilio/sms"
    assert s.status_callback_url == "https://sms.example.com/twilio/status"
    assert (s.workers, s.gsm_mode, s.max_reply_parts) == (8, "lookalikes", 3)
    assert s.validate_signatures and s.resend_on_failure and not s.dry_run
    assert (s.host, s.port) == ("127.0.0.1", 8080)


def test_missing_required_settings_are_all_listed():
    with pytest.raises(ConfigError) as err:
        Settings.from_env({})
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "PUBLIC_BASE_URL", "AGENT_BACKEND"):
        assert name in str(err.value)


def test_a_sender_is_required_unless_running_dry():
    with pytest.raises(ConfigError, match="TWILIO_FROM_NUMBER"):
        Settings.from_env(without("TWILIO_FROM_NUMBER"))
    service = Settings.from_env({**without("TWILIO_FROM_NUMBER"),
                                 "TWILIO_MESSAGING_SERVICE_SID": "MGtest"})
    assert service.twilio_messaging_service_sid == "MGtest"
    dry = Settings.from_env({**without("TWILIO_FROM_NUMBER", "TWILIO_ACCOUNT_SID"),
                             "DRY_RUN": "true"})
    assert dry.dry_run


def test_the_public_url_must_be_https_without_query_or_fragment():
    for url in ("http://sms.example.com", "https://sms.example.com/?a=1",
                "https://sms.example.com/#rc=2"):
        with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
            Settings.from_env({**BASE, "PUBLIC_BASE_URL": url})


def test_a_path_prefix_in_the_public_url_is_kept():
    s = Settings.from_env({**BASE, "PUBLIC_BASE_URL": "https://example.com/bridge/"})
    assert s.inbound_url == "https://example.com/bridge/twilio/sms"


def test_backend_specific_settings_are_required():
    with pytest.raises(ConfigError, match="AGENT_WEBHOOK_URL"):
        Settings.from_env({**BASE, "AGENT_BACKEND": "webhook"})
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        Settings.from_env({**BASE, "AGENT_BACKEND": "openai", "OPENAI_MODEL": "some-model"})
    with pytest.raises(ConfigError, match="OPENAI_MODEL"):
        Settings.from_env({**BASE, "AGENT_BACKEND": "openai", "OPENAI_API_KEY": "key"})
    with pytest.raises(ConfigError, match="AGENT_BACKEND"):
        Settings.from_env({**BASE, "AGENT_BACKEND": "magic"})


def test_numbers_booleans_and_lists_are_parsed():
    s = Settings.from_env({
        **BASE, "WORKERS": "4", "AGENT_TIMEOUT_SECONDS": "30.5", "RESEND_ON_FAILURE": "no",
        "VALIDATE_TWILIO_SIGNATURE": "false", "GSM_MODE": "STRICT", "RATE_LIMIT_MESSAGES": "0",
        "ALLOWED_SENDERS": "+1 555 000 0101, +15550000102,", "RESEND_SUFFIX": " (resent)",
    })
    assert (s.workers, s.agent_timeout_seconds, s.rate_limit_messages) == (4, 30.5, 0)
    assert not s.resend_on_failure and not s.validate_signatures
    assert s.gsm_mode == "strict"
    assert s.allowed_senders == frozenset({"+15550000101", "+15550000102"})
    assert s.resend_suffix == " (resent)"


@pytest.mark.parametrize(("name", "value"), [
    ("WORKERS", "many"), ("WORKERS", "0"), ("PORT", "70000"), ("DRY_RUN", "perhaps"),
    ("GSM_MODE", "fancy"), ("MAX_REPLY_PARTS", "0"), ("AGENT_TIMEOUT_SECONDS", "-1"),
    ("STATUS_RECHECK_SECONDS", "soon"),
])
def test_invalid_values_name_the_setting(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({**BASE, name: value})


def test_blank_optional_values_fall_back_to_defaults():
    s = Settings.from_env({**BASE, "WORKERS": "", "GSM_MODE": "  "})
    assert (s.workers, s.gsm_mode) == (8, "lookalikes")


def test_secrets_stay_out_of_the_repr():
    s = Settings.from_env({**BASE, "TWILIO_AUTH_TOKEN": "auth-value-1", "AGENT_BACKEND": "openai",
                           "OPENAI_API_KEY": "key-value-2", "OPENAI_MODEL": "some-model"})
    text = repr(s)
    assert "auth-value-1" not in text and "key-value-2" not in text
