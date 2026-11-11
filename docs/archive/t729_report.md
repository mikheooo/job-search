# T7.29 — Restore the daily run and make the correspondence check report an outcome

Date: 2026-10-02. Branch `chore-ruff-cleanup`, HEAD `98be2ea10cb1f8d5042d9caafce5509266f3f71a`
(2026-09-22). No commit, no push. Every claim below is either measured in this session or
explicitly marked as not measured.

---

## 1. What was wrong, and the confirmed cause

### 1.1 The scheduler (`0x800710E0`)

`JobSearch_Daily_Digest` was inspected cell by cell before anything was touched:

| element | value |
|---|---|
| action | `powershell.exe -ExecutionPolicy Bypass -File …\scripts\run_job_search_production.ps1` |
| working directory | `C:\Users\Misha\Documents\job-search` |
| principal | `Misha` / `InteractiveToken` / `Limited` |
| trigger | one daily trigger, `StartBoundary 2026-08-31T10:00:00+07:00`, enabled |
| settings | `MultipleInstances=IgnoreNew`, `ExecutionTimeLimit=PT1H`, `DisallowStartIfOnBatteries=false`, `UseUnifiedSchedulingEngine=true` |
| **`StartWhenAvailable`** | **`false`** |
| `LastRunTime` | 2026-10-02 13:06:55 |
| `LastTaskResult` | `0x800710E0` |

`0x800710E0` does not explain itself, and it is not an operator refusal. Correlating
`LastRunTime` with the boot/shutdown timeline (Kernel-Boot / Kernel-General) gives the real
story:

| date | machine up at 10:00 +07? | delivery |
|---|---|---|
| 09-29 | yes (boot 08:33, shutdown 17:16) | **delivered** 10:03:42 |
| 09-30 | no (booted 10:58) | none |
| 10-01 | no (booted 11:54) | none |
| 10-02 | no (booted 13:02) | none |

**Confirmed cause:** the trigger fell due while the machine was off, and
`StartWhenAvailable=false` suppressed the catch-up. `LastTaskResult` was left holding
`0x800710E0`. All 30 historical runs started at exactly `03:00 UTC` = `10:00 +07`, and every
missing day matches a day the machine was not up at 10:00.

That the action, the principal and the mechanism are sound was **proved, not assumed**, with a
temporary diagnostic task (`T729_Diag_Mechanism`, same principal, same settings, same action
shape, only the trigger differed): `LastTaskResult=0x00000000`, payload ran as `pid=29740` in
the correct working directory. The task was unregistered afterwards.

### 1.2 Who actually delivers

The last successful delivery is `2026-09-29T03:03:42.541017+00:00` (batch
`digest_batch:25d11a0a3a482c04`, Telegram `message_id: 562`, 5 vacancies marked `DELIVERED`).
`logs/job_search/job_search_production.log` records that run as:

    command: C:\Users\Misha\Documents\job-search\.venv\Scripts\python.exe
             C:\Users\Misha\AppData\Local\hermes\profiles\jobs\scripts\job_search_fetcher.py

That is `runner.py`'s `[sys.executable, script_path]` shape, so **the scheduled task is the real
deliverer**. `production-health` reports 58 delivery records, `Consecutive Failures: 0`,
circuit `CLOSED`, `Pending Undigested Vacancies: 1000`.

### 1.3 The duplicate that bypasses the lock and the circuit

There is a second, hidden deliverer: the **Hermes cron job `b1edb5ae094c`**
(`profiles/jobs/cron/jobs.json`, `expr "0 10 * * *"`, `script job_search_fetcher.py`,
`no_agent: true`). It runs the same script directly, so it goes through neither
`SingleInstanceLock` nor `ConsecutiveFailureTracker` — it is invisible to `production-health`
and can fire while the canonical path is circuit-broken. Its `catch_up` fired on 2026-10-02 at
14:34:21 with `lateness_seconds 16461.8` and left an output file of 244 bytes (banner only),
recorded as `status='completed'`. **Reported, not changed** — see §6.

