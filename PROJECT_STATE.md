# PROJECT STATE & SYSTEM ARCHITECTURE

## 1. System Overview

`job-search` is an automated, production-grade, human-in-the-loop job search, qualification matching, digest delivery, and application management platform designed for Mikhail Kolesnikov's remote career search.

- **Primary Repository:** `c:\Users\Misha\Documents\job-search`
- **Active Branch:** `codex/production-stabilization-20260901`
- **Primary Runtime:** Python 3.11+ / Windows Powershell / SQLite WAL Mode
- **Operating Posture:** Fail-closed, truth-only, review-by-default, zero hallucinated claims, zero unauthorized external mutation.

---

## 2. Production Source Policy & Provenance (Stage 88.1 / 88.2)

### Active Production Sources vs Isolated Legacy & Synthetic Artifacts

| Source Key | Adapter / Origin | Classification | Ingestion Type | Unattended Digest Eligible |
| :--- | :--- | :--- | :--- | :---: |
| `himalayas` | `HimalayasAdapter` | `ACTIVE_PRODUCTION_SOURCE` | Automated Scraper | **YES** (with genuine provenance) |
| `weworkremotely`| `WeWorkRemotelyAdapter` | `ACTIVE_PRODUCTION_SOURCE` | Automated Scraper | **YES** (with genuine provenance) |
| `remoteok` | `RemoteOkAdapter` | `ACTIVE_PRODUCTION_SOURCE` | Automated Scraper | **YES** (with genuine provenance) |
| `habrcareer` | `HabrCareerAdapter` | `ACTIVE_PRODUCTION_SOURCE` | Automated Scraper | **YES** (with genuine provenance) |
| `hh` | HeadHunter API / CDP Tab | `ACTIVE_PRODUCTION_SOURCE` | Browser / Assisted | **YES** (with genuine provenance) |
| `vacancies_json`| `vacancies.json` Benchmark | `LEGACY_IMPORT` / `CALIBRATION_DATA` | Historical Bootstrap | **NO (Isolated)** |
| `x` | Test fixture | `TEST_FIXTURE_SOURCE` | Test artifact | **NO (Isolated)** |

### Production Provenance & Digest Selection Invariant
```
Unattended Production Digest Selection
        │
        ├── 1. Genuine Active Source: source in ('himalayas', 'weworkremotely', 'remoteok', 'habrcareer', 'hh')
        ├── 2. Provenance Validation: is_genuine_production_vacancy() -> rejects dryrun/test/mock/fake/ACME/example.com
        ├── 3. Delivery Idempotency: LEFT JOIN telegram_delivery_records: status != 'DELIVERED'
        ├── 4. Canonical Matcher Qualified: decision in ('APPLY', 'REVIEW') and score >= min_score (60.0)
        └── 5. Diversity & Dedup Guard: max 2/company, max 4/role family, canonical key deduplication
```

### Historical Delivery Provenance Audit
- **Total `job_digest` Delivered Records:** 11 items across 3 batches (`-1004399255305` @remotejobd).
- **`verified_real_delivered`:** 10 items (7 `hh`, 3 `weworkremotely`).
- **`nonproduction_delivered`:** 1 item (`himalayas:dryrun-test-1` delivered on 2026-09-01T03:01:36Z in batch `1031eacdc719f432`).
- **`unknown_delivered`:** 0.
- **Historical Record Preservation:** Preserved byte-for-byte in `telegram_delivery_records` without deletion or retroactive history rewriting.

---

## 3. Core Architecture & Scheduled Execution Path

### Scheduled Execution Pipeline

