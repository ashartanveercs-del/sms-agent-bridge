"""Settings, read from environment variables (see .env.example)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .agents import DEFAULT_SYSTEM_PROMPT
from .bridge import DEFAULT_FAILURE_REPLY, DEFAULT_TIMEOUT_REPLY
from .gsm7 import MODES
from .phone import normalise_number

BACKENDS = ("echo", "webhook", "openai")
DEFAULT_BUSY_REPLY = ("I'm still working through your earlier messages - give me a moment "
                      "before sending more.")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    pass


class _Env:
    """Typed access to raw environment strings, with errors that name the variable."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def text(self, name: str, default: str | None = None) -> str | None:
        value = (self._env.get(name) or "").strip()
        return value or default

    def raw(self, name: str, default: str) -> str:
        """Like text() but keeps surrounding spaces (for suffixes such as ' (resent)')."""
        value = self._env.get(name)
        return value if value else default

    def integer(self, name: str, default: int, *, minimum: int = 0,
                maximum: int | None = None) -> int:
        value = self.text(name)
        if value is None:
            return default
        try:
            number = int(value)
        except ValueError:
            raise ConfigError(f"{name} must be a whole number (got {value!r})") from None
        if number < minimum or (maximum is not None and number > maximum):
            limit = f"between {minimum} and {maximum}" if maximum is not None else \
                f"at least {minimum}"
            raise ConfigError(f"{name} must be {limit} (got {number})")
        return number

    def number(self, name: str, default: float, *, minimum: float = 0.0) -> float:
        value = self.text(name)
        if value is None:
            return default
        try:
            number = float(value)
        except ValueError:
            raise ConfigError(f"{name} must be a number (got {value!r})") from None
        if number < minimum:
            raise ConfigError(f"{name} must be at least {minimum} (got {number})")
        return number

    def flag(self, name: str, default: bool) -> bool:
        value = self.text(name)
        if value is None:
            return default
        if value.lower() in _TRUE:
            return True
        if value.lower() in _FALSE:
            return False
        raise ConfigError(f"{name} must be true or false (got {value!r})")


