# job-search

Personal job-search automation system. Collects remote vacancies, scores them
against a candidate profile, prepares tailored applications, and — behind
explicit safety gates — can fill and submit HH.ru application forms, reply to
HH messages, and prepare email replies.

Everything that touches the outside world (browser, HH, email) is gated:
default mode is REVIEW (human decision required); AUTO modes are strictly
opt-in via environment kill-switches and never guess facts.

## 1. What it does

- Collects vacancies from remote and tech job boards (habrcareer, remoteok,
  weworkremotely, himalayas; legacy: linkedin, wellfound).
- Normalizes and deduplicates them into a canonical identity store.
- Prefilters by stop-words / required words / minimum salary.
- Scores vacancies against `candidate_profile.json` (hard constraints +
  weighted matching).
- Runs LLM deep analysis with an offline fallback.
- Builds application packages (resume adaptation + cover letter), truth-only
  Q&A for screening questions.
- Maintains a lifecycle state machine (DISCOVERED → ... → APPLIED) with a
  priority queue and a dashboard.
- Extracts real HH application forms from the DOM (read-only).
- Controlled application flow: plan → prefill → human review gate → single
  gated submit → read-only verification.
- HH messaging: read-only dialog discovery/reading, truth-only reply
  generation, REVIEW by default, limited opt-in AUTO.
- Email: read-only discovery + REVIEW-only reply previews (sending is
  physically blocked in the current MVP); Gmail read-only connector.

## 2. Architecture

```
collectors (adapters/*, linkedin, wellfound_scraper)
    → normalizer.py            (Vacancy schema normalization)
    → prefilter.py             (stop-words / salary gate)
    → matcher.py               (JobMatcher hard constraints + score)
    → job_analyzer.py          (LLM deep analysis + fallback)
    → vacancy_identity.py      (canonical vacancies + aliases, dedup)
    → application_prep.py      (application package: resume + cover letter)
    → application_qa.py        (truth-only screening answers, validation)
    → application_queue.py     (priority queue)
    → application_review.py    (review record)
    → browser_executor.py      (Playwright adapter, read-only form extraction)
    → prefill_plan/execute/orchestrate.py  (React-safe CDP prefill, URL guards)
    → application_review_gate.py           (fingerprinted human review gate)
    → hh_submission.py + hh_human_submission.py + hh_controlled_submit.py
                                     (11 hard gates → 1 click → verification)
    → auto_apply_modes.py      (REVIEW/AUTO orchestration over the above)
    → hh_message_reply.py      (HH dialogs read-only + truth-only replies)
    → email_message_reply.py   (email discovery + REVIEW-only previews)
    → gmail_readonly_connector.py (read-only Gmail transport, ADC)
    → application_tracking.py / application_dashboard.py / application_integrity.py
                                     (lifecycle, dashboard, integrity audit)
    → cli.py                   (single entry point for all commands)
```

## 3. Stage map (1-30, compact)

| Stage | Focus | Key modules / outcome |
|-------|-------|----------------------|
| 1-3 | MVP pipeline (legacy) | collect -> prefilter -> LLM -> Telegram; `pipeline.py`/`core.py`/`bot.py` legacy, not in current flow |
| 4-9 | Collectors & scoring | adapters, normalizer, prefilter, matcher, deep analysis |
| 10-12 | Application lifecycle | queue, review, prep |
| 13-14 | Browser + identity | browser_executor, canonical identity, submission verifier/recovery |
| 15 | Integrity audit | `cli audit` layer |
| 16 | Audit CLI hardening | exit codes, JSON contract, `--tracked` |
| 17 | Real HH form extraction | `hh_extractor.py`, `application_qa.py` (truth-only Q&A) |
| 18 | Authenticated session | `HH_STORAGE_STATE` -> Playwright |
| 19 | Safety finding | GET on `applicant/vacancy_response` can auto-submit; navigation forbidden, read-only only |
| 20 (A-K) | Controlled application | manual capture, normalization, prefill plan/execute/orchestrate, review gate, single-click submit, human-confirmed submit, read-only verify |
| 21 | Auto-apply modes | `auto_apply_modes.py` (REVIEW default / AUTO opt-in) |
| 22-26 | HH messaging | `hh_message_reply.py`: read-only dialogs, truth-only replies, REVIEW default, limited AUTO (kill switch + allowlist + cap 3/run) |
| 27 | Email reply MVP | `email_message_reply.py` (REVIEW-only, `EmailSendGate` always blocks) |
| 28-29 | Gmail read-only | `gmail_readonly_connector.py` (ADC, `gmail.readonly`), `gmail_provider_status` diagnostics |

## 4. HH Submission Safety Gates (11 Strict Multi-Layer Gates)

