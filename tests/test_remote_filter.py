"""Unit tests for Strict Remote-Only vacancy filtering."""

from __future__ import annotations

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.remote_filter import is_strictly_remote, classify_work_format
from ai_assistant.matcher import JobMatcher, JobProfile
from ai_assistant.candidate_profile import CandidateProfile


def _make_vac(**kwargs) -> Vacancy:
    defaults = dict(
        source="custom",
        source_job_id="test-1",
        title="Software Engineer",
        company="TechCo",
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


# --- 1. PASS: Confirmed Remote-Only ---

def test_pass_explicit_russian_remote():
    vacs = [
        _make_vac(title="Python Разработчик (Удаленно)", description="Полностью удаленная работа для опытного разработчика."),
        _make_vac(title="AI Engineer", location="Удаленно", description="Стек: Python, FastAPI, LLM."),
        _make_vac(title="DevOps", description="Формат: удаленная работа. Команда распределенная по миру."),
        _make_vac(title="QA Engineer", description="Удаленка на 100%. График гибкий."),
        _make_vac(title="ML Engineer", location="Дистанционно", description="Дистанционная работа по ТК РФ."),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is True, f"Expected PASS for {v.title} / {v.description}, got REJECT: {reason}"


def test_pass_explicit_english_remote():
    vacs = [
        _make_vac(title="Senior Python Engineer", location="Remote", description="Fully remote position anywhere in the world."),
        _make_vac(title="AI Automation Lead", location="Remote (Worldwide)", description="Work remotely with our global team."),
        _make_vac(title="Fullstack Developer", description="100% remote role. We are a remote-first company."),
        _make_vac(title="Data Scientist", description="This is a strictly remote-only opportunity. Work from anywhere."),
        _make_vac(source="remoteok", title="Backend Engineer", location="Worldwide", description="Building distributed systems."),
        _make_vac(source="weworkremotely", title="Frontend Engineer", location="USA / Europe", description="React + TypeScript."),
        _make_vac(source="himalayas", title="Solutions Architect", location="Worldwide", description="Cloud architecture."),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is True, f"Expected PASS for {v.title} / {v.description}, got REJECT: {reason}"


def test_pass_home_office_not_rejected():
    # 'home office' or 'домашний офис' should NOT trigger onsite office rejection
    v = _make_vac(
        title="Senior Python Developer (Remote)",
        location="Remote",
        description="Fully remote position. We provide a home office stipend to equip your workspace.",
    )
    ok, reason = is_strictly_remote(v)
    assert ok is True, f"Expected PASS with home office setup, got: {reason}"


# --- 2. REJECT: Hybrid / Office / Onsite ---

def test_reject_hybrid():
    vacs = [
        _make_vac(title="Python Developer (Гибрид)", description="Формат работы: гибридный график (2 дня в офисе, 3 удаленно)."),
        _make_vac(title="AI Engineer", location="Remote / Hybrid", description="Hybrid role with occasional meetings."),
        _make_vac(title="Fullstack Developer", description="We offer a hybrid working model based in London."),
        _make_vac(title="DevOps", description="Частично удаленно, частично в нашем офисе."),
        _make_vac(title="Backend Developer", location="Гибрид", description="Удаленка 3 дня, 2 дня офис."),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is False, f"Expected REJECT for hybrid vacancy {v.title}"
        assert "non-remote" in reason or "Rejected" in reason


def test_reject_onsite_and_office():
    vacs = [
        _make_vac(title="Python Dev", location="Москва (Офис)", description="Работа в офисе компании, м. Белорусская."),
        _make_vac(title="AI Specialist", location="Onsite", description="On-site position in Berlin headquarters."),
        _make_vac(title="Data Engineer", description="На территории работодателя, Санкт-Петербург."),
        _make_vac(title="ML Engineer", location="Office only", description="Work from our office."),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is False, f"Expected REJECT for onsite/office {v.title}"


def test_reject_office_visits_and_attendance():
    vacs = [
        _make_vac(title="Python Dev", location="Удаленно", description="Удаленная работа, но требуется 1 день в неделю быть в офисе."),
        _make_vac(title="AI Engineer", location="Remote", description="Remote work with mandatory office attendance twice a month."),
        _make_vac(title="Backend Dev", description="Удаленно. Иногда требуется посещение офиса в Москве для планирования."),
        _make_vac(title="Fullstack Lead", description="Fully remote, but must visit our office quarterly and come to the office for onboarding."),
        _make_vac(title="QA Engineer", description="Удаленка, но периодические приезды в офис обязательны."),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is False, f"Expected REJECT for office visits requirement: {v.description}"


# --- 3. REJECT: Conditional / Weak / Ambiguous Remote ---

def test_reject_conditional_can_remote():
    vacs = [
        _make_vac(title="Python Developer", description="Можно удалённо. Рассматриваем кандидатов из РФ."),
        _make_vac(title="Data Scientist", description="Можно удаленно при успешном прохождении собеседования."),
        _make_vac(title="Machine Learning Engineer", description="Возможна удаленная работа для сильных кандидатов."),
        _make_vac(title="Backend Dev", description="Возможность удаленной работы по согласованию с тимлидом."),
        _make_vac(title="AI Engineer", description="Remote optional based on team preference."),
        _make_vac(title="Solutions Architect", description="Open to remote candidates."),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is False, f"Expected REJECT for conditional remote: {v.description}"


def test_reject_negotiable_and_probation():
    vacs = [
        _make_vac(title="Python Dev", description="Удаленно по договоренности."),
        _make_vac(title="Fullstack", description="График работы: по договоренности."),
        _make_vac(title="AI Engineer", description="Удаленная работа после испытательного срока (3 месяца)."),
        _make_vac(title="QA Lead", description="Remote after probation period."),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is False, f"Expected REJECT for negotiable / probation remote: {v.description}"


def test_reject_unknown_and_insufficient_data():
    vacs = [
        _make_vac(title="Software Engineer", location=None, description="Building scalable backend services in Go and Python."),
        _make_vac(title="Lead Engineer", location="Москва", description="Ищем тимлида в команду платежей."),
        _make_vac(title="QA Engineer", location="", description=""),
    ]
    for v in vacs:
        ok, reason = is_strictly_remote(v)
        assert ok is False, f"Expected REJECT for unknown/insufficient data: {v.title}"


# --- 4. Remote Platforms with Embedded Hybrid Override ---

def test_remote_platform_with_hybrid_override_rejected():
    # If a vacancy is on RemoteOK / WeWorkRemotely but employer secretly wrote 'hybrid' or 'office days'
    vac = _make_vac(
        source="remoteok",
        title="Fullstack Developer",
        location="New York",
        description="We are listed on remoteok, but this specific role is hybrid with 2 days in office required.",
    )
    ok, reason = is_strictly_remote(vac)
    assert ok is False, "Expected REJECT when remote board listing contains hybrid/office requirement"


# --- 5. JobMatcher Hard Constraint Integration ---

def test_matcher_hard_rejects_non_strictly_remote():
    profile = CandidateProfile(
        desired_roles=["Python Developer"],
        skills=["python"],
        remote_required=True,
    )
    matcher = JobMatcher(profile)

    # 1. Strictly remote -> PASS
    vac_remote = _make_vac(
        title="Python Developer",
        location="Remote",
        description="python developer fully remote work",
    )
    res_remote = matcher.match(vac_remote)
    assert res_remote.decision != "SKIP"
    assert res_remote.score > 0

    # 2. Hybrid -> SKIP
    vac_hybrid = _make_vac(
        title="Python Developer",
        location="Москва",
        description="python developer гибрид 2 дня офис",
    )
    res_hybrid = matcher.match(vac_hybrid)
    assert res_hybrid.decision == "SKIP"
    assert res_hybrid.score == 0
    assert any("Remote required" in r for r in res_hybrid.reasons)

    # 3. «Можно удаленно» -> SKIP
    vac_conditional = _make_vac(
        title="Python Developer",
        location="Москва",
        description="python developer можно удаленно",
    )
    res_cond = matcher.match(vac_conditional)
    assert res_cond.decision == "SKIP"
    assert res_cond.score == 0
    assert any("Remote required" in r for r in res_cond.reasons)
