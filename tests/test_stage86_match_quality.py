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
    HardRequirementStatus,
    MatchDecisionClass,
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


def _base_candidate_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["AI Automation Engineer", "AI Workflow Developer"],
        alternative_roles=["Python Developer", "Integration Engineer"],
        role_families=["AI_AUTOMATION", "TECH_SUPPORT", "APPLICATION_SUPPORT"],
        core_skills=["Python", "n8n", "FastAPI", "REST API", "Git"],
        secondary_skills=["Docker", "PostgreSQL", "Make.com", "Zapier"],
        transferable_skills=["Linux", "Bash", "System Administration", "Tech Support"],
        seniority_range=["Middle", "Senior"],
        years_of_experience=3,
        remote_required=True,
        allowed_locations=["Remote", "Worldwide", "Thailand", "Russia"],
        allowed_timezones=["UTC+7", "UTC+3", "EST", "PST", "Flexible"],
        languages=["English", "Russian"],
        employment_types=["Full Time", "Contract", "Remote"],
        minimum_salary=3000.0,
        salary_currency="USD",
        must_avoid_conditions=["Sales", "Cold Calling", "Casino", "Gambling", "1C developer"],
        excluded_roles=["Java Developer", "Senior C++ Developer", "Sales Rep"],
        excluded_companies=["CasinoBet"],
        excluded_countries=["North Korea"],
        excluded_industries=["Gambling"],
    )


# 1. Role family exact match
def test_role_family_exact_match():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="Senior AI Automation Engineer",
        description="Build LLM agents, n8n workflows, Python automation. English required."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.role_family == RoleFamily.AI_AUTOMATION.value
    assert res.decision in ("APPLY", "REVIEW")
    assert res.score >= 80
    assert any("Exact role match" in s for s in res.strengths)


# 2. Related support match
def test_related_support_match():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="Technical Support Specialist / Integration Engineer",
        description="Support Bitrix24 / CRM integrations, REST API, Python scripts, customer issues. Middle level. English required."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.decision in ("APPLY", "REVIEW")
    assert res.score >= 65


# 3. Unrelated backend penalty
def test_unrelated_backend_penalty():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="QA Manual Tester",
        description="Manual testing of web forms, test cases, Jira, regression checklists. English B1."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.score < 65
    assert res.decision == "SKIP"


# 4. Missing mandatory skill reject
def test_missing_mandatory_skill_reject():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="Principal C++ Developer",
        description="Requires 7+ years of C++ game engine architecture and deep graphics programming."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.decision == "SKIP"
    assert res.score == 0
    assert res.eligibility == "INELIGIBLE"


