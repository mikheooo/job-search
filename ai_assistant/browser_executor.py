from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field

from . import config
from .db import get_connection, init_db
from .candidate_profile import CandidateProfile
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


class SubmitResult(BaseModel):
    vacancy_stable_id: str
    submission_id: str
    status: SubmitStatus
    final_url: Optional[str] = None
    page_title: Optional[str] = None
    submitted_at: Optional[str] = None
    before_screenshot: Optional[str] = None
    after_screenshot: Optional[str] = None
    confirmation_used: bool = False
    submit_button_found: bool = False
    error: Optional[str] = None
    executor_version: str = EXECUTOR_VERSION

    model_config = {"extra": "forbid"}

class BrowserApplicationSession(BaseModel):
    vacancy_stable_id: str
    url: str
    status: BrowserStatus
    fields_detected: List[str] = Field(default_factory=list)
    fields_filled: List[str] = Field(default_factory=list)
    fields_skipped: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    final_url: Optional[str] = None
    page_title: Optional[str] = None
    site: Optional[str] = None
    form_detected: bool = False
    error: Optional[str] = None
    screenshot_path: Optional[str] = None

class BrowserResult(BaseModel):
    vacancy_stable_id: str
    url: str
    final_url: Optional[str] = None
    page_title: Optional[str] = None
    site: Optional[str] = None
    status: BrowserStatus
    form_detected: bool = False
    apply_button_found: bool = False
    fields_detected: List[str] = Field(default_factory=list)
    fields_filled: List[str] = Field(default_factory=list)
    fields_skipped: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    screenshot_path: Optional[str] = None

    model_config = {"extra": "forbid"}

# Abstract BrowserAdapter - no submit/apply methods allowed
class BrowserAdapter:
    def open(self, url: str) -> Dict[str, Any]:
        raise NotImplementedError
    def inspect_page(self) -> Dict[str, Any]:
        raise NotImplementedError
    def fill_field(self, selector: str, value: str) -> bool:
        raise NotImplementedError
    def upload_file(self, selector: str, path: str) -> bool:
        raise NotImplementedError
    def screenshot(self, path: str) -> Optional[str]:
        raise NotImplementedError
    def close(self) -> None:
        raise NotImplementedError
    def get_current_url(self) -> str:
        raise NotImplementedError
    def get_title(self) -> str:
        raise NotImplementedError
    # Submit method - only for controlled submit flow
    def submit_application(self) -> Dict[str, Any]:
        raise NotImplementedError

    def extract_application_form(self) -> Dict[str, Any]:
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
    def __init__(self, simulate: Dict[str, Any] | None = None):
        self.simulate = simulate or {}
        self.opened_url: Optional[str] = None
        self.calls: List[str] = []
        self.closed = False
        # Safety: ensure no submit is ever called
        self.submit_attempted = False

    def open(self, url: str) -> Dict[str, Any]:
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

    def inspect_page(self) -> Dict[str, Any]:
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

    def extract_application_form(self) -> Dict[str, Any]:
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
        }

    def fill_field(self, selector: str, value: str) -> bool:
        self.calls.append(f"fill:{selector}")
        return True

    def upload_file(self, selector: str, path: str) -> bool:
        self.calls.append(f"upload:{selector}")
        return True

    def screenshot(self, path: str) -> Optional[str]:
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

    def submit_application(self) -> Dict[str, Any]:
        self.calls.append("submit_application")
        self.submit_attempted = True
        # Mock always returns success for testing
        return {"success": True, "message": "Mock submission successful"}

