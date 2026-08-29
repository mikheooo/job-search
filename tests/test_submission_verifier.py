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
from ai_assistant.application_tracking import ApplicationStatus, set_application_status, transition_application, get_application_status, verify_and_apply
from ai_assistant.submission_verifier import (
    VerificationStatus,
    SubmissionVerification,
    verify_submission,
    save_verification,
    get_verification,
    list_verifications,
    is_verified,
    VERIFICATION_VERSION,
    _detect_signals,
)
from ai_assistant.browser_executor import (
    SubmitResult,
    MockBrowserAdapter,
    submit_application_in_browser,
    verify_submission_in_browser,
)


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


def test_detect_signals_success():
    """Test detection of success signals."""
    content = "Thank you for applying. Your application has been received."
    title = "Application Submitted"
    url = "https://example.com/apply"
    
    success, error, blocked = _detect_signals(content, title, url)
    
    assert len(success) > 0
    assert any("thank you for applying" in s for s in success)
    assert len(error) == 0
    assert len(blocked) == 0


def test_detect_signals_error():
    """Test detection of error signals."""
    content = "Something went wrong. Failed to apply."
    title = "Error"
    url = "https://example.com/apply"
    
    success, error, blocked = _detect_signals(content, title, url)
    
    assert len(success) == 0
    assert len(error) > 0
    assert any("failed to apply" in e for e in error)
    assert len(blocked) == 0


def test_detect_signals_blocked():
    """Test detection of blocked signals (CAPTCHA, login, etc.)."""
    content = "Please complete the captcha to continue."
    title = "Captcha Required"
    url = "https://example.com/apply"
    
    success, error, blocked = _detect_signals(content, title, url)
    
    assert len(success) == 0
    assert len(error) == 0
    assert len(blocked) > 0
    assert any("captcha" in b for b in blocked)


def test_detect_signals_ambiguous():
    """Test detection when no clear signals found."""
    content = "Welcome to the application page. Fill out the form below."
    title = "Apply Now"
    url = "https://example.com/apply"
    
    success, error, blocked = _detect_signals(content, title, url)
    
    assert len(success) == 0
    assert len(error) == 0
    assert len(blocked) == 0


def test_submission_verification_model():
    """Test SubmissionVerification model creation."""
    ver = SubmissionVerification(
        vacancy_stable_id="test:v1",
        submission_id="test:v1_20240101_120000_abc123",
        verification_status=VerificationStatus.VERIFIED,
        evidence={"success_signals": ["thank you for applying"]},
        final_url="https://example.com/success",
        page_title="Application Submitted",
        success_signal="thank you for applying",
        screenshot_path="artifacts/verification/test.png",
        verified_at=datetime.utcnow().isoformat(),
        warnings=[],
    )
    
    assert ver.vacancy_stable_id == "test:v1"
    assert ver.verification_status == VerificationStatus.VERIFIED
    assert ver.success_signal == "thank you for applying"


def test_verification_persistence():
    """Test saving and loading verification."""
    tmp_dir = setup_test_db()
    try:
        ver = SubmissionVerification(
            vacancy_stable_id="test:v1",
            submission_id="sub_123",
            verification_status=VerificationStatus.VERIFIED,
            evidence={"success_signals": ["application received"]},
            final_url="https://example.com/success",
            page_title="Success",
            success_signal="application received",
            screenshot_path="artifacts/verification/test.png",
            verified_at=datetime.utcnow().isoformat(),
            warnings=["Success confirmed"],
        )
        
        save_verification(ver)
        
        # Load it back
        loaded = get_verification("test:v1", "sub_123")
        assert loaded is not None
        assert loaded.verification_status == VerificationStatus.VERIFIED
        assert loaded.success_signal == "application received"
        assert loaded.evidence["success_signals"] == ["application received"]
        assert "Success confirmed" in loaded.warnings
        
        # Test is_verified
        assert is_verified("test:v1", "sub_123") is True
        
        # Test list
        all_ver = list_verifications()
        assert len(all_ver) == 1
        assert all_ver[0].vacancy_stable_id == "test:v1"
    finally:
        teardown_test_db(tmp_dir)


def test_verification_version_invalidation():
    """Test that verification version invalidates old results."""
    tmp_dir = setup_test_db()
    try:
        ver_v1 = SubmissionVerification(
            vacancy_stable_id="test:v1",
            submission_id="sub_123",
            verification_status=VerificationStatus.VERIFIED,
            evidence={},
            final_url="https://example.com/success",
            page_title="Success",
            verified_at=datetime.utcnow().isoformat(),
            warnings=[],
            verification_version="v1",
        )
        save_verification(ver_v1)
        
        # Same submission_id, different version
        ver_v2 = SubmissionVerification(
            vacancy_stable_id="test:v1",
            submission_id="sub_123",
            verification_status=VerificationStatus.AMBIGUOUS,
            evidence={},
            final_url="https://example.com/success",
            page_title="Success",
            verified_at=datetime.utcnow().isoformat(),
            warnings=[],
            verification_version="v2",
        )
        save_verification(ver_v2)
        
        # Both should exist (different PK)
        loaded_v1 = get_verification("test:v1", "sub_123", "v1")
        loaded_v2 = get_verification("test:v1", "sub_123", "v2")
        assert loaded_v1 is not None
        assert loaded_v2 is not None
        assert loaded_v1.verification_version == "v1"
        assert loaded_v2.verification_version == "v2"
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_success_signal():
    """Test verify_submission with success signal returns VERIFIED."""
    tmp_dir = setup_test_db()
    try:
        # Create vacancy and submission
        vac = _vac()
        db.save_vacancy(vac)
        
        # Set tracking to SUBMITTED (creates record)
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        # Create submission record
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Mock adapter that returns success page
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Application Submitted - Thank you for applying",
            "final_url": "https://example.com/success",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert ver.verification_status == VerificationStatus.VERIFIED
        assert "thank you for applying" in ver.evidence.get("success_signals", [])
        assert ver.final_url == "https://example.com/success"
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_no_success_signal():
    """Test verify_submission with no success signal returns AMBIGUOUS."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Create tracking record
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Mock adapter with ambiguous page (no success/error/blocked)
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Application Form",
            "final_url": "https://example.com/apply",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert ver.verification_status == VerificationStatus.AMBIGUOUS
        assert ver.success_signal is None
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_error_signal():
    """Test verify_submission with error signal returns FAILED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Mock adapter with error page
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Error - Failed to apply",
            "final_url": "https://example.com/error",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert ver.verification_status == VerificationStatus.FAILED
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_blocked_captcha():
    """Test verify_submission with CAPTCHA returns BLOCKED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Mock adapter with CAPTCHA page
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Please complete the captcha",
            "final_url": "https://example.com/captcha",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert ver.verification_status == VerificationStatus.BLOCKED
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_blocked_login():
    """Test verify_submission with login required returns BLOCKED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Mock adapter with login page
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Sign in to apply",
            "final_url": "https://example.com/login",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert ver.verification_status == VerificationStatus.BLOCKED
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_url_evidence():
    """Test verification captures URL evidence."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Application Submitted",
            "final_url": "https://example.com/success?ref=123",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert ver.final_url == "https://example.com/success?ref=123"
        assert ver.evidence.get("url") == "https://example.com/success?ref=123"
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_title_evidence():
    """Test verification captures page title evidence."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Thank you for your application - TestCo",
            "final_url": "https://example.com/success",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert "Thank you for your application" in ver.page_title
        assert ver.evidence.get("title") == "Thank you for your application - TestCo"
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_screenshot_persistence():
    """Test verification saves screenshot."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Application Submitted",
            "final_url": "https://example.com/success",
        })
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        assert ver.screenshot_path is not None
        assert "verification" in ver.screenshot_path
        assert vac.stable_id().replace(":", "_") in ver.screenshot_path
    finally:
        teardown_test_db(tmp_dir)


def test_submitted_not_equal_applied():
    """Test SUBMITTED status does not equal APPLIED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Set to SUBMITTED
        track = set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        assert track.status == ApplicationStatus.SUBMITTED
        
        # Verify it's not APPLIED
        track = get_application_status(vac.stable_id())
        assert track.status != ApplicationStatus.APPLIED
        assert track.status == ApplicationStatus.SUBMITTED
    finally:
        teardown_test_db(tmp_dir)


def test_verified_transitions_to_applied():
    """Test VERIFIED verification transitions tracking to APPLIED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Create tracking record then transition to SUBMITTED
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        track = get_application_status(vac.stable_id())
        assert track.status == ApplicationStatus.SUBMITTED
        
        # Verify with VERIFIED status
        result = verify_and_apply(vac.stable_id(), "VERIFIED", note="Test verification")
        
        assert result.status == ApplicationStatus.APPLIED
    finally:
        teardown_test_db(tmp_dir)


def test_ambiguous_not_applied():
    """Test AMBIGUOUS verification does NOT transition to APPLIED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Create tracking record then transition to SUBMITTED
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        # Verify with AMBIGUOUS status
        result = verify_and_apply(vac.stable_id(), "AMBIGUOUS", note="No clear signal")
        
        # Should go back to READY_TO_APPLY, not APPLIED
        assert result.status == ApplicationStatus.READY_TO_APPLY
    finally:
        teardown_test_db(tmp_dir)


