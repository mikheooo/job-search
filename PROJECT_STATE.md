# PROJECT STATE & SYSTEM ARCHITECTURE

## 1. System Overview

`job-search` is an automated, production-grade, human-in-the-loop job search, qualification matching, digest delivery, and application management platform designed for Mikhail Kolesnikov's remote career search.

- **Primary Repository:** `c:\Users\Misha\Documents\job-search`
- **Active Branch:** `codex/production-stabilization-20260901`
- **Primary Runtime:** Python 3.11+ / Windows Powershell / SQLite WAL Mode
- **Operating Posture:** Fail-closed, truth-only, review-by-default, zero hallucinated claims, zero unauthorized external mutation.

---

## 2. Core Architecture & Component Map

```
                     ┌──────────────────────────────────────────┐
                     │          Vacancy Ingestion               │
                     │  (HH, Habr, GetMatch, RemoteOK, etc.)    │
                     └────────────────────┬─────────────────────┘
                                          │
                                          ▼
                     ┌──────────────────────────────────────────┐
                     │          State Store (state.db)          │
                     │  vacancies, matches, queue, applications │
                     └────────────────────┬─────────────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
   ┌─────────────────────────────┐                 ┌─────────────────────────────┐
   │     Matching Engine         │                 │   Application & Review      │
   │  (Stage 86/87 Calibrated)   │                 │  Queue -> Prepare -> Review │
   │  Role priorities, seniority,│                 │  Manual & CDP browser assist│
   │  domain years, skill conf   │                 └──────────────┬──────────────┘
   └──────────────┬──────────────┘                                │
                  ▼                                               ▼
   ┌─────────────────────────────┐                 ┌─────────────────────────────┐
   │   Telegram Digest Delivery  │                 │    Recruiter Messaging      │
   │  Batched, idempotent,       │                 │  (Stage 30D / 87.1 Triage)  │
   │  delivery keys, rate limits │                 │  Truth-only, REVIEW default,│
   │                             │                 │  strict fail-closed gates   │
   └─────────────────────────────┘                 └─────────────────────────────┘
```

---

## 3. Production Entry Points & CLI Commands

All capabilities are unified under `ai_assistant.cli` (`python -m ai_assistant.cli <command>`):

### Ingestion & Matching
- `python -m ai_assistant.cli collect [--sources ...]` — Ingest new vacancies from enabled scrapers/APIs.
- `python -m ai_assistant.cli analyze [--top N] [--persist]` — Match vacancies against calibrated candidate profile.
- `python -m ai_assistant.cli analyze-deep [--top N]` — Deep LLM vacancy qualification & nuance analysis.
- `python -m ai_assistant.cli export-digest [--limit N] [--min-score N]` — Generate prioritized Telegram digest.

### Application Lifecycle & Queue
- `python -m ai_assistant.cli queue [--top N] [--status ...]` — View and prioritize applications in queue.
- `python -m ai_assistant.cli applications list / status / move` — Track and transition application lifecycle states.
- `python -m ai_assistant.cli audit --tracked --json` — Run application lifecycle integrity audit.

### Recruiter Messaging & Triage
- `python -m ai_assistant.cli hh-message diagnose` — Inspect active HH CDP tab, DOM state, and chat frame.
- `python -m ai_assistant.cli hh-message preview [conversation_id]` — Preview extracted chat history and draft reply.
- `python -m ai_assistant.cli hh-message classify [conversation_id]` — Perform truth-only classification and fact-check.
- `python -m ai_assistant.cli hh-message triage [--limit N] [--json]` — Batch triage all active conversations.
- `python -m ai_assistant.cli hh-message send --conversation-id ID --confirm` — Explicit human-approved reply send.

### Operational Health & Telemetry
- `python -m ai_assistant.cli production-health --json` — Production health check, alert evaluation, delivery telemetry.

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
| `telegram_delivery_records`| Idempotent digest delivery tracking | Delivery keys prevent duplicate Telegram messages |
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

## 6. Recruiter Messaging Contract (Stage 30D / 87.1)

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

## 7. Safety Invariants & Guarantees

- **No Unauthorized Mutation:** All diagnostic, preview, triage, and audit commands are strictly read-only.
- **Fail-Closed Auto-Reply:** `AUTO` send mode is strictly opt-in via `HH_AUTO_REPLY_ENABLED=true` environment variable and requires explicit human confirmation flag for CLI dispatch.
- **Byte-for-Byte Draft Integrity:** Sent messages must match validated drafts byte-for-byte; no on-the-fly unvalidated regeneration.
- **Idempotent Telegram Deliveries:** `record_digest_attempt` and unique delivery keys prevent double-posting.
- **Database Immutability in Testing:** Pytest runs operate against isolated temporary fixtures and must not mutate production `state.db`.

---

## 8. Verified Test Metrics & Production Status

- **Stage 30D Diagnostic Suite (`tests/test_stage30d_diagnose.py`):** 80/80 passed
- **Stage 87 Profile Calibration Suite (`tests/test_stage87_candidate_profile_calibration.py`):** 17/17 passed
- **Related Recruiter / Application Suites:** 149/149 passed
- **Production Integrity Audit (`ai_assistant.cli audit --tracked`):** 0 errors, healthy = true
- **Production Operational Health (`ai_assistant.cli production-health`):** Status: HEALTHY, 0 alerts, 0 consecutive failures

---

## 9. Recommended Next Scope (Stage 88)

With Stage 86 (Ranking & Explainability), Stage 87 (Candidate Profile Calibration), and Stage 87.1 (Recruiter Message Semantic Reconciliation) fully stabilized, the recommended next focus areas are:

1. **Stage 88 — Automated Digest Scheduling & Dispatch Hardening:**
   - Integrate the calibrated Stage 86/87 matcher with the daily Windows scheduled task.
   - Enforce P1 role prioritization and minimum score thresholds in automated Telegram deliveries.
   - Add digest feedback tracking and candidate rating mechanisms.
