# T7.26 — MAKE SEARCH_ONLY ACTUALLY SEARCH HH HEADLESS + FIX NAVIGATION SETTLE

**Verdict: `FIX`** — the code is written, unit-proven and lint-clean, but the task
**stopped early** because §12 fired: `ai_assistant/cli.py` changed underneath this
session mid-task. The live verification §5/§6/§10 ask for was **not run**. See
`tools/.t726/EXTERNAL_MODIFICATION_DETECTED.md`.

Every number below is a measurement. Where something was not measured it says
`NOT RUN` and why.

---

## §A. BASELINE SEARCH_ONLY

Proven live, before any edit. Full verbatim record:
`tools/.t726/baseline_evidence.md`.

```
HH_BROWSER_MODE=headless SUBMIT_ALLOWED=false \
    .venv/Scripts/python.exe -m ai_assistant.cli autonomous once --mode SEARCH_ONLY --json
```

Exit `0`, cycle `09:17:13.655002+00:00` → `09:17:15.859709+00:00` = **2.20 s**:

```json
{"status": "SEARCH_ONLY", "discovered_count": 0, "matched_count": 0,
 "applied_count": 0, "messages_checked": 0, "errors": [],
 "summary": "SEARCH_ONLY cycle: discovered 0, matched 0. ..."}
```

Browser state **before and after — identical**:

```
OWNED_TABS 0 | HEADLESS_PROCESSES 0 [] | PORT_9111_FREE True
PROFILE_LOCK_FREE True | LEASE_ABSENT True (state ABSENT) | CLEAN True
```

An instrumented probe wrapping `hh_operation`, `launch_headless`,
`ensure_headless_browser` and `ensure_hh_browser` with call-recording spies:

```
status          : SEARCH_ONLY
discovered      : 0
matched         : 0
config.cdp_url  : 'http://127.0.0.1:9111'
evaluate_fn     : None
LAUNCH/LEASE CALLS: NONE
```

**`NONE`.** SEARCH_ONLY reached neither the ownership layer nor any launch
primitive. The four links of the defect, each measured:

1. `run_cycle` :741-742 — the SEARCH_ONLY branch returns **above** the T7.25
   ownership wrapper at :750, so no browser is ever acquired.
2. `_search_only_cycle` :1580 → `_discover_fresh_vacancies` :883.
3. :917-923 — `evaluate_fn` is `None`, so it tries
   `make_cdp_evaluate("http://127.0.0.1:9111", "hh.ru")`. Measured:
   `URLError <urlopen error [WinError 10061] …>` — nothing listens on 9111
   (correct: headless is not running), so `eval_fn` stays `None`.
4. :925 `if eval_fn:` is **skipped entirely** ⇒ zero queries ⇒ `discovered_count: 0`.

A second, latent defect sat behind the first: the JS at :929-949 sets
`location.href` and returns `{"status": "navigating"}` on its first call without
re-reading, so even with a live browser the result list came back empty.

## §B. NEW SEARCH_ONLY FLOW

```
SEARCH_ONLY cycle
  └─ hh_readonly_operation.search_operation_report(queries, read_limit, owner_type=OWNER_AUTONOMOUS, purpose="search")
       └─ hh_operation(OWNER_AUTONOMOUS, purpose="search", session_kwargs={"require_auth": True})
            acquire ─▶ session.start() ─▶ auth gate
              ├─ one work tab (PURPOSE_SEARCH), reused for every step
              ├─ per query:  navigate ─▶ _wait_for_page ─▶ READ_CARDS_JS ─▶ dedupe
              ├─ per vacancy: repurpose ─▶ navigate ─▶ _wait_for_page
              │               ─▶ structured_js ─▶ structured_preflight ─▶ geo band
              └─ finally: release  ─▶ tabs closed, browser stopped, lease cleared
  └─ score with evaluate_candidate_match, rank, summarise, write the cycle-run row
```

§2 is preserved: the cycle still writes **only** its own cycle-run row. No
application, package, review, approval, queue row or application status.

§4 is preserved: the browser exists for the search/read segment and not one second
longer — the release runs in `hh_operation`'s `finally`, and the *measured* after-state
(`after.headless_processes` / `port_listening` / `lease_present` / `lease_state`) is
read after the `with` has exited.

