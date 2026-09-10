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
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger("ai_assistant.hh_submission")

from . import config
from .application_review import ReviewStatus, get_application_review
from .candidate_profile import CandidateProfile

# In-memory set of review_ids that have already had a submit attempt.
_submitted_reviews: set[str] = set()

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
_SUBMIT_CLICK_JS = """// hh_submit_click
(() => {
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
    url_before: str | None = None
    url_after: str | None = None
    vacancy_before: str | None = None
    vacancy_after: str | None = None
    button_meta: dict[str, Any] | None = None
    reason: str = ""
    navigation_count: int = 0
    click_count: int = 0
    submit_count: int = 0
    successful_submit: int = 0
    failed_submit: int = 0
    generated_at: str = ""

    model_config = {"extra": "forbid"}


def _parse_vacancy_id(url: str) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        is_hh = host == "hh.ru" or host.endswith(".hh.ru")
        qs = parse_qs(parsed.query)
        vals = qs.get("vacancyId") or []
        if vals:
            vid = vals[0].strip()
            if is_hh:
                return vid if vid.isdigit() else None
            return vid
        match = re.search(r"/vacancy/(\d+)" if is_hh else r"/vacancy/([^/?#]+)", parsed.path)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return None


def _vacancy_from_stable(vacancy_stable_id: str) -> str | None:
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

    # Gate 0 - the kill switch and SUBMIT_ALLOWED.
    #
    # BLE001 finding #19: every path that physically clicks the real HH submit
    # button delegates its gatekeeping to this function - submit_application(),
    # hh_controlled_submit.controlled_real_submit() and the auto-apply runner -
    # and none of them checked the emergency stop. Measured: with
    # SUBMIT_ALLOWED=false AND system_settings.submit_paused=1 (exactly what
    # the Telegram "stop" command writes), run_auto_apply still clicked:
    # verdict=SUBMITTED, submit_count=1, dom.clicks=1.
    #
    # Why it was invisible: this path delegates further to
    # check_readonly_gates(), which is gates 2-10 only. Gate 1 lives in
    # check_all_gates(), which this path never calls. So both kill switches
    # guarded execute_hh_submission() and nothing else.
    #
    # Finding #18: a kill-switch read that raises must not be read as "off".
    submit_allowed = bool(config.submit_allowed())
    try:
        from . import db

        is_paused = db.is_submit_paused()
    except Exception as e:  # noqa: BLE001
        logger.error(
            "cannot read the submission kill switch during preflight - "
            "failing closed: %s",
            e,
        )
        report.status = SubmissionStatus.FAIL_CLOSED
        report.reason = (
            f"Cannot read the submission kill switch - refusing to submit: "
            f"{type(e).__name__}: {e}"
        )
        return report

    if is_paused:
        report.status = SubmissionStatus.BLOCKED
        report.reason = "Submission paused by kill switch (system_settings.submit_paused=1)"
        return report
    if not submit_allowed:
        report.status = SubmissionStatus.BLOCKED
        report.reason = "Submission is disabled by SUBMIT_ALLOWED configuration"
        return report

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
    gate_reasons: list[str] = []
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
    failed_gate: GateName | None = None
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    gate_results: dict[str, dict[str, Any]] = Field(default_factory=dict)


class HHSubmissionGates:
    ALLOWED_UNSUBMITTED_STATUSES = frozenset({"DISCOVERED", "ANALYZED", "READY_TO_APPLY"})
    RETRY_ALLOWED_SUBMISSION_STATUSES = frozenset({"FAILED", "BLOCKED", "FAIL_CLOSED", "GATE_BLOCKED", "CANCELLED", "DRY_RUN"})

    @classmethod
    def _check_review_gate(
        cls,
        vacancy_stable_id: str,
        review_obj: Any | None = None,
        approval: Any | None = None,
    ) -> tuple[GateCheckResult | None, str | None, str | None]:
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

        is_approved = rev_status in (ReviewStatus.APPROVED.value, "HUMAN_APPROVED", "APPROVED")
        if not is_approved and approval and getattr(approval, "source", "") == "policy":
            is_approved = True

        if not is_approved:
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
        expected_fp: str | None,
        fingerprint: str | None = None,
        form_snapshot: dict[str, Any] | None = None,
    ) -> GateCheckResult | None:
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
        live_page_result: Any | None = None,
    ) -> GateCheckResult | None:
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
            parsed = urlparse(cur_url if "://" in cur_url else f"https://{cur_url}")
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
        live_page_result: Any | None = None,
    ) -> GateCheckResult | None:
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

        parsed_host = ""
        if cur_url:
            try:
                parsed_host = (urlparse(cur_url if "://" in cur_url else f"https://{cur_url}").hostname or "").lower()
            except Exception:
                pass
        is_hh = vacancy_stable_id.startswith("hh:") or parsed_host == "hh.ru" or parsed_host.endswith(".hh.ru")
        expected_job_id = _vacancy_from_stable(vacancy_stable_id)
        if is_hh:
            if not expected_job_id or not expected_job_id.isdigit():
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_VACANCY_MATCH,
                    reason=f"source_job_id unavailable or not numeric for HH vacancy: {vacancy_stable_id}",
                    details={"expected_job_id": expected_job_id, "vacancy_stable_id": vacancy_stable_id},
                )
        else:
            if not expected_job_id:
                return GateCheckResult(
                    passed=False,
                    failed_gate=GateName.GATE_VACANCY_MATCH,
                    reason=f"source_job_id unavailable in vacancy_stable_id: {vacancy_stable_id}",
                    details={"expected_job_id": expected_job_id, "vacancy_stable_id": vacancy_stable_id},
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
        form_snapshot: dict[str, Any] | None = None,
        live_page_result: Any | None = None,
    ) -> tuple[GateCheckResult | None, Any]:
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
        rev_id: str | None,
        evidence: Any,
    ) -> GateCheckResult | None:
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
        fingerprint: str | None = None,
        form_snapshot: dict[str, Any] | None = None,
        review_obj: Any | None = None,
        live_page_result: Any | None = None,
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
        form_snapshot: dict[str, Any],
        human_confirmed: bool = False,
        approval: Any | None = None,
        dry_run: bool = False,
        candidate_profile: CandidateProfile | None = None,
        profile_path: str | None = None,
        review_obj: Any | None = None,
        live_page_result: Any | None = None,
    ) -> GateCheckResult:
        gate_results: dict[str, dict[str, Any]] = {}

        # Gate 1: GATE_SUBMIT_ALLOWED (kill-switch safety latch)
        # BLE001 finding #8: this used to be `env OR config`, and config froze
        # the .env value at import time, so `SUBMIT_ALLOWED=false` in the real
        # environment could never turn submission off. config.submit_allowed()
        # re-reads at call time and lets an explicit "off" win.
        submit_allowed = bool(config.submit_allowed())
        # BLE001 finding #18: this used to be `except Exception: pass`, which
        # left is_paused False when the kill-switch lookup raised. So an
        # emergency stop that could not be read was treated as "not paused",
        # and the audit record below still said "SUBMIT_ALLOWED enabled" -
        # indistinguishable from a real pass. Measured: with is_submit_paused()
        # raising, check_all_gates returned passed=True, "All 11 gates passed
        # successfully".
        #
        # The same module already treats this exact call as fatal 400 lines
        # below: the pre-click check at `if db.is_submit_paused():` has no
        # guard at all, so there a read failure aborts the submission. Same
        # call, opposite policy, one screen apart. A latch whose failure mode
        # is "submit anyway" is not a latch - fail closed, and say why.
        is_paused = False
        kill_switch_error = None
        try:
            from . import db

            is_paused = db.is_submit_paused()
        except Exception as e:  # noqa: BLE001
            kill_switch_error = f"{type(e).__name__}: {e}"
            logger.error(
                "cannot read the submission kill switch for %s - failing closed: %s",
                vacancy_stable_id,
                kill_switch_error,
            )

        if kill_switch_error:
            g1_pass = False
            g1_reason = (
                "Cannot read the submission kill switch - refusing to submit: "
                f"{kill_switch_error}"
            )
        elif is_paused:
            g1_pass = False
            g1_reason = "Submission paused by kill switch (system_settings.submit_paused=1)"
        else:
            g1_pass = submit_allowed or dry_run
            g1_reason = "SUBMIT_ALLOWED enabled" if submit_allowed else ("Bypassed (dry-run mode)" if dry_run else "Submission is disabled by SUBMIT_ALLOWED configuration")

        gate_results[GateName.GATE_SUBMIT_ALLOWED.value] = {
            "passed": g1_pass,
            "reason": g1_reason,
        }

        # Gate 2: GATE_REVIEW_APPROVED
        rev_err, expected_fp, rev_id = cls._check_review_gate(vacancy_stable_id, review_obj, approval=approval)
        gate_results[GateName.GATE_REVIEW_APPROVED.value] = {
            "passed": rev_err is None,
            "reason": "Review approved" if rev_err is None else rev_err.reason,
        }

        # Gate 3: GATE_FINGERPRINT_MATCH
        fp_err = cls._check_fingerprint_gate(vacancy_stable_id, expected_fp, form_snapshot=form_snapshot)
        gate_results[GateName.GATE_FINGERPRINT_MATCH.value] = {
            "passed": fp_err is None,
            "reason": "Fingerprint matches" if fp_err is None else fp_err.reason,
        }

        # Gate 4: GATE_URL_DOMAIN
        url_err = cls._check_url_domain_gate(current_url, live_page_result)
        gate_results[GateName.GATE_URL_DOMAIN.value] = {
            "passed": url_err is None,
            "reason": "URL and domain verified" if url_err is None else url_err.reason,
        }

        # Gate 5: GATE_VACANCY_MATCH
        vac_err = cls._check_vacancy_match_gate(vacancy_stable_id, current_url, live_page_result)
        gate_results[GateName.GATE_VACANCY_MATCH.value] = {
            "passed": vac_err is None,
            "reason": "Vacancy ID matches" if vac_err is None else vac_err.reason,
        }

        # Gate 6: GATE_PROFILE_LOADED
        from . import candidate_profile as cp_mod
        profile = candidate_profile or (cp_mod.load_candidate_profile(profile_path) if profile_path else cp_mod.load_candidate_profile())
        g6_pass = profile is not None
        gate_results[GateName.GATE_PROFILE_LOADED.value] = {
            "passed": g6_pass,
            "reason": "Candidate profile loaded" if g6_pass else "Candidate profile could not be loaded",
        }

        # Gate 7: GATE_COVER_LETTER_READY
        cover_letter = form_snapshot.get("cover_letter")
        g7_pass = bool(cover_letter and len(str(cover_letter).strip()) >= 10)
        gate_results[GateName.GATE_COVER_LETTER_READY.value] = {
            "passed": g7_pass,
            "reason": "Cover letter ready" if g7_pass else "Cover letter is missing or too short (< 10 chars)",
        }

        # Gate 8: GATE_NO_UNKNOWN_QUESTIONS
        g8_err = None
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
                from .hh_extractor import (
                    ApplicationQuestion,
                    QuestionSource,
                    QuestionType,
                )
                gen = QuestionAnswerGenerator(profile, resume_text="", deep=None, vacancy=None) if profile else None
                if gen:
                    q = ApplicationQuestion(
                        id=f.get("id") or "q_check",
                        label=label,
                        normalized_type=QuestionType.TEXT if f_type in ("textarea", "text") else QuestionType.UNKNOWN,
                        required=is_req,
                        source=QuestionSource.SCREENING,
                    )
                    ans = gen.generate(q)
                    if ans.requires_review or not ans.answer:
                        g8_err = GateCheckResult(
                            passed=False,
                            failed_gate=GateName.GATE_NO_UNKNOWN_QUESTIONS,
                            reason=f"Question requires human review: {label}",
                            details={"question": label, "requires_review": ans.requires_review, "reason": ans.reason},
                        )
                        break
        gate_results[GateName.GATE_NO_UNKNOWN_QUESTIONS.value] = {
            "passed": g8_err is None,
            "reason": "All questions resolved" if g8_err is None else g8_err.reason,
        }

        # Gate 9: GATE_NOT_ALREADY_APPLIED
        sub_err, evidence = cls._check_submission_evidence_gate(vacancy_stable_id, form_snapshot, live_page_result)
        gate_results[GateName.GATE_NOT_ALREADY_APPLIED.value] = {
            "passed": sub_err is None,
            "reason": "No previous application detected" if sub_err is None else sub_err.reason,
        }

        # Gate 10: GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT
        att_err = cls._check_previous_attempt_gate(vacancy_stable_id, rev_id, evidence)
        gate_results[GateName.GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT.value] = {
            "passed": att_err is None,
            "reason": "No previous submission attempt in session" if att_err is None else att_err.reason,
        }

        # Gate 11: GATE_HUMAN_CONFIRMED
        g11_pass = (approval is not None) or human_confirmed or dry_run
        gate_results[GateName.GATE_HUMAN_CONFIRMED.value] = {
            "passed": g11_pass,
            "reason": (
                "Approved by policy"
                if (approval and getattr(approval, "source", "") == "policy")
                else ("Human confirmed" if human_confirmed else ("Bypassed (dry-run mode)" if dry_run else "Explicit approval or human confirmation required"))
            ),
        }

        # Sequential fail-closed evaluation preserving priority order
        if not g1_pass:
            return GateCheckResult(passed=False, failed_gate=GateName.GATE_SUBMIT_ALLOWED, reason=g1_reason, details={"SUBMIT_ALLOWED": submit_allowed, "dry_run": dry_run, "kill_switch_error": kill_switch_error}, gate_results=gate_results)
        if rev_err:
            return GateCheckResult(passed=False, failed_gate=rev_err.failed_gate, reason=rev_err.reason, details=rev_err.details, gate_results=gate_results)
        if fp_err:
            return GateCheckResult(passed=False, failed_gate=fp_err.failed_gate, reason=fp_err.reason, details=fp_err.details, gate_results=gate_results)
        if url_err:
            return GateCheckResult(passed=False, failed_gate=url_err.failed_gate, reason=url_err.reason, details=url_err.details, gate_results=gate_results)
        if vac_err:
            return GateCheckResult(passed=False, failed_gate=vac_err.failed_gate, reason=vac_err.reason, details=vac_err.details, gate_results=gate_results)
        if not g6_pass:
            return GateCheckResult(passed=False, failed_gate=GateName.GATE_PROFILE_LOADED, reason="Candidate profile could not be loaded", gate_results=gate_results)
        if not g7_pass:
            return GateCheckResult(passed=False, failed_gate=GateName.GATE_COVER_LETTER_READY, reason="Cover letter is missing or too short (< 10 chars)", details={"length": len(str(cover_letter).strip()) if cover_letter else 0}, gate_results=gate_results)
        if g8_err:
            return GateCheckResult(passed=False, failed_gate=g8_err.failed_gate, reason=g8_err.reason, details=g8_err.details, gate_results=gate_results)
        if sub_err:
            return GateCheckResult(passed=False, failed_gate=sub_err.failed_gate, reason=sub_err.reason, details=sub_err.details, gate_results=gate_results)
        if att_err:
            return GateCheckResult(passed=False, failed_gate=att_err.failed_gate, reason=att_err.reason, details=att_err.details, gate_results=gate_results)
        if not g11_pass:
            return GateCheckResult(passed=False, failed_gate=GateName.GATE_HUMAN_CONFIRMED, reason="Explicit human confirmation (--confirm-submit) required", gate_results=gate_results)

        return GateCheckResult(passed=True, reason="All 11 gates passed successfully", gate_results=gate_results)


@dataclass
class SubmissionExecutionResult:
    ok: bool = False
    status: str = "BLOCKED"  # SUBMITTED, DRY_RUN_OK, ALREADY_APPLIED, BLOCKED, FAIL_CLOSED, FAILED
    reason: str = ""
    vacancy_stable_id: str = ""
    submit_count: int = 0
    gate_check_result: GateCheckResult | None = None
    live_page_result: Any | None = None
    verification_status: str | None = None
    submission_id: str | None = None


def execute_hh_submission(
    vacancy_stable_id: str,
    evaluate_fn: Callable[[str], str] | None = None,
    human_confirmed: bool = False,
    approval: Any | None = None,
    dry_run: bool = False,
    candidate_profile: CandidateProfile | None = None,
    profile_path: str | None = None,
    submission_id: str | None = None,
    sync_hh_application: bool = True,
) -> SubmissionExecutionResult:
    """Unified entry point for HeadHunter submissions across all execution paths.

    Enforces:
    1. Live DOM inspection via check_live_page
    2. Short-circuit and DB update if ALREADY_APPLIED
    3. Full snapshot extraction and dynamic package fingerprint computation
    4. Gating via HHSubmissionGates.check_all_gates (all 11 gates)
    5. Safe exit if dry_run (submit_count == 0)
    6. Single physical click if human_confirmed and SUBMIT_ALLOWED
    7. Post-submit verification and synchronized DB state update

    Args:
        vacancy_stable_id: Stable vacancy identifier (e.g. 'hh:12345678').
        evaluate_fn: Synchronous JS evaluation function on the active CDP browser session.
        human_confirmed: Explicit human confirmation to submit.
        dry_run: Read-only simulation mode (submit_count == 0).
        candidate_profile: Optional preloaded candidate profile.
        profile_path: Optional candidate profile JSON path.
        submission_id: Optional unique submission run ID.
        sync_hh_application: If True (default for Path A / standalone CLI submit), directly
            updates the `hh_applications` DB record to SUBMITTED or AMBIGUOUS_POST_SUBMIT.
            If False (used by Stage 46 Controlled Application Runner / Path B), suppresses
            direct updates to `hh_applications`. This preserves the runner's state machine
            invariant: the runner transitions application states strictly via `transition_application()`
            with complete audit trail, timestamping, and evidence payloads, preventing duplicate or
            out-of-order state mutations.
    """
    import uuid

    from . import db
    from .application_review import compute_review_fingerprint, get_application_review
    from .application_tracking import ApplicationStatus, set_application_status
    from .db import (
        get_application_package,
        get_hh_application,
        get_hh_application_by_vacancy,
        init_db,
        save_hh_application,
        save_submission,
    )
    from .hh_live_page_checks import check_live_page

    init_db()
    if approval is None and human_confirmed:
        from .hh_application_orchestrator import SubmitApproval
        approval = SubmitApproval(source="human", policy_version="legacy_confirm")
    sub_id = submission_id or f"{vacancy_stable_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    if evaluate_fn is None:
        return SubmissionExecutionResult(
            ok=False,
            status="BLOCKED",
            reason="No browser evaluation function (evaluate_fn) provided for CDP session",
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0,
            submission_id=sub_id,
        )

    # 1. Live page inspection. The expected title is not decoration: when it is
    # None, check_live_page() skips the title-similarity step entirely, and then
    # a URL that points at a *different but live* vacancy passes every check -
    # the numeric id it compares is read from the URL we just opened, so it
    # matches by construction. Verified on a live page: three URLs in
    # vacancies.json resolve to unrelated jobs (an AI role advertised at
    # /vacancy/135489102 is actually "Продавец (Чебоксары)"). Those three are
    # archived today, so the archive check happens to catch them; a live one
    # would not be caught by anything. See docs/ble001_triage.md, finding #15.
    # Fail closed. This is reachable in production, not theoretical:
    # browser_executor's submit path reads the row as
    # `vac = _row_to_vacancy(row) if row else None` and then skips the
    # hard-constraint gate entirely (`if vac:`), so a vacancy that is not
    # in the DB today flies through every check blind - remote_required and
    # all. Blocking here matches that module's own `Vacancy not found`
    # BLOCKED result in the other submit entry point.
    vacancy_row = db.get_vacancy_by_id(vacancy_stable_id)
    expected_title = None
    if vacancy_row:
        expected_title = db._row_to_vacancy(vacancy_row).title or None
    if not expected_title:
        # `vacancies` is not the only place a title lives: the runner and
        # the state machine carry it on the application record, and for an
        # application created straight from a URL that record is the only
        # copy. Measured: the stage46/47/50 runner paths have no row in
        # `vacancies` at all and would otherwise be refused here.
        app_row = get_hh_application(vacancy_stable_id) or get_hh_application_by_vacancy(
            vacancy_stable_id
        )
        if app_row:
            expected_title = (app_row.get("title") or "").strip() or None
    if not expected_title:
        logger.error(
            "refusing to submit %s: no vacancy title in the DB, so the "
            "wrong-page check cannot run",
            vacancy_stable_id,
        )
        return SubmissionExecutionResult(
            ok=False,
            status="BLOCKED",
            reason=(
                f"No vacancy row/title for {vacancy_stable_id} - refusing "
                "to submit to a page we cannot identify"
            ),
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0,
            submission_id=sub_id,
        )
    live_result = check_live_page(
        evaluate_fn,
        expected_vacancy_id=vacancy_stable_id,
        expected_title=expected_title,
    )
    if not live_result.is_ok:
        if live_result.already_applied or live_result.error_reason == "ALREADY_APPLIED":
            # Synchronize state: already applied on HH
            set_application_status(vacancy_stable_id, ApplicationStatus.SUBMITTED)
            app = get_hh_application(vacancy_stable_id) or get_hh_application_by_vacancy(vacancy_stable_id)
            if app:
                app_dict = dict(app)
                app_dict["state"] = "SUBMITTED"
                save_hh_application(app_dict)
            return SubmissionExecutionResult(
                ok=False,
                status="ALREADY_APPLIED",
                reason=live_result.reason,
                vacancy_stable_id=vacancy_stable_id,
                submit_count=0,
                live_page_result=live_result,
                submission_id=sub_id,
            )
        return SubmissionExecutionResult(
            ok=False,
            status="BLOCKED",
            reason=live_result.reason,
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0,
            live_page_result=live_result,
            submission_id=sub_id,
        )

    # 2. Form snapshot preparation
    review = get_application_review(vacancy_stable_id)
    pkg_row = get_application_package(vacancy_stable_id)
    pkg_data = json.loads(pkg_row[2]) if (pkg_row and pkg_row[2]) else {}
    cover_letter = pkg_data.get("cover_letter") or (getattr(review, "cover_letter", "") if review else "")
    actual_pkg_fp = compute_review_fingerprint(vacancy_stable_id, pkg_data) if pkg_data else None

    form_snapshot = {
        "fingerprint": actual_pkg_fp,
        "cover_letter": cover_letter,
        "package": pkg_data,
        "already_applied": live_result.already_applied,
        "fields": pkg_data.get("questions") or [],
    }

    # 3. Full gates check (all 11 gates)
    gate_result = HHSubmissionGates.check_all_gates(
        vacancy_stable_id=vacancy_stable_id,
        current_url=live_result.current_url or "",
        form_snapshot=form_snapshot,
        human_confirmed=human_confirmed,
        approval=approval,
        dry_run=dry_run,
        candidate_profile=candidate_profile,
        profile_path=profile_path,
        review_obj=review,
        live_page_result=live_result,
    )
    if not gate_result.passed:
        return SubmissionExecutionResult(
            ok=False,
            status="BLOCKED",
            reason=gate_result.reason,
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0,
            gate_check_result=gate_result,
            live_page_result=live_result,
            submission_id=sub_id,
        )

    # 4. Dry-run early exit
    if dry_run:
        return SubmissionExecutionResult(
            ok=True,
            status="DRY_RUN_OK",
            reason=f"All safety gates passed in dry-run mode ({gate_result.reason}). Zero browser mutations performed.",
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0,
            gate_check_result=gate_result,
            live_page_result=live_result,
            submission_id=sub_id,
        )

    # 5. Acquire exclusive submission claim
    hh_app = get_hh_application(vacancy_stable_id) or get_hh_application_by_vacancy(vacancy_stable_id)
    target_app_id = str((hh_app.get("application_id") if hh_app else vacancy_stable_id) or "")
    acquired, claim_reason, claim_info = db.acquire_submission_claim(
        vacancy_stable_id=vacancy_stable_id,
        application_id=target_app_id,
        worker_id="execute_hh_submission",
        claim_id=sub_id,
    )
    if not acquired:
        is_paused = "PAUSED" in claim_reason
        return SubmissionExecutionResult(
            ok=False,
            status="BLOCKED" if is_paused else ("AMBIGUOUS" if "AMBIGUOUS" in claim_reason else "ALREADY_SUBMITTED"),
            reason=f"Submission claim could not be acquired: {claim_reason}",
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0,
            gate_check_result=gate_result,
            live_page_result=live_result,
            submission_id=sub_id,
        )

    # 5.1 Final pre-click check: kill switch check immediately before irreversible browser action
    if db.is_submit_paused():
        db.update_submission_claim(
            vacancy_stable_id,
            status="FAILED_SAFE",
            claim_id=sub_id,
            details={"reason": "kill_switch_activated_before_click"},
        )
        save_submission(
            vacancy_stable_id,
            json.dumps({"error": "Submission paused by kill switch before click", "submission_id": sub_id}),
            status="BLOCKED",
            submission_id=sub_id,
        )
        return SubmissionExecutionResult(
            ok=False,
            status="BLOCKED",
            reason="Submission blocked by kill switch immediately before click",
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0,
            gate_check_result=gate_result,
            live_page_result=live_result,
            submission_id=sub_id,
        )

    save_submission(
        vacancy_stable_id,
        json.dumps({"status": "SUBMITTING", "submission_id": sub_id}),
        status="SUBMITTING",
        submission_id=sub_id,
    )

    submit_click_js = """// hh_submit_click