```
Windows Task Scheduler ("JobSearch_Daily_Digest")
        │
        ▼
scripts/run_job_search_production.ps1
        │
        ▼
python -m ai_assistant.cli production-run
        │
        ▼
ai_assistant.runner.run_production_pipeline
  (Single-instance lock: job_search.lock, log rotation, secret masking)
        │
        ▼
job_search_fetcher.py
        ├──────────────────────────────────────────┐
        ▼                                          ▼
1. Discovery & Ingestion                   2. Validated Digest Export
ai_assistant.watcher.Watcher                 ai_assistant.cli.export_digest_cmd
  - Himalayas, RemoteOK, WWR, Habr             - list_undigested_vacancies (state.db)
  - normalize_vacancy -> vacancy_identity      - Genuine provenance check (is_genuine_production_vacancy)
  - Insert / Update -> state.db                - Canonical JobMatcher(profile)
                                               - Sort: DecisionClass > RolePriority > Score
                                               - Diversity filter: max 2/company, 4/family
                                                   │
                                                   ▼
                                           3. Idempotent Telegram Dispatch
                                             ai_assistant.db.record_digest_attempt (ATTEMPTING)
                                                   │
                                                   ▼
                                             job_search_fetcher.send_to_telegram (@remotejobd)
                                               - Compact Inline Keyboard Markup (Stage 89)
                                                   │
                                                   ▼
                                             ai_assistant.db.mark_digest_delivered (DELIVERED)
```

---

## 4. Canonical Matching & Digest Selection Policy (Stage 86/87/88/88.2)

### Canonical Matcher
The sole canonical matcher used across both discovery ingestion (`Watcher`) and digest delivery (`export_digest_cmd`) is `JobMatcher` defined in `ai_assistant/matcher.py`.

### Digest Ordering Key

Candidates are ordered using a strict multi-tier tuple:
```python
(
    decision_class_rank[decision_class],  # STRONG_MATCH(5) > MATCH(4) > STRETCH(3) > BORDERLINE(2) > REJECT(1)
    role_priority_rank[role_priority],    # P1(4) > P2(3) > P3(2) > UNMAPPED(1) > EXCLUDED(0)
    adjusted_score,                       # Match score with confidence + years calibration
    recency_timestamp                     # Ingestion/publication ISO timestamp
)
```

---

## 5. Candidate Profile Truth Baseline (Stage 87)

- **Target Roles:**
  - `P1`: AI Automation Engineer, Application Support Engineer, Technical Support Engineer (Tier 2/3 / L2/L3).
  - `P2`: Python Backend Developer (Junior+/Mid), System Administrator / IT Systems Engineer.
  - `P3`: Data Engineer / ETL Developer, DevOps / SRE Junior.
  - `EXCLUDED`: Senior ML / Core AI Research Engineer, Frontend / Mobile / Embedded / Blockchain Engineer, Non-technical Customer Care.
- **Experience Baseline (Total 11+ Years in Tech):**
  - IT Support / Service Desk / Troubleshooting: 11.0 years.
  - Systems Administration / Infrastructure / Linux: 9.0 years.
  - Application Support / API Integrations: 5.0 years.
  - Automation / Scripting (Python, n8n, Webhooks, APIs): 3.5 years.
  - Modern Python Backend: 3.5 years.
  - AI Integration / LLM Agents / Prompt Orchestration: 2.0 years.
- **Salary Floor & Location Constraints:**
  - Minimum monthly compensation: **\$1,500 USD net / equivalent**.
  - Remote constraint: **Strict 100% remote**.
  - Location eligibility: Worldwide, EMEA, Cyprus, Georgia, Armenia, Thailand.

---

## 6. Skills Calibration Baseline

- **Confidence Tiers:**
  - `PROFESSIONAL` (1.00): `python`, `n8n`, `automation`, `fastapi`, `postgresql`, `docker`, `linux`, `rest api`, `sql`, `telegram`, `webhooks`.
  - `PROJECT` (0.80): `langchain`, `llamaindex`, `chromadb`, `openai`, `gemini`, `rag`, `redis`.
  - `BASIC` (0.60): `git`, `ci/cd`, `pandas`.
  - `TRANSFERABLE` (0.40): `itsm`, `troubleshooting`, `vpn`, `networking`.
  - `UNKNOWN` (0.00): `kubernetes`, `pytorch`, `c++`, `java`, `react`.

---

## 7. Telegram Feedback & External Runtime Integration (Stage 89 / 89.1 / 89.2)

### Architecture & Data Flow

