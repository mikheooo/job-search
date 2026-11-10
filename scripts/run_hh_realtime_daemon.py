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
6. Auto-recovery: restarts the browser session if the connection or page drops.
   Restarts back off exponentially (HH_REALTIME_BROWSER_RECOVERY_BASE_SECONDS,
   default 5s) up to a cap (HH_REALTIME_BROWSER_RECOVERY_MAX_SECONDS, default
   300s) so a wedged CDP on port 9111 is not relaunched in a tight loop. A
   successful chat check resets the streak. The watchdog stays armed.
7. Periodic (10m) negotiation cards scan to capture interview invitations without chat.
8. Observable autopilot pause: PAUSED/RESUMED transitions are logged once with the
   lock owner PID/timestamp, and a heartbeat (log line + logs/hh_realtime_daemon.heartbeat
   JSON) is refreshed at least every HH_REALTIME_HEARTBEAT_INTERVAL seconds (60s) in
   both RUNNING and PAUSED states, so a silent stall is visible from outside.
   A failed dialog-list read updates that heartbeat immediately (status ERROR plus
   the last error). It does not count as a successful check and does not clear
   the stall clock.
9. Watchdog: a pause (or a streak without any successful chat check) longer than
   HH_REALTIME_PAUSE_ALERT_SECONDS (default 1800s) raises one owner alert through the
   existing safe Telegram helper, repeating no more often than the cooldown
   (HH_REALTIME_PAUSE_ALERT_COOLDOWN_SECONDS, default 3600s). The stall alert
   includes the last dialog-list error and how old that error is. The watchdog
   never touches the browser, the session or the autopilot lock, and it is not
   disabled while the browser is backing off.
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
LOG_FILE = LOG_DIR / "hh_realtime_daemon.log"

DAEMON_LOCK = LOG_DIR / "hh_realtime_daemon.lock"
AUTOPILOT_LOCK = LOG_DIR / "autopilot.lock"
HEARTBEAT_FILE = LOG_DIR / "hh_realtime_daemon.heartbeat"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


POLL_INTERVAL_SECONDS = int(os.environ.get("HH_REALTIME_POLL_INTERVAL", "30"))
NEGOTIATIONS_SCAN_INTERVAL_SECONDS = 600  # Every 10 minutes

# Observability: while paused we re-check the autopilot lock every 10s; a heartbeat
# is written/logged at most once per interval (never less often than 60s).
PAUSE_RECHECK_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = float(_env_int("HH_REALTIME_HEARTBEAT_INTERVAL", 60))
HEARTBEAT_INTERVAL_SECONDS = max(1.0, HEARTBEAT_INTERVAL_SECONDS)
PAUSE_ALERT_SECONDS = _env_int("HH_REALTIME_PAUSE_ALERT_SECONDS", 1800)
PAUSE_ALERT_COOLDOWN_SECONDS = _env_int("HH_REALTIME_PAUSE_ALERT_COOLDOWN_SECONDS", 3600)
AUTH_OUTAGE_RETRY_SECONDS = _env_int("HH_REALTIME_AUTH_RETRY_SECONDS", 300)
AUTH_ALERT_SUPPRESS_SECONDS = 12 * 3600
# Browser recycle after a hung CDP / dialog-list timeout. failure 1 waits `base`,
# then 2*base, 4*base, ... until `max`. The cap stops a dead port 9111 from being
# restarted every few seconds; the watchdog is a separate clock and stays on.
BROWSER_RECOVERY_BASE_SECONDS = float(_env_int("HH_REALTIME_BROWSER_RECOVERY_BASE_SECONDS", 5))
BROWSER_RECOVERY_MAX_SECONDS = max(
    BROWSER_RECOVERY_BASE_SECONDS,
    float(_env_int("HH_REALTIME_BROWSER_RECOVERY_MAX_SECONDS", 300)),
)

# Substrings that mean the DevTools target is gone or hung, so recycling the
# browser can help. Matched against the lowercased check reason.
_BROWSER_RECYCLE_HINTS = (
    "tab is gone",
    "connection closed",
    "connection refused",
    "connection reset",
    "connectionreset",
    "connection failed",
    "broken pipe",
    "brokenpipe",
    "target closed",
    "target disconnected",
    "10061",
    "no browser transport",
    "failed to fetch",
    "timeout",
    "timed out",
)


class FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logger = logging.getLogger("hh_realtime_daemon")

_running = True
_logging_configured = False


def _configure_logging() -> None:
    """Configure file+stdout logging lazily so importing this module is side-effect free."""
    global _logging_configured
    if _logging_configured:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e_mkdir:
        logger.debug("Could not create log directory %s: %s", LOG_DIR, e_mkdir)
    handlers: list[logging.Handler] = []
    try:
        handlers.append(FlushingFileHandler(LOG_FILE, encoding="utf-8"))
    except OSError as e_handler:
        logger.debug("Could not open daemon log file %s: %s", LOG_FILE, e_handler)
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    _logging_configured = True


def _signal_handler(signum: int, frame: Any) -> None:
    global _running
    logger.info("Received termination signal (%d). Shutting down daemon gracefully...", signum)
    _running = False


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


def _read_autopilot_lock() -> tuple[int, str]:
    """Return (pid, lock_timestamp_text) recorded in logs/autopilot.lock, or (0, '')."""
    if not AUTOPILOT_LOCK.exists():
        return 0, ""
    try:
        lines = AUTOPILOT_LOCK.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0].strip()) if lines and lines[0].strip() else 0
        lock_ts = lines[1].strip() if len(lines) > 1 else ""
    except Exception:
        return 0, ""
    return pid, lock_ts


def _is_autopilot_active() -> bool:
    pid, _ = _read_autopilot_lock()
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


# ---------------------------------------------------------------------------
# Observability: unified heartbeat (log line + JSON file) and pause watchdog.
#
# Before this, the daemon logged "yielding" once and then went silent for the
# whole autopilot cycle (measured pauses up to 19.4h), so nothing outside the
# process could tell a deliberate pause from a crash. Both states now refresh
# logs/hh_realtime_daemon.heartbeat at least every HEARTBEAT_INTERVAL_SECONDS.
# ---------------------------------------------------------------------------

_state = "RUNNING"
_heartbeat_seq = 0
_last_heartbeat_at = 0.0
_last_success_at = 0.0
_checked_last = 0
_last_status: str | None = None
_last_error: str | None = None
_last_error_at: float | None = None
_browser_recovery_failures = 0
_pause_started_at: float | None = None
_pause_lock_pid: int | None = None
_pause_lock_ts = ""
_last_pause_alert_at: float | None = None
_last_stall_alert_at: float | None = None
# Paused time since the last successful check is excluded from the stall clock,
# while _last_success_at continues to describe the actual check history.
_stall_paused_seconds = 0.0


def _now() -> float:
    return time.time()


def _init_observability() -> None:
    global _state, _heartbeat_seq, _last_heartbeat_at, _last_success_at
    global _checked_last, _last_status, _last_error, _last_error_at
    global _browser_recovery_failures
    global _pause_started_at, _pause_lock_pid
    global _pause_lock_ts, _last_pause_alert_at, _last_stall_alert_at
    global _stall_paused_seconds
    _state = "RUNNING"
    _heartbeat_seq = 0
    _last_heartbeat_at = 0.0
    _last_success_at = _now()
    _checked_last = 0
    _last_status = None
    _last_error = None
    _last_error_at = None
    _browser_recovery_failures = 0
    _pause_started_at = None
    _pause_lock_pid = None
    _pause_lock_ts = ""
    _last_pause_alert_at = None
    _last_stall_alert_at = None
    _stall_paused_seconds = 0.0
    _heartbeat("RUNNING", "daemon started", 0, force=True)


def _heartbeat_payload(
    state: str, reason: str, checked: int, extra: dict[str, Any] | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "epoch": round(_now(), 3),
        "pid": os.getpid(),
        "state": state,
        "reason": reason,
        "checked": int(checked),
        "heartbeat": _heartbeat_seq,
    }
    if extra:
        payload.update(extra)
    return payload


def _write_heartbeat_file(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False)
    tmp = HEARTBEAT_FILE.with_name(HEARTBEAT_FILE.name + ".tmp")
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, HEARTBEAT_FILE)
        return True
    except OSError as e_replace:
        logger.debug("Could not atomically update heartbeat file: %s", e_replace)
        try:
            tmp.unlink(missing_ok=True)
        except OSError as e_cleanup:
            logger.debug("Could not remove stale heartbeat tmp file %s: %s", tmp, e_cleanup)
    try:
        HEARTBEAT_FILE.write_text(text, encoding="utf-8")
        return True
    except OSError as e_write:
        logger.debug("Could not write heartbeat file %s: %s", HEARTBEAT_FILE, e_write)
        return False