(() => {
        const submitBtn = document.querySelector('[data-qa*="response-submit-popup"], [data-qa*="response-submit"], button[type="submit"], [data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"]');
        if (!submitBtn) return JSON.stringify({ ok: false, reason: 'Submit or Apply button not found in DOM' });
        if (submitBtn.disabled) return JSON.stringify({ ok: false, reason: 'Submit button is disabled' });
        submitBtn.click();
        return JSON.stringify({ ok: true });
    })()"""

    try:
        raw_click = evaluate_fn(submit_click_js)
        click_res = json.loads(raw_click) if isinstance(raw_click, str) else raw_click
    except Exception as e:
        logger.error("Evaluate exception during submit click for %s: %s", vacancy_stable_id, e)
        # AMBIGUOUS OUTCOME SAFETY: The browser may have executed the click before connection drop or timeout.
        # DO NOT AUTO-RETRY! Transition attempt and claim to AMBIGUOUS.
        db.update_submission_claim(
            vacancy_stable_id,
            status="AMBIGUOUS",
            claim_id=sub_id,
            details={"error": str(e), "point": "evaluate_click_exception"},
        )
        save_submission(
            vacancy_stable_id,
            json.dumps({"error": str(e), "submission_id": sub_id, "status": "AMBIGUOUS"}),
            status="AMBIGUOUS",
            submission_id=sub_id,
        )
        from .submission_verifier import (
            SubmissionVerification,
            VerificationStatus,
            save_verification,
        )
        verif = SubmissionVerification(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=sub_id,
            verification_status=VerificationStatus.AMBIGUOUS,
            evidence={"error": str(e)},
            verified_at=datetime.utcnow().isoformat(),
        )
        try:
            save_verification(verif)
        except Exception:
            pass
        if sync_hh_application and hh_app and hh_app.get("application_id"):
            try:
                from .hh_application_orchestrator import (
                    HHApplicationState,
                    transition_application,
                )
                transition_application(
                    application_id=hh_app["application_id"],
                    to_state=HHApplicationState.AMBIGUOUS,
                    reason=f"submit_click_exception_ambiguous: {e}",
                    evidence={"exception": str(e), "submit_executed": "unknown"},
                )
            except Exception:
                pass
        return SubmissionExecutionResult(
            ok=False,
            status="AMBIGUOUS",
            reason=f"Submit click encountered exception (ambiguous outcome, auto-retry forbidden): {e}",
            vacancy_stable_id=vacancy_stable_id,
            submit_count=1,
            gate_check_result=gate_result,
            live_page_result=live_result,
            verification_status="AMBIGUOUS",
            submission_id=sub_id,
        )

    if not click_res.get("ok"):
        reason_str = click_res.get("reason", "unknown")
        is_pre_click = "not found in DOM" in reason_str or "disabled" in reason_str
        new_status = "FAILED_SAFE" if is_pre_click else "AMBIGUOUS"
        db.update_submission_claim(
            vacancy_stable_id,
            status=new_status,
            claim_id=sub_id,
            details={"error": reason_str, "is_pre_click": is_pre_click},
        )
        save_submission(
            vacancy_stable_id,
            json.dumps({"error": reason_str, "submission_id": sub_id, "status": new_status}),
            status=new_status,
            submission_id=sub_id,
        )
        return SubmissionExecutionResult(
            ok=False,
            status=new_status,
            reason=f"Submit click failed: {reason_str}",
            vacancy_stable_id=vacancy_stable_id,
            submit_count=0 if is_pre_click else 1,
            gate_check_result=gate_result,
            live_page_result=live_result,
            submission_id=sub_id,
        )

    # 6. Post-submit verification
    success_markers = ("вы откликнулись", "ваш отклик", "отклик отправлен", "negotiations", "отклики и приглашения")
    url_after = live_result.current_url or ""
    verified = False
    try:
        post_raw = evaluate_fn("""// hh_post_submit_verify
