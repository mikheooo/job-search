# Master resume — source map

Knowledge base version: 30
Claims in the knowledge base: 127
Claims rendered: 113
Claims excluded: 10
Claims subsumed: 4
Claims accounted for: 127 of 127

Every line below names the claim it came from. Open the claim in `candidate_kb.json` and check `evidence` to see the file, the line and the quote.

## Brief map

| where the content comes from | claims | what it is |
| --- | --- | --- |
| T4.5 interview | 5 | answers the candidate confirmed; the only claims above DERIVED_FROM_SOURCE |
| owner confirmation | 5 | explicit candidate confirmation of historical facts |
| project catalogue (T5) | 21 | quotes anchored in four repositories, each re-read and hashed on every run |
| `resume.md` (T1 parser) | 96 | the candidate's own resume, line by line |
| **total** | **127** | 113 rendered, 10 excluded, 4 subsumed |

Each claim's `evidence` names its file, its line and its quote, so any line of the resume can be checked against the document it came from. The excluded and subsumed tables below say why a claim is not on a line.

## Core-skills groups sourced from duties

The knowledge base holds no `skill` claim for these, so the group is declared in the generator rather than invented as skills. Every entry names the duty it came from and the phrase that duty must literally contain, which is what makes the sourcing checkable.

| group | skill | duty claim | phrase in the duty |
| --- | --- | --- | --- |
| IT Support & Systems Administration | Windows Server | `duty-39d5320cd4` | Windows Server |
| IT Support & Systems Administration | Active Directory | `duty-39d5320cd4` | Active Directory |
| IT Support & Systems Administration | Exchange | `duty-39d5320cd4` | Exchange |
| IT Support & Systems Administration | Hyper-V | `duty-39d5320cd4` | Hyper-V |
| IT Support & Systems Administration | Networking | `duty-39d5320cd4` | networking |
| IT Support & Systems Administration | IP Telephony | `duty-39d5320cd4` | IP telephony |
| IT Support & Systems Administration | IT Support | `duty-94b646ace9` | technical support |
| IT Support & Systems Administration | Bitrix24 | `duty-d87a2fdc28` | Bitrix24 |

## Approved editorial decisions

Wording the candidate approved. These lines **are** published in the resume. The quoted text each one replaces is named in `APPROVED_TEXT_EDITS`; the decision is recorded here so published prose about the candidate is attributable rather than anonymous.

### Professional summary — opening

> IT specialist with 15+ years in IT: technical support, systems administration, Bitrix24/CRM, automation, and my own projects built with AI tools.

Approved by the candidate. Names the four directions the resume positions him across instead of the single direction the quoted clause did. The tenure figure inside it is the next entry.

### Tenure — the figure that is published

> 15+ years in IT

Approved by the candidate. Supported by the resume's own timeline rather than asserted: the earlier block starts in 2009 and the last employed role ends April 2026, about 17 years. It does not contradict domain_years.it_support = 11.0, which measures the support domain rather than the whole career, and it is not years_experience = 3, which is a third number and is not total tenure.

### Projects — the phrase 'in production'

> used in my work and in my own projects

Approved by the candidate. Replaces 'in production' in the quoted summary. The systems do run, but for personal projects with no client the old phrase can read as a claim about a commercial deployment. No client, no revenue and no metric is claimed.

### Core skills — the support & systems group

> IT Support & Systems Administration

Approved by the candidate. The KB's 32 skill claims name no Windows Server, Active Directory, Exchange, Hyper-V, networking, IP telephony or Bitrix24, so the group is sourced from the duties that do name them -- each entry carries the claim it came from, and test_every_duty_sourced_skill_names_its_source checks that the duty really contains the technology.

## Rendered claims

