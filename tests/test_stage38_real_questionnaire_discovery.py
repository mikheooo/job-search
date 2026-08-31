"""Stage 38: Real HH Application Form / Questionnaire Discovery & Extraction Test Suite.

Proves:
1. Required and optional question extraction.
2. Form field types: text, textarea, number, select, radio, checkbox.
3. Options extraction for choice questions.
4. Stable, deterministic SHA-256 fingerprinting.
5. Invalidation when DOM fingerprint changes (fails closed to NEEDS_HUMAN_REVIEW).
6. Handling of unsupported question types (halts at NEEDS_HUMAN_REVIEW).
7. Empty questionnaire case (QUESTIONNAIRE = NONE).
8. Idempotent extraction (re-reads produce identical fingerprint and records).
9. Strict Zero Submit safety invariant (Submit = 0, no autonomous send).
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    HHQuestionType,
    HHQuestionStatus,
    compute_questionnaire_fingerprint,
    extract_hh_questionnaire_from_snapshot,
    validate_human_answers,
    format_hh_application_form_cli_output,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage38_questionnaire.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1: Required and Optional Question Detection & Field Types
# ---------------------------------------------------------------------------

def test_required_and_optional_question_extraction(clean_db):
    """Extracts required and optional questions across text, textarea, number, select, radio, checkbox."""
    snapshot = {
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Python Developer",
        "employer": "TechInnovations",
        "questions": [
            {
                "id": "q1_text",
                "text": "Сколько лет коммерческого опыта разработки на Python?",
                "type": "number",
                "required": True,
                "options": [],
            },
            {
                "id": "q2_textarea",
                "text": "Опишите ваши ключевые проекты и архитектурные решения:",
                "type": "textarea",
                "required": True,
                "options": [],
            },
            {
                "id": "q3_radio",
                "text": "Предпочитаемый формат занятости:",
                "type": "radio",
                "required": True,
                "options": ["Полная занятость", "Частичная занятость", "Проектная работа"],
            },
            {
                "id": "q4_select",
                "text": "Уровень владения английским языком:",
                "type": "select",
                "required": True,
                "options": ["A1/A2", "B1", "B2", "C1/C2"],
            },
            {
                "id": "q5_checkbox",
                "text": "Технологический стек (выберите всё, с чем работали):",
                "type": "checkbox",
                "required": False,  # Optional question
                "options": ["FastAPI", "Django", "PostgreSQL", "Docker", "Kubernetes", "Redis"],
            },
            {
                "id": "q6_text",
                "text": "Ссылка на профиль GitHub / портфолио:",
                "type": "text",
                "required": False,  # Optional question
                "options": [],
            }
        ]
    }

    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None
    assert len(quest.questions) == 6
    assert quest.status == HHQuestionStatus.NEEDS_HUMAN_REVIEW.value

    # Check question 1 (number, required)
    q1 = quest.questions[0]
    assert q1.question_id == "q1_text"
    assert q1.question_type == "number"
    assert q1.required is True

    # Check question 2 (textarea, required)
    q2 = quest.questions[1]
    assert q2.question_id == "q2_textarea"
    assert q2.question_type == "textarea"
    assert q2.required is True

    # Check question 3 (radio, required with options)
    q3 = quest.questions[2]
    assert q3.question_id == "q3_radio"
    assert q3.question_type == "radio"
    assert q3.required is True
    assert len(q3.options) == 3
    assert "Полная занятость" in q3.options

    # Check question 4 (select, required with options)
    q4 = quest.questions[3]
    assert q4.question_id == "q4_select"
    assert q4.question_type == "select"
    assert len(q4.options) == 4

    # Check question 5 (checkbox, optional)
    q5 = quest.questions[4]
    assert q5.question_id == "q5_checkbox"
    assert q5.question_type == "checkbox"
    assert q5.required is False
    assert len(q5.options) == 6

    # Check question 6 (text, optional)
    q6 = quest.questions[5]
    assert q6.question_id == "q6_text"
    assert q6.question_type == "text"
    assert q6.required is False


# ---------------------------------------------------------------------------
# Test 2: Stable, Deterministic Fingerprinting
# ---------------------------------------------------------------------------

def test_stable_fingerprint_computation(clean_db):
    """Re-reading the exact same questionnaire structure produces an identical SHA-256 fingerprint."""
    questions = [
        HHQuestionItem(question_id="q1", text="Опыт работы с Python", question_type="number", required=True, options=[]),
        HHQuestionItem(question_id="q2", text="Формат работы", question_type="radio", required=True, options=["Удаленно", "Офис"]),
    ]

    fp1 = compute_questionnaire_fingerprint(questions)
    fp2 = compute_questionnaire_fingerprint(questions)
    
    # Reordered questions list should still produce identical deterministic fingerprint
    reordered_questions = [questions[1], questions[0]]
    fp3 = compute_questionnaire_fingerprint(reordered_questions)

    assert fp1 == fp2
    assert fp1 == fp3
    assert fp1.startswith("hh_qfp_")


# ---------------------------------------------------------------------------
# Test 3: Changed Fingerprint Invalidation (Fail-Closed)
# ---------------------------------------------------------------------------

def test_changed_fingerprint_detection(clean_db):
    """If the employer modifies questions or options on the live page, validation fails closed to NEEDS_HUMAN_REVIEW."""
    snapshot = {
        "vacancy_stable_id": "hh:200100",
        "questions": [
            {"id": "q1", "text": "Опыт Python", "type": "number", "required": True},
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None

    answers = {"q1": "5"}
    
    # Matching fingerprint -> OK
    val_ok = validate_human_answers(quest, answers, current_dom_fingerprint=quest.fingerprint)
    assert val_ok.ok is True
    assert val_ok.status == HHQuestionStatus.READY_TO_SUBMIT.value

    # Modified DOM fingerprint -> FAILS CLOSED
    altered_fp = "hh_qfp_altered_questions_structure_999"
    val_stale = validate_human_answers(quest, answers, current_dom_fingerprint=altered_fp)
    assert val_stale.ok is False
    assert val_stale.status == HHQuestionStatus.NEEDS_HUMAN_REVIEW.value
    assert "changed" in val_stale.reason.lower()


# ---------------------------------------------------------------------------
# Test 4: Unsupported Question Type Handling
# ---------------------------------------------------------------------------

def test_unsupported_question_type_handling(clean_db):
    """Unsupported question types are flagged and preserved, halting flow at NEEDS_HUMAN_REVIEW."""
    snapshot = {
        "vacancy_stable_id": "hh:300200",
        "questions": [
            {"id": "q1", "text": "Загрузите видео-визитку", "type": "video_recording_special", "required": True},
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None
    assert quest.questions[0].question_type == "video_recording_special"
    assert quest.status == HHQuestionStatus.NEEDS_HUMAN_REVIEW.value


# ---------------------------------------------------------------------------
# Test 5: Number Field Validation
# ---------------------------------------------------------------------------

def test_number_field_validation(clean_db):
    """Number questions enforce valid numeric strings (integer or float)."""
    snapshot = {
        "vacancy_stable_id": "hh:400300",
        "questions": [
            {"id": "q1", "text": "Опыт работы (полных лет):", "type": "number", "required": True},
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None

    # Valid numeric strings
    assert validate_human_answers(quest, {"q1": "5"}).ok is True
    assert validate_human_answers(quest, {"q1": "3.5"}).ok is True
    assert validate_human_answers(quest, {"q1": "4,5"}).ok is True

    # Invalid non-numeric string
    val_inv = validate_human_answers(quest, {"q1": "много лет"})
    assert val_inv.ok is False
    assert val_inv.status == HHQuestionStatus.BLOCKED.value
    assert any("not a valid number" in err for err in val_inv.invalid_options)


# ---------------------------------------------------------------------------
# Test 6: Choice Validation for Select, Radio, Checkbox
# ---------------------------------------------------------------------------

def test_choice_validation_select_radio_checkbox(clean_db):
    """Choice questions validate strictly against allowed options list."""
    snapshot = {
        "vacancy_stable_id": "hh:500400",
        "questions": [
            {"id": "q_radio", "text": "Формат", "type": "radio", "required": True, "options": ["Удаленно", "Офис"]},
            {"id": "q_check", "text": "Стек", "type": "checkbox", "required": False, "options": ["Python", "FastAPI", "Docker"]},
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None

    # Valid choices
    valid_answers = {
        "q_radio": "Удаленно",
        "q_check": ["Python", "FastAPI"],
    }
    assert validate_human_answers(quest, valid_answers).ok is True

    # Invalid radio option
    invalid_radio = {
        "q_radio": "Гибрид",
        "q_check": ["Python"],
    }
    val_bad_radio = validate_human_answers(quest, invalid_radio)
    assert val_bad_radio.ok is False
    assert val_bad_radio.status == HHQuestionStatus.BLOCKED.value

    # Invalid checkbox option
    invalid_check = {
        "q_radio": "Удаленно",
        "q_check": ["Python", "PHP_NOT_ALLOWED"],
    }
    val_bad_check = validate_human_answers(quest, invalid_check)
    assert val_bad_check.ok is False
    assert val_bad_check.status == HHQuestionStatus.BLOCKED.value


# ---------------------------------------------------------------------------
# Test 7: Empty Questionnaire (QUESTIONNAIRE = NONE)
# ---------------------------------------------------------------------------

def test_empty_questionnaire_case(clean_db):
    """When a vacancy has no screening questions, questionnaire is None and CLI outputs QUESTIONNAIRE: NONE."""
    snapshot = {
        "vacancy_stable_id": "hh:600500",
        "title": "Middle Python Developer",
        "questions": [],
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is None

    cli_out = format_hh_application_form_cli_output(vacancy_title="Middle Python Developer", quest=None)
    assert "Questionnaire: NONE" in cli_out
    assert "Submit: BLOCKED" in cli_out


# ---------------------------------------------------------------------------
# Test 8: CLI Formatted Output for Found Questionnaire
# ---------------------------------------------------------------------------

def test_cli_formatted_output_for_found_questionnaire(clean_db):
    """CLI output matches the required human review gate specification."""
    snapshot = {
        "vacancy_stable_id": "hh:700600",
        "title": "Lead AI Engineer",
        "employer": "DeepTech",
        "questions": [
            {
                "id": "q1",
                "text": "Готовы ли вы к командировкам?",
                "type": "radio",
                "required": True,
                "options": ["Да", "Нет", "По согласованию"],
            }
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None

    cli_out = format_hh_application_form_cli_output(vacancy_title="Lead AI Engineer", quest=quest)
    assert "HH APPLICATION FORM" in cli_out
    assert "Vacancy: Lead AI Engineer" in cli_out
    assert "Questionnaire: FOUND" in cli_out
    assert f"Questionnaire ID: {quest.questionnaire_id}" in cli_out
    assert f"Fingerprint:      {quest.fingerprint}" in cli_out
    assert "Q1 [required]" in cli_out
    assert "Готовы ли вы к командировкам?" in cli_out
    assert "Type: radio" in cli_out
    assert "Options:" in cli_out
    assert "- Да" in cli_out
    assert "Status: NEEDS_HUMAN_REVIEW" in cli_out
    assert "Submit: BLOCKED" in cli_out


# ---------------------------------------------------------------------------
# Test 9: Duplicate Extraction Idempotency
# ---------------------------------------------------------------------------

def test_duplicate_extraction_idempotency(clean_db):
    """Extracting questionnaire twice from same snapshot yields identical questionnaire_id and updates DB idempotently."""
    snapshot = {
        "vacancy_stable_id": "hh:800700",
        "title": "Python Architect",
        "questions": [
            {"id": "q1", "text": "Опыт архитектурного проектирования", "type": "textarea", "required": True},
        ]
    }
    q1 = extract_hh_questionnaire_from_snapshot(snapshot)
    q2 = extract_hh_questionnaire_from_snapshot(snapshot)

    assert q1.questionnaire_id == q2.questionnaire_id
    assert q1.fingerprint == q2.fingerprint

    all_quests = db.list_hh_questionnaires()
    assert len(all_quests) == 1


# ---------------------------------------------------------------------------
# Test 10: Smart Profile-Based Questionnaire Suggestions
# ---------------------------------------------------------------------------

def test_generate_suggested_answers_from_profile(clean_db):
    """Smart suggestion generator automatically crafts validated answers tailored from candidate profile."""
    from ai_assistant.hh_questionnaire import generate_suggested_answers

    snapshot = {
        "vacancy_stable_id": "hh:900800",
        "title": "Senior AI Automation Engineer",
        "questions": [
            {"id": "q1", "text": "Локация / место работы:", "type": "radio", "required": True, "options": ["Москва", "Удалённо (Вне РФ)", "Офис"]},
            {"id": "q2", "text": "Сколько лет коммерческого опыта Python?", "type": "number", "required": True},
            {"id": "q3", "text": "Используемые технологии:", "type": "checkbox", "required": False, "options": ["Python", "FastAPI", "n8n", "Ruby"]},
            {"id": "q4", "text": "Ссылка на GitHub:", "type": "text", "required": False},
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None

    profile_dict = {
        "skills": ["Python", "FastAPI", "n8n", "AI agents", "PostgreSQL", "Docker"],
        "years_experience": 3,
        "remote_required": True,
        "github": "https://github.com/mikheooo",
        "portfolio": "https://mikheooo.github.io/portfolio/",
    }

    suggested = generate_suggested_answers(quest, profile_data=profile_dict)
    assert suggested["q1"] == "Удалённо (Вне РФ)"
    assert suggested["q2"] == "3"
    assert "Python" in suggested["q3"]
    assert "FastAPI" in suggested["q3"]
    assert "n8n" in suggested["q3"]
    assert "Ruby" not in suggested["q3"]
    assert suggested["q4"] == "https://github.com/mikheooo"

    # Must be 100% valid
    val = validate_human_answers(quest, suggested)
    assert val.ok is True
    assert val.status == HHQuestionStatus.READY_TO_SUBMIT.value


# ---------------------------------------------------------------------------
# Test 11: CLI Questionnaire Suggest Command
# ---------------------------------------------------------------------------

def test_cli_questionnaire_suggest_command(clean_db, capsys):
    """CLI suggest displays tailored suggestions and applies them when --apply is passed."""
    from ai_assistant import cli

    snapshot = {
        "vacancy_stable_id": "hh:999111",
        "title": "AI Engineer",
        "questions": [
            {"id": "q1", "text": "Опыт Python (лет):", "type": "number", "required": True},
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None

    ret = cli.questionnaire_suggest_cmd(quest.questionnaire_id, apply_answers=True)
    assert ret == 0
    out = capsys.readouterr().out
    assert "TAILORED QUESTIONNAIRE SUGGESTIONS" in out
    assert "Suggested answers automatically applied" in out

    # Verify status updated to READY_TO_SUBMIT
    updated = db.get_hh_questionnaire(quest.questionnaire_id)
    assert updated["status"] == HHQuestionStatus.READY_TO_SUBMIT.value
    assert updated["answers"] is not None
    assert len(updated["answers"]) > 0
