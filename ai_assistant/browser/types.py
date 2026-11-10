"""Domain types, enums, models, and flow classification for browser execution."""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger("browser_executor")


def _note_failure(where: str, exc: BaseException, *, level: int = logging.DEBUG) -> None:
    """Record a swallowed browser error. Silence on submit/prefill/close is not allowed."""
    logger.log(level, "%s failed: %s: %s", where, type(exc).__name__, exc)


EXECUTOR_VERSION = "v1"


class BrowserStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    OPENED = "OPENED"
    FORM_DETECTED = "FORM_DETECTED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"


class SubmitStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"
    DRY_RUN_OK = "DRY_RUN_OK"


class FlowType(str, Enum):
    NATIVE_FORM = "NATIVE_FORM"
    EXTERNAL_ATS = "EXTERNAL_ATS"
    AGGREGATOR_REDIRECT = "AGGREGATOR_REDIRECT"
    UNKNOWN = "UNKNOWN"


class AuthState(str, Enum):
    AUTHENTICATED = "AUTHENTICATED"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    UNKNOWN = "UNKNOWN"


KNOWN_ATS_DOMAINS = {
    "greenhouse.io", "boards.greenhouse.io",
    "jobs.lever.co", "lever.co",
    "apply.workable.com", "workable.com",
    "jobs.ashbyhq.com", "ashbyhq.com",
    "smartrecruiters.com", "jobs.smartrecruiters.com",
    "bamboohr.com",
    "recruitee.com",
    "myworkdayjobs.com", "workday.com",
    "icims.com",
    "jobvite.com",
    "rippling.com",
    "taleo.net",
    "applytojob.com",
}

KNOWN_AGGREGATOR_DOMAINS = {
    "remoteok.com", "remoteok.io",
    "weworkremotely.com",
    "jobspresso.co",
    "wellfound.com", "angel.co",
    "indeed.com", "indeed.ru",
    "ziprecruiter.com",
    "monster.com",
    "glassdoor.com",
    "simplyhired.com",
    "nodesk.co",
    "remoteco.com",
    "flexjobs.com",
    "otta.com",
    "workinstartups.com",
    "himalayas.app",
}


class FlowClassification(BaseModel):
    flow_type: FlowType = FlowType.UNKNOWN
    source_url: str = ""
    application_url: str | None = None
    application_domain: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    is_external_application: bool = False
    verification_strategy: str = "manual_review"
    confidence_reason: str = ""

    model_config = {"extra": "forbid"}


