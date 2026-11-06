"""Dialog-list failures must show up on the realtime daemon heartbeat.

A hung CDP used to recycle the browser every few seconds while the heartbeat
kept the previous status=CHECKED. These tests pin three contracts, with no
browser and no Telegram:

* a failed read updates status and the last error, and does not move the stall clock
* the stall watchdog stays armed and its alert names that error and its age
* browser recovery backs off and is capped, then resets after a real check
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import time

import pytest

from ai_assistant.hh_autonomous_agent import (
    MSG_CHECK_ERROR,
    AutonomousConfig,
    AutonomousJobAgent,
)
from ai_assistant.hh_message_reply import fetch_hh_conversations_list_readonly


def _load_daemon():
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "run_hh_realtime_daemon.py"
    )
    spec = importlib.util.spec_from_file_location("hh_realtime_daemon_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    mod = _load_daemon()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(mod, "LOG_DIR", log_dir)
    monkeypatch.setattr(mod, "LOG_FILE", log_dir / "hh_realtime_daemon.log")
    monkeypatch.setattr(mod, "DAEMON_LOCK", log_dir / "hh_realtime_daemon.lock")
    monkeypatch.setattr(mod, "AUTOPILOT_LOCK", log_dir / "autopilot.lock")
    monkeypatch.setattr(mod, "HEARTBEAT_FILE", log_dir / "hh_realtime_daemon.heartbeat")
    monkeypatch.setattr(mod, "BROWSER_RECOVERY_BASE_SECONDS", 5.0)
    monkeypatch.setattr(mod, "BROWSER_RECOVERY_MAX_SECONDS", 300.0)
    monkeypatch.setattr(mod, "PAUSE_ALERT_SECONDS", 1800)
    monkeypatch.setattr(mod, "PAUSE_ALERT_COOLDOWN_SECONDS", 3600)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(mod, "_now", lambda: clock["t"])
    mod._clock = clock
    return mod


def _heartbeat(daemon) -> dict:
    return json.loads(daemon.HEARTBEAT_FILE.read_text(encoding="utf-8"))


def test_dialog_list_failure_updates_heartbeat_and_keeps_stall_clock(daemon):
    daemon._init_observability()
    daemon._mark_check_success(
        {"messages_checked": 11, "messages_check_status": "CHECKED"}
    )
    assert daemon._last_status == "CHECKED"
    assert daemon._checked_last == 11
    success_at = daemon._last_success_at

    daemon._clock["t"] = success_at + 2000
    delay = daemon._note_browser_recovery_failure("http://127.0.0.1:9111")
    daemon._mark_check_failure(
        "ERROR",
        "dialog list read failed: TimeoutError: timed out",
    )

    assert delay == 5.0
    assert daemon._last_success_at == success_at
    assert daemon._last_status == "ERROR"
    assert daemon._checked_last == 11
    assert "TimeoutError" in (daemon._last_error or "")

    payload = _heartbeat(daemon)
    assert payload["status"] == "ERROR"
    assert payload["checked"] == 11
    assert "TimeoutError" in payload["last_error"]
    assert payload["reason"].startswith("chat check failed:")
    assert payload["browser_recovery_failures"] == 1
    assert payload["last_success_age_seconds"] == 2000.0


def test_stall_watchdog_alert_includes_last_error_and_age(daemon, monkeypatch):
    daemon._init_observability()
    daemon._mark_check_success(
        {"messages_checked": 11, "messages_check_status": "CHECKED"}
    )
    success_at = daemon._last_success_at
    daemon._clock["t"] = success_at + 2000
    daemon._note_browser_recovery_failure("http://127.0.0.1:9111")
    daemon._mark_check_failure(
        "ERROR",
        "dialog list read failed: TimeoutError: timed out",
    )
    daemon._clock["t"] = success_at + 2090

    alerts: list[str] = []
    monkeypatch.setattr(
        daemon,
        "_send_owner_alert",
        lambda text: alerts.append(text) or True,
    )
    daemon._watchdog_check()

    assert alerts, "the stall watchdog must stay armed after a failed read"
    text = alerts[0]
    assert "нет успешных проверок чата" in text
    assert "Последняя ошибка: dialog list read failed: TimeoutError: timed out" in text
    assert "Возраст последней ошибки: 90 с" in text
    assert "Последняя успешная проверка чата: 2090 с назад" in text
    assert "Статус: ERROR" in text
    assert "Перезапусков браузера подряд: 1" in text


def test_browser_recovery_backoff_caps_and_resets_after_success(daemon):
    assert daemon.browser_recovery_delay_seconds(1, base=5, maximum=300) == 5
    assert daemon.browser_recovery_delay_seconds(2, base=5, maximum=300) == 10
    assert daemon.browser_recovery_delay_seconds(3, base=5, maximum=300) == 20
    assert daemon.browser_recovery_delay_seconds(6, base=5, maximum=300) == 160
    assert daemon.browser_recovery_delay_seconds(7, base=5, maximum=300) == 300
    assert daemon.browser_recovery_delay_seconds(30, base=5, maximum=300) == 300

    daemon._init_observability()
    first = daemon._note_browser_recovery_failure("http://127.0.0.1:9111")
    second = daemon._note_browser_recovery_failure("http://127.0.0.1:9111")
    third = daemon._note_browser_recovery_failure("http://127.0.0.1:9111")
    assert (first, second, third) == (5.0, 10.0, 20.0)
    assert daemon._browser_recovery_failures == 3

    daemon._mark_check_success(
        {"messages_checked": 2, "messages_check_status": "CHECKED"}
    )
    assert daemon._browser_recovery_failures == 0
    assert daemon._last_error is None
    assert daemon._last_status == "CHECKED"
    assert daemon._note_browser_recovery_failure("http://127.0.0.1:9111") == 5.0


def test_logic_error_does_not_request_browser_recycle_but_timeout_does(daemon):
    assert daemon._reason_needs_browser_recycle("ValueError: bad dialog") is False
    assert daemon._reason_needs_browser_recycle("TimeoutError: timed out") is True
    assert daemon._reason_needs_browser_recycle("ConnectionResetError: Connection reset by peer") is True
    assert daemon._chat_failure_needs_browser_recycle("ERROR", "dialog list read failed") is True
    assert daemon._chat_failure_needs_browser_recycle("CHECKED", "dialog list read, 11 dialog(s)") is False


def test_fetch_timeout_is_an_error_and_does_not_fall_through():
    calls = {"n": 0}

    def hung(_expr: str) -> str:
        calls["n"] += 1
        raise TimeoutError("socket timed out")

    res = fetch_hh_conversations_list_readonly(hung, timeout_seconds=2)
    assert calls["n"] == 1
    assert res.get("conversations") == []
    assert "socket timed out" in str(res.get("error"))


def test_fetch_outer_timeout_does_not_wait_out_a_hung_evaluate():
    def hung(_expr: str) -> str:
        time.sleep(1.0)
        return "{}"

    started = time.monotonic()
    res = fetch_hh_conversations_list_readonly(hung, timeout_seconds=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert res.get("conversations") == []
    assert "timed out after" in str(res.get("error")).lower()


def test_fetch_empty_cdp_payload_is_not_an_empty_inbox():
    def empty(_expr: str) -> str:
        return "{}"

    res = fetch_hh_conversations_list_readonly(empty, timeout_seconds=2)
    assert res.get("conversations") == []
    assert "empty payload" in str(res.get("error"))


def test_fetch_chatik_list_still_returns_conversations():
    def ok(expr: str) -> str:
        if "scrollTop" in expr:
            return json.dumps({"found": False})
        return json.dumps({
            "conversations": [{"conversation_id": "42", "title": "Role"}],
            "url": "https://hh.ru/chat",
        })

    res = fetch_hh_conversations_list_readonly(ok, timeout_seconds=2)
    assert res.get("error") in (None, "")
    assert res["conversations"][0]["conversation_id"] == "42"


def test_fetch_non_transport_chatik_failure_falls_back_to_dom():
    def evaluate(expr: str) -> str:
        if "chatik/api" in expr:
            raise RuntimeError("unexpected evaluate result: object")
        if "scrollTop" in expr:
            return json.dumps({"found": False})
        return json.dumps({
            "conversations": [{"conversation_id": "7", "title": "DOM"}],
            "url": "https://hh.ru/chat",
        })

    res = fetch_hh_conversations_list_readonly(evaluate, timeout_seconds=2)
    assert not res.get("error")
    assert res["conversations"][0]["conversation_id"] == "7"


def test_process_messages_surfaces_hung_cdp_instead_of_checked():
    def hung(_script: str) -> str:
        raise TimeoutError("cdp hung")

    agent = AutonomousJobAgent(
        config=AutonomousConfig(evaluate_fn=hung, auto_start_browser=False)
    )
    res = agent._process_messages()
    assert res["messages_check_status"] == MSG_CHECK_ERROR
    assert "dialog list read failed" in res["messages_check_reason"]
    assert "TimeoutError" in res["messages_check_reason"]
    assert "cdp hung" in res["messages_check_reason"]


def test_process_messages_empty_cdp_payload_is_an_error():
    def evaluate(script: str) -> str:
        if script.strip() == "window.location.href":
            return "https://hh.ru/chat"
        return "{}"

    agent = AutonomousJobAgent(
        config=AutonomousConfig(evaluate_fn=evaluate, auto_start_browser=False)
    )
    res = agent._process_messages()
    assert res["messages_check_status"] == MSG_CHECK_ERROR
    assert "empty payload" in res["messages_check_reason"]
