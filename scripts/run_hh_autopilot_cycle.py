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
AUTH_RECHECK_DELAY_SECONDS = 12.0

# Non-auth cycle failures (import errors, transport, disk) use a separate
# ledger so they reach the same jobs-bot route without looking like a login
# loss. Default cooldown is 6h: a 4-hour schedule cannot repeat the same page.
OPS_ALERT_STATE = LOG_DIR / "hh_autopilot_ops_alert.json"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


OPS_ALERT_COOLDOWN_SECONDS = _env_int("HH_AUTOPILOT_FAILURE_ALERT_COOLDOWN_SECONDS", 6 * 3600)

_AUTH_FAILURE_NEEDLES = (
    "HH_AUTH_REQUIRED",
    "account/login",
    "hhrole=anonymous",
    "the HH login card is rendered",
)


def _auth_error_is_suspicious(error_text: str) -> bool:
    """Return True only for text that claims an authentication failure.

    Browser-start failures, profile-lock conflicts, transport errors and plain
    "session is not usable" messages are deliberately absent: they are
    transient infrastructure problems, not proof that the HH session expired.
    """
    lowered = (error_text or "").lower()
    return any(n.lower() in lowered for n in _AUTH_FAILURE_NEEDLES)


def _probe_hh_auth_via_headless(endpoint: str) -> dict:
    """Fallback one-shot headless check when no live hh.ru tab is open on CDP."""
    try:
        from ai_assistant.hh_browser_provider import (
            HH_AUTH_OK,
            HeadlessBrowserSession,
        )

        session = HeadlessBrowserSession(cdp_url=endpoint, require_auth=True)
        info = session.start()
        if info.get("ok"):
            try:
                if session.started:
                    session.stop()
                else:
                    session.close_all_owned_tabs()
            except Exception:
                pass
            return {
                "status": HH_AUTH_OK,
                "message": "HH session is authenticated",
                "auth": info.get("auth"),
            }
        status = info.get("status") or "UNKNOWN"
        return {
            "status": status,
            "message": str(info.get("message") or info.get("error") or status),
            "auth": info.get("auth"),
        }
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "message": f"HH headless auth probe failed: {type(exc).__name__}: {exc}",
            "auth": None,
        }


def _check_hh_auth_now() -> dict:
    """Read the existing HH tab over CDP, or probe cleanly if the session was closed.

    The realtime daemon owns the profile in normal operation. When a tab is open
    we read it without launching Chrome; if the previous step just tore down the
    headless browser on error, we fall back to a clean one-shot probe so a real
    HH_AUTH_REQUIRED outage is never masked as 'No existing hh.ru tab'.
    """
    from ai_assistant.hh_browser_provider import (
        auth_gate,
        check_hh_authentication,
        list_tabs,
        make_target_evaluate,
    )

    endpoint = os.environ.get("HH_HEADLESS_CDP_URL") or "http://127.0.0.1:9111"
    tabs = None
    try:
        tabs = list_tabs(endpoint, timeout=5.0)
    except Exception:
        tabs = None

    pages = [
        tab
        for tab in (tabs or [])
        if tab.get("type") == "page" and "hh.ru" in (tab.get("url") or "")
    ]
    if not pages:
        return _probe_hh_auth_via_headless(endpoint)

    def _rank(tab: dict) -> tuple[int, str]:
        url = tab.get("url") or ""
        if "/applicant/profile/me" in url:
            return 0, url
        if "/chat" in url:
            return 1, url
        return 2, url

    tab = min(pages, key=_rank)
    try:
        gate = auth_gate(
            check_hh_authentication(make_target_evaluate(endpoint, tab["id"]))
        )
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "message": f"HH auth read failed: {type(exc).__name__}: {exc}",
            "auth": None,
        }
    gate["tab_url"] = tab.get("url")
    return gate


def _confirm_hh_auth_required(error_text: str) -> dict:
    """Re-read the live HH page; report only a verified login-page state."""
    if not _auth_error_is_suspicious(error_text):
        return {
            "status": "SKIPPED",
            "message": "error is not authentication-related",
            "auth": None,
        }

    first = _check_hh_auth_now()
    if first.get("status") == "OK":
        _maybe_notify_auth_recovered()
        return {**first, "confirmation": "recovered_on_first_check"}

    delay = max(0.0, float(AUTH_RECHECK_DELAY_SECONDS))
    if delay:
        logger.info(
            "HH auth suspicion detected; re-checking existing session in %.0fs...",
            delay,
        )
        time.sleep(delay)

    second = _check_hh_auth_now()
    return {
        **second,
        "first_status": first.get("status"),
        "confirmation": "second_check",
    }


