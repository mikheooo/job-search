from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from . import config
from .db import (
    get_connection,
    init_db,
    get_submission,
    get_verification,
    list_verifications,
    get_all_submissions,
)
from .application_tracking import ApplicationStatus, get_application_status
from .application_review import ReviewStatus, get_application_review
from .browser_executor import get_browser_session, BrowserStatus

logger = logging.getLogger(__name__)


class RecoveryStatus(str, Enum):
    NO_ACTION = "NO_ACTION"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY_TO_RETRY = "READY_TO_RETRY"
    TERMINAL = "TERMINAL"


class RecoveryResult(BaseModel):
    vacancy_stable_id: str
    submission_id: Optional[str] = None
    current_tracking_status: Optional[str] = None
    recovery_status: RecoveryStatus
    reason: str
    warnings: List[str] = Field(default_factory=list)
    last_submission: Optional[Dict[str, Any]] = None
    last_verification: Optional[Dict[str, Any]] = None
    recommended_action: str

    model_config = {"extra": "forbid"}


def inspect_submission_state(vacancy_stable_id: str) -> RecoveryResult:
    """
    Deterministically determine submission state from existing records.
    Does NOT perform any browser actions or submissions.
    """
    init_db()

    # Get tracking status
    tracking = get_application_status(vacancy_stable_id)
    current_tracking_status = tracking.status.value if tracking and hasattr(tracking.status, 'value') else (str(tracking.status) if tracking else None)

    # Get latest submission
    submission_row = get_submission(vacancy_stable_id)
    submission_id = None
    last_submission = None
    if submission_row:
        try:
            # New schema: 0=vacancy_stable_id, 1=submission_id, 2=executor_version, 3=submission_json, 4=status, 5=submitted_at, 6=created_at, 7=updated_at
            sub_json = json.loads(submission_row[3]) if submission_row[3] else {}
            submission_id = submission_row[1]  # submission_id is at index 1
            last_submission = {
                "submission_id": submission_id,
                "status": submission_row[4],
                "submitted_at": submission_row[5],
                "created_at": submission_row[6],
                "updated_at": submission_row[7],
                "executor_version": submission_row[2],
            }
        except Exception:
            last_submission = {"raw": str(submission_row)}

    # Get latest verification
    ver = get_verification(vacancy_stable_id, submission_id) if submission_id else None
    last_verification = None
    if ver:
        last_verification = {
            "verification_status": ver.verification_status.value if hasattr(ver.verification_status, 'value') else str(ver.verification_status),
            "verified_at": ver.verified_at,
            "success_signal": ver.success_signal,
            "final_url": ver.final_url,
            "page_title": ver.page_title,
            "verification_version": ver.verification_version,
        }

    # Get review status
    review = get_application_review(vacancy_stable_id)
    review_status = review.status.value if review and hasattr(review.status, 'value') else (str(review.status) if review else None)

    # Get browser session status
    browser_sess = get_browser_session(vacancy_stable_id)
    browser_status = browser_sess.status.value if browser_sess and hasattr(browser_sess.status, 'value') else (str(browser_sess.status) if browser_sess else None)

    # Determine recovery status
    recovery_status, reason, recommended_action = _determine_recovery_status(
        current_tracking_status,
        last_submission,
        last_verification,
        review_status,
        browser_status,
    )

    warnings = []
    if not current_tracking_status:
        warnings.append("No application tracking record found")
    if not last_submission:
        warnings.append("No submission record found")
    if not last_verification and last_submission:
        warnings.append("Submission exists but no verification performed")

    return RecoveryResult(
        vacancy_stable_id=vacancy_stable_id,
        submission_id=submission_id,
        current_tracking_status=current_tracking_status,
        recovery_status=recovery_status,
        reason=reason,
        warnings=warnings,
        last_submission=last_submission,
        last_verification=last_verification,
        recommended_action=recommended_action,
    )


