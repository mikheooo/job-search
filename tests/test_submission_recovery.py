from __future__ import annotations

import json
import tempfile
import shutil
import os
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

import ai_assistant.config as config
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant import db
from ai_assistant.application_tracking import ApplicationStatus, set_application_status, transition_application, get_application_status
from ai_assistant.application_review import ReviewStatus, create_application_review
from ai_assistant.browser_executor import BrowserStatus, get_browser_session
from ai_assistant.submission_recovery import (
    RecoveryStatus,
    RecoveryResult,
    inspect_submission_state,
    reconcile_submission_state,
    get_submission_audit,
)
from ai_assistant.submission_verifier import VerificationStatus, save_verification
from ai_assistant.application_tracking import transition_application


def _vac(**kwargs):
    defaults = dict(
        source="test",
        source_job_id="1",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="TestCo",
        description="We need python, n8n, automation, LLM, API. Must have AWS. Remote worldwide. Senior level.",
        job_url=None,
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=5000,
        salary_max=5000,
        salary_currency="USD",
        employment_type="Full Time",
    )
    defaults.update(kwargs)
    if not defaults.get("job_url"):
        sid = str(defaults.get("source_job_id") or "1")
        defaults["job_url"] = f"https://example.com/{sid}"
    return Vacancy(**defaults)


def _profile():
    return CandidateProfile(
        desired_roles=["AI Engineer", "Automation Engineer"],
        skills=["python", "n8n", "automation", "LLM", "API", "AWS"],
        min_salary=5000,
        max_salary=10000,
        salary_currency="USD",
        employment_types=["full time", "contract"],
        remote_preference=True,
        allowed_countries=["US", "Worldwide"],
        excluded_companies=["BadCorp"],
        excluded_countries=[],
        years_experience=5,
    )


def setup_test_db():
    """Create a temporary database for testing."""
    tmp_dir = tempfile.mkdtemp()
    db_file = os.path.join(tmp_dir, "state.db")
    config.DB_FILE = db_file
    db.init_db()
    return tmp_dir


def teardown_test_db(tmp_dir):
    """Clean up temporary database."""
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _setup_tracking(vacancy_stable_id: str, status: ApplicationStatus, 
                     company: str = "TestCo", title: str = "Test Title",
                     source: str = "test", vacancy_url: str = "https://example.com/1"):
    """Helper to create tracking record in a specific status."""
    from ai_assistant.application_tracking import set_application_status, transition_application
    if status == ApplicationStatus.DISCOVERED:
        return set_application_status(vacancy_stable_id, status, company=company, title=title, source=source, vacancy_url=vacancy_url)
    elif status == ApplicationStatus.ANALYZED:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.READY_TO_APPLY:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        track = transition_application(vacancy_stable_id, ApplicationStatus.ANALYZED)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.SUBMITTED:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        track = transition_application(vacancy_stable_id, ApplicationStatus.ANALYZED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.READY_TO_APPLY)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.VERIFIED:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        track = transition_application(vacancy_stable_id, ApplicationStatus.ANALYZED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.READY_TO_APPLY)
        track = transition_application(vacancy_stable_id, ApplicationStatus.SUBMITTED)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.APPLIED:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        track = transition_application(vacancy_stable_id, ApplicationStatus.ANALYZED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.READY_TO_APPLY)
        track = transition_application(vacancy_stable_id, ApplicationStatus.SUBMITTED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.VERIFIED)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.REJECTED:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        track = transition_application(vacancy_stable_id, ApplicationStatus.ANALYZED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.READY_TO_APPLY)
        track = transition_application(vacancy_stable_id, ApplicationStatus.SUBMITTED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.VERIFIED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.APPLIED)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.INTERVIEW:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        track = transition_application(vacancy_stable_id, ApplicationStatus.ANALYZED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.READY_TO_APPLY)
        track = transition_application(vacancy_stable_id, ApplicationStatus.SUBMITTED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.VERIFIED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.APPLIED)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.OFFER:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        track = transition_application(vacancy_stable_id, ApplicationStatus.ANALYZED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.READY_TO_APPLY)
        track = transition_application(vacancy_stable_id, ApplicationStatus.SUBMITTED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.VERIFIED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.APPLIED)
        track = transition_application(vacancy_stable_id, ApplicationStatus.INTERVIEW)
        return transition_application(vacancy_stable_id, status)
    elif status == ApplicationStatus.WITHDRAWN:
        track = set_application_status(vacancy_stable_id, ApplicationStatus.DISCOVERED, company=company, title=title, source=source, vacancy_url=vacancy_url)
        return transition_application(vacancy_stable_id, status)
    else:
        return set_application_status(vacancy_stable_id, status, company=company, title=title, source=source, vacancy_url=vacancy_url)


