"""Stage 41 / Remediation Phase 1.5: Unified Live Page Checks for HeadHunter CDP Sessions.

Consolidates all read-only live DOM inspections:
- URL host and numeric ID match (fail-closed on mismatch)
- 404, CAPTCHA, Cloudflare, Access Denied, Login State
- Vacancy title similarity match
- DOM 'Already Responded' banner
- Submit / Apply button existence and disabled state
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_HH_NUMERIC_ID_PATTERN = re.compile(r"(?:hh:|/vacancy/|vacancyId=|^)(\d{6,12})", re.IGNORECASE)

# BLE001 finding #34: the response form's own answer controls.
#
# hh.ru names every field of a response form "task_<id>", and "<id>_text" is the
# "Свой вариант" companion of a choice group rather than a question of its own.
# This block is the single source of truth for "how many questions does this
# page hold", and two independent places use it:
#   * _INSPECT_LIVE_PAGE_JS below, which reports the counts to the submission
#     gates so they can tell "no questions" from "questions nobody read";
#   * submit_click_js in hh_submission.py, which refuses the click outright.
# It used to be duplicated verbatim in both. A mutation that broke the selector
# in one copy then survived the entire test suite, because the pin asserted the
# fragment "name^='task_'" and the other half of the selector still contained
# it. One block, interpolated into both, cannot drift.
_SCREENING_GUARD_PLACEHOLDER = "__SCREENING_GUARD_JS__"

_SCREENING_GUARD_JS = """
        const answerFields = Array.from(document.querySelectorAll(
            "input[type='radio'][name^='task_'], input[type='checkbox'][name^='task_']," +
            " textarea[name^='task_'], select[name^='task_']"));
        const allNames = {};
        const filledNames = {};
        for (const f of answerFields) {
            const n = f.getAttribute('name') || '';
            allNames[n] = true;
            if (f.checked || (f.value || '').trim()) filledNames[n] = true;
        }
        const unfilledNames = [];
        for (const f of answerFields) {
            const n = f.getAttribute('name') || '';
            if (filledNames[n]) continue;
            // "<group>_text" is the "Свой вариант" companion of a choice group,
            // so it is not a question of its own - count it only when no control
            // with its base name exists.
            if (n.slice(-5) === '_text' && allNames[n.slice(0, -5)]) continue;
            if (unfilledNames.indexOf(n) === -1) unfilledNames.push(n);
        }
