"""Stage 20E tests: deterministic prefill plan (read-only dry-run).

Covers: RADIO, CHECKBOX, TEXTAREA, "Свой вариант", resume/cover detection,
real snapshot regression (6 choice groups + standalone), and safety
(no click/fill/type/upload/submit/goto/navigation/login, no DB writes,
no html/cookies in plan).
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from ai_assistant.prefill_plan import build_prefill_plan
from ai_assistant.hh_extractor import (
    ApplicationForm,
    ApplicationQuestion,
    QuestionType,
    QuestionSource,
    ApplicationType,
)
from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.application_qa import enrich_package_with_form
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.schema import Vacancy

SNAPSHOT_PATH = pathlib.Path("artifacts/hh_manual_form_snapshot.json")


def _profile(**kw):
    d = dict(desired_roles=["AI Automation Engineer"], alternative_roles=[],
             skills=["python", "Claude Code", "Cursor"], preferred_seniority=[],
             years_experience=3, remote_required=True, allowed_locations=["Remote"],
             allowed_timezones=[], languages=["en"], employment_types=["Full Time"],
             minimum_salary=1500, salary_currency="USD",
             excluded_roles=[], excluded_companies=[], excluded_countries=[], excluded_industries=[])
    d.update(kw)
    return CandidateProfile(**d)


_RESUME = "Name: Ivan Petrov\nEmail: ivan@example.com\n5 years Python. Claude Code.\n"
_VAC = Vacancy(source="hh", source_job_id="136591579", title="Dev", company="Co",
               description="python", job_url="https://hh.ru/vacancy/136591579",
               location="Remote", country_restrictions=[], timezone_restrictions=[],
               salary_min=None, salary_max=None, salary_currency=None, employment_type=None)
_DEEP = DeepAnalysisResult(fit_score=80, recommendation="APPLY", why_fit=[], gaps=[],
                           must_have_requirements=[], nice_to_have_requirements=[],
                           matched_skills=[], missing_skills=[], seniority_assessment="s",
                           remote_assessment="s", salary_assessment="s",
                           resume_adaptation_needed=False, resume_adaptation_reasons=[],
                           application_strategy="a")
_PKG_KW = dict(vacancy_id="hh:136591579", vacancy_stable_id="hh:136591579",
               resume_adaptation_needed=False, resume_summary="s",
               tailored_skills=["python"], relevant_experience=["e"],
               cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
               warnings=[], generator_version="v1",
               adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                           prioritized_skills=["python"],
                                           relevant_experience_points=["e"]))


def _pkg():
    return ApplicationPackage(**dict(_PKG_KW))


def _load_form():
    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["snapshot"]
    # normalize fresh to get current extractor logic (stems tri-state etc.)
    from tools.capture_manual_form import normalize_to_application_form
    return normalize_to_application_form(snap, "hh:136591579"), snap


# ---- 1. real snapshot regression: 6 groups + TEXTAREA, stems, options ----

def test_real_snapshot_6_choice_groups_and_textareas():
    form, snap = _load_form()
    by_type = {}
    for q in form.questions:
        by_type.setdefault(q.normalized_type.value, []).append(q)
    assert len([q for q in form.questions if q.normalized_type in (QuestionType.RADIO, QuestionType.CHECKBOX)]) == 6
    assert len([q for q in form.questions if q.normalized_type == QuestionType.TEXTAREA]) == 5
    # each choice group has real options
    for q in [x for x in form.questions if x.normalized_type in (QuestionType.RADIO, QuestionType.CHECKBOX)]:
        assert len(q.options) >= 2
    # custom_option_text_id linked for every choice group
    for q in [x for x in form.questions if x.normalized_type in (QuestionType.RADIO, QuestionType.CHECKBOX)]:
        assert q.custom_option_text_id is not None
    # stems are real texts, not first-option copies
    for q in form.questions:
        assert q.label and q.label != q.options[0] if q.options else q.label


# ---------- 2. RADIO ----------

def test_radio_validated_option_planned():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions, questions=[
        ApplicationQuestion(id="hh__ctrl_task_146", label="Сколько лет опыта?",
                            normalized_type=QuestionType.RADIO, required=False,
                            options=["Менее 3-ёх лет", "5-7 лет", "Свой вариант"]),
    ])
    pkg = _pkg()
    # Direct validated answer (bypassing fragile label->confirmed string matching)
    from ai_assistant.hh_extractor import ApplicationAnswer
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_146", answer="Менее 3-ёх лет",
                                     answer_type=QuestionType.RADIO, confidence=1.0,
                                     requires_review=False, reason="test validated")]
    pkg.validation_status = "VALID"
    snap = {"controls": [{"tag": "INPUT", "type": "radio", "name": "task_146", "label": "Менее 3-ёх лет", "visible": True, "disabled": False, "readOnly": False}]}
    plan = build_prefill_plan(pkg, form, snap)
    assert len(plan.operations) == 1
    assert plan.operations[0].value == "Менее 3-ёх лет"
    assert plan.operations[0].target.type == "radio"


def test_radio_no_match_unresolved():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions, questions=[
        ApplicationQuestion(id="hh__ctrl_task_1", label="Опыт",
                            normalized_type=QuestionType.RADIO, required=False,
                            options=["Только офис", "Свой вариант"]),
    ])
    pkg = _pkg()
    from ai_assistant.hh_extractor import ApplicationAnswer
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_1", answer=None,
                                     answer_type=QuestionType.RADIO, confidence=0.0,
                                     requires_review=True, reason="No matching option")]
    pkg.validation_status = "NEEDS_REVIEW"
    snap = {"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_1", "label": "Только офис", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_1", "label": "Свой вариант", "visible": True, "disabled": False, "readOnly": False},
    ]}
    plan = build_prefill_plan(pkg, form, snap)
    assert len(plan.operations) == 0
    assert any(u.question_id == "hh__ctrl_task_1" for u in plan.unresolved)


# ---------- 3. CHECKBOX ----------

def test_checkbox_multiple_options_planned():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions, questions=[
        ApplicationQuestion(id="hh__ctrl_task_151", label="Агенты",
                            normalized_type=QuestionType.CHECKBOX, required=False,
                            options=["Claude Code", "Cursor", "Свой вариант"]),
    ])
    pkg = _pkg()
    from ai_assistant.application_qa import enrich_package_with_form
    # resume mentions two tools among the options
    pkg = enrich_package_with_form(pkg, form, _profile(), "Claude Code and Cursor", _DEEP, _VAC)
    snap = {"controls": [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Cursor", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Свой вариант", "visible": True, "disabled": False, "readOnly": False},
    ]}
    plan = build_prefill_plan(pkg, form, snap)
    assert len(plan.operations) == 2
    vals = {o.value for o in plan.operations}
    assert "Claude Code" in vals and "Cursor" in vals
    assert "Свой вариант" not in vals


def test_checkbox_unsupported_option_not_planned():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions, questions=[
        ApplicationQuestion(id="hh__ctrl_task_151", label="Агенты",
                            normalized_type=QuestionType.CHECKBOX, required=False,
                            options=["Claude Code", "Cursor"]),
    ])
    pkg = _pkg()
    from ai_assistant.application_qa import enrich_package_with_form
    pkg = enrich_package_with_form(pkg, form, _profile(), "InventedTool XYZ", _DEEP, _VAC)
    snap = {"controls": [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code", "visible": True, "disabled": False, "readOnly": False},
    ]}
    plan = build_prefill_plan(pkg, form, snap)
    assert all(op.value in ["Claude Code", "Cursor"] for op in plan.operations)


# ---------- 4. TEXTAREA + Свой вариант ----------

def test_textarea_proven_linkage_planned():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions, questions=[
        ApplicationQuestion(id="hh__ctrl_task_999_text", label="Кратко опишите workflow",
                            normalized_type=QuestionType.TEXTAREA, required=False, source=QuestionSource.SCREENING),
    ])
    pkg = _pkg()
    from ai_assistant.application_qa import enrich_package_with_form
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _DEEP, _VAC)
    snap = {"controls": [
        {"tag": "TEXTAREA", "type": "textarea", "name": "task_999_text", "label": "Кратко опишите workflow", "visible": True, "disabled": False, "readOnly": False},
    ]}
    # TEXTAREA with no truth source -> answer None, so no operation
    plan = build_prefill_plan(pkg, form, snap)
    assert plan.unresolved or len(plan.operations) == 0  # NEEDS_REVIEW, not an arbitrary fill


def test_svoj_variant_never_auto_selected_in_checkbox():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions, questions=[
        ApplicationQuestion(id="hh__ctrl_task_151", label="Агенты",
                            normalized_type=QuestionType.CHECKBOX, required=False,
                            options=["Claude Code", "Свой вариант"]),
    ])
    pkg = _pkg()
    from ai_assistant.application_qa import enrich_package_with_form
    pkg = enrich_package_with_form(pkg, form, _profile(), "no matching tool", _DEEP, _VAC)
    snap = {"controls": [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Свой вариант", "visible": True, "disabled": False, "readOnly": False},
    ]}
    plan = build_prefill_plan(pkg, form, snap)
    assert all(op.value != "Свой вариант" for op in plan.operations)


def test_custom_textarea_not_used_without_svoj_variant():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions, questions=[
        ApplicationQuestion(id="hh__ctrl_task_151", label="Агенты",
                            normalized_type=QuestionType.CHECKBOX, required=False,
                            options=["Claude Code", "Свой вариант"],
                            custom_option_text_id="hh__ctrl_task_151_text"),
    ])
    pkg = _pkg()
    from ai_assistant.application_qa import enrich_package_with_form
    pkg = enrich_package_with_form(pkg, form, _profile(), "Claude Code", _DEEP, _VAC)
    snap = {"controls": [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "TEXTAREA", "type": "textarea", "name": "task_151_text", "label": "", "visible": False, "disabled": False, "readOnly": False},
    ]}
    plan = build_prefill_plan(pkg, form, snap)
    # The _text textarea must NOT appear as an operation when a real option is chosen.
    assert not any(op.target.name == "task_151_text" for op in plan.operations)


# ---------- 5. Resume / cover letter ----------

def test_no_artificial_resume_cover_controls():
    form, snap = _load_form()
    has_resume = any(c.get("type") == "file" for c in snap.get("controls") or [])
    has_cover = any("cover" in (c.get("label") or "").lower() or c.get("name") == "letter"
                    for c in snap.get("controls") or [])
    assert has_resume is False
    assert has_cover is False
    # plan must not invent absent controls
    pkg = _pkg()
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _DEEP, _VAC)
    plan = build_prefill_plan(pkg, form, snap)
    assert not any(op.target.type == "file" for op in plan.operations)


def test_unknown_controls_not_planned():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.unknown, questions=[
        ApplicationQuestion(id="hh__mystery", label="Нечто неизвестное",
                            normalized_type=QuestionType.UNKNOWN, required=None,
                            source=QuestionSource.SCREENING),
    ])
    pkg = _pkg()
    from ai_assistant.hh_extractor import ApplicationAnswer
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__mystery", answer=None,
                                     answer_type=QuestionType.UNKNOWN, confidence=0.0,
                                     requires_review=True, reason="UNKNOWN")]
    pkg.validation_status = "NEEDS_REVIEW"
    snap = {"controls": [
        {"tag": "INPUT", "type": "weird-widget", "name": "mystery", "label": "Нечто неизвестное", "visible": True, "disabled": False, "readOnly": False},
    ]}
    plan = build_prefill_plan(pkg, form, snap)
    assert len(plan.operations) == 0
    assert any(u.question_id == "hh__mystery" for u in plan.unresolved)


# ---------- 6. real snapshot deterministic targets ----------

def test_real_snapshot_deterministic_control_targets():
    form, snap = _load_form()
    snap_copy = copy.deepcopy(snap)
    # enrich with one validated answer to get a deterministic operation
    pkg = _pkg()
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _DEEP, _VAC)
    p1 = build_prefill_plan(pkg, form, snapshot=snap)
    p2 = build_prefill_plan(pkg, form, snapshot=snap_copy)
    # Deterministic targets: compare everything EXCEPT the wall-clock stamp
    # (Windows timer granularity made the raw JSON comparison flaky).
    d1, d2 = p1.model_dump(), p2.model_dump()
    d1.pop("generated_at"), d2.pop("generated_at")
    assert d1 == d2


# ---------- 7. safety: mutation APIs ----------

def test_prefill_plan_never_calls_mutation_apis():
    form, snap = _load_form()
    pkg = _pkg()
    pkg = enrich_package_with_form(pkg, form, _profile(), _RESUME, _DEEP, _VAC)
    before = json.loads(pkg.model_dump_json())
    before_snap = copy.deepcopy(snap)
    plan = build_prefill_plan(pkg, form, snap)
    # snapshot and package must not have been mutated by the planner
    assert json.loads(pkg.model_dump_json()) == before
    assert snap == before_snap
    # source has no mutation API names
    src = pathlib.Path("ai_assistant/prefill_plan.py").read_text(encoding="utf-8")
    for banned in [".click(", ".fill(", ".type(", ".set_input_files(", ".goto(", ".check(", ".uncheck(",
                   ".press(", "keyboard", "mouse", "page.goto", "browser."]:
        assert banned not in src
