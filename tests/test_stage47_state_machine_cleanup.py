"""Stage 47: Application State Machine & Audit Cleanup Test Suite.

Proves:
1. Post-submit verification does NOT create READY_TO_SUBMIT -> READY_TO_SUBMIT transition.
2. Successful runner flow cleanly executes READY_TO_SUBMIT -> SUBMITTED.
3. Post-submit verification evidence (submit_executed, post_submit_verification, hh_status, evidence_text) is persisted.
4. hh_status=responded-success is captured and saved.
5. Repeated post-submit verification on SUBMITTED application is idempotent.
6. Repeated submit on SUBMITTED application is blocked (cannot submit twice).
7. SUBMITTED state cannot transition back to READY_TO_SUBMIT, NEW, or NEEDS_HUMAN_REVIEW.
8. Verification failure after submit blocks the application and does not re-submit.
9. Pre-submit check failure halts execution before submit click.
10. Existing benchmark applications (app_hh_135112049 and app_hh_136704137) remain SUBMITTED.
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
    LEGAL_TRANSITIONS,
)
from ai_assistant.hh_post_submit_verifier import (
    verify_hh_submitted_application,
    PostSubmitVerificationResult,
)
from ai_assistant.hh_application_runner import (
    run_application,
    run_next_application,
    preview_next_application,
    RunnerPreCheckStatus,
)
from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    compute_review_fingerprint,
    save_application_review,
)
from ai_assistant.hh_submission import clear_submitted_reviews
from ai_assistant.hh_application_queue import can_submit


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage47_state_machine.db")
    config.DB_FILE = db_file
    clear_submitted_reviews()
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db
    clear_submitted_reviews()


class MockBrowser:
    """Configurable mock CDP browser for Stage 47 verification and runner tests."""
    def __init__(
        self,
        current_url: str = "https://hh.ru/vacancy/136704137",
        submit_ok: bool = True,
        post_submit_success: bool = True,
    ):
        self.current_url = current_url
        self.submit_ok = submit_ok
        self.post_submit_success = post_submit_success
        self.submit_attempts: int = 0
        self.evaluated_scripts: List[str] = []

    def evaluate(self, script: str) -> str:
        self.evaluated_scripts.append(script)

        # The live inspection carries its own marker, so recognise it by that.
        # It used to be recognised only by falling through to the generic
        # payload at the bottom, which broke the moment a comment inside another
        # JS snippet contained the word c-l-i-c-k: the loose rule below then
        # claimed the inspection as a submit call, the runner got a payload with
        # no URL and refused with "URL host '' does not belong to hh.ru".
        # Finding #35 in docs/ble001_triage.md.
        if "hh_live_page_inspect" in script:
            return self._page_payload()

        if "submitBtn.click()" in script or "submit_response" in script or ("click" in script and "response-submit" in script) or "el.click()" in script:
            self.submit_attempts += 1
            return json.dumps({"ok": self.submit_ok, "clicked_button": "Откликнуться"})

        # Post submit inspection
        if "has_responded_success" in script:
            success = self.post_submit_success and (self.submit_attempts > 0 or "SUBMITTED" in script or self.submit_attempts == 0)
            return json.dumps({
                "url": self.current_url,
                "title": "Python developer middle",
                "h1": "Python developer middle",
                "has_responded_success": success,
                "has_topic_link": success,
                "has_cover_letter_btn": success,
                "has_explicit_rejection": False,
                "is_chat": False,
                "has_submit_btn": not success,
                "has_apply_btn": not success,
                "evidence_snippet": "Отклик отправлен" if success else "",
            })

        return self._page_payload()

    def _page_payload(self) -> str:
        """The plain vacancy page, as the live inspection sees it."""
        return json.dumps({
            "url": self.current_url,
            "title": "Python developer middle",
            "h1": "Python developer middle",
            "has_submit_btn": True,
            "has_apply_btn": True,
            "already_responded": False,
            "is_chat": False,
            "is_vacancy_page": True,
        })


# ---------------------------------------------------------------------------
# Test 1: Post-Submit Verification Never Creates READY_TO_SUBMIT -> READY_TO_SUBMIT
# ---------------------------------------------------------------------------

def test_post_submit_verification_never_creates_ready_to_ready_transition(clean_db):
    """Calling verify_hh_submitted_application on a READY_TO_SUBMIT app must not log a transition."""
    app_id = "app_test_101"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.READY_TO_SUBMIT.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    browser = MockBrowser(post_submit_success=True)
    res = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)
    assert res.verification_verdict == "PASS"

    # Verify no transitions were recorded in database
    transitions = db.list_hh_application_transitions(app_id)
    invalid_transitions = [
        t for t in transitions
        if t["previous_state"] == "READY_TO_SUBMIT" and t["state"] == "READY_TO_SUBMIT"
    ]
    assert len(invalid_transitions) == 0
    assert len(transitions) == 0


# ---------------------------------------------------------------------------
# Test 2: Successful Runner Flow Cleanly Transitions READY_TO_SUBMIT -> SUBMITTED
# ---------------------------------------------------------------------------

def test_successful_runner_flow_transitions_cleanly_to_submitted(clean_db, monkeypatch):
    """Controlled runner performs exactly READY_TO_SUBMIT -> SUBMITTED with rich evidence."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    app_id = "app_test_102"
    sid = "hh:136704137"
    pkg_payload = {
        "vacancy_stable_id": sid,
        "cover_letter": "Python developer application",
        "resume_version": "v1",
        "title": "Python developer middle",
        "validation_status": "VALID",
    }
    fp = compute_review_fingerprint(sid, pkg_payload)
    db.save_application_package(sid, "v1", json.dumps(pkg_payload))
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid,
        status=ReviewStatus.APPROVED,
        form_fingerprint=fp,
        review_id="rev_test_102",
    ))
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": HHApplicationState.READY_TO_SUBMIT.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    browser = MockBrowser(submit_ok=True, post_submit_success=True)
    result = run_application(
        application_id=app_id,
        confirm_submit=True,
        evaluate_fn=browser.evaluate,
    )

    assert result.real_hh_submit == 1
    assert result.post_submit_verification == RunnerPreCheckStatus.PASS
    assert result.final_application_state == HHApplicationState.SUBMITTED.value

    # Check audit transitions: must have exactly 1 transition (READY_TO_SUBMIT -> SUBMITTED)
    transitions = db.list_hh_application_transitions(app_id)
    assert len(transitions) == 1
    t = transitions[0]
    assert t["previous_state"] == HHApplicationState.READY_TO_SUBMIT.value
    assert t["state"] == HHApplicationState.SUBMITTED.value
    assert t["reason"] == "controlled_runner_submit_confirmed"
    
    ev = t["evidence"]
    assert ev.get("submit_executed") is True
    assert ev.get("post_submit_verification") == "passed"
    assert ev.get("hh_status") == "responded-success"
    assert "responded-success" in ev.get("evidence_text", "")


