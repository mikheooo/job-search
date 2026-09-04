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
import os
import re
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse, parse_qs

from pydantic import BaseModel, Field

from . import config
from .application_review import ReviewStatus, get_application_review
from .application_tracking import get_application_status
from .candidate_profile import CandidateProfile, load_candidate_profile
from .db import get_all_submissions

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
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        vals = qs.get("vacancyId") or []
        if vals:
            vid = vals[0].strip()
            host = (parsed.hostname or "").lower()
            if host == "hh.ru" or host.endswith(".hh.ru"):
                return vid if vid.isdigit() else None
            return vid
        match = re.search(r"/vacancy/(\d+)", parsed.path)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _vacancy_from_stable(vacancy_stable_id: str) -> Optional[str]:
    if not vacancy_stable_id or ":" not in vacancy_stable_id:
        return None
    source, part = vacancy_stable_id.split(":", 1)
    source = source.strip().lower()
    part = part.strip()
    if source == "hh":
        return part if part.isdigit() else None
    return part


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
    report.vacancy_before = vid_in_url or ""
    report.vacancy_stable_id = getattr(package, "vacancy_stable_id", "") or ""

    # Delegate read-only safety gates verification to HHSubmissionGates
    gate_res = HHSubmissionGates.check_readonly_gates(
        vacancy_stable_id=report.vacancy_stable_id,
        current_url=url_before,
        fingerprint=fingerprint,
        review_obj=entry,
    )
    if not gate_res.passed:
        if gate_res.failed_gate in (GateName.GATE_FINGERPRINT_MATCH, GateName.GATE_URL_DOMAIN, GateName.GATE_VACANCY_MATCH):
            report.status = SubmissionStatus.FAIL_CLOSED
        else:
            report.status = SubmissionStatus.BLOCKED
        report.reason = gate_res.reason
        return report

    # Also check against the review's vacancy (the approved one).
    gate_vacancy = ""
    try:
        gate_data = entry.get("gate") or {}
        gate_vacancy = gate_data.get("vacancy_stable_id") or ""
    except Exception:
        gate_vacancy = ""
    if gate_vacancy and gate_vacancy != report.vacancy_stable_id:
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
    success_markers = ("Р’С‹ РѕС‚РєР»РёРєРЅСѓР»РёСЃСЊ", "РІР°С€ РѕС‚РєР»РёРє", "РѕС‚РєР»РёРє РѕС‚РїСЂР°РІР»РµРЅ",
                       "negotiations", "РѕС‚РєР»РёРєРё Рё РїСЂРёРіР»Р°С€РµРЅРёСЏ")
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


class GateName(str, Enum):
    GATE_SUBMIT_ALLOWED = "submit_allowed"
    GATE_REVIEW_APPROVED = "review_approved"
    GATE_FINGERPRINT_MATCH = "fingerprint_match"
    GATE_URL_DOMAIN = "url_domain"
    GATE_VACANCY_MATCH = "vacancy_match"
    GATE_PROFILE_LOADED = "profile_loaded"
    GATE_COVER_LETTER_READY = "cover_letter_ready"
    GATE_NO_UNKNOWN_QUESTIONS = "no_unknown_questions"
    GATE_NOT_ALREADY_APPLIED = "not_already_applied"
    GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT = "no_previous_submission_attempt"
    GATE_HUMAN_CONFIRMED = "human_confirmed"


class GateCheckResult(BaseModel):
    passed: bool
    failed_gate: Optional[GateName] = None
    reason: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)


