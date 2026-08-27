"""Stage 20H: Human Review Gate.

Pure/read-only component. After Stage 20G produces VERIFIED, this module
builds a HumanReviewGate showing the exact future-application payload and
manages an explicit, one-time human approval state machine.

States:
    READY_FOR_HUMAN_REVIEW -> WAITING_FOR_HUMAN_APPROVAL -> HUMAN_APPROVED
    any state -> INVALIDATED (when reviewed state changes)

HARD RULES:
- This module contains NO browser APIs: no click, no fill, no goto, no
  sub\u2010mit, no key\u2010board, no upload, no navigation, no DB access.
- approve_review() only transitions an in-memory state machine.
- Fingerprint (SHA-256 of the canonical review payload) invalidates the
  approval whenever package/answers/cover letter/vacancy/DOM change.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .hh_extractor import ApplicationForm, ApplicationType, QuestionType
from .prefill_plan import PrefillPlan
from .prefill_orchestrate import OrchestrationReport


class GateStatus(str, Enum):
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    BLOCKED = "BLOCKED"
    WAITING_FOR_HUMAN_APPROVAL = "WAITING_FOR_HUMAN_APPROVAL"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    INVALIDATED = "INVALIDATED"


class ReviewQuestion(BaseModel):
    question_id: str
    question: str
    type: str
    answer: Optional[str] = None
    selected_options: List[str] = Field(default_factory=list)
    source: str = ""
    confidence: float = 0.0

    model_config = {"extra": "forbid"}


class ReviewVerification(BaseModel):
    group_name: str
    target: str = ""
    expected: List[str] = Field(default_factory=list)
    actual: List[str] = Field(default_factory=list)
    verified: bool = False

    model_config = {"extra": "forbid"}


class HumanReviewGate(BaseModel):
    review_id: str = ""
    status: GateStatus = GateStatus.BLOCKED
    block_reasons: List[str] = Field(default_factory=list)
    vacancy_stable_id: str = ""
    application_type: str = ""
    resume_info: str = ""
    cover_letter: str = ""
    screening_questions: List[ReviewQuestion] = Field(default_factory=list)
    custom_text_notes: List[str] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    verification: List[ReviewVerification] = Field(default_factory=list)
    fingerprint: str = ""
    generated_at: str = ""

    model_config = {"extra": "forbid"}


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _review_payload(gate: "HumanReviewGate") -> Dict[str, Any]:
    return {
        "vacancy": gate.vacancy_stable_id,
        "cover_letter": gate.cover_letter,
        "answers": [
            {"question_id": q.question_id, "question": q.question, "type": q.type,
             "answer": q.answer, "selected_options": q.selected_options,
             "source": q.source, "confidence": q.confidence}
            for q in gate.screening_questions
        ],
        "verified_targets": [
            {"group": v.group_name, "target": v.target, "expected": v.expected,
             "actual": v.actual, "verified": v.verified}
            for v in gate.verification
        ],
    }


def build_review_gate(
    package: Any,
    plan: PrefillPlan,
    orchestration: OrchestrationReport,
    final_snapshot: Dict[str, Any],
    form: Optional[ApplicationForm] = None,
) -> HumanReviewGate:
    """Build the human review gate. Pure/read-only; no browser, no DB."""
    # form can be passed explicitly (Stage 20C+) or derived from package
    effective_form: Optional[ApplicationForm] = form
    if effective_form is None:
        # backward compat: try package.form (populated by enrich_package_with_form)
        effective_form = getattr(package, "form", None)  # type: ignore[assignment]
    # if still None, use an empty form so the gate still builds (BLOCKED)
    if effective_form is None:
        effective_form = ApplicationForm(
            source="hh", vacancy_stable_id=getattr(package, "vacancy_stable_id", "") or "",
            application_type=ApplicationType.unknown, questions=[])
    form = effective_form  # type: ignore[assignment]
    block_reasons: List[str] = []

    pkg_status = getattr(package, "validation_status", "") or ""
    if pkg_status != "VALID":
        block_reasons.append(f"package.validation_status is {pkg_status or 'UNKNOWN'} (must be VALID)")
    if plan.status != "VALID":
        block_reasons.append(f"plan.status is {plan.status} (must be VALID)")
    unresolved = list(plan.unresolved or [])
    if unresolved:
        block_reasons.append(f"{len(unresolved)} unresolved field(s)")
    if orchestration.verdict != "VERIFIED":
        block_reasons.append(f"orchestration.verdict is {orchestration.verdict} (must be VERIFIED)")
    if orchestration.failed_operations != 0:
        block_reasons.append(f"{orchestration.failed_operations} failed operation(s)")
    if orchestration.skipped_operations != 0:
        block_reasons.append(f"{orchestration.skipped_operations} skipped operation(s)")
    verification_errors = [e for e in (orchestration.errors or []) if e]
    if verification_errors:
        block_reasons.append(f"{len(verification_errors)} verification error(s)")

    # ---- review content (only from validated answers) ----
    answers_by_qid = {}
    for a in (getattr(package, "answers", []) or []):
        if not getattr(a, "requires_review", True) and getattr(a, "answer", None):
            answers_by_qid[a.question_id] = a

    questions: List[ReviewQuestion] = []
    custom_notes: List[str] = []
    for q in (form.questions or []):
        ans = answers_by_qid.get(q.id)
        answer_text = ans.answer if ans is not None else None
        selected: List[str] = []
        if answer_text and q.normalized_type in (QuestionType.RADIO, QuestionType.SELECT,
                                                 QuestionType.CHECKBOX):
            selected = [p.strip() for p in answer_text.split(";") if p.strip()]
        questions.append(ReviewQuestion(
            question_id=q.id, question=q.label or "", type=q.normalized_type.value,
            answer=answer_text, selected_options=selected,
            source=q.source.value if hasattr(q.source, "value") else str(q.source),
            confidence=float(getattr(ans, "confidence", 0.0)) if ans is not None else 0.0))
        if q.custom_option_text_id:
            svoi = any(s.lower() == "свой вариант" for s in selected)
            custom_notes.append(
                f"question {q.id}: custom variant selected={svoi}; "
                f"custom text field ({q.custom_option_text_id}) "
                + ("requires human text" if svoi else "not used (real option selected)"))

    # resume info: honest - based on captured form controls
    controls = list(final_snapshot.get("controls") or [])
    has_resume_control = any((c.get("type") or "").lower() == "file" or
                             "резюме" in (c.get("label") or "").lower()
                             for c in controls)
    resume_info = ("resume selection control found in form"
                   if has_resume_control else
                   "no resume selection control in captured form (HH uses account default resume)")

    # verification results from orchestration group checks
    verification: List[ReviewVerification] = []
    for gc in (orchestration.group_checks or []):
        verification.append(ReviewVerification(
            group_name=gc.group_name,
            target=f"input[type='{gc.input_type}'][name={gc.group_name}]",
            expected=list(gc.expected_checked), actual=list(gc.actual_checked),
            verified=bool(gc.ok)))

    gate = HumanReviewGate(
        status=GateStatus.READY_FOR_HUMAN_REVIEW if not block_reasons else GateStatus.BLOCKED,
        block_reasons=block_reasons,
        vacancy_stable_id=getattr(package, "vacancy_stable_id", "") or form.vacancy_stable_id or "",
        application_type=getattr(form, "application_type", "").value
        if hasattr(form.application_type, "value") else str(getattr(form, "application_type", "")),
        resume_info=resume_info,
        cover_letter=getattr(package, "cover_letter", "") or "",
        screening_questions=questions,
        custom_text_notes=custom_notes,
        unresolved=[u.reason for u in unresolved],
        review_reasons=list(getattr(package, "review_reasons", []) or []),
        warnings=list(getattr(package, "warnings", []) or []),
        verification=verification,
        generated_at=datetime.utcnow().isoformat(),
    )

    gate.fingerprint = _fingerprint(_review_payload(gate))
    gate.review_id = "review_" + gate.fingerprint[:16]
    return gate


# ---------- explicit human approval state machine ----------

class HumanReviewStore:
    """In-memory review store. NO DB, NO browser, NO network."""

    def __init__(self):
        self._reviews: Dict[str, Dict[str, Any]] = {}

    def save(self, gate: HumanReviewGate) -> str:
        entry = {"gate": gate.model_dump(), "fingerprint": gate.fingerprint,
                 "state": gate.status.value if hasattr(gate.status, "value") else str(gate.status)}
        self._reviews[gate.review_id] = entry
        return gate.review_id

    def get(self, review_id: str) -> Optional[Dict[str, Any]]:
        return self._reviews.get(review_id)

    def get_state(self, review_id: str) -> Optional[str]:
        entry = self._reviews.get(review_id)
        return entry["state"] if entry else None

    def mark_waiting_for_human(self, review_id: str) -> Dict[str, Any]:
        entry = self._reviews.get(review_id)
        if entry is None:
            return {"ok": False, "state": None, "reason": "unknown review_id"}
        if entry["state"] != GateStatus.READY_FOR_HUMAN_REVIEW.value:
            return {"ok": False, "state": entry["state"],
                    "reason": f"cannot mark waiting from state {entry['state']}"}
        entry["state"] = GateStatus.WAITING_FOR_HUMAN_APPROVAL.value
        return {"ok": True, "state": entry["state"], "reason": ""}

    def approve_review(self, review_id: str, fingerprint: str) -> Dict[str, Any]:
        """Explicit human approval. Pure state transition - no browser actions."""
        entry = self._reviews.get(review_id)
        if entry is None:
            return {"ok": False, "state": None, "reason": "unknown review_id"}
        if entry["fingerprint"] != fingerprint:
            return {"ok": False, "state": entry["state"],
                    "reason": "fingerprint mismatch (stale review)"}
        if entry["state"] == GateStatus.HUMAN_APPROVED.value:
            return {"ok": False, "state": entry["state"],
                    "reason": "already approved - approval is one-time"}
        if entry["state"] not in (GateStatus.READY_FOR_HUMAN_REVIEW.value,
                                  GateStatus.WAITING_FOR_HUMAN_APPROVAL.value):
            return {"ok": False, "state": entry["state"],
                    "reason": f"cannot approve from state {entry['state']}"}
        entry["state"] = GateStatus.HUMAN_APPROVED.value
        return {"ok": True, "state": entry["state"], "reason": ""}

    def invalidate_on_change(self, review_id: str, current_fingerprint: str) -> Dict[str, Any]:
        """If reviewed state changed -> INVALIDATED; a new review is required."""
        entry = self._reviews.get(review_id)
        if entry is None:
            return {"ok": False, "state": None, "reason": "unknown review_id"}
        if entry["state"] == GateStatus.INVALIDATED.value:
            return {"ok": True, "state": GateStatus.INVALIDATED.value,
                    "reason": "already invalidated"}
        if entry["fingerprint"] != current_fingerprint:
            entry["state"] = GateStatus.INVALIDATED.value
            return {"ok": True, "state": GateStatus.INVALIDATED.value,
                    "reason": "REVIEW_STATE_CHANGED"}
        return {"ok": True, "state": entry["state"], "reason": "state unchanged"}


def verify_review_fingerprint(gate: HumanReviewGate, current_fingerprint: str) -> Dict[str, Any]:
    """Check reviewed_state == current_state. Pure function."""
    if gate.fingerprint != current_fingerprint:
        return {"ok": False, "reason": "REVIEW_STATE_CHANGED",
                "reviewed": gate.fingerprint, "current": current_fingerprint}
    return {"ok": True, "reason": "", "reviewed": gate.fingerprint, "current": current_fingerprint}