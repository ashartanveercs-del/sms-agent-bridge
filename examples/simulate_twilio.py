"""Send signed, Twilio-style inbound webhooks to a running bridge - no Twilio account needed.

    sms-agent-bridge serve --env-file examples/dry-run.env     # DRY_RUN: replies are logged
    python examples/simulate_twilio.py

Several simulated phones (+1 555 000 01xx) each send a short script back-to-back, all at the
same time. Signatures are computed the way Twilio computes them - over the *public* URL the
bridge is configured with, not the local address the request actually goes to - so this also
exercises signature checking behind a proxy or tunnel. Use --unsigned to see the 403.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import aiohttp
from twilio.request_validator import RequestValidator

SCRIPT = (
    "Hi! What are your opening hours?",
    "Is repair 1001 ready?",
    "Can I pay invoice \u201cINV-2041\u201d today \u2014 or tomorrow?",
)


async def run(args: argparse.Namespace) -> None:
    validator = RequestValidator(args.auth_token)
    public_url = args.public_url.rstrip("/") + "/twilio/sms"
    bridge = args.bridge.rstrip("/")

    async with aiohttp.ClientSession() as http:

        async def text(phone: str, body: str) -> None:
            params = {"MessageSid": "SM" + uuid.uuid4().hex, "AccountSid": "ACsimulator",
                      "From": phone, "To": args.to, "Body": body, "NumMedia": "0"}
            headers = {} if args.unsigned else {
                "X-Twilio-Signature": validator.compute_signature(public_url, params)}
            async with http.post(bridge + "/twilio/sms", data=params, headers=headers) as resp:
                print(f"{phone}  HTTP {resp.status}  {body}")

        async def phone(index: int) -> None:
            number = f"+1555000{100 + index:04d}"
            for body in SCRIPT:  # back-to-back: the bridge must keep them in order
                await text(number, body)

        await asyncio.gather(*(phone(index) for index in range(args.phones)))

        stats: dict = {}
        for _ in range(100):  # wait for the workers to finish the queued turns
            async with http.get(bridge + "/healthz") as resp:
                stats = await resp.json()
            if not stats["queue"]["pending"] and not stats["queue"]["in_flight"]:
                break
            await asyncio.sleep(0.2)
        print(json.dumps(stats, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate Twilio inbound SMS webhooks.")
    parser.add_argument("--bridge", default="http://127.0.0.1:8080",
                        help="where the bridge listens (default: %(default)s)")
    parser.add_argument("--public-url", default="https://sms.example.com",
                        help="the bridge's PUBLIC_BASE_URL (default: %(default)s)")
    parser.add_argument("--auth-token", default="local-dev-token",
                        help="the bridge's TWILIO_AUTH_TOKEN (default: %(default)s)")
    parser.add_argument("--to", default="+15550000000", help="the bridge's own number")
    parser.add_argument("--phones", type=int, default=3)
    parser.add_argument("--unsigned", action="store_true", help="omit the signature")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
