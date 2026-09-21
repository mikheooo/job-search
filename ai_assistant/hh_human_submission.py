"""Stage 20J: HH Human-Confirmed Submission.

Separate explicit human confirmation layer on top of Stage 20I gated
submission. Submit is allowed ONLY after the human explicitly confirms
the current review/fingerprint/vacancy. No auto-confirmation.

Safety: same as 20I plus one-time confirmation, fingerprint/vacancy
re-check, no navigation, no login, no DB writes.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Callable

from .hh_submission import (
    SubmissionReport,
    SubmissionStatus,
)
from .hh_submission import (
    clear_submitted_reviews as _clear_submitted,
)

# review_id -> (fingerprint, vacancy_stable_id) that was human-confirmed
_human_confirmations: dict[str, tuple[str, str]] = {}


def clear_human_confirmations() -> None:
    _human_confirmations.clear()


def clear_all_submission_state() -> None:
    _human_confirmations.clear()
    _clear_submitted()


def confirm_human_submission(
    review_store: Any,
    review_id: str,
    fingerprint: str,
    vacancy_stable_id: str,
) -> dict[str, Any]:
    """Explicit human confirmation for a single future submit.

    Checks three things: the fingerprint, the approval state, and that the
    vacancy being confirmed is the one the review approved. It does NOT run
    the 11 preflight gates - that is preflight_submission(), on the submit
    path. (Before BLE001 finding #53 this docstring claimed it did, which the
    module had no way to do: no gate runner is imported here.)
    Stores a one-time confirmation that submit_application will consume.
    Returns {"ok": bool, "reason": str}.
    Never touches browser, DB, or network.
    """
    entry = review_store.get(review_id) if hasattr(review_store, "get") else None
    if entry is None:
        return {"ok": False, "reason": "unknown review_id"}
    stored_fp = entry.get("fingerprint", "")
    if stored_fp != fingerprint:
        return {"ok": False, "reason": "fingerprint mismatch (stale review)"}
    state = entry.get("state", "")
    if state != "HUMAN_APPROVED":
        return {"ok": False, "reason": f"review state is {state} (must be HUMAN_APPROVED)"}
    # vacancy must match the approved one
    #
    # BLE001 finding #53: this used to be `entry.get("gate") or {}` inside a
    # try/except that fell back to "" - and "" skips the comparison below, so a
    # review entry whose "gate" was not a mapping confirmed the wrong vacancy
    # and recorded "human confirmation recorded" for a comparison that never
    # ran. The `or {}` is what makes this different from finding #52: a FALSY
    # non-mapping ("", [], 0) is coerced to {} without raising, so the except
    # never fires and narrowing it closes nothing. The fix has to be
    # structural. Same rule as finding #32 in hh_submission.py, deliberately
    # identical so the two copies cannot drift apart: absent (None) is
    # legitimate - reviews written before gate data existed - and stays a skip
    # (recorded as such since finding #54, which closed the reachable half of
    # that skip); anything else is an anomaly and fails closed.
    gate_data = entry.get("gate")
    if gate_data is None:
        gate_vacancy = ""
    elif isinstance(gate_data, dict):
        gate_vacancy = gate_data.get("vacancy_stable_id") or ""
    else:
        return {
            "ok": False,
            "reason": (
                f"review gate record is unreadable ({type(gate_data).__name__}), "
                "so the approved vacancy cannot be confirmed"
            ),
        }
    if gate_vacancy:
        if gate_vacancy != vacancy_stable_id:
            return {"ok": False, "reason": f"vacancy mismatch vs approved review: {gate_vacancy} != {vacancy_stable_id}"}
        # The comparison ran and passed, so the record says nothing extra.
        reason = "human confirmation recorded"
    else:
        # BLE001 finding #54: there is no approved vacancy on record, so the
        # comparison above cannot be made. The shape is legitimate rather than
        # an anomaly - HumanReviewGate.vacancy_stable_id defaults to "", and
        # build_review_gate() writes it whenever neither the package nor the
        # form carries one - so it must NOT be refused; refusing blocks real
        # reviews. What it must not do is report a comparison that never ran.
        # The confirmation is recorded with that fact stated, which is what
        # finding #52 does to a gate that cannot measure language
        # (language_match_without_description): stop asserting a check, do not
        # block what there is nothing to block. Of the three options weighed in
        # the report, this is B; A refuses legitimate reviews and C is the false
        # record this whole triage is about.
        reason = (
            "human confirmation recorded "
            "(vacancy comparison not made: no approved vacancy on record)"
        )
    # One confirmation per review; re-confirmation overwrites (human re-affirms).
    _human_confirmations[review_id] = (fingerprint, vacancy_stable_id)
    return {"ok": True, "reason": reason}


def is_human_confirmed(review_id: str, fingerprint: str, vacancy_stable_id: str) -> bool:
    entry = _human_confirmations.get(review_id)
    if entry is None:
        return False
    fp, vac = entry
    return fp == fingerprint and vac == vacancy_stable_id


def preflight_with_human_confirmation(
    review_store: Any,
    review_id: str,
    fingerprint: str,
    package: Any,
    plan: Any,
    orchestration: Any,
    evaluate_fn: Callable[[str], str],
    expected_url_markers: tuple = ("hh.ru", "applicant/vacancy_response"),
) -> SubmissionReport:
    """Read-only preflight that also checks explicit human confirmation.

    Returns READY_TO_SUBMIT only if every gate passes including confirmation.
    Never mutates browser.
    """
    vacancy_stable_id = getattr(package, "vacancy_stable_id", "") or ""
    confirmed = _human_confirmations.get(review_id)
    if confirmed is None or confirmed != (fingerprint, vacancy_stable_id):
        report = SubmissionReport(
            review_id=review_id, fingerprint=fingerprint,
            vacancy_stable_id=vacancy_stable_id,
            status=SubmissionStatus.BLOCKED,
            reason="human confirmation required - call confirm_human_submission first",
        )
        return report
    from .hh_submission import preflight_submission as _preflight

    return _preflight(
        review_store, review_id, fingerprint, package, plan, orchestration,
        evaluate_fn, expected_url_markers=expected_url_markers)


def submit_with_human_confirmation(
    review_store: Any,
    review_id: str,
    fingerprint: str,
    package: Any,
    plan: Any,
    orchestration: Any,
    evaluate_fn: Callable[[str], str],
    expected_url_markers: tuple = ("hh.ru", "applicant/vacancy_response"),
) -> SubmissionReport:
    """Human-confirmed submit. Checks explicit confirmation, then delegates.

    Re-checks all 11 gates plus confirmation before the single click.
    One approved review -> max one submit attempt (consumes confirmation).
    Any gate failure -> 0 mutations.
    """
    # Gate: explicit human confirmation must exist
    vacancy_stable_id = getattr(package, "vacancy_stable_id", "") or ""
    confirmed = _human_confirmations.get(review_id)
    if confirmed is None or confirmed != (fingerprint, vacancy_stable_id):
        report = SubmissionReport(
            review_id=review_id, fingerprint=fingerprint,
            vacancy_stable_id=vacancy_stable_id,
            status=SubmissionStatus.BLOCKED,
            reason="human confirmation required - call confirm_human_submission first",
        )
        return report

    # Delegate to the Stage 20I gated submission (which re-checks all 11 gates,
    # URL, vacancy, button, fingerprint, etc., and does the single click).
    from .hh_submission import submit_application as _submit

    # Consume the one-time confirmation before submit (so a second call needs
    # a new explicit confirmation).
    del _human_confirmations[review_id]

    return _submit(
        review_store, review_id, fingerprint, package, plan, orchestration,
        evaluate_fn, expected_url_markers=expected_url_markers)
