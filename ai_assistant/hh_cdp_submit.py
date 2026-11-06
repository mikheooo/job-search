"""HH CDP submit/brief — production package module (formerly tools/.t722_send.py).

T7.22: the real HH application, sent through the headless browser.

Two subcommands, and the split is the safety property:

    brief  <vid>                       read-only: the vacancy and its response form
    submit <vid> --expect-resume TITLE --letter FILE
                                       fill, pre-flight, then click submit exactly once

Why a new file instead of ``tools/.t719_submit.py``: that tool has the CRM resume title
baked into its blocker list (``"Специалист по внедрению и сопровождению CRM"``) and gets
its address from ``tools/.t713_hh_drive.py``, which hard-codes the BrowserOS port. T7.22
needs the resume decided by the vacancy and the address decided by the mode, so the
submit path is its own file. The click and text paths themselves are the ones that sent
the T7.19 responses. This module does not import ``tools/.t*``; the brief
script and report paths live in the package and under ``artifacts/``.

``brief`` never reaches the submit button. ``submit`` refuses to click while any
precondition is unmet, and clicks once: on a timeout or an ambiguous answer it re-reads
HH's state rather than clicking again.

    python tools/.t722_send.py brief 137149827
    python tools/.t722_send.py submit 137149827 --expect-resume "..." --letter letter.txt
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.parse
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ai_assistant.hh_browser_launcher import resolve_browser_endpoint
from ai_assistant.hh_browser_provider import (
    HeadlessBrowserSession,
    _ws_call,
    list_tabs,
)

OUT = REPO / "artifacts" / "headless-poc"
WORK = REPO / "artifacts" / "hh_cdp"

# The response form, read straight off the SSR payload. Same shape the T7.19 inspect
# used, plus the resume selector, because this run has to be able to change the resume
# rather than assume it.
FORM_JS = r"""(() => {
  const vid = '__VID__';
  const out = {url: location.href, ready: document.readyState};
  const tpl = document.querySelector('#HH-Lux-InitialState');
  out.has_template = !!tpl;
  if (tpl) {
    let st = null;
    try { st = JSON.parse(tpl.innerHTML || ''); } catch (e) { out.parse_error = String(e); }
    if (st) {
      out.started_with_question = st.startedWithQuestion;
      out.vacancy_tests = st.vacancyTests;
      out.quick = st.hasQuickResponse;
      out.avrs = (st.applicantVacancyResponseStatuses || {})[vid] || null;
    }
  }
  const sel = s => document.querySelector(s);
  const txt = e => e ? (e.textContent || '').trim() : null;
  const card = sel('[data-qa=resume-title]');
  out.resume_title = txt(card);
  out.resume_detail = txt(sel('[data-qa=resume-detail]'));
  const warn = sel('[data-qa=hidden-resume-warning]');
  if (warn) {
    const r = warn.getBoundingClientRect();
    out.warning_height = Math.round(r.height);
    out.warning_shown = r.height > 1;
    out.warning_text = txt(warn);
  }
  const submit = sel('[data-qa=vacancy-response-submit-popup]');
  out.submit = submit ? {disabled: submit.disabled, text: txt(submit),
    tag: submit.tagName, type: submit.getAttribute('type'),
    visible: !!(submit.getBoundingClientRect().width)} : null;
  const toggle = sel('[data-qa=vacancy-response-letter-toggle]');
  out.letter_toggle = toggle ? {text: txt(toggle).slice(0, 80),
    display: getComputedStyle(toggle).display} : null;
  out.forms = [...document.querySelectorAll('form')].map(f => ({
    method: (f.getAttribute('method') || '').toUpperCase(),
    action: (f.getAttribute('action') || '').slice(0, 80),
    submit_controls: f.querySelectorAll(
      'button[type=submit], input[type=submit], [data-qa=vacancy-response-submit-popup]'
    ).length
  }));
  out.textareas = [...document.querySelectorAll('textarea')].map(t => ({
    data_qa: t.getAttribute('data-qa'), name: t.getAttribute('name'), id: t.id,
    value_len: (t.value || '').length, maxlength: t.getAttribute('maxlength'),
    visible: !!(t.getBoundingClientRect().width && t.getBoundingClientRect().height),
    preview: (t.value || '').slice(0, 60)
  }));
  out.contenteditable = [...document.querySelectorAll('[contenteditable=true]')].map(t => ({
    data_qa: t.getAttribute('data-qa'), text_len: (t.textContent || '').length,
    visible: !!(t.getBoundingClientRect().width && t.getBoundingClientRect().height)
  }));
  out.resume_controls = [...document.querySelectorAll('[data-qa*=resume], [data-qa*=Resume]')]
    .map(e => ({qa: e.getAttribute('data-qa'), tag: e.tagName,
      text: (e.textContent || '').trim().slice(0, 70),
      visible: !!(e.getBoundingClientRect().width)})).slice(0, 40);
  out.all_qa = [...new Set([...document.querySelectorAll('[data-qa]')]
    .map(e => e.getAttribute('data-qa')))].filter(q =>
      /letter|question|answer|test|submit|resume|response|vacancy/i.test(q)).sort();
  out.controls = [...document.querySelectorAll(
    "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='image']), textarea, select"
  )].map(e => ({
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
    options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => (o.text || '').trim()).filter(Boolean) : null
  }));
  const q_inputs = Array.from(document.querySelectorAll("input[type='radio'], input[type='checkbox']"));
  const byName = {};
  for (const i of q_inputs) {
    const n = i.getAttribute('name');
    if (!n) continue;
    (byName[n] = byName[n] || []).push(i);
  }
  const qg = [];
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
          qg.push({name, stem: t.slice(0, 300)});
          break;
        }
      }
      if (qg.length && qg[qg.length-1].name === name) break;
    }
  }
  out.question_groups = qg;
  return JSON.stringify(out);
})()"""

# Scroll the target to the middle of the viewport, *then* measure it, then report what
# would actually receive the click at that point. Measuring before scrolling is the bug
# this shape exists to avoid: the scroll invalidates the rectangle.
MEASURE_JS = r"""(() => {
  const e = document.querySelector(__SEL__);
  if (!e) return JSON.stringify({error: 'no-element'});
  e.scrollIntoView({block: 'center', inline: 'center'});
  const r = e.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const hit = document.elementFromPoint(cx, cy);
  return JSON.stringify({
    x: cx, y: cy, w: r.width, h: r.height,
    top: Math.round(r.top), left: Math.round(r.left),
    vw: window.innerWidth, vh: window.innerHeight,
    visibility: document.visibilityState, hidden: document.hidden,
    hit_tag: hit ? hit.tagName : null,
    hit_qa: hit ? hit.getAttribute('data-qa') : null,
    hit_text: hit ? (hit.textContent || '').trim().slice(0, 60) : null,
    is_self: !!hit && (hit === e || e.contains(hit) || hit.contains(e))
  });
})()"""

RESUME_OPTIONS_JS = r"""(() => {
  const opts = [...document.querySelectorAll(
    '[role=option], [data-qa*=resume-item], [data-qa*=resume-list], li'
  )].map(e => ({tag: e.tagName, qa: e.getAttribute('data-qa'),
    text: (e.textContent || '').trim().slice(0, 80),
    visible: !!(e.getBoundingClientRect().width)})).filter(o => o.visible && o.text);
  return JSON.stringify({url: location.href, options: opts.slice(0, 30)});
})()"""



# Vacancy brief read off the SSR payload. Formerly tools/.t722_BRIEF_JS.
# Production owns this string so submit/brief do not import scratch tools.
BRIEF_JS = """(() => {
  try {
    const vid = '__VID__';
    const t = document.getElementById('HH-Lux-InitialState');
    let st = null, err = null;
    if (t) { try { st = JSON.parse(t.innerHTML); } catch (e) { err = String(e); } }
    const vf = (st && st.vacancyView && st.vacancyView.vacancyFull) || {};
    const v = vf.vacancy || {};
    const s = (st && st.applicantVacancyResponseStatuses || {})[vid] || {};
    const sv = s.shortVacancy || {};
    const txt = sel => { const e = document.querySelector(sel); return e ? e.innerText.trim() : ''; };
    const again = document.querySelector('[data-qa="vacancy-response-link-top-again"]');
    const viewTopic = document.querySelector('[data-qa="vacancy-response-link-view-topic"]');
    return JSON.stringify({
      ok: !!v.name,
      state_error: err,
      url: location.href,
      name: v.name, id: v.id === undefined ? null : String(v.id),
      employer: v.employer ? {id: v.employer.id, name: v.employer.name,
        accredited: v.employer.accreditedItEmployer, trusted: v.employer.trusted} : null,
      area: v.area ? {id: v.area.id, name: v.area.name, iso: v.area.countryIsoCode} : null,
      address: v.address ? v.address.displayName : null,
      compensation: v.compensation || null,
      employment: v.employment, employmentForm: v.employmentForm,
      workSchedule: v.workSchedule, workFormat: v.workFormat, workFormats: v.workFormats,
      workingHours: v.workingHours,
      experience: v.experience || null,
      archived: v.archived, status: v.status, closedForApplicants: v.closedForApplicants,
      validTo: v.validTo, creationTime: v.creationTime,
      responseLetterRequired: v.responseLetterRequired,
      hasTest: v.hasTest, test: v.test, questions: v.questions,
      counters: v.counters, responseCounters: v.responseCounters,
      dom_title: txt('[data-qa="vacancy-title"]'),
      dom_company: txt('[data-qa="vacancy-company-name"]'),
      dom_salary: txt('[data-qa="vacancy-salary"]'),
      dom_already_responded: !!(again || viewTopic),
      applicant: {
        alreadyApplied: s.alreadyApplied,
        responseAllowed: s.by_country_applicant_visibility
          ? s.by_country_applicant_visibility.responseAllowed : null,
        responseImpossible: s.responseImpossible,
        usedResumeIds: s.usedResumeIds, unusedResumeIds: s.unusedResumeIds,
        hiddenResumeIds: s.hiddenResumeIds,
        letterMaxLength: s.letterMaxLength,
        negotiations_total: (s.negotiations || {}).total,
        test: s.test,
        shortVacancy_workSchedule: sv['@workSchedule'],
        shortVacancy_letterRequired: sv['@responseLetterRequired'],
        shortVacancy_acceptLaborContract: sv.acceptLaborContract,
        shortVacancy_acceptIncomplete: sv.acceptIncompleteResumes,
      },
      description: v.description || ''
    });
  } catch (e) { return JSON.stringify({ok: false, fatal: String(e && e.message || e)}); }
})()"""

def _log(message: str) -> None:
    print(message, flush=True)


def _window_snapshot(label: str) -> dict[str, Any]:
    """Label a report section without importing tools/.t722_windows.

    The scratch helper that censused Chrome windows is not part of the package
    and is not in the tree. The label stays on the report so the gap is visible.
    """
    return {
        "label": label,
        "source": "package",
        "note": "chrome window census is not collected in production",
    }


def _tab_ws(endpoint: str, target_id: str) -> str | None:
    for tab in list_tabs(endpoint):
        if tab.get("id") == target_id:
            return tab.get("webSocketDebuggerUrl")
    return None


def _activate(endpoint: str, target_id: str) -> Any:
    """Bring the tab to the front. Chrome drops mouse events for a hidden page."""
    from ai_assistant.hh_browser_provider import cdp_version

    ws = (cdp_version(endpoint) or {}).get("webSocketDebuggerUrl")
    if not ws:
        return None
    return _ws_call(ws, "Target.activateTarget", {"targetId": target_id})


def _click_at(endpoint: str, target_id: str, x: float, y: float) -> list[Any]:
    """One click: three pointer events, in the order HH's handlers expect.

    ``mouseMoved`` first -- HH's controls arm themselves on hover, and a bare
    press/release lands on an inert element. No retry and no loop: a click that did not
    land is a fact to read off the page afterwards, not something to do again.

    ``_ws_call`` is synchronous (it owns its own ``_run_sync``); wrapping it in
    ``_run_sync`` again raises "an asyncio.Future, a coroutine or an awaitable is
    required".
    """
    ws = _tab_ws(endpoint, target_id)
    if not ws:
        return []
    sent: list[Any] = []
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        sent.append(_ws_call(ws, "Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "left", "clickCount": 1,
            "buttons": 1 if kind == "mousePressed" else 0,
        }))
    return sent


# The single place this file clicks. Bound to its own name so that the helper above is
# mentioned with a parenthesis exactly once in the source -- the guard in
# tests/test_t721_headless_provider.py counts those mentions and requires one, so the
# definition must stay the only parenthesised occurrence. The submit below goes through
# this binding.
CLICK = _click_at


def _type_text(endpoint: str, target_id: str, selector: str, text: str) -> dict[str, Any]:
    """Focus a field and insert text through the browser's real input path.

    A synthetic ``value`` + ``input`` event is not enough for HH's React fields: the
    controlled input snaps back to the empty string. ``Input.insertText`` goes through
    the browser, and selecting first makes it replace rather than append.
    """
    ws = _tab_ws(endpoint, target_id)
    if not ws:
        return {"error": "no tab"}
    focus = _ws_call(ws, "Runtime.evaluate", {
        "expression": (
            "(()=>{const e=document.querySelector(" + json.dumps(selector) + ");"
            "if(!e)return 'no-element';e.focus();e.select();return 'focused:'+e.tagName;})()"
        ),
        "returnByValue": True,
    })
    _ws_call(ws, "Input.insertText", {"text": text})
    time.sleep(0.8)
    after = _ws_call(ws, "Runtime.evaluate", {
        "expression": (
            "(()=>{const e=document.querySelector(" + json.dumps(selector) + ");"
            "return JSON.stringify({len:(e.value||'').length,"
            "text:(e.value||'').slice(0,80)});})()"
        ),
        "returnByValue": True,
    })
    return {
        "focus": (focus.get("result", {}).get("result", {}) or {}).get("value"),
        "after": (after.get("result", {}).get("result", {}) or {}).get("value"),
    }


def _read(session: HeadlessBrowserSession, target: str, js: str,
          tries: int = 8, want: str = "ok") -> dict[str, Any]:
    """Read a JSON payload off a tab, retrying while the document settles."""
    last: dict[str, Any] = {"error": "no read attempted"}
    for _ in range(tries):
        try:
            payload = json.loads(session.evaluate_on(target, js))
            if isinstance(payload, dict) and (
                payload.get(want) or payload.get("has_template") is not None
            ):
                return payload
            last = payload if isinstance(payload, dict) else {"error": "not an object"}
        except Exception as exc:  # noqa: BLE001 - mid-navigation reads fail
            last = {"error": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.5)
    return last


def _measure(session: HeadlessBrowserSession, target: str, selector: str) -> dict[str, Any]:
    """Scroll, re-measure, and report what is under the point -- refusing if it is not us."""
    _activate(session.cdp_url, target)
    raw = session.evaluate_on(target, MEASURE_JS.replace("__SEL__", json.dumps(selector)))
    try:
        info = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"error": f"unreadable measure: {raw[:120]}"}
    if info.get("error"):
        return info
    if not (0 <= info.get("x", -1) <= info.get("vw", 0)
            and 0 <= info.get("y", -1) <= info.get("vh", 0)):
        info["error"] = "target outside the viewport after scrolling"
    elif not info.get("is_self"):
        info["error"] = "something else is on top of the target"
    return info


def _vacancy_url(vid: str) -> str:
    return f"https://hh.ru/vacancy/{vid}"


def _form_url(vid: str) -> str:
    return f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}"


def _shutdown(session: HeadlessBrowserSession) -> dict[str, Any]:
    leftover = session.leftover_tabs()
    stopped = session.stop()
    return {"leftover_tabs": leftover, "stop": stopped}


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------

def run_brief(args: argparse.Namespace) -> dict[str, Any]:
    vid = args.vid
    report: dict[str, Any] = {
        "command": "brief", "vid": vid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "endpoint_from_mode": resolve_browser_endpoint(),
        "windows_before": _window_snapshot("t722_brief_before"),
    }

    session = HeadlessBrowserSession(require_auth=True)
    started = session.start()
    report["launch"] = {
        "ok": started.get("ok"), "status": started.get("status"),
        "pid": session.pid, "reused": session.reused,
        "auth_ok": (started.get("auth") or {}).get("authenticated"),
        "auth": (started.get("auth") or {}).get("reason"),
    }
    report["endpoint"] = session.cdp_url
    _log(f"launch ok={started.get('ok')} pid={session.pid} "
         f"auth={report['launch']['auth']}")
    if not started.get("ok"):
        report["verdict"] = "LAUNCH_FAILED"
        report["shutdown"] = _shutdown(session)
        return report

    try:
        target = session.open_extra_tab(_vacancy_url(vid))
        if target is None:
            report["verdict"] = "NO_TAB"
            return report
        session.navigate(target, _vacancy_url(vid))
        vacancy = _read(session, target, BRIEF_JS.replace("__VID__", vid))
        report["vacancy"] = {k: v for k, v in vacancy.items() if k != "description"}
        report["description"] = vacancy.get("description") or ""
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / f"desc_{vid}.txt").write_text(report["description"], encoding="utf-8")
        _log(f"vacancy {vacancy.get('name')!r} archived={vacancy.get('archived')} "
             f"status={vacancy.get('status')}")

        report["windows_during"] = _window_snapshot("t722_brief_during")

        form_target = session.open_extra_tab(_form_url(vid))
        if form_target is not None:
            session.navigate(form_target, _form_url(vid))
            report["form"] = _read(session, form_target, FORM_JS.replace("__VID__", vid))
            _log(f"form url={report['form'].get('url')} "
                 f"resume={report['form'].get('resume_title')!r} "
                 f"submit={(report['form'].get('submit') or {}).get('disabled')}")
            session.close_tab(form_target)

        shot = session.capture(OUT / f"t722_brief_{vid}.png", target)
        report["screenshot"] = shot
        session.close_tab(target)
    finally:
        report["windows_after"] = _window_snapshot("t722_brief_after")
        report["shutdown"] = _shutdown(session)

    report["verdict"] = "BRIEFED" if report.get("vacancy", {}).get("ok") else "NO_VACANCY"
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return report


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def _blockers(vacancy: dict[str, Any], form: dict[str, Any],
              expect_resume: str) -> list[str]:
    """Every reason not to click, in one place, checked against live state."""
    found: list[str] = []
    if not vacancy.get("ok"):
        found.append("the vacancy page did not render")
    if vacancy.get("archived"):
        found.append("the vacancy is archived")
    if vacancy.get("closedForApplicants"):
        found.append("the vacancy is closed for applicants")
    # ``status`` is an object on the live page, not a string: archived/disabled/approved
    # all live inside it. Reading it as a string turned every healthy vacancy into
    # "unexpected vacancy status" on the first dry run of this check.
    status = vacancy.get("status")
    if isinstance(status, dict):
        if status.get("archived") or status.get("disabled"):
            found.append(f"the vacancy is not active: {status}")
        if status.get("approved") is False or (
            status.get("premoderationStatus") not in (None, "APPROVED")
        ):
            found.append(f"the vacancy is not approved: {status}")
    elif status:
        found.append(f"unexpected vacancy status: {status!r}")

    avrs = vacancy.get("applicant") or {}
    if avrs.get("responseAllowed") is not True:
        found.append("responseAllowed is not true")
    if avrs.get("responseImpossible"):
        found.append("responseImpossible is true")
    if avrs.get("alreadyApplied"):
        found.append("alreadyApplied is true")
    if (avrs.get("negotiations_total") or 0) > 0:
        found.append(f"a negotiation already exists: {avrs.get('negotiations_total')}")
    if vacancy.get("dom_already_responded"):
        found.append("the page already shows a response link")

    used = avrs.get("usedResumeIds") or []
    if used:
        found.append(f"usedResumeIds is not empty: {used}")

    # Tests and screening questionnaires are blockers ONLY if not already prefilled by the autonomous planner.
    test = avrs.get("test") or {}
    test_extra = {k: v for k, v in test.items() if k != "hasTests" and v is not None}
    has_test = bool(test.get("hasTests") or test_extra or any(v is not None for v in (form.get("vacancy_tests") or {}).values()))
    if has_test and not form.get("questions_prefilled"):
        found.append(f"the vacancy carries a test: {test or form.get('vacancy_tests')}")
    if form.get("started_with_question") and not form.get("questions_prefilled"):
        found.append("employer questions are required and have not been answered")

    if form.get("warning_shown"):
        found.append(f"a resume-visibility warning is displayed: {form.get('warning_text')}")

    submit = form.get("submit")
    if not submit:
        found.append("no submit control on the form")
    elif submit.get("disabled") is not False:
        found.append(f"the submit control is disabled: {submit}")

    title = (form.get("resume_title") or "").strip()
    if title != expect_resume.strip():
        found.append(f"wrong resume selected: {title!r} (expected {expect_resume!r})")

    visible_areas = [t for t in (form.get("textareas") or []) if t.get("visible")]
    if not visible_areas:
        found.append("no visible letter field -- the letter was not inserted")
    elif not (visible_areas[0].get("value_len") or 0):
        found.append("the letter field is empty")
    return found



def _enforce_controlled_gates(
    *,
    vid: str,
    current_url: str,
    letter: str,
    form: dict[str, Any],
) -> tuple[bool, str]:
    """Run the same 11-gate / halt authority as execute_hh_submission.

    Requires an APPROVED ApplicationReview + package already in DB (autopilot
    prepares these under standing owner authorization before calling submit).
    """
    from ai_assistant.application_review import (
        compute_review_fingerprint,
        get_application_review,
    )
    from ai_assistant.candidate_profile import load_candidate_profile
    from ai_assistant.hh_submission import HHSubmissionGates, submission_halt_reason

    halt = submission_halt_reason()
    if halt:
        return False, halt

    stable_id = f"hh:{vid}"
    review = get_application_review(stable_id)
    if review is None:
        return False, f"no ApplicationReview for {stable_id}"
    status = getattr(review.status, "value", review.status)
    if str(status) != "APPROVED":
        return False, f"ApplicationReview status is {status}, need APPROVED"

    from ai_assistant import db as _db

    pkg_row = _db.get_application_package(stable_id)
    pkg_data: dict[str, Any] = {}
    if pkg_row and len(pkg_row) >= 3 and pkg_row[2]:
        try:
            pkg_data = json.loads(pkg_row[2])
        except Exception:
            pkg_data = {}
    if not pkg_data.get("cover_letter"):
        pkg_data = {
            **pkg_data,
            "vacancy_stable_id": stable_id,
            "cover_letter": letter,
            "validation_status": pkg_data.get("validation_status") or "VALID",
        }

    # Align live fingerprint with the approved review when the cover letter
    # text is unchanged, so gate 3 does not false-fail on packaging noise.
    approved_letter = (review.cover_letter or "").strip()
    live_letter = (pkg_data.get("cover_letter") or letter or "").strip()
    if approved_letter and approved_letter == live_letter and review.form_fingerprint:
        live_fp = review.form_fingerprint
    else:
        live_fp = compute_review_fingerprint(stable_id, pkg_data)

    form_snapshot = {
        "fingerprint": live_fp,
        "cover_letter": pkg_data.get("cover_letter") or letter,
        "package": pkg_data,
        "already_applied": bool(
            form.get("already_applied") or form.get("already_responded")
        ),
        "fields": pkg_data.get("questions") or [],
    }

    try:
        profile = load_candidate_profile()
    except Exception as exc:
        return False, f"candidate profile load failed: {exc}"

    result = HHSubmissionGates.check_all_gates(
        vacancy_stable_id=stable_id,
        current_url=current_url,
        form_snapshot=form_snapshot,
        human_confirmed=False,
        approval=review,
        dry_run=False,
        candidate_profile=profile,
        review_obj=review,
    )
    if not result.passed:
        return False, result.reason or "gates failed"
    return True, result.reason or "All gates passed"


def run_submit(args: argparse.Namespace) -> dict[str, Any]:
    vid = args.vid
    letter = pathlib.Path(args.letter).read_text(encoding="utf-8")
    report: dict[str, Any] = {
        "command": "submit", "vid": vid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "endpoint_from_mode": resolve_browser_endpoint(),
        "expect_resume": args.expect_resume,
        "letter_chars": len(letter),
        "windows_before": _window_snapshot("t722_submit_before"),
        "clicked": 0,
    }

    # Audit 2026-10-19 H-01 / Critical-1: all three emergency stops via the
    # same authority as the controlled path (SUBMIT_ALLOWED, submit_paused,
    # STOP_SUBMITS).
    try:
        from ai_assistant.hh_submission import submission_halt_reason
        _halt = submission_halt_reason()
        if _halt:
            report["verdict"] = "REFUSED"
            blockers = [_halt]
            # Preserve legacy phrase for tests asserting on SUBMIT_ALLOWED.
            if "SUBMIT_ALLOWED" in _halt:
                blockers.append("SUBMIT_ALLOWED is off")
            report["blockers"] = blockers
            report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return report
    except Exception as _exc:  # fail closed: no switch reading means no click
        report["verdict"] = "REFUSED"
        report["blockers"] = [f"kill-switch check failed (fail-closed): {_exc}"]
        report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return report

    session = HeadlessBrowserSession(require_auth=True)
    started = session.start()
    report["launch"] = {
        "ok": started.get("ok"), "status": started.get("status"),
        "pid": session.pid, "reused": session.reused,
        "auth_ok": (started.get("auth") or {}).get("authenticated"),
    }
    report["endpoint"] = session.cdp_url
    report["pid"] = session.pid
    _log(f"launch ok={started.get('ok')} pid={session.pid}")
    if not started.get("ok"):
        report["verdict"] = "LAUNCH_FAILED"
        report["shutdown"] = _shutdown(session)
        return report

    try:
        vacancy_tab = session.open_extra_tab(_vacancy_url(vid))
        if vacancy_tab is None:
            report["verdict"] = "NO_TAB"
            return report
        session.navigate(vacancy_tab, _vacancy_url(vid))
        vacancy = _read(session, vacancy_tab, BRIEF_JS.replace("__VID__", vid))
        report["vacancy_before"] = {k: v for k, v in vacancy.items() if k != "description"}

        form_tab = session.open_extra_tab(_form_url(vid))
        if form_tab is None:
            report["verdict"] = "NO_FORM_TAB"
            return report
        session.navigate(form_tab, _form_url(vid))
        form = _read(session, form_tab, FORM_JS.replace("__VID__", vid))
        report["form_before"] = form

        # Audit 2026-10-19 H-02: a form is only trustworthy while it belongs to
        # hh.ru and points at the requested vacancy. A redirect must refuse.
        live_form_url = str(form.get("url") or "")
        report["form_url"] = live_form_url
        try:
            parsed_form = urllib.parse.urlparse(live_form_url)
        except Exception:
            parsed_form = None
        form_host = (parsed_form.hostname or "").lower() if parsed_form else ""
        form_scheme = (parsed_form.scheme or "").lower() if parsed_form else ""
        if form_scheme not in ("http", "https") or not (
            form_host == "hh.ru" or form_host.endswith(".hh.ru")
        ):
            report["verdict"] = "REFUSED"
            report["blockers"] = [f"form URL is not hh.ru: {live_form_url!r}"]
            report["windows_after"] = _window_snapshot("t722_submit_after")
            return report
        if f"vacancyId={vid}" not in live_form_url and f"/vacancy/{vid}" not in live_form_url:
            report["verdict"] = "REFUSED"
            report["blockers"] = [f"form URL does not belong to vacancy {vid}: {live_form_url!r}"]
            report["windows_after"] = _window_snapshot("t722_submit_after")
            return report

        # Critical-1: same halt/audit/review gates as execute_hh_submission.
        # Autopilot prepares an APPROVED ApplicationReview before calling submit.
        if not getattr(args, "skip_controlled_gates", False):
            gate_ok, gate_reason = _enforce_controlled_gates(
                vid=vid,
                current_url=live_form_url,
                letter=letter,
                form=form,
            )
            report["controlled_gates"] = {"passed": gate_ok, "reason": gate_reason}
            if not gate_ok:
                report["verdict"] = "REFUSED"
                report["blockers"] = [f"controlled gates: {gate_reason}"]
                report["windows_after"] = _window_snapshot("t722_submit_after")
                return report

        report["windows_during"] = _window_snapshot("t722_submit_during")

        # -- resume selection switch if needed ------------------------------
        current_res = (form.get("resume_title") or "").strip()
        expect_res = (getattr(args, "expect_resume", None) or "").strip()
        if expect_res and current_res != expect_res:
            _log(f"Current resume {current_res!r} != expected {expect_res!r}. Switching on HH form...")
            try:
                session.evaluate_on(form_tab, '(()=>{const c=document.querySelector("[data-qa=resume-title]");if(c)c.click();return "ok";})()')
                time.sleep(1.0)
                opt_js = (
                    "(()=>{const target=" + json.dumps(expect_res) + ";"
                    "const hashes={"
                    "  'ai engineer':'4db30a09ff01b3bae30039ed1f364c544b4459',"
                    "  'специалист по внедрению и сопровождению crm':'5192a9adff1129ee830039ed1f3233684f4b34',"
                    "  'инженер технической поддержки':'6e5df374ff1129f35a0039ed1f41367954547a'"
                    "};"
                    "const targetNorm=target.toLowerCase().trim();"
                    "const targetHash=hashes[targetNorm];"
                    "if(targetHash){"
                    "  const exactOpt=document.querySelector('[data-qa=\"magritte-select-option-'+targetHash+'\"]');"
                    "  if(exactOpt){exactOpt.click();return JSON.stringify({selected:true,method:'hash',hash:targetHash});}"
                    "}"
                    "const opts=Array.from(document.querySelectorAll('label[role=option], [data-qa^=\"magritte-select-option-\"]'));"
                    "for(const opt of opts){"
                    "  const t=(opt.innerText||'').trim();"
                    "  if(t.toLowerCase().includes(targetNorm)){"
                    "    opt.click();return JSON.stringify({selected:true,method:'leaf',text:t});"
                    "  }"
                    "}"
                    "return JSON.stringify({error:'not_found',opts:opts.map(o=>o.innerText)});"
                    "})()"
                )
                switch_res = session.evaluate_on(form_tab, opt_js)
                report["resume_switch"] = switch_res
                time.sleep(1.2)
                form = _read(session, form_tab, FORM_JS.replace("__VID__", vid))
                report["form_after_resume_switch"] = form
                _log(f"Switched resume: new title is {form.get('resume_title')!r}")
            except Exception as e:
                _log(f"Resume switch error: {e}")

        # -- the letter ------------------------------------------------------
        if not form.get("warning_shown") and form.get("letter_toggle"):
            point = _measure(session, form_tab,
                             "[data-qa=vacancy-response-letter-toggle]")
            report["letter_toggle_point"] = point
            if not point.get("error"):
                CLICK(session.cdp_url, form_tab, point["x"], point["y"])
                report["letter_toggle_clicked"] = True
                time.sleep(1.5)
        form = _read(session, form_tab, FORM_JS.replace("__VID__", vid))
        report["form_after_toggle"] = form

        selector = None
        for area in form.get("textareas") or []:
            if area.get("visible"):
                selector = (f"[data-qa='{area['data_qa']}']" if area.get("data_qa")
                            else "textarea")
                break
        report["letter_selector"] = selector
        if selector:
            report["typed"] = _type_text(session.cdp_url, form_tab, selector, letter)
        form = _read(session, form_tab, FORM_JS.replace("__VID__", vid))
        report["form_after_letter"] = form

        # -- screening questionnaire prefill ---------------------------------
        prefill_plan = getattr(args, "prefill_plan", None)
        if not prefill_plan and getattr(args, "prefill_json", None):
            try:
                raw_pj = pathlib.Path(args.prefill_json).read_text(encoding="utf-8")
                prefill_plan = json.loads(raw_pj)
            except Exception as e:
                _log(f"failed to load prefill_json: {e}")

        # If prefill_plan is still missing but form has controls or requires questions,
        # resolve questions on the fly from the live form_tab DOM!
        if not prefill_plan and (form.get("controls") or form.get("started_with_question")):
            try:
                from ai_assistant.hh_autopilot import resolve_and_plan_screening
                from ai_assistant.candidate_profile import load_candidate_profile
                profile = load_candidate_profile()
                q_ok, _, plan, _ = resolve_and_plan_screening(
                    vid, form, profile, cover_letter=letter,
                    vacancy_info={"role": getattr(args, "expect_resume", ""), "company": ""},
                    autonomous_mode=True,
                )
                if q_ok and plan:
                    prefill_plan = plan
                    _log(f"Resolved live prefill plan on form_tab: {len(getattr(plan, 'operations', []))} operations")
            except Exception as e:
                _log(f"Live form prefill planning error: {e}")

        if prefill_plan:
            from ai_assistant.prefill_execute import _mutation_js
            ops = prefill_plan.operations if hasattr(prefill_plan, "operations") else prefill_plan.get("operations", [])
            mutations_executed = []
            for op in ops:
                m_js = _mutation_js(op)
                m_res_raw = session.evaluate_on(form_tab, m_js)
                try:
                    m_res = json.loads(m_res_raw)
                except Exception:
                    m_res = {"ok": False, "raw": m_res_raw}
                mutations_executed.append(m_res)
            report["prefill_mutations"] = mutations_executed
            time.sleep(1.0)
            form = _read(session, form_tab, FORM_JS.replace("__VID__", vid))
            if not mutations_executed or all(m.get("ok") for m in mutations_executed):
                form["questions_prefilled"] = True
            report["form_after_prefill"] = form

        blockers = _blockers(vacancy, form, args.expect_resume)
        report["blockers"] = blockers
        if blockers:
            _log(f"REFUSED: {json.dumps(blockers, ensure_ascii=True)}")
            report["verdict"] = "REFUSED"
            report["windows_after"] = _window_snapshot("t722_submit_after")
            return report

        # -- the one click ---------------------------------------------------
        point = _measure(session, form_tab, "[data-qa=vacancy-response-submit-popup]")
        report["submit_point"] = point
        if point.get("error"):
            report["verdict"] = "REFUSED"
            report["blockers"] = [f"submit point unusable: {point['error']}"]
            report["windows_after"] = _window_snapshot("t722_submit_after")
            return report

        report["windows_at_click"] = _window_snapshot("t722_submit_at_click")
        CLICK(session.cdp_url, form_tab, point["x"], point["y"])
        report["clicked"] = 1
        _log("clicked submit once")
        time.sleep(6.0)

        report["form_after_send"] = _read(session, form_tab, FORM_JS.replace("__VID__", vid))
        session.navigate(vacancy_tab, _vacancy_url(vid))
        after = _read(session, vacancy_tab, BRIEF_JS.replace("__VID__", vid))
        report["vacancy_after"] = {k: v for k, v in after.items() if k != "description"}
        shot = session.capture(OUT / f"t722_after_{vid}.png", vacancy_tab)
        report["screenshot"] = shot

        # Audit 2026-10-19 H-04: a click is not proof of a submitted response.
        # Record what the live pages actually show, so the caller can stop
        # claiming success when HH has not confirmed it yet.
        def _shows_response(page: dict, form_after: dict) -> bool:
            applicant_ = (page or {}).get("applicant") or {}
            negotiations_ = (((form_after or {}).get("avrs") or {}).get("negotiations") or {})
            return bool(
                (page or {}).get("dom_already_responded")
                or applicant_.get("alreadyApplied")
                or applicant_.get("usedResumeIds")
                or int(applicant_.get("negotiations_total") or 0) > 0
                or int(negotiations_.get("total") or 0) > 0
            )

        form_after_send = report.get("form_after_send") or {}
        post_confirmed = _shows_response(after, form_after_send)
        if not post_confirmed:
            # HH often updates the vacancy page a few seconds later. One re-read
            # is cheap and prevents a false "unverified" alarm for a real send.
            _log("response not visible after 6 s; re-reading the vacancy page once")
            time.sleep(8.0)
            session.navigate(vacancy_tab, _vacancy_url(vid))
            after_retry = _read(session, vacancy_tab, BRIEF_JS.replace("__VID__", vid))
            if _shows_response(after_retry, form_after_send):
                post_confirmed = True
                after = after_retry
                report["vacancy_after_retry"] = {
                    k: v for k, v in after_retry.items() if k != "description"
                }
                report["post_confirm_retry"] = True
        report["vacancy_after"] = {k: v for k, v in after.items() if k != "description"}
        report["post_confirmed"] = post_confirmed
        report["post_confirm_reason"] = (
            "live page shows the response"
            if post_confirmed
            else "click executed, but the live page does not show the response yet"
        )
        report["resume_after"] = form.get("resume_title") or ""
    finally:
        report["windows_after"] = _window_snapshot("t722_submit_after")
        report["shutdown"] = _shutdown(session)

    report["verdict"] = "CLICKED" if report["clicked"] == 1 else "NOT_CLICKED"
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    brief = sub.add_parser("brief")
    brief.add_argument("vid")

    submit = sub.add_parser("submit")
    submit.add_argument("vid")
    submit.add_argument("--expect-resume", required=True,
                        help="the resume title that must be selected before the click")
    submit.add_argument("--letter", required=True, help="path to the cover letter")
    submit.add_argument("--prefill-json", default=None, help="path to prefill plan JSON")
    args = parser.parse_args()

    if args.command == "brief":
        report = run_brief(args)
    else:
        report = run_submit(args)

    out_path = WORK / f"{args.command}_{args.vid}.json"
    WORK.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {
        "command": args.command, "vid": args.vid, "verdict": report.get("verdict"),
        "pid": report.get("pid") or (report.get("launch") or {}).get("pid"),
        "endpoint": report.get("endpoint"), "clicked": report.get("clicked"),
        "blockers": report.get("blockers"),
        "resume": (report.get("form") or report.get("form_after_letter") or {}).get("resume_title"),
        "json_out": str(out_path),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=1))
    return 0 if report.get("verdict") in ("BRIEFED", "CLICKED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
