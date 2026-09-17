"""Read-only calibration probe for finding #27.

Finding #27 says the "form changed -> stop" invariant is unreachable in
production, and that turning it on today would stop every questionnaire: the
fingerprint covers question text, type, required and the whole ordered options
list, so an employer fixing a typo changes it. The report also measured that the
two extraction paths disagree with each other for the same form.

This probe answers the part that could only be answered live:

  1. what does the VACANCY page hand the extractor (controls / questions)?
  2. what does the RESPONSE FORM page hand it - the one with task_* fields?
  3. do those two paths agree on ids and fingerprint?
  4. would either of them match the fingerprint stored in state.db?
  5. is field_name stable across reloads and across the two pages? It is written
     to the DB by the extractor and read by nothing, and it is NOT part of the
     fingerprint. Measured: on this live page it comes out as hh__hash_<...>,
     i.e. a hash of the label - because the inputs carry no name, no id and no
     data-qa at all, so _control_stable_id() falls through to hashing. The
     response-form task_* names are NOT visible to a read-only probe: a direct
     GET on the form URL redirects to the vacancy page.

It never clicks apply, never fills a field, never submits. It DOES NOT write to
the production DB either - and that is not automatic:

    The first version of this probe was declared read-only and wrote a row into
    state.db (quest_1c2be2caaff3bfff, vacancy_stable_id=NULL), because
    extract_hh_questionnaire_from_snapshot() ended with
    db.save_hh_questionnaire(). Finding #38.

    That has since been fixed: extract_* is now pure, and
    discover_hh_questionnaire_from_snapshot() is the one that records. The probe
    still points config.DB_FILE at a throwaway copy before doing anything, and
    still prints both row counts - a guard that is cheap now and would have
    caught the original mistake. It also now demonstrates the fix: the sandbox
    must end up with the same number of rows as production, because a read
    writes nothing at all.

Run:  .venv/Scripts/python.exe tools/questionnaire_fingerprint_probe.py
Exit: 0 = probe completed, 1 = it could not complete (no live page / no session).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# http_proxy in this shell turns a dead localhost port into a 502 and made a
# live browser read as offline once already. Same guard as the e2e probe.
for _k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

from ai_assistant import config

PRODUCTION_DB = Path(config.DB_FILE)

# Point everything at a copy BEFORE any ai_assistant module can touch the DB.
_TMPDIR = tempfile.mkdtemp(prefix="ble38_probe_")
_SANDBOX_DB = Path(_TMPDIR) / "state.db"
shutil.copy2(PRODUCTION_DB, _SANDBOX_DB)
config.DB_FILE = str(_SANDBOX_DB)

from ai_assistant.browser_executor import PlaywrightBrowserAdapter
from ai_assistant.hh_browser_launcher import resolve_cdp_url
from ai_assistant.hh_questionnaire import (
    compute_questionnaire_fingerprint,
    extract_hh_questionnaire_from_snapshot,
)

HH_BASE = "https://hh.ru"
DB_FILE = _SANDBOX_DB

TASK_SELECTOR = (
    "input[type='radio'][name^='task_'], input[type='checkbox'][name^='task_'],"
    " textarea[name^='task_'], select[name^='task_']"
)


def describe(snapshot: dict, label: str) -> dict:
    """Extract the questionnaire from a snapshot and report what came out."""
    questions = snapshot.get("questions") or []
    controls = snapshot.get("controls") or []
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    items = list(quest.questions) if quest else []
    print(f"  --- {label} ---")
    print(f"      snapshot questions = {len(questions)}   controls = {len(controls)}")
    # Why field_name comes out as a label hash: _control_stable_id() looks for
    # dataQa > name > id and only then falls back to hashing the label. Counting
    # the three attributes says whether the DOM really carries control names.
    named = sum(1 for c in controls if (c.get("name") or "").strip())
    identified = sum(1 for c in controls if (c.get("id") or "").strip())
    qa = sum(1 for c in controls if (c.get("dataQa") or "").strip())
    task_named = sum(1 for c in controls if (c.get("name") or "").startswith("task_"))
    print(
        f"      controls with name={named} id={identified} dataQa={qa}"
        f"   of them task_*={task_named}"
    )
    print(f"      question_groups    = {len(snapshot.get('question_groups') or [])}")
    if not quest:
        print("      extractor returned None")
        return {"label": label, "ids": [], "fields": [], "fingerprint": None, "count": 0}
    ids = [str(q.question_id) for q in items]
    fields = [str(q.field_name or "") for q in items]
    print(f"      extractor items    = {len(items)}")
    print(f"      ids                = {ids}")
    print(f"      field_name         = {fields}")
    print(f"      fingerprint        = {quest.fingerprint}")
    for q in items[:4]:
        print(
            f"        {q.question_id!s:28s} type={q.question_type!s:22s}"
            f" required={bool(q.required)!s:5s} text={str(q.text)[:44]!r}"
        )
    return {
        "label": label,
        "ids": ids,
        "fields": fields,
        "fingerprint": quest.fingerprint,
        "count": len(items),
    }


def row_count(db: Path) -> int:
    """Count questionnaire rows, closing the handle.

    The first version of this probe left four 20 MB copies of state.db in %TEMP%:
    it opened sqlite connections and never closed them, so rmtree() failed on a
    locked file - and ignore_errors=True hid the failure.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM hh_questionnaires").fetchone()[0]
    finally:
        conn.close()