@dataclass(frozen=True)
class Settings:
    twilio_auth_token: str = field(repr=False)
    public_base_url: str
    agent_backend: str
    twilio_account_sid: str | None = None
    twilio_from_number: str | None = None
    twilio_messaging_service_sid: str | None = None
    agent_webhook_url: str | None = None
    agent_webhook_token: str | None = field(default=None, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    history_turns: int = 12
    agent_timeout_seconds: float = 120.0
    workers: int = 8
    max_queued_per_sender: int = 10
    max_queued_total: int = 1000
    rate_limit_messages: int = 10
    rate_limit_window_seconds: float = 60.0
    allowed_senders: frozenset[str] = frozenset()
    gsm_mode: str = "lookalikes"
    max_reply_parts: int = 3
    status_recheck_seconds: float = 30.0
    resend_on_failure: bool = True
    resend_suffix: str = ""
    dry_run: bool = False
    validate_signatures: bool = True
    failure_reply: str = DEFAULT_FAILURE_REPLY
    timeout_reply: str = DEFAULT_TIMEOUT_REPLY
    busy_reply: str = DEFAULT_BUSY_REPLY
    host: str = "127.0.0.1"
    port: int = 8080

    @property
    def inbound_url(self) -> str:
        return self.public_base_url + "/twilio/sms"

    @property
    def status_callback_url(self) -> str:
        return self.public_base_url + "/twilio/status"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        e = _Env(env)
        dry_run = e.flag("DRY_RUN", False)
        required = ["TWILIO_AUTH_TOKEN", "PUBLIC_BASE_URL", "AGENT_BACKEND"]
        if not dry_run:
            required.insert(0, "TWILIO_ACCOUNT_SID")
        missing = [name for name in required if not e.text(name)]
        if missing:
            raise ConfigError("Missing required settings: " + ", ".join(missing))

        base = (e.text("PUBLIC_BASE_URL") or "").rstrip("/")
        if not base.startswith("https://") or "?" in base or "#" in base:
            raise ConfigError("PUBLIC_BASE_URL must be the https:// address Twilio uses to "
                              "reach this server, without a query string or fragment")

        from_number = normalise_number(e.text("TWILIO_FROM_NUMBER") or "") or None
        service_sid = e.text("TWILIO_MESSAGING_SERVICE_SID")
        if not dry_run and not (from_number or service_sid):
            raise ConfigError("Set TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID")

        backend = (e.text("AGENT_BACKEND") or "").lower()
        if backend not in BACKENDS:
            raise ConfigError(f"AGENT_BACKEND must be one of {', '.join(BACKENDS)}")
        if backend == "webhook" and not e.text("AGENT_WEBHOOK_URL"):
            raise ConfigError("AGENT_WEBHOOK_URL is required when AGENT_BACKEND=webhook")
        if backend == "openai":
            for name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
                if not e.text(name):
                    raise ConfigError(f"{name} is required when AGENT_BACKEND=openai")

        gsm_mode = (e.text("GSM_MODE") or "lookalikes").lower()
        if gsm_mode not in MODES:
            raise ConfigError(f"GSM_MODE must be one of {', '.join(MODES)}")

        allowed = frozenset(filter(None, (normalise_number(n) for n in
                                          (e.text("ALLOWED_SENDERS") or "").split(","))))
        return cls(
            twilio_auth_token=e.text("TWILIO_AUTH_TOKEN") or "",
            public_base_url=base,
            agent_backend=backend,
            twilio_account_sid=e.text("TWILIO_ACCOUNT_SID"),
            twilio_from_number=from_number,
            twilio_messaging_service_sid=service_sid,
            agent_webhook_url=e.text("AGENT_WEBHOOK_URL"),
            agent_webhook_token=e.text("AGENT_WEBHOOK_TOKEN"),
            openai_base_url=e.text("OPENAI_BASE_URL", "https://api.openai.com/v1") or "",
            openai_api_key=e.text("OPENAI_API_KEY"),
            openai_model=e.text("OPENAI_MODEL"),
            system_prompt=e.text("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT) or "",
            history_turns=e.integer("HISTORY_TURNS", 12),
            agent_timeout_seconds=e.number("AGENT_TIMEOUT_SECONDS", 120.0, minimum=1.0),
            workers=e.integer("WORKERS", 8, minimum=1, maximum=256),
            max_queued_per_sender=e.integer("MAX_QUEUED_PER_SENDER", 10, minimum=1),
            max_queued_total=e.integer("MAX_QUEUED_TOTAL", 1000, minimum=1),
            rate_limit_messages=e.integer("RATE_LIMIT_MESSAGES", 10),
            rate_limit_window_seconds=e.number("RATE_LIMIT_WINDOW_SECONDS", 60.0, minimum=1.0),
            allowed_senders=allowed,
            gsm_mode=gsm_mode,
            max_reply_parts=e.integer("MAX_REPLY_PARTS", 3, minimum=1, maximum=10),
            status_recheck_seconds=e.number("STATUS_RECHECK_SECONDS", 30.0),
            resend_on_failure=e.flag("RESEND_ON_FAILURE", True),
            resend_suffix=e.raw("RESEND_SUFFIX", ""),
            dry_run=dry_run,
            validate_signatures=e.flag("VALIDATE_TWILIO_SIGNATURE", True),
            failure_reply=e.text("FAILURE_REPLY", DEFAULT_FAILURE_REPLY) or "",
            timeout_reply=e.text("TIMEOUT_REPLY", DEFAULT_TIMEOUT_REPLY) or "",
            busy_reply=e.text("BUSY_REPLY", DEFAULT_BUSY_REPLY) or "",
            host=e.text("HOST", "127.0.0.1") or "",
            port=e.integer("PORT", 8080, minimum=1, maximum=65535),
        )