---

## 2. Changes

### 2.1 Scheduler — one setting

`StartWhenAvailable` set to `true` on `JobSearch_Daily_Digest`. Nothing else.

* XML exported **before** the change: `tools/.t729/JobSearch_Daily_Digest.before_t729.xml`.
* Re-exported before applying and compared with the 15:22 export: identical (no drift).
* XML exported after: `tools/.t729/JobSearch_Daily_Digest.after_t729.xml`.
* Diff of the two normalised exports: **exactly one added line** —
  `<StartWhenAvailable>true</StartWhenAvailable>`.
* Trigger, action, working directory, principal, `IgnoreNew`, `PT1H`,
  `DisallowStartIfOnBatteries` all unchanged. `NextRunTime 2026-10-03 10:00:00`.
* The canonical path `scripts/run_job_search_production.ps1 → python -m ai_assistant.cli
  production-run` was **not touched**. `run_job_search_production.ps1` does not use `os.execv`.
* Temporary diagnostic task removed; `Get-ScheduledTask -TaskName *T729*` returns nothing.
* No duplicate **scheduled task** runs the fetcher: `Hermes_Gateway`, `Hermes_WebUI` and
  `JobSearchDashboard` are logon-triggered VBS helpers only.

### 2.2 Hermes fetcher — the useful runtime-only change, kept but fixed

Canonical `integrations/hermes/job_search_fetcher.py` and the installed copy differed by one
hunk: a hand-over shim that re-executes under the project venv interpreter (3.11), because
Hermes Cron launches the script under Hermes's own Python 3.14, which cannot load the venv's
compiled binary deps. The shim is **useful and must be kept** — but it used `os.execv`, and that
is what made the Hermes cron runs meaningless.

`os.execv` on Windows spawns a detached child and exits the current process immediately, so the
caller loses the child's stdout and its exit status and records a false "completed". Reproduced
deterministically (`tools/.t729/execv_*.py`):

| variant | wall | exit | child output |
|---|---|---|---|
| `os.execv` | 2 s | 0 | truncated after `B: child start`; the 4 s sleep and the final line **lost** |
| `subprocess.run` + `sys.exit(rc)` | 8 s | 0 | complete, real status propagated |

Fix, applied to the canonical copy:

```python
_PROJECT_PY = JOB_SEARCH_ROOT / ".venv" / "Scripts" / "python.exe"
if _PROJECT_PY.is_file() and os.path.abspath(sys.executable).lower() != os.path.abspath(str(_PROJECT_PY)).lower():
    import subprocess

    _handover = subprocess.run(
        [str(_PROJECT_PY), os.path.abspath(__file__), *sys.argv[1:]],
        check=False,  # the child's status is propagated by hand on the next line
    )
    sys.exit(_handover.returncode)
```

Harmless when the script is launched by `runner.py`, which already uses `sys.executable` == the
project venv python: the condition is false and the shim is a no-op. `py_compile` OK; the file
stays LF-only.

### 2.3 Hermes adapter hook — the anchor had rotted, and the hook body was dead code

`python -m ai_assistant.cli hermes sync` could not have repaired the adapter:

* the syncer looked for the anchor `        # --- Update prompt callbacks ---`, which no longer
  exists anywhere in the deployed adapter;
* so it would have appended `ERROR_ADAPTER_INSERTION_POINT_NOT_FOUND` and left
  `routing_capable=false`;
* and it would have overwritten the installed fetcher with the canonical bytes, **deleting the
  useful 3.14 → 3.11 shim** and re-breaking the Hermes cron path.

Two repository-side fixes:

* `integrations/hermes/telegram_adapter_hook.py`
  * new `ADAPTER_HOOK_ANCHOR = "        cb = self._callback_ctx(query)\n"`. The hook is injected
    **after** that line, because its body reads both `data` and the `cb` context dict that
    `_handle_callback_query` builds on it;
  * the hook body was rebuilt for the current upstream method. The old body referenced
    `query_message` and `query_chat_id`, which the rewritten method does not define, so it raised
    `NameError` inside its own `try/except` and answered a false error toast. It now builds the
    raw callback dict from `query` and `cb.get("chat_id")`, matching
    `TelegramFeedbackProcessor.process_callback_query`'s contract.
* `ai_assistant/hermes_integration.py` imports and uses `ADAPTER_HOOK_ANCHOR`; the injection
  direction is corrected to *after* the anchor.

### 2.4 The root cause of the drift: the Hermes updater discards local changes

Hermes was running a self-update during this task (PID 25916, started 15:12,
`hermes ... update --yes --gateway --branch main --keep-stash`). Its log says:

    → Local changes detected — stashing before update...
    Saved working directory and index state On main: hermes-update-autostash-20261002-082026
    → Found 785 new commit(s)
    ℹ️  Local changes were stashed before updating and were NOT re-applied (--keep-stash).
       Stash ref: 3aafcc80ffb4a229f0f2ba360a6dc574d8a004d2

`plugins/platforms/telegram/adapter.py` was rewritten at 15:20:35, which is exactly why the
anchor vanished and the capability probes stopped matching. **Re-injecting once is not a durable
fix:** the next Hermes update will remove the hook again. `sync` is idempotent, so the durable
answer is to run it after each update (§6).

While the updater was alive no write was made into `hermes-agent/**`; `EXTERNAL_MODIFICATION_DETECTED`
was recorded in `tools/.t729/EXTERNAL_MODIFICATION_DETECTED.txt`. The updater then reported
`✓ Code updated!`, and the adapter hash stayed stable for 40 minutes before any write.

### 2.5 Correspondence check — an outcome instead of a bare zero

`ai_assistant/hh_autonomous_agent.py`. Four values, defined once:

```python
MSG_CHECK_CHECKED = "CHECKED"   # the list was read; the count is a confirmed number
MSG_CHECK_SKIPPED = "SKIPPED"   # the mode does not process correspondence (SEARCH_ONLY)
MSG_CHECK_BLOCKED = "BLOCKED"   # the transport works but the check cannot be trusted
MSG_CHECK_ERROR = "ERROR"       # the check itself failed (no transport, read error)
```

`AutonomousCycleResult` gained `messages_check_status`, `messages_check_reason`,
`messages_checked_at`. The default is **fail-closed** `ERROR` / `"message check did not complete"`,
so a cycle that dies before step 4 cannot leave a row that reads like a finished check.

`_process_messages` now decides in this order:

1. **SEARCH_ONLY → `SKIPPED`**, before the transport is even built. Search without processing
   correspondence is the mode's definition, and it says so instead of reporting a zero.
2. **no transport → `ERROR`** with the reason (a factory that cannot find a tab is an error, not
   "no messages").
3. **read failed → `ERROR`**: both a raised transport error and the `{"error": ...}` object that
   `fetch_hh_conversations_list_readonly` returns for unparseable output.
4. **a non-empty read → `CHECKED`.** Those dialogs came off a messages page; that is its own
   proof.
5. **an empty read** is where the silent zero used to live. Before a zero may be called
   confirmed: ask `check_hh_session_authenticated` **in the main frame** (T7.28 A: the chatik
   transport cannot answer "who am I" and invents a refusal on a healthy session), and require
   that the page really is a messages surface — reusing the existing single rule
   `_HH_MESSAGES_PATTERN`. An unauthenticated session, an unverifiable session, or a tab that is
   not a messages page all yield **`BLOCKED`**, never a confirmed zero.

The gate is consulted **only on an empty read**, deliberately: a non-empty read already proves the
page was readable, and asking earlier would have broken every existing mock for no gain.

The outcome travels to both places the task asked for:

* **the report** — `result.summary` now reads `Messages Checked: N (CHECKED - dialog list read,
  N dialog(s))`, and `autonomous once --json` dumps the whole model;
