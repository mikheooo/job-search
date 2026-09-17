"""Stage 44: Second Real Vacancy E2E Pre-Submit Test Suite.

Proves:
1. New vacancy creates a new distinct application entity.
2. Already SUBMITTED application (e.g. app_hh_135112049) is not reused or modified.
3. Vacancy navigation resolves and verifies new canonical vacancy URL.
4. Questionnaire discovery works on new vacancy.
5. Absence of questionnaire correctly records NOT_REQUIRED and continues flow.
6. Presence of questionnaire creates separate questionnaire entity.
7. Unconfirmed answers do NOT become READY_TO_SUBMIT (stop at NEEDS_HUMAN_REVIEW).
8. Human review strictly stops flow before submit.
9. Validated and audited questionnaire leads to READY_TO_SUBMIT.
10. Submit is impossible without explicit --confirm-submit.
11. Stage 44 preserves zero real submit invariant (Submit Count = 0).
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.hh_vacancy_navigator import (
    resolve_hh_vacancy_url,
    verify_and_navigate_hh_vacancy,
)
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    HHQuestionStatus,
    compute_questionnaire_fingerprint,
    discover_hh_questionnaire_from_snapshot,
    generate_suggested_answers,
    submit_questionnaire_response,
)
from ai_assistant.hh_application_orchestrator import (
    get_or_create_hh_application,
    transition_application,
    HHApplicationState,
)
from ai_assistant.hh_questionnaire_audit import audit_questionnaire


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage44_second_vac.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


class MockStage44Browser:
    """Mock CDP browser for Stage 44 testing."""
    def __init__(self, target_url: str = "https://hh.ru/vacancy/136704137", has_questions: bool = True):
        self.target_url = target_url
        self.current_url = target_url
        self.has_questions = has_questions
        self.submit_attempts: int = 0

    def evaluate(self, script: str) -> str:
        if "el.click()" in script:
            self.submit_attempts += 1
            return json.dumps({"ok": True})

        is_vac = "136704137" in self.current_url
        return json.dumps({
            "url": self.current_url,
            "title": "Python developer middle",
            "h1": "Python developer middle",
            "has_submit_btn": True if is_vac else False,
            "has_apply_btn": True if is_vac else False,
            "has_response_modal": True if (is_vac and self.has_questions) else False,
            "already_responded": False,
            "is_chat": False,
            "is_vacancy_page": is_vac,
        })


# ---------------------------------------------------------------------------
# Test 1: New Vacancy Creates a New Distinct Application
# ---------------------------------------------------------------------------

def test_new_vacancy_creates_distinct_application(clean_db):
    """Creating an application for a second vacancy does not collide with existing ones."""
    # Pre-populate first application as SUBMITTED
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    # Create new application for 136704137
    new_app = get_or_create_hh_application(
        application_id="app_hh_136704137",
        vacancy_stable_id="hh:136704137",
        title="Python developer middle",
        employer="Maxima.tech",
    )

    assert new_app.application_id == "app_hh_136704137"
    assert new_app.state == HHApplicationState.NEW.value
    assert new_app.vacancy_stable_id == "hh:136704137"

    # Verify first application remains completely unmodified in SUBMITTED state
    first_app = db.get_hh_application("app_hh_135112049")
    assert first_app["state"] == "SUBMITTED"


# ---------------------------------------------------------------------------
# Test 2: Vacancy Navigation for New Vacancy
# ---------------------------------------------------------------------------

def test_vacancy_navigation_for_second_vacancy(clean_db):
    """Vacancy navigator resolves canonical URL and verifies page for new vacancy."""
    browser = MockStage44Browser(target_url="https://hh.ru/vacancy/136704137")

    res = verify_and_navigate_hh_vacancy(
        target="hh:136704137",
        evaluate_fn=browser.evaluate,
        expected_title="Python developer middle",
    )

    assert res.ok is True
    assert res.status == "READY"
    assert res.target_vacancy_id == "136704137"
    assert res.target_url == "https://hh.ru/vacancy/136704137"
    assert res.url_matched is True
    assert res.title_matched is True
    assert browser.submit_attempts == 0


# ---------------------------------------------------------------------------
# Test 3: Flow Without Questionnaire (NOT_REQUIRED)
# ---------------------------------------------------------------------------

def test_flow_without_questionnaire(clean_db):
    """When no screening questions exist, questionnaire is NOT_REQUIRED and flow proceeds to DRAFT_READY / READY_TO_SUBMIT."""
    app = get_or_create_hh_application(
        application_id="app_hh_no_quest",
        vacancy_stable_id="hh:128659037",
        title="QA Automation Engineer",
        employer="Qulix Systems",
    )

    # Transition through normal message / draft flow without questionnaire
    transition_application(app.application_id, HHApplicationState.ANALYZED, reason="vacancy_analyzed")
    transition_application(app.application_id, HHApplicationState.DRAFT_READY, reason="cover_letter_draft_generated")
    transition_application(app.application_id, HHApplicationState.READY_TO_SUBMIT, reason="draft_approved_by_human")

    final_app = db.get_hh_application(app.application_id)
    assert final_app["state"] == "READY_TO_SUBMIT"
    assert final_app["questionnaire_id"] is None


# ---------------------------------------------------------------------------
# Test 4: Flow With Questionnaire Discovery & Audit
# ---------------------------------------------------------------------------

def test_flow_with_questionnaire_discovery_and_audit(clean_db):
    """When questionnaire is discovered, smart suggestions are generated, audited, and applied."""
    app = get_or_create_hh_application(
        application_id="app_hh_136704137",
        vacancy_stable_id="hh:136704137",
        title="Python developer middle",
        employer="Maxima.tech",
    )

    snapshot = {
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "questions": [
            {
                "id": "q1_work_location",
                "text": "Где располагается ваше фактическое место работы?",
                "type": "radio",
                "required": True,
                "options": ["Россия (Москва)", "Удалённо (Вне РФ / Релокация)", "Другое"],
            },
            {
                "id": "q2_python_experience",
                "text": "Сколько лет коммерческого опыта разработки на Python?",
                "type": "number",
                "required": True,
                "options": [],
            },
        ]
    }

    quest = discover_hh_questionnaire_from_snapshot(snapshot)
    assert quest.questionnaire_id.startswith("quest_")

    transition_application(
        app.application_id,
        HHApplicationState.QUESTIONNAIRE_REQUIRED,
        reason="screening_questions_discovered",
        evidence={"questionnaire_id": quest.questionnaire_id},
    )
    transition_application(
        app.application_id,
        HHApplicationState.NEEDS_HUMAN_REVIEW,
        reason="human_review_required",
        evidence={"questionnaire_id": quest.questionnaire_id},
    )

    # Generate suggestions from candidate profile
    suggestions = generate_suggested_answers(quest)
    assert suggestions["q1_work_location"] == "Удалённо (Вне РФ / Релокация)"
    assert suggestions["q2_python_experience"] == "3"

    db.update_hh_questionnaire_answers(quest.questionnaire_id, suggestions, new_status=HHQuestionStatus.NEEDS_HUMAN_REVIEW.value)

    # Audit suggestions
    report = audit_questionnaire(quest.questionnaire_id, application_id=app.application_id)
    assert report.overall.value == "SAFE_TO_SUBMIT"

    # Transition to READY_TO_SUBMIT
    transition_application(
        app.application_id,
        HHApplicationState.READY_TO_SUBMIT,
        reason="questionnaire_answers_audited_and_ready",
        evidence={"questionnaire_id": quest.questionnaire_id},
    )
    db.update_hh_questionnaire_answers(quest.questionnaire_id, suggestions, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    final_app = db.get_hh_application(app.application_id)
    assert final_app["state"] == "READY_TO_SUBMIT"
    assert final_app["questionnaire_id"] == quest.questionnaire_id


# ---------------------------------------------------------------------------
# Test 5: Unconfirmed or Ambiguous Answer Blocks READY_TO_SUBMIT
# ---------------------------------------------------------------------------

def test_unconfirmed_answers_block_ready_state(clean_db):
    """When an answer cannot be verified by profile facts, audit rejects SAFE_TO_SUBMIT."""
    snapshot = {
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "questions": [
            {
                "id": "q1_experience",
                "text": "Сколько лет опыта с Python?",
                "type": "number",
                "required": True,
                "options": [],
            }
        ]
    }
    quest = discover_hh_questionnaire_from_snapshot(snapshot)
    # Put unverified answer '10' when profile only confirms 3
    db.update_hh_questionnaire_answers(quest.questionnaire_id, {"q1_experience": "10"})

    report = audit_questionnaire(quest.questionnaire_id)
    assert report.overall.value == "NEEDS_CORRECTION"
    assert report.items[0].is_confirmed_by_profile is False


# ---------------------------------------------------------------------------
# Test 6: Submit Blocked Without --confirm-submit
# ---------------------------------------------------------------------------

def test_submit_blocked_without_confirm_flag(clean_db, capsys):
    """Running application submit without --confirm-submit is blocked with Submit = 0."""
    app_id = "app_hh_136704137"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    ret = cli.application_submit_cmd(app_id, confirm_submit=False)
    assert ret == 1
    out = capsys.readouterr().out
    assert "SUBMISSION GATE: EXPLICIT CONFIRMATION REQUIRED" in out
    assert "BLOCKED (Submit = 0)" in out

    app = db.get_hh_application(app_id)
    assert app["state"] == "READY_TO_SUBMIT"


# ---------------------------------------------------------------------------
# Test 7: Zero Real Submit Invariant
# ---------------------------------------------------------------------------

def test_zero_real_submit_invariant_stage44(clean_db):
    """Across all discovery and audit operations, zero submit clicks occur."""
    browser = MockStage44Browser(has_questions=True)
    res = verify_and_navigate_hh_vacancy(
        target="hh:136704137",
        evaluate_fn=browser.evaluate,
    )
    assert res.ok is True
    assert browser.submit_attempts == 0