def _determine_recovery_status(
    tracking_status: Optional[str],
    submission: Optional[Dict],
    verification: Optional[Dict],
    review_status: Optional[str],
    browser_status: Optional[str],
) -> tuple[RecoveryStatus, str, str]:
    """Determine recovery status based on all available state."""

    # Terminal states - no action needed
    terminal_statuses = {"APPLIED", "REJECTED", "INTERVIEW", "OFFER", "WITHDRAWN"}
    if tracking_status in terminal_statuses:
        return (
            RecoveryStatus.TERMINAL,
            f"Tracking status is {tracking_status} (terminal)",
            "No action needed - application lifecycle complete",
        )

    # No tracking record
    if not tracking_status:
        if submission:
            return (
                RecoveryStatus.NEEDS_REVIEW,
                "Submission exists but no tracking record",
                "Run reconcile to create tracking from submission, then review",
            )
        return (
            RecoveryStatus.NO_ACTION,
            "No tracking or submission records",
            "Application not yet submitted - prepare and submit if desired",
        )

    # SUBMITTED states
    if tracking_status == "SUBMITTED":
        if not submission:
            return (
                RecoveryStatus.NEEDS_REVIEW,
                "Tracking says SUBMITTED but no submission record found",
                "Investigate data inconsistency - run reconcile",
            )

        if not verification:
            return (
                RecoveryStatus.NEEDS_VERIFICATION,
                "Submission exists but no verification performed",
                "Run verification to check submission result on the website",
            )

        ver_status = verification.get("verification_status")
        if ver_status == "VERIFIED":
            return (
                RecoveryStatus.NO_ACTION,
                "Verification confirmed VERIFIED - ready for APPLIED transition",
                "Run reconcile to transition tracking to APPLIED",
            )

        if ver_status == "AMBIGUOUS":
            return (
                RecoveryStatus.NEEDS_REVIEW,
                "Verification returned AMBIGUOUS - unclear if submission succeeded",
                "Manual review required - check website manually before retrying",
            )

        if ver_status == "FAILED":
            # FAILED can be retried if explicitly allowed, but default to NEEDS_REVIEW
            return (
                RecoveryStatus.NEEDS_REVIEW,
                "Verification returned FAILED - submission error detected",
                "Manual review required - check error details before retrying",
            )

        if ver_status == "BLOCKED":
            return (
                RecoveryStatus.NEEDS_REVIEW,
                "Verification returned BLOCKED - CAPTCHA/login/404 detected",
                "Manual review required - cannot auto-retry blocked submissions",
            )

        # Unknown verification status
        return (
            RecoveryStatus.NEEDS_REVIEW,
            f"Unknown verification status: {ver_status}",
            "Manual review required",
        )

    # READY_TO_APPLY - waiting for submit
    if tracking_status == "READY_TO_APPLY":
        if submission:
            return (
                RecoveryStatus.NEEDS_VERIFICATION,
                "Tracking is READY_TO_APPLY but submission record exists",
                "Run verification to check if previous submission succeeded",
            )
        return (
            RecoveryStatus.NO_ACTION,
            "Ready to apply - awaiting manual submit with --confirm-submit",
            "Submit when ready using: python -m ai_assistant.cli submit <id> --confirm-submit",
        )

    # Earlier states (DISCOVERED, ANALYZED)
    if tracking_status in ("DISCOVERED", "ANALYZED"):
        return (
            RecoveryStatus.NO_ACTION,
            f"Tracking status is {tracking_status} - not yet ready for submission",
            "Continue pipeline: deep analysis -> package preparation -> queue -> review -> submit",
        )

    # Unknown tracking status
    return (
        RecoveryStatus.NEEDS_REVIEW,
        f"Unknown tracking status: {tracking_status}",
        "Manual review required",
    )


def reconcile_submission_state(vacancy_stable_id: str) -> RecoveryResult:
    """
    Reconcile tracking status with verified submission state.
    Only transitions VERIFIED -> APPLIED.
    Does NOT perform any submissions.
    """
    init_db()

    result = inspect_submission_state(vacancy_stable_id)

    # Only act if we have VERIFIED verification and tracking is not already APPLIED
    if result.recovery_status == RecoveryStatus.NO_ACTION and result.last_verification:
        ver_status = result.last_verification.get("verification_status")
        if ver_status == "VERIFIED" and result.current_tracking_status != "APPLIED":
            from .application_tracking import verify_and_apply, get_application_status
            try:
                track = get_application_status(vacancy_stable_id)
                if track and track.status.value != "APPLIED":
                    verify_and_apply(vacancy_stable_id, "VERIFIED", note="Reconciled: verification confirmed VERIFIED")
                    # Re-check
                    new_track = get_application_status(vacancy_stable_id)
                    if new_track:
                        result.current_tracking_status = new_track.status.value if hasattr(new_track.status, 'value') else str(new_track.status)
            except Exception as e:
                logger.warning(f"Reconcile failed for {vacancy_stable_id}: {e}")
                result.warnings.append(f"Reconcile failed: {e}")

    return result


