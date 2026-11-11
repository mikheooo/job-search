"""T7.21 E2E headless dry run: the whole safe path, without BrowserOS.

What this proves, in order (T7.21 section 9):

1. the runner starts its *own* headless Chrome (no BrowserOS, no visible window);
2. the authentication gate answers from real page evidence;
3. HH search runs through the headless endpoint;
4. at least three vacancies are opened and read (DOM + the SSR initial state);
5. one application form is opened **read-only** - nothing is typed, nothing is
   clicked, no POST is made;
6. the resume / questions / tests the form reports are recorded;
7. a screenshot is taken;
8. every tab is closed, then the browser, and port 9111 is freed.

It reuses the project's own readers - ``.t76_hh_search`` for the query set - and
the same CDP transport (``Runtime.evaluate``), addressed by target id.

Usage:
    python tools/.t721_headless_dryrun.py --json-out artifacts/headless-poc/t721_dryrun.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT_DIR = REPO / "artifacts" / "headless-poc"

# The SSR initial state keeps its payload in a DocumentFragment, so
# ``template.textContent`` is "" and JSON.parse throws - read innerHTML (T7.19).
# The vacancy object lives at vacancyView.vacancyFull.vacancy; vacancyView.vacancy
# does not exist (measured on a live page 2026-09-28).
_VACANCY_JS = """(() => {
  try {
    const t = document.getElementById('HH-Lux-InitialState');
    let state = null, err = null;
    if (t) { try { state = JSON.parse(t.innerHTML); } catch (e) { err = String(e); } }
    const v = state && state.vacancyView && state.vacancyView.vacancyFull
      ? state.vacancyView.vacancyFull.vacancy : null;
    const txt = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : ''; };
    const again = document.querySelector('[data-qa="vacancy-response-link-top-again"]');
    const viewTopic = document.querySelector('[data-qa="vacancy-response-link-view-topic"]');
    return JSON.stringify({
      url: location.href,
      dom_title: txt('[data-qa="vacancy-title"]'),
      dom_company: txt('[data-qa="vacancy-company-name"]'),
      dom_salary: txt('[data-qa="vacancy-salary"]'),
      respond_button: !!document.querySelector('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"]'),
      already_responded_dom: !!(again || viewTopic),
      state_error: err,
      ssr_name: v ? v.name : null,
      ssr_id: v ? String(v.id) : null,
      ssr_employment: v ? v.employment : null,
      ssr_compensation: v ? (v.compensation || null) : null,
      ssr_area: v && v.area ? v.area.name : null,
      response_statuses: state ? (state.applicantVacancyResponseStatuses || null) : null,
      vacancy_tests: state ? (state.vacancyTests || null) : null,
      counters: state ? (state.counters || null) : null
    });
  } catch (e) { return JSON.stringify({error: String(e && e.message || e)}); }
})()"""

_FORM_JS = """(() => {
  try {
    const t = document.getElementById('HH-Lux-InitialState');
    let state = null, err = null;
    if (t) { try { state = JSON.parse(t.innerHTML); } catch (e) { err = String(e); } }
    const form = document.querySelector('form[method=POST], form[method="POST"]');
    const submit = document.querySelector('[data-qa="vacancy-response-submit-popup"]');
    const letterToggle = document.querySelector('[data-qa="vacancy-response-letter-toggle"]');
    return JSON.stringify({
      url: location.href,
      state_error: err,
      post_forms: document.querySelectorAll('form[method=POST], form[method="POST"]').length,
      submit_buttons_in_form: form ? form.querySelectorAll('[type=submit]').length : 0,
      textareas: document.querySelectorAll('textarea').length,
      textarea_qa: Array.from(document.querySelectorAll('textarea')).map(x => x.getAttribute('data-qa')),
      submit_present: !!submit,
      submit_disabled: submit ? !!submit.disabled : null,
      letter_toggle_present: !!letterToggle,
      resume_titles: Array.from(document.querySelectorAll('[data-qa="resume-title"]'))
                        .map(e => e.innerText.trim()).filter(Boolean),
      vacancy_tests: state ? (state.vacancyTests || null) : null,
      response_statuses: state ? (state.applicantVacancyResponseStatuses || null) : null,
      question_inputs: document.querySelectorAll('[data-qa*="question"], input[name*="question"]').length
    });
  } catch (e) { return JSON.stringify({error: String(e && e.message || e)}); }
})()"""


def _log(message: str) -> None:
    print(f"[t721] {message}", file=sys.stderr, flush=True)


def _read(session, target_id: str, js: str, attempts: int = 10,
          pause: float = 0.7) -> dict[str, Any]:
    """Read a page through the session, retrying while the tab is mid-navigation."""
    last: dict[str, Any] = {}
    for _ in range(attempts):
        try:
            last = json.loads(session.evaluate_on(target_id, js))
            if last.get("ssr_name") or last.get("dom_title") or last.get("post_forms"):
                return last
        except Exception as exc:  # noqa: BLE001 - a mid-navigation read is expected
            last = {"error": f"{type(exc).__name__}: {exc}"}
        time.sleep(pause)
    return last


def _load_tool(name: str, filename: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The whole dry run, with the browser closed no matter how it ends.

    The first live attempt of this tool raised inside the vacancy loop and left
    19 Chrome processes and 2.2 GB behind - the very leak T7.21 section 6 is
    about. A runner that only cleans up on the happy path is not a runner.
    """
    from ai_assistant import hh_browser_provider as bp

    session = bp.HeadlessBrowserSession(require_auth=args.require_auth)
    try:
        return _run_inner(args, session)
    finally:
        if session.status == "RUNNING":
            _log("closing tabs and the browser (finally)")
            session.close_all_tabs()
            session.stop()


