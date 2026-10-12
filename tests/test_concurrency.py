"""The multi-sender proof: many people texting at once, through the real HTTP app.

Every sender tells the agent a secret word, asks for it back, then asks how many messages it
has sent. The agent is the real OpenAI-compatible backend, talking HTTP to a fake model that
answers *only* from the conversation history it receives, and the backend counts each
session's turns in flight. So:

* a sender that is told any other sender's word means sessions leaked between people;
* a wrong count, or a missing word, means two turns of one sender overlapped (lost update);
* two turns of one session in flight at once are caught directly, as they start;
* replies in the wrong order mean per-sender FIFO broke.

Two scenarios. In the first, every text is fired back-to-back at the same moment, with more
senders than workers. In the second, each sender's follow-ups arrive while its first turn is
still running at the model, with a free worker standing by: a scheduler that let a sender's
next text start early would hand it straight to that worker. Both also require that the
scheduler never had to skip a stale entry in its rotation. Control runs show the same checks
catch a deliberately shared session, a plain worker pool with no per-sender lock, and a
scheduler that puts a sender back in the rotation while its turn is still running.
"""

import asyncio
import dataclasses
import itertools
import logging
import random
from collections import Counter, defaultdict

from aiohttp import web
from fakes import AUTH_TOKEN, FakeGateway
from twilio.request_validator import RequestValidator

from smsbridge.agents import AgentRequest, OpenAIChatAgent
from smsbridge.config import Settings
from smsbridge.scheduler import FairScheduler
from smsbridge.server import SCHEDULER, create_app, settle

PUBLIC_URL = "https://sms.example.com"
SENDERS = [f"+1555000{n:04d}" for n in range(100, 112)]
WORDS = ["ACORN", "BIRCH", "CEDAR", "DAISY", "EMBER", "FJORD", "GROVE", "HERON", "IVORY",
         "JUNIPER", "KESTREL", "LINDEN"]
QUESTIONS = ("what is my secret word?", "how many messages have I sent?")
FIRST = "my secret word is"


class FakeModel:
    """OpenAI-compatible endpoint whose answers depend only on the messages it is sent.

    With `hold_first_turns`, each sender's first turn stays open at the model until `release`
    is set; `started[word]` is set once that turn has arrived.
    """

    def __init__(self, *, hold_first_turns: bool = False) -> None:
        self.active = 0
        self.peak = 0
        self.hold_first_turns = hold_first_turns
        self.started: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.release = asyncio.Event()
        self._jitter = random.Random(7)

    async def handler(self, request: web.Request) -> web.Response:
        messages = (await request.json())["messages"]
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            current = messages[-1]["content"]
            if self.hold_first_turns and current.startswith(FIRST):
                self.started[current.rsplit(" ", 1)[1]].set()
                await self.release.wait()
            await asyncio.sleep(self._jitter.uniform(0.005, 0.03))  # interleave the turns
            content = self.answer(messages)
        finally:
            self.active -= 1
        return web.json_response({"choices": [{"message": {"role": "assistant",
                                                            "content": content}}]})

    @staticmethod
    def answer(messages: list[dict[str, str]]) -> str:
        said = [m["content"] for m in messages if m["role"] == "user"]
        words = [text.rsplit(" ", 1)[1] for text in said if text.startswith(FIRST)]
        current = said[-1]
        if current.startswith(FIRST):
            return f"noted {words[-1]}"
        if current == QUESTIONS[0]:
            return "your word is " + ",".join(words)
        return f"you have sent {len(said)} messages"


class CountingAgent(OpenAIChatAgent):
    """The real backend, plus a count of each session's turns in flight."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.in_flight: Counter[str] = Counter()
        self.overlapping: set[str] = set()

    async def reply(self, request: AgentRequest) -> str:
        session = request.session_id
        self.in_flight[session] += 1
        if self.in_flight[session] > 1:
            self.overlapping.add(session)
        try:
            return await super().reply(request)
        finally:
            self.in_flight[session] -= 1


class SharedSessionAgent(CountingAgent):
    """The classic bug: every sender lands in one agent session."""

    async def reply(self, request: AgentRequest) -> str:
        return await super().reply(dataclasses.replace(request, session_id="shared"))


class PlainWorkerPool(FairScheduler):
    """The naive design: one FIFO, and whichever worker is free takes the next message."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._serial = itertools.count()

    def submit(self, key: str, item) -> int:
        return super().submit(f"{key}#{next(self._serial)}", item)  # no per-sender lock


class DoubleRotation(FairScheduler):
    """A scheduler bug: a text that arrives mid-turn puts its sender in the rotation again."""

    def submit(self, key: str, item) -> int:
        backlog = super().submit(key, item)
        if key in self._in_flight:
            self._ready.put_nowait(key)
        return backlog


@dataclasses.dataclass
class Run:
    gateway: FakeGateway
    model: FakeModel
    agent: CountingAgent
    settings: Settings
    waiting: dict[str, int] | None = None  # queue stats while the first turns were held


