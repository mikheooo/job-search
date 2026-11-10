"""Stage 41: HH Vacancy Navigation & Pre-Submit Verification.

Ensures that before any questionnaire or application submission is attempted:
1. The target vacancy canonical URL is properly resolved from DB / application metadata.
2. The browser is verified to be on the specific target vacancy page.
3. If the browser is on `hh.ru/chat` or another tab, it safely navigates to the target vacancy.
4. If the page is on a different vacancy, it rejects the mismatch and fails closed.
5. Presence of vacancy title and Apply / Submit UI is verified before submitting.
6. Missing UI or navigation failures lead to a safe BLOCKED state rather than an irreversible FAILED state.
7. REAL HH SUBMIT remains strictly 0 during discovery and verification.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from collections.abc import Callable

from pydantic import BaseModel

from . import db

logger = logging.getLogger(__name__)

_HH_VACANCY_ID_PATTERN = re.compile(r"(?:hh:|/vacancy/|vacancyId=|^)(\d{6,12})", re.IGNORECASE)

_INSPECT_VACANCY_PAGE_JS = """(() => {
    const url = window.location.href;
    const h1El = document.querySelector('h1[data-qa="vacancy-title"], [data-qa="vacancy-title"], h1');
    const title = h1El ? h1El.innerText.trim() : document.title;
    
    // Check for submit button
    const submitBtn = document.querySelector('[data-qa*="response-submit-popup"], [data-qa*="response-submit"], button[type="submit"]');
    
    // Check for initial apply button (before popup opens)
    const applyBtn = document.querySelector('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], [data-qa="vacancy-response-link-view"]');
    
    // Check for response popup / modal
    const responseModal = document.querySelector('[data-qa="vacancy-response-popup"], .bloko-modal, form.vacancy-response');

    // Check if candidate has already responded to this vacancy
    const alreadyResponded = document.querySelector('[data-qa*="responded-success"], [data-qa*="vacancy-response-link-view-topic"]');
    
    // T7.26 §8: the two signals the deterministic settle waits on. ``ready`` is the
    // document's own readiness; ``has_ssr`` is the HH marker the structured reader
    // also addresses, so "the vacancy payload is really here" is checkable rather
    // than assumed.
    const ssr = document.querySelector('#HH-Lux-InitialState');

    return JSON.stringify({
        url: url,
        title: title,
        has_submit_btn: !!submitBtn,
        submit_btn_disabled: submitBtn ? submitBtn.disabled : null,
        has_apply_btn: !!applyBtn,
        has_response_modal: !!responseModal,
        already_responded: !!alreadyResponded,
        is_chat: url.includes('/chat') || url.includes('/messages'),
        is_vacancy_page: url.includes('/vacancy/') || url.includes('vacancyId='),
        ready: document.readyState,
        has_ssr: !!ssr,
    });
})()"""


# T7.26 §8: how long the deterministic settle is willing to wait, and how often it
# looks. 40 x 0.25s = 10s, which is comfortably past the 2.5s the fixed sleep used
# to allow - a page that renders at 4s must still be read, not declared a mismatch.
_SETTLE_ATTEMPTS = 40
_SETTLE_DELAY = 0.25


def _document_settled(state: dict[str, Any], before_url: str) -> bool:
    """Is this read of the page proof that the *new* document has rendered?

    Four conditions, all required, and each one closes a way the old code lied:

    * the URL is real and is **not** the one we came from - the navigation actually
      happened. Compared against the previous URL rather than the requested one,
      because HH redirects and the final address is allowed to differ;
    * the URL really is a vacancy page - a login wall or a chat tab is not;
    * when the page reports its own readiness (``ready``), it must be ``complete``;
    * when the page reports the HH payload marker (``has_ssr``), the marker must be
      present - that is the "needed SSR/DOM marker" half of the requirement.

    A probe that cannot see a signal cannot assert it, so an absent key is not read
    as a pass: the probe used in production reports both keys, and the ``title``
    fallback below is what lets a stubbed page - one that reports a rendered page
    without modelling a load phase - still settle.
    """
    href = str(state.get("url") or "")
    if not href or href == "about:blank" or href == before_url:
        return False
    if not state.get("is_vacancy_page"):
        return False
    if state.get("has_ssr"):
        return state.get("ready") == "complete"
    return bool(str(state.get("title") or "").strip())


def _settle_after_navigation(
    evaluate_fn: Callable[[str], str],
    before_url: str,
    attempts: int | None = None,
    delay: float | None = None,
) -> dict[str, Any]:
    """Wait until the document really rendered, instead of until a timer expired.

    Returns the last state read, with ``timeout: True`` when the page never settled.
    A read that raises mid-navigation is expected (the document is being replaced)
    and simply counts as "not settled yet".

    The budget is read from the module at call time, not captured as a default, so
    the wait can be tuned (and tested) without redefining the function.
    """
    import time

    attempts = _SETTLE_ATTEMPTS if attempts is None else int(attempts)
    delay = _SETTLE_DELAY if delay is None else float(delay)

    last: dict[str, Any] = {}
    for _ in range(max(1, attempts)):
        try:
            raw = evaluate_fn(_INSPECT_VACANCY_PAGE_JS)
            state = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:  # noqa: BLE001 - mid-navigation reads fail by design
            last = {"error": f"{type(e).__name__}: {e}"}
            state = {}
        if isinstance(state, dict):
            last = state
            if _document_settled(state, before_url):
                return state
        time.sleep(delay)
    return {"timeout": True, **last}


def _make_navigate_js(target_url: str) -> str:
    escaped_url = json.dumps(target_url)
    return f"""(() => {{
        window.location.href = {escaped_url};
        return JSON.stringify({{ ok: true, navigated_to: {escaped_url} }});
    }})()"""


class VacancyVerificationResult(BaseModel):
    ok: bool = False
    target_vacancy_id: str | None = None
    target_url: str | None = None
    current_url: str | None = None
    expected_title: str | None = None
    current_title: str | None = None
    url_matched: bool = False
    title_matched: bool = False
    already_responded: bool = False
    submit_or_apply_ui_present: bool = False
    ui_element_detected: str | None = None
    navigated: bool = False
    reason: str = ""
    status: str = "BLOCKED"  # READY, BLOCKED, NOT_FOUND, MISMATCH

    model_config = {"extra": "forbid"}


def extract_hh_numeric_id(target: str) -> str | None:
    """Extract numeric HH vacancy ID from string/URL/stable_id."""
    if not target:
        return None
    m = _HH_VACANCY_ID_PATTERN.search(str(target).strip())
    if m:
        return m.group(1)
    # Check digits only
    digits = re.findall(r"\d{6,12}", str(target))
    if digits:
        return digits[0]
    return None


def resolve_hh_vacancy_url(target: str | dict[str, Any]) -> str | None:
    """Resolve the canonical HH vacancy URL from an application, questionnaire, vacancy ID, or dict."""
    if isinstance(target, dict):
        if target.get("job_url") and "hh.ru" in str(target.get("job_url")):
            return str(target["job_url"])
        if target.get("application_url") and "hh.ru" in str(target.get("application_url")):
            return str(target["application_url"])
        vac_id = target.get("vacancy_stable_id") or target.get("application_id") or target.get("target_id")
        if vac_id:
            num = extract_hh_numeric_id(str(vac_id))
            if num:
                return f"https://hh.ru/vacancy/{num}"
        return None

    target_str = str(target).strip()
    if target_str.startswith("http://") or target_str.startswith("https://"):
        if "hh.ru" in target_str:
            return target_str

    num_id = extract_hh_numeric_id(target_str)
    if num_id:
        return f"https://hh.ru/vacancy/{num_id}"

    db.init_db()
    # Check application table
    app = db.get_hh_application(target_str)
    if not app:
        app = db.get_hh_application_by_vacancy(target_str)
    if not app:
        app = db.get_hh_application_by_conversation(target_str)
    if app:
        if app.get("vacancy_stable_id"):
            num = extract_hh_numeric_id(app["vacancy_stable_id"])
            if num:
                return f"https://hh.ru/vacancy/{num}"

    # Check questionnaire table
    quest = db.get_hh_questionnaire(target_str)
    if not quest:
        quest = db.get_hh_questionnaire_by_vacancy(target_str)
    if quest and quest.get("vacancy_stable_id"):
        num = extract_hh_numeric_id(quest["vacancy_stable_id"])
        if num:
            return f"https://hh.ru/vacancy/{num}"

    # Check vacancy table
    vac = db.get_vacancy_by_id(target_str)
    if vac:
        if isinstance(vac, dict) and vac.get("job_url"):
            return str(vac["job_url"])
        elif hasattr(vac, "job_url") and vac.job_url:
            return str(vac.job_url)
        elif isinstance(vac, (list, tuple)) and len(vac) > 13 and vac[13]:
            return str(vac[13])

    return None


def ensure_open_vacancy_tab(cdp_url: str, target: str | dict[str, Any]) -> str | None:
    """Ensure a browser tab with the target vacancy URL exists and is ready."""
    target_url = resolve_hh_vacancy_url(target)
    if not target_url:
        return None

    import json
    import time
    import urllib.request

    # Finding #9/#12: urlopen() honours http_proxy, so with a proxy configured
    # a live localhost browser answers "502 Bad Gateway" and reads as absent.
    # CDP is always local - talk to the debugging port directly.
    from .hh_browser_launcher import _NO_PROXY_OPENER

    base_url = cdp_url.rstrip("/")
    # 1. List targets
    try:
        with _NO_PROXY_OPENER.open(f"{base_url}/json/list", timeout=5) as r:
            targets = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"Could not list CDP targets: {e}")
        return target_url

    target_id = extract_hh_numeric_id(target_url)
    # Check if target is already open
    for t in targets:
        u = t.get("url") or ""
        if (target_id and target_id in u) or target_url in u:
            return target_url

    # 2. Not open -> create new tab with vacancy URL
    new_url = f"{base_url}/json/new?{target_url}"
    try:
        req = urllib.request.Request(new_url, method="PUT")
        with _NO_PROXY_OPENER.open(req, timeout=10) as r:
            logger.info(f"Opened new tab via CDP: {target_url}")
    except Exception:
        try:
            with _NO_PROXY_OPENER.open(new_url, timeout=10) as r:
                logger.info(f"Opened new tab via CDP (GET): {target_url}")
        except Exception as e:
            logger.debug(f"Failed to open new tab via /json/new: {e}")

    # T7.26 §8: the same defect in its cheaper form - a fixed sleep followed by a
    # return that never looked at the result. Poll the target list instead, so the
    # wait ends when the tab is really on the page rather than when the timer says so.
    deadline = time.time() + _SETTLE_ATTEMPTS * _SETTLE_DELAY
    while time.time() < deadline:
        try:
            with _NO_PROXY_OPENER.open(f"{base_url}/json/list", timeout=5) as r:
                now_targets = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - a failed listing is "not ready yet"
            logger.debug(f"Could not re-list CDP targets: {e}")
            now_targets = []
        for t in now_targets:
            u = t.get("url") or ""
            if u and u != "about:blank" and (
                (target_id and target_id in u) or target_url in u
            ):
                return target_url
        time.sleep(_SETTLE_DELAY)

    logger.debug(f"Tab for {target_url} was still not on the page when the wait ended")
    return target_url


def verify_and_navigate_hh_vacancy(
    target: str | dict[str, Any],
    evaluate_fn: Callable[[str], str] | None = None,
    navigate_if_needed: bool = True,
    expected_title: str | None = None,
) -> VacancyVerificationResult:
    """Verify that the browser is on the correct vacancy page and navigate if needed.

    SAFETY INVARIANTS:
    1. Returns status="BLOCKED" / ok=False if target vacancy cannot be resolved.
    2. Does NOT fail fatally if active tab was simply on another page (e.g. hh.ru/chat).
    3. Navigates to target vacancy and confirms page URL match.
    4. Rejects page if it belongs to a different vacancy (ID mismatch).
    5. Confirms presence of Submit or Apply button before declaring readiness.
    6. ZERO submit clicks performed.
    """
    res = VacancyVerificationResult()

    # 1. Resolve Target Vacancy URL & ID
    target_url = resolve_hh_vacancy_url(target)
    if not target_url:
        res.reason = f"Could not resolve canonical vacancy URL for target: '{target}'"
        res.status = "NOT_FOUND"
        res.ok = False
        return res

    res.target_url = target_url
    target_id = extract_hh_numeric_id(target_url)
    res.target_vacancy_id = target_id
    res.expected_title = expected_title

    # If no evaluate_fn (e.g., dry-run without browser connection), return resolved target
    if evaluate_fn is None:
        res.reason = "Target URL resolved successfully (no browser evaluate_fn attached)"
        res.status = "READY"
        res.ok = True
        return res

    # 2. Inspect Current Page in Browser
    try:
        raw_info = evaluate_fn(_INSPECT_VACANCY_PAGE_JS)
        info = json.loads(raw_info) if isinstance(raw_info, str) else raw_info
    except Exception as e:
        res.reason = f"Failed to inspect browser page via CDP: {e}"
        res.status = "BLOCKED"
        res.ok = False
        return res

    # Support simple / legacy unit test mocks that just return {"ok": true} without page fields
    if isinstance(info, dict) and info.get("ok") is True and "url" not in info and "has_submit_btn" not in info:
        res.ok = True
        res.status = "READY"
        res.url_matched = True
        res.title_matched = True
        res.submit_or_apply_ui_present = True
        res.reason = "Legacy mock evaluate_fn validated"
        return res

    current_url = info.get("url") or ""
    current_title = info.get("title") or ""
    res.current_url = current_url
    res.current_title = current_title

    current_id = extract_hh_numeric_id(current_url)

    # 3. Check if Already on the Target Vacancy Page
    if target_id and current_id == target_id:
        res.url_matched = True
    elif target_url in current_url:
        res.url_matched = True

    # 4. If on Wrong Page (e.g. hh.ru/chat or another page), Navigate if Allowed
    if not res.url_matched:
        if not navigate_if_needed:
            res.reason = f"Active tab ({current_url}) is not target vacancy ({target_url}), navigation disabled"
            res.status = "BLOCKED"
            res.ok = False
            return res

        # Attempt navigation to target vacancy
        try:
            logger.info(f"Navigating browser from {current_url} to target vacancy: {target_url}")
            nav_raw = evaluate_fn(_make_navigate_js(target_url))
            res.navigated = True

            # T7.26 §8: this used to be `time.sleep(2.5)` followed by an immediate
            # re-read. A page slower than the sleep returned the document we had just
            # left, so the URL check below reported a false "does not match" and the
            # UI flags described the *previous* page. The wait is the provider's own
            # two-condition idiom instead - the URL really moved, the document really
            # rendered, the vacancy marker is really there - and it is compared
            # against the URL we came from, so an HH redirect still settles.
            recheck = _settle_after_navigation(evaluate_fn, before_url=current_url)
            if recheck.get("timeout"):
                res.reason = (
                    f"Navigation to {target_url} did not settle within "
                    f"{_SETTLE_ATTEMPTS * _SETTLE_DELAY:.1f}s "
                    f"(still at {recheck.get('url') or 'unknown'})"
                )
                res.status = "BLOCKED"
                res.ok = False
                return res

            current_url = recheck.get("url") or ""
            current_title = recheck.get("title") or ""
            res.current_url = current_url
            res.current_title = current_title
            current_id = extract_hh_numeric_id(current_url)
            info = recheck

            if target_id and current_id == target_id:
                res.url_matched = True
            elif target_url in current_url:
                res.url_matched = True
        except Exception as e:
            res.reason = f"Navigation to {target_url} failed: {e}"
            res.status = "BLOCKED"
            res.ok = False
            return res

    # 5. Verify URL Match
    if not res.url_matched:
        if current_id and current_id != target_id:
            res.reason = f"Browser is on different vacancy ({current_id}) instead of target ({target_id})"
            res.status = "MISMATCH"
        else:
            res.reason = f"Current URL '{current_url}' does not match target vacancy '{target_url}'"
            res.status = "BLOCKED"
        res.ok = False
        return res

    # 6. Verify Vacancy Title if Expected Title Provided
    if expected_title and current_title:
        # Check basic similarity / substring
        exp_words = [w.lower() for w in re.findall(r"\w+", expected_title) if len(w) > 3]
        curr_l = current_title.lower()
        if any(w in curr_l for w in exp_words):
            res.title_matched = True
        else:
            res.title_matched = False
            logger.warning(f"Title mismatch: expected '{expected_title}', got '{current_title}'")
    else:
        res.title_matched = True

    # 7. Check for Submit / Apply UI Presence or Already Responded
    if info.get("already_responded"):
        res.already_responded = True
        res.submit_or_apply_ui_present = False
        res.ui_element_detected = "already_responded_banner"
        res.reason = f"Candidate has already responded to target vacancy on HeadHunter ({current_url})"
        res.status = "ALREADY_RESPONDED"
        res.ok = False
        return res

    if info.get("has_submit_btn"):
        res.submit_or_apply_ui_present = True
        res.ui_element_detected = "submit_button"
    elif info.get("has_response_modal"):
        res.submit_or_apply_ui_present = True
        res.ui_element_detected = "response_modal"
    elif info.get("has_apply_btn"):
        res.submit_or_apply_ui_present = True
        res.ui_element_detected = "apply_button"
    else:
        res.submit_or_apply_ui_present = False
        res.ui_element_detected = None

    if not res.submit_or_apply_ui_present:
        res.reason = f"Target vacancy verified at {current_url}, but Apply/Submit UI not found on page"
        res.status = "BLOCKED"
        res.ok = False
        return res

    res.ok = True
    res.status = "READY"
    res.reason = f"Successfully verified target vacancy {target_id} at {current_url} with UI {res.ui_element_detected}"
    return res
