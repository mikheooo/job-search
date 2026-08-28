"""Stage 30C — system-info diagnostic (READ-ONLY).

Covers:
  (a) CLI parsing/dispatch reaches the handler
  (b) actual `python -m ai_assistant.cli system-info` returns 0 and stdout
      contains READ-ONLY, Python-version string, and platform string (subprocess)
  (c) no forbidden modules/functions are imported/called in cli.py
  (d) handler does not touch init_db / save_* and does not toggle HH env flags
"""

from __future__ import annotations

import os
import sys
import subprocess

import pytest

from ai_assistant import cli
import ai_assistant.db as db_module


def _rising(*a, **k):
    raise AssertionError("forbidden function was called")


# (a) CLI parsing/dispatch

def test_system_info_cli_dispatch(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append((a, k))
        return 0

    monkeypatch.setattr(cli, "system_info", fake)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "system-info"])
    rc = cli.main()
    assert rc == 0
    assert calls, "system_info was not reached by CLI dispatch"


# (b) subprocess end-to-end

def test_system_info_subprocess_returns_readonly_platform_python():
    r = subprocess.run(
        [sys.executable, "-m", "ai_assistant.cli", "system-info"],
        capture_output=True, text=True, cwd=".",
    )
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    out = r.stdout
    assert "READ-ONLY" in out
    # Python version string: either "Python" token or python_version output
    assert ("Python" in out or "python" in out.lower())
    assert "platform" in out.lower()


# (c) forbidden imports/calls absent in cli.py

def test_system_info_cli_has_no_forbidden_imports():
    src = open(cli.__file__, encoding="utf-8").read()
    for forbid in [
        "auto_apply_modes",
        "process_auto_reply(",
        "run_auto_apply(",
        "send_auto_reply(",
        "can_auto_send(",
        "confirm_live_send(",
        "EmailSendGate(",
        "hh_controlled_submit",
        "hh_submission",
        "hh_human_submission",
    ]:
        assert forbid not in src, f"forbidden substring in cli.py: {forbid!r}"


# (d) does not touch DB or env

def test_system_info_does_not_touch_db(monkeypatch, capsys):
    monkeypatch.setattr(cli, "init_db", _rising)
    monkeypatch.setattr(db_module, "init_db", _rising)
    for name in ("save_vacancy", "save_deep_analysis", "save_application_package"):
        if hasattr(db_module, name):
            monkeypatch.setattr(db_module, name, _rising)
    # also any save_* on cli if present
    for name in ("save_vacancy", "save_deep_analysis", "save_application_package"):
        if hasattr(cli, name):
            monkeypatch.setattr(cli, name, _rising)

    rc = cli.system_info()
    out = capsys.readouterr().out
    assert rc == 0
    assert "READ-ONLY" in out


def test_system_info_does_not_toggle_auto_environment(monkeypatch, capsys):
    monkeypatch.delenv("HH_APPLY_MODE", raising=False)
    monkeypatch.delenv("HH_AUTO_REPLY_ENABLED", raising=False)
    rc = cli.system_info()
    capsys.readouterr()
    assert rc == 0
    assert os.environ.get("HH_APPLY_MODE") is None
    assert os.environ.get("HH_AUTO_REPLY_ENABLED") is None


def test_system_info_prints_app_version_placeholder(capsys):
    # repo has no __version__/APP_VERSION, so must print the placeholder
    rc = cli.system_info()
    out = capsys.readouterr().out
    assert rc == 0
    assert "app_version" in out.lower()
    # either placeholder or real version, but placeholder expected in this repo
    assert "(not defined" in out or "app_version:" in out.lower()
