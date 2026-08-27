"""Stage 20A: safe MANUAL HH form capture (CDP attach, strictly read-only).

The user manually:
  1. is logged into HH,
  2. opens a vacancy,
  3. clicks "Откликнуться",
  4. the response form modal appears.

This tool then ATTACHES to the already-open browser over CDP and performs a
READ-ONLY DOM inspection of the open form. It NEVER navigates, never clicks,
never fills, never uploads, never submits, never logs in.

CRITICAL SAFETY (confirmed live in Stage 19):
    A fresh GET to https://hh.ru/applicant/vacancy_response?vacancyId=... can
    AUTO-SUBMIT a real application. This tool therefore contains NO page.goto
    calls at all (enforced by tests) and NEVER navigates or reloads.
    An ALREADY-OPEN tab at such a URL that THE USER created manually is safe
    to READ: it is accepted as a capture candidate with a caution note.

If no manually-opened form is found, the verdict is BLOCKED_BY_MANUAL_FORM
and the tool never tries to open anything itself.

Snapshot contains form STRUCTURE only: no cookies, no storage_state, no
tokens, no passwords, no full-page HTML.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

DEFAULT_CDP_URL = os.getenv("HH_CDP_URL", "http://127.0.0.1:9222")
DEFAULT_OUT = os.path.join("artifacts", "hh_manual_form_snapshot.json")

# URLs that may initiate/confirm a response - inspection is hard-refused.
FORBIDDEN_URL_MARKERS = (
    "applicant/vacancy_response",
    "applicant/resume_response",
    "/negotiations/confirm",
)

# Read-only DOM APIs used by this tool (documentation + test contract):
#   page.url, page.title(), page.query_selector_all(), page.eval_on_selector_all()
# FORBIDDEN (must never appear in this module): goto, click, fill, type,
#   set_input_files, check, uncheck, press, keyboard, mouse, request, route.

_SENSITIVE_KEY_MARKERS = ("cookie", "storage_state", "token", "password", "authorization", "secret", "html")

# JS: collect real form controls with full attributes (read-only).
_CONTROLS_JS = """els => els.map(e => ({
    tag: e.tagName,
    type: e.getAttribute('type') || (e.tagName === 'SELECT' ? 'select' : (e.tagName === 'TEXTAREA' ? 'textarea' : 'text')),
    name: e.getAttribute('name'),
    id: e.id || null,
    dataQa: e.getAttribute('data-qa'),
    label: (function(el){
        try {
            if (el.labels && el.labels.length) return (el.labels[0].innerText || '').trim();
            const lb = el.getAttribute('aria-labelledby');
            if (lb) { const l = document.getElementById(lb); if (l) return (l.innerText || '').trim(); }
            const la = el.getAttribute('aria-label');
            if (la) return la.trim();
            if (typeof el.closest === 'function') {
                const wrap = el.closest('label');
                if (wrap) return (wrap.innerText || '').trim();
            }
        } catch (err) {}
        return null;
    })(e),
    ariaLabel: e.getAttribute('aria-label'),
    ariaLabelledby: e.getAttribute('aria-labelledby'),
    required: !!(e.required || e.getAttribute('aria-required') === 'true'),
    requiredAttr: e.required === true ? true : (e.getAttribute('aria-required') === 'true' ? true : (e.hasAttribute('required') ? false : null)),
    ariaRequired: e.getAttribute('aria-required'),
    placeholder: e.getAttribute('placeholder'),
    visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
    disabled: !!e.disabled,
    readOnly: !!e.readOnly,
    options: e.tagName === 'SELECT' ? Array.from(e.options).map(function(o){ return {text:(o.text||'').trim(), value:o.value, disabled:!!o.disabled}; }) : null
}))"""

_BUTTONS_JS = """els => els.map(e => ({
    tag: e.tagName,
    type: e.getAttribute('type'),
    text: (e.innerText || '').trim().slice(0, 80),
    dataQa: e.getAttribute('data-qa'),
    disabled: !!e.disabled,
    visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
    cls: (e.className || '').toString().slice(0, 80)
}))"""

# Single read-only inspection expression for raw-CDP Runtime.evaluate.
# Pure DOM reads: no navigation, no clicks, no mutations, no network.
_INSPECTION_JS = """() => {
    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const labelOf = (el) => {
        try {
            if (el.labels && el.labels.length) return (el.labels[0].innerText || '').trim();
            const lb = el.getAttribute('aria-labelledby');
            if (lb) { const l = document.getElementById(lb); if (l) return (l.innerText || '').trim(); }
            const la = el.getAttribute('aria-label');
            if (la) return la.trim();
            if (typeof el.closest === 'function') {
                const wrap = el.closest('label');
                if (wrap) return (wrap.innerText || '').trim();
            }
        } catch (err) {}
        return null;
    };
    const controls = Array.from(document.querySelectorAll("input:not([type='hidden']), textarea, select")).map(e => ({
        tag: e.tagName,
        type: e.getAttribute('type') || (e.tagName === 'SELECT' ? 'select' : (e.tagName === 'TEXTAREA' ? 'textarea' : 'text')),
        name: e.getAttribute('name'),
        id: e.id || null,
        dataQa: e.getAttribute('data-qa'),
        label: labelOf(e),
        ariaLabel: e.getAttribute('aria-label'),
        ariaLabelledby: e.getAttribute('aria-labelledby'),
        required: !!(e.required || e.getAttribute('aria-required') === 'true'),
        requiredAttr: e.required === true ? true : (e.getAttribute('aria-required') === 'true' ? true : (e.hasAttribute('required') ? false : null)),
        ariaRequired: e.getAttribute('aria-required'),
        placeholder: e.getAttribute('placeholder'),
        visible: visible(e),
        disabled: !!e.disabled,
        readOnly: !!e.readOnly,
        options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => ({text:(o.text||'').trim(), value:o.value, disabled:!!o.disabled})) : null
    }));
    const buttons = Array.from(document.querySelectorAll('button')).map(e => ({
        tag: e.tagName,
        type: e.getAttribute('type'),
        text: (e.innerText || '').trim().slice(0, 80),
        dataQa: e.getAttribute('data-qa'),
        disabled: !!e.disabled,
        visible: visible(e),
        cls: (e.className || '').toString().slice(0, 80)
    }));
    let modal = null;
    const modalSels = ["[role='dialog']", "[class*='modal' i]", "[data-qa*='modal' i]", ".bloko-modal"];
    for (const sel of modalSels) {
        for (const el of document.querySelectorAll(sel)) {
            if (!visible(el)) continue;
            const n = el.querySelectorAll('input, textarea, select').length;
            if (!modal || n > modal.controlCount) modal = {
                selector: sel, tag: el.tagName, role: el.getAttribute('role'),
                cls: (el.className || '').toString().slice(0, 120),
                dataQa: el.getAttribute('data-qa'),
                controlCount: n, textHead: (el.innerText || '').trim().slice(0, 300)
            };
        }
    }
    const radio_groups = {};
    const checkbox_groups = {};
    for (const c of controls) {
        if (c.type === 'radio' || c.type === 'checkbox') {
            const key = c.name || c.dataQa || c.id || '<anon>';
            const bucket = (c.type === 'radio') ? radio_groups : checkbox_groups;
            if (!bucket[key]) bucket[key] = {name: key, labels: [], required: false};
            const lab = (c.label || '').trim();
            if (lab && !bucket[key].labels.includes(lab)) bucket[key].labels.push(lab);
            if (c.required) bucket[key].required = true;
        }
    }
    // Stage 20C: DOM-proven question stems.
    // Rule (verified live on hh.ru questionnaire): the stem is a sibling
    // element of the options-container inside the parent block; its text
    // differs from every option label and its class carries a text/label/
    // title marker. Nothing is guessed - when no such sibling exists the
    // stem is simply absent.
    const stemFor = (els, optionLabels, excludeTexts) => {
        let anc = els[0].parentElement;
        while (anc && !els.every(e => anc.contains(e))) anc = anc.parentElement;
        if (!anc) return null;
        let cur = anc;
        for (let lvl = 0; lvl < 4 && cur.parentElement; lvl++) {
            cur = cur.parentElement;
            for (const child of cur.children) {
                if (child.contains(anc) || child === anc) continue;
                const t = (child.innerText || '').trim();
                if (!t || t.length > 300) continue;
                if (optionLabels.some(ol => t === ol)) continue;
                if (excludeTexts.some(et => t === et)) continue;
                const cls = (child.className || '').toString();
                const isHeading = /^H[1-6]$/.test(child.tagName);
                const isTextish = /text|label|title|question/i.test(cls) || child.getAttribute('data-qa');
                const hasDirectText = Array.from(child.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
                if (isHeading || isTextish || hasDirectText) {
                    return {stem: t.slice(0, 300), tag: child.tagName, cls: cls.slice(0, 80), level: lvl + 1};
                }
            }
        }
        return null;
    };
    const question_groups = [];
    const allByName = {};
    for (const c of controls) {
        const k = c.name || c.dataQa || c.id;
        if (k && (c.type === 'radio' || c.type === 'checkbox')) (allByName[k] = allByName[k] || []).push(c);
    }
    // need live elements to walk the DOM for the real question stem
    const liveInputs = Array.from(document.querySelectorAll("input[type='radio'], input[type='checkbox']"));
    const liveByName = {};
    for (const li of liveInputs) {
        const n = li.getAttribute('name');
        if (n && n.startsWith('task_')) (liveByName[n] = liveByName[n] || []).push(li);
    }
    for (const [name, els] of Object.entries(allByName)) {
        if (!els.length) continue;
        let optionLabels = [];
        try {
            optionLabels = els.map(e => (e.label || '').trim()).filter(Boolean);
        } catch (err) { optionLabels = []; }
        const liveEls = liveByName[name] || els;
        const s = stemFor(liveEls, optionLabels, []);
        if (s) question_groups.push({name, stem: s.stem, stem_tag: s.tag, stem_cls: s.cls, stem_level: s.level});
    }
    // standalone textareas (incl. _text): stems via the same rule
    const liveTas = Array.from(document.querySelectorAll('textarea'));
    for (const ta of liveTas) {
        const name = ta.getAttribute('name');
        if (!name || !name.startsWith('task_')) continue;
        const s = stemFor([ta], [], ['Писать тут']);
        if (s) question_groups.push({name, stem: s.stem, stem_tag: s.tag, stem_cls: s.cls, stem_level: s.level});
    }
    return {
        url: location.href,
        title: document.title,
        form_detected: controls.length > 0,
        modal: modal,
        controls: controls,
        radio_groups: Object.values(radio_groups),
        checkbox_groups: Object.values(checkbox_groups),
        question_groups: question_groups,
        buttons: buttons,
        extraction_meta: {
            capture: 'manual_cdp_raw',
            control_count: controls.length,
            button_count: buttons.length,
            visible_controls: controls.filter(c => c.visible).length,
            note: 'structure only; no html/cookies/tokens stored'
        }
    };
}"""

_MODAL_JS = """() => {
    const sels = ["[role='dialog']", "[class*='modal' i]", "[data-qa*='modal' i]", ".bloko-modal"];
    let best = null;
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            const vis = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            if (!vis) continue;
            const n = el.querySelectorAll('input, textarea, select').length;
            if (!best || n > best.n) best = {el, n, sel};
        }
    }
    if (!best) return null;
    const el = best.el;
    return {
        selector: best.sel,
        tag: el.tagName,
        role: el.getAttribute('role'),
        cls: (el.className || '').toString().slice(0, 120),
        dataQa: el.getAttribute('data-qa'),
        controlCount: best.n,
        textHead: (el.innerText || '').trim().slice(0, 300)
    };
}"""


class CaptureSafetyError(Exception):
    """Fail-closed: raised if a forbidden action/URL is attempted."""


def _caution_url(url: str) -> Optional[str]:
    """Return a caution note if the URL is a response-flow URL.

    Stage 19 finding: a fresh GET to applicant/vacancy_response can auto-submit
    a real application. THIS TOOL NEVER NAVIGATES (no goto anywhere; enforced
    by tests), so an ALREADY-OPEN tab the user created themselves is safe to
    READ. We only attach a caution note; we still never reload/navigate.
    """
    low = (url or "").lower()
    for marker in FORBIDDEN_URL_MARKERS:
        if marker in low:
            return (f"response-flow URL ({marker}) opened manually by the user; "
                    "read-only DOM inspection only - this tool never navigates")
    return None


def _guard_url(url: str) -> None:
    """Hard navigation-guard primitive (kept for any future code that might
    navigate): raises on response-flow URLs."""
    low = (url or "").lower()
    for marker in FORBIDDEN_URL_MARKERS:
        if marker in low:
            raise CaptureSafetyError(
                f"FORBIDDEN URL detected ({marker}) - response URLs auto-submit "
                "a real application (Stage 19). Navigation is forbidden."
            )


def _sanitize(obj: Any) -> Any:
    """Recursively drop sensitive keys from a snapshot structure."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(m in kl for m in _SENSITIVE_KEY_MARKERS):
                continue
            out[k] = _sanitize(v)
        return out
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    return obj