def _deliver_owner_alert(text: str) -> bool:
    """Jobs-bot route. Tests replace this; production does not log the body."""
    from ai_assistant.automation_alerts import deliver_owner_alert

    return bool(deliver_owner_alert(text))


def _run_scheduled_autopilot(**kwargs):
    from ai_assistant.hh_autopilot import run_autopilot_cycle

    return run_autopilot_cycle(**kwargs)


def _bump_resumes():
    from ai_assistant.hh_resume_bumper import bump_hh_resumes

    return bump_hh_resumes()


def _run_message_agent() -> None:
    """Best-effort chat pass. Failures here do not change the cycle exit code."""
    from ai_assistant import hh_browser_lease as _lease
    from ai_assistant.autonomous_mode import FULL
    from ai_assistant.hh_autonomous_agent import (
        AutonomousConfig,
        AutonomousJobAgent,
    )

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
            except Exception as b_err:  # noqa: BLE001 - a closed page must not abort the chat pass
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


def _notify_non_auth_failure(error_text: str, submitted: int | None) -> bool:
    from ai_assistant.automation_alerts import (
        notify_hh_non_auth_failure,
        sanitize_alert_text,
    )

    safe = sanitize_alert_text(error_text)
    logger.info(
        "Non-auth HH failure; operational alert (not an auth alert): %s",
        safe[:200],
    )
    decision = notify_hh_non_auth_failure(
        error_text,
        state_path=OPS_ALERT_STATE,
        send_fn=_deliver_owner_alert,
        submitted=submitted,
        cooldown_seconds=OPS_ALERT_COOLDOWN_SECONDS,
    )
    logger.info(
        "Non-auth HH operational alert: action=%s sent=%s streak=%s",
        decision.get("action"),
        decision.get("sent"),
        decision.get("streak"),
    )
    return bool(decision.get("sent"))


def _notify_non_auth_recovery() -> bool:
    from ai_assistant.automation_alerts import notify_hh_non_auth_recovery

    decision = notify_hh_non_auth_recovery(
        state_path=OPS_ALERT_STATE,
        send_fn=_deliver_owner_alert,
        cooldown_seconds=OPS_ALERT_COOLDOWN_SECONDS,
    )
    if decision.get("action") not in {None, "none"}:
        logger.info(
            "Non-auth HH recovery alert: action=%s sent=%s",
            decision.get("action"),
            decision.get("sent"),
        )
    return bool(decision.get("sent"))


def _coerce_submitted(res) -> int | None:
    if not isinstance(res, dict) or res.get("submitted") is None:
        return None
    try:
        return int(res.get("submitted"))
    except (TypeError, ValueError):
        return None


def _handle_auth_failure(error_text: str, submitted: int | None = None) -> bool:
    """Auth loss keeps the verified-login alert. Every other failure pages too."""
    if not _auth_error_is_suspicious(error_text):
        return _notify_non_auth_failure(error_text, submitted)

    confirmation = _confirm_hh_auth_required(error_text)
    status = confirmation.get("status")
    if status == "OK":
        logger.info("HH session authenticated on re-check; auth alert skipped.")
        return False
    if status != "HH_AUTH_REQUIRED":
        logger.warning(
            "HH auth not confirmed (%s): %s",
            status,
            confirmation.get("message") or confirmation.get("reason") or "",
        )
        return False
    return _maybe_notify_auth_failure(error_text)


