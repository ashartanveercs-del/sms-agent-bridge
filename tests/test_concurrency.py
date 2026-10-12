"""The multi-sender proof: many people texting at once, through the real HTTP app.

Every sender tells the agent a secret word, asks for it back, then asks how many messages it
has sent - three texts fired back-to-back without waiting for replies, and all senders at the
same moment. The agent is the real OpenAI-compatible backend, talking HTTP to a fake model
that answers *only* from the conversation history it receives. So:

* a sender that is told any other sender's word means sessions leaked between people;
* a wrong count, or a missing word, means two turns of one sender overlapped (lost update);
* replies in the wrong order mean per-sender FIFO broke.

A control run with a deliberately shared session shows the same checks catch the leak.
"""

import asyncio
import dataclasses
import random

from aiohttp import web
from fakes import AUTH_TOKEN, FakeGateway
from twilio.request_validator import RequestValidator

from smsbridge.agents import AgentRequest, OpenAIChatAgent
from smsbridge.config import Settings
from smsbridge.server import create_app, settle

PUBLIC_URL = "https://sms.example.com"
SENDERS = [f"+1555000{n:04d}" for n in range(100, 112)]
WORDS = ["ACORN", "BIRCH", "CEDAR", "DAISY", "EMBER", "FJORD", "GROVE", "HERON", "IVORY",
         "JUNIPER", "KESTREL", "LINDEN"]
QUESTIONS = ("what is my secret word?", "how many messages have I sent?")


class FakeModel:
    """OpenAI-compatible endpoint whose answers depend only on the messages it is sent."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self._jitter = random.Random(7)

    async def handler(self, request: web.Request) -> web.Response:
        messages = (await request.json())["messages"]
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self._jitter.uniform(0.005, 0.03))  # interleave the turns
            content = self.answer(messages)
        finally:
            self.active -= 1
        return web.json_response({"choices": [{"message": {"role": "assistant",
                                                            "content": content}}]})

    @staticmethod
    def answer(messages: list[dict[str, str]]) -> str:
        said = [m["content"] for m in messages if m["role"] == "user"]
        words = [text.rsplit(" ", 1)[1] for text in said if text.startswith("my secret word is")]
        current = said[-1]
        if current.startswith("my secret word is"):
            return f"noted {words[-1]}"
        if current == QUESTIONS[0]:
            return "your word is " + ",".join(words)
        return f"you have sent {len(said)} messages"


class SharedSessionAgent(OpenAIChatAgent):
    """The classic bug: every sender lands in one agent session."""

    async def reply(self, request: AgentRequest) -> str:
        return await super().reply(dataclasses.replace(request, session_id="shared"))


async def run_scenario(aiohttp_client, aiohttp_server, agent_class=OpenAIChatAgent):
    model = FakeModel()
    model_app = web.Application()
    model_app.router.add_post("/v1/chat/completions", model.handler)
    model_server = await aiohttp_server(model_app)
    agent = agent_class(base_url=str(model_server.make_url("/v1")), api_key="test-key",
                        model="fake-model")
    gateway = FakeGateway()
    settings = Settings(twilio_auth_token=AUTH_TOKEN, public_base_url=PUBLIC_URL,
                        agent_backend="openai", twilio_account_sid="ACtest",
                        twilio_from_number="+15550000000", workers=4,
                        status_recheck_seconds=0, rate_limit_messages=0)
    client = await aiohttp_client(create_app(settings, gateway=gateway, agent=agent))
    validator = RequestValidator(AUTH_TOKEN)

    async def text(sender: str, body: str, sid: str) -> None:
        params = {"MessageSid": sid, "From": sender, "To": "+15550000000", "Body": body}
        signature = validator.compute_signature(PUBLIC_URL + "/twilio/sms", params)
        response = await client.post("/twilio/sms", data=params,
                                     headers={"X-Twilio-Signature": signature})
        assert response.status == 200

    async def conversation(index: int, sender: str, word: str) -> None:
        bodies = (f"my secret word is {word}", *QUESTIONS)
        for step, body in enumerate(bodies):  # back-to-back, no waiting for replies
            await text(sender, body, f"SM{index:02d}{step}")

    await asyncio.gather(*(conversation(i, s, w)
                           for i, (s, w) in enumerate(zip(SENDERS, WORDS, strict=True))))
    await asyncio.wait_for(settle(client.app), 15)
    return gateway, model, settings


async def test_many_senders_at_once_stay_ordered_isolated_and_parallel(aiohttp_client,
                                                                       aiohttp_server):
    gateway, model, settings = await run_scenario(aiohttp_client, aiohttp_server)

    for sender, word in zip(SENDERS, WORDS, strict=True):
        assert gateway.bodies_to(sender) == [
            f"noted {word}", f"your word is {word}", "you have sent 3 messages"], sender
    assert 1 < model.peak <= settings.workers  # parallel across people, bounded by the pool


async def test_the_same_checks_catch_a_shared_session_leak(aiohttp_client, aiohttp_server):
    gateway, _, _ = await run_scenario(aiohttp_client, aiohttp_server, SharedSessionAgent)

    leaked = [sender for sender, word in zip(SENDERS, WORDS, strict=True)
              if gateway.bodies_to(sender)[1] != f"your word is {word}"]
    assert leaked, "a shared session should have mixed up the senders' words"
