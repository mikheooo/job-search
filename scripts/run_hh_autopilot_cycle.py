"""Autonomous HeadHunter Autopilot Cycle Runner.

Designed to be executed periodically (e.g. 2-4 times a day via Windows Task Scheduler)
or run on demand.
- Bumps published resumes on HH to stay at the top of employer searches.
- Discovers remote vacancies across target directions.
- Applies candidate profile gates (100% remote, no tests, salary floor).
- Tailors cover letters.
- Submits applications via headless CDP.
- Reconciles to state.db and sends rich alerts to Telegram (@jobsassistbot).
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import pathlib
import sys
import time

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "hh_autopilot.log"


class FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after every log message for real-time monitoring."""

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
logger = logging.getLogger("hh_autopilot_runner")

LOCK_FILE = LOG_DIR / "autopilot.lock"

# Phase 4A.2: one informative owner alert per auth outage (not per failed
# cycle). Marker file suppresses repeats within the window so a 4-hour
# schedule cannot spam while the session is down.
AUTH_ALERT_MARKER = LOG_DIR / "hh_auth_alert.sent"
AUTH_ALERT_SUPPRESS_SECONDS = 12 * 3600

_AUTH_FAILURE_NEEDLES = (
    "HH_AUTH_REQUIRED",
    "not usable",
    "Cannot start browser session",
    "account/login",
    "hhrole=anonymous",
)


def _maybe_notify_auth_failure(error_text: str) -> bool:
    """Send a single auth-outage alert; True if an alert was sent now."""
    lowered = (error_text or "").lower()
    if not any(n.lower() in lowered for n in _AUTH_FAILURE_NEEDLES):
        return False
    try:
        if AUTH_ALERT_MARKER.exists():
            age = time.time() - AUTH_ALERT_MARKER.stat().st_mtime
            if age < AUTH_ALERT_SUPPRESS_SECONDS:
                logger.info("Auth alert suppressed (last sent %.1fh ago).", age / 3600)
                return False
    except Exception:
        pass
    try:
        from ai_assistant.hh_autopilot import send_telegram_alert

        send_telegram_alert(
            "🔐 <b>HH Autopilot: нужна авторизация.</b>\n\n"
            "Цикл остановлен ДО подачи откликов (лимиты и очередь не затронуты).\n"
            "Действие: вручную войти в HH в профиле JobSearchHeadless "
            "(скрипт <code>artifacts\\headless-poc\\hh-login-once.cmd</code>), "
            "затем закрыть окно. Следующий цикл подхватит сессию сам."
        )
        AUTH_ALERT_MARKER.write_text(
            f"{datetime.datetime.now().isoformat()}\n{error_text[:300]}",
            encoding="utf-8",
        )
        return True
    except Exception as e:
        logger.warning(f"Auth alert delivery failed: {e}")
        return False

# Phase 3.2: a live owner process must never lose the lock to a newcomer
# merely because the lock file is old. Previously any lock older than 20
# minutes was unlinked unconditionally, so a hung-but-alive cycle could end
# up running in parallel with the next one (doubling submit limits).
# Now the recorded PID is checked for liveness first; mtime is diagnostic.
try:
    from ai_assistant.runner import is_pid_running as _is_pid_running
except Exception:  # pragma: no cover - import-time fallback
    _is_pid_running = None


