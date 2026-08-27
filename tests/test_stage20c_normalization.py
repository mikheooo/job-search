"""Stage 20C tests: checkbox grouping, question stems, Свой вариант,
vacancyId, required tri-state, truth-only checkbox.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant.hh_extractor import (
    ApplicationForm,
    QuestionType,
    build_questions_from_controls,
    extract_application_form,
)
from ai_assistant.application_qa import enrich_package_with_form, QuestionAnswerGenerator
from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.schema import Vacancy
from tools.capture_manual_form import _vacancy_stable_id_from_url


def _profile():
    return CandidateProfile(
        desired_roles=["AI"], alternative_roles=[], skills=["python", "n8n"],
        preferred_seniority=[], years_experience=3, remote_required=True,
        allowed_locations=["Remote"], allowed_timezones=[], languages=["en"],
        employment_types=["Full Time"], minimum_salary=1500, salary_currency="USD",
        excluded_roles=[], excluded_companies=[], excluded_countries=[], excluded_industries=[],
    )


_RESUME = "Name: Ivan Petrov\nEmail: ivan@example.com\n5 years Python.\n"
_VAC = Vacancy(source="hh", source_job_id="1", title="Dev", company="Co",
               description="python", job_url="https://hh.ru/vacancy/1",
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
        vacancy_id="hh:1", vacancy_stable_id="hh:1", resume_adaptation_needed=False,
        resume_summary="s", tailored_skills=["python"], relevant_experience=["e"],
        cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
        warnings=[], generator_version="v1",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"], relevant_experience_points=["e"]),
    )


def _snap(controls=None, question_groups=None, auth_form=False):
    return {
        "html": "", "body_text": "", "questions": [],
        "controls": controls or [], "question_groups": question_groups or [],
        "auth_form": auth_form, "apply_link": None,
        "final_url": "https://hh.ru/vacancy/1", "title": "V", "site": "hh.ru",
    }


# ---- CHECKBOX grouping ----

def test_checkbox_grouped_to_single_question():
    controls = [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "id": None, "dataQa": None,
         "label": "Claude Code", "required": False, "requiredAttr": None, "options": None},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "id": None, "dataQa": None,
         "label": "Cursor", "required": False, "requiredAttr": None, "options": None},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "id": None, "dataQa": None,
         "label": "Свой вариант", "required": False, "requiredAttr": None, "options": None},
    ]
    qs = build_questions_from_controls(controls)
    assert len(qs) == 1
    q = qs[0]
    assert q.id == "hh__ctrl_task_151"
    assert q.normalized_type == QuestionType.CHECKBOX
    assert q.options == ["Claude Code", "Cursor", "Свой вариант"]


def test_checkbox_custom_text_linked():
    controls = [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "id": None, "dataQa": None,
         "label": "Claude Code", "required": False, "requiredAttr": None, "options": None},
        {"tag": "TEXTAREA", "type": "textarea", "name": "task_151_text", "id": None, "dataQa": None,
         "label": None, "required": False, "requiredAttr": None, "options": None},
    ]
    qs = build_questions_from_controls(controls)
    # only one question (group); _text textarea is consumed
    assert len(qs) == 1
    assert qs[0].custom_option_text_id == "hh__ctrl_task_151_text"
    # no standalone textarea question for _text
    assert not any("151_text" in q.id and q.normalized_type == QuestionType.TEXTAREA for q in qs)


def test_checkbox_textarea_not_emitted_separately():
    controls = [
        {"tag": "INPUT", "type": "checkbox", "name": "task_999", "id": None, "dataQa": None,
         "label": "A", "required": False, "requiredAttr": None, "options": None},
        {"tag": "TEXTAREA", "type": "textarea", "name": "task_999_text", "id": None, "dataQa": None,
         "label": None, "required": False, "requiredAttr": None, "options": None},
    ]
    qs = build_questions_from_controls(controls)
    assert len(qs) == 1  # only the group, textarea consumed
    ids = [q.id for q in qs]
    assert "hh__ctrl_task_999" in ids
    assert "hh__ctrl_task_999_text" not in ids


# ---- vacancyId parsing ----

def test_vacancyId_normal():
    assert _vacancy_stable_id_from_url(
        "https://hh.ru/applicant/vacancy_response?vacancyId=136591579&hhtmFrom=main") == "hh:136591579"


def test_vacancyId_missing():
    assert _vacancy_stable_id_from_url("https://hh.ru/vacancy/1") is None
    assert _vacancy_stable_id_from_url("https://hh.ru/applicant/vacancy_response?hhtmFrom=main") is None


def test_vacancyId_malformed():
    assert _vacancy_stable_id_from_url("https://hh.ru/applicant/vacancy_response?vacancyId=abc") is None
    assert _vacancy_stable_id_from_url("not a url") is None
    assert _vacancy_stable_id_from_url("") is None


def test_vacancyId_different_query_order():
    assert _vacancy_stable_id_from_url(
        "https://hh.ru/applicant/vacancy_response?hhtmFrom=main&vacancyId=999&employerId=1") == "hh:999"


# ---- required tri-state ----

def test_required_unknown_when_no_dom_marker():
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", _snap(
        controls=[{"tag": "INPUT", "type": "text", "name": "phone", "id": None, "dataQa": None,
                   "label": "Телефон", "required": False, "requiredAttr": None, "options": None}],
        question_groups=[{"name": "phone", "stem": "Телефон"}],
    ))
    assert form.questions[0].required is None


def test_required_true_when_marked():
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", _snap(
        controls=[{"tag": "INPUT", "type": "text", "name": "phone", "id": None, "dataQa": None,
                   "label": "Телефон", "required": True, "requiredAttr": True, "options": None}],
        question_groups=[{"name": "phone", "stem": "Телефон"}],
    ))
    assert form.questions[0].required is True


# ---- question stems ----

def test_question_stem_from_question_groups():
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", _snap(
        controls=[
            {"tag": "INPUT", "type": "radio", "name": "task_146", "id": None, "dataQa": None,
             "label": "Менее 3 лет", "required": False, "requiredAttr": None, "options": None},
            {"tag": "INPUT", "type": "radio", "name": "task_146", "id": None, "dataQa": None,
             "label": "Свой вариант", "required": False, "requiredAttr": None, "options": None},
        ],
        question_groups=[{"name": "task_146", "stem": "Сколько лет опыта backend?"}],
    ))
    assert form.questions[0].label == "Сколько лет опыта backend?"
    assert form.questions[0].requires_review is False


def test_question_stem_unknown_when_no_groups():
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", _snap(
        controls=[
            {"tag": "INPUT", "type": "radio", "name": "task_146", "id": None, "dataQa": None,
             "label": "Менее 3 лет", "required": False, "requiredAttr": None, "options": None},
            {"tag": "INPUT", "type": "radio", "name": "task_146", "id": None, "dataQa": None,
             "label": "Свой вариант", "required": False, "requiredAttr": None, "options": None},
        ],
        question_groups=[],
    ))
    # fallback: requires_review + reason
    assert form.questions[0].requires_review is True
    assert "stem not determinable" in form.questions[0].reason


# ---- truth-only CHECKBOX ----

def test_checkbox_truth_only_multi_match():
    snap = _snap(
        controls=[
            {"tag": "INPUT", "type": "checkbox", "name": "task_999", "id": None, "dataQa": None,
             "label": "Claude Code", "required": False, "requiredAttr": False, "options": None},
            {"tag": "INPUT", "type": "checkbox", "name": "task_999", "id": None, "dataQa": None,
             "label": "Свой вариант", "required": False, "requiredAttr": False, "options": None},
            {"tag": "TEXTAREA", "type": "textarea", "name": "task_999_text", "id": None, "dataQa": None,
             "label": None, "required": False, "requiredAttr": False, "options": None},
        ],
        question_groups=[{"name": "task_999", "stem": "Какие агенты?"}],
    )
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", snap)
    # resume contains "python" — must not match "Свой вариант"
    pkg = enrich_package_with_form(_pkg(), form, _profile(), "python, Claude Code", _DEEP, _VAC)
    a = pkg.answers[0]
    assert a.answer == "Claude Code"
    assert "Свой вариант" not in (a.answer or "")


def test_svoj_variant_requires_review():
    # Only "Свой вариант" matches -> requires_review (human text needed)
    snap = _snap(
        controls=[
            {"tag": "INPUT", "type": "checkbox", "name": "task_888", "id": None, "dataQa": None,
             "label": "Свой вариант", "required": False, "requiredAttr": False, "options": None},
            {"tag": "TEXTAREA", "type": "textarea", "name": "task_888_text", "id": None, "dataQa": None,
             "label": None, "required": False, "requiredAttr": False, "options": None},
        ],
        question_groups=[{"name": "task_888", "stem": "Опишите"}],
    )
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", snap)
    # no confirmed fact matches; generator falls through -> None + review
    pkg = enrich_package_with_form(_pkg(), form, _profile(), "no facts", _DEEP, _VAC)
    a = pkg.answers[0]
    assert a.requires_review is True


# ---- validator ----

def test_validator_needs_review_unknown_required():
    snap = _snap(
        controls=[{"tag": "INPUT", "type": "text", "name": "task_100", "id": None, "dataQa": None,
                   "label": "Телефон", "required": False, "requiredAttr": None, "options": None}],
        question_groups=[{"name": "task_100", "stem": "Телефон"}],
    )
    form = extract_application_form("hh:1", "https://hh.ru/vacancy/1", snap)
    pkg = enrich_package_with_form(_pkg(), form, _profile(), _RESUME, _DEEP, _VAC)
    # required is UNKNOWN -> validator flags NEEDS_REVIEW
    assert pkg.validation_status == "NEEDS_REVIEW"


def test_validator_checkbox_membership():
    # Inject a cheating answer with a non-option value -> validator rejects.
    from ai_assistant.hh_extractor import ApplicationQuestion
    pkg = _pkg()
    q = ApplicationQuestion(
        id="hh__ctrl_task_777", label="Агенты", normalized_type="CHECKBOX",
        required=False, options=["Claude Code", "Cursor"], requires_review=False,
    )
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type="screening_questions", questions=[q])
    pkg.form = form
    from ai_assistant.hh_extractor import ApplicationAnswer
    pkg.answers = [ApplicationAnswer(question_id=q.id, answer="Invented", answer_type="CHECKBOX",
                                     requires_review=False, reason="")]
    from ai_assistant.application_qa import ApplicationPackageValidator
    res = ApplicationPackageValidator().validate(pkg)
    assert res["status"] == "NEEDS_REVIEW"


# ---- deterministic normalization ----

def test_deterministic_with_stems():
    snap = _snap(
        controls=[
            {"tag": "INPUT", "type": "radio", "name": "task_146", "id": None, "dataQa": None,
             "label": "Менее 3 лет", "required": False, "requiredAttr": None, "options": None},
            {"tag": "INPUT", "type": "radio", "name": "task_146", "id": None, "dataQa": None,
             "label": "5–7 лет", "required": False, "requiredAttr": None, "options": None},
        ],
        question_groups=[{"name": "task_146", "stem": "Сколько лет опыта?"}],
    )
    f1 = extract_application_form("hh:1", "https://hh.ru/vacancy/1", snap)
    f2 = extract_application_form("hh:1", "https://hh.ru/vacancy/1", snap)
    assert f1.model_dump_json() == f2.model_dump_json()


# ---- snapshot sanitization with new fields stays safe ----

def test_new_snapshot_keys_sanitized():
    from tools.capture_manual_form import _sanitize
    d = {"controls": [{"label": "x"}], "question_groups": [{"name": "task_1", "stem": "s"}],
         "html": "<html>", "cookies": [], "storage_state": {}}
    clean = _sanitize(d)
    assert "html" not in clean
    assert "cookies" not in clean
    assert "storage_state" not in clean
    assert "controls" in clean
    assert "question_groups" in clean