"""P0 regression tests: realtime daemon pause is observable and watchdog-guarded.

Audit 2026-10-19 (ops F01 / C-01): scripts/run_hh_realtime_daemon.py logged
"yielding" once and then went completely silent for the whole autopilot cycle
(measured pauses up to 19.4h). Nothing outside the process could distinguish a
deliberate pause from a crash: no PAUSED/RESUMED transitions, no heartbeat while
paused, no watchdog.

These tests pin the fix at unit level: no browser is started, no network call is
made, no production log/state file is touched (module paths are redirected to
tmp_path / monkeypatched).

Covered:
1. RUNNING->PAUSED transition is logged exactly once with the lock PID/timestamp.
2. Heartbeat JSON (ts/state/reason/checked/pid) is written in PAUSED and RUNNING
   and refreshed at the 60s cadence, with a "Heartbeat #N: PAUSED waiting ..."
   log line no less often than that.
3. PAUSED->RUNNING transition is logged exactly once and the heartbeat returns
   to RUNNING.
4. Watchdog alerts the owner once the pause (or the no-successful-check gap)
   crosses HH_REALTIME_PAUSE_ALERT_SECONDS, then respects the cooldown.
5. The real loop logs one PAUSED and one RESUMED on the transition and leaves a
   fresh heartbeat plus released daemon lock.
6. scripts/check_runtime_health.py sees absent/stale/long-PAUSED heartbeats.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_PATH = REPO_ROOT / "scripts" / "run_hh_realtime_daemon.py"
HEALTH_PATH = REPO_ROOT / "scripts" / "check_runtime_health.py"

LOG_NAME = "hh_realtime_daemon"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec and spec.loader, f"cannot load {file_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Clock:
    """Deterministic clock injected as the daemon's _now()."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    mod = _load_module("hh_realtime_daemon_under_test", DAEMON_PATH)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "LOG_DIR", logs)
    monkeypatch.setattr(mod, "LOG_FILE", logs / "hh_realtime_daemon.log")
    monkeypatch.setattr(mod, "HEARTBEAT_FILE", logs / "hh_realtime_daemon.heartbeat")
    monkeypatch.setattr(mod, "AUTOPILOT_LOCK", logs / "autopilot.lock")
    monkeypatch.setattr(mod, "DAEMON_LOCK", logs / "hh_realtime_daemon.lock")
    return mod