```
Production Vacancy
        │
        ▼
Telegram Digest (with Inline Keyboard)
        │
        ├── [1. 👍] ──> INTERESTED: ApplicationReview(PENDING_REVIEW), tracking.ANALYZED
        ├── [1. 👎] ──> NOT_INTERESTED: ApplicationReview(REJECTED), tracking.REJECTED
        ├── [1. 📄] ──> PREPARE_APPLICATION: ApplicationReview(APPROVED), tracking.READY_TO_APPLY, application_queue
        └── [1. ⏭] ──> SKIP: ApplicationReview(REJECTED), tracking.WITHDRAWN
        │
        ▼
Single Update Consumer (Hermes Gateway / TelegramBot on long-polling)
        │
        ▼
ai_assistant.telegram_feedback.TelegramFeedbackProcessor
        │
        ├── 1. Owner Authorization Gate: callback_query.from.id == TELEGRAM_OWNER_ID (392046103)
        ├── 2. Callback Idempotency Check: deduplicates repeated updates via callback_query_id
        ├── 3. Collision-Safe Hash Decoding: fail-closed if 0 or >1 matches
        ├── 4. Provenance Verification: is_genuine_production_vacancy() -> rejects synthetic/legacy
        ├── 5. Delivery Relationship: is_vacancy_delivered_in_digest() -> requires prior delivery
        ├── 6. Canonical State Transitions: authoritative update via application_review & tracking
        ├── 7. Audit Trail: persisted in telegram_feedback_records table
        └── 8. Telegram Acknowledgement: prompt answerCallbackQuery with action toast
```

### External Runtime Management & Drift Subsystem (Stage 89.2)

1. **CANONICAL REPOSITORY SOURCE:**
   External runtime integrations are versioned under `integrations/hermes/`:
   - `integrations/hermes/job_search_fetcher.py`: Canonical fetcher script with keyboard forwarding and attempt locking.
   - `integrations/hermes/telegram_adapter_hook.py`: Canonical Hermes adapter callback routing hook.
2. **DRIFT DETECTION & RECOVERABILITY:**
   - `python -m ai_assistant.cli hermes status`: Audits presence, SHA256 integrity, and capability markers.
   - `python -m ai_assistant.cli hermes sync`: Idempotently synchronizes canonical files to deployed runtime locations.
   - `production-health` metric payload exposes real-time `hermes_integration` status (`HEALTHY` / `DRIFTED` / `MISSING`).
3. **NO DIRECT SUBMISSION INVARIANT:**
   Button `📄 Отклик` strictly prepares and transitions review/tracking state to `READY_TO_APPLY` and enqueues into `application_queue`. It **NEVER** triggers automated or external job application submission.
4. **STRICT OWNER AUTHORIZATION GATE:**
   Only the explicit operator user ID (`TELEGRAM_OWNER_ID=392046103`) is permitted to submit review callbacks. Destination channel ID `TELEGRAM_CHAT_ID` cannot authorize mutation. Unauthorized queries fail closed with `⛔ Доступ запрещён`.
5. **64-BYTE TELEGRAM CALLBACK & COLLISION SAFETY:**
   Callback data uses format `fb:<action>:<stable_id>` if length $\le$ 64 bytes, or surrogate SHA256 prefix `fb:<action>:h:<16_char_hash>` resolved dynamically from the database. Hash resolution fails closed if 0 or $>1$ matching vacancies exist.
6. **PROVENANCE & DELIVERY RELATIONSHIP GATE:**
   Callbacks are accepted only for genuine production vacancies that have a confirmed `DELIVERED` record in `telegram_delivery_records`. Synthetic/test fixtures fail closed with informational warnings.
7. **IDEMPOTENCY & TERMINAL STATE PROTECTION:**
   Vacancies already in terminal application states (`APPLIED`, `SUBMITTED`, `VERIFIED`, `INTERVIEW`, `OFFER`) fail closed with safe informational warnings and cannot be overwritten or downgraded by feedback callbacks. Duplicate callback query IDs are idempotent no-ops.
8. **SINGLE UPDATE CONSUMER ARCHITECTURE:**
   Hermes Gateway runs as the single active long-polling update consumer on bot token `<REDACTED_BOT_ID>:...`. `fb:` feedback callbacks are dispatched to `TelegramFeedbackProcessor` without initiating conflicting second pollers.

---

## 8. Recruiter Messaging Contract (Stage 30D / 87.1)

Recruiter message handling follows strict truth-only and fail-closed rules:

1. **`NEEDS_REPLY`:**
   - Explicit questions with answers provable from candidate profile (e.g. "Do you have experience with Python and n8n?", "Are you ready to discuss role details?", "Are you available for remote work?").
   - Generates verified, truth-grounded reply drafts without inventing facts.
