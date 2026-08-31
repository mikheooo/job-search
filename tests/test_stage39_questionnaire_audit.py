"""Stage 39: Final Questionnaire Pre-Submit Audit Test Suite.

Proves:
1. Audit passes when all answers are authoritatively confirmed by CandidateProfile.
2. Experience mismatch (e.g. 10 years instead of 3) is caught and flagged as REVIEW.
3. Unverified skills in selection are flagged as REVIEW and block submit.
4. Foreign/unverified portfolio links are flagged as REVIEW.
5. Missing required answers fail the audit (NEEDS_CORRECTION).
6. CLI audit commands work for both questionnaire and application IDs.
7. Zero submission safety invariants strictly hold.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    HHQuestionStatus,
    extract_hh_questionnaire_from_snapshot,
)
from ai_assistant.hh_questionnaire_audit import (
    audit_questionnaire,
    AuditVerdict,
    OverallAuditStatus,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage39_audit.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


@pytest.fixture
def mock_profile():
    return CandidateProfile(
        name="Mikhail Kolesnikov",
        skills=["python", "n8n", "automation", "ai agents", "llm", "api"],
        years_experience=3,
        remote_required=True,
        allowed_locations=["Remote", "Worldwide"],
        employment_types=["Full Time", "Contract"],
        github="https://github.com/mikheooo",
        portfolio="https://mikheooo.github.io/portfolio/",
    )


def _create_sample_quest(quest_id: str = "quest_test_audit") -> HHQuestionnaire:
    snapshot = {
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer (n8n / Python)",
        "employer": "AI Automation Lab",
        "questions": [
            {
                "id": "q1_location",
                "text": "Где располагается ваше фактическое место работы?",
                "type": "radio",
                "required": True,
                "options": ["Россия (Москва)", "Удалённо (Вне РФ)", "Офис"],
            },
            {
                "id": "q2_exp",
                "text": "Сколько лет коммерческого опыта разработки на Python?",
                "type": "number",
                "required": True,
            },
            {
                "id": "q3_mode",
                "text": "Предпочитаемый график и формат занятости:",
                "type": "radio",
                "required": True,
                "options": ["Полный день (Full-time)", "Частичная занятость"],
            },
            {
                "id": "q4_stack",
                "text": "Какие из технологий вы активно используете?",
                "type": "checkbox",
                "required": False,
                "options": ["FastAPI", "Asyncio", "n8n", "PostgreSQL", "Docker", "LLM APIs", "COBOL"],
            },
            {
                "id": "q5_github",
                "text": "Ссылка на GitHub:",
                "type": "text",
                "required": False,
            }
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    return quest


# ---------------------------------------------------------------------------
# Test 1: All Answers Confirmed by Profile -> SAFE_TO_SUBMIT
# ---------------------------------------------------------------------------

def test_audit_all_answers_confirmed_pass(clean_db, mock_profile):
    """When all questionnaire answers strictly match CandidateProfile facts, audit passes (SAFE_TO_SUBMIT)."""
    quest = _create_sample_quest()
    answers = {
        "q1_location": "Удалённо (Вне РФ)",
        "q2_exp": "3",
        "q3_mode": "Полный день (Full-time)",
        "q4_stack": ["FastAPI", "n8n", "PostgreSQL", "LLM APIs"],
        "q5_github": "https://github.com/mikheooo",
    }
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    report = audit_questionnaire(quest.questionnaire_id, profile=mock_profile)
    assert report.overall == OverallAuditStatus.SAFE_TO_SUBMIT
    assert len(report.items) == 5
    for it in report.items:
        assert it.verdict == AuditVerdict.PASS
        assert it.is_confirmed_by_profile is True


# ---------------------------------------------------------------------------
# Test 2: Experience Mismatch -> Flagged as REVIEW (NEEDS_CORRECTION)
# ---------------------------------------------------------------------------

def test_audit_unconfirmed_experience_yields_review(clean_db, mock_profile):
    """If an answer claims 10 years of experience while profile says 3, audit flags Q2 as REVIEW."""
    quest = _create_sample_quest()
    answers = {
        "q1_location": "Удалённо (Вне РФ)",
        "q2_exp": "10",  # Mismatch!
        "q3_mode": "Полный день (Full-time)",
        "q4_stack": ["n8n"],
        "q5_github": "https://github.com/mikheooo",
    }
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    report = audit_questionnaire(quest.questionnaire_id, profile=mock_profile)
    assert report.overall == OverallAuditStatus.NEEDS_CORRECTION
    
    q2_item = next(it for it in report.items if it.question_id == "q2_exp")
    assert q2_item.verdict == AuditVerdict.REVIEW
    assert q2_item.is_confirmed_by_profile is False
    assert "does not match profile" in q2_item.rationale


# ---------------------------------------------------------------------------
# Test 3: Unverified Technologies -> Flagged as REVIEW (NEEDS_CORRECTION)
# ---------------------------------------------------------------------------

def test_audit_unverified_skills_yields_review(clean_db, mock_profile):
    """If an answer selects unverified technologies (e.g. COBOL), audit flags Q4 as REVIEW."""
    quest = _create_sample_quest()
    answers = {
        "q1_location": "Удалённо (Вне РФ)",
        "q2_exp": "3",
        "q3_mode": "Полный день (Full-time)",
        "q4_stack": ["n8n", "COBOL"],  # COBOL is unverified!
        "q5_github": "https://github.com/mikheooo",
    }
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    report = audit_questionnaire(quest.questionnaire_id, profile=mock_profile)
    assert report.overall == OverallAuditStatus.NEEDS_CORRECTION
    
    q4_item = next(it for it in report.items if it.question_id == "q4_stack")
    assert q4_item.verdict == AuditVerdict.REVIEW
    assert q4_item.is_confirmed_by_profile is False
    assert "Unverified" in q4_item.rationale


# ---------------------------------------------------------------------------
# Test 4: Unverified GitHub / Portfolio Link -> Flagged as REVIEW
# ---------------------------------------------------------------------------

def test_audit_unverified_portfolio_link_yields_review(clean_db, mock_profile):
    """If an answer includes a foreign/hallucinated github link, audit flags Q5 as REVIEW."""
    quest = _create_sample_quest()
    answers = {
        "q1_location": "Удалённо (Вне РФ)",
        "q2_exp": "3",
        "q3_mode": "Полный день (Full-time)",
        "q4_stack": ["n8n"],
        "q5_github": "https://github.com/random_stranger_user",  # Hallucination!
    }
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    report = audit_questionnaire(quest.questionnaire_id, profile=mock_profile)
    assert report.overall == OverallAuditStatus.NEEDS_CORRECTION
    
    q5_item = next(it for it in report.items if it.question_id == "q5_github")
    assert q5_item.verdict == AuditVerdict.REVIEW
    assert q5_item.is_confirmed_by_profile is False


# ---------------------------------------------------------------------------
# Test 5: Missing Required Answer -> NEEDS_CORRECTION
# ---------------------------------------------------------------------------

def test_audit_missing_required_answer_yields_review(clean_db, mock_profile):
    """If a required question has no answer recorded, audit fails closed."""
    quest = _create_sample_quest()
    answers = {
        "q1_location": "Удалённо (Вне РФ)",
        # q2_exp missing!
        "q3_mode": "Полный день (Full-time)",
    }
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.NEEDS_HUMAN_REVIEW.value)

    report = audit_questionnaire(quest.questionnaire_id, profile=mock_profile)
    assert report.overall == OverallAuditStatus.NEEDS_CORRECTION
    
    q2_item = next(it for it in report.items if it.question_id == "q2_exp")
    assert q2_item.verdict == AuditVerdict.REVIEW
    assert "No answer" in q2_item.rationale


# ---------------------------------------------------------------------------
# Test 6: CLI Audit Commands & Formatting
# ---------------------------------------------------------------------------

def test_audit_cli_commands(clean_db, mock_profile, capsys):
    """CLI commands 'questionnaire audit' and 'application audit' output complete audit reports."""
    quest = _create_sample_quest()
    answers = {
        "q1_location": "Удалённо (Вне РФ)",
        "q2_exp": "3",
        "q3_mode": "Полный день (Full-time)",
        "q4_stack": ["FastAPI", "n8n"],
        "q5_github": "https://github.com/mikheooo",
    }
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "questionnaire_id": quest.questionnaire_id,
        "title": quest.title,
        "employer": quest.employer,
        "state": "READY_TO_SUBMIT",
        "submit_allowed": True,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    # Test questionnaire audit CLI
    ret_q = cli.questionnaire_audit_cmd(quest.questionnaire_id)
    assert ret_q == 0
    out_q = capsys.readouterr().out
    assert "QUESTIONNAIRE AUDIT" in out_q
    assert "SAFE_TO_SUBMIT" in out_q
    assert "REAL HH SUBMIT: NO" in out_q
    assert "PIPELINE.PY:    NOT RUN" in out_q

    # Test application audit CLI
    ret_app = cli.application_audit_cmd(app_id)
    assert ret_app == 0
    out_app = capsys.readouterr().out
    assert "QUESTIONNAIRE AUDIT" in out_app
    assert f"Application:   {app_id}" in out_app
    assert "SAFE_TO_SUBMIT" in out_app
