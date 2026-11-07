"""Operational alert ledger: cooldown, recovery, and no secret leakage.

No network, no Telegram, no browser. The sender is a list append.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from ai_assistant.automation_alerts import (
    classify_kwork_inbox,
    notify_hh_non_auth_failure,
    notify_hh_non_auth_recovery,
    observe_kwork_inbox,
    sanitize_alert_text,
)

SECRET = "bot123456:AAFakeTokenValueForTestOnlyXXXX"
HOME = "/home/misha/job-search"


def _sender(box: list[str]):
    def _send(text: str) -> bool:
        box.append(text)
        return True

    return _send


def test_sanitize_strips_token_password_and_home():
    raw = f"cannot import name hh_structured_preflight password={SECRET} from {HOME}"
    safe = sanitize_alert_text(raw)
    assert "hh_structured_preflight" in safe
    assert SECRET not in safe
    assert "misha" not in safe
    assert "password=" not in safe


def test_hh_first_alert_repeat_cooldown_and_recovery(tmp_path):
    state = tmp_path / "hh.json"
    sent: list[str] = []
    error = f"ImportError: cannot import name 'hh_structured_preflight' ({HOME}) token={SECRET}"
    clock = {"t": 1_000_000.0}

    first = notify_hh_non_auth_failure(
        error,
        state_path=state,
        send_fn=_sender(sent),
        submitted=0,
        cooldown_seconds=100,
        now=clock["t"],
    )
    assert first["action"] == "alert" and first["sent"] is True
    assert len(sent) == 1
    assert "hh_structured_preflight" in sent[0]
    assert "Отправлено откликов в этом цикле: 0" in sent[0]
    assert "не потеря авторизации" in sent[0]
    assert SECRET not in sent[0]
    assert "misha" not in sent[0]
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert SECRET not in json.dumps(stored)
    assert "misha" not in json.dumps(stored)

    clock["t"] += 10
    again = notify_hh_non_auth_failure(
        error,
        state_path=state,
        send_fn=_sender(sent),
        submitted=0,
        cooldown_seconds=100,
        now=clock["t"],
    )
    assert again["action"] == "suppressed" and again["sent"] is False
    assert len(sent) == 1
    assert again["streak"] == 2

    clock["t"] += 100
    retried = notify_hh_non_auth_failure(
        error,
        state_path=state,
        send_fn=_sender(sent),
        submitted=0,
        cooldown_seconds=100,
        now=clock["t"],
    )
    assert retried["action"] == "alert" and len(sent) == 2

    clock["t"] += 1
    recovered = notify_hh_non_auth_recovery(
        state_path=state,
        send_fn=_sender(sent),
        cooldown_seconds=100,
        now=clock["t"],
    )
    assert recovered["action"] == "recovery" and recovered["sent"] is True
    assert "снова завершился без этой ошибки" in sent[-1]
    quiet = notify_hh_non_auth_recovery(
        state_path=state,
        send_fn=_sender(sent),
        cooldown_seconds=100,
        now=clock["t"] + 1,
    )
    assert quiet["action"] == "none" and len(sent) == 3


def test_hh_delivery_failure_does_not_start_cooldown(tmp_path):
    state = tmp_path / "hh.json"
    calls = {"n": 0}

    def _fail(_text: str) -> bool:
        calls["n"] += 1
        return False

    first = notify_hh_non_auth_failure(
        "disk quota exceeded",
        state_path=state,
        send_fn=_fail,
        cooldown_seconds=100,
        now=1_000.0,
    )
    assert first["action"] == "delivery_failed" and first["sent"] is False
    second = notify_hh_non_auth_failure(
        "disk quota exceeded",
        state_path=state,
        send_fn=_fail,
        cooldown_seconds=100,
        now=1_010.0,
    )
    assert second["action"] == "delivery_failed"
    assert calls["n"] == 2


def test_hh_sender_exception_is_a_delivery_failure(tmp_path):
    state = tmp_path / "hh.json"

    def _boom(_text: str) -> bool:
        raise RuntimeError(f"transport down token={SECRET}")

    decision = notify_hh_non_auth_failure(
        "boom",
        state_path=state,
        send_fn=_boom,
        cooldown_seconds=50,
        now=50.0,
    )
    assert decision["action"] == "delivery_failed"
    assert SECRET not in json.dumps(_read(state))


def test_hh_dry_run_does_not_send_or_write(tmp_path):
    state = tmp_path / "hh.json"
    sent: list[str] = []
    decision = notify_hh_non_auth_failure(
        "ImportError: hh_structured_preflight",
        state_path=state,
        send_fn=_sender(sent),
        dry_run=True,
        now=10.0,
    )
    assert decision["action"] == "would_alert"
    assert sent == []
    assert not state.exists()


def test_kwork_transient_not_ready_does_not_alert(tmp_path):
    state = tmp_path / "kwork.json"
    sent: list[str] = []
    one = observe_kwork_inbox(
        status="PAGE_NOT_READY",
        checked=0,
        state_path=state,
        send_fn=_sender(sent),
        streak_required=3,
        cooldown_seconds=100,
        now=1.0,
    )
    assert one["action"] == "none" and one["sent"] is False and sent == []
    ready = observe_kwork_inbox(
        status="CHECKED",
        checked=4,
        state_path=state,
        send_fn=_sender(sent),
        streak_required=3,
        cooldown_seconds=100,
        now=2.0,
    )
    assert ready["action"] == "none" and ready["reason"] == "ready"
    assert sent == []


def test_kwork_persistent_not_ready_alerts_then_cools_down_and_recovers(tmp_path):
    state = tmp_path / "kwork.json"
    sent: list[str] = []
    clock = {"t": 5_000.0}

    def _observe(status: str, checked: int, error: str | None = None):
        clock["t"] += 1
        return observe_kwork_inbox(
            status=status,
            checked=checked,
            error=error,
            state_path=state,
            send_fn=_sender(sent),
            streak_required=3,
            cooldown_seconds=100,
            now=clock["t"],
        )

    assert _observe("PAGE_NOT_READY", 0)["action"] == "none"
    assert _observe("PAGE_NOT_READY", 0)["action"] == "none"
    third = _observe("PAGE_NOT_READY", 0)
    assert third["action"] == "alert" and third["sent"] is True
    assert len(sent) == 1
    assert "Kwork inbox" in sent[0]
    assert "PAGE_NOT_READY" in sent[0]
    assert "истёкшей авторизации" in sent[0]
    assert "новое сообщение" not in sent[0].lower()
    assert "HH_AUTH_REQUIRED" not in sent[0]
    assert _observe("PAGE_NOT_READY", 0)["action"] == "suppressed"
    assert len(sent) == 1

    clock["t"] += 100
    assert _observe("PAGE_NOT_READY", 0)["action"] == "alert"
    assert len(sent) == 2

    recovered = _observe("CHECKED", 3)
    assert recovered["action"] == "recovery" and recovered["sent"] is True
    assert "снова проходит" in sent[-1]
    assert _observe("CHECKED", 3)["action"] == "none"
    assert len(sent) == 3


def test_kwork_zero_checked_on_a_ready_page_is_not_a_failure():
    unhealthy, reason = classify_kwork_inbox("CHECKED", 0)
    assert unhealthy is False and reason == "empty_but_ready"
    ignored, why = classify_kwork_inbox("AUTH_REQUIRED", 0)
    assert ignored is False and why == "ignored_auth_status"


def test_kwork_generic_exception_streak_hides_secrets(tmp_path):
    state = tmp_path / "kwork.json"
    sent: list[str] = []
    error = f"RuntimeError: socket reset token={SECRET} path={HOME}"
    for step in range(3):
        decision = observe_kwork_inbox(
            status=None,
            checked=0,
            error=error,
            state_path=state,
            send_fn=_sender(sent),
            streak_required=3,
            cooldown_seconds=500,
            now=100.0 + step,
        )
    assert decision["action"] == "alert"
    assert decision["reason"] == "operational_failure"
    assert SECRET not in sent[0]
    assert "misha" not in sent[0]
    assert "истёкшей авторизации" in sent[0]
    assert SECRET not in state.read_text(encoding="utf-8")


def test_kwork_dry_run_previews_without_sending(tmp_path):
    state = tmp_path / "kwork.json"
    state.write_text(
        json.dumps({"streak": 2, "awaiting_recovery": False, "alerted_at": None}),
        encoding="utf-8",
    )
    before = state.read_text(encoding="utf-8")
    sent: list[str] = []
    decision = observe_kwork_inbox(
        status="PAGE_NOT_READY",
        checked=0,
        state_path=state,
        send_fn=_sender(sent),
        dry_run=True,
        streak_required=3,
        cooldown_seconds=100,
        now=20.0,
    )
    assert decision["action"] == "would_alert"
    assert sent == []
    assert state.read_text(encoding="utf-8") == before


def test_health_reporter_dry_run_prints_decision_without_delivery(tmp_path, capsys):
    script = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "report_kwork_inbox_health.py"
    )
    spec = importlib.util.spec_from_file_location("kwork_health_under_test", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _forbidden(_text: str) -> bool:
        raise AssertionError("dry-run must not deliver")

    monkey_send = _forbidden
    # The dry-run path does not call deliver_owner_alert; keep the patch
    # so a regression that starts sending fails closed.
    mod.deliver_owner_alert = monkey_send  # type: ignore[method-assign]
    code = mod.main([
        "--status", "PAGE_NOT_READY",
        "--checked", "0",
        "--error", f"token={SECRET}",
        "--state-file", str(tmp_path / "kwork.json"),
        "--dry-run",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "none"
    assert payload["sent"] is False
    assert SECRET not in payload["text"]
    assert not (tmp_path / "kwork.json").exists()


def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "status",
    ["PAGE_NOT_READY", "page not ready"],
)
def test_page_not_ready_spellings_are_unhealthy(status):
    unhealthy, reason = classify_kwork_inbox(status, 0)
    assert unhealthy is True and reason == "page_not_ready"
