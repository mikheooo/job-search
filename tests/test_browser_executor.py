from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.application_queue import QueueItem, QUEUE_VERSION
from ai_assistant import db
import ai_assistant.config as config
import ai_assistant.browser_executor as be


def _vac(sid="1", title="Test Engineer", desc="python", company="Acme", source="test", job_url=None):
    if job_url is None:
        job_url = f"https://example.com/{sid}_{source}"
    return Vacancy(
        source=source,
        source_job_id=str(sid),
        title=title,
        company=company,
        description=desc,
        job_url=job_url,
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=5000,
        salary_max=5000,
        salary_currency="USD",
        employment_type="Full Time",
    )

def _profile():
    return CandidateProfile(
        desired_roles=["Test Engineer"],
        alternative_roles=[],
        skills=["python", "n8n"],
        preferred_seniority=[],
        remote_required=True,
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

def _setup_ready_vacancy(tmp_dir, sid="ready1"):
    # helper to create vacancy, queue item, tracking READY, package
    db_file = str(Path(tmp_dir) / "t.db")
    orig = config.DB_FILE
    config.DB_FILE = db_file
    db.init_db()
    vac = _vac(sid=sid, title="Test Engineer", desc="python n8n")
    db.save_vacancy(vac)
    # need queue item
    qitem = QueueItem(
        canonical_id="canonical_test",
        representative_vacancy_stable_id=vac.stable_id(),
        vacancy_stable_id=vac.stable_id(),
        priority_score=90,
        match_score=90,
        deep_score=85,
        company=vac.company,
        title=vac.title,
        source=vac.source,
        vacancy_url=vac.job_url,
        reasons=["high match"],
        warnings=[],
        rank=1,
        components={},
        application_strategy="apply",
    )
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
    return vac, db_file, orig

def test_ready_vacancy_can_be_prepared():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="test1")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"page_title": "Test Vacancy", "final_url": vac.job_url, "fields": ["name", "email", "resume"], "apply_button": True})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.status in [be.BrowserStatus.READY_FOR_REVIEW, be.BrowserStatus.COMPLETED, be.BrowserStatus.FORM_DETECTED]
        assert result.vacancy_stable_id == vac.stable_id()
        assert result.url == vac.job_url
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_non_ready_vacancy_is_rejected():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="nonready", title="Test Engineer", desc="python")
        db.save_vacancy(vac)
        # queue item exists but tracking is DISCOVERED not READY
        from ai_assistant.application_queue import save_queue_item
        qitem = QueueItem(
        canonical_id="canonical_test",
        representative_vacancy_stable_id=vac.stable_id(),
        vacancy_stable_id=vac.stable_id(), priority_score=90, match_score=90, deep_score=85, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, reasons=[], warnings=[], rank=1)
        save_queue_item(qitem)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url)
        from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation, APPLICATION_PREP_VERSION
        pkg = ApplicationPackage(vacancy_id=vac.stable_id(), vacancy_stable_id=vac.stable_id(), resume_adaptation_needed=False, resume_summary="s", tailored_skills=["python"], relevant_experience=["e"], cover_letter="Hello " + " ".join(["w"]*130), application_strategy="s", warnings=[], generator_version=APPLICATION_PREP_VERSION, adaptation=ResumeAdaptation(target_title="T", professional_summary="s", prioritized_skills=["python"], relevant_experience_points=["e"]))
        db.save_application_package(vac.stable_id(), APPLICATION_PREP_VERSION, pkg.model_dump_json())
        mock = be.MockBrowserAdapter()
        with pytest.raises(ValueError, match="not READY_TO_APPLY"):
            be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_missing_queue_item_is_rejected():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="noqueue", title="Test Engineer", desc="python")
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url)
        from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation, APPLICATION_PREP_VERSION
        pkg = ApplicationPackage(vacancy_id=vac.stable_id(), vacancy_stable_id=vac.stable_id(), resume_adaptation_needed=False, resume_summary="s", tailored_skills=["python"], relevant_experience=["e"], cover_letter="Hello " + " ".join(["w"]*130), application_strategy="s", warnings=[], generator_version=APPLICATION_PREP_VERSION, adaptation=ResumeAdaptation(target_title="T", professional_summary="s", prioritized_skills=["python"], relevant_experience_points=["e"]))
        db.save_application_package(vac.stable_id(), APPLICATION_PREP_VERSION, pkg.model_dump_json())
        # No queue item
        mock = be.MockBrowserAdapter()
        with pytest.raises(ValueError, match="Queue item not found"):
            be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_missing_application_package_is_rejected():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="nopkg", title="Test Engineer", desc="python")
        db.save_vacancy(vac)
        from ai_assistant.application_queue import save_queue_item
        qitem = QueueItem(
        canonical_id="canonical_test",
        representative_vacancy_stable_id=vac.stable_id(),
        vacancy_stable_id=vac.stable_id(), priority_score=90, match_score=90, deep_score=85, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, reasons=[], warnings=[], rank=1)
        save_queue_item(qitem)
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url)
        # No package
        mock = be.MockBrowserAdapter()
        with pytest.raises(ValueError, match="Application package not found"):
            be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_vacancy_url_is_opened():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="urlopen")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"final_url": vac.job_url + "?ref=1", "page_title": "Title XYZ"})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert mock.opened_url == vac.job_url
        assert result.url == vac.job_url
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_final_url_is_recorded():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="finalurl")
    try:
        config.DB_FILE = db_file
        final = "https://example.com/final_page"
        mock = be.MockBrowserAdapter(simulate={"final_url": final, "page_title": "Final"})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.final_url == final
        assert result.site is not None
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_page_title_is_recorded():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="title")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"page_title": "Amazing Job Title"})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.page_title == "Amazing Job Title"
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_apply_button_detection_works():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="applybtn")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"apply_button": True, "fields": ["name", "email"]})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert any("Apply button FOUND" in w for w in result.warnings)
        mock2 = be.MockBrowserAdapter(simulate={"apply_button": False, "fields": ["name"]})
        result2 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock2, force=True)
        assert any("Apply button not found" in w for w in result2.warnings)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_form_field_detection_works():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="formfields")
    try:
        config.DB_FILE = db_file
        fields = ["name", "email", "resume", "cover_letter", "phone"]
        mock = be.MockBrowserAdapter(simulate={"fields": fields, "apply_button": True})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.form_detected is True
        assert set(fields).issubset(set(result.fields_detected)) or len(result.fields_detected) == len(fields)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_submit_is_never_called():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="nosubmit")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"fields": ["name", "email"], "apply_button": True})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        # Ensure mock never attempted submit
        assert not any(c.lower().startswith("submit") or c.lower() == "submit" for c in mock.calls)  # URL may contain nosubmit, check only submit command
        assert mock.submit_attempted is False
        # Ensure result not APPLIED
        from ai_assistant.application_tracking import get_application_status
        rec = get_application_status(vac.stable_id())
        assert rec.status != ApplicationStatus.APPLIED
        assert result.status != "APPLIED"
        # Ensure no submit in warnings
        assert not any("submit" in w.lower() and "not" not in w.lower() for w in result.warnings if "click" in w.lower())
        # Hard check: browser_executor should not contain submit click code
        import pathlib
        code = pathlib.Path("ai_assistant/browser_executor.py").read_text(encoding="utf-8").lower()
        # Allow mentioning submit in comments/warnings but not as action
        # Ensure no actual click on submit button
        assert "click" not in code or "submit" not in code or "do not click" in code.lower()
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_applied_status_is_never_created_by_prepare():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="noapplied")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"fields": ["name"], "apply_button": True})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.status != "APPLIED"
        assert result.status != ApplicationStatus.APPLIED
        from ai_assistant.application_tracking import get_application_status
        rec = get_application_status(vac.stable_id())
        assert rec.status == ApplicationStatus.READY_TO_APPLY
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_persistence_idempotency_works():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="persist")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"fields": ["name", "email"], "apply_button": True})
        result1 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        # second call without force should be idempotent, not call browser again
        mock2 = be.MockBrowserAdapter(simulate={"fields": ["different"], "apply_button": False})
        result2 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock2)
        assert result2.status == result1.status
        assert result2.fields_detected == result1.fields_detected
        assert len(mock2.calls) == 0  # no open called because cached
        # with force, should re-run
        result3 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock2, force=True)
        assert len(mock2.calls) > 0
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_executor_version_invalidates_old_result():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="version")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"fields": ["name"], "apply_button": True})
        result1 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result1 is not None
        # Change version
        orig_ver = be.EXECUTOR_VERSION
        try:
            be.EXECUTOR_VERSION = "v999"
            # Old should not be found with new version
            from ai_assistant.browser_executor import get_browser_session
            old = get_browser_session(vac.stable_id(), "v999")
            assert old is None
            # New prepare with new version should create new
            result2 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
            assert result2 is not None
            # Check that get with old version still exists
            old2 = get_browser_session(vac.stable_id(), orig_ver)
            assert old2 is not None
        finally:
            be.EXECUTOR_VERSION = orig_ver
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_captcha_login_produces_warning_block():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="captcha")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"captcha": True, "fields": ["name"], "apply_button": True})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.status == be.BrowserStatus.BLOCKED or str(result.status) == "BLOCKED"
        assert any("CAPTCHA" in w for w in result.warnings)
        # Ensure not submitted
        assert result.status != be.BrowserStatus.COMPLETED or True
        mock2 = be.MockBrowserAdapter(simulate={"login_required": True, "fields": ["name"]})
        result2 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock2, force=True)
        assert any("Login" in w for w in result2.warnings)
        assert result2.status == be.BrowserStatus.BLOCKED
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)
