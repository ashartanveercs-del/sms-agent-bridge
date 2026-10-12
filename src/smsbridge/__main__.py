"""Command line.

    sms-agent-bridge [serve] [--env-file .env]     run the webhook server (the default)
    sms-agent-bridge inspect TEXT [--mode MODE]    show how TEXT would be sent (TEXT '-' = stdin)
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

from aiohttp import web

from .config import ConfigError, Settings
from .gsm7 import MODES, SegmentInfo, segment_info
from .outbound import OutboundComposer
from .server import create_app

log = logging.getLogger("smsbridge")


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines, comments and lines without '=' are skipped."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _describe(info: SegmentInfo) -> str:
    return f"{info.encoding}  {_count(info.segments, 'segment')}  {info.units} units"


def _reconfigure(stream: object, **options: str) -> None:
    """Best effort: e.g. a Windows pipe defaults to a legacy code page."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(ValueError, OSError):
            reconfigure(**options)


def inspect(text: str, mode: str) -> int:
    _reconfigure(sys.stdout, errors="backslashreplace")  # never crash printing an emoji
    if text == "-":
        _reconfigure(sys.stdin, encoding="utf-8")
        text = sys.stdin.read()
    before = segment_info(text)
    line = f"{'input':<10}{_describe(before)}"
    if before.offenders:
        names = ", ".join(f"U+{ord(ch):04X} {unicodedata.name(ch, 'UNNAMED')}"
                          for ch in before.offenders[:5])
        line += f"  forced by {names}"
    print(line)

    plan = OutboundComposer(gsm_mode=mode).compose(text)
    if plan.empty:
        print(f"{'outbound':<10}nothing to send")
        return 0
    encodings = "/".join(sorted({part.info.encoding for part in plan.parts}))
    print(f"{'outbound':<10}{encodings}  {_count(plan.segments, 'segment')}  "
          f"{_count(len(plan.parts), 'part')}")
    if plan.removed_lines or plan.redactions or plan.fallback_used:
        print(f"{'sanitiser':<10}dropped {plan.removed_lines} line(s), masked "
              f"{plan.redactions} value(s){', used the fallback reply' * plan.fallback_used}")
    for number, part in enumerate(plan.parts, start=1):
        print(f"\npart {number}/{len(plan.parts)} ({_describe(part.info)}):\n{part.body}")
    return 0


def serve(env_file: Path | None, environ: Mapping[str, str]) -> int:
    env = dict(environ)
    if env_file is not None:
        env = {**read_env_file(env_file), **env}  # real environment variables win
    level = env.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env(env)
    except ConfigError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 2
    if settings.dry_run:
        log.warning("DRY_RUN is on: replies are logged, not sent")
    if not settings.validate_signatures:
        log.warning("VALIDATE_TWILIO_SIGNATURE is off: anyone can post to the webhooks")
    log.info("listening on %s:%d - agent=%s workers=%d webhook=%s", settings.host,
             settings.port, settings.agent_backend, settings.workers, settings.inbound_url)
    web.run_app(create_app(settings), host=settings.host, port=settings.port, print=None)
    return 0


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sms-agent-bridge",
        description="Production SMS front-end for any AI agent.")
    commands = parser.add_subparsers(dest="command")
    serve_cmd = commands.add_parser("serve", help="run the webhook server (default)")
    serve_cmd.add_argument("--env-file", type=Path, help="KEY=VALUE file read before startup")
    inspect_cmd = commands.add_parser("inspect", help="show how a reply would be sent")
    inspect_cmd.add_argument("text", help="the reply text, or '-' to read it from stdin")
    inspect_cmd.add_argument("--mode", choices=MODES, default="lookalikes",
                             help="GSM-7 normalisation mode (default: lookalikes)")
    args = parser.parse_args(argv)

    if args.command == "inspect":
        return inspect(args.text, args.mode)
    env = os.environ if environ is None else environ
    return serve(getattr(args, "env_file", None), env)


if __name__ == "__main__":
    sys.exit(main())