def get_submission_audit(vacancy_stable_id: str) -> List[Dict[str, Any]]:
    """
    Get chronological audit trail for a vacancy.
    Includes: reviews, browser preparations, submissions, verifications, tracking transitions.
    """
    init_db()
    conn = get_connection()
    cur = conn.cursor()

    events = []

    # Get review history
    try:
        cur.execute("""
            SELECT 'REVIEW' as type, created_at as timestamp,
                   status as status, note as detail
            FROM application_reviews
            WHERE vacancy_stable_id=?
            ORDER BY created_at ASC
        """, (vacancy_stable_id,))
        for row in cur.fetchall():
            events.append({
                "type": "REVIEW",
                "timestamp": row[1],
                "status": row[2],
                "detail": row[3],
            })
    except Exception:
        pass

    # Get browser preparation history
    try:
        cur.execute("""
            SELECT 'BROWSER_PREPARE' as type, created_at as timestamp,
                   status as status, url as detail
            FROM browser_preparations
            WHERE vacancy_stable_id=?
            ORDER BY created_at ASC
        """, (vacancy_stable_id,))
        for row in cur.fetchall():
            events.append({
                "type": "BROWSER_PREPARE",
                "timestamp": row[1],
                "status": row[2],
                "detail": f"URL: {row[3]}",
            })
    except Exception:
        pass

    # Get submission history - need to query all submissions
    try:
        cur.execute("""
            SELECT 'SUBMISSION' as type, submitted_at as timestamp,
                   status as status, submission_json as detail
            FROM application_submissions
            WHERE vacancy_stable_id=?
            ORDER BY submitted_at ASC
        """, (vacancy_stable_id,))
        for row in cur.fetchall():
            detail = row[3]
            try:
                sub_data = json.loads(detail) if detail else {}
                sub_id = sub_data.get("submission_id", "unknown")
                detail_str = f"submission_id: {sub_id}, error: {sub_data.get('error', 'none')}"
            except Exception:
                detail_str = str(detail)[:200]
            events.append({
                "type": "SUBMISSION",
                "timestamp": row[1],
                "status": row[2],
                "detail": detail_str,
            })
    except Exception:
        pass

    # Get verification history
    try:
        cur.execute("""
            SELECT 'VERIFICATION' as type, verified_at as timestamp,
                   verification_status as status, verification_json as detail
            FROM submission_verifications
            WHERE vacancy_stable_id=?
            ORDER BY verified_at ASC
        """, (vacancy_stable_id,))
        for row in cur.fetchall():
            detail = row[3]
            try:
                ver_data = json.loads(detail) if detail else {}
                success_signal = ver_data.get("success_signal", "none")
                detail_str = f"signal: {success_signal}, url: {ver_data.get('final_url', 'none')}"
            except Exception:
                detail_str = str(detail)[:200]
            events.append({
                "type": "VERIFICATION",
                "timestamp": row[1],
                "status": row[2],
                "detail": detail_str,
            })
    except Exception:
        pass

    # Get tracking status history
    try:
        cur.execute("""
            SELECT 'TRACKING' as type, changed_at as timestamp,
                   old_status || ' -> ' || new_status as status, note as detail
            FROM application_status_history
            WHERE vacancy_stable_id=?
            ORDER BY changed_at ASC
        """, (vacancy_stable_id,))
        for row in cur.fetchall():
            events.append({
                "type": "TRACKING",
                "timestamp": row[1],
                "status": row[2],
                "detail": row[3] or "",
            })
    except Exception:
        pass

    conn.close()

    # Sort all events chronologically
    events.sort(key=lambda e: e.get("timestamp", ""))
    return events