2. **`NO_REPLY_NEEDED`:**
   - Informational status updates, automated platform notifications, application view notices, candidate already responded, or employer rejection notices.
3. **`HUMAN_REVIEW` (Fail-Closed Safety):**
   - Salary negotiation / compensation requests.
   - Specific calendar scheduling dates / time slots.
   - Questions naming unverified technologies (e.g. C++, Haskell, 1C, Bitrix).
   - Ambiguous, multi-part, or non-standard inquiries.
   - High-risk or sensitive questions.
   - Cannot be automatically approved or sent.

---

## 9. Safety Invariants & Guarantees

- **No Unauthorized Mutation:** All diagnostic, preview, triage, and audit commands are strictly read-only.
- **Dry-Run Zero-Mutation:** `is_dry_run()` guard inside `save_vacancy` guarantees zero database mutation during dry-run cycles.
- **Fail-Closed Auto-Reply:** `AUTO` send mode is strictly opt-in via `HH_AUTO_REPLY_ENABLED=true` environment variable and requires explicit human confirmation flag for CLI dispatch.
- **Byte-for-Byte Draft Integrity:** Sent messages must match validated drafts byte-for-byte; no on-the-fly unvalidated regeneration.
- **Idempotent Telegram Deliveries:** `record_digest_attempt` and unique delivery keys prevent double-posting.
- **Database Immutability in Testing:** Pytest runs operate against isolated temporary fixtures and must not mutate production `state.db`.

- **Fit vs Preference Separation:** Matcher evaluates factual candidate qualification (`match_score`, `decision_class`, `eligibility`). Preference calibration applies a separate, bounded `preference_adjustment` (default max $\pm 8.0$ points) to compute `ranking_score` without altering factual eligibility, resume skills, or turning `REJECT` into `MATCH`.
- **Factual Profile Immutability:** User feedback on job titles or technologies (e.g. liking Kubernetes roles) never mutates `candidate_profile.json` or promotes skill confidence from `UNKNOWN` to `PROFESSIONAL`.
- **Minimum Evidence Threshold & Ambiguity:** Single feedback events are recorded as `RECORD_ONLY` with 0 ranking adjustment. Contradictory feedback produces `AMBIGUOUS_PREFERENCE` and suppresses confidence to 0.

- **HH Submission Safety Gates (11 Strict Multi-Layer Gates in `HHSubmissionGates`):**
  1. `GATE_SUBMIT_ALLOWED`: Environment / config kill-switch `SUBMIT_ALLOWED` must be explicitly enabled (`true`/`1`/`yes`) unless in dry_run mode.
  2. `GATE_REVIEW_APPROVED`: Vacancy review in DB must exist and have status `ReviewStatus.APPROVED`.
  3. `GATE_FINGERPRINT_MATCH`: Form fingerprint from live browser DOM snapshot must match approved review fingerprint.
  4. `GATE_URL_DOMAIN`: Current URL hostname must strictly match `hh.ru` or `*.hh.ru` (verified via `urllib.parse`).
  5. `GATE_VACANCY_MATCH`: Numeric vacancy ID in URL must match expected `source_job_id` extracted from `vacancy_stable_id`. Missing or non-numeric IDs fail closed.
  6. `GATE_PROFILE_LOADED`: CandidateProfile must be loaded and non-empty.
  7. `GATE_COVER_LETTER_READY`: Cover letter text must be present and at least 10 characters long.
  8. `GATE_NO_UNKNOWN_QUESTIONS`: Form fields must not contain unfillable or review-requiring questions (`requires_review=True`).
  9. `GATE_NOT_ALREADY_APPLIED`: Application tracking status must be in whitelist (`DISCOVERED`, `ANALYZED`, `READY_TO_APPLY`); no prior completed/ambiguous submission records in DB (`SUBMITTED`, `AMBIGUOUS_POST_SUBMIT`, `CONFIRMED`, `VERIFIED`, `FAILED`).
  10. `GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT`: No concurrent `SUBMITTING` status in DB; vacancy/review must not be present in current in-memory submission session set.
  11. `GATE_HUMAN_CONFIRMED`: Explicit human confirmation flag (`--confirm-submit`) required unless in dry_run mode.

---

