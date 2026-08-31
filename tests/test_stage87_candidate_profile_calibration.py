from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile, load_candidate_profile
from ai_assistant.matcher import (
    JobMatcher,
    RoleFamily,
    RolePriority,
    HardRequirementStatus,
    MatchDecisionClass,
    SkillConfidenceLevel,
    classify_role_family,
    extract_seniority,
    extract_required_years,
    evaluate_hard_requirements,
)


def _make_vacancy(**kwargs) -> Vacancy:
    return Vacancy(
        source=kwargs.get("source", "remoteok"),
        source_job_id=kwargs.get("id") or kwargs.get("source_job_id", "v1"),
        title=kwargs.get("title", "AI Automation Engineer"),
        company=kwargs.get("company", "Acme Corp"),
        description=kwargs.get("description", "We are looking for an AI Automation Engineer with Python and n8n experience."),
        job_url=kwargs.get("job_url", "https://example.com/job/1"),
        application_url=kwargs.get("application_url"),
        location=kwargs.get("location", "Remote"),
        country_restrictions=kwargs.get("country_restrictions", []),
        timezone_restrictions=kwargs.get("timezone_restrictions", []),
        salary_min=kwargs.get("salary_min", 4000.0),
        salary_max=kwargs.get("salary_max", 6000.0),
        salary_currency=kwargs.get("salary_currency", "USD"),
        employment_type=kwargs.get("employment_type", "Full Time"),
        published_at=kwargs.get("published_at", datetime.now(timezone.utc)),
    )


# 1. test_total_it_experience_does_not_equal_python_experience
def test_total_it_experience_does_not_equal_python_experience():
    prof = load_candidate_profile()
    # Python role requiring 8 years
    py_vac = _make_vacancy(
        title="Senior Python Backend Developer",
        description="Requires 8+ years of commercial Python experience, FastAPI, and distributed databases. English."
    )
    # Support role requiring 8 years
    supp_vac = _make_vacancy(
        title="Senior Application Support Engineer",
        description="Requires 8+ years of IT support, Active Directory, SQL, and troubleshooting experience. English."
    )
    matcher = JobMatcher(prof)
    py_res = matcher.match(py_vac)
    supp_res = matcher.match(supp_vac)

    # Candidate has 3.5 yrs Python, 11 yrs IT Support
    assert py_res.score < supp_res.score
    assert any("Experience gap" in g for g in py_res.gaps)
    assert not any("Experience gap" in g for g in supp_res.gaps)


# 2. test_seniority_is_role_family_specific
def test_seniority_is_role_family_specific():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    # Senior Application Support -> Senior is in seniority list for APPLICATION_SUPPORT
    supp_vac = _make_vacancy(
        title="Senior Application Support Engineer",
        description="Technical support, SQL, Active Directory, troubleshooting. English required."
    )
    supp_res = matcher.match(supp_vac)
    assert any("Seniority match: senior" in s for s in supp_res.strengths)

    # Lead Python Backend Architect -> Lead is NOT in junior/mid list for PYTHON_BACKEND
    py_lead_vac = _make_vacancy(
        title="Lead Python Architect",
        description="Lead backend engineering team, Python, FastAPI, Docker. English required."
    )
    py_res = matcher.match(py_lead_vac)
    assert any("Seniority mismatch" in g for g in py_res.gaps)


# 3. test_professional_skill_outweighs_project_skill
def test_professional_skill_outweighs_project_skill():
    prof = CandidateProfile(
        desired_roles=["Automation Developer"],
        role_families=["AI_AUTOMATION"],
        core_skills=["python", "fastapi"],
        skill_confidence={"python": "PROFESSIONAL", "fastapi": "PROJECT"},
        remote_required=True,
        allowed_locations=["Remote"],
    )
    matcher = JobMatcher(prof)

    # Role matching professional skill (Python)
    vac_prof = _make_vacancy(
        title="Automation Developer",
        description="Python automation scripts. Remote."
    )
    # Role matching project skill (FastAPI)
    vac_proj = _make_vacancy(
        title="Automation Developer",
        description="FastAPI web services. Remote."
    )

    res_prof = matcher.match(vac_prof)
    res_proj = matcher.match(vac_proj)
    assert res_prof.score >= res_proj.score


