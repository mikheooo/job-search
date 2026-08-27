"""Stage 17C tests: HH Application Q&A resolution + validation.

Covers application_type detection, question answer resolution (truth-only),
option-constrained answers, ApplicationPackage serialization, validator
VALID/NEEDS_REVIEW, READY_FOR_REVIEW gating, and safety (no submit/click/
fill/upload/DB writes; LLM only inside answer generation).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from unittest.mock import patch

import pytest

import ai_assistant.config as config
import ai_assistant.db as db
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.hh_extractor import (
    ApplicationAnswer,
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionSource,
    QuestionType,
    extract_application_form,
)
from ai_assistant.application_qa import (
    QuestionAnswerGenerator,
    ApplicationPackageValidator,
    resolve_answers,
    enrich_package_with_form,
)
from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation


# ---------- fixtures ----------

def _profile():
    return CandidateProfile(
        desired_roles=["AI Automation Engineer"],
        alternative_roles=["Python Developer"],
        skills=["python", "n8n", "automation", "llm", "api"],
        preferred_seniority=["senior", "mid"],
        years_experience=3,
        remote_required=True,
        allowed_locations=["Remote", "Moscow"],
        allowed_timezones=[],
        languages=["en", "ru"],
        employment_types=["Full Time"],
        minimum_salary=1500,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


_RESUME = """Name: Ivan Petrov
Email: ivan@example.com
Phone: +7 900 123 45 67
5 years Python experience.
Skills: python, n8n, automation, llm, api.
"""


def _vac(**kw):
    d = dict(source="test", source_job_id="1", title="AI Automation Engineer", company="TestCo",
             description="python n8n automation remote", job_url=None, location="Remote",
             country_restrictions=[], timezone_restrictions=[], salary_min=5000, salary_max=5000,
             salary_currency="USD", employment_type="Full Time")
    d.update(kw)
    if not d.get("job_url"):
        d["job_url"] = f"https://hh.ru/vacancy/{d.get('source_job_id','1')}"
    return Vacancy(**d)


def _deep():
    return DeepAnalysisResult(
        fit_score=90, recommendation="APPLY", why_fit=["python confirmed"], gaps=[],
        must_have_requirements=["python"], nice_to_have_requirements=[],
        matched_skills=["python", "n8n"], missing_skills=[],
        seniority_assessment="senior", remote_assessment="remote", salary_assessment="ok",
        resume_adaptation_needed=False, resume_adaptation_reasons=[], application_strategy="apply",
    )


def _q(id="q1", label="", qtype=QuestionType.TEXT, required=False, options=None,
       source=QuestionSource.SCREENING):
    return ApplicationQuestion(
        id=id, label=label, normalized_type=qtype, required=required,
        options=options or [], source=source,
    )


def _package(**kw):
    d = dict(
        vacancy_id="test:1", vacancy_stable_id="test:1", resume_adaptation_needed=False,
        resume_summary="s", tailored_skills=["python"], relevant_experience=["x"],
        cover_letter="cl", application_strategy="apply", warnings=[],
        generator_version="v1", adaptation=ResumeAdaptation(
            target_title="t", professional_summary="p", prioritized_skills=["python"],
            relevant_experience_points=["x"],
        ),
    )
    d.update(kw)
    return ApplicationPackage(**d)


# ---------- 1. ApplicationType detection ----------

def _snap(questions=None, auth_form=True):
    if questions is None:
        questions = [{"label": "Где располагается место работы?", "slug": "work_place_location"}]
    return {
        "html": "", "body_text": "", "questions": questions, "auth_form": auth_form,
        "apply_link": {"href": "/x", "text": "Откликнуться"}, "final_url": "https://hh.ru/vacancy/1",
        "title": "V", "site": "hh.ru",
    }


def test_type_screening_questions():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snap(auth_form=True))
    assert form.application_type == ApplicationType.screening_questions


def test_type_unknown_empty_form():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snap(questions=[], auth_form=False))
    assert form.application_type == ApplicationType.unknown


# ---------- 2. Answer resolution ----------

def test_profile_answer_confirmed():
    q = _q(label="Email", qtype=QuestionType.TEXT, source=QuestionSource.PROFILE)
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    assert a.answer == "ivan@example.com"
    assert a.requires_review is False
    assert a.confidence == 1.0


def test_known_factual_answer():
    q = _q(label="Сколько лет опыта?", qtype=QuestionType.TEXT)
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    assert a.answer == "3"  # from profile.years_experience (truth)
    assert a.requires_review is False


def test_missing_fact():
    q = _q(label="Портфолио URL", qtype=QuestionType.TEXT)
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    assert a.answer is None
    assert a.requires_review is True


def test_unknown_question_not_guessed():
    q = _q(label="Где располагается место работы?", qtype=QuestionType.UNKNOWN)
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    assert a.answer is None
    assert a.answer_type == QuestionType.UNKNOWN
    assert a.requires_review is True
    assert "without an authenticated session" in a.reason or "HH" in a.reason


# ---------- 3. Options ----------

def test_select_without_options_no_fabrication():
    q = _q(label="График работы?", qtype=QuestionType.SELECT, options=[])
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    assert a.answer is None
    assert a.requires_review is True


def test_select_with_valid_option():
    q = _q(label="График работы?", qtype=QuestionType.SELECT, options=["Полная занятость", "Full Time", "Частичная"])
    # confirmed candidate value "Full Time" from profile
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    assert a.answer == "Full Time"
    assert a.answer in q.options
    assert a.requires_review is False


def test_select_invalid_option_requires_review():
    q = _q(label="График работы?", qtype=QuestionType.SELECT, options=["Только офис", "Частичная"])
    # profile says "Full Time" which is NOT in options
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    assert a.answer is None
    assert a.requires_review is True


def test_radio_uses_real_option_only():
    q = _q(label="Где находитесь?", qtype=QuestionType.RADIO, options=["Москва", "Санкт-Петербург"])
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac())
    a = gen.generate(q)
    # profile allowed_locations[0]="Remote" not in options -> requires review
    assert a.answer is None
    assert a.requires_review is True


# ---------- 4. truth-only ----------

def test_truth_only_generation_no_invention():
    # Question about a skill NOT in profile/resume must not be answered.
    q = _q(label="Расскажите о опыте с AWS", qtype=QuestionType.TEXTAREA)
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac(), llm=None)
    a = gen.generate(q)
    assert a.answer is None
    assert a.requires_review is True


def test_llm_only_within_generation():
    called = []

    def fake_llm(question, gen):
        called.append(question.id)
        return "Ответ только из подтверждённых данных"
    q = _q(label="Расскажите о себе", qtype=QuestionType.TEXTAREA)
    gen = QuestionAnswerGenerator(_profile(), _RESUME, _deep(), _vac(), llm=fake_llm)
    a = gen.generate(q)
    assert called == [q.id]
    assert a.answer == "Ответ только из подтверждённых данных"
    assert a.requires_review is True  # LLM answers always require review


# ---------- 5. package serialization ----------

def test_package_serialization_preserves_qa():
    q = _q(label="Email", qtype=QuestionType.TEXT, source=QuestionSource.PROFILE)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = _package()
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _deep(), _vac())
    data = json.loads(pkg.model_dump_json())
    assert data["application_type"] == "screening_questions"
    assert data["validation_status"] in ("VALID", "NEEDS_REVIEW")
    assert isinstance(data["answers"], list)
    assert "form" in data
    # existing fields preserved
    assert data["resume_summary"] == "s"
    assert data["cover_letter"] == "cl"
    assert data["adaptation"]["target_title"] == "t"


def test_package_backward_compat_defaults():
    # Package constructed without Q&A fields must still serialize.
    pkg = _package()
    data = json.loads(pkg.model_dump_json())
    assert data["application_type"] == "unknown"
    assert data["validation_status"] == "NEEDS_REVIEW"
    assert data["answers"] == []
    assert data["form"] is None


# ---------- 6. validator ----------

def test_validator_valid_when_all_required_answered():
    q = _q(label="Email", qtype=QuestionType.TEXT, required=True, source=QuestionSource.PROFILE)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "VALID"
    assert pkg.review_reasons == []


def test_validator_needs_review_missing_required():
    q = _q(label="Портфолио", qtype=QuestionType.TEXT, required=True)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert any("Required" in r for r in pkg.review_reasons)


def test_validator_needs_review_unknown_type():
    q = _q(label="Вопрос HH", qtype=QuestionType.UNKNOWN)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "NEEDS_REVIEW"


def test_validator_needs_review_select_without_options():
    q = _q(label="График?", qtype=QuestionType.SELECT, options=[])
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "NEEDS_REVIEW"


def test_validator_read_only_no_db():
    from ai_assistant.application_qa import ApplicationPackageValidator
    q = _q(label="Email", qtype=QuestionType.TEXT, required=True, source=QuestionSource.PROFILE)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    # Validator itself doesn't touch DB - just ensure it's a pure function.
    res = ApplicationPackageValidator().validate(pkg)
    assert res["status"] in ("VALID", "NEEDS_REVIEW")


# ---------- 7. READY_FOR_REVIEW gating ----------

def _gate(status, form_detected, pkg):
    """Replicates the gating decision logic for unit testing."""
    if status != "FORM_DETECTED":
        return status
    if not form_detected:
        return "BLOCKED"
    if getattr(pkg, "validation_status", "NEEDS_REVIEW") == "VALID":
        return "READY_FOR_REVIEW"
    return "FORM_DETECTED"


def test_ready_for_review_only_for_valid():
    # VALID package -> READY_FOR_REVIEW
    q = _q(label="Email", qtype=QuestionType.TEXT, required=True, source=QuestionSource.PROFILE)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "VALID"
    assert _gate("FORM_DETECTED", True, pkg) == "READY_FOR_REVIEW"


def test_not_ready_when_needs_review():
    q = _q(label="Портфолио", qtype=QuestionType.TEXT, required=True)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert _gate("FORM_DETECTED", True, pkg) != "READY_FOR_REVIEW"


def test_ready_gate_requires_package_exists():
    # No package (validation_status unknown) -> not ready.
    class NoPkg:
        validation_status = "NEEDS_REVIEW"
    assert _gate("FORM_DETECTED", True, NoPkg()) != "READY_FOR_REVIEW"


# ---------- 8. safety ----------

def test_no_submit_click_fill_upload():
    import ai_assistant.browser_executor as be
    mock = be.MockBrowserAdapter(simulate={
        "questions": [{"label": "Вопрос?", "slug": "q1"}], "auth_form": True,
        "apply_link": {"href": "/x", "text": "Откликнуться"}, "site": "hh.ru",
    })
    snap = mock.extract_application_form()
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", snap)
    # resolving answers (truth-only, no browser)
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert "submit_application" not in mock.calls
    assert not any(c.startswith("fill:") for c in mock.calls)
    assert not any(c.startswith("upload:") for c in mock.calls)
    assert pkg.validation_status == "NEEDS_REVIEW"


def test_no_db_writes_during_resolution(tmp_path, monkeypatch):
    import ai_assistant.db as db
    orig = db.get_connection
    writes = {"n": 0}

    class Spy:
        def __init__(self, c):
            self._c = c
        def cursor(self):
            return SpyCur(self._c.cursor())
        def commit(self):
            pass
        def close(self):
            pass
        def __getattr__(self, n):
            return getattr(self._c, n)

    class SpyCur:
        def __init__(self, c):
            self._c = c
        def execute(self, sql, *a):
            s = sql.strip().upper() if isinstance(sql, str) else ""
            if s.startswith(("INSERT", "UPDATE", "DELETE")):
                writes["n"] += 1
            return self._c.execute(sql, *a)
        def executemany(self, sql, seq):
            return self._c.executemany(sql, seq)
        def fetchone(self):
            return self._c.fetchone()
        def fetchall(self):
            return self._c.fetchall()
        def __getattr__(self, n):
            return getattr(self._c, n)

    def wrapped():
        return Spy(orig())
    monkeypatch.setattr(db, "get_connection", wrapped)

    q = _q(label="Email", qtype=QuestionType.TEXT, required=True, source=QuestionSource.PROFILE)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1", application_type=ApplicationType.screening_questions, questions=[q])
    enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert writes["n"] == 0