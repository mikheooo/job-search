"""Stage 20F: safe prefill execution + read-only verification.

Executes an ALREADY-BUILT PrefillPlan (Stage 20E) against the ALREADY-OPEN
HH form tab over raw CDP. Hard rules:

- NO navigation (never goto/reload; URL is guarded before and after).
- NO submit (submit button is never touched; no submit events dispatched).
- NO upload.
- radio/checkbox: native HTMLInputElement 'checked' setter + click/change
  events so React-controlled HH questionnaires observe the change (the
  framework's value tracker is bypassed exactly like the textarea setter).
- Only operations from the PrefillPlan are applied; every operation is
  re-verified before mutation (exists, exact name/label, not disabled,
  not readonly, value non-empty). Review/None/UNKNOWN answers can never
  reach execution (defense in depth: plan construction already excludes
  them, and execution re-checks).
- Read-only verification after mutation: checked/value read back and
  compared to expected. VERIFIED / PARTIALLY_VERIFIED / FAILED.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from .hh_extractor import QuestionType
from .prefill_plan import PrefillPlan, PrefillOperation


class MutationResult(BaseModel):
    question_id: str
    target_name: Optional[str] = None
    target_label: Optional[str] = None
    op_type: str = ""
    value: str = ""
    ok: bool = False
    reason: str = ""

    model_config = {"extra": "forbid"}


class ExecutionReport(BaseModel):
    verdict: str = "NOTHING_TO_EXECUTE"  # VERIFIED | PARTIALLY_VERIFIED | FAILED | FAIL_CLOSED | NOTHING_TO_EXECUTE
    generated_at: str = ""
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    navigation_count: int = 0
    click_count: int = 0
    submit_count: int = 0
    fill_count: int = 0
    upload_count: int = 0
    successful_mutations: int = 0
    failed_mutations: int = 0
    skipped_mutations: int = 0
    mutations: List[MutationResult] = Field(default_factory=list)
    verification: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


# ---------- JS builders (direct DOM mutations, no clicks, no submit) ----------

def _js_str(value: str) -> str:
    """Safely embed a python string into JS as a literal."""
    return json.dumps(value, ensure_ascii=False)


def _mutation_js(op: PrefillOperation) -> str:
    """Build a read-mostly mutation expression for one planned operation.

    - radio/checkbox: el.checked = true (+ change event so framework state
      updates; NOT a click, NOT a submit).
    - textarea/text: native value setter + input/change events (React-safe).
    - select: set value if the option exists.
    Guards: exact name + exact label, not disabled, not readonly.
    """
    name = _js_str(op.target.name or "")
    label = _js_str(op.target.label or "")
    value = _js_str(op.value)
    t = (op.target.type or "").lower()
    if t in ("radio", "checkbox"):
        input_type = t
        return f"""(() => {{
    const name = {name}, label = {label};
    const els = Array.from(document.querySelectorAll("input[type='{input_type}'][name=" + JSON.stringify(name) + "]"));
    for (const el of els) {{
        let lab = '';
        try {{
            if (el.labels && el.labels[0]) lab = (el.labels[0].innerText || '').trim();
            else if (typeof el.closest === 'function') {{ const w = el.closest('label'); if (w) lab = (w.innerText || '').trim(); }}
        }} catch (err) {{}}
        if (lab !== label) continue;
        if (el.disabled || el.readOnly) return JSON.stringify({{ok: false, reason: 'control is disabled/readonly'}});
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked').set;
        setter.call(el, true);
        el.dispatchEvent(new Event('click', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return JSON.stringify({{ok: el.checked === true, checked: el.checked, reason: el.checked ? '' : 'checked did not stick'}});
    }}
    return JSON.stringify({{ok: false, reason: '{input_type} with exact label not found'}});
}})()"""
    if t in ("textarea", "text"):
        tag = "TEXTAREA" if t == "textarea" else "INPUT"
        return f"""(() => {{
    const name = {name}, label = {label}, value = {value};
    const els = Array.from(document.querySelectorAll("{tag.lower()}[name=" + JSON.stringify(name) + "]"));
    for (const el of els) {{
        let lab = '';
        try {{
            const lb = el.getAttribute('aria-labelledby');
            if (lb) {{ const l = document.getElementById(lb); if (l) lab = (l.innerText || '').trim(); }}
            if (!lab) {{ const la = el.getAttribute('aria-label'); if (la) lab = la.trim(); }}
            if (!lab && typeof el.closest === 'function') {{ const w = el.closest('label'); if (w) lab = (w.innerText || '').trim(); }}
        }} catch (err) {{}}
        if (label && lab && lab !== label) continue;
        if (el.disabled || el.readOnly) return JSON.stringify({{ok: false, reason: 'control is disabled/readonly'}});
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, value);
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return JSON.stringify({{ok: el.value === value, value: el.value, reason: el.value === value ? '' : 'value did not stick'}});
    }}
    return JSON.stringify({{ok: false, reason: '{t} with exact name not found'}});
}})()"""
    if t == "select":
        return f"""(() => {{
    const name = {name}, value = {value};
    const els = Array.from(document.querySelectorAll("select[name=" + JSON.stringify(name) + "]"));
    for (const el of els) {{
        if (el.disabled || el.readOnly) return JSON.stringify({{ok: false, reason: 'control is disabled/readonly'}});
        const opt = Array.from(el.options).find(o => (o.text || '').trim() === value || o.value === value);
        if (!opt) return JSON.stringify({{ok: false, reason: 'option not found in select'}});
        el.value = opt.value;
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return JSON.stringify({{ok: el.value === opt.value, reason: ''}});
    }}
    return JSON.stringify({{ok: false, reason: 'select with exact name not found'}});
}})()"""
    return "JSON.stringify({ok: false, reason: 'unsupported operation type'})"


def _verify_js(op: PrefillOperation) -> str:
    """Read-only verification: read back checked/value for one target."""
    name = _js_str(op.target.name or "")
    label = _js_str(op.target.label or "")
    value = _js_str(op.value)
    t = (op.target.type or "").lower()
    if t in ("radio", "checkbox"):
        return f"""(() => {{
    const name = {name}, label = {label};
    const els = Array.from(document.querySelectorAll("input[type='{t}'][name=" + JSON.stringify(name) + "]"));
    for (const el of els) {{
        let lab = '';
        try {{
            if (el.labels && el.labels[0]) lab = (el.labels[0].innerText || '').trim();
            else if (typeof el.closest === 'function') {{ const w = el.closest('label'); if (w) lab = (w.innerText || '').trim(); }}
        }} catch (err) {{}}
        if (lab !== label) continue;
        return JSON.stringify({{found: true, checked: el.checked, disabled: el.disabled, readOnly: el.readOnly}});
    }}
    return JSON.stringify({{found: false}});
}})()"""
    if t in ("textarea", "text"):
        tag = "TEXTAREA" if t == "textarea" else "INPUT"
        return f"""(() => {{
    const name = {name};
    const els = Array.from(document.querySelectorAll("{tag.lower()}[name=" + JSON.stringify(name) + "]"));
    for (const el of els) {{
        return JSON.stringify({{found: true, value: el.value, disabled: el.disabled, readOnly: el.readOnly}});
    }}
    return JSON.stringify({{found: false}});
}})()"""
    if t == "select":
        return f"""(() => {{
    const name = {name}, value = {value};
    const els = Array.from(document.querySelectorAll("select[name=" + JSON.stringify(name) + "]"));
    for (const el of els) {{
        const opt = Array.from(el.options).find(o => o.value === el.value);
        return JSON.stringify({{found: true, value: el.value, text: opt ? (opt.text || '').trim() : null}});
    }}
    return JSON.stringify({{found: false}});
}})()"""
    return "JSON.stringify({found: false})"


def _url_js() -> str:
    return "JSON.stringify({url: location.href})"


def execute_prefill_plan(
    plan: PrefillPlan,
    evaluate_fn: Callable[[str], str],
    allowed_url_markers: List[str] = ("hh.ru",),
    required_url_markers: Optional[List[str]] = None,
    stop_on_failure: bool = False,
) -> ExecutionReport:
    """Execute a PrefillPlan against an already-open tab.

    evaluate_fn: callable(expression) -> JSON-string result of
    Runtime.evaluate (raw CDP). Read-only transport; all mutations happen
    inside the evaluated JS as direct DOM property sets.

    Fail-closed URL guard: location.href must contain ALL allowed_url_markers
    before any mutation and must be unchanged after.

    stop_on_failure (Stage 20G atomicity): when True, the first failed
    mutation stops execution; remaining operations are reported as skipped.
    """
    report = ExecutionReport(generated_at=datetime.utcnow().isoformat())
    required = list(allowed_url_markers) + list(required_url_markers or [])

    def _read_url() -> Optional[str]:
        try:
            raw = evaluate_fn(_url_js())
            return json.loads(raw).get("url")
        except Exception as e:
            report.errors.append(f"URL read failed: {e}")
            return None

    # --- URL guard (before) ---
    url_before = _read_url()
    report.url_before = url_before
    if not url_before:
        report.verdict = "FAIL_CLOSED"
        report.errors.append("Cannot read current URL - fail closed")
        return report
    missing = [m for m in required if m.lower() not in url_before.lower()]
    if missing:
        report.verdict = "FAIL_CLOSED"
        report.errors.append(f"URL guard failed: missing markers {missing} in {url_before}")
        return report

    if not plan.operations:
        report.verdict = "NOTHING_TO_EXECUTE"
        return report

    # --- execute operations (defense in depth: re-check every op) ---
    aborted = False
    executed_ops: List[PrefillOperation] = []
    for idx, op in enumerate(plan.operations):
        # Stage 20G atomicity: stop subsequent mutations after a failure.
        if aborted and stop_on_failure:
            report.mutations.append(MutationResult(
                question_id=op.question_id, target_name=op.target.name,
                target_label=op.target.label, op_type=op.target.type,
                value=op.value, ok=False, reason="skipped due to earlier failure"))
            report.skipped_mutations += 1
            continue
        # Never execute review/None/UNKNOWN answers.
        if not op.value or not str(op.value).strip():
            report.mutations.append(MutationResult(
                question_id=op.question_id, target_name=op.target.name,
                target_label=op.target.label, op_type=op.target.type,
                value=op.value, ok=False, reason="empty/review value - refused"))
            report.failed_mutations += 1
            if stop_on_failure:
                aborted = True
            continue
        if op.target.disabled or op.target.readOnly:
            report.mutations.append(MutationResult(
                question_id=op.question_id, target_name=op.target.name,
                target_label=op.target.label, op_type=op.target.type,
                value=op.value, ok=False, reason="target disabled/readonly - no mutation attempted"))
            report.failed_mutations += 1
            if stop_on_failure:
                aborted = True
            continue
        try:
            raw = evaluate_fn(_mutation_js(op))
            res = json.loads(raw)
        except Exception as e:
            report.mutations.append(MutationResult(
                question_id=op.question_id, target_name=op.target.name,
                target_label=op.target.label, op_type=op.target.type,
                value=op.value, ok=False, reason=f"evaluate failed: {e}"))
            report.failed_mutations += 1
            if stop_on_failure:
                aborted = True
            continue
        ok = bool(res.get("ok"))
        mr = MutationResult(
            question_id=op.question_id, target_name=op.target.name,
            target_label=op.target.label, op_type=op.target.type,
            value=op.value, ok=ok, reason=res.get("reason", ""))
        report.mutations.append(mr)
        if ok:
            report.successful_mutations += 1
            executed_ops.append(op)
            if op.target.type in ("textarea", "text"):
                report.fill_count += 1
        else:
            report.failed_mutations += 1
            if stop_on_failure:
                aborted = True

    # --- read-only verification (only executed mutations) ---
    for op in executed_ops:
        try:
            raw = evaluate_fn(_verify_js(op))
            vres = json.loads(raw)
        except Exception as e:
            report.verification.append({"question_id": op.question_id, "ok": False, "reason": f"verify failed: {e}"})
            continue
        if not vres.get("found"):
            report.verification.append({"question_id": op.question_id, "ok": False, "reason": "target not found in DOM"})
            continue
        t = (op.target.type or "").lower()
        if t in ("radio", "checkbox"):
            ok = vres.get("checked") is True
            report.verification.append({"question_id": op.question_id,
                                        "value": op.value,
                                        "ok": ok,
                                        "checked": vres.get("checked"),
                                        "reason": "" if ok else "checked != true"})
        elif t in ("textarea", "text", "select"):
            ok = vres.get("value") == op.value
            report.verification.append({"question_id": op.question_id,
                                        "value": op.value,
                                        "ok": ok,
                                        "actual_value": vres.get("value"),
                                        "reason": "" if ok else "value != expected"})

    # --- URL guard (after) ---
    url_after = _read_url()
    report.url_after = url_after
    if url_after != url_before:
        report.verdict = "FAILED"
        report.errors.append(f"URL changed during execution: {url_before} -> {url_after}")
        return report

    # --- verdict ---
    total = len(plan.operations)
    verified_ok = sum(1 for v in report.verification if v.get("ok"))
    if report.failed_mutations == 0 and total > 0 and verified_ok == total:
        report.verdict = "VERIFIED"
    elif report.successful_mutations > 0:
        report.verdict = "PARTIALLY_VERIFIED" if verified_ok > 0 else "FAILED"
    else:
        report.verdict = "FAILED"
    return report


# ---------- raw CDP transport ----------

def make_cdp_evaluate(cdp_url: str, url_substring: str):
    """Return evaluate_fn(expression)->str bound to the already-open tab whose
    URL contains url_substring. Read-only transport (Runtime.evaluate)."""
    import urllib.request

    def _list_targets():
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    targets = _list_targets()
    tab = next((t for t in targets
                if t.get("type") == "page" and url_substring in (t.get("url") or "")
                and t.get("webSocketDebuggerUrl")), None)
    if tab is None:
        raise RuntimeError(f"no open tab matching {url_substring!r} on {cdp_url}")
    ws_url = tab["webSocketDebuggerUrl"]

    def evaluate_fn(expression: str) -> str:
        async def _run() -> str:
            import websockets
            async with websockets.connect(ws_url, open_timeout=20, close_timeout=20) as ws:
                await asyncio.wait_for(ws.send(json.dumps({
                    "id": 1, "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True,
                               "awaitPromise": False},
                })), 20)
                while True:
                    raw = await asyncio.wait_for(ws.recv(), 20)
                    msg = json.loads(raw)
                    if msg.get("id") == 1:
                        res = msg.get("result", {}).get("result", {})
                        if res.get("type") == "string":
                            return res["value"]
                        raise RuntimeError(f"unexpected evaluate result: {str(res)[:200]}")

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                raise RuntimeError("no sync loop")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_run())

    return evaluate_fn
# ---------- isolated-world transport (read-only, cross-origin iframe) ---------
#
# Stage 30C-2: the HH chatik conversation lives in a CROSS-ORIGIN iframe
# (chatik.hh.ru/chat/<id>) inside the hh.ru messages page. A main-frame
# Runtime.evaluate (make_cdp_evaluate) cannot reach into that iframe's DOM, so
# `fetch_hh_conversation_readonly` returned messages=[] even when a conversation
# was open. The read-only fix: evaluate the SAME expression inside the chatik
# iframe's own isolated world via CDP Page.createIsolatedWorld on the matching
# frame, then Runtime.evaluate in that execution context. No navigation, no
# clicks, no DOM writes, no sends - the same access as the existing transport.
# This is a transport-only extension: the read-only JS (_CONVERSATION_JS) and
# the CLI classify/preview handlers are unchanged in behaviour.


class ChatikFrameNotFound(RuntimeError):
    """Raised internally when no frame matches the expected chatik iframe."""


# Sentinel returned by the isolated-world evaluate_fn when the expected chatik
# iframe is not present, so callers degrade to the existing 'no messages,
# nothing sent' safety path instead of raising mid-read.
_EMPTY_CONVERSATION_JSON = json.dumps({
    "error": "chatik frame not found",
    "conversation_id": None,
    "messages": [],
    "composer_present": False,
})


def select_frame_id_by_url(frame_tree, url_substrings):
    """Return the frameId of a frame whose URL contains any url_substring.

    Prefers a frame whose URL contains '/chat/' (an open HH conversation);
    otherwise returns the first matching frame. Returns None when none match.
    Pure, deterministic - unit-testable without a browser.
    """
    subs = tuple(url_substrings) if not isinstance(url_substrings, str) else (url_substrings,)

    def _walk(node, depth, matches):
        frame = node.get("frame") or {}
        url = frame.get("url") or ""
        fid = frame.get("id")
        if fid and url and any(s.lower() in url.lower() for s in subs):
            matches.append((depth, url, fid))
        for child in node.get("childFrames") or []:
            _walk(child, depth + 1, matches)

    matches = []
    _walk(frame_tree or {}, 0, matches)
    if not matches:
        return None
    chat = [m for m in matches if "/chat/" in m[1].lower()]
    return (chat[0] if chat else matches[0])[2]


def _run_coro(coro_fn, *args, **kwargs):
    """Run a coroutine synchronously from the (possibly absent) event loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("no sync loop")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro_fn(*args, **kwargs))


