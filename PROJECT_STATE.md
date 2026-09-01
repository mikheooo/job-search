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
    role_priority_rank[role_priority],    # P1(3) > P2(2) > P3(1) > NOT_TARGET(0)
    numeric_score,                        # Descending 100 to 0
    eligibility_rank[eligibility],        # ELIGIBLE(2) > BORDERLINE(1) > INELIGIBLE(0)
    recency_timestamp                     # published_at / first_seen_at
)
```

### Backlog Semantics (894 Pending Undigested Vacancies)
The metric `pending_undigested_vacancies_count: 894` in `production-health` represents all genuine non-delivered vacancies in `state.db`:
- **Audited Breakdown:**
  - `REJECT` (862 items / 96.4%): Non-remote, in-office, wrong technical domains (SAP, .NET, Senior SharePoint, Sales/Marketing). Correctly suppressed.
  - `BORDERLINE` (32 items / 3.6%): Moderate scores (50–70) with missing specific skills or unverified requirements.
  - `DELIVERED` (10 real + 1 dryrun): Successfully recorded with durable delivery keys.
  - `ELIGIBLE UNSENT`: 0 (All qualified matches in DB have been delivered).

---

## 5. State & Database Model (`state.db`)

SQLite database operating with `journal_mode=WAL` and `synchronous=NORMAL`:

| Table | Purpose | Key Invariant |
| :--- | :--- | :--- |
| `vacancies` | Raw & parsed vacancy records across all sources | Unique `(source, source_job_id)` |
| `matches` | Scored qualification matches and breakdowns | 1:1 linked with vacancy records |
| `application_queue` | Prioritized pipeline for manual/assisted application | Ordered by priority score & match quality |
| `applications` | Formal application records and lifecycle tracking | Statuses: `READY`, `SUBMITTED`, `REJECTED`, etc. |
| `canonical_vacancies` | Cross-source vacancy deduplication clusters | Normalizes company & title |
| `telegram_delivery_records`| Idempotent digest delivery tracking | Delivery keys (`digest:<id>`, `digest_batch:<key>`) prevent duplicates |
| `conversation_audits` | Recruiter message processing & send audit log | Tracks all incoming/outgoing message hashes |

---

## 6. Calibrated Candidate Profile (`candidate_profile.json`)

Validated and calibrated during Stage 87 against Mikhail Kolesnikov's resume:

- **Location:** Thailand (UTC+7), 100% Remote required.
- **Languages:** Russian (Native), English (B1 Intermediate).
- **Decoupled Experience:**
  - Support & Systems: 11.0 yrs IT Support, 9.0 yrs Sysadmin, 5.0 yrs Application Support (15+ yrs total IT).
  - Automation & Backend: 3.5 yrs Python development, 3.5 yrs n8n/API automation, 2.0 yrs AI/LLM integrations.
- **Role Family Priorities:**
  - `P1` (Primary Target): `AI_AUTOMATION`, `APPLICATION_SUPPORT`, `TECH_SUPPORT`
  - `P2` (Secondary Adjacent): `PYTHON_BACKEND`, `SYSTEM_ADMIN`
  - `P3` (Stretch / Experimental): `DATA_ENGINEERING`, `DEVOPS_SRE`
- **Skill Confidence Model:**
  - `PROFESSIONAL` (1.00): `python`, `n8n`, `automation`, `telegram`, `rest api`, `webhooks`, `linux`, `sql`, `active directory`, `whisper`, `make`, `docker`, `bash`, `powershell`.
  - `PROJECT` (0.85): `fastapi`, `ai agents`, `llm`, `postgresql`, `telethon`, `aiogram`, `asyncio`.
  - `BASIC` (0.60): `git`, `ci/cd`, `pandas`.
  - `TRANSFERABLE` (0.40): `itsm`, `troubleshooting`, `vpn`, `networking`.
  - `UNKNOWN` (0.00): `kubernetes`, `pytorch`, `c++`, `java`, `react`.

---

## 7. Feedback Capability Audit & Architecture

- **Current Status:** `PARTIAL`
- **Existing Assets:**
  - `application_reviews`: Stores human review actions (`APPROVED`, `REJECTED`, `COMPLETED`), reviewer notes, and skipped fields.
  - `application_tracking`: Tracks state progression (`DISCOVERED`, `ANALYZED`, `READY_TO_APPLY`, `APPLIED`, `REJECTED`, `WITHDRAWN`, `INTERVIEW`, `OFFER`).
  - `matches`: Stores dimension breakdown, reasons, strengths, and gaps.
- **Proposed Unified Feedback Loop:**
  - Expose inline Telegram callback buttons or CLI command `python -m ai_assistant.cli digest-feedback --vacancy-id <id> --action [INTERESTED|NOT_INTERESTED|APPLIED|SKIPPED] [--reason ...]`.
  - Records feedback directly into `application_reviews` / `matches` to adjust dynamic candidate preferences and exclude skipped vacancies.

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

- **Full Offline Pytest Regression:** **1278 passed** (0 failed, 0 errors in ~10.5m)
  - `tests/test_stage88_2_production_provenance_hardening.py`: 15/15 passed
  - `tests/test_stage88_1_production_vacancy_provenance.py`: 12/12 passed
  - `tests/test_stage88_production_match_digest_wiring.py`: 18/18 passed
  - `tests/test_stage87_candidate_profile_calibration.py`: 17/17 passed
  - `tests/test_stage30d_diagnose.py`: 80/80 passed
  - `tests/test_stage83_production_operations.py`: 16/16 passed
  - Related Recruiter / Application Suites: 149/149 passed
- **Production Integrity Audit (`ai_assistant.cli audit --tracked`):** 0 errors, healthy = true
- **Production Operational Health (`ai_assistant.cli production-health`):** Status: HEALTHY, 0 alerts, 0 consecutive failures
- **Production Database SHA256:** `d5c9a505af6d22850bed5c3fd8ca9e5cf1ac05527ed59edd5996181e82f45777` (Verified 100% immutable)
