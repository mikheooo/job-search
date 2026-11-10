# T6.2 — целостность доказательств + закрытие T5.2

Дата: 2026-09-24. Проект: `C:\Users\Misha\Documents\job-search`.
Файлы, которых касается отчёт: `ai_assistant/candidate/job_brief.py`,
`ai_assistant/candidate/master_resume.py`, `tests/test_candidate_job_brief.py`,
`tests/test_candidate_master_resume.py`, `tools/t5_guard_mutation.py`,
`tools/.t6_2_probe.py`, `artifacts/master_resume.md`, `artifacts/master_resume.ru.md`,
`artifacts/master_resume_sources.md`.

---

## 0. Коротко: что изменилось

| # | Что | Где в коде | Замер |
| - | --- | --- | --- |
| 1 | Класс A требует, чтобы оплачиваемое доказательство **называло** технологию требования | `job_brief._coverage_gap_note`, `_uncovered_terms_note`, блок containment в `assess_requirements` | Python Developer: было A=6, стало A=2 / B=2 / C=2 / D=1 / E=1 |
| 2 | Блок Freelance больше не коммерческий опыт | `job_brief._own_work_roles`, `_evidence_for(..., own_work_roles)`, `Evidence.is_own_work`, `Assessment.project_evidence` | все 5 обязанностей `role-aee78081ce` → `origin="own_work"` |
| 3 | Четыре правки T5.2 внесены в **генератор** | `master_resume.APPROVED_SUMMARY_OPENING`, `APPROVED_TENURE`, `APPROVED_PROJECTS_PHRASE`, `APPROVED_TEXT_EDITS`, `DUTY_SOURCED_SKILL_GROUPS`, `APPROVED_EDITORIAL_DECISIONS` | 122 из 122 claims учтены, обе версии резюме содержат все четыре правки |
| 4 | Ноты «T5.2 не применён» больше нет | `job_brief.render_brief_short` — условие читается из файлов | 0 вхождений в выводе по пяти вакансиям |

Отдельно: **одна правка не планировалась и нашлась по ходу.** Краткий отчёт печатал
«НЕЗАВЕРШЁННЫЙ ЭТАП: T5.2 не применён…» по условию «есть ли в KB хоть одно общее
предупреждение», а не по состоянию T5.2, и текст строки был вписан руками. После
закрытия T5.2 строка продолжала бы врать на том экране, где она решает, можно ли
брать текст резюме как есть. Условие теперь берётся из `t5_2_pending_note()`, которая
считает маркеры в черновиках при каждом вызове.

---

## 1. Часть 1 — проверка доказательств навыков

### 1.1 Правило

Требование, называющее технологии, теперь отвечает только теми фактами, которые
называют **хотя бы одну** из них, и класс A ставится, только если **все** названные
технологии закрыты оплачиваемыми доказательствами.

```
wanted        = технологии, которые называет требование (только из словаря KB)
named[факт]   = технологии, которые называет сам факт
факт остаётся в доказательствах  ⇔  named ∩ wanted ≠ ∅
класс A                          ⇔  paid_covered == wanted
класс B                          ⇔  own_covered  == wanted
```

Правило **одностороннее**: оно умеет только понижать класс, никогда не повышать.
Недосказать стоит одной фразы в сопроводительном письме; пересказать стоит
кандидату собеседования.

Замер технического предела, на котором это держится: `_canonical_terms_in` матчит
только цифры и ASCII-буквы (`(?<![0-9a-zа-я])…(?![0-9a-zа-я])`), поэтому
**`PostgreSQL` никогда не совпадает с `postgresql`** (заглавный `SQL` ломает
lookbehind). Ни одна обязанность в KB не называет `postgresql` и не называет
`python`: `duty-39d5320cd4` называет `sql`, `duty-0ce6eed193` — `bash, powershell`.
Поэтому пункты 1 и 2 требований выполняются **без ослабления настоящих совпадений**.

### 1.2 Что именно выполняется

