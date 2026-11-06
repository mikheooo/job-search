"""T7.22 phase 1: find one vacancy worth a real application, read-only.

Runs the whole thing inside one process, because our headless Chrome does not outlive
the command that started it: launch, authenticate, search, brief, then close every tab and
the browser. Nothing here writes to HH -- no click, no keystroke, no POST. The only
non-GET behaviour in the file is the optional mouse probe (``--probe-mouse``), which clicks
the *letter toggle* on one form: it opens a textarea and submits nothing.

Search reuses ``.t76_hh_search``'s query set and card selectors, but not its navigation:
that module resolves a tab by URL substring, and with more than one hh.ru tab open it
answers about the wrong page (the T7.21 trap). Here every read is addressed by target id.

    python tools/.t722_scan.py --briefs 12 --probe-mouse
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ai_assistant.hh_browser_provider import HeadlessBrowserSession  # noqa: E402
from ai_assistant.hh_cdp_submit import BRIEF_JS as _BRIEF_JS  # noqa: E402

OUT = REPO / "artifacts" / "headless-poc"
WORK = REPO / "tools" / ".t722"

# Vacancies already acted on. Read out of state.db on 2026-09-28 (tracking
# SUBMITTED/APPLIED, submissions, WITHDRAWN). Hard-coded on purpose: this is the
# "do not touch twice" list, and it should be visible in the diff, not inferred.
ALREADY_DONE = {
    "128659037", "135824428", "135854121", "135893265", "136097888", "136225042",
    "136296220", "136483612", "136611193", "136704137", "136716836", "136745031",
    "136745352", "136821538", "136965331", "137163684", "137364729", "137496550",
}

# Title keywords, from T7.22 section 2. P1 first, then P2.
P1_WORDS = (
    "bitrix24", "битрикс", "crm", "help desk", "helpdesk", "service desk",
    "application support", "it support", "technical support", "tech support",
    "support engineer", "system administrator", "системный администратор",
    "технической поддержки", "техподдержк", "сопровождени",
)
P2_WORDS = ("инфраструктур", "интеграц", "infrastructure", "integration", "1 лини", "1-ой лини")

# Hard nos, from section 2: roles that are a different profession or a level above.
BAD_WORDS = (
    "java", "qa ", " qa", "sdet", "senior", "сеньор", "lead ", "team lead", "техлид",
    "kubernetes", "k8s", "devops", "архитектор", "architect", "head of", "директор",
    "1с", "1c ", "стажер", "стажёр", "junior",
)

_CARDS_JS = """(() => {
  const cards = Array.from(document.querySelectorAll('[data-qa="vacancy-serp__vacancy"]'));
  return JSON.stringify({
    url: location.href,
    ready: document.readyState,
    cards: cards.map(c => {
      const link = c.querySelector('a[data-qa="serp-item__title"]');
      const emp  = c.querySelector('[data-qa="vacancy-serp__vacancy-employer"]');
      const comp = c.querySelector('[data-qa="vacancy-serp__vacancy-compensation"]');
      const addr = c.querySelector('[data-qa="vacancy-serp__vacancy-address"]');
      const date = c.querySelector('[data-qa="vacancy-serp__vacancy-date"]');
      const resp = c.querySelector('[data-qa*="responded"]');
      const url  = link ? link.href : '';
      const m    = url.match(/vacancy\\/(\\d+)/);
      return {
        vacancy_id: m ? m[1] : '',
        title: link ? link.innerText.trim() : '',
        employer: emp ? emp.innerText.trim() : '',
        compensation: comp ? comp.innerText.trim() : '',
        address: addr ? addr.innerText.trim() : '',
        date: date ? date.innerText.trim() : '',
        already_responded: !!resp
      };
    })
  });
})()"""

# Compact brief lives in ai_assistant.hh_cdp_submit.BRIEF_JS (imported above).
# vacancyView.vacancy does not exist (measured 2026-09-28, T7.21).

# Mouse probe: the letter toggle only. Opens a textarea; reaches no submit button.
_TOGGLE_JS = """(() => {
  const t = document.querySelector('[data-qa="vacancy-response-letter-toggle"]');
  const ta = document.querySelector('textarea');
  return JSON.stringify({
    visibility: document.visibilityState,
    hidden: document.hidden,
    toggle: t ? {tag: t.tagName, text: (t.innerText || '').trim().slice(0, 60),
                 w: Math.round(t.getBoundingClientRect().width),
                 h: Math.round(t.getBoundingClientRect().height)} : null,
    textarea_before: ta ? ta.getAttribute('data-qa') : null,
    textareas: [...document.querySelectorAll('textarea')].length
  });
})()"""

_CLICK_JS = """(() => {
  const e = document.querySelector('[data-qa="vacancy-response-letter-toggle"]');
  if (!e) return JSON.stringify({error: 'no-toggle'});
  e.scrollIntoView({block: 'center', inline: 'center'});
  const r = e.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const hit = document.elementFromPoint(cx, cy);
  return JSON.stringify({
    x: cx, y: cy, vw: window.innerWidth, vh: window.innerHeight,
    hit_tag: hit ? hit.tagName : null,
    hit_text: hit ? (hit.innerText || '').trim().slice(0, 40) : null,
    is_self: !!hit && (hit === e || e.contains(hit) || hit.contains(e))
  });
})()"""


def _log(message: str) -> None:
    print(message, flush=True)


def _read(session: HeadlessBrowserSession, target: str, js: str,
          tries: int = 8) -> dict[str, Any]:
    """Read a JSON payload off a tab, retrying while the document settles."""
    last: dict[str, Any] = {"error": "no read attempted"}
    for _ in range(tries):
        try:
            payload = json.loads(session.evaluate_on(target, js))
            if isinstance(payload, dict) and (payload.get("ok") or payload.get("cards") is not None
                                              or payload.get("toggle") is not None
                                              or payload.get("visibility")):
                return payload
            last = payload if isinstance(payload, dict) else {"error": "not an object"}
        except Exception as exc:  # noqa: BLE001 - mid-navigation reads fail
            last = {"error": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.5)
    return last


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tab_ws(endpoint: str, target_id: str) -> str | None:
    from ai_assistant.hh_browser_provider import list_tabs

    for tab in list_tabs(endpoint):
        if tab.get("id") == target_id:
            return tab.get("webSocketDebuggerUrl")
    return None


def _activate(endpoint: str, target_id: str) -> Any:
    """Bring the tab to the front. Chrome drops mouse events for a hidden page."""
    from ai_assistant.hh_browser_provider import _ws_call, cdp_version

    ws = (cdp_version(endpoint) or {}).get("webSocketDebuggerUrl")
    if not ws:
        return None
    return _ws_call(ws, "Target.activateTarget", {"targetId": target_id})


def _click_at(endpoint: str, target_id: str, x: float, y: float) -> Any:
    """A real three-event click at a measured point.

    This lives here rather than in the provider on purpose: the provider is asserted by
    ``test_t721_headless_provider.test_the_provider_cannot_click_or_type`` to contain no
    ``Input.dispatch*`` call at all, and that invariant is worth more than the
    convenience of one shared helper. The click path itself is the one T7.19 used --
    ``mouseMoved`` first, then press, then release.

    ``_ws_call`` is synchronous (it owns its own ``_run_sync``); wrapping it in
    ``_run_sync`` again is what broke the first probe run with "an asyncio.Future,
    a coroutine or an awaitable is required".
    """
    from ai_assistant.hh_browser_provider import _ws_call

    ws = _tab_ws(endpoint, target_id)
    if not ws:
        return None
    sent = []
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        sent.append(_ws_call(ws, "Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "left", "clickCount": 1,
            "buttons": 1 if kind == "mousePressed" else 0,
        }))
    return sent


def _score(card: dict[str, Any]) -> int:
    """Rank a search card. Higher is better; the title decides most of it."""
    title = (card.get("title") or "").lower()
    score = 0
    if any(w in title for w in P1_WORDS):
        score += 100
    if any(w in title for w in P2_WORDS):
        score += 40
    if any(w in title for w in BAD_WORDS):
        score -= 200
    if "удален" in title or "удалён" in title or "remote" in title:
        score += 20
    if card.get("compensation"):
        score += 5
    return score


def _probe_mouse(session: HeadlessBrowserSession, brief: dict[str, Any]) -> dict[str, Any]:
    """Prove the mouse path works in headless, using a click that cannot submit.

    The letter toggle opens a textarea and reaches no submit button: it is the one
    control on the response form whose click is both meaningful and harmless. If a
    real ``Input.dispatchMouseEvent`` cannot drive HH here, it cannot drive the
    submit either, and that is worth knowing before the real thing.
    """
    vid = brief["vacancy_id"]
    url = f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}"
    target = session.open_extra_tab(url)
    if target is None:
        return {"vacancy_id": vid, "error": "no tab", "submitted_anything": False}
    try:
        session.navigate(target, url)
        before = _read(session, target, _TOGGLE_JS)
        clicked: dict[str, Any] = {"clicked": False}
        if (before.get("toggle") or {}).get("h"):
            _activate(session.cdp_url, target)
            box = json.loads(session.evaluate_on(target, _CLICK_JS))
            if not box.get("error") and 0 <= box.get("x", -1) <= box.get("vw", 0):
                _click_at(session.cdp_url, target, box["x"], box["y"])
                clicked = {"clicked": True, **box}
        time.sleep(1.5)
        after = _read(session, target, _TOGGLE_JS)
        result = {
            "vacancy_id": vid, "url": url,
            "before": before, "click": clicked, "after": after,
            "textarea_appeared": (after.get("textareas") or 0) > (before.get("textareas") or 0),
            "submitted_anything": False,
        }
        _log(f"mouse probe on {vid}: {json.dumps(clicked, ensure_ascii=True)}")
        _log(f"  textareas {before.get('textareas')} -> {after.get('textareas')} "
             f"visibility={after.get('visibility')}")
        return result
    finally:
        session.close_tab(target)


def run(args: argparse.Namespace) -> dict[str, Any]:
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "headless", "search": None, "cards": [], "briefs": [],
        "mouse_probe": None, "shutdown": None, "port_freed": None,
    }

    session = HeadlessBrowserSession(require_auth=True)
    started = session.start()
    report["endpoint"] = session.cdp_url
    report["launch"] = {
        "ok": started.get("ok"), "status": started.get("status"),
        "pid": session.pid, "reused": session.reused,
        "auth": (started.get("auth") or {}).get("reason"),
        "auth_ok": (started.get("auth") or {}).get("authenticated"),
    }
    _log(f"launch ok={started.get('ok')} pid={session.pid} auth={report['launch']['auth']}")
    if not started.get("ok"):
        report["verdict"] = "LAUNCH_FAILED"
        report["shutdown"] = session.stop()
        return report

    try:
        work = session.ensure_work_tab()
        if work is None:
            report["verdict"] = "NO_WORK_TAB"
            return report

        t76 = _load_tool("t76_search", ".t76_hh_search.py")
        directions = args.directions or list(t76.PRIMARY_DIRECTIONS)
        directions = [d for d in directions if d in t76.DIRECTION_QUERIES]
        if not directions:
            report["verdict"] = "NO_DIRECTIONS"
            return report
        seen: dict[str, dict[str, Any]] = {}
        per_query: list[dict[str, Any]] = []

        for direction in directions:
            for query in t76.DIRECTION_QUERIES[direction]:
                url = t76.SEARCH_URL.format(q=query.replace(" ", "+"))
                session.navigate(work, url)
                payload = _read(session, work, _CARDS_JS)
                cards = payload.get("cards") or []
                fresh = 0
                for card in cards:
                    vid = card.get("vacancy_id")
                    if not vid:
                        continue
                    card["direction"] = direction
                    card["query"] = query
                    if vid not in seen:
                        seen[vid] = card
                        fresh += 1
                per_query.append({"direction": direction, "query": query,
                                  "cards": len(cards), "new": fresh,
                                  "error": payload.get("error")})
                _log(f"  [{direction}] {query!r}: {len(cards)} cards, {fresh} new")

        report["search"] = {"directions": directions, "per_query": per_query,
                            "unique": len(seen)}

        ranked = sorted(
            seen.values(),
            key=lambda c: (-_score(c), c.get("date") or ""),
        )
        report["cards"] = ranked[:60]
        _log(f"unique vacancies: {len(seen)}")

        picked: list[dict[str, Any]] = []
        skip = set(args.exclude or [])
        for card in ranked:
            vid = card["vacancy_id"]
            if vid in ALREADY_DONE or vid in skip:
                continue
            if _score(card) < 40:
                continue
            picked.append(card)
            if len(picked) >= args.briefs:
                break
        _log(f"briefs to read: {len(picked)}")

        for card in picked:
            vid = card["vacancy_id"]
            url = f"https://hh.ru/vacancy/{vid}"
            target = session.open_extra_tab(url)
            if target is None:
                report["briefs"].append({"vacancy_id": vid, "error": "no tab"})
                continue
            session.navigate(target, url)
            brief = _read(session, target, _BRIEF_JS.replace("__VID__", vid))
            brief["vacancy_id"] = vid
            brief["search_title"] = card.get("title")
            brief["search_employer"] = card.get("employer")
            brief["search_compensation"] = card.get("compensation")
            brief["search_address"] = card.get("address")
            brief["search_date"] = card.get("date")
            brief["search_direction"] = card.get("direction")
            brief["score"] = _score(card)
            desc = brief.pop("description", "") or ""
            (WORK / f"desc_{vid}.txt").write_text(desc, encoding="utf-8")
            brief["desc_chars"] = len(desc)
            _log(f"  {vid} {brief.get('name')!r} area={(brief.get('area') or {}).get('name')!r} "
                 f"desc={len(desc)}")
            report["briefs"].append(brief)
            session.close_tab(target)

        if args.probe_mouse and report["briefs"]:
            probe = next((b for b in report["briefs"] if b.get("ok")), None)
            if probe:
                # The probe is a side quest: if it breaks, the scan still counts. The
                # first run of this tool lost 900 search results to a TypeError here.
                try:
                    report["mouse_probe"] = _probe_mouse(session, probe)
                except Exception as exc:  # noqa: BLE001
                    report["mouse_probe"] = {
                        "error": f"{type(exc).__name__}: {exc}",
                        "submitted_anything": False,
                    }
                    _log(f"mouse probe failed: {report['mouse_probe']['error']}")
    finally:
        report["leftover_tabs"] = session.leftover_tabs()
        report["shutdown"] = session.stop()
        report["port_freed"] = bool(report["shutdown"].get("port_freed"))
        report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    report["verdict"] = "SCANNED" if report["briefs"] else "NO_CANDIDATES"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--briefs", type=int, default=12)
    parser.add_argument("--directions", nargs="*", default=None,
                        help="subset of .t76_hh_search directions; default is all primary")
    parser.add_argument("--exclude", nargs="*", default=None,
                        help="vacancy ids to skip (e.g. the ones already briefed)")
    parser.add_argument("--probe-mouse", action="store_true")
    parser.add_argument("--json-out", default=str(OUT / "t722_scan.json"))
    args = parser.parse_args()

    report = run(args)
    Path(args.json_out).write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    summary = {
        "verdict": report.get("verdict"),
        "pid": (report.get("launch") or {}).get("pid"),
        "unique": (report.get("search") or {}).get("unique"),
        "briefs": len(report.get("briefs") or []),
        "mouse_probe": report.get("mouse_probe", {}).get("textarea_appeared")
        if report.get("mouse_probe") else None,
        "port_freed": report.get("port_freed"),
        "json_out": args.json_out,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=1))
    return 0 if report.get("verdict") == "SCANNED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
