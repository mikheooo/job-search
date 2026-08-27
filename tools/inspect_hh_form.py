"""Stage 19: real HH authenticated form inspection (READ-ONLY diagnostic).

Usage:
    set HH_STORAGE_STATE=C:\\path\\hh_storage_state.json
    python tools/inspect_hh_form.py [vacancy_url]

Reads the DOM of the real HH application form using an existing authenticated
session (Playwright storage_state). NEVER submits, clicks, fills, uploads,
logs in, or bypasses CAPTCHA. Output contains diagnostic data only - no
cookies, tokens, passwords, or session state.

!!! CRITICAL SAFETY FINDING (Stage 19, confirmed live) !!!
NEVER navigate (GET) to https://hh.ru/applicant/vacancy_response?vacancyId=...
For vacancies with the simple-apply flow, HH EXECUTES THE RESPONSE
IMMEDIATELY (with the default resume) on that GET - it is NOT a form page.
The real application form opens only in a modal after a human click on
"Откликнуться", which this tool never automates.

Verdicts:
    AUTHENTICATED_FORM_INSPECTED  - authenticated form DOM was actually read
    BLOCKED_BY_AUTH               - no session / auth-form present
    BLOCKED_BY_CAPTCHA            - captcha challenge present
    FORM_NOT_FOUND                - authenticated but no form controls found
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DEFAULT_VACANCY = "https://hh.ru/vacancy/135112049"


def _visible(el) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False


def inspect(vacancy_url: str) -> dict:
    from ai_assistant.browser_executor import PlaywrightBrowserAdapter
    from ai_assistant.hh_extractor import extract_application_form

    # HARD BAN: the response URL auto-submits a real application on GET.
    if "applicant/vacancy_response" in vacancy_url:
        return {
            "generated_at": datetime.utcnow().isoformat(),
            "vacancy_url": vacancy_url,
            "verdict": "FORBIDDEN_URL",
            "reason": "applicant/vacancy_response auto-submits a real response on GET "
                      "(confirmed live in Stage 19). Inspection of the vacancy page only.",
        }

    report: dict = {
        "generated_at": datetime.utcnow().isoformat(),
        "vacancy_url": vacancy_url,
        "storage_state_set": bool(os.getenv("HH_STORAGE_STATE")),
        "storage_state_path": os.getenv("HH_STORAGE_STATE") or None,
        "storage_state_exists": None,
        "final_url": None,
        "auth_form": None,
        "captcha": None,
        "cloudflare": None,
        "form_detected": None,
        "login_redirect": None,
        "controls": [],
        "radio_groups": [],
        "screening_questions": [],
        "iframes": [],
        "modals": [],
        "extractor_result": None,
        "verdict": None,
        "reason": None,
    }

    ss = os.getenv("HH_STORAGE_STATE") or None
    if not ss:
        report["verdict"] = "BLOCKED_BY_AUTH"
        report["reason"] = "HH_STORAGE_STATE is not set - no authenticated session available"
        return report
    if not os.path.isfile(ss):
        report["storage_state_exists"] = False
        report["verdict"] = "BLOCKED_BY_AUTH"
        report["reason"] = f"HH_STORAGE_STATE points to a missing file: {ss}"
        return report
    report["storage_state_exists"] = True

    adapter = PlaywrightBrowserAdapter(headless=True, storage_state=ss)
    try:
        open_res = adapter.open(vacancy_url)
        report["final_url"] = open_res.get("final_url")
        if open_res.get("blocked") and not adapter.page:
            report["verdict"] = "BLOCKED_BY_AUTH"
            report["reason"] = f"page open failed: {open_res.get('reason')}"
            return report

        # login redirect detection (signup/auth pages)
        final = (report["final_url"] or "").lower()
        report["login_redirect"] = any(k in final for k in ["account/signup", "account/login", "auth"])

        snapshot = adapter.extract_application_form()
        page = adapter.page

        report["auth_form"] = bool(snapshot.get("auth_form"))
        html_low = (snapshot.get("html") or "").lower()
        report["captcha"] = 'data-qa="captcha' in html_low or ("captcha" in html_low and "bloko-modal" in html_low)
        report["cloudflare"] = any(k in html_low for k in ["cf-challenge", "challenge-platform", "cf-error"])

        # screening question containers (label texts only)
        try:
            report["screening_questions"] = [
                {"slug": q.get("slug"), "label": q.get("label")}
                for q in (snapshot.get("questions") or [])
            ]
        except Exception:
            report["screening_questions"] = []

        # detailed control diagnostics (read-only)
        controls = snapshot.get("controls") or []
        detailed = []
        for c in controls:
            entry = {
                "tag": c.get("tag"),
                "type": c.get("type"),
                "name": c.get("name"),
                "id": c.get("id"),
                "data-qa": c.get("dataQa"),
                "aria-label": None,
                "label": c.get("label"),
                "required": bool(c.get("required")),
                "options": c.get("options"),
            }
            detailed.append(entry)
        report["controls"] = detailed

        # visibility + aria-label + modal/iframe structure straight from DOM
        if page is not None:
            try:
                report["iframes"] = page.eval_on_selector_all(
                    "iframe", "els => els.map(e => ({src: e.getAttribute('src'), name: e.name, visible: (e.offsetWidth>0&&e.offsetHeight>0)}))")
            except Exception:
                report["iframes"] = []
            try:
                report["modals"] = page.eval_on_selector_all(
                    "[class*='modal' i], [data-qa*='modal' i], [role='dialog']",
                    "els => els.slice(0,10).map(e => ({tag: e.tagName, cls: (e.className||'').toString().slice(0,80), visible: (e.offsetWidth>0&&e.offsetHeight>0)}))")
            except Exception:
                report["modals"] = []
            # enrich controls with aria-label + visibility
            try:
                vis = page.eval_on_selector_all(
                    "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='image']), textarea, select",
                    """els => els.map(e => ({
                        ariaLabel: e.getAttribute('aria-label'),
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
                    }))""")
                for i, entry in enumerate(report["controls"]):
                    if i < len(vis):
                        entry["aria-label"] = vis[i].get("ariaLabel")
                        entry["visible"] = vis[i].get("visible")
            except Exception:
                pass

        # radio groups summary
        groups = {}
        for c in controls:
            if (c.get("tag") or "").upper() == "INPUT" and (c.get("type") or "").lower() == "radio":
                key = c.get("name") or c.get("dataQa") or c.get("id") or "<anon>"
                groups.setdefault(key, {"name": key, "required": False, "labels": []})
                lab = (c.get("label") or "").strip()
                if lab and lab not in groups[key]["labels"]:
                    groups[key]["labels"].append(lab)
                if c.get("required"):
                    groups[key]["required"] = True
        report["radio_groups"] = list(groups.values())

        report["form_detected"] = bool(controls) or bool(report["screening_questions"])

        # run the existing Stage 18 extractor on this DOM
        ex = extract_application_form(
            vacancy_stable_id="inspection:1", url=vacancy_url,
            dom_snapshot=snapshot, canonical_id=None,
        )
        report["extractor_result"] = {
            "application_type": ex.application_type.value,
            "questions": [
                {"id": q.id, "label": q.label[:60], "type": q.normalized_type.value,
                 "required": q.required, "options": q.options,
                 "requires_review": q.requires_review, "reason": q.reason[:90]}
                for q in ex.questions
            ],
            "controls_used": (ex.extraction_meta or {}).get("controls_used"),
        }

        # verdict
        if report["captcha"]:
            report["verdict"] = "BLOCKED_BY_CAPTCHA"
            report["reason"] = "captcha challenge present on page"
        elif report["auth_form"] or report["login_redirect"]:
            report["verdict"] = "BLOCKED_BY_AUTH"
            report["reason"] = "auth-form present / login redirect - session not authenticated for HH"
        elif not controls:
            report["verdict"] = "FORM_NOT_FOUND"
            report["reason"] = "no form controls found on page (authenticated?)"
        else:
            report["verdict"] = "AUTHENTICATED_FORM_INSPECTED"
            report["reason"] = f"{len(controls)} real controls read from authenticated form DOM"
        return report
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VACANCY
    rep = inspect(url)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())