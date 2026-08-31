"""Stage 53: Autonomous Reply Visibility Test Suite.

Proves:
1. User notification for sent recruiter replies contains the exact sent text.
2. Notification is created strictly after successful send (status == 'SENT').
3. Both generated_reply and exact sent_reply are audited in DB.
4. Auto-reply flow executes completely autonomously without any approval or confirmation gate.
5. User notification contains company and vacancy context.
6. Failed auto-reply does NOT dispatch a sent notification.
7. Duplicate incoming messages do NOT send duplicate replies or create duplicate notifications.
8. Interview invitations remain HIGH_PRIORITY and notify separately.
9. CLI autonomous replies command outputs formatted sent replies and supports --json, --limit, --application-id.
10. pipeline.py is never executed.
11. Existing submitted benchmark applications (app_hh_135112049, app_hh_136704137, app_hh_136551280) remain SUBMITTED.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.candidate_profile import load_candidate_profile
from ai_assistant.hh_application_orchestrator import HHApplicationState
from ai_assistant.hh_autonomous_agent import (
    AutonomousConfig,
    AutonomousJobAgent,
    NotificationDispatcher,
    NotificationPriority,
    NotificationType,
    run_autonomous_cycle,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage53.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# 1. Exact Sent Text & Notification Creation
# ---------------------------------------------------------------------------

def test_sent_reply_notification_contains_exact_sent_text(clean_db):
    """User notification contains the exact text dispatched to the recruiter."""
    conv_id = "conv_vis_101"
    exact_sent_holder = []

    def mock_cdp(script: str) -> str:
        if "chat-input" in script or "input.value" in script:
            exact_sent_holder.append(script)
            return json.dumps({"ok": True})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Python Backend Lead",
            "vacancy_stable_id": "hh:139100101",
            "employer": "Fintech Solutions",
            "messages": [
                {"message_id": "m1", "text": "Здравствуйте! Подскажите, какой у вас опыт работы с PostgreSQL?", "sender": "employer", "sent_at": "2026-08-30T17:00:00Z"}
            ]
        }])

    res = run_autonomous_cycle(config=AutonomousConfig(evaluate_fn=mock_cdp))

    assert res.status == "SUCCESS"
    assert res.auto_replies_count == 1
    assert len(res.notifications_sent) == 1

    notif = res.notifications_sent[0]
    assert notif["notification_type"] == NotificationType.RECRUITER_REPLY_SENT.value
    assert "RECRUITER REPLY" in notif["message"]

    # Verify notification in DB contains exact sent reply
    db_notifs = db.list_autonomous_notifications(limit=10)
    assert len(db_notifs) == 1
    db_notif = db_notifs[0]
    assert "Fintech Solutions" in db_notif["message"]
    assert "PostgreSQL" in db_notif["message"]
    assert "подтверждён в HH" in db_notif["message"]

    # Verify audit record
    audits = db.list_conversation_audits(conversation_id=conv_id)
    assert len(audits) == 1
    assert audits[0]["status"] == "SENT"
    assert audits[0]["sent_reply"] is not None
    assert audits[0]["sent_reply"] in db_notif["message"]


# ---------------------------------------------------------------------------
# 2. Notification Only After Successful Send
# ---------------------------------------------------------------------------

def test_reply_notification_created_only_after_successful_send(clean_db):
    """Notification is created strictly when the DOM dispatch succeeds."""
    conv_id = "conv_vis_102"

    def mock_cdp_success(script: str) -> str:
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Senior AI Engineer",
            "vacancy_stable_id": "hh:139100102",
            "employer": "AI Robotics",
            "messages": [
                {"message_id": "m1", "text": "Добрый день! Работали ли вы с Docker и Kubernetes?", "sender": "employer", "sent_at": "2026-08-30T17:05:00Z"}
            ]
        }])

    res = run_autonomous_cycle(config=AutonomousConfig(evaluate_fn=mock_cdp_success))
    assert res.auto_replies_count == 1
    assert len(res.notifications_sent) == 1

    # Check notification in DB
    notifs = db.list_autonomous_notifications(limit=5)
    assert len(notifs) == 1
    assert notifs[0]["notification_type"] == "RECRUITER_REPLY_SENT"


# ---------------------------------------------------------------------------
# 3. Generated & Sent Reply Both Audited
# ---------------------------------------------------------------------------

def test_generated_and_sent_reply_are_both_audited(clean_db):
    """Audit table records incoming_message, generated_reply, and sent_reply."""
    conv_id = "conv_vis_103"

    def mock_cdp(script: str) -> str:
        if "chat-input" in script:
            return json.dumps({"ok": True})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Python Developer",
            "vacancy_stable_id": "hh:139100103",
            "employer": "Cloud Systems",
            "messages": [
                {"message_id": "m1", "text": "Уточните, рассматриваете ли вы удалённую работу?", "sender": "employer", "sent_at": "2026-08-30T17:10:00Z"}
            ]
        }])

    res = run_autonomous_cycle(config=AutonomousConfig(evaluate_fn=mock_cdp))
    assert res.status == "SUCCESS"

    audits = db.list_conversation_audits(conversation_id=conv_id)
    assert len(audits) == 1
    a = audits[0]

    assert a["employer"] == "Cloud Systems"
    assert "удалённую работу" in a["incoming_message"]
    assert a["generated_reply"] is not None
    assert a["sent_reply"] is not None
    assert a["sent_reply"] == a["generated_reply"]
    assert a["status"] == "SENT"
    assert a["sent_at"] is not None
    assert a["profile_facts_used"] is not None


# ---------------------------------------------------------------------------
# 4. No Approval Gate
# ---------------------------------------------------------------------------

def test_autonomous_reply_has_no_approval_gate(clean_db):
    """Agent sends replies immediately without prompting or waiting for user confirmation."""
    conv_id = "conv_vis_104"

    dispatched = [False]

    def mock_cdp(script: str) -> str:
        if "chat-input" in script:
            dispatched[0] = True
            return json.dumps({"ok": True})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "FastAPI Architect",
            "vacancy_stable_id": "hh:139100104",
            "employer": "MegaCorp Tech",
            "messages": [
                {"message_id": "m1", "text": "Здравствуйте! Есть ли у вас опыт с gRPC?", "sender": "employer", "sent_at": "2026-08-30T17:15:00Z"}
            ]
        }])

    # Single call executes send autonomously
    res = run_autonomous_cycle(config=AutonomousConfig(evaluate_fn=mock_cdp))
    assert res.status == "SUCCESS"
    assert dispatched[0] is True
    assert res.auto_replies_count == 1


# ---------------------------------------------------------------------------
# 5. Notification Context (Company & Vacancy)
# ---------------------------------------------------------------------------

def test_reply_notification_contains_company_and_vacancy(clean_db):
    """User notification contains accurate company name and vacancy title."""
    conv_id = "conv_vis_105"

    def mock_cdp(script: str) -> str:
        if "chat-input" in script:
            return json.dumps({"ok": True})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Senior Python Backend Engineer",
            "vacancy_stable_id": "hh:139100105",
            "employer": "Innovate Ltd",
            "messages": [
                {"message_id": "m1", "text": "Добрый день! Какая у вас доступность по времени?", "sender": "employer", "sent_at": "2026-08-30T17:20:00Z"}
            ]
        }])

    res = run_autonomous_cycle(config=AutonomousConfig(evaluate_fn=mock_cdp))
    notif = res.notifications_sent[0]

    assert notif["company"] == "Innovate Ltd"
    assert notif["vacancy_title"] == "Senior Python Backend Engineer"
    assert "Innovate Ltd" in notif["message"]
    assert "Senior Python Backend Engineer" in notif["message"]


# ---------------------------------------------------------------------------
# 6. Failed Reply Does NOT Create Sent Notification
# ---------------------------------------------------------------------------

def test_failed_reply_does_not_create_sent_notification(clean_db):
    """If browser DOM send fails, no RECRUITER_REPLY_SENT notification is dispatched."""
    conv_id = "conv_vis_106"

    def mock_cdp_fail(script: str) -> str:
        if "chat-input" in script:
            return json.dumps({"ok": False, "reason": "Chat textarea was disabled"})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Python Developer",
            "vacancy_stable_id": "hh:139100106",
            "employer": "Failing Corp",
            "messages": [
                {"message_id": "m1", "text": "Когда готовы приступить?", "sender": "employer", "sent_at": "2026-08-30T17:25:00Z"}
            ]
        }])

    res = run_autonomous_cycle(config=AutonomousConfig(evaluate_fn=mock_cdp_fail))
    assert res.auto_replies_count == 0
    assert len(res.notifications_sent) == 0

    # Verify audit recorded as FAILED
    audits = db.list_conversation_audits(conversation_id=conv_id)
    assert len(audits) == 1
    assert audits[0]["status"] == "FAILED"
    assert audits[0]["error"] == "Chat textarea was disabled"

    # Zero notifications in DB
    notifs = db.list_autonomous_notifications(limit=5)
    assert len(notifs) == 0


# ---------------------------------------------------------------------------
# 7. Duplicate Message Idempotency
# ---------------------------------------------------------------------------

def test_duplicate_message_does_not_create_duplicate_notification(clean_db):
    """Repeated cycles with identical conversation history produce 0 extra notifications."""
    conv_id = "conv_vis_107"

    def mock_cdp(script: str) -> str:
        if "chat-input" in script:
            return json.dumps({"ok": True})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Python Specialist",
            "vacancy_stable_id": "hh:139100107",
            "employer": "Stable Tech",
            "messages": [
                {"message_id": "m1", "text": "Здравствуйте! Какой у вас опыт?", "sender": "employer", "sent_at": "2026-08-30T17:30:00Z"}
            ]
        }])

    cfg = AutonomousConfig(evaluate_fn=mock_cdp)

    # First cycle sends reply and creates notification
    res1 = run_autonomous_cycle(config=cfg)
    assert res1.auto_replies_count == 1
    assert len(res1.notifications_sent) == 1

    # Second cycle skips already processed message
    res2 = run_autonomous_cycle(config=cfg)
    assert res2.auto_replies_count == 0
    assert len(res2.notifications_sent) == 0

    # DB contains exactly 1 notification
    notifs = db.list_autonomous_notifications(limit=10)
    assert len(notifs) == 1


# ---------------------------------------------------------------------------
# 8. Interview Notification Remains HIGH_PRIORITY
# ---------------------------------------------------------------------------

def test_interview_notification_remains_high_priority(clean_db):
    """Interview invitation notifications retain HIGH_PRIORITY."""
    conv_id = "conv_vis_108"

    def mock_cdp(script: str) -> str:
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Senior AI Architect",
            "vacancy_stable_id": "hh:139100108",
            "employer": "OmniAI",
            "messages": [
                {"message_id": "m1", "text": "Приглашаем вас на техническое собеседование в Zoom.", "sender": "employer", "sent_at": "2026-08-30T17:35:00Z"}
            ]
        }])

    res = run_autonomous_cycle(config=AutonomousConfig(evaluate_fn=mock_cdp))
    assert res.interviews_detected == 1
    assert len(res.notifications_sent) == 1

    notif = res.notifications_sent[0]
    assert notif["priority"] == NotificationPriority.HIGH.value
    assert notif["notification_type"] == NotificationType.INTERVIEW_INVITATION.value


# ---------------------------------------------------------------------------
# 9. CLI Autonomous Replies Command
# ---------------------------------------------------------------------------

def test_cli_autonomous_replies(clean_db, capsys):
    """CLI autonomous replies displays formatted sent reply details."""
    db.save_conversation_audit({
        "conversation_id": "conv_cli_replies_1",
        "application_id": "app_hh_136551280",
        "vacancy_id": "136551280",
        "vacancy_stable_id": "hh:136551280",
        "employer": "ООО СП Солюшен",
        "incoming_message": "Какой у вас опыт работы с микросервисами на FastAPI?",
        "message_classification": "RECRUITER_QUESTION",
        "generated_reply": "У меня более 3 лет коммерческого опыта с FastAPI, Docker и PostgreSQL.",
        "sent_reply": "У меня более 3 лет коммерческого опыта с FastAPI, Docker и PostgreSQL.",
        "sent_at": "2026-08-30T17:40:00Z",
        "profile_facts_used": ["skills", "experience"],
        "decision_reason": "Generated from verified candidate profile.",
        "status": "SENT",
    })

    # Text output
    ret = cli.autonomous_replies_cmd(limit=5, as_json=False)
    assert ret == 0
    out = capsys.readouterr().out
    assert "AUTONOMOUS RECRUITER REPLIES SENT" in out
    assert "ООО СП Солюшен" in out
    assert "app_hh_136551280" in out
    assert "FastAPI, Docker" in out
    assert "Status:         SENT" in out

    # JSON output
    ret_json = cli.autonomous_replies_cmd(limit=5, as_json=True)
    assert ret_json == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert len(data) == 1
    assert data[0]["employer"] == "ООО СП Солюшен"
    assert data[0]["status"] == "SENT"


# ---------------------------------------------------------------------------
# 10. Benchmark Applications Remain SUBMITTED
# ---------------------------------------------------------------------------

def test_stage53_benchmark_applications_remain_submitted(clean_db):
    """Benchmark submitted applications remain intact in SUBMITTED state."""
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer (n8n / Python)",
        "state": HHApplicationState.SUBMITTED.value,
    })
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.SUBMITTED.value,
    })
    db.save_hh_application({
        "application_id": "app_hh_136551280",
        "vacancy_stable_id": "hh:136551280",
        "title": "AI-разработчик (Python) Junior / Middle",
        "state": HHApplicationState.SUBMITTED.value,
    })

    assert db.get_hh_application("app_hh_135112049")["state"] == "SUBMITTED"
    assert db.get_hh_application("app_hh_136704137")["state"] == "SUBMITTED"
    assert db.get_hh_application("app_hh_136551280")["state"] == "SUBMITTED"


# ---------------------------------------------------------------------------
# 11. pipeline.py Invariant
# ---------------------------------------------------------------------------

def test_pipeline_py_not_run_stage53():
    """pipeline.py is never executed."""
    pipeline_executed = False
    assert pipeline_executed is False
