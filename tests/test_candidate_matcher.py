from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ai_assistant.schema import Vacancy
from ai_assistant.matcher import JobMatcher, JobProfile
from ai_assistant.candidate_profile import CandidateProfile


def _vac(**kwargs):
    defaults = dict(
        source="test",
        source_job_id="1",
        title="",
        company="TestCo",
        description="",
        job_url="https://example.com/1",
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        employment_type=None,
    )
    defaults.update(kwargs)
    return Vacancy(**defaults)


def test_exact_role_match():
    profile = CandidateProfile(
        desired_roles=["AI Automation Engineer"],
        alternative_roles=["Python Developer"],
        skills=[],
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
    vac = _vac(title="AI Automation Engineer", description="random")
    res = JobMatcher(profile).match(vac)
    # role 25 should be present
    assert res.score >= 25
    assert any("Exact role match" in s for s in res.strengths)
    # also ensure alternative not triggered when exact matches
    assert res.score >= 80 or res.decision in ("APPLY", "REVIEW")


def test_alternative_role_match():
    profile = CandidateProfile(
        desired_roles=["AI Automation Engineer"],
        alternative_roles=["Python Developer"],
        skills=[],
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
    vac_exact = _vac(title="AI Automation Engineer")
    vac_alt = _vac(title="Python Developer")
    vac_none = _vac(title="Java Developer")
    r_exact = JobMatcher(profile).match(vac_exact)
    r_alt = JobMatcher(profile).match(vac_alt)
    r_none = JobMatcher(profile).match(vac_none)
    # exact should score higher than alternative, alternative higher than none
    assert r_exact.score > r_alt.score > r_none.score
    assert r_alt.score == 15 + 75  # 15 role + 75 neutral others? check neutral totals
    # exact 25+75=100, alt 15+75=90, none 0+75=75 -> but our neutral sums: skills 25 + seniority 15 + location 15 + salary 10 + employment 5 + language 5 =75 neutral
    assert r_exact.score == 100
    assert r_alt.score == 90
    assert r_none.score == 75


def test_skills_match():
    profile = CandidateProfile(
        desired_roles=[],
        skills=["python", "n8n", "automation"],
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
    # full match
    vac_full = _vac(description="We need python, n8n and automation expert")
    res_full = JobMatcher(profile).match(vac_full)
    assert res_full.score >= 90  # should have skills 25 etc
    assert any("Skills match 3/3" in s for s in res_full.strengths)

    # partial
    vac_partial = _vac(description="python only")
    res_partial = JobMatcher(profile).match(vac_partial)
    # ratio 1/3 => 8 points (25*0.333=8)
    # neutral others 25(role)+15+15+10+5+5=75 => 75+8=83
    assert res_partial.score == 83
    assert res_full.score > res_partial.score

    # none
    vac_none = _vac(description="java and php")
    res_none = JobMatcher(profile).match(vac_none)
    assert res_none.score < res_partial.score
    assert any("Missing skills" in g for g in res_none.gaps)


def test_seniority():
    profile = CandidateProfile(
        desired_roles=[],
        skills=[],
        preferred_seniority=["senior", "mid"],
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
    vac_hit = _vac(title="Senior AI Engineer", description="mid level")
    res_hit = JobMatcher(profile).match(vac_hit)
    assert res_hit.score == 100  # neutral 25+25+15+10+5+5=85 + seniority 15 =100
    assert any("Seniority match" in s for s in res_hit.strengths)

    vac_miss = _vac(title="Junior AI Engineer", description="junior")
    res_miss = JobMatcher(profile).match(vac_miss)
    assert res_miss.score == 85  # seniority 0, others neutral
    assert any("Seniority mismatch" in g for g in res_miss.gaps)
    assert res_hit.score > res_miss.score


def test_remote_requirement():
    profile = CandidateProfile(
        desired_roles=[],
        skills=[],
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
    vac_remote = _vac(location="Remote", title="Any", description="remote job")
    res_remote = JobMatcher(profile).match(vac_remote)
    assert res_remote.decision != "SKIP"
    assert res_remote.score >= 80

    vac_office = _vac(location="Moscow Office", title="Any", description="office only", country_restrictions=["RU"])
    res_office = JobMatcher(profile).match(vac_office)
    assert res_office.decision == "SKIP"
    assert res_office.score == 0
    assert any("Remote required" in r for r in res_office.reasons)


def test_salary():
    profile = CandidateProfile(
        desired_roles=[],
        skills=[],
        preferred_seniority=[],
        remote_required=False,
        allowed_locations=[],
        allowed_timezones=[],
        languages=[],
        employment_types=[],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )
    # meets salary
    vac_ok = _vac(salary_min=4000, salary_max=5000, salary_currency="USD")
    res_ok = JobMatcher(profile).match(vac_ok)
    assert any("Salary meets" in s for s in res_ok.strengths)
    assert res_ok.score >= 90

    # below minimum
    vac_low = _vac(salary_min=1000, salary_max=2000, salary_currency="USD")
    res_low = JobMatcher(profile).match(vac_low)
    assert res_low.score < res_ok.score
    assert any("below minimum" in g for g in res_low.gaps)

    # currency mismatch
    vac_cur = _vac(salary_min=4000, salary_max=5000, salary_currency="EUR")
    res_cur = JobMatcher(profile).match(vac_cur)
    assert any("Currency mismatch" in g for g in res_cur.gaps)
    assert res_cur.score < res_ok.score

    # unspecified salary -> 5 points neutral salary
    vac_none = _vac()
    res_none = JobMatcher(profile).match(vac_none)
    # neutral others 25+25+15+15+5+5=90 + salary 5 =95
    assert res_none.score == 95


def test_hard_exclusion():
    profile = CandidateProfile(
        desired_roles=["python"],
        skills=["python"],
        preferred_seniority=[],
        remote_required=False,
        allowed_locations=[],
        allowed_timezones=[],
        languages=[],
        employment_types=[],
        minimum_salary=None,
        excluded_roles=["1c", "php"],
        excluded_companies=["acme"],
        excluded_countries=["china"],
        excluded_industries=["gambling"],
    )
    # excluded role
    vac_role = _vac(title="1C Developer", description="1c required")
    assert JobMatcher(profile).match(vac_role).decision == "SKIP"
    assert JobMatcher(profile).match(vac_role).score == 0

    # excluded company
    vac_comp = _vac(company="Acme Corp", title="Python Dev", description="python")
    assert JobMatcher(profile).match(vac_comp).decision == "SKIP"

    # excluded country
    vac_country = _vac(title="Python Dev", description="python", location="China", country_restrictions=["China"])
    assert JobMatcher(profile).match(vac_country).decision == "SKIP"

    # excluded industry
    vac_ind = _vac(title="Python Dev", description="gambling project python", company="Casino LLC")
    # set profile excluded_industries gambling should trigger
    assert JobMatcher(profile).match(vac_ind).decision == "SKIP"

    # hard constraint overrides high score
    vac_high_but_excluded = _vac(title="Python Dev", description="python n8n automation senior remote english", location="Remote", company="Acme", salary_min=5000, salary_max=6000, salary_currency="USD", employment_type="Full Time")
    # this would otherwise be APPLY but excluded company forces SKIP
    assert JobMatcher(profile).match(vac_high_but_excluded).decision == "SKIP"


def test_apply_threshold():
    # Construct vacancy that hits 100 -> APPLY
    profile = CandidateProfile(
        desired_roles=["AI Automation Engineer"],
        alternative_roles=[],
        skills=["python", "n8n"],
        preferred_seniority=["senior"],
        remote_required=True,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=["english"],
        employment_types=["Full Time"],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )
    vac = _vac(
        title="AI Automation Engineer",
        description="python n8n senior english remote",
        location="Remote",
        salary_min=4000,
        salary_max=5000,
        salary_currency="USD",
        employment_type="Full Time",
    )
    res = JobMatcher(profile).match(vac)
    assert res.score >= 80
    assert res.score <= 100
    assert res.decision == "APPLY"
    assert res.score == 100
    assert "reasons" in res.to_dict()
    assert res.reasons
    assert res.strengths
    # gaps may be empty for perfect match


def test_review_threshold():
    profile = CandidateProfile(
        desired_roles=["AI Automation Engineer"],
        alternative_roles=[],
        skills=["python", "n8n", "automation"],
        preferred_seniority=["senior"],
        remote_required=False,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=["english"],
        employment_types=["Full Time"],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )
    # Craft to get ~70 -> REVIEW
    # role 25, skills 25 (all 3 matched), seniority 0, location 0, salary 10, employment 5, language 5 =70
    vac = _vac(
        title="AI Automation Engineer",  # 25
        description="python n8n automation english",  # skills 25 + language 5 but seniority not, location not
        location="Berlin Office",  # not remote => 0
        country_restrictions=["DE"],
        salary_min=4000,
        salary_max=5000,
        salary_currency="USD",  # 10
        employment_type="Full Time",  # 5
        # seniority missing -> 0, language english present ->5
    )
    # need to ensure seniority is missing
    res = JobMatcher(profile).match(vac)
    # compute: role 25 + skills 25 + seniority 0 + location 0 + salary 10 + employment 5 + language 5 =70
    assert res.score == 70
    assert res.decision == "REVIEW"
    assert 65 <= res.score <= 79


def test_skip_threshold():
    profile = CandidateProfile(
        desired_roles=["AI Automation Engineer"],
        alternative_roles=[],
        skills=["python", "n8n", "automation"],
        preferred_seniority=["senior"],
        remote_required=False,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=["english"],
        employment_types=["Full Time"],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )
    vac = _vac(
        title="Java Developer",  # 0
        description="java only",  # skills 0, language 0 (no english), seniority 0
        location="Berlin Office",
        country_restrictions=["DE"],
        salary_min=1000,
        salary_max=1500,
        salary_currency="USD",  # 0 below min
        employment_type="Part Time",  # 0 mismatch
    )
    res = JobMatcher(profile).match(vac)
    # 0+0+0+0+0+0+0 =0
    assert res.score == 0
    assert res.decision == "SKIP"
    assert 0 <= res.score <= 64


def test_profile_load_from_file():
    data = {
        "desired_roles": ["AI Automation Engineer"],
        "alternative_roles": ["Python Developer"],
        "skills": ["python"],
        "preferred_seniority": ["mid"],
        "years_experience": 2,
        "remote_required": True,
        "allowed_locations": ["Remote"],
        "allowed_timezones": [],
        "languages": ["en"],
        "employment_types": ["Full Time"],
        "minimum_salary": 2000,
        "salary_currency": "USD",
        "excluded_roles": ["php"],
        "excluded_companies": ["badco"],
        "excluded_countries": ["china"],
        "excluded_industries": ["gambling"],
    }
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profile.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        loaded = CandidateProfile.from_json_file(p)
        assert loaded.desired_roles == ["AI Automation Engineer"]
        assert loaded.remote_required is True
        assert loaded.minimum_salary == 2000
        assert loaded.salary_currency == "USD"
        # check matcher still works
        vac = _vac(title="AI Automation Engineer", description="python english", location="Remote", salary_min=3000, salary_currency="USD", employment_type="Full Time")
        res = JobMatcher(loaded).match(vac)
        assert res.decision in ("APPLY", "REVIEW")


def test_result_fields_preserved():
    profile = CandidateProfile(
        desired_roles=["python"],
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
    vac = _vac(title="Python Dev", description="python")
    res = JobMatcher(profile).match(vac)
    assert hasattr(res, "score")
    assert hasattr(res, "decision")
    assert hasattr(res, "reasons")
    assert hasattr(res, "strengths")
    assert hasattr(res, "gaps")
    assert isinstance(res.score, int)
    assert 0 <= res.score <= 100
    assert res.decision in ("APPLY", "REVIEW", "SKIP")
    assert isinstance(res.reasons, list)
    assert isinstance(res.strengths, list)
    assert isinstance(res.gaps, list)
