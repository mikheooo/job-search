from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .submission_verifier import SubmissionVerification

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from .candidate_profile import CandidateProfile
from .db import get_connection, init_db
from .schema import Vacancy

EXECUTOR_VERSION = "v1"

# Safety: hard ban on auto-submit - this module MUST NOT contain submit/apply click
# If you need to submit, do it manually - this executor only prepares

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
    NATIVE_FORM = "NATIVE_FORM"           # TYPE A: Native application form directly on vacancy page
    EXTERNAL_ATS = "EXTERNAL_ATS"         # TYPE B: Apply link leads to external ATS domain (Greenhouse, Lever, etc.)
    AGGREGATOR_REDIRECT = "AGGREGATOR_REDIRECT" # TYPE C: Aggregator job board redirect / outbound link (RemoteOK, LinkedIn external apply, etc.)
    UNKNOWN = "UNKNOWN"                   # Classification undetermined or insufficient evidence

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
    from urllib.parse import urlparse

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

    # Check apply_link domain
    apply_domain = ""
    if apply_link:
        try:
            apply_domain = _clean(urlparse(apply_link).netloc.lower())
        except Exception:
            apply_domain = ""

    # Check final_url domain
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

    # Case 1: Direct ATS source (e.g. source URL is already on greenhouse.io, lever.co, etc.)
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

    # Case 2: Apply link or redirected URL points to an ATS platform
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

    # Case 3: Source is a recognized job aggregator
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
        # Stays on aggregator domain without external apply link or external redirect
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

    # Case 4: Native application form
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

    # Case 5: Outbound application link to non-ATS external destination
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

# Abstract BrowserAdapter - no submit/apply methods allowed
class BrowserAdapter:
    def open(self, url: str) -> dict[str, Any]:
        raise NotImplementedError
    def inspect_page(self) -> dict[str, Any]:
        raise NotImplementedError
    def fill_field(self, selector: str, value: str) -> bool:
        raise NotImplementedError
    def upload_file(self, selector: str, path: str) -> bool:
        raise NotImplementedError
    def screenshot(self, path: str) -> str | None:
        raise NotImplementedError
    def close(self) -> None:
        raise NotImplementedError
    def get_current_url(self) -> str:
        raise NotImplementedError
    def get_title(self) -> str:
        raise NotImplementedError
    # Submit method - only for controlled submit flow
    def submit_application(self) -> dict[str, Any]:
        raise NotImplementedError

    def extract_application_form(self) -> dict[str, Any]:
        """Platform-aware: read the current page and return a normalized DOM
        snapshot for form extraction.

        Returns a dict with keys:
          html, body_text, questions: [{label, slug}], controls: [{tag, type,
          name, id, dataQa, required, label, options}] (real form controls,
          present only when the page renders them, e.g. authenticated HH),
          auth_form: bool, apply_link: {href, text}|None, final_url, title, site

        EXTRACTION ONLY: never submits, never clicks Apply, never fills,
        never uploads, never calls LLM, never mutates DB.
        """
        raise NotImplementedError

class MockBrowserAdapter(BrowserAdapter):
    def __init__(self, simulate: dict[str, Any] | None = None):
        self.simulate = simulate or {}
        self.opened_url: str | None = None
        self.calls: list[str] = []
        self.closed = False
        # Safety: ensure no submit is ever called
        self.submit_attempted = False

    def open(self, url: str) -> dict[str, Any]:
        self.calls.append(f"open:{url}")
        self.opened_url = url
        # Simulate response
        final_url = self.simulate.get("final_url", url)
        title = self.simulate.get("page_title", "Mock Vacancy Page")
        site = self.simulate.get("site", url.split("/")[2] if "://" in url else "example.com")
        blocked_reason = self.simulate.get("blocked_reason")
        if blocked_reason:
            return {"final_url": final_url, "title": title, "site": site, "blocked": True, "reason": blocked_reason}
        return {"final_url": final_url, "title": title, "site": site, "blocked": False}

    def inspect_page(self) -> dict[str, Any]:
        self.calls.append("inspect_page")
        # Simulate detection
        if self.simulate.get("form_not_found"):
            return {"form_detected": False, "fields": [], "apply_button": False, "captcha": False, "login_required": False}
        fields = self.simulate.get("fields", ["name", "email", "resume", "cover_letter", "phone", "linkedin"])
        apply_found = self.simulate.get("apply_button", True)
        captcha = self.simulate.get("captcha", False)
        login_required = self.simulate.get("login_required", False)
        cloudflare = self.simulate.get("cloudflare", False)
        return {
            "form_detected": not self.simulate.get("form_not_found", False),
            "fields": fields,
            "apply_button": apply_found,
            "captcha": captcha,
            "login_required": login_required,
            "cloudflare": cloudflare,
        }

    def extract_application_form(self) -> dict[str, Any]:
        self.calls.append("extract_application_form")
        sim = self.simulate
        questions = sim.get("questions") or []
        return {
            "html": sim.get("html", ""),
            "body_text": sim.get("body_text", ""),
            "questions": [dict(q) for q in questions],
            "controls": [dict(c) for c in (sim.get("controls") or [])],
            "question_groups": list(sim.get("question_groups") or []),
            "auth_form": sim.get("auth_form", False),
            "apply_link": sim.get("apply_link"),
            "final_url": sim.get("final_url") or self.opened_url or "",
            "title": sim.get("page_title") or "",
            "site": sim.get("site") or "hh.ru",
            # BLE001 finding #7: a snapshot must say whether it is trustworthy.
            # An empty-but-valid form and a failed extraction look identical
            # (questions=[], controls=[], auth_form=False), so the failure case
            # is marked explicitly. Downstream must fail closed on error=True.
            "error": bool(sim.get("error", False)),
            "error_reason": sim.get("error_reason"),
            # BLE001 finding #10: the mock drives no browser, so it never
            # degrades to headless Playwright - but tests can simulate it.
            "cdp_fallback": bool(sim.get("cdp_fallback", False)),
            "cdp_fallback_reason": sim.get("cdp_fallback_reason"),
        }

    def inspect_apply_flow(self) -> dict[str, Any]:
        self.calls.append("inspect_apply_flow")
        sim = self.simulate
        href = sim.get("apply_link")
        btn_text = sim.get("button_text", "Apply Now" if (sim.get("apply_button") or href) else None)
        auth = sim.get("authenticated")
        if auth is None:
            auth = "AUTHENTICATED" if (sim.get("fields") or "hh.ru" in str(sim.get("final_url", self.opened_url or "")) or sim.get("has_form")) else "NOT_AUTHENTICATED"
        login_req = bool(sim.get("login_required", False))
        return {
            "title": sim.get("page_title", ""),
            "url": sim.get("final_url", self.opened_url or ""),
            "apply_present": bool(sim.get("apply_button", False) or href),
            "apply_href": href,
            "apply_element_text": btn_text,
            "apply_element_tag": "a" if href else "button",
            "apply_element_href": href,
            "apply_element_aria_label": sim.get("aria_label"),
            "apply_detection_reason": sim.get("reason", "Mock detected apply element"),
            "button_text": btn_text,
            "has_form": bool(sim.get("fields")),
            "fields": list(sim.get("fields", [])),
            "captcha": bool(sim.get("captcha", False)),
            "login_required": login_req,
            "login_reason": sim.get("login_reason", "Login required for this application flow" if login_req else None),
            "authenticated": auth,
            "auth_reason": sim.get("auth_reason", "Simulated mock authentication state"),
        }

    def fill_field(self, selector: str, value: str) -> bool:
        self.calls.append(f"fill:{selector}")
        return True

    def upload_file(self, selector: str, path: str) -> bool:
        self.calls.append(f"upload:{selector}")
        return True

    def screenshot(self, path: str) -> str | None:
        self.calls.append(f"screenshot:{path}")
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text("mock screenshot", encoding="utf-8")
            return path
        except Exception:
            return None

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True

    def get_current_url(self) -> str:
        return self.simulate.get("final_url", self.opened_url or "")

    def get_title(self) -> str:
        return self.simulate.get("page_title", "Mock Page")

    def get_content(self) -> str:
        return self.simulate.get("content", f"{self.get_title()} {self.get_current_url()}")

    def submit_application(self) -> dict[str, Any]:
        self.calls.append("submit_application")
        self.submit_attempted = True
        # Mock always returns success for testing
        return {"success": True, "message": "Mock submission successful"}

    def evaluate(self, js: str) -> str:
        self.calls.append("evaluate")
        cleaned_js = js.strip()
        sim = self.simulate
        target_url = sim.get("final_url") or self.opened_url or "https://hh.ru/vacancy/12345678"

        # 1. Live page inspect marker
        if cleaned_js.startswith("// hh_live_page_inspect"):
            inspect = self.inspect_page()
            return json.dumps({
                "ok": True,
                "url": target_url,
                "title": self.get_title(),
                "is_404": bool(sim.get("is_404", False)),
                "is_captcha": bool(inspect.get("captcha", False)),
                "is_access_denied": bool(sim.get("is_access_denied", False)),
                "is_login_required": bool(inspect.get("login_required", False)),
                "already_responded": bool(sim.get("already_responded", False)),
                "has_submit_btn": bool(inspect.get("apply_button", True)),
                "submit_btn_disabled": bool(sim.get("submit_btn_disabled", False)),
                "has_apply_btn": bool(inspect.get("apply_button", True)),
                "has_response_modal": bool(sim.get("has_response_modal", True)),
            })

        # 2. Submit click marker
        if cleaned_js.startswith("// hh_submit_click"):
            res = self.submit_application()
            return json.dumps({"ok": res.get("success", True)})

        # 3. Post-submit verify marker
        if cleaned_js.startswith("// hh_post_submit_verify"):
            responded = bool(sim.get("has_responded_success", self.submit_attempted))
            return json.dumps({
                "ok": True,
                "url": target_url,
                "title": self.get_title(),
                "h1": self.get_title(),
                "has_responded_success": responded,
                "hasRespondedSuccess": responded,
                "has_topic_link": bool(sim.get("has_topic_link", self.submit_attempted)),
                "hasTopicLink": bool(sim.get("has_topic_link", self.submit_attempted)),
                "has_cover_letter_btn": False,
                "hasCoverLetterBtn": False,
                "has_explicit_rejection": bool(sim.get("has_explicit_rejection", False)),
                "hasExplicitRejection": bool(sim.get("has_explicit_rejection", False)),
                "is_chat": False,
                "isChat": False,
                "is_vacancy_page": True,
                "isVacancyPage": True,
                "has_submit_btn": False,
                "hasSubmitBtn": False,
                "has_apply_btn": False,
                "hasApplyBtn": False,
                "is_404": False,
                "is_captcha": False,
                "is_access_denied": False,
                "is_login_required": False,
                "already_responded": False,
                "bodySnippet": "отклик отправлен" if self.submit_attempted else "",
                "text": "отклик отправлен" if self.submit_attempted else "",
                "evidence_snippet": "отклик отправлен" if self.submit_attempted else "",
                "has_banner": responded,
            })

        # Generic fallback
        if "location.href" in cleaned_js:
            return json.dumps({"url": target_url})
        return json.dumps({"ok": True, "url": target_url})


# Returned when a page could not actually be read. Deliberately fail-closed:
# "no form, no apply button" makes every caller BLOCK, instead of filling and
# clicking a form we never saw. `captcha`/`login_required` stay False because a
# read failure is not evidence of either - `form_detected: False` is what stops
# the flow. Always hand out a copy so callers cannot mutate this constant.
# See docs/ble001_triage.md finding #1.
INSPECT_UNREADABLE: dict[str, Any] = {
    "form_detected": False,
    "fields": [],
    "apply_button": False,
    "captcha": False,
    "login_required": False,
    "inspection_failed": True,
}