| Пункт ТЗ | Как выполнен | Где проверено |
| --- | --- | --- |
| 1. PostgreSQL не подтверждается обязанностью, где только SQL | требование «SQL и PostgreSQL» → класс **C**, в объяснении названы и «postgresql», и «sql» | `test_postgresql_is_never_confirmed_by_a_duty_that_only_says_sql` |
| 2. Python не подтверждается обязанностью, где только Bash и PowerShell | синтетический KB: обязанность про Bash/PowerShell → Python не класс A | `test_a_duty_that_names_bash_and_powershell_never_confirms_python` |
| 3. Docker, Python, PostgreSQL, Bitrix24 — только из фактов с этой технологией | класс A проверяется по **содержимому** несущего факта, а не по факту наличия коммерческого доказательства | `test_a_class_a_technology_is_named_by_the_paid_claim_that_carries_it`, `test_no_class_a_answer_rests_on_evidence_that_names_none_of_its_technologies` |
| 4. Частичное совпадение с соседней технологией не поднимает класс | A→C и B→C с объяснением, какой технологии не хватает | `test_a_partial_match_of_a_neighbouring_technology_never_raises_the_class` |
| 5. Общий IT-стаж не выдаётся за конкретную технологию | факт, не называющий ни одной технологии требования, в доказательства не попадает вовсе | `test_a_technology_requirement_drops_evidence_that_names_none_of_it` |

### 1.3 Замер по пяти вакансиям (до → после)

| Вакансия | Было (T6.1) | Стало (T6.2) |
| --- | --- | --- |
| IT Support (Porkbun) | A=2, E=1 | A=2, E=1 |
| System Administrator (DMI) | A=4, E=12 | A=4, E=12 |
| Bitrix24 (ТехноГрупп) | A=2, C=1, E=6 | A=2, C=1, E=6 |
| AI Automation (Toptal) | A=4, D=4, E=2 | **A=3, C=1, D=4, E=2** |
| Python Developer (Okkam) | A=6, D=1, E=1 | **A=2, B=2, C=2, D=1, E=1** |

Четыре требования, которые перестали быть классом A на Python Developer, и почему:

| Требование | Было | Стало | Причина |
| --- | --- | --- | --- |
| «Практический опыт работы с LLM API (OpenAI, Anthropic, локальные модели)» | A | **C** | оплачиваемая обязанность называет только `openai api`; `anthropic api` есть лишь в списке навыков резюме |
| «Асинхронное программирование: asyncio, aiohttp/httpx» | A | **B** | единственный факт — обязанность Freelance, то есть свой проект |
| «SQL и PostgreSQL — уверенно…» | A | **C** | оплачиваемое называет только `sql`; `postgresql` и `redis` — из проекта |
| «Фреймворки для агентов и RAG…» | A | **B** | единственный факт — обязанность Freelance |

На AI Automation одно требование («AI & Agentic Service Composition») понижено A→C по
той же причине: оплачиваемое называет `classification`, а требование просит ещё и
`anthropic api`.

Остальные классы A **не понизились**, потому что их несёт факт, который называет
технологию требования: Docker — `duty-464bce9700` («Administered Linux and Docker…»),
Python — `duty-b3a936c5e1` (Sayama), Bitrix24 — четыре оплачиваемые обязанности,
Linux — две оплачиваемые обязанности.

---

## 2. Часть 2 — проверка коммерческого опыта

### 2.1 Единственный источник правды

`master_resume.INDEPENDENT_ROLE_EVIDENCE = {"role-aee78081ce": "experience_note-7d59a994c6"}`.

Это уже существующее объявление: `experience_note-7d59a994c6` — `CONFIRMED_BY_USER`,
привязан к `role-aee78081ce`, значение: «С мая 2026 года я разрабатывал только
собственные проекты, без оплачиваемых заказов.» `job_brief` его раньше не читал.
Теперь читает через `_own_work_roles()` — **второго списка не заведено**.

### 2.2 Аудит блока Freelance

Роль: `role-aee78081ce`, заголовок `AI Automation Engineer | Freelance - AI Integration & Automation`,
май 2026 – настоящее, Таиланд.

| Поле роли | Значение | Вывод |
| --- | --- | --- |
| `role_company` | `Freelance - AI Integration & Automation` | компания есть |
| `role_employer` | **нет** | работодателя нет — блок не может быть наёмным по своим полям |
| `role_title` | `AI Automation Engineer` | **совпадает с названием наёмной роли Sayama** — по названию блоки неразличимы |
| `role_start` / `role_end` | `May 2026` / `Present` | |
| `experience_note-7d59a994c6` | `CONFIRMED_BY_USER`, `role_id` = эта роль | подтверждение самого кандидата |

Пять обязанностей и их технологии:

| claim_id | технологии | класс |
| --- | --- | --- |
| `duty-0512d75ae3` | ai agents, google workspace apis, n8n, crm | own_work |
| `duty-2a6c4a25f1` | open source llms, telegram bot api | own_work |
| `duty-56204a9718` | aiogram, telegram bot api | own_work |
| `duty-b54fefb3bf` | asyncio, mtproto, python, telethon | own_work |
| `duty-c10be2ad60` | ai agents, prompt engineering, rag | own_work |

Все пять получают `origin="own_work"`, `role_linked=False`,
`where="свои проекты — без оплаты"`. Ни одна не попадает в `commercial_evidence`.

### 2.3 Что выполняется

| Пункт ТЗ | Как выполнен |
| --- | --- |
| Класс A использует только доказательства из работы по найму | `Evidence.is_commercial == (origin == "resume" and role_linked)`; `role_linked` вычитает объявленные роли |
| Собственные проекты относятся к проектному опыту | `Assessment.project_evidence` = `origin in ("own_work", "project")` |
| Свой проект не становится коммерческим через `role_id` | `role_linked=role_linked and record.role_id not in own_work_roles` |
| …через название роли | название не участвует в решении вообще; метка печатается словами «свои проекты — без оплаты» |
| …через исходный заголовок раздела | решение принимается по `role_id`, а не по разделу `resume.md`; закреплено тестом про совпадающие `role_title` |
| Стаж коммерческой работы по технологии не выводится из общего IT-стажа | `_technology_tenure_caveat` висит на требовании любого класса; `15` в объяснение не попадает |
| Исправление на уровне модели доказательств, а не отображения | изменены `Evidence.origin`, `Evidence.is_commercial`, `Assessment.*_evidence`, `assess_requirements`; рендер только читает |

---

## 3. Часть 3 — T5.2 внесён в генератор

Четыре решения внесены **в `ai_assistant/candidate/master_resume.py`**, затем генератор
запущен повторно, файлы прочитаны с диска.

### 3.1 Как это устроено

| Решение | Константа | Механизм |
| --- | --- | --- |
| 1. Вступление | `APPROVED_SUMMARY_OPENING` | подстановка в цитату через `APPROVED_TEXT_EDITS` |
| 2. «15+ лет в IT» | `APPROVED_TENURE` | входит во вступление и публикуется отдельной записью решения |
| 3. Убрать «in production» | `APPROVED_PROJECTS_PHRASE` | вторая подстановка в `APPROVED_TEXT_EDITS` |
| 4. Группа навыков | `DUTY_SOURCED_GROUP_HEADING`, `DUTY_SOURCED_SKILL_GROUPS` | отдельная группа в `## CORE SKILLS`, каждый навык со ссылкой на обязанность |

`APPROVED_TEXT_EDITS` — не удаление, а **подстановка**: указан и заменяемый фрагмент, и
замена, и причина. Если фрагмента в источнике нет — `ValueError`, а не молчаливый
пропуск: правка, переставшая применяться, оставила бы черновик выглядящим утверждённым.

`PROPOSALS` **пуст**: три прежних предложения утверждены и переехали в
`APPROVED_EDITORIAL_DECISIONS`, которая печатается в карте источников разделом
«Approved editorial decisions». Раздел «Proposals awaiting approval» в карте больше не
появляется.

### 3.2 Источник каждого навыка новой группы

В KB нет ни одного `skill`-факта про эти технологии (все 32 — про AI, автоматизацию и
Python). Источник — профессиональная история:

| Навык | Факт | Фраза, которая обязана быть в факте |
| --- | --- | --- |
| Windows Server | `duty-39d5320cd4` | `Windows Server` |
| Active Directory | `duty-39d5320cd4` | `Active Directory` |
| Exchange | `duty-39d5320cd4` | `Exchange` |
| Hyper-V | `duty-39d5320cd4` | `Hyper-V` |
| Networking | `duty-39d5320cd4` | `networking` |
| IP Telephony | `duty-39d5320cd4` | `IP telephony` |
| IT Support | `duty-94b646ace9` | `technical support` |
| Bitrix24 | `duty-d87a2fdc28` | `Bitrix24` |

`duty-39d5320cd4` — обязанность блока 2009–2018:
«Worked across Windows Server and Linux infrastructure, Active Directory, Exchange, SQL,
Hyper-V, networking, IP telephony, monitoring, API/web-service testing, and ITSM systems.»

