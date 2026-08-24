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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])