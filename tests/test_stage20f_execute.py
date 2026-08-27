"""Stage 20F tests: safe prefill execution + verification.

Fake CDP evaluate simulates the real DOM: mutations change simulated state,
verification reads it back. Tests prove: exact-target mutations, fail-closed
URL guard, no click/submit/navigation/upload, review/UNKNOWN never executed,
verification catches mismatches, deterministic report.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ai_assistant.prefill_plan import (
    PrefillOperation,
    PrefillPlan,
    PrefillTarget,
)
from ai_assistant.prefill_execute import execute_prefill_plan
from ai_assistant.hh_extractor import QuestionType


class FakeDOM:
    """Simulates a page with form controls; evaluate_fn executes JS subsets.

    Only the JS patterns emitted by prefill_execute are understood:
      - URL read
      - radio/checkbox mutation + verification
      - textarea/text mutation + verification
    Any unknown expression -> error (fail closed).
    """

    def __init__(self, url="https://hh.ru/applicant/vacancy_response?vacancyId=136591579"):
        self.url = url
        self.controls: dict = {}  # (type, name, label) -> {"checked": bool, "value": str}
        self.submit_pressed = False
        self.navigated_to = None
        self.evaluate_calls = 0

    def add(self, ctype, name, label, checked=False, value="", disabled=False, readonly=False):
        key = (ctype, name, label)
        self.controls[key] = {"checked": checked, "value": value,
                              "disabled": disabled, "readonly": readonly}
        return key

    def evaluate(self, expression: str) -> str:
        self.evaluate_calls += 1
        # URL read
        if expression == "JSON.stringify({url: location.href})":
            if self.navigated_to:
                return json.dumps({"url": self.navigated_to})
            return json.dumps({"url": self.url})
        # radio/checkbox mutation (React-safe: native 'checked' setter + click/change)
        if "HTMLInputElement.prototype, 'checked'" in expression and "type='radio'" in expression:
            name = json.loads(expression.split("const name = ")[1].split(", label")[0])
            label = json.loads(expression.split("label = ")[1].split(";\n")[0])
            key = ("radio", name, label)
            if key not in self.controls:
                return json.dumps({"ok": False, "reason": "radio with exact label not found"})
            c = self.controls[key]
            if c["disabled"] or c["readonly"]:
                return json.dumps({"ok": False, "reason": "control is disabled/readonly"})
            c["checked"] = True
            return json.dumps({"ok": True, "checked": True, "reason": ""})
        # checkbox mutation (React-safe)
        if "HTMLInputElement.prototype, 'checked'" in expression and "type='checkbox'" in expression:
            name = json.loads(expression.split("const name = ")[1].split(", label")[0])
            label = json.loads(expression.split("label = ")[1].split(";\n")[0])
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
        # radio/checkbox verification (JS object literal: found: true, checked)
        if "found: true, checked" in expression:
            t = "radio" if "type='radio'" in expression else "checkbox"
            name = json.loads(expression.split("const name = ")[1].split(", label")[0])
            label = json.loads(expression.split("label = ")[1].split(";\n")[0])
            key = (t, name, label)
            if key not in self.controls:
                return json.dumps({"found": False})
            c = self.controls[key]
            return json.dumps({"found": True, "checked": c["checked"],
                               "disabled": c["disabled"], "readOnly": c["readonly"]})
        # textarea verification
        if "found: true, value" in expression:
            tag = "textarea" if "textarea[name=" in expression else "input"
            name = json.loads(expression.split("const name = ")[1].split(";\n")[0])
            key = (tag, name, "")
            if key not in self.controls:
                return json.dumps({"found": False})
            c = self.controls[key]
            return json.dumps({"found": True, "value": c["value"],
                               "disabled": c["disabled"], "readOnly": c["readonly"]})
        raise RuntimeError(f"FakeDOM: unknown expression: {expression[:120]}")


def _op(qid, ttype, name, label, value):
    return PrefillOperation(
        question_id=qid,
        target=PrefillTarget(tag="INPUT" if ttype in ("radio", "checkbox", "text") else "TEXTAREA",
                             type=ttype, name=name, label=label),
        value=value, source_answer=value, confidence=1.0, reason="test")


def _plan(ops):
    return PrefillPlan(vacancy_stable_id="hh:136591579", status="NEEDS_REVIEW", operations=ops)


# ---------- 1. valid RADIO execution ----------

def test_valid_radio_execution():
    dom = FakeDOM()
    dom.add("radio", "task_146", "Less than 3 years")
    plan = _plan([_op("hh__ctrl_task_146", "radio", "task_146", "Less than 3 years", "Less than 3 years")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.successful_mutations == 1
    assert dom.controls[("radio", "task_146", "Less than 3 years")]["checked"] is True


# ---------- 2-3. CHECKBOX ----------

def test_valid_checkbox_execution():
    dom = FakeDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is True


def test_multiple_checkbox_operations():
    dom = FakeDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    dom.add("checkbox", "task_151", "Cursor")
    plan = _plan([
        _op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code"),
        _op("hh__ctrl_task_151", "checkbox", "task_151", "Cursor", "Cursor"),
    ])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert rep.successful_mutations == 2
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is True
    assert dom.controls[("checkbox", "task_151", "Cursor")]["checked"] is True


# ---------- 4. exact target mismatch -> fail closed ----------

def test_exact_target_mismatch_fail_closed():
    dom = FakeDOM()
    dom.add("checkbox", "task_151", "Cursor")  # plan says Claude Code, DOM has Cursor only
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.verdict == "FAILED"
    assert rep.failed_mutations == 1
    # nothing was mutated
    assert dom.controls[("checkbox", "task_151", "Cursor")]["checked"] is False


# ---------- 5. missing target -> fail closed ----------

def test_missing_target_fail_closed():
    dom = FakeDOM()
    plan = _plan([_op("hh__ctrl_task_999", "checkbox", "task_999", "Ghost", "Ghost")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.verdict == "FAILED"
    assert rep.failed_mutations == 1


# ---------- 6. disabled control -> no mutation ----------

def test_disabled_control_no_mutation():
    dom = FakeDOM()
    dom.add("radio", "task_146", "Менее 3-ёх лет", disabled=True)
    plan = _plan([_op("hh__ctrl_task_146", "radio", "task_146", "Менее 3-ёх лет", "Менее 3-ёх лет")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert dom.controls[("radio", "task_146", "Менее 3-ёх лет")]["checked"] is False
    assert rep.failed_mutations == 1


# ---------- 7. readonly textarea -> no mutation ----------

def test_readonly_textarea_no_mutation():
    dom = FakeDOM()
    dom.add("textarea", "task_999_text", "", readonly=True)
    # PrefillTarget.readOnly is also set - execution refuses before mutation
    plan = _plan([PrefillOperation(
        question_id="hh__ctrl_task_999_text",
        target=PrefillTarget(tag="TEXTAREA", type="textarea", name="task_999_text", readOnly=True),
        value="text", source_answer="text", confidence=1.0, reason="test")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert dom.controls[("textarea", "task_999_text", "")]["value"] == ""
    assert rep.failed_mutations == 1


# ---------- 8. invalid option -> no mutation ----------

def test_invalid_option_no_mutation():
    dom = FakeDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Ghost Option", "Ghost Option")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False
    assert rep.failed_mutations == 1


# ---------- 9. review answer -> never executed ----------

def test_review_answer_never_executed():
    dom = FakeDOM()
    dom.add("textarea", "task_169_text", "")
    op = _op("hh__ctrl_task_169_text", "textarea", "task_169_text", "", "")
    op.value = ""  # review/empty answer
    plan = _plan([op])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.failed_mutations == 1
    assert dom.controls[("textarea", "task_169_text", "")]["value"] == ""


# ---------- 10. UNKNOWN answer -> never executed ----------

def test_unknown_answer_never_executed():
    dom = FakeDOM()
    dom.add("textarea", "task_169_text", "")
    op = PrefillOperation(
        question_id="hh__ctrl_task_169_text",
        target=PrefillTarget(tag="TEXTAREA", type="textarea", name="task_169_text"),
        value="", source_answer="", confidence=0.0, reason="UNKNOWN")
    plan = _plan([op])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert dom.controls[("textarea", "task_169_text", "")]["value"] == ""
    assert rep.failed_mutations == 1


# ---------- 11. forbidden URL -> zero mutations ----------

def test_forbidden_url_zero_mutations():
    dom = FakeDOM(url="https://evil.example.com/form")
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate, allowed_url_markers=["hh.ru"])
    assert rep.verdict == "FAIL_CLOSED"
    assert rep.successful_mutations == 0
    assert rep.failed_mutations == 0
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False


# ---------- 12. wrong tab -> zero mutations ----------

def test_wrong_tab_zero_mutations():
    dom = FakeDOM(url="https://hh.ru/search/vacancy")  # right domain, wrong page
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate,
                               allowed_url_markers=["hh.ru"],
                               required_url_markers=["applicant/vacancy_response"])
    assert rep.verdict == "FAIL_CLOSED"
    assert rep.successful_mutations == 0
    assert dom.controls[("checkbox", "task_151", "Claude Code")]["checked"] is False


# ---------- 13. navigation attempt -> zero mutations / detected ----------

def test_url_change_during_execution_detected():
    class NavigatingDOM(FakeDOM):
        def evaluate(self, expression):
            # navigate after first mutation call
            if "HTMLInputElement.prototype, 'checked'" in expression and self.navigated_to is None:
                self.navigated_to = "https://hh.ru/other"
            return super().evaluate(expression)

    dom = NavigatingDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    dom.add("checkbox", "task_151", "Cursor")
    plan = _plan([
        _op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code"),
        _op("hh__ctrl_task_151", "checkbox", "task_151", "Cursor", "Cursor"),
    ])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.verdict == "FAILED"
    assert "URL changed" in " ".join(rep.errors)
    assert rep.navigation_count == 0  # we never navigated; the page changed itself


# ---------- 14. submit button can never be executed ----------

def test_submit_button_never_executed():
    dom = FakeDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    # submit button exists on real page but is never part of any plan operation
    assert rep.submit_count == 0
    assert rep.click_count == 0
    assert rep.navigation_count == 0
    assert rep.upload_count == 0
    dom.submit_pressed = False  # never set


# ---------- 15-16. verification detects mismatches ----------

def test_verification_detects_wrong_checked_state():
    class LyingDOM(FakeDOM):
        def evaluate(self, expression):
            res = super().evaluate(expression)
            # verification lies about checked state (new matching: no quotes)
            if "found: true, checked" in expression:
                d = json.loads(res)
                if d.get("found"):
                    d["checked"] = False
                return json.dumps(d)
            return res

    dom = LyingDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    # Spec: any verification mismatch -> FAILED
    assert rep.verdict == "FAILED"
    assert any(not v["ok"] for v in rep.verification)
    assert any("checked != true" in v.get("reason", "") for v in rep.verification)


def test_verification_detects_wrong_textarea_value():
    class LyingDOM(FakeDOM):
        def evaluate(self, expression):
            res = super().evaluate(expression)
            if "found: true, value" in expression:
                d = json.loads(res)
                if d.get("found"):
                    d["value"] = "WRONG"
                return json.dumps(d)
            return res

    dom = LyingDOM()
    dom.add("textarea", "task_999_text", "")
    plan = _plan([_op("hh__ctrl_task_999_text", "textarea", "task_999_text", "", "Expected text")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    # Spec: any verification mismatch -> FAILED
    assert rep.verdict == "FAILED"
    assert any(not v["ok"] for v in rep.verification)
    assert any("value != expected" in v.get("reason", "") for v in rep.verification)


# ---------- 17. deterministic execution report ----------

def test_deterministic_execution_report():
    def run():
        dom = FakeDOM()
        dom.add("checkbox", "task_151", "Claude Code")
        dom.add("checkbox", "task_151", "Cursor")
        plan = _plan([
            _op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code"),
            _op("hh__ctrl_task_151", "checkbox", "task_151", "Cursor", "Cursor"),
        ])
        rep = execute_prefill_plan(plan, dom.evaluate)
        d = json.loads(rep.model_dump_json())
        d.pop("generated_at")
        return d
    assert run() == run()


# ---------- 18. real HH snapshot regression ----------

def test_real_snapshot_plan_executes_on_simulated_real_dom():
    snap = json.loads(pathlib.Path("artifacts/hh_manual_form_snapshot.json").read_text(encoding="utf-8"))["snapshot"]
    # build a FakeDOM from the real snapshot controls (checkbox Claude Code)
    dom = FakeDOM()
    for c in snap["controls"]:
        if c.get("type") == "checkbox" and c.get("label") == "Claude Code":
            dom.add("checkbox", c["name"], "Claude Code")
    plan = _plan([_op("hh__ctrl_task_384589151", "checkbox", "task_384589151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate,
                               allowed_url_markers=["hh.ru"],
                               required_url_markers=["applicant/vacancy_response"])
    assert rep.verdict == "VERIFIED"
    assert rep.successful_mutations == 1
    # instrumentation: no clicks, no submit, no navigation, no upload
    assert rep.click_count == 0
    assert rep.submit_count == 0
    assert rep.navigation_count == 0
    assert rep.upload_count == 0


# ---------- 19. no DB writes ----------

def test_no_db_writes(monkeypatch):
    import ai_assistant.db as db
    writes = {"n": 0}

    def forbidden(*a, **k):
        writes["n"] += 1
        raise AssertionError("DB access attempted during prefill execution")
    monkeypatch.setattr(db, "get_connection", forbidden)
    dom = FakeDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    assert rep.verdict == "VERIFIED"
    assert writes["n"] == 0


# ---------- 20. no cookies/storage access ----------

def test_no_cookies_storage_in_report():
    dom = FakeDOM()
    dom.add("checkbox", "task_151", "Claude Code")
    plan = _plan([_op("hh__ctrl_task_151", "checkbox", "task_151", "Claude Code", "Claude Code")])
    rep = execute_prefill_plan(plan, dom.evaluate)
    s = json.loads(rep.model_dump_json())
    raw = json.dumps(s, ensure_ascii=False).lower()
    for marker in ["cookie", "storage_state", "token", "password", "authorization"]:
        assert marker not in raw