"""Phase 2.1 Test Suite: Autonomous Submission Crash Safety, Idempotency & Claims.

Proves:
1. Crash before submit: Active claim prevents second runner from submitting (0 calls).
2. Crash after submit before persistence: Claim blocks second runner, external submit count remains 1.
3. Ambiguous timeout on click: Timeout marks state & claim AMBIGUOUS, zero blind retries.
4. Duplicate autonomous run on already submitted vacancy: Runner halts, 0 submit calls.
5. Concurrent claim: Two workers racing -> exactly one acquires claim, exactly 1 submit.
6. Already submitted application in DB: Submission blocked before any DOM/CDP interaction.
7. Kill switch race: Kill switch enabled immediately before click aborts execution (0 clicks).
8. Telegram notification failure after submit: Observability exception does not rollback SUBMITTED.
9. Policy re-approval cannot resubmit: Valid policy approval cannot bypass claimed/submitted invariant.
10. Recovery / second runner does not blindly retry an AMBIGUOUS attempt without human reconciliation.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

import ai_assistant.config as cfg
from ai_assistant import db
from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    compute_review_fingerprint,
    save_application_review,
)
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
)
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.hh_application_orchestrator import (
    HHApplicationState,
    SubmitApproval,
    transition_application,
)
from ai_assistant.hh_application_queue import can_submit
from ai_assistant.hh_application_runner import (
    run_application,
    run_next_application,
)
from ai_assistant.hh_submission import (
    clear_submitted_reviews,
    execute_hh_submission,
)
from ai_assistant.hh_submit_policy import evaluate as evaluate_policy
from ai_assistant.schema import Vacancy


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    clear_submitted_reviews()
    db_file = str(tmp_path / "test_phase2_1_crash.db")
    monkeypatch.setattr(cfg, "DB_FILE", db_file)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setenv("HH_AUTO_SUBMIT", "1")
    db.init_db()
    yield
    clear_submitted_reviews()


def _make_candidate_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["Старший разработчик Python", "Senior Python Developer", "Python Developer"],
        skills=["Python", "FastAPI", "PostgreSQL", "Docker"],
        languages=["Russian"],
        employment_types=["Full-time"],
        minimum_salary=3500,
        salary_currency="USD",
        remote_required=True,
    )


def _setup_vacancy_and_app(
    vid: str = "1234001",
    state: str = "READY_TO_SUBMIT",
    letter_text: str | None = None,
) -> tuple[str, str]:
    sid = f"hh:{vid}"
    app_id = f"app_{vid}"
    vac = Vacancy(
        source="hh",
        source_job_id=vid,
        title="Старший разработчик Python",
        company="TechCorp Solutions",
        description="Разработка высоконагруженных распределенных бэкенд сервисов на Python, PostgreSQL, FastAPI и Docker в компании TechCorp Solutions.",
        job_url=f"https://hh.ru/vacancy/{vid}",
        location="Remote",
    )
    db.save_vacancy(vac)

    if letter_text is None:
        letter_text = (
            "Здравствуйте! Меня очень заинтересовала позиция Старший разработчик Python в компании TechCorp Solutions. "
            "Я обладаю многолетним практическим опытом разработки отказоустойчивых сервисов на Python, "
            "проектирования реляционных баз данных PostgreSQL, построения микросервисной архитектуры на FastAPI "
            "и развертывания в Docker. С удовольствием приму участие в техническом интервью и внесу вклад в развитие проекта."
        )

    pkg = {
        "vacancy_stable_id": sid,
        "cover_letter": letter_text,
        "title": "Старший разработчик Python",
        "employer": "TechCorp Solutions",
        "validation_status": "VALID",
    }
    db.save_application_package(sid, "v1", json.dumps(pkg))
    fp = compute_review_fingerprint(sid, pkg)
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=ReviewStatus.APPROVED,
            form_fingerprint=fp,
            review_id=f"rev_{vid}",
        )
    )

    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": sid,
        "vacancy_id": vid,
        "title": "Старший разработчик Python",
        "employer": "TechCorp Solutions",
        "draft": letter_text,
        "state": state,
    })
    set_application_status(
        sid,
        ApplicationStatus.READY_TO_APPLY if state == "READY_TO_SUBMIT" else ApplicationStatus.SUBMITTED,
    )
    return sid, app_id


class MockCdpEvaluator:
    """Deterministic offline mock for HeadHunter DOM / CDP evaluation."""

    def __init__(self, vid: str, submit_should_succeed: bool = True):
        self.vid = vid
        self.submit_should_succeed = submit_should_succeed
        self.submit_click_count = 0
        self.total_eval_count = 0
        self.call_history: list[str] = []

    def __call__(self, script: str) -> str:
        self.total_eval_count += 1
        self.call_history.append(script)

        # 1. Submit click evaluation
        if "submitBtn.click()" in script or ".click()" in script:
            self.submit_click_count += 1
            if not self.submit_should_succeed:
                return json.dumps({"ok": False, "error": "mock_submit_failed"})
            return json.dumps({"ok": True})

        # 2. Post-submit verification inspect JS
        if "hh_post_submit_verify" in script or "has_responded_success" in script or "hasRespondedSuccess" in script:
            if self.submit_click_count > 0 and self.submit_should_succeed:
                return json.dumps({
                    "url": f"https://hh.ru/vacancy/{self.vid}",
                    "title": "Старший разработчик Python",
                    "h1": "Старший разработчик Python",
                    "has_responded_success": True,
                    "has_topic_link": True,
                    "has_cover_letter_btn": True,
                    "has_explicit_rejection": False,
                    "is_chat": False,
                    "has_submit_btn": False,
                    "has_apply_btn": False,
                    "evidence_snippet": "Отклик отправлен",
                })
            return json.dumps({
                "url": f"https://hh.ru/vacancy/{self.vid}",
                "title": "Старший разработчик Python",
                "h1": "Старший разработчик Python",
                "has_responded_success": False,
                "has_topic_link": False,
                "has_cover_letter_btn": False,
                "has_explicit_rejection": False,
                "is_chat": False,
                "has_submit_btn": True,
                "has_apply_btn": True,
            })

        # 3. Live page check / pre-submit inspection
        return json.dumps({
            "ok": True,
            "url": f"https://hh.ru/vacancy/{self.vid}",
            "title": "Старший разработчик Python",
            "h1": "Старший разработчик Python",
            "has_submit_btn": True,
            "submit_btn_disabled": False,
            "has_apply_btn": True,
            "already_responded": False,
            "is_chat": False,
            "is_vacancy_page": True,
        })


# ==============================================================================
# Scenario 1: Crash before submit
# ==============================================================================
def test_crash_before_submit():
    """Claim acquired, process crashes before submit -> second run sees active claim and does NOT submit."""
    sid, app_id = _setup_vacancy_and_app("1234001")

    # Step 1: Worker 1 acquires claim, then crashes before submitting
    acquired, _reason, claim_info = db.acquire_submission_claim(
        vacancy_stable_id=sid,
        application_id=app_id,
        worker_id="worker_process_1",
    )
    assert acquired is True
    assert claim_info is not None
    assert claim_info["status"] == "ATTEMPTING"

    # Verify DB state after crash: claim is ATTEMPTING, no submission record exists
    claim = db.get_submission_claim(sid)
    assert claim is not None
    assert claim["status"] == "ATTEMPTING"
    assert db.get_submission(sid) is None

    # Step 2: Worker 2 starts autonomous runner
    cdp = MockCdpEvaluator("1234001")
    res = run_application(app_id, auto=True, evaluate_fn=cdp)

    # Invariants: 0 DOM clicks, real_hh_submit == 0, claim remains ATTEMPTING
    assert cdp.submit_click_count == 0
    assert res.real_hh_submit == 0
    assert "claim" in res.reason.lower() or "not eligible" in res.reason.lower()

    claim_after = db.get_submission_claim(sid)
    assert claim_after is not None
    assert claim_after["status"] == "ATTEMPTING"
    assert claim_after["worker_id"] == "worker_process_1"


# ==============================================================================
# Scenario 2: Crash after submit before persistence
# ==============================================================================
def test_crash_after_submit_before_persistence():
    """Submit executed, process crashes before persistence -> second run blocked by claim, total submits == 1."""
    sid, app_id = _setup_vacancy_and_app("1234002")

    # Step 1: Worker 1 acquires claim and executes submit click
    acquired, _reason, _claim_info = db.acquire_submission_claim(
        vacancy_stable_id=sid,
        application_id=app_id,
        worker_id="worker_process_1",
    )
    assert acquired is True

    cdp1 = MockCdpEvaluator("1234002")
    # Worker 1 sends submit click
    click_res = cdp1("submitBtn.click()")
    assert json.loads(click_res)["ok"] is True
    assert cdp1.submit_click_count == 1

    # Worker 1 crashes before updating application_submissions or hh_applications state to SUBMITTED.
    # Claim remains in ATTEMPTING in DB.
    assert db.get_hh_application(app_id)["state"] == "READY_TO_SUBMIT"
    assert db.get_submission_claim(sid)["status"] == "ATTEMPTING"

    # Step 2: Worker 2 restarts and attempts submission on the same vacancy
    cdp2 = MockCdpEvaluator("1234002")
    res2 = run_application(app_id, auto=True, evaluate_fn=cdp2)

    # Invariants: Second worker executes 0 clicks; total submits across test remains 1
    assert cdp2.submit_click_count == 0
    assert res2.real_hh_submit == 0
    assert cdp1.submit_click_count == 1
    assert "claim" in res2.reason.lower() or "not eligible" in res2.reason.lower()


# ==============================================================================
# Scenario 3: Ambiguous timeout on click
# ==============================================================================
def test_ambiguous_timeout_on_click():
    """Evaluate/click throws timeout/disconnect -> transitions to AMBIGUOUS, claim in AMBIGUOUS, 0 auto-retries."""
    sid, app_id = _setup_vacancy_and_app("1234003")

    cdp = MockCdpEvaluator("1234003")

    def timing_out_eval(script: str) -> str:
        if "submitBtn.click()" in script or ".click()" in script:
            cdp.submit_click_count += 1
            raise TimeoutError("CDP connection timed out waiting for click ack")
        return cdp(script)

    # Execute submission directly
    res = execute_hh_submission(
        vacancy_stable_id=sid,
        evaluate_fn=timing_out_eval,
        candidate_profile=_make_candidate_profile(),
        human_confirmed=True,
    )

    # Invariants on timeout:
    # 1. Result is not ok, status is AMBIGUOUS
    assert res.ok is False
    assert res.status == "AMBIGUOUS"
    assert "timed out" in res.reason.lower() or "timeout" in res.reason.lower() or "ambiguous" in res.reason.lower()
    assert cdp.submit_click_count == 1

    # 2. Claim in DB is marked AMBIGUOUS
    claim = db.get_submission_claim(sid)
    assert claim is not None
    assert claim["status"] == "AMBIGUOUS"

    # 3. hh_applications is transitioned to AMBIGUOUS
    app = db.get_hh_application(app_id)
    assert app is not None
    assert app["state"] == "AMBIGUOUS"

    # 4. Attempting automatic re-run MUST be rejected (zero blind retries)
    cdp_retry = MockCdpEvaluator("1234003")
    retry_res = run_application(app_id, auto=True, evaluate_fn=cdp_retry)
    assert retry_res.real_hh_submit == 0
    assert cdp_retry.submit_click_count == 0
    assert "not eligible" in retry_res.reason.lower() or "ambiguous" in retry_res.reason.lower()


# ==============================================================================
# Scenario 4: Duplicate autonomous run on already submitted vacancy
# ==============================================================================
def test_duplicate_autonomous_run_on_already_submitted_vacancy():
    """Re-running runner on already submitted vacancy does not submit; external calls == 0."""
    sid, app_id = _setup_vacancy_and_app("1234004")

    # First run succeeds
    cdp1 = MockCdpEvaluator("1234004")
    res1 = run_application(app_id, auto=True, evaluate_fn=cdp1)
    assert res1.real_hh_submit == 1
    assert res1.final_application_state == "SUBMITTED"
    assert cdp1.submit_click_count == 1

    # Verify DB state: claim, hh_applications, application_submissions are all SUBMITTED
    assert db.get_submission_claim(sid)["status"] == "SUBMITTED"
    assert db.get_hh_application(app_id)["state"] == "SUBMITTED"
    assert db.get_submission(sid)[4] == "SUBMITTED"

    # Second autonomous run on the same application
    cdp2 = MockCdpEvaluator("1234004")
    res2 = run_application(app_id, auto=True, evaluate_fn=cdp2)

    # Invariants: 0 clicks, real_hh_submit == 0, state remains SUBMITTED
    assert cdp2.submit_click_count == 0
    assert res2.real_hh_submit == 0
    assert res2.final_application_state == "SUBMITTED"
    assert "not eligible" in res2.reason.lower()


# ==============================================================================
# Scenario 5: Concurrent claim
# ==============================================================================
def test_concurrent_claim():
    """Two concurrent workers race to submit the same vacancy -> exactly one succeeds, exactly 1 submit."""
    sid, _app_id = _setup_vacancy_and_app("1234005")

    cdp_a = MockCdpEvaluator("1234005")
    cdp_b = MockCdpEvaluator("1234005")

    results: list[tuple[str, Any]] = []
    lock = threading.Lock()

    def run_worker(worker_id: str, cdp: MockCdpEvaluator):
        res = execute_hh_submission(
            vacancy_stable_id=sid,
            evaluate_fn=cdp,
            candidate_profile=_make_candidate_profile(),
            human_confirmed=True,
        )
        with lock:
            results.append((worker_id, res))

    t1 = threading.Thread(target=run_worker, args=("worker_alpha", cdp_a))
    t2 = threading.Thread(target=run_worker, args=("worker_beta", cdp_b))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Invariants: Exactly one succeeded, exactly one was blocked
    assert len(results) == 2
    successes = [r for r in results if r[1].ok is True]
    failures = [r for r in results if r[1].ok is False]

    assert len(successes) == 1
    assert len(failures) == 1

    # External DOM submit click was executed exactly ONCE across both workers
    total_clicks = cdp_a.submit_click_count + cdp_b.submit_click_count
    assert total_clicks == 1
    assert successes[0][1].submit_count == 1
    assert failures[0][1].submit_count == 0

    # Claim is in SUBMITTED status
    claim = db.get_submission_claim(sid)
    assert claim is not None
    assert claim["status"] == "SUBMITTED"


# ==============================================================================
# Scenario 6: Already submitted application in DB
# ==============================================================================
def test_already_submitted_application_in_db():
    """Attempting submit on vacancy already marked SUBMITTED in DB rejects before external call."""
    sid, app_id = _setup_vacancy_and_app("1234006", state="SUBMITTED")

    # Save completed submission in application_submissions
    db.save_submission(
        sid,
        json.dumps({"status": "SUBMITTED"}),
        status="SUBMITTED",
        submission_id="sub_pre_existing",
    )
    # Save completed claim
    db.acquire_submission_claim(sid, app_id, "worker_old")
    db.update_submission_claim(sid, status="SUBMITTED", details={"verified": True})

    cdp = MockCdpEvaluator("1234006")

    # 1. Check can_submit gate
    elig = can_submit(app_id)
    assert elig.allowed is False
    assert "submitted" in elig.reason.lower()

    # 2. Check run_application gate
    run_res = run_application(app_id, auto=True, evaluate_fn=cdp)
    assert run_res.real_hh_submit == 0
    assert cdp.submit_click_count == 0

    # 3. Check execute_hh_submission gate
    exec_res = execute_hh_submission(
        vacancy_stable_id=sid,
        evaluate_fn=cdp,
        candidate_profile=_make_candidate_profile(),
        human_confirmed=True,
    )
    assert exec_res.ok is False
    assert exec_res.status == "BLOCKED"
    assert cdp.submit_click_count == 0


# ==============================================================================
# Scenario 7: Kill switch race
# ==============================================================================
def test_kill_switch_race(monkeypatch):
    """Kill switch activated after gate check but before submit click -> execution aborted, 0 clicks."""
    sid, app_id = _setup_vacancy_and_app("1234007")

    cdp = MockCdpEvaluator("1234007")

    orig_acquire = db.acquire_submission_claim

    def racing_acquire(*args, **kwargs):
        # Claim is acquired in ATTEMPTING status
        res = orig_acquire(*args, **kwargs)
        # Kill switch is engaged immediately after claim acquisition, right before click!
        db.set_submit_paused(True)
        return res

    monkeypatch.setattr(db, "acquire_submission_claim", racing_acquire)

    res = execute_hh_submission(
        vacancy_stable_id=sid,
        evaluate_fn=cdp,
        candidate_profile=_make_candidate_profile(),
        human_confirmed=True,
    )

    # Invariants:
    # 1. Submission blocked, 0 DOM clicks executed
    assert res.ok is False
    assert res.status == "BLOCKED"
    assert "kill switch" in res.reason.lower()
    assert cdp.submit_click_count == 0

    # 2. Claim status updated to FAILED_SAFE
    claim = db.get_submission_claim(sid)
    assert claim is not None
    assert claim["status"] == "FAILED_SAFE"
    assert "kill_switch" in claim["details"].get("reason", "")

    # 3. Application remains in unsubmitted state (recorded as BLOCKED, not SUBMITTED)
    sub = db.get_submission(sid)
    assert sub is not None
    assert sub[4] == "BLOCKED"
    app = db.get_hh_application(app_id)
    assert app["state"] != "SUBMITTED"


# ==============================================================================
# Scenario 8: Telegram notification failure after submit
# ==============================================================================
def test_telegram_notification_failure_after_submit(monkeypatch):
    """Submit succeeded and verified, but Telegram alert raises exception -> submission does NOT rollback."""
    sid, app_id = _setup_vacancy_and_app("1234008")

    cdp = MockCdpEvaluator("1234008")

    def failing_tg_notify(*args, **kwargs):
        raise RuntimeError("Telegram API Network Failure: 504 Gateway Timeout")

    import ai_assistant.telegram_notifier as tgn
    monkeypatch.setattr(tgn, "send_post_submit_notification", failing_tg_notify)

    res = run_application(app_id, auto=True, evaluate_fn=cdp)

    # Invariants:
    # 1. Submission is successful and verified despite Telegram failure
    assert res.real_hh_submit == 1
    assert res.final_application_state == "SUBMITTED"
    assert cdp.submit_click_count == 1

    # 2. DB states are intact and SUBMITTED
    assert db.get_submission_claim(sid)["status"] == "SUBMITTED"
    assert db.get_hh_application(app_id)["state"] == "SUBMITTED"
    assert db.get_submission(sid)[4] == "SUBMITTED"
    assert get_application_status(sid).status == ApplicationStatus.SUBMITTED


# ==============================================================================
# Scenario 9: Policy re-approval cannot resubmit
# ==============================================================================
def test_policy_reapproval_cannot_resubmit():
    """Vacancy already claimed/submitted -> policy approve decision cannot bypass executor claim check."""
    sid, app_id = _setup_vacancy_and_app("1234009")

    # Mark claim as SUBMITTED in DB
    db.acquire_submission_claim(sid, app_id, "initial_worker")
    db.update_submission_claim(sid, status="SUBMITTED", details={"verified": True})
    db.save_submission(
        sid,
        json.dumps({"status": "SUBMITTED"}),
        status="SUBMITTED",
        submission_id="sub_1234009",
    )

    # Call policy evaluate directly on the application
    app_data = db.get_hh_application(app_id)
    decision = evaluate_policy(app_data)
    # The letter & package are valid, so policy alone returns approve
    assert decision.approve is True
    assert decision.approval is not None

    # Now pass this approval to execute_hh_submission
    cdp = MockCdpEvaluator("1234009")
    exec_res = execute_hh_submission(
        vacancy_stable_id=sid,
        evaluate_fn=cdp,
        approval=decision.approval,
        candidate_profile=_make_candidate_profile(),
        human_confirmed=False,
    )

    # Invariants:
    # Executor blocks at Gate 1 / claim check before any DOM click
    assert exec_res.ok is False
    assert exec_res.status == "BLOCKED"
    assert cdp.submit_click_count == 0


# ==============================================================================
# Scenario 10: Recovery / second runner does not blindly retry an AMBIGUOUS attempt
# ==============================================================================
def test_recovery_runner_does_not_blindly_retry_ambiguous():
    """AMBIGUOUS application is not auto-retried by runner; can only be resolved by human reconciliation."""
    sid, app_id = _setup_vacancy_and_app("1234010", state="AMBIGUOUS")

    # Setup claim as AMBIGUOUS
    db.acquire_submission_claim(sid, app_id, "worker_timeout")
    db.update_submission_claim(sid, status="AMBIGUOUS", details={"error": "cdp_timeout_ambiguous"})

    cdp = MockCdpEvaluator("1234010")

    # 1. run_next_application skips AMBIGUOUS applications
    next_res = run_next_application(auto=True, evaluate_fn=cdp)
    assert next_res.selected_application is None
    assert cdp.submit_click_count == 0

    # 2. direct run_application is blocked
    run_res = run_application(app_id, auto=True, evaluate_fn=cdp)
    assert run_res.real_hh_submit == 0
    assert cdp.submit_click_count == 0
    assert "not eligible" in run_res.reason.lower()

    # 3. Policy approval cannot transition AMBIGUOUS -> SUBMITTED (orchestrator invariant)
    policy_approval = SubmitApproval(source="policy", policy_version="v2.0_autonomous")
    res_trans = transition_application(
        application_id=app_id,
        to_state=HHApplicationState.SUBMITTED,
        reason="policy_autonomous_reconciliation_attempt",
        approval=policy_approval,
        evidence={"fingerprint": "mock_fp"},
    )
    assert res_trans.ok is False
    assert res_trans.error == "AMBIGUOUS_RECONCILIATION_REQUIRES_HUMAN"

    # 4. Human admin performs manual reconciliation
    reconcile_ok = db.reconcile_submission_claim(
        vacancy_stable_id=sid,
        new_status="SUBMITTED",
        reason="Human operator confirmed application exists in hh.ru/applicant/negotiations",
        actor="misha_admin",
    )
    assert reconcile_ok is True

    # 5. Transition with human approval succeeds
    human_approval = SubmitApproval(source="human", policy_version="manual_admin_reconciliation")
    trans_res = transition_application(
        application_id=app_id,
        to_state=HHApplicationState.SUBMITTED,
        reason="human_manual_reconciliation_verified",
        approval=human_approval,
        evidence={
            "fingerprint": "mock_fp",
            "post_submit_verification": "human_verified",
            "actor": "misha_admin",
        },
    )
    assert trans_res.ok is True

    # Check DB state
    app_after = db.get_hh_application(app_id)
    assert app_after["state"] == "SUBMITTED"

    claim_after = db.get_submission_claim(sid)
    assert claim_after["status"] == "SUBMITTED"
    assert len(claim_after["details"].get("reconciliation_history", [])) == 1
    rec_entry = claim_after["details"]["reconciliation_history"][0]
    assert rec_entry["reconciled_by"] == "misha_admin"
    assert rec_entry["from_status"] == "AMBIGUOUS"
    assert rec_entry["to_status"] == "SUBMITTED"
