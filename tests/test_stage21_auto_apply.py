"""Stage 21 tests: dual-mode controlled auto-apply.

FakeHH simulates the full live lifecycle over raw CDP expressions:
URL reads, React-safe checked mutations, letter value mutation, group-state
reads, submit-button meta/click, post-submit markers. Covers every case in
the Stage 21 spec: mode defaults, simple/letter auto submit, questionnaire
review stop, AUTO gates, duplicate block, wrong URL/vacancy, fingerprint
mismatch, failed/skipped, required=None STOP, no retry, determinism.
"""

from __future__ import annotations

import json
import re

import pytest

from ai_assistant.auto_apply_modes import (
    DEFAULT_MODE,
    ApplyMode,
    AutoApplyReport,
    FormKind,
    V_DUPLICATE,
    V_NEEDS_HUMAN_REVIEW,
    V_PREFILL_FAILED,
    V_REQUIRED_UNKNOWN,
    V_UNRESOLVED,
    classify_form,
    clear_session_state,
    resolve_mode,
    run_auto_apply,
)
from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.hh_extractor import (
    ApplicationAnswer,
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionSource,
    QuestionType,
)


class FakeHH:
    """Simulates the HH response page for the whole apply lifecycle."""

    def __init__(self, url="https://hh.ru/applicant/vacancy_response?vacancyId=222",
                 btn_disabled=False, markers=(), reset_checked_on_click=False):
        self.url = url
        self.btn_disabled = btn_disabled
        self.markers = list(markers)
        self.reset_checked_on_click = reset_checked_on_click
        self.controls = {}   # (type, name, label) -> {"checked": bool, "value": str}
        self.clicks = 0

    def add(self, ctype, name, label, checked=False, value="", disabled=False):
        self.controls[(ctype, name, label)] = {"checked": checked, "value": value,
                                               "disabled": disabled}
        return (ctype, name, label)

    # -- expression routing -------------------------------------------------
    def evaluate(self, expression: str) -> str:
        if expression == "JSON.stringify({url: location.href})":
            return json.dumps({"url": self.url})
        if "const markers" in expression and "location.href" in expression:
            found = self.markers if self.clicks else []
            return json.dumps({"found": found, "url": self.url})
        if "checkedLabels" in expression:
            t = "radio" if "type='radio'" in expression else "checkbox"
            m = re.search(r'\+ ("(?:[^"\\]|\\.)*") \+ "\]"', expression)
            name = json.loads(m.group(1)) if m else ""
            labels = [lab for (ct, cn, lab), c in self.controls.items()
                      if ct == t and cn == name and c["checked"]]
            present = any(ct == t and cn == name for (ct, cn, _) in self.controls)
            return json.dumps({"found": present, "checkedLabels": labels})
        if '"vacancy-response-submit-popup"' in expression and "el.click()" not in expression:
            return json.dumps({"found": True, "tag": "BUTTON", "type": "submit",
                               "text": "Откликнуться",
                               "dataQa": "vacancy-response-submit-popup",
                               "disabled": self.btn_disabled, "visible": True,
                               "cls": "magritte"})
        if "el.click()" in expression:
            self.clicks += 1
            if self.btn_disabled:
                return json.dumps({"ok": False, "reason": "submit button is disabled"})
            if self.reset_checked_on_click:
                for c in self.controls.values():
                    c["checked"] = False
            return json.dumps({"ok": True})
        if "HTMLInputElement.prototype, 'checked'" in expression:
            t = "radio" if "type='radio'" in expression else "checkbox"
            name, label = self._name_label(expression)
            key = (t, name, label)
            if key not in self.controls:
                return json.dumps({"ok": False, "reason": f"{t} with exact label not found"})
            c = self.controls[key]
            if c["disabled"]:
                return json.dumps({"ok": False, "reason": "control is disabled/readonly"})
            c["checked"] = True
            return json.dumps({"ok": True, "checked": True, "reason": ""})
        if "setter.call(el, value)" in expression:
            tag = "textarea" if "TEXTAREA" in expression else "input"
            name = json.loads(expression.split("const name = ")[1].split(", label")[0])
            value = json.loads(expression.split("value = ")[-1].split(";\n")[0])
            key = (tag, name, "")
            if key not in self.controls:
                return json.dumps({"ok": False, "reason": f"{tag} with exact name not found"})
            c = self.controls[key]
            if c["disabled"]:
                return json.dumps({"ok": False, "reason": "control is disabled/readonly"})
            c["value"] = value
            return json.dumps({"ok": True, "value": value, "reason": ""})
        if "found: true, checked" in expression:
            t = "radio" if "type='radio'" in expression else "checkbox"
            name, label = self._name_label(expression)
            c = self.controls.get((t, name, label))
            if c is None:
                return json.dumps({"found": False})
            return json.dumps({"found": True, "checked": c["checked"],
                               "disabled": c["disabled"], "readOnly": False})
        if "found: true, value" in expression:
            tag = "textarea" if "textarea[name=" in expression else "input"
            name = json.loads(expression.split("const name = ")[1].split(";\n")[0])
            c = self.controls.get((tag, name, ""))
            if c is None:
                return json.dumps({"found": False})
            return json.dumps({"found": True, "value": c["value"],
                               "disabled": c["disabled"], "readOnly": False})
        raise RuntimeError(f"FakeHH unknown expr: {expression[:100]}")

    def _name_label(self, expression):
        name = json.loads(expression.split("const name = ")[1].split(", label")[0])
        label = json.loads(expression.split("label = ")[1].split(";\n")[0])
        return name, label


