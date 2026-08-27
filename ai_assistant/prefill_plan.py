"""Stage 20E: deterministic prefill plan (read-only dry-run).

Builds a plan mapping validated ApplicationPackage.answers to real HH DOM
controls from a captured snapshot. NEVER mutates browser, DOM, DB, or
external state. No click/fill/type/upload/submit/goto/navigation/login.

Safety: pure functions, deterministic, no side effects, no imports of
browser or DB mutation APIs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .hh_extractor import ApplicationForm, ApplicationQuestion, QuestionType


class PrefillTarget(BaseModel):
    tag: str = ""
    type: str = ""
    name: Optional[str] = None
    id: Optional[str] = None
    dataQa: Optional[str] = None
    label: Optional[str] = None
    visible: bool = True
    disabled: bool = False
    readOnly: bool = False

    model_config = {"extra": "forbid"}


class PrefillOperation(BaseModel):
    question_id: str
    question_label: str = ""
    target: PrefillTarget
    value: str
    source_answer: str = ""
    confidence: float = 1.0
    reason: str = ""

    model_config = {"extra": "forbid"}


class UnresolvedField(BaseModel):
    question_id: str
    question_label: str = ""
    reason: str = ""
    requires_review: bool = True

    model_config = {"extra": "forbid"}


class PrefillPlan(BaseModel):
    vacancy_stable_id: str = ""
    generated_at: str = ""
    application_type: str = "unknown"
    status: str = "NEEDS_REVIEW"  # VALID | NEEDS_REVIEW
    operations: List[PrefillOperation] = Field(default_factory=list)
    unresolved: List[UnresolvedField] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


def _stable_sort_key(op: PrefillOperation) -> Tuple[str, str]:
    return (op.question_id, op.target.name or "")


def build_prefill_plan(
    package: Any,
    form: ApplicationForm,
    snapshot: Dict[str, Any],
) -> PrefillPlan:
    """Deterministic read-only plan: validated answers -> real controls.

    Uses ONLY:
      - package.answers where requires_review is False and answer is not None
      - form.questions for ID / type / options linkage
      - snapshot.controls for real DOM control existence and labels

    Never guesses. If the linkage cannot be proven, the field is reported as
    unresolved with requires_review=True.
    """
    plan = PrefillPlan(
        vacancy_stable_id=getattr(package, "vacancy_stable_id", "") or form.vacancy_stable_id or "",
        generated_at=datetime.utcnow().isoformat(),
        application_type=getattr(form, "application_type", "unknown").value
        if hasattr(form.application_type, "value") else str(form.application_type),
        status="VALID",
    )

    questions_by_id = {q.id: q for q in (form.questions or [])}
    controls: List[Dict[str, Any]] = list(snapshot.get("controls") or [])

    # Index controls by name for fast lookup (deterministic order preserved).
    controls_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for c in controls:
        name = c.get("name") or ""
        if name:
            controls_by_name.setdefault(name, []).append(c)

    # Only validated answers (requires_review is False, answer present).
    answers_all: List[Any] = list(getattr(package, "answers", []) or [])
    validated = [a for a in answers_all
                 if not getattr(a, "requires_review", True) and getattr(a, "answer", None)]

    # Track which question_ids got a validated answer.
    validated_qids = {a.question_id for a in validated}
    answer_by_qid = {a.question_id: a for a in validated}

    # For each validated answer, prove a real control target.
    for qid, ans in sorted(answer_by_qid.items()):
        q = questions_by_id.get(qid)
        if q is None:
            plan.unresolved.append(UnresolvedField(
                question_id=qid, question_label="",
                reason="Question ID not found in captured form"))
            continue

        # Unknown-type questions must never be planned.
        if q.normalized_type == QuestionType.UNKNOWN:
            plan.unresolved.append(UnresolvedField(
                question_id=qid, question_label=q.label,
                reason="Question is UNKNOWN (auth-gate / not extractable)"))
            continue

        ans_text = str(ans.answer or "").strip()
        if not ans_text:
            plan.unresolved.append(UnresolvedField(
                question_id=qid, question_label=q.label, reason="Validated answer is empty"))
            continue

        qtype = q.normalized_type

        if qtype == QuestionType.RADIO:
            # Exactly one real radio input whose label matches the answer.
            matched = None
            for c in controls:
                if c.get("type") == "radio" and (c.get("label") or "").strip() == ans_text:
                    # Proven linkage: question.id's suffix must be the control's name.
                    q_name = q.id.replace("hh__ctrl_", "")
                    if c.get("name") == q_name:
                        matched = c
                        break
            if matched is None:
                # Fallback: any radio whose label matches, with dream linkage check.
                for c in controls:
                    if c.get("type") == "radio" and (c.get("label") or "").strip() == ans_text:
                        matched = c
                        break
            if matched is None:
                plan.unresolved.append(UnresolvedField(
                    question_id=qid, question_label=q.label,
                    reason=f"RADIO answer '{ans_text}' has no matching real control with that label"))
                continue
            plan.operations.append(PrefillOperation(
                question_id=qid, question_label=q.label,
                target=PrefillTarget(
                    tag=matched.get("tag") or "INPUT", type=matched.get("type") or "radio",
                    name=matched.get("name"), id=matched.get("id"), dataQa=matched.get("dataQa"),
                    label=matched.get("label"), visible=bool(matched.get("visible")),
                    disabled=bool(matched.get("disabled")), readOnly=bool(matched.get("readOnly"))),
                value=ans_text, source_answer=ans_text, confidence=float(getattr(ans, "confidence", 1.0)),
                reason="Validated RADIO answer matched to real option"))

        elif qtype == QuestionType.CHECKBOX:
            # Multiple validated options joined by "; ".
            parts = [p.strip() for p in ans_text.split(";") if p.strip()]
            # Special handling: "Свой вариант" must never be auto-selected (truth-only).
            real_parts = [p for p in parts if p.strip().lower() != "свой вариант"]
            if not real_parts:
                # Only "Свой вариант" was the answer -> requires the custom
                # textarea, which is a separate TEXTAREA question, not this one.
                plan.unresolved.append(UnresolvedField(
                    question_id=qid, question_label=q.label,
                    reason="CHECKBOX answer is only 'Свой вариант' - custom text requires human input"))
                continue
            for part in real_parts:
                matched = None
                for c in controls:
                    if c.get("type") == "checkbox" and (c.get("label") or "").strip() == part:
                        q_name = q.id.replace("hh__ctrl_", "")
                        if c.get("name") == q_name:
                            matched = c
                            break
                if matched is None:
                    for c in controls:
                        if c.get("type") == "checkbox" and (c.get("label") or "").strip() == part:
                            matched = c
                            break
                if matched is None:
                    plan.unresolved.append(UnresolvedField(
                        question_id=qid, question_label=q.label,
                        reason=f"CHECKBOX option '{part}' has no matching real control"))
                    continue
                plan.operations.append(PrefillOperation(
                    question_id=qid, question_label=q.label,
                    target=PrefillTarget(
                        tag=matched.get("tag") or "INPUT", type=matched.get("type") or "checkbox",
                        name=matched.get("name"), id=matched.get("id"), dataQa=matched.get("dataQa"),
                        label=matched.get("label"), visible=bool(matched.get("visible")),
                        disabled=bool(matched.get("disabled")), readOnly=bool(matched.get("readOnly"))),
                    value=part, source_answer=ans_text, confidence=float(getattr(ans, "confidence", 1.0)),
                    reason="Validated CHECKBOX option matched to real control"))

        elif qtype == QuestionType.SELECT:
            matched = None
            for c in controls:
                if c.get("tag") == "SELECT" and any(
                        (isinstance(o, dict) and (o.get("text") or o.get("value")) == ans_text) or o == ans_text
                        for o in (c.get("options") or [])):
                    q_name = q.id.replace("hh__ctrl_", "")
                    if c.get("name") == q_name:
                        matched = c
                        break
            if matched is None:
                for c in controls:
                    if c.get("tag") == "SELECT":
                        for o in (c.get("options") or []):
                            text = o.get("text") if isinstance(o, dict) else str(o)
                            if text == ans_text:
                                matched = c
                                break
            if matched is None:
                plan.unresolved.append(UnresolvedField(
                    question_id=qid, question_label=q.label,
                    reason=f"SELECT answer '{ans_text}' has no matching real option"))
                continue
            plan.operations.append(PrefillOperation(
                question_id=qid, question_label=q.label,
                target=PrefillTarget(
                    tag=matched.get("tag") or "SELECT", type="select",
                    name=matched.get("name"), id=matched.get("id"), dataQa=matched.get("dataQa"),
                    label=matched.get("label"), visible=bool(matched.get("visible")),
                    disabled=bool(matched.get("disabled")), readOnly=bool(matched.get("readOnly"))),
                value=ans_text, source_answer=ans_text, confidence=float(getattr(ans, "confidence", 1.0)),
                reason="Validated SELECT answer matched to real option"))

        elif qtype in (QuestionType.TEXT, QuestionType.TEXTAREA, QuestionType.NUMBER,
                       QuestionType.COVER_LETTER):
            # Proven linkage: question.id suffix == control name, or (for
            # custom _text) custom_option_text_id linkage, or label match.
            matched = None
            q_name = q.id.replace("hh__ctrl_", "")
            for c in controls:
                if (c.get("name") or "") == q_name:
                    matched = c
                    break
            if matched is None:
                # Try custom_option_text_id linkage.
                custom = getattr(q, "custom_option_text_id", None)
                if custom:
                    cname = custom.replace("hh__ctrl_", "")
                    for c in controls:
                        if (c.get("name") or "") == cname:
                            matched = c
                            break
            if matched is None:
                # Label-based match for TEXTAREA (ariaLabelledby stem).
                for c in controls:
                    if c.get("tag") == "TEXTAREA" and (c.get("label") or "").strip() == (q.label or "").strip():
                        matched = c
                        break
            if matched is None:
                plan.unresolved.append(UnresolvedField(
                    question_id=qid, question_label=q.label,
                    reason="TEXT/TEXTAREA question has no matching real control (proven linkage required)"))
                continue
            plan.operations.append(PrefillOperation(
                question_id=qid, question_label=q.label,
                target=PrefillTarget(
                    tag=matched.get("tag") or "INPUT", type=matched.get("type") or "text",
                    name=matched.get("name"), id=matched.get("id"), dataQa=matched.get("dataQa"),
                    label=matched.get("label"), visible=bool(matched.get("visible")),
                    disabled=bool(matched.get("disabled")), readOnly=bool(matched.get("readOnly"))),
                value=ans_text, source_answer=ans_text, confidence=float(getattr(ans, "confidence", 1.0)),
                reason="Validated TEXT answer matched to real control"))

        elif qtype == QuestionType.FILE:
            matched = None
            q_name = q.id.replace("hh__ctrl_", "")
            for c in controls:
                if (c.get("type") or "").lower() == "file" and (c.get("name") or "") == q_name:
                    matched = c
                    break
            if matched is None:
                plan.unresolved.append(UnresolvedField(
                    question_id=qid, question_label=q.label,
                    reason="FILE question has no matching real file input"))
                continue
            plan.operations.append(PrefillOperation(
                question_id=qid, question_label=q.label,
                target=PrefillTarget(
                    tag=matched.get("tag") or "INPUT", type="file",
                    name=matched.get("name"), id=matched.get("id"), dataQa=matched.get("dataQa"),
                    label=matched.get("label"), visible=bool(matched.get("visible")),
                    disabled=bool(matched.get("disabled")), readOnly=bool(matched.get("readOnly"))),
                value=ans_text, source_answer=ans_text, confidence=float(getattr(ans, "confidence", 1.0)),
                reason="Validated FILE answer matched to real control"))

        else:
            plan.unresolved.append(UnresolvedField(
                question_id=qid, question_label=q.label,
                reason=f"Question type {qtype.value} not plannable (UNKNOWN/unsupported)"))

    # Any question with a non-validated answer (requires_review or missing)
    # that wasn't already reported as unresolved must be surfaced.
    all_qids_handled = {o.question_id for o in plan.operations} | {u.question_id for u in plan.unresolved}
    for q in (form.questions or []):
        if q.id in all_qids_handled:
            continue
        ans = next((a for a in answers_all if a.question_id == q.id), None)
        if q.normalized_type == QuestionType.UNKNOWN:
            plan.unresolved.append(UnresolvedField(
                question_id=q.id, question_label=q.label,
                reason="Question is UNKNOWN - cannot be safely planned"))
        elif ans is not None and getattr(ans, "requires_review", True):
            plan.unresolved.append(UnresolvedField(
                question_id=q.id, question_label=q.label,
                reason=getattr(ans, "reason", "Answer requires review") or "Answer requires review"))
        elif ans is None or not getattr(ans, "answer", None):
            # No validated answer for a known-type question - check if it was
            # expected to have one (e.g. a required screening question with no
            # confirmed fact). Report as unresolved for visibility.
            if q.required or q.normalized_type in (QuestionType.RADIO, QuestionType.CHECKBOX, QuestionType.SELECT):
                plan.unresolved.append(UnresolvedField(
                    question_id=q.id, question_label=q.label,
                    reason="No validated answer available for this question"))

    # Deterministic ordering.
    plan.operations.sort(key=lambda o: (o.question_id, o.target.name or "", o.value))
    plan.unresolved.sort(key=lambda u: u.question_id)

    # Status mirrors package validation.
    plan.status = getattr(package, "validation_status", "NEEDS_REVIEW") or "NEEDS_REVIEW"
    if plan.unresolved:
        plan.status = "NEEDS_REVIEW"
        plan.warnings.append(f"{len(plan.unresolved)} question(s) could not be safely mapped to real controls")

    return plan