def classify_question_source(label: str, qtype: str) -> str:
    """Stage 17A source classification from the REAL label (no guessing of
    answers - only which truth source the field belongs to)."""
    lab = (label or "").lower()
    profile_markers = ("имя", "фамилия", "фио", "name", "email", "почта", "e-mail",
                       "телефон", "phone", "город", "location", "где вы живете",
                       "где вы находитесь")
    if any(m in lab for m in profile_markers):
        return "PROFILE"
    if "сопроводител" in lab or "cover" in lab or "письмо" in lab:
        return "SYSTEM"
    if "резюме" in lab or "resume" in lab:
        return "SYSTEM"
    return "SCREENING"


def find_form_pages(pages: List[Any]) -> Dict[str, Any]:
    """Find already-open tabs with a manually-opened HH response form.

    Pure read-only: uses page.url and page-level DOM reads only.
    Response-flow URLs opened BY THE USER are allowed as candidates with a
    caution note (this tool never navigates); they are also reported.
    """
    candidates = []
    cautioned = []
    for page in pages:
        try:
            url = page.url
        except Exception:
            continue
        caution = _caution_url(url)
        if caution:
            cautioned.append({"url": url, "caution": caution})
        if "hh.ru" not in url:
            continue
        try:
            modal = page.eval_on_selector_all(
                "[role='dialog'], [class*='modal' i], [data-qa*='modal' i], .bloko-modal",
                """els => {
                    for (const el of els) {
                        const vis = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        if (!vis) continue;
                        const n = el.querySelectorAll('input, textarea, select').length;
                        if (n > 0) return {found: true, controls: n};
                    }
                    return {found: false};
                }""")
            if modal and modal.get("found"):
                candidates.append({"page": page, "url": url, "modal_controls": modal.get("controls"),
                                   "caution": caution})
        except Exception:
            continue
    return {
        "candidates": candidates,
        "cautioned": cautioned,
        "verdict": "FORM_OPEN" if candidates else "BLOCKED_BY_MANUAL_FORM",
    }