# ---------------- builders ----------------

def _pkg(vid="hh:222", status="VALID", cover="", answers=None):
    pkg = ApplicationPackage(
        vacancy_id=vid, vacancy_stable_id=vid,
        resume_adaptation_needed=False, resume_summary="s",
        tailored_skills=["python"], relevant_experience=["e"],
        cover_letter=cover, application_strategy="st",
        warnings=[], generator_version="t",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"],
                                    relevant_experience_points=["e"]))
    pkg.validation_status = status
    pkg.answers = answers or []
    return pkg


def _simple_form(vid="hh:222"):
    return ApplicationForm(source="hh", vacancy_stable_id=vid,
                           application_type=ApplicationType.unknown, questions=[])


def _letter_form(vid="hh:222"):
    return ApplicationForm(source="hh", vacancy_stable_id=vid,
                           application_type=ApplicationType.cover_letter,
                           questions=[ApplicationQuestion(
                               id="hh__ctrl_cover_letter", label="Сопроводительное письмо",
                               normalized_type=QuestionType.COVER_LETTER, required=True,
                               source=QuestionSource.SYSTEM)])


LETTER_SNAPSHOT = {
    # DOM-proven REQUIRED letter: explicit requiredAttr on the control.
    "controls": [{"tag": "TEXTAREA", "type": "textarea", "name": "cover_letter",
                  "dataQa": "vacancy-response-popup-form-letter-input",
                  "label": "", "visible": True, "disabled": False,
                  "requiredAttr": True}],
    "buttons": [{"dataQa": "vacancy-response-submit-popup", "disabled": True}],
}

# Letter textarea present but NO required marker; submit button enabled on
# the empty form -> the form sends without a letter -> OPTIONAL.
OPTIONAL_LETTER_SNAPSHOT = {
    "controls": [{"tag": "TEXTAREA", "type": "textarea", "name": "",
                  "dataQa": "vacancy-response-popup-form-letter-input",
                  "label": "", "visible": True, "disabled": False,
                  "requiredAttr": None, "ariaRequired": None}],
    "buttons": [{"dataQa": "vacancy-response-submit-popup", "disabled": False}],
}

# Letter present, no required marker, submit button DISABLED on empty form:
# blocked for an unproven reason -> requiredness UNKNOWN.
UNKNOWN_LETTER_SNAPSHOT = {
    "controls": [{"tag": "TEXTAREA", "type": "textarea", "name": "",
                  "dataQa": "vacancy-response-popup-form-letter-input",
                  "label": "", "visible": True, "disabled": False,
                  "requiredAttr": None, "ariaRequired": None}],
    "buttons": [{"dataQa": "vacancy-response-submit-popup", "disabled": True}],
}


def _letter_answers():
    return [ApplicationAnswer(question_id="hh__ctrl_cover_letter",
                              answer="Готов к интервью.",
                              answer_type=QuestionType.COVER_LETTER,
                              confidence=1.0, requires_review=False, reason="truth")]