"""


def _inject_screening_guard(js: str) -> str:
    """Interpolate the shared screening-form guard into a JS snippet.

    Raises instead of returning the snippet unchanged: an un-replaced
    placeholder is a bare JS identifier, so the snippet would still parse and
    would fail only in the browser - on a live submission.

    Watch the wording of any comment added to a snippet that goes through here.
    The browser doubles in tests/test_stage46_*.py and test_stage47_*.py decide
    what a script is by looking for substrings in it, and one of their rules
    reads "contains the word c-l-i-c-k AND the word response-submit". A comment
    in this file's inspection snippet that used that word made the live
    inspection answer as if it were the submit call: the runner got a payload
    with no URL and refused with "URL host '' does not belong to hh.ru". Four
    tests failed, and the cause was a comment. Finding #35 in
    docs/ble001_triage.md.
    """
    if _SCREENING_GUARD_PLACEHOLDER not in js:
        raise ValueError(
            "JS snippet carries no " + _SCREENING_GUARD_PLACEHOLDER + " placeholder"
        )
    return js.replace(_SCREENING_GUARD_PLACEHOLDER, _SCREENING_GUARD_JS)


_INSPECT_LIVE_PAGE_JS = _inject_screening_guard("""// hh_live_page_inspect
(() => {
    try {
        const url = window.location.href || "";
        const h1El = document.querySelector('h1[data-qa="vacancy-title"], [data-qa="vacancy-title"], h1');
        const title = h1El ? (h1El.innerText || "").trim() : (document.title || "").trim();
        const bodyText = document.body ? (document.body.innerText || "").slice(0, 4000).toLowerCase() : "";
        const titleLower = title.toLowerCase();

        // 404 indicators
        const is404 = titleLower.includes("404") || titleLower.includes("страница не найдена") || bodyText.includes("страница не найдена") || bodyText.includes("page not found") || bodyText.includes("вакансия не найдена") || bodyText.includes("вакансия удалена") || bodyText.includes("вакансия в архиве");

        // CAPTCHA / Cloudflare indicators
        const captchaEl = document.querySelector('.captcha, [data-qa="captcha"], #captcha, .cf-turnstile, #cf-challenge-running');
        const isCaptcha = !!captchaEl || titleLower.includes("captcha") || bodyText.includes("робот ли вы") || bodyText.includes("cloudflare") || bodyText.includes("captcha");

        // Access Denied indicators
        const isAccessDenied = titleLower.includes("access denied") || titleLower.includes("доступ ограничен") || titleLower.includes("доступ запрещён") || titleLower.includes("доступ запрещен") || bodyText.includes("access denied") || bodyText.includes("доступ к странице ограничен") || bodyText.includes("доступ ограничен");

        // Login required indicators
        const loginEl = document.querySelector('.account-login-actions, [data-qa="login"], [data-qa="account-login-submit"]');
        const isLoginRequired = !!loginEl || titleLower.includes("вход в личный кабинет") || titleLower.includes("войдите в личный кабинет") || bodyText.includes("войдите в личный кабинет");

        // Already responded banner / elements
        const alreadyRespondedEl = document.querySelector('[data-qa*="responded-success"], [data-qa*="response-link-view-topic"], [data-qa*="vacancy-already-responded"], .vacancy-response-status');
        const alreadyResponded = !!alreadyRespondedEl || bodyText.includes("вы уже откликнулись") || bodyText.includes("отклик уже отправлен");

        // Submit button inside popup / modal
        const submitElement = document.querySelector('[data-qa*="response-submit-popup"], [data-qa*="response-submit"], button[type="submit"]');
        
        // Initial apply button on vacancy page
        const applyElement = document.querySelector('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], [data-qa="vacancy-response-link-view"]');

        // Response modal / form
        const responseModal = document.querySelector('[data-qa="vacancy-response-popup"], .bloko-modal, form.vacancy-response');

        // BLE001 finding #34: the response form's own answer controls. The
        // counting block is shared verbatim with the guard that precedes the
        // submit in hh_submission.py, so the two cannot drift apart.
        __SCREENING_GUARD_JS__

        return JSON.stringify({
            ok: true,
            url: url,
            title: title,
            is_404: is404,
            is_captcha: isCaptcha,
            is_access_denied: isAccessDenied,
            is_login_required: isLoginRequired,
            already_responded: alreadyResponded,
            has_submit_btn: !!submitElement,
            submit_btn_disabled: submitElement ? !!submitElement.disabled : false,
            has_apply_btn: !!applyElement,
            has_response_modal: !!responseModal,
            screening_control_count: answerFields.length,
            screening_unanswered_count: unfilledNames.length,
            screening_unanswered_sample: unfilledNames.slice(0, 5),
        });
    } catch (e) {
        return JSON.stringify({ ok: false, error: String(e) });
    }
})()""")


def extract_numeric_id(target: str) -> str | None:
    """Extract numeric HH vacancy ID from string/URL/stable_id."""
    if not target:
        return None
    m = _HH_NUMERIC_ID_PATTERN.search(str(target).strip())
    if m:
        return m.group(1)
    digits = re.findall(r"\d+", str(target))
    if digits:
        return digits[-1]
    return None


@dataclass
class LivePageResult:
    # Exact required fields per audit specification
    is_ok: bool = False
    error_reason: str | None = None  # 'CAPTCHA', 'VACANCY_NOT_FOUND', 'ACCESS_DENIED', 'AUTH_REQUIRED', 'WRONG_PAGE', 'ALREADY_APPLIED'
    current_url: str | None = None
    page_title: str | None = None
    already_applied: bool = False
    numeric_id_match: bool = False
    title_matched: bool = False
    title_similarity: float = 0.0

    # Additional diagnostic and backwards-compatible fields
    status: str = "BLOCKED"  # READY, BLOCKED, FAIL_CLOSED, MISMATCH, ALREADY_RESPONDED
    reason: str = ""
    expected_id: str | None = None
    numeric_id: str | None = None
    has_submit_btn: bool = False
    submit_btn_disabled: bool = False
    has_apply_btn: bool = False
    has_response_modal: bool = False
    # BLE001 finding #34: how many answer controls the page itself holds.
    # 0 means "the page showed us no form" - not "there is no form".
    screening_control_count: int = 0
    screening_unanswered_count: int = 0
    is_404: bool = False
    is_captcha: bool = False
    is_access_denied: bool = False
    is_login_required: bool = False
    raw_info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.is_ok

    @ok.setter
    def ok(self, val: bool) -> None:
        self.is_ok = val

    @property
    def current_title(self) -> str:
        return self.page_title or ""

    @current_title.setter
    def current_title(self, val: str) -> None:
        self.page_title = val

    @property
    def url_matched(self) -> bool:
        return self.numeric_id_match

    @url_matched.setter
    def url_matched(self, val: bool) -> None:
        self.numeric_id_match = val

    @property
    def already_responded(self) -> bool:
        return self.already_applied

    @already_responded.setter
    def already_responded(self, val: bool) -> None:
        self.already_applied = val


def check_live_page(
    evaluate_fn: Callable[[str], str],
    expected_vacancy_id: str,
    expected_title: str | None = None,
) -> LivePageResult:
    """Execute live DOM inspection and verify all invariants fail-closed."""
    res = LivePageResult(expected_id=extract_numeric_id(expected_vacancy_id))

    try:
        raw = evaluate_fn(_INSPECT_LIVE_PAGE_JS)
        data = json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        res.is_ok = False
        res.status = "FAIL_CLOSED"
        res.error_reason = "FAIL_CLOSED"
        res.reason = f"Cannot inspect live DOM: {e}"
        return res

    if data.get("ok") is False or (not data.get("ok") and not data.get("url")):
        res.is_ok = False
        res.status = "FAIL_CLOSED"
        res.error_reason = "FAIL_CLOSED"
        res.reason = f"Live DOM inspection error: {data.get('error', 'unknown error')}"
        return res

    res.raw_info = data
    current_url = data.get("url") or ""
    current_title = data.get("title") or ""
    res.current_url = current_url
    res.page_title = current_title

    # 1. Host verification
    try:
        parsed = urlparse(current_url if "://" in current_url else f"https://{current_url}")
        host = (parsed.hostname or "").lower()
        if not host or (host != "hh.ru" and not host.endswith(".hh.ru")):
            res.is_ok = False
            res.status = "FAIL_CLOSED"
            res.error_reason = "FAIL_CLOSED"
            res.reason = f"URL host '{host}' does not belong to hh.ru or *.hh.ru"
            return res
    except Exception as e:  # noqa: BLE001
        res.is_ok = False
        res.status = "FAIL_CLOSED"
        res.error_reason = "FAIL_CLOSED"
        res.reason = f"Invalid current URL structure '{current_url}': {e}"
        return res

    # 2. Numeric ID match
    current_id = extract_numeric_id(current_url)
    res.numeric_id = current_id
    expected_num = res.expected_id
    if not expected_num:
        res.is_ok = False
        res.status = "FAIL_CLOSED"
        res.error_reason = "FAIL_CLOSED"
        res.reason = f"Expected vacancy ID '{expected_vacancy_id}' does not contain numeric digits"
        return res

    if not current_id or current_id != expected_num:
        res.is_ok = False
        res.status = "MISMATCH"
        res.numeric_id_match = False
        res.error_reason = "WRONG_PAGE"
        res.reason = f"Vacancy ID mismatch: expected {expected_num}, got {current_id} ({current_url})"
        return res

    res.numeric_id_match = True

    # 3. 404 check
    if data.get("is_404"):
        res.is_ok = False
        res.is_404 = True
        res.status = "BLOCKED"
        res.error_reason = "VACANCY_NOT_FOUND"
        res.reason = "404 Vacancy not found or archived"
        return res

    # 4. CAPTCHA / Cloudflare check
    if data.get("is_captcha"):
        res.is_ok = False
        res.is_captcha = True
        res.status = "BLOCKED"
        res.error_reason = "CAPTCHA"
        res.reason = "CAPTCHA or Cloudflare challenge detected on page"
        return res

    # 5. Access denied check
    if data.get("is_access_denied"):
        res.is_ok = False
        res.is_access_denied = True
        res.status = "BLOCKED"
        res.error_reason = "ACCESS_DENIED"
        res.reason = "Access denied to vacancy page"
        return res

    # 6. Login required check
    if data.get("is_login_required"):
        res.is_ok = False
        res.is_login_required = True
        res.status = "BLOCKED"
        res.error_reason = "AUTH_REQUIRED"
        res.reason = "Login / authentication required to view or apply to vacancy"
        return res

    # 7. Title similarity check.
    #
    # BLE001 finding #17, two holes in the old expression:
    #
    #   if exp_words and any(...) or sim >= 0.6 or not exp_words:
    #
    # (a) `or not exp_words` switched the check OFF for any title whose words
    #     are all 4 characters or shorter. Measured: expected "Go Dev" against
    #     a page titled "Уборщица" scored title_matched = True at similarity
    #     0.00. Two different jobs, and the only check that can catch a
    #     substitution said yes.
    #
    # (b) When nothing matched but similarity landed in [0.3, 0.6), the branch
    #     logged a warning and fell through - `is_ok` stayed True and the page
    #     was accepted. Measured: "HR Generalist" vs "HR Manager" (0.52), no
    #     shared word: a failed check that silently passed.
    #
    # Now: a title that does not match fails closed, at any similarity.
    if expected_title and current_title:
        res.title_similarity = difflib.SequenceMatcher(
            None, expected_title.lower().strip(), current_title.lower().strip()
        ).ratio()
        exp_words = [w.lower() for w in re.findall(r"\w+", expected_title) if len(w) > 3]
        curr_l = current_title.lower()
        if (exp_words and any(w in curr_l for w in exp_words)) or res.title_similarity >= 0.6:
            res.title_matched = True
        else:
            logger.warning(
                "Vacancy title low similarity (%0.2f): expected '%s', got '%s'",
                res.title_similarity,
                expected_title,
                current_title,
            )
            res.is_ok = False
            res.status = "MISMATCH"
            res.error_reason = "WRONG_PAGE"
            res.reason = f"Title mismatch (similarity {res.title_similarity:.2f}): expected '{expected_title}', got '{current_title}'"
            return res

    else:
        res.title_matched = True

    # 8. Already responded banner
    if data.get("already_responded"):
        res.is_ok = False
        res.already_applied = True
        res.status = "ALREADY_RESPONDED"
        res.error_reason = "ALREADY_APPLIED"
        res.reason = "Candidate has already responded to target vacancy on HeadHunter"
        return res

    # 9. Submit or Apply UI presence
    has_submit = bool(data.get("has_submit_btn"))
    submit_disabled = bool(data.get("submit_btn_disabled"))
    has_apply = bool(data.get("has_apply_btn"))
    has_modal = bool(data.get("has_response_modal"))

    res.has_submit_btn = has_submit
    res.submit_btn_disabled = submit_disabled
    res.has_apply_btn = has_apply
    res.has_response_modal = has_modal
    # BLE001 finding #34: report what the page holds, so the submission gates
    # can tell "no questions" from "questions nobody read". This is data, not a
    # verdict - check_live_page's own job stays "is this the right page".
    res.screening_control_count = int(data.get("screening_control_count") or 0)
    res.screening_unanswered_count = int(data.get("screening_unanswered_count") or 0)

    if not (has_submit or has_apply or has_modal):
        res.is_ok = False
        res.status = "BLOCKED"
        res.reason = "Neither submit button nor apply button found in DOM"
        return res

    res.is_ok = True
    res.status = "READY"
    res.reason = "Live page verified and ready for submission"
    return res