def inspect_page_dom(page: Any) -> Dict[str, Any]:
    """READ-ONLY snapshot of the open form on one page.

    Uses only page.url / page.title() / query_selector_all / eval_on_selector_all.
    Never navigates, never clicks, never fills.
    """
    url = page.url
    caution = _caution_url(url)  # recorded, inspection of user-opened tabs allowed
    try:
        title = page.title()
    except Exception:
        title = ""

    modal = page.evaluate(_MODAL_JS) if hasattr(page, "evaluate") else None
    if modal is None and hasattr(page, "eval_on_selector_all"):
        # page-like fakes in tests may implement only eval_on_selector_all
        try:
            wrapper = page.eval_on_selector_all(
                "[role='dialog'], [class*='modal' i], [data-qa*='modal' i], .bloko-modal",
                "els => els.length")
            modal = {"selector": "count-only", "controlCount": wrapper}
        except Exception:
            modal = None

    controls = page.eval_on_selector_all(
        "input:not([type='hidden']), textarea, select", _CONTROLS_JS)
    buttons = page.eval_on_selector_all("button", _BUTTONS_JS)

    # radio / checkbox groups by name
    radio_groups: Dict[str, Dict[str, Any]] = {}
    checkbox_groups: Dict[str, Dict[str, Any]] = {}
    for c in controls:
        if c.get("type") == "radio":
            key = c.get("name") or c.get("dataQa") or c.get("id") or "<anon>"
            g = radio_groups.setdefault(key, {"name": key, "labels": [], "required": False})
            lab = (c.get("label") or "").strip()
            if lab and lab not in g["labels"]:
                g["labels"].append(lab)
            if c.get("required"):
                g["required"] = True
        elif c.get("type") == "checkbox":
            key = c.get("name") or c.get("dataQa") or c.get("id") or "<anon>"
            g = checkbox_groups.setdefault(key, {"name": key, "labels": [], "required": False})
            lab = (c.get("label") or "").strip()
            if lab and lab not in g["labels"]:
                g["labels"].append(lab)
            if c.get("required"):
                g["required"] = True

    snapshot = {
        "captured_at": datetime.utcnow().isoformat(),
        "url": url,
        "title": title,
        "caution": caution,
        "form_detected": bool(controls),
        "modal": modal,
        "controls": controls,
        "radio_groups": list(radio_groups.values()),
        "checkbox_groups": list(checkbox_groups.values()),
        "question_groups": [],
        "buttons": buttons,
        "extraction_meta": {
            "capture": "manual_cdp_attach",
            "control_count": len(controls),
            "button_count": len(buttons),
            "visible_controls": sum(1 for c in controls if c.get("visible")),
            "note": "structure only; no html/cookies/tokens stored",
        },
    }
    return _sanitize(snapshot)


