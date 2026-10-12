import asyncio

import pytest
from aiohttp import web

from smsbridge.agents import (
    AgentError,
    AgentRequest,
    ConversationMemory,
    EchoAgent,
    Media,
    OpenAIChatAgent,
    WebhookAgent,
)

SENDER = "+15550000101"


def request(text: str, sender: str = SENDER, sid: str = "SM1", media=()) -> AgentRequest:
    return AgentRequest(session_id=f"sms:{sender}", sender=sender, text=text, message_sid=sid,
                        media=tuple(media))


async def serve(aiohttp_server, path: str, handler) -> str:
    app = web.Application()
    app.router.add_post(path, handler)
    server = await aiohttp_server(app)
    return str(server.make_url(path))


async def hang_up_server() -> asyncio.Server:
    """Accepts TCP connections and closes them at once - an agent that crashed mid-request."""

    def drop(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    return await asyncio.start_server(drop, "127.0.0.1", 0)


async def test_echo_agent_repeats_the_message():
    agent = EchoAgent()
    assert await agent.reply(request("hello")) == "You said: hello"
    await agent.close()


# --- webhook backend -------------------------------------------------------------------

async def test_webhook_agent_posts_the_message_and_returns_the_reply(aiohttp_server):
    seen = []

    async def handler(req: web.Request) -> web.Response:
        seen.append((req.headers.get("Authorization"), await req.json()))
        return web.json_response({"reply": "Invoice 42 is paid."})

    url = await serve(aiohttp_server, "/agent", handler)
    agent = WebhookAgent(url, token="shared-secret")
    media = [Media("https://example.com/media/1", "image/jpeg")]
    try:
        assert await agent.reply(request("Is invoice 42 paid?", media=media)) == \
            "Invoice 42 is paid."
    finally:
        await agent.close()
    auth, body = seen[0]
    assert auth == "Bearer shared-secret"
    assert body == {"session_id": f"sms:{SENDER}", "from": SENDER, "text": "Is invoice 42 paid?",
                    "message_sid": "SM1",
                    "media": [{"url": "https://example.com/media/1",
                               "content_type": "image/jpeg"}]}


@pytest.mark.parametrize("response", [
    web.json_response({"reply": ""}),
    web.json_response({"reply": None}),
    web.Response(status=204),
], ids=["empty-reply", "null-reply", "204"])
async def test_webhook_agent_can_explicitly_decline_to_reply(aiohttp_server, response):
    async def handler(req: web.Request) -> web.Response:
        return response

    agent = WebhookAgent(await serve(aiohttp_server, "/agent", handler))
    try:
        assert await agent.reply(request("thanks!")) == ""
    finally:
        await agent.close()


@pytest.mark.parametrize(("status", "retryable"), [(500, True), (503, True), (429, True),
                                                   (401, False), (404, False)])
async def test_webhook_agent_http_errors_are_classified(aiohttp_server, status, retryable):
    async def handler(req: web.Request) -> web.Response:
        return web.Response(status=status, text="nope")

    agent = WebhookAgent(await serve(aiohttp_server, "/agent", handler))
    try:
        with pytest.raises(AgentError) as err:
            await agent.reply(request("hi"))
    finally:
        await agent.close()
    assert err.value.retryable is retryable


@pytest.mark.parametrize("body", ["", "not json", '{"answer": "hi"}', '{"reply": 5}', "[]"])
async def test_webhook_agent_malformed_answers_are_errors_not_silence(aiohttp_server, body):
    async def handler(req: web.Request) -> web.Response:
        return web.Response(text=body, content_type="application/json")

    agent = WebhookAgent(await serve(aiohttp_server, "/agent", handler))
    try:
        with pytest.raises(AgentError):
            await agent.reply(request("hi"))
    finally:
        await agent.close()


async def test_webhook_agent_dropping_the_connection_is_a_retryable_error():
    server = await hang_up_server()
    port = server.sockets[0].getsockname()[1]
    agent = WebhookAgent(f"http://127.0.0.1:{port}/agent")
    try:
        with pytest.raises(AgentError) as err:
            await agent.reply(request("hi"))
    finally:
        await agent.close()
        server.close()
        await server.wait_closed()
    assert err.value.retryable


# --- OpenAI-compatible backend ---------------------------------------------------------

class FakeChatCompletions:
    """Minimal /v1/chat/completions: records requests, answers from a script."""

    def __init__(self, answers=None) -> None:
        self.requests: list[dict] = []
        self.headers: list[str | None] = []
        self.answers = list(answers or [])

    async def handler(self, req: web.Request) -> web.Response:
        self.requests.append(await req.json())
        self.headers.append(req.headers.get("Authorization"))
        answer = self.answers.pop(0) if self.answers else f"reply {len(self.requests)}"
        if isinstance(answer, web.Response):
            return answer
        return web.json_response(
            {"choices": [{"index": 0, "message": {"role": "assistant", "content": answer}}]})


async def openai_agent(aiohttp_server, fake: FakeChatCompletions, **kwargs) -> OpenAIChatAgent:
    url = await serve(aiohttp_server, "/v1/chat/completions", fake.handler)
    base = url.removesuffix("/chat/completions")
    return OpenAIChatAgent(base_url=base, api_key="test-key", model="test-model",
                           system_prompt="Be brief.", **kwargs)


async def test_openai_agent_keeps_one_history_per_session(aiohttp_server):
    fake = FakeChatCompletions()
    agent = await openai_agent(aiohttp_server, fake)
    try:
        assert await agent.reply(request("hi", sender="+15550000101")) == "reply 1"
        assert await agent.reply(request("hello", sender="+15550000102")) == "reply 2"
        assert await agent.reply(request("again", sender="+15550000101")) == "reply 3"
    finally:
        await agent.close()

    assert fake.headers[0] == "Bearer test-key"
    assert fake.requests[0]["model"] == "test-model"
    assert fake.requests[1]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hello"},
    ]
    assert fake.requests[2]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "again"},
    ]


