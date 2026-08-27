"""Stage 20K tests: controlled real submit + read-only verification.

FakeCDP simulates the full lifecycle: gates, preflight, single click,
post-submit markers. Verifies exactly-one-click, fail-closed on all
gate failures, and correct verdicts.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant.hh_controlled_submit import controlled_real_submit
from ai_assistant.hh_human_submission import clear_human_confirmations
from ai_assistant.hh_submission import SubmissionStatus, clear_submitted_reviews
from ai_assistant.application_review_gate import (
    GateStatus,
    HumanReviewGate,
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


class FakeCDP:
    """Simulates CDP evaluate for full submit lifecycle."""

    def __init__(self, url="https://hh.ru/applicant/vacancy_response?vacancyId=136591579",
                 submit_found=True, submit_disabled=False,
                 body_markers=None, navigate_after_click=False):
        self.url = url
        self.submit_found = submit_found
        self.submit_disabled = submit_disabled
        self.body_markers = body_markers or []
        self.navigate_after_click = navigate_after_click
        self.clicked = False
        self.click_count_internal = 0

    def evaluate(self, expression: str) -> str:
        # URL read
        if expression == 'JSON.stringify({url: location.href})':
            url = self.url
            if self.navigate_after_click and self.clicked:
                url = "https://hh.ru/applicant/negotiations"
            return json.dumps({"url": url})
        # Post-submit marker check
        if "markers" in expression and "body" in expression:
            return json.dumps({"found": self.body_markers if self.clicked else [],
                               "url": self.url})
        # Submit button meta (read-only)
        if '"vacancy-response-submit-popup"' in expression and "el.click()" not in expression:
            if not self.submit_found:
                return json.dumps({"found": False})
            return json.dumps({"found": True, "tag": "BUTTON", "type": "submit",
                               "text": "Откликнуться", "dataQa": "vacancy-response-submit-popup",
                               "disabled": self.submit_disabled, "visible": True, "cls": "magritte-button_mode-primary"})
        # Submit click
        if "el.click()" in expression:
            self.click_count_internal += 1
            if not self.submit_found:
                return json.dumps({"ok": False, "reason": "submit button not found"})
            if self.submit_disabled:
                return json.dumps({"ok": False, "reason": "submit button is disabled"})
            self.clicked = True
            return json.dumps({"ok": True})
        raise RuntimeError(f"FakeCDP: unknown expression: {expression[:80]}")


def _form():
    return ApplicationForm(
        source="hh", vacancy_stable_id="hh:136591579",
        application_type=ApplicationType.screening_questions,
        questions=[
            ApplicationQuestion(id="hh__ctrl_task_384589146", label="Опыт?",
                                normalized_type=QuestionType.RADIO, required=False,
                                options=["Менее 3 лет", "5-7 лет"], source=QuestionSource.SCREENING),
            ApplicationQuestion(id="hh__ctrl_task_384589151", label="Агенты?",
                                normalized_type=QuestionType.CHECKBOX, required=False,
                                options=["Claude Code", "Cursor"], source=QuestionSource.SCREENING),
        ])


def _pkg():
    pkg = ApplicationPackage(
        vacancy_id="hh:136591579", vacancy_stable_id="hh:136591579",
        resume_adaptation_needed=False, resume_summary="s",
        tailored_skills=["python"], relevant_experience=["e"],
        cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
        warnings=[], generator_version="v1",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"],
                                    relevant_experience_points=["e"]))
    pkg.validation_status = "VALID"
    return pkg


def _plan():
    return PrefillPlan(vacancy_stable_id="hh:136591579", status="VALID")


def _orch():
    return OrchestrationReport(
        vacancy_stable_id="hh:136591579",
        verdict="VERIFIED", planned_operations=2, executed_operations=2,
        verified_operations=2, failed_operations=0, skipped_operations=0,
        group_checks=[
            GroupCheck(group_name="task_384589146", input_type="radio",
                       expected_checked=["Менее 3 лет"],
                       actual_checked=["Менее 3 лет"], ok=True),
            GroupCheck(group_name="task_384589151", input_type="checkbox",
                       expected_checked=["Claude Code"],
                       actual_checked=["Claude Code"], ok=True),
        ])


def _approved_setup():
    form = _form()
    plan = _plan()
    orch = _orch()
    pkg = _pkg()
    from ai_assistant.hh_extractor import ApplicationAnswer
    pkg.answers = [
        ApplicationAnswer(question_id="hh__ctrl_task_384589146", answer="Менее 3 лет",
                          answer_type=QuestionType.RADIO, confidence=1.0,
                          requires_review=False, reason="c"),
        ApplicationAnswer(question_id="hh__ctrl_task_384589151", answer="Claude Code",
                          answer_type=QuestionType.CHECKBOX, confidence=1.0,
                          requires_review=False, reason="c"),
    ]
    gate = build_review_gate(pkg, plan, orch, {"controls": []}, form)
    assert gate.status == GateStatus.READY_FOR_HUMAN_REVIEW
    store = HumanReviewStore()
    rid = store.save(gate)
    store.mark_waiting_for_human(rid)
    res = store.approve_review(rid, gate.fingerprint)
    assert res["ok"] is True

    from ai_assistant.hh_human_submission import confirm_human_submission
    conf = confirm_human_submission(store, rid, gate.fingerprint, pkg.vacancy_stable_id)
    assert conf["ok"] is True

    return gate, plan, orch, pkg, store, rid


@pytest.fixture(autouse=True)
def _clear():
    from ai_assistant.hh_human_submission import clear_all_submission_state
    clear_all_submission_state()
    yield


# ---------- 1. all gates pass -> 1 click ----------

def test_all_gates_pass_one_click():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.verdict == "SUBMITTED"
    assert rep.click_count == 1
    assert rep.submit_count == 1
    assert rep.successful_submit == 1
    assert rep.failed_submit == 0
    assert dom.clicked is True
    assert dom.click_count_internal == 1


# ---------- 2. gate failure -> 0 clicks ----------

def test_gate_failure_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    pkg.validation_status = "NEEDS_REVIEW"
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.verdict == "BLOCKED"
    assert rep.click_count == 0
    assert rep.submit_count == 0
    assert dom.clicked is False


# ---------- 3. wrong vacancy -> 0 clicks ----------

def test_wrong_vacancy_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    pkg.vacancy_stable_id = "hh:99999999"
    pkg.vacancy_id = "hh:99999999"
    dom = FakeCDP(url="https://hh.ru/applicant/vacancy_response?vacancyId=99999999")
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.click_count == 0
    assert rep.verdict in ("BLOCKED", "FAIL_CLOSED")


# ---------- 4. wrong URL -> 0 clicks ----------

def test_wrong_url_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(url="https://evil.example.com/submit")
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.verdict == "FAIL_CLOSED"
    assert rep.click_count == 0


# ---------- 5. fingerprint mismatch -> 0 clicks ----------

def test_fingerprint_mismatch_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, "0" * 64, pkg, plan, orch, dom.evaluate)
    assert rep.click_count == 0
    assert rep.verdict in ("BLOCKED", "FAIL_CLOSED")


# ---------- 6. stale confirmation -> 0 clicks ----------

def test_stale_confirmation_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    # invalidate review after confirmation
    store.invalidate_on_change(rid, "changed" * 8)
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.click_count == 0


# ---------- 7. missing button -> 0 clicks ----------

def test_missing_button_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(submit_found=False)
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.click_count == 0
    assert rep.submit_count == 0
    assert rep.verdict == "BLOCKED"


# ---------- 8. disabled button -> 0 clicks ----------

def test_disabled_button_zero_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(submit_disabled=True)
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.click_count == 0
    assert rep.submit_count == 0
    assert rep.verdict == "BLOCKED"


# ---------- 9. second call -> 0 additional clicks ----------

def test_second_call_zero_additional_clicks():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=["Вы откликнулись"])

    # First call - real click
    rep1 = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep1.click_count == 1
    first_internal = dom.click_count_internal

    # Second call with same confirmation -> BLOCKED (one-shot consumed)
    rep2 = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep2.verdict in ("BLOCKED", "FAIL_CLOSED")
    assert rep2.click_count == 0
    assert rep2.submit_count == 0
    # No additional internal clicks happened
    assert dom.click_count_internal == first_internal


# ---------- 10. SUBMITTED verdict ----------

def test_submitted_verdict():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.verdict == "SUBMITTED"
    assert len(rep.body_markers_found) > 0


# ---------- 11. SUBMISSION_UNKNOWN verdict ----------

def test_unknown_verdict():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=[])  # no success markers
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.verdict == "SUBMISSION_UNKNOWN"
    assert rep.submit_count == 1
    assert rep.successful_submit == 1  # click succeeded


# ---------- 12. FAILED verdict ----------

def test_failed_verdict():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    # Button exists but disabled -> preflight blocks
    dom = FakeCDP(submit_disabled=True)
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.verdict == "BLOCKED"
    assert rep.submit_count == 0


# ---------- 13. FAIL_CLOSED on URL leaving hh.ru ----------

def test_fail_closed_url_leaves_hh():
    class LeavingDOM(FakeCDP):
        def evaluate(self, expression):
            res = super().evaluate(expression)
            if "markers" in expression and "body" in expression and self.clicked:
                return json.dumps({"found": [], "url": "https://evil.example.com/page"})
            return res

    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = LeavingDOM()
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.verdict == "FAIL_CLOSED"
    assert "left hh.ru" in rep.stop_reason


# ---------- 14. no DB writes ----------

def test_no_db_writes(monkeypatch):
    import ai_assistant.db as db

    def forbidden(*a, **k):
        raise AssertionError("DB access during submission")

    monkeypatch.setattr(db, "get_connection", forbidden)
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.submit_count == 1


# ---------- 15. no cookies/storage ----------

def test_no_cookies_storage():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    raw = rep.model_dump_json().lower()
    for marker in ["cookie", "storage_state", "token", "password", "authorization"]:
        assert marker not in raw


# ---------- 16. no navigation ----------

def test_no_navigation():
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = FakeCDP(body_markers=["Вы откликнулись"])
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    assert rep.navigation_count == 0


# ---------- 17. deterministic report ----------

def test_deterministic_report():
    def run():
        gate, plan, orch, pkg, store, rid = _approved_setup()
        dom = FakeCDP(body_markers=["Вы откликнулись"])
        rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
        d = rep.model_dump()
        d.pop("generated_at")
        # reset global one-shot state so a fresh run is possible (deterministic review_id)
        clear_submitted_reviews()
        return d

    r1 = run()
    r2 = run()
    assert r1 == r2


# ---------- 18. module has no forbidden APIs ----------

def test_module_no_forbidden_apis():
    src = pathlib.Path("ai_assistant/hh_controlled_submit.py").read_text(encoding="utf-8")
    for banned in [".goto(", ".fill(", ".type(", ".set_input_files(",
                   ".check(", ".uncheck(", ".keyboard", ".mouse",
                   "sqlite3", "get_connection", "requests.", "urllib"]:
        assert banned not in src, f"FORBIDDEN: {banned} in hh_controlled_submit.py"


import pathlib