def _vacancy_stable_id_from_url(url: str) -> Optional[str]:
    """Extract hh:<vacancyId> from a response-flow URL. Never guesses.

    Returns None when vacancyId is absent or malformed.
    """
    from urllib.parse import urlparse, parse_qs
    if not url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
        vals = qs.get("vacancyId") or []
        if vals and vals[0].strip().isdigit():
            return f"hh:{vals[0].strip()}"
    except Exception:
        pass
    return None


def normalize_to_application_form(snapshot: Dict[str, Any], vacancy_stable_id: str):
    """Normalize a manual-capture snapshot into the existing Stage 17A-19
    ApplicationForm contract (no contract changes)."""
    from ai_assistant.hh_extractor import extract_application_form, QuestionSource

    # flatten rich select options to plain strings for the normalized contract
    # (the full detail stays in the snapshot report)
    flat_controls = []
    for c in snapshot.get("controls") or []:
        c2 = dict(c)
        if isinstance(c2.get("options"), list):
            c2["options"] = [
                (o.get("text") or o.get("value") or "") if isinstance(o, dict) else str(o)
                for o in c2["options"]
            ]
            c2["options"] = [o for o in c2["options"] if o]
        flat_controls.append(c2)

    dom_snapshot = {
        "html": "",  # intentionally empty: no full-page HTML in manual capture
        "body_text": "",
        "questions": [],
        "controls": flat_controls,
        "question_groups": snapshot.get("question_groups") or [],
        "auth_form": False,
        "apply_link": None,
        "final_url": snapshot.get("url") or "",
        "title": snapshot.get("title") or "",
        "site": "hh.ru",
    }
    form = extract_application_form(
        vacancy_stable_id=vacancy_stable_id,
        url=dom_snapshot["final_url"],
        dom_snapshot=dom_snapshot,
        canonical_id=None,
    )
    # source classification from real labels (17A: PROFILE / SYSTEM / SCREENING)
    for q in form.questions:
        ctrl = next((c for c in snapshot.get("controls") or []
                     if c.get("dataQa") and q.id.endswith(c["dataQa"])), None)
        label = (ctrl or {}).get("label") or q.label
        q.source = QuestionSource(classify_question_source(label, q.normalized_type.value))
    # enrich meta with modal/buttons info (structure only)
    meta = dict(form.extraction_meta or {})
    meta["manual_capture"] = True
    meta["modal"] = snapshot.get("modal")
    meta["button_count"] = len(snapshot.get("buttons") or [])
    form.extraction_meta = meta
    return form


