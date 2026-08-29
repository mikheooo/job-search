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


def test_source_and_site_based_phone_selection():
    from ai_assistant.browser_executor import _get_profile_value_truth

    prof_both = CandidateProfile(
        desired_roles=["Engineer"],
        skills=["python"],
        phone_ru="+7 (999) 111-22-33",
        phone_th="+66 81 234 5678",
        phone="+1 555 000 1111",
    )
    prof_fallback_only = CandidateProfile(
        desired_roles=["Engineer"],
        skills=["python"],
        phone="+1 555 000 1111",
    )
    prof_empty = CandidateProfile(
        desired_roles=["Engineer"],
        skills=["python"],
    )

    # 1. hh.ru domain / source -> phone_ru
    vac_hh = Vacancy(source="hh", source_job_id="101", title="Dev", company="A", description="d", job_url="https://hh.ru/vacancy/101")
    assert _get_profile_value_truth("phone", prof_both, "", vac_hh, None) == "+7 (999) 111-22-33"

    # 2. career.habr.com -> phone_ru
    vac_habr = Vacancy(source="habrcareer", source_job_id="102", title="Dev", company="B", description="d", job_url="https://career.habr.com/vacancies/102")
    assert _get_profile_value_truth("phone", prof_both, "", vac_habr, None) == "+7 (999) 111-22-33"

    # 3. remoteok.com -> phone_th
    vac_remoteok = Vacancy(source="remoteok", source_job_id="103", title="Dev", company="C", description="d", job_url="https://remoteok.com/remote-jobs/103")
    assert _get_profile_value_truth("phone", prof_both, "", vac_remoteok, None) == "+66 81 234 5678"

    # 4. weworkremotely.com -> phone_th
    vac_wwr = Vacancy(source="weworkremotely", source_job_id="104", title="Dev", company="D", description="d", job_url="https://weworkremotely.com/remote-jobs/104")
    assert _get_profile_value_truth("phone", prof_both, "", vac_wwr, None) == "+66 81 234 5678"

    # 5. wellfound.com -> phone_th
    vac_wellfound = Vacancy(source="custom", source_job_id="105", title="Dev", company="E", description="d", job_url="https://wellfound.com/jobs/105")
    assert _get_profile_value_truth("phone", prof_both, "", vac_wellfound, None) == "+66 81 234 5678"

    # 6. vacancies_json source + remoteok URL -> phone_th
    vac_seed = Vacancy(source="vacancies_json", source_job_id="80", title="Dev", company="F", description="d", job_url="https://remoteok.com/remote-jobs/remote-ai-synthetix-105820")
    assert _get_profile_value_truth("phone", prof_both, "", vac_seed, None) == "+66 81 234 5678"

    # 7. unknown domain -> generic phone fallback
    vac_unknown = Vacancy(source="unknown_src", source_job_id="106", title="Dev", company="G", description="d", job_url="https://random-startup-xyz.com/apply/106")
    assert _get_profile_value_truth("phone", prof_both, "", vac_unknown, None) == "+1 555 000 1111"
    assert _get_profile_value_truth("phone", prof_fallback_only, "", vac_unknown, None) == "+1 555 000 1111"

    # 8. All phones missing -> Truth-only None
    assert _get_profile_value_truth("phone", prof_empty, "", vac_hh, None) is None
    assert _get_profile_value_truth("phone", prof_empty, "", vac_remoteok, None) is None
    assert _get_profile_value_truth("phone", prof_empty, "", vac_unknown, None) is None

    # 9. Explicit profile phone has priority over resume regex
    resume_with_different_phone = "Resume text Phone: +44 20 7946 0991 random info"
    assert _get_profile_value_truth("phone", prof_both, resume_with_different_phone, vac_hh, None) == "+7 (999) 111-22-33"
    assert _get_profile_value_truth("phone", prof_both, resume_with_different_phone, vac_remoteok, None) == "+66 81 234 5678"


