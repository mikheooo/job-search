"""Stage 17D integration tests: vacancy -> package -> validation -> browser.

Full pipeline: prepare_application -> extract form -> resolve answers ->
validate -> persist -> prepare_application_in_browser ->
READY_FOR_REVIEW only if validation_status == VALID.

Safety: no submit/click, no DB writes during extraction/validation,
deterministic serialization/answers, backward compatibility with old
packages.
"""

from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path

import pytest

import ai_assistant.config as config
import ai_assistant.db as db
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.application_queue import QueueItem, save_queue_item
from ai_assistant.application_prep import (
    ApplicationPackage,
    ResumeAdaptation,
    APPLICATION_PREP_VERSION,
    prepare_application,
)
from ai_assistant.hh_extractor import (
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionSource,
    QuestionType,
    extract_application_form,
)
from ai_assistant.application_qa import (
    prepare_package_with_form,
    enrich_package_with_form,
    resolve_answers,
    QuestionAnswerGenerator,
)
import ai_assistant.browser_executor as be


# ---------- fixtures ----------

def _vac(sid="1", **kw):
    d = dict(source="test", source_job_id=str(sid), title="AI Automation Engineer",
             company="TestCo", description="python n8n automation", job_url=None,
             location="Remote", country_restrictions=[], timezone_restrictions=[],
             salary_min=5000, salary_max=5000, salary_currency="USD",
             employment_type="Full Time")
    d.update(kw)
    if not d.get("job_url"):
        d["job_url"] = f"https://hh.ru/vacancy/{sid}"
    return Vacancy(**d)


def _profile():
    return CandidateProfile(
        desired_roles=["AI Automation Engineer"], alternative_roles=[],
        skills=["python", "n8n"], preferred_seniority=["senior"],
        years_experience=3, remote_required=True,
        allowed_locations=["Remote"], allowed_timezones=[],
        languages=["en"], employment_types=["Full Time"],
        minimum_salary=1500, salary_currency="USD",
        excluded_roles=[], excluded_companies=[], excluded_countries=[],
        excluded_industries=[],
    )


_RESUME = "Name: Ivan Petrov\nEmail: ivan@example.com\nPhone: +7 900 123 45 67\n5 years Python experience.\n"


def _deep():
    return DeepAnalysisResult(
        fit_score=90, recommendation="APPLY", why_fit=["python confirmed"], gaps=[],
        must_have_requirements=["python"], nice_to_have_requirements=[],
        matched_skills=["python"], missing_skills=[],
        seniority_assessment="senior", remote_assessment="remote",
        salary_assessment="ok", resume_adaptation_needed=False,
        resume_adaptation_reasons=[], application_strategy="apply",
    )


def _base_package(sid="1", **kw):
    vac = _vac(sid)
    d = dict(
        vacancy_id=vac.stable_id(), vacancy_stable_id=vac.stable_id(),
        resume_adaptation_needed=False, resume_summary="summary",
        tailored_skills=["python"], relevant_experience=["exp"],
        cover_letter="Hello " + " ".join(["word"] * 130),
        application_strategy="strategy", warnings=[],
        generator_version=APPLICATION_PREP_VERSION,
        adaptation=ResumeAdaptation(target_title="t", professional_summary="s",
                                    prioritized_skills=["python"],
                                    relevant_experience_points=["e"]),
    )
    d.update(kw)
    return ApplicationPackage(**d)


def _hh_questions_snapshot(auth_form=True, questions=None):
    if questions is None:
        questions = [
            {"label": "Где располагается место работы?", "slug": "work_place_location"},
            {"label": "Какой график работы?", "slug": "employment_and_work_mode"},
            {"label": "Какая оплата труда?", "slug": "salary_options"},
        ]
    return {
        "html": "", "body_text": "vacancy", "questions": questions,
        "auth_form": auth_form,
        "apply_link": {"href": "/applicant/vacancy_response?vacancyId=1", "text": "Откликнуться"},
        "final_url": "https://hh.ru/vacancy/1", "title": "V", "site": "hh.ru",
    }


def _hh_mock_adapter(auth_form=True, questions=None):
    return be.MockBrowserAdapter(simulate={
        "questions": questions if questions is not None else _hh_questions_snapshot(auth_form)["questions"],
        "auth_form": auth_form,
        "apply_link": {"href": "/applicant/vacancy_response?vacancyId=1", "text": "Откликнуться"},
        "site": "hh.ru",
        "page_title": "Vacancy",
        "final_url": "https://hh.ru/vacancy/1",
    })