def _heartbeat(
    state: str,
    reason: str,
    checked: int = 0,
    *,
    force: bool = False,
    extra: dict[str, Any] | None = None,
) -> int:
    """Refresh the heartbeat (file + counter) if due. Returns heartbeat number or 0."""
    global _last_heartbeat_at, _heartbeat_seq
    now = _now()
    if not force and (now - _last_heartbeat_at) < HEARTBEAT_INTERVAL_SECONDS:
        return 0
    _last_heartbeat_at = now
    _heartbeat_seq += 1
    if not _write_heartbeat_file(_heartbeat_payload(state, reason, checked, extra)):
        return 0
    return _heartbeat_seq


def _pause_reason() -> str:
    pid = _pause_lock_pid if _pause_lock_pid is not None else "?"
    ts = _pause_lock_ts or "unknown"
    return f"autopilot lock held by pid {pid} since {ts}"


def _enter_pause(pid: int | None, lock_ts: str) -> bool:
    """Log the RUNNING->PAUSED transition exactly once. Returns True on transition."""
    global _state, _pause_started_at, _pause_lock_pid, _pause_lock_ts
    if _state == "PAUSED":
        return False
    _state = "PAUSED"
    _pause_started_at = _now()
    _pause_lock_pid = pid if pid else None
    _pause_lock_ts = lock_ts
    logger.info(
        "PAUSED: autopilot cycle active; yielding browser and pausing realtime daemon "
        "(autopilot lock pid=%s, lock ts=%s, pause watchdog threshold=%ds)",
        pid or "?",
        lock_ts or "?",
        PAUSE_ALERT_SECONDS,
    )
    _heartbeat("PAUSED", _pause_reason(), _checked_last, force=True)
    return True


def _leave_pause() -> bool:
    """Log the PAUSED->RUNNING transition exactly once. Returns True on transition."""
    global _state, _pause_started_at, _pause_lock_pid, _pause_lock_ts, _last_pause_alert_at
    global _stall_paused_seconds
    if _state != "PAUSED":
        return False
    duration = _now() - (_pause_started_at if _pause_started_at is not None else _now())
    _stall_paused_seconds += max(0.0, duration)
    pid = _pause_lock_pid
    lock_ts = _pause_lock_ts or "unknown"
    _state = "RUNNING"
    _pause_started_at = None
    _pause_lock_pid = None
    _pause_lock_ts = ""
    # A new pause occurrence gets its own threshold alert (the cooldown only
    # throttles repeats of the same occurrence, not distinct pauses).
    _last_pause_alert_at = None
    _heartbeat(
        "RUNNING",
        "resumed after pause",
        _checked_last,
        force=True,
        extra={"pause_seconds": round(duration, 1)},
    )
    logger.info(
        "RESUMED: autopilot cycle finished; realtime chat checks are back after %.0fs "
        "(was waiting for autopilot PID %s, lock ts %s)",
        duration,
        pid if pid is not None else "?",
        lock_ts,
    )
    return True


def _heartbeat_tick(tick_count: int) -> int:
    """Emit the interval heartbeat for the current state (single source of truth)."""
    if _state == "PAUSED":
        duration = _now() - (_pause_started_at if _pause_started_at is not None else _now())
        hb = _heartbeat(
            "PAUSED",
            _pause_reason(),
            _checked_last,
            extra={
                "pause_seconds": round(duration, 1),
                "tick": tick_count,
                "autopilot_pid": _pause_lock_pid,
            },
        )
        if hb:
            logger.info(
                "Heartbeat #%d: PAUSED waiting for autopilot PID %s for %.0fs "
                "(no chat checks; lock ts %s)",
                hb,
                _pause_lock_pid if _pause_lock_pid is not None else "?",
                duration,
                _pause_lock_ts or "?",
            )
        return hb

    success_age = _now() - _last_success_at if _last_success_at else 0.0
    extra = _running_heartbeat_extra(tick_count, success_age)
    hb = _heartbeat("RUNNING", _running_heartbeat_reason(), _checked_last, extra=extra)
    if hb:
        logger.info(
            "💓 Heartbeat #%d: checked=%d chat(s), status=%s, state=RUNNING, last check %.0fs ago%s",
            hb,
            _checked_last,
            _last_status or "UNKNOWN",
            success_age,
            _last_error_log_suffix(),
        )
    return hb


