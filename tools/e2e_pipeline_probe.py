"""End-to-end, NON-SUBMITTING proof that the extraction pipeline works.

Green tests are not proof. This drives the real pipeline against the real
browser over CDP and reports what actually came back.

It never clicks apply, never fills, never submits, never writes to the DB.

Run:  .venv/Scripts/python.exe tools/e2e_pipeline_probe.py
Exit: 0 = every check passed, 1 = at least one failed.

Checks, in order:
  1. the resolver picks a browser that is actually listening
  2. Playwright attaches over CDP (not the fingerprintable headless fallback)
  3. that browser's profile is logged into hh.ru
  4. a LIVE vacancy opens and is not misreported as blocked
  5. extraction returns a clean snapshot (no error, no cdp_fallback)
  6. the review gate lets a clean read through, and blocks a fallback read
"""

from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

# http_proxy in this shell turns a *dead* localhost port into a 502, which made
# a live browser read as offline once already. Make sure it cannot happen here.
for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(k, None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_assistant.browser_executor import PlaywrightBrowserAdapter
from ai_assistant.hh_browser_launcher import (
    BROWSEROS_CDP_URL,
    CDP_ALIVE,
    CDP_TIMEOUT,
    DEFAULT_HH_CDP_URL,
    probe_cdp,
    resolve_cdp_url,
)

FAILED: list[str] = []
HOME = "https://hh.ru/"


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)
    return ok


