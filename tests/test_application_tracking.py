from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
    transition_application,
    list_applications,
    get_application_history,
    sync_application_tracking,
)
from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.job_analyzer import DeepAnalysisResult, ANALYZER_VERSION
from ai_assistant.application_prep import APPLICATION_PREP_VERSION


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

def _profile_for_sync():
    # Simple profile that will make _vac APPLY
    return CandidateProfile(
        desired_roles=["Test Engineer"],
        alternative_roles=[],
        skills=["python"],
        preferred_seniority=[],
        remote_required=False,
        allowed_locations=[],
        allowed_timezones=[],
        languages=[],
        employment_types=[],
        minimum_salary=None,
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )

def _write_temp_profile(tmp_dir: str, profile: CandidateProfile) -> str:
    p = Path(tmp_dir) / "profile.json"
    p.write_text(json.dumps(profile.to_dict()), encoding="utf-8")
    return str(p)

def test_new_vacancy_discovered():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        prof = _profile_for_sync()
        prof_path = _write_temp_profile(tmp, prof)
        vac = _vac(sid="disc1", title="Test Engineer", desc="python remote")
        db.save_vacancy(vac)
        # ensure deep/package not exist
        res = sync_application_tracking(profile_path=prof_path)
        assert res["Created"] == 1
        rec = get_application_status(vac.stable_id())
        assert rec is not None
        assert rec.status == ApplicationStatus.DISCOVERED
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_deep_analysis_promotes_to_analyzed():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        prof = _profile_for_sync()
        prof_path = _write_temp_profile(tmp, prof)
        vac = _vac(sid="analyzed1", title="Test Engineer", desc="python")
        db.save_vacancy(vac)
        sync_application_tracking(profile_path=prof_path)
        rec = get_application_status(vac.stable_id())
        assert rec.status == ApplicationStatus.DISCOVERED
        # add deep
        deep = DeepAnalysisResult(
            fit_score=80, recommendation="APPLY", why_fit=[], gaps=[],
            must_have_requirements=[], nice_to_have_requirements=[],
            matched_skills=["python"], missing_skills=[],
            seniority_assessment="", remote_assessment="", salary_assessment="",
            resume_adaptation_needed=False, resume_adaptation_reasons=[], application_strategy=""
        )
        db.save_deep_analysis(vac.stable_id(), ANALYZER_VERSION, deep.fit_score, deep.recommendation, deep.model_dump_json())
        res2 = sync_application_tracking(profile_path=prof_path)
        assert res2["Updated"] == 1
        rec2 = get_application_status(vac.stable_id())
        assert rec2.status == ApplicationStatus.ANALYZED
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_application_package_promotes_to_ready():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        prof = _profile_for_sync()
        prof_path = _write_temp_profile(tmp, prof)
        vac = _vac(sid="ready1", title="Test Engineer", desc="python")
        db.save_vacancy(vac)
        sync_application_tracking(profile_path=prof_path)
        # add deep
        deep = DeepAnalysisResult(
            fit_score=90, recommendation="APPLY", why_fit=[], gaps=[],
            must_have_requirements=[], nice_to_have_requirements=[],
            matched_skills=["python"], missing_skills=[],
            seniority_assessment="", remote_assessment="", salary_assessment="",
            resume_adaptation_needed=False, resume_adaptation_reasons=[], application_strategy=""
        )
        db.save_deep_analysis(vac.stable_id(), ANALYZER_VERSION, deep.fit_score, deep.recommendation, deep.model_dump_json())
        sync_application_tracking(profile_path=prof_path)
        assert get_application_status(vac.stable_id()).status == ApplicationStatus.ANALYZED
        # add package
        from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
        pkg = ApplicationPackage(
            vacancy_id=vac.stable_id(), vacancy_stable_id=vac.stable_id(),
            resume_adaptation_needed=False, resume_summary="summary",
            tailored_skills=["python"], relevant_experience=["exp"],
            cover_letter="Hello " + " ".join(["word"]*130),
            application_strategy="strategy", warnings=[], generator_version=APPLICATION_PREP_VERSION,
            adaptation=ResumeAdaptation(target_title="Test Engineer", professional_summary="sum", prioritized_skills=["python"], relevant_experience_points=["exp"])
        )
        db.save_application_package(vac.stable_id(), APPLICATION_PREP_VERSION, pkg.model_dump_json())
        res3 = sync_application_tracking(profile_path=prof_path)
        assert res3["Updated"] == 1
        rec3 = get_application_status(vac.stable_id())
        assert rec3.status == ApplicationStatus.READY_TO_APPLY
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_valid_transition():
        tmp = tempfile.mkdtemp()
        orig = config.DB_FILE
        try:
            db_file = str(Path(tmp) / "t.db")
            config.DB_FILE = db_file
            db.init_db()
            vac = _vac(sid="valid1")
            db.save_vacancy(vac)
            set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED, company=vac.company, title=vac.title)
            rec = transition_application(vac.stable_id(), ApplicationStatus.ANALYZED)
            assert rec.status == ApplicationStatus.ANALYZED
            rec2 = transition_application(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
            assert rec2.status == ApplicationStatus.READY_TO_APPLY
            # New flow: READY_TO_APPLY -> SUBMITTED -> VERIFIED -> APPLIED
            rec3 = transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
            assert rec3.status == ApplicationStatus.SUBMITTED
            rec4 = transition_application(vac.stable_id(), ApplicationStatus.VERIFIED)
            assert rec4.status == ApplicationStatus.VERIFIED
            rec5 = transition_application(vac.stable_id(), ApplicationStatus.APPLIED)
            assert rec5.status == ApplicationStatus.APPLIED
            assert rec5.applied_at is not None
        finally:
            config.DB_FILE = orig
            shutil.rmtree(tmp, ignore_errors=True)

def test_invalid_transition_rejected():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="invalid1")
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        # DISCOVERED -> APPLIED should still be invalid (must go through SUBMITTED/VERIFIED)
        with pytest.raises(ValueError):
            transition_application(vac.stable_id(), ApplicationStatus.APPLIED)
        with pytest.raises(ValueError):
            transition_application(vac.stable_id(), ApplicationStatus.OFFER)
        # also test invalid string
        with pytest.raises(ValueError):
            transition_application(vac.stable_id(), "INVALID_STATUS")
        # But READY_TO_APPLY -> SUBMITTED should be valid
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        rec = transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        assert rec.status == ApplicationStatus.SUBMITTED
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_applied_timestamp():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="applied_ts")
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        rec_before = get_application_status(vac.stable_id())
        assert rec_before.applied_at is None
        # New flow: READY_TO_APPLY -> SUBMITTED -> VERIFIED -> APPLIED
        rec_submitted = transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        assert rec_submitted.status == ApplicationStatus.SUBMITTED
        rec_verified = transition_application(vac.stable_id(), ApplicationStatus.VERIFIED)
        assert rec_verified.status == ApplicationStatus.VERIFIED
        rec = transition_application(vac.stable_id(), ApplicationStatus.APPLIED)
        assert rec.applied_at is not None
        assert rec.last_status_change_at is not None
        # applied_at should stay after further transitions? e.g., to INTERVIEW, applied_at remains
        rec2 = transition_application(vac.stable_id(), ApplicationStatus.INTERVIEW)
        assert rec2.applied_at == rec.applied_at
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_history_created():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="hist1")
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        h1 = get_application_history(vac.stable_id())
        assert len(h1) == 1
        assert h1[0].new_status == "DISCOVERED"
        transition_application(vac.stable_id(), ApplicationStatus.ANALYZED, note="deep done")
        h2 = get_application_history(vac.stable_id())
        assert len(h2) == 2
        assert h2[1].old_status == "DISCOVERED"
        assert h2[1].new_status == "ANALYZED"
        assert h2[1].note == "deep done"
        transition_application(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        h3 = get_application_history(vac.stable_id())
        assert len(h3) == 3
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_repeated_transition_is_idempotent():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="idem1")
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        h_before = len(get_application_history(vac.stable_id()))
        rec = transition_application(vac.stable_id(), ApplicationStatus.DISCOVERED)
        assert rec.status == ApplicationStatus.DISCOVERED
        h_after = get_application_history(vac.stable_id())
        assert len(h_after) == h_before  # no new history
        # also transition to ANALYZED twice
        transition_application(vac.stable_id(), ApplicationStatus.ANALYZED)
        hc = len(get_application_history(vac.stable_id()))
        transition_application(vac.stable_id(), ApplicationStatus.ANALYZED)
        assert len(get_application_history(vac.stable_id())) == hc
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_manual_applied_survives_sync():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        prof = _profile_for_sync()
        prof_path = _write_temp_profile(tmp, prof)
        vac = _vac(sid="manual_applied", title="Test Engineer", desc="python")
        db.save_vacancy(vac)
        # create DISCOVERED->ANALYZED->READY->SUBMITTED->VERIFIED->APPLIED manually
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        transition_application(vac.stable_id(), ApplicationStatus.ANALYZED)
        transition_application(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        transition_application(vac.stable_id(), ApplicationStatus.VERIFIED)
        transition_application(vac.stable_id(), ApplicationStatus.APPLIED)
        rec = get_application_status(vac.stable_id())
        assert rec.status == ApplicationStatus.APPLIED
        # sync should not downgrade or change
        res = sync_application_tracking(profile_path=prof_path)
        # should be unchanged, not updated
        rec2 = get_application_status(vac.stable_id())
        assert rec2.status == ApplicationStatus.APPLIED
        # Created 0, Updated 0, Unchanged at least 1
        assert res["Unchanged"] >= 1
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_rejected_survives_sync():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        prof = _profile_for_sync()
        prof_path = _write_temp_profile(tmp, prof)
        vac = _vac(sid="rejected1", title="Test Engineer", desc="python")
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED)
        transition_application(vac.stable_id(), ApplicationStatus.ANALYZED)
        transition_application(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        transition_application(vac.stable_id(), ApplicationStatus.SUBMITTED)
        transition_application(vac.stable_id(), ApplicationStatus.VERIFIED)
        transition_application(vac.stable_id(), ApplicationStatus.APPLIED)
        transition_application(vac.stable_id(), ApplicationStatus.REJECTED)
        rec = get_application_status(vac.stable_id())
        assert rec.status == ApplicationStatus.REJECTED
        res = sync_application_tracking(profile_path=prof_path)
        rec2 = get_application_status(vac.stable_id())
        assert rec2.status == ApplicationStatus.REJECTED
        assert res["Unchanged"] >= 1
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_list_filtering_by_status():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "t.db")
        config.DB_FILE = db_file
        db.init_db()
        vac1 = _vac(sid="list1", title="A")
        vac2 = _vac(sid="list2", title="B")
        vac3 = _vac(sid="list3", title="C")
        db.save_vacancy(vac1)
        db.save_vacancy(vac2)
        db.save_vacancy(vac3)
        set_application_status(vac1.stable_id(), ApplicationStatus.DISCOVERED)
        set_application_status(vac2.stable_id(), ApplicationStatus.APPLIED)
        set_application_status(vac3.stable_id(), ApplicationStatus.APPLIED)
        all_recs = list_applications(limit=10)
        assert len(all_recs) == 3
        applied = list_applications(status="APPLIED", limit=10)
        assert len(applied) == 2
        assert all(r.status == ApplicationStatus.APPLIED for r in applied)
        discovered = list_applications(status=ApplicationStatus.DISCOVERED, limit=10)
        assert len(discovered) == 1
        assert discovered[0].vacancy_stable_id == vac1.stable_id()
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_persistence_after_db_reopen():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "persist.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="persist", title="Persist Test")
        db.save_vacancy(vac)
        set_application_status(vac.stable_id(), ApplicationStatus.DISCOVERED, company="Acme", title="Persist Test", notes="note123")
        transition_application(vac.stable_id(), ApplicationStatus.ANALYZED)
        # Simulate reopen: close and reopen same file (just re-init and query)
        rec = get_application_status(vac.stable_id())
        hist = get_application_history(vac.stable_id())
        assert rec.status == ApplicationStatus.ANALYZED
        assert len(hist) == 2
        # Re-init should not lose data
        db.init_db()
        rec2 = get_application_status(vac.stable_id())
        assert rec2.status == ApplicationStatus.ANALYZED
        assert rec2.notes == "note123"
        hist2 = get_application_history(vac.stable_id())
        assert len(hist2) == 2
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)