def _running_heartbeat_reason() -> str:
    if _last_error and (_last_status or "") not in {"CHECKED", ""}:
        return f"chat check failed: {_last_error}"
    return "monitoring chat"


def _last_error_age_seconds() -> float:
    if not _last_error_at:
        return 0.0
    return max(0.0, _now() - _last_error_at)


def _last_error_log_suffix() -> str:
    if not _last_error or (_last_status or "") in {"CHECKED", ""}:
        return ""
    return f", last error {_last_error_age_seconds():.0f}s ago: {_last_error}"


def _running_heartbeat_extra(tick_count: int, success_age: float) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "tick": tick_count,
        "status": _last_status or "UNKNOWN",
        "last_success_age_seconds": round(success_age, 1),
    }
    if _last_error:
        extra["last_error"] = _last_error
        extra["last_error_age_seconds"] = round(_last_error_age_seconds(), 1)
    if _browser_recovery_failures:
        extra["browser_recovery_failures"] = _browser_recovery_failures
        extra["browser_recovery_delay_seconds"] = browser_recovery_delay_seconds(
            _browser_recovery_failures
        )
    return extra


def _mark_check_success(chat_res: dict[str, Any] | None) -> None:
    global _last_success_at, _checked_last, _last_status, _last_stall_alert_at
    global _stall_paused_seconds, _last_error, _last_error_at, _browser_recovery_failures
    _last_success_at = _now()
    # Checks are flowing again: a future stall streak may alert on its own.
    _last_stall_alert_at = None
    _stall_paused_seconds = 0.0
    _last_error = None
    _last_error_at = None
    _browser_recovery_failures = 0
    chat_res = chat_res or {}
    try:
        _checked_last = int(chat_res.get("messages_checked", 0) or 0)
    except (TypeError, ValueError):
        _checked_last = 0
    status = chat_res.get("messages_check_status")
    _last_status = str(status) if status else None


def _mark_check_failure(status: str, reason: str) -> None:
    """Record a failed chat check without moving the stall clock.

    ``_last_success_at`` stays on the last check that actually read the dialog
    list. Heartbeat status must not keep saying CHECKED while every read is
    timing out — that is what hid the CDP 9111 outage from the heartbeat file.
    """
    global _last_status, _last_error, _last_error_at
    cleaned = " ".join(str(reason or "").split())
    if len(cleaned) > 500:
        cleaned = cleaned[:497] + "..."
    _last_status = str(status).strip() if str(status or "").strip() else "FAILED"
    _last_error = cleaned or _last_status
    _last_error_at = _now()
    success_age = _now() - _last_success_at if _last_success_at else 0.0
    _heartbeat(
        "RUNNING",
        f"chat check failed: {_last_error}",
        _checked_last,
        force=True,
        extra={
            "status": _last_status,
            "last_error": _last_error,
            "last_error_age_seconds": 0.0,
            "last_success_age_seconds": round(success_age, 1),
            "browser_recovery_failures": _browser_recovery_failures,
        },
    )
    logger.warning(
        "Chat check failed: status=%s error=%s (last success %.0fs ago; stall watchdog still armed)",
        _last_status,
        _last_error,
        success_age,
    )


def browser_recovery_delay_seconds(
    failure_count: int,
    *,
    base: float | None = None,
    maximum: float | None = None,
) -> float:
    """Seconds to wait before the next CDP browser restart.

    ``failure_count`` is 1-based. The delay doubles each consecutive failure and
    never exceeds ``maximum`` (default HH_REALTIME_BROWSER_RECOVERY_MAX_SECONDS).
    """
    base_s = BROWSER_RECOVERY_BASE_SECONDS if base is None else float(base)
    cap_s = BROWSER_RECOVERY_MAX_SECONDS if maximum is None else float(maximum)
    cap_s = max(cap_s, base_s)
    if failure_count <= 0 or base_s <= 0:
        return 0.0
    # 2**10 * base is already far past any reasonable cap; clamp the shift so a
    # long outage cannot overflow the float math.
    shift = min(int(failure_count) - 1, 10)
    return float(min(cap_s, base_s * (2 ** shift)))