## 10. Stage 90 / 90.1 / Stage 91 / Stage 91.1: Feedback Analytics, Provenance & Reason UX Architecture

- **Evidence Provenance & Signal Hierarchy (Stage 90.1 / Stage 91 / Stage 91.1):**
  - `EXPLICIT_HUMAN_FEEDBACK` (1 event): Genuine Telegram client button press by authorized owner (`392046103`) on real production vacancy (`hh:136551280`).
  - `CONFIRMED_REAL_APPLICATION` (1 event): Human-confirmed controlled runner submission on genuine vacancy (`hh:136704137`).
  - Excluded Non-Production / Automated Rows (14 rows): Synthetic validation callbacks (`live_val_stage89_1_001`), fixture vacancies (`vacancies_json:*`), unverified reviews, scraped chat states.
  - Total Raw Evidence Rows = 16; Validated Production Ground Truth = **2 independent human events**; Excluded = 14 rows.
- **Action Semantics & Negative Signal Distinction (Stage 91):**
  - `PREPARE_APPLICATION` / `APPLIED`: `VERY_STRONG_POSITIVE` (weight 1.5, raw value +1.0) — explicit intent to apply.
  - `INTERESTED`: `STRONG_POSITIVE` (weight 1.0, raw value +0.8) — vacancy looks relevant / interesting.
  - `NOT_INTERESTED`: `STRONG_NEGATIVE` (weight 1.0, raw value -0.8) — vacancy is genuinely undesirable.
  - `SKIP`: `WEAK_NEGATIVE` (weight 0.5, raw value -0.3) — contextual skip / not pursuing now.
  - `SKIP` with negative reason: `NEGATIVE` (weight 0.8, raw value -0.5).
- **Structured Feedback Reasons & Telegram 2-Level Keyboard (Stage 91 / 91.1):**
  - Supported reason tokens: `ROLE`, `SALARY`, `COMPANY`, `LOCATION`, `TECH_STACK`, `SENIORITY`, `LANGUAGE`, `EMPLOYMENT_TYPE`, `TOO_COMPLEX`, `TOO_JUNIOR`, `TOO_SENIOR`, `REMOTE`, `CAREER_GROWTH`, `OTHER`.
  - Compact Telegram callback encoding: `fb:RSN:<act_code>:<reason_code>:<target>` with strict 64-byte payload safety.
  - Optional UX: First tap marks feedback immediately; optional second-level compact keyboard captures structured reason without forcing extra steps. Follow-up reason clicks correlate to the same event.
- **Feedback Edit & Temporal Supersession Semantics (Stage 91 / 91.1):**
  - Newer user feedback for the same vacancy supersedes older preference while preserving complete audit history in `telegram_feedback_records`.
  - Multi-step progression (`INTERESTED` -> `PREPARE_APPLICATION` -> `SUBMITTED`) merges into 1 opportunity with upgraded evidence strength, preventing artificial sample count inflation.
  - Unanswered digest vacancies (`no_feedback`) are tracked in coverage metrics and **never inferred as negative preference**.
- **Coverage Semantics Clarification (Stage 91.1):**
  - `delivered_vacancies`: 11 total unique vacancies delivered via Telegram digest.
  - `telegram_feedback_vacancies`: 1 vacancy with explicit Telegram button tap (9.1% Telegram feedback rate).
  - `confirmed_applications`: 1 vacancy with human-confirmed real application.
  - `canonical_human_evidence_vacancies`: 2 unique vacancies with verified preference ground truth (18.2% human evidence coverage rate).
- **Reason-Aware Signal Routing & Profile Immutability (Stage 91):**
  - `COMPANY` dislike reasons stay company-specific and do not penalize role families.
  - `SALARY` and `LOCATION` reasons do not penalize technical skills or role families.
  - `TECH_STACK` reasons target skills and do not mutate factual candidate qualifications (`candidate_profile.json` is strictly immutable).
- **Calibration Readiness Dashboard & CLI (Stage 91 / 91.1):**
  - `python -m ai_assistant.cli feedback coverage [--json]` — reports separated Telegram feedback and confirmed application coverage rates.
  - `python -m ai_assistant.cli feedback analytics [--json]` — reports canonical human events (`2 / 5`), events needed before threshold (`3 more required`), dimension-specific readiness, and bias notes.
  - `python -m ai_assistant.cli feedback provenance [--json]` — inspects raw evidence rows vs validated ground truth.
  - `python -m ai_assistant.cli feedback simulate [--limit N] [--json]` — read-only ranking comparison.
  - `python -m ai_assistant.cli hermes status` — verifies Hermes outbound keyboard, callback routing, and attempt locking.