(() => {
            const bodyText = (document.body ? document.body.innerText : '').slice(0, 4000).toLowerCase();
            const url = window.location.href || '';
            const respondedSuccessEl = document.querySelector('[data-qa*="responded-success"], [data-qa*="vacancy-response-link-view-topic"]');
            return JSON.stringify({
                has_responded_success: !!respondedSuccessEl,
                has_topic_link: !!respondedSuccessEl,
                text: bodyText,
                url: url,
                has_banner: !!respondedSuccessEl
            });
        })()""")
        post_data = json.loads(post_raw) if isinstance(post_raw, str) else post_raw
        body_head = post_data.get("text", "") or post_data.get("evidence_snippet", "")
        url_after = post_data.get("url", url_after)
        has_banner = post_data.get("has_banner", False) or post_data.get("has_responded_success", False) or post_data.get("has_topic_link", False)
        verified = bool(has_banner or any(m in body_head.lower() for m in success_markers) or "negotiations" in url_after.lower())
    except Exception:
        verified = False

    # 7. Update all databases synchronously
    final_status = "SUBMITTED" if verified else "AMBIGUOUS"
    sub_payload = {
        "submission_id": sub_id,
        "vacancy_stable_id": vacancy_stable_id,
        "status": final_status,
        "verified": verified,
        "url_after": url_after,
    }
    db.update_submission_claim(
        vacancy_stable_id,
        status=final_status,
        claim_id=sub_id,
        details={"verified": verified, "url_after": url_after},
    )
    save_submission(
        vacancy_stable_id,
        json.dumps(sub_payload),
        status=final_status,
        submission_id=sub_id,
    )

    from .submission_verifier import (
        SubmissionVerification,
        VerificationStatus,
        save_verification,
    )
    verif = SubmissionVerification(
        vacancy_stable_id=vacancy_stable_id,
        submission_id=sub_id,
        verification_status=VerificationStatus.VERIFIED if verified else VerificationStatus.AMBIGUOUS,
        evidence={"url_after": url_after, "body_marker": verified},
        final_url=url_after,
        verified_at=datetime.utcnow().isoformat(),
    )
    try:
        save_verification(verif)
    except Exception as e:
        logger.warning(f"Could not save submission verification: {e}")

    if verified:
        set_application_status(
            vacancy_stable_id,
            ApplicationStatus.SUBMITTED,
        )

    if sync_hh_application:
        app = get_hh_application(vacancy_stable_id) or get_hh_application_by_vacancy(vacancy_stable_id)
        if app:
            app_id = app.get("application_id")
            if verified and app_id:
                from .hh_application_orchestrator import (
                    HHApplicationState,
                    transition_application,
                )
                sub_fp = actual_pkg_fp or (form_snapshot.get("fingerprint") if form_snapshot else "") or "submission_verified_fp"
                transition_application(
                    application_id=app_id,
                    to_state=HHApplicationState.SUBMITTED,
                    reason="submission_verified_on_live_page",
                    evidence={
                        "fingerprint": sub_fp,
                        "post_submit_verification": "verified",
                        "url_after": url_after,
                    },
                    approval=approval,
                )
            elif app_id:
                from .hh_application_orchestrator import (
                    HHApplicationState,
                    transition_application,
                )
                transition_application(
                    application_id=app_id,
                    to_state=HHApplicationState.AMBIGUOUS,
                    reason="post_submit_verification_ambiguous",
                    evidence={
                        "submit_executed": True,
                        "post_submit_verification": "ambiguous",
                        "url_after": url_after,
                    },
                )

    if review and getattr(review, "review_id", None):
        _submitted_reviews.add(str(review.review_id))
    _submitted_reviews.add(vacancy_stable_id)

    return SubmissionExecutionResult(
        ok=verified,
        status=final_status,
        reason="Application submitted and verified successfully" if verified else "Submit clicked but post-submit verification ambiguous",
        vacancy_stable_id=vacancy_stable_id,
        submit_count=1,
        gate_check_result=gate_result,
        live_page_result=live_result,
        verification_status="VERIFIED" if verified else "AMBIGUOUS",
        submission_id=sub_id,
    )
