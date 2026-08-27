"""Stage 21: dual-mode controlled auto-apply orchestration for HH.

Wraps the EXISTING Stage 20E-20K pipeline (PrefillPlan / prefill_execute /
prefill_orchestrate / review gate / controlled submit). Nothing here
duplicates or weakens those components; this module only decides WHETHER and
HOW FAR an already-built ApplicationPackage may proceed for one vacancy.

Modes:
    REVIEW_MODE (default)
        simple_response        -> all gates + auto submit (no judgment needed)
        cover_letter_only      -> truth-only letter prefill + gates + auto submit
        screening_questions    -> safe prefill + read-only verification,
                                  then STOP before submit -> HumanReviewGate
                                  (READY_FOR_HUMAN_REVIEW is surfaced, never
                                  approved by the machine in this mode)
    AUTO_APPLY_MODE (opt-in; never default)
        same as REVIEW_MODE except screening_questions MAY auto-submit, but
        ONLY when every gate holds simultaneously:
            package VALID, plan VALID, unresolved == [], orchestration VERIFIED,
            failed == 0, skipped == 0, fingerprint/URL/vacancy match,
            review state not stale, answers proven truth-only (no UNKNOWN /
            review / required=None / custom-text without proven value).
        Any mismatch -> STOP without retry.

Hard rules preserved from Stage 20/21:
    - React-safe prefill only (native checked setter + click/change events);
      never a bare ``el.checked = true``.
    - exactly ONE submit attempt per vacancy per session (duplicate block);
    - no retry after SUBMISSION_UNKNOWN;
    - no navigation, no login, no cookies/storage, no DB writes;
    - forbidden modules untouched.

Cover-letter rule (Stage 21 addendum):
    A letter textarea does NOT imply a required letter. Requiredness must be
    DOM-proven (requiredAttr/ariaRequired on the control). If the submit
    button is enabled on an otherwise-empty form, the form sends without a
    letter -> treated as simple_response, nothing generated or prefilled.
    If requiredness is UNKNOWN (button blocked, no marker) -> never
    auto-generate: REVIEW_MODE hands it to the human, AUTO_APPLY_MODE stops.

Mode switch: pass ``mode=`` explicitly, or set env HH_APPLY_MODE=AUTO.
Default is always REVIEW_MODE.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from .hh_extractor import QuestionType
from .prefill_plan import PrefillPlan, build_prefill_plan
from .prefill_orchestrate import (
    OperationStatus,
    OrchestrationReport,
    TrackedOperation,
    prepare_and_execute_prefill,
)
from .application_review_gate import (
    GateStatus,
    HumanReviewGate,
    HumanReviewStore,
    build_review_gate,
)
from .hh_human_submission import confirm_human_submission
from .hh_controlled_submit import controlled_real_submit


class ApplyMode(str, Enum):
    REVIEW = "REVIEW_MODE"
    AUTO = "AUTO_APPLY_MODE"


DEFAULT_MODE = ApplyMode.REVIEW

# Env switch (opt-in): HH_APPLY_MODE=AUTO enables AUTO_APPLY_MODE.
_ENV_MODE_VAR = "HH_APPLY_MODE"
_AUTO_ALIASES = {"AUTO", "AUTO_APPLY", "AUTO_APPLY_MODE"}


def resolve_mode(explicit: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> ApplyMode:
    """Resolve the apply mode. Anything unrecognized falls back to REVIEW."""
    raw = (explicit or (env or os.environ).get(_ENV_MODE_VAR, "") or "").strip().upper()
    return ApplyMode.AUTO if raw in _AUTO_ALIASES else ApplyMode.REVIEW


class FormKind(str, Enum):
    SIMPLE = "simple_response"
    COVER_LETTER_ONLY = "cover_letter_only"
    QUESTIONNAIRE = "screening_questions"


_TASK_ID_RE = re.compile(r"^task_\d+(_text)?$")

# Verdicts surfaced by run_auto_apply (submit verdicts pass through verbatim).
V_SUBMITTED = "SUBMITTED"
V_UNKNOWN = "SUBMISSION_UNKNOWN"
V_NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
V_DUPLICATE = "BLOCKED_DUPLICATE"
V_INVALID_PACKAGE = "BLOCKED_INVALID_PACKAGE"
V_UNRESOLVED = "BLOCKED_UNRESOLVED"
V_REQUIRED_UNKNOWN = "BLOCKED_REQUIRED_UNKNOWN"
V_PREFILL_FAILED = "PREFILL_FAILED"
V_INTERNAL = "BLOCKED_INTERNAL"

# Vacancies with ANY submit attempt this session (one attempt per vacancy,
# duplicates are blocked; SUBMISSION_UNKNOWN is never retried).
_attempted_vacancies: Set[str] = set()


def clear_session_state() -> None:
    _attempted_vacancies.clear()


def _letter_required_state(snapshot: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Prove cover-letter requiredness from the captured DOM. No guessing.

    Returns:
        True  - letter control carries an explicit required attribute/flag;
        False - form is submittable without it (submit button enabled on an
                otherwise-empty form), so the letter is optional;
        None  - UNKNOWN (button blocked with empty form but no required
                marker on the letter): cannot prove -> never auto-generate.
    """
    controls = list((snapshot or {}).get("controls") or [])
    letter = None
    for c in controls:
        tag = (c.get("tag") or "").upper()
        name = (c.get("name") or "").strip()
        qa = (c.get("dataQa") or c.get("data-qa") or "").lower()
        if tag == "TEXTAREA" and ("letter" in qa or "letter" in name.lower()):
            letter = c
            break
    if letter is None:
        return False  # nothing letter-like to require

    # Tri-state required proof (same contract as hh_extractor):
    # requiredAttr when present, else legacy boolean 'required'.
    ra = letter["requiredAttr"] if "requiredAttr" in letter else letter.get("required")
    if ra is True or (str(letter.get("ariaRequired") or "").lower() == "true"):
        return True

    buttons = list((snapshot or {}).get("buttons") or [])
    btn = next((b for b in buttons
                if "vacancy-response-submit-popup" in (b.get("dataQa") or "")), None)
    if btn is not None and btn.get("disabled"):
        return None  # blocked empty form - cause unproven -> UNKNOWN
    return False