def _setup_ready_vacancy(tmp_dir, sid="ready1"):
    db_file = str(Path(tmp_dir) / "t.db")
    orig = config.DB_FILE
    config.DB_FILE = db_file
    db.init_db()
    vac = _vac(sid)
    db.save_vacancy(vac)
    qitem = QueueItem(
        canonical_id="canonical_test",
        representative_vacancy_stable_id=vac.stable_id(),
        vacancy_stable_id=vac.stable_id(),
        priority_score=90, match_score=90, deep_score=85,
        company=vac.company, title=vac.title, source=vac.source,
        vacancy_url=vac.job_url, reasons=["high match"], warnings=[],
        rank=1, components={}, application_strategy="apply",
    )
    save_queue_item(qitem)
    set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY,
                           company=vac.company, title=vac.title, source=vac.source,
                           vacancy_url=vac.job_url, match_score=90, deep_score=85)
    return vac, db_file, orig


# ---------- 1. full pipeline: VALID -> READY_FOR_REVIEW ----------

def test_valid_package_reaches_ready_for_review(tmp_path):
    vac, db_file, orig = _setup_ready_vacancy(str(tmp_path))
    try:
        # Build a package with a resolvable required PROFILE question -> VALID
        pkg = _base_package(sid=vac.source_job_id)
        q = ApplicationQuestion(id="hh__email", label="Email", normalized_type=QuestionType.TEXT,
                                required=True, source=QuestionSource.PROFILE)
        form = ApplicationForm(source="hh", vacancy_stable_id=vac.stable_id(),
                               application_type=ApplicationType.screening_questions, questions=[q])
        pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _deep(), vac)
        assert pkg.validation_status == "VALID"
        db.save_application_package(vac.stable_id(), be.EXECUTOR_VERSION, pkg.model_dump_json())

        mock = be.MockBrowserAdapter(simulate={
            "fields": ["name", "email"], "apply_button": True, "site": "example.com",
        })
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.status == be.BrowserStatus.READY_FOR_REVIEW
        assert not mock.submit_attempted
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_needs_review_package_never_ready_for_review(tmp_path):
    vac, db_file, orig = _setup_ready_vacancy(str(tmp_path), sid="ready2")
    try:
        # Package with UNKNOWN HH questions -> NEEDS_REVIEW
        pkg = _base_package(sid=vac.source_job_id)
        q = ApplicationQuestion(id="hh__work_place_location", label="Где располагается место работы?",
                                normalized_type=QuestionType.UNKNOWN)
        form = ApplicationForm(source="hh", vacancy_stable_id=vac.stable_id(),
                               application_type=ApplicationType.screening_questions, questions=[q])
        pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _deep(), vac)
        assert pkg.validation_status == "NEEDS_REVIEW"
        db.save_application_package(vac.stable_id(), be.EXECUTOR_VERSION, pkg.model_dump_json())

        mock = be.MockBrowserAdapter(simulate={
            "fields": ["name", "email"], "apply_button": True, "site": "example.com",
        })
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert result.status != be.BrowserStatus.READY_FOR_REVIEW
        assert result.status == be.BrowserStatus.FORM_DETECTED
        assert any("NOT VALID" in w for w in result.warnings)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp_path, ignore_errors=True)


# ---------- 2. HH screening questions -> answers -> package ----------

def test_hh_screening_questions_flow_into_package():
    pkg = _base_package()
    adapter = _hh_mock_adapter(auth_form=True)
    pkg = prepare_package_with_form(pkg, "test:1", "https://hh.ru/vacancy/1",
                                    _profile(), _RESUME, _deep(), _vac(), adapter=adapter)
    assert pkg.application_type == ApplicationType.screening_questions
    assert len(pkg.form.questions) == 3
    assert len(pkg.answers) == 3
    # auth-gated fields stay UNKNOWN with explicit reason
    for a in pkg.answers:
        assert a.answer is None
        assert a.answer_type == QuestionType.UNKNOWN
        assert a.requires_review is True
        assert "authenticated session" in a.reason
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert any("auth gate" in r for r in pkg.review_reasons)


