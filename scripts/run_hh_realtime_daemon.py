"""Production HeadHunter Real-Time Message Watcher & Auto-Reply Daemon.

Runs continuously in the background (30s polling cycle) to provide near-instant
replies to incoming employer messages and questions on HeadHunter.

Key features:
1. Continuous headless Chrome session with live session on https://hh.ru/chat.
2. 30-second heartbeat check for any incoming recruiter questions or messages.
3. Natural human-like response: grounded in candidate_kb.json, no repeated greetings.
4. Auto-reconciled with state.db and immediate push alerts to Telegram (@jobsassistbot).
5. Autopilot lock yielding: cleanly pauses and yields browser access whenever
   'JobSearch_HH_Autopilot' (run_hh_autopilot_cycle.py) runs.
6. Auto-recovery: restarts browser session if connection or page drops.
7. Periodic (10m) negotiation cards scan to capture interview invitations without chat.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import signal
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

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

from ai_assistant.hh_browser_provider import HeadlessBrowserSession
from ai_assistant.hh_autonomous_agent import AutonomousJobAgent, AutonomousConfig
from ai_assistant.hh_chat_monitor import process_negotiation_alerts, scan_hh_negotiations
from ai_assistant import autonomous_mode

try:
    from ai_assistant.runner import is_pid_running as _is_pid_running
except Exception:
    _is_pid_running = None

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "hh_realtime_daemon.log"

DAEMON_LOCK = LOG_DIR / "hh_realtime_daemon.lock"
AUTOPILOT_LOCK = LOG_DIR / "autopilot.lock"

POLL_INTERVAL_SECONDS = int(os.environ.get("HH_REALTIME_POLL_INTERVAL", "30"))
NEGOTIATIONS_SCAN_INTERVAL_SECONDS = 600  # Every 10 minutes


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
logger = logging.getLogger("hh_realtime_daemon")

_running = True


def _signal_handler(signum: int, frame: Any) -> None:
    global _running
    logger.info("Received termination signal (%d). Shutting down daemon gracefully...", signum)
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _is_autopilot_active() -> bool:
    if not AUTOPILOT_LOCK.exists():
        return False
    try:
        lines = AUTOPILOT_LOCK.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0].strip()) if lines else 0
    except Exception:
        pid = 0
    if pid and _is_pid_running is not None and _is_pid_running(pid):
        return True
    return False


def acquire_daemon_lock() -> bool:
    if DAEMON_LOCK.exists():
        try:
            lines = DAEMON_LOCK.read_text(encoding="utf-8").splitlines()
            pid = int(lines[0].strip()) if lines else 0
        except Exception:
            pid = 0
        if pid and _is_pid_running is not None and _is_pid_running(pid):
            return False
        try:
            DAEMON_LOCK.unlink(missing_ok=True)
        except Exception:
            return False
    try:
        DAEMON_LOCK.write_text(f"{os.getpid()}\n{datetime.datetime.now().isoformat()}", encoding="utf-8")
        return True
    except Exception:
        return False


def release_daemon_lock() -> None:
    try:
        DAEMON_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


def run_daemon_loop() -> None:
    global _running

    if not acquire_daemon_lock():
        logger.warning("Another instance of HeadHunter Realtime Daemon is already running. Exiting.")
        sys.exit(3)

    logger.info("=======================================================")
    logger.info("   HEADHUNTER REAL-TIME AUTO-REPLY DAEMON STARTED     ")
    logger.info("=======================================================")
    logger.info("Polling Interval:   %ds", POLL_INTERVAL_SECONDS)
    logger.info("Negotiations Scan:  Every %ds", NEGOTIATIONS_SCAN_INTERVAL_SECONDS)
    logger.info("PID:                %d", os.getpid())
    logger.info("Log File:           %s", LOG_FILE)
    logger.info("=======================================================")

    session: HeadlessBrowserSession | None = None
    last_neg_scan_time = 0.0
    tick_count = 0

    try:
        while _running:
            tick_count += 1
            # 1. Coordinate with HeadHunter Autopilot
            if _is_autopilot_active():
                if session:
                    logger.info("Autopilot cycle active; yielding browser and pausing realtime daemon...")
                    try:
                        session.stop()
                    except Exception as e_stop:
                        logger.debug("Error stopping session during yield: %s", e_stop)
                    session = None
                time.sleep(10)
                continue

            # 2. Maintain Headless Chrome session
            if session is None:
                try:
                    logger.info("Starting dedicated headless browser session for real-time monitoring...")
                    session = HeadlessBrowserSession(require_auth=True)
                    info = session.start()
                    if not info.get("ok"):
                        logger.warning("Could not start headless session: %s. Retrying in 15s...", info.get("error"))
                        session = None
                        time.sleep(15)
                        continue
                    session.open_hh_page("https://hh.ru/chat")
                    time.sleep(3)
                    logger.info("Browser session initialized on https://hh.ru/chat (CDP: %s)", session.cdp_url)
                except Exception as e_init:
                    logger.warning("Failed to initialize browser session: %s. Retrying in 15s...", e_init)
                    session = None
                    time.sleep(15)
                    continue

            # 3. Check live incoming chats
            try:
                agent = AutonomousJobAgent(
                    config=AutonomousConfig(
                        mode=autonomous_mode.FULL,
                        cdp_url=session.cdp_url,
                        auto_start_browser=False,
                        max_auto_replies_per_cycle=3,
                    )
                )
                chat_res = agent._process_messages()
                status = chat_res.get("messages_check_status")
                replies_sent = chat_res.get("auto_replies_count", 0)
                interviews = chat_res.get("interviews_detected", 0)

                if status == "ERROR" or (chat_res.get("messages_check_reason") and any(k in str(chat_res.get("messages_check_reason")).lower() for k in ["tab is gone", "connection closed", "target closed", "10061", "no browser transport", "failed to fetch"])):
                    logger.warning("Browser check error: %s (reason: %s); recycling session...", status, chat_res.get("messages_check_reason"))
                    try:
                        if session:
                            session.stop()
                    except Exception:
                        pass
                    session = None
                    time.sleep(5)
                    continue

                if replies_sent > 0 or interviews > 0:
                    logger.info(
                        "⚡ Real-time event processed: replies_sent=%d, interviews=%d, status=%s",
                        replies_sent,
                        interviews,
                        status,
                    )
                elif tick_count % 10 == 0:
                    logger.info(
                        "💓 Heartbeat #%d: checked=%d chat(s), status=%s, all active dialogs up to date",
                        tick_count,
                        chat_res.get("messages_checked", 0),
                        status,
                    )
                elif status != "CHECKED":
                    logger.debug("Chat check status: %s (reason: %s)", status, chat_res.get("messages_check_reason"))

            except Exception as e_chat:
                logger.warning("Error processing live chat messages: %s", e_chat)
                err_str = str(e_chat).lower()
                if any(k in err_str for k in ["tab is gone", "connection closed", "target closed", "10061"]):
                    logger.info("Browser connection lost; recycling session...")
                    try:
                        if session:
                            session.stop()
                    except Exception:
                        pass
                    session = None
                    time.sleep(5)
                    continue

            # 4. Periodic negotiation scan for interviews/updates without chat
            now = time.time()
            if now - last_neg_scan_time >= NEGOTIATIONS_SCAN_INTERVAL_SECONDS:
                try:
                    logger.info("Running periodic negotiation scan...")
                    cards = scan_hh_negotiations(session=session) if session else scan_hh_negotiations()
                    if cards:
                        alerts = process_negotiation_alerts(cards)
                        if alerts > 0:
                            logger.info("Periodic scan: %d new alerts sent to Telegram.", alerts)
                    last_neg_scan_time = now
                    # Return tab back to chat
                    if session:
                        session.open_hh_page("https://hh.ru/chat")
                except Exception as e_neg:
                    logger.debug("Periodic negotiation scan exception: %s", e_neg)
                    last_neg_scan_time = now

            # 5. Sleep for the next tick
            sleep_remaining = float(POLL_INTERVAL_SECONDS)
            while sleep_remaining > 0 and _running:
                step = min(1.0, sleep_remaining)
                time.sleep(step)
                sleep_remaining -= step

    finally:
        logger.info("HeadHunter Realtime Daemon stopping...")
        if session:
            try:
                session.stop()
            except Exception as e_final:
                logger.debug("Error stopping session at exit: %s", e_final)
        release_daemon_lock()
        logger.info("HeadHunter Realtime Daemon stopped cleanly.")


if __name__ == "__main__":
    run_daemon_loop()