* **the stored cycle record** — the three fields ride in `log_json` via `result.model_dump()`, so
  no schema migration was needed. `_search_only_cycle` sets `SKIPPED` explicitly and names it in
  its summary.

No auto-replies were enabled for any of this. `cli.py` was **not** edited, so its freeze holds.

### 2.6 `hermes sync` rewrote the whole adapter — the text-mode newline trap

`sync_hermes_integration` already synced the fetcher with `read_bytes`/`write_bytes`, but the
adapter went through `read_text`/`write_text`. On Windows that is not a no-op round-trip:
`read_text` applies universal-newline translation and `write_text` re-expands every `\n` to
`os.linesep`. The upstream adapter is **LF-only**, so the first sync rewrote every one of its
line endings. Measured, not inferred:

| adapter | bytes | CRLF | bare LF |
|---|---|---|---|
| before the sync (`adapter.py.bak`) | 440168 | 0 | 7380 |
| after the sync | 449319 | 7414 | 0 |

Python does not care, but it is a 7380-line mutation of a file we do not own, and it is the
same class of defect as the documented `cli.py` trap. Fixed by reading and writing the adapter
as bytes, mirroring the fetcher path. The deployed adapter was then restored from the pre-sync
backup and re-synced with the fixed code:

| adapter | bytes | sha256 | CRLF | bare LF |
|---|---|---|---|---|
| restored + re-synced | 441905 | `d13eb158…` | 0 | **7414** |

(7414 = 7380 upstream lines + 34 hook lines.)

Making the read byte-exact exposed a second, latent defect: the anchor was matched **with** its
`\n` terminator, so it silently missed every CRLF file. The real upstream adapter is LF, so
production was unaffected — but `test_sync_operation_is_idempotent` writes its fake adapter
with `write_text` and therefore CRLF, and the byte-exact read turned that into a failure
(`junit_t729_step5_4.xml`: 434 tests, 1 failure, attributed to exactly this). The anchor is now
matched **without** its terminator and the hook is re-terminated with the file's own convention,
so the injection is newline-agnostic and never rewrites a byte it was not asked to. Pinned by
`test_sync_preserves_the_adapters_line_endings` (parametrised LF/CRLF): it asserts the sync adds
the hook, keeps the module compilable, introduces no foreign line ending, and rewrites nothing
before the insertion point.

---

## 3. Tests

### 3.1 The browser stand-in in the SEARCH_ONLY tests (step 5)

The three red SEARCH_ONLY tests passed `evaluate_fn` into `AutonomousConfig`, but
`_search_only_cycle` never consults it: it runs the *owned* operation
(`search_operation_report`), which acquires the HH browser through the ownership layer. The
stand-in was therefore never reached, the cycle dialled the real CDP port 9111 and `conftest`
blocked the connection.

Fixed by replacing the operation **at its own seam** with a canned report
(`canned_search` fixture), and adding a `cdp_tripwire` fixture that records any attempt to reach
a browser. The tests now assert that the cycle really ran (`canned_search` non-empty,
`discovered_count == 1`) and that no transport was reached for (`cdp_tripwire == []`) — so
"nothing was written" is no longer a vacuous claim. Whether the search itself works is T7.26's
subject and stays pinned against a fake browser session in
`tests/test_t726_search_only_headless.py`. **The real-port-9111 protection in `conftest` is
untouched.**

Two consequences that had to be faced honestly rather than papered over:

* `test_the_chat_guard_records_the_draft_and_sends_nothing` called `_process_messages` directly
  under `SEARCH_ONLY` and expected the *second* guard to fire. With §2.5 that state is
  unreachable — the mode check refuses first. The test was repointed at an **unrecognised** mode
  (`"FUL"`), where the second guard still has to hold: a typo is not FULL and must not buy a
  reply either. The stronger SEARCH_ONLY claim is pinned separately in the new file.