def _questionnaire_form(vid="hh:222", required=False):
    return ApplicationForm(source="hh", vacancy_stable_id=vid,
                           application_type=ApplicationType.screening_questions,
                           questions=[
        ApplicationQuestion(id="hh__ctrl_task_100", label="Опыт?",
                            normalized_type=QuestionType.RADIO, required=required,
                            options=["1 год", "3 года"], source=QuestionSource.SCREENING),
        ApplicationQuestion(id="hh__ctrl_task_200_text", label="Опишите проект",
                            normalized_type=QuestionType.TEXTAREA, required=required,
                            options=[], source=QuestionSource.SCREENING),
    ])


QUESTIONNAIRE_SNAPSHOT = {
    "controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_100", "label": "1 год",
         "visible": True, "disabled": False},
        {"tag": "INPUT", "type": "radio", "name": "task_100", "label": "3 года",
         "visible": True, "disabled": False},
        {"tag": "TEXTAREA", "type": "textarea", "name": "task_200_text",
         "label": "Писать тут", "visible": True, "disabled": False},
    ],
}


def _qa(qid, answer, qtype, review=False):
    return ApplicationAnswer(question_id=qid, answer=answer, answer_type=qtype,
                             confidence=1.0, requires_review=review, reason="t")


@pytest.fixture(autouse=True)
def _clean():
    clear_session_state()
    from ai_assistant.hh_human_submission import clear_all_submission_state
    clear_all_submission_state()
    yield
    clear_all_submission_state()
    clear_session_state()


# ---------------- classification / switch ----------------

def test_default_mode_is_review_and_env_switch_off_by_default():
    assert DEFAULT_MODE is ApplyMode.REVIEW
    assert resolve_mode() is ApplyMode.REVIEW
    assert resolve_mode("") is ApplyMode.REVIEW
    assert resolve_mode("AUTO") is ApplyMode.AUTO
    assert resolve_mode("auto_apply_mode") is ApplyMode.AUTO
    assert resolve_mode(None, env={"HH_APPLY_MODE": "AUTO"}) is ApplyMode.AUTO


def test_classification_simple_letter_questionnaire():
    assert classify_form(_simple_form()) is FormKind.SIMPLE
    snap = {"controls": [{"tag": "TEXTAREA", "type": "textarea", "name": "",
                          "dataQa": "vacancy-response-popup-form-letter-input"}]}
    assert classify_form(_simple_form(), snap) is FormKind.COVER_LETTER_ONLY
    assert classify_form(_questionnaire_form(), QUESTIONNAIRE_SNAPSHOT) is FormKind.QUESTIONNAIRE


# ---------------- REVIEW_MODE: simple & letter -> auto submit ----------------

def test_review_simple_form_auto_submits_once():
    dom = FakeHH(markers=["Вы откликнулись"])
    rep = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                         mode=ApplyMode.REVIEW)
    assert rep.verdict == "SUBMITTED"
    assert rep.submit_count == 1 and rep.click_count == 1
    assert dom.clicks == 1
    assert rep.approved_by == "policy:simple_response:REVIEW_MODE"


def test_review_cover_letter_filled_then_submitted():
    dom = FakeHH(markers=["Вы откликнулись"])
    dom.add("textarea", "cover_letter", "")
    pkg = _pkg(cover="Готов к интервью.", answers=_letter_answers())
    rep = run_auto_apply(pkg, dom.evaluate, LETTER_SNAPSHOT,
                         form=_letter_form(), mode=ApplyMode.REVIEW)
    assert rep.form_kind == FormKind.COVER_LETTER_ONLY.value
    assert rep.verdict == "SUBMITTED"
    assert rep.fill_count == 1 and rep.verified_operations == 1
    assert dom.controls[("textarea", "cover_letter", "")]["value"] == "Готов к интервью."
    assert dom.clicks == 1


# ---------------- cover letter rule (Stage 21 addendum) ---------------------