def test_failed_not_applied():
    """Test FAILED verification does NOT transition to APPLIED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Create tracking record then transition to SUBMITTED
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        # Verify with FAILED status
        result = verify_and_apply(vac.stable_id(), "FAILED", note="Submission error")
        
        # Should go back to READY_TO_APPLY, not APPLIED
        assert result.status == ApplicationStatus.READY_TO_APPLY
    finally:
        teardown_test_db(tmp_dir)


def test_blocked_not_applied():
    """Test BLOCKED verification does NOT transition to APPLIED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Create tracking record then transition to SUBMITTED
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        # Verify with BLOCKED status
        result = verify_and_apply(vac.stable_id(), "BLOCKED", note="CAPTCHA detected")
        
        # Should go back to READY_TO_APPLY, not APPLIED
        assert result.status == ApplicationStatus.READY_TO_APPLY
    finally:
        teardown_test_db(tmp_dir)


def test_verify_never_calls_submit():
    """Test verify_submission never calls submit_application."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Create mock adapter that tracks calls
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Application Submitted",
            "final_url": "https://example.com/success",
        })
        
        # Track calls
        calls_before = mock_adapter.calls.copy()
        
        ver = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        calls_after = mock_adapter.calls
        
        # Verify submit_application was NOT called
        submit_calls = [c for c in calls_after if "submit_application" in c]
        assert len(submit_calls) == 0, "submit_application should NOT be called during verification"
        
        # Verify only open and screenshot were called
        assert any("open:" in c for c in calls_after)
        assert any("screenshot:" in c for c in calls_after)
    finally:
        teardown_test_db(tmp_dir)


def test_repeated_verify_idempotent():
    """Test repeated verification is idempotent."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        set_application_status(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Application Submitted - Thank you",
            "final_url": "https://example.com/success",
        })
        
        # First verification
        ver1 = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        # Second verification
        ver2 = verify_submission(vac.stable_id(), sub_id, adapter=mock_adapter)
        
        # Both should be VERIFIED
        assert ver1.verification_status == VerificationStatus.VERIFIED
        assert ver2.verification_status == VerificationStatus.VERIFIED
        
        # Should have same submission_id and vacancy
        assert ver1.vacancy_stable_id == ver2.vacancy_stable_id
        assert ver1.submission_id == ver2.submission_id
        
        # Only one record in DB (upsert)
        all_ver = list_verifications()
        # Filter for this vacancy
        vacancy_vers = [v for v in all_ver if v.vacancy_stable_id == vac.stable_id()]
        assert len(vacancy_vers) == 1
    finally:
        teardown_test_db(tmp_dir)


def test_old_submission_remains_in_audit():
    """Test old submission attempts remain in audit history."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # First submission attempt - FAILED
        sub_id_1 = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": False, "submission_id": sub_id_1, "error": "Network error"}),
            status="FAILED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Verification for first attempt
        mock_adapter = MockBrowserAdapter(simulate={
                "page_title": "Error - Failed to apply",
                "final_url": "https://example.com/error",
            })
        ver1 = verify_submission(vac.stable_id(), sub_id_1, adapter=mock_adapter)
        assert ver1.verification_status == VerificationStatus.FAILED
        
        # Second submission attempt - VERIFIED
        sub_id_2 = f"{vac.stable_id()}_20240101_130000_def456"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id_2}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Application Submitted - Thank you",
            "final_url": "https://example.com/success",
        })
        ver2 = verify_submission(vac.stable_id(), sub_id_2, adapter=mock_adapter)
        assert ver2.verification_status == VerificationStatus.VERIFIED
        
        # Both verifications should exist in audit
        all_ver = list_verifications()
        vacancy_vers = [v for v in all_ver if v.vacancy_stable_id == vac.stable_id()]
        assert len(vacancy_vers) == 2
        
        # Both submissions should exist
        sub_1 = db.get_submission(vac.stable_id())
        # Note: application_submissions uses vacancy_stable_id as PK, so it will overwrite
        # But verifications use (vacancy_stable_id, submission_id, verification_version) as PK
        # So both verifications are preserved
    finally:
        teardown_test_db(tmp_dir)


def test_verify_submission_in_browser_integration():
    """Test verify_submission_in_browser function integrates with browser_executor."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac()
        db.save_vacancy(vac)
        
        # Set tracking and submission
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        
        sub_id = f"{vac.stable_id()}_20240101_120000_abc123"
        db.save_submission(
            vacancy_stable_id=vac.stable_id(),
            submission_json=json.dumps({"success": True, "submission_id": sub_id}),
            status="SUBMITTED",
            submitted_at=datetime.utcnow().isoformat(),
        )
        
        # Call verify_submission_in_browser
        from ai_assistant.browser_executor import verify_submission_in_browser
        ver = verify_submission_in_browser(vac.stable_id(), sub_id)
        
        assert ver is not None
        assert ver.verification_status in [VerificationStatus.VERIFIED, VerificationStatus.AMBIGUOUS, VerificationStatus.FAILED, VerificationStatus.BLOCKED]
    finally:
        teardown_test_db(tmp_dir)