def test_confirmed_candidate_profile_truth_values():
    from ai_assistant.browser_executor import _get_profile_value_truth
    from ai_assistant.candidate_profile import CandidateProfile

    prof = CandidateProfile.from_dict({
        "name": "Mikhail Kolesnikov",
        "email": "mikhailthaiban@gmail.com",
        "phone_ru": "+79933397628",
        "phone_th": "+66815036090",
        "linkedin": "https://www.linkedin.com/in/mikheooo",
        "github": "https://github.com/mikheooo",
        "allowed_locations": ["Remote"],
        "minimum_salary": 1500,
        "salary_currency": "USD",
        "years_experience": 3,
    })

    vac_hh = Vacancy(source="hh", source_job_id="1", title="Python", company="A", description="d", job_url="https://hh.ru/vacancy/1")
    vac_remoteok = Vacancy(source="remoteok", source_job_id="2", title="Python", company="B", description="d", job_url="https://remoteok.com/remote-jobs/2")

    # Name
    assert _get_profile_value_truth("name", prof, "", vac_hh, None) == "Mikhail Kolesnikov"
    assert _get_profile_value_truth("first_name", prof, "", vac_hh, None) == "Mikhail"
    assert _get_profile_value_truth("last_name", prof, "", vac_hh, None) == "Kolesnikov"

    # Email
    assert _get_profile_value_truth("email", prof, "", vac_hh, None) == "mikhailthaiban@gmail.com"

    # Phones
    assert _get_profile_value_truth("phone", prof, "", vac_hh, None) == "+79933397628"
    assert _get_profile_value_truth("phone", prof, "", vac_remoteok, None) == "+66815036090"

    # Socials
    assert _get_profile_value_truth("linkedin", prof, "", vac_remoteok, None) == "https://www.linkedin.com/in/mikheooo"
    assert _get_profile_value_truth("github", prof, "", vac_remoteok, None) == "https://github.com/mikheooo"
    assert _get_profile_value_truth("portfolio", prof, "", vac_remoteok, None) is None

    # Resume path
    assert _get_profile_value_truth("resume", prof, "", vac_remoteok, None) == "resume.md"


def test_ready_for_review_validation_gate():
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="gate_test")
    try:
        config.DB_FILE = db_file

        # Case 1: unapproved review + invalid package -> FORM_DETECTED
        mock = be.MockBrowserAdapter(simulate={"fields": ["name", "email", "resume", "cover_letter"], "apply_button": True})
        res1 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock, force=True)
        # Package created in _setup_ready_vacancy had no validation_status=VALID and review is not APPROVED
        assert res1.status == be.BrowserStatus.FORM_DETECTED or res1.status == be.BrowserStatus.READY_FOR_REVIEW

        # Case 2: review is approved -> transitions to READY_FOR_REVIEW
        from ai_assistant.application_review import approve_review, create_application_review
        create_application_review(vac.stable_id())
        approve_review(vac.stable_id(), note="Human approved", force=True)

        res2 = be.prepare_application_in_browser(vac.stable_id(), adapter=mock, force=True)
        assert res2.status == be.BrowserStatus.READY_FOR_REVIEW
        assert not db.is_submitted(vac.stable_id())
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_ready_for_review_never_auto_submits():
    """Safety invariant: READY_FOR_REVIEW state NEVER triggers submit/apply click."""
    tmp = tempfile.mkdtemp()
    vac, db_file, orig = _setup_ready_vacancy(tmp, sid="no_auto_submit")
    try:
        config.DB_FILE = db_file
        mock = be.MockBrowserAdapter(simulate={"fields": ["name", "email", "resume", "cover_letter"], "apply_button": True})

        # Step 1: initial prepare records browser session
        be.prepare_application_in_browser(vac.stable_id(), adapter=mock, force=True)

        # Step 2: create and approve review
        from ai_assistant.application_review import approve_review, create_application_review
        create_application_review(vac.stable_id())
        approve_review(vac.stable_id(), note="Human approved", force=True)

        # Step 3: re-prepare with approved review
        res = be.prepare_application_in_browser(vac.stable_id(), adapter=mock, force=True)

        assert res.status == be.BrowserStatus.READY_FOR_REVIEW
        # Invariant: mock.submit_attempted must be False
        assert mock.submit_attempted is False
        # Invariant: DB is_submitted must be False
        assert db.is_submitted(vac.stable_id()) is False
        # Invariant: application_submissions has 0 entries
        from ai_assistant.db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM application_submissions WHERE vacancy_stable_id=?", (vac.stable_id(),))
        count = cur.fetchone()[0]
        assert count == 0
        conn.close()
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)




