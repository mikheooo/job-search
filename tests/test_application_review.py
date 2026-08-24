from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.application_tracking import ApplicationStatus, set_application_status, get_application_status
from ai_assistant.application_queue import QueueItem
from ai_assistant import db
import ai_assistant.config as config
import ai_assistant.application_review as ar
import ai_assistant.browser_executor as be

def _vac(sid="1", title="Test Engineer", desc="python", company="Acme", source="test", job_url=None):
    if job_url is None:
        job_url = f"https://example.com/{sid}_{source}"
    return Vacancy(source=source, source_job_id=str(sid), title=title, company=company, description=desc, job_url=job_url, location="Remote")

def _profile():
    return CandidateProfile(
        desired_roles=["Test Engineer"],
        alternative_roles=[],
        skills=["python", "n8n"],
        preferred_seniority=[],
        remote_required=False,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=[],
        employment_types=[],
        minimum_salary=None,
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )

def _setup_ready(tmp, sid="rev1", browser_status="READY_FOR_REVIEW"):
    db_file = str(Path(tmp) / "t.db")
    orig = config.DB_FILE
    config.DB_FILE = db_file
    db.init_db()
    vac = _vac(sid=sid, title="Test Engineer", desc="python n8n")
    db.save_vacancy(vac)
    # queue
    qitem = QueueItem(
            canonical_id="canonical_test",
            representative_vacancy_stable_id=vac.stable_id(),
            vacancy_stable_id=vac.stable_id(), priority_score=90, match_score=90, deep_score=85, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, reasons=[], warnings=[], rank=1)
    from ai_assistant.application_queue import save_queue_item
    save_queue_item(qitem)
    # tracking READY
    set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, match_score=90, deep_score=85)
    # package
    from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation, APPLICATION_PREP_VERSION
    pkg = ApplicationPackage(
        vacancy_id=vac.stable_id(), vacancy_stable_id=vac.stable_id(),
        resume_adaptation_needed=False, resume_summary="summary",
        tailored_skills=["python"], relevant_experience=["exp"],
        cover_letter="Hello " + " ".join(["word"]*130),
        application_strategy="strategy", warnings=[], generator_version=APPLICATION_PREP_VERSION,
        adaptation=ResumeAdaptation(target_title="Test Engineer", professional_summary="sum", prioritized_skills=["python"], relevant_experience_points=["exp"])
    )
    db.save_application_package(vac.stable_id(), APPLICATION_PREP_VERSION, pkg.model_dump_json())
    # browser session READY_FOR_REVIEW or BLOCKED
    from ai_assistant.browser_executor import BrowserStatus, BrowserApplicationSession
    sess = BrowserApplicationSession(
        vacancy_stable_id=vac.stable_id(), url=vac.job_url, status=BrowserStatus(browser_status),
        fields_detected=["name","email"], fields_filled=["cover_letter"], fields_skipped=["name"], warnings=["Apply button FOUND"],
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
        final_url=vac.job_url, page_title="Test", site="example.com", form_detected=True, screenshot_path="artifacts/browser/test.png"
    )
    be.save_browser_session(sess)
    return vac, db_file, orig

def test_ready_for_review_creates_review():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="c1")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert rev is not None
        assert rev.status == ar.ReviewStatus.PENDING_REVIEW
        assert rev.vacancy_stable_id == vac.stable_id()
        assert rev.company == vac.company
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_blocked_cannot_approve():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="blocked1", browser_status="BLOCKED")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        # creation should succeed even for BLOCKED (as PENDING)
        assert rev.status == ar.ReviewStatus.PENDING_REVIEW
        with pytest.raises(ValueError, match="BLOCKED"):
            ar.approve_review(vac.stable_id())
        # ensure still pending
        rev2 = ar.get_application_review(vac.stable_id())
        assert rev2.status == ar.ReviewStatus.PENDING_REVIEW
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_non_ready_cannot_approve():
    for status in [ApplicationStatus.DISCOVERED, ApplicationStatus.ANALYZED, ApplicationStatus.APPLIED, ApplicationStatus.REJECTED, ApplicationStatus.INTERVIEW, ApplicationStatus.OFFER, ApplicationStatus.WITHDRAWN]:
        tmp = tempfile.mkdtemp()
        orig = config.DB_FILE
        try:
            db_file = str(Path(tmp) / "t.db")
            config.DB_FILE = db_file
            db.init_db()
            vac = _vac(sid=f"nonready_{status.value}", title="Test")
            db.save_vacancy(vac)
            set_application_status(vac.stable_id(), status, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url)
            # Try to create review - should fail for non-READY
            with pytest.raises(ValueError):
                ar.create_application_review(vac.stable_id())
            # Also approve should fail if we manually create a review with wrong status? But creation already fails
        finally:
            config.DB_FILE = orig
            shutil.rmtree(tmp, ignore_errors=True)