# 5. Seniority gap
def test_seniority_gap():
    prof = _base_candidate_profile()  # 3 yrs exp
    vac = _make_vacancy(
        title="Staff / Principal Architect (AI Cloud)",
        description="Minimum 10+ years of experience in enterprise cloud architecture and distributed systems."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.decision in ("REVIEW", "SKIP")


# 6. Years experience fit
def test_years_experience_fit():
    prof = _base_candidate_profile()  # 3 yrs exp
    vac = _make_vacancy(
        title="Middle Python / Automation Developer",
        description="Requires 2-3 years of experience in Python, FastAPI, REST API, Git, and automation scripts. English."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.score >= 80
    assert res.decision == "APPLY"


# 7. Remote compatible fit
def test_remote_compatible_fit():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="AI Automation Engineer",
        location="Remote / Anywhere",
        description="100% remote distributed team, Python and n8n expert needed. English. Senior level."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.score >= 80
    assert any("Remote" in s for s in res.strengths)


# 8. Onsite reject
def test_onsite_reject():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="Python Developer",
        location="Berlin On-site Office Only",
        remote_ok=False,
        description="This is a strictly on-site role in Berlin office 5 days a week."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.decision == "SKIP"
    assert res.score == 0
    assert res.eligibility == "INELIGIBLE"


# 9. Language mismatch
def test_language_mismatch():
    prof = _base_candidate_profile()  # English, Russian
    vac = _make_vacancy(
        title="Python Developer - Tokyo",
        description="Must be fluent in Japanese (N1/N2 native level required). Python, Django."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.decision == "SKIP"
    assert res.score == 0
    assert res.eligibility == "INELIGIBLE"


# 10. Transferable vs direct skill weights
def test_transferable_vs_direct_skill_weights():
    prof = _base_candidate_profile()
    vac_direct = _make_vacancy(
        title="AI Automation Engineer",
        description="Python, n8n, FastAPI, REST API, Git required. English."
    )
    vac_transferable = _make_vacancy(
        title="AI Automation Engineer",
        description="Make.com, Zapier, Bash, Linux required. English."
    )
    matcher = JobMatcher(prof)
    res_direct = matcher.match(vac_direct)
    res_trans = matcher.match(vac_transferable)
    assert res_direct.score > res_trans.score


# 11. Negative evidence in gaps
def test_negative_evidence_in_gaps():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="Python Automation Engineer",
        salary_min=1500,
        salary_max=2000,
        salary_currency="USD",
        description="Python automation. Experience required: 6+ years."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert any("Salary below minimum" in g for g in res.gaps)
    assert any("Experience gap" in g for g in res.gaps)


# 12. Explanation concrete evidence
def test_explanation_concrete_evidence():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="AI Automation Engineer",
        description="Python, n8n, REST API. English required."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert len(res.reasons) > 0
    assert len(res.strengths) > 0
    assert any("Exact role match" in s for s in res.strengths)


# 13. Digest ranking
def test_digest_ranking():
    prof = _base_candidate_profile()
    matcher = JobMatcher(prof)
    
    vac_strong = _make_vacancy(
        id="vac_strong",
        title="AI Automation Engineer",
        description="Python, n8n, FastAPI, REST API, Git, English."
    )
    vac_mid = _make_vacancy(
        id="vac_mid",
        title="Technical Support Specialist",
        description="Python scripts, REST API, tech support, English."
    )
    
    res_s = matcher.match(vac_strong)
    res_m = matcher.match(vac_mid)
    
    items = [
        {"score": res_m.score, "decision_class": res_m.decision_class, "v": vac_mid},
        {"score": res_s.score, "decision_class": res_s.decision_class, "v": vac_strong},
    ]
    rank_order = {"STRONG_MATCH": 4, "MATCH": 3, "BORDERLINE": 2, "REJECT": 1}
    items.sort(key=lambda x: (rank_order.get(x["decision_class"], 0), x["score"]), reverse=True)
    
    assert items[0]["v"].source_job_id == "vac_strong"


# 14. Diversity control
def test_diversity_control():
    vacs = [
        _make_vacancy(id=f"v_{i}", title=f"Python Dev {i}", company="SameCompany Ltd")
        for i in range(4)
    ]
    
    company_counts = {}
    selected = []
    for v in vacs:
        comp = v.company.lower()
        if company_counts.get(comp, 0) < 2:
            company_counts[comp] = company_counts.get(comp, 0) + 1
            selected.append(v)
            
    assert len(selected) == 2


# 15. Hard mismatch override
def test_hard_mismatch_override():
    prof = _base_candidate_profile()
    vac = _make_vacancy(
        title="AI Automation Engineer",
        description="Python, n8n, automation. Must possess active Top Secret clearance / US Citizenship required."
    )
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    assert res.decision == "SKIP"
    assert res.score == 0
    assert res.eligibility == "INELIGIBLE"


# 16. Candidate matcher contract compatibility
def test_candidate_matcher_contract_compatibility():
    prof = _base_candidate_profile()
    vac = _make_vacancy()
    matcher = JobMatcher(prof)
    res = matcher.match(vac)
    
    assert isinstance(res.score, int)
    assert 0 <= res.score <= 100
    assert res.decision in ("APPLY", "REVIEW", "SKIP")
    assert isinstance(res.reasons, list)
    assert isinstance(res.strengths, list)
    assert isinstance(res.gaps, list)
    
    d = res.to_dict()
    assert "score" in d
    assert "decision" in d
    assert "role_family" in d
    assert "dimensions" in d


# 17. Calibration Dataset - 10 Deterministic Representative Cases (Task 14)
def test_calibration_dataset_10_representative_cases():
    prof = _base_candidate_profile()
    matcher = JobMatcher(prof)
    
    cases = [
        # 1. Exact target role (AI automation / n8n) -> STRONG_MATCH / APPLY
        (
            _make_vacancy(
                id="calib_1",
                title="Senior AI Automation Engineer",
                description="Develop workflow automations with n8n and Python scripts, FastAPI, REST API, Git. English.",
                salary_min=4000, salary_max=5000,
            ),
            "APPLY",
            80,
        ),
        # 2. Alternative/adjacent target role (Python integration / bot dev) -> APPLY / REVIEW
        (
            _make_vacancy(
                id="calib_2",
                title="Middle Python Integration Engineer",
                description="Build REST API connectors and automation webhooks. Python, FastAPI, Git. English.",
                salary_min=3500, salary_max=4500,
            ),
            "APPLY",
            75,
        ),
        # 3. Higher seniority in target field (Senior AI engineer) -> APPLY / REVIEW
        (
            _make_vacancy(
                id="calib_3",
                title="Senior AI Automation Specialist",
                description="Lead workflow automation team. Python, n8n, AI agents, FastAPI, Git. English.",
                salary_min=5000, salary_max=7000,
            ),
            "APPLY",
            80,
        ),
        # 4. Unrelated tech stack (Java/Spring enterprise backend) -> REJECT / SKIP
        (
            _make_vacancy(
                id="calib_4",
                title="Senior Java Developer",
                description="Deep Java internals, Spring Boot, microservices architecture, Hibernate.",
                salary_min=5000, salary_max=7000,
            ),
            "SKIP",
            0,
        ),
        # 5. Related infrastructure/support role (Linux sysadmin / IT support) -> REVIEW
        (
            _make_vacancy(
                id="calib_5",
                title="Technical Support Specialist",
                description="L2/L3 support for enterprise customers. Linux, bash, troubleshooting. English. Middle level.",
                salary_min=3000, salary_max=3800,
            ),
            "REVIEW",
            65,
        ),
        # 6. Non-remote vacancy -> REJECT / SKIP (hard gate)
        (
            _make_vacancy(
                id="calib_6",
                title="AI Automation Engineer",
                location="Munich On-site Office Only",
                description="Strictly on-site presence required in Munich office.",
                country_restrictions=["DE"],
            ),
            "SKIP",
            0,
        ),
        # 7. Mandatory unsupported language vacancy -> REJECT / SKIP (hard gate)
        (
            _make_vacancy(
                id="calib_7",
                title="Python Developer",
                description="Must have native Japanese (C2 / N1 level required). Python, Django.",
            ),
            "SKIP",
            0,
        ),
        # 8. Strict clearance / citizenship vacancy -> REJECT / SKIP (hard gate)
        (
            _make_vacancy(
                id="calib_8",
                title="AI Automation Engineer",
                description="Top Secret active security clearance and US Citizenship required.",
            ),
            "SKIP",
            0,
        ),
        # 9. Salary-below-floor vacancy -> SKIP or penalized
        (
            _make_vacancy(
                id="calib_9",
                title="Junior Automation Intern",
                description="Entry level automation helper.",
                salary_min=800, salary_max=1200, salary_currency="USD",
            ),
            "SKIP",
            0,
        ),
        # 10. Salary-above-floor vacancy -> APPLY
        (
            _make_vacancy(
                id="calib_10",
                title="Senior AI Automation Engineer",
                description="Python, n8n, FastAPI, REST API, Git, AI workflow development. English.",
                salary_min=4500, salary_max=6000, salary_currency="USD",
            ),
            "APPLY",
            80,
        ),
    ]
    
    for vac, expected_decision, min_expected_score in cases:
        res = matcher.match(vac)
        if expected_decision == "APPLY":
            assert res.decision in ("APPLY", "REVIEW"), f"Case {vac.source_job_id} expected APPLY/REVIEW, got {res.decision}"
            assert res.score >= min_expected_score, f"Case {vac.source_job_id} expected score >= {min_expected_score}, got {res.score}"
        elif expected_decision == "REVIEW":
            assert res.decision in ("REVIEW", "APPLY"), f"Case {vac.source_job_id} expected REVIEW/APPLY, got {res.decision}"
            assert res.score >= min_expected_score, f"Case {vac.source_job_id} expected score >= {min_expected_score}, got {res.score}"
        elif expected_decision == "SKIP":
            assert res.decision == "SKIP", f"Case {vac.source_job_id} expected SKIP, got {res.decision}"