def _strip_letter_answers(package: Any) -> None:
    """Drop in-memory COVER_LETTER answers so the plan can never target a
    non-required letter (nothing generated, nothing prefilled)."""
    answers = list(getattr(package, "answers", []) or [])
    package.answers = [a for a in answers
                       if getattr(a, "answer_type", None) is not QuestionType.COVER_LETTER]


def classify_form(form: Any, snapshot: Optional[Dict[str, Any]] = None) -> FormKind:
    """DOM-provable form classification (no guessing).

    Priority:
      1. any control/question bound to an HH screening id (task_<digits>) or
         any UNKNOWN-type question            -> QUESTIONNAIRE
      2. letter textarea present (dataQa/name contains 'letter', or a
         non-task TEXTAREA question exists)   -> COVER_LETTER_ONLY
      3. otherwise                            -> SIMPLE
    Nameless checkbox toggles (HH's standard employer-info panel on response
    popups) do NOT make a form a questionnaire.
    """
    questions = list(getattr(form, "questions", []) or [])

    def _is_task(qid: str) -> bool:
        return bool(_TASK_ID_RE.match((qid or "").replace("hh__ctrl_", "")))

    for q in questions:
        if q.normalized_type == QuestionType.UNKNOWN or _is_task(q.id):
            return FormKind.QUESTIONNAIRE

    controls = list((snapshot or {}).get("controls") or [])
    has_letter_control = False
    has_other_input = False
    for c in controls:
        tag = (c.get("tag") or "").upper()
        if tag not in ("INPUT", "TEXTAREA", "SELECT"):
            continue
        name = (c.get("name") or "").strip()
        qa = (c.get("dataQa") or c.get("data-qa") or "").lower()
        if name and _TASK_ID_RE.match(name):
            return FormKind.QUESTIONNAIRE
        ctype = (c.get("type") or "").lower()
        if tag == "TEXTAREA" or ctype in ("text", "email", "tel", "number") or tag == "SELECT":
            if "letter" in qa or "letter" in name.lower():
                has_letter_control = True
            else:
                has_other_input = True
    if questions and not has_letter_control:
        # Non-task, non-letter questions survived extraction: treat as
        # questionnaire so a human reviews them (conservative).
        if any(q.normalized_type != QuestionType.COVER_LETTER for q in questions):
            return FormKind.QUESTIONNAIRE
    if has_letter_control or any(
            q.normalized_type == QuestionType.COVER_LETTER for q in questions):
        return FormKind.COVER_LETTER_ONLY
    if has_other_input:
        return FormKind.QUESTIONNAIRE
    return FormKind.SIMPLE