def test_stage30e_confirmation_gate_and_verification_suite():
    """Stage 30E: Comprehensive test suite for confirmation gate and post-submit verification."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac(source_job_id="stage30e_1")
        db.save_vacancy(vac)
        
        # Setup: tracking READY_TO_APPLY, review APPROVED, browser session READY_FOR_REVIEW, package exists, queue exists
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        from ai_assistant.application_review import ApplicationReview, ReviewStatus, save_application_review
        rev = ApplicationReview(vacancy_stable_id=vac.stable_id(), status=ReviewStatus.APPROVED)
        save_application_review(rev)
        from ai_assistant.browser_executor import BrowserApplicationSession, BrowserStatus, save_browser_session
        now_str = datetime.utcnow().isoformat()
        sess = BrowserApplicationSession(
            vacancy_stable_id=vac.stable_id(),
            url=vac.job_url,
            status=BrowserStatus.READY_FOR_REVIEW,
            created_at=now_str,
            updated_at=now_str,
        )
        save_browser_session(sess)

        from ai_assistant.application_queue import QueueItem, save_queue_item
        q = QueueItem(
            vacancy_stable_id=vac.stable_id(),
            canonical_id=vac.stable_id(),
            representative_vacancy_stable_id=vac.stable_id(),
            priority_score=80,
            rank=1,
            match_score=80.0,
            deep_score=80.0,
        )
        save_queue_item(q)

        pkg_json = json.dumps({"cover_letter": "Letters", "validation_status": "VALID"})
        db.save_application_package(vac.stable_id(), "v1", pkg_json)

        # Test A: confirm_submit=False -> Submit is BLOCKED, not executed
        mock_adapter_no_confirm = MockBrowserAdapter(simulate={"fields": ["name", "email"], "apply_button": True})
        res_no_confirm = submit_application_in_browser(vac.stable_id(), confirm_submit=False, adapter=mock_adapter_no_confirm)
        assert res_no_confirm.status == "BLOCKED"
        assert "confirmation required" in res_no_confirm.error.lower()
        assert mock_adapter_no_confirm.submit_attempted is False
        assert db.is_submitted(vac.stable_id()) is False
        assert get_application_status(vac.stable_id()).status == ApplicationStatus.READY_TO_APPLY

        # Test B: confirm_submit=True -> Submit executed
        mock_adapter_confirm = MockBrowserAdapter(simulate={"fields": ["name", "email"], "apply_button": True})
        res_confirm = submit_application_in_browser(vac.stable_id(), confirm_submit=True, adapter=mock_adapter_confirm)
        assert res_confirm.status == "SUBMITTED"
        assert mock_adapter_confirm.submit_attempted is True
        assert db.is_submitted(vac.stable_id()) is True
        assert get_application_status(vac.stable_id()).status == ApplicationStatus.SUBMITTED

        # Test D: duplicate submit blocked
        res_duplicate = submit_application_in_browser(vac.stable_id(), confirm_submit=True, adapter=mock_adapter_confirm)
        assert res_duplicate.status == "BLOCKED"
        assert "already submitted" in res_duplicate.error.lower()

        # Test C: Post-Submit verifier distinguishes success / failure / ambiguous / blocked
        # C1: success
        mock_success = MockBrowserAdapter(simulate={"page_title": "Thank you for applying", "final_url": "https://example.com/done"})
        ver_success = verify_submission(vac.stable_id(), res_confirm.submission_id, adapter=mock_success)
        assert ver_success.verification_status == VerificationStatus.VERIFIED
        assert get_application_status(vac.stable_id()).status == ApplicationStatus.APPLIED

        # C2: error/blocked signals
        mock_error = MockBrowserAdapter(simulate={"page_title": "Error: submission failed", "final_url": "https://example.com/error"})
        s2, e2, b2 = _detect_signals("Error: submission failed", "Error", "https://example.com/error")
        assert len(e2) > 0

        mock_blocked = MockBrowserAdapter(simulate={"page_title": "Cloudflare captcha", "final_url": "https://example.com/challenge"})
        s3, e3, b3 = _detect_signals("Cloudflare captcha verification required", "Captcha", "https://example.com")
        assert len(b3) > 0

        # Test E: Cannot bypass confirmation via submit_vacancy CLI wrapper
        from ai_assistant.cli import submit_vacancy
        vac_e = _vac(source_job_id="stage30e_e")
        db.save_vacancy(vac_e)
        code = submit_vacancy(vac_e.stable_id(), confirm_submit=False)
        assert code == 1
        assert db.is_submitted(vac_e.stable_id()) is False

    finally:
        teardown_test_db(tmp_dir)


def test_stage30f_ambiguous_post_submit_verification_invariants():
    """Stage 30F: Verify that AMBIGUOUS post-submit state preserves honest truth and does not fake VERIFIED."""
    tmp_dir = setup_test_db()
    try:
        vac = _vac(source_job_id="stage30f_1")
        db.save_vacancy(vac)

        # 1. Setup ready vacancy
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        from ai_assistant.application_review import ApplicationReview, ReviewStatus, save_application_review
        rev = ApplicationReview(vacancy_stable_id=vac.stable_id(), status=ReviewStatus.APPROVED)
        save_application_review(rev)

        from ai_assistant.browser_executor import BrowserApplicationSession, BrowserStatus, save_browser_session
        now_str = datetime.utcnow().isoformat()
        sess = BrowserApplicationSession(
            vacancy_stable_id=vac.stable_id(),
            url=vac.job_url,
            status=BrowserStatus.READY_FOR_REVIEW,
            created_at=now_str,
            updated_at=now_str,
        )
        save_browser_session(sess)

        from ai_assistant.application_queue import QueueItem, save_queue_item
        q = QueueItem(
            vacancy_stable_id=vac.stable_id(),
            canonical_id=vac.stable_id(),
            representative_vacancy_stable_id=vac.stable_id(),
            priority_score=80,
            rank=1,
            match_score=80.0,
            deep_score=80.0,
        )
        save_queue_item(q)

        pkg_json = json.dumps({"cover_letter": "Letters", "validation_status": "VALID"})
        db.save_application_package(vac.stable_id(), "v1", pkg_json)

        # 2. Perform submit with confirm_submit=True
        mock_adapter = MockBrowserAdapter(simulate={"fields": ["name", "email"], "apply_button": True})
        submit_res = submit_application_in_browser(vac.stable_id(), confirm_submit=True, adapter=mock_adapter)
        assert submit_res.status == "SUBMITTED"
        assert db.is_submitted(vac.stable_id()) is True

        # Invariant 1: Application tracking is SUBMITTED
        assert get_application_status(vac.stable_id()).status == ApplicationStatus.SUBMITTED

        # 3. Post-submit verification on page with NO explicit confirmation message (e.g. RemoteOK job board page)
        mock_no_signal_adapter = MockBrowserAdapter(simulate={
            "page_title": "Remote Job Listing at Company",
            "final_url": "https://example.com/job/123",
            "body_text": "Company is hiring remote engineers. Apply here.",
        })
        ver_res = verify_submission(vac.stable_id(), submit_res.submission_id, adapter=mock_no_signal_adapter)

        # Invariant 2: Result MUST be AMBIGUOUS (not falsely VERIFIED, not FAILED)
        assert ver_res.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_res.success_signal is None
        assert "No clear success/error/blocked signals found" in ver_res.warnings

        # Invariant 3: AMBIGUOUS MUST NOT transition tracking to APPLIED or VERIFIED
        tracking_rec = get_application_status(vac.stable_id())
        assert tracking_rec.status == ApplicationStatus.SUBMITTED
        assert tracking_rec.status != ApplicationStatus.APPLIED
        assert tracking_rec.status != ApplicationStatus.VERIFIED

        # Invariant 4: Duplicate submit is strictly BLOCKED even when verification is AMBIGUOUS
        dup_res = submit_application_in_browser(vac.stable_id(), confirm_submit=True, adapter=mock_adapter)
        assert dup_res.status == "BLOCKED"
        assert "already submitted" in dup_res.error.lower()

        # Invariant 5: When real success signal IS present, status transitions to VERIFIED -> APPLIED
        mock_real_success = MockBrowserAdapter(simulate={
            "page_title": "Thank you for applying",
            "final_url": "https://example.com/thanks",
            "body_text": "Your application has been received. Thank you for your application.",
        })
        ver_real = verify_submission(vac.stable_id(), submit_res.submission_id, adapter=mock_real_success)
        assert ver_real.verification_status == VerificationStatus.VERIFIED
        assert get_application_status(vac.stable_id()).status == ApplicationStatus.APPLIED

    finally:
        teardown_test_db(tmp_dir)


def test_stage30g_flow_classification_and_routing():
    """Stage 30G: Apply Flow Classification + Safe Routing test suite."""
    from ai_assistant.browser_executor import (
        FlowType,
        classify_apply_flow,
        prepare_application_in_browser,
        submit_application_in_browser,
        BrowserStatus,
    )
    from ai_assistant.submission_verifier import verify_submission, VerificationStatus

    # A. Native form -> NATIVE_FORM
    res_a = classify_apply_flow(
        source_url="https://careers.google.com/jobs/results/123",
        final_url="https://careers.google.com/jobs/results/123",
        has_form=True,
    )
    assert res_a.flow_type == FlowType.NATIVE_FORM
    assert res_a.is_external_application is False
    assert res_a.verification_strategy == "native_submission_verifier"
    assert res_a.application_domain == "careers.google.com"

    # B. External ATS redirect -> EXTERNAL_ATS
    res_b1 = classify_apply_flow(
        source_url="https://company.com/careers/engineer",
        final_url="https://jobs.lever.co/company/456",
        apply_link="https://jobs.lever.co/company/456",
    )
    assert res_b1.flow_type == FlowType.EXTERNAL_ATS
    assert res_b1.is_external_application is True
    assert res_b1.application_domain == "jobs.lever.co"
    assert res_b1.verification_strategy == "external_ats_verifier"

    res_b2 = classify_apply_flow(
        source_url="https://startup.io/jobs/1",
        apply_link="https://boards.greenhouse.io/startup/jobs/789",
    )
    assert res_b2.flow_type == FlowType.EXTERNAL_ATS
    assert res_b2.application_domain == "boards.greenhouse.io"

    # C. Aggregator redirect -> AGGREGATOR_REDIRECT
    # C1: Aggregator with external destination
    res_c1 = classify_apply_flow(
        source_url="https://remoteok.com/remote-jobs/remote-engineer-105820",
        apply_link="https://company.com/apply",
    )
    assert res_c1.flow_type == FlowType.AGGREGATOR_REDIRECT
    assert res_c1.is_external_application is True
    assert res_c1.verification_strategy == "aggregator_redirect_pause"
    assert res_c1.application_domain == "company.com"

    res_c2 = classify_apply_flow(
        source_url="https://weworkremotely.com/remote-jobs/example",
        apply_link="https://company.com/apply",
    )
    assert res_c2.flow_type == FlowType.AGGREGATOR_REDIRECT
    assert res_c2.is_external_application is True

    # C3: Aggregator staying on same domain
    res_c3 = classify_apply_flow(
        source_url="https://remoteok.com/remote-jobs/remote-engineer-105820",
        final_url="https://remoteok.com/remote-jobs/remote-engineer-105820",
    )
    assert res_c3.flow_type == FlowType.AGGREGATOR_REDIRECT
    assert res_c3.is_external_application is False
    assert res_c3.application_url is None
    assert res_c3.application_domain is None

    # D. Unknown -> UNKNOWN
    res_d = classify_apply_flow(source_url="")
    assert res_d.flow_type == FlowType.UNKNOWN
    assert res_d.verification_strategy == "manual_review"

    # E. Aggregator click does NOT become VERIFIED without explicit employer confirmation
    tmp_dir = setup_test_db()
    try:
        vac_e = _vac(source_job_id="stage30g_agg", job_url="https://remoteok.com/remote-jobs/105820")
        db.save_vacancy(vac_e)
        set_application_status(vac_e.stable_id(), ApplicationStatus.READY_TO_APPLY)
        from ai_assistant.application_review import ApplicationReview, ReviewStatus, save_application_review
        save_application_review(ApplicationReview(vacancy_stable_id=vac_e.stable_id(), status=ReviewStatus.APPROVED))
        from ai_assistant.browser_executor import BrowserApplicationSession, save_browser_session
        now_str = datetime.utcnow().isoformat()
        save_browser_session(BrowserApplicationSession(
            vacancy_stable_id=vac_e.stable_id(),
            url=vac_e.job_url,
            status=BrowserStatus.READY_FOR_REVIEW,
            created_at=now_str,
            updated_at=now_str,
        ))
        from ai_assistant.application_queue import QueueItem, save_queue_item
        save_queue_item(QueueItem(
            vacancy_stable_id=vac_e.stable_id(),
            canonical_id=vac_e.stable_id(),
            representative_vacancy_stable_id=vac_e.stable_id(),
            priority_score=80,
            rank=1,
            match_score=80.0,
            deep_score=80.0,
        ))
        db.save_application_package(vac_e.stable_id(), "v1", json.dumps({"cover_letter": "Letter", "validation_status": "VALID"}))

        # Submit on aggregator
        mock_agg = MockBrowserAdapter(simulate={"fields": ["name", "email"], "apply_button": True, "page_title": "Remote Job on RemoteOK"})
        submit_agg = submit_application_in_browser(vac_e.stable_id(), confirm_submit=True, adapter=mock_agg)
        assert submit_agg.status == "SUBMITTED"

        # Verify on aggregator page without explicit success text
        mock_ver_agg = MockBrowserAdapter(simulate={"page_title": "Remote Job on RemoteOK", "body_text": "Apply now at RemoteOK"})
        ver_agg = verify_submission(vac_e.stable_id(), submit_agg.submission_id, adapter=mock_ver_agg)
        assert ver_agg.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_agg.flow_type == FlowType.AGGREGATOR_REDIRECT
        assert get_application_status(vac_e.stable_id()).status == ApplicationStatus.SUBMITTED

        # F. External ATS without confirmation does NOT become VERIFIED
        vac_f = _vac(source_job_id="stage30g_ats", job_url="https://jobs.lever.co/company/123")
        db.save_vacancy(vac_f)
        set_application_status(vac_f.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_f.stable_id(), status=ReviewStatus.APPROVED))
        save_browser_session(BrowserApplicationSession(
            vacancy_stable_id=vac_f.stable_id(),
            url=vac_f.job_url,
            status=BrowserStatus.READY_FOR_REVIEW,
            created_at=now_str,
            updated_at=now_str,
        ))
        save_queue_item(QueueItem(
            vacancy_stable_id=vac_f.stable_id(),
            canonical_id=vac_f.stable_id(),
            representative_vacancy_stable_id=vac_f.stable_id(),
            priority_score=80,
            rank=1,
            match_score=80.0,
            deep_score=80.0,
        ))
        db.save_application_package(vac_f.stable_id(), "v1", json.dumps({"cover_letter": "Letter", "validation_status": "VALID"}))
        mock_ats = MockBrowserAdapter(simulate={"fields": ["name", "email"], "apply_button": True, "page_title": "Job at Lever"})
        submit_ats = submit_application_in_browser(vac_f.stable_id(), confirm_submit=True, adapter=mock_ats)

        mock_ver_ats = MockBrowserAdapter(simulate={"page_title": "Job at Lever", "body_text": "Submit application to company."})
        ver_ats = verify_submission(vac_f.stable_id(), submit_ats.submission_id, adapter=mock_ver_ats)
        assert ver_ats.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_ats.flow_type == FlowType.EXTERNAL_ATS

        # G. Only explicit success signal -> VERIFIED
        mock_ver_success = MockBrowserAdapter(simulate={"page_title": "Thank you for applying", "body_text": "Your application has been received."})
        ver_success = verify_submission(vac_f.stable_id(), submit_ats.submission_id, adapter=mock_ver_success)
        assert ver_success.verification_status == VerificationStatus.VERIFIED
        assert get_application_status(vac_f.stable_id()).status == ApplicationStatus.APPLIED

    finally:
        teardown_test_db(tmp_dir)


def test_stage30h_apply_destination_detection_invariants():
    """Stage 30H: Rigorous test suite for destination detection, redirect chains, and external invariants."""
    from ai_assistant.browser_executor import (
        FlowType,
        classify_apply_flow,
    )

    # A. RemoteOK -> external ATS
    res_a = classify_apply_flow(
        source_url="https://remoteok.com/remote-jobs/105820",
        apply_link="https://boards.greenhouse.io/company/jobs/123",
    )
    assert res_a.flow_type == FlowType.EXTERNAL_ATS
    assert res_a.is_external_application is True
    assert res_a.application_url == "https://boards.greenhouse.io/company/jobs/123"
    assert res_a.application_domain == "boards.greenhouse.io"
    assert res_a.verification_strategy == "external_ats_verifier"

    # B. RemoteOK -> same domain RemoteOK
    res_b = classify_apply_flow(
        source_url="https://remoteok.com/remote-jobs/105820",
        final_url="https://remoteok.com/remote-jobs/105820-senior-developer",
    )
    assert res_b.flow_type == FlowType.AGGREGATOR_REDIRECT
    assert res_b.is_external_application is False
    assert res_b.application_url is None
    assert res_b.application_domain is None

    # C. RemoteOK without detected Apply href
    res_c = classify_apply_flow(
        source_url="https://remoteok.com/remote-jobs/105820",
        apply_link=None,
    )
    assert res_c.flow_type == FlowType.AGGREGATOR_REDIRECT
    assert res_c.is_external_application is False
    assert res_c.application_url is None
    assert res_c.application_domain is None

    # D. Native application (source_domain == application_domain)
    res_d = classify_apply_flow(
        source_url="https://careers.google.com/jobs/results/123",
        final_url="https://careers.google.com/jobs/results/123",
        has_form=True,
    )
    assert res_d.flow_type == FlowType.NATIVE_FORM
    assert res_d.is_external_application is False
    assert res_d.application_domain == "careers.google.com"

    # E. Greenhouse / Lever / Workable
    # E1: Outbound link to Lever
    res_e1 = classify_apply_flow(
        source_url="https://corp.com/careers/engineer",
        apply_link="https://jobs.lever.co/corp/789",
    )
    assert res_e1.flow_type == FlowType.EXTERNAL_ATS
    assert res_e1.is_external_application is True
    assert res_e1.application_domain == "jobs.lever.co"

    # E2: Direct ATS source
    res_e2 = classify_apply_flow(
        source_url="https://apply.workable.com/company/j/12345",
    )
    assert res_e2.flow_type == FlowType.EXTERNAL_ATS
    assert res_e2.is_external_application is False
    assert res_e2.application_domain == "apply.workable.com"

    # F. Unknown (insufficient data)
    res_f1 = classify_apply_flow(source_url="")
    assert res_f1.flow_type == FlowType.UNKNOWN
    assert res_f1.is_external_application is False
    assert res_f1.application_url is None
    assert res_f1.application_domain is None

    res_f2 = classify_apply_flow(source_url="not-a-valid-url")
    assert res_f2.flow_type == FlowType.UNKNOWN
    assert res_f2.is_external_application is False

    # G. Invariant: NEVER is_external_application=True if source_domain == application_domain
    for test_domain in ["example.com", "remoteok.com", "hh.ru", "jobs.lever.co", "company.io"]:
        res_g = classify_apply_flow(
            source_url=f"https://{test_domain}/job/1",
            final_url=f"https://{test_domain}/job/1/apply",
            apply_link=f"https://{test_domain}/submit",
            has_form=True,
        )
        if res_g.application_domain:
            assert res_g.application_domain == test_domain
        assert res_g.is_external_application is False, f"Invariant violated for {test_domain}"


def test_stage30i_read_only_apply_flow_audit_invariants():
    """Stage 30I: Verify read-only audit never submits, never creates submissions/verifications, and preserves DB."""
    from pathlib import Path
    from ai_assistant.browser_executor import (
        audit_apply_flow_for_vacancy,
        run_apply_flow_audit,
        MockBrowserAdapter,
        FlowType,
    )
    from ai_assistant.application_tracking import get_application_status

    tmp_dir = setup_test_db()
    try:
        # Create test vacancies
        vac1 = _vac(source_job_id="s30i_1", job_url="https://remoteok.com/remote-jobs/105820")
        vac2 = _vac(source_job_id="s30i_2", job_url="https://jobs.lever.co/corp/123")
        vac3 = _vac(source_job_id="s30i_3", job_url="https://careers.google.com/jobs/456")

        db.save_vacancy(vac1)
        db.save_vacancy(vac2)
        db.save_vacancy(vac3)

        set_application_status(vac1.stable_id(), ApplicationStatus.READY_TO_APPLY)
        set_application_status(vac2.stable_id(), ApplicationStatus.READY_TO_APPLY)
        set_application_status(vac3.stable_id(), ApplicationStatus.READY_TO_APPLY)

        mock_adapter = MockBrowserAdapter(simulate={
            "page_title": "Job Title",
            "apply_button": True,
            "apply_link": "https://boards.greenhouse.io/corp/jobs/789",
            "fields": ["name", "email"],
        })

        # Run audit on vac1
        res1 = audit_apply_flow_for_vacancy(vac1.stable_id(), adapter=mock_adapter)

        # Verify classification
        assert res1["flow_type"] == FlowType.EXTERNAL_ATS.value
        assert res1["is_external_application"] is True
        assert res1["apply_href"] == "https://boards.greenhouse.io/corp/jobs/789"
        assert res1["application_domain"] == "boards.greenhouse.io"

        # Check safety invariants: ZERO submits, ZERO DB submissions, ZERO DB verifications
        assert mock_adapter.submit_attempted is False
        assert "submit_application" not in mock_adapter.calls
        assert db.is_submitted(vac1.stable_id()) is False

        # Verify tracking status unchanged
        assert get_application_status(vac1.stable_id()).status == ApplicationStatus.READY_TO_APPLY

        # Run multi-vacancy audit
        audit_file = str(Path(tmp_dir) / "audit_results.json")
        all_res = run_apply_flow_audit([vac1.stable_id(), vac2.stable_id(), vac3.stable_id()], adapter=mock_adapter, output_path=audit_file)
        assert len(all_res) == 3
        assert Path(audit_file).exists()

        # Ensure no submission/verification records created in DB
        conn = db.get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM application_submissions")
        assert c.fetchone()[0] == 0
        c.execute("SELECT COUNT(*) FROM submission_verifications")
        assert c.fetchone()[0] == 0

    finally:
        teardown_test_db(tmp_dir)


def test_stage30j_real_apply_destination_detection_suite():
    """Stage 30J: Test accurate detection of real apply CTA vs navigation links and extended evidence."""
    from pathlib import Path
    from ai_assistant.browser_executor import (
        audit_apply_flow_for_vacancy,
        classify_apply_flow,
        MockBrowserAdapter,
        FlowType,
    )

    tmp_dir = setup_test_db()
    try:
        # A. Real Apply link on Greenhouse from RemoteOK
        vac_a = _vac(source_job_id="s30j_a", job_url="https://remoteok.com/remote-jobs/105820_a")
        db.save_vacancy(vac_a)
        mock_a = MockBrowserAdapter(simulate={
            "page_title": "Senior Engineer at RemoteOK",
            "apply_button": True,
            "apply_link": "https://boards.greenhouse.io/company/jobs/123",
            "button_text": "Apply for this job",
            "reason": "Direct external ATS link in href",
        })
        res_a = audit_apply_flow_for_vacancy(vac_a.stable_id(), adapter=mock_a)
        assert res_a["flow_type"] == FlowType.EXTERNAL_ATS.value
        assert res_a["is_external_application"] is True
        assert res_a["apply_href"] == "https://boards.greenhouse.io/company/jobs/123"
        assert res_a["application_domain"] == "boards.greenhouse.io"
        assert res_a["verification_strategy"] == "external_ats_verifier"
        assert res_a["evidence"]["apply_element_text"] == "Apply for this job"
        assert "Direct external ATS link" in res_a["evidence"]["apply_detection_reason"]

        # B. Apply link to company website from WeWorkRemotely
        vac_b = _vac(source_job_id="s30j_b", job_url="https://weworkremotely.com/remote-jobs/123_b")
        db.save_vacancy(vac_b)
        mock_b = MockBrowserAdapter(simulate={
            "page_title": "Developer at Company",
            "apply_button": True,
            "apply_link": "https://company.com/careers/apply/456",
            "button_text": "Apply for this position",
            "reason": "Exact primary Apply CTA text",
        })
        res_b = audit_apply_flow_for_vacancy(vac_b.stable_id(), adapter=mock_b)
        assert res_b["flow_type"] == FlowType.AGGREGATOR_REDIRECT.value
        assert res_b["is_external_application"] is True
        assert res_b["apply_href"] == "https://company.com/careers/apply/456"
        assert res_b["application_domain"] == "company.com"
        assert res_b["verification_strategy"] == "aggregator_redirect_pause"

        # C. JS button without static outbound href (modal / in-page)
        vac_c = _vac(source_job_id="s30j_c", job_url="https://remoteok.com/remote-jobs/105820_c")
        db.save_vacancy(vac_c)
        mock_c = MockBrowserAdapter(simulate={
            "page_title": "Remote Job",
            "apply_button": True,
            "apply_link": None,
            "button_text": "Apply Now",
            "reason": "Exact primary Apply CTA text 'Apply Now' (JS click-handler / in-page button without static outbound href)",
        })
        res_c = audit_apply_flow_for_vacancy(vac_c.stable_id(), adapter=mock_c)
        assert res_c["flow_type"] == FlowType.AGGREGATOR_REDIRECT.value
        assert res_c["is_external_application"] is False
        assert res_c["apply_href"] is None
        assert res_c["application_url"] is None
        assert res_c["application_domain"] is None
        assert res_c["verification_strategy"] == "aggregator_redirect_pause"
        assert "JS click-handler" in res_c["evidence"]["apply_detection_reason"]

        # D. Russian HH.ru CTA vs navigation
        vac_d = _vac(source_job_id="s30j_d", job_url="https://hh.ru/vacancy/136097888_d")
        db.save_vacancy(vac_d)
        mock_d = MockBrowserAdapter(simulate={
            "page_title": "Python Developer на HH.ru",
            "apply_button": True,
            "apply_link": None,
            "button_text": "Откликнуться",
            "fields": ["resume", "cover_letter"],
            "reason": "Exact primary Apply CTA text 'Откликнуться'",
        })
        res_d = audit_apply_flow_for_vacancy(vac_d.stable_id(), adapter=mock_d)
        assert res_d["flow_type"] == FlowType.NATIVE_FORM.value
        assert res_d["is_external_application"] is False
        assert res_d["application_domain"] == "hh.ru"
        assert res_d["evidence"]["apply_element_text"] == "Откликнуться"

    finally:
        teardown_test_db(tmp_dir)


def test_stage30l_verification_decision_matrix_and_idempotency_suite():
    """Stage 30L: Test all verification decision matrix scenarios (A-G) and idempotency guards."""
    from ai_assistant.submission_verifier import (
        verify_submission,
        VerificationStatus,
    )
    from ai_assistant.browser_executor import (
        MockBrowserAdapter,
        submit_application_in_browser,
        FlowType,
    )
    from ai_assistant.application_tracking import (
        get_application_status,
        set_application_status,
        ApplicationStatus,
    )

    tmp_dir = setup_test_db()
    try:
        # A. Native success -> VERIFIED and tracking becomes APPLIED
        vac_a = _vac(source_job_id="s30l_a", job_url="https://company.com/jobs/1")
        db.save_vacancy(vac_a)
        set_application_status(vac_a.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_a.stable_id(), "sub_a", "SUBMITTED", "raw")
        mock_a = MockBrowserAdapter(simulate={
            "page_title": "Application Received - Company Careers",
            "final_url": "https://company.com/jobs/1/thank-you",
            "content": "Thank you for applying! Your application has been received.",
        })
        ver_a = verify_submission(vac_a.stable_id(), "sub_a", adapter=mock_a)
        assert ver_a.verification_status == VerificationStatus.VERIFIED
        assert ver_a.success_signal in ("thank you for applying", "application received")
        assert get_application_status(vac_a.stable_id()).status == ApplicationStatus.APPLIED

        # B. Native no confirmation -> AMBIGUOUS and tracking stays SUBMITTED
        vac_b = _vac(source_job_id="s30l_b", job_url="https://company.com/jobs/2")
        db.save_vacancy(vac_b)
        set_application_status(vac_b.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_b.stable_id(), "sub_b", "SUBMITTED", "raw")
        mock_b = MockBrowserAdapter(simulate={
            "page_title": "Company Careers",
            "final_url": "https://company.com/jobs/2",
            "content": "Job Description for Engineer. Requirements: Python.",
        })
        ver_b = verify_submission(vac_b.stable_id(), "sub_b", adapter=mock_b)
        assert ver_b.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_b.success_signal is None
        assert get_application_status(vac_b.stable_id()).status == ApplicationStatus.SUBMITTED

        # C. Aggregator -> external ATS -> confirmation -> VERIFIED and tracking becomes APPLIED
        vac_c = _vac(source_job_id="s30l_c", job_url="https://remoteok.com/remote-jobs/100")
        db.save_vacancy(vac_c)
        set_application_status(vac_c.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_c.stable_id(), "sub_c", "SUBMITTED", "raw")
        mock_c = MockBrowserAdapter(simulate={
            "page_title": "Job Application Submitted - Greenhouse",
            "final_url": "https://boards.greenhouse.io/corp/jobs/100/confirmation",
            "content": "Your application has been received. Thank you for your application!",
        })
        ver_c = verify_submission(vac_c.stable_id(), "sub_c", adapter=mock_c)
        assert ver_c.verification_status == VerificationStatus.VERIFIED
        assert ver_c.is_external_application is True
        assert ver_c.application_domain == "boards.greenhouse.io"
        assert get_application_status(vac_c.stable_id()).status == ApplicationStatus.APPLIED

        # D. Aggregator -> external ATS -> no confirmation -> AMBIGUOUS and tracking stays SUBMITTED
        vac_d = _vac(source_job_id="s30l_d", job_url="https://remoteok.com/remote-jobs/101")
        db.save_vacancy(vac_d)
        set_application_status(vac_d.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_d.stable_id(), "sub_d", "SUBMITTED", "raw")
        mock_d = MockBrowserAdapter(simulate={
            "page_title": "Job Application - Greenhouse",
            "final_url": "https://boards.greenhouse.io/corp/jobs/101",
            "content": "Please fill out your details.",
        })
        ver_d = verify_submission(vac_d.stable_id(), "sub_d", adapter=mock_d)
        assert ver_d.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_d.is_external_application is True
        assert ver_d.application_domain == "boards.greenhouse.io"
        assert get_application_status(vac_d.stable_id()).status == ApplicationStatus.SUBMITTED

        # E. Aggregator -> Cloudflare -> BLOCKED and tracking stays SUBMITTED (never APPLIED)
        vac_e = _vac(source_job_id="s30l_e", job_url="https://remoteok.com/remote-jobs/102")
        db.save_vacancy(vac_e)
        set_application_status(vac_e.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_e.stable_id(), "sub_e", "SUBMITTED", "raw")
        mock_e = MockBrowserAdapter(simulate={
            "page_title": "Remote OK - Cloudflare Security",
            "final_url": "https://remoteok.com/remote-jobs/102",
            "content": "Checking your browser before accessing. Cloudflare ray ID: 12345.",
        })
        ver_e = verify_submission(vac_e.stable_id(), "sub_e", adapter=mock_e)
        assert ver_e.verification_status == VerificationStatus.BLOCKED
        assert "cloudflare" in ver_e.warnings[0].lower()
        assert get_application_status(vac_e.stable_id()).status == ApplicationStatus.SUBMITTED

        # F. Aggregator page after Submit (URL remains on aggregator) -> AMBIGUOUS, never VERIFIED
        vac_f = _vac(source_job_id="s30l_f", job_url="https://weworkremotely.com/remote-jobs/103")
        db.save_vacancy(vac_f)
        set_application_status(vac_f.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_f.stable_id(), "sub_f", "SUBMITTED", "raw")
        mock_f = MockBrowserAdapter(simulate={
            "page_title": "We Work Remotely - Top Remote Jobs",
            "final_url": "https://weworkremotely.com/remote-jobs/103",
            "content": "Browse more jobs in Development.",
        })
        ver_f = verify_submission(vac_f.stable_id(), "sub_f", adapter=mock_f)
        assert ver_f.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_f.is_external_application is False
        assert get_application_status(vac_f.stable_id()).status == ApplicationStatus.SUBMITTED

        # G. False-positive confirmation rejection -> AMBIGUOUS, never VERIFIED
        vac_g = _vac(source_job_id="s30l_g", job_url="https://example.com/jobs/104")
        db.save_vacancy(vac_g)
        set_application_status(vac_g.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_g.stable_id(), "sub_g", "SUBMITTED", "raw")
        mock_g = MockBrowserAdapter(simulate={
            "page_title": "Apply for Job - Example",
            "final_url": "https://example.com/jobs/104",
            "content": "Click Apply now to submit your application. My applications section is available in profile.",
        })
        ver_g = verify_submission(vac_g.stable_id(), "sub_g", adapter=mock_g)
        assert ver_g.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_g.success_signal is None
        assert get_application_status(vac_g.stable_id()).status == ApplicationStatus.SUBMITTED

        # Idempotency regression: Submit blocked when already submitted
        vac_idemp = _vac(source_job_id="s30l_idemp", job_url="https://example.com/jobs/105")
        db.save_vacancy(vac_idemp)
        set_application_status(vac_idemp.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_idemp.stable_id(), "sub_idemp_1", "SUBMITTED", "raw")

        dup_submit = submit_application_in_browser(
            vac_idemp.stable_id(),
            confirm_submit=True,
            adapter=mock_g,
            force=True
        )
        assert dup_submit.status == "BLOCKED"
        assert "already submitted" in str(dup_submit.error).lower()
        # Verify exactly 1 submission in DB
        conn = db.get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM application_submissions WHERE vacancy_stable_id=?", (vac_idemp.stable_id(),))
        assert c.fetchone()[0] == 1
        conn.close()

    finally:
        teardown_test_db(tmp_dir)


def test_stage30m_real_external_ats_detection_suite():
    """Stage 30M: Comprehensive tests for all external ATS platforms, same-domain rules, and redirect chains."""
    from ai_assistant.browser_executor import (
        classify_apply_flow,
        FlowType,
    )
    from ai_assistant.submission_verifier import (
        verify_submission,
        VerificationStatus,
    )
    from ai_assistant.application_tracking import (
        get_application_status,
        set_application_status,
        ApplicationStatus,
    )

    tmp_dir = setup_test_db()
    try:
        # 1. Aggregator -> Greenhouse = EXTERNAL_ATS, is_external = True
        r_gh = classify_apply_flow(
            source_url="https://remoteok.com/remote-jobs/101",
            apply_link="https://boards.greenhouse.io/company/jobs/555",
        )
        assert r_gh.flow_type == FlowType.EXTERNAL_ATS
        assert r_gh.is_external_application is True
        assert r_gh.application_domain == "boards.greenhouse.io"
        assert r_gh.verification_strategy == "external_ats_verifier"

        # 2. Aggregator -> Lever = EXTERNAL_ATS, is_external = True
        r_lev = classify_apply_flow(
            source_url="https://weworkremotely.com/remote-jobs/102",
            apply_link="https://jobs.lever.co/enterprise/888",
        )
        assert r_lev.flow_type == FlowType.EXTERNAL_ATS
        assert r_lev.is_external_application is True
        assert r_lev.application_domain == "jobs.lever.co"

        # 3. Aggregator -> Workable = EXTERNAL_ATS, is_external = True
        r_wrk = classify_apply_flow(
            source_url="https://himalayas.app/companies/tech/jobs/lead",
            apply_link="https://apply.workable.com/techcorp/j/999/",
        )
        assert r_wrk.flow_type == FlowType.EXTERNAL_ATS
        assert r_wrk.is_external_application is True
        assert r_wrk.application_domain == "apply.workable.com"

        # 4. Aggregator -> Ashby = EXTERNAL_ATS, is_external = True
        r_ash = classify_apply_flow(
            source_url="https://remoteok.com/remote-jobs/103",
            apply_link="https://jobs.ashbyhq.com/startup/1234",
        )
        assert r_ash.flow_type == FlowType.EXTERNAL_ATS
        assert r_ash.is_external_application is True
        assert r_ash.application_domain == "jobs.ashbyhq.com"

        # 5. Aggregator -> SmartRecruiters = EXTERNAL_ATS, is_external = True
        r_sr = classify_apply_flow(
            source_url="https://remoteok.com/remote-jobs/104",
            apply_link="https://jobs.smartrecruiters.com/AcmeCorp/5678",
        )
        assert r_sr.flow_type == FlowType.EXTERNAL_ATS
        assert r_sr.is_external_application is True
        assert r_sr.application_domain == "jobs.smartrecruiters.com"

        # 6. Aggregator -> same domain = is_external_application is False
        r_same = classify_apply_flow(
            source_url="https://remoteok.com/remote-jobs/105",
            final_url="https://remoteok.com/remote-jobs/105-python-dev",
        )
        assert r_same.flow_type == FlowType.AGGREGATOR_REDIRECT
        assert r_same.is_external_application is False
        assert r_same.application_domain is None

        # 7. ATS source -> same ATS domain = is_external_application is False
        r_ats_same = classify_apply_flow(
            source_url="https://boards.greenhouse.io/company/jobs/111",
            final_url="https://boards.greenhouse.io/company/jobs/111#apply",
            has_form=True,
        )
        assert r_ats_same.flow_type == FlowType.EXTERNAL_ATS
        assert r_ats_same.is_external_application is False
        assert r_ats_same.application_domain == "boards.greenhouse.io"

        # 8. Navigation href -> excluded / not external
        r_nav = classify_apply_flow(
            source_url="https://remoteok.com/remote-jobs/106",
            apply_link=None,
        )
        assert r_nav.is_external_application is False

        # 9. Missing / invalid URL = UNKNOWN
        r_inv = classify_apply_flow(source_url="")
        assert r_inv.flow_type == FlowType.UNKNOWN
        assert r_inv.is_external_application is False

        # 10. External ATS without confirmation receipt -> AMBIGUOUS, never VERIFIED
        vac_ats_no_conf = _vac(source_job_id="s30m_ats_nc", job_url="https://remoteok.com/remote-jobs/200")
        db.save_vacancy(vac_ats_no_conf)
        set_application_status(vac_ats_no_conf.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_ats_no_conf.stable_id(), "sub_ats_nc", "SUBMITTED", "raw")

        from ai_assistant.browser_executor import MockBrowserAdapter
        mock_ats = MockBrowserAdapter(simulate={
            "page_title": "Application Form - Lever",
            "final_url": "https://jobs.lever.co/enterprise/200",
            "content": "Please upload your resume.",
        })
        ver_ats = verify_submission(vac_ats_no_conf.stable_id(), "sub_ats_nc", adapter=mock_ats)
        assert ver_ats.verification_status == VerificationStatus.AMBIGUOUS
        assert ver_ats.is_external_application is True
        assert ver_ats.application_domain == "jobs.lever.co"
        assert get_application_status(vac_ats_no_conf.stable_id()).status == ApplicationStatus.SUBMITTED

    finally:
        teardown_test_db(tmp_dir)


def test_stage30n_orchestration_e2e_safety_gate_suite():
    """Stage 30N: Full Apply Orchestration E2E Safety Gate verification."""
    from ai_assistant.browser_executor import (
        classify_apply_flow,
        submit_application_in_browser,
        prepare_application_in_browser,
        audit_apply_flow_for_vacancy,
        MockBrowserAdapter,
        FlowType,
        BrowserStatus,
    )
    from ai_assistant.submission_verifier import (
        verify_submission,
        VerificationStatus,
    )
    from ai_assistant.application_tracking import (
        get_application_status,
        set_application_status,
        ApplicationStatus,
    )
    from ai_assistant.application_review import ApplicationReview, ReviewStatus, save_application_review
    from ai_assistant.application_queue import QueueItem, save_queue_item

    tmp_dir = setup_test_db()
    try:
        # 1. Native Application (HH/Habr-like)
        vac_1 = _vac(source_job_id="s30n_1", job_url="https://hh.ru/vacancy/9991")
        db.save_vacancy(vac_1)
        r1 = classify_apply_flow(
            source_url="https://hh.ru/vacancy/9991",
            final_url="https://hh.ru/vacancy/9991",
            apply_link=None,
            has_form=True,
        )
        assert r1.flow_type == FlowType.NATIVE_FORM
        assert r1.verification_strategy == "native_submission_verifier"
        assert r1.is_external_application is False

        # 2. Aggregator internal redirect (RemoteOK/WWR/Himalayas)
        r2 = classify_apply_flow(
            source_url="https://remoteok.com/remote-jobs/101",
            final_url="https://remoteok.com/remote-jobs/101-dev",
            apply_link="https://remoteok.com/l/101",
        )
        assert r2.flow_type == FlowType.AGGREGATOR_REDIRECT
        assert r2.verification_strategy == "aggregator_redirect_pause"
        assert r2.is_external_application is False

        # 3. Aggregator -> external ATS (Greenhouse, Lever, Workable, Ashby, SmartRecruiters)
        ats_targets = [
            ("https://boards.greenhouse.io/corp/jobs/1", "boards.greenhouse.io"),
            ("https://jobs.lever.co/corp/2", "jobs.lever.co"),
            ("https://apply.workable.com/corp/j/3", "apply.workable.com"),
            ("https://jobs.ashbyhq.com/corp/4", "jobs.ashbyhq.com"),
            ("https://jobs.smartrecruiters.com/corp/5", "jobs.smartrecruiters.com"),
        ]
        for ats_url, expected_dom in ats_targets:
            r3 = classify_apply_flow(
                source_url="https://weworkremotely.com/remote-jobs/200",
                apply_link=ats_url,
            )
            assert r3.flow_type == FlowType.EXTERNAL_ATS
            assert r3.is_external_application is True
            assert r3.application_domain == expected_dom
            assert r3.verification_strategy == "external_ats_verifier"

        # 4. Same-domain ATS (Direct Greenhouse/Lever source)
        r4 = classify_apply_flow(
            source_url="https://boards.greenhouse.io/company/jobs/123",
            final_url="https://boards.greenhouse.io/company/jobs/123#apply",
            has_form=True,
        )
        assert r4.flow_type == FlowType.EXTERNAL_ATS
        assert r4.is_external_application is False
        assert r4.application_domain == "boards.greenhouse.io"

        # 5. JS Apply without static href
        mock_js = MockBrowserAdapter(simulate={
            "page_title": "Job Posting",
            "apply_button": True,
            "apply_link": None,
            "button_text": "Apply Now",
            "reason": "Exact primary Apply CTA text 'Apply Now' (JS click-handler / in-page button without static outbound href)",
        })
        vac_5 = _vac(source_job_id="s30n_5", job_url="https://remoteok.com/remote-jobs/505")
        db.save_vacancy(vac_5)
        res_5 = audit_apply_flow_for_vacancy(vac_5.stable_id(), adapter=mock_js)
        assert res_5["apply_present"] is True
        assert res_5["apply_href"] is None
        assert res_5["application_url"] is None
        assert res_5["is_external_application"] is False

        # 6. Post-submit verification matrix (A-E)
        # 6A. Explicit success -> VERIFIED -> tracking APPLIED
        vac_6a = _vac(source_job_id="s30n_6a", job_url="https://employer.com/jobs/1")
        db.save_vacancy(vac_6a)
        set_application_status(vac_6a.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_6a.stable_id(), "sub_6a", "SUBMITTED", "raw")
        mock_6a = MockBrowserAdapter(simulate={"content": "Thank you for applying! Your application has been received."})
        ver_6a = verify_submission(vac_6a.stable_id(), "sub_6a", adapter=mock_6a)
        assert ver_6a.verification_status == VerificationStatus.VERIFIED
        assert get_application_status(vac_6a.stable_id()).status == ApplicationStatus.APPLIED

        # 6B. No confirmation -> AMBIGUOUS -> tracking SUBMITTED
        vac_6b = _vac(source_job_id="s30n_6b", job_url="https://employer.com/jobs/2")
        db.save_vacancy(vac_6b)
        set_application_status(vac_6b.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_6b.stable_id(), "sub_6b", "SUBMITTED", "raw")
        mock_6b = MockBrowserAdapter(simulate={"content": "Job Description and Requirements."})
        ver_6b = verify_submission(vac_6b.stable_id(), "sub_6b", adapter=mock_6b)
        assert ver_6b.verification_status == VerificationStatus.AMBIGUOUS
        assert get_application_status(vac_6b.stable_id()).status == ApplicationStatus.SUBMITTED

        # 6C. Cloudflare/CAPTCHA/WAF -> BLOCKED -> tracking SUBMITTED
        vac_6c = _vac(source_job_id="s30n_6c", job_url="https://remoteok.com/remote-jobs/3")
        db.save_vacancy(vac_6c)
        set_application_status(vac_6c.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_6c.stable_id(), "sub_6c", "SUBMITTED", "raw")
        mock_6c = MockBrowserAdapter(simulate={"content": "Checking browser. Cloudflare captcha challenge."})
        ver_6c = verify_submission(vac_6c.stable_id(), "sub_6c", adapter=mock_6c)
        assert ver_6c.verification_status == VerificationStatus.BLOCKED
        assert get_application_status(vac_6c.stable_id()).status == ApplicationStatus.SUBMITTED

        # 6D. Aggregator page after submit -> AMBIGUOUS -> tracking SUBMITTED
        vac_6d = _vac(source_job_id="s30n_6d", job_url="https://weworkremotely.com/remote-jobs/4")
        db.save_vacancy(vac_6d)
        set_application_status(vac_6d.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_6d.stable_id(), "sub_6d", "SUBMITTED", "raw")
        mock_6d = MockBrowserAdapter(simulate={"content": "Find more remote jobs at WeWorkRemotely."})
        ver_6d = verify_submission(vac_6d.stable_id(), "sub_6d", adapter=mock_6d)
        assert ver_6d.verification_status == VerificationStatus.AMBIGUOUS
        assert get_application_status(vac_6d.stable_id()).status == ApplicationStatus.SUBMITTED

        # 6E. Misleading text ("Apply now", "Job submitted", "My applications") -> AMBIGUOUS
        vac_6e = _vac(source_job_id="s30n_6e", job_url="https://example.com/jobs/5")
        db.save_vacancy(vac_6e)
        set_application_status(vac_6e.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_6e.stable_id(), "sub_6e", "SUBMITTED", "raw")
        mock_6e = MockBrowserAdapter(simulate={"content": "Apply now! View My applications tab in dashboard."})
        ver_6e = verify_submission(vac_6e.stable_id(), "sub_6e", adapter=mock_6e)
        assert ver_6e.verification_status == VerificationStatus.AMBIGUOUS
        assert get_application_status(vac_6e.stable_id()).status == ApplicationStatus.SUBMITTED

        # 7. Idempotency Guard (Duplicate submit returns BLOCKED)
        vac_7 = _vac(source_job_id="s30n_7", job_url="https://remoteok.com/remote-jobs/700")
        db.save_vacancy(vac_7)
        set_application_status(vac_7.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_7.stable_id(), "sub_7", "SUBMITTED", "raw")

        dup_res = submit_application_in_browser(
            vac_7.stable_id(),
            confirm_submit=True,
            adapter=mock_6a,
            force=True
        )
        assert dup_res.status == "BLOCKED"
        assert "already submitted" in str(dup_res.error).lower()
        conn = db.get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM application_submissions WHERE vacancy_stable_id=?", (vac_7.stable_id(),))
        assert c.fetchone()[0] == 1
        conn.close()

        # 8. Orchestrator Safety Invariants
        # 8A. Unconfirmed submit cannot proceed without confirm_submit=True
        vac_8 = _vac(source_job_id="s30n_8", job_url="https://company.com/jobs/8")
        db.save_vacancy(vac_8)
        set_application_status(vac_8.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_8.stable_id(), status=ReviewStatus.APPROVED))
        save_queue_item(QueueItem(
            vacancy_stable_id=vac_8.stable_id(),
            canonical_id=vac_8.stable_id(),
            representative_vacancy_stable_id=vac_8.stable_id(),
            priority_score=90,
            rank=1,
            match_score=90.0,
            deep_score=90.0,
        ))
        db.save_application_package(vac_8.stable_id(), "v1", '{"cover_letter": "Hi", "validation_status": "VALID"}')

        mock_8 = MockBrowserAdapter(simulate={"apply_button": True, "fields": ["name", "email"]})
        unconfirmed = submit_application_in_browser(vac_8.stable_id(), confirm_submit=False, adapter=mock_8)
        assert unconfirmed.status == "BLOCKED"
        assert "confirmation required" in str(unconfirmed.error).lower()

    finally:
        teardown_test_db(tmp_dir)


def test_stage30o_queue_to_apply_orchestrator_integration_suite():
    """Stage 30O: Full lifecycle from application queue to browser apply decision, safety gates, and post-submit state machine."""
    from ai_assistant.browser_executor import (
        classify_apply_flow,
        submit_application_in_browser,
        prepare_application_in_browser,
        audit_apply_flow_for_vacancy,
        MockBrowserAdapter,
        FlowType,
    )
    from ai_assistant.submission_verifier import (
        verify_submission,
        VerificationStatus,
    )
    from ai_assistant.application_tracking import (
        get_application_status,
        set_application_status,
        ApplicationStatus,
    )
    from ai_assistant.application_review import ApplicationReview, ReviewStatus, save_application_review
    from ai_assistant.application_queue import QueueItem, save_queue_item, get_queue_item, list_queue

    tmp_dir = setup_test_db()
    try:
        # 1. Vacancy Eligibility & Queue Integrity
        vac_e1 = _vac(source_job_id="s30o_e1", job_url="https://company.com/jobs/e1")
        vac_e2 = _vac(source_job_id="s30o_e2", job_url="https://company.com/jobs/e2")
        db.save_vacancy(vac_e1)
        db.save_vacancy(vac_e2)

        # e1 is READY_TO_APPLY, e2 is SUBMITTED
        set_application_status(vac_e1.stable_id(), ApplicationStatus.READY_TO_APPLY)
        set_application_status(vac_e2.stable_id(), ApplicationStatus.SUBMITTED)
        db.save_submission(vac_e2.stable_id(), "sub_e2", "SUBMITTED", "raw")

        item1 = QueueItem(
            vacancy_stable_id=vac_e1.stable_id(),
            canonical_id=vac_e1.stable_id(),
            representative_vacancy_stable_id=vac_e1.stable_id(),
            priority_score=95,
            rank=1,
            match_score=92.0,
            deep_score=94.0,
        )
        save_queue_item(item1)

        # Queue contains item1
        items = list_queue()
        assert len(items) >= 1
        assert any(it.vacancy_stable_id == vac_e1.stable_id() for it in items)
        assert get_queue_item(vac_e1.stable_id()) is not None

        # 2. Review Gate
        # PENDING_REVIEW -> blocked
        vac_rev_req = _vac(source_job_id="s30o_rev_req", job_url="https://company.com/jobs/rr")
        db.save_vacancy(vac_rev_req)
        set_application_status(vac_rev_req.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_rev_req.stable_id(), status=ReviewStatus.PENDING_REVIEW))
        db.save_application_package(vac_rev_req.stable_id(), "v1", '{"cover_letter": "Hi", "validation_status": "VALID"}')
        
        mock_adapter = MockBrowserAdapter(simulate={"apply_button": True, "fields": ["name", "email"]})
        res_rr = submit_application_in_browser(vac_rev_req.stable_id(), confirm_submit=True, adapter=mock_adapter)
        assert res_rr.status.value == "BLOCKED"
        assert "review" in str(res_rr.error).lower() or "approved" in str(res_rr.error).lower()

        # REJECTED -> blocked
        vac_rej = _vac(source_job_id="s30o_rej", job_url="https://company.com/jobs/rej")
        db.save_vacancy(vac_rej)
        set_application_status(vac_rej.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_rej.stable_id(), status=ReviewStatus.REJECTED))
        db.save_application_package(vac_rej.stable_id(), "v1", '{"cover_letter": "Hi", "validation_status": "VALID"}')
        res_rej = submit_application_in_browser(vac_rej.stable_id(), confirm_submit=True, adapter=mock_adapter)
        assert res_rej.status.value == "BLOCKED"

        # 3. Package Gate
        # Missing package -> prepare raises ValueError (cannot proceed)
        vac_no_pkg = _vac(source_job_id="s30o_no_pkg", job_url="https://company.com/jobs/np")
        db.save_vacancy(vac_no_pkg)
        set_application_status(vac_no_pkg.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_no_pkg.stable_id(), status=ReviewStatus.APPROVED))
        save_queue_item(QueueItem(vacancy_stable_id=vac_no_pkg.stable_id(), canonical_id=vac_no_pkg.stable_id(), representative_vacancy_stable_id=vac_no_pkg.stable_id(), priority_score=90, rank=1))
        with pytest.raises(ValueError, match="package"):
            prepare_application_in_browser(vac_no_pkg.stable_id(), adapter=mock_adapter, force=True)

        # Invalid package -> prepare does not allow READY_FOR_REVIEW
        vac_inv_pkg = _vac(source_job_id="s30o_inv_pkg", job_url="https://company.com/jobs/ip")
        db.save_vacancy(vac_inv_pkg)
        set_application_status(vac_inv_pkg.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_inv_pkg.stable_id(), status=ReviewStatus.APPROVED))
        save_queue_item(QueueItem(vacancy_stable_id=vac_inv_pkg.stable_id(), canonical_id=vac_inv_pkg.stable_id(), representative_vacancy_stable_id=vac_inv_pkg.stable_id(), priority_score=90, rank=2))
        db.save_application_package(vac_inv_pkg.stable_id(), "v1", '{"cover_letter": "Hi", "validation_status": "INVALID"}')
        prep_ip = prepare_application_in_browser(vac_inv_pkg.stable_id(), adapter=mock_adapter, force=True)
        assert prep_ip.status.value != "READY_FOR_REVIEW"

        # 4. Failure Recovery & Error Safety
        # Browser failure / network error / timeout does NOT set APPLIED
        vac_err = _vac(source_job_id="s30o_err", job_url="https://company.com/jobs/err")
        db.save_vacancy(vac_err)
        set_application_status(vac_err.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_err.stable_id(), status=ReviewStatus.APPROVED))
        save_queue_item(QueueItem(vacancy_stable_id=vac_err.stable_id(), canonical_id=vac_err.stable_id(), representative_vacancy_stable_id=vac_err.stable_id(), priority_score=90, rank=3))
        db.save_application_package(vac_err.stable_id(), "v1", '{"cover_letter": "Hi", "validation_status": "VALID"}')
        mock_err = MockBrowserAdapter(simulate={"network_error": True, "apply_button": False})
        res_err = submit_application_in_browser(vac_err.stable_id(), confirm_submit=True, adapter=mock_err)
        assert res_err.status.value in ("FAILED", "BLOCKED")
        assert get_application_status(vac_err.stable_id()).status != ApplicationStatus.APPLIED

        # 5. Full End-to-End Success via Mock Adapter
        vac_ok = _vac(source_job_id="s30o_ok", job_url="https://company.com/jobs/ok")
        db.save_vacancy(vac_ok)
        set_application_status(vac_ok.stable_id(), ApplicationStatus.READY_TO_APPLY)
        save_application_review(ApplicationReview(vacancy_stable_id=vac_ok.stable_id(), status=ReviewStatus.APPROVED))
        save_queue_item(QueueItem(vacancy_stable_id=vac_ok.stable_id(), canonical_id=vac_ok.stable_id(), representative_vacancy_stable_id=vac_ok.stable_id(), priority_score=90, rank=4))
        db.save_application_package(vac_ok.stable_id(), "v1", '{"cover_letter": "Hi", "validation_status": "VALID"}')
        mock_ok = MockBrowserAdapter(simulate={
            "page_title": "Careers at Acme",
            "apply_button": True,
            "fields": ["name", "email", "resume", "cover_letter"],
            "final_url": "https://company.com/jobs/ok/thank-you",
            "content": "Thank you for applying! Your application has been received.",
        })
        # Prepare
        prep_ok = prepare_application_in_browser(vac_ok.stable_id(), adapter=mock_ok, force=True)
        assert prep_ok.status.value == "READY_FOR_REVIEW"

        # Submit with explicit confirmation
        sub_ok = submit_application_in_browser(vac_ok.stable_id(), confirm_submit=True, adapter=mock_ok, force=True)
        assert sub_ok.status.value == "SUBMITTED"
        assert db.is_submitted(vac_ok.stable_id()) is True

        # Verify
        ver_ok = verify_submission(vac_ok.stable_id(), sub_ok.submission_id, adapter=mock_ok)
        assert ver_ok.verification_status == VerificationStatus.VERIFIED
        assert get_application_status(vac_ok.stable_id()).status == ApplicationStatus.APPLIED

        # Idempotency check: duplicate submit blocked
        dup_sub = submit_application_in_browser(vac_ok.stable_id(), confirm_submit=True, adapter=mock_ok, force=True)
        assert dup_sub.status.value == "BLOCKED"
        assert "already submitted" in str(dup_sub.error).lower()

    finally:
        teardown_test_db(tmp_dir)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
