"""T7.24 — one read-only HH operation, with the browser tied to its lifetime.

This is the shape §4 asks for: the browser is not kept alive because the caller is
long-lived. An operation starts, acquires the browser, does its reading, and
releases it - so a web UI that has been up for three hours still owns no Chrome
between requests.

Read-only by construction: nothing here clicks, submits, types or sends. The only
CDP calls are navigation, ``Runtime.evaluate`` of a reader script, and the
teardown. There is no mouse dispatch anywhere in this module.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Sequence
from typing import Any

from . import hh_browser_lease as lease
from . import hh_browser_provider as bp
from . import hh_structured_preflight as sp
from . import shortlist_selection as selection
from .candidate_profile import load_candidate_profile

SEARCH_URL = "https://hh.ru/search/vacancy?text=%D0%91%D0%B8%D1%82%D1%80%D0%B8%D0%BA%D1%81&area=1"

# ---------------------------------------------------------------------------
# T7.26.2: the SERP card container, as one named selector
# ---------------------------------------------------------------------------
# HH renamed the attribute. Measured live on 2026-09-30 against
# ``https://hh.ru/search/vacancy?text=IT Support&area=1&order_by=publication_time``:
# ``[data-qa="vacancy-serp__vacancy"]`` -> 20 cards, ``[data-qa="serp-item"]`` -> **0**.
# The old name is kept *second* as a fallback so a board-side rollback does not blind
# the search again, but it is no longer the one that has to match.
#
# It lives in a constant rather than inlined twice because the same string used to be
# copy-pasted into two scripts, and a stale copy is exactly how the search went blind.
CARD_CONTAINER_SELECTORS = (
    '[data-qa="vacancy-serp__vacancy"]',
    '[data-qa="serp-item"]',
)
CARD_CONTAINER_SELECTOR = ", ".join(CARD_CONTAINER_SELECTORS)

# The salary line, also measured rather than assumed. HH no longer puts ``data-qa`` on
# the amount: the salary is the **first direct child** of the ``compensation-labels``
# block, and the only ``data-qa`` inside that block
# (``vacancy-serp__vacancy-compensation-frequency-*``) belongs to the *payment
# frequency* note ("Выплаты: Два раза в месяц"), not to the amount. The old fallback
# ``[data-qa*="compensation"]`` therefore matched that note and would have reported it
# as the salary - a wrong value, which is worse than ``None`` - so it is gone.
SALARY_SELECTOR = (
    '[data-qa="vacancy-serp__vacancy-compensation"], '
    '[class*="compensation-labels"] > span'
)

# How many results the search page shows. Counted, not clicked: the point of the
# search step is to prove the operation really reached a rendered result list.
COUNT_RESULTS_JS = """(() => {
  const items = document.querySelectorAll('__CARD_CONTAINER__');
  const links = document.querySelectorAll('a[data-qa="serp-item__title"]');
  return JSON.stringify({items: items.length, links: links.length,
                         title: document.title, url: location.href});
})()""".replace("__CARD_CONTAINER__", CARD_CONTAINER_SELECTOR)


# ---------------------------------------------------------------------------
# T7.26 §5: the query-driven search, on the same owned operation
# ---------------------------------------------------------------------------

def search_url(
    query: str,
    area: int | None = 1,
    schedule: str | None = None,
) -> str:
    """The SERP address for one query.

    The same shape the autonomous discovery uses (``text`` + ``area`` + newest
    first). When ``schedule`` is specified (e.g. 'remote'), it filters by schedule.
    When ``area`` is None, the region constraint is omitted.
    """
    params = [f"text={urllib.parse.quote(str(query))}"]
    if area is not None:
        params.append(f"area={int(area)}")
    if schedule:
        params.append(f"schedule={urllib.parse.quote(str(schedule))}")
    params.append("order_by=publication_time")
    return "https://hh.ru/search/vacancy?" + "&".join(params)


# The result list. Read, never clicked: cards are counted and extracted from the
# rendered page, and nothing in this module dispatches input.
READ_CARDS_JS = r"""(() => {
  const items = document.querySelectorAll('__CARD_CONTAINER__');
  const cards = [];
  items.forEach((it) => {
    const link = it.querySelector('a[data-qa="serp-item__title"]')
      || it.querySelector('a[href*="/vacancy/"]');
    const url = link ? link.href : '';
    const m = url.match(/vacancy\/(\d+)/);
    const emp = it.querySelector('[data-qa="vacancy-serp__vacancy-employer"], [data-qa*="vacancy-employer"]');
    const sal = it.querySelector('__SALARY_SELECTOR__');
    const addr = it.querySelector('[data-qa="vacancy-serp__vacancy-address"], [data-qa*="vacancy-address"]');
    const fmt = it.querySelector('[data-qa*="vacancy-label"], [class*="magritte-tag"]');
    cards.push({
      vacancy_id: m ? m[1] : '',
      title: link ? link.innerText.trim() : '',
      employer: emp ? emp.innerText.trim() : '',
      salary: sal ? sal.innerText.trim() : null,
      area: addr ? addr.innerText.trim() : null,
      format_label: fmt ? fmt.innerText.trim() : null,
      url: url,
    });
  });
  return JSON.stringify({url: location.href, count: items.length, cards: cards});
})()""".replace("__CARD_CONTAINER__", CARD_CONTAINER_SELECTOR).replace(
    "__SALARY_SELECTOR__", SALARY_SELECTOR)


# The employer's own prose, which is what the A/B/C band is decided from. The
# structured payload carries the *fields*; it does not carry the words, and §7 asks
# for a real band rather than a guess from ``workFormats``.
VACANCY_DESCRIPTION_JS = r"""(() => {
  const tpl = document.querySelector('#HH-Lux-InitialState');
  let fromPayload = null;
  try {
    const st = JSON.parse((tpl || {}).innerHTML || '');
    const v = (((st.vacancyView || {}).vacancyFull || {}).vacancy) || {};
    fromPayload = v.description || null;
  } catch (e) { fromPayload = null; }
  const el = document.querySelector('[data-qa="vacancy-description"]');
  const fromDom = el ? el.innerText : null;
  return JSON.stringify({from_dom: fromDom, from_payload: fromPayload});
})()"""


class HhOperationError(RuntimeError):
    """The operation could not be completed. Carries what was measured so far."""

    def __init__(self, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.evidence = evidence or {}


# T7.26 §9: the wait budget, as module state rather than default arguments, so the
# same wait can be tuned (and tested) without redefining the function.
_WAIT_ATTEMPTS = 12
_WAIT_DELAY = 0.6


def _wait_for_page(session: bp.HeadlessBrowserSession, target: str,
                   attempts: int | None = None, delay: float | None = None) -> str:
    """Poll the document until it is past ``about:blank`` and finished loading.

    The T7.21 lesson: ``readyState == "complete"`` alone is true on a fresh
    ``about:blank`` tab, so a single read returns the *previous* document.
    """
    attempts = _WAIT_ATTEMPTS if attempts is None else int(attempts)
    delay = _WAIT_DELAY if delay is None else float(delay)
    last = ""
    for _ in range(max(1, attempts)):
        raw = session.evaluate_on(target, bp._READY_JS)
        try:
            state = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError):
            state = {}
        last = json.dumps(state, ensure_ascii=False)
        href = str((state or {}).get("href") or "")
        if (state or {}).get("ready") == "complete" and href and href != "about:blank":
            return href
        time.sleep(delay)
    raise HhOperationError(f"the page never settled: {last}")


def run_read_only_preflight(
    vacancy_id: str,
    owner_type: str = lease.OWNER_UNKNOWN,
    purpose: str = "",
    profile: str | None = None,
    cdp_port: int | None = None,
) -> dict[str, Any]:
    """Search, read one vacancy structurally, run the preflight. Nothing else.

    Returns the measurements *and* the release report, because "the browser is
    gone afterwards" is a claim that has to be in the same result as the reading
    it refers to.
    """
    outcome: dict[str, Any] = {
        "vacancy_id": str(vacancy_id),
        "owner_type": owner_type,
        "steps": [],
        "mouse_events_dispatched": 0,
        "read_only": True,
    }

    try:
        with lease.hh_operation(
            owner_type, purpose=purpose or f"preflight:{vacancy_id}",
            profile=profile, cdp_port=cdp_port,
        ) as op:
            session = op["session"]
            outcome["lease_status"] = op["status"]
            outcome["session_id"] = op["lease"]["session_id"]
            outcome["owner_pid"] = op["lease"]["owner_pid"]

            start = session.start()
            outcome["start"] = {"ok": start.get("ok"), "status": start.get("status"),
                                "reused": start.get("reused")}
            if not start.get("ok"):
                raise HhOperationError(
                    f"the browser session is not usable: {start.get('status')} "
                    f"{start.get('message') or ''}".strip(),
                    outcome,
                )

            # --- search ---------------------------------------------------
            search_tab = session.ensure_work_tab(SEARCH_URL, purpose=bp.PURPOSE_SEARCH)
            if search_tab is None:
                raise HhOperationError("could not open the working tab", outcome)
            session.navigate(search_tab, SEARCH_URL)
            outcome["search_url"] = _wait_for_page(session, search_tab)
            raw = session.evaluate_on(search_tab, COUNT_RESULTS_JS)
            try:
                outcome["search"] = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                outcome["search"] = {"raw": str(raw)[:200]}
            outcome["steps"].append("search")

            # --- read the vacancy -----------------------------------------
            vacancy_url = f"https://hh.ru/vacancy/{vacancy_id}"
            session.repurpose_work_tab(bp.PURPOSE_VACANCY, vacancy_url)
            session.navigate(search_tab, vacancy_url)
            outcome["vacancy_url"] = _wait_for_page(session, search_tab)
            snapshot = session.evaluate_on(search_tab, sp.structured_js(vacancy_id))
            try:
                snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
            except (TypeError, ValueError) as exc:
                raise HhOperationError(
                    f"the structured snapshot is not JSON: {exc}", outcome) from exc
            if not isinstance(snapshot, dict):
                raise HhOperationError("the structured snapshot is empty", outcome)
            outcome["steps"].append("vacancy")

            # --- preflight ------------------------------------------------
            preflight = sp.structured_preflight(snapshot, load_candidate_profile())
            outcome["preflight"] = preflight
            outcome["summary"] = sp.human_summary(preflight)
            outcome["steps"].append("preflight")

            outcome["owned_tabs_during"] = session.ownership_registry()
            outcome["browser_during"] = bp.port_is_listening(
                int(op["lease"].get("cdp_port") or bp.HEADLESS_CDP_PORT)
            )
        # The ``with`` has exited: the release has run, and its report is readable.
        outcome["release"] = op["release_report"]
    except lease.BrowserBusy as exc:
        outcome["busy"] = True
        outcome["error"] = str(exc)
        outcome["steps"].append("busy")
        return outcome

    return outcome


def operation_report(vacancy_id: str, **kwargs: Any) -> dict[str, Any]:
    """``run_read_only_preflight`` plus the measured after-state.

    Kept separate so the release report is read *after* the teardown, from the
    world rather than from the call that performed it.
    """
    outcome = run_read_only_preflight(vacancy_id, **kwargs)
    port = int(kwargs.get("cdp_port") or bp.HEADLESS_CDP_PORT)
    outcome["after"] = {
        "headless_processes": len(bp.dedicated_profile_processes()),
        "port_listening": bp.port_is_listening(port),
        "lease_present": lease.read_lease() is not None,
        "lease_state": lease.lease_state()["state"],
    }
    return outcome


# ---------------------------------------------------------------------------
# T7.26 §3-§7: the owned search + read segment
# ---------------------------------------------------------------------------

def _geo_band(title: str, description: str, location: str | None,
              source: str | None = None) -> dict[str, Any]:
    """The project's own A/B/C verdict for one vacancy, employer-first.

    Uses ``eligibility`` rather than a fresh set of rules: the band is a decision the
    project already knows how to make, and re-deciding it here would be the second
    scraper §5 forbids, one layer up.
    """
    from .eligibility import classify_geo_band, classify_remote_mode

    remote_mode = classify_remote_mode(title, description, location, source)
    verdict = classify_geo_band(
        title=title,
        description=description,
        location=location,
        remote_mode=remote_mode,
    )
    return {
        "band": verdict.band.value,
        "evidence": verdict.evidence.value,
        "remote_mode": remote_mode.value,
        "reasons": list(verdict.reasons),
        "conflict": verdict.conflict,
        "employer_allows_thailand": verdict.employer_allows_thailand,
    }


def run_search_operation(
    queries: Sequence[str],
    read_limit: int = 10,
    owner_type: str = lease.OWNER_AUTONOMOUS,
    purpose: str = "search",
    profile: str | None = None,
    cdp_port: int | None = None,
    area: int | None = 1,
    schedule: str | None = None,
    thailand_settled: bool = False,
    require_auth: bool = True,
) -> dict[str, Any]:
    """Search HH for several queries and read the vacancies, on one owned browser.

    T7.26 §3-§7. This is the SEARCH_ONLY HH segment, and its shape is the whole
    point: **one** ``hh_operation`` with an explicit owner and purpose, so the
    browser exists for the search and the reads and not one second longer -
    acquire, search/read, release. The release report is read after the ``with``
    has exited, from the world rather than from the call that performed it.

    Read-only by construction. Nothing here submits, sends, answers, edits or
    clicks; ``submitted`` and ``messages_sent`` are reported as zeros so the claim
    travels in the same result as the reading it refers to.

    Per query the caller gets RESULT COUNT, UNIQUE COUNT and the address actually
    settled on. Per vacancy it gets the structured preflight (§6) and the A/B/C band
    (§7), or the reason it could not be read.
    """
    outcome: dict[str, Any] = {
        "owner_type": owner_type,
        "purpose": purpose,
        "queries": [],
        "vacancies": [],
        "read_limit": int(read_limit),
        "read_only": True,
        "mouse_events_dispatched": 0,
        "submitted": 0,
        "messages_sent": 0,
        "steps": [],
    }

    try:
        with lease.hh_operation(
            owner_type,
            purpose=purpose,
            profile=profile,
            cdp_port=cdp_port,
            session_kwargs={"require_auth": require_auth},
        ) as op:
            session = op["session"]
            outcome["lease_status"] = op["status"]
            outcome["session_id"] = op["lease"]["session_id"]
            outcome["owner_pid"] = op["lease"]["owner_pid"]
            outcome["cdp_port"] = int(op["lease"].get("cdp_port") or bp.HEADLESS_CDP_PORT)

            start = session.start()
            outcome["start"] = {"ok": start.get("ok"), "status": start.get("status"),
                                "reused": start.get("reused")}
            # The gate's own verdict, carried out so "AUTH = PASS" is a measurement
            # rather than an assumption about a session that started.
            outcome["auth"] = start.get("auth")
            outcome["auth_ok"] = bool((start.get("auth") or {}).get("authenticated"))
            if not start.get("ok"):
                raise HhOperationError(
                    f"the browser session is not usable: {start.get('status')} "
                    f"{start.get('message') or ''}".strip(),
                    outcome,
                )

            # One tab for the whole operation: search, vacancy, search, vacancy.
            tab = session.ensure_work_tab(purpose=bp.PURPOSE_SEARCH)
            if tab is None:
                raise HhOperationError("could not open the working tab", outcome)

            seen: dict[str, dict[str, Any]] = {}

            # --- the searches ---------------------------------------------
            for query in queries:
                url = search_url(query, area=area, schedule=schedule)
                row: dict[str, Any] = {"query": str(query), "url": url}
                try:
                    session.repurpose_work_tab(bp.PURPOSE_SEARCH, url)
                    session.navigate(tab, url)
                    row["settled_url"] = _wait_for_page(session, tab)
                    raw = session.evaluate_on(tab, READ_CARDS_JS)
                    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    if not isinstance(payload, dict):
                        payload = {}
                    cards = payload.get("cards")
                    cards = cards if isinstance(cards, list) else []
                    row["result_count"] = int(payload.get("count") or len(cards))
                    row["extracted"] = len(cards)
                    new = 0
                    for card in cards:
                        vid = str(card.get("vacancy_id") or "").strip()
                        if not vid:
                            continue
                        if vid not in seen:
                            seen[vid] = {**card, "found_by": []}
                            new += 1
                        if str(query) not in seen[vid]["found_by"]:
                            seen[vid]["found_by"].append(str(query))
                    row["unique_count"] = new
                    row["status"] = "OK"
                except Exception as exc:  # noqa: BLE001 - one query may fail, the rest run
                    row["status"] = "ERROR"
                    row["error"] = f"{type(exc).__name__}: {exc}"
                outcome["queries"].append(row)
                outcome["steps"].append(f"search:{query}")

            outcome["result_count"] = sum(
                int(q.get("result_count") or 0) for q in outcome["queries"])
            outcome["unique_count"] = len(seen)
            outcome["read_count"] = 0
            # T7.27 §2: the card-level pool, so a caller can rank and report on
            # everything that was discovered - not only on the part it chose to read.
            # Additive: the operation still returns the same reads it always did.
            outcome["pool"] = [
                {
                    "vacancy_id": vid,
                    "found_by": list(entry.get("found_by") or []),
                    **{k: entry.get(k) for k in
                       ("title", "employer", "salary", "area", "format_label", "url")},
                }
                for vid, entry in seen.items()
            ]

            # --- the reads -------------------------------------------------
            profile_obj = load_candidate_profile()
            # T7.27 §1: which vacancies get the expensive read is a *policy*, not a
            # position. Reading ``list(seen)[:read_limit]`` made the shortlist depend
            # on the order of the query list: with ["IT Support", "Bitrix24"] and
            # read_limit=10 every read came from the first query and Bitrix24 got
            # none - not because it found nothing, but because it was listed second.
            # The selection walks the target directions round-robin instead.
            picked = selection.select_read_targets(seen, read_limit)
            outcome["read_selection"] = picked.as_dict()
            for vid in picked.targets:
                entry: dict[str, Any] = {
                    "vacancy_id": vid,
                    "found_by": seen[vid]["found_by"],
                    "card": {k: seen[vid].get(k) for k in
                             ("title", "employer", "salary", "area",
                              "format_label", "url")},
                }
                try:
                    vacancy_url = f"https://hh.ru/vacancy/{vid}"
                    session.repurpose_work_tab(bp.PURPOSE_VACANCY, vacancy_url)
                    session.navigate(tab, vacancy_url)
                    entry["settled_url"] = _wait_for_page(session, tab)

                    raw = session.evaluate_on(tab, sp.structured_js(vid))
                    snapshot = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(snapshot, dict):
                        raise HhOperationError("the structured snapshot is empty")
                    entry["snapshot_ok"] = bool(snapshot.get("ok"))

                    entry["preflight"] = sp.structured_preflight(
                        snapshot, profile_obj, thailand_settled=thailand_settled)
                    entry["summary"] = sp.human_summary(entry["preflight"])

                    # §6/§7: the fields the preflight carries, lifted out so the
                    # report does not have to re-derive them, plus the band, which
                    # needs the employer's prose.
                    vacancy = snapshot.get("vacancy") or {}
                    title = str(vacancy.get("name") or entry["card"].get("title") or "")
                    desc_raw = session.evaluate_on(tab, VACANCY_DESCRIPTION_JS)
                    desc = json.loads(desc_raw) if isinstance(desc_raw, str) else desc_raw
                    description = ""
                    if isinstance(desc, dict):
                        description = str(desc.get("from_dom")
                                          or desc.get("from_payload") or "")
                    area_obj = vacancy.get("area") or {}
                    location = str(area_obj.get("name") or entry["card"].get("area") or "")
                    entry["read"] = {
                        "id": vid,
                        "title": title,
                        "employer": (entry["preflight"].get("employer") or {}).get("visibleName")
                        or entry["card"].get("employer"),
                        "salary": vacancy.get("compensation"),
                        "remote_wording": vacancy.get("workFormats"),
                        "work_experience": (entry["preflight"].get("experience") or {}).get("work_experience"),
                        "employment": (entry["preflight"].get("employment") or {}).get("label"),
                        "civil_law_contracts": (entry["preflight"].get("employment") or {}).get("civil_law_contracts"),
                        "area": location,
                        "archived": entry["preflight"].get("archived"),
                        "response_allowed": (snapshot.get("applicant") or {}).get("responseAllowed"),
                        "description_chars": len(description),
                        "description": description,
                        # T7.27 §5: the rest of the fields the shortlist has to show.
                        # Additive on purpose - the preflight already gates on these,
                        # this only lifts them out so a report does not re-read them.
                        "work_formats": vacancy.get("workFormats"),
                        "work_schedule": (snapshot.get("short_vacancy") or {}).get("workSchedule"),
                        "area_obj": area_obj,
                        "response_impossible": (snapshot.get("applicant") or {}).get("responseImpossible"),
                        "test": (snapshot.get("applicant") or {}).get("test"),
                        "vacancy_tests": (snapshot.get("applicant") or {}).get("vacancyTests"),
                    }
                    entry["geo"] = _geo_band(title, description, location)
                    entry["ok"] = True
                except Exception as exc:  # noqa: BLE001 - one unreadable vacancy is recorded
                    entry["ok"] = False
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                outcome["vacancies"].append(entry)
                outcome["read_count"] = sum(1 for v in outcome["vacancies"] if v.get("ok"))
                outcome["steps"].append(f"read:{vid}")

            outcome["owned_tabs_during"] = session.ownership_registry()

        # The ``with`` has exited: the release has run, and its report is readable.
        outcome["release"] = op["release_report"]
    except lease.BrowserBusy as exc:
        outcome["busy"] = True
        outcome["error"] = str(exc)
        outcome["steps"].append("busy")
        return outcome

    return outcome


def search_operation_report(queries: Sequence[str], **kwargs: Any) -> dict[str, Any]:
    """``run_search_operation`` plus the measured after-state.

    The same split as ``operation_report``: "the browser is gone afterwards" is a
    claim about the world, so it is measured from the world, after the teardown.
    """
    outcome = run_search_operation(queries, **kwargs)
    port = int(kwargs.get("cdp_port") or bp.HEADLESS_CDP_PORT)
    outcome["after"] = {
        "headless_processes": len(bp.dedicated_profile_processes()),
        "port_listening": bp.port_is_listening(port),
        "lease_present": lease.read_lease() is not None,
        "lease_state": lease.lease_state()["state"],
    }
    return outcome
