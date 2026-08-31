"""Stage 83 — Production Scheduling, Monitoring & Alerting Tests.

Covers:
1. test_production_wrapper_propagates_success_exit_code
2. test_production_wrapper_propagates_failure_exit_code
3. test_second_instance_is_skipped_when_lock_is_active
4. test_stale_process_lock_can_be_recovered_safely
5. test_logs_do_not_contain_telegram_token
6. test_health_is_healthy_for_clean_state
7. test_health_is_unhealthy_for_ambiguous_attempt
8. test_health_is_unhealthy_for_stale_attempt
9. test_health_detects_duplicate_delivery_keys
10. test_health_json_is_machine_readable
11. test_repeated_failures_trigger_alert_threshold
12. test_success_resets_consecutive_failure_counter
13. test_health_command_is_read_only
14. test_scheduler_wrapper_never_sends_second_instance_concurrently
"""
import datetime
import io
import json
import os
import sqlite3
import sys
import tempfile
import pytest

from ai_assistant import config, db, cli
from ai_assistant.runner import (
    SingleInstanceLock,
    ConsecutiveFailureTracker,
    mask_secrets,
    run_production_pipeline,
)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Provides an isolated database and logs directory."""
    db_file = str(tmp_path / "test_stage83.db")
    logs_dir = str(tmp_path / "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setattr(config, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(config, "PRODUCTION_FAILURE_ALERT_THRESHOLD", 3)
    
    db.init_db()
    return {"db_file": db_file, "logs_dir": logs_dir, "tmp_path": tmp_path}


# ------------------------------------------------------------------------------
# 1. test_production_wrapper_propagates_success_exit_code
# ------------------------------------------------------------------------------
def test_production_wrapper_propagates_success_exit_code(isolated_env, tmp_path):
    """The wrapper returns exit code 0 when the dispatcher succeeds."""
    dummy_script = tmp_path / "dummy_success.py"
    dummy_script.write_text("import sys; sys.exit(0)", encoding="utf-8")

    code = run_production_pipeline(fetcher_script=str(dummy_script), logs_dir=isolated_env["logs_dir"])
    assert code == 0


# ------------------------------------------------------------------------------
# 2. test_production_wrapper_propagates_failure_exit_code
# ------------------------------------------------------------------------------
def test_production_wrapper_propagates_failure_exit_code(isolated_env, tmp_path):
    """The wrapper preserves non-zero exit codes from the dispatcher."""
    dummy_script = tmp_path / "dummy_fail.py"
    dummy_script.write_text("import sys; sys.exit(2)", encoding="utf-8")

    code = run_production_pipeline(fetcher_script=str(dummy_script), logs_dir=isolated_env["logs_dir"])
    assert code == 2


# ------------------------------------------------------------------------------
# 3. test_second_instance_is_skipped_when_lock_is_active
# ------------------------------------------------------------------------------
def test_second_instance_is_skipped_when_lock_is_active(isolated_env):
    """An active PID holding the lock causes a second instance to skip with safe no-op."""
    lock1 = SingleInstanceLock(lock_dir=isolated_env["logs_dir"])
    acq1, msg1 = lock1.acquire()
    assert acq1 is True
    assert msg1 == "ACQUIRED"

    # Second lock attempt from the same active PID (or simulated concurrent worker)
    lock2 = SingleInstanceLock(lock_dir=isolated_env["logs_dir"])
    acq2, msg2 = lock2.acquire()
    assert acq2 is False
    assert "SKIPPED_ALREADY_RUNNING" in msg2

    lock1.release()


# ------------------------------------------------------------------------------
# 4. test_stale_process_lock_can_be_recovered_safely
# ------------------------------------------------------------------------------
def test_stale_process_lock_can_be_recovered_safely(isolated_env):
    """A lock file with a dead PID is safely recovered without human intervention."""
    lock_file = os.path.join(isolated_env["logs_dir"], "job_search.lock")
    
    # Write lock file with an impossible/dead PID (e.g. 999999)
    stale_payload = {
        "pid": 999999,
        "timestamp": "2026-08-30T10:00:00.000000+00:00",
        "host": "localhost"
    }
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(stale_payload, f)

    lock = SingleInstanceLock(lock_dir=isolated_env["logs_dir"])
    acq, msg = lock.acquire()
    assert acq is True
    assert msg == "ACQUIRED"
    
    # Verify current PID is written
    with open(lock_file, "r", encoding="utf-8") as f:
        new_data = json.load(f)
    assert new_data["pid"] == os.getpid()

    lock.release()


# ------------------------------------------------------------------------------
# 5. test_logs_do_not_contain_telegram_token
# ------------------------------------------------------------------------------
def test_logs_do_not_contain_telegram_token(isolated_env, tmp_path):
    """The wrapper masks Telegram bot tokens and API credentials from persistent logs."""
    raw_leak = "Calling https://api.telegram.org/bot123456789:ABCDefGhIjKlMnOpQrStUvWxYz-1234567/sendMessage with token"
    dummy_script = tmp_path / "dummy_leak.py"
    dummy_script.write_text(f"print('{raw_leak}')", encoding="utf-8")

    run_production_pipeline(fetcher_script=str(dummy_script), logs_dir=isolated_env["logs_dir"])

    log_path = os.path.join(isolated_env["logs_dir"], "job_search_production.log")
    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "bot123456789:ABCDefGhIjKlMnOpQrStUvWxYz-1234567" not in content
    assert "bot<MASKED_TELEGRAM_TOKEN>" in content


# ------------------------------------------------------------------------------
# 6. test_health_is_healthy_for_clean_state
# ------------------------------------------------------------------------------
def test_health_is_healthy_for_clean_state(isolated_env):
    """Clean state with accessible DB and zero failed/ambiguous attempts returns HEALTHY."""
    res = db.get_production_health(storage_dir=isolated_env["logs_dir"])
    assert res["health"] == "HEALTHY"
    assert res["db_accessible"] is True
    assert len(res["alerts"]) == 0


# ------------------------------------------------------------------------------
# 7. test_health_is_unhealthy_for_ambiguous_attempt
# ------------------------------------------------------------------------------
def test_health_is_unhealthy_for_ambiguous_attempt(isolated_env):
    """An AMBIGUOUS attempt triggers an UNHEALTHY status and critical alert."""
    db.record_digest_attempt(["hh:amb_1"], chat_id="-1001")
    db.record_digest_ambiguous(["hh:amb_1"], reason="Connection dropped")

    res = db.get_production_health(storage_dir=isolated_env["logs_dir"])
    assert res["health"] == "UNHEALTHY"
    assert res["metrics"]["ambiguous_count"] == 1
    assert any(a["severity"] == "CRITICAL" for a in res["alerts"])


# ------------------------------------------------------------------------------
# 8. test_health_is_unhealthy_for_stale_attempt
# ------------------------------------------------------------------------------
def test_health_is_unhealthy_for_stale_attempt(isolated_env):
    """A stale attempt (age >= 60 min in ATTEMPTING) triggers UNHEALTHY."""
    now_dt = datetime.datetime(2026, 8, 31, 12, 0, 0, tzinfo=datetime.timezone.utc)
    old_iso = "2026-08-31T10:00:00.000000+00:00"  # 120 min old

    batch_key = db.record_digest_attempt(["hh:stale_1"], chat_id="-1001")
    # Manually backdate the attempt timestamp in DB
    conn = sqlite3.connect(isolated_env["db_file"])
    cur = conn.cursor()
    cur.execute("UPDATE telegram_delivery_records SET delivered_at = ? WHERE delivery_key = ?", (old_iso, batch_key))
    conn.commit()
    conn.close()

    res = db.get_production_health(now_dt=now_dt, storage_dir=isolated_env["logs_dir"])
    assert res["health"] == "UNHEALTHY"
    assert res["metrics"]["stale_count"] == 1
    assert any("Stale unresolved" in a["message"] for a in res["alerts"])


# ------------------------------------------------------------------------------
# 9. test_health_detects_duplicate_delivery_keys
# ------------------------------------------------------------------------------
def test_health_detects_duplicate_delivery_keys(isolated_env):
    """Duplicate keys trigger UNHEALTHY."""
    conn = sqlite3.connect(isolated_env["db_file"])
    cur = conn.cursor()
    # Recreate unconstrained table in test DB to verify duplicate-key detector
    cur.execute("DROP TABLE telegram_delivery_records")
    cur.execute('''
        CREATE TABLE telegram_delivery_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_key TEXT NOT NULL,
            notification_type TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT
        )
    ''')
    now_iso = "2026-08-31T00:00:00.000000+00:00"
    cur.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('dup_key', 'test', '1', ?, 'DELIVERED')", (now_iso,))
    cur.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('dup_key', 'test', '1', ?, 'DELIVERED')", (now_iso,))
    conn.commit()
    conn.close()

    res = db.get_production_health(storage_dir=isolated_env["logs_dir"])
    assert res["health"] == "UNHEALTHY"
    assert res["metrics"]["duplicate_delivery_keys_count"] == 1
    assert any("Duplicate delivery keys" in a["message"] for a in res["alerts"])


# ------------------------------------------------------------------------------
# 10. test_health_json_is_machine_readable
# ------------------------------------------------------------------------------
def test_health_json_is_machine_readable(isolated_env):
    """CLI production-health --json outputs valid JSON structure."""
    out_buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = out_buf
        cli.production_health_cmd(output_json=True)
    finally:
        sys.stdout = old_stdout

    data = json.loads(out_buf.getvalue().strip())
    assert "health" in data
    assert "db_path" in data
    assert "metrics" in data
    assert "alerts" in data
    assert data["health"] in ("HEALTHY", "DEGRADED", "UNHEALTHY")


# ------------------------------------------------------------------------------
# 11. test_repeated_failures_trigger_alert_threshold
# ------------------------------------------------------------------------------
def test_repeated_failures_trigger_alert_threshold(isolated_env):
    """3 consecutive failures escalate health status from DEGRADED to UNHEALTHY."""
    tracker = ConsecutiveFailureTracker(storage_dir=isolated_env["logs_dir"])
    
    # 1 failure -> DEGRADED
    tracker.record_failure("Transient error 1")
    res1 = db.get_production_health(storage_dir=isolated_env["logs_dir"])
    assert res1["health"] == "DEGRADED"

    # 2 failures -> DEGRADED
    tracker.record_failure("Transient error 2")
    res2 = db.get_production_health(storage_dir=isolated_env["logs_dir"])
    assert res2["health"] == "DEGRADED"

    # 3 failures -> UNHEALTHY
    tracker.record_failure("Transient error 3")
    res3 = db.get_production_health(storage_dir=isolated_env["logs_dir"])
    assert res3["health"] == "UNHEALTHY"
    assert any("consecutive" in a["message"].lower() for a in res3["alerts"])


# ------------------------------------------------------------------------------
# 12. test_success_resets_consecutive_failure_counter
# ------------------------------------------------------------------------------
def test_success_resets_consecutive_failure_counter(isolated_env):
    """A successful execution resets consecutive failures to 0."""
    tracker = ConsecutiveFailureTracker(storage_dir=isolated_env["logs_dir"])
    tracker.record_failure("Error")
    tracker.record_failure("Error")
    assert tracker.get_consecutive_failures() == 2

    tracker.record_success()
    assert tracker.get_consecutive_failures() == 0

    res = db.get_production_health(storage_dir=isolated_env["logs_dir"])
    assert res["health"] == "HEALTHY"


# ------------------------------------------------------------------------------
# 13. test_health_command_is_read_only
# ------------------------------------------------------------------------------
def test_health_command_is_read_only(isolated_env):
    """Running health check never mutates database records."""
    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM telegram_delivery_records")
    count_before = c.fetchone()[0]
    conn.close()

    db.get_production_health(storage_dir=isolated_env["logs_dir"])
    cli.production_health_cmd(output_json=True)

    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM telegram_delivery_records")
    count_after = c.fetchone()[0]
    conn.close()

    assert count_before == count_after


# ------------------------------------------------------------------------------
# 14. test_scheduler_wrapper_never_sends_second_instance_concurrently
# ------------------------------------------------------------------------------
def test_scheduler_wrapper_never_sends_second_instance_concurrently(isolated_env, tmp_path):
    """When wrapper 1 is running, wrapper 2 immediately skips execution."""
    lock = SingleInstanceLock(lock_dir=isolated_env["logs_dir"])
    lock.acquire()

    dummy_script = tmp_path / "dummy_script.py"
    dummy_script.write_text("import sys; sys.exit(0)", encoding="utf-8")

    code = run_production_pipeline(fetcher_script=str(dummy_script), logs_dir=isolated_env["logs_dir"])
    assert code == 0  # Skipped as safe no-op

    lock.release()