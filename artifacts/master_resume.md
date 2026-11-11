Mikhail Kolesnikov
Application Support & IT Support | Bitrix24 / CRM Automation | Python Automation & AI Projects | Systems & Network Administration
Thailand · Remote · mikhailthaiban@gmail.com · Telegram: @brokeassh

## PROFESSIONAL SUMMARY

IT specialist with 15+ years in IT: technical support, systems administration, Bitrix24/CRM, automation, and my own projects built with AI tools. API integration, workflow automation, and LLM-powered systems used in my work and in my own projects. Builds end-to-end systems combining Python, LLMs (OpenAI, Gemini, Anthropic), REST/webhook integrations, Telegram bots, n8n/Make workflows, and Linux/Docker infrastructure. English B1, fully remote, ICT (+7) timezone.

## CORE SKILLS

- **AI & LLM:** AI agents, Anthropic API, Google Gemini API, OpenAI API, RAG, classification, content generation, fact-checking, open-source LLMs, prompt engineering, summarization, transcription, workflow automation
- **Automation & delivery:** Bash, Docker, Git, Linux, MS SQL, Make, PostgreSQL, PowerShell, SQL, n8n
- **Python & integrations:** Google Workspace APIs, MTProto, Python, REST APIs, Telegram Bot API, Telethon, aiogram, asyncio, webhooks
- **IT Support & Systems Administration:** Windows Server, Active Directory, Exchange, Hyper-V, Networking, IP Telephony, IT Support, Bitrix24

## EMPLOYMENT HISTORY

### AI Automation Engineer | Sayama Travel

*Thailand | November 2022 – April 2026*

- Administered Linux and Docker infrastructure (systemd services, logs, monitoring); configured VPN infrastructure and maintained backups and network security.
- Automated business processes around Bitrix24, including workflows, robots, triggers, telephony, email, analytics, and lead-generation channels.
- Built a voice-message pipeline using Whisper for transcription, followed by AI summarization and storage in Bitrix24 deal records.
- Developed Telegram bots and asynchronous media-transfer pipelines using Python, aiogram, Telethon, and MTProto.
- Developed n8n workflows connecting Bitrix24, Google Sheets, Telegram channels, and external databases.
- Integrated OpenAI and Google Gemini into corporate chats and CRM workflows for incoming-request processing, lead classification, and multilingual response generation.

### Bitrix24 Technical Support Specialist | Avantis

*Moscow | April 2021 – August 2022*

- Administered self-hosted Bitrix24; configured integrations, business processes, automation robots, and system workflows.
- Provided technical support and troubleshooting for business users.

### Earlier IT Infrastructure & Support Experience | Speedcast / Germsat; Intravision; Slavyanka; Russian Post

*2009 – 2018*

- Automated routine administrative tasks with Bash and PowerShell.
- Worked across Windows Server and Linux infrastructure, Active Directory, Exchange, SQL, Hyper-V, networking, IP telephony, monitoring, API/web-service testing, and ITSM systems.

*Speedcast / Germsat: March 2018 – March 2021 (period confirmed by the candidate).*

## INDEPENDENT PROJECTS

### Own projects in this period

*Thailand | May 2026 – Present*

No paid client work in this period (confirmed by the candidate).

- Build asynchronous Python ETL pipelines with asyncio, Telethon/MTProto, and external APIs.
- Build n8n workflows connecting databases, AI agents, Google Workspace APIs, CRM systems, notifications, and external services.
- Design and implement end-to-end AI automation pipelines for content processing and distribution (RAG, prompt engineering, AI agents).
- Develop Telegram bots with aiogram, external APIs, task queues, and FSM-based state management.
- Develop media-processing workflows integrating Google Drive, Whisper, LLMs, and Telegram.

### Job Search AI (Job Search Hub) — Implemented

Personal job-search automation system. Collects remote vacancies, scores them against a candidate profile, prepares tailored applications, and — behind explicit safety gates — can fill and submit HH.ru application forms, reply to HH messages, and prepare email replies.

- Everything that touches the outside world (browser, HH, email) is gated: default mode is REVIEW (human decision required); AUTO modes are strictly opt-in via environment kill-switches and never guess facts.
- Collects vacancies from remote and tech job boards (habrcareer, remoteok, weworkremotely, himalayas; legacy: linkedin, wellfound).
- Controlled application flow: plan → prefill → human review gate → single gated submit → read-only verification.

### Reels Analyzer — Implemented

A Telegram bot and asynchronous worker pipeline that analyzes short-form video (Instagram Reels, TikTok, YouTube Shorts): extracts transcripts and visual evidence, checks claims against sources, and produces structured, evidence-backed analysis results.

- Accepts video URLs via a Telegram bot (aiogram) and queues them for processing (Redis + ARQ async task queue, PostgreSQL job/task store).
- Fact-checks claims against web sources (Exa) with QA controls.

**Stack:** Python 3.13, aiogram, ARQ + Redis, PostgreSQL (asyncpg/SQLAlchemy), Pydantic, Google Gemini API, Exa, Docker / docker-compose.

### Repiton — Prototype

REPITON — an AI pronunciation coach. It teaches you to speak so that a native speaker understands the word, rather than merely confirming that speech-to-text guessed the text.

- `Listen → Repeat → Analyze → Explain → Retry → Master`
- **STAGE_1_FINAL_NO_GO.** In a single sealed-final experiment on 1,218 recordings from 6 independent C-COST speakers (macro-F1 = 0.702 < 0.80, low recall = 0.318 < 0.65)
- Stage 1 remains **NO-GO**: the current model is unstable across speakers, and learner/error gold evidence is missing.
- **TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO`

### WHUBN — Implemented

WHUBN is a local-first iOS app that reconstructs where a person's life happened from permitted location evidence such as photo metadata and location-history imports.

- High-throughput SQLite persistence engine with WAL and Sendable Swift 6 concurrency
- Interactive 2D life map using Apple MapKit (`AtlasMapView`, `AtlasMKMapView`)
- Deterministic chronological segmentation of `LocationEvent` records into canonical `Stay` records (`StaySegmentationEngine`)

## EDUCATION

- **Sakhalin State University** — Technological Institute, Technology and Entrepreneurship, Higher Education, 2005

## LANGUAGES

- English — B1 (Intermediate)
- Russian — Native

## CONTACT INFORMATION

- mikhailthaiban@gmail.com
- Telegram: @brokeassh
- Thailand
- Remote
