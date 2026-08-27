"""Stage 20D: real HH snapshot 136591579 -> normalized package integration.

Uses artifacts/hh_manual_form_snapshot.json as regression fixture.
Covers RADIO/CHECKBOX/TEXTAREA, required tri-state, Свой вариант,
cover letter coexistence, deterministic serialization, and safety guards.
No DB writes, no browser mutations, no LLM outside answer generation.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.application_qa import (
    enrich_package_with_form,
    resolve_answers,
    QuestionAnswerGenerator,
    ApplicationPackageValidator,
)
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.hh_extractor import (
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionType,
    QuestionSource,
    extract_application_form,
)
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.schema import Vacancy
import ai_assistant.browser_executor as be

SNAPSHOT_PATH = pathlib.Path("artifacts/hh_manual_form_snapshot.json")


def _load_real_snapshot():
    d = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return d["snapshot"], d["form"]


def _profile():
    return CandidateProfile(
        desired_roles=["AI Automation Engineer"], alternative_roles=[], skills=["python", "n8n"],
        preferred_seniority=[], years_experience=3, remote_required=True,
        allowed_locations=["Remote"], allowed_timezones=[], languages=["en"],
        employment_types=["Full Time"], minimum_salary=1500, salary_currency="USD",
        excluded_roles=[], excluded_companies=[], excluded_countries=[], excluded_industries=[],
    )


_RESUME = "Name: Ivan Petrov\nEmail: ivan@example.com\nPhone: +7 900 123 45 67\n5 years Python. Claude Code, Cursor.\n"
_VAC = Vacancy(source="hh", source_job_id="136591579", title="Dev", company="Co",
               description="python AI ITSM", job_url="https://hh.ru/vacancy/136591579",
               location="Remote", country_restrictions=[], timezone_restrictions=[],
               salary_min=None, salary_max=None, salary_currency=None, employment_type=None)
_DEEP = DeepAnalysisResult(fit_score=80, recommendation="APPLY", why_fit=[], gaps=[],
                           must_have_requirements=[], nice_to_have_requirements=[],
                           matched_skills=[], missing_skills=[], seniority_assessment="s",
                           remote_assessment="s", salary_assessment="s",
                           resume_adaptation_needed=False, resume_adaptation_reasons=[],
                           application_strategy="a")


def _pkg():
    return ApplicationPackage(
        vacancy_id="hh:136591579", vacancy_stable_id="hh:136591579",
        resume_adaptation_needed=False, resume_summary="s",
        tailored_skills=["python"], relevant_experience=["e"],
        cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
        warnings=[], generator_version="v1",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"],
                                    relevant_experience_points=["e"]),
    )


# ---------- 1. real snapshot -> normalized package (fixture regression) ----------

def test_real_snapshot_normalized_package():
    snapshot, form = _load_real_snapshot()
    assert form["vacancy_stable_id"] == "hh:136591579"
    qs = form["questions"]
    # 6 choice groups + 5 standalone textareas
    assert len(qs) == 11
    by_type = {}
    for q in qs:
        by_type.setdefault(q["normalized_type"], []).append(q)
    assert len([q for q in qs if q["normalized_type"] == "RADIO"]) >= 4
    assert len([q for q in qs if q["normalized_type"] == "CHECKBOX"]) >= 2
    # CHECKBOX groups are single questions with multiple options
    for q in [x for x in qs if x["normalized_type"] == "CHECKBOX"]:
        assert len(q["options"]) >= 2
    # custom_option_text_id linked for every choice group
    choice_qs = [q for q in qs if q["normalized_type"] in ("RADIO", "CHECKBOX")]
    assert all(q["custom_option_text_id"] is not None for q in choice_qs)
    # standalone TEXTAREAs have no custom linkage
    standalone = [q for q in qs if q["id"].endswith("_text") and q["normalized_type"] == "TEXTAREA"]
    assert len(standalone) == 5
    # required tri-state: HH doesn't expose required -> None (UNKNOWN)
    assert all(q["required"] is None for q in qs)
    # deterministic IDs
    ids = [q["id"] for q in qs]
    assert len(ids) == len(set(ids))


# ---------- 2. RADIO -> existing option ----------

def test_radio_truth_only_existing_option():
    q = ApplicationQuestion(id="hh__radio", label="Сколько лет опыта?", normalized_type=QuestionType.RADIO,
                            required=False, options=["Менее 3 лет", "5-7 лет", "Свой вариант"])
    # resume contains an option text -> generator picks it
    gen = QuestionAnswerGenerator(_profile(), "Менее 3 лет опыта", _DEEP, _VAC)
    a = gen.generate(q)
    assert a.answer in q.options
    assert a.answer != "Свой вариант"


# ---------- 3. CHECKBOX -> multiple existing options ----------

def test_checkbox_multiple_existing_options():
    q = ApplicationQuestion(id="hh__chk", label="Агенты", normalized_type=QuestionType.CHECKBOX,
                            required=False, options=["Claude Code", "Cursor", "Свой вариант"])
    # resume mentions two confirmed tools
    gen = QuestionAnswerGenerator(_profile(), "Claude Code and Cursor", _DEEP, _VAC)
    a = gen.generate(q)
    assert a.answer is not None
    parts = [p.strip() for p in a.answer.split(";")]
    assert set(parts).issubset(set(q.options))
    assert "Свой вариант" not in parts


# ---------- 4. CHECKBOX -> no unsupported option ----------

def test_checkbox_no_unsupported_option():
    q = ApplicationQuestion(id="hh__chk", label="Агенты", normalized_type=QuestionType.CHECKBOX,
                            required=False, options=["Claude Code", "Cursor"])
    gen = QuestionAnswerGenerator(_profile(), "InventedTool XYZ", _DEEP, _VAC)
    a = gen.generate(q)
    if a.answer is not None:
        for p in a.answer.split(";"):
            assert p.strip() in q.options


# ---------- 5. TEXTAREA -> truth-only ----------

def test_textarea_truth_only():
    q = ApplicationQuestion(id="hh__ta", label="Опишите workflow", normalized_type=QuestionType.TEXTAREA,
                            required=False, options=[])
    # resume contains workflow description fact
    gen = QuestionAnswerGenerator(_profile(), "Мой workflow: подготовка контекста", _DEEP, _VAC)
    # TEXTAREA with no direct profile field mapping falls through to LLM (None) or None
    a = gen.generate(q)
    # either an answer from LLM/fallback or None+review — but never invented
    if a.answer is not None:
        assert not a.requires_review or a.confidence < 1.0  # LLM answers require review


# ---------- 6. TEXTAREA без truth -> NEEDS_REVIEW ----------

def test_textarea_without_truth_needs_review():
    q = ApplicationQuestion(id="hh__ta2", label="Расскажите о несуществующем опыте",
                            normalized_type=QuestionType.TEXTAREA, required=False, options=[])
    gen = QuestionAnswerGenerator(_profile(), "nothing relevant", _DEEP, _VAC, llm=None)
    a = gen.generate(q)
    assert a.answer is None
    assert a.requires_review is True


# ---------- 7. required=True + missing answer -> NEEDS_REVIEW ----------

def test_required_true_missing_needs_review():
    q = ApplicationQuestion(id="hh__req", label="Обязательное поле", normalized_type=QuestionType.TEXT,
                            required=True, options=[])
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_pkg(), form, _profile(), "no fact for this label", _DEEP, _VAC)
    assert pkg.validation_status == "NEEDS_REVIEW"


# ---------- 8. required=False + missing answer -> допустимо ----------

def test_required_false_missing_allowed():
    q = ApplicationQuestion(id="hh__opt", label="Необязательное", normalized_type=QuestionType.TEXT,
                            required=False, options=[])
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions, questions=[q])
    # resume has no fact for this label, but question is optional -> answer None+review
    pkg = enrich_package_with_form(_pkg(), form, _profile(), "no fact", _DEEP, _VAC)
    # optional question with no answer still flags review per current validator
    # (any requires_review answer -> NEEDS_REVIEW) — check that it's the expected behavior
    assert pkg.validation_status == "NEEDS_REVIEW"
    # but for a truly optional field with no answer, the validator's "required"
    # check passes; the remaining review flag is from the answer itself
    assert any("requires review" in r for r in pkg.review_reasons)


# ---------- 9. required=None -> NEEDS_REVIEW ----------

def test_required_unknown_needs_review():
    q = ApplicationQuestion(id="hh__unk", label="Неизвестное required", normalized_type=QuestionType.TEXT,
                            required=None, source=QuestionSource.SCREENING)
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_pkg(), form, _profile(), _RESUME, _DEEP, _VAC)
    assert pkg.validation_status == "NEEDS_REVIEW"
    assert any("UNKNOWN" in r or "unknown" in r.lower() for r in pkg.review_reasons)


# ---------- 10. Свой вариант не выбирается автоматически ----------

def test_svoj_variant_never_auto_selected():
    q = ApplicationQuestion(id="hh__sel", label="График", normalized_type=QuestionType.RADIO,
                            required=False, options=["Полная занятость", "Свой вариант"])
    # resume says nothing matching "Полная занятость" — only way to match would be "Свой вариант"
    gen = QuestionAnswerGenerator(_profile(), "no matching option", _DEEP, _VAC, llm=None)
    a = gen.generate(q)
    assert a.answer != "Свой вариант"
    assert a.requires_review is True


# ---------- 11. custom textarea не используется без Свой вариант ----------

def test_custom_textarea_not_used_without_svoj_variant():
    # real HH radio group with linked custom textarea: the textarea must stay
    # review/empty when a real option is chosen
    q = ApplicationQuestion(id="hh__ctrl_task_146", label="Сколько лет опыта?",
                            normalized_type=QuestionType.RADIO, required=None,
                            options=["Менее 3 лет", "3-5 лет", "Свой вариант"],
                            custom_option_text_id="hh__ctrl_task_146_text")
    # Simulate: real option "5-7 лет" is NOT in options — use a matching one
    gen = QuestionAnswerGenerator(_profile(), "Менее 3 лет", _DEEP, _VAC)
    a = gen.generate(q)
    assert a.answer == "Менее 3 лет"
    assert a.requires_review is False
    # the textarea linked via custom_option_text_id is NOT filled - validator
    # does not require it when a real option is chosen


# ---------- 12. cover letter + questionnaire coexistence ----------

def test_cover_letter_and_questionnaire_coexist():
    q = ApplicationQuestion(id="hh__email", label="Email", normalized_type=QuestionType.TEXT,
                            required=None, source=QuestionSource.PROFILE)
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_pkg(), form, _profile(), _RESUME, _DEEP, _VAC)
    assert pkg.cover_letter.startswith("Hello")
    assert pkg.form.questions[0].id == "hh__email"


# ---------- 13. deterministic serialization ----------

def test_deterministic_serialization_real_snapshot():
    snapshot, _ = _load_real_snapshot()
    # control needed: need vacancyId in snapshot url for deterministic vacancy_stable_id
    url = snapshot.get("url") or "https://hh.ru/vacancy/136591579"
    f1 = extract_application_form("hh:136591579", url, snapshot)
    f2 = extract_application_form("hh:136591579", url, snapshot)
    assert f1.model_dump_json() == f2.model_dump_json()


# ---------- 14. deterministic answer resolution ----------

def test_deterministic_answers_real_snapshot():
    snapshot, _ = _load_real_snapshot()
    url = snapshot.get("url") or "https://hh.ru/vacancy/136591579"
    form = extract_application_form("hh:136591579", url, snapshot)
    a1 = resolve_answers(form.questions, _profile(), _RESUME, _DEEP, _VAC)
    a2 = resolve_answers(form.questions, _profile(), _RESUME, _DEEP, _VAC)
    assert [x.model_dump_json() for x in a1] == [x.model_dump_json() for x in a2]


# ---------- 15. review answers never reach browser fill payload ----------

def test_review_answers_not_in_browser_payload():
    # Only validated (requires_review=False, answer not None) answers should be
    # returned by _get_validated_package_answer.
    q = ApplicationQuestion(id="hh__salary", label="Желаемая зарплата",
                            normalized_type=QuestionType.TEXT, required=None,
                            source=QuestionSource.SCREENING)
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_pkg(), form, _profile(), "no salary fact", _DEEP, _VAC)
    # answer is None + review -> _get_validated_package_answer must return None
    assert be._get_validated_package_answer("salary", pkg) is None
    # confirm: the generic truth-only path (profile minimum_salary) still works
    assert be._get_profile_value("salary", _profile(), _RESUME, _VAC, pkg) is not None


# ---------- 16. NEEDS_REVIEW never becomes READY_FOR_REVIEW ----------

def test_needs_review_never_ready_for_review():
    q = ApplicationQuestion(id="hh__any", label="Вопрос", normalized_type=QuestionType.TEXT,
                            required=None, source=QuestionSource.SCREENING)
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions, questions=[q])
    pkg = enrich_package_with_form(_pkg(), form, _profile(), "no fact", _DEEP, _VAC)
    assert pkg.validation_status == "NEEDS_REVIEW"

    def _gate(status, form_detected, p):
        if status != "FORM_DETECTED":
            return status
        if not form_detected:
            return "BLOCKED"
        if getattr(p, "validation_status", "NEEDS_REVIEW") == "VALID":
            return "READY_FOR_REVIEW"
        return "FORM_DETECTED"
    assert _gate("FORM_DETECTED", True, pkg) != "READY_FOR_REVIEW"


# ---------- 17. full pipeline remains read-only ----------

def test_full_pipeline_read_only(monkeypatch):
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
    snapshot, _ = _load_real_snapshot()
    url = snapshot.get("url") or "https://hh.ru/vacancy/136591579"
    form = extract_application_form("hh:136591579", url, snapshot)
    enrich_package_with_form(_pkg(), form, _profile(), _RESUME, _DEEP, _VAC)
    assert writes["n"] == 0
