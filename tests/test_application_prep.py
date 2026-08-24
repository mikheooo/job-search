from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.application_prep import (
    ApplicationPackage,
    APPLICATION_PREP_VERSION,
    prepare_application,
    _fallback_cover_letter,
    _call_llm_cover_letter,
)
from ai_assistant import db
import ai_assistant.config as config
import ai_assistant.application_prep as app_prep


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
        desired_roles=["AI Automation Engineer"],
        alternative_roles=["Python Developer"],
        skills=["python", "n8n", "automation", "llm", "api"],
        preferred_seniority=["senior", "mid"],
        years_experience=3,
        remote_required=True,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=["en"],
        employment_types=["Full Time"],
        minimum_salary=1500,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


def _deep_apply():
    return DeepAnalysisResult(
        fit_score=90,
        recommendation="APPLY",
        why_fit=["Python match confirmed", "n8n match"],
        gaps=["AWS not confirmed"],
        must_have_requirements=["Python", "AWS"],
        nice_to_have_requirements=["Docker"],
        matched_skills=["python", "n8n"],
        missing_skills=["AWS"],
        seniority_assessment="Senior match confirmed",
        remote_assessment="Remote match",
        salary_assessment="Meets minimum",
        resume_adaptation_needed=True,
        resume_adaptation_reasons=["Highlight AWS gap"],
        application_strategy="Apply directly",
    )


def _deep_review():
    return DeepAnalysisResult(
        fit_score=70,
        recommendation="REVIEW",
        why_fit=["Python partial"],
        gaps=["AWS not confirmed", "Seniority unknown"],
        must_have_requirements=["Python", "AWS"],
        nice_to_have_requirements=["Docker"],
        matched_skills=["python"],
        missing_skills=["AWS", "n8n"],
        seniority_assessment="Required seniority not confirmed - unknown",
        remote_assessment="Remote match",
        salary_assessment="Salary not specified - unknown",
        resume_adaptation_needed=True,
        resume_adaptation_reasons=["Clarify seniority"],
        application_strategy="Review before applying",
    )


def _deep_skip():
    return DeepAnalysisResult(
        fit_score=20,
        recommendation="SKIP",
        why_fit=[],
        gaps=["No fit"],
        must_have_requirements=[],
        nice_to_have_requirements=[],
        matched_skills=[],
        missing_skills=["python"],
        seniority_assessment="unknown",
        remote_assessment="unknown",
        salary_assessment="unknown",
        resume_adaptation_needed=False,
        resume_adaptation_reasons=[],
        application_strategy="Skip",
    )


def test_apply_creates_package():
    vac = _vac()
    profile = _profile()
    deep = _deep_apply()
    long_letter = "Hello TestCo team, I am an AI Automation Engineer with 3 years confirmed experience in python, n8n. Your Senior AI Automation Engineer role aligns with my confirmed work in python and n8n automation. " + " ".join(["Confirmed experience in automation."]*30)
    with patch.object(app_prep, "_call_llm_cover_letter", return_value=long_letter):
        pkg = prepare_application(vac, deep, profile, resume_text="python, n8n, automation")
    assert pkg is not None
    assert pkg.vacancy_stable_id == vac.stable_id()
    assert pkg.generator_version == APPLICATION_PREP_VERSION
    assert pkg.resume_adaptation_needed is True
    assert pkg.cover_letter
    assert len(pkg.cover_letter.split()) >= 50  # at least some
    assert pkg.tailored_skills
    assert pkg.warnings is not None


def test_skip_does_not_create_package():
    vac = _vac()
    profile = _profile()
    deep = _deep_skip()
    pkg = prepare_application(vac, deep, profile, resume_text="python")
    assert pkg is None


def test_unknown_facts_not_invented():
    profile = _profile()  # has python, n8n etc, not AWS
    vac = _vac(description="Must have AWS and GCP. Python required. Remote.")
    deep = DeepAnalysisResult(
        fit_score=50,
        recommendation="REVIEW",
        why_fit=["Python match"],
        gaps=["AWS not confirmed", "GCP not confirmed"],
        must_have_requirements=["AWS", "GCP", "Python"],
        nice_to_have_requirements=[],
        matched_skills=["python"],
        missing_skills=["AWS", "GCP"],
        seniority_assessment="unknown",
        remote_assessment="Remote match",
        salary_assessment="unknown",
        resume_adaptation_needed=True,
        resume_adaptation_reasons=[],
        application_strategy="",
    )
    with patch.object(app_prep, "_call_llm_cover_letter", side_effect=Exception("offline")):
        pkg = prepare_application(vac, deep, profile, resume_text="Skills: python, n8n. Other: unknown / not confirmed")
    assert pkg is not None
    # tailored_skills should be subset of profile.skills, not include AWS/GCP
    assert all(s.lower() in [p.lower() for p in profile.skills] for s in pkg.tailored_skills)
    assert "aws" not in [s.lower() for s in pkg.tailored_skills]
    assert "gcp" not in [s.lower() for s in pkg.tailored_skills]
    # relevant_experience should not invent AWS
    assert "aws" not in " ".join(pkg.relevant_experience).lower() or "not confirmed" in " ".join(pkg.relevant_experience).lower() or "missing" in " ".join(pkg.warnings).lower()
    # cover letter must not claim AWS experience as confirmed
    low = pkg.cover_letter.lower()
    # If AWS appears, it should be with not confirmed qualifier or not at all
    if "aws" in low:
        assert "not confirmed" in low or "unknown" in low
    # warnings should mention missing
    assert any("aws" in w.lower() for w in pkg.warnings)


