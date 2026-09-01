# PROJECT STATE & SYSTEM ARCHITECTURE

## 1. System Overview

`job-search` is an automated, production-grade, human-in-the-loop job search, qualification matching, digest delivery, and application management platform designed for Mikhail Kolesnikov's remote career search.

- **Primary Repository:** `c:\Users\Misha\Documents\job-search`
- **Active Branch:** `codex/production-stabilization-20260901`
- **Primary Runtime:** Python 3.11+ / Windows Powershell / SQLite WAL Mode
- **Operating Posture:** Fail-closed, truth-only, review-by-default, zero hallucinated claims, zero unauthorized external mutation.

---

## 2. Core Architecture & Scheduled Execution Path

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
  - Himalayan, RemoteOK, WWR, Habr             - list_undigested_vacancies (state.db)
  - normalize_vacancy -> vacancy_identity      - Canonical JobMatcher(profile)
  - Insert / Update -> state.db                - Sort: DecisionClass > RolePriority > Score
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

## 3. Canonical Matching & Digest Selection Policy (Stage 86/87/88)

### Canonical Matcher
The sole canonical matcher used across both discovery ingestion (`Watcher`) and digest delivery (`export_digest_cmd`) is `JobMatcher` defined in `ai_assistant/matcher.py`.

### Matcher Capabilities vs Production Digest Usage

| Feature | Matcher Supports | Production Digest Uses | Verification Status |
| :--- | :---: | :---: | :--- |
| **Role Priorities (P1 / P2 / P3)** | YES | YES | Primary sorting dimension within decision class |
| **Skill Confidence (0.0 to 1.0)** | YES | YES | Directly influences score & strong match eligibility |
| **Domain-Specific Seniority** | YES | YES | Decoupled support (11y), sysadmin (9y), python (3.5y) |
| **STRETCH Decision Class** | YES | YES | Explicitly ranked between MATCH and BORDERLINE (Rank 3) |
| **Fail-Closed Hard Gates** | YES | YES | Hard requirement failure -> REJECT -> Excluded from digest |
| **Delivery History Deduplication** | YES | YES | `telegram_delivery_records` prevents re-delivery |
| **Company & Role Diversity** | YES | YES | Max 2 per company, max 4 per role family |

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

### Backlog Semantics (901 Pending Undigested Vacancies)
The metric `pending_undigested_vacancies_count: 901` in `production-health` represents all non-legacy vacancy records in `state.db` that have not been delivered to Telegram.
- **Audited Breakdown:**
  - `REJECT` (96.45% / 869 items): Non-remote, in-office, wrong technical domains (SAP, .NET, Senior SharePoint, Sales/Marketing). Correctly suppressed.
  - `BORDERLINE` (3.55% / 32 items): Moderate scores (50–70) with missing specific skills or unverified requirements.
  - `DELIVERED` (11 top items): Already successfully posted to `@remotejobd` with durable delivery keys.
  - `ELIGIBLE UNSENT`: 0 (All high-confidence matches in DB have been delivered).

---

## 4. State & Database Model (`state.db`)

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

## 5. Calibrated Candidate Profile (`candidate_profile.json`)

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

## 6. Feedback Capability Audit & Architecture

- **Current Status:** `PARTIAL`
- **Existing Assets:**
  - `application_reviews`: Stores human review actions (`APPROVED`, `REJECTED`, `COMPLETED`), reviewer notes, and skipped fields.
  - `application_tracking`: Tracks state progression (`DISCOVERED`, `ANALYZED`, `READY_TO_APPLY`, `APPLIED`, `REJECTED`, `WITHDRAWN`, `INTERVIEW`, `OFFER`).
  - `matches`: Stores dimension breakdown, reasons, strengths, and gaps.
- **Proposed Unified Feedback Loop:**
  - Expose inline Telegram callback buttons or CLI command `python -m ai_assistant.cli digest-feedback --vacancy-id <id> --action [INTERESTED|NOT_INTERESTED|APPLIED|SKIPPED] [--reason ...]`.
  - Records feedback directly into `application_reviews` / `matches` to adjust dynamic candidate preferences and exclude skipped vacancies.

---

## 7. Recruiter Messaging Contract (Stage 30D / 87.1)

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

## 8. Safety Invariants & Guarantees

- **No Unauthorized Mutation:** All diagnostic, preview, triage, and audit commands are strictly read-only.
- **Fail-Closed Auto-Reply:** `AUTO` send mode is strictly opt-in via `HH_AUTO_REPLY_ENABLED=true` environment variable and requires explicit human confirmation flag for CLI dispatch.
- **Byte-for-Byte Draft Integrity:** Sent messages must match validated drafts byte-for-byte; no on-the-fly unvalidated regeneration.
- **Idempotent Telegram Deliveries:** `record_digest_attempt` and unique delivery keys prevent double-posting.
- **Database Immutability in Testing:** Pytest runs operate against isolated temporary fixtures and must not mutate production `state.db`.

---

## 9. Verified Test Metrics & Production Status

- **Full Offline Pytest Regression:** **1251 passed** (0 failed, 0 errors in 580.02s)
  - `tests/test_stage88_production_match_digest_wiring.py`: 18/18 passed
  - `tests/test_stage87_candidate_profile_calibration.py`: 17/17 passed
  - `tests/test_stage30d_diagnose.py`: 80/80 passed
  - `tests/test_stage83_production_operations.py`: 16/16 passed
  - Related Recruiter / Application Suites: 149/149 passed
- **Production Integrity Audit (`ai_assistant.cli audit --tracked`):** 0 errors, healthy = true
- **Production Operational Health (`ai_assistant.cli production-health`):** Status: HEALTHY, 0 alerts, 0 consecutive failures
- **Production Database SHA256:** `d5c9a505af6d22850bed5c3fd8ca9e5cf1ac05527ed59edd5996181e82f45777` (Verified 100% immutable)