def stored_rows() -> list[dict]:
    """Read the hand-made questionnaire rows. Read-only connection."""
    if not DB_FILE.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT questionnaire_id, vacancy_stable_id, fingerprint, questions_json"
            " FROM hh_questionnaires"
        )
        out = []
        for qid, sid, fp, qjson in cur.fetchall():
            recomputed = None
            try:
                payload = json.loads(qjson) if qjson else []
                from ai_assistant.hh_questionnaire import HHQuestionItem
                items = [
                    HHQuestionItem(
                        question_id=str(x.get("question_id") or ""),
                        text=str(x.get("text") or ""),
                        question_type=str(x.get("question_type") or x.get("type") or "text"),
                        required=bool(x.get("required", True)),
                        options=list(x.get("options") or []),
                    )
                    for x in payload
                ]
                recomputed = compute_questionnaire_fingerprint(items)
            except Exception as exc:  # noqa: BLE001 - probe must report, not crash
                recomputed = f"<unreadable: {type(exc).__name__}: {exc}>"
            out.append({
                "questionnaire_id": qid,
                "vacancy_stable_id": sid,
                "stored_fingerprint": fp,
                "recomputed": recomputed,
                "ids": [x.get("question_id") for x in (json.loads(qjson) if qjson else [])],
                "fields": [x.get("field_name") for x in (json.loads(qjson) if qjson else [])],
            })
        return out
    finally:
        conn.close()


