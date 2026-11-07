"""Record one Kwork inbox health observation and maybe alert the owner.

The inbox cycle itself (``scripts/run_kwork_inbox_monitor_cycle.py``) is not
in this repository. This command is the in-repo hook a scheduler or that
local script can call without sending new-message cards.

Choose exactly one mode:

  python scripts/report_kwork_inbox_health.py --status PAGE_NOT_READY --checked 0 --dry-run
  python scripts/report_kwork_inbox_health.py --status PAGE_NOT_READY --checked 0 --live

``--dry-run`` prints the decision and does not send or write the ledger.
``--live`` uses the existing jobs-bot route. A single not-ready read does
not alert; see ``KWORK_INBOX_NOT_READY_STREAK``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant.automation_alerts import (
    KWORK_ALERT_COOLDOWN_SECONDS,
    KWORK_NOT_READY_STREAK,
    deliver_owner_alert,
    env_int,
    observe_kwork_inbox,
)

DEFAULT_STATE = PROJECT_ROOT / "logs" / "kwork_inbox_ops_alert.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kwork inbox operational health alert")
    parser.add_argument("--status", required=True, help="Cycle status, e.g. PAGE_NOT_READY or CHECKED")
    parser.add_argument("--checked", type=int, default=None, help="How many dialogs were read")
    parser.add_argument("--error", default="", help="Optional failure text; secrets are stripped")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview only; no send, no ledger write")
    mode.add_argument("--live", action="store_true", help="Deliver through the existing owner alert route")
    args = parser.parse_args(argv)

    decision = observe_kwork_inbox(
        status=args.status,
        checked=args.checked,
        error=args.error or None,
        state_path=args.state_file,
        send_fn=deliver_owner_alert if args.live else None,
        dry_run=bool(args.dry_run),
        cooldown_seconds=env_int(
            "KWORK_INBOX_ALERT_COOLDOWN_SECONDS", KWORK_ALERT_COOLDOWN_SECONDS
        ),
        streak_required=env_int("KWORK_INBOX_NOT_READY_STREAK", KWORK_NOT_READY_STREAK),
    )
    public = {
        "action": decision.get("action"),
        "sent": decision.get("sent"),
        "streak": decision.get("streak"),
        "reason": decision.get("reason"),
        "unhealthy": decision.get("unhealthy"),
        "text": decision.get("text") or "",
    }
    print(json.dumps(public, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