async def test_openai_agent_history_is_bounded(aiohttp_server):
    fake = FakeChatCompletions()
    agent = await openai_agent(aiohttp_server, fake, history_turns=2)
    try:
        for n in range(5):
            await agent.reply(request(f"message {n}"))
    finally:
        await agent.close()
    messages = fake.requests[-1]["messages"]
    assert [m["content"] for m in messages] == [
        "Be brief.", "message 2", "reply 3", "message 3", "reply 4", "message 4"]


async def test_openai_agent_does_not_record_failed_turns(aiohttp_server):
    fake = FakeChatCompletions(answers=[web.Response(status=502), "ok"])
    agent = await openai_agent(aiohttp_server, fake)
    try:
        with pytest.raises(AgentError) as err:
            await agent.reply(request("first try"))
        assert err.value.retryable
        assert await agent.reply(request("first try")) == "ok"
    finally:
        await agent.close()
    # the retried turn is sent once, not stacked on top of the failed attempt
    assert [m["content"] for m in fake.requests[1]["messages"]] == ["Be brief.", "first try"]


@pytest.mark.parametrize("answer", ["", None])
async def test_openai_agent_empty_completion_is_an_error(aiohttp_server, answer):
    fake = FakeChatCompletions(answers=[answer])
    agent = await openai_agent(aiohttp_server, fake)
    try:
        with pytest.raises(AgentError):
            await agent.reply(request("hi"))
    finally:
        await agent.close()


async def test_openai_agent_rejects_bad_credentials_without_retry(aiohttp_server):
    fake = FakeChatCompletions(answers=[web.json_response({"error": "bad key"}, status=401)])
    agent = await openai_agent(aiohttp_server, fake)
    try:
        with pytest.raises(AgentError) as err:
            await agent.reply(request("hi"))
    finally:
        await agent.close()
    assert not err.value.retryable
    assert "test-key" not in str(err.value)


async def test_openai_agent_describes_attachments_in_the_user_turn(aiohttp_server):
    fake = FakeChatCompletions()
    agent = await openai_agent(aiohttp_server, fake)
    media = [Media("https://example.com/media/7", "image/png")]
    try:
        await agent.reply(request("what is this receipt?", media=media))
    finally:
        await agent.close()
    assert fake.requests[0]["messages"][-1]["content"] == (
        "what is this receipt?\n[attachment: image/png https://example.com/media/7]")


def test_conversation_memory_evicts_the_least_recent_session():
    memory = ConversationMemory(max_turns=4, max_sessions=2)
    memory.record("a", "hi", "hello")
    memory.record("b", "hi", "hello")
    memory.history("a")  # reading "a" makes "b" the least recent
    memory.record("c", "hi", "hello")
    assert memory.history("b") == []
    assert len(memory.history("a")) == 2
    assert len(memory) == 2
