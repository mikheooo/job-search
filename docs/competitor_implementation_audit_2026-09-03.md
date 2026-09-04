# Competitor implementation audit — 2026-09-03

## Scope and evidence rules

This is a point-in-time desk audit of public product pages and public GitHub
repositories. Vendor feature statements are recorded as vendor claims, not as
independently measured performance. No competitor code was copied. Repository
licenses were checked before implementation work.

## Market implementation patterns

| Product | Publicly described implementation | Better than current `job-search` in | Current system advantage / decision |
| :--- | :--- | :--- | :--- |
| [Teal](https://www.tealhq.com/) | Browser extension saves jobs from many boards; CRM-style tracker; per-job resume tailoring, ATS checks and application autofill with review before submit. | Polished onboarding, cross-site capture, editable resume versions. | Current system has stronger explicit evidence provenance and fail-closed delivery records. Keep Teal-like version review as backlog; do not weaken approval gates. |
| [Huntr](https://huntr.co/) | Job clipper and tracker; semantic match across keywords, responsibilities and qualifications; per-suggestion approval; autofill across many sites. | Clear, multi-dimensional fit explanation and reversible per-role resume edits. | Adopt the explainability pattern later using verified profile evidence only; never invent missing experience. |
| [Simplify](https://simplify.jobs/ai-job-search) | Profile matching, resume tailoring, networking assistance, ATS autofill and automatic application tracking in one workflow. | Breadth across Greenhouse, Workday, Lever and other ATS surfaces. | Current HH/browser path is narrower but has stricter mutation gates and forensic states for unknown outcomes. Broad autofill is not copied into unattended execution. |
| [LoopCV](https://www.loopcv.pro/) | Continuous multi-board search, filters/company exclusions, multiple CVs, auto-apply or manual review, outreach and A/B analytics. | Resume-variant routing and outcome experiments. | Mass submission and automated recruiter email conflict with this repository's review-first/no-email-send posture. Resume routing and experiment design remain candidates only after evidence-safe specifications. |
| [`fikstt2/hh-ai-agent`](https://github.com/fikstt2/hh-ai-agent) | HH assistant with approval queue, Telegram pause/resume/diagnostics, structured fit summary, setup wizard and circuit breaker for repeated page/technical failures. Snapshot `c675c1667e74c684e1d19182fcc8705870d3de3f`. | Operator controls and automatic pause after repeated failures. | **Adopted independently:** persistent production circuit, offline probe, health diagnostics and explicit operator resume. MIT licensed, but no source lines were copied. |
| [`AgentShekel/hh-bot`](https://github.com/AgentShekel/hh-bot) | Playwright search, primary/fallback LLMs, title/company filters, rating floor, Telegram-channel ingestion and multiple resume/feed variants. Snapshot `3a12421e7166ec588d589d7ebbfee1189d55b214`. | Multiple resume routing and LLM provider failover. | PolyForm Noncommercial 1.0.0: ideas only, no code copied. Provider failover is a future independent design candidate. |
| [`Vlad9572324/hh.ru-clicker`](https://github.com/Vlad9572324/hh.ru-clicker) | Web/mobile/auto HH clients, web fallback for supported operations, WebSocket chat updates, dashboard and resume analysis. Snapshot `6f9309586e98d1cfec8e1fd5f7ca72121940421f`. | Broad operational UI and real-time account/chat status. | No repository license was declared at inspection time, so no code was copied. Private/mobile API fallback is rejected for now because it expands account and platform risk. |
| [`lookr-fyi/job-application-bot-by-ollama-ai`](https://github.com/lookr-fyi/job-application-bot-by-ollama-ai) | Public documentation claims URL-started searches, ATS resume generation, application tracking and company-career-site application. Snapshot `410d6b9257e3ee50bb7e9b68473317122a82de44`. | Claimed breadth of company-site coverage. | No repository license was declared and the public implementation is not sufficiently inspectable for code adoption. Treat claims as unverified. |

## Adopted now: persistent production circuit breaker

The existing system already counted consecutive production failures and raised
health alerts, but it continued launching later scheduled runs. It also treated
a successful offline dry-run as live recovery and cleared the failure counter.

The independently implemented safety change now:

1. opens the circuit at the existing configurable failure threshold;
2. blocks later live `production-run` executions before discovery or delivery;
3. leaves offline `production-run --dry-run` available for diagnosis;
4. prevents dry-runs from changing live failure evidence;
5. exposes circuit state in `production-health` and `production-control status`;
6. requires explicit `production-control resume` after operator review.

## Prioritized follow-up, not yet implemented

1. **Evidence map in every fit explanation:** show verified matches and gaps by
   skills, responsibilities and qualifications, with source pointers to the
   candidate profile.
2. **Role-specific resume variants:** route a vacancy to a reviewed base resume
   variant before generating an application package; prohibit fabricated facts.
3. **LLM provider resilience:** configurable provider fallback, bounded retries
   and cooldowns, while retaining the deterministic truth-only fallback.
4. **Onboarding diagnostics:** one offline setup/check command for sources,
   profile completeness, credentials presence and production circuit state.

## Explicitly not adopted

- unattended mass auto-apply or recruiter email outreach;
- private/mobile API fallback without a separate risk and terms review;
- code from repositories with no compatible license;
- vendor marketing metrics as proof of effectiveness.