HH form submission is protected by 11 fail-closed gates (unified execution via `HHSubmissionGates` and `execute_hh_submission` in `ai_assistant/hh_submission.py`, fully wired into all submission paths; see `docs/audit_remediation_log.md`):

1. `GATE_SUBMIT_ALLOWED`: Environment / config kill-switch `SUBMIT_ALLOWED` must be explicitly enabled (`true`/`1`/`yes`) unless in dry_run mode.
2. `GATE_REVIEW_APPROVED`: Application review in DB must exist and have status `ReviewStatus.APPROVED`.
3. `GATE_FINGERPRINT_MATCH`: Form fingerprint from live browser DOM snapshot must match approved review fingerprint.
4. `GATE_URL_DOMAIN`: Current URL hostname must strictly match `hh.ru` or `*.hh.ru` (verified via `urllib.parse`).
5. `GATE_VACANCY_MATCH`: Numeric vacancy ID in URL must match expected `source_job_id` extracted from `vacancy_stable_id`. Missing or non-numeric IDs fail closed.
6. `GATE_PROFILE_LOADED`: CandidateProfile must be loaded and non-empty.
7. `GATE_COVER_LETTER_READY`: Cover letter text must be present and at least 10 characters long.
8. `GATE_NO_UNKNOWN_QUESTIONS`: Form fields must not contain unfillable or review-requiring questions (`requires_review=True`).
9. `GATE_NOT_ALREADY_APPLIED`: Application tracking status must be in whitelist (`DISCOVERED`, `ANALYZED`, `READY_TO_APPLY`); no prior completed/ambiguous submission records in DB (`SUBMITTED`, `AMBIGUOUS_POST_SUBMIT`, `CONFIRMED`, `VERIFIED`, `FAILED`).
10. `GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT`: No concurrent `SUBMITTING` status in DB; vacancy/review must not be present in current in-memory submission session set.
11. `GATE_HUMAN_CONFIRMED`: Explicit human confirmation flag (`--confirm-submit`) required unless in dry_run mode.

## 5. Setup

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium
```

Configuration lives in `ai_assistant/.env` (or environment):

| Variable | Purpose |
|----------|---------|
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | LLM provider (OpenAI-compatible) |
| `TG_BOT_TOKEN`, `TG_CHAT_ID` | legacy Telegram notification |
| `VACANCIES_FILE`, `DB_FILE` | data paths |
| `CANDIDATE_PROFILE_FILE` | path to candidate profile JSON |
| `HH_STORAGE_STATE` | path to HH authenticated Playwright storage state (never committed) |
| `HH_CDP_URL` | CDP endpoint for manual capture (`http://127.0.0.1:<port>`) |
| `HH_APPLY_MODE` | `AUTO` enables auto-apply mode (default REVIEW) |
| `HH_AUTO_REPLY_ENABLED` | `true` enables limited HH auto-reply (default off) |
| `SUBMIT_ALLOWED` | Master kill-switch latch for submitting HH applications (default: false) |
| `STOP_WORDS`, `REQUIRED_WORDS`, `MIN_SALARY`, `BATCH_LIMIT` | prefilter tuning |

## 6. Candidate profile configuration

The runtime truth source is `candidate_profile.json` (project root). It is a
**local runtime user configuration and is intentionally NOT tracked in git**
(see `.gitignore`: `*.json`). Copy `candidate_profile.example.json` to
`candidate_profile.json` and fill it in:

```bash
cp candidate_profile.example.json candidate_profile.json
```

All reply/answer generation is truth-only: it reads only this file (plus the
resume text); missing facts always route to HUMAN_REVIEW, never guessed.

## 7. CLI

```
python -m ai_assistant.cli collect <source...>        # fetch vacancies (habrcareer, himalayas, remoteok, weworkremotely)
python -m ai_assistant.cli list [--limit 20]           # list stored vacancies
python -m ai_assistant.cli analyze                     # basic analysis
python -m ai_assistant.cli analyze-deep                # LLM deep analysis
python -m ai_assistant.cli prepare-applications        # build packages
python -m ai_assistant.cli applications                # lifecycle status
python -m ai_assistant.cli queue                       # priority queue
python -m ai_assistant.cli review                      # review records
python -m ai_assistant.cli ui [--port 8000]            # launch interactive web dashboard
python -m ai_assistant.cli browser                     # browser prep/execute
python -m ai_assistant.cli submit / submit-next        # gated submit
python -m ai_assistant.cli submissions                 # submissions + verify/recover
python -m ai_assistant.cli dashboard                   # dashboard
python -m ai_assistant.cli identity / duplicates       # canonical identity
python -m ai_assistant.cli audit [--tracked] [--json]  # integrity audit (read-only)
python -m ai_assistant.cli production-run [--dry-run]  # fail-closed scheduled pipeline
python -m ai_assistant.cli production-health [--json]  # operational health + adapter failures
python -m ai_assistant.cli production-control status [--json]  # persistent circuit state
python -m ai_assistant.cli production-control resume [--json]  # explicit operator resume after review
```