def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            lines = LOCK_FILE.read_text(encoding="utf-8").splitlines()
            locked_pid = int(lines[0].strip()) if lines else 0
        except Exception:
            locked_pid = 0
        try:
            mtime = LOCK_FILE.stat().st_mtime
        except Exception:
            mtime = 0.0
        owner_alive = bool(locked_pid) and _is_pid_running is not None and _is_pid_running(locked_pid)
        if owner_alive:
            # Live cycle holds the lock no matter how old the file is.
            return False
        try:
            if mtime and time.time() - mtime <= 1200 and locked_pid and _is_pid_running is None:
                # No liveness oracle available: fail closed, keep the lock.
                return False
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            return False
    try:
        LOCK_FILE.write_text(f"{os.getpid()}\n{datetime.datetime.now().isoformat()}", encoding="utf-8")
        return True
    except Exception:
        return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="HeadHunter Autonomous Application Cycle Runner")
    parser.add_argument("--max-submits", type=int, default=8, help="Max applications to submit in one cycle (default 8)")
    parser.add_argument("--read-limit", type=int, default=45, help="Number of vacancies to inspect (default 45)")
    parser.add_argument("--query", action="append", default=None, help="Target search query (repeatable)")
    args = parser.parse_args()

    if not acquire_lock():
        logger.info("Another autopilot cycle is already running or lock is held. Exiting.")
        return 0

    try:
        # 1. Auto-bump resume to top of HH search
        try:
            from ai_assistant.hh_resume_bumper import bump_hh_resumes
            bump_res = bump_hh_resumes()
            logger.info(f"HH resume auto-bump result: {bump_res}")
        except Exception as be:
            logger.warning(f"Resume auto-bump failed: {be}")

        # 2. Run application cycle
        from ai_assistant.hh_autopilot import run_autopilot_cycle
        logger.info(f"Starting scheduled autopilot run (max_submits={args.max_submits})...")
        res = run_autopilot_cycle(
            max_submits=args.max_submits,
            queries=args.query,
            read_limit=args.read_limit,
        )
        logger.info(f"Autopilot cycle finished with result: {res}")

        # 3. Auto-reply to incoming chat messages via autonomous agent.
        # The agent handles all safety gates (allowlist, kill switch, DOM
        # verification of the sent message). Drafts are always recorded; sends
        # happen only when HH_AUTONOMOUS_REPLY_ENABLED=true is set in .env.
        # Failures here are logged but never block the application cycle.
        try:
            from ai_assistant.hh_autonomous_agent import (
                AutonomousConfig,
                AutonomousJobAgent,
            )
            from ai_assistant.autonomous_mode import FULL
            from ai_assistant import hh_browser_lease as _lease

            logger.info("Starting message-agent cycle (auto-reply to incoming HH chats)...")
            with _lease.hh_operation(
                _lease.OWNER_AUTONOMOUS,
                purpose="message_processing",
                session_kwargs={"require_auth": False},
            ) as op:
                if op.get("session"):
                    try:
                        op["session"].start()
                        from ai_assistant.hh_browser_launcher import DEFAULT_HH_URL
                        op["session"].open_hh_page(DEFAULT_HH_URL)
                    except Exception as b_err:
                        logger.debug("Session start/open in message cycle: %s", b_err)
                msg_cfg = AutonomousConfig(mode=FULL, auto_start_browser=True)
                msg_agent = AutonomousJobAgent(config=msg_cfg)
                msg_res = msg_agent._process_messages()
            logger.info(
                "Message-agent cycle: checked=%d, status=%s, reason=%s, "
                "auto_replies=%d, interviews=%d, rejections=%d",
                msg_res.get("messages_checked", 0),
                msg_res.get("messages_check_status", ""),
                msg_res.get("messages_check_reason", ""),
                msg_res.get("auto_replies_count", 0),
                msg_res.get("interviews_detected", 0),
                msg_res.get("rejections_count", 0),
            )
        except Exception as me:
            logger.warning(f"Message-agent cycle failed (non-fatal): {me}")

        # Phase 4A.2: an ERROR result must not masquerade as success (exit 0).
        # Auth outages additionally trigger the single-suppressed alert.
        if isinstance(res, dict) and res.get("status") == "ERROR":
            _maybe_notify_auth_failure(str(res.get("error") or ""))
            return 1
        return 0
    except Exception as e:
        logger.error(f"Autopilot cycle failed with unhandled exception: {e}", exc_info=True)
        _maybe_notify_auth_failure(str(e))
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
