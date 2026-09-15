"""Single Source of Truth for Application Submission Evidence and State.

Reconciles evidence across all storage locations:
1. application_submissions (records and statuses)
2. submission_verifications (verification status)
3. hh_applications (state machine state)
4. application_tracking (lifecycle status)
5. Live DOM indicators (e.g. 'already responded' banner)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import db
from .application_tracking import get_application_status

logger = logging.getLogger(__name__)

# Statuses in application_submissions that indicate an application attempt exists that blocks submission
BLOCKED_SUBMISSION_STATUSES: set[str] = {
    "SUBMITTED",
    "CONFIRMED",
    "AMBIGUOUS",
    "AMBIGUOUS_POST_SUBMIT",
    "VERIFIED",
    "SUCCESS",
    "AUTO_SUBMITTED",
}

# Submission statuses that explicitly allow a retry (pre-click failures only)
RETRY_ALLOWED_SUBMISSION_STATUSES: set[str] = {
    "FAILED",
    "BLOCKED",
    "FAIL_CLOSED",
    "GATE_BLOCKED",
    "CANCELLED",
    "DRY_RUN",
    "FAILED_SAFE",
}

# Verification statuses that block submission
BLOCKED_VERIFICATION_STATUSES: set[str] = {
    "VERIFIED",
    "CONFIRMED",
    "AMBIGUOUS",
    "AMBIGUOUS_POST_SUBMIT",
}

# HH application states in hh_applications table that block submission
BLOCKED_HH_APPLICATION_STATES: set[str] = {
    "SUBMITTED",
    "VERIFIED",
    "COMPLETED",
    "APPLIED",
    "AMBIGUOUS",
}

# Application tracking statuses that are allowed before submission (whitelist)
ALLOWED_TRACKING_STATUSES: set[str] = {
    "DISCOVERED",
    "ANALYZED",
    "READY_TO_APPLY",
}


@dataclass
class SubmissionEvidence:
    vacancy_stable_id: str
    tracking_status: str | None = None
    hh_application_state: str | None = None
    submissions: list[dict[str, Any]] = field(default_factory=list)
    latest_verification_status: str | None = None
    dom_already_applied: bool = False
    claim_status: str | None = None
    # BLE001 finding #31: sources that could not be read. A source that
    # raised is not "no evidence" - it is "unknown", and unknown blocks.
    read_errors: list[str] = field(default_factory=list)

    @property
    def blocked_reasons(self) -> list[str]:
        reasons: list[str] = []
        # BLE001 finding #31: first, before any evidence-based reason, admit
        # that the picture is incomplete. Measured: with all five sources
        # raising, this property used to come back empty, so can_submit()
        # answered True and the duplicate-submission guard in
        # browser_executor let the run continue. A guard that answers
        # "go ahead" when it could not look is not a guard.
        if self.read_errors:
            reasons.append(
                "Submission evidence could not be read from "
                f"{len(self.read_errors)} source(s): " + "; ".join(self.read_errors)
            )
        if self.dom_already_applied:
            reasons.append("DOM live page indicates already responded to vacancy")

        if self.tracking_status and self.tracking_status not in ALLOWED_TRACKING_STATUSES:
            reasons.append(
                f"Tracking status '{self.tracking_status}' is not in allowed whitelist {sorted(ALLOWED_TRACKING_STATUSES)}"
            )

        if self.hh_application_state and self.hh_application_state in BLOCKED_HH_APPLICATION_STATES:
            reasons.append(f"HH application state is '{self.hh_application_state}' (blocks submission)")

        if self.claim_status and self.claim_status in ("SUBMITTED", "ATTEMPTING", "AMBIGUOUS", "FAILED_SAFE"):
            reasons.append(f"Submission claim is active with status '{self.claim_status}'")

        for sub in self.submissions:
            st = sub.get("status")
            if st in BLOCKED_SUBMISSION_STATUSES:
                reasons.append(
                    f"Existing submission record has status '{st}' (submission_id={sub.get('submission_id')})"
                )

        if self.latest_verification_status and self.latest_verification_status in BLOCKED_VERIFICATION_STATUSES:
            reasons.append(f"Submission verification status is '{self.latest_verification_status}'")

        return reasons

    @property
    def is_already_applied(self) -> bool:
        """True if ANY source indicates an existing submission, or if a source
        could not be read.

        BLE001 finding #31: the second half is deliberate. "I could not read
        the evidence" and "there is no evidence" are different answers, and
        only the caller's safety depends on telling them apart - so an
        unreadable source counts as blocking, not as clean.
        """
        return len(self.blocked_reasons) > 0

    @property
    def has_active_submitting_attempt(self) -> bool:
        """True if there is an attempt currently marked SUBMITTING or ATTEMPTING."""
        if self.claim_status == "ATTEMPTING":
            return True
        for sub in self.submissions:
            if sub.get("status") in ("SUBMITTING", "ATTEMPTING"):
                return True
        return False

    def can_submit(self) -> tuple[bool, str | None]:
        reasons = self.blocked_reasons
        if reasons:
            return False, "; ".join(reasons)
        return True, None


def get_submission_evidence(vacancy_stable_id: str, dom_already_applied: bool = False) -> SubmissionEvidence:
    """Collect evidence across all tables, fail-closed on DB errors.

    BLE001 finding #31: every source that raises is recorded in
    ``read_errors`` and surfaces through ``blocked_reasons``, so
    ``can_submit()`` refuses and ``is_already_applied`` stays True. The
    docstring claimed this all along; the code did the opposite - it logged
    a warning and carried on with an empty value, which is indistinguishable
    from "this vacancy was never applied to".
    """
    evidence = SubmissionEvidence(vacancy_stable_id=vacancy_stable_id, dom_already_applied=dom_already_applied)

    # 1. Tracking status
    try:
        track = get_application_status(vacancy_stable_id)
        if track:
            evidence.tracking_status = track.status.value if hasattr(track.status, "value") else str(track.status)
    except Exception as e:
        logger.warning("Failed to query tracking status for %s: %s", vacancy_stable_id, e)
        evidence.read_errors.append(f"application_tracking: {type(e).__name__}: {e}")

    # 2. application_submissions
    try:
        subs = db.get_all_submissions(vacancy_stable_id)
        for s in subs:
            evidence.submissions.append({
                "submission_id": s[1] if len(s) > 1 else None,
                "status": s[4] if len(s) > 4 else None,
                "submitted_at": s[5] if len(s) > 5 else None,
            })
    except Exception as e:
        logger.warning("Failed to query application_submissions for %s: %s", vacancy_stable_id, e)
        evidence.read_errors.append(f"application_submissions: {type(e).__name__}: {e}")

    # 3. submission_verifications
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT verification_status FROM submission_verifications WHERE vacancy_stable_id = ? ORDER BY verified_at DESC LIMIT 1",
            (vacancy_stable_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            evidence.latest_verification_status = str(row[0])
    except Exception as e:
        logger.warning("Failed to query submission_verifications for %s: %s", vacancy_stable_id, e)
        evidence.read_errors.append(f"submission_verifications: {type(e).__name__}: {e}")

    # 4. hh_applications
    try:
        hh_app = db.get_hh_application_by_vacancy(vacancy_stable_id)
        if hh_app and hh_app.get("state"):
            evidence.hh_application_state = str(hh_app.get("state"))
    except Exception as e:
        logger.warning("Failed to query hh_applications for %s: %s", vacancy_stable_id, e)
        evidence.read_errors.append(f"hh_applications: {type(e).__name__}: {e}")

    # 5. submission_claims
    try:
        claim = db.get_submission_claim(vacancy_stable_id)
        if claim and claim.get("status"):
            evidence.claim_status = str(claim.get("status"))
    except Exception as e:
        logger.warning("Failed to query submission_claims for %s: %s", vacancy_stable_id, e)
        evidence.read_errors.append(f"submission_claims: {type(e).__name__}: {e}")

    return evidence


def has_definite_submission(vacancy_stable_id: str) -> bool:
    """Return True if any submission evidence indicates this vacancy has already been applied/submitted."""
    return get_submission_evidence(vacancy_stable_id).is_already_applied