def test_optional_letter_not_generated_not_filled_submits_without_it():
    from ai_assistant.auto_apply_modes import _letter_required_state
    assert _letter_required_state(OPTIONAL_LETTER_SNAPSHOT) is False
    dom = FakeHH(markers=["Вы откликнулись"])
    dom.add("textarea", "", "")  # letter control exists but must stay empty
    pkg = _pkg(cover="Черновик из upstream - НЕ должен попасть в форму.",
               answers=_letter_answers())  # upstream generated a letter anyway
    rep = run_auto_apply(pkg, dom.evaluate, OPTIONAL_LETTER_SNAPSHOT,
                         form=_letter_form(), mode=ApplyMode.REVIEW)
    # optional letter -> plain simple_response path, letter untouched
    assert rep.form_kind == FormKind.SIMPLE.value
    assert rep.cover_letter_required is False
    assert rep.verdict == "SUBMITTED"
    assert rep.fill_count == 0 and rep.planned_operations == 0
    assert dom.controls[("textarea", "", "")]["value"] == ""  # never filled
    assert all(a.answer_type is not QuestionType.COVER_LETTER for a in pkg.answers)
    assert dom.clicks == 1


def test_required_letter_generated_from_truth_sources_and_filled():
    from ai_assistant.auto_apply_modes import _letter_required_state
    assert _letter_required_state(LETTER_SNAPSHOT) is True
    dom = FakeHH(markers=["Вы откликнулись"])
    dom.add("textarea", "cover_letter", "")
    pkg = _pkg(cover="Готов к интервью.", answers=_letter_answers())
    rep = run_auto_apply(pkg, dom.evaluate, LETTER_SNAPSHOT,
                         form=_letter_form(), mode=ApplyMode.REVIEW)
    assert rep.cover_letter_required is True
    assert rep.verdict == "SUBMITTED"
    assert rep.fill_count == 1
    assert dom.controls[("textarea", "cover_letter", "")]["value"] == "Готов к интервью."


def test_unknown_letter_requiredness_stops_never_autogenerates():
    from ai_assistant.auto_apply_modes import _letter_required_state
    assert _letter_required_state(UNKNOWN_LETTER_SNAPSHOT) is None
    # REVIEW_MODE: stopped for the human, zero mutations, nothing submitted
    dom = FakeHH()
    pkg = _pkg(cover="Черновик.", answers=_letter_answers())
    rep = run_auto_apply(pkg, dom.evaluate, UNKNOWN_LETTER_SNAPSHOT,
                         form=_letter_form(), mode=ApplyMode.REVIEW)
    assert rep.verdict == V_NEEDS_HUMAN_REVIEW
    assert rep.cover_letter_required is None
    assert rep.submit_count == 0 and rep.click_count == 0 and dom.clicks == 0
    # AUTO_APPLY_MODE: hard stop, never auto-generates
    dom2 = FakeHH()
    rep2 = run_auto_apply(_pkg(cover="Черновик.", answers=_letter_answers()),
                          dom2.evaluate, UNKNOWN_LETTER_SNAPSHOT,
                          form=_letter_form(), mode=ApplyMode.AUTO)
    assert rep2.verdict == V_REQUIRED_UNKNOWN
    assert dom2.clicks == 0


def test_no_letter_control_means_not_required():
    from ai_assistant.auto_apply_modes import _letter_required_state
    assert _letter_required_state({}) is False
    assert _letter_required_state({"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_1"}]}) is False


# ---------------- REVIEW_MODE: questionnaire stops before submit ------------

def test_review_questionnaire_stops_before_submit_for_human():
    dom = FakeHH()
    dom.add("radio", "task_100", "3 года")
    dom.add("textarea", "task_200_text", "")
    pkg = _pkg(answers=[_qa("hh__ctrl_task_100", "3 года", QuestionType.RADIO),
                        _qa("hh__ctrl_task_200_text", "Проект X", QuestionType.TEXTAREA)])
    rep = run_auto_apply(pkg, dom.evaluate, QUESTIONNAIRE_SNAPSHOT,
                         form=_questionnaire_form(), mode=ApplyMode.REVIEW)
    assert rep.verdict == V_NEEDS_HUMAN_REVIEW
    assert rep.review_status == "READY_FOR_HUMAN_REVIEW"
    assert rep.submit_count == 0 and rep.click_count == 0
    assert dom.clicks == 0
    # prefill DID happen (safe, verified) but submit did not
    assert rep.verified_operations == 2
    assert len(rep.answers) == 2 and rep.fingerprint


