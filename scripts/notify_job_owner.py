#!/usr/bin/env python
"""Send one idempotent HH/employer event notification to @jobsassistbot.

This is the replacement for ``hermes send --to telegram`` in the HH Chat
Monitor cron. It has no fallback to the main Hermes bot, records the event
before sending, and never retries an ambiguous outcome automatically.

Usage:
    python scripts/notify_job_owner.py --event-key "gmail:<id>" \
        --source hh_chat_monitor --company "..." --role "..." \
        --chat-url "..." < notification.txt
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import telegram_routing
from ai_assistant.job_event_notifier import (
    JobEventNotificationError,
    notify_job_event,
)

EXIT_CODES = {
    "delivered": 0,
    "duplicate": 0,
    "unconfigured": 3,
    "ambiguous": 4,
    "auth_failed": 5,
    "rate_limited": 6,
    "failed": 6,
    "record_failed": 7,
    "skipped": 8,
}


def _read_text(path: str | None) -> str:
    if path:
        return pathlib.Path(path).read_text(encoding="utf-8")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-key", required=True)
    parser.add_argument("--source", default="hh_chat_monitor")
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--chat-url", default="")
    parser.add_argument("--text-file")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        text = _read_text(args.text_file)
    except OSError as exc:
        print(json.dumps({"status": "failed", "detail": f"cannot read text: {exc}"}))
        return EXIT_CODES["failed"]

    if not text.strip():
        print(json.dumps({"status": "failed", "detail": "notification text is empty"}))
        return EXIT_CODES["failed"]

    if args.dry_run:
        try:
            telegram_routing.resolve_job_route()
        except telegram_routing.TelegramRoutingError as exc:
            print(json.dumps({"status": "unconfigured", "detail": str(exc)}))
            return EXIT_CODES["unconfigured"]
        print(json.dumps({"status": "dry_run", "chars": len(text)}))
        return 0

    try:
        result = notify_job_event(
            args.event_key,
            text,
            source=args.source,
            company=args.company,
            role=args.role,
            chat_url=args.chat_url,
            retry_failed=args.retry_failed,
        )
    except JobEventNotificationError as exc:
        print(json.dumps({"status": "failed", "detail": str(exc)}))
        return EXIT_CODES["failed"]

    print(
        json.dumps(
            {
                "status": result.status,
                "detail": result.detail,
                "message_id": result.message_id,
                "delivery_key": result.delivery_key,
                "record_status": result.record_status,
            },
            ensure_ascii=False,
        )
    )
    return EXIT_CODES.get(result.status, EXIT_CODES["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