Фраза хранится рядом с меткой именно потому, что для «IT Support» она другая
(в истории «technical support»). Это различие видно, а не спрятано, и проверяется
тестом `test_every_duty_sourced_skill_names_its_source`.

### 3.3 Баланс фактов не сломан

`candidate-master build` печатает: **122 = 113 rendered + 5 excluded + 4 subsumed, 0 unaccounted**.
Ни один факт KB не добавлен, не удалён и не изменён; новых `skill`-фактов не создавалось.

---

## 4. Полный текст `artifacts/master_resume.md`

```markdown
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
```

---

## 5. Полный текст `artifacts/master_resume.ru.md`

```markdown
Mikhail Kolesnikov
Application Support & IT Support | Bitrix24 / CRM Automation | Python Automation & AI Projects | Systems & Network Administration
Thailand · Remote · mikhailthaiban@gmail.com · Telegram: @brokeassh

## КРАТКОЕ ОПИСАНИЕ

IT-специалист с 15+ лет опыта в IT: техническая поддержка, администрирование систем, Bitrix24/CRM, автоматизация и собственные проекты, построенные с помощью AI-инструментов. Интеграция API, автоматизация процессов и LLM-системы, применяемые в работе и в собственных проектах. Строит сквозные системы на Python, LLM (OpenAI, Gemini, Anthropic), REST/webhook-интеграциях, Telegram-ботах, n8n/Make-воркфлоу и инфраструктуре Linux/Docker. Английский B1, полностью удалённо, часовой пояс ICT (+7).

## КЛЮЧЕВЫЕ НАВЫКИ

- **AI & LLM:** AI agents, Anthropic API, Google Gemini API, OpenAI API, RAG, classification, content generation, fact-checking, open-source LLMs, prompt engineering, summarization, transcription, workflow automation
- **Automation & delivery:** Bash, Docker, Git, Linux, MS SQL, Make, PostgreSQL, PowerShell, SQL, n8n
- **Python & integrations:** Google Workspace APIs, MTProto, Python, REST APIs, Telegram Bot API, Telethon, aiogram, asyncio, webhooks
- **IT Support & Systems Administration:** Windows Server, Active Directory, Exchange, Hyper-V, Networking, IP Telephony, IT Support, Bitrix24

## ОПЫТ РАБОТЫ

### AI Automation Engineer | Sayama Travel

*Thailand | November 2022 – April 2026*

- Администрировал инфраструктуру Linux и Docker (службы systemd, логи, мониторинг); настраивал VPN-инфраструктуру, поддерживал резервное копирование и сетевую безопасность.
- Автоматизировал бизнес-процессы вокруг Bitrix24: воркфлоу, роботы, триггеры, телефонию, почту, аналитику и каналы лидогенерации.
- Построил пайплайн голосовых сообщений: транскрибация через Whisper, затем AI-суммаризация и сохранение в сделках Bitrix24.
- Разрабатывал Telegram-ботов и асинхронные пайплайны передачи медиа на Python, aiogram, Telethon и MTProto.
- Разрабатывал n8n-воркфлоу, связывающие Bitrix24, Google Sheets, Telegram-каналы и внешние базы данных.
- Интегрировал OpenAI и Google Gemini в корпоративные чаты и CRM-процессы: обработка входящих заявок, классификация лидов, генерация ответов на разных языках.

### Bitrix24 Technical Support Specialist | Avantis

*Moscow | April 2021 – August 2022*

- Администрировал self-hosted Bitrix24; настраивал интеграции, бизнес-процессы, роботов автоматизации и системные воркфлоу.
- Оказывал техническую поддержку и диагностику для бизнес-пользователей.

### Earlier IT Infrastructure & Support Experience | Speedcast / Germsat; Intravision; Slavyanka; Russian Post

*2009 – 2018*

- Автоматизировал рутинные административные задачи на Bash и PowerShell.
- Работал с инфраструктурой Windows Server и Linux, Active Directory, Exchange, SQL, Hyper-V, сетями, IP-телефонией, мониторингом, тестированием API/веб-сервисов и ITSM-системами.

*Speedcast / Germsat: март 2018 – март 2021 (период подтверждён кандидатом).*

## СОБСТВЕННЫЕ ПРОЕКТЫ

### Собственные проекты в этот период

*Thailand | May 2026 – Present*

В этот период оплачиваемых заказов не было (подтверждено кандидатом).

- Строю асинхронные ETL-пайплайны на Python с asyncio, Telethon/MTProto и внешними API.
- Строю n8n-воркфлоу, связывающие базы данных, AI-агентов, Google Workspace API, CRM-системы, уведомления и внешние сервисы.
- Проектирую и реализую сквозные пайплайны AI-автоматизации для обработки и распространения контента (RAG, prompt engineering, AI-агенты).
- Разрабатываю Telegram-ботов на aiogram с внешними API, очередями задач и управлением состоянием на FSM.
- Разрабатываю процессы обработки медиа с интеграцией Google Drive, Whisper, LLM и Telegram.

### Job Search AI (Job Search Hub) — Реализовано

Персональная система автоматизации поиска работы. Собирает удалённые вакансии, оценивает их по профилю кандидата, готовит адаптированные отклики и — за явными предохранителями — может заполнять и отправлять формы отклика на HH.ru, отвечать на сообщения HH и готовить ответы по email.

- Всё, что взаимодействует с внешним миром (браузер, HH, email), закрыто предохранителями: режим по умолчанию — REVIEW (решение принимает человек); режимы AUTO включаются только явно через kill-switch в окружении и никогда не додумывают факты.
- Собирает вакансии с удалённых и технических джоб-бордов (habrcareer, remoteok, weworkremotely, himalayas; ранее: linkedin, wellfound).
- Управляемый поток отклика: план → предзаполнение → ворота ручной проверки → одна отправка под предохранителем → проверка в режиме чтения.

### Reels Analyzer — Реализовано

Telegram-бот и асинхронный воркер-пайплайн для анализа коротких видео (Instagram Reels, TikTok, YouTube Shorts): извлекает транскрипты и визуальные доказательства, проверяет утверждения по источникам и выдаёт структурированный результат с опорой на доказательства.

- Принимает ссылки на видео через Telegram-бота (aiogram) и ставит их в очередь на обработку (асинхронная очередь задач Redis + ARQ, хранилище задач на PostgreSQL).
- Проверяет утверждения по веб-источникам (Exa) с контролем качества.

**Stack:** Python 3.13, aiogram, ARQ + Redis, PostgreSQL (asyncpg/SQLAlchemy), Pydantic, Google Gemini API, Exa, Docker / docker-compose.

### Repiton — Прототип

REPITON — AI-тренер произношения. Он учит говорить так, чтобы носитель языка правильно понял слово, а не просто подтверждает, что Speech-to-Text угадал текст.

- `Listen → Repeat → Analyze → Explain → Retry → Master`
- **STAGE_1_FINAL_NO_GO.** По итогам однократного sealed-final эксперимента на 1 218 записях 6 независимых дикторов C-COST (macro-F1 = 0.702 < 0.80, low recall = 0.318 < 0.65)
- Stage 1 остаётся **NO-GO**: текущая модель неустойчива между дикторами, а learner/error gold evidence отсутствует.
- **TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO`

