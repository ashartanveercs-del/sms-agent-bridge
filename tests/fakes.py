"""Test doubles shared across the suite. Nothing in here touches the network or Twilio."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from twilio.request_validator import RequestValidator

from smsbridge.twilio_gateway import MessageStatus, SentMessage

AUTH_TOKEN = "test-auth-token"


class FakeGateway:
    """In-memory MessagingGateway: records sends, serves scripted statuses.

    `hold_fetches` parks every status lookup until `release_fetches` is set, so a test can
    land a status callback while a re-check is mid-flight - deterministically.
    """

    def __init__(self, auth_token: str = AUTH_TOKEN) -> None:
        self._validator = RequestValidator(auth_token)
        self.sent: list[dict[str, Any]] = []
        self.statuses: dict[str, tuple[str, int | None]] = {}
        self.send_errors: list[Exception] = []
        self.fetches: list[str] = []
        self.hold_fetches = False
        self.fetch_started = asyncio.Event()
        self.release_fetches = asyncio.Event()
        self._sent_changed = asyncio.Event()

    def is_valid_request(self, url: str, params: Any, signature: str) -> bool:
        return bool(signature) and bool(self._validator.validate(url, params, signature))

    async def send(self, to: str, body: str, *,
                   status_callback: str | None = None) -> SentMessage:
        await asyncio.sleep(0)
        if self.send_errors:
            raise self.send_errors.pop(0)
        sid = f"SM{len(self.sent) + 1:032x}"
        self.sent.append({"sid": sid, "to": to, "body": body, "status_callback": status_callback})
        self._sent_changed.set()
        return SentMessage(sid, "queued")

    async def fetch_status(self, sid: str) -> MessageStatus:
        self.fetches.append(sid)
        self.fetch_started.set()
        if self.hold_fetches:
            await self.release_fetches.wait()
        status, code = self.statuses.get(sid, ("sent", None))
        return MessageStatus(sid, status, code)

    def bodies_to(self, to: str) -> list[str]:
        return [m["body"] for m in self.sent if m["to"] == to]

    async def wait_for_sent(self, count: int, timeout: float = 3.0) -> None:
        async def reached() -> None:
            while len(self.sent) < count:
                self._sent_changed.clear()
                await self._sent_changed.wait()

        await asyncio.wait_for(reached(), timeout)


class _StubMessages:
    """Mimics `client.messages.create(...)` and `client.messages(sid).fetch()`."""

    def __init__(self, client: StubTwilioClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self._client.maybe_hang(kwargs.get("to", ""))
        if self._client.fail_with is not None:
            raise self._client.fail_with
        self._client.created.append(kwargs)
        return SimpleNamespace(sid=f"SM{len(self._client.created):032x}", status="queued")

    def __call__(self, sid: str) -> SimpleNamespace:
        def fetch() -> SimpleNamespace:
            self._client.maybe_hang(sid)
            status, error_code = self._client.statuses.get(sid, ("sent", None))
            return SimpleNamespace(sid=sid, status=status, error_code=error_code)

        return SimpleNamespace(fetch=fetch)


class StubTwilioClient:
    """Stand-in for `twilio.rest.Client` - records calls, never touches the network.

    A call that sends to, or looks up, anything in `hang` blocks its thread until `release`
    is set (5 s at most), like a request on a dead connection with no timeout.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.statuses: dict[str, tuple[str, int | None]] = {}
        self.fail_with = fail_with
        self.hang: set[str] = set()
        self.release = threading.Event()
        self.messages = _StubMessages(self)

    def maybe_hang(self, target: str) -> None:
        if target in self.hang:
            self.release.wait(5)


class ScriptedAgent:
    """Agent double that answers from a script of replies and exceptions.

    `gate`, when given, parks every reply until the test sets it.
    """

    def __init__(self, *script: Any, delay: float = 0.0,
                 gate: asyncio.Event | None = None) -> None:
        self.script = list(script)
        self.delay = delay
        self.gate = gate
        self.requests: list[Any] = []
        self.closed = False

    async def reply(self, request: Any) -> str:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        step = self.script.pop(0) if self.script else f"echo: {request.text}"
        if isinstance(step, BaseException):
            raise step
        return step

    async def close(self) -> None:
        self.closed = True


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