Files changed:

| file | size | sha256 (16) | lines |
|---|---|---|---|
| `ai_assistant/hh_readonly_operation.py` | 22 064 B | `156bf29acbc68e64` | 484 |
| `ai_assistant/hh_structured_preflight.py` | 23 971 B | `da1f2ecd2eb986ca` | 574 |
| `ai_assistant/hh_vacancy_navigator.py` | 19 619 B | `9adf8f2b5b24de76` | 481 |
| `ai_assistant/hh_autonomous_agent.py` | 83 200 B | `c8338f7e8a39bfbe` | 1 741 |
| `tests/test_t726_search_only_headless.py` | 26 931 B | `bdf1db7e05288985` | 633 |

## §C. HH LIVE SEARCH — **NOT RUN**

Blocked by the §12 STOP. The operation and its per-query accounting are implemented
and proven against a scripted session (§I), but no live HH search was executed in
this task. The §1 baseline run is the only live HH traffic this task generated, and
it performed no search.

## §D. 10 VACANCY READ — **NOT RUN**

Blocked by the same STOP. The read path (§6/§7) is implemented in
`run_search_operation` and covered by tests, but no live vacancy was read.

## §E. STRUCTURED PREFLIGHT

The T7.23 reader is **reused, not duplicated**, and extended with the two §6 material
fields it did not carry. `STRUCTURED_JS` now also reads:

* `vacancy.archived` — `typeof v.archived === "boolean" ? v.archived : null`, so an
  absent value is `None` ("the page did not state it"), never `False`.
* `employer` — `{visibleName, name, id}` from `vacancyView.vacancyFull.company`, with
  a `[data-qa="vacancy-company-name"]` DOM fallback.

Both are surfaced on `structured_preflight()`'s result and are **reported, not
gated**: they are deliberately absent from `REQUIRED_STRUCTURED_FIELDS`, so no gate's
verdict moved. `test_the_snapshot_reader_carries_the_two_added_material_fields` pins
this, and `test_the_structured_preflight_receives_the_full_payload` proves
`fields_missing == []` for a full payload.

§7's classifications: salary and employment come from the preflight's own gates
(`salary_gate` → `SALARY_OK` / `SALARY_UNKNOWN` / `SALARY_BELOW_MINIMUM` /
`CURRENT_FX_REQUIRED` / `GROSS_NET_BASIS_UNVERIFIED`; `employment_gate` →
`TK_RF` / `GPH_IP` / `GPH_SELF_EMPLOYED` / `GPH_OTHER` / `UNKNOWN`). The A/B/C band
comes from `eligibility.classify_geo_band` + `classify_remote_mode`, i.e. the
project's own employer-first classifier, fed the employer's own description text read
from the vacancy page. `USER_DECISION_REQUIRED` is never converted into an allowance:
`auto_send_permitted` stays `verdict == CLEAR`.

## §F. NAVIGATION FIX

Two sites in `hh_vacancy_navigator.py`, both the same defect.

**The named one** — `verify_and_navigate_hh_vacancy` used to set `location.href`,
`time.sleep(2.5)`, then read the page once. A page slower than the sleep returned the
document we had just left, so the URL check reported a false "does not match" and the
UI flags described the *previous* page.

**The cheaper one** — `ensure_open_vacancy_tab` did `time.sleep(2.0)` and returned
without looking at the result at all.

Both now use the provider's own two-condition idiom, expressed once as
`_settle_after_navigation`:

* the URL is real, is **not** `about:blank`, and **changed** from the URL we came
  from — compared against *before*, not against the requested URL, because HH
  redirects;
* the URL really is a vacancy page;
* when the page reports `ready` (`readyState`), it must be `complete`;
* when the page reports `has_ssr` (`#HH-Lux-InitialState`), the marker must be present.

`time.sleep(2.5)` and `time.sleep(2.0)` are gone; the two remaining `time.sleep`
calls are inside bounded polling loops (40 × 0.25 s = 10 s for the navigator; the
same budget for the tab wait). A page that never settles is a **measured timeout**
reported as `BLOCKED` — "did not settle within 10.0s (still at …)" — rather than a
misleading "does not match".

