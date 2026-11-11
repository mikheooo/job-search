"""Production HeadHunter Message & Interview Monitor Cycle.

Executed periodically (every 15-30 minutes) via Task Scheduler 'JobSearch_HH_Message_Monitor'.
- Checks https://hh.ru/applicant/negotiations via Headless Chrome.
- Detects new employer messages, questions, and interview invitations.
- Formulates optimal responses via interview_auto_responder.
- Alerts Mikhail immediately in Telegram (@jobsassistbot) with ready-to-copy drafts.
- Fail-closed: does not autonomously post messages to HH without human action.
- Deduplicated: only alerts once per unique status/message event.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import time
from typing import Any

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant.hh_chat_monitor import process_negotiation_alerts, scan_hh_negotiations

# Setup logging both to stdout and to logs/hh_monitor.log
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "hh_monitor.log"

log_handlers: list[logging.Handler] = [
    logging.FileHandler(LOG_FILE, encoding="utf-8"),
]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("hh_monitor")


def run_monitor_cycle() -> dict[str, Any]:
    """Run single negotiation scan and alert dispatch."""
    logger.info("Starting HeadHunter negotiation & interview monitor cycle...")
    start_time = time.time()

    cards = scan_hh_negotiations()
    alerts_sent = 0
    if cards:
        alerts_sent = process_negotiation_alerts(cards)
        logger.info(f"Scan complete: {len(cards)} negotiations checked, {alerts_sent} new alerts sent.")
    else:
        logger.info("Scan complete: No negotiations found or browser did not return cards.")

    duration = round(time.time() - start_time, 1)
    return {
        "status": "SUCCESS",
        "cards_found": len(cards),
        "alerts_sent": alerts_sent,
        "duration_seconds": duration,
    }


if __name__ == "__main__":
    result = run_monitor_cycle()
    print(json.dumps(result, indent=2, ensure_ascii=False))
