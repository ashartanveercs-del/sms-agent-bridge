"""Agent backends: where replies come from.

The bridge only needs `reply(request) -> str`. Three backends ship with it:

* `EchoAgent`        - repeats the message; for demos and smoke tests.
* `WebhookAgent`     - POSTs each message to your own agent (any framework, any language).
* `OpenAIChatAgent`  - any OpenAI-compatible `/chat/completions` endpoint, with a bounded
                       conversation history per sender.

Every request carries an explicit per-sender `session_id`. Agent runtimes that default to a
single shared session will otherwise mix everybody's conversations together - one sender can
be read back another sender's details. Keying the session on the phone number, and never
running two turns for the same number at once (see `scheduler.py`), keeps conversations apart.

Contract for replies: a string is sent; an *explicit* empty reply means "nothing to say".
Anything else - an HTTP error, an empty or malformed body, an empty completion - raises
`AgentError`, so a failure is never mistaken for a deliberate silence.
"""

from __future__ import annotations

import json
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant replying by text message. Keep answers short - a few "
    "sentences, ideally under 300 characters - in plain text without Markdown or tables."
)


@dataclass(frozen=True)
class Media:
    url: str
    content_type: str


@dataclass(frozen=True)
class AgentRequest:
    session_id: str
    sender: str
    text: str
    message_sid: str = ""
    media: tuple[Media, ...] = ()


class AgentError(Exception):
    """The agent could not produce a reply. `retryable` marks failures worth one more try."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class AgentBackend(Protocol):
    async def reply(self, request: AgentRequest) -> str: ...

    async def close(self) -> None: ...


class EchoAgent:
    async def reply(self, request: AgentRequest) -> str:
        if request.text:
            return f"You said: {request.text}"
        return f"Got {len(request.media)} attachment(s)." if request.media else ""

    async def close(self) -> None:
        return None


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class _HttpBackend:
    def __init__(self, session: aiohttp.ClientSession | None) -> None:
        self._session = session
        self._owns_session = session is None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            # No total timeout here: the bridge bounds every agent turn with its own deadline.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
        return self._session

    async def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str],
                         what: str) -> tuple[int, Any]:
        try:
            async with self._http().post(url, json=payload, headers=headers) as response:
                status = response.status
                body = await response.text()
        except aiohttp.ClientError as err:
            raise AgentError(f"{what} unreachable ({type(err).__name__})") from err
        if status >= 400:
            raise AgentError(f"{what} returned HTTP {status}", retryable=_retryable(status))
        if status == 204:
            return status, None
        try:
            return status, json.loads(body)
        except ValueError as err:
            raise AgentError(f"{what} returned a body that is not JSON") from err

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None


class WebhookAgent(_HttpBackend):
    """POST {session_id, from, text, message_sid, media} -> expects {"reply": "..."}.

    Reply with `{"reply": ""}`, `{"reply": null}` or HTTP 204 to send nothing.
    """

    def __init__(self, url: str, *, token: str | None = None,
                 session: aiohttp.ClientSession | None = None) -> None:
        super().__init__(session)
        self.url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def reply(self, request: AgentRequest) -> str:
        payload = {
            "session_id": request.session_id,
            "from": request.sender,
            "text": request.text,
            "message_sid": request.message_sid,
            "media": [{"url": m.url, "content_type": m.content_type} for m in request.media],
        }
        status, data = await self._post_json(self.url, payload, self._headers, "agent webhook")
        if status == 204:
            return ""
        if not isinstance(data, dict) or "reply" not in data:
            raise AgentError("agent webhook answered without a 'reply' field", retryable=False)
        reply = data["reply"]
        if reply is None:
            return ""
        if not isinstance(reply, str):
            raise AgentError("agent webhook 'reply' must be a string", retryable=False)
        return reply


class ConversationMemory:
    """Last `max_turns` exchanges per session; least-recently-used sessions are evicted."""

    def __init__(self, *, max_turns: int = 12, max_sessions: int = 1000) -> None:
        self._max_messages = max(0, max_turns) * 2
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, deque[dict[str, str]]] = OrderedDict()

    def history(self, session_id: str) -> list[dict[str, str]]:
        turns = self._sessions.get(session_id)
        if turns is None:
            return []
        self._sessions.move_to_end(session_id)
        return list(turns)

    def record(self, session_id: str, user: str, assistant: str) -> None:
        turns = self._sessions.get(session_id)
        if turns is None:
            turns = self._sessions[session_id] = deque(maxlen=self._max_messages)
        turns.append({"role": "user", "content": user})
        turns.append({"role": "assistant", "content": assistant})
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._sessions)


def _user_content(request: AgentRequest) -> str:
    lines = [request.text] if request.text else []
    lines += [f"[attachment: {m.content_type} {m.url}]" for m in request.media]
    return "\n".join(lines)


class OpenAIChatAgent(_HttpBackend):
    """Any OpenAI-compatible chat completions endpoint, one conversation per sender.

    History is read at the start of a turn and written at the end. That read-modify-write is
    only safe because the scheduler never runs two turns for the same sender concurrently.
    A failed turn is not recorded, so a retry is not stacked on top of it.
    """

    def __init__(self, *, base_url: str, api_key: str, model: str,
                 system_prompt: str = DEFAULT_SYSTEM_PROMPT, history_turns: int = 12,
                 max_sessions: int = 1000, session: aiohttp.ClientSession | None = None) -> None:
        super().__init__(session)
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.model = model
        self.system_prompt = system_prompt
        self.memory = ConversationMemory(max_turns=history_turns, max_sessions=max_sessions)

    async def reply(self, request: AgentRequest) -> str:
        user = _user_content(request)
        messages = [{"role": "system", "content": self.system_prompt},
                    *self.memory.history(request.session_id),
                    {"role": "user", "content": user}]
        _, data = await self._post_json(self._url, {"model": self.model, "messages": messages},
                                        self._headers, "chat completions endpoint")
        try:
            content = data["choices"][0]["message"]["content"]
        except (TypeError, KeyError, IndexError) as err:
            raise AgentError("chat completions response had no message content") from err
        if not isinstance(content, str) or not content.strip():
            raise AgentError("the model returned an empty completion")
        self.memory.record(request.session_id, user, content)
        return content