No new navigation framework was built: `_INSPECT_VACANCY_PAGE_JS` is reused as the
settle probe, and `hh_browser_provider.navigate` / `open_hh_page` remain the provider's
own idiom.

## §G. 3-CYCLE LIVE TEST — **NOT RUN**

Blocked by the §12 STOP. Proven at unit level instead
(`test_three_search_cycles_do_not_leave_a_browser`): three cycles, each leaving
`lease_present False`, `headless_processes 0`, `port_listening False`, and a lease
file that does not exist.

## §H. CLEANUP

Measured at the only two moments this task touched the world: before and after the §1
baseline, both `CLEAN True` with `OWNED_TABS 0 / HEADLESS_PROCESSES 0 /
PORT_9111_FREE True / PROFILE_LOCK_FREE True / LEASE_ABSENT True`. The §9 mutation
probe restored `hh_vacancy_navigator.py` byte-for-byte (sha256
`9adf8f2b5b24de7619261bab3d17e71e1fd28a989d8e02271ed100b44b9b137e`, verified after
the write), and so did the ruff parity measurement for both files it touched.

## §I. TESTS

New file `tests/test_t726_search_only_headless.py` — **20 passed**. It covers all
eleven §14 requirements plus the §9 pairs:

| §14 requirement | test |
|---|---|
| acquires owned browser | `test_the_search_segment_acquires_an_owned_browser` |
| releases on success | `test_the_search_segment_releases_the_browser_on_success` |
| releases on exception | `test_the_search_segment_releases_the_browser_on_exception` |
| never calls submit | `test_the_search_segment_never_submits_and_never_sends` |
| returns real search results | `test_the_search_segment_returns_real_search_results` |
| waits past 2.5 s | `test_the_settle_waits_past_the_old_two_and_a_half_seconds`, `test_the_navigator_waits_and_then_reports_ready_on_a_slow_page` |
| redirect handled | `test_a_redirect_still_settles` |
| `about:blank` cannot count | `test_about_blank_cannot_count_as_a_loaded_vacancy` |
| full payload to preflight | `test_the_structured_preflight_receives_the_full_payload` |
| three cycles leave nothing | `test_three_search_cycles_do_not_leave_a_browser` |
| AST ownership guard green | `test_the_ownership_guard_still_sees_zero_unowned_launch_paths` |

Plus `test_the_old_fixed_sleep_plus_immediate_read_would_have_read_the_old_page`,
`test_a_query_that_cannot_settle_is_recorded_not_hidden`,
`test_the_search_segment_deduplicates_across_queries`,
`test_the_band_is_classified_from_the_employers_own_words`,
`test_search_only_uses_the_owned_segment_and_not_the_old_discovery`,
`test_the_autonomous_agent_calls_no_launch_primitive_directly`,
`test_a_page_that_never_settles_is_reported_not_assumed`,
`test_the_snapshot_reader_carries_the_two_added_material_fields`.

**Red/green (§9).** The regression is real, not asserted. With
`recheck = _settle_after_navigation(...)` mutated back to
`time.sleep(2.5)` + one immediate read, `test_the_navigator_waits_and_then_reports_ready_on_a_slow_page`
**failed**: `AssertionError: the navigator did not wait for the slow page — assert 2 >= 300`.
The file was restored byte-for-byte and the sha256 re-verified.

**Neighbour suites, all green:**

```
tests/test_stage41_vacancy_navigation.py  tests/test_stage44_second_vacancy_flow.py
tests/test_stage48_next_suitable_vacancy.py  tests/test_stage49_fetch_and_select_vacancy.py
tests/test_stage50_submit_selected_vacancy.py  tests/test_t723_structured_preflight.py
tests/test_t724_browser_lease.py  tests/test_t725_ownership.py      → 137 passed
tests/test_stage51_autonomous_agent.py  tests/test_stage52_real_autonomous_agent.py
tests/test_stage53_reply_visibility.py  tests/test_t77_geo_context.py
tests/test_stage32b_browser_lifecycle.py                            →  90 passed
tests/test_ble001_fail_closed.py                                    → 393 passed
```