async def _cdp_evaluate_in_frame(ws_url: str, frame_substrings, expression: str) -> str:
    """CDP: run `expression` inside the isolated world of the first frame whose
    URL matches frame_substrings. Read-only, request/response only."""
    import websockets

    async with websockets.connect(ws_url, open_timeout=20, close_timeout=20) as ws:
        _id = 0

        async def _call(method, params=None):
            nonlocal _id
            _id += 1
            req_id = _id
            await asyncio.wait_for(ws.send(json.dumps({
                "id": req_id, "method": method, "params": params or {},
            })), 20)
            while True:
                raw = await asyncio.wait_for(ws.recv(), 20)
                msg = json.loads(raw)
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(
                            f"CDP {method} error: {json.dumps(msg['error'])}")
                    return msg.get("result", {})

        await _call("Page.enable")
        tree = await _call("Page.getFrameTree")
        frame_id = select_frame_id_by_url(tree.get("frameTree"), frame_substrings)
        if frame_id is None:
            raise ChatikFrameNotFound(
                f"no HH chatik frame matched {frame_substrings}")
        created = await _call("Page.createIsolatedWorld", {
            "frameId": frame_id, "grantUniveralAccess": False})
        ctx_id = created.get("executionContextId")
        if ctx_id is None:
            raise RuntimeError("Page.createIsolatedWorld returned no contextId")
        res = await _call("Runtime.evaluate", {
            "expression": expression, "contextId": ctx_id,
            "returnByValue": True, "awaitPromise": False})
        inner = res.get("result", {})
        if inner.get("type") == "string":
            return inner["value"]
        raise RuntimeError(f"unexpected isolated-world result: {str(inner)[:200]}")