- **Production Status:** `PREFERENCE_CALIBRATION_ENABLED=False` (default feature flag; ready for activation when evidence scales).

---

## 11. Verified Test Metrics & Production Status

- **Stage 92 — competitor implementation audit and fail-closed circuit:**
  - Point-in-time audit: `docs/competitor_implementation_audit_2026-09-03.md`.
  - Public product claims and inspectable repository behavior are separated; no vendor effectiveness metric is treated as verified performance.
  - Repository license boundary: MIT implementation was used only as design evidence; PolyForm Noncommercial and unlicensed repositories were inspected but no code was copied.
  - Adopted independently: a persistent production circuit opens after `PRODUCTION_FAILURE_ALERT_THRESHOLD` consecutive live failures, blocks later live runs before the fetcher starts (exit `5`), permits an offline dry-run probe without clearing live evidence, and requires explicit `production-control resume` after review.
  - Deferred: evidence-mapped fit summaries, reviewed resume-variant routing, bounded LLM provider failover, and offline onboarding diagnostics.
  - Rejected in the current safety posture: unattended mass auto-apply, recruiter email outreach, and private/mobile API fallback.
- **Full Offline Pytest Regression:** **1408 passed** (0 failed, 0 errors in 12:26)
  - This run covered the completed Stage 92 implementation. A separate concurrent edit to `ai_assistant/external_form_solver.py` appeared afterwards; validation of that unrelated edit is **UNKNOWN** and it was not modified by Stage 92.
  - `tests/test_stage83_production_operations.py`: 20/20 passed
  - Focused production safety/provenance/Hermes regression: 43/43 passed
  - `tests/test_stage91_1_feedback_reason_production_wiring.py`: 20/20 passed
  - `tests/test_stage91_feedback_collection_quality.py`: 20/20 passed
  - `tests/test_stage90_1_feedback_evidence_provenance.py`: 20/20 passed
  - `tests/test_stage90_feedback_calibration.py`: 20/20 passed
  - `tests/test_stage89_2_external_runtime_persistence.py`: 8/8 passed
  - `tests/test_stage89_1_telegram_feedback_production_wiring.py`: 18/18 passed
  - `tests/test_stage89_telegram_feedback.py`: 20/20 passed
  - `tests/test_stage88_2_production_provenance_hardening.py`: 15/15 passed
  - `tests/test_stage88_1_production_vacancy_provenance.py`: 12/12 passed
  - `tests/test_stage88_production_match_digest_wiring.py`: 18/18 passed
  - `tests/test_stage87_candidate_profile_calibration.py`: 17/17 passed
  - `tests/test_stage30d_diagnose.py`: 80/80 passed
- **Production Integrity Audit (`ai_assistant.cli audit --tracked`):** healthy = false, 1 error, 0 warnings (652 checked)
  - Current error: `REVIEW_BROWSER_MISMATCH` for legacy `vacancies_json:71` (`review=APPROVED`, `browser=BLOCKED`). It appeared after an earlier clean Stage 92 audit and was not reconciled because production-data mutation was outside this task.
- **Production Operational Health (`ai_assistant.cli production-health`):** Status: HEALTHY, 0 alerts, 0 consecutive failures, production circuit CLOSED
  - The first Stage 92 snapshot found the deployed Hermes Telegram adapter DRIFTED; `hermes sync --dry-run --json` proposed `INJECTED_ADAPTER_HOOK`, and Stage 92 did not apply it.
  - A later concurrent external change made the adapter HEALTHY and callback-capable. The actor/process is **UNKNOWN**; the canonical fetcher remained HEALTHY and hash-matched.
- **Production Database SHA256:** **UNKNOWN at final handoff** because another process held `state.db`; earlier Stage 92 snapshot was `0B57699086F5616D1CAFFB0FBFE3F5E9C339660A172BC49398AC0B8EFC6F56F1`, but it may now be stale.
- **PROJECT_STATE.md updated: YES**