* `tests/test_stage89_2_external_runtime_persistence.py` built its fake adapter around the old
  anchor. Its fixture and its idempotency test now use `ADAPTER_HOOK_ANCHOR`. It already used an
  isolated fake Hermes directory, so it never wrote to the real runtime.

### 3.2 New file — `tests/test_t729_message_check_status.py` (15 tests)

Covers the five checks the task asked for, plus the vocabulary and the "writes nothing" property:
SEARCH_ONLY never reaches a transport; a missing transport is an explicit ERROR; a read that
raises is an ERROR; a transport error object is not an empty list; an empty list on a messages
page with a verified session is a confirmed zero; an empty list off a messages page is BLOCKED;
an unauthenticated session is BLOCKED; an unverifiable session blocks rather than passes;
messages that are read give the right counters and status; the status is always one of the four
words; the status reaches the stored cycle row and the report; and a BLOCKED or ERROR check
writes no message event, no audit and no notification.

### 3.3 Results

Every verdict below is read from its junit file, never from `$?`.

| run | junit | verdict |
|---|---|---|
| narrow set (12 files) | `tools/.t729/junit_t729_step5_3.xml` | **167 passed, 0 failed, 0 errors** |
| new file alone | — | 15 passed |
| affected set after the bytes fix (5 files) | `tools/.t729/junit_t729_step5_6.xml` | **447 passed, 0 failed, 0 errors** |
| stage89.2 alone, after the fix | `tools/.t729/junit_t729_step5_5.xml` | 8 passed, 0 failed |
| clean full regression | `tools/.t729/junit_full_t729_20261002_clean.xml` | **3380 tests, 2 failed, 0 errors, 0 skipped** (§3.4) |
| ruff parity | — | no new findings; two explainable deltas (see below) |

Three intermediate runs are recorded too, and each is attributed rather than hidden:

* `junit_t729_step4_narrow_1.xml` — 14 failures. The first attempt at §2.5 consulted the session
  gate *before* the read, so it intercepted every existing mock. All 14 traced to that one cause and
  were fixed by moving the gate to the empty-read branch; `junit_t729_step4_narrow_2.xml` is the
  green run after the correction.
* `junit_t729_step5_4.xml` — 434 tests, **1 failure** (`test_sync_operation_is_idempotent`), caused
  by §2.6: the byte-exact read made the `\n`-terminated anchor miss the CRLF fake adapter that the
  test writes with `write_text`. Fixed by making the anchor newline-agnostic;
  `junit_t729_step5_5.xml` is green.
* The first clean full run was **killed on purpose** at 16:30, 12m51s in, because it had started at
  16:17 — *before* the §2.6 code change. A verdict that predates a code change is not evidence, so
  that run was discarded and restarted rather than reported.

### 3.4 The clean full run

`tools/.t729/junit_full_t729_20261002_clean.xml` — **3380 tests, 2 failures, 0 errors, 0 skipped,
3723.3 s**. Both failures are pre-existing and neither is T7.29's:

| failure | why it is not this task's |
|---|---|
| `test_candidate_master_resume::test_a_project_sync_does_not_touch_resume_or_interview_claims` | `C:\Users\Misha\reels_bot\README.md` was modified externally at 2026-10-02 15:16:18, so the KB's stored `source_sha256` (`5a2d1d76…`) no longer matches the file (`f289ed7d…`) and `sync_project_claims` correctly refreshes the claim. Reproduced in isolation. It is T5's test; tightening it would change T5 semantics. |
| `test_report_line_refs::test_the_report_has_no_new_symbol_mismatches` | Red at pristine HEAD — it is the `docs/ble001_triage.md` ratchet, not a regression. |

Everything that was red before is now green: the three `test_t78_search_only` tests (fixed in
§3.1) and the two `inspect.getsource` contamination failures that polluted the T8 run. **No new
failure was introduced by T7.29.**