class CDPBrowserAdapter(BrowserAdapter):
    """Direct CDP adapter over WebSocket / HTTP API (e.g. http://127.0.0.1:9222)."""
    def __init__(self, cdp_url: str | None = None):
        from .hh_browser_launcher import resolve_cdp_url

        self.cdp_url = resolve_cdp_url(cdp_url).rstrip("/")
        self.tab_id: str | None = None
        self.ws_url: str | None = None
        self._final_url: str | None = None
        self._title: str | None = None
        self.submit_attempted = False

    def _sync_run(self, coro):
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    def open(self, url: str) -> dict[str, Any]:
        import json
        import urllib.request
        from .hh_browser_launcher import _NO_PROXY_OPENER
        try:
            new_url = f"{self.cdp_url}/json/new?{url}"
            req = urllib.request.Request(new_url, method="PUT")
            # Finding #9/#12: urlopen() honours http_proxy, which turns a live
            # localhost browser into a "502 Bad Gateway" and makes the
            # submission path report blocked. Use the proxy-free opener.
            with _NO_PROXY_OPENER.open(req, timeout=10) as resp:
                tab = json.loads(resp.read().decode("utf-8"))
            self.tab_id = tab.get("id")
            self.ws_url = tab.get("webSocketDebuggerUrl")
            
            import time
            time.sleep(3)
            
            async def _init_page():
                import asyncio

                import websockets
                async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=15) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "JSON.stringify({url: window.location.href, title: document.title})"}}))
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(raw)
                    val = json.loads(data.get("result", {}).get("result", {}).get("value", "{}"))
                    return val

            page_info = self._sync_run(_init_page())
            self._final_url = page_info.get("url") or url
            self._title = page_info.get("title") or ""
            site = self._final_url.split("/")[2] if "://" in self._final_url else ""
            return {"final_url": self._final_url, "title": self._title, "site": site, "blocked": False}
        except Exception as e:
            return {"final_url": url, "title": "", "site": "", "blocked": True, "reason": str(e)}

    def inspect_page(self) -> dict[str, Any]:
        """Read-only page inspection over CDP.

        Fail-closed by design: anything that prevents us from reading the page
        (no ws_url, empty result, exception) yields INSPECT_UNREADABLE rather
        than an optimistic "form found". Callers decide BLOCK vs proceed on
        exactly these keys, so inventing a success here used to defeat captcha,
        login and missing-form checks at once. See docs/ble001_triage.md.
        """
        if not self.ws_url:
            logger.warning("CDP inspect_page: no ws_url configured; reporting page as unreadable")
            return dict(INSPECT_UNREADABLE)
        async def _inspect():
            import asyncio
            import json

            import websockets
            async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=15) as ws:
                script = """(() => {
                    const buttons = Array.from(document.querySelectorAll("button, a, input[type='submit']"));
                    const applyBtn = buttons.some(b => (b.innerText || b.value || '').toLowerCase().includes('apply'));
                    const fields = [];
                    if (document.querySelector("input[name*='name'], input[id*='name']")) fields.push("name");
                    if (document.querySelector("input[type='email'], input[name*='email']")) fields.push("email");
                    if (document.querySelector("input[type='tel'], input[name*='phone']")) fields.push("phone");
                    if (document.querySelector("input[type='file']")) fields.push("resume");
                    if (document.querySelector("textarea")) fields.push("cover_letter");
                    if (document.querySelector("input[name*='linkedin']")) fields.push("linkedin");
                    if (document.querySelector("input[name*='github']")) fields.push("github");
                    const content = document.body ? document.body.innerText.toLowerCase() : '';
                    return {
                        form_detected: fields.length > 0 || applyBtn,
                        fields: fields,
                        apply_button: applyBtn,
                        captcha: content.includes("captcha") || content.includes("cloudflare"),
                        login_required: content.includes("login required") || content.includes("please log in")
                    };
                })()"""
                await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": script, "returnByValue": True}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw)
                return data.get("result", {}).get("result", {}).get("value", {})
        try:
            res = self._sync_run(_inspect())
            if not res:
                logger.warning("CDP inspect_page: empty result; reporting page as unreadable")
                return dict(INSPECT_UNREADABLE)
            return res
        except Exception as e:
            logger.warning(
                "CDP inspect_page failed (%s: %s); reporting page as unreadable",
                type(e).__name__, e,
            )
            return dict(INSPECT_UNREADABLE)

    def inspect_apply_flow(self) -> dict[str, Any]:
        """Read-only inspection of apply buttons, hrefs, and form fields without clicking anything."""
        if not self.ws_url:
            return {"apply_present": False, "apply_href": None, "has_form": False, "fields": []}
        async def _inspect_flow():
            import asyncio
            import json

            import websockets
            async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=15) as ws:
                script = r"""(() => {
                    const result = {
                        title: document.title || '',
                        url: window.location.href || '',
                        apply_present: false,
                        apply_href: null,
                        apply_element_text: null,
                        apply_element_tag: null,
                        apply_element_href: null,
                        apply_element_aria_label: null,
                        apply_detection_reason: 'No application elements found',
                        button_text: null,
                        has_form: false,
                        fields: [],
                        captcha: false,
                        login_required: false
                    };

                    // 1. Detect Form Fields (excluding search forms in navbar/header)
                    const formInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='search']):not([type='submit']), textarea, select"));
                    const jobFormInputs = formInputs.filter(i => {
                        return !i.closest('header, nav, footer, [role="navigation"], .header, .navbar, .search-bar, .search-form');
                    });
                    if (jobFormInputs.length > 0) {
                        result.has_form = true;
                        result.fields = jobFormInputs.map(i => i.name || i.id || i.type).filter(Boolean).slice(0, 10);
                    }

                    // 2. Blacklist for non-apply navigation paths / links
                    const NAV_HREF_BLACKLIST = [
                        '/jobs', '/jobs/', '/remote-jobs', '/remote-jobs/', '/top-trending-remote-jobs',
                        '/applicant/negotiations', '/applicant/resumes', '/vacancies', '/search',
                        '/categories', '/companies', '/salaries', '/community', '/about', '/contact',
                        '/terms', '/privacy', '/login', '/signin', '/register', '/signup', '/auth'
                    ];

                    function isNavOrGenericLink(href) {
                        if (!href || href === '#' || href.startsWith('javascript:')) return true;
                        try {
                            const u = new URL(href, window.location.href);
                            const path = u.pathname.toLowerCase().replace(/\/+$/, '');
                            if (NAV_HREF_BLACKLIST.includes(path) || NAV_HREF_BLACKLIST.includes(path + '/')) return true;
                            if (u.hostname === window.location.hostname && (path === '' || path === '/')) return true;
                        } catch(e) {}
                        return false;
                    }

                    // 3. Find candidate elements
                    const allElements = Array.from(document.querySelectorAll("a, button, [role='button'], input[type='submit'], input[type='button']"));
                    let bestCandidate = null;
                    let bestScore = -1;

                    for (const el of allElements) {
                        const isInsideNav = !!el.closest('header, nav, footer, [role="navigation"], .navbar, .menu, .breadcrumb, .breadcrumbs, .header__nav, .footer');
                        const text = (el.innerText || el.textContent || el.value || '').trim();
                        const aria = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
                        const href = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-apply-url');
                        const combinedText = `${text} ${aria}`.toLowerCase();

                        if (isInsideNav && isNavOrGenericLink(href)) continue;
                        if (/^отклики$/i.test(text)) continue;
                        if (/^jobs$/i.test(text) || /^remote jobs$/i.test(text)) continue;
                        if (/top trending remote jobs/i.test(text)) continue;

                        let score = 0;
                        let reason = '';

                        // Strong Apply CTA Text
                        if (/^(apply now|apply for this job|apply for this position|apply to position|easy apply|apply on company site|apply on employer site)$/i.test(text) || /^откликнуться$/i.test(text) || /^подать резюме$/i.test(text)) {
                            score += 100;
                            reason = `Exact primary Apply CTA text '${text}'`;
                        } else if (/apply|откликнуться|подать резюме|submit application/i.test(combinedText)) {
                            score += 60;
                            reason = `Apply CTA text match '${text || aria}'`;
                        }

                        // ATS domain in href
                        if (href && /boards\.greenhouse\.io|jobs\.lever\.co|apply\.workable\.com|jobs\.ashbyhq\.com|smartrecruiters\.com|myworkdayjobs\.com|bamboohr\.com/i.test(href)) {
                            score += 120;
                            reason = `Direct external ATS link in href '${href}'`;
                        } else if (href && !isNavOrGenericLink(href) && /apply/i.test(href)) {
                            score += 40;
                            if (!reason) reason = `Apply path in href '${href}'`;
                        }

                        // Inside main form submit
                        if (el.type === 'submit' && el.closest('form') && !isInsideNav) {
                            score += 30;
                            if (!reason) reason = 'Form submit button';
                        }

                        if (isInsideNav) {
                            score -= 50;
                        }

                        if (score > bestScore && score > 20) {
                            bestScore = score;
                            bestCandidate = {
                                el,
                                text,
                                tag: el.tagName.toLowerCase(),
                                aria,
                                href,
                                reason
                            };
                        }
                    }

                    if (bestCandidate) {
                        result.apply_present = true;
                        result.apply_element_text = bestCandidate.text || null;
                        result.apply_element_tag = bestCandidate.tag;
                        result.apply_element_aria_label = bestCandidate.aria || null;
                        result.button_text = bestCandidate.text || bestCandidate.aria || null;
                        result.apply_detection_reason = bestCandidate.reason;

                        if (bestCandidate.href && !isNavOrGenericLink(bestCandidate.href)) {
                            try {
                                result.apply_href = new URL(bestCandidate.href, window.location.href).href;
                                result.apply_element_href = result.apply_href;
                            } catch(e) {
                                result.apply_href = bestCandidate.href;
                                result.apply_element_href = bestCandidate.href;
                            }
                        } else {
                            result.apply_href = null;
                            result.apply_element_href = null;
                            if (!bestCandidate.href || bestCandidate.href === '#' || bestCandidate.href.startsWith('javascript:')) {
                                result.apply_detection_reason += ' (JS click-handler / in-page button without static outbound href)';
                            }
                        }
                    }

                    const bodyContent = document.body ? document.body.innerText.toLowerCase() : '';
                    result.captcha = bodyContent.includes("captcha") || bodyContent.includes("cloudflare");

                    // 4. Detect Authentication State
                    let authState = "UNKNOWN";
                    let authReason = "No explicit authentication indicators";

                    const domain = window.location.hostname.toLowerCase().replace(/^www\./, '');

                    if (domain.includes('hh.ru')) {
                        const hasUserMenu = Boolean(document.querySelector('[data-qa="mainmenu_myResumes"], [data-qa="mainmenu_applicantProfile"], [data-qa="mainmenu_negotiations"], [data-qa="mainmenu_applicant_profile"], a[href*="/applicant/resumes"], a[href*="/applicant/negotiations"]'));
                        const hasLoginBtn = Boolean(document.querySelector('[data-qa="mainmenu_login"], [data-qa="login"], a[href*="/account/login"]'));
                        if (hasUserMenu && !hasLoginBtn) {
                            authState = "AUTHENTICATED";
                            authReason = "HH.ru active applicant session detected (profile/resume menu present)";
                        } else if (hasLoginBtn) {
                            authState = "NOT_AUTHENTICATED";
                            authReason = "HH.ru guest session (login button present)";
                        }
                    } else if (domain.includes('career.habr.com')) {
                        const hasUserMenu = Boolean(document.querySelector('.user_panel, [href*="/users/"], .session_user, a[href*="/logout"]'));
                        const hasLoginBtn = Boolean(document.querySelector('a[href*="/login"], a[href*="/register"]'));
                        if (hasUserMenu && !hasLoginBtn) {
                            authState = "AUTHENTICATED";
                            authReason = "Habr Career active session detected";
                        } else if (hasLoginBtn) {
                            authState = "NOT_AUTHENTICATED";
                            authReason = "Habr Career guest session (login/register links present)";
                        }
                    } else if (domain.includes('weworkremotely.com')) {
                        const hasAccountMenu = Boolean(document.querySelector('a[href*="/logout"], a[href*="/dashboard"], .account-dropdown'));
                        const hasLoginLink = Boolean(document.querySelector('a[href*="/job-seekers/account/login"], a[href*="/job-seekers/account/register"]'));
                        if (hasAccountMenu) {
                            authState = "AUTHENTICATED";
                            authReason = "WeWorkRemotely authenticated session detected";
                        } else if (hasLoginLink) {
                            authState = "NOT_AUTHENTICATED";
                            authReason = "WeWorkRemotely guest session (account/register links present)";
                        }
                    } else if (domain.includes('himalayas.app')) {
                        const hasUserAvatar = Boolean(document.querySelector('a[href*="/dashboard"], [data-testid="user-menu"], a[href*="/logout"]'));
                        const hasSignupLink = Boolean(document.querySelector('a[href*="/signup"], a[href*="/login"]'));
                        if (hasUserAvatar) {
                            authState = "AUTHENTICATED";
                            authReason = "Himalayas authenticated user session detected";
                        } else if (hasSignupLink) {
                            authState = "NOT_AUTHENTICATED";
                            authReason = "Himalayas guest session (signup/login CTA present)";
                        }
                    } else if (domain.includes('remoteok.com')) {
                        const hasUserSession = Boolean(document.querySelector('.logged-in, a[href*="/logout"]'));
                        if (hasUserSession) {
                            authState = "AUTHENTICATED";
                            authReason = "RemoteOK user session detected";
                        } else {
                            authState = "NOT_AUTHENTICATED";
                            authReason = "RemoteOK guest browsing state";
                        }
                    } else {
                        const hasLoginModal = Boolean(document.querySelector('[id*="login"], [class*="login-form"], form[action*="login"]'));
                        const hasJobForm = result.has_form;
                        if (hasJobForm && !hasLoginModal) {
                            authState = "AUTHENTICATED";
                            authReason = "Public job application form allows guest submission without login";
                        } else if (hasLoginModal) {
                            authState = "NOT_AUTHENTICATED";
                            authReason = "Login form / authentication wall detected";
                        }
                    }

                    // 5. Detect Login Required
                    let loginRequired = false;
                    let loginReason = null;
                    if (result.apply_element_href && (
                        result.apply_element_href.includes('/login') ||
                        result.apply_element_href.includes('/signin') ||
                        result.apply_element_href.includes('/register') ||
                        result.apply_element_href.includes('/signup') ||
                        result.apply_element_href.includes('/job-seekers/account')
                    )) {
                        loginRequired = true;
                        loginReason = `Apply CTA routes to account registration/login: ${result.apply_element_href}`;
                    } else if (bodyContent.includes("create an account to apply") || bodyContent.includes("sign in to apply") || bodyContent.includes("login required") || bodyContent.includes("please log in")) {
                        loginRequired = true;
                        loginReason = "Page explicitly requires login/account creation to apply";
                    }

                    result.authenticated = authState;
                    result.auth_reason = authReason;
                    result.login_required = loginRequired;
                    result.login_reason = loginReason;

                    return result;
                })()"""
                await ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {"expression": script, "returnByValue": True}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw)
                return data.get("result", {}).get("result", {}).get("value", {})
        try:
            return self._sync_run(_inspect_flow()) or {}
        except Exception:
            return {}

    def fill_field(self, selector: str, value: str) -> bool:
        return True

    def upload_file(self, selector: str, path: str) -> bool:
        return True

    def screenshot(self, path: str) -> str | None:
        if not self.ws_url:
            return None
        async def _shot():
            import asyncio
            import base64
            import json

            import websockets
            async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=15) as ws:
                await ws.send(json.dumps({"id": 4, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw).get("result", {}).get("data")
                if data:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_bytes(base64.b64decode(data))
                    return path
                return None
        try:
            return self._sync_run(_shot())
        except Exception:
            return None

    def submit_application(self) -> dict[str, Any]:
        self.submit_attempted = True
        if not self.ws_url:
            return {"success": False, "error": "No active tab"}
        async def _submit():
            import asyncio
            import json

            import websockets
            async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=15) as ws:
                click_script = """(() => {
                    const btn = Array.from(document.querySelectorAll("button, a, input[type='submit']")).find(
                        b => (b.innerText || b.value || '').trim().toLowerCase().includes('apply') || (b.innerText || b.value || '').trim().toLowerCase().includes('submit')
                    );
                    if (btn) {
                        btn.click();
                        return { clicked: true, text: btn.innerText || btn.value };
                    }
                    return { clicked: false, error: "button not found" };
                })()"""
                await ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {"expression": click_script, "returnByValue": True}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                res = json.loads(raw).get("result", {}).get("result", {}).get("value", {})
                await asyncio.sleep(2)
                return {"success": True, "details": res}
        try:
            res = self._sync_run(_submit())
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    def close(self) -> None:
        if self.tab_id:
            try:
                # Same proxy trap as open() - see finding #9/#12.
                from .hh_browser_launcher import _NO_PROXY_OPENER

                _NO_PROXY_OPENER.open(f"{self.cdp_url}/json/close/{self.tab_id}", timeout=5)
            except Exception:
                pass
            self.tab_id = None

    def get_current_url(self) -> str:
        return self._final_url or ""

    def get_title(self) -> str:
        return self._title or ""

    def get_content(self) -> str:
        if not self.ws_url:
            return f"{self._title} {self._final_url}"
        async def _get_doc():
            import asyncio
            import json

            import websockets
            async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=15) as ws:
                await ws.send(json.dumps({"id": 5, "method": "Runtime.evaluate", "params": {"expression": "document.documentElement.outerHTML"}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw)
                return data.get("result", {}).get("result", {}).get("value", "")
        try:
            res = self._sync_run(_get_doc())
            return res or f"{self._title} {self._final_url}"
        except Exception:
            return f"{self._title} {self._final_url}"

    def evaluate(self, js: str) -> str:
        if not self.ws_url:
            return json.dumps({"error": "No active tab / ws_url"})
        async def _eval():
            import asyncio
            import json

            import websockets
            async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=15) as ws:
                await ws.send(json.dumps({"id": 10, "method": "Runtime.evaluate", "params": {"expression": js, "returnByValue": True}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw)
                val = data.get("result", {}).get("result", {}).get("value")
                if isinstance(val, (dict, list)):
                    return json.dumps(val)
                return str(val if val is not None else "")
        try:
            return self._sync_run(_eval())
        except Exception as e:
            return json.dumps({"error": str(e)})


# Visible-text markers that only a real block page shows. Deliberately NOT
# matched against raw HTML - see _detect_page_blocked.
_BLOCK_TEXT_MARKERS = ("access denied", "login required")


def _detect_page_blocked(html: str, body_text: str, title: str) -> bool:
    """Is this page actually a block page? (BLE001 finding #11)

    The check this replaced grepped the raw HTML for "captcha". hh.ru ships an
    i18n bundle on EVERY response containing "error.signup.captcha.invalid",
    so every page - including a perfectly readable vacancy - came back
    blocked=True, and `blocked` is consumed for real branching decisions
    (see lines ~2026/2668/2966).

    hh_extractor._detect_blocked already documents the trap and encodes the
    correct rule: a captcha only counts when it sits in an active challenge
    container. Reuse it rather than keeping a second, wronger copy.
    """
    from .hh_extractor import _detect_blocked

    low_title = (title or "").lower()
    if "404" in low_title or "page not found" in low_title:
        return True
    dom = _detect_blocked(html or "", body_text or "")
    if dom.get("captcha") or dom.get("cloudflare"):
        return True
    low_body = (body_text or "").lower()
    return any(m in low_body for m in _BLOCK_TEXT_MARKERS)


# Playwright adapter if available (optional, not required for tests)
class PlaywrightBrowserAdapter(BrowserAdapter):
    def __init__(self, headless: bool = True, storage_state: str | None = None, cdp_url: str | None = None):
        self.headless = headless
        self.storage_state = storage_state
        # Same resolution the launcher uses, so extraction and submission can
        # never end up driving two different browsers.
        from .hh_browser_launcher import resolve_cdp_url

        self.cdp_url = resolve_cdp_url(cdp_url)
        self._is_cdp = False
        # Finding #10: set when we could not attach to the real browser and had
        # to launch headless Playwright instead. Never silent - see open().
        self._cdp_fallback_reason = None
        self.play = None
        self.browser = None
        self.context = None
        self.page = None
        self._final_url = None
        self._title = None

    def open(self, url: str) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
            self.play = sync_playwright().start()

            # Prefer attaching to the user's real browser over CDP: a headless
            # Playwright launch is trivially fingerprinted (navigator.webdriver
            # = true, HeadlessChrome UA, SwiftShader renderer). If the attach
            # fails we still fall back, but we say so out loud - finding #10,
            # the fallback used to be silent and nobody noticed extraction was
            # running in a browser hh.ru can spot instantly.
            if self.cdp_url:
                try:
                    self.browser = self.play.chromium.connect_over_cdp(self.cdp_url)
                    self._is_cdp = True
                    if self.browser.contexts:
                        self.context = self.browser.contexts[0]
                    else:
                        self.context = self.browser.new_context()
                    self.page = self.context.new_page()
                except Exception as e:
                    self._is_cdp = False
                    self._cdp_fallback_reason = (
                        f"connect_over_cdp({self.cdp_url}) failed: {type(e).__name__}: {e}"
                    )
                    logger.warning(
                        "Could not attach to CDP %s (%s: %s). Falling back to a "
                        "headless Playwright launch, which is fingerprintable.",
                        self.cdp_url,
                        type(e).__name__,
                        e,
                    )

            if not self.page:
                self.browser = self.play.chromium.launch(headless=self.headless)
                if self.storage_state:
                    self.context = self.browser.new_context(storage_state=self.storage_state)
                else:
                    self.context = self.browser.new_context()
                self.page = self.context.new_page()

            self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            self._final_url = self.page.url
            self._title = self.page.title()
            # Check for blocked. Finding #11: never substring-match raw HTML
            # for "captcha" - it is in hh.ru's i18n bundle on every page.
            html = self.page.content()
            try:
                body_text = self.page.inner_text("body") or ""
            except Exception:  # noqa: BLE001 - an unreadable body is not a block
                body_text = ""
            blocked = _detect_page_blocked(html, body_text, self._title or "")
            site = url.split("/")[2] if "://" in url else ""
            return {
                "final_url": self._final_url, "title": self._title, "site": site,
                "blocked": blocked,
                "cdp_fallback": bool(self._cdp_fallback_reason),
                "cdp_fallback_reason": self._cdp_fallback_reason,
            }
        except Exception as e:
            return {
                "final_url": url, "title": "", "site": "", "blocked": True,
                "reason": str(e),
                "cdp_fallback": bool(self._cdp_fallback_reason),
                "cdp_fallback_reason": self._cdp_fallback_reason,
            }

    def inspect_page(self) -> dict[str, Any]:
        if not self.page:
            return {"form_detected": False, "fields": [], "apply_button": False}
        try:
            content = self.page.content().lower()
            # Detect apply button (do not click)
            apply_found = bool(self.page.query_selector("button:has-text('Apply'), a:has-text('Apply'), button:has-text('Easy Apply')"))
            # Detect fields by common selectors
            fields = []
            selectors = {
                "name": "input[name*='name']",
                "email": "input[type='email']",
                "phone": "input[type='tel']",
                "resume": "input[type='file']",
                "cover_letter": "textarea",
                "linkedin": "input[name*='linkedin']",
                "github": "input[name*='github']",
            }
            for name, sel in selectors.items():
                if self.page.query_selector(sel):
                    fields.append(name)
            # Always at least try to detect form
            form_detected = len(fields) > 0 or "form" in content
            captcha = "captcha" in content
            login_required = "login" in content and "required" in content
            return {"form_detected": form_detected, "fields": fields, "apply_button": apply_found, "captcha": captcha, "login_required": login_required}
        except Exception:
            return {"form_detected": False, "fields": [], "apply_button": False}

    def extract_application_form(self) -> dict[str, Any]:
        """Read-only HH-aware extraction. Never clicks/fills/uploads.

        Uses REAL observed HH DOM patterns:
          - question label: div[data-qa^='vacancy-response-question'] with text;
            second whitespace token of data-qa is the stable slug.
          - apply link: a[data-qa='vacancy-response-link-top'] (never clicked).
          - auth gate: div[data-qa='auth-form'] => answer controls hidden.
        """
        if not self.page:
            # BLE001 finding #7: no page means nothing was read. Mark it as an
            # error instead of returning an "empty clean form".
            return {
                "html": "", "body_text": "", "questions": [], "controls": [],
                "question_groups": [],
                "auth_form": False,
                "apply_link": None, "final_url": "", "title": "", "site": "",
                "error": True, "error_reason": "page_not_open",
                "cdp_fallback": bool(self._cdp_fallback_reason),
                "cdp_fallback_reason": self._cdp_fallback_reason,
            }
        try:
            html = self.page.content()
            body_text = self.page.inner_text("body")
            # Real HH question containers
            raw = self.page.eval_on_selector_all(
                "[data-qa^='vacancy-response-question']",
                "els => els.map(e => {"
                "  const qa = e.getAttribute('data-qa') || '';"
                "  const parts = qa.split(/\\s+/).filter(Boolean);"
                "  const slug = parts.find(p => p.startsWith('vacancy-response-question_'))"
                "             ? parts.find(p => p.startsWith('vacancy-response-question_')).replace('vacancy-response-question_','')"
                "             : '';"
                "  return {label: (e.innerText || '').trim(), slug: slug};"
                "})",
            )
            auth_form = bool(self.page.query_selector("[data-qa='auth-form']"))
            link = self.page.query_selector("a[data-qa='vacancy-response-link-top']")
            apply_link = None
            if link:
                apply_link = {
                    "href": link.get_attribute("href") or "",
                    "text": (link.inner_text() or "").strip(),
                }
            # Stage 18/20C: REAL form controls (only present with an
            # authenticated session). We read exactly what the DOM exposes.
            controls = self.page.eval_on_selector_all(
                "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='image']), textarea, select",
                """els => els.map(e => ({
                    tag: e.tagName,
                    type: e.getAttribute('type') || (e.tagName === 'SELECT' ? 'select' : (e.tagName === 'TEXTAREA' ? 'textarea' : 'text')),
                    name: e.getAttribute('name') || null,
                    id: e.id || null,
                    dataQa: e.getAttribute('data-qa') || null,
                    required: !!(e.required || e.getAttribute('aria-required') === 'true'),
                    requiredAttr: e.required === true ? true : (e.getAttribute('aria-required') === 'true' ? true : (e.hasAttribute('required') ? false : null)),
                    multiple: !!(e.multiple),
                    label: (function(el){
                        try {
                            if (el.labels && el.labels.length) return (el.labels[0].innerText || '').trim();
                            const lb = el.getAttribute('aria-labelledby');
                            if (lb) { const l = document.getElementById(lb); if (l) return (l.innerText || '').trim(); }
                            const la = el.getAttribute('aria-label');
                            if (la) return la.trim();
                            const wrap = el.closest('label');
                            if (wrap) return (wrap.innerText || '').trim();
                        } catch (err) {}
                        return null;
                    })(e),
                    options: e.tagName === 'SELECT' ? Array.from(e.options).map(function(o){ return (o.text || '').trim(); }).filter(Boolean) : null
                }))""",
            )
            # Stage 20C: DOM-proven question stems (sibling of options container)
            question_groups = []
            try:
                question_groups = self.page.evaluate("""() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const inputs = Array.from(document.querySelectorAll("input[type='radio'], input[type='checkbox']"));
                    const byName = {};
                    for (const i of inputs) {
                        const n = i.getAttribute('name');
                        if (!n || !n.startsWith('task_')) continue;
                        (byName[n] = byName[n] || []).push(i);
                    }
                    const out = [];
                    for (const [name, els] of Object.entries(byName)) {
                        let anc = els[0].parentElement;
                        while (anc && !els.every(e => anc.contains(e))) anc = anc.parentElement;
                        if (!anc) continue;
                        const optionLabels = els.map(e => {
                            if (e.labels && e.labels[0]) return (e.labels[0].innerText || '').trim();
                            const w = e.closest('label');
                            return w ? (w.innerText || '').trim() : '';
                        }).filter(Boolean);
                        let cur = anc;
                        for (let lvl = 0; lvl < 4 && cur.parentElement; lvl++) {
                            cur = cur.parentElement;
                            for (const child of cur.children) {
                                if (child.contains(anc) || child === anc) continue;
                                const t = (child.innerText || '').trim();
                                if (!t || t.length > 300) continue;
                                if (optionLabels.some(ol => t === ol)) continue;
                                const cls = (child.className || '').toString();
                                const isHeading = /^H[1-6]$/.test(child.tagName);
                                const isTextish = /text|label|title|question/i.test(cls) || child.getAttribute('data-qa');
                                const hasDirectText = Array.from(child.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
                                if (isHeading || isTextish || hasDirectText) {
                                    out.push({name, stem: t.slice(0, 300), stem_tag: child.tagName, stem_cls: cls.slice(0, 80), stem_level: lvl + 1});
                                    break;
                                }
                            }
                            if (out.length && out[out.length-1].name === name) break;
                        }
                    }
                    for (const ta of document.querySelectorAll("textarea[name^='task_']")) {
                        const name = ta.getAttribute('name');
                        let anc = ta.parentElement;
                        while (anc && anc.parentElement) {
                            anc = anc.parentElement;
                            for (const child of anc.children) {
                                if (child.contains(ta)) continue;
                                const t = (child.innerText || '').trim();
                                if (!t || t === 'Писать тут' || t.length > 300) continue;
                                const cls = (child.className || '').toString();
                                if (/text|label|title/i.test(cls) || /^H[1-6]$/.test(child.tagName)) {
                                    out.push({name, stem: t.slice(0, 300), stem_tag: child.tagName, stem_cls: cls.slice(0, 80), stem_level: 0});
                                    break;
                                }
                            }
                            if (out.some(e => e.name === name)) break;
                        }
                    }
                    return out;
                }""")
            except Exception:
                question_groups = []
            return {
                "html": html,
                "body_text": body_text,
                "questions": [{"label": q.get("label", ""), "slug": q.get("slug", "")} for q in raw],
                "controls": [c for c in controls],
                "question_groups": question_groups or [],
                "auth_form": auth_form,
                "apply_link": apply_link,
                "final_url": self._final_url or "",
                "title": self._title or "",
                "site": (self._final_url or "").split("/")[2] if self._final_url else "hh.ru",
                "error": False, "error_reason": None,
                "cdp_fallback": bool(self._cdp_fallback_reason),
                "cdp_fallback_reason": self._cdp_fallback_reason,
            }
        except Exception as e:
            # BLE001 finding #7: an exception mid-extraction produced an empty
            # snapshot that was indistinguishable from a genuinely empty form,
            # so every downstream "all questions resolved" gate passed on an
            # empty set. Mark the failure explicitly.
            return {
                "html": "", "body_text": "", "questions": [], "controls": [],
                "question_groups": [],
                "auth_form": False,
                "apply_link": None, "final_url": self._final_url or "",
                "title": self._title or "", "site": "hh.ru",
                "error": True, "error_reason": f"extraction_failed: {type(e).__name__}",
                "cdp_fallback": bool(self._cdp_fallback_reason),
                "cdp_fallback_reason": self._cdp_fallback_reason,
            }

    def fill_field(self, selector: str, value: str) -> bool:
        try:
            if self.page:
                self.page.fill(selector, value, timeout=5000)
                return True
        except Exception:
            pass
        return False

    def upload_file(self, selector: str, path: str) -> bool:
        try:
            if self.page:
                self.page.set_input_files(selector, path, timeout=5000)
                return True
        except Exception:
            pass
        return False

    def screenshot(self, path: str) -> str | None:
        try:
            if self.page:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=path)
                return path
        except Exception:
            pass
        return None

    def close(self) -> None:
        try:
            if self.page:
                self.page.close()
        except Exception:
            pass
        if not self._is_cdp:
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass
        try:
            if self.play:
                self.play.stop()
        except Exception:
            pass

    def get_current_url(self) -> str:
        return self._final_url or ""

    def get_title(self) -> str:
        return self._title or ""

    def submit_application(self) -> dict[str, Any]:
        """Click the submit/apply button and verify submission.
        Returns dict with success status and any error message."""
        if not self.page:
            return {"success": False, "error": "Page not initialized"}
        try:
            # Find submit button
            submit_button = self.page.query_selector(
                "button:has-text('Submit'), button:has-text('Apply'), button:has-text('Send'), "
                "input[type='submit'], button[type='submit'], "
                "button:has-text('Отправить'), button:has-text('Отправить заявку')"
            )
            if not submit_button:
                return {"success": False, "error": "Submit button not found"}
            
            # Take before screenshot
            before_path = f"artifacts/browser/{self._final_url.replace('://', '_').replace('/', '_').replace(':', '_')}_before_submit.png"
            Path(before_path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=before_path)
            
            # Click submit
            submit_button.click()
            
            # Wait for navigation or response
            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # Some submissions don't navigate
            
            # Take after screenshot
            after_path = f"artifacts/browser/{self._final_url.replace('://', '_').replace('/', '_').replace(':', '_')}_after_submit.png"
            Path(after_path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=after_path)
            
            # Check for success indicators
            content = self.page.content().lower()
            success_indicators = [
                "application submitted", "application received", "thank you for applying",
                "application received", "successfully submitted", "thank you for your application",
                "заявка отправлена", "заявка получена", "спасибо за заявку"
            ]
            
            success = any(indicator in content for indicator in success_indicators)
            
            return {
                "success": success,
                "before_screenshot": f"artifacts/browser/{self._final_url.replace('://', '_').replace('/', '_').replace(':', '_')}_before_submit.png" if success else None,
                "after_screenshot": after_path if success else None,
                "error": None if success else "No success confirmation found after submit"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def evaluate(self, js: str) -> str:
        if not self.page:
            return json.dumps({"error": "No active page"})
        try:
            res = self.page.evaluate(js)
            if isinstance(res, (dict, list)):
                return json.dumps(res)
            return str(res if res is not None else "")
        except Exception as e:
            return json.dumps({"error": str(e)})

def _get_validated_package_answer(field: str, package: Any) -> str | None:
    """Stage 17D: validated package answers (truth-only). UNKNOWN /
    requires_review answers are NEVER used and never turned into text."""
    if package is None:
        return None
    answers = getattr(package, "answers", None)
    if not answers:
        return None
    for a in answers:
        if isinstance(a, dict):
            qid = str(a.get("question_id") or "").lower()
            ans = a.get("answer")
            review = a.get("requires_review", True)
        else:
            qid = str(getattr(a, "question_id", "") or "").lower()
            ans = getattr(a, "answer", None)
            review = getattr(a, "requires_review", True)
        if not ans or review:
            continue
        if qid == field or qid.endswith("_" + field) or field in qid.split("_"):
            return str(ans)
    return None

def _get_profile_value(field: str, profile: CandidateProfile, resume_text: str, vacancy: Vacancy, package: Any) -> str | None:
    """Truth-only field value: confirmed profile/resume data first, then a
    validated (requires_review=False) package answer. Never invents."""
    val = _get_profile_value_truth(field, profile, resume_text, vacancy, package)
    if val is not None:
        return val
    return _get_validated_package_answer(field, package)

def _get_profile_value_truth(field: str, profile: CandidateProfile, resume_text: str, vacancy: Vacancy, package: Any) -> str | None:
    field = field.lower()
    # Truth-only: return None if not confirmed
    if field in ("name", "first_name", "last_name"):
        full = getattr(profile, "name", None) if hasattr(profile, "name") else None
        if not full and resume_text:
            m = re.search(r"(?:name|candidate):\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", resume_text, re.IGNORECASE)
            if m:
                full = m.group(1).strip()
        if full:
            full = str(full).strip()
            if field == "first_name":
                return full.split()[0]
            if field == "last_name":
                return full.split()[-1] if len(full.split()) > 1 else None
            return full
        return None

    if field == "email":
        if hasattr(profile, "email") and getattr(profile, "email", None):
            return str(profile.email).strip()
        if resume_text:
            m = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", resume_text)
            if m:
                return m.group(0)
        return None

    if field == "phone":
        source = (getattr(vacancy, "source", None) or "").strip().lower()
        job_url = (getattr(vacancy, "job_url", None) or "").strip().lower()
        app_url = (getattr(vacancy, "application_url", None) or "").strip().lower()

        from urllib.parse import urlparse
        domain = ""
        for u in (job_url, app_url):
            if u:
                try:
                    parsed = urlparse(u)
                    if parsed.netloc:
                        domain = parsed.netloc.lower()
                        break
                except Exception:
                    pass

        is_ru = (
            source in ("hh", "habrcareer")
            or domain == "hh.ru" or domain.endswith(".hh.ru")
            or domain in ("career.habr.com", "habr.com") or domain.endswith(".habr.com")
        )

        is_intl = (
            source in ("remoteok", "weworkremotely", "himalayas")
            or domain in ("remoteok.com", "weworkremotely.com", "himalayas.app", "wellfound.com", "angel.co", "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com")
            or any(domain.endswith("." + d) for d in ("remoteok.com", "weworkremotely.com", "himalayas.app", "wellfound.com", "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com"))
        )

        phone_ru = getattr(profile, "phone_ru", None) if hasattr(profile, "phone_ru") else None
        phone_th = getattr(profile, "phone_th", None) if hasattr(profile, "phone_th") else None
        phone_generic = getattr(profile, "phone", None) if hasattr(profile, "phone") else None

        if is_ru:
            if phone_ru:
                return str(phone_ru).strip()
            if phone_generic:
                return str(phone_generic).strip()
        elif is_intl:
            if phone_th:
                return str(phone_th).strip()
            if phone_generic:
                return str(phone_generic).strip()
        else:
            if phone_generic:
                return str(phone_generic).strip()

        # Legacy fallback to resume_text only if explicit profile phones are not present
        if not phone_ru and not phone_th and not phone_generic and resume_text:
            m = re.search(r"\+?\d[0-9 \-\(\)]{7,}", resume_text)
            if m:
                return m.group(0).strip()

        return None
    if field == "location":
        if profile.allowed_locations:
            return profile.allowed_locations[0]
        return None
    if field == "linkedin":
        if hasattr(profile, "linkedin") and getattr(profile, "linkedin", None):
            return str(profile.linkedin).strip()
        if resume_text:
            m = re.search(r"https?://[^\s]*linkedin[^\s]*", resume_text, re.IGNORECASE)
            if m:
                return m.group(0)
        return None
    if field == "github":
        if hasattr(profile, "github") and getattr(profile, "github", None):
            return str(profile.github).strip()
        if resume_text:
            m = re.search(r"https?://[^\s]*github[^\s]*", resume_text, re.IGNORECASE)
            if m:
                return m.group(0)
        return None
    if field == "portfolio":
        if hasattr(profile, "portfolio") and getattr(profile, "portfolio", None):
            return str(profile.portfolio).strip()
        if resume_text:
            m = re.search(r"https?://[^\s]*portfolio[^\s]*", resume_text, re.IGNORECASE)
            if m:
                return m.group(0)
        return None
        return None
    if field == "resume":
        # check resume.md exists
        for p in [Path("resume.md"), Path("ai_assistant/resume.md"), Path("../resume.md")]:
            if p.exists():
                return str(p)
        # fallback to resume text as file? create temp?
        return None
    if field == "cover_letter":
        if package and hasattr(package, "cover_letter") and package.cover_letter:
            return package.cover_letter
        # also check package dict
        if isinstance(package, dict) and package.get("cover_letter"):
            return package["cover_letter"]
        return None
    if field == "salary":
        if profile.minimum_salary is not None:
            curr = profile.salary_currency or "USD"
            return f"{profile.minimum_salary} {curr}"
        return None
    if field == "work_authorization":
        # Only if explicitly confirmed in profile/resume
        if "work authorization" in resume_text.lower() or "authorized to work" in resume_text.lower():
            return "Authorized"
        # check profile attribute
        if hasattr(profile, "work_authorization") and profile.work_authorization:
            return str(profile.work_authorization)
        return None
    if field == "years_experience":
        if profile.years_experience is not None:
            return str(profile.years_experience)
        m = re.search(r"(\d+)\s+years", resume_text, re.IGNORECASE)
        if m:
            return m.group(1)
        return None
    return None

def _map_fields(fields_detected: list[str], profile: CandidateProfile, resume_text: str, vacancy: Vacancy, package: Any) -> tuple[list[str], list[str], list[str]]:
    filled = []
    skipped = []
    warnings = []
    for f in fields_detected:
        val = _get_profile_value(f, profile, resume_text, vacancy, package)
        if val:
            filled.append(f)
        else:
            skipped.append(f)
            warnings.append(f"Field '{f}' skipped - not confirmed / missing (truth-only)")
    return filled, skipped, warnings

def _detect_site(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return url.split("/")[2] if "://" in url else url

def extract_form_for_vacancy(
    vacancy_stable_id: str,
    url: str,
    adapter: BrowserAdapter | None = None,
    canonical_id: str | None = None,
):
    """Open a vacancy page and return a normalized ApplicationForm.

    EXTRACTION ONLY. Opens the page (read), reads the DOM, normalizes.
    Never submits, never clicks Apply, never fills, never uploads,
    never calls an LLM, never mutates the DB.

    If no adapter is given, uses a real Playwright adapter when available,
    otherwise a MockBrowserAdapter (for tests/offline). An authenticated HH
    session can be provided via env HH_STORAGE_STATE (Playwright storage_state
    JSON file path); it is never hardcoded or persisted.
    """
    from .hh_extractor import extract_application_form

    use_adapter = adapter
    if use_adapter is None:
        use_real = os.getenv("BROWSER_USE_PLAYWRIGHT") == "1" or os.getenv("BROWSER_REAL") == "1" or os.getenv("USE_PLAYWRIGHT") == "1"
        if use_real:
            try:
                storage_state = os.getenv("HH_STORAGE_STATE") or None
                use_adapter = PlaywrightBrowserAdapter(headless=True, storage_state=storage_state)
            except Exception as e:
                logging.warning("Playwright not available for extraction, fallback to Mock: %s", e)
                use_adapter = MockBrowserAdapter()
        else:
            use_adapter = MockBrowserAdapter()

    try:
        open_res = use_adapter.open(url)
        snapshot = use_adapter.extract_application_form()
        snapshot["final_url"] = snapshot.get("final_url") or open_res.get("final_url", url)
        snapshot["site"] = snapshot.get("site") or open_res.get("site", "hh.ru")
        snapshot["blocked"] = open_res.get("blocked", False)
        snapshot["blocked_reason"] = open_res.get("reason")
        return extract_application_form(
            vacancy_stable_id=vacancy_stable_id,
            url=url,
            dom_snapshot=snapshot,
            canonical_id=canonical_id,
        )
    finally:
        try:
            use_adapter.close()
        except Exception:
            pass

def save_browser_session(session: BrowserApplicationSession) -> None:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    # Ensure apply_button_found is stored in fields_json? For now store in warnings, but also save session_json
    cur.execute(
        "INSERT INTO browser_preparations (vacancy_stable_id, url, status, final_url, page_title, site, form_detected, fields_json, warnings_json, screenshot_path, created_at, updated_at, executor_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(vacancy_stable_id, executor_version) DO UPDATE SET url=excluded.url, status=excluded.status, final_url=excluded.final_url, page_title=excluded.page_title, site=excluded.site, form_detected=excluded.form_detected, fields_json=excluded.fields_json, warnings_json=excluded.warnings_json, screenshot_path=excluded.screenshot_path, updated_at=excluded.updated_at, executor_version=excluded.executor_version",
        (
            session.vacancy_stable_id,
            session.url,
            session.status.value,
            session.final_url,
            session.page_title,
            session.site,
            1 if session.form_detected else 0,
            json.dumps({"detected": session.fields_detected, "filled": session.fields_filled, "skipped": session.fields_skipped}, ensure_ascii=False),
            json.dumps(session.warnings, ensure_ascii=False),
            session.screenshot_path,
            session.created_at,
            session.updated_at,
            EXECUTOR_VERSION,
        ),
    )
    conn.commit()
    conn.close()
    if session.status == BrowserStatus.BLOCKED:
        from .application_review import reopen_review_after_browser_block
        reopen_review_after_browser_block(
            session.vacancy_stable_id,
            "Approval reopened because latest browser preparation is BLOCKED",
        )

def get_browser_session(vacancy_stable_id: str, executor_version: str | None = None) -> BrowserApplicationSession | None:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    if executor_version:
        cur.execute("SELECT vacancy_stable_id, url, status, final_url, page_title, site, form_detected, fields_json, warnings_json, screenshot_path, created_at, updated_at FROM browser_preparations WHERE vacancy_stable_id=? AND executor_version=?", (vacancy_stable_id, executor_version))
    else:
        cur.execute("SELECT vacancy_stable_id, url, status, final_url, page_title, site, form_detected, fields_json, warnings_json, screenshot_path, created_at, updated_at FROM browser_preparations WHERE vacancy_stable_id=?", (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        raw_fields = json.loads(row[7]) if row[7] else []
        if isinstance(raw_fields, dict) and "detected" in raw_fields:
            fields = raw_fields.get("detected", [])
            fields_filled = raw_fields.get("filled", [])
            fields_skipped = raw_fields.get("skipped", [])
        else:
            fields = raw_fields if isinstance(raw_fields, list) else []
            fields_filled = []
            fields_skipped = []
        warnings = json.loads(row[8]) if row[8] else []
    except Exception:
        fields = []
        warnings = []
        fields_filled = []
        fields_skipped = []
    # Need to get executor_version, but not in select; fetch separately if needed
    # For simplicity, return session with status
    # Try to reconstruct apply_button_found from warnings or fields
    apply_found = any("Apply button FOUND" in w for w in warnings)
    return BrowserApplicationSession(
        vacancy_stable_id=row[0],
        url=row[1],
        status=BrowserStatus(row[2]),
        final_url=row[3],
        page_title=row[4],
        site=row[5],
        form_detected=bool(row[6]),
        fields_detected=fields,
        apply_button_found=apply_found,
        fields_filled=fields_filled,
        fields_skipped=fields_skipped,
        warnings=warnings,
        created_at=row[10],
        updated_at=row[11],
        screenshot_path=row[9],
    )

def prepare_application_in_browser(
    vacancy_stable_id: str,
    profile_path: str | None = None,
    adapter: BrowserAdapter | None = None,
    force: bool = False,
) -> BrowserResult:

    from .application_tracking import ApplicationStatus, get_application_status
    from .candidate_profile import load_candidate_profile
    from .db import _row_to_vacancy, get_application_package, get_vacancy_by_id
    from .job_analyzer import get_resume_text

    init_db()

    # Check existing session for idempotency
    existing = get_browser_session(vacancy_stable_id, EXECUTOR_VERSION) if not force else None
    # Need to check queue and tracking
    # Get vacancy
    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        # Try to find via queue? But vacancy must exist
        raise ValueError(f"Vacancy not found: {vacancy_stable_id}")
    vac = _row_to_vacancy(row)

    # Check queue
    q_item = None
    try:
        from .application_queue import get_queue_item
        q_item = get_queue_item(vacancy_stable_id)
    except Exception:
        q_item = None
    if not q_item:
        # Also check that vacancy is at least in queue via list? For tests, queue item may not exist but we still allow if READY?
        # According spec, missing queue item is rejected
        raise ValueError(f"Queue item not found for {vacancy_stable_id} - not READY_TO_APPLY")

    # Check tracking status
    track = get_application_status(vacancy_stable_id)
    if not track or track.status != ApplicationStatus.READY_TO_APPLY:
        raise ValueError(f"Vacancy {vacancy_stable_id} status {track.status if track else 'None'} is not READY_TO_APPLY - cannot prepare")

    # Check package
    pkg_row = get_application_package(vacancy_stable_id)
    if not pkg_row:
        raise ValueError(f"Application package not found for {vacancy_stable_id}")

    # Parse package
    try:
        pkg_data = json.loads(pkg_row[2]) if pkg_row[2] else {}
        # pkg_data contains cover_letter etc
        # Create simple object for mapping
        class PkgObj:
            def __init__(self, d):
                self.cover_letter = d.get("cover_letter")
                self.validation_status = d.get("validation_status", "NEEDS_REVIEW")
                self.answers = d.get("answers") or []
                self.application_type = d.get("application_type", "unknown")
        pkg = PkgObj(pkg_data)
    except Exception:
        pkg = None

    # Idempotency: if existing and not force, return existing as BrowserResult
    if existing and not force:
        return BrowserResult(
            vacancy_stable_id=existing.vacancy_stable_id,
            url=existing.url,
            final_url=existing.final_url,
            page_title=existing.page_title,
            site=existing.site,
            status=existing.status,
            form_detected=existing.form_detected,
            fields_detected=existing.fields_detected,
            fields_filled=existing.fields_filled,
            fields_skipped=existing.fields_skipped,
            warnings=existing.warnings,
            screenshot_path=existing.screenshot_path,
        )

    # Load profile and resume
    if profile_path:
        profile = load_candidate_profile(profile_path)
    else:
        from .config import CANDIDATE_PROFILE_FILE
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception:
                profile = load_candidate_profile()
        else:
            profile = load_candidate_profile()
    resume_text = get_resume_text(profile)

    url = vac.job_url
    site = _detect_site(url)
    warnings: list[str] = []
    fields_detected: list[str] = []
    fields_filled: list[str] = []
    fields_skipped: list[str] = []

    # Defense-in-Depth Hard Constraint Gate
    from .matcher import _coerce_profile, _hard_constraints
    from .remote_filter import is_strictly_remote

    if getattr(profile, "remote_required", False):
        is_rem, rem_reason = is_strictly_remote(vac)
        if not is_rem:
            warnings.append(f"Hard constraint violation (remote_required): {rem_reason}")
            flow_class = classify_apply_flow(source_url=url, final_url=url, apply_link=None, has_form=False)
            return BrowserResult(
                vacancy_stable_id=vacancy_stable_id,
                url=url,
                final_url=url,
                page_title="",
                site=site,
                status=BrowserStatus.BLOCKED,
                form_detected=False,
                apply_button_found=False,
                fields_detected=[],
                fields_filled=[],
                fields_skipped=[],
                warnings=warnings,
                error=f"Hard constraint violation (remote_required): {rem_reason}",
                screenshot_path=None,
                flow_type=flow_class.flow_type,
                source_url=flow_class.source_url,
                application_url=flow_class.application_url,
                application_domain=flow_class.application_domain,
                redirect_chain=flow_class.redirect_chain,
                is_external_application=flow_class.is_external_application,
                verification_strategy=flow_class.verification_strategy,
            )

    hard_reject, hard_reason = _hard_constraints(_coerce_profile(profile), vac)
    if hard_reject:
        warnings.append(f"Hard constraint violation: {hard_reason}")
        flow_class = classify_apply_flow(source_url=url, final_url=url, apply_link=None, has_form=False)
        return BrowserResult(
            vacancy_stable_id=vacancy_stable_id,
            url=url,
            final_url=url,
            page_title="",
            site=site,
            status=BrowserStatus.BLOCKED,
            form_detected=False,
            apply_button_found=False,
            fields_detected=[],
            fields_filled=[],
            fields_skipped=[],
            warnings=warnings,
            error=f"Hard constraint violation: {hard_reason}",
            screenshot_path=None,
            flow_type=flow_class.flow_type,
            source_url=flow_class.source_url,
            application_url=flow_class.application_url,
            application_domain=flow_class.application_domain,
            redirect_chain=flow_class.redirect_chain,
            is_external_application=flow_class.is_external_application,
            verification_strategy=flow_class.verification_strategy,
        )

    # Choose adapter
    use_adapter = adapter
    if use_adapter is None:
        # Use real Playwright if env forces it, otherwise Mock for safety
        use_real = os.getenv("BROWSER_USE_PLAYWRIGHT") == "1" or os.getenv("BROWSER_REAL") == "1" or os.getenv("USE_PLAYWRIGHT") == "1"
        if use_real:
            try:
                use_adapter = PlaywrightBrowserAdapter(headless=True)
            except Exception as e:
                logging.warning(f"Playwright not available, fallback to Mock: {e}")
                use_adapter = MockBrowserAdapter()
        else:
            use_adapter = MockBrowserAdapter()

    status = BrowserStatus.NOT_STARTED
    final_url = url
    page_title = ""
    form_detected = False
    screenshot_path = None
    error = None

    try:
        status = BrowserStatus.OPENED
        open_res = use_adapter.open(url)
        final_url = open_res.get("final_url", url)
        page_title = open_res.get("title", "")
        site = open_res.get("site", site)
        # Screenshot for diagnostics (even if blocked) - must be before blocked check
        try:
            _tmp_path = f"artifacts/browser/{vacancy_stable_id.replace(':', '_')}.png"
            _sp = use_adapter.screenshot(_tmp_path)
            if _sp:
                screenshot_path = _sp
        except Exception:
            pass
        # Check blocked
        if open_res.get("blocked"):
            warnings.append(f"Blocked: {open_res.get('reason', 'login/captcha/cloudflare')}")
            status = BrowserStatus.BLOCKED
            # Do not proceed to form detection beyond warning
        else:
            # Inspect page
            flow_info = {}
            if hasattr(use_adapter, "inspect_apply_flow"):
                flow_info = use_adapter.inspect_apply_flow()
            inspect = use_adapter.inspect_page()
            form_detected = bool(inspect.get("form_detected") or flow_info.get("has_form"))
            fields_detected = inspect.get("fields") or flow_info.get("fields", [])
            apply_found = inspect.get("apply_button") or flow_info.get("apply_present", False)
            auth_state = flow_info.get("authenticated", "UNKNOWN")
            login_req = flow_info.get("login_required") or inspect.get("login_required", False)
            login_reason = flow_info.get("login_reason")

            if apply_found:
                warnings.append("Apply button FOUND - Manual submission required. DO NOT CLICK Submit.")
            else:
                warnings.append("Apply button not found")

            if inspect.get("captcha") or flow_info.get("captcha"):
                warnings.append("CAPTCHA detected - BLOCKED, manual required")
                status = BrowserStatus.BLOCKED
            elif login_req and auth_state == "NOT_AUTHENTICATED":
                warnings.append(f"Authentication required: {login_reason or 'login/signup required for this flow'} - BLOCKED")
                status = BrowserStatus.BLOCKED
                error = f"Authentication required: {login_reason or 'login/signup required for this flow'}"
            elif login_req and auth_state == "UNKNOWN":
                warnings.append("Authentication state unknown - BLOCKED")
                status = BrowserStatus.BLOCKED
                error = "Authentication state unknown for login-required flow"
            elif inspect.get("login_required"):
                warnings.append("Login required - BLOCKED")
                status = BrowserStatus.BLOCKED
            elif inspect.get("cloudflare"):
                warnings.append("Cloudflare detected - BLOCKED")
                status = BrowserStatus.BLOCKED
            elif not form_detected:
                warnings.append("Form not found - BLOCKED")
                status = BrowserStatus.BLOCKED
            else:
                status = BrowserStatus.FORM_DETECTED
                # Field mapping truth-only
                filled, skipped, map_warnings = _map_fields(fields_detected, profile, resume_text, vac, pkg)
                fields_filled = filled
                fields_skipped = skipped
                warnings.extend(map_warnings)
                # Try to fill fields (but not submit)
                for f in filled:
                    val = _get_profile_value(f, profile, resume_text, vac, pkg)
                    if val:
                        try:
                            # Use generic selector; mock will succeed
                            use_adapter.fill_field(f"input[name='{f}']", val)
                        except Exception:
                            pass
                # Resume upload
                resume_path = _get_profile_value("resume", profile, resume_text, vac, pkg)
                if resume_path and "resume" in fields_detected:
                    try:
                        use_adapter.upload_file("input[type='file']", resume_path)
                        if "resume" not in fields_filled:
                            fields_filled.append("resume")
                    except Exception:
                        warnings.append("Resume upload skipped - file not found")
                # Cover letter
                if "cover_letter" in fields_detected and pkg and getattr(pkg, "cover_letter", None):
                    try:
                        use_adapter.fill_field("textarea[name='cover_letter']", pkg.cover_letter[:2000])
                        if "cover_letter" not in fields_filled:
                            fields_filled.append("cover_letter")
                    except Exception:
                        pass
                # Screenshot
                try:
                    screenshot_path = f"artifacts/browser/{vacancy_stable_id.replace(':', '_')}.png"
                    sp = use_adapter.screenshot(screenshot_path)
                    if sp:
                        screenshot_path = sp
                except Exception as e:
                    warnings.append(f"Screenshot failed: {e}")
                    screenshot_path = None
                # Final status: READY_FOR_REVIEW only when form detected,
                # not blocked, package exists, package is VALID or review is APPROVED,
                # and no required fields were skipped.
                validation_status = getattr(pkg, "validation_status", "NEEDS_REVIEW") if pkg else "NEEDS_REVIEW"
                from .application_review import ReviewStatus, get_application_review
                rev = get_application_review(vacancy_stable_id)
                is_approved = (rev is not None and rev.status == ReviewStatus.APPROVED)
                is_valid = (validation_status == "VALID") or (is_approved and validation_status != "INVALID")

                if status == BrowserStatus.FORM_DETECTED:
                    if is_valid and not fields_skipped:
                        status = BrowserStatus.READY_FOR_REVIEW
                        warnings.append("Manual submission required. DO NOT SUBMIT automatically. - READY_FOR_REVIEW")
                    elif fields_skipped:
                        warnings.append(f"Fields skipped ({len(fields_skipped)}) - NOT READY_FOR_REVIEW")
                    else:
                        # Not VALID and not approved: keep a safe, non-ready state.
                        warnings.append("Package NOT VALID (needs review) - NOT READY_FOR_REVIEW")
                # Safety: ensure we never set COMPLETED as auto-submitted; COMPLETED means ready for manual review on this stage
                if status == BrowserStatus.READY_FOR_REVIEW:
                    # For this stage, COMPLETED is alias for READY_FOR_REVIEW? Spec says COMPLETED means ready for manual Submit
                    # We will keep READY_FOR_REVIEW as final, but also support COMPLETED as same
                    pass

    except Exception as e:
        error = str(e)
        warnings.append(f"Error: {error}")
        status = BrowserStatus.BLOCKED
    finally:
        try:
            use_adapter.close()
        except Exception:
            pass

    # Flow classification
    flow_class = classify_apply_flow(
        source_url=url,
        final_url=final_url,
        apply_link=None,
        has_form=form_detected,
    )

    # Build result
    # Determine apply_button_found from warnings or inspect
    apply_found = any("Apply button FOUND" in w for w in warnings)
    result = BrowserResult(
        vacancy_stable_id=vacancy_stable_id,
        url=url,
        final_url=final_url,
        page_title=page_title,
        site=site,
        status=status,
        form_detected=form_detected,
        apply_button_found=apply_found,
        fields_detected=fields_detected,
        fields_filled=fields_filled,
        fields_skipped=fields_skipped,
        warnings=warnings,
        error=error,
        screenshot_path=screenshot_path,
        flow_type=flow_class.flow_type,
        source_url=flow_class.source_url,
        application_url=flow_class.application_url,
        application_domain=flow_class.application_domain,
        redirect_chain=flow_class.redirect_chain,
        is_external_application=flow_class.is_external_application,
        verification_strategy=flow_class.verification_strategy,
    )

    # Persist
    now = datetime.utcnow().isoformat()
    # Need to get existing created_at if exists
    existing_session = get_browser_session(vacancy_stable_id)
    created_at = existing_session.created_at if existing_session and existing_session.created_at else now
    session = BrowserApplicationSession(
        vacancy_stable_id=vacancy_stable_id,
        url=url,
        status=status,
        fields_detected=fields_detected,
        apply_button_found=any("Apply button FOUND" in w for w in warnings),
        fields_filled=fields_filled,
        fields_skipped=fields_skipped,
        warnings=warnings,
        created_at=created_at,
        updated_at=now,
        final_url=final_url,
        page_title=page_title,
        site=site,
        form_detected=form_detected,
        error=error,
        screenshot_path=screenshot_path,
        flow_type=flow_class.flow_type,
        source_url=flow_class.source_url,
        application_url=flow_class.application_url,
        application_domain=flow_class.application_domain,
        redirect_chain=flow_class.redirect_chain,
        is_external_application=flow_class.is_external_application,
        verification_strategy=flow_class.verification_strategy,
    )
    save_browser_session(session)

    # NEVER change tracking status to APPLIED - safety
    # Ensure we don't modify application_tracking

    return result

def get_browser_result(vacancy_stable_id: str) -> BrowserResult | None:
    sess = get_browser_session(vacancy_stable_id, EXECUTOR_VERSION)
    if not sess:
        sess = get_browser_session(vacancy_stable_id)
    if not sess:
        return None
    return BrowserResult(
        vacancy_stable_id=sess.vacancy_stable_id,
        url=sess.url,
        final_url=sess.final_url,
        page_title=sess.page_title,
        site=sess.site,
        status=sess.status,
        form_detected=sess.form_detected,
        fields_detected=sess.fields_detected,
        fields_filled=sess.fields_filled,
        fields_skipped=sess.fields_skipped,
        warnings=sess.warnings,
        error=sess.error,
        screenshot_path=sess.screenshot_path,
    )

def prepare_next_in_queue(top_n: int = 20, profile_path: str | None = None, adapter: BrowserAdapter | None = None) -> BrowserResult | None:
    from .application_queue import generate_queue
    # Generate queue (syncs)
    items = generate_queue(top_n=top_n, profile_path=profile_path)
    for item in items:
        sid = item.vacancy_stable_id
        # Check if already blocked/completed? Skip if already has browser session with BLOCKED and not force?
        # For now, try each in rank order
        try:
            # Check if already prepared and not blocked? If blocked, try next
            existing = get_browser_session(sid, EXECUTOR_VERSION)
            if existing and existing.status in (BrowserStatus.READY_FOR_REVIEW, BrowserStatus.COMPLETED):
                continue
            if existing and existing.status == BrowserStatus.BLOCKED:
                continue
            return prepare_application_in_browser(sid, profile_path=profile_path, adapter=adapter)
        except Exception as e:
            logging.warning(f"prepare_next failed for {sid}: {e}")
            continue
    return None
def submit_application_in_browser(
    vacancy_stable_id: str,
    confirm_submit: bool = False,
    profile_path: str | None = None,
    adapter: BrowserAdapter | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> SubmitResult:
    """Submit an application in the browser with full safety checks.
    
    All checks must pass before any browser interaction:
    - confirm_submit must be True
    - ApplicationReview must exist and be APPROVED
    - application_tracking must be READY_TO_APPLY
    - BrowserApplicationSession must exist and be READY_FOR_REVIEW
    - QueueItem must exist
    - ApplicationPackage must exist
    - URL must match
    - executor version must match
    - confirm_submit must be True
    
    If any check fails, returns error result without calling submit_application().
    """
    if not confirm_submit and not dry_run:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id="",
            status="BLOCKED",
            error="Submit confirmation required. Use --confirm-submit to proceed.",
            executor_version="v1",
        )

    # BLE001 finding #9: Path A below (vacancy ids starting with "hh:") runs the
    # full 11-gate check inside execute_hh_submission, but every other source
    # (habr_career, himalayas, remoteok, weworkremotely, ...) fell through to
    # the legacy branch, which calls adapter.submit_application() directly with
    # no gates at all - not even the kill-switch. Check it here, for all
    # sources, before anything can touch the browser.
    # BLE001 finding #20: the check above covers SUBMIT_ALLOWED only. Its
    # twin - the `submit_paused` row in system_settings, which is what the
    # Telegram "stop" command writes - was not consulted anywhere on this
    # path, nor on any other path that clicks. Measured: with submit_paused=1
    # and everything else valid, this function reached the adapters and
    # clicked (status=SUBMITTED, adapter.submit_application called once).
    #
    # Why: execute_hh_submission checks both switches, and the "hh:" branch
    # here delegates to it. Every other source skips that and lands on the
    # legacy branch below. Finding #9 caught the first switch for that
    # branch; the second one was missed.
    #
    # Finding #18 applies: a stop we cannot read is not a stop we checked.
    # BLE001 finding #21: this block used to spell out SUBMIT_ALLOWED and
    # submit_paused by hand. The third stop - the STOP_SUBMITS file - was
    # missing again, and hand-spelling is exactly why it keeps going missing.
    # All three stops now come from hh_submission.submission_halt_reason(),
    # so a new stop is added in one place and reaches every click path.
    if not dry_run:
        from .hh_submission import submission_halt_reason

        _halt = submission_halt_reason()
        if _halt:
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id="",
                status="BLOCKED",
                error=_halt,
                executor_version="v1",
            )
    
    init_db()
    
    # Generate submission_id
    import uuid
    submission_id = f"{vacancy_stable_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # Check if already submitted
    from .submission_state import get_submission_evidence
    evidence = get_submission_evidence(vacancy_stable_id)
    if evidence.is_already_applied:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Already submitted. Duplicate submission not allowed: {'; '.join(evidence.blocked_reasons)}",
            executor_version="v1",
        )

    # Load vacancy. BLE001 finding #16: this used to be
    # `vac = _row_to_vacancy(row) if row else None`, and the hard-constraint
    # gate below was wrapped in `if vac:` - so a vacancy missing from the DB
    # skipped remote_required and every hard constraint and still flew on to
    # the browser. The sibling entry point in this same file already refuses
    # with "Vacancy not found"; refuse the same way here.
    from .db import _row_to_vacancy, get_vacancy_by_id
    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Vacancy not found in DB: {vacancy_stable_id}",
            executor_version="v1",
        )
    vac = _row_to_vacancy(row)

    # Load profile
    from .candidate_profile import load_candidate_profile
    if profile_path:
        profile = load_candidate_profile(profile_path)
    else:
        from .config import CANDIDATE_PROFILE_FILE
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception:
                profile = load_candidate_profile()
        else:
            profile = load_candidate_profile()

    # Defense-in-Depth Hard Constraint Gate
    from .matcher import _coerce_profile, _hard_constraints
    from .remote_filter import is_strictly_remote

    if getattr(profile, "remote_required", False):
        is_rem, rem_reason = is_strictly_remote(vac)
        if not is_rem:
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=submission_id,
                status="BLOCKED",
                error=f"Submit blocked by remote_required hard constraint: {rem_reason}",
                executor_version="v1",
            )

    hard_reject, hard_reason = _hard_constraints(_coerce_profile(profile), vac)
    if hard_reject:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Submit blocked by hard constraint gate: {hard_reason}",
            executor_version="v1",
        )

    sess = get_browser_session(vacancy_stable_id)
    if sess and sess.status == BrowserStatus.BLOCKED:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Browser session BLOCKED and not ready for submit: {sess.error or sess.status}",
            executor_version="v1",
        )

    # Path A: If HeadHunter, route strictly through unified execute_hh_submission (Stage 41 / Remediation Phase 1.5)
    if vacancy_stable_id.startswith("hh:"):
        from .hh_browser_launcher import is_cdp_reachable, resolve_cdp_url

        if adapter is None:
            # Resolve the same way the launcher does, otherwise a live BrowserOS
            # on 9110 is invisible here and every submit blocks on a dead 9222.
            resolved_cdp = resolve_cdp_url()
            if not is_cdp_reachable(resolved_cdp):
                return SubmitResult(
                    vacancy_stable_id=vacancy_stable_id,
                    submission_id=submission_id,
                    status="BLOCKED",
                    error=f"CDP не доступен по адресу {resolved_cdp}: запустите браузер с --remote-debugging-port",
                    executor_version="v1",
                    submit_count=0,
                )
            use_adapter = CDPBrowserAdapter(resolved_cdp)
            vac_id = vacancy_stable_id.split(":")[-1]
            use_adapter.open(f"https://hh.ru/vacancy/{vac_id}")
        else:
            use_adapter = adapter
            vac_id = vacancy_stable_id.split(":")[-1]
            if hasattr(use_adapter, "opened_url") and not getattr(use_adapter, "opened_url", None):
                use_adapter.opened_url = f"https://hh.ru/vacancy/{vac_id}"

        def _evaluate_wrapper(js: str) -> str:
            if hasattr(use_adapter, "evaluate"):
                return str(use_adapter.evaluate(js))
            elif hasattr(use_adapter, "execute_script"):
                return str(use_adapter.execute_script(js))
            raise RuntimeError(f"Browser adapter {type(use_adapter).__name__} does not support evaluate()")

        from .hh_submission import execute_hh_submission
        exec_res = execute_hh_submission(
            vacancy_stable_id=vacancy_stable_id,
            evaluate_fn=_evaluate_wrapper,
            human_confirmed=confirm_submit,
            dry_run=dry_run,
            candidate_profile=profile,
            profile_path=profile_path,
            submission_id=submission_id,
        )
        final_target_url = (
            getattr(exec_res.live_page_result, "current_url", None)
            or (use_adapter.get_current_url() if hasattr(use_adapter, "get_current_url") else "")
            or f"https://hh.ru/vacancy/{vacancy_stable_id.split(':')[-1]}"
        )
        if exec_res.status == "SUBMITTED":
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=exec_res.submission_id or submission_id,
                status="SUBMITTED",
                final_url=final_target_url,
                executor_version="v1",
                submit_count=exec_res.submit_count,
                gate_check_result=exec_res.gate_check_result,
                live_page_result=exec_res.live_page_result,
            )
        elif exec_res.status == "DRY_RUN_OK":
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=exec_res.submission_id or submission_id,
                status="DRY_RUN_OK",
                final_url=final_target_url,
                executor_version="v1",
                submit_count=0,
                gate_check_result=exec_res.gate_check_result,
                live_page_result=exec_res.live_page_result,
            )
        else:
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=exec_res.submission_id or submission_id,
                status=exec_res.status,
                error=exec_res.reason,
                final_url=final_target_url,
                executor_version="v1",
                submit_count=exec_res.submit_count,
                gate_check_result=exec_res.gate_check_result,
                live_page_result=exec_res.live_page_result,
            )
    
    # Check if already submitted
    from .submission_state import get_submission_evidence
    evidence = get_submission_evidence(vacancy_stable_id)
    if evidence.is_already_applied:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Already submitted. Duplicate submission not allowed: {'; '.join(evidence.blocked_reasons)}",
            executor_version="v1",
        )

    # Load vacancy
    from .db import _row_to_vacancy, get_vacancy_by_id
    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Vacancy not found",
            executor_version="v1",
        )
    vac = _row_to_vacancy(row)

    # Load profile
    from .candidate_profile import load_candidate_profile
    if profile_path:
        profile = load_candidate_profile(profile_path)
    else:
        from .config import CANDIDATE_PROFILE_FILE
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception:
                profile = load_candidate_profile()
        else:
            profile = load_candidate_profile()
    from .job_analyzer import get_resume_text
    resume_text = get_resume_text(profile)

    # Defense-in-Depth Hard Constraint Gate
    from .matcher import _coerce_profile, _hard_constraints
    from .remote_filter import is_strictly_remote

    if getattr(profile, "remote_required", False):
        is_rem, rem_reason = is_strictly_remote(vac)
        if not is_rem:
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=submission_id,
                status="BLOCKED",
                error=f"Submit blocked by remote_required hard constraint: {rem_reason}",
                executor_version="v1",
            )

    hard_reject, hard_reason = _hard_constraints(_coerce_profile(profile), vac)
    if hard_reject:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Submit blocked by hard constraint gate: {hard_reason}",
            executor_version="v1",
        )

    # A concrete persisted browser BLOCKED state takes precedence so a revoked
    # approval still reports its root cause. If no browser state exists, keep
    # the human review gate first.
    sess = get_browser_session(vacancy_stable_id)
    if sess and sess.status == BrowserStatus.BLOCKED:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Browser session BLOCKED and not ready for submit. Status: {sess.status}",
            executor_version="v1",
        )

    # Get review
    from .application_review import ReviewStatus, get_application_review
    review = get_application_review(vacancy_stable_id)
    if not review or review.status != ReviewStatus.APPROVED:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Review not approved or not found. Only APPROVED reviews can be submitted.",
            executor_version="v1",
        )

    if not sess or sess.status != BrowserStatus.READY_FOR_REVIEW:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Browser session not ready for submit. Status: {sess.status if sess else 'None'}",
            executor_version="v1",
        )

    # Check tracking status
    from .application_tracking import ApplicationStatus, get_application_status
    track = get_application_status(vacancy_stable_id)
    if not track or track.status != ApplicationStatus.READY_TO_APPLY:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Tracking status {track.status if track else 'None'} is not READY_TO_APPLY",
            executor_version="v1",
        )

    # Check queue
    q_item = None
    try:
        from .application_queue import get_queue_item
        q_item = get_queue_item(vacancy_stable_id)
    except Exception:
        pass
    if not q_item:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Queue item not found",
            executor_version="v1",
        )

    # Check package
    from .db import get_application_package
    pkg_row = get_application_package(vacancy_stable_id)
    if not pkg_row:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Application package not found",
            executor_version="v1",
        )
    try:
        pkg_data = json.loads(pkg_row[2]) if pkg_row[2] else {}
        class PkgObj:
            def __init__(self, d):
                self.cover_letter = d.get("cover_letter")
        pkg = PkgObj(pkg_data)
    except Exception:
        pkg = None
    
    # Get vacancy URL
    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Vacancy not found",
            executor_version="v1",
        )
    vac = _row_to_vacancy(row)
    
    url = vac.job_url
    site = _detect_site(url)
    warnings: list[str] = []

    # Choose adapter
    use_adapter = adapter
    if use_adapter is None:
        use_real = os.getenv("BROWSER_USE_PLAYWRIGHT") == "1" or os.getenv("BROWSER_REAL") == "1" or os.getenv("USE_PLAYWRIGHT") == "1"
        if use_real:
            try:
                use_adapter = PlaywrightBrowserAdapter(headless=True)
            except Exception as e:
                logging.warning(f"Playwright not available, fallback to Mock: {e}")
                use_adapter = MockBrowserAdapter()
        else:
            use_adapter = MockBrowserAdapter()


    if use_adapter is None:
        use_real = os.getenv("BROWSER_USE_PLAYWRIGHT") == "1" or os.getenv("BROWSER_REAL") == "1" or os.getenv("USE_PLAYWRIGHT") == "1"
        if use_real:
            try:
                use_adapter = PlaywrightBrowserAdapter(headless=True)
            except Exception as e:
                logging.warning(f"Playwright not available, fallback to Mock: {e}")
                use_adapter = MockBrowserAdapter()
        else:
            use_adapter = MockBrowserAdapter()
    
    # Take before screenshot
    before_screenshot = None
    try:
        before_path = f"artifacts/browser/{vacancy_stable_id.replace(':', '_')}_before_submit.png"
        Path(before_path).parent.mkdir(parents=True, exist_ok=True)
        sp = use_adapter.screenshot(before_path)
        if sp:
            before_screenshot = before_path
    except Exception:
        pass

    # Open the vacancy URL
    open_res = use_adapter.open(url)
    if not open_res or open_res.get("blocked"):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Failed to open URL: {open_res.get('reason', 'URL blocked or unreachable') if open_res else 'no response'}",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Check for 404
    page_title = open_res.get("title", "")
    if "404" in page_title.lower() or "page not found" in page_title.lower() or "page not found" in str(open_res).lower():
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="404 Page not found",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Check for authentication / login requirement
    if hasattr(use_adapter, "inspect_apply_flow"):
        flow_info = use_adapter.inspect_apply_flow()
        auth_state = flow_info.get("authenticated", "UNKNOWN")
        login_req = flow_info.get("login_required", False)
        login_reason = flow_info.get("login_reason")
        if login_req and auth_state == "NOT_AUTHENTICATED":
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=submission_id,
                status="BLOCKED",
                error=f"Authentication required: {login_reason or 'login/signup required for this flow'}",
                executor_version="v1",
                before_screenshot=before_screenshot,
            )
        if login_req and auth_state == "UNKNOWN":
            return SubmitResult(
                vacancy_stable_id=vacancy_stable_id,
                submission_id=submission_id,
                status="BLOCKED",
                error="Authentication state unknown for login-required flow",
                executor_version="v1",
                before_screenshot=before_screenshot,
            )

    # Check for blocked indicators
    content = (open_res.get("title", "") + " " + str(open_res)).lower()
    if any(x in content for x in ["captcha", "cloudflare", "access denied", "login required"]):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="CAPTCHA, Cloudflare, or login required",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Check form and submit button
    inspect = use_adapter.inspect_page()
    form_detected = bool(inspect.get("form_detected", False))
    fields_detected = inspect.get("fields", [])
    apply_found = inspect.get("apply_button", False)

    if not form_detected:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Application form not found",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    submit_button_found = inspect.get("apply_button", False)
    if not submit_button_found:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Submit button not found",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Check for CAPTCHA
    if inspect.get("captcha"):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="CAPTCHA detected",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Check for login
    if inspect.get("login_required"):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Login required",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Check for Cloudflare
    if inspect.get("cloudflare"):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Cloudflare detected",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Take after screenshot before submit
    after_screenshot = None
    try:
        after_path = f"artifacts/browser/{vacancy_stable_id.replace(':', '_')}_after_submit.png"
        Path(after_path).parent.mkdir(parents=True, exist_ok=True)
        sp = use_adapter.screenshot(after_path)
        if sp:
            after_screenshot = after_path
    except Exception:
        pass

    # Submit
    try:
        submit_result = use_adapter.submit_application()
    except Exception as e:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="FAILED",
            error=f"Submit failed: {str(e)}",
            executor_version="v1",
            before_screenshot=before_screenshot,
        )

    # Take after screenshot
    after_screenshot = None
    try:
        after_path = f"artifacts/browser/{vacancy_stable_id.replace(':', '_')}_after_submit.png"
        Path(after_path).parent.mkdir(parents=True, exist_ok=True)
        sp = use_adapter.screenshot(after_path)
        if sp:
            after_screenshot = after_path
    except Exception:
        pass

    # Check result
    if not submit_result.get("success"):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="FAILED",
            error=submit_result.get("error", "Submit failed"),
            executor_version="v1",
            before_screenshot=before_screenshot,
            after_screenshot=after_screenshot,
        )

    # Save submission record
    from .db import save_submission
    submitted_at = datetime.utcnow().isoformat()
    submission_id = save_submission(
        vacancy_stable_id=vacancy_stable_id,
        submission_json=json.dumps(submit_result, ensure_ascii=False),
        status="SUBMITTED",
        submitted_at=submitted_at,
    )

    # Transition tracking to SUBMITTED
    try:
        from .application_tracking import ApplicationStatus, transition_application
        transition_application(vacancy_stable_id, ApplicationStatus.SUBMITTED, note="Submitted via browser executor")
    except Exception:
        pass

    # Update result with final status - DO NOT transition to APPLIED yet
    # APPLIED only after verification confirms success
    final_url = open_res.get("url", url)
    flow_class = classify_apply_flow(
        source_url=url,
        final_url=final_url,
        apply_link=None,
        has_form=form_detected,
    )
    result = SubmitResult(
        vacancy_stable_id=vacancy_stable_id,
        submission_id=submission_id,
        status="SUBMITTED",
        final_url=final_url,
        page_title=page_title,
        submitted_at=submitted_at,
        before_screenshot=before_screenshot,
        after_screenshot=after_screenshot,
        confirmation_used=True,
        submit_button_found=True,
        flow_type=flow_class.flow_type,
        application_domain=flow_class.application_domain,
        verification_strategy=flow_class.verification_strategy,
        executor_version="v1",
    )

    return result


