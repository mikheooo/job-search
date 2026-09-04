"""Stage 45: Controlled Application Queue.

Provides safe, deterministic queueing and classification of real HeadHunter applications.
Supports filtered views for ready applications and human-review items, eligibility checks,
and machine-readable JSON exports.

SAFETY INVARIANTS:
1. Queue evaluation NEVER performs browser clicks or submit actions (Submit = 0).
2. can_submit() is strictly read-only and returns boolean eligibility + reason.
3. SUBMITTED applications are excluded from ready queue and can_submit returns false.
4. Idempotent: no duplicate applications per vacancy.
5. All operations are safe and deterministic (REAL HH SUBMIT = 0, PIPELINE = NOT RUN).
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from . import db
from .candidate_profile import load_candidate_profile
from .hh_application_orchestrator import HHApplicationState
from .hh_questionnaire import HHQuestionnaire, HHQuestionStatus
from .hh_questionnaire_audit import audit_questionnaire

logger = logging.getLogger(__name__)


class SubmitEligibilityResult(BaseModel):
    allowed: bool
    reason: str
    application_id: str
    state: str

    model_config = {"extra": "forbid"}


class HumanReviewQuestionDetail(BaseModel):
    question_id: str
    question_text: str
    current_answer: Any
    source_of_truth: str
    action_required: str
    is_confirmed: bool

    model_config = {"extra": "forbid"}


class HHQueueItem(BaseModel):
    application_id: str
    vacancy_id: str
    vacancy_title: str
    company: str
    application_state: str
    questionnaire_id: Optional[str] = None
    questionnaire_state: str
    audit_state: str
    can_submit_allowed: bool
    can_submit_reason: str
    reason_blocker: Optional[str] = None
    last_updated: str
    questions: List[HumanReviewQuestionDetail] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


def can_submit(application_id: str) -> SubmitEligibilityResult:
    """Check if an application is eligible for submission.

    SAFETY: This function is strictly READ-ONLY and NEVER executes browser actions.
    """
    db.init_db()
    app_data = db.get_hh_application(application_id)
    if not app_data:
        app_data = db.get_hh_application_by_vacancy(application_id)
    if not app_data:
        return SubmitEligibilityResult(
            allowed=False,
            reason="application_not_found",
            application_id=application_id,
            state="UNKNOWN",
        )

    current_state = app_data.get("state", "UNKNOWN")
    app_id = app_data.get("application_id", application_id)
    qid = app_data.get("questionnaire_id")

    if current_state == HHApplicationState.SUBMITTED.value:
        return SubmitEligibilityResult(
            allowed=False,
            reason="application_already_submitted",
            application_id=app_id,
            state=current_state,
        )

    if current_state == HHApplicationState.NEEDS_HUMAN_REVIEW.value:
        return SubmitEligibilityResult(
            allowed=False,
            reason="human_review_required",
            application_id=app_id,
            state=current_state,
        )

    if current_state == HHApplicationState.QUESTIONNAIRE_REQUIRED.value:
        return SubmitEligibilityResult(
            allowed=False,
            reason="questionnaire_answers_required",
            application_id=app_id,
            state=current_state,
        )

    if current_state == HHApplicationState.BLOCKED.value:
        return SubmitEligibilityResult(
            allowed=False,
            reason="application_blocked",
            application_id=app_id,
            state=current_state,
        )

    if current_state == HHApplicationState.FAILED.value:
        return SubmitEligibilityResult(
            allowed=False,
            reason="application_failed",
            application_id=app_id,
            state=current_state,
        )

    if current_state in (HHApplicationState.AMBIGUOUS.value, "AMBIGUOUS", "AMBIGUOUS_POST_SUBMIT"):
        return SubmitEligibilityResult(
            allowed=False,
            reason="application_ambiguous_outcome",
            application_id=app_id,
            state=current_state,
        )

    if current_state == "NOT_ELIGIBLE":
        return SubmitEligibilityResult(
            allowed=False,
            reason="vacancy_not_eligible",
            application_id=app_id,
            state=current_state,
        )

    vac_stable = app_data.get("vacancy_stable_id") or ""
    if vac_stable:
        claim = db.get_submission_claim(vac_stable)
        if claim:
            c_status = claim.get("status")
            if c_status in ("SUBMITTED", "ATTEMPTING", "AMBIGUOUS", "FAILED_SAFE"):
                return SubmitEligibilityResult(
                    allowed=False,
                    reason=f"submission_claim_{c_status.lower()}",
                    application_id=app_id,
                    state=current_state,
                )

    if current_state != HHApplicationState.READY_TO_SUBMIT.value:
        return SubmitEligibilityResult(
            allowed=False,
            reason="application_not_in_ready_state",
            application_id=app_id,
            state=current_state,
        )

    # State is READY_TO_SUBMIT: verify questionnaire audit if questionnaire exists
    if qid:
        q_data = db.get_hh_questionnaire(qid)
        if not q_data:
            return SubmitEligibilityResult(
                allowed=False,
                reason="questionnaire_not_found",
                application_id=app_id,
                state=current_state,
            )
        q_status = q_data.get("status")
        if q_status not in (HHQuestionStatus.READY_TO_SUBMIT.value, HHQuestionStatus.SUBMITTED.value):
            try:
                report = audit_questionnaire(qid, application_id=app_id)
                if report.overall.value != "SAFE_TO_SUBMIT":
                    return SubmitEligibilityResult(
                        allowed=False,
                        reason="questionnaire_audit_required",
                        application_id=app_id,
                        state=current_state,
                    )
            except Exception as e:
                logger.warning(f"Error auditing questionnaire {qid}: {e}")
                return SubmitEligibilityResult(
                    allowed=False,
                    reason="questionnaire_audit_failed",
                    application_id=app_id,
                    state=current_state,
                )

    return SubmitEligibilityResult(
        allowed=True,
        reason="ready_to_submit",
        application_id=app_id,
        state=current_state,
    )


def get_controlled_application_queue(filter_mode: Optional[str] = None) -> List[HHQueueItem]:
    """Retrieve and classify all HeadHunter applications in a controlled queue."""
    db.init_db()
    raw_apps = db.list_hh_applications(limit=200)

    # Deduplicate applications by application_id / vacancy_stable_id
    seen_apps = set()
    queue_items: List[HHQueueItem] = []

    for app in raw_apps:
        app_id = app.get("application_id")
        if not app_id or app_id in seen_apps:
            continue
        seen_apps.add(app_id)

        vac_stable_id = app.get("vacancy_stable_id") or ""
        vac_id = vac_stable_id.split(":")[-1] if ":" in vac_stable_id else vac_stable_id
        vac_title = app.get("title") or "Unknown Vacancy"
        company = app.get("employer") or "Unknown Company"
        app_state = app.get("state") or "NEW"
        qid = app.get("questionnaire_id")
        last_reason = app.get("last_transition_reason") or "N/A"
        updated_at = app.get("updated_at") or app.get("created_at") or "N/A"

        # Questionnaire state & audit state
        q_state = "NOT_REQUIRED"
        audit_state = "N/A"
        questions_detail: List[HumanReviewQuestionDetail] = []

        if qid:
            q_data = db.get_hh_questionnaire(qid)
            if q_data:
                q_state = q_data.get("status") or "UNKNOWN"
                try:
                    report = audit_questionnaire(qid, application_id=app_id)
                    audit_state = report.overall.value
                    for item in report.items:
                        questions_detail.append(
                            HumanReviewQuestionDetail(
                                question_id=item.question_id,
                                question_text=item.question_text,
                                current_answer=item.current_answer,
                                source_of_truth=item.source_of_truth,
                                action_required="Confirm answer" if item.is_confirmed_by_profile else "Provide human decision / facts",
                                is_confirmed=item.is_confirmed_by_profile,
                            )
                        )
                except Exception:
                    audit_state = "SAFE_TO_SUBMIT" if q_state in (HHQuestionStatus.READY_TO_SUBMIT.value, HHQuestionStatus.SUBMITTED.value) else "NEEDS_CORRECTION"
            else:
                q_state = "MISSING"
                audit_state = "NEEDS_CORRECTION"

        # Eligibility
        elig = can_submit(app_id)

        item = HHQueueItem(
            application_id=app_id,
            vacancy_id=vac_id,
            vacancy_title=vac_title,
            company=company,
            application_state=app_state,
            questionnaire_id=qid,
            questionnaire_state=q_state,
            audit_state=audit_state,
            can_submit_allowed=elig.allowed,
            can_submit_reason=elig.reason,
            reason_blocker=last_reason,
            last_updated=updated_at,
            questions=questions_detail,
        )

        # Apply filtering if requested
        if filter_mode == "ready":
            if item.application_state == HHApplicationState.READY_TO_SUBMIT.value and item.can_submit_allowed:
                queue_items.append(item)
        elif filter_mode == "human_review":
            if item.application_state == HHApplicationState.NEEDS_HUMAN_REVIEW.value or item.can_submit_reason == "human_review_required":
                queue_items.append(item)
        else:
            queue_items.append(item)

    # Sort: READY_TO_SUBMIT first, then NEEDS_HUMAN_REVIEW, then SUBMITTED, then others
    state_order = {
        HHApplicationState.READY_TO_SUBMIT.value: 1,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value: 2,
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value: 3,
        HHApplicationState.DRAFT_READY.value: 4,
        HHApplicationState.ANALYZED.value: 5,
        HHApplicationState.NEW.value: 6,
        HHApplicationState.SUBMITTED.value: 7,
        HHApplicationState.BLOCKED.value: 8,
        HHApplicationState.FAILED.value: 9,
        HHApplicationState.STALE.value: 10,
    }
    queue_items.sort(key=lambda x: (state_order.get(x.application_state, 99), 0 if x.vacancy_id else 1, x.last_updated), reverse=False)
    return queue_items


def format_queue_cli(items: List[HHQueueItem]) -> str:
    """Format full application queue for CLI display."""
    lines = [
        "======================================================================================================",
        "                                  CONTROLLED HH APPLICATION QUEUE                                     ",
        "======================================================================================================",
        f"{'APPLICATION ID':<18} | {'VACANCY ID':<10} | {'COMPANY':<18} | {'TITLE':<30} | {'STATE':<16} | {'CAN SUBMIT':<10}",
        "-" * 114,
    ]
    for item in items:
        can_sub_str = "YES" if item.can_submit_allowed else f"NO ({item.can_submit_reason[:15]})"
        lines.append(
            f"{item.application_id:<18} | {item.vacancy_id:<10} | {item.company[:18]:<18} | {item.vacancy_title[:30]:<30} | {item.application_state:<16} | {can_sub_str:<10}"
        )
    lines.append("=" * 114)
    return "\n".join(lines)


def format_ready_queue_cli(items: List[HHQueueItem]) -> str:
    """Format READY queue for CLI display."""
    lines = [
        "======================================================================================================",
        "                             READY TO SUBMIT APPLICATION QUEUE (HUMAN GATED)                          ",
        "======================================================================================================",
    ]
    if not items:
        lines.append("No applications currently in READY_TO_SUBMIT state.")
    else:
        for idx, item in enumerate(items, 1):
            lines.extend([
                f"{idx}. Application:        {item.application_id}",
                f"   Vacancy:            {item.vacancy_title} ({item.vacancy_id}) @ {item.company}",
                f"   Questionnaire:      {item.questionnaire_state} (ID: {item.questionnaire_id or 'N/A'})",
                f"   Audit Result:       {item.audit_state}",
                f"   Submit Eligibility: {'ELIGIBLE' if item.can_submit_allowed else 'BLOCKED'} ({item.can_submit_reason})",
                f"   Action Required:    Explicit human confirmation (--confirm-submit)",
                "-" * 80,
            ])
    lines.append("======================================================================================================")
    return "\n".join(lines)


def format_human_review_queue_cli(items: List[HHQueueItem]) -> str:
    """Format HUMAN REVIEW queue for CLI display."""
    lines = [
        "======================================================================================================",
        "                                  HUMAN REVIEW REQUIRED QUEUE                                         ",
        "======================================================================================================",
    ]
    if not items:
        lines.append("No applications currently require human review.")
    else:
        for idx, item in enumerate(items, 1):
            lines.extend([
                f"{idx}. Application:        {item.application_id}",
                f"   Vacancy:            {item.vacancy_title} ({item.vacancy_id}) @ {item.company}",
                f"   State:              {item.application_state}",
                f"   Questionnaire ID:   {item.questionnaire_id or 'N/A'}",
                f"   Questions requiring human decision:",
            ])
            if item.questions:
                for q in item.questions:
                    conf_str = "CONFIRMED" if q.is_confirmed else "UNVERIFIED / REVIEW"
                    lines.append(f"     * [{q.question_id}] {q.question_text}")
                    lines.append(f"       Current: {q.current_answer} | Source: {q.source_of_truth} | [{conf_str}]")
                    lines.append(f"       Decision Needed: {q.action_required}")
            else:
                lines.append(f"     * Blocker Reason: {item.reason_blocker or 'Human decision required'}")
            lines.append("-" * 80)
    lines.append("======================================================================================================")
    return "\n".join(lines)