# 4. test_project_skill_outweighs_transferable_skill
def test_project_skill_outweighs_transferable_skill():
    prof = CandidateProfile(
        desired_roles=["Developer"],
        role_families=["AI_AUTOMATION"],
        core_skills=["fastapi", "vpn"],
        skill_confidence={"fastapi": "PROJECT", "vpn": "TRANSFERABLE"},
        remote_required=True,
        allowed_locations=["Remote"],
    )
    matcher = JobMatcher(prof)

    vac_proj = _make_vacancy(title="Developer", description="FastAPI services. Remote.")
    vac_trans = _make_vacancy(title="Developer", description="VPN network support. Remote.")

    res_proj = matcher.match(vac_proj)
    res_trans = matcher.match(vac_trans)
    assert res_proj.score > res_trans.score


# 5. test_unknown_skill_gives_no_positive_match_credit
def test_unknown_skill_gives_no_positive_match_credit():
    prof = CandidateProfile(
        desired_roles=["Engineer"],
        role_families=["AI_AUTOMATION"],
        core_skills=["kubernetes"],
        skill_confidence={"kubernetes": "UNKNOWN"},
        remote_required=True,
        allowed_locations=["Remote"],
    )
    matcher = JobMatcher(prof)
    vac = _make_vacancy(title="Engineer", description="Kubernetes cluster management. Remote.")
    res = matcher.match(vac)
    assert res.dimensions["skills"]["score"] == 0


# 6. test_primary_role_ranks_above_equivalent_stretch_role
def test_primary_role_ranks_above_equivalent_stretch_role():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    # Primary P1 role: AI Automation
    p1_vac = _make_vacancy(
        title="AI Automation Engineer",
        description="Build n8n workflows and Python automation pipelines. Remote. English."
    )
    # Stretch P3 role: Data Engineer
    p3_vac = _make_vacancy(
        title="Data Engineer",
        description="Build Python SQL data pipelines. Remote. English."
    )

    p1_res = matcher.match(p1_vac)
    p3_res = matcher.match(p3_vac)

    assert p1_res.role_priority == "P1"
    assert p3_res.role_priority == "P3"
    assert p1_res.decision_class in ("STRONG_MATCH", "MATCH")
    assert p3_res.decision_class == "STRETCH"


# 7. test_python_backend_role_uses_python_specific_experience
def test_python_backend_role_uses_python_specific_experience():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    vac = _make_vacancy(
        title="Python Backend Developer",
        description="Requires 6+ years of commercial Python experience and FastAPI. Remote. English."
    )
    res = matcher.match(vac)
    # 6 years required vs 3.5 years python experience -> gap penalty
    assert any("Experience gap" in g for g in res.gaps)
    assert any("3.5" in g for g in res.gaps)


# 8. test_application_support_uses_support_specific_experience
def test_application_support_uses_support_specific_experience():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    vac = _make_vacancy(
        title="Application Support Specialist",
        description="Requires 6+ years of IT technical support and troubleshooting. Remote. English."
    )
    res = matcher.match(vac)
    # 6 years required vs 11 years support experience -> NO gap penalty
    assert not any("Experience gap" in g for g in res.gaps)


# 9. test_remote_country_restriction_is_respected
def test_remote_country_restriction_is_respected():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    vac = _make_vacancy(
        title="AI Automation Engineer",
        description="Python and n8n remote role.",
        country_restrictions=["US only", "United States"]
    )
    res = matcher.match(vac)
    # Candidate in Thailand should be flagged/rejected for US-restricted role
    assert res.decision == "SKIP" or res.eligibility == "INELIGIBLE" or res.score < 65


# 10. test_missing_salary_is_not_treated_as_below_floor
def test_missing_salary_is_not_treated_as_below_floor():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    vac = _make_vacancy(
        title="AI Automation Engineer",
        description="Python n8n automation. Remote. English.",
        salary_min=None,
        salary_max=None,
    )
    res = matcher.match(vac)
    # Missing salary gets neutral 5 pts and is not marked as below floor penalty
    assert res.dimensions["salary"]["score"] == 5
    assert not any("Salary below minimum" in g for g in res.gaps)


# 11. test_language_gate_requires_explicit_requirement
def test_language_gate_requires_explicit_requirement():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    # Non-mandatory mention: should NOT hard-reject
    weak_vac = _make_vacancy(
        title="Python Developer",
        description="We are a global company with headquarters in Berlin, Germany. Python, Docker. Remote. English."
    )
    weak_res = matcher.match(weak_vac)
    assert weak_res.eligibility == "ELIGIBLE"

    # Explicit requirement: MUST hard-reject
    strict_vac = _make_vacancy(
        title="Python Developer",
        description="Fluent in German (C1 German required) and Python. Remote."
    )
    strict_res = matcher.match(strict_vac)
    assert strict_res.eligibility == "INELIGIBLE"
    assert strict_res.score == 0


