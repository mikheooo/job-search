"""Phase 4A.2 offline tests: HH auth recovery & failure behavior.

Pure/unit level only: no browser launch, no network (conftest blocks urlopen),
no prod DB, no real submits or sends. CDP entry points are stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import time

import pytest

from ai_assistant.hh_browser_provider import (
    HH_AUTH_OK,
    HH_AUTH_REQUIRED,
    auth_gate,
    decide_authentication,
    launch_headless,
    require_hh_auth,
)


def _js(**fields) -> str:
    base = {
        "login_card_visible": False,
        "login_input_present": False,
        "profile_link_href": "",
        "hhrole": "applicant",
        "applicant_ui_present": False,
        "path": "/applicant/profile/me",
    }
    base.update(fields)
    return json.dumps(base)


# Expired session: login card rendered.
def test_expired_session_detected():
    d = decide_authentication(json.loads(_js(login_card_visible=True)))
    assert d["authenticated"] is False and d["verified"] is True
    assert auth_gate(d)["status"] == HH_AUTH_REQUIRED


# Valid session: applicant UI, no login card.
def test_valid_session_accepted():
    d = decide_authentication(json.loads(_js(applicant_ui_present=True)))
    assert d["authenticated"] is True and d["verified"] is True
    assert auth_gate(d)["status"] == HH_AUTH_OK


# CDP/read error and ambiguous pages fail closed (never as authenticated).
def test_unreadable_and_ambiguous_fail_closed():
    d1 = decide_authentication({"error": "TimeoutError: tab gone"})
    assert d1["authenticated"] is False and d1["verified"] is False
    d2 = decide_authentication(json.loads(_js()))
    assert d2["authenticated"] is False and d2["verified"] is False
    assert "proves nothing" in d2["reason"]


# Re-auth flow: login page first, applicant UI after (simulated).
def test_reauth_flow_transitions():
    before = require_hh_auth(lambda js: _js(login_card_visible=True))
    assert before["status"] == HH_AUTH_REQUIRED
    after = require_hh_auth(lambda js: _js(applicant_ui_present=True))
    assert after["status"] == HH_AUTH_OK


# Wrong profile / missing executable: loud refusal, no hang, no browser.
def test_bad_chrome_exe_refuses_cleanly(monkeypatch):
    import ai_assistant.hh_browser_provider as bp

    monkeypatch.setattr(bp, "port_is_listening", lambda port: False)
    res = launch_headless(
        cdp_url="http://127.0.0.1:59911",
        profile_dir="C:\\definitely\\not\\a\\profile",
        chrome_exe="C:\\definitely-not-chrome.exe",
        timeout=5.0,
    )
    assert isinstance(res, dict) and res.get("ok") is False


def _load_cycle_runner():
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "run_hh_autopilot_cycle.py"
    )
    spec = importlib.util.spec_from_file_location("hh_cycle_auth_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cycle(tmp_path, monkeypatch):
    mod = _load_cycle_runner()
    monkeypatch.setattr(mod, "LOCK_FILE", tmp_path / "autopilot.lock")
    monkeypatch.setattr(mod, "AUTH_ALERT_MARKER", tmp_path / "hh_auth_alert.sent")
    monkeypatch.setattr(mod, "AUTH_ALERT_SUPPRESS_SECONDS", 12 * 3600)
    monkeypatch.setattr(mod, "OPS_ALERT_STATE", tmp_path / "hh_autopilot_ops_alert.json")
    monkeypatch.setattr(mod, "OPS_ALERT_COOLDOWN_SECONDS", 6 * 3600)
    monkeypatch.setattr(mod, "AUTH_RECHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(mod, "_run_message_agent", lambda: None)
    monkeypatch.setattr("sys.argv", ["prog"])
    return mod


def _capture_alerts(monkeypatch, cycle):
    sent: list[str] = []
    monkeypatch.setattr(cycle, "_deliver_owner_alert", lambda text: sent.append(text) or True)
    return sent


def _stub_cycle_env(monkeypatch, cycle, result):
    monkeypatch.setattr(cycle, "_bump_resumes", lambda: {"status": "SKIPPED"})
    monkeypatch.setattr(cycle, "_run_scheduled_autopilot", lambda **kw: result)


# Auth ERROR result: exit 1 (not false success) + exactly one alert.
def test_error_result_exits_1_with_single_alert(cycle, tmp_path, monkeypatch):
    _stub_cycle_env(
        monkeypatch,
        cycle,
        {"status": "ERROR",
         "error": "the browser session is not usable: HH_AUTH_REQUIRED test",
         "submitted": 0},
    )
    monkeypatch.setattr(
        cycle,
        "_check_hh_auth_now",
        lambda: {"status": "HH_AUTH_REQUIRED", "message": "login", "auth": None},
    )
    sent = _capture_alerts(monkeypatch, cycle)
    assert cycle.main() == 1
    assert len(sent) == 1, "exactly one informative alert"
    assert "HH_AUTH_REQUIRED" in sent[0]
    # Second failing run inside the window: still exit 1, no second alert.
    _stub_cycle_env(
        monkeypatch,
        cycle,
        {"status": "ERROR", "error": "HH_AUTH_REQUIRED again", "submitted": 0},
    )
    assert cycle.main() == 1
    assert len(sent) == 1, "alerts must not repeat inside suppression window"


# Non-auth errors: exit 1 and one operational alert, not an auth alert.
def test_nonauth_error_sends_operational_alert(cycle, tmp_path, monkeypatch):
    secret = "bot123456:AAFakeTokenValueForTestOnlyXXXX"
    _stub_cycle_env(
        monkeypatch,
        cycle,
        {
            "status": "ERROR",
            "error": (
                "cannot import name 'hh_structured_preflight' "
                f"from /home/misha/job-search password={secret}"
            ),
            "submitted": 0,
        },
    )
    sent = _capture_alerts(monkeypatch, cycle)
    assert cycle.main() == 1
    assert len(sent) == 1
    assert "hh_structured_preflight" in sent[0]
    assert "Отправлено откликов в этом цикле: 0" in sent[0]
    assert "не потеря авторизации" in sent[0]
    assert "HH_AUTH_REQUIRED" not in sent[0]
    assert secret not in sent[0]
    assert "misha" not in sent[0]
    assert cycle.main() == 1
    assert len(sent) == 1, "the same non-auth failure stays inside the cooldown"
    _stub_cycle_env(
        monkeypatch,
        cycle,
        {"status": "SUCCESS", "submitted": 0, "applications": []},
    )
    assert cycle.main() == 0
    assert len(sent) == 2
    assert "снова завершился без этой ошибки" in sent[1]


def test_unhandled_import_error_alerts_without_secrets(cycle, monkeypatch):
    secret = "bot123456:AAFakeTokenValueForTestOnlyXXXX"

    def _boom(**_kwargs):
        raise ImportError(
            f"cannot import name 'hh_structured_preflight' password={secret}"
        )

    monkeypatch.setattr(cycle, "_bump_resumes", lambda: {"status": "SKIPPED"})
    monkeypatch.setattr(cycle, "_run_scheduled_autopilot", _boom)
    sent = _capture_alerts(monkeypatch, cycle)
    assert cycle.main() == 1
    assert len(sent) == 1
    assert "hh_structured_preflight" in sent[0]
    assert secret not in sent[0]
    assert "HH_AUTH_REQUIRED" not in sent[0]
    assert "не потеря авторизации" in sent[0]


# Success path with no prior failure: exit 0, no alert.
def test_success_path_unchanged(cycle, tmp_path, monkeypatch):
    _stub_cycle_env(
        monkeypatch,
        cycle,
        {"status": "SUCCESS", "submitted": 0, "applications": []},
    )
    sent = _capture_alerts(monkeypatch, cycle)
    assert cycle.main() == 0
    assert sent == []


# Suppression expiry: old marker allows a fresh alert.
def test_alert_suppression_expires(cycle, tmp_path, monkeypatch):
    sent = _capture_alerts(monkeypatch, cycle)
    assert cycle._maybe_notify_auth_failure("HH_AUTH_REQUIRED x") is True
    assert cycle._maybe_notify_auth_failure("HH_AUTH_REQUIRED x") is False
    old = time.time() - 13 * 3600
    import os

    os.utime(tmp_path / "hh_auth_alert.sent", (old, old))
    assert cycle._maybe_notify_auth_failure("HH_AUTH_REQUIRED x") is True
    assert len(sent) == 2
