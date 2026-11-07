"""Controlled Pilot Runner: Kwork Inbox Monitor Cycle.

Brought in from the local untracked Aorus15 script and wired to the
operational health ledger. Message cards still go through
``process_monitor_cycle`` (dry-run by default, ``--live`` to send).
``PAGE_NOT_READY`` / ``CDP_UNAVAILABLE`` / ``UNKNOWN`` with nothing read
update a separate persistence alert. ``AUTH_REQUIRED`` / ``AUTH_LOST`` stay
on the monitor's own auth warning and do not touch that ledger.

Provenance and the still-missing ``hh_structured_preflight`` are documented
in ``docs/automation_alerts.md``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import pathlib
import sys
from typing import Any

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:  # noqa: BLE001 - missing dotenv falls back to a manual .env read
    _env_file = PROJECT_ROOT / ".env"
    if _env_file.exists():
        try:
            with open(_env_file, encoding="utf-8", errors="replace") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'").strip('"')
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
        except OSError as exc:
            logging.getLogger("kwork_pilot_cycle").debug(
                "Could not read .env: %s", type(exc).__name__
            )

from ai_assistant.gig.kwork_inbox_monitor import (
    DEFAULT_CDP_URL,
    KworkInboxStorage,
    get_ambiguous_diagnostic_report,
    process_monitor_cycle,
    scan_kwork_inbox_via_cdp,
)

try:
    from ai_assistant.runner import is_pid_running as _is_pid_running
except Exception:  # noqa: BLE001 - a missing pid helper must not block the cycle
    _is_pid_running = None

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "kwork_monitor.log"
MONITOR_LOCK = LOG_DIR / "kwork_monitor.lock"
DISABLED_FLAG = PROJECT_ROOT / "artifacts" / "kwork_monitor.disabled"
KWORK_HEALTH_STATE = LOG_DIR / "kwork_inbox_ops_alert.json"
# None keeps KworkInboxStorage's own default (artifacts/kwork_inbox.db).
INBOX_DB: pathlib.Path | None = None


class FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


log_handlers: list[logging.Handler] = [
    FlushingFileHandler(LOG_FILE, encoding="utf-8"),
]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("kwork_pilot_cycle")


def is_monitor_disabled() -> bool:
    """Check emergency kill-switch."""
    if DISABLED_FLAG.exists():
        return True
    env_val = os.getenv("KWORK_MONITOR_ENABLED", "true").strip().lower()
    return env_val in ("false", "0", "off", "no")


def acquire_monitor_lock() -> bool:
    """Acquire single-instance lock."""
    if MONITOR_LOCK.exists():
        try:
            lines = MONITOR_LOCK.read_text(encoding="utf-8").splitlines()
            pid = int(lines[0].strip()) if lines else 0
        except (OSError, ValueError):
            pid = 0
        if pid and _is_pid_running is not None and _is_pid_running(pid):
            return False
        try:
            MONITOR_LOCK.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        MONITOR_LOCK.write_text(
            f"{os.getpid()}\n{datetime.datetime.now(datetime.timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def release_monitor_lock() -> None:
    try:
        MONITOR_LOCK.unlink(missing_ok=True)
    except OSError:
        pass


def _record_cycle_health(report: Any, *, dry_run: bool) -> dict[str, Any]:
    """Persist operational health from the monitor report. Does not send message cards."""
    from ai_assistant.automation_alerts import (
        KWORK_ALERT_COOLDOWN_SECONDS,
        KWORK_NOT_READY_STREAK,
        deliver_owner_alert,
        env_int,
        kwork_report_should_update_health,
        observe_kwork_inbox,
    )

    status = str(getattr(report, "auth_status", "") or "")
    if not kwork_report_should_update_health(status):
        logger.info(
            "Kwork auth status %s stays on the inbox monitor; operational ledger unchanged",
            status,
        )
        return {
            "action": "skipped_auth",
            "sent": False,
            "streak": None,
            "reason": "monitor_owns_auth_alert",
        }

    checked = int(getattr(report, "scanned_dialogues", 0) or 0)
    error = None
    if status != "OK":
        details = [str(item) for item in (getattr(report, "details", None) or [])]
        error = " | ".join(details[-2:]) or status
    try:
        decision = observe_kwork_inbox(
            status=status,
            checked=checked,
            error=error,
            state_path=KWORK_HEALTH_STATE,
            send_fn=None if dry_run else deliver_owner_alert,
            dry_run=dry_run,
            cooldown_seconds=env_int(
                "KWORK_INBOX_ALERT_COOLDOWN_SECONDS", KWORK_ALERT_COOLDOWN_SECONDS
            ),
            streak_required=env_int("KWORK_INBOX_NOT_READY_STREAK", KWORK_NOT_READY_STREAK),
        )
    except Exception as exc:  # noqa: BLE001 - health bookkeeping must not drop the scan result
        logger.warning("Kwork operational health update failed: %s", type(exc).__name__)
        return {"action": "error", "sent": False, "streak": None, "reason": type(exc).__name__}
    logger.info(
        "Kwork operational health: action=%s sent=%s streak=%s reason=%s dry_run=%s",
        decision.get("action"),
        decision.get("sent"),
        decision.get("streak"),
        decision.get("reason"),
        dry_run,
    )
    return {
        "action": decision.get("action"),
        "sent": bool(decision.get("sent")),
        "streak": decision.get("streak"),
        "reason": decision.get("reason"),
    }


def run_cycle(dry_run: bool = True, send_one: bool = False, cdp_url: str = DEFAULT_CDP_URL) -> dict[str, Any]:
    if is_monitor_disabled():
        logger.warning("Kwork monitor is DISABLED via kill-switch (artifacts/kwork_monitor.disabled or env). Aborting.")
        return {"status": "DISABLED"}

    if not acquire_monitor_lock():
        logger.warning("Another Kwork monitor instance is currently running. Skipping cycle.")
        return {"status": "LOCKED"}

    try:
        storage = KworkInboxStorage() if INBOX_DB is None else KworkInboxStorage(INBOX_DB)

        diag = get_ambiguous_diagnostic_report(storage)
        if diag["needs_operator_review"]:
            logger.warning("Found %s AMBIGUOUS messages requiring operator review!", diag["ambiguous_count"])

        logger.info("Starting Kwork inbox scan (dry_run=%s, send_one=%s)...", dry_run, send_one)

        def _scan():
            return scan_kwork_inbox_via_cdp(cdp_url=cdp_url)

        report = process_monitor_cycle(
            storage=storage,
            scan_fn=_scan,
            dry_run=dry_run,
            send_one=send_one,
        )

        logger.info(
            "Cycle finished: auth=%s, scanned=%s, new=%s, sent=%s, pending=%s, failed=%s",
            report.auth_status,
            report.scanned_dialogues,
            report.new_detected,
            report.alerts_sent,
            report.alerts_pending,
            report.alerts_failed,
        )
        return {
            "status": "OK",
            "auth": report.auth_status,
            "scanned": report.scanned_dialogues,
            "new": report.new_detected,
            "sent": report.alerts_sent,
            "pending": report.alerts_pending,
            "failed": report.alerts_failed,
            "details": report.details,
            "health_alert": _record_cycle_health(report, dry_run=dry_run),
        }
    finally:
        release_monitor_lock()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Kwork Inbox Monitor Runner")
    parser.add_argument("--live", action="store_true", help="Enable live Telegram alerts (default is safe dry-run)")
    parser.add_argument("--send-one", action="store_true", help="Send at most 1 alert then exit (controlled verification)")
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL, help="CDP endpoint URL")
    args = parser.parse_args(argv)

    is_dry_run = not args.live
    res = run_cycle(dry_run=is_dry_run, send_one=args.send_one, cdp_url=args.cdp_url)
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