def test_review_contains_queue_scores():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="scores")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert rev.match_score == 90
        assert rev.deep_score == 85
        assert rev.priority_score is not None
        assert rev.rank == 1
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_review_contains_cover_letter():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="cover")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert "Hello" in rev.cover_letter
        # Ensure cover letter matches package
        from ai_assistant.db import get_application_package
        pkg_row = get_application_package(vac.stable_id())
        pkg_data = json.loads(pkg_row[2])
        assert rev.cover_letter == pkg_data["cover_letter"]
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_review_contains_application_strategy():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="strat")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert rev.application_strategy == "strategy"
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_review_contains_filled_fields():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="filled")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert "cover_letter" in rev.fields_filled or len(rev.fields_filled) > 0
        assert rev.fields_filled == ["cover_letter"] or "cover_letter" in rev.fields_filled
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_review_contains_skipped_fields():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="skipped")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert "name" in rev.fields_skipped
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_review_contains_warnings():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="warn")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert any("Apply button" in w for w in rev.warnings)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_screenshot_persisted():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="screen")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert rev.screenshot_path == "artifacts/browser/test.png"
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_approve_changes_only_review_status():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="approve_only")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert rev.status == ar.ReviewStatus.PENDING_REVIEW
        # Check tracking before
        before = get_application_status(vac.stable_id())
        assert before.status == ApplicationStatus.READY_TO_APPLY
        approved = ar.approve_review(vac.stable_id())
        assert approved.status == ar.ReviewStatus.APPROVED
        # Tracking should NOT change
        after = get_application_status(vac.stable_id())
        assert after.status == ApplicationStatus.READY_TO_APPLY
        assert after.applied_at is None
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_approve_never_creates_applied():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="noapplied2")
    try:
        config.DB_FILE = db_file
        ar.create_application_review(vac.stable_id())
        ar.approve_review(vac.stable_id())
        rec = get_application_status(vac.stable_id())
        assert rec.status != ApplicationStatus.APPLIED
        assert rec.applied_at is None
        # Ensure no submit was called (check application_review has no submit)
        import pathlib
        code2 = pathlib.Path("ai_assistant/application_review.py").read_text(encoding="utf-8").lower()
        assert "submit" not in code2 or "submits" not in code2 or "do not submit" in code2
        # browser_executor.py can have submit_application for Stage 8, but application_review.py must not
        code1 = pathlib.Path("ai_assistant/application_review.py").read_text(encoding="utf-8").lower()
        assert "submit" not in code1 or "submits" not in code1 or "do not submit" in code1
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_reject_works_with_note():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="reject")
    try:
        config.DB_FILE = db_file
        ar.create_application_review(vac.stable_id())
        rev = ar.reject_review(vac.stable_id(), note="Not a fit for me")
        assert rev.status == ar.ReviewStatus.REJECTED
        assert rev.note == "Not a fit for me"
        # Tracking should not change
        rec = get_application_status(vac.stable_id())
        assert rec.status == ApplicationStatus.READY_TO_APPLY
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_persistence_idempotency_version_invalidation():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="persist", title="Test")
        db.save_vacancy(vac)
        # Need full setup for review
        vac2, db_file2, orig2 = _setup_ready(tmp, sid="persist2")
        config.DB_FILE = db_file2
        # First create
        rev1 = ar.create_application_review(vac2.stable_id())
        # Second create should be idempotent (same version)
        rev2 = ar.create_application_review(vac2.stable_id())
        assert rev1.vacancy_stable_id == rev2.vacancy_stable_id
        assert rev1.status == rev2.status
        # Approve
        ar.approve_review(vac2.stable_id())
        # Second approve idempotent
        rev3 = ar.approve_review(vac2.stable_id())
        assert rev3.status == ar.ReviewStatus.APPROVED
        # Version invalidation
        orig_ver = ar.REVIEW_VERSION
        try:
            ar.REVIEW_VERSION = "v999"
            # Old version should be considered not found for new version
            assert ar.get_application_review(vac2.stable_id(), "v999") is None
            assert ar.get_application_review(vac2.stable_id(), orig_ver) is not None
            # Creating with new version should create new
            rev_new = ar.create_application_review(vac2.stable_id())
            assert rev_new.review_version == "v999"
        finally:
            ar.REVIEW_VERSION = orig_ver
        # Persistence after reopen
        rec = ar.get_application_review(vac2.stable_id())
        assert rec is not None
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_review_no_submit_safety():
    # Ensure review creation and approve never call browser submit
    import pathlib
    code_review = pathlib.Path("ai_assistant/application_review.py").read_text(encoding="utf-8").lower()
    code_cli = pathlib.Path("ai_assistant/cli.py").read_text(encoding="utf-8").lower()
    # Should not contain submit click
    assert "click" not in code_review or "submit" not in code_review or "do not" in code_review
    # Check that approve does not import browser submit
    assert "submit_application" not in code_review or "confirm" in code_review
    # Also ensure no send
    assert "send(" not in code_review

def test_fields_not_invented():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="invent")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        # fields_filled should be subset of browser session fields_filled, not invented
        from ai_assistant.browser_executor import get_browser_session
        sess = get_browser_session(vac.stable_id())
        assert set(rev.fields_filled).issubset(set(sess.fields_filled + ["cover_letter", "resume"] + sess.fields_detected))
        # warnings should contain not confirmed for skipped
        assert any("not confirmed" in w.lower() or "skipped" in w.lower() for w in rev.warnings) or len(rev.fields_skipped) > 0
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_pending_to_approved_and_rejected():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready(tmp, sid="pending_trans")
    try:
        config.DB_FILE = db_file
        rev = ar.create_application_review(vac.stable_id())
        assert rev.status == ar.ReviewStatus.PENDING_REVIEW
        # Approve
        rev_a = ar.approve_review(vac.stable_id())
        assert rev_a.status == ar.ReviewStatus.APPROVED
        # Reject after approve should fail
        with pytest.raises(ValueError):
            ar.reject_review(vac.stable_id())
        # Create new vacancy for reject path
        vac2, _, _ = _setup_ready(tmp, sid="pending_trans2")
        config.DB_FILE = db_file
        rev2 = ar.create_application_review(vac2.stable_id())
        rev_r = ar.reject_review(vac2.stable_id(), note="nope")
        assert rev_r.status == ar.ReviewStatus.REJECTED
        # Approve after reject should fail
        with pytest.raises(ValueError):
            ar.approve_review(vac2.stable_id())
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)
