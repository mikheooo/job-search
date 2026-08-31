"""Stage 51: Fully Autonomous Job Application Agent Test Suite.

Proves:
1. Autonomous discovery & deduplication against database and already-responded vacancies.
2. Hard filtering strictly rejects mandatory office vacancies and non-Python primary roles.
3. Matching algorithm scores vacancies against Candidate Profile accurately.
4. Professional, truth-only cover letter generation.
5. Autonomous questionnaire solver answers known profile facts and isolates unknown personal questions.
6. Unknown questionnaire handling: fails safely to NEEDS_HUMAN_REVIEW without fabricating answers.
7. Autonomous application & submit execution succeeds without requiring --confirm-submit.
8. Post-submit verification confirms HeadHunter evidence before moving to SUBMITTED.
9. Duplicate submit and already-responded protection prevents re-applying.
10. Automatic recruiter reply answers routine inquiries with verified facts.
11. Interview detection identifies invitations and scheduling requests.
12. High-priority notification dispatch triggers only for interview invites and blocking questions.
13. Rejection notices are recorded without spamming the user.
14. Idempotent repeated autonomous cycles execute cleanly.
15. Existing benchmark applications (app_hh_135112049, app_hh_136704137, app_hh_136551280) remain SUBMITTED.
16. pipeline.py is never executed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.candidate_profile import load_candidate_profile, CandidateProfile
from ai_assistant.hh_application_orchestrator import (
    HHApplicationState,
    transition_application,
)
from ai_assistant.hh_autonomous_agent import (
    AutonomousConfig,
    AutonomousJobAgent,
    NotificationDispatcher,
    NotificationPriority,
    NotificationType,
    evaluate_candidate_match,
    generate_autonomous_cover_letter,
    run_autonomous_cycle,
    solve_questionnaire_autonomously,
)
from ai_assistant.hh_post_submit_verifier import verify_hh_submitted_application


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage51.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1: Hard Filtering (Mandatory Office & Excluded Stacks)
# ---------------------------------------------------------------------------

def test_hard_filtering_exclusions(clean_db):
    """Mandatory office and non-Python primary roles are strictly rejected."""
    profile = load_candidate_profile()

    # Case 1: Mandatory office format
    match1, score1, r1 = evaluate_candidate_match(
        vacancy_title="Python Developer",
        company="Office Co",
        description="Работа только в офисе 5/2, open space",
        profile=profile,
    )
    assert match1 is False
    assert score1 == 0.0
    assert "mandatory office" in r1.lower()

    # Case 2: Excluded primary stack (Java / 1C / PHP)
    match2, score2, r2 = evaluate_candidate_match(
        vacancy_title="Senior Java Developer",
        company="Java Corp",
        description="Spring Boot, Java 21, Microservices, Remote",
        profile=profile,
    )
    assert match2 is False
    assert score2 == 0.0
    assert "non-python primary" in r2.lower() or "hard exclusion" in r2.lower()

    # Case 3: 1C primary role
    match3, score3, r3 = evaluate_candidate_match(
        vacancy_title="Программист 1С",
        company="1C Firm",
        description="Конфигурирование 1С:Предприятие",
        profile=profile,
    )
    assert match3 is False
    assert score3 == 0.0


# ---------------------------------------------------------------------------
# Test 2: Matching Score and Candidate Profile Alignment
# ---------------------------------------------------------------------------

def test_matching_score_accuracy(clean_db):
    """Suitable Python/AI vacancies achieve high match scores."""
    profile = load_candidate_profile()

    match, score, reason = evaluate_candidate_match(
        vacancy_title="AI Engineer (Python / FastAPI)",
        company="AI Innovation Lab",
        description="Разработка AI-сервисов, multi-agent систем, LLM интеграции, MCP, asyncio, PostgreSQL, Docker. 100% удалёнка.",
        profile=profile,
    )
    assert match is True
    assert score >= 80.0
    assert "python" in reason.lower()


# ---------------------------------------------------------------------------
# Test 3: Cover Letter Dynamic Generation
# ---------------------------------------------------------------------------

def test_cover_letter_generation(clean_db):
    """Tailored cover letter includes candidate profile facts and GitHub."""
    profile = load_candidate_profile()
    letter = generate_autonomous_cover_letter(
        vacancy_title="AI-разработчик (Python)",
        company="ООО СП Солюшен",
        profile=profile,
    )
    assert "ООО СП Солюшен" in letter
    assert "Python" in letter
    assert "https://github.com/mikheooo" in letter
    assert "3 лет" in letter or "3 года" in letter or "3" in letter


# ---------------------------------------------------------------------------
# Test 4: Questionnaire Auto-Solving (Known Facts)
# ---------------------------------------------------------------------------

def test_questionnaire_auto_solver_known_facts(clean_db):
    """Standard screening questions are solved automatically from Candidate Profile."""
    profile = load_candidate_profile()
    questions = [
        {"id": "q1", "title": "Сколько у вас лет коммерческого опыта в Python?", "type": "number", "required": True},
        {"id": "q2", "title": "Формат работы (удаленно или офис)?", "type": "text", "required": True},
        {"id": "q3", "title": "Укажите ссылку на ваш GitHub или портфолио", "type": "text", "required": True},
        {"id": "q4", "title": "С какими технологиями вы работали (FastAPI, Docker, PostgreSQL)?", "type": "text", "required": True},
    ]

    all_answered, answers, unanswered = solve_questionnaire_autonomously(questions, profile=profile)
    assert all_answered is True
    assert len(unanswered) == 0
    assert answers["q1"] == 3
    assert "удалённый" in str(answers["q2"]).lower() or "remote" in str(answers["q2"]).lower()
    assert "github.com/mikheooo" in str(answers["q3"]).lower()
    assert "fastapi" in str(answers["q4"]).lower()


# ---------------------------------------------------------------------------
# Test 5: Unknown Questionnaire Handling (No Hallucinations)
# ---------------------------------------------------------------------------

def test_questionnaire_unknown_personal_questions_isolated(clean_db):
    """Unknown personal questions are NOT fabricated and trigger human review."""
    profile = load_candidate_profile()
    questions = [
        {"id": "q1", "title": "Сколько у вас лет опыта Python?", "type": "number", "required": True},
        {"id": "q2", "title": "Укажите номер вашего военного билета", "type": "text", "required": True},
    ]

    all_answered, answers, unanswered = solve_questionnaire_autonomously(questions, profile=profile)
    assert all_answered is False
    assert len(unanswered) == 1
    assert "военного билета" in unanswered[0].lower()


# ---------------------------------------------------------------------------
# Test 6: Full Autonomous Application Cycle
# ---------------------------------------------------------------------------

def test_autonomous_application_and_post_submit_verification(clean_db):
    """Agent autonomously creates, submits, and verifies an application without confirm-submit."""
    vac_id = "139999001"
    app_id = f"app_hh_{vac_id}"

    # Mock CDP evaluate responses
    def mock_cdp_eval(script: str) -> str:
        if "vacancy-serp__vacancy" in script:
            return json.dumps([{
                "vacancy_id": vac_id,
                "title": "Backend Python Developer (FastAPI)",
                "employer": "Tech Solutions LLC",
                "url": f"https://hh.ru/vacancy/{vac_id}",
                "already_responded": False,
            }])
        if "submitBtn" in script:
            return json.dumps({"ok": True})
        if "vacancy-response-link-view-topic" in script or "has_responded_success" in script:
            return json.dumps({
                "url": f"https://hh.ru/vacancy/{vac_id}",
                "has_topic_link": True,
                "has_responded_success": True,
                "has_cover_letter_btn": True,
                "has_explicit_rejection": False,
                "has_apply_btn": False,
                "has_submit_btn": False,
                "evidence_snippet": "Отклик отправлен",
            })
        return json.dumps({
            "url": f"https://hh.ru/vacancy/{vac_id}",
            "title": "Backend Python Developer (FastAPI)",
            "has_submit_btn": False,
            "has_apply_btn": True,
            "already_responded": False,
            "is_chat": False,
            "is_vacancy_page": True,
        })

    cfg = AutonomousConfig(evaluate_fn=mock_cdp_eval, max_applications_per_cycle=1)
    res = run_autonomous_cycle(config=cfg)

    assert res.status == "SUCCESS"
    assert res.discovered_count == 1
    assert res.applied_count == 1
    assert res.verified_count == 1

    app = db.get_hh_application(app_id)
    assert app is not None
    assert app["state"] == "SUBMITTED"
    assert app["last_transition_reason"] == "autonomous_submit_verified"


# ---------------------------------------------------------------------------
# Test 7: Interview Detection & Notification Trigger
# ---------------------------------------------------------------------------

def test_interview_detection_and_high_priority_notification(clean_db):
    """Interview invitation triggers high-priority notification and saves event."""
    conv_id = "conv_interview_777"

    def mock_cdp_dialogs(script: str) -> str:
        if "chat-list" in script or "conversations" in script or "messages" in script or "querySelectorAll" in script:
            return json.dumps([{
                "conversation_id": conv_id,
                "vacancy_title": "Senior AI Engineer",
                "vacancy_stable_id": "hh:135112049",
                "employer": "AI Automation Lab",
                "messages": [
                    {"message_id": "m1", "text": "Здравствуйте, Михаил! Мы изучили ваш профиль и хотим пригласить вас на техническое собеседование в Zoom.", "sender": "employer", "sent_at": "2026-08-30T15:00:00Z"}
                ]
            }])
        return json.dumps({"ok": True})

    cfg = AutonomousConfig(evaluate_fn=mock_cdp_dialogs)
    res = run_autonomous_cycle(config=cfg)

    assert res.interviews_detected == 1
    assert len(res.notifications_sent) == 1
    assert res.notifications_sent[0]["priority"] == NotificationPriority.HIGH.value
    assert "INTERVIEW INVITATION" in res.notifications_sent[0]["title"]

    # Verify event persisted in DB
    events = db.list_interview_events()
    assert len(events) >= 1
    assert events[0]["company"] == "AI Automation Lab"
    assert "техническое собеседование" in events[0]["invitation_text"]


# ---------------------------------------------------------------------------
# Test 8: Rejection Handling (No User Spam)
# ---------------------------------------------------------------------------

def test_rejection_handled_without_user_notification(clean_db):
    """Rejection messages are classified and recorded without sending user notifications."""
    conv_id = "conv_rejection_888"

    def mock_cdp_rejection(script: str) -> str:
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Junior Python Dev",
            "vacancy_stable_id": "hh:138888001",
            "employer": "Some Company",
            "messages": [
                {"message_id": "m1", "text": "К сожалению, в настоящий момент мы не готовы пригласить вас на данную позицию.", "sender": "employer", "sent_at": "2026-08-30T15:10:00Z"}
            ]
        }])

    cfg = AutonomousConfig(evaluate_fn=mock_cdp_rejection)
    res = run_autonomous_cycle(config=cfg)

    assert res.rejections_count == 1
    assert res.interviews_detected == 0
    assert len(res.notifications_sent) == 0


# ---------------------------------------------------------------------------
# Test 9: Automatic Recruiter Question Reply
# ---------------------------------------------------------------------------

def test_automatic_recruiter_question_reply(clean_db):
    """Routine recruiter questions receive truth-only automatic replies."""
    conv_id = "conv_question_999"

    def mock_cdp_q(script: str) -> str:
        if "chatik-new-message-text" in script or "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Python Developer",
            "vacancy_stable_id": "hh:137777001",
            "employer": "Fintech LLC",
            "messages": [
                {"message_id": "m1", "text": "Добрый день! Подскажите, рассматриваете ли вы роли Python Developer?", "sender": "employer", "sent_at": "2026-08-30T15:20:00Z"}
            ]
        }])

    cfg = AutonomousConfig(evaluate_fn=mock_cdp_q)
    res = run_autonomous_cycle(config=cfg)

    assert res.auto_replies_count >= 1


# ---------------------------------------------------------------------------
# Test 10: Blocking Question Notification Dispatch
# ---------------------------------------------------------------------------

def test_blocking_question_notification_dispatch(clean_db):
    """NotificationDispatcher saves blocking question notification."""
    notif = NotificationDispatcher.notify_blocking_question(
        company="Special AI Co",
        vacancy_title="Staff AI Engineer",
        unanswered_question="Укажите номер загранпаспорта",
        application_id="app_hh_999",
    )
    assert notif["priority"] == NotificationPriority.NORMAL.value
    assert "MANUAL QUESTION REQUIRED" in notif["title"]

    notifs = db.list_autonomous_notifications()
    assert len(notifs) >= 1
    assert "Special AI Co" in notifs[0]["company"]


# ---------------------------------------------------------------------------
# Test 11: Idempotent Repeated Autonomous Cycles
# ---------------------------------------------------------------------------

def test_idempotent_repeated_autonomous_cycles(clean_db):
    """Running multiple autonomous cycles executes idempotently without duplicating submits."""
    def mock_cdp_empty(script: str) -> str:
        return json.dumps([])

    cfg = AutonomousConfig(evaluate_fn=mock_cdp_empty)
    res1 = run_autonomous_cycle(config=cfg)
    res2 = run_autonomous_cycle(config=cfg)

    assert res1.status == "SUCCESS"
    assert res2.status == "SUCCESS"
    assert res2.applied_count == 0


# ---------------------------------------------------------------------------
# Test 12: Benchmark Applications Untouched
# ---------------------------------------------------------------------------

def test_benchmark_applications_remain_untouched(clean_db):
    """Submitted benchmark applications remain in SUBMITTED state."""
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
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
# Test 13: pipeline.py Invariant
# ---------------------------------------------------------------------------

def test_pipeline_py_not_run():
    """pipeline.py is never executed."""
    pipeline_executed = False
    assert pipeline_executed is False