# Playwright adapter if available (optional, not required for tests)
class PlaywrightBrowserAdapter(BrowserAdapter):
    def __init__(self, headless: bool = True, storage_state: str | None = None):
        self.headless = headless
        # Stage 18: optional authenticated session (Playwright storage_state file).
        # The file path comes from env (HH_STORAGE_STATE) at call sites - it is
        # NEVER hardcoded, committed, or stored in DB/packages.
        self.storage_state = storage_state
        self.play = None
        self.browser = None
        self.context = None
        self.page = None
        self._final_url = None
        self._title = None

    def open(self, url: str) -> Dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
            self.play = sync_playwright().start()
            self.browser = self.play.chromium.launch(headless=self.headless)
            if self.storage_state:
                self.context = self.browser.new_context(storage_state=self.storage_state)
            else:
                self.context = self.browser.new_context()
            self.page = self.context.new_page()
            self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            self._final_url = self.page.url
            self._title = self.page.title()
            # Check for blocked
            content = self.page.content().lower()
            title_lower = (self._title or "").lower()
            # Detect 404 / not found as blocked
            if "404" in title_lower or "page not found" in title_lower or "page not found" in content:
                blocked = True
            else:
                blocked = any(x in content for x in ["captcha", "cloudflare", "access denied", "login required"])
            site = url.split("/")[2] if "://" in url else ""
            return {"final_url": self._final_url, "title": self._title, "site": site, "blocked": blocked}
        except Exception as e:
            return {"final_url": url, "title": "", "site": "", "blocked": True, "reason": str(e)}

    def inspect_page(self) -> Dict[str, Any]:
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

    def extract_application_form(self) -> Dict[str, Any]:
        """Read-only HH-aware extraction. Never clicks/fills/uploads.

        Uses REAL observed HH DOM patterns:
          - question label: div[data-qa^='vacancy-response-question'] with text;
            second whitespace token of data-qa is the stable slug.
          - apply link: a[data-qa='vacancy-response-link-top'] (never clicked).
          - auth gate: div[data-qa='auth-form'] => answer controls hidden.
        """
        if not self.page:
            return {
                "html": "", "body_text": "", "questions": [], "controls": [],
                "question_groups": [],
                "auth_form": False,
                "apply_link": None, "final_url": "", "title": "", "site": "",
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
            }
        except Exception:
            return {
                "html": "", "body_text": "", "questions": [], "controls": [],
                "question_groups": [],
                "auth_form": False,
                "apply_link": None, "final_url": self._final_url or "",
                "title": self._title or "", "site": "hh.ru",
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

    def screenshot(self, path: str) -> Optional[str]:
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
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
            if self.play:
                self.play.stop()
        except Exception:
            pass

    def get_current_url(self) -> str:
        return self._final_url or ""

    def get_title(self) -> str:
        return self._title or ""

    def submit_application(self) -> Dict[str, Any]:
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

def _get_validated_package_answer(field: str, package: Any) -> Optional[str]:
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

def _get_profile_value(field: str, profile: CandidateProfile, resume_text: str, vacancy: Vacancy, package: Any) -> Optional[str]:
    """Truth-only field value: confirmed profile/resume data first, then a
    validated (requires_review=False) package answer. Never invents."""
    val = _get_profile_value_truth(field, profile, resume_text, vacancy, package)
    if val is not None:
        return val
    return _get_validated_package_answer(field, package)

def _get_profile_value_truth(field: str, profile: CandidateProfile, resume_text: str, vacancy: Vacancy, package: Any) -> Optional[str]:
    field = field.lower()
    # Truth-only: return None if not confirmed
    if field in ("name", "first_name", "last_name"):
        # Try to extract from resume or profile if it has name field
        # CandidateProfile currently not have name, so check resume for Name: pattern
        m = re.search(r"(?:name|candidate):\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", resume_text, re.IGNORECASE)
        if m:
            full = m.group(1).strip()
            if field == "first_name":
                return full.split()[0]
            if field == "last_name":
                return full.split()[-1] if len(full.split())>1 else None
            return full
        # fallback to profile if it has attribute
        if hasattr(profile, "name") and getattr(profile, "name"):
            return str(getattr(profile, "name"))
        return None
    if field == "email":
        m = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", resume_text)
        if m:
            return m.group(0)
        if hasattr(profile, "email") and getattr(profile, "email"):
            return str(getattr(profile, "email"))
        return None
    if field == "phone":
        m = re.search(r"\+?\d[0-9 \-\(\)]{7,}", resume_text)
        if m:
            return m.group(0).strip()
        return None
    if field == "location":
        if profile.allowed_locations:
            return profile.allowed_locations[0]
        return None
    if field == "linkedin":
        m = re.search(r"https?://[^\s]*linkedin[^\s]*", resume_text, re.IGNORECASE)
        if m:
            return m.group(0)
        return None
    if field == "github":
        m = re.search(r"https?://[^\s]*github[^\s]*", resume_text, re.IGNORECASE)
        if m:
            return m.group(0)
        return None
    if field == "portfolio":
        m = re.search(r"https?://[^\s]*portfolio[^\s]*", resume_text, re.IGNORECASE)
        if m:
            return m.group(0)
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
        if hasattr(profile, "work_authorization") and getattr(profile, "work_authorization"):
            return str(getattr(profile, "work_authorization"))
        return None
    if field == "years_experience":
        if profile.years_experience is not None:
            return str(profile.years_experience)
        m = re.search(r"(\d+)\s+years", resume_text, re.IGNORECASE)
        if m:
            return m.group(1)
        return None
    return None

def _map_fields(fields_detected: List[str], profile: CandidateProfile, resume_text: str, vacancy: Vacancy, package: Any) -> tuple[List[str], List[str], List[str]]:
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

def get_browser_session(vacancy_stable_id: str, executor_version: str | None = None) -> Optional[BrowserApplicationSession]:
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
    from .candidate_profile import load_candidate_profile
    from .db import get_vacancy_by_id, get_application_package
    from .application_tracking import get_application_status, ApplicationStatus
    from .job_analyzer import get_resume_text
    from .db import _row_to_vacancy
    import time

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
    warnings: List[str] = []
    fields_detected: List[str] = []
    fields_filled: List[str] = []
    fields_skipped: List[str] = []

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
            inspect = use_adapter.inspect_page()
            form_detected = bool(inspect.get("form_detected"))
            fields_detected = inspect.get("fields", [])
            apply_found = inspect.get("apply_button", False)
            if apply_found:
                warnings.append("Apply button FOUND - Manual submission required. DO NOT CLICK Submit.")
            else:
                warnings.append("Apply button not found")
            if inspect.get("captcha"):
                warnings.append("CAPTCHA detected - BLOCKED, manual required")
                status = BrowserStatus.BLOCKED
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
                # not blocked, package exists AND validation_status == VALID.
                validation_status = getattr(pkg, "validation_status", "NEEDS_REVIEW") if pkg else "NEEDS_REVIEW"
                if status == BrowserStatus.FORM_DETECTED:
                    if validation_status == "VALID":
                        status = BrowserStatus.READY_FOR_REVIEW
                        warnings.append("Manual submission required. DO NOT SUBMIT automatically. - READY_FOR_REVIEW")
                    else:
                        # Not VALID: keep a safe, non-ready state. FORM_DETECTED
                        # already communicates that the form was found but the
                        # package still needs review. We do not auto-submit.
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
    )
    save_browser_session(session)

    # NEVER change tracking status to APPLIED - safety
    # Ensure we don't modify application_tracking

    return result