def _note_browser_recovery_failure(cdp_url: str | None = None) -> float:
    """Count one recycle and return how long to sleep before the next restart."""
    global _browser_recovery_failures
    _browser_recovery_failures += 1
    delay = browser_recovery_delay_seconds(_browser_recovery_failures)
    logger.warning(
        "Browser recovery #%d on %s: backing off %.0fs before the next CDP restart "
        "(cap %.0fs). Watchdog is unchanged.",
        _browser_recovery_failures,
        cdp_url or "CDP",
        delay,
        BROWSER_RECOVERY_MAX_SECONDS,
    )
    return delay


def _reason_needs_browser_recycle(reason: str | None) -> bool:
    text = str(reason or "").lower()
    return any(hint in text for hint in _BROWSER_RECYCLE_HINTS)


def _chat_failure_needs_browser_recycle(status: str | None, reason: str | None) -> bool:
    # messages_check_status ERROR is the dialog-list failure contract: the
    # browser target did not produce a list, so recycle it. A raised exception
    # uses ``_reason_needs_browser_recycle`` instead, so a logic bug does not
    # restart Chrome.
    if str(status or "").strip().upper() == "ERROR":
        return True
    return _reason_needs_browser_recycle(reason)


def _stop_browser_session(session: HeadlessBrowserSession | None) -> None:
    if session is None:
        return
    try:
        session.stop()
    except Exception as exc:  # noqa: BLE001 - shutdown must not skip the backoff sleep
        logger.debug("Error stopping browser session during recovery: %s", exc)


def _send_owner_alert(text: str) -> bool:
    """Deliver an owner alert through the existing safe Telegram helper."""
    try:
        from ai_assistant.hh_autopilot import send_owner_alert

        return bool(send_owner_alert(text))
    except Exception as e:
        logger.warning("Watchdog could not deliver owner alert: %s", e)
        return False


def _notify_auth_outage_once(error_text: str) -> bool:
    """Send a single Telegram alert when HH_AUTH_REQUIRED is detected (12h suppression)."""
    marker = LOG_DIR / "hh_auth_alert.sent"
    try:
        if marker.exists():
            age = _now() - marker.stat().st_mtime
            if age < AUTH_ALERT_SUPPRESS_SECONDS:
                return False
    except Exception:
        pass
    try:
        from ai_assistant.auth_notifications import AUTH_FAILURE_MESSAGE

        msg = AUTH_FAILURE_MESSAGE
    except Exception:
        msg = (
            "⚠️ HH Autopilot: подтверждена потеря авторизации (HH_AUTH_REQUIRED).\n"
            "Запустите: python scripts/hh_open_login_window.py"
        )
    if not _send_owner_alert(msg):
        return False
    try:
        marker.write_text(
            f"{datetime.datetime.now().isoformat()}\n{(error_text or '')[:300]}",
            encoding="utf-8",
        )
    except Exception:
        pass
    return True


def _interruptible_sleep(total_seconds: float) -> None:
    """Sleep in 1s increments, waking early on shutdown or autopilot lock."""
    remaining = float(total_seconds)
    while remaining > 0 and _running and not _is_autopilot_active():
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step


def _watchdog_alert_text(condition: str, reason: str, duration: float) -> str:
    last_success_age = _now() - _last_success_at if _last_success_at else 0.0
    lines = [
        f"⚠️ HH Realtime Daemon: {condition}",
        "",
        f"Состояние: {_state}",
        f"Причина: {reason}",
        f"Длится: {duration / 60.0:.1f} мин ({duration:.0f} с)",
        f"Последняя успешная проверка чата: {last_success_age:.0f} с назад",
        f"Статус: {_last_status or 'UNKNOWN'}",
    ]
    if _last_error:
        lines.append(f"Последняя ошибка: {_last_error}")
        lines.append(f"Возраст последней ошибки: {_last_error_age_seconds():.0f} с")
    if _browser_recovery_failures:
        lines.append(
            f"Перезапусков браузера подряд: {_browser_recovery_failures} "
            f"(пауза {browser_recovery_delay_seconds(_browser_recovery_failures):.0f} с, "
            f"потолок {BROWSER_RECOVERY_MAX_SECONDS:.0f} с)"
        )
    lines.append(
        f"Порог алерта: {PAUSE_ALERT_SECONDS} с, кулдаун {PAUSE_ALERT_COOLDOWN_SECONDS} с"
    )
    lines.append(f"PID демона: {os.getpid()}")
    lines.append(
        "Смотри: logs/hh_realtime_daemon.log, logs/hh_realtime_daemon.heartbeat, logs/autopilot.lock"
    )
    return "\n".join(lines)