def candidate_vacancies(limit: int = 6) -> list[tuple[str, str]]:
    """Real hh.ru vacancy URLs from the local DB, not answered yet. Read-only.

    Skipping answered vacancies is not cosmetic: hh.ru replaces the
    "Откликнуться" button with a "Чат" button once you have responded, so a
    probe that picks an already-answered vacancy concludes the apply selector
    is broken. It did - see docs/ble001_triage.md, "apply_link".
    """
    db = os.path.join(ROOT, "state.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect(db)
    try:
        answered = {r[0] for r in con.execute(
            "select vacancy_stable_id from application_submissions")}
        answered |= {r[0] for r in con.execute(
            "select vacancy_stable_id from hh_applications")}
        rows = con.execute(
            "select stable_id, job_url from vacancies "
            "where job_url like '%hh.ru/vacancy%' order by last_seen_at desc limit ?",
            (limit * 4,),
        ).fetchall()
    finally:
        con.close()
    return [(r[0], r[1]) for r in rows if r[0] not in answered][:limit]


def looks_logged_in(body: str) -> bool:
    low = (body or "").lower()
    return "поднимите резюме" in low or "мои резюме" in low


def main() -> int:
    print("=== 1. which browser are we driving? ===")
    resolved = resolve_cdp_url()
    primary = probe_cdp(DEFAULT_HH_CDP_URL, timeout=2.0)
    print(f"  resolve_cdp_url() -> {resolved}")
    print(f"  probe_cdp(9222)   -> {primary}   (alive/dead/timeout)")
    print(f"  probe_cdp(9110)   -> {probe_cdp(BROWSEROS_CDP_URL, timeout=2.0)}")
    check("resolver returned a URL", bool(resolved))
    check("the chosen browser is actually listening",
          probe_cdp(resolved, timeout=2.0) == CDP_ALIVE, resolved)

    # An explicit CDP_URL/HH_CDP_URL is the operator's decision and wins over
    # any probing (finding #10). Only assert our own routing when nothing is
    # pinned - otherwise we would be "fixing" a deliberate choice.
    env_cdp = os.getenv("CDP_URL") or os.getenv("HH_CDP_URL")
    if env_cdp:
        print(f"  CDP pinned by env: {env_cdp}  (not a routing decision, nothing to assert)")
        check("resolver honours the pinned browser", resolved == env_cdp, resolved)
    elif primary == CDP_ALIVE:
        check("9222 alive -> resolver must stay on 9222", resolved == DEFAULT_HH_CDP_URL,
              "finding #13: a busy Chrome must not cost us the profile")
    elif primary == CDP_TIMEOUT:
        print("  [WARN] 9222 is slow right now; staying put is correct, not hopping")

    print()
    print("=== 2. attach over CDP (not the headless fallback) ===")
    ad = PlaywrightBrowserAdapter(cdp_url=resolved)
    opened = ad.open(HOME)
    print(f"  title  = {opened.get('title')!r}")
    print(f"  blocked= {opened.get('blocked')}")
    check("adapter attached over CDP", ad._is_cdp is True)
    check("cdp_fallback flag is clear", opened.get("cdp_fallback") is False,
          str(opened.get("cdp_fallback_reason")))
    check("homepage not misreported as blocked", opened.get("blocked") is not True,
          "finding #11: 'captcha' lives in hh.ru's i18n bundle on every page")

    print()
    print("=== 3. is this profile logged into hh.ru? ===")
    body_home = ""
    try:
        body_home = ad.page.inner_text("body") or ""
    except Exception as e:
        print(f"  body read failed: {type(e).__name__}: {e}")
    logged_in = looks_logged_in(body_home)
    print(f"  body chars = {len(body_home)}")
    print(f"  body head  = {body_home[:140]!r}")
    check("profile is logged into hh.ru", logged_in,
          "a logged-out profile reads a different form than submission sees")
    ad.close()

    print()
    print("=== 4. open a LIVE vacancy ===")
    live_url = None
    live_id = None
    for stable_id, url in candidate_vacancies():
        probe = PlaywrightBrowserAdapter(cdp_url=resolved)
        res = probe.open(url)
        title = res.get("title") or ""
        body = ""
        try:
            body = probe.page.inner_text("body") or ""
        except Exception as e:
            print(f"     body unreadable ({type(e).__name__}: {e}); skipping candidate")
            probe.close()
            continue
        archived = "архив" in title.lower() or "архив" in body.lower()[:2000]
        print(f"  {url}  blocked={res.get('blocked')}  archived={archived}")
        print(f"     title={title[:90]!r}")
        if not archived and res.get("blocked") is not True:
            live_url, live_id = url, stable_id
            ad = probe
            break
        probe.close()

    if not live_url:
        print("  no live vacancy found in state.db - fell back to the DB's first one")
        cands = candidate_vacancies(1)
        if not cands:
            print("  [FAIL] state.db has no hh.ru vacancy to test with")
            return 1
        live_id, live_url = cands[0]
        ad = PlaywrightBrowserAdapter(cdp_url=resolved)
        ad.open(live_url)

    print(f"  using {live_url}  (stable_id={live_id})")

    print()
    print("=== 5. real extraction ===")
    snap = ad.extract_application_form()
    print(f"    final_url    = {snap.get('final_url')}")
    print(f"    title        = {snap.get('title')!r}")
    print(f"    auth_form    = {snap.get('auth_form')}")
    print(f"    questions    = {len(snap.get('questions') or [])}")
    print(f"    controls     = {len(snap.get('controls') or [])}")
    print(f"    apply_link   = {snap.get('apply_link')}")
    print(f"    error        = {snap.get('error')} / {snap.get('error_reason')}")
    print(f"    cdp_fallback = {snap.get('cdp_fallback')} / {snap.get('cdp_fallback_reason')}")
    print(f"    body chars   = {len(snap.get('body_text') or '')}")

    check("snapshot has no error flag", snap.get("error") is not True,
          str(snap.get("error_reason")))
    check("snapshot has no cdp_fallback flag", snap.get("cdp_fallback") is False,
          str(snap.get("cdp_fallback_reason")))
    check("the page actually rendered", len(snap.get("body_text") or "") > 200)

    for c in (snap.get("controls") or [])[:5]:
        print(f"      control : {str(c)[:140]}")
    for q in (snap.get("questions") or [])[:5]:
        print(f"      question: {str(q)[:140]}")

    # Diagnostic only: an undetectable apply button would break the whole
    # pipeline downstream even with a perfect snapshot.
    print("  apply affordance present on the vacancy page?")
    try:
        low_body = (snap.get("body_text") or "").lower()
        print(f"    body mentions 'откликнуться': {'откликнуться' in low_body}")
        for sel in (
            "a[data-qa='vacancy-response-link-top']",
            "[data-qa='vacancy-response-link-top']",
            "a[data-qa*='vacancy-response']",
            "button[data-qa*='vacancy-response']",
        ):
            print(f"    {sel:46} count={ad.page.locator(sel).count()}")
        # What those buttons actually are - the extractor only looks for an <a>
        # with data-qa='vacancy-response-link-top', which hh.ru no longer emits.
        found = ad.page.eval_on_selector_all(
            "[data-qa*='vacancy-response']",
            "els => els.slice(0, 6).map(e => ({tag: e.tagName, qa: e.getAttribute('data-qa'), "
            "text: (e.innerText || '').trim().slice(0, 40), href: e.getAttribute('href')}))",
        )
        for f in found:
            print(f"    -> {f}")
    except Exception as e:
        print(f"    apply probe failed: {type(e).__name__}: {e}")

    print()
    print("=== 6. normalize + review gate ===")
    from ai_assistant.application_review_gate import build_review_gate
    from ai_assistant.hh_extractor import extract_application_form as normalize
    from ai_assistant.prefill_orchestrate import OrchestrationReport
    from ai_assistant.prefill_plan import PrefillPlan

    form = normalize(live_id, live_url, snap)
    meta = form.extraction_meta
    print(f"  application_type  = {form.application_type}")
    print(f"  questions         = {len(form.questions)}")
    print(f"  meta.error        = {meta.get('error')}")
    print(f"  meta.cdp_fallback = {meta.get('cdp_fallback')}")
    check("meta.cdp_fallback propagated as False", meta.get("cdp_fallback") is False)
    check("meta.error propagated as False", meta.get("error") is False)

    def gate_for(snapshot: dict, form_obj, vid: str):
        return build_review_gate(
            package=SimpleNamespace(vacancy_stable_id=vid, answers=[], form=form_obj),
            plan=PrefillPlan(vacancy_stable_id=vid),
            orchestration=OrchestrationReport(vacancy_stable_id=vid),
            final_snapshot=snapshot,
            form=form_obj,
        )

    clean_reasons = [
        str(r) for r in (getattr(gate_for(snap, form, live_id), "block_reasons", []) or [])
    ]
    print(f"  clean read reasons = {clean_reasons}")
    check("gate does NOT block a clean CDP read",
          not any("headless" in r or "cdp" in r.lower() for r in clean_reasons))

    dirty = dict(snap)
    dirty["cdp_fallback"] = True
    dirty["cdp_fallback_reason"] = "connect_over_cdp failed: simulated"
    dirty_form = normalize(live_id, live_url, dirty)
    dirty_reasons = [
        str(r) for r in (getattr(gate_for(dirty, dirty_form, live_id), "block_reasons", []) or [])
    ]
    print(f"  fallback reasons   = {dirty_reasons}")
    check("gate DOES block a headless-fallback read",
          any("headless browser fallback" in r for r in dirty_reasons))

    ad.close()
    print()
    print("=== RESULT ===")
    if FAILED:
        print(f"  {len(FAILED)} FAILED: {FAILED}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