def main() -> int:
    resolved = resolve_cdp_url()
    print("=== CDP ===")
    print(f"  resolve_cdp_url() -> {resolved}")
    if not resolved:
        print("  [FAIL] no browser listening; cannot calibrate without a live page")
        return 1

    # A vacancy that actually has screening questions, taken from the DB.
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    try:
        candidates = [
            (sid, url)
            for sid, url in conn.execute(
                "SELECT stable_id, job_url FROM vacancies"
                " WHERE source = 'hh' AND job_url IS NOT NULL LIMIT 40"
            ).fetchall()
        ]
    finally:
        conn.close()
    print(f"  hh candidates in state.db: {len(candidates)}")
    if not candidates:
        print("  [FAIL] no hh vacancy to probe")
        return 1

    ad = None
    result_a = result_b = result_a2 = None
    task_controls = None
    form_url = None
    vacancy_url = None

    try:
        for sid, url in candidates:
            probe = PlaywrightBrowserAdapter(cdp_url=resolved)
            res = probe.open(url)
            if res.get("blocked") is True:
                probe.close()
                continue
            snap = probe.extract_application_form()
            if (snap.get("questions") or []) and not snap.get("error"):
                ad = probe
                vacancy_url = url
                print(f"\n=== 1. VACANCY page: {url}  ({sid}) ===")
                result_a = describe(snap, "vacancy page (what the runner sees first)")
                link = (snap.get("apply_link") or {}).get("href")
                if link:
                    form_url = link if link.startswith("http") else urljoin(HH_BASE, link)
                break
            probe.close()

        if ad is None:
            print("  [FAIL] no live vacancy with questions found")
            return 1

        if not form_url:
            print("\n  no apply_link on the page - cannot read the response form")
            return 1

        print(f"\n=== 2. RESPONSE FORM page: {form_url} ===")
        print("  (opening, not filling, not submitting)")
        ad.open(form_url)
        snap_b = ad.extract_application_form()
        result_b = describe(snap_b, "response form page (where task_* fields live)")
        try:
            task_controls = ad.page.locator(TASK_SELECTOR).count()
        except Exception as exc:  # noqa: BLE001
            task_controls = f"<query failed: {type(exc).__name__}>"
        print(f"      task_* answer controls on this page = {task_controls}")
        print("      (finding #34 gate 8 reads exactly this number)")
        print(f"      final_url = {snap_b.get('final_url')}")
        print(f"      title     = {str(snap_b.get('title'))[:70]!r}")

        print("\n=== 2b. VACANCY page, second read (is field_name stable?) ===")
        ad.open(vacancy_url)
        result_a2 = describe(ad.extract_application_form(), "vacancy page, reloaded")

        print("\n=== 2c. does the DOM carry the names at all? ===")
        print("  The snapshot builder reads e.getAttribute('name') - the ATTRIBUTE.")
        print("  A name assigned from JS as a property has no attribute, and the")
        print("  extractor would be blind to it. Comparing both, on the same page:")
        try:
            attr_task = ad.page.eval_on_selector_all(
                "input[name^='task_'], textarea[name^='task_'], select[name^='task_']",
                "els => els.length",
            )
            prop_task = ad.page.evaluate(
                "() => Array.from(document.querySelectorAll('input, textarea, select'))"
                ".filter(e => String(e.name || '').startsWith('task_')).length"
            )
            attr_any = ad.page.eval_on_selector_all(
                "input[name], textarea[name], select[name]", "els => els.length"
            )
            prop_any = ad.page.evaluate(
                "() => Array.from(document.querySelectorAll('input, textarea, select'))"
                ".filter(e => e.name).length"
            )
            hidden_only = ad.page.evaluate(
                "() => Array.from(document.querySelectorAll('input'))"
                ".filter(e => e.name && !e.getAttribute('name')).length"
            )
            print(f"      task_* by attribute : {attr_task}")
            print(f"      task_* by property  : {prop_task}")
            print(f"      any name by attrib  : {attr_any}")
            print(f"      any name by property: {prop_any}")
            print(f"      property-only names : {hidden_only}")
            if prop_task > attr_task:
                print("  -> [FINDING] names exist as JS properties, not attributes.")
                print("     getAttribute('name') returns null, so the snapshot, the")
                print("     question_groups builder and gate 8's CSS selector all see 0.")
            elif prop_any == attr_any == 0:
                print("  -> no names of either kind: this page really is not the form.")
        except Exception as exc:  # noqa: BLE001
            print(f"      [query failed] {type(exc).__name__}: {exc}")

        print("\n=== 2d. the normalised form vs the questionnaire: same ids? ===")
        print("  Production reads a page with extract_form_for_vacancy(), which")
        print("  returns a normalised ApplicationForm - and then THROWS THE SNAPSHOT")
        print("  AWAY. If the questionnaire were built from the normalised form")
        print("  instead, it would be a third producer with its own id scheme, and")
        print("  the comparison would be between two different shapes. Measuring:")
        try:
            from ai_assistant.hh_extractor import extract_application_form as normalise

            ad.open(vacancy_url)
            raw = ad.extract_application_form()
            form = normalise(
                vacancy_stable_id="hh:probe",
                url=vacancy_url,
                dom_snapshot=raw,
            )
            norm_ids = [str(q.id) for q in (form.questions or [])]
            print(f"      normalised application_type = {getattr(form.application_type, 'value', form.application_type)}")
            print(f"      normalised question ids     = {norm_ids[:6]}")
            print(f"      questionnaire ids           = {(result_a or {}).get('ids', [])[:6]}")
            if norm_ids[:6] == (result_a or {}).get("ids", [])[:6]:
                print("  -> the two agree; either source would do.")
            else:
                print("  -> [DECISIVE] the id schemes differ, so a questionnaire")
                print("     built from the normalised form would never match one read")
                print("     from a snapshot. Collection MUST use the snapshot path.")
        except Exception as exc:  # noqa: BLE001
            print(f"      [failed] {type(exc).__name__}: {exc}")
    finally:
        if ad is not None:
            ad.close()

    print("\n=== 3. do the two paths agree? ===")
    if result_a and result_b:
        same_ids = result_a["ids"] == result_b["ids"]
        same_fp = result_a["fingerprint"] == result_b["fingerprint"]
        same_fields = result_a["fields"] == result_b["fields"]
        print(f"  ids equal        : {same_ids}")
        print(f"  field_name equal : {same_fields}")
        print(f"  fingerprint equal: {same_fp}")
        if not same_ids:
            print(f"    vacancy page ids: {result_a['ids']}")
            print(f"    form page ids   : {result_b['ids']}")

    print("\n=== 3b. is field_name stable across a reload of the same page? ===")
    if result_a and result_a2:
        print(f"  field_name equal : {result_a['fields'] == result_a2['fields']}")
        print(f"  fingerprint equal: {result_a['fingerprint'] == result_a2['fingerprint']}")
        if result_a["fields"] != result_a2["fields"]:
            print(f"    read 1: {result_a['fields']}")
            print(f"    read 2: {result_a2['fields']}")
        if result_a["fields"] and result_a["fields"] == result_a2["fields"]:
            print("  -> field_name is deterministic for this page. It is still a")
            print("     hash of the label, not the control name, so it adds nothing")
            print("     the fingerprint does not already cover (the label is the text).")

    print("\n=== 4. would a live read match what is stored? ===")
    rows = stored_rows()
    if not rows:
        print("  no rows in hh_questionnaires")
    for row in rows:
        print(f"  {row['questionnaire_id']} (vacancy {row['vacancy_stable_id']})")
        print(f"    stored fingerprint : {row['stored_fingerprint']}")
        print(f"    recomputed from DB : {row['recomputed']}")
        print(f"    stored ids         : {row['ids'][:6]}")
        print(f"    stored field_name  : {row['fields'][:6]}")
        for res in (result_a, result_b, result_a2):
            if not res or not res["fingerprint"]:
                continue
            match = res["fingerprint"] == row["stored_fingerprint"]
            print(f"    live {res['label'][:22]:22s} -> would match stored? {match}")

    print("\n=== 5. what this settles for option B ===")
    print("  The stored rows are not all of one kind, and that is the whole problem:")
    print("    - rows built from a hand-made 'questions' list (branch 2 of the")
    print("      extractor) get semantic ids (q1_work_location) and can NEVER match")
    print("      a live read, because a live page always has controls and always")
    print("      takes branch 1, which numbers questions positionally q1..q6;")
    print("    - a row built from a live read matches live reads exactly.")
    print("  So B does not need a treaty about id schemes. It needs the stored row")
    print("  and the comparison read to come from the SAME call path. Collect live,")
    print("  compare live, and the comparison is meaningful on the first try.")

    print("\n=== RESULT ===")
    print("  probe completed; nothing was clicked, filled or submitted")
    print(f"  sandbox DB (all writes land here): {_SANDBOX_DB}")
    prod_rows = row_count(PRODUCTION_DB)
    sand_rows = row_count(_SANDBOX_DB)
    print(f"  hh_questionnaires rows: production={prod_rows}  sandbox={sand_rows}")
    if sand_rows == prod_rows:
        print("  [OK] the read recorded nothing: extract_* is pure (finding #38).")
    else:
        print("  [WARN] the sandbox gained rows, so something on this path writes.")
    print("  Note: equality alone would not prove that before #38 was fixed - the")
    print("  extractor's id is sha256(f'{vid}_{cid}_{fp}'), so a repeat write")
    print("  overwrites the same row instead of appending. The pin for the fix is")
    print("  test_extracting_a_questionnaire_does_not_write_to_the_database.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
        if Path(_TMPDIR).exists():
            # Never hide this: a leftover copy is 20 MB, and a silent failure to
            # clean up looks exactly like a successful clean-up.
            print(f"  [WARN] could not remove {_TMPDIR} - delete it by hand")
