"""Read-only runtime health diagnostics (Phase 3, Step 4).

Prints background-job health: scheduler tasks, processes, locks, heartbeats,
DB counters (SELECT only), recent log errors. Never writes, never sends.
Secrets are masked. Safe to run anytime.

Realtime chat daemon: logs/hh_realtime_daemon.heartbeat is read to prove the
daemon is alive, to expose a deliberate autopilot pause, and to flag a pause
that outlived the owner-alert threshold. The status is printed (OK/WARN/FAIL)
but the exit code stays 0, as before — this script is advisory diagnostics,
not a gate.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ai_assistant.runner import is_pid_running, mask_secrets

# Freshness/alert thresholds for the realtime daemon heartbeat. Stale heartbeat
# means the process is dead or hung; a long PAUSED means chat checks are skipped
# while the autopilot (or a stuck lock) holds the browser.
REALTIME_HEARTBEAT_NAME = "hh_realtime_daemon.heartbeat"
REALTIME_HEARTBEAT_STALE_SECONDS = int(
    os.environ.get("HH_REALTIME_HEARTBEAT_STALE_SECONDS", "600")
)
REALTIME_PAUSE_ALERT_SECONDS = int(os.environ.get("HH_REALTIME_PAUSE_ALERT_SECONDS", "1800"))

TASKS = [
    "JobSearchDashboard",
    "JobSearchMultiSourceDiscovery",
    "JobSearchTelegramControlBot",
    "JobSearch_Daily_Digest",
    "JobSearch_HH_Autopilot",
    "JobSearch_HH_Message_Monitor",
]


def _ps(script: str) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return mask_secrets(out.stdout.strip())
    except Exception as e:
        return f"<ps-error: {e}>"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _parse_heartbeat_epoch(data: dict, path: str) -> float | None:
    """Best-effort heartbeat age source: epoch -> ISO ts -> file mtime."""
    epoch = data.get("epoch")
    if isinstance(epoch, (int, float)):
        return float(epoch)
    ts = data.get("ts")
    if isinstance(ts, str):
        try:
            parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.timestamp()
        except Exception:
            pass
    try:
        return os.path.getmtime(path)
    except Exception:
        return None


def check_realtime_daemon(
    project_root: str = PROJECT_ROOT, now: float | None = None
) -> tuple[str, list[str]]:
    """Inspect the realtime daemon heartbeat (read-only).

    Returns (status, report_lines) where status is OK/WARN/FAIL. Never raises
    for missing/unreadable files: every failure is a report line.
    """
    current = time.time() if now is None else now
    path = os.path.join(project_root, "logs", REALTIME_HEARTBEAT_NAME)
    lines: list[str] = []
    if not os.path.exists(path):
        return "WARN", [
            f"realtime-daemon: absent ({REALTIME_HEARTBEAT_NAME} not written; "
            "daemon stopped or running pre-heartbeat code)"
        ]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("heartbeat is not a JSON object")
    except Exception as e:
        return "FAIL", [f"realtime-daemon: unreadable heartbeat ({mask_secrets(str(e))})"]

    state = str(data.get("state") or "?").upper()
    epoch = _parse_heartbeat_epoch(data, path)
    age = None if epoch is None else max(0.0, current - epoch)
    pid = data.get("pid")
    checked = data.get("checked")
    reason = mask_secrets(str(data.get("reason") or ""))
    seq = data.get("heartbeat")
    if age is None:
        age_text = "unknown"
    else:
        age_text = f"{age:.0f}s"

    alert_line = None
    if age is None:
        status = "FAIL"
        detail = "heartbeat timestamp unreadable"
    elif age > REALTIME_HEARTBEAT_STALE_SECONDS:
        status = "FAIL"
        detail = (
            f"heartbeat stale {age:.0f}s > {REALTIME_HEARTBEAT_STALE_SECONDS}s "
            "(daemon is not writing heartbeats: dead or hung)"
        )
    elif state == "PAUSED":
        paused_for = data.get("pause_seconds")
        paused_seconds = float(paused_for) if isinstance(paused_for, (int, float)) else age
        if paused_seconds > REALTIME_PAUSE_ALERT_SECONDS:
            status = "FAIL"
            detail = (
                f"PAUSED for {paused_seconds:.0f}s > {REALTIME_PAUSE_ALERT_SECONDS}s "
                "alert threshold (chat checks skipped)"
            )
            alert_line = (
                f"realtime-daemon-alert: WATCHDOG ALERT DUE: paused {paused_seconds:.0f}s "
                f"(threshold {REALTIME_PAUSE_ALERT_SECONDS}s); owner alert expected from daemon"
            )
        else:
            status = "WARN"
            detail = (
                f"paused {paused_seconds:.0f}s waiting for autopilot "
                f"(alert threshold {REALTIME_PAUSE_ALERT_SECONDS}s)"
            )
    else:
        status = "OK"
        detail = "heartbeat fresh"
    lines.append(
        f"realtime-daemon: {status} state={state} age={age_text} pid={pid} "
        f"checked={checked} hb={seq} reason={reason}"
    )
    lines.append(f"realtime-daemon-detail: {detail}")
    if alert_line:
        lines.append(alert_line)
    return status, lines


def main() -> int:
    section("windows-tasks")
    print(_ps(
        "Get-ScheduledTask | Where-Object { $_.TaskName -like 'JobSearch*' } |"
        " ForEach-Object { $i = $_ | Get-ScheduledTaskInfo;"
        " \"{0} state={1} last={2} result={3} next={4}\" -f"
        " $_.TaskName, $_.State, $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime }"
    ))

    section("processes")
    print(_ps(
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ai_assistant.cli ui*'"
        " -or $_.CommandLine -like '*telegram_control_bot.py*'"
        " -or $_.CommandLine -like '*run_hh_autopilot_cycle*'"
        " -or $_.CommandLine -like '*run_hh_monitor_cycle*'"
        " -or $_.CommandLine -like '*run_hh_realtime_daemon*'"
        " -or $_.CommandLine -like '*run_multi_source_discovery.py*'} |"
        " Select-Object ProcessId, CommandLine | Format-List"
    ))

    section("locks-and-heartbeats")
    logs = os.path.join(PROJECT_ROOT, "logs")
    for name in ("autopilot.lock", "job_search.lock", "job_search_fetcher.lock",
                 "telegram_control_bot.lock"):
        p = os.path.join(logs, name) if not name.startswith("job_search") else \
            os.path.join(logs, "job_search", name) if name == "job_search.lock" else \
            os.path.join(logs, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                pid = data.get("pid", 0)
                alive = is_pid_running(pid) if pid else False
                print(f"{name}: pid={pid} alive={alive} ts={data.get('timestamp', '?')}")
            except Exception:
                print(f"{name}: <unreadable, size={os.path.getsize(p)}>")
        else:
            print(f"{name}: absent")
    hb = os.path.join(logs, "telegram_control_bot.heartbeat")
    if os.path.exists(hb):
        try:
            with open(hb, encoding="utf-8") as f:
                data = json.load(f)
            print(f"control-bot heartbeat: {data}")
        except Exception:
            print("control-bot heartbeat: <unreadable>")
    else:
        print("control-bot heartbeat: absent (bot never completed a poll cycle)")

    section("realtime-daemon-heartbeat")
    try:
        _daemon_status, daemon_lines = check_realtime_daemon(PROJECT_ROOT)
    except Exception as e:
        _daemon_status = "FAIL"
        daemon_lines = [f"realtime-daemon: <error: {mask_secrets(str(e))}>"]
    for line in daemon_lines:
        print(line)

    section("db-counters SELECT-only")
    db_path = os.path.join(PROJECT_ROOT, "state.db")
    try:
        con = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
        cur = con.cursor()
        queries = {
            "vacancies_total": "SELECT COUNT(*) FROM vacancies",
            "vacancies_by_state": "SELECT state, COUNT(*) FROM vacancies GROUP BY state",
            "tracking_by_status": "SELECT status, COUNT(*) FROM application_tracking GROUP BY status",
            "delivery_by_status": "SELECT status, COUNT(*) FROM telegram_delivery_records GROUP BY status",
            "gigs_by_status": "SELECT status, COUNT(*) FROM gigs GROUP BY status",
            "hh_apps_by_state": "SELECT state, COUNT(*) FROM hh_applications GROUP BY state",
            "stuck_attempting": "SELECT COUNT(*) FROM telegram_delivery_records WHERE status IN ('ATTEMPTING','AMBIGUOUS')",
        }
        for label, sql in queries.items():
            print(f"{label}: {mask_secrets(str(cur.execute(sql).fetchall()))}")
        con.close()
    except Exception as e:
        print(f"<db-error: {mask_secrets(str(e))}>")

    section("recent-log-errors last-3-lines-each")
    for rel in ("logs/hh_autopilot.log", "logs/hh_monitor.log",
                "logs/hh_realtime_daemon.log",
                "logs/job_search/job_search_production.log",
                "logs/telegram_control_bot.log"):
        p = os.path.join(PROJECT_ROOT, rel)
        if not os.path.exists(p):
            print(f"{rel}: absent")
            continue
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                f.seek(max(0, size - 4000))
                tail = f.read().decode("utf-8", errors="replace").splitlines()
            errs = [ln for ln in tail if "ERROR" in ln or "Traceback" in ln or "CRITICAL" in ln]
            shown = errs[-3:] if errs else ["<no ERROR lines in tail>"]
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p)).isoformat()
            print(f"{rel} (mtime={mtime}, size={size}):")
            for ln in shown:
                print("   " + mask_secrets(ln[:300]))
        except Exception as e:
            print(f"{rel}: <read-error: {mask_secrets(str(e))}>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