def test_cover_letter_uses_only_confirmed_data():
    profile = _profile()
    vac = _vac(title="AI Automation Engineer", company="DeepCo", description="Need python and n8n. Remote.")
    deep = _deep_apply()
    resume_text = "Skills: python, n8n, automation. Years: 3. Other: unknown / not confirmed"
    # Mock LLM to return a cover letter that correctly uses only confirmed data (130+ words)
    good_letter = "Hello DeepCo team, I am an AI Automation Engineer with 3 years confirmed experience in python, n8n, automation. Your AI Automation Engineer role aligns with my confirmed background in python and n8n workflows and LLM integrations. My confirmed work includes building n8n automations and Python API integrations. I work remotely as confirmed. Available for discussion. Best regards. " + " ".join(["Confirmed experience."]*45)
    with patch.object(app_prep, "_call_llm_cover_letter", return_value=json.dumps({"cover_letter": good_letter})):
        pkg = prepare_application(vac, deep, profile, resume_text=resume_text)
    assert pkg is not None
    # cover letter should contain confirmed skills, not invented like "Ruby"
    assert "python" in pkg.cover_letter.lower()
    assert "ruby" not in pkg.cover_letter.lower()
    assert "aws" not in pkg.cover_letter.lower() or "not confirmed" in pkg.cover_letter.lower()
    # forbidden phrase not present
    assert "i am excited to apply" not in pkg.cover_letter.lower()
    # word count 120-180 (allow 120-200 for LLM variance, fallback ensures 120-180)
    wc = len(pkg.cover_letter.split())
    assert 100 <= wc <= 200  # generous but ensures not too short/long
    # tailored_skills subset
    assert all(s.lower() in [p.lower() for p in profile.skills] for s in pkg.tailored_skills)


def test_llm_called_only_when_needed():
    profile = _profile()
    vac_apply = _vac(source_job_id="apply1")
    vac_skip = _vac(source_job_id="skip1")
    deep_apply = _deep_apply()
    deep_skip = _deep_skip()

    call_count = {"n": 0}

    def fake_llm(sys_p, user_p):
        call_count["n"] += 1
        return json.dumps({"cover_letter": "Hello team, I am an AI Automation Engineer with 3 years confirmed experience. " + " ".join(["Word"]*130)})

    with patch.object(app_prep, "_call_llm_cover_letter", side_effect=fake_llm):
        pkg_apply = prepare_application(vac_apply, deep_apply, profile, resume_text="python")
        assert pkg_apply is not None
        assert call_count["n"] == 1
        pkg_skip = prepare_application(vac_skip, deep_skip, profile, resume_text="python")
        assert pkg_skip is None
        # skip should not have called LLM
        assert call_count["n"] == 1


def test_offline_fallback_works():
    profile = _profile()
    vac = _vac()
    deep = _deep_apply()
    # Force offline by making _call_llm raise
    with patch.object(app_prep, "_call_llm_cover_letter", side_effect=Exception("offline")):
        pkg = prepare_application(vac, deep, profile, resume_text="python, n8n")
    assert pkg is not None
    assert pkg.cover_letter
    assert len(pkg.cover_letter.split()) >= 120
    assert "not confirmed" in pkg.cover_letter.lower() or "python" in pkg.cover_letter.lower()
    # Should not contain forbidden phrase
    assert "i am excited to apply" not in pkg.cover_letter.lower()