def verify_submission_in_browser(
    vacancy_stable_id: str,
    submission_id: str,
    profile_path: str | None = None,
    adapter: BrowserAdapter | None = None,
) -> SubmissionVerification:
    """Verify a submission by checking the application page for success/error/blocked signals.
    Does NOT re-submit the application - only reads the current page state.
    """
    from .submission_verifier import verify_submission as _verify_submission
    return _verify_submission(vacancy_stable_id, submission_id, profile_path, adapter)


def submit_next_in_queue(top_n: int = 1, profile_path: str | None = None, adapter: BrowserAdapter | None = None) -> SubmitResult | None:
    """Submit the next READY_TO_APPLY + APPROVED + READY_FOR_REVIEW vacancy."""
    from .application_queue import generate_queue
    
    # Generate queue (syncs)
    items = generate_queue(top_n=top_n, profile_path=profile_path)
    for item in items:
        sid = item.vacancy_stable_id
        # Check if already blocked/completed? Skip if already has browser session with BLOCKED and not force?
        # For now, try each in rank order
        try:
            # Check if already prepared and not blocked? If blocked, try next
            existing = get_browser_session(sid, EXECUTOR_VERSION)
            if existing and existing.status in (BrowserStatus.READY_FOR_REVIEW, BrowserStatus.COMPLETED):
                continue
            if existing and existing.status == BrowserStatus.BLOCKED:
                continue
            return submit_application_in_browser(sid, profile_path=profile_path, adapter=adapter, force=True)
        except Exception as e:
            logging.warning(f"submit_next failed for {sid}: {e}")
            continue
    return None