class HHSubmissionGates:
    ALLOWED_UNSUBMITTED_STATUSES = frozenset({"DISCOVERED", "ANALYZED", "READY_TO_APPLY"})
    RETRY_ALLOWED_SUBMISSION_STATUSES = frozenset({"FAILED", "BLOCKED", "FAIL_CLOSED", "GATE_BLOCKED", "CANCELLED", "DRY_RUN"})

    @classmethod
    def _check_review_gate(cls, vacancy_stable_id: str, review_obj: Optional[Any] = None) -> tuple[Optional[GateCheckResult], Optional[str], Optional[str]]:
        """Gate 2: GATE_REVIEW_APPROVED."""
        review = review_obj or get_application_review(vacancy_stable_id)
        if not review:
            return (
                GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_REVIEW_APPROVED,
                    reason=f"No review found for vacancy: {vacancy_stable_id}",
                ),
                None,
                None,
            )
        if isinstance(review, dict):
            rev_status = review.get("state") or review.get("status")
            expected_fp = review.get("fingerprint") or review.get("form_fingerprint")
            rev_id = review.get("review_id") or ""
        else:
            rev_status = review.status.value if hasattr(review.status, "value") else str(review.status)
            expected_fp = getattr(review, "form_fingerprint", None) or getattr(review, "fingerprint", None)
            rev_id = getattr(review, "review_id", "")

        if rev_status not in (ReviewStatus.APPROVED.value, "HUMAN_APPROVED", "APPROVED"):
            return (
                GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_REVIEW_APPROVED,
                    reason=f"Review status is {rev_status}, expected APPROVED or HUMAN_APPROVED",
                    details={"status": rev_status},
                ),
                None,
                None,
            )
        return None, expected_fp, rev_id

    @classmethod
    def _check_fingerprint_gate(
        cls,
        vacancy_stable_id: str,
        expected_fp: Optional[str],
        fingerprint: Optional[str] = None,
        form_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[GateCheckResult]:
        """Gate 3: GATE_FINGERPRINT_MATCH."""
        actual_fp = fingerprint
        if not actual_fp and form_snapshot:
            actual_fp = form_snapshot.get("fingerprint")
            if not actual_fp:
                from .application_review import compute_review_fingerprint
                if "package" in form_snapshot:
                    actual_fp = compute_review_fingerprint(vacancy_stable_id, form_snapshot["package"])
                elif "cover_letter" in form_snapshot or "answers" in form_snapshot:
                    actual_fp = compute_review_fingerprint(vacancy_stable_id, form_snapshot)

        if not expected_fp or not actual_fp or expected_fp != actual_fp:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_FINGERPRINT_MATCH,
                reason=f"Fingerprint mismatch: review={expected_fp}, page={actual_fp}",
                details={"expected": expected_fp, "actual": actual_fp},
            )
        return None

    @classmethod
    def _check_url_domain_gate(
        cls,
        current_url: str,
        live_page_result: Optional[Any] = None,
    ) -> Optional[GateCheckResult]:
        """Gate 4: GATE_URL_DOMAIN."""
        cur_url = current_url or (getattr(live_page_result, "current_url", "") if live_page_result else "")
        if live_page_result and not getattr(live_page_result, "is_ok", True):
            err_reason = getattr(live_page_result, "error_reason", "")
            if err_reason in ("CAPTCHA", "VACANCY_NOT_FOUND", "ACCESS_DENIED", "AUTH_REQUIRED"):
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_URL_DOMAIN,
                    reason=f"Live page blocked: {getattr(live_page_result, 'reason', '')}",
                    details={"error_reason": err_reason, "url": cur_url},
                )

        if not cur_url:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_URL_DOMAIN,
                reason="Missing or empty current URL",
            )
        try:
            parsed = urlparse(cur_url)
            host = (parsed.hostname or "").lower()
            if not host:
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_URL_DOMAIN,
                    reason=f"Cannot parse hostname from URL: {cur_url}",
                )
            if host != "hh.ru" and not host.endswith(".hh.ru"):
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_URL_DOMAIN,
                    reason=f"URL host '{host}' does not belong to hh.ru or *.hh.ru",
                    details={"hostname": host, "url": cur_url},
                )
        except Exception as e:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_URL_DOMAIN,
                reason=f"Invalid URL structure: {e}",
            )
        return None

    @classmethod
    def _check_vacancy_match_gate(
        cls,
        vacancy_stable_id: str,
        current_url: str,
        live_page_result: Optional[Any] = None,
    ) -> Optional[GateCheckResult]:
        """Gate 5: GATE_VACANCY_MATCH."""
        cur_url = current_url or (getattr(live_page_result, "current_url", "") if live_page_result else "")
        if live_page_result:
            if not getattr(live_page_result, "numeric_id_match", True) or getattr(live_page_result, "error_reason", "") == "WRONG_PAGE":
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_VACANCY_MATCH,
                    reason=f"Live page vacancy mismatch: {getattr(live_page_result, 'reason', 'ID mismatch')}",
                    details={"expected": vacancy_stable_id, "url": cur_url},
                )

        expected_job_id = _vacancy_from_stable(vacancy_stable_id)
        is_hh = vacancy_stable_id.startswith("hh:") or (urlparse(cur_url).hostname or "").endswith("hh.ru")
        if is_hh:
            if not expected_job_id or not expected_job_id.isdigit():
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_VACANCY_MATCH,
                    reason=f"source_job_id unavailable or not numeric for HH vacancy: {vacancy_stable_id}",
                )
        else:
            if not expected_job_id:
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_VACANCY_MATCH,
                    reason=f"source_job_id unavailable in vacancy_stable_id: {vacancy_stable_id}",
                )
        url_job_id = _parse_vacancy_id(cur_url)
        if not url_job_id or url_job_id != expected_job_id:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_VACANCY_MATCH,
                reason=f"URL {cur_url} does not match expected vacancy id {expected_job_id} (got {url_job_id})",
                details={"expected_job_id": expected_job_id, "url_job_id": url_job_id},
            )
        return None

    @classmethod
    def _check_submission_evidence_gate(
        cls,
        vacancy_stable_id: str,
        form_snapshot: Optional[Dict[str, Any]] = None,
        live_page_result: Optional[Any] = None,
    ) -> tuple[Optional[GateCheckResult], Any]:
        """Gate 9: GATE_NOT_ALREADY_APPLIED."""
        from .submission_state import get_submission_evidence
        dom_already_applied = bool(
            (form_snapshot.get("already_applied") or form_snapshot.get("already_responded"))
            if form_snapshot else False
        ) or bool(
            live_page_result and (
                getattr(live_page_result, "already_applied", False)
                or getattr(live_page_result, "already_responded", False)
            )
        )
        try:
            evidence = get_submission_evidence(vacancy_stable_id, dom_already_applied=dom_already_applied)
        except Exception as e:
            return (
                GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_NOT_ALREADY_APPLIED,
                    reason=f"Database error while querying submission evidence: {e}",
                ),
                None,
            )

        can_sub, block_reason = evidence.can_submit()
        if not can_sub:
            return (
                GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_NOT_ALREADY_APPLIED,
                    reason=f"Vacancy was already applied or cannot be re-applied: {block_reason}",
                    details={
                        "tracking_status": evidence.tracking_status,
                        "hh_application_state": evidence.hh_application_state,
                        "latest_verification_status": evidence.latest_verification_status,
                        "dom_already_applied": evidence.dom_already_applied,
                        "submissions": evidence.submissions,
                        "blocked_reasons": evidence.blocked_reasons,
                    },
                ),
                evidence,
            )
        return None, evidence

    @classmethod
    def _check_previous_attempt_gate(
        cls,
        vacancy_stable_id: str,
        rev_id: Optional[str],
        evidence: Any,
    ) -> Optional[GateCheckResult]:
        """Gate 10: GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT."""
        if vacancy_stable_id in _submitted_reviews or (rev_id and rev_id in _submitted_reviews):
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT,
                reason="Review or vacancy was already attempted in this session",
            )
        if evidence and getattr(evidence, "has_active_submitting_attempt", False):
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT,
                reason="Previous submission attempt is currently in progress (SUBMITTING)",
                details={"submissions": getattr(evidence, "submissions", [])},
            )
        return None

    @classmethod
    def check_readonly_gates(
        cls,
        vacancy_stable_id: str,
        current_url: str,
        fingerprint: Optional[str] = None,
        form_snapshot: Optional[Dict[str, Any]] = None,
        review_obj: Optional[Any] = None,
        live_page_result: Optional[Any] = None,
    ) -> GateCheckResult:
        """Read-only preflight gates (2, 3, 4, 5, 9, 10).

        Checks all non-mutating safety invariants without requiring cover
        letter readiness or explicit human confirmation.
        """
        # Gate 2: Review approved
        res_err, expected_fp, rev_id = cls._check_review_gate(vacancy_stable_id, review_obj)
        if res_err:
            return res_err

        # Gate 3: Fingerprint match
        fp_err = cls._check_fingerprint_gate(vacancy_stable_id, expected_fp, fingerprint, form_snapshot)
        if fp_err:
            return fp_err

        # Gate 4: URL domain
        url_err = cls._check_url_domain_gate(current_url, live_page_result)
        if url_err:
            return url_err

        # Gate 5: Vacancy match
        vac_err = cls._check_vacancy_match_gate(vacancy_stable_id, current_url, live_page_result)
        if vac_err:
            return vac_err

        # Gate 9: Not already applied
        sub_err, evidence = cls._check_submission_evidence_gate(vacancy_stable_id, form_snapshot, live_page_result)
        if sub_err:
            return sub_err

        # Gate 10: No previous submission attempt
        att_err = cls._check_previous_attempt_gate(vacancy_stable_id, rev_id, evidence)
        if att_err:
            return att_err

        return GateCheckResult(passed=True, reason="All readonly gates passed successfully")

    @classmethod
    def check_all_gates(
        cls,
        vacancy_stable_id: str,
        current_url: str,
        form_snapshot: Dict[str, Any],
        human_confirmed: bool = False,
        dry_run: bool = False,
        candidate_profile: Optional[CandidateProfile] = None,
        profile_path: Optional[str] = None,
        review_obj: Optional[Any] = None,
        live_page_result: Optional[Any] = None,
    ) -> GateCheckResult:
        # Gate 1: GATE_SUBMIT_ALLOWED (kill-switch safety latch)
        submit_allowed = (
            os.getenv("SUBMIT_ALLOWED", "").strip().lower() in ("1", "true", "yes")
            or bool(getattr(config, "SUBMIT_ALLOWED", False))
        )
        if not submit_allowed and not dry_run:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_SUBMIT_ALLOWED,
                reason="Submission is disabled by SUBMIT_ALLOWED configuration",
                details={"SUBMIT_ALLOWED": submit_allowed, "dry_run": dry_run},
            )

        # Gate 2: GATE_REVIEW_APPROVED
        rev_err, expected_fp, rev_id = cls._check_review_gate(vacancy_stable_id, review_obj)
        if rev_err:
            return rev_err

        # Gate 3: GATE_FINGERPRINT_MATCH
        fp_err = cls._check_fingerprint_gate(vacancy_stable_id, expected_fp, form_snapshot=form_snapshot)
        if fp_err:
            return fp_err

        # Gate 4: GATE_URL_DOMAIN
        url_err = cls._check_url_domain_gate(current_url, live_page_result)
        if url_err:
            return url_err

        # Gate 5: GATE_VACANCY_MATCH
        vac_err = cls._check_vacancy_match_gate(vacancy_stable_id, current_url, live_page_result)
        if vac_err:
            return vac_err

        # Gate 6: GATE_PROFILE_LOADED
        from . import candidate_profile as cp_mod
        profile = candidate_profile or (cp_mod.load_candidate_profile(profile_path) if profile_path else cp_mod.load_candidate_profile())
        if not profile:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_PROFILE_LOADED,
                reason="Candidate profile could not be loaded",
            )

        # Gate 7: GATE_COVER_LETTER_READY
        cover_letter = form_snapshot.get("cover_letter")
        if not cover_letter or len(str(cover_letter).strip()) < 10:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_COVER_LETTER_READY,
                reason="Cover letter is missing or too short (< 10 chars)",
                details={"length": len(str(cover_letter).strip()) if cover_letter else 0},
            )

        # Gate 8: GATE_NO_UNKNOWN_QUESTIONS
        fields = form_snapshot.get("fields", [])
        for f in fields:
            f_type = f.get("type", "")
            label = f.get("label", "")
            is_req = f.get("required", False)
            val = f.get("value")
            if val:
                continue
            if label and (is_req or f_type in ("textarea", "text", "radio", "checkbox", "select")):
                from .application_qa import QuestionAnswerGenerator
                from .hh_extractor import ApplicationQuestion, QuestionType, QuestionSource
                gen = QuestionAnswerGenerator(profile, resume_text="", deep=None, vacancy=None)
                q = ApplicationQuestion(
                    id=f.get("id") or "q_check",
                    label=label,
                    normalized_type=QuestionType.TEXT if f_type in ("textarea", "text") else QuestionType.UNKNOWN,
                    required=is_req,
                    source=QuestionSource.SCREENING,
                )
                ans = gen.generate(q)
                if ans.requires_review or not ans.answer:
                    return GateCheckResult(
                        passed=False,
                        failed_gate=GateName.GATE_NO_UNKNOWN_QUESTIONS,
                        reason=f"Question requires human review: {label}",
                        details={
                            "question": label,
                            "requires_review": ans.requires_review,
                            "reason": ans.reason,
                        },
                    )

        # Gate 9: GATE_NOT_ALREADY_APPLIED
        sub_err, evidence = cls._check_submission_evidence_gate(vacancy_stable_id, form_snapshot, live_page_result)
        if sub_err:
            return sub_err

        # Gate 10: GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT
        att_err = cls._check_previous_attempt_gate(vacancy_stable_id, rev_id, evidence)
        if att_err:
            return att_err

        # Gate 11: GATE_HUMAN_CONFIRMED
        if not human_confirmed and not dry_run:
            return GateCheckResult(
                passed=False,
                failed_gate=GateName.GATE_HUMAN_CONFIRMED,
                reason="Explicit human confirmation (--confirm-submit) required",
            )

        return GateCheckResult(passed=True, reason="All 11 gates passed successfully")