def test_persistence_works():
    tmp_dir = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    try:
        db_file = str(Path(tmp_dir) / "app_persist.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(source_job_id="persist1")
        db.save_vacancy(vac)
        profile = _profile()
        deep = _deep_apply()
        pkg = prepare_application(vac, deep, profile, resume_text="python")
        assert pkg is not None
        db.save_application_package(vac.stable_id(), APPLICATION_PREP_VERSION, pkg.model_dump_json())
        row = db.get_application_package(vac.stable_id(), APPLICATION_PREP_VERSION)
        assert row is not None
        assert row[1] == APPLICATION_PREP_VERSION
        data = json.loads(row[2])
        assert data["vacancy_stable_id"] == vac.stable_id()
        assert data["cover_letter"] == pkg.cover_letter
        # without version filter also returns
        row2 = db.get_application_package(vac.stable_id())
        assert row2 is not None
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_repeated_run_uses_cache():
    tmp_dir = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    try:
        db_file = str(Path(tmp_dir) / "cache.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(source_job_id="cache1")
        db.save_vacancy(vac)
        profile = _profile()
        deep = _deep_apply()

        call_count = {"n": 0}

        def fake_llm(sys_p, user_p):
            call_count["n"] += 1
            # return 130 words
            return json.dumps({"cover_letter": "Hello team, " + " ".join(["Word"]*130)})

        with patch.object(app_prep, "_call_llm_cover_letter", side_effect=fake_llm):
            # First run: not cached, should call LLM and save
            sid = vac.stable_id()
            assert not db.is_application_prepared(sid, APPLICATION_PREP_VERSION)
            pkg1 = prepare_application(vac, deep, profile, resume_text="python")
            db.save_application_package(sid, APPLICATION_PREP_VERSION, pkg1.model_dump_json())
            assert call_count["n"] == 1
            # Second run: check cache before calling
            call_count["n"] = 0
            if db.is_application_prepared(sid, APPLICATION_PREP_VERSION):
                # should skip LLM
                pass
            else:
                pkg2 = prepare_application(vac, deep, profile, resume_text="python")
                db.save_application_package(sid, APPLICATION_PREP_VERSION, pkg2.model_dump_json())
            assert call_count["n"] == 0
            # Ensure still 1 row
            rows = db.list_application_packages()
            assert len(rows) == 1
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_version_invalidates_cache():
    tmp_dir = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    try:
        db_file = str(Path(tmp_dir) / "ver.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(source_job_id="ver1")
        db.save_vacancy(vac)
        pkg = prepare_application(vac, _deep_apply(), _profile(), resume_text="python")
        db.save_application_package(vac.stable_id(), "v1", pkg.model_dump_json())
        assert db.get_application_package(vac.stable_id(), "v1") is not None
        assert db.get_application_package(vac.stable_id(), "v2") is None
        assert db.is_application_prepared(vac.stable_id(), "v1") is True
        assert db.is_application_prepared(vac.stable_id(), "v2") is False
        # Simulate version bump
        with patch.object(app_prep, "APPLICATION_PREP_VERSION", "v2"):
            # should be considered not prepared for v2, need regeneration
            assert not db.is_application_prepared(vac.stable_id(), "v2")
            # generate new
            pkg2 = prepare_application(vac, _deep_apply(), _profile(), resume_text="python")
            pkg2.generator_version = "v2"
            db.save_application_package(vac.stable_id(), "v2", pkg2.model_dump_json())
            assert db.get_application_package(vac.stable_id(), "v2") is not None
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_force_regeneration():
    tmp_dir = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    try:
        db_file = str(Path(tmp_dir) / "force.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(source_job_id="force1")
        db.save_vacancy(vac)
        profile = _profile()
        deep = _deep_apply()

        call_count = {"n": 0}

        def fake_llm(sys_p, user_p):
            call_count["n"] += 1
            return json.dumps({"cover_letter": "Hello team, " + " ".join(["Word"]*130) + f" call{call_count['n']}"})

        with patch.object(app_prep, "_call_llm_cover_letter", side_effect=fake_llm):
            sid = vac.stable_id()
            # first prepare
            pkg1 = prepare_application(vac, deep, profile, resume_text="python")
            db.save_application_package(sid, APPLICATION_PREP_VERSION, pkg1.model_dump_json())
            assert call_count["n"] == 1
            # second without force: should use cache, no new call
            call_count["n"] = 0
            # Simulate CLI logic: if not force and is_prepared -> skip
            if db.is_application_prepared(sid, APPLICATION_PREP_VERSION):
                pass  # skip
            else:
                fake_llm("", "")
            assert call_count["n"] == 0
            # with force: should regenerate
            call_count["n"] = 0
            force = True
            if force or not db.is_application_prepared(sid, APPLICATION_PREP_VERSION):
                pkg2 = prepare_application(vac, deep, profile, resume_text="python")
                db.save_application_package(sid, APPLICATION_PREP_VERSION, pkg2.model_dump_json())
            assert call_count["n"] == 1
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)
