"""Stage 20G: full safe prefill orchestration.

Orchestrates: VALID ApplicationPackage -> PrefillPlan -> safe execution ->
read-only verification -> single deterministic report.

HARD RULES:
- Automatic prefill ONLY when package.validation_status == "VALID" AND
  plan.status == "VALID" AND plan.unresolved is empty. Any NEEDS_REVIEW /
  UNKNOWN / missing validated answer / unknown required / custom-text issue
  => complete STOP with ZERO mutations.
- No submit, no click on "Откликнуться", no navigation/goto, no login,
  no upload, no cookies/storage access, no DB writes.
- Atomicity: first failed mutation stops subsequent mutations (skipped),
  verdict FAILED, executed operations are reported, no rollback.
- Group verification: RADIO group must have exactly the expected option
  checked; CHECKBOX group checked set must equal expected set.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from .hh_extractor import ApplicationForm, QuestionType
from .prefill_plan import PrefillPlan, build_prefill_plan
from .prefill_execute import execute_prefill_plan


class OperationStatus(str, Enum):
    PLANNED = "PLANNED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class TrackedOperation(BaseModel):
    question_id: str
    question_label: str = ""
    op_type: str = ""
    target_name: Optional[str] = None
    target_label: Optional[str] = None
    value: str = ""
    status: OperationStatus = OperationStatus.PLANNED
    reason: str = ""

    model_config = {"extra": "forbid"}


class GroupCheck(BaseModel):
    group_name: str
    input_type: str
    expected_checked: List[str] = Field(default_factory=list)
    actual_checked: List[str] = Field(default_factory=list)
    ok: bool = False

    model_config = {"extra": "forbid"}


class OrchestrationReport(BaseModel):
    verdict: str = "NOTHING_TO_EXECUTE"
    # VERIFIED | FAILED | STOPPED_NEEDS_REVIEW | FAIL_CLOSED | NOTHING_TO_EXECUTE
    stop_reason: str = ""
    generated_at: str = ""
    vacancy_stable_id: str = ""
    package_validation_status: str = ""
    plan_status: str = ""
    unresolved_count: int = 0
    planned_operations: int = 0
    executed_operations: int = 0
    verified_operations: int = 0
    skipped_operations: int = 0
    failed_operations: int = 0
    navigation_count: int = 0
    click_count: int = 0
    submit_count: int = 0
    fill_count: int = 0
    upload_count: int = 0
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    operations: List[TrackedOperation] = Field(default_factory=list)
    group_checks: List[GroupCheck] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


def _group_state_js(name: str, input_type: str) -> str:
    name_js = json.dumps(name)
    return f"""(() => {{
    const els = Array.from(document.querySelectorAll("input[type='{input_type}'][name=" + {name_js} + "]"));
    const checkedLabels = [];
    for (const el of els) {{
        if (!el.checked) continue;
        let lab = '';
        try {{
            if (el.labels && el.labels[0]) lab = (el.labels[0].innerText || '').trim();
            else if (typeof el.closest === 'function') {{ const w = el.closest('label'); if (w) lab = (w.innerText || '').trim(); }}
        }} catch (err) {{}}
        checkedLabels.push(lab);
    }}
    return JSON.stringify({{found: els.length > 0, checkedLabels}});
}})()"""


def _stopped(package_status: str, plan: PrefillPlan, reason: str,
             review_reasons: Optional[List[str]] = None) -> OrchestrationReport:
    return OrchestrationReport(
        verdict="STOPPED_NEEDS_REVIEW",
        stop_reason=reason,
        generated_at=datetime.utcnow().isoformat(),
        vacancy_stable_id=getattr(plan, "vacancy_stable_id", ""),
        package_validation_status=package_status,
        plan_status=plan.status,
        unresolved_count=len(plan.unresolved or []),
        planned_operations=len(plan.operations or []),
        review_reasons=list(review_reasons or []),
    )


def prepare_and_execute_prefill(
    package: Any,
    form: ApplicationForm,
    snapshot: Dict[str, Any],
    evaluate_fn: Callable[[str], str],
    allowed_url_markers: List[str] = ("hh.ru",),
    required_url_markers: Optional[List[str]] = None,
    stop_on_failure: bool = True,
) -> OrchestrationReport:
    """Full safe orchestration: gates -> plan -> execute -> verify -> report.

    Zero-mutation STOP unless package AND plan are both fully VALID with no
    unresolved fields. Never submits; never navigates; never clicks.
    """
    report = OrchestrationReport(
        generated_at=datetime.utcnow().isoformat(),
        vacancy_stable_id=getattr(package, "vacancy_stable_id", "") or form.vacancy_stable_id or "",
        package_validation_status=getattr(package, "validation_status", "") or "",
    )

    # --- Gate 1: package validation ---
    if report.package_validation_status != "VALID":
        reasons = list(getattr(package, "review_reasons", []) or [])
        report = _stopped(report.package_validation_status,
                          PrefillPlan(vacancy_stable_id=report.vacancy_stable_id),
                          f"Package validation_status is "
                          f"{report.package_validation_status or 'UNKNOWN'} - prefill forbidden",
                          review_reasons=reasons)
        return report

    # --- Gate 2: deterministic plan ---
    plan = build_prefill_plan(package, form, snapshot)
    report.plan_status = plan.status
    report.unresolved_count = len(plan.unresolved or [])
    report.planned_operations = len(plan.operations or [])

    if plan.status != "VALID":
        r = _stopped(report.package_validation_status, plan,
                     f"PrefillPlan status is {plan.status} - prefill forbidden",
                     review_reasons=[u.reason for u in plan.unresolved])
        report.plan_status = r.plan_status
        report.unresolved_count = r.unresolved_count
        report.planned_operations = r.planned_operations
        report.review_reasons = r.review_reasons
        report.stop_reason = r.stop_reason
        report.verdict = r.verdict
        return report

    if plan.unresolved:
        r = _stopped(report.package_validation_status, plan,
                     f"{len(plan.unresolved)} unresolved field(s) - prefill forbidden",
                     review_reasons=[u.reason for u in plan.unresolved])
        report.plan_status = r.plan_status
        report.unresolved_count = r.unresolved_count
        report.planned_operations = r.planned_operations
        report.review_reasons = r.review_reasons
        report.stop_reason = r.stop_reason
        report.verdict = r.verdict
        return report

    if not plan.operations:
        report.verdict = "NOTHING_TO_EXECUTE"
        report.stop_reason = "No validated operations in plan"
        return report

    # --- Track planned operations ---
    tracked = {
        op.question_id + "::" + op.value: TrackedOperation(
            question_id=op.question_id, question_label=op.question_label,
            op_type=op.target.type, target_name=op.target.name,
            target_label=op.target.label, value=op.value,
            status=OperationStatus.PLANNED)
        for op in plan.operations
    }
    report.operations = list(tracked.values())

    # --- Execute (atomicity handled by stop_on_failure) ---
    exec_report = execute_prefill_plan(
        plan, evaluate_fn,
        allowed_url_markers=allowed_url_markers,
        required_url_markers=required_url_markers,
        stop_on_failure=stop_on_failure)

    report.url_before = exec_report.url_before
    report.url_after = exec_report.url_after
    report.navigation_count = exec_report.navigation_count
    report.click_count = exec_report.click_count
    report.submit_count = exec_report.submit_count
    report.fill_count = exec_report.fill_count
    report.upload_count = exec_report.upload_count
    report.errors.extend(exec_report.errors)

    # Propagate fail-closed / URL-change verdicts from the executor (these are
    # not operation-level; group verification below must not override them).
    if exec_report.verdict in ("FAIL_CLOSED", "FAILED"):
        tracked_status = {m.question_id + "::" + m.value: m for m in exec_report.mutations}
        for t in report.operations:
            m = tracked_status.get(t.question_id + "::" + t.value)
            if m is None:
                continue
            if "skipped due to earlier failure" in (m.reason or ""):
                t.status = OperationStatus.SKIPPED
                t.reason = m.reason
            elif not m.ok:
                t.status = OperationStatus.FAILED
                t.reason = m.reason
        report.operations = list(tracked.values())
        report.executed_operations = sum(1 for t in report.operations if t.status in (
            OperationStatus.EXECUTED, OperationStatus.VERIFIED))
        report.verified_operations = sum(1 for t in report.operations if t.status == OperationStatus.VERIFIED)
        report.skipped_operations = sum(1 for t in report.operations if t.status == OperationStatus.SKIPPED)
        report.failed_operations = sum(1 for t in report.operations if t.status == OperationStatus.FAILED)
        report.verdict = exec_report.verdict
        report.stop_reason = exec_report.errors[0] if exec_report.errors else exec_report.verdict
        return report

    # Map mutation results onto tracked operations.
    verified_by_key: Dict[str, bool] = {}
    verify_map: Dict[str, Dict[str, Any]] = {}
    for v in exec_report.verification:
        verify_map[v.get("question_id", "") + "::" + str(v.get("value", ""))] = v

    for mut in exec_report.mutations:
        key = mut.question_id + "::" + mut.value
        t = tracked.get(key)
        if t is None:
            continue
        ver = verify_map.get(key, {})
        if not mut.ok:
            t.status = OperationStatus.FAILED
            t.reason = mut.reason
        elif ver.get("ok"):
            t.status = OperationStatus.VERIFIED
        elif "verify failed" in str(ver.get("reason", "")) or "not found in DOM" in str(ver.get("reason", "")):
            # verification could not be performed (uncertain), mutation was applied
            t.status = OperationStatus.EXECUTED
            t.reason = str(ver.get("reason", ""))
        else:
            t.status = OperationStatus.FAILED
            t.reason = str(ver.get("reason", "")) or "verification mismatch"
        verified_by_key[key] = bool(ver.get("ok"))

    # Skipped operations (atomicity).
    for mut in exec_report.mutations:
        key = mut.question_id + "::" + mut.value
        t = tracked.get(key)
        if t is not None and "skipped due to earlier failure" in (mut.reason or ""):
            t.status = OperationStatus.SKIPPED

    report.operations = list(tracked.values())
    report.executed_operations = sum(1 for t in report.operations if t.status in (
        OperationStatus.EXECUTED, OperationStatus.VERIFIED))
    report.verified_operations = sum(1 for t in report.operations if t.status == OperationStatus.VERIFIED)
    report.skipped_operations = sum(1 for t in report.operations if t.status == OperationStatus.SKIPPED)
    report.failed_operations = sum(1 for t in report.operations if t.status == OperationStatus.FAILED)

    # --- Group verification (RADIO exactly-one, CHECKBOX set equality) ---
    answers_by_qid = {a.question_id: a for a in (getattr(package, "answers", []) or [])}
    seen_groups: set = set()
    for q in (form.questions or []):
        if q.normalized_type not in (QuestionType.RADIO, QuestionType.CHECKBOX):
            continue
        group_name = q.id.replace("hh__ctrl_", "")
        if group_name in seen_groups:
            continue
        seen_groups.add(group_name)
        input_type = "radio" if q.normalized_type == QuestionType.RADIO else "checkbox"

        ans = answers_by_qid.get(q.id)
        expected: List[str] = []
        if ans is not None and getattr(ans, "answer", None):
            expected = [p.strip() for p in str(ans.answer).split(";") if p.strip()]

        actual: List[str] = []
        try:
            raw = evaluate_fn(_group_state_js(group_name, input_type))
            state = json.loads(raw)
            actual = [l for l in (state.get("checkedLabels") or []) if l]
        except Exception as e:
            report.errors.append(f"group read failed for {group_name}: {e}")
            actual = ["<unreadable>"]

        ok = sorted(actual) == sorted(expected)
        report.group_checks.append(GroupCheck(
            group_name=group_name, input_type=input_type,
            expected_checked=expected, actual_checked=actual, ok=ok))

    # --- Verdict ---
    if any(not gc.ok for gc in report.group_checks):
        report.verdict = "FAILED"
        report.stop_reason = "group verification mismatch"
    elif report.failed_operations > 0 or report.skipped_operations > 0:
        report.verdict = "FAILED"
        report.stop_reason = f"{report.failed_operations} failed / {report.skipped_operations} skipped operation(s)"
    elif (report.executed_operations == report.planned_operations
          and report.verified_operations == report.planned_operations
          and all(gc.ok for gc in report.group_checks)):
        report.verdict = "VERIFIED"
        report.stop_reason = "all planned operations executed and verified"
    else:
        report.verdict = "FAILED"
        report.stop_reason = "incomplete execution/verification"

    return report