`production-run` opens a persistent circuit after
`PRODUCTION_FAILURE_ALERT_THRESHOLD` consecutive live failures (default: 3).
While open, later live runs stop before discovery or delivery with exit code `5`.
An offline `production-run --dry-run` probe remains available and never clears
live failure evidence; only the explicit `production-control resume` action does.

Exit codes: `0` healthy, `1` warnings, `2` errors, `3` invalid usage; a blocked
`production-run` uses `5` (`PRODUCTION_CIRCUIT_OPEN`).

## 8. Tests

```bash
pytest tests -q          # 1,408 tests passed in the latest full offline run
```

The canonical `tests/` suite contains 106 test files covering core modules and
staged regressions through Stage 92, including integrity,
controlled HH flows, messaging, digest crash consistency, production
operations, ingestion robustness, matching quality, and profile calibration.
An autouse fixture redirects SQLite, vacancies, and logs to per-test temporary
paths, blocks all non-loopback sockets and all `urllib` requests;
known CDP ports are blocked too, while ephemeral loopback remains available to
in-process ASGI test clients. Transport behavior must use fakes/mocks. Production dry-run also
skips creation and invocation of Himalayas, RemoteOK, WeWorkRemotely, and Habr
Career adapters. Disaster-recovery snapshot folders are not part of this count.

The Stage 20D regression fixture `artifacts/hh_manual_form_snapshot.json`
(real HH application-form DOM structure, no cookies/tokens/personal data;
self-described as "structure only") is tracked in git, so the suite runs
unchanged in a clean clone.

## 9. Safety model

- **Truth-only**: never invent facts; missing data → HUMAN_REVIEW.
- **Fail-closed**: any uncertainty blocks the action (0 mutations).
- **REVIEW by default**: Автономный режим — prepare-only с 2.0; до этого отправлял без подтверждения. Любая отправка требует одобрения человека и запуска через `application runner next --confirm-submit`.
- **Production discovery is review-only**: the scheduled `production-run`
  entry point does not import or call `auto_apply_modes.run_auto_apply`.
- **SendGate / EmailSendGate**: REVIEW never sends; email sending is
  physically absent from the codebase.
- **11 submission gates** (Stage 20I) before the single submit click; no
  retry after `SUBMISSION_UNKNOWN`.
- **Race-condition re-check** before any AUTO send (fingerprint re-read).
- **Dedup**: one submit per vacancy per session; message dedup keyed to the
  last incoming message; email dedup by provider message_id (or explicit
  fallback fingerprint).
- **No navigation to HH response URLs** (Stage 19): capture attaches to an
  already-open user tab only.
- **Browser mutation protection**: React-safe native setters, URL guards
  before/after, no clicks in prefill; extractors are read-only.
- **Gmail**: minimal `gmail.readonly` scope; no send/modify/delete API
  surface; mutation counters.

## 10. What the system NEVER does without an explicit gate

- Never submits an HH application autonomously: автономный режим — prepare-only с 2.0; до этого отправлял без подтверждения. Отправка требует явного подтверждения человека (`--confirm-submit`).
- Never sends an HH message in REVIEW mode; AUTO requires kill switch +
  allowlist + composer + unchanged fingerprint + explicit live confirmation.
- Never sends an email (physically impossible in the current MVP).
- Never navigates to `applicant/vacancy_response` URLs.
- Never retries after an unknown submit/send outcome.
- Never guesses salary, dates, experience, status or other sensitive facts.

## 11. Local / runtime state (NOT in git)

| Path | Content |
|------|---------|
| `ai_assistant/.env` | secrets/keys (ignored) |
| `hh_storage_state.json` | HH authenticated session (ignored) |
| `state.db` | runtime SQLite database (ignored) |
| `candidate_profile.json` | personal candidate profile (ignored) |
| `artifacts/` | runtime state, snapshots, live artifacts (ignored) |
| `baseline_stage14_snapshot/`, `snapshot_stage15_current/` | DR snapshots (ignored) |
| `baseline_stage14_manifest.txt`, `snapshot_stage15_manifest.txt` | local sha256 manifests of snapshot contents (ignored) |

## 12. Current limitations

- Gmail live access depends on correctly configured OAuth Application Default
  Credentials with the `gmail.readonly` scope; `gmail_provider_status()`
  reports explicit blockers otherwise.
- HH auto-reply / auto-apply require explicit gates (kill switches +
  confirmation); live AUTO sends are capped (3 per run) and never retried.
- Email sending is physically blocked in the current MVP (REVIEW-only).
- `vacancies.json` is a tracked data snapshot; collectors refresh it.
- Playwright / feedparser / google clients are optional at runtime but required
  by the full test suite (see requirements-dev.txt).
