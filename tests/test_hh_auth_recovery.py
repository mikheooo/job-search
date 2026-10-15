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
    HH_AUTH_REQUIRED,
    HH_AUTH_OK,
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
    monkeypatch.setattr("sys.argv", ["prog"])
    return mod


def _stub_cycle_env(monkeypatch, result):
    import ai_assistant.hh_autopilot as ha
    import ai_assistant.hh_resume_bumper as hb

    monkeypatch.setattr(hb, "bump_hh_resumes", lambda: {"status": "SKIPPED"})
    monkeypatch.setattr(ha, "run_autopilot_cycle", lambda **kw: result)


# Auth ERROR result: exit 1 (not false success) + exactly one alert.
def test_error_result_exits_1_with_single_alert(cycle, tmp_path, monkeypatch):
    _stub_cycle_env(
        monkeypatch,
        {"status": "ERROR",
         "error": "the browser session is not usable: HH_AUTH_REQUIRED test",
         "submitted": 0},
    )
    import ai_assistant.hh_autopilot as ha

    sent = []
    monkeypatch.setattr(ha, "send_telegram_alert", lambda text: sent.append(text) or True)
    assert cycle.main() == 1
    assert len(sent) == 1, "exactly one informative alert"
    # Second failing run inside the window: still exit 1, no second alert.
    _stub_cycle_env(
        monkeypatch,
        {"status": "ERROR", "error": "HH_AUTH_REQUIRED again", "submitted": 0},
    )
    assert cycle.main() == 1
    assert len(sent) == 1, "alerts must not repeat inside suppression window"


# Non-auth errors: honest exit 1, no auth alert.
def test_nonauth_error_no_alert(cycle, tmp_path, monkeypatch):
    _stub_cycle_env(
        monkeypatch,
        {"status": "ERROR", "error": "disk quota exceeded (test)", "submitted": 0},
    )
    import ai_assistant.hh_autopilot as ha

    sent = []
    monkeypatch.setattr(ha, "send_telegram_alert", lambda text: sent.append(text) or True)
    assert cycle.main() == 1
    assert sent == []


# Success path unchanged: exit 0, no alert.
def test_success_path_unchanged(cycle, tmp_path, monkeypatch):
    _stub_cycle_env(
        monkeypatch,
        {"status": "SUCCESS", "submitted": 0, "applications": []},
    )
    import ai_assistant.hh_autopilot as ha

    sent = []
    monkeypatch.setattr(ha, "send_telegram_alert", lambda text: sent.append(text) or True)
    assert cycle.main() == 0
    assert sent == []


# Suppression expiry: old marker allows a fresh alert.
def test_alert_suppression_expires(cycle, tmp_path, monkeypatch):
    import ai_assistant.hh_autopilot as ha

    sent = []
    monkeypatch.setattr(ha, "send_telegram_alert", lambda text: sent.append(text) or True)
    assert cycle._maybe_notify_auth_failure("HH_AUTH_REQUIRED x") is True
    assert cycle._maybe_notify_auth_failure("HH_AUTH_REQUIRED x") is False
    old = time.time() - 13 * 3600
    import os

    os.utime(tmp_path / "hh_auth_alert.sent", (old, old))
    assert cycle._maybe_notify_auth_failure("HH_AUTH_REQUIRED x") is True
    assert len(sent) == 2
