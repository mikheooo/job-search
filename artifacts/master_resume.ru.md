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
