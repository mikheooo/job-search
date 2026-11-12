from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .application_review import get_application_review
from .application_tracking import get_application_status
from .browser_executor import get_browser_session
from .db import (
    get_connection,
    get_submission,
    get_verification,
    init_db,
)

logger = logging.getLogger(__name__)

AUDIT_INCOMPLETE = "AUDIT_INCOMPLETE"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_failure(kind: str, exc: Exception) -> dict[str, Any]:
    """Событие-маркер: категорию следа прочитать не удалось.

    BLE001 finding #30: раньше падение запроса проглатывалось
    (`except Exception: pass`). След молча терял целую категорию, а при отказе
    всех пяти запросов `get_submission_audit` возвращал пустой список — и CLI
    печатал «No audit events found.» с кодом 0. Вакансия с отправленным и
    подтверждённым откликом выглядела как вакансия, по которой вообще ничего не
    происходило, а частичный след выглядел полным.

    Это тот же класс, что №4: диагностика, которая отвечает «всё чисто», когда
    сама не смогла посмотреть.
    """
    logger.exception("submission audit: could not read %s records", kind)
    return {
        "type": AUDIT_INCOMPLETE,
        "timestamp": _now_iso(),
        "status": "INCOMPLETE",
        "detail": (
            f"{kind} records could not be read: {type(exc).__name__}: {exc}. "
            "The trail below is INCOMPLETE."
        ),
    }


class RecoveryStatus(str, Enum):
    NO_ACTION = "NO_ACTION"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY_TO_RETRY = "READY_TO_RETRY"
    TERMINAL = "TERMINAL"


class RecoveryResult(BaseModel):
    vacancy_stable_id: str
    submission_id: str | None = None
    current_tracking_status: str | None = None
    recovery_status: RecoveryStatus
    reason: str
    warnings: list[str] = Field(default_factory=list)
    last_submission: dict[str, Any] | None = None
    last_verification: dict[str, Any] | None = None
    recommended_action: str

    model_config = {"extra": "forbid"}


