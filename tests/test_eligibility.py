"""Comprehensive unit tests for Remote Eligibility & Multi-Dimensional Classification."""

from __future__ import annotations

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.eligibility import (
    assess_vacancy_eligibility,
    classify_remote_mode,
    classify_geo_scope,
    classify_work_auth,
    classify_timezone,
    classify_language,
    classify_employment_scope,
    RemoteMode,
    GeoScope,
    WorkAuthorization,
    TimezoneRequirement,
    LanguageRequirement,
    EmploymentScope,
    EligibilityStatus,
)


def _make_vac(**kwargs) -> Vacancy:
    defaults = dict(
        source="custom",
        source_job_id="elig-test-1",
        title="Senior AI Engineer",
        company="GlobalTech",
        description="",
        job_url="https://example.com/job/1",
        location=None,
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        employment_type="Full Time",
    )
    defaults.update(kwargs)
    return Vacancy(**defaults)


# --- 1. CORE SPECIFICATION TESTS ---

def test_eligible_remote_worldwide_contractor_english():
    # Remote + Worldwide + Contractor + English required -> ELIGIBLE
    vac = _make_vac(
        title="Senior Python / AI Developer",
        location="Remote (Worldwide)",
        description="Fully remote position. We hire worldwide contractors via B2B. Working English required.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.ELIGIBLE
    assert res.remote_mode == RemoteMode.REMOTE
    assert res.geo_scope == GeoScope.WORLDWIDE
    assert res.employment_scope == EmploymentScope.WORLDWIDE_CONTRACTOR
    assert res.language_requirement == LanguageRequirement.ENGLISH


def test_ineligible_remote_us_only():
    # Remote + US only -> INELIGIBLE
    vac = _make_vac(
        title="Backend Engineer",
        location="Remote",
        description="Fully remote role. US only. Candidates must be located in the US.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.geo_scope == GeoScope.COUNTRY_SPECIFIC
    assert "US" in res.matched_countries
    assert any("Country restricted to US" in r for r in res.eligibility_reasons)


def test_ineligible_remote_europe_only():
    # Remote + Europe only -> INELIGIBLE
    vac = _make_vac(
        title="Fullstack Developer",
        location="Remote - Europe Only",
        description="100% remote opportunity. EU only. Must reside in the EU/EEA.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.geo_scope == GeoScope.REGIONAL
    assert "EU" in res.matched_regions
    assert any("Region restricted to EU" in r for r in res.eligibility_reasons)


def test_ineligible_must_reside_in_germany():
    # "Remote, but candidates must reside in Germany" -> INELIGIBLE
    vac = _make_vac(
        title="DevOps Lead",
        location="Remote",
        description="Remote position, but candidates must reside in Germany.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.geo_scope == GeoScope.COUNTRY_SPECIFIC
    assert "Germany" in res.matched_countries


def test_ineligible_hybrid_berlin_office():
    # "Remote, but must occasionally work from our Berlin office" -> INELIGIBLE
    vac = _make_vac(
        title="Engineering Manager",
        location="Remote",
        description="Remote, but must occasionally work from our Berlin office for key meetings.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.remote_mode in (RemoteMode.HYBRID, RemoteMode.ONSITE, RemoteMode.UNKNOWN)
    assert any("Hybrid" in r or "Onsite" in r for r in res.eligibility_reasons)


def test_ineligible_native_english_restriction():
    # "Remote, native English speaker required" -> INELIGIBLE (or LANGUAGE_RESTRICTION)
    vac = _make_vac(
        title="Technical Writer / AI Evangelist",
        location="Remote Worldwide",
        description="100% remote. Native English speaker required.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.language_requirement == LanguageRequirement.NATIVE_ENGLISH
    assert any("LANGUAGE_RESTRICTION" in r for r in res.eligibility_reasons)


def test_eligible_with_timezone_warning():
    # Remote + timezone UTC-5 -> ELIGIBLE_WITH_TIMEZONE_WARNING
    vac = _make_vac(
        title="Senior Automation Engineer",
        location="Remote Worldwide",
        description="Worldwide remote position. Requires 4 hours overlap with EST (UTC-5).",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.ELIGIBLE_WITH_WARNING
    assert res.timezone_requirement == TimezoneRequirement.SPECIFIED
    assert any("ELIGIBLE_WITH_TIMEZONE_WARNING" in r for r in res.eligibility_reasons)


def test_unknown_geography_triggers_geo_unknown():
    # Remote + география не указана -> UNKNOWN (GEO_UNKNOWN)
    vac = _make_vac(
        source="custom",
        title="Python Engineer",
        location="Remote",
        description="Удаленная работа. Разработка микросервисов на Python.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.UNKNOWN
    assert res.geo_scope == GeoScope.UNKNOWN
    assert any("GEO_UNKNOWN" in r for r in res.eligibility_reasons)


def test_eligible_remote_from_thailand():
    # Remote from Thailand explicitly stated -> ELIGIBLE
    vac = _make_vac(
        title="AI Engineer",
        location="Remote (Thailand)",
        description="Work remotely from Thailand. Bangkok tech hub.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.ELIGIBLE
    assert res.geo_scope == GeoScope.THAILAND


def test_ineligible_us_w2_work_authorization():
    # Remote, W2 only, US work authorization required -> INELIGIBLE
    vac = _make_vac(
        title="Solutions Architect",
        location="Remote",
        description="100% remote. W2 only. US work authorization required without sponsorship.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.INELIGIBLE
    assert res.work_authorization == WorkAuthorization.COUNTRY_RESTRICTED
    assert res.employment_scope == EmploymentScope.LOCAL_ONLY


# --- 2. LANGUAGE LEVEL DIFFERENTIATION TESTS ---

def test_language_levels_differentiation():
    # 1. English required (B2/Working) -> PASS
    v1 = _make_vac(title="Dev", location="Remote Worldwide", description="Remote worldwide. English required.")
    r1 = assess_vacancy_eligibility(v1)
    assert r1.language_requirement == LanguageRequirement.ENGLISH
    assert r1.eligibility == EligibilityStatus.ELIGIBLE

    # 2. Fluent English (C1/C2) -> PASS
    v2 = _make_vac(title="Dev", location="Remote Worldwide", description="Remote worldwide. Fluent English C1.")
    r2 = assess_vacancy_eligibility(v2)
    assert r2.language_requirement == LanguageRequirement.FLUENT_ENGLISH
    assert r2.eligibility == EligibilityStatus.ELIGIBLE

    # 3. Russian required/speaking -> PASS
    v3 = _make_vac(title="Dev", location="Remote Worldwide", description="Remote worldwide. Знание русского языка обязательно.")
    r3 = assess_vacancy_eligibility(v3)
    assert r3.language_requirement == LanguageRequirement.RUSSIAN
    assert r3.eligibility == EligibilityStatus.ELIGIBLE

    # 4. Other language (e.g. Fluent German) -> INELIGIBLE
    v4 = _make_vac(title="Dev", location="Remote Worldwide", description="Remote worldwide. Fluent German required.")
    r4 = assess_vacancy_eligibility(v4)
    assert r4.language_requirement == LanguageRequirement.OTHER
    assert r4.eligibility == EligibilityStatus.INELIGIBLE


# --- 3. REMOTE PLATFORM DEFAULTS ---

def test_remote_platform_worldwide_defaults():
    # Vacancies from remoteok/weworkremotely/himalayas with clean locations default to Worldwide
    vac = _make_vac(
        source="remoteok",
        title="Senior Python Architect",
        location="Worldwide",
        description="Build scalable distributed APIs. Full-time contractor.",
    )
    res = assess_vacancy_eligibility(vac, candidate_country="TH")
    assert res.eligibility == EligibilityStatus.ELIGIBLE
    assert res.geo_scope == GeoScope.WORLDWIDE
