"""Direct Chrome DevTools Protocol (CDP) browser adapter."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

from ai_assistant.browser.base import BrowserAdapter
from ai_assistant.browser.mock_adapter import INSPECT_UNREADABLE
from ai_assistant.browser.types import _note_failure

logger = logging.getLogger("browser_executor")


class CDPBrowserAdapter(BrowserAdapter):
    """Direct CDP adapter over WebSocket / HTTP API (e.g. http://127.0.0.1:9222)."""

    def __init__(self, cdp_url: str | None = None):
        from ai_assistant.hh_browser_launcher import resolve_cdp_url

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
        from ai_assistant.hh_browser_launcher import _NO_PROXY_OPENER
        try:
            new_url = f"{self.cdp_url}/json/new?{url}"
            req = urllib.request.Request(new_url, method="PUT")
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
        """Read-only page inspection over CDP."""
        if not self.ws_url:
            logger.warning("CDP inspect_page: no ws_url configured; reporting page as unreadable")
            return dict(INSPECT_UNREADABLE)
        async def _inspect():
            import asyncio
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

                    const formInputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='search']):not([type='submit']), textarea, select"));
                    const jobFormInputs = formInputs.filter(i => {
                        return !i.closest('header, nav, footer, [role="navigation"], .header, .navbar, .search-bar, .search-form');
                    });
                    if (jobFormInputs.length > 0) {
                        result.has_form = true;
                        result.fields = jobFormInputs.map(i => i.name || i.id || i.type).filter(Boolean).slice(0, 10);
                    }

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

                        if (/^(apply now|apply for this job|apply for this position|apply to position|easy apply|apply on company site|apply on employer site)$/i.test(text) || /^откликнуться$/i.test(text) || /^подать резюме$/i.test(text)) {
                            score += 100;
                            reason = `Exact primary Apply CTA text '${text}'`;
                        } else if (/apply|откликнуться|подать резюме|submit application/i.test(combinedText)) {
                            score += 60;
                            reason = `Apply CTA text match '${text || aria}'`;
                        }

                        if (href && /boards\.greenhouse\.io|jobs\.lever\.co|apply\.workable\.com|jobs\.ashbyhq\.com|smartrecruiters\.com|myworkdayjobs\.com|bamboohr\.com/i.test(href)) {
                            score += 120;
                            reason = `Direct external ATS link in href '${href}'`;
                        } else if (href && !isNavOrGenericLink(href) && /apply/i.test(href)) {
                            score += 40;
                            if (!reason) reason = `Apply path in href '${href}'`;
                        }

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
                if not isinstance(res, dict) or res.get("clicked") is not True:
                    why = res.get("error") if isinstance(res, dict) else None
                    return {
                        "success": False,
                        "error": (
                            "Submit click did not happen: "
                            + (str(why) if why else "no click confirmation from the page")
                        ),
                        "details": res,
                    }
                return {"success": True, "details": res}
        try:
            res = self._sync_run(_submit())
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    def close(self) -> None:
        if self.tab_id:
            try:
                from ai_assistant.hh_browser_launcher import _NO_PROXY_OPENER
                _NO_PROXY_OPENER.open(f"{self.cdp_url}/json/close/{self.tab_id}", timeout=5)
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                _note_failure(f"CDP tab close {self.tab_id}", exc)
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