def _verification_record_is_unreadable(vacancy_stable_id: str, submission_id: str) -> bool:
    """True when a verification row exists for this submission but cannot be parsed.

    get_verification() answers None both for "there is no row" and for "there is
    a row that SubmissionVerification refuses", so its callers cannot tell a
    missing verification from a damaged one. BLE001 finding #55: the recovery
    report called a damaged record missing - "Submission exists but no
    verification performed" - and advised re-checking the result on the website,
    for a submission whose stored record says VERIFIED.

    This reads the column the way application_integrity._check_verification_json_records
    does: by SQL, independently of the model getter, choosing the same row
    (latest by verified_at) so the auditor and this report cannot disagree about
    which row they mean.

    The except is narrowed on purpose. json.JSONDecodeError and pydantic's
    ValidationError both descend from ValueError, and a non-text column raises
    TypeError; any other exception is not "this row is unreadable" but a bug in
    this helper, and it should be loud rather than be filed as a damaged record.
    """
    from .submission_verifier import SubmissionVerification

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT verification_json FROM submission_verifications "
            "WHERE vacancy_stable_id=? AND submission_id=? "
            "ORDER BY verified_at DESC LIMIT 1",
            (vacancy_stable_id, submission_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return False
    try:
        data = json.loads(row[0]) if row[0] else {}
        SubmissionVerification.model_validate(data)
    except (ValueError, TypeError):
        return True
    return False


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
    submission_payload_unreadable = False
    if submission_row:
        # BLE001 finding #65. submission_id is index 1 and owes nothing to the
        # payload at index 3, so it is read FIRST and the parse below can no
        # longer take it away. The old shape parsed index 3 inside the same try
        # that assigned submission_id, and its except discarded both - while the
        # parsed value was never read anywhere in this function, so the parse
        # was dead work whose only possible effect was to lose the key the
        # verification lookup is made of.
        #
        # Measured on the live state.db: 9 of 15 rows have a payload column that
        # is not JSON (an older save_submission() argument order put the
        # submission_id there and the payload in submitted_at), and four of them
        # have a VERIFIED verification on file that this report then described
        # as "no verification performed" - the sentence #55 forbade.
        submission_id = submission_row[1]  # submission_id is at index 1
        if submission_row[3]:
            try:
                json.loads(submission_row[3])
            except (ValueError, TypeError) as e:
                submission_payload_unreadable = True
                logger.warning(
                    "submission_recovery: submission payload for %s is not readable "
                    "(%s: %s); submission_id %r survives, because it is not made of it",
                    vacancy_stable_id, type(e).__name__, e, submission_id,
                )
        last_submission = {
            "submission_id": submission_id,
            "status": submission_row[4],
            "submitted_at": submission_row[5],
            "created_at": submission_row[6],
            "updated_at": submission_row[7],
            "executor_version": submission_row[2],
            "payload_unreadable": submission_payload_unreadable,
        }

    # Get latest verification
    ver = get_verification(vacancy_stable_id, submission_id) if submission_id else None
    # None means either "no row" or "row I cannot read". The difference decides
    # whether the report may say a verification was never performed (#55).
    verification_unreadable = bool(
        ver is None and submission_id
        and _verification_record_is_unreadable(vacancy_stable_id, submission_id)
    )
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
        verification_unreadable=verification_unreadable,
        submission_payload_unreadable=submission_payload_unreadable,
    )

    warnings = []
    if not current_tracking_status:
        warnings.append("No application tracking record found")
    if not last_submission:
        warnings.append("No submission record found")
    if verification_unreadable:
        warnings.append(
            "Verification record exists but cannot be read - its content is unknown"
        )
    elif submission_payload_unreadable:
        # BLE001 finding #65, the same shape as #55 one record earlier: the row
        # is there and this reader could not read it, so "no verification was
        # performed" is not a sentence it is entitled to say.
        warnings.append(
            "Submission record exists but its payload cannot be read - "
            "whether a verification exists is unknown"
        )
    elif not last_verification and last_submission:
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
    tracking_status: str | None,
    submission: dict | None,
    verification: dict | None,
    review_status: str | None,
    browser_status: str | None,
    verification_unreadable: bool = False,
    submission_payload_unreadable: bool = False,
) -> tuple[RecoveryStatus, str, str]:
    """Determine recovery status based on all available state.

    `verification` is None both when no verification was performed and when the
    stored one cannot be read; `verification_unreadable` carries the second case
    so the report does not assert the first (#55). The status stays
    NEEDS_VERIFICATION either way - an unreadable record must not look more
    settled than a missing one.

    `submission_payload_unreadable` carries the same distinction one record
    earlier (#65): the submission row is there, but its payload column cannot be
    parsed, so the reader has no business saying that no verification was
    performed. It says the record cannot be read instead. The status stays
    NEEDS_VERIFICATION for the same reason as above.
    """

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
            if verification_unreadable:
                return (
                    RecoveryStatus.NEEDS_VERIFICATION,
                    "Submission exists but its verification record cannot be read",
                    (
                        "Inspect the stored verification record - it exists but cannot "
                        "be parsed, so whether the submission succeeded is unknown. "
                        "Do not assume it was never verified"
                    ),
                )
            if submission_payload_unreadable:
                # BLE001 finding #65. The advice must not be "go and check the
                # website": the record is in the database and this reader could
                # not read it, so the honest action is to look at the record.
                return (
                    RecoveryStatus.NEEDS_VERIFICATION,
                    (
                        "Submission record cannot be read - whether its verification "
                        "exists is unknown"
                    ),
                    (
                        "Inspect the stored submission record - its payload cannot be "
                        "parsed, so a verification stored under a different key would be "
                        "invisible here. Do not assume it was never verified"
                    ),
                )
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
            from .application_tracking import get_application_status, verify_and_apply
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


