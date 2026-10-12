"""A tiny agent behind AGENT_BACKEND=webhook: the text assistant of a fictional bike shop.

Run it, then point the bridge at it:

    python examples/agent_webhook_server.py --port 8090
    # in the bridge's env:  AGENT_BACKEND=webhook
    #                       AGENT_WEBHOOK_URL=http://127.0.0.1:8090/agent

The bridge POSTs {"session_id", "from", "text", "message_sid", "media"} and expects
{"reply": "..."} back. An empty reply (or HTTP 204) means "send nothing". Every phone number
arrives with its own session_id, so per-customer state is keyed on it. Put LangGraph, the
OpenAI Agents SDK, the Claude Agent SDK or anything else behind this same contract.
"""

from __future__ import annotations

import argparse
import hmac
import os
import re

from aiohttp import web

HOURS = "We're open Tuesday to Saturday, 9am to 6pm."
REPAIRS = {"1001": "ready for pickup", "1002": "waiting on a part (due Thursday)"}
THANKS = {"thanks", "thank you", "ok", "great", "cool"}


def make_app(token: str | None = None) -> web.Application:
    names: dict[str, str] = {}  # session_id -> the customer's name, once they tell us

    def answer(session: str, text: str, has_media: bool) -> str:
        lowered = text.lower().strip(" .!")
        if match := re.search(r"\bmy name is (\w+)", text, re.IGNORECASE):
            names[session] = match.group(1).title()
            return f"Nice to meet you, {names[session]}!"
        if lowered in THANKS:
            return ""  # nothing useful to add: stay quiet rather than "you're welcome!"
        prefix = f"{names[session]}, " if session in names else ""
        if "hour" in lowered or "open" in lowered:
            return prefix + HOURS
        if ticket := re.search(r"\b(\d{4})\b", text):
            number = ticket.group(1)
            status = REPAIRS.get(number)
            if status:
                return f"{prefix}repair #{number} is {status}."
            return f"{prefix}I can't find repair #{number} - could you check the number?"
        if has_media:
            return "Thanks for the photo - a mechanic will take a look and text you back."
        return (prefix + "I can check a repair (send the 4-digit ticket number) or tell you "
                "our opening hours.")

    async def agent(request: web.Request) -> web.Response:
        if token:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return web.json_response({"error": "unauthorized"}, status=401)
        payload = await request.json()
        reply = answer(payload["session_id"], payload.get("text") or "",
                       bool(payload.get("media")))
        return web.json_response({"reply": reply})

    app = web.Application()
    app.router.add_post("/agent", agent)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    web.run_app(make_app(os.environ.get("AGENT_WEBHOOK_TOKEN")), host=args.host,
                port=args.port)


if __name__ == "__main__":
    main()
