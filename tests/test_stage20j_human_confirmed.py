"""Stage 20J tests: Human-Confirmed Submission.

Tests the explicit confirmation layer on top of Stage 20I. Without
confirmation -> 0 clicks. All 11 gates are re-checked before the single
click. One approved review -> max one submit, no retry.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant.hh_submission import (
    SubmissionStatus,
    clear_submitted_reviews,
)
from ai_assistant.hh_human_submission import (
    clear_human_confirmations,
    confirm_human_submission,
    submit_with_human_confirmation,
)
from ai_assistant.application_review_gate import (
    GateStatus,
    HumanReviewStore,
    build_review_gate,
)
from ai_assistant.prefill_orchestrate import GroupCheck, OrchestrationReport
from ai_assistant.prefill_plan import PrefillPlan
from ai_assistant.hh_extractor import (
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionType,
    QuestionSource,
)
from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation


class FakeDOM:
    def __init__(self, url="https://hh.ru/applicant/vacancy_response?vacancyId=136591579",
                 submit_found=True, submit_disabled=False, body_after=""):
        self.url = url
        self.submit_found = submit_found
        self.submit_disabled = submit_disabled
        self.body_after = body_after
        self.clicked = False

    def _url_json(self):
        return json.dumps({"url": self.url})

    def evaluate(self, expression: str) -> str:
        if expression == 'JSON.stringify({url: location.href})':
            return self._url_json()
        if '"vacancy-response-submit-popup"' in expression and "el.click()" not in expression:
            if not self.submit_found:
                return json.dumps({"found": False})
            return json.dumps({"found": True, "tag": "BUTTON", "type": "submit",
                               "text": "Откликнуться", "dataQa": "vacancy-response-submit-popup",
                               "disabled": self.submit_disabled, "visible": True, "cls": "magritte-button_mode-primary"})
        if 'el.click()' in expression:
            if not self.submit_found:
                return json.dumps({"ok": False, "reason": "submit button not found"})
            if self.submit_disabled:
                return json.dumps({"ok": False, "reason": "submit button is disabled"})
            self.clicked = True
            return json.dumps({"ok": True})
        if "document.body" in expression:
            text = self.body_after if self.clicked else ""
            return json.dumps({"text": text})
        raise RuntimeError(f"FakeDOM: unknown expression: {expression[:80]}")


def _form():
    return ApplicationForm(
        source="hh", vacancy_stable_id="hh:136591579",
        application_type=ApplicationType.screening_questions,
        questions=[
            ApplicationQuestion(id="hh__ctrl_task_384589146", label="Опыт?",
                                normalized_type=QuestionType.RADIO, required=False,
                                options=["Менее 3 лет", "5-7 лет"], source=QuestionSource.SCREENING),
        ])


def _snapshot():
    return {"controls": [
        {"tag": "INPUT", "type": "radio", "name": "task_384589146", "label": "Менее 3 лет", "visible": True, "disabled": False, "readOnly": False},
    ]}


def _pkg():
    pkg = ApplicationPackage(
        vacancy_id="hh:136591579", vacancy_stable_id="hh:136591579",
        resume_adaptation_needed=False, resume_summary="s",
        tailored_skills=["python"], relevant_experience=["e"],
        cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
        warnings=[], generator_version="v1",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"], relevant_experience_points=["e"]))
    pkg.validation_status = "VALID"
    return pkg


def _valid_plan():
    return PrefillPlan(vacancy_stable_id="hh:136591579", status="VALID")


def _valid_orchestration():
    return OrchestrationReport(
        vacancy_stable_id="hh:136591579",
        verdict="VERIFIED", planned_operations=1, executed_operations=1,
        verified_operations=1, failed_operations=0, skipped_operations=0,
        group_checks=[GroupCheck(group_name="task_384589146", input_type="radio",
                                 expected_checked=["Менее 3 лет"],
                                 actual_checked=["Менее 3 лет"], ok=True)])


def _approved_review():
    form = _form()
    plan = _valid_plan()
    orch = _valid_orchestration()
    pkg = _pkg()
    from ai_assistant.hh_extractor import ApplicationAnswer
    pkg.answers = [ApplicationAnswer(question_id="hh__ctrl_task_384589146", answer="Менее 3 лет",
                                     answer_type=QuestionType.RADIO, confidence=1.0,
                                     requires_review=False, reason="c")]
    gate = build_review_gate(pkg, plan, orch, _snapshot(), form)
    assert gate.status == GateStatus.READY_FOR_HUMAN_REVIEW
    store = HumanReviewStore()
    rid = store.save(gate)
    store.mark_waiting_for_human(rid)
    res = store.approve_review(rid, gate.fingerprint)
    assert res["ok"] is True
    return gate, plan, orch, pkg, store, rid


@pytest.fixture(autouse=True)
def _clear():
    clear_submitted_reviews()
    clear_human_confirmations()
    yield
    clear_submitted_reviews()
    clear_human_confirmations()


def test_no_confirmation_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_review()
    dom = FakeDOM(body_after="Вы откликнулись")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0
    assert rep.click_count == 0
    assert rep.status == SubmissionStatus.BLOCKED
    assert dom.clicked is False


def test_all_gate_failures_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    pkg.validation_status = "NEEDS_REVIEW"
    dom = FakeDOM()
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0


def test_successful_single_click():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 1
    assert rep.click_count == 1
    assert rep.status in (SubmissionStatus.SUBMITTED, SubmissionStatus.SUBMISSION_UNKNOWN)
    assert dom.clicked is True


def test_no_retry_second_submit_zero():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep1 = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep1.submit_count == 1
    # second attempt must be blocked even with a new confirmation (one-shot submit)
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    rep2 = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep2.submit_count == 0
    assert rep2.status == SubmissionStatus.BLOCKED


def test_repeated_submit_zero_additional():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep1 = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep1.submit_count == 1
    # Without a new explicit confirmation, second call is blocked
    rep2 = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep2.submit_count == 0


def test_missing_submit_button_zero():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(submit_found=False)
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0
    assert rep.status == SubmissionStatus.BLOCKED


def test_disabled_button_zero():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(submit_disabled=True)
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0
    assert rep.status == SubmissionStatus.BLOCKED
    assert dom.clicked is False


def test_wrong_url_zero():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(url="https://evil.example.com/form")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0
    assert rep.status in (SubmissionStatus.BLOCKED, SubmissionStatus.FAIL_CLOSED)


def test_wrong_vacancy_zero():
    gate, plan, orch, pkg, store, rid = _approved_review()
    # confirm with correct vacancy, but pkg has wrong vacancy at submit time
    confirm_human_submission(store, rid, gate.fingerprint, "hh:136591579")
    pkg.vacancy_stable_id = "hh:99999999"
    pkg.vacancy_id = "hh:99999999"
    dom = FakeDOM(url="https://hh.ru/applicant/vacancy_response?vacancyId=99999999")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0


def test_fingerprint_change_zero():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep = submit_with_human_confirmation(store, rid, "0" * 64, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0
    assert rep.status in (SubmissionStatus.BLOCKED, SubmissionStatus.FAIL_CLOSED)


def test_dom_change_zero():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    # Simulate DOM change by invalidating the review
    store.invalidate_on_change(rid, "0" * 64)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 0


def test_unknown_result():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="")  # no success marker
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.status == SubmissionStatus.SUBMISSION_UNKNOWN
    assert rep.submit_count == 1


def test_url_change_after_submit():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(url="https://hh.ru/applicant/vacancy_response?vacancyId=136591579")
    orig_evaluate = dom.evaluate

    def navigating_evaluate(expr):
        res = orig_evaluate(expr)
        if "el.click()" in expr:
            import json as _j
            d = _j.loads(res)
            if d.get("ok"):
                dom.url = "https://hh.ru/applicant/negotiations"
        return res

    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, navigating_evaluate)
    assert rep.url_before != rep.url_after
    assert rep.submit_count == 1


def test_submit_failure():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(submit_disabled=True)  # will fail at preflight, not click
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.status == SubmissionStatus.BLOCKED
    assert rep.submit_count == 0
    # Now test a click that fails
    dom2 = FakeDOM()
    orig = dom2.evaluate

    def fail_click(expr):
        if "el.click()" in expr:
            import json as _j
            return _j.dumps({"ok": False, "reason": "click intercepted"})
        return orig(expr)

    clear_submitted_reviews()
    clear_human_confirmations()
    gate2, plan2, orch2, pkg2, store2, rid2 = _approved_review()
    confirm_human_submission(store2, rid2, gate2.fingerprint, pkg2.vacancy_stable_id)
    rep2 = submit_with_human_confirmation(store2, rid2, gate2.fingerprint, pkg2, plan2, orch2, fail_click)
    assert rep2.status == SubmissionStatus.FAILED
    assert rep2.submit_count == 1
    assert rep2.failed_submit == 1


def test_no_db_writes(monkeypatch):
    import ai_assistant.db as db

    def forbidden(*a, **k):
        raise AssertionError("DB access during submission")

    monkeypatch.setattr(db, "get_connection", forbidden)
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 1


def test_no_cookies_storage_in_report():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    raw = rep.model_dump_json().lower()
    for marker in ["cookie", "storage_state", "token", "password", "authorization"]:
        assert marker not in raw


def test_no_navigation():
    gate, plan, orch, pkg, store, rid = _approved_review()
    confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    dom = FakeDOM(body_after="Вы откликнулись")
    rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.navigation_count == 0
    assert rep.url_before == rep.url_after or "negotiations" in (rep.url_after or "").lower() or rep.url_after == rep.url_before


def test_deterministic_report():
    def run():
        clear_submitted_reviews()
        clear_human_confirmations()
        gate, plan, orch, pkg, store, rid = _approved_review()
        confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
        dom = FakeDOM(body_after="Вы откликнулись")
        rep = submit_with_human_confirmation(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
        d = rep.model_dump()
        d.pop("generated_at")
        return d

    r1 = run()
    r2 = run()
    assert r1 == r2
