from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from . import config
from .db import init_db, get_connection
from .browser_executor import (
    BrowserAdapter,
    MockBrowserAdapter,
    PlaywrightBrowserAdapter,
    FlowType,
    FlowClassification,
    classify_apply_flow,
)

VERIFICATION_VERSION = "v1"

logger = logging.getLogger(__name__)


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"


class SubmissionVerification(BaseModel):
    vacancy_stable_id: str
    submission_id: str
    verification_status: VerificationStatus
    evidence: Dict[str, Any] = Field(default_factory=dict)
    final_url: Optional[str] = None
    page_title: Optional[str] = None
    success_signal: Optional[str] = None
    screenshot_path: Optional[str] = None
    verified_at: str
    warnings: List[str] = Field(default_factory=list)
    flow_type: Optional[FlowType] = None
    source_url: Optional[str] = None
    application_url: Optional[str] = None
    application_domain: Optional[str] = None
    redirect_chain: List[str] = Field(default_factory=list)
    is_external_application: bool = False
    verification_strategy: Optional[str] = None
    verification_version: str = VERIFICATION_VERSION

    model_config = {"extra": "forbid"}


# Success indicators that confirm application was accepted
SUCCESS_INDICATORS = [
    "application submitted",
    "application received",
    "thank you for applying",
    "successfully submitted",
    "thank you for your application",
    "your application has been received",
    "application confirmed",
    "we have received your application",
    "application sent",
    "submitted successfully",
    "заявка отправлена",
    "заявка получена",
    "спасибо за заявку",
    "ваша заявка принята",
    "заявка успешно отправлена",
    "application complete",
    "you have successfully applied",
]

# Error indicators that confirm failure
ERROR_INDICATORS = [
    "application failed",
    "submission failed",
    "error submitting",
    "could not submit",
    "failed to apply",
    "application error",
    "something went wrong",
    "try again",
    "ошибка при отправке",
    "не удалось отправить",
    "ошибка заявки",
]

# Blocked indicators (CAPTCHA, login, etc.)
BLOCKED_INDICATORS = [
    "captcha",
    "cloudflare",
    "access denied",
    "login required",
    "please log in",
    "sign in to apply",
    "authentication required",
    "verify you are human",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "403 forbidden",
    "404 not found",
    "page not found",
    "blocked",
    "rate limited",
    "too many requests",
]