# ---------------- AUTO_APPLY_MODE: questionnaire only after all gates -------

def test_auto_questionnaire_all_gates_pass_single_submit():
    dom = FakeHH(markers=["Вы откликнулись"])
    dom.add("radio", "task_100", "3 года")
    dom.add("textarea", "task_200_text", "")
    pkg = _pkg(answers=[_qa("hh__ctrl_task_100", "3 года", QuestionType.RADIO),
                        _qa("hh__ctrl_task_200_text", "Проект X", QuestionType.TEXTAREA)])
    form = _questionnaire_form(required=False)
    rep = run_auto_apply(pkg, dom.evaluate, QUESTIONNAIRE_SNAPSHOT,
                         form=form, mode=ApplyMode.AUTO)
    assert rep.verdict == "SUBMITTED"
    assert rep.approved_by == "policy:screening_questions:AUTO_APPLY_MODE"
    assert dom.clicks == 1 and rep.submit_count == 1


def test_auto_unknown_answer_stops_before_any_mutation_or_submit():
    dom = FakeHH()
    pkg = _pkg(answers=[_qa("hh__ctrl_task_100", "3 года", QuestionType.RADIO),
                        _qa("hh__ctrl_task_200_text", "Проект X", QuestionType.TEXTAREA,
                            review=True)])
    rep = run_auto_apply(pkg, dom.evaluate, QUESTIONNAIRE_SNAPSHOT,
                         form=_questionnaire_form(), mode=ApplyMode.AUTO)
    assert rep.verdict == V_UNRESOLVED
    assert rep.submit_count == 0 and rep.executed_operations == 0
    assert dom.clicks == 0


def test_required_none_questionnaire_auto_mode_stops():
    dom = FakeHH()
    pkg = _pkg(answers=[_qa("hh__ctrl_task_100", "3 года", QuestionType.RADIO),
                        _qa("hh__ctrl_task_200_text", "Проект X", QuestionType.TEXTAREA)])
    rep = run_auto_apply(pkg, dom.evaluate, QUESTIONNAIRE_SNAPSHOT,
                         form=_questionnaire_form(required=None), mode=ApplyMode.AUTO)
    assert rep.verdict == V_REQUIRED_UNKNOWN
    assert rep.submit_count == 0 and dom.clicks == 0
    # REVIEW mode with same shape goes to human review instead of hard stop
    dom2 = FakeHH()
    dom2.add("radio", "task_100", "3 года")
    dom2.add("textarea", "task_200_text", "")
    rep2 = run_auto_apply(_pkg(answers=[_qa("hh__ctrl_task_100", "3 года", QuestionType.RADIO),
                                        _qa("hh__ctrl_task_200_text", "Проект X", QuestionType.TEXTAREA)]),
                          dom2.evaluate, QUESTIONNAIRE_SNAPSHOT,
                          form=_questionnaire_form(required=None), mode=ApplyMode.REVIEW)
    assert rep2.verdict == V_NEEDS_HUMAN_REVIEW and dom2.clicks == 0


def test_unresolved_plan_stops_both_modes():
    # letter-only form but package has empty cover letter -> unresolved op
    dom = FakeHH()
    pkg = _pkg(cover="", answers=[])
    rep = run_auto_apply(pkg, dom.evaluate, LETTER_SNAPSHOT,
                         form=_letter_form(), mode=ApplyMode.REVIEW)
    assert rep.verdict == V_UNRESOLVED and dom.clicks == 0


# ---------------- safety gates ---------------------------------------------

def test_duplicate_vacancy_blocked_after_attempt():
    dom = FakeHH(markers=["Вы откликнулись"])
    first = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                           mode=ApplyMode.REVIEW)
    assert first.verdict == "SUBMITTED"
    second = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                            mode=ApplyMode.REVIEW)
    assert second.verdict == V_DUPLICATE
    assert second.stop_reason and "duplicates" in second.stop_reason
    assert dom.clicks == 1  # no second click ever


def test_external_submitted_registry_blocks_duplicate():
    dom = FakeHH()
    rep = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                         mode=ApplyMode.REVIEW,
                         submitted_vacancies={"hh:222"})
    assert rep.verdict == V_DUPLICATE and dom.clicks == 0


