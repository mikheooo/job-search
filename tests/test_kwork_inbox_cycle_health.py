"""Kwork cycle health wiring. No CDP, no Telegram, no production inbox db.

The scan is a fake ``KworkScanResult``. Message delivery and the operational
health sender are separate mocks.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from ai_assistant.gig.kwork_inbox_monitor import (
    KworkInboxStorage,
    KworkMessage,
    KworkScanResult,
    process_monitor_cycle,
)


def _load_cycle():
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "run_kwork_inbox_monitor_cycle.py"
    )
    spec = importlib.util.spec_from_file_location("kwork_cycle_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cycle(tmp_path, monkeypatch):
    mod = _load_cycle()
    monkeypatch.setattr(mod, "MONITOR_LOCK", tmp_path / "kwork.lock")
    monkeypatch.setattr(mod, "DISABLED_FLAG", tmp_path / "not-disabled")
    monkeypatch.setattr(mod, "KWORK_HEALTH_STATE", tmp_path / "health.json")
    monkeypatch.setattr(mod, "INBOX_DB", tmp_path / "inbox.db")
    monkeypatch.setenv("KWORK_INBOX_NOT_READY_STREAK", "3")
    monkeypatch.setenv("KWORK_INBOX_ALERT_COOLDOWN_SECONDS", "100000")
    return mod


def _install_scan(monkeypatch, cycle, result: KworkScanResult):
    monkeypatch.setattr(cycle, "scan_kwork_inbox_via_cdp", lambda cdp_url: result)


def _message_sender(box: list[str]):
    from ai_assistant.telegram_routing import JobSendResult

    def _send(text: str, parse_mode: str | None = None) -> JobSendResult:
        box.append(text)
        return JobSendResult(True, "delivered", "mock", 1)

    return _send


def test_page_not_ready_report_has_zero_checked_and_no_message_card(tmp_path):
    storage = KworkInboxStorage(tmp_path / "inbox.db")
    sent: list[str] = []
    report = process_monitor_cycle(
        storage,
        lambda: KworkScanResult(
            is_authenticated=False,
            auth_status="PAGE_NOT_READY",
            auth_error="DOM readyState is loading",
            active_dialogues_count=99,
        ),
        sender_fn=_message_sender(sent),
        dry_run=False,
    )
    assert report.auth_status == "PAGE_NOT_READY"
    assert report.scanned_dialogues == 0
    assert report.alerts_sent == 0
    assert sent == []


def test_live_cycle_alerts_only_after_persistent_not_ready(cycle, monkeypatch):
    messages: list[str] = []
    health: list[str] = []
    monkeypatch.setattr(
        "ai_assistant.gig.kwork_inbox_monitor.telegram_routing.send_job_message_result",
        _message_sender(messages),
    )
    monkeypatch.setattr(
        "ai_assistant.automation_alerts.deliver_owner_alert",
        lambda text: health.append(text) or True,
    )
    _install_scan(
        monkeypatch,
        cycle,
        KworkScanResult(
            is_authenticated=False,
            auth_status="PAGE_NOT_READY",
            auth_error="DOM readyState is loading password=supersecretvalue",
        ),
    )

    first = cycle.run_cycle(dry_run=False)
    second = cycle.run_cycle(dry_run=False)
    third = cycle.run_cycle(dry_run=False)

    assert first["auth"] == "PAGE_NOT_READY"
    assert first["scanned"] == 0
    assert first["sent"] == 0
    assert first["health_alert"]["action"] == "none"
    assert second["health_alert"]["action"] == "none"
    assert third["health_alert"]["action"] == "alert"
    assert third["health_alert"]["sent"] is True
    assert messages == []
    assert len(health) == 1
    assert "PAGE_NOT_READY" in health[0]
    assert "не признак истёкшей авторизации" in health[0]
    assert "supersecretvalue" not in health[0]
    assert "новое сообщение" not in health[0].lower()

    _install_scan(
        monkeypatch,
        cycle,
        KworkScanResult(is_authenticated=True, auth_status="OK", active_dialogues_count=0),
    )
    recovered = cycle.run_cycle(dry_run=False)
    assert recovered["auth"] == "OK"
    assert recovered["scanned"] == 0
    assert recovered["health_alert"]["action"] == "recovery"
    assert messages == []
    assert "снова проходит" in health[-1]


def test_dry_run_does_not_send_health_or_message_cards(cycle, monkeypatch):
    messages: list[str] = []
    health: list[str] = []
    monkeypatch.setattr(
        "ai_assistant.gig.kwork_inbox_monitor.telegram_routing.send_job_message_result",
        _message_sender(messages),
    )
    monkeypatch.setattr(
        "ai_assistant.automation_alerts.deliver_owner_alert",
        lambda text: health.append(text) or True,
    )
    _install_scan(
        monkeypatch,
        cycle,
        KworkScanResult(is_authenticated=False, auth_status="PAGE_NOT_READY", auth_error="loading"),
    )
    for _ in range(3):
        result = cycle.run_cycle(dry_run=True)
        assert result["health_alert"]["sent"] is False
    assert health == []
    assert messages == []
    assert not cycle.KWORK_HEALTH_STATE.exists()


def test_auth_loss_does_not_use_the_operational_ledger(cycle, monkeypatch, tmp_path):
    messages: list[str] = []
    health: list[str] = []
    monkeypatch.setattr(
        "ai_assistant.automation_alerts.deliver_owner_alert",
        lambda text: health.append(text) or True,
    )
    _install_scan(
        monkeypatch,
        cycle,
        KworkScanResult(
            is_authenticated=False,
            auth_status="PAGE_NOT_READY",
            auth_error="loading",
        ),
    )
    cycle.run_cycle(dry_run=False)
    assert cycle.KWORK_HEALTH_STATE.exists()
    streak_before = json.loads(cycle.KWORK_HEALTH_STATE.read_text(encoding="utf-8"))["streak"]

    monkeypatch.setattr(
        "ai_assistant.gig.kwork_inbox_monitor.telegram_routing.send_job_message_result",
        _message_sender(messages),
    )
    _install_scan(
        monkeypatch,
        cycle,
        KworkScanResult(
            is_authenticated=False,
            auth_status="AUTH_REQUIRED",
            auth_error="Confirmed login page rendered (/login)",
        ),
    )
    first = cycle.run_cycle(dry_run=False)
    second = cycle.run_cycle(dry_run=False)
    assert first["auth"] == "AUTH_REQUIRED"
    assert first["health_alert"]["action"] == "skipped_auth"
    assert second["auth"] == "AUTH_LOST"
    assert second["health_alert"]["action"] == "skipped_auth"
    assert health == []
    streak_after = json.loads(cycle.KWORK_HEALTH_STATE.read_text(encoding="utf-8"))["streak"]
    assert streak_after == streak_before
    assert messages, "the monitor's own auth warning still sends on the second strike"
    assert "истёкшей авторизации" not in messages[-1]


def test_new_buyer_message_is_not_repeated_as_a_health_alert(cycle, monkeypatch):
    messages: list[str] = []
    health: list[str] = []
    monkeypatch.setattr(
        "ai_assistant.gig.kwork_inbox_monitor.telegram_routing.send_job_message_result",
        _message_sender(messages),
    )
    monkeypatch.setattr(
        "ai_assistant.automation_alerts.deliver_owner_alert",
        lambda text: health.append(text) or True,
    )
    incoming = KworkMessage(
        message_key="kwork:d1:m1",
        dialogue_id="d1",
        project_id="p1",
        project_title="Лендинг",
        buyer_name="Buyer",
        message_id="m1",
        text="Нужен бот к понедельнику",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/Buyer",
        detected_at="2026-11-07T00:00:00+00:00",
        delivery_status="PENDING",
    )
    _install_scan(
        monkeypatch,
        cycle,
        KworkScanResult(
            is_authenticated=True,
            auth_status="OK",
            messages=[incoming],
            active_dialogues_count=1,
        ),
    )
    result = cycle.run_cycle(dry_run=False)
    assert result["new"] == 1
    assert result["sent"] == 1
    assert result["health_alert"]["action"] == "none"
    assert health == []
    assert len(messages) == 1
    assert "Нужен бот" in messages[0]


def test_cli_live_flag_reaches_the_cycle(cycle, monkeypatch, capsys):
    seen: dict[str, bool] = {}

    def _run(*, dry_run: bool, send_one: bool, cdp_url: str):
        seen["dry_run"] = dry_run
        seen["send_one"] = send_one
        return {"status": "OK", "auth": "OK", "scanned": 1}

    monkeypatch.setattr(cycle, "run_cycle", _run)
    cycle.main(["--live", "--send-one"])
    assert seen == {"dry_run": False, "send_one": True}
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] == 1

    cycle.main([])
    assert seen["dry_run"] is True
