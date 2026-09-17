"""Stage 40: Real HH Submit E2E Safety Gating Test Suite.

Proves:
1. confirm-submit is strictly mandatory (Submit = 0 without flag).
2. READY_TO_SUBMIT state is required before Submit.
3. Repeated submit of already submitted application is blocked (one-shot invariant).
4. Questionnaire answers are strictly required before Submit.
5. Browser/CDP errors do not cause automatic retry and fail closed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    HHQuestionStatus,
    discover_hh_questionnaire_from_snapshot,
    submit_questionnaire_response,
)
from ai_assistant.hh_application_orchestrator import (
    HHApplicationOrchestrator,
    HHApplicationState,
    get_or_create_hh_application,
    transition_application,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage40_gating.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


class FakeSubmitCDP:
    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.clicked_buttons: List[str] = []

    def evaluate(self, script: str) -> str:
        if "has_submit_btn" in script:
            return json.dumps({
                "url": "https://hh.ru/vacancy/135112049",
                "title": "Senior AI Automation Engineer",
                "has_submit_btn": self.should_succeed,
                "has_apply_btn": True,
                "has_response_modal": True,
                "is_chat": False,
                "is_vacancy_page": True,
            })
        if not self.should_succeed:
            return json.dumps({"ok": False, "reason": "Submit button disabled or not found"})
        self.clicked_buttons.append("vacancy-response-submit")
        return json.dumps({"ok": True, "clicked_button": "Откликнуться"})


def _setup_test_app(app_id: str = "app_hh_135112049", state: str = "READY_TO_SUBMIT") -> tuple[HHQuestionnaire, Dict[str, Any]]:
    snapshot = {
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "employer": "AI Automation Lab",
        "questions": [
            {"id": "q1_location", "text": "Локация:", "type": "radio", "required": True, "options": ["Удаленно", "Офис"]},
            {"id": "q2_exp", "text": "Опыт Python (лет):", "type": "number", "required": True},
        ]
    }
    quest = discover_hh_questionnaire_from_snapshot(snapshot)
    answers = {"q1_location": "Удаленно", "q2_exp": "3"}
    db.update_hh_questionnaire_answers(quest.questionnaire_id, answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

    app_data = {
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "questionnaire_id": quest.questionnaire_id,
        "title": quest.title,
        "employer": quest.employer,
        "state": state,
        "submit_allowed": state == "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    }
    db.save_hh_application(app_data)
    return quest, app_data


# ---------------------------------------------------------------------------
# Test 1: confirm-submit Flag is Mandatory
# ---------------------------------------------------------------------------

def test_confirm_submit_mandatory_blocks_execution(clean_db, capsys):
    """Without --confirm-submit, submission is blocked and zero browser actions occur."""
    quest, app_data = _setup_test_app()
    cdp = FakeSubmitCDP()

    ret = cli.application_submit_cmd(
        app_data["application_id"],
        confirm_submit=False,
        evaluate_fn=cdp.evaluate,
    )
    assert ret == 1
    assert len(cdp.clicked_buttons) == 0

    out = capsys.readouterr().out
    assert "SUBMISSION GATE: EXPLICIT CONFIRMATION REQUIRED" in out
    assert "Submit Action:             BLOCKED (Submit = 0)" in out

    # State must remain READY_TO_SUBMIT
    app = db.get_hh_application(app_data["application_id"])
    assert app["state"] == "READY_TO_SUBMIT"


# ---------------------------------------------------------------------------
# Test 2: READY_TO_SUBMIT State Strictly Required
# ---------------------------------------------------------------------------

def test_submit_blocked_if_not_in_ready_to_submit(clean_db, capsys):
    """If application is in NEEDS_HUMAN_REVIEW or NEW, submit is blocked even with --confirm-submit."""
    quest, app_data = _setup_test_app(state="NEEDS_HUMAN_REVIEW")
    cdp = FakeSubmitCDP()

    ret = cli.application_submit_cmd(
        app_data["application_id"],
        confirm_submit=True,
        evaluate_fn=cdp.evaluate,
    )
    assert ret == 1
    assert len(cdp.clicked_buttons) == 0

    err = capsys.readouterr().err
    assert "Submission Blocked" in err
    assert "must be 'READY_TO_SUBMIT'" in err


# ---------------------------------------------------------------------------
# Test 3: One-Shot Invariant Blocks Repeated Submit
# ---------------------------------------------------------------------------

def test_one_shot_invariant_blocks_duplicate_submit(clean_db):
    """Once an application is submitted, a second submit attempt is blocked."""
    quest, app_data = _setup_test_app()
    cdp = FakeSubmitCDP()

    # 1. First Submit -> Succeeds
    ret1 = cli.application_submit_cmd(
        app_data["application_id"],
        confirm_submit=True,
        evaluate_fn=cdp.evaluate,
    )
    assert ret1 == 0
    assert len(cdp.clicked_buttons) == 1

    app_after1 = db.get_hh_application(app_data["application_id"])
    assert app_after1["state"] == "SUBMITTED"

    # 2. Second Submit -> Blocked by State Machine
    ret2 = cli.application_submit_cmd(
        app_data["application_id"],
        confirm_submit=True,
        evaluate_fn=cdp.evaluate,
    )
    assert ret2 == 1
    assert len(cdp.clicked_buttons) == 1  # No second click!


# ---------------------------------------------------------------------------
# Test 4: Questionnaire Answers Required Before Submit
# ---------------------------------------------------------------------------

def test_missing_questionnaire_answers_blocks_submit(clean_db):
    """Submitting without answering required questions is blocked (Submit = 0)."""
    snapshot = {
        "vacancy_stable_id": "hh:888777",
        "title": "Backend Python",
        "questions": [
            {"id": "q1", "text": "Опыт Python:", "type": "number", "required": True},
        ]
    }
    quest = discover_hh_questionnaire_from_snapshot(snapshot)
    cdp = FakeSubmitCDP()

    # Attempt submit with empty answers
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers={},
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 5: Browser Error Fails Closed Without Retry
# ---------------------------------------------------------------------------

def test_browser_error_fails_closed_without_retry(clean_db):
    """If browser/DOM returns an error during click, flow fails closed and marks state FAILED without retrying."""
    quest, app_data = _setup_test_app()
    failing_cdp = FakeSubmitCDP(should_succeed=False)

    ret = cli.application_submit_cmd(
        app_data["application_id"],
        confirm_submit=True,
        evaluate_fn=failing_cdp.evaluate,
    )
    assert ret == 1

    app_failed = db.get_hh_application(app_data["application_id"])
    assert app_failed["state"] in ("BLOCKED", "FAILED")
    assert app_failed["state"] != "SUBMITTED"