| line | claim | status | kind | value |
| --- | --- | --- | --- | --- |
| h1 | `name-5e0fb44f14` | CONFIRMED_BY_USER | name | Mikhail Kolesnikov |
| contact | `contact_location-21435774d4` | CONFIRMED_BY_USER | contact_location | Thailand |
| contact | `work_mode-d068a6b8c7` | CONFIRMED_BY_USER | work_mode | Remote |
| contact | `contact_email-48df9241e1` | CONFIRMED_BY_USER | contact_email | mikhailthaiban@gmail.com |
| contact | `contact_telegram-02e66f8504` | CONFIRMED_BY_USER | contact_telegram | Telegram: @brokeassh |
| paragraph | `summary-865c184449` | CONFIRMED_BY_USER | summary | AI Automation Engineer with 15+ years of IT experience: API integration, workflow automation, and LLM-power... |
| bullet | `skill_group-d467013436` | CONFIRMED_BY_USER | skill_group | AI & LLM |
| bullet | `skill-5030c1540e` | CONFIRMED_BY_USER | skill | AI agents |
| bullet | `skill-d4fe374910` | CONFIRMED_BY_USER | skill | Anthropic API |
| bullet | `skill-724ecabd15` | CONFIRMED_BY_USER | skill | Google Gemini API |
| bullet | `skill-1662a044dc` | CONFIRMED_BY_USER | skill | OpenAI API |
| bullet | `skill-517072cc25` | CONFIRMED_BY_USER | skill | RAG |
| bullet | `skill-2708bcf02d` | CONFIRMED_BY_USER | skill | classification |
| bullet | `skill-3eab3af122` | CONFIRMED_BY_USER | skill | content generation |
| bullet | `skill-7975914e63` | CONFIRMED_BY_USER | skill | fact-checking |
| bullet | `skill-a1903dda33` | CONFIRMED_BY_USER | skill | open-source LLMs |
| bullet | `skill-d740b91bff` | CONFIRMED_BY_USER | skill | prompt engineering |
| bullet | `skill-30b27a5c1b` | CONFIRMED_BY_USER | skill | summarization |
| bullet | `skill-5877fc4f0a` | CONFIRMED_BY_USER | skill | transcription |
| bullet | `skill-deb97b89e6` | CONFIRMED_BY_USER | skill | workflow automation |
| bullet | `skill_group-0bede53b28` | CONFIRMED_BY_USER | skill_group | Automation & delivery |
| bullet | `skill-281e20d20a` | CONFIRMED_BY_USER | skill | Bash |
| bullet | `skill-68a2a982ea` | CONFIRMED_BY_USER | skill | Docker |
| bullet | `skill-55261f09d3` | CONFIRMED_BY_USER | skill | Git |
| bullet | `skill-52ce101e01` | CONFIRMED_BY_USER | skill | Linux |
| bullet | `skill-f7e322dec9` | CONFIRMED_BY_USER | skill | MS SQL |
| bullet | `skill-b5fbd631e0` | CONFIRMED_BY_USER | skill | Make |
| bullet | `skill-c483a2308a` | CONFIRMED_BY_USER | skill | PostgreSQL |
| bullet | `skill-23e9ae952a` | CONFIRMED_BY_USER | skill | PowerShell |
| bullet | `skill-0fdaa41531` | CONFIRMED_BY_USER | skill | SQL |
| bullet | `skill-ce7cc0a4a1` | CONFIRMED_BY_USER | skill | n8n |
| bullet | `skill_group-9863f55354` | CONFIRMED_BY_USER | skill_group | Python & integrations |
| bullet | `skill-ec229b5a26` | CONFIRMED_BY_USER | skill | Google Workspace APIs |
| bullet | `skill-81be988e88` | CONFIRMED_BY_USER | skill | MTProto |
| bullet | `skill-cd4ad6f53b` | CONFIRMED_BY_USER | skill | Python |
| bullet | `skill-24d663fd71` | CONFIRMED_BY_USER | skill | REST APIs |
| bullet | `skill-68e483505f` | CONFIRMED_BY_USER | skill | Telegram Bot API |
| bullet | `skill-19770067ad` | CONFIRMED_BY_USER | skill | Telethon |
| bullet | `skill-3ae3d48e58` | CONFIRMED_BY_USER | skill | aiogram |
| bullet | `skill-2ac2073a2a` | CONFIRMED_BY_USER | skill | asyncio |
| bullet | `skill-912e2652b6` | CONFIRMED_BY_USER | skill | webhooks |
| bullet | `duty-39d5320cd4` | CONFIRMED_BY_USER | duty | Worked across Windows Server and Linux infrastructure, Active Directory, Exchange, SQL, Hyper-V, networking... |
| bullet | `duty-94b646ace9` | CONFIRMED_BY_USER | duty | Provided technical support and troubleshooting for business users. |
| bullet | `duty-d87a2fdc28` | CONFIRMED_BY_USER | duty | Administered self-hosted Bitrix24; configured integrations, business processes, automation robots, and syst... |
| h3 | `role-73195eaf1a` | CONFIRMED_BY_USER | role | AI Automation Engineer \| Sayama Travel |
| h3 | `role_title-640fc3b102` | CONFIRMED_BY_USER | role_title | AI Automation Engineer |
| h3 | `role_company-3102d0cf9a` | CONFIRMED_BY_USER | role_company | Sayama Travel |
| meta | `role_location-8890c7d677` | CONFIRMED_BY_USER | role_location | Thailand |
| meta | `role_start-a4c61e0193` | CONFIRMED_BY_USER | role_start | November 2022 |
| meta | `role_end-ab4f12fd82` | CONFIRMED_BY_USER | role_end | April 2026 |
| bullet | `duty-464bce9700` | CONFIRMED_BY_USER | duty | Administered Linux and Docker infrastructure (systemd services, logs, monitoring); configured VPN infrastru... |
| bullet | `duty-5588820a08` | CONFIRMED_BY_USER | duty | Automated business processes around Bitrix24, including workflows, robots, triggers, telephony, email, anal... |
| bullet | `duty-44d3a669dd` | CONFIRMED_BY_USER | duty | Built a voice-message pipeline using Whisper for transcription, followed by AI summarization and storage in... |
| bullet | `duty-b3a936c5e1` | CONFIRMED_BY_USER | duty | Developed Telegram bots and asynchronous media-transfer pipelines using Python, aiogram, Telethon, and MTPr... |
| bullet | `duty-60e928bbbf` | CONFIRMED_BY_USER | duty | Developed n8n workflows connecting Bitrix24, Google Sheets, Telegram channels, and external databases. |
| bullet | `duty-7c606ae226` | CONFIRMED_BY_USER | duty | Integrated OpenAI and Google Gemini into corporate chats and CRM workflows for incoming-request processing,... |
| h3 | `role-56fc2f2a27` | CONFIRMED_BY_USER | role | Bitrix24 Technical Support Specialist \| Avantis |
| h3 | `role_title-128507e960` | CONFIRMED_BY_USER | role_title | Bitrix24 Technical Support Specialist |
| h3 | `role_company-fbbc55cd18` | CONFIRMED_BY_USER | role_company | Avantis |
| meta | `role_location-845cb688d7` | CONFIRMED_BY_USER | role_location | Moscow |
| meta | `role_start-a3d3239464` | CONFIRMED_BY_USER | role_start | April 2021 |
| meta | `role_end-7ee130cf88` | CONFIRMED_BY_USER | role_end | August 2022 |
| bullet | `duty-d87a2fdc28` | CONFIRMED_BY_USER | duty | Administered self-hosted Bitrix24; configured integrations, business processes, automation robots, and syst... |
| bullet | `duty-94b646ace9` | CONFIRMED_BY_USER | duty | Provided technical support and troubleshooting for business users. |
| h3 | `role-7dcdc7ae59` | CONFIRMED_BY_USER | role | Earlier IT Infrastructure & Support Experience \| Speedcast / Germsat; Intravision; Slavyanka; Russian Post |
| h3 | `role_title-c09e9d8041` | CONFIRMED_BY_USER | role_title | Earlier IT Infrastructure & Support Experience |
| h3 | `role_company-715f9ac7c9` | CONFIRMED_BY_USER | role_company | Speedcast / Germsat; Intravision; Slavyanka; Russian Post |
| h3 | `role_employer-3e4d613396` | CONFIRMED_BY_USER | role_employer | Intravision |
| h3 | `role_employer-5c9f9a7847` | CONFIRMED_BY_USER | role_employer | Russian Post |
| h3 | `role_employer-24ee9b31bb` | CONFIRMED_BY_USER | role_employer | Slavyanka |
| h3 | `role_employer-ab93224052` | CONFIRMED_BY_USER | role_employer | Speedcast / Germsat |
| meta | `role_start-6da0482626` | CONFIRMED_BY_USER | role_start | 2009 |
| meta | `role_end-27a202567d` | CONFIRMED_BY_USER | role_end | 2018 |
| bullet | `duty-0ce6eed193` | CONFIRMED_BY_USER | duty | Automated routine administrative tasks with Bash and PowerShell. |
| bullet | `duty-39d5320cd4` | CONFIRMED_BY_USER | duty | Worked across Windows Server and Linux infrastructure, Active Directory, Exchange, SQL, Hyper-V, networking... |
| note | `chronology_note-451eec4688` | CONFIRMED_BY_USER | chronology_note | Speedcast/Germsat: период работы март 2018 — март 2021 подтверждён. Учесть при восстановлении хронологии, н... |
| h3 | `role-aee78081ce` | CONFIRMED_BY_USER | role | AI Automation Engineer \| Freelance - AI Integration & Automation |
| h3 | `role_title-54cbca59f0` | CONFIRMED_BY_USER | role_title | AI Automation Engineer |
| meta | `role_location-f392ae6ed4` | CONFIRMED_BY_USER | role_location | Thailand |
| meta | `role_start-531ab1564e` | CONFIRMED_BY_USER | role_start | May 2026 |
| meta | `role_end-4afee780b0` | CONFIRMED_BY_USER | role_end | Present |
| meta | `experience_note-7d59a994c6` | CONFIRMED_BY_USER | experience_note | С мая 2026 года я разрабатывал только собственные проекты, без оплачиваемых заказов. |
| bullet | `duty-b54fefb3bf` | CONFIRMED_BY_USER | duty | Build asynchronous Python ETL pipelines with asyncio, Telethon/MTProto, and external APIs. |
| bullet | `duty-0512d75ae3` | CONFIRMED_BY_USER | duty | Build n8n workflows connecting databases, AI agents, Google Workspace APIs, CRM systems, notifications, and... |
| bullet | `duty-c10be2ad60` | CONFIRMED_BY_USER | duty | Design and implement end-to-end AI automation pipelines for content processing and distribution (RAG, promp... |
| bullet | `duty-56204a9718` | CONFIRMED_BY_USER | duty | Develop Telegram bots with aiogram, external APIs, task queues, and FSM-based state management. |
| bullet | `duty-2a6c4a25f1` | CONFIRMED_BY_USER | duty | Develop media-processing workflows integrating Google Drive, Whisper, LLMs, and Telegram. |
| h3 | `project-d5ea623e87` | DERIVED_FROM_SOURCE | project | Job Search AI (Job Search Hub) |
| paragraph | `project_summary-09dad64829` | DERIVED_FROM_SOURCE | project_summary | Personal job-search automation system. Collects remote vacancies, scores them against a candidate profile, ... |
| bullet | `project_bullet-f7ab3fe1d1` | DERIVED_FROM_SOURCE | project_bullet | Everything that touches the outside world (browser, HH, email) is gated: default mode is REVIEW (human deci... |
| bullet | `project_bullet-b5762ce634` | DERIVED_FROM_SOURCE | project_bullet | - Collects vacancies from remote and tech job boards (habrcareer, remoteok, weworkremotely, himalayas; lega... |
| bullet | `project_bullet-92e1a2822c` | DERIVED_FROM_SOURCE | project_bullet | - Controlled application flow: plan → prefill → human review gate → single gated submit → read-only verific... |
| h3 | `project-0c3d17e11a` | DERIVED_FROM_SOURCE | project | Reels Analyzer |
| paragraph | `project_summary-9e92802939` | DERIVED_FROM_SOURCE | project_summary | A Telegram bot and asynchronous worker pipeline that analyzes short-form video (Instagram Reels, TikTok, Yo... |
| bullet | `project_bullet-32a3cc34a1` | DERIVED_FROM_SOURCE | project_bullet | - Accepts video URLs via a Telegram bot (aiogram) and queues them for processing (Redis + ARQ async task qu... |
| bullet | `project_bullet-66af869ed3` | DERIVED_FROM_SOURCE | project_bullet | - Fact-checks claims against web sources (Exa) with QA controls. |
| meta | `project_stack-a4e7dce20e` | DERIVED_FROM_SOURCE | project_stack | Python 3.13, aiogram, ARQ + Redis, PostgreSQL (asyncpg/SQLAlchemy), Pydantic, Google Gemini API, Exa, Docke... |
| h3 | `project-3e4d5f0359` | DERIVED_FROM_SOURCE | project | Repiton |
| paragraph | `project_summary-91011674d7` | DERIVED_FROM_SOURCE | project_summary | REPITON — AI-тренер произношения. Он учит говорить так, чтобы носитель языка правильно понял слово, а не пр... |
| bullet | `project_bullet-af9f94f145` | DERIVED_FROM_SOURCE | project_bullet | `Listen → Repeat → Analyze → Explain → Retry → Master` |
| bullet | `project_bullet-b6fe076fce` | DERIVED_FROM_SOURCE | project_bullet | **STAGE_1_FINAL_NO_GO.** По итогам однократного sealed-final эксперимента на 1 218 записях 6 независимых ди... |
| bullet | `project_status-d67eb22b0d` | DERIVED_FROM_SOURCE | project_status | Stage 1 остаётся **NO-GO**: текущая модель неустойчива между дикторами, а learner/error gold evidence отсут... |
| bullet | `project_status-608c03fb4e` | DERIVED_FROM_SOURCE | project_status | - **TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO` |
| h3 | `project-3e3925760e` | DERIVED_FROM_SOURCE | project | WHUBN |
| paragraph | `project_summary-612fa8b5bb` | DERIVED_FROM_SOURCE | project_summary | WHUBN is a local-first iOS app that reconstructs where a person's life happened from permitted location evi... |
| bullet | `project_bullet-19994f2a95` | DERIVED_FROM_SOURCE | project_bullet | - High-throughput SQLite persistence engine with WAL and Sendable Swift 6 concurrency |
| bullet | `project_bullet-351f1af6ac` | DERIVED_FROM_SOURCE | project_bullet | - Interactive 2D life map using Apple MapKit (`AtlasMapView`, `AtlasMKMapView`) |
| bullet | `project_bullet-2e63e97c09` | DERIVED_FROM_SOURCE | project_bullet | - Deterministic chronological segmentation of `LocationEvent` records into canonical `Stay` records (`StayS... |
| bullet | `education_institution-d6fa43f79c` | CONFIRMED_BY_USER | education_institution | Sakhalin State University |
| bullet | `education_program-66eb4c03a3` | CONFIRMED_BY_USER | education_program | Technological Institute, Technology and Entrepreneurship |
| bullet | `education_level-1bac63d506` | CONFIRMED_BY_USER | education_level | Higher Education |
| bullet | `education_year-7274535562` | CONFIRMED_BY_USER | education_year | 2005 |
| bullet | `language-f3534ca421` | CONFIRMED_BY_USER | language | English |
| bullet | `language_level-f536acbf66` | CONFIRMED_BY_USER | language_level | B1 (Intermediate) |
| bullet | `language-5bb2c66d6c` | CONFIRMED_BY_USER | language | Russian |
| bullet | `language_level-5e230513b4` | CONFIRMED_BY_USER | language_level | Native |
| bullet | `contact_email-48df9241e1` | CONFIRMED_BY_USER | contact_email | mikhailthaiban@gmail.com |
| bullet | `contact_telegram-02e66f8504` | CONFIRMED_BY_USER | contact_telegram | Telegram: @brokeassh |
| bullet | `contact_location-21435774d4` | CONFIRMED_BY_USER | contact_location | Thailand |
| bullet | `work_mode-d068a6b8c7` | CONFIRMED_BY_USER | work_mode | Remote |

## Declared presentation decisions

- headline assembled from the candidate's own role lists and the confirmed Bitrix24 preference; it covers four directions and is ordered alphabetically so that no direction is made the main one for every vacancy. A proposal, and it needs approval.
- summary: the quoted clause 'AI Automation Engineer with 15+ years of IT experience: ' is replaced by the approved opening, and 'in production' by the approved wording -- see APPROVED_TEXT_EDITS for both, and APPROVED_EDITORIAL_DECISIONS for who approved them. The candidate's own wording is untouched in the KB
- chronology note chronology_note-451eec4688 transcribed into the period March 2018 – March 2021 and attributed to Speedcast / Germsat alone, not to the block's other employers; the block keeps its own dates from the resume and no other employer is given a date the source does not state
- reclassified from the resume's 'Freelance' heading (role role-aee78081ce); the employer name is not reproduced because the candidate confirmed no paid client work (fact experience_note-7d59a994c6)
- project status labels (Implemented / Prototype) are the catalogue's reading of each project's state, not an independent assessment; where a repository states its own status an anchored line carries that sentence verbatim, and STATUS_WITHOUT_ANCHOR names the projects that do not. No project claims a client, a revenue figure or a metric

## Excluded claims

| claim | kind | reason | value |
| --- | --- | --- | --- |
| `experience_statement-03e30e1738` | experience_statement | restates total IT tenure in a form that equates it with tenure in one domain; contradicted by candidate_profile.json domain_years.it_support = 11.0 and by the T3 rule total_experience_vs_it_support. The same phrase is deleted from the summary via OMITTED_SPANS. | 15+ years of IT experience |
| `goal_direction-fa6ff566ec` | goal_direction | search strategy, not resume content | Приоритет: любая подходящая IT-работа, чтобы быстрее получить доход. |
| `interview-ex-metacritic` | interview_example | interview stories and deep-dive examples reserved for targeted Q&A rather than master resume prose | Вел платформу как заказчик/архитектор (API, Postgres+pgvector, Celery, Docker, demo-сте... |
| `interview-ex-fastapi-auth` | interview_example | interview stories and deep-dive examples reserved for targeted Q&A rather than master resume prose | Проектировал аддитивные FastAPI-роутеры поверх живого API и общий token-middleware; при... |
| `interview-ex-sqlite-dedup` | interview_example | interview stories and deep-dive examples reserved for targeted Q&A rather than master resume prose | Проектировал дедупликацию параллельных сборщиков в SQLite (стабильные ID, UNIQUE, идемп... |
| `interview-ex-tg-delivery` | interview_example | interview stories and deep-dive examples reserved for targeted Q&A rather than master resume prose | Проектировал доставку уведомлений ровно один раз (стабильные ключи, атомарный reserve, ... |
| `interview-ex-kwork-parse` | interview_example | interview stories and deep-dive examples reserved for targeted Q&A rather than master resume prose | Ставил задачу честного парсинга (никаких выдуманных данных, явные None); принимал по 20... |
| `preference_condition-3d50e70c67` | preference_condition | salary expectation: excluded from the public resume on purpose | Минимальный доход: от $1000 в месяц, прежние $1500 в профиле необходимо пересмотреть. |
| `preference_role-1f18d3dc89` | preference_role | search strategy, not resume content | Bitrix24: рассматривать наравне с другими вакансиями, не исключать из поиска. |
| `role_company-fcd10805e4` | role_company | 'Freelance - AI Integration & Automation' names an employer and implies paid client work, which the candidate confirmed did not happen; the period is presented as 'own projects' instead | Freelance - AI Integration & Automation |

## Subsumed claims

Present in the resume but carried by a different line, so they are not quoted verbatim. Kept here as raw material for per-vacancy versions.

| claim | kind | reason | value |
| --- | --- | --- | --- |
| `headline-e0d7b28fbb` | headline | represented by the resume headline, which is assembled as a proposal (POSITIONING_LABELS) rather than quoted verbatim; the candidate's own wording stays in the KB for per-vacancy versions | AI Engineer \| AI Automation Engineer \| Python Developer |
| `title-931a5ec847` | title | one of the candidate's own target titles, represented by the headline proposal; kept in the KB as raw material for per-vacancy versions | AI Automation Engineer |
| `title-a2e497e830` | title | one of the candidate's own target titles, represented by the headline proposal; kept in the KB as raw material for per-vacancy versions | AI Engineer |
| `title-61c18ca65c` | title | one of the candidate's own target titles, represented by the headline proposal; kept in the KB as raw material for per-vacancy versions | Python Developer |

## Project repositories

| project | path | remote | first commit | last commit | commits |
| --- | --- | --- | --- | --- | --- |
| job_search_ai | `C:\Users\Misha\Documents\job-search` | https://github.com/mikheooo/job-search.git | 2026-07-10 | 2026-11-10 | 194 |
| reels_analyzer | `C:\Users\Misha\reels_bot` | https://github.com/mikheooo/reels_bot.git | 2026-07-10 | 2026-10-10 | 136 |
| repiton | `C:\Users\Misha\Documents\Repiton` | — | 2026-09-08 | 2026-09-26 | 53 |
| whubn | `C:\Users\Misha\Documents\WHUBN` | https://github.com/mikheooo/whubn.git | 2026-09-02 | 2026-09-03 | 18 |
