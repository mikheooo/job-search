"""Stage 35: HH Application State Machine & Orchestrator.

Unifies Stages 30D–34 into a single, authoritative application state machine and orchestrator
for HeadHunter (HH) interactions.

SAFETY INVARIANTS:
1. NEW -> SUBMITTED = FORBIDDEN.
2. MESSAGE_DETECTED -> SUBMITTED = FORBIDDEN.
3. QUESTIONNAIRE_REQUIRED -> SUBMITTED = FORBIDDEN.
4. NEEDS_HUMAN_REVIEW -> SUBMITTED = FORBIDDEN.
5. READY_TO_SUBMIT without explicit confirmation: Submit = 0.
6. READY_TO_SUBMIT + explicit confirmation: Submit MAY proceed.
7. Questionnaire changed: Submit = 0, moves to STALE -> NEEDS_HUMAN_REVIEW.
8. Required answer missing / invalid option / unknown question_id: Submit = 0.
9. Duplicate event: duplicate application = 0 (idempotent).
10. Autonomous watcher: Submit = 0 (strictly read-only).
11. Browser/CDP failure or unauthenticated session: Submit = 0.
12. All state transitions are audited with timestamp, reason, and evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from . import db
from .hh_questionnaire import (
    HHQuestionnaire,
    HHQuestionStatus,
    compute_questionnaire_fingerprint,
    validate_human_answers,
    submit_questionnaire_response,
)

logger = logging.getLogger(__name__)

# Sequential lock for atomic transitions per process
_ORCHESTRATOR_LOCK = threading.Lock()


class HHApplicationState(str, Enum):
    NEW = "NEW"
    DISCOVERED = "DISCOVERED"
    MATCHED = "MATCHED"
    APPLICATION_IN_PROGRESS = "APPLICATION_IN_PROGRESS"
    QUESTIONNAIRE_AUTO_FILLED = "QUESTIONNAIRE_AUTO_FILLED"  # legacy Stage 35, deprecated
    READY_FOR_AUTONOMOUS_SUBMIT = "READY_FOR_AUTONOMOUS_SUBMIT"  # legacy Stage 35, deprecated
    MESSAGE_DETECTED = "MESSAGE_DETECTED"
    ANALYZED = "ANALYZED"
    DRAFT_READY = "DRAFT_READY"
    QUESTIONNAIRE_REQUIRED = "QUESTIONNAIRE_REQUIRED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    MESSAGE_AUTO_REPLIED = "MESSAGE_AUTO_REPLIED"
    INTERVIEW_INVITED = "INTERVIEW_INVITED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    STALE = "STALE"


# Explicit map of legal transitions
LEGAL_TRANSITIONS: Dict[str, Set[str]] = {
    HHApplicationState.NEW.value: {
        HHApplicationState.DISCOVERED.value,
        HHApplicationState.MATCHED.value,
        HHApplicationState.APPLICATION_IN_PROGRESS.value,
        HHApplicationState.MESSAGE_DETECTED.value,
        HHApplicationState.ANALYZED.value,
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.DISCOVERED.value: {
        HHApplicationState.MATCHED.value,
        HHApplicationState.APPLICATION_IN_PROGRESS.value,
        HHApplicationState.ANALYZED.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.MATCHED.value: {
        HHApplicationState.APPLICATION_IN_PROGRESS.value,
        HHApplicationState.QUESTIONNAIRE_AUTO_FILLED.value,
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.APPLICATION_IN_PROGRESS.value: {
        HHApplicationState.QUESTIONNAIRE_AUTO_FILLED.value,
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.QUESTIONNAIRE_AUTO_FILLED.value: {
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value: {
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.MESSAGE_DETECTED.value: {
        HHApplicationState.ANALYZED.value,
        HHApplicationState.MESSAGE_RECEIVED.value,
        HHApplicationState.MESSAGE_AUTO_REPLIED.value,
        HHApplicationState.INTERVIEW_INVITED.value,
        HHApplicationState.REJECTED.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
        HHApplicationState.STALE.value,
    },
    HHApplicationState.ANALYZED.value: {
        HHApplicationState.MATCHED.value,
        HHApplicationState.APPLICATION_IN_PROGRESS.value,
        HHApplicationState.DRAFT_READY.value,
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
        HHApplicationState.STALE.value,
    },
    HHApplicationState.DRAFT_READY.value: {
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
        HHApplicationState.STALE.value,
    },
    HHApplicationState.QUESTIONNAIRE_REQUIRED.value: {
        HHApplicationState.QUESTIONNAIRE_AUTO_FILLED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
        HHApplicationState.STALE.value,
    },
    HHApplicationState.NEEDS_HUMAN_REVIEW.value: {
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.QUESTIONNAIRE_AUTO_FILLED.value,
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.STALE.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.READY_TO_SUBMIT.value: {
        HHApplicationState.SUBMITTED.value,  # ONLY with explicit human confirmation or verified submit
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.STALE.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.SUBMITTED.value: {
        HHApplicationState.SUBMITTED.value,  # Terminal state: self-transition for read-only audit/verification
        HHApplicationState.MESSAGE_RECEIVED.value,
        HHApplicationState.MESSAGE_AUTO_REPLIED.value,
        HHApplicationState.INTERVIEW_INVITED.value,
        HHApplicationState.REJECTED.value,
    },
    HHApplicationState.MESSAGE_RECEIVED.value: {
        HHApplicationState.MESSAGE_AUTO_REPLIED.value,
        HHApplicationState.INTERVIEW_INVITED.value,
        HHApplicationState.REJECTED.value,
        HHApplicationState.BLOCKED.value,
    },
    HHApplicationState.MESSAGE_AUTO_REPLIED.value: {
        HHApplicationState.MESSAGE_RECEIVED.value,
        HHApplicationState.INTERVIEW_INVITED.value,
        HHApplicationState.REJECTED.value,
    },
    HHApplicationState.INTERVIEW_INVITED.value: {
        HHApplicationState.INTERVIEW_INVITED.value,
    },
    HHApplicationState.REJECTED.value: {
        HHApplicationState.REJECTED.value,
    },
    HHApplicationState.FAILED.value: {
        HHApplicationState.NEW.value,
        HHApplicationState.DISCOVERED.value,
        HHApplicationState.MESSAGE_DETECTED.value,
        HHApplicationState.ANALYZED.value,
        HHApplicationState.DRAFT_READY.value,
        HHApplicationState.QUESTIONNAIRE_REQUIRED.value,
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.BLOCKED.value,
    },
    HHApplicationState.STALE.value: {
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.ANALYZED.value,
        HHApplicationState.NEW.value,
        HHApplicationState.BLOCKED.value,
        HHApplicationState.FAILED.value,
    },
    HHApplicationState.BLOCKED.value: {
        HHApplicationState.NEEDS_HUMAN_REVIEW.value,
        HHApplicationState.READY_TO_SUBMIT.value,
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT.value,
        HHApplicationState.FAILED.value,
        HHApplicationState.NEW.value,
    },
}


class HHApplication(BaseModel):
    application_id: str
    conversation_id: Optional[str] = None
    vacancy_stable_id: Optional[str] = None
    title: Optional[str] = None
    employer: Optional[str] = None
    state: str = HHApplicationState.NEW.value
    draft: Optional[str] = None
    questionnaire_id: Optional[str] = None
    answers: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    last_transition_reason: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    model_config = {"extra": "forbid"}


class TransitionResult(BaseModel):
    ok: bool = False
    application_id: str = ""
    from_state: str = ""
    to_state: str = ""
    reason: str = ""
    transition_id: Optional[int] = None
    error: Optional[str] = None

    model_config = {"extra": "forbid"}


class HHApplicationTransitionRecord(BaseModel):
    id: int
    application_id: str
    conversation_id: Optional[str] = None
    vacancy_stable_id: Optional[str] = None
    state: str
    previous_state: Optional[str] = None
    reason: str
    evidence: Dict[str, Any] = Field(default_factory=dict)
    created_at: str

    model_config = {"extra": "forbid"}


def get_or_create_hh_application(
    application_id: str,
    conversation_id: Optional[str] = None,
    vacancy_stable_id: Optional[str] = None,
    title: Optional[str] = None,
    employer: Optional[str] = None,
) -> HHApplication:
    """Retrieve an existing HHApplication or create an initial one in NEW state."""
    db.init_db()
    data = db.get_hh_application(application_id)
    if not data and conversation_id:
        data = db.get_hh_application_by_conversation(conversation_id)
    if not data and vacancy_stable_id:
        data = db.get_hh_application_by_vacancy(vacancy_stable_id)
        
    if data:
        return HHApplication(**data)

    now = datetime.utcnow().isoformat()
    app = HHApplication(
        application_id=application_id,
        conversation_id=conversation_id,
        vacancy_stable_id=vacancy_stable_id,
        title=title,
        employer=employer,
        state=HHApplicationState.NEW.value,
        draft=None,
        questionnaire_id=None,
        answers={},
        error=None,
        last_transition_reason="initial_creation",
        created_at=now,
        updated_at=now,
    )
    db.save_hh_application(app.model_dump())
    return app


def transition_application(
    application_id: str,
    to_state: HHApplicationState | str,
    reason: str,
    evidence: Optional[Dict[str, Any]] = None,
    expected_from_state: Optional[HHApplicationState | str] = None,
    confirm_submit: bool = False,
) -> TransitionResult:
    """Execute an explicit, audited state transition for an HH application.

    SAFETY CHECKS:
    1. Rejects illegal transitions not in LEGAL_TRANSITIONS.
    2. Rejects any transition to SUBMITTED without from_state == READY_TO_SUBMIT.
    3. Rejects any transition to SUBMITTED without confirm_submit == True.
    4. Records transition history in database.
    """
    with _ORCHESTRATOR_LOCK:
        db.init_db()
        data = db.get_hh_application(application_id)
        if not data:
            # Initialize with NEW state
            now = datetime.utcnow().isoformat()
            app = HHApplication(
                application_id=application_id,
                state=HHApplicationState.NEW.value,
                created_at=now,
                updated_at=now,
            )
            db.save_hh_application(app.model_dump())
            current_state = HHApplicationState.NEW.value
            conv_id = None
            vac_id = None
        else:
            app = HHApplication(**data)
            current_state = app.state
            conv_id = app.conversation_id
            vac_id = app.vacancy_stable_id

        target_state_str = to_state.value if isinstance(to_state, HHApplicationState) else str(to_state).strip()

        # Check expected_from_state if specified
        if expected_from_state is not None:
            exp_str = expected_from_state.value if isinstance(expected_from_state, HHApplicationState) else str(expected_from_state).strip()
            if current_state != exp_str:
                return TransitionResult(
                    ok=False,
                    application_id=application_id,
                    from_state=current_state,
                    to_state=target_state_str,
                    reason=f"Transition rejected: current state '{current_state}' does not match expected '{exp_str}'",
                    error="EXPECTED_FROM_STATE_MISMATCH",
                )

        # Invariant: Legal transition check
        allowed = LEGAL_TRANSITIONS.get(current_state, set())
        if target_state_str not in allowed:
            return TransitionResult(
                ok=False,
                application_id=application_id,
                from_state=current_state,
                to_state=target_state_str,
                reason=f"Illegal transition: '{current_state}' -> '{target_state_str}' is forbidden",
                error="ILLEGAL_TRANSITION",
            )

        # Invariant: Human Confirmation Gate for SUBMITTED
        if target_state_str == HHApplicationState.SUBMITTED.value:
            if not confirm_submit:
                return TransitionResult(
                    ok=False,
                    application_id=application_id,
                    from_state=current_state,
                    to_state=target_state_str,
                    reason="Explicit human confirmation (--confirm-submit / confirm_submit=True) required to enter SUBMITTED state.",
                    error="MISSING_HUMAN_CONFIRMATION",
                )

            if current_state == HHApplicationState.SUBMITTED.value:
                # Read-only audit/verification update on an already SUBMITTED application
                now = datetime.now(timezone.utc).isoformat()
                app.last_transition_reason = reason
                app.updated_at = now
                if evidence:
                    if "answers" in evidence and evidence["answers"] is not None:
                        app.answers = evidence["answers"]
                    if "error" in evidence:
                        app.error = evidence["error"]
                db.save_hh_application(app.model_dump())
                trans_id = db.save_hh_application_transition({
                    "application_id": application_id,
                    "conversation_id": app.conversation_id or conv_id,
                    "vacancy_stable_id": app.vacancy_stable_id or vac_id,
                    "state": HHApplicationState.SUBMITTED.value,
                    "previous_state": HHApplicationState.SUBMITTED.value,
                    "reason": reason,
                    "evidence": evidence or {},
                    "created_at": now,
                })
                return TransitionResult(
                    ok=True,
                    application_id=application_id,
                    from_state=current_state,
                    to_state=target_state_str,
                    reason=reason,
                    transition_id=trans_id,
                )

            if current_state != HHApplicationState.READY_TO_SUBMIT.value:
                return TransitionResult(
                    ok=False,
                    application_id=application_id,
                    from_state=current_state,
                    to_state=target_state_str,
                    reason=f"Cannot transition to SUBMITTED from '{current_state}'. Must be READY_TO_SUBMIT.",
                    error="SUBMIT_FORBIDDEN_FROM_STATE",
                )

        # Apply update
        now = datetime.utcnow().isoformat()
        app.state = target_state_str
        app.last_transition_reason = reason
        app.updated_at = now
        if evidence:
            if "draft" in evidence and evidence["draft"] is not None:
                app.draft = evidence["draft"]
            if "questionnaire_id" in evidence and evidence["questionnaire_id"] is not None:
                app.questionnaire_id = evidence["questionnaire_id"]
            if "answers" in evidence and evidence["answers"] is not None:
                app.answers = evidence["answers"]
            if "error" in evidence:
                app.error = evidence["error"]
            if "title" in evidence and evidence["title"]:
                app.title = evidence["title"]
            if "employer" in evidence and evidence["employer"]:
                app.employer = evidence["employer"]
            if "conversation_id" in evidence and evidence["conversation_id"]:
                app.conversation_id = evidence["conversation_id"]
            if "vacancy_stable_id" in evidence and evidence["vacancy_stable_id"]:
                app.vacancy_stable_id = evidence["vacancy_stable_id"]

        db.save_hh_application(app.model_dump())

        # Save audit record
        trans_id = db.save_hh_application_transition({
            "application_id": application_id,
            "conversation_id": app.conversation_id or conv_id,
            "vacancy_stable_id": app.vacancy_stable_id or vac_id,
            "state": target_state_str,
            "previous_state": current_state,
            "reason": reason,
            "evidence": evidence or {},
            "created_at": now,
        })

        return TransitionResult(
            ok=True,
            application_id=application_id,
            from_state=current_state,
            to_state=target_state_str,
            reason=reason,
            transition_id=trans_id,
        )


def format_application_cli_output(app: HHApplication) -> str:
    """Format an HH application for human-friendly CLI display."""
    lines = [
        "-------------------------------------------------------",
        "HH APPLICATION",
        "-------------------------------------------------------",
        f"Application:   {app.application_id}",
        f"Conversation:  {app.conversation_id or 'N/A'}",
        f"Vacancy:       {app.title or app.vacancy_stable_id or 'N/A'}",
        f"Employer:      {app.employer or 'N/A'}",
        "",
        f"State:         {app.state}",
        f"Last reason:   {app.last_transition_reason or 'N/A'}",
        "",
    ]

    # Determine human action required
    action_lines = []
    if app.state == HHApplicationState.NEEDS_HUMAN_REVIEW.value:
        if app.questionnaire_id:
            action_lines.append("- Answer questionnaire questions (`questionnaire show / answer`)")
        else:
            action_lines.append("- Review reply draft or employer request (`review approve / reject`)")
    elif app.state == HHApplicationState.READY_TO_SUBMIT.value:
        action_lines.append("- Explicitly confirm Submit (`application submit <id> --confirm-submit`)")
    elif app.state == HHApplicationState.QUESTIONNAIRE_REQUIRED.value:
        action_lines.append("- Inspect questionnaire questions and prepare answers")
    elif app.state == HHApplicationState.BLOCKED.value:
        action_lines.append(f"- Resolve safety blocker: {app.error or 'Check logs'}")
    elif app.state == HHApplicationState.STALE.value:
        action_lines.append("- Page or questionnaire DOM changed; re-inspect in browser")
    elif app.state == HHApplicationState.SUBMITTED.value:
        action_lines.append("- None (Application already submitted)")
    else:
        action_lines.append("- None (Processing)")

    lines.append("Human action required:")
    for al in action_lines:
        lines.append(al)
    lines.append("")

    submit_allowed = "YES" if app.state == HHApplicationState.READY_TO_SUBMIT.value else "NO"
    lines.extend([
        f"Submit allowed: {submit_allowed}",
        "-------------------------------------------------------",
    ])
    return "\n".join(lines)


class HHApplicationOrchestrator:
    """Authoritative orchestrator for HH application and message lifecycle."""

    def __init__(self):
        db.init_db()

    def orchestrate_incoming_event(
        self,
        conversation_id: str,
        message_id: str,
        sender: str,
        text: str,
        sent_at: Optional[str] = None,
        vacancy_stable_id: Optional[str] = None,
        title: Optional[str] = None,
        employer: Optional[str] = None,
        classification: Optional[str] = None,
        draft: Optional[str] = None,
        validation_status: Optional[str] = None,
        questionnaire_data: Optional[Dict[str, Any]] = None,
    ) -> HHApplication:
        """Process an incoming message event through the state machine pipeline.

        IDEMPOTENCY:
        - If the application is already in a downstream review or submitted state for this conversation,
          returns the existing application without duplicating state transitions.
        """
        app_id = f"app_{conversation_id}"
        app = get_or_create_hh_application(
            application_id=app_id,
            conversation_id=conversation_id,
            vacancy_stable_id=vacancy_stable_id,
            title=title,
            employer=employer,
        )

        # If already SUBMITTED or in active human review with same draft/answers, return idempotently
        if app.state in {
            HHApplicationState.SUBMITTED.value,
            HHApplicationState.NEEDS_HUMAN_REVIEW.value,
            HHApplicationState.READY_TO_SUBMIT.value,
        }:
            # If draft or title changed, update metadata without invalidating terminal state
            return app

        evidence_base = {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "sender": sender,
            "sent_at": sent_at,
            "title": title,
            "employer": employer,
            "vacancy_stable_id": vacancy_stable_id,
        }

        # Step 1: NEW -> MESSAGE_DETECTED
        transition_application(
            application_id=app_id,
            to_state=HHApplicationState.MESSAGE_DETECTED,
            reason="new_incoming_message_detected",
            evidence=evidence_base,
        )

        # Step 2: MESSAGE_DETECTED -> ANALYZED
        cls = classification or "GENERAL_INQUIRY"
        transition_application(
            application_id=app_id,
            to_state=HHApplicationState.ANALYZED,
            reason=f"message_classified_as_{cls}",
            evidence={**evidence_base, "classification": cls},
        )

        # Step 3: Check for questionnaire requirement
        if questionnaire_data:
            quest = HHQuestionnaire(**questionnaire_data)
            evidence_q = {**evidence_base, "questionnaire_id": quest.questionnaire_id, "questions_count": len(quest.questions)}
            transition_application(
                application_id=app_id,
                to_state=HHApplicationState.QUESTIONNAIRE_REQUIRED,
                reason="screening_questions_detected",
                evidence=evidence_q,
            )
            transition_application(
                application_id=app_id,
                to_state=HHApplicationState.NEEDS_HUMAN_REVIEW,
                reason="human_answers_required_for_questionnaire",
                evidence=evidence_q,
            )
            return get_or_create_hh_application(app_id)

        # Step 4: Draft generation and validation
        if draft:
            evidence_d = {**evidence_base, "draft": draft, "validation": validation_status}
            transition_application(
                application_id=app_id,
                to_state=HHApplicationState.DRAFT_READY,
                reason="reply_draft_generated",
                evidence=evidence_d,
            )

            # Step 5: Route based on validation
            if validation_status == "APPROVED":
                transition_application(
                    application_id=app_id,
                    to_state=HHApplicationState.READY_TO_SUBMIT,
                    reason="draft_approved_ready_for_confirmed_send",
                    evidence=evidence_d,
                )
            else:
                transition_application(
                    application_id=app_id,
                    to_state=HHApplicationState.NEEDS_HUMAN_REVIEW,
                    reason=f"draft_validation_{validation_status or 'REQUIRES_REVIEW'}",
                    evidence=evidence_d,
                )
        else:
            if cls == "HUMAN_REVIEW":
                transition_application(
                    application_id=app_id,
                    to_state=HHApplicationState.NEEDS_HUMAN_REVIEW,
                    reason="sensitive_topic_requires_human_decision",
                    evidence=evidence_base,
                )
            else:
                # System notifications or no reply
                pass

        return get_or_create_hh_application(app_id)

    def record_questionnaire_answers(
        self,
        application_id: str,
        human_answers: Dict[str, Any],
        current_dom_fingerprint: Optional[str] = None,
    ) -> TransitionResult:
        """Validate human questionnaire answers and transition to READY_TO_SUBMIT or STALE."""
        app = get_or_create_hh_application(application_id)
        if not app.questionnaire_id:
            return TransitionResult(
                ok=False,
                application_id=application_id,
                from_state=app.state,
                to_state=app.state,
                reason="No questionnaire associated with this application",
                error="NO_QUESTIONNAIRE",
            )

        q_data = db.get_hh_questionnaire(app.questionnaire_id)
        if not q_data:
            return TransitionResult(
                ok=False,
                application_id=application_id,
                from_state=app.state,
                to_state=app.state,
                reason=f"Questionnaire {app.questionnaire_id} not found in database",
                error="QUESTIONNAIRE_NOT_FOUND",
            )

        quest = HHQuestionnaire(**q_data)

        # Invariant: Detect changed questionnaire DOM -> STALE -> NEEDS_HUMAN_REVIEW
        if current_dom_fingerprint and current_dom_fingerprint != quest.fingerprint:
            transition_application(
                application_id=application_id,
                to_state=HHApplicationState.STALE,
                reason="questionnaire_dom_fingerprint_mismatch",
                evidence={"expected_fp": quest.fingerprint, "actual_fp": current_dom_fingerprint},
            )
            return transition_application(
                application_id=application_id,
                to_state=HHApplicationState.NEEDS_HUMAN_REVIEW,
                reason="re_review_required_due_to_stale_questionnaire",
                evidence={"answers": human_answers},
            )

        # Validate answers
        val = validate_human_answers(quest, human_answers, current_dom_fingerprint=current_dom_fingerprint)
        if not val.ok:
            return TransitionResult(
                ok=False,
                application_id=application_id,
                from_state=app.state,
                to_state=app.state,
                reason=f"Questionnaire answers validation failed: {val.reason}",
                error="VALIDATION_FAILED",
            )

        # Store answers in questionnaire table
        db.update_hh_questionnaire_answers(quest.questionnaire_id, human_answers, new_status=HHQuestionStatus.READY_TO_SUBMIT.value)

        # Transition application: NEEDS_HUMAN_REVIEW -> READY_TO_SUBMIT
        return transition_application(
            application_id=application_id,
            to_state=HHApplicationState.READY_TO_SUBMIT,
            reason="all_questionnaire_answers_validated_by_human",
            evidence={"answers": human_answers, "questionnaire_id": quest.questionnaire_id},
        )

    def execute_confirmed_submit(
        self,
        application_id: str,
        confirm_submit: bool = False,
        evaluate_fn: Optional[Callable[[str], str]] = None,
        current_dom_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute real/mock submission with full invariant enforcement.

        SAFETY INVARIANTS:
        - confirm_submit == False -> Submit = 0, BLOCKED
        - current_state != READY_TO_SUBMIT -> Submit = 0, BLOCKED
        - questionnaire changed -> Submit = 0, STALE -> NEEDS_HUMAN_REVIEW
        - evaluate_fn failure -> Submit = 0, FAILED
        - One-shot submit invariant strictly enforced
        """
        app = get_or_create_hh_application(application_id)

        # Gate 1: State check
        if app.state != HHApplicationState.READY_TO_SUBMIT.value:
            return {
                "verdict": "BLOCKED",
                "submit_count": 0,
                "reason": f"Cannot submit: current state is '{app.state}' (must be READY_TO_SUBMIT)",
                "application_id": application_id,
                "state": app.state,
            }

        # Gate 2: Explicit human confirmation check
        if not confirm_submit:
            return {
                "verdict": "BLOCKED",
                "submit_count": 0,
                "reason": "Submit blocked: explicit human confirmation (--confirm-submit / confirm_submit=True) is required",
                "application_id": application_id,
                "state": app.state,
            }

        # Gate 3: Questionnaire verification if present
        if app.questionnaire_id:
            q_data = db.get_hh_questionnaire(app.questionnaire_id)
            if q_data:
                quest = HHQuestionnaire(**q_data)
                if current_dom_fingerprint and current_dom_fingerprint != quest.fingerprint:
                    transition_application(
                        application_id=application_id,
                        to_state=HHApplicationState.STALE,
                        reason="questionnaire_dom_changed_before_submit",
                    )
                    transition_application(
                        application_id=application_id,
                        to_state=HHApplicationState.NEEDS_HUMAN_REVIEW,
                        reason="new_review_required_before_submit",
                    )
                    return {
                        "verdict": "BLOCKED",
                        "submit_count": 0,
                        "reason": "Questionnaire changed on live page; submit blocked and reset to NEEDS_HUMAN_REVIEW",
                        "application_id": application_id,
                        "state": HHApplicationState.NEEDS_HUMAN_REVIEW.value,
                    }

        # Gate 4: Execute DOM submit click if evaluator provided
        if evaluate_fn is not None:
            try:
                js_submit = """(() => {
                    const btn = document.querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-submit"]');
                    if (!btn) return JSON.stringify({ok: false, reason: 'Submit button not found'});
                    if (btn.disabled) return JSON.stringify({ok: false, reason: 'Submit button disabled'});
                    btn.click();
                    return JSON.stringify({ok: true});
                })()"""
                raw = evaluate_fn(js_submit)
                res = json.loads(raw) if isinstance(raw, str) else raw
                if not res.get("ok"):
                    transition_application(
                        application_id=application_id,
                        to_state=HHApplicationState.FAILED,
                        reason=f"dom_submit_click_failed: {res.get('reason')}",
                    )
                    return {
                        "verdict": "FAILED",
                        "submit_count": 0,
                        "reason": f"DOM submit failed: {res.get('reason')}",
                        "application_id": application_id,
                        "state": HHApplicationState.FAILED.value,
                    }
            except Exception as e:
                transition_application(
                    application_id=application_id,
                    to_state=HHApplicationState.FAILED,
                    reason=f"evaluate_exception: {e}",
                )
                return {
                    "verdict": "FAILED",
                    "submit_count": 0,
                    "reason": f"Browser evaluate exception: {e}",
                    "application_id": application_id,
                    "state": HHApplicationState.FAILED.value,
                }

        # Step 5: Transition to SUBMITTED
        trans = transition_application(
            application_id=application_id,
            to_state=HHApplicationState.SUBMITTED,
            reason="human_confirmed_submission_completed",
            confirm_submit=True,
        )

        return {
            "verdict": "SUBMITTED",
            "submit_count": 1,
            "click_count": 1 if evaluate_fn else 0,
            "reason": "Submission completed successfully with explicit human confirmation",
            "application_id": application_id,
            "state": HHApplicationState.SUBMITTED.value,
            "transition_id": trans.transition_id,
        }