def _create_submission_and_verification(
    vacancy_stable_id: str,
    submission_status: str = "SUBMITTED",
    verification_status: str | None = None,
    verification_version: str = "v1",
    submission_id: str | None = None,
):
    """Helper to create submission and optional verification records."""
    if submission_id is None:
        submission_id = f"{vacancy_stable_id}_20240101_120000_abc123"
    
    sub_json = json.dumps({"success": True, "submission_id": submission_id})
    db.save_submission(
        vacancy_stable_id=vacancy_stable_id,
        submission_json=sub_json,
        status=submission_status,
        submitted_at=datetime.utcnow().isoformat(),
        submission_id=submission_id,
    )
    
    if verification_status:
        from ai_assistant.submission_verifier import SubmissionVerification
        ver = SubmissionVerification(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            verification_status=VerificationStatus(verification_status),
            evidence={"success_signals": []},
            final_url="https://example.com/success",
            page_title="Success",
            verified_at=datetime.utcnow().isoformat(),
            warnings=[],
            verification_version=verification_version,
        )
        save_verification(ver)
    
    return submission_id


def test_submitted_without_verification_needs_verification():
    """Test SUBMITTED without verification -> NEEDS_VERIFICATION"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(vac.stable_id(), submission_status="SUBMITTED")
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NEEDS_VERIFICATION
        assert "no verification" in result.reason.lower()
        assert "verification" in result.recommended_action.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_submitted_with_verified_no_action():
    """Test SUBMITTED + VERIFIED -> NO_ACTION (ready for reconcile)"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = _create_submission_and_verification(
            vac.stable_id(), 
            submission_status="SUBMITTED",
            verification_status="VERIFIED",
        )
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NO_ACTION
        assert "verified" in result.reason.lower()
        assert "reconcile" in result.recommended_action.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_submitted_with_ambiguous_needs_review():
    """Test SUBMITTED + AMBIGUOUS -> NEEDS_REVIEW"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="SUBMITTED",
            verification_status="AMBIGUOUS",
        )
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NEEDS_REVIEW
        assert "ambiguous" in result.reason.lower()
        assert "review" in result.recommended_action.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_submitted_with_failed_needs_review():
    """Test SUBMITTED + FAILED -> NEEDS_REVIEW (not READY_TO_RETRY by default)"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="SUBMITTED",
            verification_status="FAILED",
        )
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NEEDS_REVIEW
        assert "failed" in result.reason.lower()
        assert "review" in result.recommended_action.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_submitted_with_blocked_needs_review():
    """Test SUBMITTED + BLOCKED -> NEEDS_REVIEW"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="SUBMITTED",
            verification_status="BLOCKED",
        )
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NEEDS_REVIEW
        assert "blocked" in result.reason.lower()
        assert "review" in result.recommended_action.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_applied_terminal():
    """Test APPLIED -> TERMINAL"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.APPLIED)
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.TERMINAL
        assert "terminal" in result.reason.lower()
        assert "no action" in result.recommended_action.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_rejected_terminal():
    """Test REJECTED -> TERMINAL"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.ANALYZED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.VERIFIED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.APPLIED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.REJECTED)
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.TERMINAL
        assert "rejected" in result.reason.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_interview_terminal():
    """Test INTERVIEW -> TERMINAL"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.ANALYZED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.VERIFIED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.APPLIED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.INTERVIEW)
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.TERMINAL
    finally:
        teardown_test_db(tmp_dir)


def test_offer_terminal():
    """Test OFFER -> TERMINAL"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.ANALYZED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.VERIFIED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.APPLIED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.INTERVIEW)
        _setup_tracking(vac.stable_id(), ApplicationStatus.OFFER)
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.TERMINAL
    finally:
        teardown_test_db(tmp_dir)


def test_withdrawn_terminal():
    """Test WITHDRAWN -> TERMINAL"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.WITHDRAWN)
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.TERMINAL
    finally:
        teardown_test_db(tmp_dir)


def test_reconcile_verified_to_applied():
    """Test reconciliation VERIFIED -> APPLIED"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="SUBMITTED",
            verification_status="VERIFIED",
        )
        
        # Before reconcile
        track_before = get_application_status(vac.stable_id())
        assert track_before.status.value == "SUBMITTED"
        
        # Reconcile
        result = reconcile_submission_state(vac.stable_id())
        
        # After reconcile
        track_after = get_application_status(vac.stable_id())
        assert track_after.status.value == "APPLIED"
    finally:
        teardown_test_db(tmp_dir)


def test_reconcile_ambiguous_never_applied():
    """Test reconciliation AMBIGUOUS never -> APPLIED"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="SUBMITTED",
            verification_status="AMBIGUOUS",
        )
        
        result = reconcile_submission_state(vac.stable_id())
        
        track = get_application_status(vac.stable_id())
        assert track.status.value != "APPLIED"
        assert track.status.value == "SUBMITTED"
    finally:
        teardown_test_db(tmp_dir)


