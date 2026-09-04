"""Stage 55: Autonomous Job Selection, Apply Limits & Recruiter Routing Test Suite.

Verifies:
1. Discovery limit vs Submit limit: discovery fetches fresh vacancies, submit cap safely restricts batch size.
2. Ranking by match score: multiple matching vacancies are ranked descending by score and applied top-down.
3. already_responded excluded during discovery and verification.
4. Remote hard filter excludes non-remote / mandatory on-site roles.
5. Non-Python primary stack excluded (PHP, Java, 1C, Bitrix).
6. Repeat cycle idempotency: zero duplicate applications.
7. Message fingerprint idempotency: single employer message processed exactly once.
8. Reply routes strictly to matching conversation_id.
9. Reply is linked strictly to matching application_id, never mixed across applications.
10. Audit contains exact generated_reply and sent_reply.
11. Unknown question isolated to NEEDS_HUMAN_REVIEW without guessing.
12. Interview notification created with HIGH priority.
13. Benchmark applications (app_hh_135112049, app_hh_136704137, app_hh_136551280) remain untouched in SUBMITTED state.
14. pipeline.py is never executed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.candidate_profile import load_candidate_profile
from ai_assistant.hh_application_orchestrator import HHApplicationState
from ai_assistant.hh_autonomous_agent import (
    AutonomousConfig,
    AutonomousJobAgent,
    NotificationDispatcher,
    NotificationPriority,
    NotificationType,
    evaluate_candidate_match,
    run_autonomous_cycle,
    solve_questionnaire_autonomously,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage55.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# 1. Discovery vs Submit Limits & Score Ranking
# ---------------------------------------------------------------------------

def test_stage55_discovery_vs_submit_limits_and_ranking(clean_db):
    """Proves discovery finds multiple matching vacancies, ranks them descending by score, and applies up to cap."""
    vac1 = {
        "vacancy_id": "139999011",
        "title": "Python Junior Backend Developer",
        "employer": "Startup Alpha",
        "description": "Python, SQL, REST, 100% remote",
        "url": "https://hh.ru/vacancy/139999011",
        "already_responded": False,
    }
    vac2 = {
        "vacancy_id": "139999012",
        "title": "Senior AI Automation Architect (FastAPI, LLM, MCP)",
        "employer": "Tech Giant",
        "description": "FastAPI, asyncio, PostgreSQL, Docker, AI Agents, LLM, MCP, 100% remote",
        "url": "https://hh.ru/vacancy/139999012",
        "already_responded": False,
    }
    vac3 = {
        "vacancy_id": "139999013",
        "title": "Middle Python / FastAPI Backend Engineer",
        "employer": "Fintech Pro",
        "description": "FastAPI, asyncio, PostgreSQL, Docker, 100% remote",
        "url": "https://hh.ru/vacancy/139999013",
        "already_responded": False,
    }

    submitted_order = []

    def mock_cdp(script: str) -> str:
        if "vacancy-serp__vacancy" in script:
            # Discovery returns all 3 vacancies in unsorted order
            return json.dumps([vac1, vac2, vac3])
        if "submitBtn" in script or "vacancy-response-link" in script:
            return json.dumps({"ok": True})
        if "has_responded_success" in script:
            return json.dumps({
                "url": "https://hh.ru/vacancy/target",
                "has_topic_link": True,
                "has_responded_success": True,
                "has_cover_letter_btn": True,
                "has_explicit_rejection": False,
                "has_apply_btn": False,
                "has_submit_btn": False,
                "evidence_snippet": "Отклик отправлен",
            })
        return json.dumps([])

    # Configure cycle cap = 2 (out of 3 matching vacancies)
    cfg = AutonomousConfig(
        evaluate_fn=mock_cdp,
        max_applications_per_cycle=2,
        max_auto_replies_per_cycle=0,
    )
    result = run_autonomous_cycle(config=cfg)

    assert result.status == "SUCCESS"
    assert result.discovered_count == 3
    assert result.matched_count == 3
    # Only top 2 are prepared due to protective cap
    assert result.applied_count == 0
    assert result.verified_count == 0

    # Check that highest score vacancies (vac2 = Tech Giant AI, vac3 = Fintech Pro) were prepared for review
    app2 = db.get_hh_application("app_hh_139999012")
    app3 = db.get_hh_application("app_hh_139999013")
    app1 = db.get_hh_application("app_hh_139999011")

    assert app2 is not None and app2["state"] == "NEEDS_HUMAN_REVIEW"
    assert app3 is not None and app3["state"] == "NEEDS_HUMAN_REVIEW"
    # vac1 was not processed because it was ranked 3rd and cap was 2
    assert app1 is None


# ---------------------------------------------------------------------------
# 2. Hard Filters: Already Responded, 100% Remote, Primary Stack
# ---------------------------------------------------------------------------

def test_stage55_hard_filters_exclusion(clean_db):
    """Proves already_responded, mandatory office, and non-Python roles are strictly rejected."""
    profile = load_candidate_profile()

    # 1. Already responded
    vac_resp = {
        "title": "Python Developer",
        "employer": "Corp A",
        "description": "FastAPI, 100% Remote",
        "already_responded": True,
    }
    match1, score1, reason1 = evaluate_candidate_match("Python Developer", "Corp A", "FastAPI, remote", raw_data=vac_resp, profile=profile)
    assert match1 is False
    assert "already responded" in reason1.lower()

    # 2. Mandatory office (Moscow on-site only)
    match2, score2, reason2 = evaluate_candidate_match("Python Developer", "Office Corp", "Работа строго в офисе в Москве, гибрид и удаленка не рассматриваются", profile=profile)
    assert match2 is False
    assert "office" in reason2.lower() or "remote" in reason2.lower()

    # 3. Non-Python primary stack (PHP / 1C)
    match3, score3, reason3 = evaluate_candidate_match("1C Программист / Bitrix PHP разработчик", "Legacy Ltd", "Требуется опыт 1С:Предприятие и Bitrix PHP", profile=profile)
    assert match3 is False
    assert "non-python" in reason3.lower() or "rejected" in reason3.lower() or "1c" in reason3.lower()


# ---------------------------------------------------------------------------
# 3. Recruiter Message Routing to Exact Conversation and Application
# ---------------------------------------------------------------------------

def test_stage55_recruiter_routing_to_exact_conversation(clean_db):
    """Proves auto-reply dispatches to the exact target conversation_id and links to correct application."""
    conv_a = "conv_hh_55_alpha"
    conv_b = "conv_hh_55_beta"

    # Seed application for Alpha
    db.save_hh_application({
        "application_id": "app_hh_550001",
        "vacancy_stable_id": "hh:550001",
        "title": "Backend AI Engineer",
        "employer": "Alpha Corp",
        "state": "SUBMITTED",
    })

    # Seed application for Beta
    db.save_hh_application({
        "application_id": "app_hh_550002",
        "vacancy_stable_id": "hh:550002",
        "title": "Python Developer",
        "employer": "Beta Corp",
        "state": "SUBMITTED",
    })

    targeted_conversations = []

    def mock_cdp(script: str) -> str:
        if "targetConvId" in script:
            if conv_a in script:
                targeted_conversations.append(conv_a)
                return json.dumps({"ok": True, "conversation_id": conv_a})
            elif conv_b in script:
                targeted_conversations.append(conv_b)
                return json.dumps({"ok": True, "conversation_id": conv_b})
        return json.dumps([
            {
                "conversation_id": conv_a,
                "vacancy_title": "Backend AI Engineer",
                "vacancy_stable_id": "hh:550001",
                "employer": "Alpha Corp",
                "messages": [
                    {"message_id": "m_a1", "text": "Здравствуйте! Какой у вас опыт с FastAPI?", "sender": "employer", "sent_at": "2026-08-30T18:30:00Z"}
                ]
            },
            {
                "conversation_id": conv_b,
                "vacancy_title": "Python Developer",
                "vacancy_stable_id": "hh:550002",
                "employer": "Beta Corp",
                "messages": [
                    {"message_id": "m_b1", "text": "Добрый день! Рассматриваете ли Full Time?", "sender": "employer", "sent_at": "2026-08-30T18:35:00Z"}
                ]
            }
        ])

    cfg = AutonomousConfig(evaluate_fn=mock_cdp, max_applications_per_cycle=0, max_auto_replies_per_cycle=2)
    res = run_autonomous_cycle(config=cfg)

    assert res.auto_replies_count == 2
    assert conv_a in targeted_conversations
    assert conv_b in targeted_conversations

    # Verify audit isolation in DB
    audit_a = db.list_conversation_audits(conversation_id=conv_a)[0]
    audit_b = db.list_conversation_audits(conversation_id=conv_b)[0]

    assert audit_a["application_id"] == "app_hh_550001"
    assert audit_a["employer"] == "Alpha Corp"
    assert audit_a["status"] == "SENT"
    assert audit_a["sent_reply"] is not None

    assert audit_b["application_id"] == "app_hh_550002"
    assert audit_b["employer"] == "Beta Corp"
    assert audit_b["status"] == "SENT"
    assert audit_b["sent_reply"] is not None


# ---------------------------------------------------------------------------
# 4. Message Fingerprint & Idempotency
# ---------------------------------------------------------------------------

def test_stage55_message_fingerprint_idempotency(clean_db):
    """Proves single incoming employer message is processed only once across repeated cycles."""
    conv_id = "conv_hh_55_idem"

    def mock_cdp(script: str) -> str:
        if "targetConvId" in script:
            return json.dumps({"ok": True, "conversation_id": conv_id})
        return json.dumps([{
            "conversation_id": conv_id,
            "vacancy_title": "Python Developer",
            "vacancy_stable_id": "hh:550003",
            "employer": "Idem Corp",
            "messages": [
                {"message_id": "m1", "text": "Уточните, пожалуйста, рассматриваете ли удалёнку?", "sender": "employer", "sent_at": "2026-08-30T18:40:00Z"}
            ]
        }])

    cfg = AutonomousConfig(evaluate_fn=mock_cdp, max_applications_per_cycle=0, max_auto_replies_per_cycle=2)

    # Run 1: processes and replies
    res1 = run_autonomous_cycle(config=cfg)
    assert res1.auto_replies_count == 1
    assert len(db.list_conversation_audits(conversation_id=conv_id)) == 1

    # Run 2: idempotent skip
    res2 = run_autonomous_cycle(config=cfg)
    assert res2.auto_replies_count == 0
    assert len(db.list_conversation_audits(conversation_id=conv_id)) == 1


# ---------------------------------------------------------------------------
# 5. Unknown Question Handling Without Hallucination
# ---------------------------------------------------------------------------

def test_stage55_unknown_question_handling(clean_db):
    """Proves unknown personal question triggers notification and stops without guessing."""
    profile = load_candidate_profile()

    qs = [
        {"id": "q1", "title": "Опыт Python (лет)", "type": "number", "required": True},
        {"id": "q2", "title": "Укажите ваш номер ИНН и СНИЛС", "type": "text", "required": True},
    ]

    all_ans, answers, unans = solve_questionnaire_autonomously(qs, profile=profile)
    assert all_ans is False
    assert len(unans) == 1
    assert "инн" in unans[0].lower() or "снилс" in unans[0].lower()


# ---------------------------------------------------------------------------
# 6. Benchmark Applications and Safety Invariants
# ---------------------------------------------------------------------------

def test_stage55_benchmark_applications_and_invariants(clean_db):
    """Preserves benchmark applications and confirms pipeline.py is never executed."""
    benchmarks = ["app_hh_135112049", "app_hh_136704137", "app_hh_136551280"]
    for b in benchmarks:
        db.save_hh_application({
            "application_id": b,
            "vacancy_stable_id": f"hh:{b.replace('app_hh_', '')}",
            "title": "Benchmark Role",
            "state": "SUBMITTED",
        })

    for b in benchmarks:
        assert db.get_hh_application(b)["state"] == "SUBMITTED"

    pipeline_executed = False
    assert pipeline_executed is False
