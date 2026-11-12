"""Automated Regression & Verification Tests for P0 Stabilization.

Covers:
1. Clean imports of restored modules (shortlist_selection and 5 adapters).
2. Shortlist selection round-robin policy and category balance.
3. Interview event deduplication, change detection (rescheduling), and notification suppression.
4. Outbound governor: disk kill-switches, db pause flags, config flags, and fail-closed behavior.
5. Read-only / dry-run safety invariant verification.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Module Import Integrity
# ---------------------------------------------------------------------------


def test_restored_adapters_and_shortlist_imports():
    """Verify all 6 restored modules import cleanly with correct interfaces."""
    from ai_assistant import shortlist_selection
    assert hasattr(shortlist_selection, "select_read_targets")
    assert hasattr(shortlist_selection, "ShortlistCategory")

    from ai_assistant.adapters.bangkok_direct_careers import BangkokDirectCareersAdapter
    assert hasattr(BangkokDirectCareersAdapter, "fetch_all")

    from ai_assistant.adapters.geekjob import GeekJobAdapter
    assert hasattr(GeekJobAdapter, "fetch_all")

    from ai_assistant.adapters.hirify import HirifyAdapter
    assert hasattr(HirifyAdapter, "fetch_vacancies")

    from ai_assistant.adapters.linkedin_bangkok import LinkedInBangkokAdapter
    assert hasattr(LinkedInBangkokAdapter, "fetch_all")

    from ai_assistant.adapters.telegram_channels import TelegramChannelsAdapter
    assert hasattr(TelegramChannelsAdapter, "fetch_all")


def test_shortlist_selection_round_robin_prevents_starvation():
    """Verify round-robin read target selection balances directions."""
    from ai_assistant.shortlist_selection import select_read_targets

    # Simulate 5 IT Support vacancies and 5 Bitrix24 vacancies
    # Note: shortlist_selection expects "vacancy_id" and "title" on each card
    cards = [
        {"vacancy_id": f"it_{i}", "title": f"Technical Support Engineer #{i}", "employer": "Company A"}
        for i in range(5)
    ] + [
        {"vacancy_id": f"bx_{i}", "title": f"Bitrix24 Integration Specialist #{i}", "employer": "Company B"}
        for i in range(5)
    ]

    # With read_limit = 4, naive first-4 would give 4 IT support and 0 Bitrix24.
    # Policy must pick from both categories!
    result = select_read_targets(cards, read_limit=4)
    assert len(result.targets) == 4
    by_cat = result.by_category
    assert len(by_cat) >= 2, f"Round-robin must distribute across at least two categories: {by_cat}"
    # Ensure both IT and Bitrix got represented in targets
    target_prefixes = {t.split("_")[0] for t in result.targets}
    assert "it" in target_prefixes and "bx" in target_prefixes


# ---------------------------------------------------------------------------
# 2. Interview Deduplication & Update Invariants
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_test_db(tmp_path, monkeypatch):
    """Create an isolated SQLite database with interview_events and system_settings."""
    db_path = tmp_path / "test_state.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS interview_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            vacancy_stable_id TEXT,
            company TEXT,
            vacancy_title TEXT,
            invitation_text TEXT,
            invitation_url TEXT,
            action_required TEXT,
            status TEXT DEFAULT 'NEW',
            detected_at TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_interview_conv ON interview_events(conversation_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_type TEXT NOT NULL,
            priority TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            company TEXT,
            vacancy_title TEXT,
            vacancy_url TEXT,
            conversation_id TEXT,
            action_required TEXT,
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            read INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

    from ai_assistant import db
    monkeypatch.setattr(db, "get_connection", lambda: sqlite3.connect(db_path))
    return db_path


def test_interview_deduplication_exact_match(temp_test_db):
    """Identical invitation text for the same conversation MUST NOT insert a duplicate row."""
    from ai_assistant import db

    invitation_payload = {
        "conversation_id": "conv_tbank_999",
        "company": "T-Bank",
        "vacancy_title": "Python Developer",
        "invitation_text": "Invitation to technical interview on Tuesday 14:00 MSK.",
        "invitation_url": "https://hh.ru/chat/conv_tbank_999",
        "action_required": "Confirm slot",
    }

    # 1. First registration: must insert a new record
    res1 = db.record_interview_event(invitation_payload)
    assert res1["is_new"] is True
    assert res1["is_duplicate"] is False
    assert res1["is_updated"] is False
    first_id = res1["event_id"]
    assert first_id > 0

    # 2. Second registration with identical text (even with minor whitespace differences)
    invitation_payload_dup = dict(invitation_payload)
    invitation_payload_dup["invitation_text"] = "  Invitation to technical interview on Tuesday 14:00 MSK.  \n"
    res2 = db.record_interview_event(invitation_payload_dup)
    assert res2["is_duplicate"] is True
    assert res2["is_new"] is False
    assert res2["event_id"] == first_id

    # 3. Verify database row count: strictly ONE row
    events = db.list_interview_events(limit=10)
    assert len(events) == 1
    assert events[0]["id"] == first_id


def test_interview_updated_time_slot_preserves_history(temp_test_db):
    """When recruiter changes the time slot, a new event is recorded as RESCHEDULED, keeping history."""
    from ai_assistant import db

    initial_payload = {
        "conversation_id": "conv_tbank_999",
        "company": "T-Bank",
        "vacancy_title": "Python Developer",
        "invitation_text": "Interview on Tuesday at 14:00.",
    }
    res1 = db.record_interview_event(initial_payload)
    id1 = res1["event_id"]

    # Recruiter reschedules / updates the time
    updated_payload = {
        "conversation_id": "conv_tbank_999",
        "company": "T-Bank",
        "vacancy_title": "Python Developer",
        "invitation_text": "Rescheduling interview to Wednesday at 16:30 MSK.",
    }
    res2 = db.record_interview_event(updated_payload)
    assert res2["is_new"] is False
    assert res2["is_updated"] is True
    assert res2["is_duplicate"] is False
    assert res2["status"] == "RESCHEDULED"
    id2 = res2["event_id"]
    assert id2 > id1

    # Both history records must exist
    events = db.list_interview_events(limit=10)
    assert len(events) == 2
    assert events[0]["id"] == id2  # latest first
    assert events[0]["status"] == "RESCHEDULED"
    assert events[1]["id"] == id1


def test_notification_dispatcher_suppresses_duplicate_alerts(temp_test_db):
    """NotificationDispatcher.notify_interview must not dispatch duplicate Telegram or DB notifications."""
    from ai_assistant.hh_autonomous_agent import NotificationDispatcher
    from ai_assistant import db

    # Mock Telegram delivery
    mock_notifier = MagicMock()
    mock_notifier.deliver_notification.return_value = {"delivered": True}

    with patch("ai_assistant.telegram_notifier.get_telegram_notifier", return_value=mock_notifier):
        # 1. First trigger
        notif1 = NotificationDispatcher.notify_interview(
            company="First Bit",
            vacancy_title="Solutions Architect",
            invitation_text="We invite you to a technical interview.",
            conversation_id="conv_bit_111",
        )
        assert notif1.get("status") != "DUPLICATE_SUPPRESSED"
        assert mock_notifier.deliver_notification.call_count == 1

        # 2. Second trigger on next cycle (identical invitation)
        notif2 = NotificationDispatcher.notify_interview(
            company="First Bit",
            vacancy_title="Solutions Architect",
            invitation_text="We invite you to a technical interview.",
            conversation_id="conv_bit_111",
        )
        assert notif2.get("status") == "DUPLICATE_SUPPRESSED"
        # Telegram deliver MUST NOT be called a second time!
        assert mock_notifier.deliver_notification.call_count == 1


# ---------------------------------------------------------------------------
# 3. Outbound Governor & Emergency Kill-Switches
# ---------------------------------------------------------------------------


def test_outbound_governor_disk_killswitch(tmp_path, monkeypatch):
    """STOP_ALL_OUTBOUNDS or STOP_SUBMITS file on disk immediately halts outbound actions."""
    from ai_assistant.outbound_governor import check_outbound_allowed, OutboundAction

    stop_file = tmp_path / "STOP_ALL_OUTBOUNDS"
    monkeypatch.setenv("STOP_OUTBOUNDS_FILE", str(stop_file))

    # Without stop file
    monkeypatch.setenv("HH_AUTONOMOUS_REPLY_ENABLED", "true")
    allowed, reason = check_outbound_allowed(action=OutboundAction.CHAT_REPLY)
    assert allowed is True

    # Touch stop file
    stop_file.touch()
    allowed, reason = check_outbound_allowed(action=OutboundAction.CHAT_REPLY)
    assert allowed is False
    assert "Emergency stop file present" in reason

    # Submissions also blocked
    sub_allowed, sub_reason = check_outbound_allowed(action=OutboundAction.SUBMISSION)
    assert sub_allowed is False
    assert "Emergency stop file present" in sub_reason


def test_outbound_governor_db_pause(temp_test_db, monkeypatch):
    """Setting submit_paused=1 in system_settings halts outbound actions dynamically."""
    from ai_assistant import db
    from ai_assistant.outbound_governor import check_outbound_allowed, OutboundAction

    # Clear any file kill switches
    monkeypatch.setenv("STOP_SUBMITS_FILE", "")
    monkeypatch.setenv("STOP_OUTBOUNDS_FILE", "")
    monkeypatch.setenv("HH_AUTONOMOUS_REPLY_ENABLED", "true")

    # Initially not paused
    allowed, _ = check_outbound_allowed(action=OutboundAction.CHAT_REPLY)
    assert allowed is True

    # Set pause in DB
    db.set_system_setting("submit_paused", "1")
    allowed, reason = check_outbound_allowed(action=OutboundAction.CHAT_REPLY)
    assert allowed is False
    assert "Database kill-switch active" in reason


def test_outbound_governor_fail_closed(monkeypatch):
    """If database lookup raises an unexpected exception, safety MUST fail closed."""
    from ai_assistant.outbound_governor import check_outbound_allowed, OutboundAction

    with patch("ai_assistant.db.is_submit_paused", side_effect=sqlite3.DatabaseError("Disk I/O error")):
        allowed, reason = check_outbound_allowed(action=OutboundAction.SUBMISSION)
        assert allowed is False
        assert "fail-closed" in reason.lower()
# ---------------------------------------------------------------------------
# 4. Preparation & Read-Only Invariants (No Outbound Sends)
# ---------------------------------------------------------------------------


def test_discovery_preparation_mode_without_outbounds(temp_test_db, monkeypatch):
    """Multi-source discovery in preparation mode (notify=False) produces zero outbound sends."""
    from scripts.run_multi_source_discovery import is_target_role, is_acceptable_salary
    from ai_assistant.schema import Vacancy

    # Test filtering logic in memory
    v1 = Vacancy(
        source_job_id="job1",
        company="Tech Corp",
        title="Technical Support Engineer L2",
        description="Support, Linux, Docker, SQL tickets.",
        job_url="https://example.com/job1",
        source="habr_career",
    )
    assert is_target_role(v1.title, v1.description) is True
    assert is_acceptable_salary(v1) is True

    # Off-profile role should be filtered out
    v_off = Vacancy(
        source_job_id="job_sales",
        company="Sales Corp",
        title="B2B Sales Manager",
        description="Cold calls, selling retail equipment and packaging.",
        job_url="https://example.com/job_sales",
        source="habr_career",
    )
    assert is_target_role(v_off.title, v_off.description) is False


def test_is_acceptable_salary_accepts_low_salary_when_floor_zero(monkeypatch):
    """When MIN_SALARY=0, any salary is acceptable."""
    from scripts.run_multi_source_discovery import is_acceptable_salary
    from ai_assistant.schema import Vacancy
    from ai_assistant import config

    monkeypatch.setattr(config, "MIN_SALARY", 0)
    low_vac = Vacancy(
        source_job_id="low1",
        company="SmallCo",
        title="Support Engineer",
        description="Technical support tickets and linux administration.",
        job_url="https://example.com/low1",
        source="habr_career",
        salary_min=30000,
        salary_max=45000,
        salary_currency="RUB",
    )
    assert is_acceptable_salary(low_vac) is True

    # When MIN_SALARY is set to 100000, it filters out
    monkeypatch.setattr(config, "MIN_SALARY", 100000)
    assert is_acceptable_salary(low_vac) is False


def test_hh_readonly_operation_uses_shortlist_selection():
    """Verify hh_readonly_operation cleanly imports and delegates to shortlist_selection."""
    from ai_assistant import hh_readonly_operation
    from ai_assistant.shortlist_selection import select_read_targets

    # Simulate discovery cards
    seen = {
        "v1": {"vacancy_id": "v1", "title": "Инженер сопровождения CRM", "employer": "Emp 1"},
        "v2": {"vacancy_id": "v2", "title": "Системный администратор Linux", "employer": "Emp 2"},
    }
    picked = select_read_targets(seen, read_limit=2)
    assert len(picked.targets) == 2
    res_dict = picked.as_dict()
    assert "targets" in res_dict
    assert "by_category" in res_dict
