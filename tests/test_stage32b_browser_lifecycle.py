"""Stage 32B: Tests for HH Browser Lifecycle and Launcher.

Verifies:
1. Chrome executable discovery on Windows.
2. Port and profile validation (9222 and C:\\Users\\Misha\\chrome-cdp-profile).
3. Reusing existing running browser (no duplicate Chrome spawn).
4. Launching Chrome when absent with correct remote debugging flags.
5. Session authentication detection (reports BLOCKED when login required).
6. Message watcher integration with auto-start browser lifecycle.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_browser_launcher import (
    DEFAULT_HH_CDP_PORT,
    DEFAULT_HH_CDP_URL,
    DEFAULT_HH_PROFILE_DIR,
    check_hh_session_authenticated,
    ensure_hh_browser,
    find_chrome_executable,
    get_cdp_targets,
    get_cdp_version_info,
    is_cdp_reachable,
)
from ai_assistant.hh_message_watcher import (
    HHMessageWatcherConfig,
    run_message_watcher_cycle,
)


def _create_test_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["AI Automation Engineer", "Python Developer"],
        skills=["Python", "FastAPI", "n8n", "Docker", "PostgreSQL", "LLM"],
        preferred_seniority=["Senior", "Lead"],
        remote_required=True,
        allowed_locations=["Remote", "Worldwide"],
        allowed_timezones=[],
        languages=["English", "Russian"],
        employment_types=["Full-time"],
        minimum_salary=5000,
        salary_currency="USD",
        years_experience="5",
        excluded_roles=["DevOps"],
        excluded_companies=["SpammyCorp"],
        excluded_countries=[],
        excluded_industries=[],
    )


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_lifecycle.db")
    config.DB_FILE = db_file
    db.init_db()

    profile = _create_test_profile()
    profile_path = str(tmp_path / "test_profile.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f)

    yield {"db_file": db_file, "profile_path": profile_path, "profile": profile}

    config.DB_FILE = orig_db


def test_find_chrome_executable():
    """Verify Chrome executable locator finds binary on Windows."""
    exe = find_chrome_executable()
    # In standard Windows environment with Chrome installed:
    if os.path.exists(r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        assert exe == r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    elif exe is not None:
        assert os.path.isfile(exe)


def test_default_ports_and_profile():
    """Verify default CDP port is 9222 and profile is chrome-cdp-profile."""
    assert DEFAULT_HH_CDP_PORT == 9222
    assert "9222" in DEFAULT_HH_CDP_URL
    assert "chrome-cdp-profile" in DEFAULT_HH_PROFILE_DIR


def test_ensure_hh_browser_reused_when_already_running():
    """When CDP endpoint is already reachable, browser is reused without spawning a new process."""
    with patch("ai_assistant.hh_browser_launcher.is_cdp_reachable", return_value=True), \
         patch("ai_assistant.hh_browser_launcher.get_cdp_version_info", return_value={"Browser": "Chrome/120.0"}), \
         patch("ai_assistant.hh_browser_launcher.get_cdp_targets", return_value=[{"type": "page", "url": "https://hh.ru/chat/1"}]), \
         patch("subprocess.Popen") as mock_popen:

        res = ensure_hh_browser(cdp_url="http://127.0.0.1:9222", auto_start=True)

        assert res["ok"] is True
        assert res["reused"] is True
        assert res["pid"] is None
        mock_popen.assert_not_called()


def test_ensure_hh_browser_starts_chrome_when_missing():
    """When CDP endpoint is unreachable, launcher spawns Chrome with correct port and profile."""
    # First check False (not running), then True (ready after launch)
    reachable_calls = [False, True]

    def mock_reachable(*args, **kwargs):
        if reachable_calls:
            return reachable_calls.pop(0)
        return True

    mock_proc = MagicMock()
    mock_proc.pid = 99999

    with patch("ai_assistant.hh_browser_launcher.is_cdp_reachable", side_effect=mock_reachable), \
         patch("ai_assistant.hh_browser_launcher.find_chrome_executable", return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe"), \
         patch("ai_assistant.hh_browser_launcher.get_cdp_version_info", return_value={"Browser": "Chrome/120.0"}), \
         patch("ai_assistant.hh_browser_launcher.get_cdp_targets", return_value=[{"type": "page", "url": "https://hh.ru/applicant/negotiations"}]), \
         patch("subprocess.Popen", return_value=mock_proc) as mock_popen:

        res = ensure_hh_browser(
            cdp_url="http://127.0.0.1:9222",
            profile_dir=r"C:\Users\Misha\chrome-cdp-profile",
            auto_start=True,
            timeout_seconds=5.0,
        )

        assert res["ok"] is True
        assert res["reused"] is False
        assert res["pid"] == 99999

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        assert "--remote-debugging-port=9222" in cmd
        assert "--user-data-dir=C:\\Users\\Misha\\chrome-cdp-profile" in cmd


def test_ensure_hh_browser_timeout_fails_closed():
    """When Chrome process starts but CDP does not respond within timeout, fails closed."""
    mock_proc = MagicMock()
    mock_proc.pid = 88888

    with patch("ai_assistant.hh_browser_launcher.is_cdp_reachable", return_value=False), \
         patch("ai_assistant.hh_browser_launcher.find_chrome_executable", return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe"), \
         patch("subprocess.Popen", return_value=mock_proc):

        res = ensure_hh_browser(
            cdp_url="http://127.0.0.1:9222",
            auto_start=True,
            timeout_seconds=0.5,
        )

        assert res["ok"] is False
        assert "did not become reachable" in res["error"]


def test_unauthenticated_session_detection():
    """When page is on login page or requires login, reports authenticated=False."""
    def fake_eval_login(js):
        return json.dumps({"authenticated": False, "reason": "Browser is on HH login page", "url": "https://hh.ru/account/login"})

    def fake_eval_logged_in(js):
        return json.dumps({"authenticated": True, "reason": "Applicant profile navigation element found", "url": "https://hh.ru/applicant/negotiations"})

    res_login = check_hh_session_authenticated(fake_eval_login)
    assert res_login["authenticated"] is False
    assert "login" in res_login["reason"].lower()

    res_auth = check_hh_session_authenticated(fake_eval_logged_in)
    assert res_auth["authenticated"] is True


def test_watcher_stops_if_browser_fails_launch(clean_db):
    """Watcher records clear BLOCKED reason if browser cannot be launched."""
    with patch("ai_assistant.hh_browser_launcher.ensure_hh_browser", return_value={"ok": False, "error": "Chrome executable not found"}):
        cfg = HHMessageWatcherConfig(
            profile_path=clean_db["profile_path"],
            auto_start_browser=True,
        )
        res = run_message_watcher_cycle(cfg)
        assert res.blocked == 1
        assert res.new_messages == 0
        assert any("Chrome executable not found" in err for err in res.errors)