### WHUBN — Реализовано

WHUBN — local-first iOS-приложение, которое восстанавливает, где происходила жизнь человека, по разрешённым данным о местоположении: метаданным фотографий и импорту истории перемещений.

- Высоконагруженный движок хранения на SQLite с WAL и конкурентностью Swift 6 (Sendable)
- Интерактивная 2D-карта жизни на Apple MapKit (`AtlasMapView`, `AtlasMKMapView`)
- Детерминированная хронологическая сегментация записей `LocationEvent` в канонические `Stay` (`StaySegmentationEngine`)

## ОБРАЗОВАНИЕ

- **Сахалинский государственный университет** — Технологический институт, технология и предпринимательство, высшее образование, 2005

## ЯЗЫКИ

- Английский — B1 (средний)
- Русский — родной

## КОНТАКТЫ

- mikhailthaiban@gmail.com
- Telegram: @brokeassh
- Thailand
- Remote
```

---

## 6. `artifacts/master_resume_sources.md` — что изменилось

Шапка и баланс не изменились: `Knowledge base version: 27`, `122`, `113 / 5 / 4`,
`Claims accounted for: 122 of 122`. Появились два новых раздела, третий исчез.

### 6.1 Новый раздел «Core-skills groups sourced from duties»

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

### 6.2 Новый раздел «Approved editorial decisions»

> **Professional summary — opening**
> IT specialist with 15+ years in IT: technical support, systems administration, Bitrix24/CRM, automation, and my own projects built with AI tools.

> **Tenure — the figure that is published**
> 15+ years in IT

> **Projects — the phrase 'in production'**
> used in my work and in my own projects

> **Core skills — the support & systems group**
> IT Support & Systems Administration

У каждой записи — почему она защитима. У числа стажа, в частности, приведена
арифметика: блок начинается в 2009, последняя наёмная роль кончается в апреле 2026,
это около 17 лет; `domain_years.it_support = 11.0` меряет домен поддержки, а
`years_experience = 3` — третье число и не общий стаж.

### 6.3 Исчез раздел «Proposals awaiting approval»

Утверждать больше нечего: все три прежних предложения утверждены. Раздел печатается
только при непустом `PROPOSALS`; `test_the_source_map_carries_the_brief_map_and_the_decisions`
это фиксирует.

---

## 7. Пример Job-to-Candidate Brief после исправлений

Вакансия `habrcareer:1000168093` — «PHP/Bitrix24 разработчик (Коробка/On-premise)»,
ТехноГрупп, Москва. Краткая версия целиком:

```
ВАКАНСИЯ: Требуется «PHP/Bitrix24 разработчик (Коробка/On-premise)» (Москва)
Работодатель: ТехноГрупп
Зарплата: 24 (валюта в источнике не указана) — сравнить нельзя, минимум кандидата $1000
Формат: не удалённо | География: Москва
Уровень: mid

ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ:
  • [обязательно] #Битрикс24
      — Administered self-hosted Bitrix24; configured integrations… (Bitrix24 Technical Support Specialist)
      — Developed n8n workflows connecting Bitrix24, Google Sheets… (AI Automation Engineer)
  • [обязательно] #Docker
      — Administered Linux and Docker infrastructure… (AI Automation Engineer)
      — дополнительно, свой проект (не оплачиваемая работа): Reels Analyzer