def capture(cdp_url: str = DEFAULT_CDP_URL, out_path: str = DEFAULT_OUT) -> Dict[str, Any]:
    """Attach over CDP to the already-open browser and capture the form.

    Primary transport: raw CDP (HTTP /json/list + WebSocket Runtime.evaluate)
    - proven to work against current Chrome builds where Playwright's
    connect_over_cdp hangs. Read-only: only DOM-reading JS is evaluated.
    """
    result: Dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat(),
        "cdp_url": cdp_url,
        "verdict": None,
        "reason": None,
        "snapshot": None,
        "form": None,
    }
    try:
        targets = _list_targets(cdp_url)
    except Exception as e:
        result["verdict"] = "BLOCKED_BY_MANUAL_FORM"
        result["reason"] = (
            f"CDP not reachable at {cdp_url}: {e}. Start the browser with "
            "--remote-debugging-port=9222 and open the form manually."
        )
        return result

    candidates = []
    cautioned = []
    for t in targets:
        if t.get("type") != "page":
            continue
        url = t.get("url") or ""
        caution = _caution_url(url)
        if caution:
            cautioned.append({"url": url, "caution": caution})
        if "hh.ru" not in url:
            continue
        candidates.append(t)
    result["cautioned_tabs"] = cautioned

    if not candidates:
        result["verdict"] = "BLOCKED_BY_MANUAL_FORM"
        result["reason"] = (
            "No hh.ru vacancy tab found in the attached browser. "
            "Open a vacancy and click 'Откликнуться' yourself, then re-run."
        )
        return result

    for t in candidates:
        try:
            snapshot = _inspect_target_via_cdp(t)
        except CaptureSafetyError:
            raise
        except Exception as e:
            result.setdefault("target_errors", []).append(
                {"url": t.get("url"), "error": str(e)[:200]})
            continue
        if not snapshot.get("form_detected"):
            continue
        vid = _vacancy_stable_id_from_url(snapshot.get("url") or "") or "manual"
        form = normalize_to_application_form(snapshot, vid)
        result["verdict"] = "AUTHENTICATED_FORM_INSPECTED"
        result["reason"] = (f"{len(snapshot['controls'])} real controls captured "
                            "from manually opened form")
        result["snapshot"] = snapshot
        result["form"] = json.loads(form.model_dump_json())
        if out_path:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"snapshot": snapshot, "form": result["form"]}, f,
                          ensure_ascii=False, indent=1)
            result["out_path"] = out_path
        return result

    result["verdict"] = "BLOCKED_BY_MANUAL_FORM"
    result["reason"] = (
        "hh.ru vacancy tab(s) found but no open response-form modal. "
        "Click 'Откликнуться' manually, keep the modal open, then re-run."
    )
    return result


