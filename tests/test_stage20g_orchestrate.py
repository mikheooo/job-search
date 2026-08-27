"""Stage 20G tests: full safe prefill orchestration.

Covers the orchestrator gates (VALID only), atomicity, group verification,
RADIO/CHECKBOX/TEXTAREA, all STOP conditions (zero mutations), and safety
instrumentation. FakeDOM simulates DOM; group-state JS is supported.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from ai_assistant.prefill_orchestrate import (
    OperationStatus,
    prepare_and_execute_prefill,
)
from ai_assistant.prefill_plan import PrefillOperation, PrefillTarget, PrefillPlan
from ai_assistant.hh_extractor import (
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionType,
    QuestionSource,
    ApplicationAnswer,
)
from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.schema import Vacancy


class FakeDOM:
    """Simulates a page with form controls; group-state JS is supported."""

    def __init__(self, url="https://hh.ru/applicant/vacancy_response?vacancyId=136591579"):
        self.url = url
        self.controls: dict = {}
        self.evaluate_calls = 0

    def add(self, ctype, name, label, checked=False, value="", disabled=False, readonly=False):
        self.controls[(ctype, name, label)] = {"checked": checked, "value": value,
                                               "disabled": disabled, "readonly": readonly}

    def _parse_name_label(self, expression, split_on):
        name = json.loads(expression.split("const name = ")[1].split(", label")[0])
        label = json.loads(expression.split("label = ")[1].split(split_on)[0])
        return name, label

    def _parse_name_only(self, expression):
        return json.loads(expression.split("const name = ")[1].split(";\n")[0])

    def evaluate(self, expression: str) -> str:
        self.evaluate_calls += 1
        if expression == "JSON.stringify({url: location.href})":
            return json.dumps({"url": self.url})

        # group-state JS (Stage 20G): input[type='radio'][name="..."] checkedLabels
        if "checkedLabels" in expression:
            mtype = re.search(r"input\[type='(\w+)'\]", expression)
            if not mtype:
                return json.dumps({"found": False, "checkedLabels": []})
            t = mtype.group(1)
            # name is embedded as + "task_151" + (JSON string literal) in the JS
            mname = re.search(r"\[name=\"\s*\+\s*(\"[^\"]+\")\s*\+\s*\"\]", expression)
            if mname:
                name = json.loads(mname.group(1))
            else:
                try:
                    name = self._parse_name_only(expression)
                except Exception:
                    name = None
            labels = [lab for (ct, cn, lab), c in self.controls.items()
                      if ct == t and cn == name and c["checked"]]
            present = any(ct == t and cn == name for (ct, cn, _) in self.controls)
            return json.dumps({"found": present, "checkedLabels": labels})

        # radio mutation (React-safe native setter)
        if "HTMLInputElement.prototype, 'checked'" in expression and "type='radio'" in expression:
            name, label = self._parse_name_label(expression, ";\n")
            key = ("radio", name, label)
            if key not in self.controls:
                return json.dumps({"ok": False, "reason": "radio with exact label not found"})
            c = self.controls[key]
            if c["disabled"] or c["readonly"]:
                return json.dumps({"ok": False, "reason": "control is disabled/readonly"})
            c["checked"] = True
            return json.dumps({"ok": True, "checked": True, "reason": ""})
        # checkbox mutation (React-safe native setter)
        if "HTMLInputElement.prototype, 'checked'" in expression and "type='checkbox'" in expression:
            name, label = self._parse_name_label(expression, ";\n")
            key = ("checkbox", name, label)
            if key not in self.controls:
                return json.dumps({"ok": False, "reason": "checkbox with exact label not found"})
            c = self.controls[key]
            if c["disabled"] or c["readonly"]:
                return json.dumps({"ok": False, "reason": "control is disabled/readonly"})
            c["checked"] = True
            return json.dumps({"ok": True, "checked": True, "reason": ""})
        # textarea/text mutation
        if "setter.call(el, value)" in expression:
            tag = "textarea" if "TEXTAREA" in expression else "input"
            name = json.loads(expression.split("const name = ")[1].split(", label")[0])
            value = json.loads(expression.split("value = ")[-1].split(";\n")[0])
            key = (tag, name, "")
            if key not in self.controls:
                return json.dumps({"ok": False, "reason": f"{tag} with exact name not found"})
            c = self.controls[key]
            if c["disabled"] or c["readonly"]:
                return json.dumps({"ok": False, "reason": "control is disabled/readonly"})
            c["value"] = value
            return json.dumps({"ok": True, "value": value, "reason": ""})
        # radio/checkbox verification
        if "found: true, checked" in expression:
            t = "radio" if "type='radio'" in expression else "checkbox"
            name, label = self._parse_name_label(expression, ";\n")
            key = (t, name, label)
            if key not in self.controls:
                return json.dumps({"found": False})
            c = self.controls[key]
            return json.dumps({"found": True, "checked": c["checked"],
                               "disabled": c["disabled"], "readOnly": c["readonly"]})
        # textarea verification
        if "found: true, value" in expression:
            tag = "textarea" if "textarea[name=" in expression else "input"
            name = self._parse_name_only(expression)
            key = (tag, name, "")
            if key not in self.controls:
                return json.dumps({"found": False})
            c = self.controls[key]
            return json.dumps({"found": True, "value": c["value"],
                               "disabled": c["disabled"], "readOnly": c["readonly"]})
        raise RuntimeError(f"FakeDOM: unknown expression: {expression[:120]}")


# ---------- fixtures ----------

def _profile(**kw):
    d = dict(desired_roles=["AI"], alternative_roles=[], skills=["python", "Claude Code", "Cursor"],
             preferred_seniority=[], years_experience=3, remote_required=True,
             allowed_locations=["Remote"], allowed_timezones=[], languages=["en"],
             employment_types=["Full Time"], minimum_salary=1500, salary_currency="USD",
             excluded_roles=[], excluded_companies=[], excluded_countries=[], excluded_industries=[])
    d.update(kw)
    return CandidateProfile(**d)


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
_RESUME = "Name: Ivan Petrov\nEmail: ivan@example.com\n5 years Python. Claude Code and Cursor.\n"


def _pkg():
    return ApplicationPackage(
        vacancy_id="hh:136591579", vacancy_stable_id="hh:136591579",
        resume_adaptation_needed=False, resume_summary="s",
        tailored_skills=["python"], relevant_experience=["e"],
        cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
        warnings=[], generator_version="v1",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"],
                                    relevant_experience_points=["e"]))


def _checkbox_form():
    """FORM with one CHECKBOX group 'Какие агенты?' (options Claude Code, Cursor)."""
    return ApplicationForm(
        source="hh", vacancy_stable_id="hh:136591579",
        application_type=ApplicationType.screening_questions,
        questions=[
            ApplicationQuestion(id="hh__ctrl_task_151", label="Какие агенты?",
                                normalized_type=QuestionType.CHECKBOX, required=False,
                                options=["Claude Code", "Cursor", "Свой вариант"],
                                source=QuestionSource.SCREENING),
        ])


def _checkbox_snapshot(dom):
    return {"controls": [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code",
         "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Cursor",
         "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Свой вариант",
         "visible": True, "disabled": False, "readOnly": False},
    ]}


def _enrich_valid(pkg, form, resume=_RESUME):
    """Run the 17C enrich + force VALID (fixtures set required=False known)."""
    from ai_assistant.application_qa import enrich_package_with_form
    pkg = enrich_package_with_form(pkg, form, _profile(), resume, _DEEP, _VAC)
    # fixtures use required=False -> validator gives VALID when all answered
    return pkg


def _dom_for(form, snapshot_controls=None):
    dom = FakeDOM()
    controls = snapshot_controls
    if isinstance(controls, dict):
        controls = controls.get("controls", [])
    for c in (controls or _checkbox_snapshot(dom)["controls"]):
        dom.add(c["type"], c["name"], c["label"] or "", value="")
    return dom


# ---------- 1. VALID package + one operation ----------

def test_valid_single_checkbox():
    form = _checkbox_form()
    dom = _dom_for(form)
    pkg = _enrich_valid(_pkg(), form)  # resume has Claude Code -> validated
    # only Claude Code validated (resume lacks Cursor? it has Cursor too). Build explicit:
    # force answers to only Claude Code to test single op
    pkg.answers = [a for a in pkg.answers if a.question_id == "hh__ctrl_task_151" and a.answer == "Claude Code"]
    # rebuild answers to contain only Claude Code
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="confirmed")]
    pkg.validation_status = "VALID"
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.executed_operations == 1
    assert rep.verified_operations == 1
    assert all(t.status == OperationStatus.VERIFIED for t in rep.operations)
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is True


# ---------- 2. VALID + multiple RADIO ----------

def test_valid_multiple_radio():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[
                               ApplicationQuestion(id="hh__ctrl_task_146", label="Опыт",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False,
                                                   options=["Менее 3 лет", "5-7 лет"]),
                               ApplicationQuestion(id="hh__ctrl_task_163", label="Частота",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False,
                                                   options=["Ежедневно", "Редко"]),
                           ])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [
        ApplicationAnswer(question_id="hh__ctrl_task_146", answer="Менее 3 лет",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="confirmed"),
        ApplicationAnswer(question_id="hh__ctrl_task_163", answer="Ежедневно",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="confirmed"),
    ]
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "Менее 3 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "5-7 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_163", "label": "Ежедневно", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_163", "label": "Редко", "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.executed_operations == 2
    assert all(gc.ok for gc in rep.group_checks)
    # exactly one checked per group
    assert dom.controls[("radio", "task_146", "Менее 3 лет")]["checked"] is True
    assert dom.controls[("radio", "task_146", "5-7 лет")]["checked"] is False
    assert dom.controls[("radio", "task_163", "Ежедневно")]["checked"] is True
    assert dom.controls[("radio", "task_163", "Редко")]["checked"] is False


# ---------- 3. VALID + multiple CHECKBOX ----------

def test_valid_multiple_checkbox():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code; Cursor",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="confirmed")]
    pkg.validation_status = "VALID"
    dom = _dom_for(form)
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.executed_operations == 2
    assert rep.verified_operations == 2
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is True
    assert dom.controls[("checkbox", "task_151", "Cursor")]["checked"] is True
    assert dom.controls[("checkbox", "task_151", "Свой вариант")]["checked"] is False


# ---------- 4. VALID + RADIO + CHECKBOX mixed ----------

def test_valid_radio_and_checkbox():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[
                               ApplicationQuestion(id="hh__ctrl_task_146", label="Опыт",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False, options=["Менее 3 лет", "5-7 лет"]),
                               ApplicationQuestion(id="hh__ctrl_task_151", label="Агенты",
                                                   normalized_type=QuestionType.CHECKBOX,
                                                   required=False,
                                                   options=["Claude Code", "Cursor"]),
                           ])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [
        ApplicationAnswer(question_id="hh__ctrl_task_146", answer="Менее 3 лет",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="c"),
        ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                          answer_type=QuestionType.CHECKBOX, confidence=1.0,
                          requires_review=False, reason="c"),
    ]
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "Менее 3 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "5-7 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Cursor", "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.executed_operations == 2
    assert all(gc.ok for gc in rep.group_checks)


# ---------- 5. VALID + TEXTAREA ----------

def test_valid_textarea():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[
                               ApplicationQuestion(id="hh__ctrl_task_169_text",
                                                   label="Опишите workflow",
                                                   normalized_type=QuestionType.TEXTAREA,
                                                   required=False,
                                                   source=QuestionSource.SCREENING),
                           ])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_169_text",
                                     answer="Подготовка контекста, постановка задачи, review",
                                     answer_type=QuestionType.TEXTAREA, confidence=1.0,
                                     requires_review=False, reason="confirmed")]
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "TEXTAREA", "type": "textarea", "name": "task_169_text", "label": "",
         "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.fill_count == 1
    assert dom.controls[("textarea", "task_169_text", "")]["value"] == "Подготовка контекста, постановка задачи, review"


# ---------- 6-9. STOP conditions (zero mutations) ----------

def test_needs_review_zero_mutations():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = []
    pkg.validation_status = "NEEDS_REVIEW"
    dom = _dom_for(form)
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "STOPPED_NEEDS_REVIEW"
    assert rep.executed_operations == 0
    assert rep.operations == []
    assert dom.evaluate_calls == 0  # no DOM read even (gated before)

def test_unresolved_zero_mutations():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    # answer is None + review -> not validated -> plan unresolved
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer=None,
                                     answer_type=QuestionType.CHECKBOX, confidence=0.0,
                                     requires_review=True, reason="no fact")]
    pkg.validation_status = "VALID"  # but plan will be NEEDS_REVIEW due to unresolved
    dom = _dom_for(form)
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "STOPPED_NEEDS_REVIEW"
    assert rep.executed_operations == 0
    # no mutations attempted
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False


def test_unknown_zero_mutations():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[ApplicationQuestion(id="hh__mystery", label="Нечто",
                                                          normalized_type=QuestionType.UNKNOWN,
                                                          required=None)])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = []
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "INPUT", "type": "weird", "name": "mystery", "label": "Нечто", "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    assert rep.verdict == "STOPPED_NEEDS_REVIEW"
    assert rep.executed_operations == 0


def test_review_answer_zero_mutations():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    # answer has answer set but requires_review=True -> must never execute
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=0.5,
                                     requires_review=True, reason="LLM-generated")]
    pkg.validation_status = "VALID"
    dom = _dom_for(form)
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "STOPPED_NEEDS_REVIEW"
    assert rep.executed_operations == 0
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False


# ---------- 10-12. missing/disabled/readonly target ----------

def test_missing_target_zero_mutations():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    # snapshot has NO matching checkbox (only "Свой вариант")
    snapshot = {"controls": [
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Свой вариант", "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    # missing target -> build_prefill_plan reports unresolved -> STOP (zero mutations)
    assert rep.verdict == "STOPPED_NEEDS_REVIEW"
    assert rep.executed_operations == 0
    assert rep.failed_operations == 0


def test_disabled_target_zero_mutations():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    dom = _dom_for(form)
    dom.controls[("checkbox", "task_151", "Claude Code")]["disabled"] = True
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "FAILED"
    assert rep.executed_operations == 0
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False


def test_readonly_textarea_zero_mutations():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[ApplicationQuestion(id="hh__ctrl_task_169_text",
                                                          label="Опишите", normalized_type=QuestionType.TEXTAREA,
                                                          required=False)])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_169_text", answer="text",
                                     answer_type=QuestionType.TEXTAREA, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "TEXTAREA", "type": "textarea", "name": "task_169_text", "label": "", "visible": True, "disabled": False, "readOnly": True},
    ]}
    dom = _dom_for(form, snapshot)
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    assert rep.verdict == "FAILED"
    assert rep.executed_operations == 0
    assert dom.controls[("textarea", "task_169_text", "")]["value"] == ""


# ---------- 13. operation failure stops subsequent ----------

def test_failure_stops_subsequent_mutations():
    # Plan ops are sorted by (question_id, name, value): task_146 < task_151 < task_163.
    # task_163 radio is removed -> the LAST op fails -> nothing after it. To test
    # "subsequent stopped", make the MIDDLE op fail (task_151 checkbox removed).
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[
                               ApplicationQuestion(id="hh__ctrl_task_146", label="Опыт",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False, options=["Менее 3 лет", "5-7 лет"]),
                               ApplicationQuestion(id="hh__ctrl_task_151", label="Агенты",
                                                   normalized_type=QuestionType.CHECKBOX,
                                                   required=False, options=["Claude Code", "Cursor"]),
                               ApplicationQuestion(id="hh__ctrl_task_163", label="Частота",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False, options=["Ежедневно", "Редко"]),
                           ])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [
        ApplicationAnswer(question_id="hh__ctrl_task_146", answer="Менее 3 лет",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="c"),
        ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                          answer_type=QuestionType.CHECKBOX, confidence=1.0,
                          requires_review=False, reason="c"),
        ApplicationAnswer(question_id="hh__ctrl_task_163", answer="Ежедневно",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="c"),
    ]
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "Менее 3 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "5-7 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Cursor", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_163", "label": "Ежедневно", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_163", "label": "Редко", "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    # remove the MIDDLE op target (task_151 checkbox) -> op fails after task_146 succeeds,
    # task_163 must be SKIPPED (not executed)
    del dom.controls[("checkbox", "task_151", "Claude Code")]
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    # task_146 executed; task_151 failed; task_163 skipped
    assert dom.controls[("radio", "task_146", "Менее 3 лет")]["checked"] is True
    assert rep.executed_operations == 1
    assert rep.failed_operations == 1
    assert rep.skipped_operations == 1
    # task_163 radio must NOT be executed (atomicity)
    assert dom.controls[("radio", "task_163", "Ежедневно")]["checked"] is False
    assert rep.verdict == "FAILED"


def test_failure_stops_later_ops_skipped():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[
                               ApplicationQuestion(id="hh__ctrl_task_146", label="Опыт",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False, options=["Менее 3 лет", "5-7 лет"]),
                               ApplicationQuestion(id="hh__ctrl_task_151", label="Агенты",
                                                   normalized_type=QuestionType.CHECKBOX,
                                                   required=False, options=["Claude Code", "Cursor"]),
                           ])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [
        ApplicationAnswer(question_id="hh__ctrl_task_146", answer="Менее 3 лет",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="c"),
        ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                          answer_type=QuestionType.CHECKBOX, confidence=1.0,
                          requires_review=False, reason="c"),
    ]
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "Менее 3 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "5-7 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Claude Code", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "checkbox", "name": "task_151", "label": "Cursor", "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    # remove the FIRST op target (task_146 radio) -> task_146 fails, task_151 skipped
    del dom.controls[("radio", "task_146", "Менее 3 лет")]
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False  # skipped
    assert rep.skipped_operations == 1
    assert rep.failed_operations == 1
    assert rep.executed_operations == 0
    assert rep.verdict == "FAILED"


# ---------- 14. verification failure -> FAILED ----------

def test_verification_failure_failed():
    class LyingDOM(FakeDOM):
        def evaluate(self, expression):
            res = super().evaluate(expression)
            if "found: true, checked" in expression:
                d = json.loads(res)
                if d.get("found"):
                    d["checked"] = False
                return json.dumps(d)
            return res

    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    dom = LyingDOM()
    for c in _checkbox_snapshot(dom)["controls"]:
        dom.add(c["type"], c["name"], c["label"] or "")
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "FAILED"
    assert rep.verified_operations == 0


# ---------- 15. no rollback ----------

def test_no_rollback():
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:1",
                           application_type=ApplicationType.screening_questions,
                           questions=[
                               ApplicationQuestion(id="hh__ctrl_task_146", label="Опыт",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False, options=["Менее 3 лет", "5-7 лет"]),
                               ApplicationQuestion(id="hh__ctrl_task_163", label="Частота",
                                                   normalized_type=QuestionType.RADIO,
                                                   required=False, options=["Ежедневно", "Редко"]),
                           ])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [
        ApplicationAnswer(question_id="hh__ctrl_task_146", answer="Менее 3 лет",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="c"),
        ApplicationAnswer(question_id="hh__ctrl_task_163", answer="Ежедневно",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="c"),
    ]
    pkg.validation_status = "VALID"
    snapshot = {"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "Менее 3 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_146", "label": "5-7 лет", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_163", "label": "Ежедневно", "visible": True, "disabled": False, "readOnly": False},
        {"tag": "INPUT", "type": "radio", "name": "task_163", "label": "Редко", "visible": True, "disabled": False, "readOnly": False},
    ]}
    dom = _dom_for(form, snapshot)
    del dom.controls[("radio", "task_163", "Ежедневно")]  # op2 fails
    rep = prepare_and_execute_prefill(pkg, form, snapshot, dom.evaluate)
    assert rep.verdict == "FAILED"
    # op1 remains executed (no rollback)
    assert dom.controls[("radio", "task_146", "Менее 3 лет")]["checked"] is True


# ---------- 16. deterministic report ----------

def test_deterministic_report():
    def run():
        form = _checkbox_form()
        pkg = _pkg()
        pkg.form = form
        pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                         answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                         requires_review=False, reason="c")]
        pkg.validation_status = "VALID"
        dom = _dom_for(form)
        rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
        d = json.loads(rep.model_dump_json())
        d.pop("generated_at")
        return d
    assert run() == run()


# ---------- 17. URL change -> FAILED ----------

def test_url_change_failed():
    class NavigatingDOM(FakeDOM):
        navigated_to = None
        def evaluate(self, expression):
            if "HTMLInputElement.prototype, 'checked'" in expression and self.navigated_to is None:
                self.navigated_to = "https://hh.ru/other"
            if expression == "JSON.stringify({url: location.href})":
                return json.dumps({"url": self.navigated_to or self.url})
            return super().evaluate(expression)

    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    dom = NavigatingDOM()
    for c in _checkbox_snapshot(dom)["controls"]:
        dom.add(c["type"], c["name"], c["label"] or "")
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "FAILED"
    assert any("URL changed" in e for e in rep.errors)
    assert rep.navigation_count == 0  # we never navigated


# ---------- 18. forbidden URL -> zero mutations ----------

def test_forbidden_url_zero_mutations():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    dom = FakeDOM(url="https://evil.example.com/form")
    for c in _checkbox_snapshot(dom)["controls"]:
        dom.add(c["type"], c["name"], c["label"] or "")
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "FAIL_CLOSED"
    assert rep.executed_operations == 0
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False


# ---------- 19. no DB writes ----------

def test_no_db_writes(monkeypatch):
    import ai_assistant.db as db
    writes = {"n": 0}
    def forbidden(*a, **k):
        writes["n"] += 1
        raise AssertionError("DB access during orchestration")
    monkeypatch.setattr(db, "get_connection", forbidden)
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    dom = _dom_for(form)
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert writes["n"] == 0


# ---------- 20. no cookies/storage ----------

def test_no_cookies_storage_in_report():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="c")]
    pkg.validation_status = "VALID"
    dom = _dom_for(form)
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    raw = json.dumps(json.loads(rep.model_dump_json()), ensure_ascii=False).lower()
    for marker in ["cookie", "storage_state", "token", "password", "authorization"]:
        assert marker not in raw


# ---------- 21. real snapshot regression ----------

def test_real_snapshot_plan_executes_on_simulated_real_dom():
    snap = json.loads(pathlib.Path("artifacts/hh_manual_form_snapshot.json").read_text(encoding="utf-8"))["snapshot"]
    # build a form with the real checkbox group
    form = ApplicationForm(source="hh", vacancy_stable_id="hh:136591579",
                           application_type=ApplicationType.screening_questions,
                           questions=[ApplicationQuestion(id="hh__ctrl_task_384589151",
                                                          label="Какие агенты?", normalized_type=QuestionType.CHECKBOX,
                                                          required=False,
                                                          options=["Claude Code", "Cursor", "Свой вариант"])])
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_384589151", answer="Claude Code; Cursor",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="confirmed")]
    pkg.validation_status = "VALID"
    dom = FakeDOM()
    for c in snap["controls"]:
        if c.get("type") in ("checkbox", "radio"):
            dom.add(c["type"], c["name"], c.get("label") or "")
        elif c.get("type") in ("textarea", "text"):
            dom.add(c["type"], c["name"], "")
    rep = prepare_and_execute_prefill(pkg, form, snap, dom.evaluate,
                                      allowed_url_markers=["hh.ru"],
                                      required_url_markers=["applicant/vacancy_response"])
    assert rep.verdict == "VERIFIED"
    assert rep.executed_operations == 2
    assert rep.verified_operations == 2
    assert dom.controls[("checkbox", "task_384589151", "Claude Code")]["checked"] is True
    assert dom.controls[("checkbox", "task_384589151", "Cursor")]["checked"] is True


# ---------- 22. two validated checkbox operations both verified ----------

def test_two_checkbox_ops_both_verified():
    form = _checkbox_form()
    pkg = _pkg()
    pkg.form = form
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_151", answer="Claude Code; Cursor",
                                     answer_type=QuestionType.CHECKBOX, confidence=1.0,
                                     requires_review=False, reason="confirmed")]
    pkg.validation_status = "VALID"
    dom = _dom_for(form)
    rep = prepare_and_execute_prefill(pkg, form, _checkbox_snapshot(dom), dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.planned_operations == 2
    assert rep.executed_operations == 2
    assert rep.verified_operations == 2
    assert rep.failed_operations == 0
    assert rep.skipped_operations == 0
    # group check: exactly the two expected
    gc = [g for g in rep.group_checks if g.group_name == "task_151"]
    assert gc and gc[0].ok
    assert sorted(gc[0].expected_checked) == sorted(gc[0].actual_checked) == ["Claude Code", "Cursor"]