ЧАСТИЧНО:
  • [обязательно] #GitLab

ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ БЕЗ ПОДТВЕРЖДЕНИЯ:
  • #JavaScript
  • #PHP
  • #MySQL
  • #CI/CD
  • #RabbitMQ
  • #Jira

Отсутствие подтверждения в доступных источниках — не доказательство отсутствия навыка.

ЧТО УТОЧНИТЬ (влияет на отклик):
  • Есть ли опыт с «#JavaScript»? В KB подтверждения нет.
  • Есть ли опыт с «#PHP»? В KB подтверждения нет.
  • Есть ли опыт с «#MySQL»? В KB подтверждения нет.

ЧТО ВЫДЕЛИТЬ В АДАПТИРОВАННОМ РЕЗЮМЕ:
  • Опыт работы — #Битрикс24 (AI Automation Engineer, Bitrix24 Technical Support Specialist)
  • Опыт работы — #Docker (AI Automation Engineer)

ФИЛЬТРЫ ПОИСКА ПРОТИВОРЕЧАТ ПОДТВЕРЖДЁННЫМ ПРЕДПОЧТЕНИЯМ (T6 их не меняет):
  • candidate_profile.json → minimum_salary: сейчас 1500 USD → нужно 1000 USD (подтверждено на интервью)
  • candidate_profile.json → excluded_roles, must_avoid_conditions: сейчас Bitrix24 в списке исключений → нужно рассматривать наравне с другими вакансиями (подтверждено на интервью)
  • candidate_profile.json → years_experience / years_of_experience: сейчас 3 → нужно не подставлять; утверждено только «15+ лет в IT»
  • hh_autonomous_agent.DEFAULT_SEARCH_QUERIES: сейчас Python Developer, Backend Python Developer, AI Engineer, AI Agent Engineer, Python FastAPI, Automation Engineer Python → нужно запросы также по: IT Support, System Administration, Bitrix24/CRM

