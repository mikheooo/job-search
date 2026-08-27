"""Stage 20K: HH Controlled Real Submit + Read-Only Verification.

Executes exactly ONE el.click() on the confirmed submit button after ALL
gates pass. After click: read-only verification only. No retry, no second
click, no navigation.

Verdicts:
    SUBMITTED           - proven success via read-only DOM/URL
    SUBMISSION_UNKNOWN  - clicked but success cannot be proven
    FAILED              - clicked but explicit failure observed
    FAIL_CLOSED         - safety invariant violated (URL/DOM/fingerprint change)
    BLOCKED             - gate failure, zero mutations
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from .hh_human_submission import confirm_human_submission, preflight_with_human_confirmation
from .hh_submission import SubmissionStatus


class TrackedMutation(BaseModel):
    op_index: int = 0
    question_id: str = ""
    value: str = ""
    ok: bool = False
    reason: str = ""

    model_config = {"extra": "forbid"}


class ControlledSubmitReport(BaseModel):
    verdict: str = "BLOCKED"
    # SUBMITTED | SUBMISSION_UNKNOWN | FAILED | FAIL_CLOSED | BLOCKED | READY_TO_SUBMIT | NOTHING_TO_EXECUTE
    stop_reason: str = ""
    generated_at: str = ""
    vacancy_stable_id: str = ""
    review_id: str = ""
    fingerprint: str = ""
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    vacancy_before: Optional[str] = None
    vacancy_after: Optional[str] = None
    button_meta: Optional[Dict[str, Any]] = None
    navigation_count: int = 0
    click_count: int = 0
    submit_count: int = 0
    successful_submit: int = 0
    failed_submit: int = 0
    mutations: List[TrackedMutation] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    body_markers_found: List[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


# Read-only JS: check post-submit markers in body text.
_POST_SUBMIT_JS = """(() => {
    const text = (document.body ? document.body.innerText : '').slice(0, 5000);
    const markers = ["Вы откликнулись", "ваш отклик", "отклик отправлен",
                     "отклики и приглашения", "negotiations"];
    const found = [];
    const low = text.toLowerCase();
    for (const m of markers) {
        if (low.includes(m.toLowerCase())) found.push(m);
    }
    return JSON.stringify({found: found, url: location.href});
})()"""


def controlled_real_submit(
    review_store: Any,
    review_id: str,
    fingerprint: str,
    package: Any,
    plan: Any,
    orchestration_report: Any,
    evaluate_fn: Callable[[str], str],
    expected_url_markers: tuple = ("hh.ru", "applicant/vacancy_response"),
) -> ControlledSubmitReport:
    """Execute exactly ONE el.click() on the confirmed HH submit button.

    All gates must pass; any failure -> zero mutations. After the single
    click, performs read-only verification only.
    """
    report = ControlledSubmitReport(
        generated_at=datetime.utcnow().isoformat(),
        review_id=review_id,
        fingerprint=fingerprint,
        vacancy_stable_id=getattr(package, "vacancy_stable_id", "") or "",
    )

    # --- One-shot invariant: a review already submitted cannot click again ---
    from .hh_submission import _submitted_reviews
    if review_id in _submitted_reviews:
        report.verdict = "BLOCKED"
        report.stop_reason = "already submitted - one approved review allows at most one submit attempt"
        return report

    # --- Step 1: Explicit human confirmation ---
    conf_res = confirm_human_submission(
        review_store, review_id, fingerprint,
        getattr(package, "vacancy_stable_id", "") or "")
    if not conf_res.get("ok"):
        report.verdict = "BLOCKED"
        report.stop_reason = f"human confirmation gate: {conf_res.get('reason')}"
        report.review_reasons = list(getattr(package, "review_reasons", []) or [])
        return report

    # --- Step 2: Full read-only preflight (all 11 gates + button) ---
    from .hh_submission import preflight_submission
    pre = preflight_submission(
        review_store, review_id, fingerprint, package, plan=plan,
        orchestration=orchestration_report, evaluate_fn=evaluate_fn,
        expected_url_markers=expected_url_markers)

    report.url_before = pre.url_before
    report.button_meta = pre.button_meta
    report.vacancy_before = pre.vacancy_before
    report.vacancy_stable_id = pre.vacancy_stable_id or ""

    if pre.status != SubmissionStatus.READY_TO_SUBMIT:
        report.verdict = pre.status.value if hasattr(pre.status, 'value') else str(pre.status)
        report.stop_reason = pre.reason
        report.errors.extend([pre.reason])
        return report

    # --- Step 3: The single click ---
    from .hh_submission import _SUBMIT_CLICK_JS
    try:
        raw = evaluate_fn(_SUBMIT_CLICK_JS)
        res = json.loads(raw)
    except Exception as e:
        report.click_count = 0
        report.submit_count = 0
        report.failed_submit = 0
        report.verdict = "FAIL_CLOSED"
        report.stop_reason = f"submit evaluate error: {e}"
        report.errors.append(report.stop_reason)
        return report

    report.click_count = 1
    report.submit_count = 1

    # Mark this review as submitted (one-shot invariant).
    from .hh_submission import _submitted_reviews
    _submitted_reviews.add(review_id)

    if not res.get("ok"):
        report.failed_submit = 1
        report.verdict = "FAILED"
        report.stop_reason = res.get("reason") or "submit click reported failure"
        # URL after failed click
        try:
            raw = evaluate_fn("JSON.stringify({url: location.href})")
            report.url_after = json.loads(raw).get("url")
        except Exception:
            report.url_after = report.url_before
        report.vacancy_after = None
        return report

    report.successful_submit = 1

    # --- Step 4: Read-only verification (no further mutations) ---
    try:
        raw = evaluate_fn(_POST_SUBMIT_JS)
        post = json.loads(raw)
    except Exception as e:
        report.errors.append(f"post-submit read failed: {e}")
        post = {}

    report.url_after = post.get("url") or report.url_before
    report.body_markers_found = post.get("found") or []

    # URL change detection (read-only observation, not treated as success alone)
    if report.url_after != report.url_before:
        # URL changed - this could be normal redirect or dangerous change
        # Check if it went to a safe HH domain
        if "hh.ru" not in (report.url_after or "").lower():
            report.verdict = "FAIL_CLOSED"
            report.stop_reason = f"URL left hh.ru after submit: {report.url_after}"
            report.vacancy_after = None
            return report

    # Vacancy identity after submit
    from .hh_submission import _parse_vacancy_id
    report.vacancy_after = _parse_vacancy_id(report.url_after or "")

    # --- Verdict based on observable evidence ---
    if report.body_markers_found:
        report.verdict = "SUBMITTED"
        report.stop_reason = (f"success markers found: {report.body_markers_found}; "
                              "read-only DOM confirms submission")
    else:
        # Click succeeded but no proof -> UNKNOWN
        report.verdict = "SUBMISSION_UNKNOWN"
        report.stop_reason = ("submit click executed but success cannot be "
                              "proven via read-only DOM/URL")

    return report