def test_wrong_url_fail_closed_zero_clicks():
    dom = FakeHH(url="https://evil.example.com/submit")
    rep = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                         mode=ApplyMode.REVIEW)
    assert rep.verdict in ("FAIL_CLOSED", "BLOCKED_INTERNAL")
    assert rep.submit_report and rep.submit_report["click_count"] == 0
    assert dom.clicks == 0


def test_wrong_vacancy_fail_closed_zero_clicks():
    dom = FakeHH(url="https://hh.ru/applicant/vacancy_response?vacancyId=999")
    rep = run_auto_apply(_pkg(vid="hh:222"), dom.evaluate, {},
                         form=_simple_form(), mode=ApplyMode.REVIEW)
    assert rep.verdict in ("FAIL_CLOSED", "BLOCKED")
    assert dom.clicks == 0


def test_fingerprint_mismatch_blocks_zero_clicks(monkeypatch):
    # Tamper with the stored fingerprint right before the gated submission
    # re-checks it -> confirmation/preflight must refuse (stale review).
    import ai_assistant.hh_controlled_submit as hcs

    orig_confirm = hcs.confirm_human_submission

    def tampering_confirm(store, rid, fp, vid):
        store._reviews[rid]["fingerprint"] = "0" * 64
        return orig_confirm(store, rid, fp, vid)

    monkeypatch.setattr(hcs, "confirm_human_submission", tampering_confirm)
    dom = FakeHH(markers=["Вы откликнулись"])
    rep = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                         mode=ApplyMode.REVIEW)
    assert rep.verdict in ("BLOCKED", "FAIL_CLOSED")
    assert rep.submit_report is None or rep.submit_report["click_count"] == 0
    assert dom.clicks == 0


def test_failed_prefill_operation_stops_no_submit():
    dom = FakeHH()
    dom.add("radio", "task_100", "3 года", disabled=True)  # target broken
    pkg = _pkg(answers=[_qa("hh__ctrl_task_100", "3 года", QuestionType.RADIO),
                        _qa("hh__ctrl_task_200_text", "Проект X", QuestionType.TEXTAREA)])
    rep = run_auto_apply(pkg, dom.evaluate, QUESTIONNAIRE_SNAPSHOT,
                         form=_questionnaire_form(), mode=ApplyMode.AUTO)
    assert rep.verdict == V_PREFILL_FAILED
    assert rep.failed_operations >= 1 or rep.skipped_operations >= 1
    assert rep.submit_count == 0 and dom.clicks == 0


def test_invalid_package_stops():
    dom = FakeHH()
    rep = run_auto_apply(_pkg(status="NEEDS_REVIEW"), dom.evaluate, {},
                         form=_simple_form(), mode=ApplyMode.AUTO)
    assert rep.verdict == "BLOCKED_INVALID_PACKAGE"
    assert dom.clicks == 0


# ---------------- no retry / determinism ------------------------------------

def test_no_retry_after_submission_unknown():
    dom = FakeHH(markers=[])  # HH proves nothing -> SUBMISSION_UNKNOWN
    rep1 = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                          mode=ApplyMode.REVIEW)
    assert rep1.verdict == "SUBMISSION_UNKNOWN"
    assert dom.clicks == 1
    rep2 = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                          mode=ApplyMode.REVIEW)
    assert rep2.verdict == V_DUPLICATE
    assert dom.clicks == 1  # never retried


def test_deterministic_behavior():
    from ai_assistant.hh_human_submission import clear_all_submission_state

    def run_once():
        clear_session_state()
        clear_all_submission_state()
        dom = FakeHH(markers=["Вы откликнулись"])
        r = run_auto_apply(_pkg(), dom.evaluate, {}, form=_simple_form(),
                           mode=ApplyMode.REVIEW)
        d = r.model_dump()
        d.pop("generated_at")
        sub = d.pop("submit_report") or {}
        for k in ("generated_at", "button_meta"):
            sub.pop(k, None)
        d["submit_report"] = sub
        return d

    a, b = run_once(), run_once()
    assert a == b
    assert a["verdict"] == "SUBMITTED"