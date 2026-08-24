from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.matcher import JobMatcher, JobProfile
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult, ANALYZER_VERSION, analyze_job_deep, should_analyze, _build_system_prompt
from ai_assistant import db
import ai_assistant.config as config
import ai_assistant.job_analyzer as job_analyzer


def _vac(**kwargs):
    defaults = dict(
        source="test",
        source_job_id="1",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="TestCo",
        description="We need python, n8n, automation, LLM, API. Must have AWS. Nice to have Docker. Remote worldwide. Senior level. Salary $5k USD. English required.",
        job_url=None,  # will be auto-generated unique if not provided
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
        sid = str(defaults.get("source_job_id") or defaults.get("source") or "1")
        defaults["job_url"] = f"https://example.com/{sid}"
    return Vacancy(**defaults)


def _profile():
    return CandidateProfile(
        desired_roles=["AI Automation Engineer", "n8n Developer"],
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


def test_structured_result_validation():
    # valid
    valid = DeepAnalysisResult(
        fit_score=85,
        recommendation="APPLY",
        why_fit=["Python match"],
        gaps=["AWS not confirmed"],
        must_have_requirements=["Python", "AWS"],
        nice_to_have_requirements=["Docker"],
        matched_skills=["python"],
        missing_skills=["AWS"],
        seniority_assessment="Senior match",
        remote_assessment="Remote match",
        salary_assessment="Meets minimum",
        resume_adaptation_needed=True,
        resume_adaptation_reasons=["Add AWS if not confirmed"],
        application_strategy="Apply directly",
    )
    assert valid.fit_score == 85
    # invalid fit_score >100 should raise
    with pytest.raises(Exception):
        DeepAnalysisResult(
            fit_score=150,
            recommendation="APPLY",
            why_fit=[],
            gaps=[],
            must_have_requirements=[],
            nice_to_have_requirements=[],
            matched_skills=[],
            missing_skills=[],
            seniority_assessment="",
            remote_assessment="",
            salary_assessment="",
            resume_adaptation_needed=False,
            resume_adaptation_reasons=[],
            application_strategy="",
        )
    # invalid recommendation
    with pytest.raises(Exception):
        DeepAnalysisResult.model_validate({
            "fit_score": 50,
            "recommendation": "INVALID",
            "why_fit": [],
            "gaps": [],
            "must_have_requirements": [],
            "nice_to_have_requirements": [],
            "matched_skills": [],
            "missing_skills": [],
            "seniority_assessment": "",
            "remote_assessment": "",
            "salary_assessment": "",
            "resume_adaptation_needed": False,
            "resume_adaptation_reasons": [],
            "application_strategy": ""
        })
    # extra field forbidden
    with pytest.raises(Exception):
        DeepAnalysisResult.model_validate({
            "fit_score": 50,
            "recommendation": "REVIEW",
            "why_fit": [],
            "gaps": [],
            "must_have_requirements": [],
            "nice_to_have_requirements": [],
            "matched_skills": [],
            "missing_skills": [],
            "seniority_assessment": "",
            "remote_assessment": "",
            "salary_assessment": "",
            "resume_adaptation_needed": False,
            "resume_adaptation_reasons": [],
            "application_strategy": "",
            "unknown_field": "should fail"
        })


def test_unknown_experience_is_not_invented():
    profile = _profile()
    # vacancy requires AWS and GCP, profile only has python/n8n
    vac = _vac(description="Must have AWS and GCP and Kubernetes. Python and n8n are nice. Senior remote.")
    match = JobMatcher(profile).match(vac)
    # Use fallback (no LLM) to ensure principle
    with patch.object(job_analyzer, "_call_llm", side_effect=Exception("LLM disabled for test")):
        result = analyze_job_deep(vac, profile, match, resume_text="Skills: python, n8n. Other: unknown / not confirmed")
    # matched should only contain profile skills that are in text
    assert "python" in [s.lower() for s in result.matched_skills] or "n8n" in [s.lower() for s in result.matched_skills]
    # missing should contain AWS/GCP as not confirmed, and should not claim them as matched
    missing_lc = [m.lower() for m in result.missing_skills]
    # at least one missing should be reported, and gaps should contain unknown/not confirmed
    assert len(result.missing_skills) > 0
    gaps_text = " ".join(result.gaps).lower()
    assert "not confirmed" in gaps_text or "unknown" in gaps_text
    # Ensure system prompt contains instruction
    prompt = _build_system_prompt()
    assert "unknown" in prompt.lower()
    assert "not confirmed" in prompt.lower()
    assert "not invent" in prompt.lower() or "не имеет права придумывать" in prompt.lower() or "must not invent" in prompt.lower()


def test_apply_vacancy_reaches_llm():
    profile = _profile()
    vac_apply = _vac(title="AI Automation Engineer", description="python n8n automation llm api remote senior english")
    match_apply = JobMatcher(profile).match(vac_apply)
    assert match_apply.decision in ("APPLY", "REVIEW")
    assert should_analyze(match_apply) is True

    call_count = {"n": 0}

    def fake_llm(sys_p, user_p):
        call_count["n"] += 1
        fake = {
            "fit_score": 90,
            "recommendation": "APPLY",
            "why_fit": ["Python match"],
            "gaps": ["AWS not confirmed"],
            "must_have_requirements": ["Python"],
            "nice_to_have_requirements": ["Docker"],
            "matched_skills": ["python"],
            "missing_skills": ["AWS"],
            "seniority_assessment": "Senior match",
            "remote_assessment": "Remote match",
            "salary_assessment": "Meets",
            "resume_adaptation_needed": False,
            "resume_adaptation_reasons": [],
            "application_strategy": "Apply"
        }
        return json.dumps(fake)

    with patch.object(job_analyzer, "_call_llm", side_effect=fake_llm):
        result = analyze_job_deep(vac_apply, profile, match_apply, resume_text="python, n8n")
    assert call_count["n"] == 1
    assert result.fit_score == 90


def test_skip_vacancy_does_not_reach_llm():
    profile = CandidateProfile(
        desired_roles=["AI Automation Engineer"],
        skills=["python"],
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
    vac_skip = Vacancy(
        source="test",
        source_job_id="skip1",
        title="Java 1C Developer",
        company="Test",
        description="Java and 1C required, office in Moscow",
        job_url="https://example.com/skip",
        location="Moscow Office",
        country_restrictions=["RU"],
    )
    # Use JobProfile with excluded to force SKIP
    prof2 = JobProfile(desired_roles=["python"], excluded_roles=["java"], excluded_countries=[])
    # But easier: profile with remote_required True and vacancy not remote => SKIP
    match_skip = JobMatcher(profile).match(vac_skip)
    # Ensure it's SKIP: remote required but vacancy not remote should be SKIP
    # Our profile has remote_required True, vac location Moscow => hard SKIP
    assert match_skip.decision == "SKIP"
    assert should_analyze(match_skip) is False

    # Verify CLI filtering would skip LLM
    call_count = {"n": 0}

    def fake_llm(sys_p, user_p):
        call_count["n"] += 1
        return json.dumps({
            "fit_score": 10,
            "recommendation": "SKIP",
            "why_fit": [],
            "gaps": ["not fit"],
            "must_have_requirements": [],
            "nice_to_have_requirements": [],
            "matched_skills": [],
            "missing_skills": [],
            "seniority_assessment": "",
            "remote_assessment": "",
            "salary_assessment": "",
            "resume_adaptation_needed": False,
            "resume_adaptation_reasons": [],
            "application_strategy": ""
        })

    with patch.object(job_analyzer, "_call_llm", side_effect=fake_llm):
        # Simulate two-stage: only call if should_analyze
        if should_analyze(match_skip):
            analyze_job_deep(vac_skip, profile, match_skip)
        else:
            pass
    assert call_count["n"] == 0


def test_analysis_persistence():
    tmp_dir = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    try:
        db_file = str(Path(tmp_dir) / "deep_test.db")
        config.DB_FILE = db_file
        db.init_db()
        # create vacancy
        vac = _vac(source_job_id="persist1")
        db.save_vacancy(vac)
        # create fake deep result
        result = DeepAnalysisResult(
            fit_score=88,
            recommendation="APPLY",
            why_fit=["Python"],
            gaps=["AWS not confirmed"],
            must_have_requirements=["Python", "AWS"],
            nice_to_have_requirements=["Docker"],
            matched_skills=["python"],
            missing_skills=["AWS"],
            seniority_assessment="Senior match",
            remote_assessment="Remote match",
            salary_assessment="Meets",
            resume_adaptation_needed=True,
            resume_adaptation_reasons=["Add AWS clarification"],
            application_strategy="Apply via referral",
        )
        db.save_deep_analysis(vac.stable_id(), ANALYZER_VERSION, result.fit_score, result.recommendation, result.model_dump_json())
        row = db.get_deep_analysis(vac.stable_id(), ANALYZER_VERSION)
        assert row is not None
        assert row[2] == 88
        assert row[3] == "APPLY"
        assert "Python" in row[4]
        # without version filter also returns
        row2 = db.get_deep_analysis(vac.stable_id())
        assert row2 is not None
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_analyzer_idempotency():
    tmp_dir = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    try:
        db_file = str(Path(tmp_dir) / "empot.db")
        config.DB_FILE = db_file
        db.init_db()
        vac1 = _vac(source_job_id="idemp1", title="AI Automation Engineer python n8n", description="python n8n automation llm api remote senior")
        vac2 = _vac(source_job_id="idemp2", title="AI Automation Engineer python", description="python remote senior")
        db.save_vacancy(vac1)
        db.save_vacancy(vac2)
        profile = _profile()

        call_count = {"n": 0}

        def fake_llm(sys_p, user_p):
            call_count["n"] += 1
            return json.dumps({
                "fit_score": 80,
                "recommendation": "REVIEW",
                "why_fit": ["Python"],
                "gaps": ["AWS not confirmed"],
                "must_have_requirements": ["Python"],
                "nice_to_have_requirements": [],
                "matched_skills": ["python"],
                "missing_skills": ["AWS"],
                "seniority_assessment": "senior",
                "remote_assessment": "remote",
                "salary_assessment": "ok",
                "resume_adaptation_needed": False,
                "resume_adaptation_reasons": [],
                "application_strategy": "apply"
            })

        with patch.object(job_analyzer, "_call_llm", side_effect=fake_llm):
            # First run: mimic CLI analyze-deep logic
            from ai_assistant.db import get_deep_analysis as gda, save_deep_analysis as sda
            from ai_assistant.matcher import JobMatcher
            matcher = JobMatcher(profile)
            # simulate top 2
            for vac in [vac1, vac2]:
                m = matcher.match(vac)
                assert should_analyze(m)
                sid = vac.stable_id()
                if gda(sid, ANALYZER_VERSION):
                    continue
                res = analyze_job_deep(vac, profile, m)
                sda(sid, ANALYZER_VERSION, res.fit_score, res.recommendation, res.model_dump_json())
            assert call_count["n"] == 2
            # Second run should be idempotent - no new LLM calls
            call_count["n"] = 0
            for vac in [vac1, vac2]:
                m = matcher.match(vac)
                sid = vac.stable_id()
                if gda(sid, ANALYZER_VERSION):
                    continue
                res = analyze_job_deep(vac, profile, m)
                sda(sid, ANALYZER_VERSION, res.fit_score, res.recommendation, res.model_dump_json())
            assert call_count["n"] == 0
            # Ensure still 2 rows
            from ai_assistant.db import list_deep_analyses
            rows = list_deep_analyses()
            assert len(rows) == 2
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_analyzer_version_invalidates_old_result():
    tmp_dir = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    try:
        db_file = str(Path(tmp_dir) / "ver.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(source_job_id="ver1")
        db.save_vacancy(vac)
        profile = _profile()
        # Save with v1
        result_v1 = DeepAnalysisResult(
            fit_score=70,
            recommendation="REVIEW",
            why_fit=["a"],
            gaps=["b"],
            must_have_requirements=[],
            nice_to_have_requirements=[],
            matched_skills=["python"],
            missing_skills=[],
            seniority_assessment="",
            remote_assessment="",
            salary_assessment="",
            resume_adaptation_needed=False,
            resume_adaptation_reasons=[],
            application_strategy="",
        )
        db.save_deep_analysis(vac.stable_id(), "v1", result_v1.fit_score, result_v1.recommendation, result_v1.model_dump_json())
        # Check v1 exists
        assert db.get_deep_analysis(vac.stable_id(), "v1") is not None
        # v2 should be not found (invalidates)
        assert db.get_deep_analysis(vac.stable_id(), "v2") is None
        assert db.is_deep_analyzed(vac.stable_id(), "v1") is True
        assert db.is_deep_analyzed(vac.stable_id(), "v2") is False

        # Now analyze with new version should trigger LLM
        call_count = {"n": 0}

        def fake_llm2(sys_p, user_p):
            call_count["n"] += 1
            return json.dumps({
                "fit_score": 85,
                "recommendation": "APPLY",
                "why_fit": ["Python"],
                "gaps": [],
                "must_have_requirements": [],
                "nice_to_have_requirements": [],
                "matched_skills": ["python"],
                "missing_skills": [],
                "seniority_assessment": "",
                "remote_assessment": "",
                "salary_assessment": "",
                "resume_adaptation_needed": False,
                "resume_adaptation_reasons": [],
                "application_strategy": ""
            })

        # Simulate version bump
        with patch.object(job_analyzer, "ANALYZER_VERSION", "v2"):
            with patch.object(job_analyzer, "_call_llm", side_effect=fake_llm2):
                # Check should re-analyze
                if not db.is_deep_analyzed(vac.stable_id(), "v2"):
                    res = analyze_job_deep(vac, profile, JobMatcher(profile).match(vac))
                    db.save_deep_analysis(vac.stable_id(), "v2", res.fit_score, res.recommendation, res.model_dump_json())
                assert call_count["n"] == 1
                # Verify v2 now exists, v1 still but overwritten? With single PK, v1 overwritten. Check v2 exists
                assert db.get_deep_analysis(vac.stable_id(), "v2") is not None
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp_dir, ignore_errors=True)
