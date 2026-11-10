"""Playwright browser adapter for interactive browser automation."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ai_assistant.browser.base import BrowserAdapter
from ai_assistant.browser.page_checks import _detect_page_blocked
from ai_assistant.browser.types import _note_failure

logger = logging.getLogger("browser_executor")


class PlaywrightBrowserAdapter(BrowserAdapter):
    def __init__(self, headless: bool = True, storage_state: str | None = None, cdp_url: str | None = None):
        self.headless = headless
        self.storage_state = storage_state
        from ai_assistant.hh_browser_launcher import resolve_cdp_url

        self.cdp_url = resolve_cdp_url(cdp_url)
        self._is_cdp = False
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
            html = self.page.content()
            try:
                body_text = self.page.inner_text("body") or ""
            except Exception:
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
            apply_found = bool(self.page.query_selector("button:has-text('Apply'), a:has-text('Apply'), button:has-text('Easy Apply')"))
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
            form_detected = len(fields) > 0 or "form" in content
            captcha = "captcha" in content
            login_required = "login" in content and "required" in content
            return {"form_detected": form_detected, "fields": fields, "apply_button": apply_found, "captcha": captcha, "login_required": login_required}
        except Exception:
            return {"form_detected": False, "fields": [], "apply_button": False}

    def extract_application_form(self) -> dict[str, Any]:
        """Read-only HH-aware extraction. Never clicks/fills/uploads."""
        if not self.page:
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
        except Exception as exc:
            _note_failure(f"fill_field {selector}", exc, level=logging.WARNING)
        return False

    def upload_file(self, selector: str, path: str) -> bool:
        try:
            if self.page:
                self.page.set_input_files(selector, path, timeout=5000)
                return True
        except Exception as exc:
            _note_failure(f"upload_file {selector}", exc, level=logging.WARNING)
        return False

    def screenshot(self, path: str) -> str | None:
        try:
            if self.page:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=path)
                return path
        except Exception as exc:
            _note_failure(f"screenshot {path}", exc)
        return None

    def close(self) -> None:
        try:
            if self.page:
                self.page.close()
        except Exception as exc:
            _note_failure("playwright page close", exc)
        if not self._is_cdp:
            try:
                if self.context:
                    self.context.close()
            except Exception as exc:
                _note_failure("playwright context close", exc)
            try:
                if self.browser:
                    self.browser.close()
            except Exception as exc:
                _note_failure("playwright browser close", exc)
        try:
            if self.play:
                self.play.stop()
        except Exception as exc:
            _note_failure("playwright stop", exc)

    def get_current_url(self) -> str:
        return self._final_url or ""

    def get_title(self) -> str:
        return self._title or ""

    def submit_application(self) -> dict[str, Any]:
        """Click submit/apply button and verify submission."""
        if not self.page:
            return {"success": False, "error": "Page not initialized"}
        try:
            submit_button = self.page.query_selector(
                "button:has-text('Submit'), button:has-text('Apply'), button:has-text('Send'), "
                "input[type='submit'], button[type='submit'], "
                "button:has-text('Отправить'), button:has-text('Отправить заявку')"
            )
            if not submit_button:
                return {"success": False, "error": "Submit button not found"}

            before_path = f"artifacts/browser/{self._final_url.replace('://', '_').replace('/', '_').replace(':', '_')}_before_submit.png"
            Path(before_path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=before_path)

            submit_button.click()

            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as exc:
                _note_failure("submit wait_for_load_state", exc)

            after_path = f"artifacts/browser/{self._final_url.replace('://', '_').replace('/', '_').replace(':', '_')}_after_submit.png"
            Path(after_path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=after_path)

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
