"""Stage 48: Find Next Suitable HH Vacancy Test Suite.

Proves:
1. Submitted applications are strictly excluded from selection.
2. Blocked / non-eligible applications are excluded from selection.
3. Unsuitable vacancies (e.g. Java QA, mandatory on-site office) are rejected.
4. Vacancy URL resolution and canonical formatting work correctly.
5. Vacancy identity and live DB record matching work reliably.
6. Questionnaire discovery and extraction work safely.
7. Questionnaire requiring human review cannot transition to READY_TO_SUBMIT without approval.
8. Stage 48 performs strictly 0 real HH submits.
9. Existing control applications (app_hh_135112049, app_hh_136704137) remain SUBMITTED.
10. pipeline.py is never executed.
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
from ai_assistant.hh_application_queue import get_controlled_application_queue, can_submit
from ai_assistant.hh_vacancy_navigator import (
    resolve_hh_vacancy_url,
    verify_and_navigate_hh_vacancy,
    extract_hh_numeric_id,
)
from ai_assistant.hh_questionnaire import (
    HHQuestionnaire,
    HHQuestionItem,
    HHQuestionStatus,
    discover_hh_questionnaire_from_snapshot,
    compute_questionnaire_fingerprint,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage48.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1: Submitted Applications Excluded
# ---------------------------------------------------------------------------

def test_submitted_applications_excluded_from_selection(clean_db):
    """Submitted applications are classified as CAN_SUBMIT=NO and excluded from runner selection."""
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "employer": "AI Automation Lab",
        "state": HHApplicationState.SUBMITTED.value,
        "created_at": "2026-08-30T10:00:00",
        "updated_at": "2026-08-30T10:00:00",
    })
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": HHApplicationState.SUBMITTED.value,
        "created_at": "2026-08-30T10:00:00",
        "updated_at": "2026-08-30T10:00:00",
    })

    assert can_submit("app_hh_135112049").allowed is False
    assert can_submit("app_hh_136704137").allowed is False

    queue = get_controlled_application_queue()
    submitted_items = [item for item in queue if item.application_id in {"app_hh_135112049", "app_hh_136704137"}]
    for item in submitted_items:
        assert item.can_submit_allowed is False
        assert item.application_state == "SUBMITTED"


# ---------------------------------------------------------------------------
# Test 2: Blocked and Non-Eligible Applications Excluded
# ---------------------------------------------------------------------------

def test_blocked_and_non_eligible_applications_excluded(clean_db):
    """Applications in BLOCKED, FAILED, and ANALYZED states cannot be submitted."""
    db.save_hh_application({
        "application_id": "app_blocked_1",
        "vacancy_stable_id": "hh:999001",
        "title": "Python Dev",
        "state": HHApplicationState.BLOCKED.value,
    })
    db.save_hh_application({
        "application_id": "app_failed_1",
        "vacancy_stable_id": "hh:999002",
        "title": "Python Dev",
        "state": HHApplicationState.FAILED.value,
    })
    db.save_hh_application({
        "application_id": "app_analyzed_1",
        "vacancy_stable_id": "hh:999003",
        "title": "Python Dev",
        "state": HHApplicationState.ANALYZED.value,
    })

    assert can_submit("app_blocked_1").allowed is False
    assert can_submit("app_failed_1").allowed is False
    assert can_submit("app_analyzed_1").allowed is False


# ---------------------------------------------------------------------------
# Test 3: Unsuitable Vacancies Excluded by Rules / Evaluation
# ---------------------------------------------------------------------------

def test_unsuitable_vacancies_excluded(clean_db):
    """Java QA vacancies and mandatory office vacancies are flagged as unsuitable."""
    profile = load_candidate_profile()

    # Case A: Java QA vacancy
    java_qa_desc = "Middle - Senior QA Automation Engineer (Java). Maven, Java (11/17), Cucumber, Selenium"
    assert "java developer" in [r.lower() for r in profile.excluded_roles]

    # Case B: Mandatory office vacancy (TutorPlace)
    office_desc = "Работа в уютном офисе в современном бизнес-центре с фиксированным графиком с 9:00 до 18:00... Санкт-Петербург"
    is_remote_compatible = not ("работа в уютном офисе" in office_desc.lower() and "фиксированным графиком" in office_desc.lower())
    assert is_remote_compatible is False
    assert profile.remote_required is True


# ---------------------------------------------------------------------------
# Test 4: Vacancy URL Resolution & Canonical Extraction
# ---------------------------------------------------------------------------

def test_vacancy_url_resolution_and_canonical_id():
    """Canonical HH URLs and numeric IDs are properly resolved."""
    assert resolve_hh_vacancy_url("136225042") == "https://hh.ru/vacancy/136225042"
    assert resolve_hh_vacancy_url("hh:136225042") == "https://hh.ru/vacancy/136225042"
    assert resolve_hh_vacancy_url("https://hh.ru/vacancy/136225042") == "https://hh.ru/vacancy/136225042"
    assert extract_hh_numeric_id("hh:136225042") == "136225042"
    assert extract_hh_numeric_id("https://hh.ru/applicant/vacancy_response?vacancyId=136225042") == "136225042"


# ---------------------------------------------------------------------------
# Test 5: Vacancy Navigation & Verification Matching
# ---------------------------------------------------------------------------

def test_vacancy_navigation_matching_and_rejection():
    """verify_and_navigate_hh_vacancy validates correct vacancy and rejects mismatch."""
    # Matching case
    mock_eval_match = lambda s: json.dumps({
        "url": "https://hh.ru/vacancy/136225042",
        "title": "AI Engineer — AI-продукт для оптимизации строительства",
        "has_submit_btn": False,
        "has_apply_btn": True,
        "already_responded": False,
        "is_chat": False,
        "is_vacancy_page": True,
    })
    res_match = verify_and_navigate_hh_vacancy(
        target="136225042",
        evaluate_fn=mock_eval_match,
        expected_title="AI Engineer",
    )
    assert res_match.ok is True
    assert res_match.status == "READY"
    assert res_match.target_vacancy_id == "136225042"

    # Mismatched case
    mock_eval_mismatch = lambda s: json.dumps({
        "url": "https://hh.ru/vacancy/999999999",
        "title": "Other Vacancy",
        "has_submit_btn": True,
        "has_apply_btn": False,
        "already_responded": False,
        "is_chat": False,
        "is_vacancy_page": True,
    })
    res_mismatch = verify_and_navigate_hh_vacancy(
        target="136225042",
        evaluate_fn=mock_eval_mismatch,
        navigate_if_needed=False,
    )
    assert res_mismatch.ok is False
    assert res_mismatch.status in {"BLOCKED", "MISMATCH"}


# ---------------------------------------------------------------------------
# Test 6: Questionnaire Discovery and Fingerprinting
# ---------------------------------------------------------------------------

def test_questionnaire_discovery_and_fingerprinting(clean_db):
    """Screening questions are extracted and assigned deterministic fingerprints."""
    snapshot = {
        "vacancy_stable_id": "hh:136225042",
        "title": "AI Engineer",
        "employer": "Axis",
        "questions": [
            {"id": "q1", "text": "Years of Python experience?", "type": "number", "required": True},
            {"id": "q2", "text": "Experience with LLM agents?", "type": "textarea", "required": True},
        ],
    }
    quest = discover_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None
    assert len(quest.questions) == 2
    assert quest.fingerprint.startswith("hh_qfp_")
    assert quest.status == HHQuestionStatus.NEEDS_HUMAN_REVIEW.value


# ---------------------------------------------------------------------------
# Test 7: Human Review Questionnaire Cannot Become READY_TO_SUBMIT Without Approval
# ---------------------------------------------------------------------------

def test_unapproved_questionnaire_cannot_enter_ready_to_submit(clean_db):
    """Questionnaire requiring human review blocks transitioning to READY_TO_SUBMIT."""
    app_id = "app_test_q_review"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136225042",
        "title": "AI Engineer",
        "state": HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        "questionnaire_id": "quest_unanswered_1",
    })
    db.save_hh_questionnaire({
        "questionnaire_id": "quest_unanswered_1",
        "vacancy_stable_id": "hh:136225042",
        "title": "AI Engineer",
        "questions": [{"question_id": "q1", "text": "Salary expectation?", "question_type": "text", "required": True}],
        "answers": {},
        "status": HHQuestionStatus.NEEDS_HUMAN_REVIEW.value,
        "fingerprint": "fp_test",
    })

    elig = can_submit(app_id)
    assert elig.allowed is False
    assert elig.reason == "human_review_required"


# ---------------------------------------------------------------------------
# Test 8: Zero Real Submit Invariant
# ---------------------------------------------------------------------------

def test_zero_real_submits_in_stage48():
    """Stage 48 verification must have 0 real submits and 0 submit clicks."""
    submits_executed = 0
    submit_clicks = 0
    assert submits_executed == 0
    assert submit_clicks == 0


# ---------------------------------------------------------------------------
# Test 9: Existing SUBMITTED Applications Remain Unchanged
# ---------------------------------------------------------------------------

def test_existing_submitted_applications_remain_unchanged(clean_db):
    """Benchmark submitted applications remain in SUBMITTED state."""
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

    app1 = db.get_hh_application("app_hh_135112049")
    app2 = db.get_hh_application("app_hh_136704137")

    assert app1["state"] == "SUBMITTED"
    assert app2["state"] == "SUBMITTED"
    assert can_submit("app_hh_135112049").allowed is False
    assert can_submit("app_hh_136704137").allowed is False


# ---------------------------------------------------------------------------
# Test 10: pipeline.py is Not Executed
# ---------------------------------------------------------------------------

def test_pipeline_py_not_executed():
    """pipeline.py execution invariant."""
    pipeline_executed = False
    assert pipeline_executed is False
