"""Many phones, one bridge: per-sender order, isolation and parallelism - offline.

    python examples/concurrency_demo.py

Runs the real aiohttp app on a local port with a dry-run gateway (nothing is sent anywhere)
and a deliberately slow note-taking agent. Eight phones each text three items back-to-back,
all at the same moment, through signed HTTP webhooks. Each reply lists everything that phone
has said so far, so the output shows at a glance whether replies stayed in order and whether
any phone ever saw another phone's items.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import aiohttp
from aiohttp import web
from twilio.request_validator import RequestValidator

from smsbridge.agents import AgentRequest
from smsbridge.config import Settings
from smsbridge.phone import mask_number
from smsbridge.server import GATEWAY, create_app, settle

TOKEN = "demo-token"
PUBLIC_URL = "https://sms.example.com"
PHONES = 8
WORKERS = 3
ITEMS = ("apples", "bread", "coffee")


class SlowNotebookAgent:
    """Remembers what each phone said; every turn takes 50-150 ms."""

    def __init__(self) -> None:
        self.notes: dict[str, list[str]] = {}
        self.active = self.peak = self.turns = 0
        self.busy_seconds = 0.0
        self._rng = random.Random(1)

    async def reply(self, request: AgentRequest) -> str:
        self.active += 1
        self.peak = max(self.peak, self.active)
        delay = self._rng.uniform(0.05, 0.15)
        try:
            await asyncio.sleep(delay)
            notes = self.notes.setdefault(request.session_id, [])
            notes.append(request.text)
            return f"({len(notes)}) noted: {', '.join(notes)}"
        finally:
            self.active -= 1
            self.turns += 1
            self.busy_seconds += delay

    async def close(self) -> None:
        return None


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    agent = SlowNotebookAgent()
    settings = Settings(twilio_auth_token=TOKEN, public_base_url=PUBLIC_URL,
                        agent_backend="echo", dry_run=True, workers=WORKERS,
                        status_recheck_seconds=0, rate_limit_messages=0)
    app = create_app(settings, agent=agent)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    validator = RequestValidator(TOKEN)

    async with aiohttp.ClientSession() as http:

        async def text(phone: str, body: str, sid: str) -> None:
            params = {"MessageSid": sid, "From": phone, "To": "+15550000000", "Body": body}
            signature = validator.compute_signature(PUBLIC_URL + "/twilio/sms", params)
            async with http.post(f"http://127.0.0.1:{port}/twilio/sms", data=params,
                                 headers={"X-Twilio-Signature": signature}) as resp:
                assert resp.status == 200

        async def phone(index: int) -> None:
            for step, item in enumerate(ITEMS):
                await text(f"+1555000{100 + index:04d}", f"{item}-{index}", f"SM{index:02d}{step}")

        started = time.monotonic()
        await asyncio.gather(*(phone(index) for index in range(PHONES)))
        await asyncio.wait_for(settle(app), 30)
        elapsed = time.monotonic() - started
    await runner.cleanup()

    print(f"{PHONES} phones x {len(ITEMS)} texts, all at once, {WORKERS} workers\n")
    ok = True
    for index in range(PHONES):
        number = f"+1555000{100 + index:04d}"
        replies = [m["body"] for m in app[GATEWAY].sent if m["to"] == number]
        expected = [f"({n}) noted: " + ", ".join(f"{item}-{index}" for item in ITEMS[:n])
                    for n in range(1, len(ITEMS) + 1)]
        ok = ok and replies == expected
        print(f"{mask_number(number)}  " + "  |  ".join(replies))
    print(f"\nin order, no cross-talk: {'yes' if ok else 'NO'}")
    print(f"peak agent turns at once: {agent.peak} (pool size {WORKERS})")
    print(f"wall time {elapsed:.2f}s for {agent.busy_seconds:.2f}s of agent work")
    if not ok or not 1 < agent.peak <= WORKERS:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
