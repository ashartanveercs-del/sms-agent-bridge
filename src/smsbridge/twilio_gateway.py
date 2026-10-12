"""Thin wrapper over the official Twilio SDK, plus a dry-run stand-in for local work.

Signature checks use the SDK's `RequestValidator` (Twilio recommends it over hand-rolled
HMAC code because the set of signed parameters keeps evolving). Blocking SDK calls run in a
worker thread so they never stall the event loop, and every call has a deadline. The SDK's
HTTP client has no timeout by default, so one dead connection to the API would otherwise hold
a scheduler worker (and a thread) forever, and enough of them would freeze every conversation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .phone import mask_number

log = logging.getLogger(__name__)

T = TypeVar("T")
DEFAULT_API_TIMEOUT = 10.0  # seconds; a Twilio REST call normally takes well under one

# Delivery failures where sending the same text to the same number again cannot succeed, or
# would ignore the recipient's opt-out. The delivery monitor never resends these.
NO_RESEND_ERROR_CODES: dict[int, str] = {
    21211: "invalid 'To' number",
    21610: "recipient has opted out",
    21614: "'To' is not a mobile number",
    21617: "body longer than 1600 characters",
    30002: "account suspended",
    30004: "message blocked",
    30006: "landline or unreachable carrier",
    30007: "filtered by the carrier",
    30034: "sender not registered for US A2P 10DLC",
    30630: "recipient has opted out",
}


class SendError(Exception):
    def __init__(self, message: str, *, code: int | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class SentMessage:
    sid: str
    status: str


@dataclass(frozen=True)
class MessageStatus:
    sid: str
    status: str
    error_code: int | None = None


class MessagingGateway(Protocol):
    def is_valid_request(self, url: str, params: Mapping[str, Any], signature: str) -> bool: ...

    async def send(self, to: str, body: str, *,
                   status_callback: str | None = None) -> SentMessage: ...

    async def fetch_status(self, sid: str) -> MessageStatus: ...


class _SignatureChecks:
    def __init__(self, auth_token: str) -> None:
        self._validator = RequestValidator(auth_token)

    def is_valid_request(self, url: str, params: Mapping[str, Any], signature: str) -> bool:
        """X-Twilio-Signature: HMAC-SHA1 over the exact public URL plus the sorted params."""
        if not signature:
            return False
        return bool(self._validator.validate(url, params, signature))


def parse_error_code(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class TwilioGateway(_SignatureChecks):
    def __init__(self, account_sid: str, auth_token: str, *, from_number: str | None = None,
                 messaging_service_sid: str | None = None, client: Any | None = None,
                 api_timeout: float = DEFAULT_API_TIMEOUT) -> None:
        if not (from_number or messaging_service_sid):
            raise ValueError("a from_number or a messaging_service_sid is required")
        super().__init__(auth_token)
        self.api_timeout = api_timeout
        self._client = client if client is not None else Client(
            account_sid, auth_token, http_client=TwilioHttpClient(timeout=api_timeout))
        self._sender: dict[str, str] = {}
        if from_number:
            self._sender["from_"] = from_number
        if messaging_service_sid:
            self._sender["messaging_service_sid"] = messaging_service_sid

    async def _api(self, call: Callable[[], T], refused: str) -> T:
        """Run a blocking SDK call in a thread, bounded by `api_timeout`; failures -> SendError.

        The HTTP timeout above frees the thread; this deadline frees the caller even where the
        HTTP timeout does not apply (a stalled DNS lookup, a response that trickles in).
        """
        try:
            return await asyncio.wait_for(asyncio.to_thread(call), self.api_timeout)
        except TwilioRestException as err:
            raise SendError(f"{refused} (HTTP {err.status}, code {err.code})",
                            code=err.code, status=err.status) from err
        except TimeoutError as err:
            raise SendError(f"Twilio did not answer within {self.api_timeout:g}s") from err
        except Exception as err:
            raise SendError(f"could not reach Twilio ({type(err).__name__})") from err

    async def send(self, to: str, body: str, *,
                   status_callback: str | None = None) -> SentMessage:
        kwargs: dict[str, Any] = {"to": to, "body": body, **self._sender}
        if status_callback:
            kwargs["status_callback"] = status_callback
        message = await self._api(lambda: self._client.messages.create(**kwargs),
                                  "Twilio rejected the message")
        return SentMessage(str(message.sid), str(message.status or "queued"))

    async def fetch_status(self, sid: str) -> MessageStatus:
        message = await self._api(lambda: self._client.messages(sid).fetch(),
                                  "status lookup failed")
        return MessageStatus(str(message.sid), str(message.status),
                             parse_error_code(message.error_code))


class DryRunGateway(_SignatureChecks):
    """Logs what would have been sent. Inbound signatures are still verified for real."""

    def __init__(self, auth_token: str) -> None:
        super().__init__(auth_token)
        self.sent: list[dict[str, str]] = []

    async def send(self, to: str, body: str, *,
                   status_callback: str | None = None) -> SentMessage:
        sid = "SM" + uuid.uuid4().hex
        self.sent.append({"sid": sid, "to": to, "body": body})
        log.info("[dry-run] %s to %s (%d chars): %s", sid, mask_number(to), len(body), body)
        return SentMessage(sid, "queued")

    async def fetch_status(self, sid: str) -> MessageStatus:
        return MessageStatus(sid, "delivered")