**640 tests green** across everything this task touched (620 neighbouring + 20 new).
The full-suite run was **not** performed: §12 said STOP, and a ~20-minute run against
a `cli.py` another session is actively editing is exactly the moving target §12
exists to avoid.

**AST ownership guard:** `tools/.t725_launch_map.py --expect-zero` → rc `0`,
`PRODUCTION UNOWNED LAUNCH PATHS : 0`, 3 production primitive call sites, **9** owning
operations (was 8 — the new `hh_readonly_operation.py:312` operation is counted).

**Ruff parity** (`uvx ruff@0.16.6`, measured inside the repository):

| file | HEAD / before | now | delta |
|---|---|---|---|
| `hh_vacancy_navigator.py` | 12 | 12 | **0** |
| `hh_autonomous_agent.py` | 25 | 24 | **−1** |
| `hh_structured_preflight.py` | 0 | 0 | **0** |
| `hh_readonly_operation.py` | 0 | 0 | **0** |
| `tests/test_t726_search_only_headless.py` | — | 0 | new file, cleaned to zero |

No new rule families in any file. The −1 on the agent is one fewer `BLE001`, because
the new `_search_only_cycle` body has fewer bare `except`s than the discovery-based
one it replaced.

## §J. cli.py HASH CHECK

**`EXTERNAL_MODIFICATION_DETECTED`.**

| | sha256 | size | mtime |
|---|---|---|---|
| before | `d8bd9c75eb3413bc6abfc64f67f5f9d06dc52a6ce5ba6296833debfa368efe8e` | 307 501 B | `2026-09-29 15:06:03.906443` |
| after | `62ea9a81d3623729dcad688710fc22b2a5f9606adb866ebe714516e6774c8bc5` | 309 121 B | `2026-09-29 16:25:16.469136` |

`+1 620` bytes, `12 970` → `13 002` lines. Not one of the four sizes the T7.25
session saw being replayed, so this is a fresh external write. `0` occurrences of
`T7.26` in it, and the T7.25 patches are intact (`T7.25: 5`, `open_hh_page: 2`,
`ensure_work_tab: 0`), so it is another session's additive work.

This session never wrote `cli.py` — only `sed`/`grep` reads. It was not restored, and
`.t725_reapply_cli.py` was not run. A byte-exact copy is preserved as
`tools/.t726/cli.py.external_62ea9a81.py`. `cli.py` still imports and still routes
`autonomous once` → `autonomous_once_cmd`, so the SEARCH_ONLY fix stays reachable.

---

## ИТОГОВАЯ ТАБЛИЦА

| пункт | значение | измерено? |
|---|---|---|
| SEARCH_ONLY USES HEADLESS | **YES** (code) | unit only — live `NOT RUN` |
| HH SEARCH WORKS | **NOT RUN** | blocked by §12 STOP |
| VACANCIES READ | **NOT RUN** (≥ 10 required) | blocked by §12 STOP |
| NAVIGATION DETERMINISTIC | **YES** | measured + mutation-proven |
| CYCLE 1 / 2 / 3 CLEAN | **NOT RUN** | blocked by §12 STOP |
| VISIBLE WINDOWS | **0** | measured (§1 baseline; headless only) |
| FOCUS STOLEN | **NO** | measured (§1 baseline) |
| APPLICATIONS SENT | **0** | measured — no live run performed any |
| MESSAGES SENT | **0** | measured — no live run performed any |
| PRODUCTION UNOWNED PATHS | **0** | measured, rc 0 |
| CLI.PY MODIFIED BY THIS TASK | **NO** | measured |
| EXTERNAL CLI.PY CHANGE DETECTED | **YES** | measured |

## VERDICT: **FIX**

The §8 navigation defect is fixed and the fix is proven, including by mutation. The
SEARCH_ONLY ownership defect is fixed in code and proven at unit level. But the task's
live half — §5's real query-driven searches, §6's ten vacancy reads, §10's three live
cycles — was **not executed**, because §12 fired and the instruction was to stop.

**One decision is needed from Misha:** whether to re-run the live half now (the
`cli.py` freeze scalar must be re-taken first, and another session's version of
`cli.py` is the one on disk), or to leave T7.26 as code-complete and verify live in a
later task once `cli.py` is stable.