def save_verification(verification: SubmissionVerification) -> None:
    """Save verification result to database."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO submission_verifications 
        (vacancy_stable_id, submission_id, verification_version, verification_status, 
         verification_json, verified_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vacancy_stable_id, submission_id, verification_version) DO UPDATE SET
            verification_status=excluded.verification_status,
            verification_json=excluded.verification_json,
            verified_at=excluded.verified_at,
            updated_at=excluded.updated_at
    ''', (
        verification.vacancy_stable_id,
        verification.submission_id,
        verification.verification_version,
        verification.verification_status.value,
        verification.model_dump_json(),
        verification.verified_at,
        verification.verified_at,
        verification.verified_at,
    ))
    conn.commit()
    conn.close()


def get_verification(vacancy_stable_id: str, submission_id: str, verification_version: str | None = None) -> Optional[SubmissionVerification]:
    """Get verification result from database."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    if verification_version is not None:
        cur.execute('''
            SELECT vacancy_stable_id, submission_id, verification_version, verification_status, 
                   verification_json, verified_at, created_at, updated_at
            FROM submission_verifications 
            WHERE vacancy_stable_id=? AND submission_id=? AND verification_version=?
        ''', (vacancy_stable_id, submission_id, verification_version))
    else:
        cur.execute('''
            SELECT vacancy_stable_id, submission_id, verification_version, verification_status, 
                   verification_json, verified_at, created_at, updated_at
            FROM submission_verifications 
            WHERE vacancy_stable_id=? AND submission_id=?
            ORDER BY verified_at DESC LIMIT 1
        ''', (vacancy_stable_id, submission_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        data = json.loads(row[4]) if row[4] else {}
        return SubmissionVerification(**data)
    except Exception:
        return None


def list_verifications(limit: int = 50) -> List[SubmissionVerification]:
    """List all verification results."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT vacancy_stable_id, submission_id, verification_version, verification_status, 
               verification_json, verified_at, created_at, updated_at
        FROM submission_verifications 
        ORDER BY verified_at DESC LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    results = []
    for row in rows:
        try:
            data = json.loads(row[4]) if row[4] else {}
            results.append(SubmissionVerification(**data))
        except Exception:
            continue
    return results


def is_verified(vacancy_stable_id: str, submission_id: str, verification_version: str | None = None) -> bool:
    """Check if a submission has been verified as VERIFIED."""
    ver = get_verification(vacancy_stable_id, submission_id, verification_version)
    return ver is not None and ver.verification_status == VerificationStatus.VERIFIED


def _detect_signals(content: str, title: str, url: str) -> Tuple[List[str], List[str], List[str]]:
    """Detect success, error, and blocked signals from page content."""
    content_lower = content.lower()
    title_lower = title.lower()
    url_lower = url.lower()
    combined = f"{content_lower} {title_lower} {url_lower}"

    success_signals = [ind for ind in SUCCESS_INDICATORS if ind in combined]
    error_signals = [ind for ind in ERROR_INDICATORS if ind in combined]
    blocked_signals = [ind for ind in BLOCKED_INDICATORS if ind in combined]

    return success_signals, error_signals, blocked_signals


def verify_submission(
    vacancy_stable_id: str,
    submission_id: str,
    profile_path: str | None = None,
    adapter: BrowserAdapter | None = None,
) -> SubmissionVerification:
    """
    Verify a submission by checking the application page for success/error/blocked signals.
    Does NOT re-submit the application - only reads the current page state.
    """
    from .db import get_submission, get_vacancy_by_id
    from .db import _row_to_vacancy
    from .candidate_profile import load_candidate_profile
    import os

    init_db()

    # Get the submission record
    submission_row = get_submission(vacancy_stable_id)
    if not submission_row:
        return SubmissionVerification(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            verification_status=VerificationStatus.FAILED,
            verified_at=datetime.utcnow().isoformat(),
            warnings=["Submission record not found"],
        )

    # Get vacancy URL
    vacancy_row = get_vacancy_by_id(vacancy_stable_id)
    if not vacancy_row:
        return SubmissionVerification(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            verification_status=VerificationStatus.FAILED,
            verified_at=datetime.utcnow().isoformat(),
            warnings=["Vacancy not found"],
        )
    vac = _row_to_vacancy(vacancy_row)

    # Choose adapter
    use_adapter = adapter
    if use_adapter is None:
        use_real = os.getenv("BROWSER_USE_PLAYWRIGHT") == "1" or os.getenv("BROWSER_REAL") == "1" or os.getenv("USE_PLAYWRIGHT") == "1"
        if use_real:
            try:
                use_adapter = PlaywrightBrowserAdapter(headless=True)
            except Exception as e:
                logger.warning(f"Playwright not available, fallback to Mock: {e}")
                use_adapter = MockBrowserAdapter()
        else:
            use_adapter = MockBrowserAdapter()

    warnings: List[str] = []
    final_url = ""
    page_title = ""
    content = ""
    screenshot_path = None
    evidence: Dict[str, Any] = {}

    try:
        # Open the vacancy URL to check current state
        open_res = use_adapter.open(vac.job_url)
        if not open_res:
            warnings.append("Failed to open vacancy URL for verification")
            return SubmissionVerification(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=submission_id,
                verification_status=VerificationStatus.FAILED,
                verified_at=datetime.utcnow().isoformat(),
                warnings=warnings,
                verification_version=VERIFICATION_VERSION,
            )

        final_url = open_res.get("final_url", vac.job_url)
        page_title = open_res.get("title", "")

        # Get page content for signal detection
        if hasattr(use_adapter, "get_content"):
            content = use_adapter.get_content()
        elif hasattr(use_adapter, "page") and use_adapter.page:
            content = use_adapter.page.content()
        else:
            content = f"{page_title} {final_url}"

        # Take screenshot for evidence
        try:
            screenshot_path = f"artifacts/verification/{vacancy_stable_id.replace(':', '_')}_{submission_id}_verification.png"
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            sp = use_adapter.screenshot(screenshot_path)
            if sp:
                screenshot_path = sp
        except Exception as e:
            warnings.append(f"Screenshot failed: {e}")
            screenshot_path = None

        # Detect signals
        success_signals, error_signals, blocked_signals = _detect_signals(content, page_title, final_url)

        # Flow classification
        flow_class = classify_apply_flow(
            source_url=vac.job_url,
            final_url=final_url,
            apply_link=None,
            has_form=bool(success_signals or not error_signals),
        )

        evidence = {
            "success_signals": success_signals,
            "error_signals": error_signals,
            "blocked_signals": blocked_signals,
            "content_length": len(content),
            "url": final_url,
            "title": page_title,
            "flow_type": flow_class.flow_type.value,
            "source_url": flow_class.source_url,
            "application_url": flow_class.application_url,
            "application_domain": flow_class.application_domain,
            "redirect_chain": flow_class.redirect_chain,
            "is_external_application": flow_class.is_external_application,
            "verification_strategy": flow_class.verification_strategy,
        }

        # Determine verification status
        if blocked_signals:
            status = VerificationStatus.BLOCKED
            warnings.append(f"Blocked signals detected: {', '.join(blocked_signals)}")
            success_signal = None
        elif error_signals:
            status = VerificationStatus.FAILED
            warnings.append(f"Error signals detected: {', '.join(error_signals)}")
            success_signal = None
        elif success_signals:
            status = VerificationStatus.VERIFIED
            success_signal = success_signals[0]
            warnings.append(f"Success confirmed: {success_signal}")
        else:
            status = VerificationStatus.AMBIGUOUS
            if flow_class.flow_type == FlowType.AGGREGATOR_REDIRECT:
                warnings.append(f"Aggregator redirect flow ({flow_class.application_domain}): clicking Apply on aggregator does not confirm employer receipt")
            elif flow_class.flow_type == FlowType.EXTERNAL_ATS:
                warnings.append(f"External ATS flow ({flow_class.application_domain}): confirmation not detected on ATS domain")
            else:
                warnings.append("No clear success/error/blocked signals found")
            success_signal = None

    except Exception as e:
        logger.error(f"Verification failed for {vacancy_stable_id}: {e}")
        warnings.append(f"Verification error: {str(e)}")
        status = VerificationStatus.FAILED
        success_signal = None
    finally:
        try:
            use_adapter.close()
        except Exception:
            pass

    verification = SubmissionVerification(
        vacancy_stable_id=vacancy_stable_id,
        submission_id=submission_id,
        verification_status=status,
        evidence=evidence,
        final_url=final_url,
        page_title=page_title,
        success_signal=success_signal,
        screenshot_path=screenshot_path,
        verified_at=datetime.utcnow().isoformat(),
        warnings=warnings,
        flow_type=flow_class.flow_type,
        source_url=flow_class.source_url,
        application_url=flow_class.application_url,
        application_domain=flow_class.application_domain,
        redirect_chain=flow_class.redirect_chain,
        is_external_application=flow_class.is_external_application,
        verification_strategy=flow_class.verification_strategy,
        verification_version=VERIFICATION_VERSION,
    )

    save_verification(verification)

    # Transition tracking based on verified status
    try:
        from .application_tracking import verify_and_apply
        if status == VerificationStatus.VERIFIED:
            verify_and_apply(vacancy_stable_id, "VERIFIED", note="Submission verified by browser verifier")
    except Exception as e:
        logger.debug(f"Tracking update note: {e}")

    return verification