class AutoApplyReport(BaseModel):
    mode: str = DEFAULT_MODE.value
    form_kind: str = ""
    vacancy_stable_id: str = ""
    url: Optional[str] = None
    title: Optional[str] = None
    verdict: str = V_INTERNAL
    stop_reason: str = ""
    generated_at: str = ""
    # human-review surface (Stage 21 §6)
    review_id: str = ""
    fingerprint: str = ""
    review_status: str = ""
    cover_letter_len: int = 0
    cover_letter_required: Optional[bool] = None
    answers: List[Dict[str, str]] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    # execution surface
    planned_operations: int = 0
    executed_operations: int = 0
    verified_operations: int = 0
    failed_operations: int = 0
    skipped_operations: int = 0
    fill_count: int = 0
    click_count: int = 0
    submit_count: int = 0
    successful_submit: int = 0
    navigation_count: int = 0
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    submit_report: Optional[Dict[str, Any]] = None
    approved_by: str = ""  # "" | "policy:<form_kind>:<mode>"

    model_config = {"extra": "forbid"}


def _answers_summary(pkg: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for a in sorted(getattr(pkg, "answers", []) or [], key=lambda x: x.question_id):
        if getattr(a, "requires_review", True) or not getattr(a, "answer", None):
            continue
        out.append({"question_id": a.question_id, "value": str(a.answer)})
    return out


def _unresolved_reasons(plan: PrefillPlan) -> List[str]:
    return [u.reason for u in (plan.unresolved or [])]


def _zero_op_orchestration(vacancy_stable_id: str, reason: str) -> OrchestrationReport:
    """Honest zero-operation report: nothing prefillable on this form kind."""
    return OrchestrationReport(
        verdict="VERIFIED",
        stop_reason=reason,
        generated_at=datetime.utcnow().isoformat(),
        vacancy_stable_id=vacancy_stable_id,
        package_validation_status="VALID",
        plan_status="VALID",
        planned_operations=0,
        executed_operations=0,
        verified_operations=0,
        skipped_operations=0,
        failed_operations=0,
    )


def run_auto_apply(
    package: Any,
    evaluate_fn: Callable[[str], str],
    snapshot: Optional[Dict[str, Any]] = None,
    *,
    form: Any = None,
    mode: Optional[ApplyMode] = None,
    expected_url_markers: Tuple[str, ...] = ("hh.ru", "applicant/vacancy_response"),
    submitted_vacancies: Optional[Iterable[str]] = None,
) -> AutoApplyReport:
    """Run one vacancy through the dual-mode pipeline. At most ONE submit.

    Reuses: build_prefill_plan -> prepare_and_execute_prefill (React-safe) ->
    build_review_gate/HumanReviewStore -> confirm_human_submission ->
    controlled_real_submit (single click, read-only verification, one-shot).
    """
    mode = mode or DEFAULT_MODE
    effective_form = form if form is not None else getattr(package, "form", None)
    vacancy_stable_id = getattr(package, "vacancy_stable_id", "") or ""

    report = AutoApplyReport(mode=mode.value, vacancy_stable_id=vacancy_stable_id,
                             generated_at=datetime.utcnow().isoformat())
    meta = getattr(effective_form, "extraction_meta", {}) or {}
    report.url = meta.get("url")
    report.title = meta.get("title")
    report.cover_letter_len = len(getattr(package, "cover_letter", "") or "")
    report.answers = _answers_summary(package)

    # --- Gate: duplicate vacancy (already attempted/submitted anywhere) ---
    external = {v for v in (submitted_vacancies or set()) if v}
    if vacancy_stable_id in _attempted_vacancies or vacancy_stable_id in external:
        report.verdict = V_DUPLICATE
        report.stop_reason = ("vacancy already processed/submitted - duplicates "
                              "are never re-handled")
        return report

    if effective_form is None:
        report.verdict = V_INTERNAL
        report.stop_reason = "no ApplicationForm available for classification"
        return report

    kind = classify_form(effective_form, snapshot)
    report.form_kind = kind.value
    snapshot = snapshot if snapshot is not None else {}

    # --- Cover-letter rule (Stage 21 addendum) ---
    # A letter textarea alone proves nothing. Fill/generate ONLY on DOM proof
    # of requiredness; optional letter -> plain simple_response path; UNKNOWN
    # -> never auto-generate (REVIEW stops for the human, AUTO hard-stops).
    report.cover_letter_required = None
    if kind is FormKind.COVER_LETTER_ONLY:
        lreq = _letter_required_state(snapshot)
        report.cover_letter_required = lreq
        if lreq is False:
            _strip_letter_answers(package)
            # Downgrade to SIMPLE honestly: drop the letter question from the
            # in-memory form so the plan cannot target it as required.
            qs = [q for q in (getattr(effective_form, "questions", []) or [])
                  if getattr(q, "normalized_type", None) is not QuestionType.COVER_LETTER]
            try:
                effective_form = effective_form.model_copy(update={"questions": qs})
            except Exception:
                pass
            report.answers = _answers_summary(package)
            kind = FormKind.SIMPLE
            report.form_kind = kind.value
        elif lreq is None:
            if mode is ApplyMode.AUTO:
                report.verdict = V_REQUIRED_UNKNOWN
                report.stop_reason = ("cover letter requiredness UNKNOWN "
                                      "(submit blocked on empty form, no required "
                                      "marker) - AUTO mode never auto-generates")
                return report
            report.verdict = V_NEEDS_HUMAN_REVIEW
            report.stop_reason = ("cover letter requiredness UNKNOWN - stopped; "
                                  "human decides whether to attach a letter")
            report.review_status = "READY_FOR_HUMAN_REVIEW"
            return report

    # --- Gate: package validation ---
    pkg_status = getattr(package, "validation_status", "") or ""
    if pkg_status != "VALID":
        report.verdict = V_INVALID_PACKAGE
        report.stop_reason = f"package.validation_status is {pkg_status or 'UNKNOWN'} (must be VALID)"
        report.review_reasons = list(getattr(package, "review_reasons", []) or [])
        return report

    # --- Gate: answer purity (truth-only; never invent) ---
    for q in (getattr(effective_form, "questions", []) or []):
        if getattr(q, "required", False) is None and kind is FormKind.QUESTIONNAIRE:
            if mode is ApplyMode.AUTO:
                report.verdict = V_REQUIRED_UNKNOWN
                report.stop_reason = (f"required status unknown for '{q.label or q.id}' "
                                      "- AUTO_APPLY_MODE forbids proceeding")
                return report
    for a in (getattr(package, "answers", []) or []):
        if getattr(a, "requires_review", True):
            if mode is ApplyMode.AUTO and kind is FormKind.QUESTIONNAIRE:
                report.verdict = V_UNRESOLVED
                report.stop_reason = f"answer for '{a.question_id}' requires review - AUTO mode forbids submit"
                return report

    # --- Deterministic plan ---
    plan = build_prefill_plan(package, effective_form, snapshot)
    if plan.status != "VALID" or plan.unresolved:
        report.unresolved = _unresolved_reasons(plan)
        if kind is FormKind.QUESTIONNAIRE and mode is ApplyMode.REVIEW:
            # Stop BEFORE submit; hand everything to the human (§ REVIEW 3).
            gate = build_review_gate(package, plan, _zero_op_orchestration(
                vacancy_stable_id, "plan unresolved - nothing prefilled"),
                {"controls": snapshot.get("controls") or []}, effective_form)
            store = HumanReviewStore()
            rid = store.save(gate)
            report.verdict = V_NEEDS_HUMAN_REVIEW
            report.stop_reason = "questionnaire has unresolved fields - stopped before submit for human review"
            report.review_id = gate.review_id
            report.fingerprint = gate.fingerprint
            report.review_status = GateStatus.READY_FOR_HUMAN_REVIEW.value
            report.review_reasons = report.unresolved
            return report
        report.verdict = V_UNRESOLVED
        report.stop_reason = (f"plan.status={plan.status}, "
                              f"{len(plan.unresolved)} unresolved field(s)")
        return report

    # --- Safe prefill (React-safe executor inside) ---
    if plan.operations:
        orch = prepare_and_execute_prefill(
            package, effective_form, snapshot, evaluate_fn,
            allowed_url_markers=[m for m in expected_url_markers[:1]],
            required_url_markers=list(expected_url_markers[1:]) or None,
            stop_on_failure=True)
        if orch.verdict != "VERIFIED":
            report.verdict = V_PREFILL_FAILED
            report.stop_reason = f"prefill orchestration verdict={orch.verdict}: {orch.stop_reason}"
            report.planned_operations = orch.planned_operations
            report.executed_operations = orch.executed_operations
            report.verified_operations = orch.verified_operations
            report.failed_operations = orch.failed_operations
            report.skipped_operations = orch.skipped_operations
            report.url_before = orch.url_before
            report.url_after = orch.url_after
            return report
    else:
        orch = _zero_op_orchestration(
            vacancy_stable_id, f"{kind.value}: nothing to prefill (honest zero-op report)")

    report.planned_operations = orch.planned_operations
    report.executed_operations = orch.executed_operations
    report.verified_operations = orch.verified_operations
    report.failed_operations = orch.failed_operations
    report.skipped_operations = orch.skipped_operations
    report.fill_count = orch.fill_count
    report.url_before = orch.url_before
    report.url_after = orch.url_after

    # --- Human Review Gate ---
    gate = build_review_gate(package, plan, orch,
                             {"controls": snapshot.get("controls") or []},
                             effective_form)
    report.review_id = gate.review_id
    report.fingerprint = gate.fingerprint
    if gate.status != GateStatus.READY_FOR_HUMAN_REVIEW:
        report.review_status = gate.status.value
        report.review_reasons = list(gate.block_reasons or [])
        if kind is FormKind.QUESTIONNAIRE and mode is ApplyMode.REVIEW:
            report.verdict = V_NEEDS_HUMAN_REVIEW
            report.stop_reason = "review gate blocked - surfaced to human"
            return report
        report.verdict = V_INTERNAL
        report.stop_reason = f"review gate blocked: {gate.block_reasons}"
        return report

    # --- Mode decision at the submit boundary ---
    needs_human = (kind is FormKind.QUESTIONNAIRE and mode is ApplyMode.REVIEW)
    if needs_human:
        store = HumanReviewStore()
        rid = store.save(gate)
        report.verdict = V_NEEDS_HUMAN_REVIEW
        report.stop_reason = ("screening questionnaire - stopped before submit; "
                              "answers handed to human review")
        report.review_id = gate.review_id
        report.review_status = GateStatus.READY_FOR_HUMAN_REVIEW.value
        return report

    # AUTO questionnaire extra gates were all enforced above (VALID/plan/
    # unresolved/orch/purity/required). Approve per policy, then reuse the
    # full gated single-click submission unchanged.
    store = HumanReviewStore()
    rid = store.save(gate)
    report.approved_by = f"policy:{kind.value}:{mode.value}"
    store.mark_waiting_for_human(rid)
    appr = store.approve_review(rid, gate.fingerprint)
    if not appr.get("ok"):
        report.verdict = V_INTERNAL
        report.stop_reason = f"policy approval failed: {appr.get('reason')}"
        report.review_status = appr.get("state") or ""
        return report
    conf = confirm_human_submission(store, rid, gate.fingerprint, vacancy_stable_id)
    if not conf.get("ok"):
        report.verdict = V_INTERNAL
        report.stop_reason = f"confirmation gate failed: {conf.get('reason')}"
        return report

    sub = controlled_real_submit(store, rid, gate.fingerprint, package, plan,
                                 orch, evaluate_fn,
                                 expected_url_markers=expected_url_markers)

    # One attempt per vacancy, regardless of outcome (never retry UNKNOWN).
    _attempted_vacancies.add(vacancy_stable_id)

    report.click_count = sub.click_count
    report.submit_count = sub.submit_count
    report.successful_submit = sub.successful_submit
    report.navigation_count = sub.navigation_count
    report.url_after = sub.url_after or report.url_after
    report.submit_report = sub.model_dump()
    report.verdict = sub.verdict
    report.stop_reason = sub.stop_reason
    report.review_status = GateStatus.HUMAN_APPROVED.value
    return report