async def run_scenario(aiohttp_client, aiohttp_server, *, agent_class=CountingAgent,
                       follow_ups_mid_turn: bool = False) -> Run:
    model = FakeModel(hold_first_turns=follow_ups_mid_turn)
    model_app = web.Application()
    model_app.router.add_post("/v1/chat/completions", model.handler)
    model_server = await aiohttp_server(model_app)
    agent = agent_class(base_url=str(model_server.make_url("/v1")), api_key="test-key",
                        model="fake-model")
    gateway = FakeGateway()
    # mid-turn: every first turn is held, so keep one worker free to (wrongly) take a follow-up
    workers = len(SENDERS) + 1 if follow_ups_mid_turn else 4
    settings = Settings(twilio_auth_token=AUTH_TOKEN, public_base_url=PUBLIC_URL,
                        agent_backend="openai", twilio_account_sid="ACtest",
                        twilio_from_number="+15550000000", workers=workers,
                        status_recheck_seconds=0, rate_limit_messages=0)
    client = await aiohttp_client(create_app(settings, gateway=gateway, agent=agent))
    validator = RequestValidator(AUTH_TOKEN)
    run = Run(gateway, model, agent, settings)

    async def text(sender: str, body: str, sid: str) -> None:
        params = {"MessageSid": sid, "From": sender, "To": "+15550000000", "Body": body}
        signature = validator.compute_signature(PUBLIC_URL + "/twilio/sms", params)
        response = await client.post("/twilio/sms", data=params,
                                     headers={"X-Twilio-Signature": signature})
        assert response.status == 200

    async def conversation(index: int, sender: str, word: str) -> None:
        bodies = (f"{FIRST} {word}", *QUESTIONS)
        for step, body in enumerate(bodies):  # never waiting for a reply
            await text(sender, body, f"SM{index:02d}{step}")
            if step == 0 and follow_ups_mid_turn:
                await model.started[word].wait()  # the first turn is running at the model

    await asyncio.wait_for(asyncio.gather(*(
        conversation(i, s, w) for i, (s, w) in enumerate(zip(SENDERS, WORDS, strict=True)))), 15)
    if follow_ups_mid_turn:
        await asyncio.sleep(0.05)  # time for a free worker to pick up a follow-up, if allowed
        run.waiting = client.app[SCHEDULER].stats()
        model.release.set()
    await asyncio.wait_for(settle(client.app), 15)
    return run


def expected_replies(word: str) -> list[str]:
    return [f"noted {word}", f"your word is {word}", "you have sent 3 messages"]


def check_ordered_isolated_and_one_turn_at_a_time(run: Run) -> None:
    for sender, word in zip(SENDERS, WORDS, strict=True):
        assert run.gateway.bodies_to(sender) == expected_replies(word), sender
    assert run.agent.overlapping == set()


def scheduler_errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name.startswith("smsbridge") and r.levelno >= logging.ERROR]


async def test_many_senders_at_once_stay_ordered_isolated_and_parallel(aiohttp_client,
                                                                       aiohttp_server, caplog):
    run = await run_scenario(aiohttp_client, aiohttp_server)

    check_ordered_isolated_and_one_turn_at_a_time(run)
    assert 1 < run.model.peak <= run.settings.workers  # parallel, bounded by the pool
    assert scheduler_errors(caplog) == []


async def test_texts_that_arrive_mid_turn_wait_for_that_turn(aiohttp_client, aiohttp_server,
                                                             caplog):
    run = await run_scenario(aiohttp_client, aiohttp_server, follow_ups_mid_turn=True)

    # every sender had a turn running and two texts waiting behind it, with a worker free
    assert run.waiting is not None
    assert (run.waiting["in_flight"], run.waiting["pending"]) == (len(SENDERS),
                                                                  2 * len(SENDERS))
    check_ordered_isolated_and_one_turn_at_a_time(run)
    assert scheduler_errors(caplog) == []  # and no stale entry ever reached the rotation


async def test_the_same_checks_catch_a_shared_session_leak(aiohttp_client, aiohttp_server):
    run = await run_scenario(aiohttp_client, aiohttp_server, agent_class=SharedSessionAgent)

    leaked = [sender for sender, word in zip(SENDERS, WORDS, strict=True)
              if run.gateway.bodies_to(sender)[1] != f"your word is {word}"]
    assert leaked, "a shared session should have mixed up the senders' words"


async def test_the_same_checks_catch_a_pool_without_a_per_sender_lock(aiohttp_client,
                                                                      aiohttp_server,
                                                                      monkeypatch):
    monkeypatch.setattr("smsbridge.server.FairScheduler", PlainWorkerPool)
    run = await run_scenario(aiohttp_client, aiohttp_server, follow_ups_mid_turn=True)

    assert run.agent.overlapping, "a plain pool should have run two turns of one sender at once"
    wrong = [sender for sender, word in zip(SENDERS, WORDS, strict=True)
             if run.gateway.bodies_to(sender) != expected_replies(word)]
    assert wrong, "overlapping turns should have lost an exchange or reordered the replies"


async def test_the_same_checks_catch_a_sender_put_in_the_rotation_twice(aiohttp_client,
                                                                        aiohttp_server,
                                                                        monkeypatch, caplog):
    monkeypatch.setattr("smsbridge.server.FairScheduler", DoubleRotation)
    await run_scenario(aiohttp_client, aiohttp_server, follow_ups_mid_turn=True)

    # the workers' defensive guard keeps the turns apart, but not quietly
    assert scheduler_errors(caplog), "stale rotation entries should have been reported"