# 12. test_low_confidence_profile_field_cannot_create_strong_match
def test_low_confidence_profile_field_cannot_create_strong_match():
    prof = CandidateProfile(
        desired_roles=["Rust Developer"],
        role_families=["PYTHON_BACKEND"],
        core_skills=["rust", "solidity"],
        skill_confidence={"rust": "BASIC", "solidity": "BASIC"},
        remote_required=True,
        allowed_locations=["Remote"],
    )
    matcher = JobMatcher(prof)

    vac = _make_vacancy(
        title="Rust Developer",
        description="Rust and Solidity smart contract development. Remote. English."
    )
    res = matcher.match(vac)
    # Low confidence skills cannot achieve STRONG_MATCH
    assert res.decision_class != "STRONG_MATCH"


# 13. test_stretch_role_is_not_labeled_strong_match
def test_stretch_role_is_not_labeled_strong_match():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    # Stretch P3 Data Engineering role with 100% matching keywords
    vac = _make_vacancy(
        title="Data Engineer",
        description="Python, SQL, PostgreSQL, Docker, ETL pipelines, Git. Remote. English.",
        salary_min=5000.0,
        salary_max=7000.0
    )
    res = matcher.match(vac)
    assert res.role_priority == "P3"
    assert res.decision_class == "STRETCH"
    assert res.decision_class != "STRONG_MATCH"


# 14. test_candidate_profile_provenance_is_serializable
def test_candidate_profile_provenance_is_serializable():
    prof = load_candidate_profile()
    d = prof.to_dict()
    assert "provenance" in d
    assert "skill_confidence" in d
    assert "role_priorities" in d
    assert "domain_years" in d

    restored = CandidateProfile.from_dict(d)
    assert restored.provenance == prof.provenance
    assert restored.skill_confidence == prof.skill_confidence
    assert restored.role_priorities == prof.role_priorities
    assert restored.domain_years == prof.domain_years


# 15. test_stage86_match_contract_remains_compatible
def test_stage86_match_contract_remains_compatible():
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)
    vac = _make_vacancy()
    res = matcher.match(vac)

    assert hasattr(res, "score")
    assert hasattr(res, "decision")
    assert hasattr(res, "decision_class")
    assert hasattr(res, "eligibility")
    assert hasattr(res, "role_family")
    assert hasattr(res, "role_priority")
    assert hasattr(res, "reasons")
    assert hasattr(res, "strengths")
    assert hasattr(res, "gaps")
    assert hasattr(res, "dimensions")
    assert isinstance(res.to_dict(), dict)


# 16. test_delivery_state_is_not_modified_by_profile_calibration
def test_delivery_state_is_not_modified_by_profile_calibration():
    import sqlite3
    conn = sqlite3.connect("state.db")
    cur = conn.cursor()
    
    # Check table count if table exists
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    before_attempts = cur.execute("SELECT count(*) FROM digest_attempts").fetchone()[0] if "digest_attempts" in tables else 0
    before_vacancies = cur.execute("SELECT count(*) FROM vacancies").fetchone()[0] if "vacancies" in tables else 0
    conn.close()

    # Execute matching
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)
    vac = _make_vacancy()
    _ = matcher.match(vac)

    # Verify counts unchanged
    conn2 = sqlite3.connect("state.db")
    cur2 = conn2.cursor()
    after_attempts = cur2.execute("SELECT count(*) FROM digest_attempts").fetchone()[0] if "digest_attempts" in tables else 0
    after_vacancies = cur2.execute("SELECT count(*) FROM vacancies").fetchone()[0] if "vacancies" in tables else 0
    conn2.close()

    assert before_attempts == after_attempts
    assert before_vacancies == after_vacancies


# 17. test_application_state_is_not_modified_by_profile_calibration
def test_application_state_is_not_modified_by_profile_calibration():
    import sqlite3
    conn = sqlite3.connect("state.db")
    cur = conn.cursor()
    
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    before_apps = cur.execute("SELECT count(*) FROM applications").fetchone()[0] if "applications" in tables else 0
    conn.close()

    prof = load_candidate_profile()
    matcher = JobMatcher(prof)
    vac = _make_vacancy(title="Senior Automation Developer", description="n8n, Python, Remote.")
    _ = matcher.match(vac)

    conn2 = sqlite3.connect("state.db")
    cur2 = conn2.cursor()
    after_apps = cur2.execute("SELECT count(*) FROM applications").fetchone()[0] if "applications" in tables else 0
    conn2.close()

    assert before_apps == after_apps
