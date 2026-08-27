"""Stage 17B tests: HH application form extraction + normalization.

Uses REAL HH DOM patterns discovered during live inspection (2026-08-25):
  - question container: div[data-qa^='vacancy-response-question'] with text;
    second whitespace token of data-qa is the stable slug.
  - apply link: a[data-qa='vacancy-response-link-top'] (never clicked).
  - auth gate: div[data-qa='auth-form'] => answer controls hidden.
  - The six observed standard HH screening slugs.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

import pytest

from ai_assistant.hh_extractor import (
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionSource,
    QuestionType,
    extract_application_form,
    _stable_id,
    _classify_known_slug,
)
from ai_assistant import browser_executor as be


# ---- REAL HH fixtures (DOM patterns observed live) ----

REAL_HH_QUESTION_HTML = """
<div class="vacancy-response-suggest--wtk2n6t3gpQC1wc2 vacancy-response-suggest_magritte--dSwxz5ibseM6tqkJ">
  <div data-qa="vacancy-response-question vacancy-response-question_work_place_location" class="magritte-text___pbpft_5-3-11">Где располагается место работы?</div>
  <div data-qa="vacancy-response-question vacancy-response-question_employment_and_work_mode" class="magritte-text___pbpft_5-3-11">Какой график работы?</div>
  <div data-qa="vacancy-response-question vacancy-response-question_is_vacancy_open" class="magritte-text___pbpft_5-3-11">Вакансия открыта?</div>
  <div data-qa="vacancy-response-question vacancy-response-question_salary_options" class="magritte-text___pbpft_5-3-11">Какая оплата труда?</div>
  <div data-qa="vacancy-response-question vacancy-response-question_how_to_contact" class="magritte-text___pbpft_5-3-11">Как с вами связаться?</div>
  <div data-qa="vacancy-response-question vacancy-response-question_other" class="magritte-text___pbpft_5-3-11">Другой вопрос</div>