def _maybe_notify_auth_failure(error_text: str) -> bool:
    """Send a single auth-outage alert; True if an alert was confirmed now."""
    if not _auth_error_is_suspicious(error_text):
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
        from ai_assistant.auth_notifications import AUTH_FAILURE_MESSAGE
    except ImportError:
        AUTH_FAILURE_MESSAGE = (
            "⚠️ HH Autopilot: подтверждена потеря авторизации (HH_AUTH_REQUIRED).\n"
            "Запустите: python scripts/hh_open_login_window.py"
        )

    try:
        sent = bool(_deliver_owner_alert(AUTH_FAILURE_MESSAGE))
    except Exception as e:
        logger.warning("Auth alert delivery failed: %s", type(e).__name__)
        return False
    if not sent:
        logger.warning("Auth alert not confirmed; suppression marker not written.")
        return False
    try:
        from ai_assistant.automation_alerts import sanitize_alert_text

        AUTH_ALERT_MARKER.write_text(
            f"{datetime.datetime.now().isoformat()}\n{sanitize_alert_text(error_text, limit=300)}",
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Auth alert sent but marker write failed: {e}")
    return True


def _maybe_notify_auth_recovered() -> bool:
    """Send one recovery notice and always clear a confirmed stale marker."""
    if not AUTH_ALERT_MARKER.exists():
        return False

    try:
        from ai_assistant.auth_notifications import AUTH_RECOVERED_MESSAGE
    except ImportError:
        AUTH_RECOVERED_MESSAGE = (
            "✅ HH Autopilot: сессия HH снова авторизована."
        )

    sent = False
    try:
        sent = bool(_deliver_owner_alert(AUTH_RECOVERED_MESSAGE))
    except Exception as e:
        logger.warning("Auth recovery alert failed: %s", type(e).__name__)
    finally:
        # A confirmed authenticated session must never leave a 12-hour marker
        # behind, even when the recovery notice itself could not be delivered.
        try:
            AUTH_ALERT_MARKER.unlink()
        except Exception:
            pass
    return sent

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
    parser.add_argument("--max-submits", type=int, default=10, help="Max applications to submit in one cycle (default 10)")
    parser.add_argument("--read-limit", type=int, default=60, help="Number of vacancies to inspect (default 60)")
    parser.add_argument("--query", action="append", default=None, help="Target search query (repeatable)")
    args = parser.parse_args()

    if not acquire_lock():
        logger.info("Another autopilot cycle is already running or lock is held. Exiting.")
        return 0

    try:
        # 1. Auto-bump resume to top of HH search
        try:
            bump_res = _bump_resumes()
            logger.info(f"HH resume auto-bump result: {bump_res}")
        except Exception as be:
            logger.warning(f"Resume auto-bump failed: {be}")

        # 2. Run application cycle
        logger.info(f"Starting scheduled autopilot run (max_submits={args.max_submits})...")
        res = _run_scheduled_autopilot(
            max_submits=args.max_submits,
            queries=args.query,
            read_limit=args.read_limit,
        )
        logger.info(f"Autopilot cycle finished with result: {res}")

        # 3. Auto-reply to incoming chat messages via autonomous agent.
        # Skip if the application cycle already aborted with a suspicious auth error.
        cycle_err = str(res.get("error") or "") if isinstance(res, dict) else ""
        if isinstance(res, dict) and res.get("status") == "ERROR" and _auth_error_is_suspicious(cycle_err):
            logger.warning("Skipping message-agent cycle because autopilot reported auth error: %s", cycle_err[:200])
        else:
            try:
                _run_message_agent()
            except Exception as me:
                logger.warning(f"Message-agent cycle failed (non-fatal): {me}")

        # 4. Multi-source direct outreach (Telegram recruiters, Email with CV, ATS links)
        try:
            from scripts.run_telegram_outreach import dispatch_outreach_batch
            outreach_res = dispatch_outreach_batch(limit=3)
            logger.info(f"Multi-source outreach cycle finished with result: {outreach_res}")
        except Exception as oe:
            logger.warning(f"Multi-source outreach cycle failed (non-fatal): {oe}")

        # An ERROR result must not masquerade as success (exit 0).
        # Confirmed auth loss uses the auth alert. Every other failure uses
        # the operational alert (cooldown + recovery) on the same route.
        if isinstance(res, dict) and res.get("status") == "ERROR":
            _handle_auth_failure(
                str(res.get("error") or ""),
                submitted=_coerce_submitted(res),
            )
            return 1
        _maybe_notify_auth_recovered()
        _notify_non_auth_recovery()
        return 0
    except Exception as e:
        logger.error(f"Autopilot cycle failed with unhandled exception: {e}", exc_info=True)
        _handle_auth_failure(f"{type(e).__name__}: {e}")
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
