"""Mock browser adapter for testing and simulation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_assistant.browser.base import BrowserAdapter

INSPECT_UNREADABLE: dict[str, Any] = {
    "form_detected": False,
    "fields": [],
    "apply_button": False,
    "captcha": False,
    "login_required": False,
    "inspection_failed": True,
}


class MockBrowserAdapter(BrowserAdapter):
    def __init__(self, simulate: dict[str, Any] | None = None):
        self.simulate = simulate or {}
        self.opened_url: str | None = None
        self.calls: list[str] = []
        self.closed = False
        self.submit_attempted = False

    def open(self, url: str) -> dict[str, Any]:
        self.calls.append(f"open:{url}")
        self.opened_url = url
        final_url = self.simulate.get("final_url", url)
        title = self.simulate.get("page_title", "Mock Vacancy Page")
        site = self.simulate.get("site", url.split("/")[2] if "://" in url else "example.com")
        blocked_reason = self.simulate.get("blocked_reason")
        if blocked_reason:
            return {"final_url": final_url, "title": title, "site": site, "blocked": True, "reason": blocked_reason}
        return {"final_url": final_url, "title": title, "site": site, "blocked": False}

    def inspect_page(self) -> dict[str, Any]:
        self.calls.append("inspect_page")
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
            "error": bool(sim.get("error", False)),
            "error_reason": sim.get("error_reason"),
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
        return {"success": True, "message": "Mock submission successful"}

    def evaluate(self, js: str) -> str:
        self.calls.append("evaluate")
        cleaned_js = js.strip()
        sim = self.simulate
        target_url = sim.get("final_url") or self.opened_url or "https://hh.ru/vacancy/12345678"

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

        if cleaned_js.startswith("// hh_submit_click"):
            res = self.submit_application()
            return json.dumps({"ok": res.get("success", True)})

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

        if "location.href" in cleaned_js:
            return json.dumps({"url": target_url})
        return json.dumps({"ok": True, "url": target_url})