def test_unknown_questions_need_review():
    pkg = _base_package()
    adapter = _hh_mock_adapter(auth_form=True)
    pkg = prepare_package_with_form(pkg, "test:1", "https://hh.ru/vacancy/1",
                                    _profile(), _RESUME, _deep(), _vac(), adapter=adapter)
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert any("UNKNOWN" in r for r in pkg.review_reasons)


def test_missing_required_answer_needs_review():
    pkg = _base_package()
    q = ApplicationQuestion(id="hh__portfolio", label="Портфолио",
                            normalized_type=QuestionType.TEXT, required=True)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "NEEDS_REVIEW"


def test_valid_select_answer_package():
    pkg = _base_package()
    q = ApplicationQuestion(id="hh__employment", label="Занятость",
                            normalized_type=QuestionType.SELECT, required=False,
                            options=["Full Time", "Part Time"])
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _deep(), _vac())
    a = [x for x in pkg.answers if x.question_id == "hh__employment"][0]
    assert a.answer == "Full Time"
    assert a.requires_review is False
    assert pkg.validation_status == "VALID"


def test_invalid_select_answer_needs_review():
    pkg = _base_package()
    q = ApplicationQuestion(id="hh__employment", label="Занятость",
                            normalized_type=QuestionType.SELECT,
                            options=["Только офис", "Частичная"])
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "NEEDS_REVIEW"