def _cdp_list_targets(cdp_url: str):
    """Return the CDP /json/list target array for cdp_url."""
    import urllib.request

    with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def make_isolated_world_evaluate(
    cdp_url: str,
    page_substring: str,
    frame_substrings,
    _run_in_frame=None,
):
    """Return evaluate_fn(expression)->str bound to the isolated world of the
    matching chatik iframe inside the open HH page tab.

    Read-only transport: CDP Page.getFrameTree + Page.createIsolatedWorld +
    Runtime.evaluate in the selected frame's own world (cross-origin safe).
    Unlike make_cdp_evaluate, the expression runs in the IFRAME, not the main
    frame, so _CONVERSATION_JS can actually see the chatik message DOM.

    When the expected iframe is not present, evaluate_fn returns an empty
    conversation JSON (messages:[]) so callers take the existing 'no messages,
    nothing sent' safety path - never raising mid-read, never attempting a send.

    _run_in_frame: injectable test seam (expression)->str.
    """
    if _run_in_frame is not None:
        # Test seam: bypass target discovery + websocket entirely. Keeps the
        # same read-only degrade contract (missing frame -> empty conversation).
        def _seam(expression: str) -> str:
            try:
                return _run_in_frame(expression)
            except ChatikFrameNotFound:
                return _EMPTY_CONVERSATION_JSON
        return _seam

    tab = next((t for t in _cdp_list_targets(cdp_url)
                if t.get("type") == "page"
                and page_substring in (t.get("url") or "")
                and t.get("webSocketDebuggerUrl")), None)
    if tab is None:
        raise RuntimeError(f"no open tab matching {page_substring!r} on {cdp_url}")
    ws_url = tab["webSocketDebuggerUrl"]

    def evaluate_fn(expression: str) -> str:
        try:
            return _run_coro(_cdp_evaluate_in_frame, ws_url,
                             frame_substrings, expression)
        except ChatikFrameNotFound:
            return _EMPTY_CONVERSATION_JSON

    return evaluate_fn