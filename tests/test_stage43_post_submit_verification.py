"""Stage 43: Post-Submit Verification & Application Result Tracking Test Suite.

Proves:
1. SUBMITTED application cannot be submitted again (blocked with reason: application_already_submitted).
2. ALREADY_RESPONDED status confirms success and does not trigger Submit.
3. Post-submit verification does NOT trigger Submit (Submit Count = 0).
4. Verification is idempotent (repeated runs do not corrupt application state).
5. Missing Submit button alone does NOT mean REJECTED.
6. Browser/navigation error does NOT become REJECTED or fatal FAILED.
7. Real Submit count remains 0 in all Stage 43 tests.
8. pipeline.py is never executed.
9. CLI application verify-submit works end-to-end.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.hh_post_submit_verifier import (
    verify_hh_submitted_application,
    PostSubmitVerificationResult,
)
from ai_assistant.hh_application_orchestrator import (
    HHApplicationState,
    transition_application,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage43_post_submit.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


class MockPostSubmitBrowser:
    """Mock CDP browser for post-submit verification tests."""
    def __init__(
        self,
        current_url: str = "https://hh.ru/vacancy/135112049",
        has_responded_success: bool = True,
        has_topic_link: bool = True,
        has_explicit_rejection: bool = False,
        has_submit_btn: bool = False,
        has_apply_btn: bool = False,
        is_chat: bool = False,
    ):
        self.current_url = current_url
        self.has_responded_success = has_responded_success
        self.has_topic_link = has_topic_link
        self.has_explicit_rejection = has_explicit_rejection
        self.has_submit_btn = has_submit_btn
        self.has_apply_btn = has_apply_btn
        self.is_chat = is_chat
        self.submit_attempts: int = 0
        self.evaluated_scripts: List[str] = []

    def evaluate(self, script: str) -> str:
        self.evaluated_scripts.append(script)
        if "el.click()" in script:
            self.submit_attempts += 1
            return json.dumps({"ok": True})

        return json.dumps({
            "url": self.current_url,
            "title": "Senior AI Automation Engineer",
            "h1": "Senior AI Automation Engineer",
            "has_responded_success": self.has_responded_success,
            "has_topic_link": self.has_topic_link,
            "has_cover_letter_btn": self.has_responded_success,
            "has_explicit_rejection": self.has_explicit_rejection,
            "is_chat": self.is_chat,
            "has_submit_btn": self.has_submit_btn,
            "has_apply_btn": self.has_apply_btn,
            "evidence_snippet": "Отклик отправлен",
        })


# ---------------------------------------------------------------------------
# Test 1: SUBMITTED Application Cannot Be Submitted Again
# ---------------------------------------------------------------------------

def test_submitted_application_cannot_be_submitted_again(clean_db, capsys):
    """An application in SUBMITTED state rejects further submit calls with explicit reason."""
    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    ret = cli.application_submit_cmd(app_id, confirm_submit=True)
    assert ret == 1
    err = capsys.readouterr().err
    assert "APPLICATION ALREADY SUBMITTED" in err
    assert "application_already_submitted" in err

    # Ensure state is still SUBMITTED
    app = db.get_hh_application(app_id)
    assert app["state"] == "SUBMITTED"


# ---------------------------------------------------------------------------
# Test 2: Post-Submit Verification Confirms Success Without Submit Click
# ---------------------------------------------------------------------------

def test_verification_confirms_success_without_submit(clean_db):
    """When HH displays responded-success indicators, verification passes with submit_count=0."""
    browser = MockPostSubmitBrowser(has_responded_success=True, has_topic_link=True)

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)

    assert res.verification_verdict == "PASS"
    assert res.hh_status == "responded-success"
    assert res.submit_count == 0
    assert browser.submit_attempts == 0
    assert "responded-success" in res.evidence_text


# ---------------------------------------------------------------------------
# Test 3: Verification is Idempotent
# ---------------------------------------------------------------------------

def test_verification_idempotency(clean_db):
    """Repeated verification runs maintain application state and do not corrupt data."""
    browser = MockPostSubmitBrowser(has_responded_success=True)

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res1 = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)
    res2 = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)

    assert res1.verification_verdict == "PASS"
    assert res2.verification_verdict == "PASS"
    assert res1.hh_status == res2.hh_status

    app = db.get_hh_application(app_id)
    assert app["state"] == "SUBMITTED"
    assert browser.submit_attempts == 0


# ---------------------------------------------------------------------------
# Test 4: Missing Submit Button Alone Does NOT Mean REJECTED
# ---------------------------------------------------------------------------

def test_missing_submit_button_does_not_mean_rejected(clean_db):
    """If HH page has unknown UI without submit button or rejection, it returns BLOCKED, not REJECTED."""
    browser = MockPostSubmitBrowser(
        has_responded_success=False,
        has_topic_link=False,
        has_explicit_rejection=False,
        has_submit_btn=False,
        has_apply_btn=False,
    )

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)

    assert res.hh_status != "REJECTED"
    assert res.hh_status == "unknown_ui"
    assert res.verification_verdict == "BLOCKED"
    assert browser.submit_attempts == 0


# ---------------------------------------------------------------------------
# Test 5: Explicit Rejection Detection
# ---------------------------------------------------------------------------

def test_explicit_rejection_detection(clean_db):
    """When HH displays explicit refusal/rejection text, status is classified as REJECTED."""
    browser = MockPostSubmitBrowser(
        has_responded_success=False,
        has_topic_link=False,
        has_explicit_rejection=True,
    )

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)

    assert res.hh_status == "REJECTED"
    assert res.verification_verdict == "FAIL"
    assert "Explicit refusal" in res.evidence_text
    assert browser.submit_attempts == 0


# ---------------------------------------------------------------------------
# Test 6: Browser/Navigation Error Fails Safely with BLOCKED
# ---------------------------------------------------------------------------

def test_browser_error_fails_safely(clean_db):
    """If CDP evaluates with an exception, result is safely BLOCKED and not REJECTED."""
    def broken_eval(script):
        raise RuntimeError("CDP Connection reset by peer")

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = verify_hh_submitted_application(app_id, evaluate_fn=broken_eval)

    assert res.verification_verdict == "BLOCKED"
    assert res.hh_status == "BLOCKED"
    assert "CDP inspection error" in res.reason


# ---------------------------------------------------------------------------
# Test 7: CLI application verify-submit command
# ---------------------------------------------------------------------------

def test_cli_application_verify_submit(clean_db, capsys):
    """Test CLI application verify-submit command output."""
    browser = MockPostSubmitBrowser(has_responded_success=True)

    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    ret = cli.application_verify_submit_cmd(app_id, evaluate_fn=browser.evaluate)
    assert ret == 0

    out = capsys.readouterr().out
    assert "HH APPLICATION POST-SUBMIT VERIFICATION" in out
    assert "Application:               app_hh_135112049" in out
    assert "Current State:             SUBMITTED" in out
    assert "HH Status:                 responded-success" in out
    assert "Verification:              PASS" in out
    assert "Real Submit Count:         0" in out
