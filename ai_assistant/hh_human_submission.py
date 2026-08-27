"""Stage 20J: HH Human-Confirmed Submission.

Separate explicit human confirmation layer on top of Stage 20I gated
submission. Submit is allowed ONLY after the human explicitly confirms
the current review/fingerprint/vacancy. No auto-confirmation.

Safety: same as 20I plus one-time confirmation, fingerprint/vacancy
re-check, no navigation, no login, no DB writes.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Set, Tuple

from .hh_submission import (
    SubmissionReport,
    SubmissionStatus,
    clear_submitted_reviews as _clear_submitted,
)

# review_id -> (fingerprint, vacancy_stable_id) that was human-confirmed
_human_confirmations: Dict[str, Tuple[str, str]] = {}


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
) -> Dict[str, Any]:
    """Explicit human confirmation for a single future submit.

    Checks the same 11 gates as preflight plus the approval state.
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
    gate_vacancy = ""
    try:
        gate_data = entry.get("gate") or {}
        gate_vacancy = gate_data.get("vacancy_stable_id") or ""
    except Exception:
        gate_vacancy = ""
    if gate_vacancy and gate_vacancy != vacancy_stable_id:
        return {"ok": False, "reason": f"vacancy mismatch vs approved review: {gate_vacancy} != {vacancy_stable_id}"}
    # One confirmation per review; re-confirmation overwrites (human re-affirms).
    _human_confirmations[review_id] = (fingerprint, vacancy_stable_id)
    return {"ok": True, "reason": "human confirmation recorded"}


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
