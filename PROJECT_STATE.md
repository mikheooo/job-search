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

## 7. Telegram Feedback & Application Review Integration (Stage 89)

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
ai_assistant.telegram_feedback.TelegramFeedbackProcessor
        │
        ├── Authorization Gate: fail-closed against TELEGRAM_OWNER_ID / TELEGRAM_CHAT_ID
        ├── Callback Data Decoding: compact 64-byte safe encoding fb:<action>:<id_or_hash>
        ├── Idempotency Check: deduplicates repeated updates via callback_query_id
        ├── Canonical State Transitions: authoritative update via application_review & tracking
        └── Audit Trail: persisted in telegram_feedback_records table
```

### Safety Invariants & Rules

1. **NO DIRECT SUBMISSION INVARIANT:**
   Button `📄 Отклик` strictly prepares and transitions review/tracking state to `READY_TO_APPLY` and enqueues into `application_queue`. It **NEVER** triggers automated or external job application submission.
2. **OPERATOR AUTHORIZATION GATE:**
   Only the configured operator/owner (`TELEGRAM_OWNER_ID` or `TELEGRAM_CHAT_ID`) is permitted to submit review callbacks. Unauthorized queries fail closed with `⛔ Доступ запрещён`.
3. **64-BYTE TELEGRAM CALLBACK CONSTRAINT:**
   Callback data uses format `fb:<action>:<stable_id>` if length $\le$ 64 bytes, or surrogate SHA256 prefix `fb:<action>:h:<16_char_hash>` resolved dynamically from the database.
4. **IDEMPOTENCY & TERMINAL STATE PROTECTION:**
   Vacancies already in terminal application states (`APPLIED`, `SUBMITTED`, `VERIFIED`, `INTERVIEW`, `OFFER`) fail closed with safe informational warnings and cannot be overwritten or downgraded by feedback callbacks.

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

---

## 10. Verified Test Metrics & Production Status

- **Full Offline Pytest Regression:** **1298 passed** (0 failed, 0 errors in 14:06)
  - `tests/test_stage89_telegram_feedback.py`: 20/20 passed
  - `tests/test_stage88_2_production_provenance_hardening.py`: 15/15 passed
  - `tests/test_stage88_1_production_vacancy_provenance.py`: 12/12 passed
  - `tests/test_stage88_production_match_digest_wiring.py`: 18/18 passed
  - `tests/test_stage87_candidate_profile_calibration.py`: 17/17 passed
  - `tests/test_stage30d_diagnose.py`: 80/80 passed
  - `tests/test_stage83_production_operations.py`: 16/16 passed
  - Related Recruiter / Application Suites: 149/149 passed
- **Production Integrity Audit (`ai_assistant.cli audit --tracked`):** 0 errors, healthy = true
- **Production Operational Health (`ai_assistant.cli production-health`):** Status: HEALTHY, 0 alerts, 0 consecutive failures
- **Production Database SHA256:** `41644ac4518a83b547bd63e36eb72f9ff328ccc7a9b7495e14662e47b3310750`