@pytest.fixture()
def clock(daemon, monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(daemon, "_now", fake)
    return fake


@pytest.fixture()
def alerts(daemon, monkeypatch):
    """Capture watchdog alerts instead of sending Telegram messages."""
    sent: list[str] = []
    monkeypatch.setattr(
        daemon, "_send_owner_alert", lambda text: bool(sent.append(text)) or True
    )
    return sent


def _records(caplog, substring: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOG_NAME and substring in record.getMessage()
    ]


def _read_heartbeat(daemon) -> dict:
    return json.loads(daemon.HEARTBEAT_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1-3. Transition visibility + unified heartbeat
# ---------------------------------------------------------------------------


def test_pause_transition_logged_once_with_lock_context(daemon, clock, caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    daemon._init_observability()

    assert daemon._enter_pause(28836, "2026-10-19T11:04:20") is True
    assert daemon._enter_pause(28836, "2026-10-19T11:04:20") is False

    paused_lines = _records(caplog, "PAUSED")
    assert len(paused_lines) == 1, f"pause transition must be logged once: {paused_lines}"
    assert "28836" in paused_lines[0]
    assert "2026-10-19T11:04:20" in paused_lines[0]

    heartbeat = _read_heartbeat(daemon)
    # Required keys of the heartbeat contract.
    for key in ("ts", "state", "reason", "checked", "pid"):
        assert key in heartbeat, f"heartbeat misses {key!r}: {heartbeat}"
    assert heartbeat["state"] == "PAUSED"
    assert heartbeat["pid"] == os.getpid()
    assert heartbeat["checked"] == 0
    assert "28836" in heartbeat["reason"]
    assert "2026-10-19T11:04:20" in heartbeat["reason"]
    assert heartbeat["ts"]


def test_paused_heartbeat_refreshed_at_least_every_60s(daemon, clock, caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    daemon._init_observability()
    daemon._enter_pause(28836, "2026-10-19T11:04:20")

    # Not due yet: no heartbeat line, no file rewrite.
    clock.advance(daemon.HEARTBEAT_INTERVAL_SECONDS - 1)
    assert daemon._heartbeat_tick(5) == 0
    assert _records(caplog, "Heartbeat #") == []

    # At the interval boundary the heartbeat appears and names the pause reason.
    clock.advance(1)
    seq = daemon._heartbeat_tick(6)
    assert seq >= 1
    pause_heartbeats = _records(caplog, "PAUSED waiting for autopilot PID 28836")
    assert len(pause_heartbeats) == 1, pause_heartbeats
    assert "Heartbeat #" in pause_heartbeats[0]

    heartbeat = _read_heartbeat(daemon)
    assert heartbeat["state"] == "PAUSED"
    assert heartbeat["heartbeat"] == seq
    assert heartbeat["pause_seconds"] >= daemon.HEARTBEAT_INTERVAL_SECONDS

    # And again a minute later — the pause never goes silent for longer than 60s.
    clock.advance(daemon.HEARTBEAT_INTERVAL_SECONDS)
    assert daemon._heartbeat_tick(12) >= 1
    assert len(_records(caplog, "PAUSED waiting for autopilot PID 28836")) == 2


def test_running_heartbeat_carries_check_results(daemon, clock, caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    daemon._init_observability()
    clock.advance(30)
    daemon._mark_check_success(
        {"messages_checked": 7, "messages_check_status": "CHECKED"}
    )

    clock.advance(daemon.HEARTBEAT_INTERVAL_SECONDS)
    assert daemon._heartbeat_tick(2) >= 1

    heartbeat = _read_heartbeat(daemon)
    assert heartbeat["state"] == "RUNNING"
    assert heartbeat["checked"] == 7
    assert heartbeat["status"] == "CHECKED"
    assert heartbeat["last_success_age_seconds"] >= 0
    assert len(_records(caplog, "state=RUNNING")) == 1


def test_resume_transition_logged_once_and_heartbeat_back_to_running(daemon, clock, caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    daemon._init_observability()
    daemon._enter_pause(4242, "2026-10-19T11:04:20")
    clock.advance(125)

    assert daemon._leave_pause() is True
    assert daemon._leave_pause() is False

    resumed = _records(caplog, "RESUMED")
    assert len(resumed) == 1, resumed
    assert "4242" in resumed[0]
    assert "2026-10-19T11:04:20" in resumed[0]

    heartbeat = _read_heartbeat(daemon)
    assert heartbeat["state"] == "RUNNING"
    assert heartbeat["pause_seconds"] == pytest.approx(125.0, abs=1.0)


# ---------------------------------------------------------------------------
# 4. Watchdog
# ---------------------------------------------------------------------------


def test_pause_watchdog_fires_on_threshold_and_respects_cooldown(daemon, clock, alerts):
    daemon._init_observability()
    daemon._enter_pause(4242, "2026-10-19T11:04:20")

    clock.advance(daemon.PAUSE_ALERT_SECONDS - 1)
    daemon._watchdog_check()
    assert alerts == [], "no alert before the pause threshold"

    clock.advance(1)
    daemon._watchdog_check()
    assert len(alerts) == 1, "one owner alert at the pause threshold"
    assert "PAUSED" in alerts[0]
    assert "4242" in alerts[0]
    assert "пауза затянулась" in alerts[0]

    # Cooldown suppresses a repeat...
    clock.advance(daemon.PAUSE_ALERT_COOLDOWN_SECONDS - 1)
    daemon._watchdog_check()
    assert len(alerts) == 1

    # ...but the next window may alert once again.
    clock.advance(1)
    daemon._watchdog_check()
    assert len(alerts) == 2

    # A distinct pause after resume gets its own alert (cooldown is per occurrence).
    daemon._leave_pause()
    daemon._enter_pause(777, "2026-10-19T12:00:00")
    clock.advance(daemon.PAUSE_ALERT_SECONDS)
    daemon._watchdog_check()
    assert len(alerts) == 3


def test_stall_watchdog_fires_when_no_successful_check(daemon, clock, alerts):
    daemon._init_observability()
    clock.advance(daemon.PAUSE_ALERT_SECONDS - 1)
    daemon._watchdog_check()
    assert alerts == []

    clock.advance(1)
    daemon._watchdog_check()
    assert len(alerts) == 1
    assert "RUNNING" in alerts[0]
    assert "нет успешных проверок чата" in alerts[0]

    clock.advance(5)
    daemon._watchdog_check()
    assert len(alerts) == 1, "cooldown must prevent alert spam"

    # Recovery resets the streak: a brand new stall may alert on its own.
    daemon._mark_check_success({"messages_checked": 1, "messages_check_status": "CHECKED"})
    daemon._watchdog_check()
    assert len(alerts) == 1
    clock.advance(daemon.PAUSE_ALERT_SECONDS)
    daemon._watchdog_check()
    assert len(alerts) == 2


def test_long_pause_does_not_become_a_browser_stall_on_resume(daemon, clock, alerts):
    daemon._init_observability()
    daemon._mark_check_success({"messages_checked": 11, "messages_check_status": "CHECKED"})
    last_success = daemon._last_success_at
    clock.advance(30)
    daemon._enter_pause(4242, "2026-10-23T07:04:15")
    clock.advance(3957)
    daemon._watchdog_check()
    assert len(alerts) == 1
    assert "пауза затянулась" in alerts[0]

    daemon._leave_pause()
    daemon._watchdog_check()
    assert len(alerts) == 1, "resume must not reclassify the pause as a browser failure"
    assert daemon._last_success_at == last_success, "resume is not a successful check"
    daemon._heartbeat_tick(1)
    clock.advance(daemon.HEARTBEAT_INTERVAL_SECONDS)
    daemon._heartbeat_tick(2)
    assert _read_heartbeat(daemon)["last_success_age_seconds"] >= 3987

    daemon._mark_check_success({"messages_checked": 11, "messages_check_status": "CHECKED"})
    clock.advance(daemon.PAUSE_ALERT_SECONDS - 1)
    daemon._watchdog_check()
    assert len(alerts) == 1


def test_real_stall_after_resume_still_alerts_at_active_threshold(daemon, clock, alerts):
    daemon._init_observability()
    clock.advance(100)
    daemon._enter_pause(4242, "lock")
    clock.advance(4000)
    daemon._leave_pause()
    clock.advance(daemon.PAUSE_ALERT_SECONDS - 101)
    daemon._watchdog_check()
    assert alerts == []
    clock.advance(1)
    daemon._watchdog_check()
    assert len(alerts) == 1
    assert "нет успешных проверок чата" in alerts[0]
    assert "паузы автопилота исключены" in alerts[0]
    assert "Длится: 30.0 мин (1800 с)" in alerts[0]
    assert "5800 с назад" in alerts[0], "actual last check age must remain honest"
    daemon._watchdog_check()
    assert len(alerts) == 1, "stall cooldown remains in force"


def test_multiple_pauses_excluded_without_erasing_active_failures(daemon, clock, alerts):
    daemon._init_observability()
    for _ in range(3):
        clock.advance(daemon.PAUSE_ALERT_SECONDS / 4)
        daemon._enter_pause(4242, "lock")
        clock.advance(5000)
        daemon._leave_pause()
        daemon._watchdog_check()
        assert alerts == []
    clock.advance(daemon.PAUSE_ALERT_SECONDS / 4)
    daemon._watchdog_check()
    assert len(alerts) == 1, "active failures must accumulate across pauses"


def test_success_resets_pause_accounting_for_the_next_stall(daemon, clock, alerts):
    daemon._init_observability()
    daemon._enter_pause(4242, "lock")
    clock.advance(5000)
    daemon._leave_pause()
    daemon._mark_check_success({"messages_checked": 1, "messages_check_status": "CHECKED"})
    clock.advance(daemon.PAUSE_ALERT_SECONDS)
    daemon._watchdog_check()
    assert len(alerts) == 1


def test_watchdog_does_not_alert_while_checks_succeed(daemon, clock, alerts):
    daemon._init_observability()
    for _ in range(5):
        clock.advance(daemon.PAUSE_ALERT_SECONDS // 2)
        daemon._mark_check_success({"messages_checked": 1, "messages_check_status": "CHECKED"})
        daemon._watchdog_check()
    assert alerts == []


# ---------------------------------------------------------------------------
# 5. Real loop integration (no browser, no network)
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stands in for HeadlessBrowserSession; fails fast without touching Chrome."""

    def __init__(self, require_auth: bool = True) -> None:
        self.cdp_url = "http://127.0.0.1:1"

    def start(self) -> dict:
        return {"ok": False, "error": "test-stub: no browser in this test"}

    def stop(self) -> None:  # pragma: no cover - loop may call it on pause
        pass

    def open_hh_page(self, url: str) -> None:  # pragma: no cover
        pass


def test_pause_preserves_shared_browser_and_resumes_same_session(daemon, monkeypatch):
    sessions = []
    checks = []
    class Session:
        def __init__(self, require_auth=True):
            self.cdp_url = 'http://127.0.0.1:1'
            self.stop_calls = 0
            sessions.append(self)
        def start(self):
            return {'ok': True}
        def open_hh_page(self, url):
            pass
        def close_all_owned_tabs(self):
            self.closed = True
        def stop(self):
            self.stop_calls += 1
    class Agent:
        def __init__(self, config):
            pass
        def _process_messages(self):
            checks.append('check')
            if len(checks) == 1:
                daemon.AUTOPILOT_LOCK.write_text(f'{os.getpid()}\nnow', encoding='utf-8')
            else:
                daemon._running = False
            return {'messages_check_status': 'CHECKED', 'messages_checked': 1}
    def sleep(seconds):
        if seconds == daemon.PAUSE_RECHECK_SECONDS:
            assert sessions[0].stop_calls == 0
            assert len(checks) == 1
            daemon.AUTOPILOT_LOCK.unlink()
    monkeypatch.setattr(daemon, 'HeadlessBrowserSession', Session)
    monkeypatch.setattr(daemon, 'AutonomousJobAgent', Agent)
    monkeypatch.setattr(daemon, '_is_pid_running', lambda pid: pid == os.getpid())
    monkeypatch.setattr(daemon, '_send_owner_alert', lambda _: True)
    monkeypatch.setattr(daemon, 'scan_hh_negotiations', lambda **kwargs: [])
    monkeypatch.setattr(daemon.time, 'sleep', sleep)
    daemon.run_daemon_loop()
    assert len(sessions) == 1
    assert checks == ['check', 'check']
    assert sessions[0].stop_calls == 0
    assert sessions[0].closed is True  # only its own tabs on process shutdown


def test_daemon_loop_logs_pause_writes_heartbeat_then_resumes(
    daemon, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    daemon.AUTOPILOT_LOCK.write_text(
        f"{os.getpid()}\n2026-10-19T11:04:20", encoding="utf-8"
    )
    monkeypatch.setattr(daemon, "_is_pid_running", lambda pid: pid == os.getpid())
    monkeypatch.setattr(daemon, "_send_owner_alert", lambda text: True)
    monkeypatch.setattr(daemon, "HeadlessBrowserSession", _FakeSession)

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # Second iteration: the autopilot finishes and releases its lock.
        if len(sleeps) == 2:
            daemon.AUTOPILOT_LOCK.unlink()
        # Third sleep is the browser-start retry after RESUMED: stop the loop.
        if len(sleeps) >= 3:
            daemon._running = False

    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)

    daemon.run_daemon_loop()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == LOG_NAME
    ]
    paused = [m for m in messages if "PAUSED" in m]
    assert len(paused) == 1, f"exactly one PAUSED transition log, got: {paused}"
    assert "2026-10-19T11:04:20" in paused[0]

    resumed = [m for m in messages if "RESUMED" in m]
    assert len(resumed) == 1, f"exactly one RESUMED transition log, got: {resumed}"

    assert sleeps[0] == daemon.PAUSE_RECHECK_SECONDS
    heartbeat = _read_heartbeat(daemon)
    assert heartbeat["state"] == "RUNNING", "resume must refresh the heartbeat"
    assert not daemon.DAEMON_LOCK.exists(), "daemon lock must be released on exit"


# ---------------------------------------------------------------------------
# 6. Health check sees the daemon heartbeat
# ---------------------------------------------------------------------------


@pytest.fixture()
def health():
    return _load_module("check_runtime_health_under_test", HEALTH_PATH)


def _write_heartbeat(project_root: Path, payload: dict) -> None:
    logs = project_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "hh_realtime_daemon.heartbeat").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _heartbeat_payload(now: float, **overrides) -> dict:
    payload = {
        "ts": "2026-10-19T05:00:00+00:00",
        "epoch": now - 5,
        "pid": 1234,
        "state": "RUNNING",
        "reason": "monitoring chat",
        "checked": 3,
        "heartbeat": 9,
    }
    payload.update(overrides)
    return payload


def test_health_check_reports_absent_heartbeat(health, tmp_path):
    status, lines = health.check_realtime_daemon(str(tmp_path), now=1_000_000.0)
    assert status == "WARN"
    assert "realtime-daemon: absent" in lines[0]


def test_health_check_reports_fresh_running_heartbeat(health, tmp_path):
    now = 1_000_000.0
    _write_heartbeat(tmp_path, _heartbeat_payload(now))
    status, lines = health.check_realtime_daemon(str(tmp_path), now=now)
    assert status == "OK"
    assert "state=RUNNING" in lines[0]
    assert "reason=monitoring chat" in lines[0]


def test_health_check_flags_stale_heartbeat(health, tmp_path):
    now = 1_000_000.0
    _write_heartbeat(tmp_path, _heartbeat_payload(now, epoch=now - 1200))
    status, lines = health.check_realtime_daemon(str(tmp_path), now=now)
    assert status == "FAIL"
    assert "stale" in lines[1]


def test_health_check_surfaces_short_pause(health, tmp_path):
    now = 1_000_000.0
    _write_heartbeat(
        tmp_path,
        _heartbeat_payload(now, state="PAUSED", pause_seconds=120.0),
    )
    status, lines = health.check_realtime_daemon(str(tmp_path), now=now)
    assert status == "WARN"
    assert "state=PAUSED" in lines[0]


def test_health_check_surfaces_long_pause_alert(health, tmp_path):
    now = 1_000_000.0
    _write_heartbeat(
        tmp_path,
        _heartbeat_payload(now, state="PAUSED", pause_seconds=1900.0),
    )
    status, lines = health.check_realtime_daemon(str(tmp_path), now=now)
    assert status == "FAIL"
    joined = "\n".join(lines)
    assert "realtime-daemon-alert" in joined
    assert "WATCHDOG ALERT DUE" in joined
    assert "1900" in joined


def test_health_check_never_raises_on_unreadable_heartbeat(health, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "hh_realtime_daemon.heartbeat").write_text("not json", encoding="utf-8")
    status, lines = health.check_realtime_daemon(str(tmp_path), now=1_000_000.0)
    assert status == "FAIL"
    assert "unreadable heartbeat" in lines[0]