def _watchdog_check() -> None:
    """Alert the owner once per cooldown if the pause/stall exceeds the threshold."""
    global _last_pause_alert_at, _last_stall_alert_at
    now = _now()
    if _state == "PAUSED":
        duration = now - (_pause_started_at if _pause_started_at is not None else now)
        if duration < PAUSE_ALERT_SECONDS:
            return
        if (
            _last_pause_alert_at is not None
            and now - _last_pause_alert_at < PAUSE_ALERT_COOLDOWN_SECONDS
        ):
            return
        _last_pause_alert_at = now
        reason = _pause_reason()
        logger.warning(
            "Watchdog: realtime daemon has been paused for %.0fs (threshold %ds); alerting owner (%s)",
            duration,
            PAUSE_ALERT_SECONDS,
            reason,
        )
        sent = _send_owner_alert(_watchdog_alert_text("пауза затянулась", reason, duration))
        logger.warning("Watchdog pause alert delivery: %s", "ok" if sent else "FAILED")
        return

    if not _last_success_at:
        return
    gap = max(0.0, now - _last_success_at - _stall_paused_seconds)
    if gap < PAUSE_ALERT_SECONDS:
        return
    if _last_stall_alert_at is not None and now - _last_stall_alert_at < PAUSE_ALERT_COOLDOWN_SECONDS:
        return
    _last_stall_alert_at = now
    reason = "нет успешных проверок в активном режиме (паузы автопилота исключены)"
    logger.warning(
        "Watchdog: no successful chat check for %.0fs of active monitoring "
        "(threshold %ds, status=%s, last error=%s); alerting owner",
        gap,
        PAUSE_ALERT_SECONDS,
        _last_status or "UNKNOWN",
        _last_error or "none",
    )
    sent = _send_owner_alert(_watchdog_alert_text("нет успешных проверок чата", reason, gap))
    logger.warning("Watchdog stall alert delivery: %s", "ok" if sent else "FAILED")