</div>
"""

REAL_HH_APPLY_LINK = '<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=135112049&amp;employerId=12843981">Откликнуться</a>'

REAL_HH_AUTH_FORM = '<div data-qa="auth-form"><input type="hidden" name="_xsrf" value="x"><input name="login" data-qa="account-signup-email"></div>'


def _snapshot(questions=None, auth_form=True, html=None, body_text=None, title="Вакансия X", url="https://hh.ru/vacancy/135112049", apply_link=None):
    if questions is None:
        questions = [
            {"label": "Где располагается место работы?", "slug": "work_place_location"},
            {"label": "Какой график работы?", "slug": "employment_and_work_mode"},
            {"label": "Вакансия открыта?", "slug": "is_vacancy_open"},
            {"label": "Какая оплата труда?", "slug": "salary_options"},
            {"label": "Как с вами связаться?", "slug": "how_to_contact"},
            {"label": "Другой вопрос", "slug": "other"},
        ]
    return {
        "html": html or (REAL_HH_QUESTION_HTML + (REAL_HH_AUTH_FORM if auth_form else "")),
        "body_text": body_text or "Вакансия Охранник (Чукотка) в Билибино",
        "questions": questions,
        "auth_form": auth_form,
        "apply_link": apply_link if apply_link is not None else {"href": "/applicant/vacancy_response?vacancyId=135112049", "text": "Откликнуться"},
        "final_url": url,
        "title": title,
        "site": "hh.ru",
    }


# ---- 1. empty form ----
def test_empty_form():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(questions=[], auth_form=False))
    assert form.source == "hh"
    assert form.questions == []
    assert form.application_type == ApplicationType.unknown


# ---- 2. profile fields (should be classified, but HH renders them as screening text) ----
def test_profile_fields_not_mixed_with_screening():
    # HH only exposes screening questions; profile fields come from profile source later.
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    for q in form.questions:
        assert q.source == QuestionSource.SCREENING
    assert all(q.source == QuestionSource.SCREENING for q in form.questions)


# ---- 3. text screening question ----
def test_screening_question_extracted():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert len(form.questions) == 6
    assert form.questions[0].label == "Где располагается место работы?"
    assert form.questions[0].id == "hh__work_place_location"
    assert form.questions[0].source == QuestionSource.SCREENING


# ---- 4. textarea screening question (slug with no type info -> UNKNOWN, not guessed) ----
def test_textarea_question_unknown_without_dom():
    # Without auth we cannot see it's a textarea; must stay UNKNOWN + review.
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert all(q.normalized_type == QuestionType.UNKNOWN for q in form.questions)
    assert all(q.requires_review for q in form.questions)


# ---- 5. select + options ----
def test_select_options_not_guessed_without_auth():
    # Options are NOT in DOM without login -> empty options, requires_review.
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    for q in form.questions:
        assert q.options == []
        assert q.requires_review is True


# ---- 6. radio + options ----
def test_radio_options_not_guessed_without_auth():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    for q in form.questions:
        assert q.options == []


# ---- 7. checkbox + options ----
def test_checkbox_options_not_guessed_without_auth():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    for q in form.questions:
        assert q.options == []


# ---- 8. required field ----
def test_required_not_guessed_without_auth():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    for q in form.questions:
        # Stage 20C contract: required is UNKNOWN (None) when the DOM proves
        # nothing - it must never be silently turned into False.
        assert q.required is None
        assert q.requires_review is True
        assert q.reason != ""


# ---- 9. unknown field type ----
def test_unknown_field_type():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert all(q.normalized_type == QuestionType.UNKNOWN for q in form.questions)


# ---- 10. deterministic question IDs ----
def test_deterministic_question_ids():
    f1 = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    f2 = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert [q.id for q in f1.questions] == [q.id for q in f2.questions]
    # stable slug-based ids
    assert f1.questions[0].id == "hh__work_place_location"
    assert all(not q.id.startswith("hh__hash_") for q in f1.questions)


def test_deterministic_hash_when_no_slug():
    # No slug -> deterministic hash from label, same label -> same id.
    a = _stable_id("Где располагается место работы?", None)
    b = _stable_id("Где располагается место работы?", None)
    assert a == b
    assert a.startswith("hh__hash_")
    c = _stable_id("Другой вопрос", None)
    assert a != c


# ---- 11. duplicate-looking questions get distinct stable ids ----
def test_duplicate_questions_distinct_ids():
    # Two questions with same label but different slugs -> distinct ids.
    snap = _snapshot(auth_form=False, questions=[
        {"label": "Вопрос?", "slug": "q1"},
        {"label": "Вопрос?", "slug": "q2"},
    ])
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", snap)
    ids = [q.id for q in form.questions]
    assert len(ids) == len(set(ids))
    assert ids[0] == "hh__q1"
    assert ids[1] == "hh__q2"


# ---- 12. captcha / login / cloudflare blocked ----
def test_auth_form_marks_answer_hidden():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert form.extraction_meta["auth_form"] is True


def test_no_auth_form_when_absent():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=False))
    assert form.extraction_meta["auth_form"] is False


def test_captcha_detected_from_challenge_container():
    html = REAL_HH_QUESTION_HTML + '<div data-qa="captcha"><input></div>'
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=False, html=html))
    assert form.extraction_meta["captcha"] is True


def test_cloudflare_detected():
    html = REAL_HH_QUESTION_HTML + '<div class="cf-challenge"><script>challenge-platform</script></div>'
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=False, html=html))
    assert form.extraction_meta["cloudflare"] is True


# ---- 13. extraction does not write DB ----
def test_extraction_no_db_writes(tmp_path, monkeypatch):
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

    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert form.source == "hh"
    assert writes["n"] == 0


# ---- 14. extraction does not call LLM ----
def test_extraction_no_llm():
    from ai_assistant import job_analyzer, application_prep
    calls = []
    with patch.object(job_analyzer, "_call_llm", lambda *a, **k: calls.append("llm") or "{}"), \
         patch.object(application_prep, "_call_llm_cover_letter", lambda *a, **k: calls.append("cl") or "{}"):
        form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert "llm" not in calls
    assert "cl" not in calls
    assert form.source == "hh"


# ---- 15. extraction does not submit / click / fill / upload ----
def test_extraction_no_submit_click_fill_upload():
    mock = be.MockBrowserAdapter(simulate={
        "questions": [
            {"label": "Где располагается место работы?", "slug": "work_place_location"},
            {"label": "Какой график работы?", "slug": "employment_and_work_mode"},
        ],
        "auth_form": True,
        "apply_link": {"href": "/applicant/vacancy_response?vacancyId=1", "text": "Откликнуться"},
        "site": "hh.ru",
    })
    # ensure submit/click are not invoked
    from ai_assistant.hh_extractor import extract_application_form
    snap = mock.extract_application_form()
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", snap)
    assert form.source == "hh"
    assert len(form.questions) == 2
    assert "submit_application" not in mock.calls
    assert "extract_application_form" in mock.calls
    assert not mock.submit_attempted
    # no fill / upload calls
    assert not any(c.startswith("fill:") for c in mock.calls)
    assert not any(c.startswith("upload:") for c in mock.calls)


# ---- 16. top-level extract_form_for_vacancy is read-only ----
def test_extract_form_for_vacancy_readonly():
    mock = be.MockBrowserAdapter(simulate={
        "questions": [
            {"label": "Где располагается место работы?", "slug": "work_place_location"},
            {"label": "Какой график работы?", "slug": "employment_and_work_mode"},
        ],
        "auth_form": True,
        "apply_link": {"href": "/x", "text": "Откликнуться"},
        "site": "hh.ru",
        "page_title": "Вакансия X",
        "final_url": "https://hh.ru/vacancy/1",
    })
    form = be.extract_form_for_vacancy("test:1", "https://hh.ru/vacancy/1", adapter=mock)
    assert form.source == "hh"
    assert len(form.questions) == 2
    assert "extract_application_form" in mock.calls
    assert not mock.submit_attempted
    assert not any(c.startswith("fill:") for c in mock.calls)
    assert not any(c.startswith("upload:") for c in mock.calls)
    assert mock.closed is True  # adapter closed after extraction


# ---- 17. application_type classification ----
def test_application_type_screening():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(auth_form=True))
    assert form.application_type == ApplicationType.screening_questions


def test_application_type_unknown_empty():
    form = extract_application_form("test:1", "https://hh.ru/vacancy/1", _snapshot(questions=[], auth_form=False))
    assert form.application_type == ApplicationType.unknown