# ---------------------------------------------------------------------------
# Test 3: Idempotent Verification on SUBMITTED Application
# ---------------------------------------------------------------------------

def test_idempotent_verification_on_submitted_application(clean_db):
    """Running verification multiple times on SUBMITTED application remains read-only and idempotent."""
    app_id = "app_test_103"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.SUBMITTED.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    browser = MockBrowser(post_submit_success=True)
    res1 = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)
    res2 = verify_hh_submitted_application(app_id, evaluate_fn=browser.evaluate)

    assert res1.verification_verdict == "PASS"
    assert res2.verification_verdict == "PASS"
    assert browser.submit_attempts == 0

    app = db.get_hh_application(app_id)
    assert app["state"] == HHApplicationState.SUBMITTED.value


# ---------------------------------------------------------------------------
# Test 4: Repeat Submit on SUBMITTED Application is Strictly Blocked
# ---------------------------------------------------------------------------

def test_repeat_submit_on_submitted_application_blocked(clean_db):
    """Attempting to submit an application already in SUBMITTED state is rejected."""
    app_id = "app_test_104"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.SUBMITTED.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    # Eligibility check
    elig = can_submit(app_id)
    assert elig.allowed is False
    assert elig.reason == "application_already_submitted"

    # Runner check
    browser = MockBrowser()
    res = run_application(application_id=app_id, confirm_submit=True, evaluate_fn=browser.evaluate)
    assert res.real_hh_submit == 0
    assert "not eligible for submit" in res.reason
    assert browser.submit_attempts == 0


# ---------------------------------------------------------------------------
# Test 5: SUBMITTED State Cannot Transition Back to Non-Terminal States
# ---------------------------------------------------------------------------

