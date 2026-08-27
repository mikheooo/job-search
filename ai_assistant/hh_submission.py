"""Stage 20I: HH Controlled Submission (gated, one-shot, fail-closed).

Hard gates (all must pass) before ANY browser mutation:
  1. review.status == HUMAN_APPROVED
  2. fingerprint matches approved fingerprint
  3. package.validation_status == VALID
  4. plan.status == VALID
  5. orchestration.verdict == VERIFIED
  6. failed == 0
  7. skipped == 0
  8. unresolved == []
  9. verification errors == 0
 10. current URL is the expected HH vacancy response page
 11. vacancy_stable_id matches approved vacancy

Safety:
- No login, no navigation (never goto), no vacancy switching, no form
  mutation before submit, no retry, max one submit per approved review.
- Any URL/DOM/fingerprint change after approval -> FAIL_CLOSED.
- If a gate fails -> 0 browser mutations.
- After submit the result is UNKNOWN unless success is proven via
  read-only DOM/URL inspection.

Uses only the real submit button found in the already-open form:
  data-qa="vacancy-response-submit-popup"
Before submit: read-only snapshot of the target (URL, vacancy, button meta,
fingerprint). After submit: read-only verification; URL change is recorded
but not treated as success by itself.

No DB writes, no cookies/storage access.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

# In-memory set of review_ids that have already had a submit attempt.
_submitted_reviews: Set[str] = set()

# JS: find the real HH submit button (read-only).
_SUBMIT_BTN_JS = """(() => {
    const el = document.querySelector('[data-qa="vacancy-response-submit-popup"]');
    if (!el) return JSON.stringify({found: false});
    return JSON.stringify({
        found: true,
        tag: el.tagName,
        type: el.getAttribute('type'),
        text: (el.innerText || '').trim().slice(0, 80),
        dataQa: el.getAttribute('data-qa'),
        disabled: !!el.disabled,
        visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
        cls: (el.className || '').toString().slice(0, 80)
    });
})()"""

# JS: click that exact button (only the submit mutation in this module).
_SUBMIT_CLICK_JS = """(() => {
    const el = document.querySelector('[data-qa="vacancy-response-submit-popup"]');
    if (!el) return JSON.stringify({ok: false, reason: 'submit button not found'});
    if (el.disabled) return JSON.stringify({ok: false, reason: 'submit button is disabled'});
    el.click();
    return JSON.stringify({ok: true});
})()"""

_URL_JS = "JSON.stringify({url: location.href})"


class SubmissionStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    BLOCKED = "BLOCKED"
    FAIL_CLOSED = "FAIL_CLOSED"
    FAILED = "FAILED"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"


class SubmissionReport(BaseModel):
    status: SubmissionStatus = SubmissionStatus.BLOCKED
    vacancy_stable_id: str = ""
    review_id: str = ""
    fingerprint: str = ""
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    vacancy_before: Optional[str] = None
    vacancy_after: Optional[str] = None
    button_meta: Optional[Dict[str, Any]] = None
    reason: str = ""
    navigation_count: int = 0
    click_count: int = 0
    submit_count: int = 0
    successful_submit: int = 0
    failed_submit: int = 0
    generated_at: str = ""

    model_config = {"extra": "forbid"}


def _parse_vacancy_id(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        vals = qs.get("vacancyId") or []
        if vals and vals[0].strip().isdigit():
            return vals[0].strip()
    except Exception:
        pass
    return None


def _vacancy_from_stable(vacancy_stable_id: str) -> Optional[str]:
    if not vacancy_stable_id or ":" not in vacancy_stable_id:
        return None
    part = vacancy_stable_id.split(":", 1)[1].strip()
    return part if part.isdigit() else None


def clear_submitted_reviews() -> None:
    _submitted_reviews.clear()


def preflight_submission(
    review_store: Any,
    review_id: str,
    fingerprint: str,
    package: Any,
    plan: Any,
    orchestration: Any,
    evaluate_fn: Callable[[str], str],
    expected_url_markers: tuple = ("hh.ru", "applicant/vacancy_response"),
) -> SubmissionReport:
    """Read-only preflight: checks all gates and the real submit button.

    Never mutates browser/DOM. Returns READY_TO_SUBMIT only if every gate
    passes and the submit button is found and enabled. Otherwise BLOCKED or
    FAIL_CLOSED with submit_count == 0.
    """
    report = SubmissionReport(
        review_id=review_id, fingerprint=fingerprint,
        generated_at=datetime.utcnow().isoformat())

    # Gate 1-2: review + fingerprint via store.
    entry = review_store.get(review_id) if hasattr(review_store, "get") else None
    if entry is None:
        report.status = SubmissionStatus.BLOCKED
        report.reason = "unknown review_id"
        return report
    stored_fp = entry.get("fingerprint", "")
    if stored_fp != fingerprint:
        report.status = SubmissionStatus.FAIL_CLOSED
        report.reason = "fingerprint mismatch (stale review)"
        report.fingerprint = stored_fp or fingerprint
        return report
    state = entry.get("state", "")
    if state != "HUMAN_APPROVED":
        report.status = SubmissionStatus.BLOCKED
        report.reason = f"review state is {state} (must be HUMAN_APPROVED)"
        return report
    # Gate: one approved review -> max one submit attempt.
    if review_id in _submitted_reviews:
        report.status = SubmissionStatus.BLOCKED
        report.reason = "already submitted - one approved review allows at most one submit attempt"
        return report

    # Gates 3-9: package / plan / orchestration.
    gate_reasons: List[str] = []
    pkg_status = getattr(package, "validation_status", "") or ""
    if pkg_status != "VALID":
        gate_reasons.append(f"package.validation_status is {pkg_status or 'UNKNOWN'} (must be VALID)")
    plan_status = getattr(plan, "status", "") or ""
    if plan_status != "VALID":
        gate_reasons.append(f"plan.status is {plan_status} (must be VALID)")
    if getattr(plan, "unresolved", None) and len(plan.unresolved) > 0:  # type: ignore[attr-defined]
        gate_reasons.append(f"{len(plan.unresolved)} unresolved field(s)")  # type: ignore[attr-defined]
    orch_verdict = getattr(orchestration, "verdict", "") or ""
    if orch_verdict != "VERIFIED":
        gate_reasons.append(f"orchestration.verdict is {orch_verdict} (must be VERIFIED)")
    if getattr(orchestration, "failed_operations", 0) != 0:
        gate_reasons.append(f"{orchestration.failed_operations} failed operation(s)")
    if getattr(orchestration, "skipped_operations", 0) != 0:
        gate_reasons.append(f"{orchestration.skipped_operations} skipped operation(s)")
    if getattr(orchestration, "errors", None) and len(orchestration.errors) > 0:  # type: ignore[attr-defined]
        gate_reasons.append(f"{len(orchestration.errors)} verification error(s)")  # type: ignore[attr-defined]
    if gate_reasons:
        report.status = SubmissionStatus.BLOCKED
        report.reason = "; ".join(gate_reasons)
        return report

    # Gate 10-11: URL + vacancy match (read-only).
    try:
        raw = evaluate_fn(_URL_JS)
        url_before = json.loads(raw).get("url") or ""
    except Exception as e:
        report.status = SubmissionStatus.FAIL_CLOSED
        report.reason = f"cannot read URL: {e}"
        return report
    report.url_before = url_before
    missing = [m for m in expected_url_markers if m.lower() not in url_before.lower()]
    if missing:
        report.status = SubmissionStatus.FAIL_CLOSED
        report.reason = f"URL guard: missing markers {missing} in {url_before}"
        return report
    vid_in_url = _parse_vacancy_id(url_before)
    expected_vid = _vacancy_from_stable(getattr(package, "vacancy_stable_id", "") or "")
    report.vacancy_before = vid_in_url or ""
    report.vacancy_stable_id = getattr(package, "vacancy_stable_id", "") or ""
    if expected_vid and vid_in_url and expected_vid != vid_in_url:
        report.status = SubmissionStatus.FAIL_CLOSED
        report.reason = f"vacancy mismatch: expected {expected_vid} but URL has {vid_in_url}"
        return report
    # Also check against the review's vacancy (the approved one).
    gate_vacancy = ""
    try:
        gate_data = entry.get("gate") or {}
        gate_vacancy = gate_data.get("vacancy_stable_id") or ""
    except Exception:
        gate_vacancy = ""
    if gate_vacancy and expected_vid and gate_vacancy != getattr(package, "vacancy_stable_id", ""):
        report.status = SubmissionStatus.FAIL_CLOSED
        report.reason = f"vacancy_stable_id mismatch vs approved review: {gate_vacancy} != {report.vacancy_stable_id}"
        return report

    # Find the real submit button (read-only).
    try:
        raw = evaluate_fn(_SUBMIT_BTN_JS)
        btn = json.loads(raw)
    except Exception as e:
        report.status = SubmissionStatus.BLOCKED
        report.reason = f"cannot find submit button: {e}"
        return report
    if not btn.get("found"):
        report.status = SubmissionStatus.BLOCKED
        report.reason = "submit button not found (data-qa=\"vacancy-response-submit-popup\")"
        return report
    report.button_meta = btn
    if btn.get("disabled"):
        report.status = SubmissionStatus.BLOCKED
        report.reason = "submit button is disabled"
        return report

    report.status = SubmissionStatus.READY_TO_SUBMIT
    report.reason = "all gates passed; submit button found and enabled"
    return report


def submit_application(
    review_store: Any,
    review_id: str,
    fingerprint: str,
    package: Any,
    plan: Any,
    orchestration: Any,
    evaluate_fn: Callable[[str], str],
    expected_url_markers: tuple = ("hh.ru", "applicant/vacancy_response"),
) -> SubmissionReport:
    """Gated, one-shot submission via the real HH submit button.

    Reuses preflight gates; on success clicks the button exactly once,
    then does a read-only verification. Never navigates, never logs in.
    """
    pre = preflight_submission(
        review_store, review_id, fingerprint, package, plan, orchestration,
        evaluate_fn, expected_url_markers=expected_url_markers)

    # If preflight is not READY, return its report as-is (0 mutations).
    if pre.status != SubmissionStatus.READY_TO_SUBMIT:
        # Map the preflight status to the submission report's blocked/fail-closed.
        report = SubmissionReport(
            status=pre.status,  # BLOCKED or FAIL_CLOSED
            vacancy_stable_id=pre.vacancy_stable_id,
            review_id=review_id, fingerprint=fingerprint,
            url_before=pre.url_before, url_after=pre.url_before,
            vacancy_before=pre.vacancy_before, vacancy_after=pre.vacancy_before,
            button_meta=pre.button_meta, reason=pre.reason,
            generated_at=datetime.utcnow().isoformat())
        return report

    # Mark this review as having had a submit attempt (one-shot).
    _submitted_reviews.add(review_id)

    report = SubmissionReport(
        status=SubmissionStatus.FAILED,
        vacancy_stable_id=pre.vacancy_stable_id,
        review_id=review_id, fingerprint=fingerprint,
        url_before=pre.url_before, button_meta=pre.button_meta,
        generated_at=datetime.utcnow().isoformat())

    # Click the real submit button (the ONLY browser mutation in this module).
    try:
        raw = evaluate_fn(_SUBMIT_CLICK_JS)
        res = json.loads(raw)
    except Exception as e:
        report.reason = f"submit click failed: {e}"
        report.failed_submit = 1
        return report

    report.click_count = 1
    report.submit_count = 1
    if not res.get("ok"):
        report.status = SubmissionStatus.FAILED
        report.reason = res.get("reason") or "submit click failed"
        report.failed_submit = 1
        # url_after stays as before (no navigation observed yet)
        report.url_after = report.url_before
        report.vacancy_after = report.vacancy_before
        return report

    report.successful_submit = 1

    # Read-only verification after submit: URL + vacancy.
    try:
        raw = evaluate_fn(_URL_JS)
        url_after = json.loads(raw).get("url") or ""
    except Exception:
        url_after = report.url_before or ""
    report.url_after = url_after
    report.vacancy_after = _parse_vacancy_id(url_after) or ""

    # If URL changed (common after HH submit: redirect to negotiations/vacancy),
    # record it but do not treat as success by itself.
    # Try to prove success via read-only DOM markers.
    success_markers = ("Вы откликнулись", "ваш отклик", "отклик отправлен",
                       "negotiations", "отклики и приглашения")
    try:
        # Generic body-text check (read-only).
        probe = json.loads(evaluate_fn(
            "JSON.stringify({text: (document.body ? document.body.innerText : '').slice(0, 3000)})"))
        body_head = (probe.get("text") or "").lower()
    except Exception:
        body_head = ""

    proven = any(m in body_head for m in success_markers) or (url_after != report.url_before and "negotiations" in (url_after or "").lower())
    # Also consider a vacancy-change as a navigation signal, but not proof of success.

    if proven:
        report.status = SubmissionStatus.SUBMITTED
        report.reason = "submit click succeeded and success marker observed in read-only DOM/URL"
    else:
        report.status = SubmissionStatus.SUBMISSION_UNKNOWN
        report.reason = ("submit click succeeded but success cannot be proven via "
                         "read-only DOM/URL - treating as SUBMISSION_UNKNOWN")

    return report
