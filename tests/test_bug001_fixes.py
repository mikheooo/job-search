"""Regression and behavior tests for BUG-001 fixes.

Verifies runtime safety of:
- F821 fixes: time import in hh_message_reply, Any import in scripts.
- F811 fixes: strip_repeated_greeting single definition and casing behavior.
- RUF034 / F811 fixes: gig store _row_to_gig and tracker retention_expires_at.
"""
from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

from ai_assistant.smart_experience_generator import strip_repeated_greeting
from ai_assistant.gig.models import Gig
from ai_assistant.gig.store import _row_to_gig
from ai_assistant.gig.tracker import EncryptedSubmissionTracker, SubmissionTracker


def test_strip_repeated_greeting_behavior():
    # Empty string
    assert strip_repeated_greeting("") == ""
    # None-safe or empty
    assert strip_repeated_greeting("   ") == ""
    # Здравствуйте with punctuation
    assert strip_repeated_greeting("Здравствуйте, подскажите детали") == "Подскажите детали"
    assert strip_repeated_greeting("Добрый день! Рассмотрите резюме") == "Рассмотрите резюме"
    assert strip_repeated_greeting("Привет, когда созвон?") == "Когда созвон?"
    assert strip_repeated_greeting("Hello! Thanks for reaching out") == "Thanks for reaching out"
    # No greeting remains untouched
    assert strip_repeated_greeting("Во сколько интервью?") == "Во сколько интервью?"


def test_row_to_gig_with_dict():
    row = {
        "id": "gig_1",
        "source": "contra",
        "source_id": "c1",
        "url": "https://example.com/gig1",
        "title": "Python Automation",
        "description": "Scripting",
        "skills_json": '["python"]',
        "budget_min": 100,
        "budget_max": 200,
        "currency": "USD",
        "budget_type": "fixed",
    }
    gig = _row_to_gig(row)
    assert isinstance(gig, Gig)
    assert gig.id == "gig_1"
    assert gig.skills == ["python"]


def test_submission_tracker_retention_expires_at(tmp_path):
    db_file = tmp_path / "tracker.db"
    tracker_with_retention = EncryptedSubmissionTracker(path=db_file, retention_seconds=3600)
    expires = tracker_with_retention.retention_expires_at()
    assert expires is not None
    assert "T" in expires

    tracker_no_retention = EncryptedSubmissionTracker(path=db_file, retention_seconds=None)
    assert tracker_no_retention.retention_expires_at() is None


def test_hh_message_reply_has_time_module():
    import ai_assistant.hh_message_reply as reply_module
    assert hasattr(reply_module, "time")
    assert hasattr(reply_module.time, "sleep")


def test_scripts_import_any_cleanly():
    # Verify scripts don't raise NameError on import
    scripts = [
        "scripts.run_australia_search_cycle",
        "scripts.run_hh_monitor_cycle",
    ]
    for script_name in scripts:
        mod = importlib.import_module(script_name)
        assert mod is not None