def _run_inner(args: argparse.Namespace, session) -> dict[str, Any]:
    from ai_assistant import hh_browser_provider as bp

    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": bp.browser_mode(),
        "endpoint": session.cdp_url,
        "launch": None,
        "auth": None,
        "search": None,
        "vacancies": [],
        "form": None,
        "screenshot": None,
        "tabs_closed": 0,
        "shutdown": None,
        "port_freed": None,
        "nothing_sent": None,
        "verdict": "FIX",
    }
    _log(f"mode={report['mode']} endpoint={session.cdp_url} starting browser")

    started = session.start()
    report["auth"] = started.get("auth")
    report["launch"] = {
        "ok": started.get("ok"), "status": started.get("status"),
        "message": started.get("message", ""),
        "started": session.started, "reused": session.reused, "pid": session.pid,
        "launch_error": (started.get("launch") or {}).get("error"),
    }
    _log(f"launch ok={started.get('ok')} status={started.get('status')} pid={session.pid}")
    if not started.get("ok"):
        report["verdict"] = started.get("status") or "LAUNCH_FAILED"
        report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return report

    # --- search ------------------------------------------------------------
    t76 = _load_tool("t76_search", ".t76_hh_search.py")
    queries = args.queries or ["IT Support"]
    t76.DIRECTION_QUERIES["t721_smoke"] = queries
    _log(f"searching: {queries}")
    search = t76.run_search(session.cdp_url, ["t721_smoke"], pause=1.0, verbose=False)
    items = search.get("items") or []
    report["search"] = {
        "queries": queries,
        "unique_vacancies": len(items),
        "per_query": search.get("per_query"),
    }

    # --- vacancy reads -----------------------------------------------------
    wanted = [i for i in items if i.get("vacancy_id")][: args.vacancies]
    for item in wanted:
        vid = item["vacancy_id"]
        target = session.open_extra_tab(f"https://hh.ru/vacancy/{vid}")
        if target is None:
            report["vacancies"].append({"vacancy_id": vid, "error": "could not open a tab"})
            continue
        session.navigate(target, f"https://hh.ru/vacancy/{vid}")
        _log(f"reading vacancy {vid}")
        rec = _read(session, target, _VACANCY_JS)
        rec.update({"vacancy_id": vid, "search_title": item.get("title"),
                    "search_employer": item.get("employer")})
        _log(f"  vacancy {vid}: ssr={rec.get('ssr_name')!r} dom={rec.get('dom_title')!r} "
             f"err={rec.get('error')!r}")
        report["vacancies"].append(rec)
        if report["screenshot"] is None:
            report["screenshot"] = session.capture(OUT_DIR / "t721-vacancy.png", target)
        session.close_tab(target)

    read_ok = [v for v in report["vacancies"] if v.get("ssr_name") or v.get("dom_title")]

    # --- one application form, read-only ----------------------------------
    if read_ok and args.form:
        vid = read_ok[0]["vacancy_id"]
        url = f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}"
        target = session.open_extra_tab(url)
        if target is not None:
            session.navigate(target, url)
            _log(f"reading the response form (read-only) for {vid}")
            form = _read(session, target, _FORM_JS)
            form["vacancy_id"] = vid
            form["clicked_anything"] = False
            report["form"] = form
            session.close_tab(target)

    # --- nothing sent ------------------------------------------------------
    # The form above was read and closed. Re-read the first vacancy: a real
    # response would move the response status and bump the counters (the T7.19
    # fingerprint), and the DOM would switch to "уже откликнулись".
    if read_ok:
        vid = read_ok[0]["vacancy_id"]
        before = read_ok[0]
        target = session.open_extra_tab(f"https://hh.ru/vacancy/{vid}")
        if target is not None:
            session.navigate(target, f"https://hh.ru/vacancy/{vid}")
            after = _read(session, target, _VACANCY_JS)
            report["nothing_sent"] = {
                "vacancy_id": vid,
                "response_statuses_before": before.get("response_statuses"),
                "response_statuses_after": after.get("response_statuses"),
                "counters_before": before.get("counters"),
                "counters_after": after.get("counters"),
                "respond_button_before": before.get("respond_button"),
                "respond_button_after": after.get("respond_button"),
                "already_responded_before": before.get("already_responded_dom"),
                "already_responded_after": after.get("already_responded_dom"),
                "unchanged": (
                    before.get("response_statuses") == after.get("response_statuses")
                    and before.get("counters") == after.get("counters")
                    and before.get("already_responded_dom") == after.get("already_responded_dom")
                ),
            }
            session.close_tab(target)

    # --- shutdown ----------------------------------------------------------
    _log("closing tabs and the browser")
    report["tabs_closed"] = session.close_all_tabs()
    shutdown = session.stop()
    report["shutdown"] = shutdown
    report["port_freed"] = shutdown.get("port_freed")
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    ok = (
        bool(report["launch"]["ok"])
        and (report["auth"] or {}).get("authenticated") is True
        and len(read_ok) >= args.min_vacancies
        and (report["nothing_sent"] or {}).get("unchanged") is True
        and shutdown.get("port_freed") is True
    )
    report["verdict"] = "PASS" if ok else "FIX"
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="*", default=None)
    ap.add_argument("--vacancies", type=int, default=3)
    ap.add_argument("--min-vacancies", type=int, default=3)
    ap.add_argument("--form", action="store_true", default=True)
    ap.add_argument("--no-form", dest="form", action="store_false")
    ap.add_argument("--require-auth", action="store_true", default=True)
    ap.add_argument("--json-out", default=str(OUT_DIR / "t721_dryrun.json"))
    args = ap.parse_args()

    report = run(args)
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ASCII-only console output: the Windows console mangles Cyrillic.
    print(json.dumps({
        "verdict": report["verdict"],
        "endpoint": report["endpoint"],
        "launch": report["launch"],
        "auth": {"authenticated": (report.get("auth") or {}).get("authenticated"),
                 "reason": (report.get("auth") or {}).get("reason")},
        "search_unique": (report.get("search") or {}).get("unique_vacancies"),
        "vacancies_read": [v.get("ssr_name") or v.get("dom_title") or v.get("error")
                           for v in report.get("vacancies", [])],
        "form": {k: (report.get("form") or {}).get(k) for k in
                 ("post_forms", "textareas", "submit_present", "submit_disabled",
                  "resume_titles", "vacancy_tests", "question_inputs",
                  "response_statuses")},
        "screenshot": report.get("screenshot"),
        "tabs_closed": report.get("tabs_closed"),
        "shutdown": report.get("shutdown"),
        "nothing_sent": report.get("nothing_sent"),
        "json_out": str(out),
    }, ensure_ascii=True, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