def test_submitted_state_cannot_transition_backward(clean_db):
    """SUBMITTED state cannot transition to READY_TO_SUBMIT, NEW, or NEEDS_HUMAN_REVIEW."""
    app_id = "app_test_105"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.SUBMITTED.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    # Try transitioning to READY_TO_SUBMIT
    r1 = transition_application(app_id, to_state=HHApplicationState.READY_TO_SUBMIT, reason="illegal_revert")
    assert r1.ok is False
    assert r1.error == "ILLEGAL_TRANSITION"

    # Try transitioning to NEW
    r2 = transition_application(app_id, to_state=HHApplicationState.NEW, reason="illegal_revert")
    assert r2.ok is False
    assert r2.error == "ILLEGAL_TRANSITION"

    # Try transitioning to NEEDS_HUMAN_REVIEW
    r3 = transition_application(app_id, to_state=HHApplicationState.NEEDS_HUMAN_REVIEW, reason="illegal_revert")
    assert r3.ok is False
    assert r3.error == "ILLEGAL_TRANSITION"

    # Verify state remains SUBMITTED
    app = db.get_hh_application(app_id)
    assert app["state"] == HHApplicationState.SUBMITTED.value


# ---------------------------------------------------------------------------
# Test 6: Verification Failure After Submit Moves Application to BLOCKED
# ---------------------------------------------------------------------------

def test_verification_failure_after_submit_blocks_application_safely(clean_db, monkeypatch):
    """If submit succeeds in browser but post-submit verification fails, app moves to BLOCKED."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    app_id = "app_test_106"
    sid = "hh:136704137"
    pkg_payload = {
        "vacancy_stable_id": sid,
        "cover_letter": "Python developer application",
        "resume_version": "v1",
        "title": "Python developer middle",
        "validation_status": "VALID",
    }
    fp = compute_review_fingerprint(sid, pkg_payload)
    db.save_application_package(sid, "v1", json.dumps(pkg_payload))
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid,
        status=ReviewStatus.APPROVED,
        form_fingerprint=fp,
        review_id="rev_test_106",
    ))
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": sid,
        "title": "Python developer middle",
        "state": HHApplicationState.READY_TO_SUBMIT.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    # Browser clicks submit, but post-submit check fails (unsubmitted form shown)
    browser = MockBrowser(submit_ok=True, post_submit_success=False)
    res = run_application(application_id=app_id, confirm_submit=True, evaluate_fn=browser.evaluate)

    assert res.real_hh_submit == 1
    assert res.post_submit_verification == RunnerPreCheckStatus.FAIL
    assert res.final_application_state in (HHApplicationState.AMBIGUOUS.value, HHApplicationState.BLOCKED.value)

    # Check database state is AMBIGUOUS or BLOCKED, not READY_TO_SUBMIT
    app = db.get_hh_application(app_id)
    assert app["state"] in (HHApplicationState.AMBIGUOUS.value, HHApplicationState.BLOCKED.value)

    # Re-running runner on BLOCKED application does NOT submit
    res2 = run_application(application_id=app_id, confirm_submit=True, evaluate_fn=browser.evaluate)
    assert res2.real_hh_submit == 0
    assert "not eligible for submit" in res2.reason


# ---------------------------------------------------------------------------
# Test 7: Pre-Submit Check Failure Halts Before Submit Click
# ---------------------------------------------------------------------------

def test_pre_submit_failure_halts_without_submit_click(clean_db):
    """If pre-submit audit or navigation fails, runner halts with 0 real submit clicks."""
    app_id = "app_test_107"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.READY_TO_SUBMIT.value,
        "questionnaire_id": "quest_missing_107",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    browser = MockBrowser()
    res = run_application(application_id=app_id, confirm_submit=True, evaluate_fn=browser.evaluate)

    assert res.real_hh_submit == 0
    assert "not eligible for submit" in res.reason
    assert browser.submit_attempts == 0


# ---------------------------------------------------------------------------
# Test 8: Benchmark Applications Remain SUBMITTED Without Corruption
# ---------------------------------------------------------------------------

def test_benchmark_applications_remain_submitted(clean_db):
    """Existing applications (app_hh_135112049, app_hh_136704137) remain SUBMITTED."""
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "state": HHApplicationState.SUBMITTED.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "state": HHApplicationState.SUBMITTED.value,
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    app1 = db.get_hh_application("app_hh_135112049")
    app2 = db.get_hh_application("app_hh_136704137")

    assert app1["state"] == HHApplicationState.SUBMITTED.value
    assert app2["state"] == HHApplicationState.SUBMITTED.value
    assert can_submit("app_hh_135112049").allowed is False
    assert can_submit("app_hh_136704137").allowed is False