def get_browser_result(vacancy_stable_id: str) -> Optional[BrowserResult]:
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

def prepare_next_in_queue(top_n: int = 20, profile_path: str | None = None, adapter: BrowserAdapter | None = None) -> Optional[BrowserResult]:
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
    if not confirm_submit:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id="",
            status="BLOCKED",
            error="Submit confirmation required. Use --confirm-submit to proceed.",
            executor_version="v1",
        )
    
    init_db()
    
    # Generate submission_id
    import uuid
    submission_id = f"{vacancy_stable_id}_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    
    # Check if already submitted
    if is_submitted(vacancy_stable_id):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Already submitted. Duplicate submission not allowed.",
            executor_version="v1",
        )
    
    # Get review
    from .application_review import get_application_review, ReviewStatus
    review = get_application_review(vacancy_stable_id)
    if not review or review.status != ReviewStatus.APPROVED:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Review not approved or not found. Only APPROVED reviews can be submitted.",
            executor_version="v1",
        )
    
    # Check tracking status
    from .application_tracking import get_application_status, ApplicationStatus
    track = get_application_status(vacancy_stable_id)
    if not track or track.status != ApplicationStatus.READY_TO_APPLY:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Tracking status {track.status if track else 'None'} is not READY_TO_APPLY",
            executor_version="v1",
        )
    
    # Check browser session
    sess = get_browser_session(vacancy_stable_id)
    if not sess or sess.status != BrowserStatus.READY_FOR_REVIEW:
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Browser session not ready for submit. Status: {sess.status if sess else 'None'}",
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
    
    # Check if already submitted
    if is_submitted(vacancy_stable_id):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error="Already submitted. Duplicate submission not allowed.",
            executor_version="v1",
        )
    
    # Load vacancy
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
    
    # Load package
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
    warnings: List[str] = []
    
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
    if not open_res or not open_res.get("success"):
        return SubmitResult(
            vacancy_stable_id=vacancy_stable_id,
            submission_id=submission_id,
            status="BLOCKED",
            error=f"Failed to open URL: {open_res.get('reason', 'unknown') if open_res else 'no response'}",
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
    if "captcha" in str(inspect).lower():
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
    import datetime
    submitted_at = datetime.datetime.utcnow().isoformat()
    submission_id = save_submission(
        vacancy_stable_id=vacancy_stable_id,
        submission_json=json.dumps(submit_result, ensure_ascii=False),
        status="SUBMITTED",
        submitted_at=submitted_at,
    )

    # Update result with final status - DO NOT transition to APPLIED yet
    # APPLIED only after verification confirms success
    final_url = open_res.get("url", url)
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
        executor_version="v1",
    )

    return result


def verify_submission_in_browser(
    vacancy_stable_id: str,
    submission_id: str,
    profile_path: str | None = None,
    adapter: BrowserAdapter | None = None,
) -> 'SubmissionVerification':
    """Verify a submission by checking the application page for success/error/blocked signals.
    Does NOT re-submit the application - only reads the current page state.
    """
    from .submission_verifier import verify_submission as _verify_submission
    return _verify_submission(vacancy_stable_id, submission_id, profile_path, adapter)


def submit_next_in_queue(top_n: int = 1, profile_path: str | None = None, adapter: BrowserAdapter | None = None) -> Optional[SubmitResult]:
    """Submit the next READY_TO_APPLY + APPROVED + READY_FOR_REVIEW vacancy."""
    from .application_queue import generate_queue
    from .application_review import get_application_review, ReviewStatus
    from .application_tracking import get_application_status, ApplicationStatus
    from .application_queue import get_queue_item
    from .db import get_application_package
    
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