def classify_apply_flow(
    source_url: str,
    final_url: str | None = None,
    apply_link: str | None = None,
    has_form: bool = False,
    redirect_chain: list[str] | None = None,
) -> FlowClassification:
    """Classify the application flow into NATIVE_FORM, EXTERNAL_ATS, AGGREGATOR_REDIRECT, or UNKNOWN."""
    chain = list(redirect_chain or [])
    if not source_url or not isinstance(source_url, str) or not source_url.strip():
        return FlowClassification(
            flow_type=FlowType.UNKNOWN,
            source_url=source_url or "",
            application_url=None,
            application_domain=None,
            redirect_chain=chain,
            is_external_application=False,
            verification_strategy="manual_review",
            confidence_reason="Empty or invalid source URL",
        )

    try:
        src_parsed = urlparse(source_url)
        src_domain = src_parsed.netloc.lower()
    except Exception:
        src_domain = ""

    if not src_domain:
        return FlowClassification(
            flow_type=FlowType.UNKNOWN,
            source_url=source_url,
            application_url=None,
            application_domain=None,
            redirect_chain=chain,
            is_external_application=False,
            verification_strategy="manual_review",
            confidence_reason="Could not determine source domain",
        )

    def _clean(d: str) -> str:
        return d.removeprefix("www.")

    src_clean = _clean(src_domain)

    apply_domain = ""
    if apply_link:
        try:
            apply_domain = _clean(urlparse(apply_link).netloc.lower())
        except Exception:
            apply_domain = ""

    fin_domain = ""
    if final_url:
        try:
            fin_domain = _clean(urlparse(final_url).netloc.lower())
        except Exception:
            fin_domain = ""

    fin_clean = fin_domain
    apply_clean = apply_domain

    def _is_match(domain: str, domain_set: set[str]) -> bool:
        if not domain:
            return False
        return any(domain == kd or domain.endswith("." + kd) for kd in domain_set)

    is_src_aggregator = _is_match(src_clean, KNOWN_AGGREGATOR_DOMAINS)
    is_src_ats = _is_match(src_clean, KNOWN_ATS_DOMAINS)
    is_apply_ats = _is_match(apply_domain, KNOWN_ATS_DOMAINS)
    is_fin_ats = _is_match(fin_domain, KNOWN_ATS_DOMAINS)

    if is_src_ats:
        app_url = final_url or source_url
        app_dom = fin_clean if fin_clean else src_clean
        is_ext = bool(app_dom and app_dom != src_clean)
        return FlowClassification(
            flow_type=FlowType.EXTERNAL_ATS,
            source_url=source_url,
            application_url=app_url,
            application_domain=app_dom,
            redirect_chain=chain,
            is_external_application=is_ext,
            verification_strategy="external_ats_verifier",
            confidence_reason=f"Source domain '{src_clean}' is a recognized ATS platform",
        )

    if (apply_link and is_apply_ats) or (fin_domain and is_fin_ats and fin_domain != src_clean):
        target_url = apply_link if (apply_link and is_apply_ats) else final_url
        target_dom = apply_domain if (apply_link and is_apply_ats) else fin_domain
        is_ext = bool(target_dom and target_dom != src_clean)
        return FlowClassification(
            flow_type=FlowType.EXTERNAL_ATS,
            source_url=source_url,
            application_url=target_url,
            application_domain=target_dom,
            redirect_chain=chain,
            is_external_application=is_ext,
            verification_strategy="external_ats_verifier",
            confidence_reason=f"Application flow points to external ATS platform '{target_dom}'",
        )

    if is_src_aggregator:
        has_ext_apply = bool(apply_link and apply_domain and apply_domain != src_clean)
        has_ext_redirect = bool(fin_domain and fin_domain != src_clean)
        if has_ext_apply or has_ext_redirect:
            target_url = apply_link if has_ext_apply else final_url
            target_dom = apply_domain if has_ext_apply else fin_domain
            is_ats = _is_match(target_dom, KNOWN_ATS_DOMAINS)
            return FlowClassification(
                flow_type=FlowType.EXTERNAL_ATS if is_ats else FlowType.AGGREGATOR_REDIRECT,
                source_url=source_url,
                application_url=target_url,
                application_domain=target_dom,
                redirect_chain=chain,
                is_external_application=True,
                verification_strategy="external_ats_verifier" if is_ats else "aggregator_redirect_pause",
                confidence_reason=f"Aggregator '{src_clean}' routes to external destination '{target_dom}' (ATS: {is_ats})",
            )
        return FlowClassification(
            flow_type=FlowType.AGGREGATOR_REDIRECT,
            source_url=source_url,
            application_url=None,
            application_domain=None,
            redirect_chain=chain,
            is_external_application=False,
            verification_strategy="aggregator_redirect_pause",
            confidence_reason=f"Source domain '{src_clean}' is a recognized job aggregator with no external destination observed",
        )

    if has_form and (not apply_link or apply_domain == src_clean or not apply_domain):
        app_url = final_url or source_url
        app_dom = fin_clean or src_clean
        is_ext = bool(app_dom and app_dom != src_clean)
        return FlowClassification(
            flow_type=FlowType.NATIVE_FORM,
            source_url=source_url,
            application_url=app_url,
            application_domain=app_dom,
            redirect_chain=chain,
            is_external_application=is_ext,
            verification_strategy="native_submission_verifier",
            confidence_reason=f"Native application form detected directly on '{src_clean}'",
        )

    if apply_link and apply_domain and apply_domain != src_clean:
        return FlowClassification(
            flow_type=FlowType.AGGREGATOR_REDIRECT,
            source_url=source_url,
            application_url=apply_link,
            application_domain=apply_domain,
            redirect_chain=chain,
            is_external_application=True,
            verification_strategy="aggregator_redirect_pause",
            confidence_reason=f"Outbound application link points from '{src_clean}' to '{apply_domain}'",
        )

    if has_form:
        app_url = final_url or source_url
        app_dom = fin_clean or src_clean
        is_ext = bool(app_dom and app_dom != src_clean)
        return FlowClassification(
            flow_type=FlowType.NATIVE_FORM,
            source_url=source_url,
            application_url=app_url,
            application_domain=app_dom,
            redirect_chain=chain,
            is_external_application=is_ext,
            verification_strategy="native_submission_verifier",
            confidence_reason=f"Application form detected on '{src_clean}'",
        )

    return FlowClassification(
        flow_type=FlowType.UNKNOWN,
        source_url=source_url,
        application_url=None,
        application_domain=None,
        redirect_chain=chain,
        is_external_application=False,
        verification_strategy="manual_review",
        confidence_reason="No definitive flow signatures detected",
    )


class SubmitResult(BaseModel):
    vacancy_stable_id: str
    submission_id: str
    status: SubmitStatus
    final_url: str | None = None
    page_title: str | None = None
    submitted_at: str | None = None
    before_screenshot: str | None = None
    after_screenshot: str | None = None
    confirmation_used: bool = False
    submit_button_found: bool = False
    error: str | None = None
    flow_type: FlowType | None = None
    application_domain: str | None = None
    verification_strategy: str | None = None
    submit_count: int = 0
    executor_version: str = EXECUTOR_VERSION
    gate_check_result: Any | None = None
    live_page_result: Any | None = None

    model_config = {"extra": "forbid"}


class BrowserApplicationSession(BaseModel):
    vacancy_stable_id: str
    url: str
    status: BrowserStatus
    fields_detected: list[str] = Field(default_factory=list)
    fields_filled: list[str] = Field(default_factory=list)
    fields_skipped: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    final_url: str | None = None
    page_title: str | None = None
    site: str | None = None
    form_detected: bool = False
    error: str | None = None
    screenshot_path: str | None = None
    flow_type: FlowType | None = None
    source_url: str | None = None
    application_url: str | None = None
    application_domain: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    is_external_application: bool = False
    verification_strategy: str | None = None


class BrowserResult(BaseModel):
    vacancy_stable_id: str
    url: str
    final_url: str | None = None
    page_title: str | None = None
    site: str | None = None
    status: BrowserStatus
    form_detected: bool = False
    apply_button_found: bool = False
    fields_detected: list[str] = Field(default_factory=list)
    fields_filled: list[str] = Field(default_factory=list)
    fields_skipped: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    screenshot_path: str | None = None
    flow_type: FlowType | None = None
    source_url: str | None = None
    application_url: str | None = None
    application_domain: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    is_external_application: bool = False
    verification_strategy: str | None = None

    model_config = {"extra": "forbid"}