В вакансии не указано: язык работы, валюта зарплаты.
Общий опыт в IT: 15+ лет — подтверждено пользователем
В профиле years_experience=3; что именно означает это поле, не установлено, поэтому оно не используется ни как общий стаж, ни как стаж в специализации.
```

Что видно на телефоне: два обязательных требования подтверждены, оба — оплачиваемой
работой; свой проект назван прямо как «не оплачиваемая работа»; непокрытое честно
перечислено; строка «НЕЗАВЕРШЁННЫЙ ЭТАП» исчезла.

### 7.1 Пример требования, понижённого в классе

Python Developer (`hh:136706048`), требование «SQL и PostgreSQL — уверенно…»:

```
ЧАСТИЧНО:
  • [пожелание] SQL и PostgreSQL — уверенно; опыт хотя бы с одной аналитической или NoSQL СУБД (ClickHouse, Elasticsearch, Mon…
      — SQL (резюме)
      — PostgreSQL (резюме)
```

Объяснение (из `--details`):
«Требование называет «postgresql», «redis», «sql», а оплачиваемые обязанности — только
«sql». Совпадение частичное: класс A требует оплачиваемой работы именно с этой
технологией, а не с соседней.»

До T6.2 здесь стояло «ПОДТВЕРЖДЕНО ОПЫТОМ РАБОТЫ» с обязанностью, где написано только
SQL.

---

## 8. Краткий отчёт о тестах

| Что | Команда | Результат |
| --- | --- | --- |
| Модуль Job-to-Candidate Brief | `pytest tests/test_candidate_job_brief.py` | **99 passed, 0 failed** (было 86 в T6.1) |
| Модуль master-резюме | `pytest tests/test_candidate_master_resume.py` | **65 passed, 0 failed** (было 59 в T5.1) |
| Оба модуля вместе | `pytest tests/test_candidate_job_brief.py tests/test_candidate_master_resume.py` | **164 passed, 0 failed** (`.pytest_t62_08.xml`) |
| Мутационные пробы T5 | `tools/t5_guard_mutation.py` (4 батча) | **21/21 CAUGHT** |
| ruff (версия из `ruff.toml`, 0.16.6) | `uvx ruff@0.16.6 check …` | чисто, кроме унаследованного `N999` у dot-префиксного пробника |
| Полная регрессия | `pytest tests/` | см. §8.2 |

### 8.1 Что добавлено в тесты

**Job-to-Candidate Brief** (86 → 99 тестов):

- `test_a_class_a_technology_is_named_by_the_paid_claim_that_carries_it` — замена
  прежнего теста, который требовал коммерческого подтверждения для PostgreSQL;
  теперь проверяется, что несущий факт **называет** технологию.
- `test_postgresql_is_never_confirmed_by_a_duty_that_only_says_sql` — Часть 1, п. 1.
- `test_a_duty_that_names_bash_and_powershell_never_confirms_python` — Часть 1, п. 2.
- `test_a_technology_requirement_drops_evidence_that_names_none_of_it` — Часть 1, пп. 3 и 5.
- `test_a_partial_match_of_a_neighbouring_technology_never_raises_the_class` — Часть 1, п. 4.
- `test_no_class_a_answer_rests_on_evidence_that_names_none_of_its_technologies` — то же
  правило сразу по всем пяти вакансиям.
- `test_the_independent_role_declaration_is_shared_with_the_generator`,
  `test_the_own_work_declaration_is_a_confirmed_fact_about_that_role` — объявление
  читается из `master_resume`, а не дублируется.
- `test_every_freelance_duty_is_own_work_and_never_commercial` — все 5 обязанностей
  Freelance, с `role_id`.
- `test_the_own_work_role_has_a_company_but_no_employer` — почему блок неразличим по
  своим полям: `role_company` есть, `role_employer` нет, `role_title` совпадает с наёмной ролью.
- `test_class_a_uses_only_evidence_from_hired_work` — по всем пяти вакансиям.
- `test_a_freelance_duty_is_never_printed_as_paid_work` — то же с телефона.
- `test_technology_tenure_is_never_taken_from_the_total_it_history` — «5 лет Technical
  Lead» не получает 15 лет IT.
- `test_the_t5_2_stage_is_recorded_as_finished` — нота T5.2 читает файлы.
- `test_the_phone_view_only_announces_an_unfinished_stage_when_there_is_one` — правка
  жёстко вписанной строки.

**master-резюме** (59 → 65 тестов):

- `test_the_four_approved_edits_are_in_the_files_on_disk` — **читает файлы с диска**,
  все четыре правки в обеих языковых версиях, и заменённых формулировок там нет.
- `test_the_files_on_disk_are_what_the_generator_produces_now` — устаревший черновик
  валит тест.
- `test_the_shipped_source_map_attributes_the_approved_wording` — карта источников.
- `test_every_duty_sourced_skill_names_its_source` — каждый из 8 навыков называет свою
  обязанность, и обязанность содержит нужную фразу.
- `test_the_duty_sourced_group_reaches_both_drafts` + `test_an_approved_decision_is_not_still_listed_as_a_proposal`.

Переписаны: `test_the_approved_edit_is_still_present_to_be_applied`,
`test_the_summary_states_the_approved_tenure_and_no_other_figure`,
`test_the_summary_opens_with_the_approved_opening`,
`test_the_tenure_decision_shows_its_arithmetic`,
`test_the_source_map_carries_the_brief_map_and_the_decisions`.

### 8.2 Полная регрессия

Запущена после всех правок: `.venv/Scripts/python.exe -m pytest tests/ -q --junitxml=.pytest_t62_full.xml`.
Вердикт берётся из junit, а не из кода возврата: сторож массовых удалений убивает pytest
на уборке `%TEMP%\pytest-of-Misha\`, поэтому `EXIT=1` возможен при `failures=0`.

**Отдельно про `test_report_line_refs`:** ратчет ссылок `docs/ble001_triage.md` красный
(24 при пороге 18) и был красным **до** всей работы T1–T6.2 — это записанный базис
проекта, а не следствие T6.2. T6.2 этот отчёт не трогал; отчёт о задаче лежит отдельным
файлом, который ратчет не читает. Порог не поднимался.

### 8.3 Мутационная проверка нашла две ошибки в моей же работе

Прибор `tools/t5_guard_mutation.py` — не формальность, и в этот раз он поймал меня:

1. **`approved_decision_back_in_the_waiting_room` — SURVIVED.** Проба добавляла в
   `PROPOSALS` произвольную запись, а не **утверждённую**; пин проверяет пересечение
   множеств, и пересечения не возникало. Проба была бессмысленной, а не пин слепым.
   После правки пробы (подставляется `APPROVED_TENURE`) — **CAUGHT**.
2. **`duty_sourced_group_dropped` — INVALID: ANCHOR STALE (found 2, expected 1).**
   Якорь `for heading, entries in DUTY_SOURCED_SKILL_GROUPS:` встречается дважды — в
   выпуске и в карте источников. Якорь удлинён до уникального — **CAUGHT**.

Обе правки внесены в пробу, а не в пин. Итог: **21/21 CAUGHT**, дерево восстановлено
(`grep -c MUTATION` = 0).

---

## 9. Что не делалось

- `resume.md`, `candidate_profile.json`, `candidate_kb.json`, `state.db`, статусы фактов
  KB, действующие фильтры и правила отправки — не изменялись.
- Новых `skill`-фактов в KB не создавалось: группа навыков объявлена в генераторе и
  опирается на существующие обязанности.
- Реальные отклики не запускались, платные API не вызывались, `git push` не делался,
  T7–T8 не начинались.

## 10. Известные ограничения

1. **Словарь технологий строится только из KB.** Если требование называет технологию,
   которой кандидат нигде не заявил (`vLLM`, `Ollama`, `GitLab CI`, `Jenkins`), словарь
   её не видит, `wanted` пусто, и требование классифицируется по рабочим словам. Так,
   требование «Docker; опыт настройки CI/CD-пайплайнов (GitLab CI, GitHub Actions,
   Jenkins)» получает класс A по Docker, хотя про CI/CD в KB ничего нет. Это свойство
   конструкции («матчить только по тому, что кандидат действительно заявил»), а не
   следствие T6.2; чтобы закрыть, нужны новые факты в KB, а их добавлять запрещено.
2. **Требование без распознанных технологий** (например, «Experience with domains,
   hosting, DNS») остаётся классом A по обязанности поддержки. Формально это тот же
   случай, что и п. 1: словарь не знает слов `DNS`, `hosting`, `domains`. Понижать
   такие требования вслепую означало бы выкинуть лучшие совпадения в вакансии на
   техподдержку.
3. `_canonical_terms_in` не матчит `PostgreSQL` против `postgresql` из-за заглавных букв
   в lookbehind. Сегодня это работает в нужную сторону (не даёт ложного подтверждения),
   но это случайность реализации, а не проверенное свойство.
4. Требования вроде «SQL и PostgreSQL» считаются одной конъюнкцией. Если вакансия
   перечисляет альтернативы («или то, или это»), конъюнкция может понизить класс
   сильнее, чем нужно. Ошибка в безопасную сторону.