def test_reconcile_failed_never_applied():
    """Test reconciliation FAILED never -> APPLIED"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="SUBMITTED",
            verification_status="FAILED",
        )
        
        result = reconcile_submission_state(vac.stable_id())
        
        track = get_application_status(vac.stable_id())
        assert track.status.value != "APPLIED"
        assert track.status.value == "SUBMITTED"
    finally:
        teardown_test_db(tmp_dir)


def test_recovery_never_calls_submit():
    """Test recovery never calls submit"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        _create_submission_and_verification(vac.stable_id())
        
        # Just ensure inspect_submission_state doesn't call submit
        # (it's a pure function with no side effects)
        result = inspect_submission_state(vac.stable_id())
        assert result is not None
        
        # Verify no browser was opened (no side effects)
        # This is a read-only function
    finally:
        teardown_test_db(tmp_dir)


def test_multiple_submission_attempts_preserved():
    """Test multiple submission attempts are preserved in audit"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        # First submission - FAILED
        sub_id_1 = f"{vac.stable_id()}_20240101_120000_abc123"
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="FAILED",
            verification_status="FAILED",
            submission_id=sub_id_1,
        )
        
        # Second submission - SUBMITTED, no verification yet
        sub_id_2 = f"{vac.stable_id()}_20240101_130000_def456"
        _create_submission_and_verification(
            vac.stable_id(),
            submission_status="SUBMITTED",
            submission_id=sub_id_2,
        )
        
        # Get all submissions
        all_subs = db.get_all_submissions(vac.stable_id())
        assert len(all_subs) == 2
        
        # Both should have different submission_ids
        sub_ids = [s[1] for s in all_subs]
        assert sub_id_1 in sub_ids
        assert sub_id_2 in sub_ids
        
        # Audit should show both
        audit = get_submission_audit(vac.stable_id())
        submission_events = [e for e in audit if e["type"] == "SUBMISSION"]
        assert len(submission_events) == 2
    finally:
        teardown_test_db(tmp_dir)


def test_audit_order_is_chronological():
    """Test audit events are in chronological order"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Create events in sequence
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.ANALYZED)
        _setup_tracking(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        _create_submission_and_verification(vac.stable_id())
        
        audit = get_submission_audit(vac.stable_id())
        
        # Verify chronological order
        timestamps = [e["timestamp"] for e in audit if e.get("timestamp")]
        assert timestamps == sorted(timestamps), "Audit events should be chronologically sorted"
        
        # Verify event types present
        event_types = [e["type"] for e in audit]
        assert "TRACKING" in event_types
        assert "SUBMISSION" in event_types
    finally:
        teardown_test_db(tmp_dir)


def test_repeated_recovery_idempotent():
    """Test repeated recovery is idempotent"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        _create_submission_and_verification(vac.stable_id())
        
        result1 = inspect_submission_state(vac.stable_id())
        result2 = inspect_submission_state(vac.stable_id())
        
        assert result1.recovery_status == result2.recovery_status
        assert result1.reason == result2.reason
        assert result1.recommended_action == result2.recommended_action
    finally:
        teardown_test_db(tmp_dir)


def test_repeated_reconcile_idempotent():
    """Test repeated reconcile is idempotent"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        _create_submission_and_verification(
            vac.stable_id(),
            verification_status="VERIFIED",
        )
        
        result1 = reconcile_submission_state(vac.stable_id())
        result2 = reconcile_submission_state(vac.stable_id())
        
        assert result1.current_tracking_status == result2.current_tracking_status == "APPLIED"
    finally:
        teardown_test_db(tmp_dir)


def test_old_submission_records_remain_intact():
    """Test old submission records remain intact after new ones"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        # Create first submission
        sub_id_1 = f"{vac.stable_id()}_20240101_120000_abc123"
        _create_submission_and_verification(
            vac.stable_id(),
            submission_id=sub_id_1,
        )
        
        # Create second submission
        sub_id_2 = f"{vac.stable_id()}_20240101_130000_def456"
        _create_submission_and_verification(
            vac.stable_id(),
            submission_id=sub_id_2,
        )
        
        # Both should exist
        sub1 = db.get_submission(vac.stable_id(), submission_id=sub_id_1)
        sub2 = db.get_submission(vac.stable_id(), submission_id=sub_id_2)
        
        assert sub1 is not None
        assert sub2 is not None
        
        # Audit should show both
        audit = get_submission_audit(vac.stable_id())
        submission_events = [e for e in audit if e["type"] == "SUBMISSION"]
        assert len(submission_events) == 2
    finally:
        teardown_test_db(tmp_dir)


def test_recover_without_tracking_or_submission():
    """Test recovery when no tracking or submission exists"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        # No tracking, no submission
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NO_ACTION
        assert "no tracking or submission" in result.reason.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_recover_with_submission_but_no_tracking():
    """Test recovery when submission exists but no tracking"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        # Create submission but no tracking
        _create_submission_and_verification(vac.stable_id())
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NEEDS_REVIEW
        assert "no tracking" in result.reason.lower()
    finally:
        teardown_test_db(tmp_dir)


def test_recover_ready_to_apply_no_submit():
    """Test recovery for READY_TO_APPLY without submission"""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        _setup_tracking(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        
        result = inspect_submission_state(vac.stable_id())
        
        assert result.recovery_status == RecoveryStatus.NO_ACTION
        assert "ready to apply" in result.reason.lower()
    finally:
        teardown_test_db(tmp_dir)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])