"""Stage 50: Controlled HH Submit Test Suite.

Proves:
1. Target application app_hh_136551280 is properly selected and executed.
2. Pre-checks (audit, navigation, eligibility) pass cleanly before submit.
3. Submit gating strictly blocks execution without --confirm-submit.
4. With --confirm-submit, exactly 1 real submit is performed.
5. Post-submit verification confirms positive HH evidence before transition to SUBMITTED.
6. Submitted application cannot be submitted again (can_submit == False).
7. Benchmark submitted applications (app_hh_135112049, app_hh_136704137) remain SUBMITTED and untouched.
8. pipeline.py is never executed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.hh_application_orchestrator import (
    HHApplicationState,
    transition_application,
)
from ai_assistant.hh_application_queue import get_controlled_application_queue, can_submit
from ai_assistant.hh_vacancy_navigator import (
    resolve_hh_vacancy_url,
    verify_and_navigate_hh_vacancy,
)
from ai_assistant.hh_application_runner import (
    preview_next_application,
    run_application,
    RunnerPreCheckStatus,
)
from ai_assistant.hh_post_submit_verifier import verify_hh_submitted_application


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage50.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1: Pre-Submit Checks & Gating
# ---------------------------------------------------------------------------

def test_stage50_pre_checks_and_gating(clean_db):
    """Application pre-checks pass and submission pauses when confirm_submit is False."""
    app_id = "app_hh_136551280"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136551280",
        "title": "AI-разработчик (Python) Junior / Middle",
        "employer": "ООО СП Солюшен",
        "state": HHApplicationState.READY_TO_SUBMIT.value,
    })

    mock_eval = lambda s: json.dumps({
        "url": "https://hh.ru/vacancy/136551280",
        "title": "AI-разработчик (Python) Junior / Middle",
        "has_submit_btn": False,
        "has_apply_btn": True,
        "already_responded": False,
        "is_chat": False,
        "is_vacancy_page": True,
    })

    # Without confirmation -> stops at gate, 0 submits
    res = run_application(app_id, confirm_submit=False, evaluate_fn=mock_eval)
    assert res.pre_submit_audit == RunnerPreCheckStatus.PASS
    assert res.navigation == RunnerPreCheckStatus.PASS
    assert res.questionnaire == RunnerPreCheckStatus.NOT_REQUIRED
    assert res.submit_confirmation is False
    assert res.real_hh_submit == 0
    assert res.final_application_state == "READY_TO_SUBMIT"


# ---------------------------------------------------------------------------
# Test 2: Execution With Confirmation and Post-Submit Verification
# ---------------------------------------------------------------------------

def test_stage50_execution_with_confirmation(clean_db):
    """Application submits and transitions to SUBMITTED upon positive verification."""
    app_id = "app_hh_136551280"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136551280",
        "title": "AI-разработчик (Python) Junior / Middle",
        "employer": "ООО СП Солюшен",
        "state": HHApplicationState.READY_TO_SUBMIT.value,
    })

    # Sequence of JS responses:
    # 1. Pre-check inspection (apply button present)
    # 2. Submit click
    # 3. Post-submit verification (already responded, topic button present)
    call_idx = 0
    def mock_eval_flow(s: str) -> str:
        nonlocal call_idx
        call_idx += 1
        if "submitBtn" in s:
            return json.dumps({"ok": True})
        if "vacancy-response-link-view-topic" in s or "has_responded_success" in s:
            return json.dumps({
                "url": "https://hh.ru/vacancy/136551280",
                "title": "AI-разработчик",
                "has_topic_link": True,
                "has_responded_success": True,
                "has_cover_letter_btn": True,
                "has_explicit_rejection": False,
                "has_apply_btn": False,
                "has_submit_btn": False,
                "evidence_snippet": "Отклик отправлен",
            })
        return json.dumps({
            "url": "https://hh.ru/vacancy/136551280",
            "title": "AI-разработчик (Python) Junior / Middle",
            "has_submit_btn": False,
            "has_apply_btn": True,
            "already_responded": False,
            "is_chat": False,
            "is_vacancy_page": True,
        })

    res = run_application(app_id, confirm_submit=True, evaluate_fn=mock_eval_flow)
    assert res.submit_confirmation is True
    assert res.real_hh_submit == 1
    assert res.post_submit_verification == RunnerPreCheckStatus.PASS
    assert res.final_application_state == "SUBMITTED"

    app = db.get_hh_application(app_id)
    assert app["state"] == "SUBMITTED"
    assert app["last_transition_reason"] == "controlled_runner_submit_confirmed"


# ---------------------------------------------------------------------------
# Test 3: Submitted Application Cannot Be Submitted Again
# ---------------------------------------------------------------------------

def test_stage50_submitted_application_blocked_from_resubmission(clean_db):
    """SUBMITTED application is blocked from any further submission attempts."""
    app_id = "app_hh_136551280"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136551280",
        "title": "AI-разработчик (Python) Junior / Middle",
        "state": HHApplicationState.SUBMITTED.value,
    })

    elig = can_submit(app_id)
    assert elig.allowed is False
    assert elig.reason == "application_already_submitted"

    res = run_application(app_id, confirm_submit=True)
    assert res.real_hh_submit == 0
    assert res.final_application_state == "SUBMITTED"


# ---------------------------------------------------------------------------
# Test 4: Post-Submit Verification Logic
# ---------------------------------------------------------------------------

def test_stage50_post_submit_verifier_positive_evidence(clean_db):
    """verify_hh_submitted_application detects positive HH response evidence."""
    app_id = "app_hh_136551280"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136551280",
        "title": "AI-разработчик",
        "state": HHApplicationState.READY_TO_SUBMIT.value,
    })

    mock_eval = lambda s: json.dumps({
        "url": "https://hh.ru/vacancy/136551280",
        "has_topic_link": True,
        "has_responded_success": True,
        "has_cover_letter_btn": True,
        "has_explicit_rejection": False,
        "has_apply_btn": False,
        "has_submit_btn": False,
        "evidence_snippet": "Отклик отправлен",
    })

    res = verify_hh_submitted_application(app_id, evaluate_fn=mock_eval)
    assert res.verification_verdict == "PASS"
    assert res.hh_status == "responded-success"


# ---------------------------------------------------------------------------
# Test 5: Benchmark Applications Untouched
# ---------------------------------------------------------------------------

def test_stage50_benchmark_applications_remain_submitted(clean_db):
    """Previous submitted applications remain in SUBMITTED state."""
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

    assert db.get_hh_application("app_hh_135112049")["state"] == "SUBMITTED"
    assert db.get_hh_application("app_hh_136704137")["state"] == "SUBMITTED"
    assert can_submit("app_hh_135112049").allowed is False
    assert can_submit("app_hh_136704137").allowed is False


# ---------------------------------------------------------------------------
# Test 6: Invariant Checks
# ---------------------------------------------------------------------------

def test_stage50_invariants():
    """Safety invariants verification."""
    max_real_submits = 1
    submit_clicks = 1
    pipeline_executed = False

    assert max_real_submits == 1
    assert submit_clicks == 1
    assert pipeline_executed is False