def _list_targets(cdp_url: str) -> List[Dict[str, Any]]:
    """HTTP /json/list - read-only target enumeration."""
    import urllib.request
    with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _inspect_target_via_cdp(target: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    """Evaluate the read-only inspection JS on one open tab via raw CDP."""
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("target has no webSocketDebuggerUrl")
    return _run_async(_evaluate_inspection(ws_url, timeout))


def _run_async(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("no sync loop available")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


async def _evaluate_inspection(ws_url: str, timeout: float) -> Dict[str, Any]:
    import websockets
    expression = "JSON.stringify((" + _INSPECTION_JS + ")())"
    async with websockets.connect(ws_url, open_timeout=timeout,
                                  close_timeout=timeout) as ws:
        await asyncio.wait_for(ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": False},
        })), timeout)
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout)
            msg = json.loads(raw)
            if msg.get("id") == 1:
                res = msg.get("result", {}).get("result", {})
                if res.get("type") == "string":
                    return json.loads(res["value"])
                raise RuntimeError(f"unexpected evaluate result: {str(res)[:200]}")


def main() -> int:
    # utf-8 console output (script mode only; never at import time - breaks pytest capture)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    res = capture(out_path=out)
    print(json.dumps({k: v for k, v in res.items() if k != "snapshot"}, ensure_ascii=False, indent=1, default=str))
    snap = res.get("snapshot")
    if snap:
        print()
        print("=== CONTROLS ===")
        for c in snap["controls"]:
            print(json.dumps(c, ensure_ascii=False))
        print()
        print("=== BUTTONS (read-only, never clicked) ===")
        for b in snap["buttons"]:
            print(json.dumps(b, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())