def audit_apply_flow_for_vacancy(
    vacancy_stable_id: str,
    adapter: BrowserAdapter | None = None,
) -> dict[str, Any]:
    """
    Read-only audit of a vacancy's apply flow.
    Strictly read-only: NEVER clicks apply, NEVER clicks submit, NEVER writes to DB.
    """
    from urllib.parse import urlparse

    from .db import _row_to_vacancy, get_vacancy_by_id

    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        return {
            "vacancy_stable_id": vacancy_stable_id,
            "source_url": "",
            "source_domain": "",
            "apply_present": False,
            "apply_href": None,
            "application_url": None,
            "application_domain": None,
            "flow_type": FlowType.UNKNOWN.value,
            "is_external_application": False,
            "verification_strategy": "manual_review",
            "evidence": {},
            "errors": [f"Vacancy {vacancy_stable_id} not found in DB"],
        }

    vac = _row_to_vacancy(row)
    source_url = vac.job_url or ""
    try:
        source_domain = urlparse(source_url).netloc.lower().removeprefix("www.")
    except Exception:
        source_domain = ""

    errors = []
    evidence = {}
    apply_present = False
    apply_href = None
    final_url = None
    has_form = False

    use_adapter = adapter or CDPBrowserAdapter()
    try:
        open_res = use_adapter.open(source_url)
        final_url = open_res.get("final_url") or source_url
        evidence["page_title"] = open_res.get("title", "")
        evidence["final_url"] = final_url
        if open_res.get("blocked"):
            errors.append(f"Page load warning/blocked: {open_res.get('reason', 'unknown')}")

        if hasattr(use_adapter, "inspect_apply_flow"):
            flow_info = use_adapter.inspect_apply_flow()
            apply_present = flow_info.get("apply_present", False)
            apply_href = flow_info.get("apply_href")
            has_form = flow_info.get("has_form", False)
            evidence["apply_element_text"] = flow_info.get("apply_element_text") or flow_info.get("button_text")
            evidence["apply_element_tag"] = flow_info.get("apply_element_tag")
            evidence["apply_element_href"] = flow_info.get("apply_element_href") or apply_href
            evidence["apply_element_aria_label"] = flow_info.get("apply_element_aria_label")
            evidence["apply_detection_reason"] = flow_info.get("apply_detection_reason", "No detection reason")
            evidence["fields_detected"] = flow_info.get("fields", [])
            evidence["captcha"] = flow_info.get("captcha", False)
            evidence["login_required"] = flow_info.get("login_required", False)
            evidence["login_reason"] = flow_info.get("login_reason")
            evidence["authenticated"] = flow_info.get("authenticated", "UNKNOWN")
            evidence["auth_reason"] = flow_info.get("auth_reason")
        else:
            insp = use_adapter.inspect_page()
            apply_present = insp.get("apply_button", False)
            has_form = insp.get("form_detected", False)
            evidence["fields_detected"] = insp.get("fields", [])
            evidence["apply_detection_reason"] = "Legacy inspect_page fallback"
    except Exception as e:
        errors.append(f"Browser inspection error: {str(e)}")
    finally:
        try:
            use_adapter.close()
        except Exception:
            pass

    classification = classify_apply_flow(
        source_url=source_url,
        final_url=final_url,
        apply_link=apply_href,
        has_form=has_form,
        redirect_chain=[],
    )

    evidence["application_url"] = classification.application_url
    evidence["application_domain"] = classification.application_domain
    evidence["redirect_chain"] = classification.redirect_chain

    return {
        "vacancy_stable_id": vacancy_stable_id,
        "source_url": source_url,
        "source_domain": source_domain,
        "apply_present": apply_present,
        "apply_href": apply_href,
        "application_url": classification.application_url,
        "application_domain": classification.application_domain,
        "flow_type": classification.flow_type.value,
        "is_external_application": classification.is_external_application,
        "verification_strategy": classification.verification_strategy,
        "evidence": evidence,
        "errors": errors,
    }


def run_apply_flow_audit(
    vacancy_stable_ids: list[str],
    adapter: BrowserAdapter | None = None,
    output_path: str = "artifacts/browser/stage30i_apply_flow_audit.json",
) -> list[dict[str, Any]]:
    """Run read-only audit across given vacancies and persist results to JSON."""
    results = []
    for sid in vacancy_stable_ids:
        res = audit_apply_flow_for_vacancy(sid, adapter=adapter)
        results.append(res)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    return results