def test_cover_letter_and_screening_answers_coexist():
    pkg = _base_package()
    assert pkg.cover_letter.startswith("Hello")
    q = ApplicationQuestion(id="hh__email", label="Email",
                            normalized_type=QuestionType.TEXT, required=False,
                            source=QuestionSource.PROFILE)
    form = ApplicationForm(source="hh", vacancy_stable_id="test:1",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.cover_letter.startswith("Hello")  # preserved
    assert any(a.answer == "ivan@example.com" for a in pkg.answers)
    assert pkg.validation_status == "VALID"


# ---------- 3. backward compatibility ----------

def test_old_package_json_backward_compatible():
    old = {
        "vacancy_id": "test:9", "vacancy_stable_id": "test:9",
        "resume_adaptation_needed": False, "resume_summary": "s",
        "tailored_skills": ["python"], "relevant_experience": ["e"],
        "cover_letter": "cl", "application_strategy": "st", "warnings": [],
        "generator_version": "v1",
        "adaptation": {"target_title": "t", "professional_summary": "p",
                       "prioritized_skills": ["python"], "relevant_experience_points": ["e"]},
    }
    pkg = ApplicationPackage.model_validate(old)
    assert pkg.vacancy_stable_id == "test:9"
    assert pkg.application_type == ApplicationType.unknown
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert pkg.answers == []
    assert pkg.form is None
    assert pkg.review_reasons == []


# ---------- 4. safety: no submit / no DB writes ----------

def test_extraction_auth_gate_does_not_submit():
    pkg = _base_package()
    adapter = _hh_mock_adapter(auth_form=True)
    pkg = prepare_package_with_form(pkg, "test:1", "https://hh.ru/vacancy/1",
                                    _profile(), _RESUME, _deep(), _vac(), adapter=adapter)
    assert "submit_application" not in adapter.calls
    assert not adapter.submit_attempted
    assert not any(c.startswith("fill:") for c in adapter.calls)
    assert not any(c.startswith("upload:") for c in adapter.calls)


def test_full_pipeline_no_submit_or_click(tmp_path):
    vac, db_file, orig = _setup_ready_vacancy(str(tmp_path), sid="safety1")
    try:
        pkg = _base_package(sid=vac.source_job_id)
        db.save_application_package(vac.stable_id(), be.EXECUTOR_VERSION, pkg.model_dump_json())
        mock = be.MockBrowserAdapter(simulate={"fields": ["name"], "apply_button": True})
        result = be.prepare_application_in_browser(vac.stable_id(), adapter=mock)
        assert "submit_application" not in mock.calls
        assert not mock.submit_attempted
        assert not any(c.startswith("upload:") for c in mock.calls)
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_no_db_writes_during_extraction_and_validation(monkeypatch):
    orig = db.get_connection
    writes = {"n": 0}

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

    monkeypatch.setattr(db, "get_connection", lambda: Spy(orig()))

    pkg = _base_package()
    adapter = _hh_mock_adapter(auth_form=True)
    prepare_package_with_form(pkg, "test:1", "https://hh.ru/vacancy/1",
                              _profile(), _RESUME, _deep(), _vac(), adapter=adapter)
    assert writes["n"] == 0


# ---------- 5. determinism ----------

def test_deterministic_package_serialization():
    def build():
        pkg = _base_package()
        adapter = _hh_mock_adapter(auth_form=True)
        return prepare_package_with_form(pkg, "test:1", "https://hh.ru/vacancy/1",
                                         _profile(), _RESUME, _deep(), _vac(), adapter=adapter)
    j1 = build().model_dump_json()
    j2 = build().model_dump_json()
    assert j1 == j2


def test_deterministic_answers():
    q1 = ApplicationQuestion(id="hh__email", label="Email",
                             normalized_type=QuestionType.TEXT, source=QuestionSource.PROFILE)
    q2 = ApplicationQuestion(id="hh__salary", label="Какая оплата труда?",
                             normalized_type=QuestionType.UNKNOWN)
    a1 = resolve_answers([q1, q2], _profile(), _RESUME, _deep(), _vac())
    b1 = resolve_answers([q1, q2], _profile(), _RESUME, _deep(), _vac())
    assert [x.model_dump_json() for x in a1] == [x.model_dump_json() for x in b1]


# ---------- 6. validated answers used in browser fill; review answers not ----------

def test_validated_answer_used_review_answer_ignored():
    pkg = _base_package()
    pkg.answers = [
        {"question_id": "hh__portfolio", "answer": "https://portfolio.example", "requires_review": False},
        {"question_id": "hh__salary", "answer": None, "requires_review": True},
    ]
    val = be._get_profile_value("portfolio", _profile(), _RESUME, _vac(), pkg)
    assert val == "https://portfolio.example"
    # UNKNOWN / requires_review answer must not be used
    val2 = be._get_profile_value("salary", _profile(), _RESUME, _vac(), pkg)
    # profile has minimum_salary -> returns profile truth (NOT a fabricated answer)
    assert val2 is not None and "думаю" not in val2
    pkg2 = _base_package()
    pkg2.answers = [{"question_id": "hh__salary", "answer": "думаю, нормально", "requires_review": True}]
    val3 = be._get_profile_value("salary", _profile(), _RESUME, _vac(), pkg2)
    assert val3 is not None and "думаю" not in val3  # profile truth, not the review-flagged answer
    assert val3.startswith("1500")


def test_extraction_failure_leaves_needs_review():
    pkg = _base_package()

    class BoomAdapter(be.MockBrowserAdapter):
        def extract_application_form(self):
            raise RuntimeError("browser crashed")

    adapter = BoomAdapter()
    pkg = prepare_package_with_form(pkg, "test:1", "https://hh.ru/vacancy/1",
                                    _profile(), _RESUME, _deep(), _vac(), adapter=adapter)
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert any("extraction failed" in r for r in pkg.review_reasons)


def test_cli_prepare_integrates_form_step(tmp_path, monkeypatch):
    """prepare_applications must run extraction+validation before persisting."""
    from ai_assistant import cli
    db_file = str(Path(tmp_path) / "t.db")
    orig = config.DB_FILE
    config.DB_FILE = db_file
    db.init_db()
    try:
        vac = _vac("cli1")
        db.save_vacancy(vac)
        # profile file
        prof_file = Path(tmp_path) / "profile.json"
        prof_file.write_text(json.dumps({
            "desired_roles": ["AI Automation Engineer"], "skills": ["python", "n8n"],
            "years_experience": 3, "remote_required": True,
            "allowed_locations": ["Remote"], "languages": ["en"],
            "employment_types": ["Full Time"], "minimum_salary": 1500, "salary_currency": "USD",
        }), encoding="utf-8")
        monkeypatch.setattr(cli, "list_vacancies", lambda limit=10: [db.get_vacancy_by_id(vac.stable_id())])
        monkeypatch.setattr(cli, "BATCH_LIMIT", 5)
        res = cli.prepare_applications(5, profile_path=str(prof_file))
        row = db.get_application_package(vac.stable_id(), APPLICATION_PREP_VERSION)
        assert row is not None
        data = json.loads(row[2])
        # form step executed: HH mock (no adapter -> Mock default) produced questions
        assert "application_type" in data
        assert "validation_status" in data
        assert data["validation_status"] in ("VALID", "NEEDS_REVIEW")
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp_path, ignore_errors=True)