def run_daemon_loop() -> None:
    global _running

    _configure_logging()

    if not acquire_daemon_lock():
        logger.warning("Another instance of HeadHunter Realtime Daemon is already running. Exiting.")
        sys.exit(3)

    logger.info("=======================================================")
    logger.info("   HEADHUNTER REAL-TIME AUTO-REPLY DAEMON STARTED     ")
    logger.info("=======================================================")
    logger.info("Polling Interval:   %ds", POLL_INTERVAL_SECONDS)
    logger.info("Negotiations Scan:  Every %ds", NEGOTIATIONS_SCAN_INTERVAL_SECONDS)
    logger.info("Heartbeat File:     %s (every %.0fs)", HEARTBEAT_FILE, HEARTBEAT_INTERVAL_SECONDS)
    logger.info(
        "Pause Watchdog:     alert after %ds, cooldown %ds",
        PAUSE_ALERT_SECONDS,
        PAUSE_ALERT_COOLDOWN_SECONDS,
    )
    logger.info("PID:                %d", os.getpid())
    logger.info("Log File:           %s", LOG_FILE)
    logger.info("=======================================================")

    _init_observability()

    session: HeadlessBrowserSession | None = None
    last_neg_scan_time = 0.0
    tick_count = 0

    try:
        while _running:
            tick_count += 1
            # 1. Coordinate with HeadHunter Autopilot (observable, watchdog-guarded pause)
            autopilot_pid, autopilot_lock_ts = _read_autopilot_lock()
            autopilot_active = bool(
                autopilot_pid and _is_pid_running is not None and _is_pid_running(autopilot_pid)
            )
            if autopilot_active:
                _enter_pause(autopilot_pid, autopilot_lock_ts)
                _heartbeat_tick(tick_count)
                _watchdog_check()
                time.sleep(PAUSE_RECHECK_SECONDS)
                continue

            _leave_pause()
            _heartbeat_tick(tick_count)
            _watchdog_check()

            # 2. Maintain Headless Chrome session
            if session is None:
                try:
                    logger.info("Starting dedicated headless browser session for real-time monitoring...")
                    session = HeadlessBrowserSession(require_auth=True)
                    info = session.start()
                    if not info.get("ok"):
                        err_detail = (
                            info.get("error")
                            or info.get("message")
                            or info.get("status")
                            or "unknown error"
                        )
                        if info.get("status") == "HH_AUTH_REQUIRED":
                            _last_status = "HH_AUTH_REQUIRED"
                            logger.warning(
                                "Could not start headless session: HH_AUTH_REQUIRED (%s). Backing off for %ds...",
                                err_detail,
                                AUTH_OUTAGE_RETRY_SECONDS,
                            )
                            _heartbeat(
                                "RUNNING",
                                "HH_AUTH_REQUIRED: manual login needed",
                                _checked_last,
                                force=True,
                                extra={"status": "HH_AUTH_REQUIRED"},
                            )
                            _notify_auth_outage_once(str(err_detail))
                            session = None
                            _interruptible_sleep(AUTH_OUTAGE_RETRY_SECONDS)
                            continue
                        logger.warning(
                            "Could not start headless session: %s. Backing off before retry...",
                            err_detail,
                        )
                        session = None
                        _interruptible_sleep(_note_browser_recovery_failure(None))
                        continue
                    session.open_hh_page("https://hh.ru/chat")
                    time.sleep(3)
                    logger.info("Browser session initialized on https://hh.ru/chat (CDP: %s)", session.cdp_url)
                except Exception as e_init:  # noqa: BLE001 - a dead CDP must back off, not kill the daemon
                    logger.warning(
                        "Failed to initialize browser session: %s. Backing off before retry...",
                        e_init,
                    )
                    session = None
                    _interruptible_sleep(_note_browser_recovery_failure(None))
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

                reason = str(chat_res.get("messages_check_reason") or "")
                if _chat_failure_needs_browser_recycle(str(status or ""), reason):
                    cdp_url = getattr(session, "cdp_url", None)
                    logger.warning(
                        "Browser check error: %s (reason: %s); recycling session...",
                        status,
                        reason,
                    )
                    # Count the recycle before the heartbeat write so the file
                    # shows this attempt, not the previous one.
                    delay = _note_browser_recovery_failure(cdp_url)
                    _mark_check_failure(str(status or "ERROR"), reason or f"chat check status {status}")
                    _stop_browser_session(session)
                    session = None
                    _interruptible_sleep(delay)
                    continue

                if replies_sent > 0 or interviews > 0:
                    logger.info(
                        "⚡ Real-time event processed: replies_sent=%d, interviews=%d, status=%s",
                        replies_sent,
                        interviews,
                        status,
                    )
                elif status != "CHECKED":
                    logger.debug("Chat check status: %s (reason: %s)", status, chat_res.get("messages_check_reason"))

                # Successful check: feed the unified heartbeat + stall watchdog.
                _mark_check_success(chat_res)

            except Exception as e_chat:  # noqa: BLE001 - one bad tick must not kill the daemon
                reason = f"{type(e_chat).__name__}: {e_chat}"
                logger.warning("Error processing live chat messages: %s", reason)
                # Status ERROR is recorded either way. Only a hung or dropped
                # target restarts Chrome; a logic error retries on the same session.
                if _reason_needs_browser_recycle(reason):
                    cdp_url = getattr(session, "cdp_url", None)
                    logger.info("Browser connection lost; recycling session with backoff...")
                    delay = _note_browser_recovery_failure(cdp_url)
                    _mark_check_failure("ERROR", reason)
                    _stop_browser_session(session)
                    session = None
                    _interruptible_sleep(delay)
                    continue
                _mark_check_failure("ERROR", reason)

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
            _interruptible_sleep(POLL_INTERVAL_SECONDS)

    finally:
        logger.info("HeadHunter Realtime Daemon stopping...")
        if session:
            try:
                session.close_all_owned_tabs()
            except Exception as e_final:
                logger.debug("Error closing session tabs at exit: %s", e_final)
        release_daemon_lock()
        logger.info("HeadHunter Realtime Daemon stopped cleanly.")


if __name__ == "__main__":
    _install_signal_handlers()
    run_daemon_loop()