**Ruff parity, measured per rule against a HEAD snapshot** (`tools/.t729/ruff_base/`), file by
file. Aggregate: HEAD `BLE001 27, F401 8, S110 7, PIE804 7, I001 5, DTZ005 3, SIM102 2, FURB167 2,
F841 2, SIM114 1, RUF013 1, E902 1, G201 1` (67) → working tree `BLE001 27, F401 7, S110 7,
PIE804 7, I001 5, DTZ005 3, SIM102 2, FURB167 2, F841 2, SIM114 1, RUF013 1, G201 1` (65). The two
deltas are both explainable and both benign:

* `tests/test_stage89_2_…py` **F401 8 → 7** — an *improvement*: `HERMES_CALLBACK_HOOK_MARKER` was
  imported but unused, and the new regression test now uses it.
* `tests/test_t78_search_only.py` **E902 1 → 0** — a snapshot artifact, not a finding: the file is
  untracked at HEAD (`git status` shows `A`), so `ruff_base/` could not contain it and ruff reported
  an IO error for the missing snapshot path.

Every other file is byte-for-byte identical in its findings. Two findings I had introduced were
removed rather than declared: `PLW1510` on the new `subprocess.run` (fixed with an explicit
`check=False`) and a redundant broad catch around `check_hh_session_authenticated`, which already
fails closed on its own (BLE001 finding #28) — duplicating a tested contract bought nothing. The
new test file passes ruff clean.

---

## 4. Current statuses

| item | before | now |
|---|---|---|
| `JobSearch_Daily_Digest` `StartWhenAvailable` | `false` | **`true`** |
| `LastTaskResult` | `0x800710E0` | unchanged until the next run |
| `NextRunTime` | 2026-10-03 10:00 | 2026-10-03 10:00 |
| Hermes integration | `DRIFTED` (fetcher + adapter) | **`HEALTHY`** |
| `routing_capable` | `false` | **`true`** |
| installed fetcher vs canonical | drifted | **hash-identical** `008a41da…` (15925 B) |
| deployed adapter | `239cbe96…` (LF, no hook) | `d13eb158…` (441905 B, **0 CRLF / 7414 LF**, hook present) |
| `hermes sync` repeat runs | — | `Actions Taken: []` — measured, twice (§2.6) |
| `production-health` alerts | 2 (digest failure + DRIFTED) | **1** (digest failure) |
| `production-health` overall | `DEGRADED` | `DEGRADED` |
| last successful delivery | 2026-09-29T03:03:42Z | **unchanged** |
| `application_queue` | 0 | 0 (untouched) |
| `hh_message_events` | 38, last `seen_at` 2026-09-30 | unchanged (nothing reclassified) |
| `cli.py` | `e0921aa1b62276f1…`, 316808 B, `CR CR LF` 6593 | **identical** |

**The daily delivery is NOT declared restored.** What is restored is the *mechanism*: the trigger
now survives the machine being off. Whether the next run delivers can only be seen when it
happens. Green tests and matching file hashes are not evidence of delivery.

---

## 5. What is proven, and where

**Proven offline (no browser, no network, no external send):**
* the `os.execv` defect and the `subprocess` replacement — a controlled experiment, not a reading;
* the adapter hook injection — `tools/.t729/verify_hook_injection.py` reads the live adapter
  **as bytes**, applies exactly the inject-or-skip decision `sync_hermes_integration` makes, and
  proves the result compiles, sits inside `_handle_callback_query` between the anchor and the
  dispatch table, matches the canonical hook code byte-for-byte, binds `query_message` before
  use, no longer mentions `query_chat_id`, has `data` and `cb` bound before it, and is idempotent
  on a second pass — while leaving the file on disk untouched (`RESULT: OK`, rc 0);
* the text-mode newline mutation — the adapter's byte / CRLF / bare-LF counts before the first
  sync, after it, and after the restore + re-sync (§2.6);
* the newline-agnostic anchor — `test_sync_preserves_the_adapters_line_endings`, parametrised
  over LF and CRLF;
* the narrow tests and the 15 new ones (see §3.3);
* ruff parity against the HEAD snapshot, measured per rule;
* the scheduler settings and the exact one-line XML diff;
* the missing day timeline against the boot/shutdown events.

**Verified on the installed runtime:**
* `StartWhenAvailable=true` is now the stored setting, read back from the live task;
* `hermes sync` pushed both files: `hermes status` = `HEALTHY`, `routing_capable` = YES, installed
  fetcher hash-identical to canonical, deployed adapter compiles under the project venv;
* the second `hermes sync` was a no-op — idempotency is measured, not asserted;
* `production-health` lost the DRIFTED alert.

**Still requires live verification (not done, and not claimed):**
1. That the 10:00 trigger actually catches up after a missed window. Needs a day the machine is
   off at 10:00 and then a run: `Get-ScheduledTaskInfo -TaskName JobSearch_Daily_Digest` should
   show `LastTaskResult=0x00000000` and a `LastRunTime` shortly after boot, and
   `telegram_delivery_records` a new `DELIVERED` batch. `NumberOfMissedRuns` is expected to
   become non-zero — that counter is the observable for this fix.
2. That a real HH session produces `CHECKED` rather than `BLOCKED` through the new gate. Needs a
   headless read-only run against the live messages page; the branch to watch is "empty read on a
   real messages page with an authenticated session".
3. That the injected hook actually routes a real `fb:` tap. Needs a tap on a live digest message
   (out of scope: it is an external interaction).

---

## 6. Limitations and things left open

* **The hook is fragile by construction.** It is an injection into a file Hermes rewrites on every
  self-update, and the updater stashes local changes with `--keep-stash` without restoring them.
  Recommendation (not applied — it changes the production pipeline): run
  `python -m ai_assistant.cli hermes sync` as part of the daily run or right after a Hermes
  update, so the hook is re-injected automatically. `sync` is idempotent, so this is safe to
  repeat.
* **A useful runtime-only change was found and NOT restored.** The change the updater stashed is
  not our hook — it is a 12-line **kanban** hook (`/kanban`, `/board`, `/tasks` →
  `kanban_tg_plugin`). `C:\Users\Misha\tools\kanban_tg_plugin.py` still exists, but the hook is
  gone from the live adapter, so those commands are currently dead in Telegram. Recovery is
  available without guessing: `git -C <hermes-agent> stash show -p
  3aafcc80ffb4a229f0f2ba360a6dc574d8a004d2` (entry `stash@{0}`). Left alone: it is another
  project's change, restoring it writes to the Hermes runtime, and the file has moved 785 commits
  since the patch was made. **Misha's decision.**
* **The Hermes cron duplicate is documented, not disabled.** Job `b1edb5ae094c` still fires at
  `0 10 * * *` outside the lock and the circuit breaker. Disabling it is a delivery-behaviour
  change and was left for an explicit decision.
* **`test_candidate_master_resume.py::test_a_project_sync_does_not_touch_resume_or_interview_claims`
  fails, and it is not T7.29's.** Reproduced in isolation. Cause: `C:\Users\Misha\reels_bot\README.md`
  was modified externally at 2026-10-02 15:16:18, so the KB's stored `source_sha256`
  (`5a2d1d76…`) no longer matches the file (`f289ed7d…`), and `sync_project_claims` correctly
  updates the claim. The test snapshots *all* claims, including the project claims the sync is
  designed to refresh. Not fixed: it is T5's test and tightening it would change T5 semantics.
* **Two failures in the T8 full run were contamination, not regressions.** That run started before
  this task's edits; `tests/test_t726_search_only_headless.py` and `tests/test_t77_geo_context.py`
  both use `inspect.getsource`, which re-reads the changed file using pre-edit line numbers. The
  clean run below is the one to trust.
* **`sync` keys on the marker, so a change to the hook *body* will not propagate.** The adapter
  step injects only when `HERMES_CALLBACK_HOOK_MARKER` is absent. If the hook code is edited
  while the marker line stays, `sync` reports `Already in sync` and the deployed adapter keeps the
  old body. Nothing depends on this today — the marker and the body changed together in §2.3 —
  but it is a trap for the next edit. A content hash of the hook would close it.
* **Two defects in my own probe were found and fixed; both produced a false green.**
  `verify_hook_injection.py` hashed the adapter through `read_text`, so its `sha256_before`
  described the newline-normalised *string*, not the file on disk; and it injected
  unconditionally, so on an already-healthy adapter it double-injected and reported
  `idempotent_second_pass_noop: False`. The probe now reads bytes and mirrors the syncer's own
  inject-or-skip decision, and it exits non-zero on any failed assertion.
* `state.db` was never mutated by this task; `application_queue` and `hh_message_events` were not
  cleaned or reclassified.

---

## 7. Rollback

Everything is reversible, and nothing was committed.

| what | how |
|---|---|
| scheduled task | re-export the current XML for the record, then either `Set-ScheduledTask -TaskName JobSearch_Daily_Digest -Settings` with `StartWhenAvailable=$false`, or `Register-ScheduledTask -Xml (Get-Content tools/.t729/JobSearch_Daily_Digest.before_t729.xml -Raw)` |
| installed Hermes fetcher | `cp -p tools/.t729/hermes-bak-20261002-160536/job_search_fetcher.py.installed.bak <installed path>` (was `f26cc4fd…`) |
| deployed Hermes adapter | `cp -p tools/.t729/hermes-bak-20261002-160536/adapter.py.bak <deployed path>` — the upstream **LF** adapter *without* the hook (`239cbe96…`); then `hermes sync` re-injects. The CRLF variant the first sync produced is kept for the record as `adapter.py.post_sync_crlf.bak` (`83c9592c…`) |
| `hermes_integration.py` bytes fix (§2.6) | restore `read_text`/`write_text` for the adapter step. **Not recommended** — it re-introduces the 7380-line CRLF rewrite |
| repository files | revert the 6 modified files and delete `tests/test_t729_message_check_status.py`; the pre-work snapshot of the whole working tree (including the index) is in `C:\Users\Misha\Documents\job-search-checkpoints\t729-20261002-151611\` |
| diagnostic task | already unregistered — nothing to undo |

`tools/.t729/` holds every export, probe, backup and junit from this task.

---

## 8. Verdict

The scheduler cause is confirmed and fixed with a one-line change that preserves the canonical
path: `StartWhenAvailable=true`, read back from the live task, with a one-line XML diff. The Hermes
integration is reconciled and healthy — `HEALTHY`, `routing_capable=YES`, fetcher hash-identical to
canonical, `sync` measured idempotent — its fetcher divergence adopted rather than overwritten, and
the silent no-op that made the Hermes cron runs meaningless is fixed and proven. Syncing it also
exposed and fixed a defect of my own making: the adapter step used text mode and rewrote all 7380
upstream line endings, and the anchor silently missed CRLF files. Both are fixed, the deployed
adapter restored to upstream-LF + hook, and the behaviour pinned by a parametrised test. The
correspondence check can no longer report a bare zero: it reports CHECKED, SKIPPED, BLOCKED or ERROR
with a reason and a time, into the report and into the stored cycle row.

| | |
|---|---|
| narrow set (5 files) | **447 passed, 0 failed, 0 errors** |
| clean full regression | **3380 tests, 2 failed, 0 errors** — both pre-existing, neither T7.29's |
| ruff | no new findings (two explainable deltas, §3.4) |
| `cli.py` | untouched — `e0921aa1b62276f1…`, 316808 B, `CR CR LF` 6593 |

Offline evidence: complete. Installed-runtime evidence: complete for settings, sync and integration
health. Live evidence: **three checks outstanding**, listed in §5 — the daily delivery is not
claimed as restored.

EXTERNAL SENDS=0
PROJECT_STATE.md updated: YES