def reconcile_ambiguous_submission(
    vacancy_stable_id: str,
    target_status: str = "FAILED",
    reason: str = "Reconciled with proof screenshot: apply button remained active and unsubmitted",
    actor: str = "operator_manual_audit",
) -> dict[str, Any]:
    """Reconcile an AMBIGUOUS submission to FAILED or APPLIED with audit proof."""
    from datetime import datetime, timezone
    from pathlib import Path
    import json
    from .application_tracking import ApplicationStatus, get_application_status, set_application_status
    from . import config, db

    init_db()
    clean_vid = str(vacancy_stable_id).replace("hh:", "")
    sid = f"hh:{clean_vid}"
    now_iso = datetime.now(timezone.utc).isoformat()

    shot_path = Path(config.PROJECT_ROOT) / "artifacts" / "applications" / f"hh-{clean_vid}" / "submission_success.png"
    proof_exists = shot_path.exists()

    # 1. Update application_tracking
    target_app_status = ApplicationStatus(target_status)
    note_text = f"Reconciled ({actor}) [{now_iso}]: {reason}. Screenshot proof: {'found' if proof_exists else 'not found'} ({shot_path})"
    set_application_status(
        sid,
        target_app_status,
        notes=note_text,
    )

    # 2. Update application_submissions if row exists
    with db.db_session(commit=True) as conn:
        row = conn.execute(
            "SELECT submission_id, submission_json FROM application_submissions WHERE vacancy_stable_id = ?",
            (sid,),
        ).fetchone()
        if row:
            sub_id, raw_json = row[0], row[1]
            try:
                payload = json.loads(raw_json) if raw_json else {}
            except Exception as je:
                logger.warning("Failed to parse submission_json for %s: %s", sid, je)
                payload = {}
            payload["record_status"] = target_status
            payload["post_reason"] = f"Reconciled: {reason}"
            payload["reconciled_at"] = now_iso
            payload["reconciled_by"] = actor
            conn.execute(
                """UPDATE application_submissions
                   SET status = ?, submission_json = ?, updated_at = ?
                   WHERE vacancy_stable_id = ?""",
                (target_status, json.dumps(payload, ensure_ascii=False), now_iso, sid),
            )

        # Update or record submission_verifications
        v_exists = conn.execute(
            "SELECT 1 FROM submission_verifications WHERE vacancy_stable_id = ?",
            (sid,),
        ).fetchone()
        if v_exists:
            conn.execute(
                """UPDATE submission_verifications
                   SET verification_status = ?, updated_at = ?
                   WHERE vacancy_stable_id = ?""",
                (target_status, now_iso, sid),
            )
        else:
            conn.execute(
                """INSERT INTO submission_verifications (
                       vacancy_stable_id, submission_id, verification_version,
                       verification_status, verification_json, verified_at,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid,
                    f"reconcile-{clean_vid}",
                    "v1_reconcile",
                    target_status,
                    json.dumps({"reason": reason, "actor": actor}, ensure_ascii=False),
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )

    # 3. Update preflight.json if present
    pf_path = Path(config.PROJECT_ROOT) / "artifacts" / "applications" / f"hh-{clean_vid}" / "preflight.json"
    if pf_path.exists():
        try:
            pf = json.loads(pf_path.read_text(encoding="utf-8"))
            pf["preflight_verdict"] = target_status
            if "submission" in pf and isinstance(pf["submission"], dict):
                pf["submission"]["status"] = target_status
                pf["submission"]["reconciled_at"] = now_iso
                pf["submission"]["reconciled_reason"] = reason
            pf_path.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as pe:
            logger.warning(f"Error updating preflight.json for {sid}: {pe}")

    # 4. Reconcile submission claim if exists
    try:
        db.reconcile_submission_claim(
            vacancy_stable_id=sid,
            new_status="FAILED_SAFE" if target_status == "FAILED" else target_status,
            reason=reason,
            actor=actor,
        )
    except Exception as ce:
        logger.warning("Failed to reconcile submission claim for %s: %s", sid, ce)

    return {
        "ok": True,
        "vacancy_stable_id": sid,
        "from_status": "AMBIGUOUS",
        "to_status": target_status,
        "proof_screenshot": str(shot_path) if proof_exists else None,
        "reason": reason,
        "reconciled_at": now_iso,
    }


def get_submission_audit(vacancy_stable_id: str) -> list[dict[str, Any]]:
    """
    Get chronological audit trail for a vacancy.
    Includes: reviews, browser preparations, submissions, verifications, tracking transitions.

    A category that could not be read is reported as an ``AUDIT_INCOMPLETE`` event
    rather than being dropped silently (BLE001 finding #30). Callers must treat a
    non-empty result containing that marker as an incomplete trail, not a clean one.
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
    except Exception as e:
        events.append(_audit_failure("REVIEW", e))

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
    except Exception as e:
        events.append(_audit_failure("BROWSER_PREPARE", e))

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
            except Exception as e:
                logger.warning(
                    "submission audit: %s detail is not JSON, showing raw text: %s",
                    "SUBMISSION", e,
                )
                detail_str = str(detail)[:200]
            events.append({
                "type": "SUBMISSION",
                "timestamp": row[1],
                "status": row[2],
                "detail": detail_str,
            })
    except Exception as e:
        events.append(_audit_failure("SUBMISSION", e))

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
            except Exception as e:
                logger.warning(
                    "submission audit: %s detail is not JSON, showing raw text: %s",
                    "VERIFICATION", e,
                )
                detail_str = str(detail)[:200]
            events.append({
                "type": "VERIFICATION",
                "timestamp": row[1],
                "status": row[2],
                "detail": detail_str,
            })
    except Exception as e:
        events.append(_audit_failure("VERIFICATION", e))

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
    except Exception as e:
        events.append(_audit_failure("TRACKING", e))

    conn.close()

    # Sort all events chronologically
    events.sort(key=lambda e: e.get("timestamp", ""))
    return events