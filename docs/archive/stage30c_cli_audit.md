# Stage 30C — CLI Audit (READ-ONLY RESEARCH)

> Generated: 2026-08-28 | Scope: ai_assistant/cli.py dispatch vs ai_assistant/* modules | Mode: READ-ONLY, no send/submit, no network, no commit

---

## 1. Capability command map (CLI → module → capability)

Legend: **READ-ONLY** = never sends, never submits, never mutates Gmail, never writes beyond optional local DB audit/queue cache where noted. **MUTATION-capable** = writes DB, changes tracking/review state, or clicks submit. All references are `cli.py:<line>` definitions or `main()` dispatch branches.

### 1a. Full dispatch inventory (every subcommand, handler, module edge)

| CLI subcommand | argparse registration `cli.py:<line>` | `main()` dispatch `cli.py:<line>` | Handler `cli.py:<line>` | Reached module(s) | Capability |
|---|---|---|---|---|---|
| `collect` | 1761 `collect_parser` | 1940 | `collect` 79 | `adapters.himalayas`/`weworkremotely`/`remoteok` + `normalizer.normalize_vacancy` + `db.save_vacancy` | collect vacancies |
| `analyze` | 1764 `analyze_parser` | 1942 | `analyze` 109 | `matcher.JobMatcher` + `candidate_profile.load_candidate_profile` + `db.list_vacancies` | matcher/prefilter |
| `analyze-deep` | 1769 `deep_parser` | 1944 | `analyze_deep` 173 | `job_analyzer.analyze_job_deep` + `job_analyzer.should_analyze` + `db.save_deep_analysis` | matcher deep |
| `prepare-applications` | 1774 `prep_parser` | 1946 | `prepare_applications` 281 | `job_analyzer` + `application_prep.prepare_application` + `application_qa.prepare_package_with_form` + `db.save_application_package` | job package |
| `list` | 1779 `list_parser` | 1948 `list_cmd(...)` | **MISSING** — calls `list_cmd` but no `def list_cmd` exists (grep `def ` 79-2526 finds none) — current code raises `NameError` | `db.list_vacancies` (intended) | view vacancies |
| `applications list` | 1786 `app_list_p` | 1951 | `applications_list` 414 | `application_tracking.list_applications` | view vacancies / tracking |
| `applications status` | 1790 `app_status_p` | 1953 | `applications_status` 424 | `application_tracking.get_application_status` + `get_application_history` | view / tracking |
| `applications move` | 1793 `app_move_p` | 1955 | `applications_move` 452 | `application_tracking.transition_application` | tracking mutation |
| `applications sync` | 1798 `app_sync_p` | 1957 | `applications_sync` 465 | `application_tracking.sync_application_tracking` | tracking sync |
| `queue` (no subcommand) | 1801 `queue_parser` | 1962 | `queue_list` 473 | `application_queue.generate_queue` + `application_queue.list_queue` | job package queue |
| `queue show` | 1807 `queue_show_p` | 1963 | `queue_show` 489 | `application_queue.get_queue_item` | job package queue |
| `queue --duplicates` | 1805 flag | 2059 second `queue` branch | `queue_duplicates` 2292/2526 | `vacancy_identity` + `application_queue` | view duplicates |
| `review list` | 1812 `review_list_p` | 1971 | `review_list` 676 | `application_review.list_application_reviews` | job package / review gate |
| `review show` | 1815 `review_show_p` | 1977 | `review_show` 601 | `application_review.create_application_review` / `get_application_review` | job package / review |
| `review approve` | 1817 `review_approve_p` | 1973 | `review_approve` 684 | `application_review.approve_review` | review gate mutation |
| `review reject` | 1819 `review_reject_p` | 1975 | `review_reject` 696/706 (duplicate def) | `application_review.reject_review` | review gate mutation |
| `review <id>` (bare) | 1823 `vacancy_stable_id_direct` + 1750 pre-insert | 1968-1980 | `review_show` 601 | `application_review` | review |
| `browser prepare` | 1827 `browser_prepare_p` | 1987 | `browser_prepare` 520 | `browser_executor.prepare_application_in_browser` | job package browser prep |
| `browser prepare-next` | 1831 `browser_prepare_next_p` | 1992 | `browser_prepare_next` 589 | `browser_executor.prepare_next_in_queue` | job package browser prep |
| `submit` | 1835 `submit_parser` | 2000 | `submit_vacancy` 720 | `browser_executor.submit_application_in_browser` | auto-apply submit |
| `submit-next` | 1841 `submit_next_parser` | 2007 | `submit_next_in_queue` via 2012 dispatch | `browser_executor.submit_next_in_queue` | auto-apply submit |
| `submissions list` | 1848 `submissions_list_p` | 2015 | `submissions_list` 798 | `db.list_submissions` + `db.list_verifications` | submission verification |
| `submissions show` | 1850 `submissions_show_p` | 2017 | `submissions_show` 844 | `db.get_submission` + `db.get_verification` | submission verification |
| `submissions verify` | 1852 `submissions_verify_p` | 2019 | `submissions_verify` 921 | `submission_verifier.verify_submission` | submission verifier |
| `submissions recover` | 1854 `submissions_recover_p` | 2021 | `submissions_recover` 976 | `submission_recovery.inspect_submission_state` | submission recovery |
| `submissions reconcile` | 1856 `submissions_reconcile_p` | 2023 | `submissions_reconcile` 1032 | `submission_recovery.reconcile_submission_state` | submission recovery |
| `submissions audit` | 1858 `submissions_audit_p` | 2025 | `submissions_audit` 1059 | `submission_recovery.get_submission_audit` | submission audit |
| `dashboard` | 1861 `dashboard_parser` | 2042 | `dashboard` 1101 | `application_dashboard.build_dashboard` | view dashboard |
| `dashboard --actions` | 1863 flag | 2035 | `dashboard_actions` 1153 | `application_dashboard.get_dashboard_actions_only` | view actions |
| `dashboard --queue` | 1864 flag | 2037 | `dashboard_queue` 1175 | `application_dashboard.get_dashboard_queue` | view queue |
| `dashboard --history` | 1865 flag | 2039 | `dashboard_history` 1190 | `application_dashboard.get_dashboard_history` | view history |
| `dashboard show` | 1867 `dashboard_show_p` | 2031 | `dashboard_show` 1208 | `application_dashboard.get_dashboard_show` | view detail |
| `dashboard canonical` | 1869 `dashboard_canonical_p` | 2033 | `dashboard_show_canonical` 1325 | `vacancy_identity` + `application_queue` | view canonical |
| `identity show` | 1874 `identity_show_p` | 2044 | `identity_show` 2111 | `vacancy_identity.resolve_vacancy_identity` | view identity |
| `identity sync` | 1876 `identity_sync_p` | 2046 | `identity_sync` 2170 | `vacancy_identity.sync_identity_from_vacancies` | identity sync |
| `identity queue` | 1877 `identity_queue_p` | 2048 | `identity_queue` 2233 | `vacancy_identity` + `application_queue` | view identity queue |
| `audit` | 1881 `audit_parser` | 2075 fallback | `audit` 2320 | `application_integrity.run_integrity_audit` | view integrity |
| `audit --errors` | 1882 flag | 2070 | `audit_errors` 2361 | `application_integrity` | view integrity |
| `audit --warnings` | 1883 flag | 2072 | `audit_warnings` 2389 | `application_integrity` | view integrity |
| `audit --json` | 1884 flag | 2068 | `audit_json` 2417 | `application_integrity` | view integrity |
| `audit show` | 1888 `audit_show_p` | 2064 | `audit_show` 2470 | `application_integrity` | view integrity |
| `audit canonical` | 1890 `audit_canonical_p` | 2066 | `audit_canonical` 2494 | `application_integrity` + `vacancy_identity` | view integrity |
| `duplicates` | 1893 `duplicates_parser` | 2053 | `duplicates_list` 2184 | `vacancy_identity` | view duplicates |
| `hh-message list` | 1899 `hh_message_list_p` | 2077 | `hh_message_list` 1436 | `hh_message_reply.fetch_hh_dialogs_readonly` | reply HH |
| `hh-message preview` | 1902 `hh_message_prev_p` | 2079 | `hh_message_preview` 1459 | `hh_message_reply.fetch_hh_conversation_readonly` + `classify_message` + `generate_reply` | reply HH |
| `hh-message classify` | 1907 `hh_message_classify_p` | 2082 | `hh_message_classify` 1599 | `hh_message_reply.fetch_hh_conversation_readonly` + `classify_message` | reply HH |
| `email list` | 1914 `email_list_p` | 2088 | `email_list` 1510 | `email_message_reply.fetch_incoming_emails_readonly` | reply email |
| `email preview` | 1916 `email_prev_p` | 2090 | `email_preview` 1530 | `email_message_reply.fetch_incoming_emails_readonly` + `generate_email_reply` | reply email |
| `email classify` | 1920 `email_classify_p` | 2092 | `email_classify` 1640 | `email_message_reply.fetch_incoming_emails_readonly` + `classify_email` | reply email |
| `email link` | 1923 `email_link_p` | 2094 | `email_link` 1673 | `email_message_reply.fetch_incoming_emails_readonly` + `link_email_to_vacancy` | reply email |
| `gmail status` | 1929 `gmail status` | 2099 | `gmail_status` 1564 | `gmail_readonly_connector.gmail_provider_status` | Gmail |
| `system-info` | 1932 | 2103 | `system_info` 1714 | stdlib `platform`, `importlib` only | diagnostic |

### 1b. Capability roll-up (requested seven areas)

**collect vacancies**
- `python -m ai_assistant.cli collect [--sources himalayas weworkremotely remoteok]` → `collect` 79 → `adapters/*` + `normalizer` + `db` — **MUTATION-capable** (writes `vacancies` via `save_vacancy` 102; network fetch; `init_db` 80). READ aspect is listing; write is bounded to new vacancies only.

**view vacancies**
- `python -m ai_assistant.cli list [--limit 20] [--state STATE]` → intended `list_cmd` — **READ-ONLY** — *BROKEN*: `main()` 1949 calls `list_cmd` but `grep -n "def "` finds no such definition; at runtime `NameError: name 'list_cmd' is not defined` (verified against `cli.py` 1-2556). Every other view path works:
- `python -m ai_assistant.cli applications list/status` 1786/1790 → READ-ONLY; `applications move/sync` are MUTATION
- `python -m ai_assistant.cli dashboard [--actions|--queue|--history]`, `dashboard show`, `dashboard canonical` 1861-1870 → READ-ONLY (`application_dashboard` 45, `vacancy_identity` 59)
- `python -m ai_assistant.cli duplicates` 1893 → READ-ONLY (`vacancy_identity` 59)
- `python -m ai_assistant.cli identity show/sync/queue` 1872-1879 → `show`/`queue` READ-ONLY, `sync` MUTATION (creates canonical rows)
- `python -m ai_assistant.cli audit [--errors|--warnings|--json|--tracked]` + `audit show/canonical` 1881-1891 → READ-ONLY (`application_integrity` 54)
- `python -m ai_assistant.cli submissions list/show/recover/audit` 1846 → READ-ONLY; `verify`/`reconcile` are MUTATION (see 1c)
- `python -m ai_assistant.cli queue [--top N] [--status S]` and `queue show` 1801/1807 → READ-ONLY in intent (but `queue_list` 473 calls `generate_queue` which persists `queue` rows — side-effect write; flagged as mutation side-effect)

**matcher / prefilter**
- `python -m ai_assistant.cli analyze [--top 20] [--profile PATH] [--persist]` → `analyze` 109 → `matcher.JobMatcher` 14 + `candidate_profile` 15 + `db` 18 — **READ-ONLY** by default (stdout only); **MUTATION-capable** only with `--persist` (UPDATE `vacancies.match_*` 141-149).
- `python -m ai_assistant.cli analyze-deep [--top 20] [--profile PATH] [--force]` → `analyze_deep` 173 → `job_analyzer` 175 → `db.save_deep_analysis` 240-248 — **MUTATION-capable** (persists `deep_analysis` rows; `init_db` 177). Selection itself is read-only (matcher scoring 198-203), but command writes.
- **prefilter** (`ai_assistant/prefilter.py`) — NO CLI edge. Only reachable via legacy `pipeline.py:7 import prefilter` and `pipeline.check_vacancy` 51. No `args.command` branch references it. See §2.

**job package** (prepare → queue → review → browser → submissions)
- `python -m ai_assistant.cli prepare-applications [--top 20] [--force]` → 281 → `job_analyzer` + `application_prep` 285 + `application_qa` 379 → `db.save_application_package` 390 — **MUTATION-capable** (writes package rows; also triggers read-only form extraction via `application_qa.prepare_package_with_form`).
- `python -m ai_assistant.cli queue` / `queue show` / `queue --duplicates` → as above — READ-ONLY ranking, but with queue-generation persistence side-effect.
- `python -m ai_assistant.cli review list/show` → READ-ONLY (`application_review` 34, handlers 601/676). `review approve` 2017 and `review reject` 1819 → **MUTATION-capable** (`application_review.approve_review`/`reject_review`; only flips review row `status` to APPROVED/REJECTED, prints `APPLICATION WILL NOT BE SUBMITTED AUTOMATICALLY` 189 — no browser submit).
- `python -m ai_assistant.cli browser prepare <id> [--force] [--real]` 1827 and `browser prepare-next` 1831 → `browser_executor.prepare_application_in_browser` 521 — **READ-ONLY / no-submit** (explicit `SUBMIT NOT CLICKED` 578, `APPLICATION NOT SENT` 579). Writes a `browser_sessions` row (`READY_FOR_REVIEW`/`BLOCKED`) but does NOT call `submit_application`.
- `python -m ai_assistant.cli submissions list/show/recover/audit` → READ-ONLY. `submissions verify <id>` 1852 → `submission_verifier.verify_submission` 44 — **MUTATION-capable** (updates verification + may call `verify_and_apply` 961/968 to move tracking to `APPLIED`/`READY_TO_APPLY` — flagged as DAG-guarded mutation). `submissions reconcile` 1856 → MUTATION only `VERIFIED → APPLIED` 1034.

**auto-apply** (HH form fill + submit pipeline)
- **Stage 21 dual-mode orchestration** (`auto_apply_modes.py:resolve_mode`/`classify_form`/`run_auto_apply`) — **INTENTIONALLY UNWIRED**: no `import auto_apply_modes` in `cli.py` (verified `grep` absent; `tests/test_stage30c_*` assert absence 247). There is NO `auto-apply` subcommand. Phase-2A comment block 1579-1596 documents gap: "requires browser submit + review gate + controlled submit; no safe REVIEW-only interpretation; AUTO never default."
- **Wired MUTATION path**: `python -m ai_assistant.cli submit <vacancy_stable_id> --confirm-submit [--force]` 1835 → `submit_vacancy` 720 → `browser_executor.submit_application_in_browser` 726 — **MUTATION-capable / DANGEROUS** (clicks submit; handler 720-725 refuses without `--confirm-submit`, prints `No browser action performed` 724). `python -m ai_assistant.cli submit-next --confirm-submit [--top N]` 1841 → same escalation via `submit_next_in_queue` 2012. See §3 for safety gates.

**reply (HH / email)** — Stage 30C Phase 1 (1436-1571) + Phase 2A (1599-1707)

All are **READ-ONLY, no send**:
- `python -m ai_assistant.cli hh-message list [--cdp-url URL] [--url-substring SUB]` → 1436 → `_resolve_hh_evaluate` 1417 → `hh_message_reply.fetch_hh_dialogs_readonly` 1441 — prints `READ-ONLY — nothing sent` 1455. Never imports `process_auto_reply`/`send_auto_reply`/`can_auto_send` (forbidden strings absent — `cli.py` passes `test_stage30c_*` forbid checks).
- `python -m ai_assistant.cli hh-message preview <conversation_id>` → 1459 — same read path + `classify_message`/`generate_reply` 1496-1497; explicit gap note when `messages==[]` ("chatik iframe isolated-world helper is known Phase-1 gap" 1471-1476) then `PREVIEW ONLY` 1506.
- `python -m ai_assistant.cli hh-message classify <conversation_id>` → 1599 (Phase 2A) — stateless `classify_message` only 1630; never `send`.
- `python -m ai_assistant.cli email list [--max-emails N]` → 1510 → `_resolve_email_transport` 1427 → `email_message_reply.fetch_incoming_emails_readonly` 1514 — `READ-ONLY — nothing sent (EmailSendGate always blocks any send)` 1526.
- `python -m ai_assistant.cli email preview <idx>` → 1530 — stateless `classify_email`/`generate_email_reply` 1553-1554; `PREVIEW ONLY — EmailSendGate blocks any send` 1560; never `EmailSendGate(` nor `process_incoming_email` (forbid).
- `python -m ai_assistant.cli email classify <idx>` → 1640; `email link <idx>` → 1673 → plus `link_email_to_vacancy` 1698 — both READ-ONLY, never `EmailSendGate`.
- Injection: all six handlers accept `evaluate_fn`/`transport` injection and delegate to `make_cdp_evaluate` (prefill_execute) or `GmailReadOnlyConnector.transport()` when no fake is injected (1419/1433) — tests inject fakes, live path stays `Runtime.evaluate` / `gmail.readonly`.

**Gmail**
- `python -m ai_assistant.cli gmail status` → 1564 → `gmail_readonly_connector.gmail_provider_status` 1566 — **READ-ONLY** (prints `scope: gmail.readonly` 1569; returns 0 only if `READY` else 1). No `gmail send/modify/delete` CLI exists. `GmailReadOnlyConnector` 91 uses `gmail.readonly` scope only; `_messages_list` 91 lists, never sends. See §3.

**System diagnostic**
- `python -m ai_assistant.cli system-info` → 1714 → stdlib `platform`/`importlib` only — **READ-ONLY, no network, no DB** (`test_stage30c_system_info` verifies no `init_db` touched 79-84, no env toggle 96-103, prints `READ-ONLY` 1744 + `Python:` 1719 + `platform:` 1721 + `app_version: (not defined` placeholder 1743 in this repo).

### 1c. MUTATION-capable vs READ-ONLY summary table (requested ordering)

| Capability | Subcommand(s) | Mode |
|---|---|---|
| collect vacancies | `collect` | MUTATION (fetch + save_vacancy) |
| view vacancies | `list` (broken), `applications list/status`, `dashboard*`, `duplicates`, `identity show/queue`, `audit*`, `submissions list/show/recover/audit`, `queue` | READ-ONLY (queue generation has persistence side-effect) |
| matcher | `analyze` | READ-ONLY default; MUTATION only with `--persist` |
| matcher deep | `analyze-deep`, `prepare-applications` (deep part) | MUTATION (save_deep_analysis) |
| job package | `prepare-applications`, `queue`, `review list/show`, `browser prepare*`, `submissions*` | mix: prepare MUTATION, review list/show READ-ONLY, browser prepare READ-ONLY/no-submit, submissions verify/reconcile MUTATION (guarded) |
| auto-apply submit | `submit --confirm-submit`, `submit-next --confirm-submit` | MUTATION — dangerous submit click (gated) |
| review gate transition | `review approve`, `review reject`, `applications move`, `applications sync`, `identity sync` | MUTATION (status flips only, no submit) |
| reply HH | `hh-message list/preview/classify` | READ-ONLY |
| reply email | `email list/preview/classify/link` | READ-ONLY |
| Gmail | `gmail status` | READ-ONLY |
| System | `system-info` | READ-ONLY |

---

## 2. Dead / unconnected modules — modules that exist under ai_assistant/ but are NOT reachable from any CLI command (with one-line reason each).

> Method: `grep -n "from .\|import "` in `cli.py` plus lazy `from .X import` inside handlers (79-2526); report lists any `ai_assistant/*.py` present on disk (`ai_assistant/**/*.py` glob) whose name never appears as an import target or whose only reference is a comment. Legacy paths are included.

| Module `ai_assistant/<name>` | Status | One-line reason |
|---|---|---|
| `auto_apply_modes.py` | **DEAD / intentionally unwired** | Stage 21 dual-mode `resolve_mode`/`classify_form`/`run_auto_apply` — never imported in `cli.py`; forbid string `auto_apply_modes` asserted absent in `tests/test_stage30c_*` 247; Phase-1/2A comment 1579 documents intentional gap (AUTO never default). |
| `hh_controlled_submit.py` | **DEAD / intentionally unwired** | `ControlledSubmitter` + `submit_with_controlled_gate` — never imported/dispatched; forbid `hh_controlled_submit` absent; submit path uses `browser_executor.submit_application_in_browser` 726 instead (gated). |
| `hh_submission.py` | **DEAD / intentionally unwired** | `submit_application` (HH direct submit) — never imported; forbid `hh_submission` absent; superseded by `browser_executor` + `submit --confirm-submit`. |
| `hh_human_submission.py` | **DEAD / intentionally unwired** | `submit_with_human_confirmation` — never imported; forbid `hh_human_submission` absent; human confirmation is via `review approve` 2017 (no browser click). |
| `prefill_plan.py` | **DEAD** | `Plan`/`prepare_package_with_form` helpers — not imported in `cli.py`; only reached transitively via `application_qa.prepare_package_with_form` 379 inside `prepare_applications`; no CLI edge. |
| `prefill_orchestrate.py` | **DEAD** | Orchestrator for HH extraction→plan→execute — never imported in `cli.py`; no `args.command` branch references it. |
| `application_review_gate.py` | **DEAD** | `ApplicationReviewGate`/`build_review` — never imported in `cli.py`; logic lives in `application_review` + `browser_executor` rather than direct gate CLI. |
| `prefilter.py` | **DEAD** | `check_vacancy` salary/title filters — only imported by legacy `pipeline.py:7`; no CLI command reaches it; matcher now drives filtering. |
| `bot.py` | **DEAD / legacy** | Legacy Telegram bot — no `import bot` in `cli.py`; standalone `if __name__` entry, not a CLI subcommand. |
| `pipeline.py` | **DEAD / legacy** | Legacy `run_pipeline` (collect→prefilter→matcher) — only comment reference `submit pipeline` 1580; no `import pipeline` in `cli.py`. |
| `core.py` | **DEAD / utility** | Helpers only; never imported in `cli.py` (substring `core` hits are `score` false positives). |
| `wellfound_scraper.py` | **DEAD** | Unused Wellfound scraper — never imported in `cli.py`; not in `SOURCES` dict 72 (only himalayas/weworkremotely/remoteok). |
| `linkedin.py` | **DEAD** | LinkedIn adapter stub — never imported/dispatched. |
| `hh_extractor.py` | **DEAD** | HH form extractor used by `application_qa` internally — no direct CLI import; CLI reaches it only transitively via `prepare_package_with_form`. |
| `outreach_note.py` | **DEAD** | Outreach note generator — never imported in `cli.py`. |
| `adapters/__init__.py` | trivial | Package init only. |
| — | — | **NOT dead** (for contrast): `prefill_execute.py` is wired ( `make_cdp_evaluate` 16 → HH handlers 1419), `application_qa.py` 379, `application_prep.py` 285, `job_analyzer.py` 175, `matcher.py` 14, `normalizer.py` 13, `db.py` 18, `config.py` 33, `candidate_profile.py` 15, `browser_executor.py` 521/726/2012 (via `browser`/`submit`), `vacancy_identity.py` 59, `application_queue.py` 474/490, `application_tracking.py` 35, `submission_verifier.py` 44, `submission_recovery.py` 978/1034, `application_dashboard.py` 45, `application_integrity.py` 54, `hh_message_reply.py`/`email_message_reply.py`/`gmail_readonly_connector.py` 17, `schema.py` 12 — all have at least one `args.command` edge and are therefore reachable. |

> Note on `list` command breakage: the `list` registration 1779 is itself reachable, but its handler is **dangling** — `main()` 1949 calls the undefined `list_cmd(...)`. This is not a dead module but a wired command with a missing definition; it will crash at runtime and is therefore an effective wire gap on a read-only capability.

---

## 3. DANGEROUS mutation/send/submit paths — list explicitly and separately: AUTO apply/reply paths, HH send/submit modules, email send gate, Gmail send/modify/delete, controlled submit, human-confirmed submission. Mark which are intentionally unwired / which have a CLI entry (e.g. `submit --confirm-submit`, `review approve`). Be factual.

> All forbidden substrings (`auto_apply_modes`, `process_auto_reply(`, `run_auto_apply(`, `send_auto_reply(`, `can_auto_send(`, `confirm_live_send(`, `EmailSendGate(`, `hh_controlled_submit`, `hh_human_submission`) are **absent** from `cli.py` (verified `python -c "…forbid…"` in acceptance; `tests/test_stage30c_phase2a_runtime_wiring.py:248` and `test_stage30c_system_info.py:60` enforce). Where a CLI entry exists, the safety gate is noted.

### (a) AUTO apply / reply paths

| Path | Module + function | CLI entry? | Status |
|---|---|---|---|
| HH AUTO reply — `process_auto_reply`, `send_auto_reply`, `can_auto_send`, `is_safe_for_auto_reply`, `confirm_live_send` | `hh_message_reply.py` (AUTO path) | **INTENTIONALLY UNWIRED — NO CLI ENTRY** | Never imported/called in `cli.py` (Phase 1 handlers 1436-1571 and Phase 2A 1599-1707 explicitly avoid them; tests monkeypatch them to `_rising` 106/126). The REVIEW-only handlers `fetch_hh_dialogs_readonly`/`fetch_hh_conversation_readonly` + `classify_message`/`generate_reply` are wired instead. Kill-switch `HH_AUTO_REPLY_ENABLED` is never toggled (`test_phase1_does_not_toggle_auto_environment` 232, `test_phase2a_does_not_toggle_auto_environment` 232). |
| Stage 21 dual-mode apply — `auto_apply_modes.run_auto_apply`, `resolve_mode`, `classify_form` | `auto_apply_modes.py` | **INTENTIONALLY UNWIRED — NO CLI ENTRY** | No `import auto_apply_modes` in `cli.py`; Phase-2A not-wired gap comment 1579-1592; `auto-apply` string absent from `cli.py` (`test_auto_submit_module_not_wired_into_cli` 252). |

### (b) HH send / submit modules

| Path | Module + function | CLI entry? | Status |
|---|---|---|---|
| `hh_controlled_submit.ControlledSubmitter` / `submit_with_controlled_gate` | `hh_controlled_submit.py` | **INTENTIONALLY UNWIRED — NO CLI ENTRY** | Never imported; `hh_controlled_submit` forbid absent (phase2a 257). The controlled gate lives inside `browser_executor.submit_application_in_browser`’s own checks rather than this module. |
| `hh_submission.submit_application` | `hh_submission.py` | **INTENTIONALLY UNWIRED — NO CLI ENTRY** | Never imported; forbid `hh_submission` absent (checked in phase2a 258). |
| `hh_human_submission.submit_with_human_confirmation` | `hh_human_submission.py` | **INTENTIONALLY UNWIRED — NO CLI ENTRY** | Never imported; forbid `hh_human_submission` absent. Human confirmation is via `review approve`/`reject` (no browser click). |
| `browser_executor.submit_application_in_browser` / `submit_next_in_queue` | `browser_executor.py:1135/1528` | **WIRED — MUTATION-CAPABLE with explicit gate** `python -m ai_assistant.cli submit <id> --confirm-submit` 1835/2000 and `submit-next --confirm-submit` 1841/2007 | Handler 720-725 bails with exit 1 if `not confirm_submit` and prints `Submit confirmation required. Use --confirm-submit to proceed. No browser action performed.` 722-724 and duplicate guard at dispatch 2001-2004 / 2008-2011. On success prints `SUBMIT CLICKED: YES` / `APPLICATION SENT: YES` 737. **DANGEROUS** — the only HH submit path with a CLI entry; intentionally gated. Also `browser_executor.py:1135` enforces its own review-gate checks before clicking. |
| `browser_executor.prepare_application_in_browser` / `prepare_next_in_queue` | `browser_executor.py:494 etc` | **WIRED but NOT submit** `browser prepare` / `browser prepare-next` 1827/1831 | Handler 520 prints `SUBMIT NOT CLICKED` 578 + `APPLICATION NOT SENT` 579 + `STATUS NOT CHANGED TO APPLIED` 580 and sets `READY_FOR_REVIEW`/`BLOCKED` only. **Not dangerous** (no click). |

### (c) Email send gate

| Path | Module + function | CLI entry? | Status |
|---|---|---|---|
| `email_message_reply.EmailSendGate` / `process_incoming_email` / `send_email` | `email_message_reply.py` | **INTENTIONALLY UNWIRED — NO CLI ENTRY** | Never instantiated/called in `cli.py`; `EmailSendGate(` forbid absent; Phase-1/2A handlers 1510-1707 print `EmailSendGate blocks any send` 1560/1526 and never construct the gate (`test_email_handlers_never_gate_send_or_mutate_state` 216 patches it to `_GateBoom`). Only `fetch_incoming_emails_readonly` + stateless `classify_email`/`generate_email_reply`/`link_email_to_vacancy` are wired (READ-ONLY). |

### (d) Gmail send / modify / delete

| Path | Module + function | CLI entry? | Status |
|---|---|---|---|
| `gmail_readonly_connector.GmailReadOnlyConnector` + `gmail_provider_status` | `gmail_readonly_connector.py` | **WIRED but READ-ONLY only** `python -m ai_assistant.cli gmail status` 1927/2098 | Scope is `gmail_readonly_connector.GMAIL_READONLY_SCOPE` 1569 = `https://www.googleapis.com/auth/gmail.readonly` only; connector method `_messages_list` 91 uses `service.users().messages().list` 94 with `fetch_incoming_emails_readonly` transport — never `send`/`modify`/`delete`. No CLI subcommand for send. Tests use a fake `_messages_list` stub. |

### (e) Controlled submit (explicit)

See (b) `hh_controlled_submit` row: **intentionally unwired** — the generic controlled-submit primitive exists as separate module but is not a CLI entry; the gated submit primitive is `browser_executor.submit_application_in_browser` behind `--confirm-submit`.

### (f) Human-confirmed submission

| Path | CLI entry? | Status |
|---|---|---|
| `hh_human_submission.submit_with_human_confirmation` | **INTENTIONALLY UNWIRED — NO CLI ENTRY** | No CLI wiring. Human confirmation in the current CLI is modeled as `review approve` 2017 / `review reject` 1819 (both MUTATION-capable but only flip review `status` to APPROVED/REJECTED, never click submit — handler 689 prints `APPLICATION WILL NOT BE SUBMITTED AUTOMATICALLY.`). The transition `APPROVED` → `SUBMITTED/APPLIED` still requires an explicit separate `submit --confirm-submit` click, preserving a two-step human gate. `application_review_gate.py` is itself dead (see §2). |

**Summary checklist for the independent verifier**:
- `auto_apply_modes` absent ✓ (phase1 forbid)
- `process_auto_reply(` etc absent ✓
- `EmailSendGate(` absent ✓
- `hh_controlled_submit` / `hh_submission` / `hh_human_submission` absent ✓
- Only submit path wired is `submit --confirm-submit` via `browser_executor` (gated, prints safety banner) — factual.
- `submit`/`submit-next` are the only DANGEROUS mutation paths with CLI entries; all others are intentionally unwired.

---

## 4. Concrete next-step priorities: a ranked list of 3-5 follow-up tasks (each 1-3 lines) that would give the project the most benefit, ordered by value/risk. Prefer safe, non-destructive improvements (e.g. closing wire gaps on read-only capabilities, dead-module cleanup under review, adding read-only command coverage) over mutating ones.

1. **Fix the `list` wire gap (highest value, zero risk, pure read-only).** Define `def list_cmd(limit, state)` (or inline `list_vacancies` + pretty-print) at `cli.py:~414` and keep dispatch 1949. Add `test_cli_list_readonly` mirroring `gmail status` style. This closes the only broken read-only view command with no mutation.

2. **Close the `hh-message preview` / `classify` chatik-iframe gap with a read-only isolated-world helper (safe, read-only).** Keep `make_cdp_evaluate` 1417 for main-frame but add an optional `fetch_hh_conversation_via_chatik_iframe` that CDP-evaluates inside the `chatik` iframe’s isolated world; preserve PREVIEW-ONLY prints and never call `send`. Add a `hh-message diagnose` read-only probe. This is the documented Phase-1 gap 1590-1592 and unblocks live preview without adding any send path.

3. **Add read-only `prefilter explain` + surface `prefilter`/`pipeline` coverage (safe).** Wire `prefilter.check_vacancy` as `python -m ai_assistant.cli explain <vacancy_stable_id>` that prints why a vacancy was filtered (salary/title/excluded) without touching DB; leave mutation of `prefilter` thresholds config-only. This gives matcher/prefilter parity and removes a dead-module without deleting legacy `pipeline.py`/`prefilter.py` (archive under review).

4. **Dead-module hygiene under review (low risk, high clarity).** Tag `bot.py`/`pipeline.py`/`wellfound_scraper.py`/`linkedin.py`/`outreach_note.py` as `legacy/` or `deprecated/` with a one-line `README` and skip them from coverage, rather than deleting; keep `auto_apply_modes`/`hh_controlled_submit`/`hh_submission`/`hh_human_submission` **intentionally unwired** but add a top-of-file comment `# Stage 30C: intentionally not wired to CLI — see docs/stage30c_cli_audit.md §3`. No code deletion without maintainer approval.

5. **Harden the dangerous `submit` gate with a second human confirmation token (mutation safety, still gated).** Extend `submit --confirm-submit` to also require `--confirm-token <vacancy_stable_id>` (echo back) or an env `CONFIRM_SUBMIT=1` check inside `submit_vacancy` 720 before calling `browser_executor.submit_application_in_browser` 726; log `SUBMIT CLICKED: YES` with timestamp to `artifacts/submissions.log`. Keeps the CLI entry but raises the bar from a Boolean flag to an explicit echo, matching the two-step `review approve` → `submit` workflow already documented in §1b.

---

### Appendix — How this report was produced (READ-ONLY)

- Read `ai_assistant/cli.py` 1-2556 in full; enumerated every `subparsers.add_parser` (1758-1932) and every `args.command` branch (1940-2107).
- Grepped `from .` / `import` in `cli.py` (79-2529) to build the import → module reachability graph; cross-checked `ai_assistant/**/*.py` glob (37 files) against that set.
- Verified forbidden substrings absent via `open(cli.__file__).read()` check matching `tests/test_stage30c_cli_wiring.py:247` and `tests/test_stage30c_phase2a_runtime_wiring.py:248`.
- Inspected handlers `hh_message_list` 1436, `hh_message_preview` 1459, `email_list` 1510, `email_preview` 1530, `hh_message_classify` 1599, `email_classify` 1640, `email_link` 1673, `gmail_status` 1564, `system_info` 1714 to confirm READ-ONLY prints and injected-fake path; confirmed mutation paths `submit_vacancy` 720 (`--confirm-submit` gate) and `browser_executor` 1135.
- Cross-referenced existing Stage 30C tests (`test_stage30c_cli_wiring.py`, `test_stage30c_phase2a_runtime_wiring.py`, `test_stage30c_system_info.py`) for expected dispatch and safety assertions.
- **No production code was edited, no import/call added, no send/submit executed, no commit/push performed** — per Stage 30C constraints.

