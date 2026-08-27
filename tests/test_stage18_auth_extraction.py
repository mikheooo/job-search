"""Stage 18 tests: authenticated HH session interface + real controls extraction.

Covers:
- HH_STORAGE_STATE -> Playwright context wiring (never hardcoded/committed)
- real DOM controls -> ApplicationQuestion (SELECT/RADIO/CHECKBOX/TEXT/
  TEXTAREA/NUMBER/FILE) with real options and required flags
- UNKNOWN safety for unrecognized controls and auth-gated pages
- deterministic ids, end-to-end VALID package with real options
- safety: no submit/fill/DB writes; storage state protected by .gitignore
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

import pytest

from ai_assistant.hh_extractor import (
    ApplicationForm,
    ApplicationType,
    QuestionSource,
    QuestionType,
    build_questions_from_controls,
    extract_application_form,
)
from ai_assistant.application_qa import enrich_package_with_form
from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.schema import Vacancy
import ai_assistant.browser_executor as be


# ---------- fixtures ----------

def _profile():
    return CandidateProfile(
        desired_roles=["AI Automation Engineer"], alternative_roles=[],
        skills=["python"], preferred_seniority=[], years_experience=3,
        remote_required=True, allowed_locations=["Remote"], allowed_timezones=[],
        languages=["en"], employment_types=["Full Time"],
        minimum_salary=1500, salary_currency="USD",
        excluded_roles=[], excluded_companies=[], excluded_countries=[],
        excluded_industries=[],
    )


_RESUME = "Name: Ivan Petrov\nEmail: ivan@example.com\nPhone: +7 900 123 45 67\n5 years Python experience.\n"


def _vac():
    return Vacancy(source="hh", source_job_id="1", title="Dev", company="Co",
                   description="python", job_url="https://hh.ru/vacancy/1",
                   location="Remote", country_restrictions=[], timezone_restrictions=[],
                   salary_min=None, salary_max=None, salary_currency=None, employment_type=None)


def _deep():
    return DeepAnalysisResult(fit_score=80, recommendation="APPLY", why_fit=["python"],
                              gaps=[], must_have_requirements=[], nice_to_have_requirements=[],
                              matched_skills=["python"], missing_skills=[],
                              seniority_assessment="senior", remote_assessment="remote",
                              salary_assessment="ok", resume_adaptation_needed=False,
                              resume_adaptation_reasons=[], application_strategy="apply")


def _package():
    return ApplicationPackage(
        vacancy_id="hh:1", vacancy_stable_id="hh:1", resume_adaptation_needed=False,
        resume_summary="s", tailored_skills=["python"], relevant_experience=["e"],
        cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
        warnings=[], generator_version="v1",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"],
                                    relevant_experience_points=["e"]),
    )


def _snap(controls=None, auth_form=False, questions=None):
    return {
        "html": "", "body_text": "", "questions": questions or [],
        "controls": controls or [], "auth_form": auth_form,
        "apply_link": None, "final_url": "https://hh.ru/vacancy/1",
        "title": "V", "site": "hh.ru",
    }


# ---------- 1. storage_state wiring ----------

class _FakePage:
    def __init__(self):
        self.url = "https://hh.ru/vacancy/1"
    def title(self):
        return "Vacancy"
    def content(self):
        return ""
    def inner_text(self, sel):
        return ""
    def goto(self, url, **kw):
        pass
    def close(self):
        pass


class _FakeContext:
    def __init__(self, recorder):
        self._recorder = recorder
    def new_page(self):
        return _FakePage()
    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, recorder):
        self._recorder = recorder
    def new_context(self, **kw):
        self._recorder["context_kwargs"] = kw
        return _FakeContext(self._recorder)
    def close(self):
        pass


class _FakePlay:
    def __init__(self, recorder):
        self.chromium = _FakeChromium(recorder)
    def stop(self):
        pass


class _FakeChromium:
    def __init__(self, recorder):
        self._recorder = recorder
    def launch(self, headless=True):
        return _FakeBrowser(self._recorder)


class _FakePW:
    def __init__(self, recorder):
        self._recorder = recorder
    def start(self):
        return _FakePlay(self._recorder)


def test_storage_state_passed_to_context(monkeypatch):
    recorder = {}
    import playwright.sync_api as papi
    monkeypatch.setattr(papi, "sync_playwright", lambda: _FakePW(recorder))
    adapter = be.PlaywrightBrowserAdapter(headless=True, storage_state="C:/tmp/ss.json")
    adapter.open("https://hh.ru/vacancy/1")
    assert recorder["context_kwargs"].get("storage_state") == "C:/tmp/ss.json"
    adapter.close()


def test_no_storage_state_no_context_kwarg(monkeypatch):
    recorder = {}
    import playwright.sync_api as papi
    monkeypatch.setattr(papi, "sync_playwright", lambda: _FakePW(recorder))
    adapter = be.PlaywrightBrowserAdapter(headless=True)
    adapter.open("https://hh.ru/vacancy/1")
    assert "storage_state" not in recorder["context_kwargs"]
    adapter.close()


def test_missing_storage_state_file_blocks_gracefully(monkeypatch):
    """Stage 19 B-path: a storage_state pointing to a missing file must fail
    gracefully (blocked + reason), never crash the pipeline."""
    recorder = {}

    import playwright.sync_api as papi

    real_new_context = {}

    class StrictContext:
        def __init__(self, kw):
            real_new_context.update(kw)
            ss = kw.get("storage_state")
            if ss and not os.path.isfile(ss):
                raise FileNotFoundError(f"[Errno 2] No such file or directory: {ss}")
        def new_page(self):
            return _FakePage()
        def close(self):
            pass

    class StrictBrowser:
        def new_context(self, **kw):
            return StrictContext(kw)
        def close(self):
            pass

    class StrictChromium:
        def launch(self, headless=True):
            return StrictBrowser()

    class StrictPlay:
        def __init__(self):
            self.chromium = StrictChromium()
        def stop(self):
            pass

    monkeypatch.setattr(papi, "sync_playwright", lambda: type("PW", (), {"start": staticmethod(lambda: StrictPlay())})())
    missing = os.path.join(tempfile.gettempdir(), "definitely_missing_hh_session.json")
    if os.path.exists(missing):
        os.remove(missing)
    adapter = be.PlaywrightBrowserAdapter(headless=True, storage_state=missing)
    res = adapter.open("https://hh.ru/vacancy/1")
    assert res.get("blocked") is True
    assert "No such file or directory" in str(res.get("reason"))
    adapter.close()


def test_extract_form_for_vacancy_reads_env_storage_state(monkeypatch):
    captured = {}

    class SpyAdapter(be.MockBrowserAdapter):
        def __init__(self, simulate=None, **kwargs):
            super().__init__(simulate)
            captured["kwargs"] = kwargs
            captured["created"] = True

    monkeypatch.setenv("BROWSER_USE_PLAYWRIGHT", "1")
    monkeypatch.setenv("HH_STORAGE_STATE", "C:/tmp/hh_session.json")
    # Patch PlaywrightBrowserAdapter to avoid a real browser launch
    monkeypatch.setattr(be, "PlaywrightBrowserAdapter", SpyAdapter)
    form = be.extract_form_for_vacancy("hh:1", "https://hh.ru/vacancy/1")
    assert captured.get("created") is True
    assert captured["kwargs"].get("storage_state") == "C:/tmp/hh_session.json"
    assert form.source == "hh"


def test_gitignore_protects_storage_state():
    gi = pathlib.Path(".gitignore").read_text(encoding="utf-8")
    assert "storage_state" in gi


def test_no_storage_state_hardcoded_in_code():
    for f in ["ai_assistant/browser_executor.py", "ai_assistant/hh_extractor.py",
              "ai_assistant/application_qa.py"]:
        src = pathlib.Path(f).read_text(encoding="utf-8")
        assert "hh_storage_state.json" not in src  # no hardcoded session path
        # only env-based lookup allowed
        if "HH_STORAGE_STATE" in src:
            assert 'os.getenv("HH_STORAGE_STATE")' in src


# ---------- 2. real controls -> questions ----------

def test_select_with_real_options():
    controls = [{"tag": "SELECT", "type": "select", "name": "employment", "id": None,
                 "dataQa": "employment-type", "required": False, "label": "Занятость",
                 "options": ["Полная занятость", "Частичная занятость"]}]
    qs = build_questions_from_controls(controls)
    assert len(qs) == 1
    q = qs[0]
    assert q.normalized_type == QuestionType.SELECT
    assert q.options == ["Полная занятость", "Частичная занятость"]
    assert q.required is False
    assert q.requires_review is False
    assert q.id == "hh__ctrl_employment-type"


def test_radio_group_with_real_labels():
    controls = [
        {"tag": "INPUT", "type": "radio", "name": "work_place", "id": "r1", "dataQa": None,
         "required": True, "label": "Удалённо", "options": None},
        {"tag": "INPUT", "type": "radio", "name": "work_place", "id": "r2", "dataQa": None,
         "required": False, "label": "В офисе", "options": None},
    ]
    qs = build_questions_from_controls(controls)
    assert len(qs) == 1  # grouped into ONE question
    q = qs[0]
    assert q.normalized_type == QuestionType.RADIO
    assert q.options == ["Удалённо", "В офисе"]
    assert q.required is True  # any radio in group required -> group required
    assert q.id == "hh__ctrl_work_place"


def test_textarea_and_text_and_number_and_file():
    controls = [
        {"tag": "TEXTAREA", "type": "textarea", "name": "cover", "id": None,
         "dataQa": "cover-letter", "required": True, "label": "Сопроводительное письмо", "options": None},
        {"tag": "INPUT", "type": "text", "name": "phone", "id": None, "dataQa": None,
         "required": True, "label": "Телефон", "options": None},
        {"tag": "INPUT", "type": "number", "name": "salary", "id": None, "dataQa": None,
         "required": False, "label": "Желаемая зарплата", "options": None},
        {"tag": "INPUT", "type": "file", "name": "resume", "id": None, "dataQa": None,
         "required": True, "label": "Резюме", "options": None},
    ]
    qs = build_questions_from_controls(controls)
    types = {q.id: q.normalized_type for q in qs}
    req = {q.id: q.required for q in qs}
    assert types["hh__ctrl_cover-letter"] == QuestionType.TEXTAREA
    assert types["hh__ctrl_phone"] == QuestionType.TEXT
    assert types["hh__ctrl_salary"] == QuestionType.NUMBER
    assert types["hh__ctrl_resume"] == QuestionType.FILE
    assert req["hh__ctrl_cover-letter"] is True
    assert req["hh__ctrl_salary"] is False


def test_unknown_control_type_stays_unknown():
    controls = [{"tag": "INPUT", "type": "custom-widget", "name": "weird", "id": None,
                 "dataQa": None, "required": False, "label": "Странный виджет", "options": None}]
    qs = build_questions_from_controls(controls)
    assert qs[0].normalized_type == QuestionType.UNKNOWN
    assert qs[0].requires_review is True
    assert "not recognized" in qs[0].reason


def test_select_without_options_requires_review():
    controls = [{"tag": "SELECT", "type": "select", "name": "emp", "id": None,
                 "dataQa": None, "required": True, "label": "Занятость", "options": []}]
    qs = build_questions_from_controls(controls)
    assert qs[0].normalized_type == QuestionType.SELECT
    assert qs[0].options == []
    assert qs[0].requires_review is True
    assert "cannot resolve safely" in qs[0].reason


def test_auth_gate_ignores_controls():
    controls = [{"tag": "SELECT", "type": "select", "name": "emp", "id": None,
                 "dataQa": None, "required": True, "label": "Занятость",
                 "options": ["Полная занятость"]}]
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1",
                                    _snap(controls=controls, auth_form=True,
                                          questions=[{"label": "Вопрос?", "slug": "q"}]))
    assert len(form.questions) == 1
    assert form.questions[0].normalized_type == QuestionType.UNKNOWN
    assert form.extraction_meta["controls_used"] is False


def test_deterministic_control_ids():
    controls = [{"tag": "INPUT", "type": "text", "name": "phone", "id": None,
                 "dataQa": None, "required": False, "label": "Телефон", "options": None}]
    q1 = build_questions_from_controls(controls)[0]
    q2 = build_questions_from_controls(controls)[0]
    assert q1.id == q2.id
    # no name/id/dataQa -> stable hash from label
    anon = [{"tag": "INPUT", "type": "text", "name": None, "id": None,
             "dataQa": None, "required": False, "label": "Анонимное поле", "options": None}]
    a1 = build_questions_from_controls(anon)[0]
    a2 = build_questions_from_controls(anon)[0]
    assert a1.id == a2.id and a1.id.startswith("hh__hash_")


# ---------- 3. end-to-end: real options -> VALID package ----------

def test_real_options_form_resolves_to_valid_package():
    controls = [
        {"tag": "SELECT", "type": "select", "name": "employment", "id": None,
         "dataQa": "employment-type", "required": True, "label": "Занятость",
         "options": ["Full Time", "Part Time"]},
        {"tag": "INPUT", "type": "text", "name": "phone", "id": None, "dataQa": None,
         "required": True, "label": "Телефон", "options": None},
    ]
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1",
                                    _snap(controls=controls, auth_form=False))
    assert form.application_type == ApplicationType.screening_questions
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    # SELECT resolved to real option "Full Time" (profile truth), phone from resume
    by_id = {a.question_id: a for a in pkg.answers}
    assert by_id["hh__ctrl_employment-type"].answer == "Full Time"
    assert by_id["hh__ctrl_employment-type"].requires_review is False
    assert by_id["hh__ctrl_phone"].answer == "+7 900 123 45 67"
    assert pkg.validation_status == "VALID"


def test_real_options_invalid_select_needs_review():
    controls = [{"tag": "SELECT", "type": "select", "name": "employment", "id": None,
                 "dataQa": None, "required": True, "label": "Занятость",
                 "options": ["Только офис"]}]
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1",
                                    _snap(controls=controls, auth_form=False))
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert any("Required" in r for r in pkg.review_reasons)


# ---------- 4. safety ----------

def test_controls_normalization_no_db_writes(monkeypatch):
    import ai_assistant.db as db
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

    controls = [{"tag": "SELECT", "type": "select", "name": "e", "id": None,
                 "dataQa": None, "required": False, "label": "L", "options": ["a", "b"]}]
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1",
                                    _snap(controls=controls, auth_form=False))
    pkg = enrich_package_with_form(_package(), form, _profile(), _RESUME, _deep(), _vac())
    assert writes["n"] == 0


def test_mock_adapter_passes_controls_through():
    controls = [{"tag": "SELECT", "type": "select", "name": "e", "id": None,
                 "dataQa": None, "required": False, "label": "L", "options": ["a"]}]
    mock = be.MockBrowserAdapter(simulate={"controls": controls, "auth_form": False, "site": "hh.ru"})
    snap = mock.extract_application_form()
    assert snap["controls"] == controls
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", snap)
    assert form.questions[0].normalized_type == QuestionType.SELECT
    assert "submit_application" not in mock.calls
    assert not mock.submit_attempted