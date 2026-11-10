# CONTRA-WORK-ITEMS-PREP — 4 готовые карточки

Ничего в Contra не публиковалось. Bot detection не обходился. Все данные ниже взяты из
существующих файлов, живых дашбордов и проверенных публичных репозиториев; выдуманных
клиентов, revenue, пользователей и результатов нет.

**Позиционирование**: AI automation / integrations / internal tools / workflow systems.
AI подан как инженерный инструмент, не как «генерация контента».

**Порядок**: 1) Job Search Hub · 2) Reels Analyzer · 3) Reels2Action · 4) Remote / Hermes Dashboard.

**Про авторство (важно, constraint из задания)**: во всех четырёх кейсах роль сформулирована
честно — вы проектировали систему и руководили сборкой, значительная часть кода написана
AI coding-агентами под вашим ревью. Формулировка «hand-wrote everything» нигде не используется.

---

## Общая инструкция по медиа

Composer Contra требует **обязательное** изображение и текст («Post text is required» +
«Please add at least one image or video»). Поэтому для каждого кейса ниже указано
**конкретное изображение**, что на нём должно быть видно и рекомендуемый crop.

**Оговорка про язык интерфейса.** Все существующие скриншоты — с русскоязычным UI
(Job Search Hub и Hermes OS). Для англоязычной аудитории Contra это не идеально, но это
**реальные** кадры, а не нарисованные. Если хотите английский UI — вариант указан в конце
документа; я не стал подменять интерфейс.

---

# Кейс 1 — Job Search Hub / Automation

## A. Title

```
Job Search Hub — multi-source vacancy pipeline with a review-first dashboard
```

## B. Short description

```
I was spending hours a day reading job boards and repeating the same steps: filter,
tailor a resume, write a cover letter, track what was sent. Vacancies were spread across
several boards, each with its own format.

So I built a pipeline that collects from five job boards, normalizes everything into one
schema, deduplicates into a canonical record, and scores each vacancy against a structured
candidate profile. For the ones that pass, it generates an application package: an adapted
resume, a cover letter, and answers to screening questions built only from facts that
actually exist in the profile.

Everything that touches the outside world is gated. Browser form filling and message
replies run in REVIEW mode by default, so a human approves each action, and HH.ru
submissions pass eleven fail-closed checks — form fingerprint match, strict domain check,
vacancy match, and a guard against submitting twice. The dashboard shows every stage:
collected, scored, queued, ready to apply, submitted.

Python with Playwright/CDP for browser automation, Pydantic schemas, SQLite for state, and
LLM APIs for deep analysis with an offline fallback. This is my own system, built for my
own search — not a client project.
```

## C. My role

```
Designed the system and directed its build; implementation largely produced with AI coding agents under my review.
```

## D. Skills

```
Python · Playwright · Browser Automation · API Integration · Data Pipelines · Web Scraping · SQLite · Pydantic · LLM Integration · Workflow Automation
```

## E. Link

```
https://github.com/mikheooo/job-search
```

Проверено: HTTP 200, публичный репозиторий.

## F. Media

| Пункт | Значение |
| :--- | :--- |
| **Source** | `artifacts/contra_work_dashboard_8000.png` — живой дашборд (снят с работающего приложения на `localhost:8000`) |
| **Альтернатива (устарела)** | `artifacts/contra_work_items/job-search-vacancies.png` — версия с портфолио, цифры старые (592 вакансии, авг. 2026) |
| **Что должно быть видно** | Шапка «Job-Search Hub» + четыре KPI-карточки: всего вакансий **2 529**, в очереди, сгенерировано пакетов **35**, статус откликов **10 откликов**. Ниже — таблица вакансий с бейджами источника (`himalayas`) и вердикта (`Ineligible` / `Review Req`), колонки: источник, компания, должность, зарплата, действие |
| **Рекомендуемый crop** | Обрезать до шапки + строки KPI + первых 5–6 строк таблицы. Убрать пустое поле справа и нижний край. ~1600×1000 |

**Если хотите свежий кадр**: откройте `http://localhost:8000/` (дашборд уже запущен на порту 8000, `ai_assistant.cli ui`), вкладка **Вакансии**, и снимите как выше. Цифры на живом дашборде выше портфельных — кадр будет актуальнее.

---

# Кейс 2 — Reels Analyzer

## A. Title

```
Reels Analyzer — turning short video into fact-checked, structured tasks
```

## B. Short description

```
Short-form video is where I find a lot of useful tools and workflows, but re-watching a
Reel to work out what it actually claims — and then verifying those claims by hand — does
not scale. I wanted the video to arrive as a structured, checked brief instead of a link I
have to re-watch.

A Telegram bot takes a Reel, TikTok or Shorts URL, queues the job, and returns a structured
analysis: transcript, the claims and promises made, monetization hypotheses, calls to
action, and reproducibility notes. Every field is typed by a schema rather than free text,
so the output can be consumed by other tools instead of read once and forgotten.

A worker downloads the video, extracts the transcript and key frames, runs language-aware
LLM analysis, then fact-checks the claims against web sources before anything is published.
A deterministic priority policy decides which clips are worth a full analysis, so the
expensive calls are not spent on low-signal videos.

Python 3.13, aiogram, ARQ workers over Redis, PostgreSQL via SQLAlchemy/asyncpg, Pydantic
schemas, Gemini for analysis, Exa for source checks, FFmpeg for media, Docker Compose for
deployment. Personal project.
```

## C. My role

```
Designed the system and directed its build; implementation largely produced with AI coding agents under my review.
```

## D. Skills

```
Python · Telegram Bot (aiogram) · Redis · ARQ · PostgreSQL · LLM Integration · Gemini API · Fact Checking · FFmpeg · Docker
```

## E. Link

```
https://github.com/mikheooo/reels_bot
```

Проверено: HTTP 200, публичный репозиторий.

## F. Media

| Пункт | Значение |
| :--- | :--- |
| **Source (основной)** | `artifacts/contra_work_items/reels_jules_drawer.png` — карточка разбора с меткой **Reels Analyzer** |
| **Что должно быть видно** | Правая панель детали: тег `Reels Analyzer`, ссылка **«Открыть ролик в Instagram»**, блок «Основная информация» с сутью и проблемой, блок «Ресурсы и метрики затрат», раздел «Детальный разбор (Markdown)» с раскрытыми пунктами «Практическая цель» и «Практическая ценность» |
| **Рекомендуемый crop** | Оставить **только правую панель**, затемнённую доску слева убрать. Обрезать по нижней границе видимого блока «Практическая ценность». ~1000×1000 (квадрат) |
| **Альтернатива** | `artifacts/contra_work_items/reels_tasks.png` — доска задач с карточкой «Идея из видео (Reels)», тегами `ВЫСОКИЙ` / `Ролики` и счётчиками 110 задач. Показывает результат пайплайна как рабочий процесс; crop — левая колонка «АКТИВНЫЕ ЗАДАЧИ», ~900×1100 |

---

# Кейс 3 — Reels2Action

## A. Title

```
Reels2Action — async worker that turns Reels and Shorts into action items
```

## B. Short description

```
Two different jobs were getting mixed up inside one bot: deep analysis of a video, and
simply turning a video into a task I could act on. I split them out. Reels2Action is the
smaller, single-purpose one — it exists to produce tasks, not commentary.

It takes a Reel, TikTok or Shorts URL in Telegram and returns structured tasks and action
items: the idea in the video converted into a brief that can be executed, rather than a
summary that has to be interpreted. Tasks land on a small kanban board the bot itself
serves, so each one moves through queued, active and done instead of being buried in chat.

The bot layer handles URL submission, status updates and the board; an ARQ worker over Redis
handles downloading, transcript extraction and structured LLM prompt processing. The whole
thing comes up from docker-compose — bot, worker, PostgreSQL and Redis.

Python, aiogram, ARQ + Redis, PostgreSQL via asyncpg, Pydantic schemas, Google Gemini for
analysis, Docker Compose. Separate repository and a separate bot from Reels Analyzer by
design — the two are never merged. Personal project.
```

## C. My role

```
Designed the system and directed its build; implementation largely produced with AI coding agents under my review.
```

## D. Skills

```
Python · Telegram Bot (aiogram) · ARQ · Redis · Docker Compose · LLM Integration · Gemini API · Async Processing · PostgreSQL · Workflow Automation
```

## E. Link

```
https://github.com/mikheooo/reels2action
```

Проверено: HTTP 200, публичный репозиторий. Замечание для честности: репозиторий молодой
(3 коммита, 0 звёзд) — это нормально для личного проекта, но если для вас важна «весомость»
ссылки, можно вести на `reels_bot` и упомянуть этот как отдельный сервис.

## F. Media

**Готового скриншота нет.** В `C:\Users\Misha\reels2action` и в `C:\Users\Misha\reels_bot\scratch`
изображений этого бота нет — поэтому вымышленный screenshot я не рисую, а указываю, что снять.

| Пункт | Значение |
| :--- | :--- |
| **Что открыть** | Telegram, чат **бота Reels2Action** (тот, что в `reels2action/bot/`), не чат Reels Analyzer — проекты нельзя путать |
| **Что сделать перед снимком** | Отправить боту одну ссылку на Reels/Shorts и дождаться ответа, чтобы в кадре был виден **и запрос, и результат** |
| **Что должно быть видно** | Сообщение с отправленной ссылкой и следующее сообщение бота со структурированными задачами/action items |
| **Рекомендуемый crop** | Только область переписки с ботом, без списка чатов и без системных панелей телефона. ~900×1200 (портрет) |
| **Обязательно** | В кадр не должны попасть другие чаты, имена контактов, номера и любые личные данные. Если бот ещё не запущен — сначала `docker-compose up --build -d`, затем снимок |

---

# Кейс 4 — Remote / Hermes Dashboard

## A. Title

```
Hermes Dashboard — task board and drift detection for an external agent runtime
```

## B. Short description

```
Part of my automation stack runs outside the main repository, in a local agent runtime.
Two things kept failing quietly: the deployed copies of the runtime's scripts drifting away
from the canonical ones, and tasks piling up with no single place to see their state.

So I built a dashboard that renders the runtime's task list — active and completed, with
priority, tags and source — plus an integration module that inspects the external runtime,
detects drift between deployed and canonical files by SHA-256, and synchronizes them
deterministically.

The integration layer is version-controlled and tested inside my job-search repository. The
dashboard front end is a local single-file server and is not published as a public
repository, so there is no link to it.

Python, FastAPI, SHA-256 drift detection, local HTTP serving. Internal tooling, built for
my own workflow.
```

## C. My role

```
Designed the system and directed its build; implementation largely produced with AI coding agents under my review.
```

## D. Skills

```
Python · FastAPI · Internal Tools · Integration · Automation · Dashboard Development · Drift Detection · Local Deployment
```

## E. Link

```
No standalone public repository for the dashboard. The Hermes integration itself is public:
https://github.com/mikheooo/job-search — see integrations/hermes/ and ai_assistant/hermes_integration.py
```

Проверено: `integrations/hermes/job_search_fetcher.py`, `integrations/hermes/telegram_adapter_hook.py`
и `ai_assistant/hermes_integration.py` присутствуют в `origin/main` (проверено `git cat-file -e`).

**Чего не делаю**: не выдаю за своё `hermes-webui` (это репозиторий `nesquena/hermes-webui`)
и не ссылаюсь на `hermes-agent` как на свою работу — это форк `NousResearch/hermes-agent`.

## F. Media

| Пункт | Значение |
| :--- | :--- |
| **Source** | `artifacts/contra_work_dashboard_8050.png` — живой дашборд Hermes OS (снят с `localhost:8050`) |
| **Что должно быть видно** | Слева навигация и статус «Синхронизировано»; в шапке «Задачи» со счётчиками **Всего 112 (активных 81, выполненных 31)**; две колонки — «АКТИВНЫЕ ЗАДАЧИ» и «ВЫПОЛНЕННЫЕ ЗАДАЧИ» с теми же числами на бейджах. В карточках — теги `Ролики`, `AI`, `Исследование`, `Автоматизация` |
| **Рекомендуемый crop** | Оставить шапку со счётчиками и две колонки с 2–3 карточками в каждой. Убрать браузерные панели. ~1600×900 (16:9) |

**Если хотите свежий кадр**: дашборд обслуживается локальным `dashboard_server.py` на порту
8050 — откройте `http://localhost:8050/`, дождитесь «Синхронизировано» и снимайте по описанию
выше.

---

# Готовые блоки для copy/paste

Ниже — ровно то, что вставляется в Contra. Каждый блок самодостаточен: первая строка — заголовок,
далее описание, затем роль, навыки и ссылка.

## Блок 1

```
Job Search Hub — multi-source vacancy pipeline with a review-first dashboard

I was spending hours a day reading job boards and repeating the same steps: filter, tailor a
resume, write a cover letter, track what was sent. Vacancies were spread across several boards,
each with its own format.

So I built a pipeline that collects from five job boards, normalizes everything into one schema,
deduplicates into a canonical record, and scores each vacancy against a structured candidate
profile. For the ones that pass, it generates an application package: an adapted resume, a cover
letter, and answers to screening questions built only from facts that actually exist in the profile.

Everything that touches the outside world is gated. Browser form filling and message replies run
in REVIEW mode by default, so a human approves each action, and HH.ru submissions pass eleven
fail-closed checks — form fingerprint match, strict domain check, vacancy match, and a guard
against submitting twice. The dashboard shows every stage: collected, scored, queued, ready to
apply, submitted.

Python with Playwright/CDP for browser automation, Pydantic schemas, SQLite for state, and LLM APIs
for deep analysis with an offline fallback. This is my own system, built for my own search — not a
client project.

Role: Designed the system and directed its build; implementation largely produced with AI coding
agents under my review.
Skills: Python, Playwright, Browser Automation, API Integration, Data Pipelines, Web Scraping,
SQLite, Pydantic, LLM Integration, Workflow Automation.
Link: https://github.com/mikheooo/job-search
```

## Блок 2

```
Reels Analyzer — turning short video into fact-checked, structured tasks

Short-form video is where I find a lot of useful tools and workflows, but re-watching a Reel to
work out what it actually claims — and then verifying those claims by hand — does not scale. I
wanted the video to arrive as a structured, checked brief instead of a link I have to re-watch.

A Telegram bot takes a Reel, TikTok or Shorts URL, queues the job, and returns a structured
analysis: transcript, the claims and promises made, monetization hypotheses, calls to action, and
reproducibility notes. Every field is typed by a schema rather than free text, so the output can be
consumed by other tools instead of read once and forgotten.

A worker downloads the video, extracts the transcript and key frames, runs language-aware LLM
analysis, then fact-checks the claims against web sources before anything is published. A
deterministic priority policy decides which clips are worth a full analysis, so the expensive calls
are not spent on low-signal videos.

Python 3.13, aiogram, ARQ workers over Redis, PostgreSQL via SQLAlchemy/asyncpg, Pydantic schemas,
Gemini for analysis, Exa for source checks, FFmpeg for media, Docker Compose for deployment.
Personal project.

Role: Designed the system and directed its build; implementation largely produced with AI coding
agents under my review.
Skills: Python, Telegram Bot (aiogram), Redis, ARQ, PostgreSQL, LLM Integration, Gemini API, Fact
Checking, FFmpeg, Docker.
Link: https://github.com/mikheooo/reels_bot
```

## Блок 3

```
Reels2Action — async worker that turns Reels and Shorts into action items

Two different jobs were getting mixed up inside one bot: deep analysis of a video, and simply
turning a video into a task I could act on. I split them out. Reels2Action is the smaller,
single-purpose one — it exists to produce tasks, not commentary.

It takes a Reel, TikTok or Shorts URL in Telegram and returns structured tasks and action items:
the idea in the video converted into a brief that can be executed, rather than a summary that has
to be interpreted. Tasks land on a small kanban board the bot itself serves, so each one moves
through queued, active and done instead of being buried in chat.

The bot layer handles URL submission, status updates and the board; an ARQ worker over Redis
handles downloading, transcript extraction and structured LLM prompt processing. The whole thing
comes up from docker-compose — bot, worker, PostgreSQL and Redis.

Python, aiogram, ARQ + Redis, PostgreSQL via asyncpg, Pydantic schemas, Google Gemini for
analysis, Docker Compose. Separate repository and a separate bot from Reels Analyzer by design —
the two are never merged. Personal project.

Role: Designed the system and directed its build; implementation largely produced with AI coding
agents under my review.
Skills: Python, Telegram Bot (aiogram), ARQ, Redis, Docker Compose, LLM Integration, Gemini API,
Async Processing, PostgreSQL, Workflow Automation.
Link: https://github.com/mikheooo/reels2action
```

## Блок 4

```
Hermes Dashboard — task board and drift detection for an external agent runtime

Part of my automation stack runs outside the main repository, in a local agent runtime. Two things
kept failing quietly: the deployed copies of the runtime's scripts drifting away from the canonical
ones, and tasks piling up with no single place to see their state.

So I built a dashboard that renders the runtime's task list — active and completed, with priority,
tags and source — plus an integration module that inspects the external runtime, detects drift
between deployed and canonical files by SHA-256, and synchronizes them deterministically.

The integration layer is version-controlled and tested inside my job-search repository. The
dashboard front end is a local single-file server and is not published as a public repository, so
there is no link to it.

Python, FastAPI, SHA-256 drift detection, local HTTP serving. Internal tooling, built for my own
workflow.

Role: Designed the system and directed its build; implementation largely produced with AI coding
agents under my review.
Skills: Python, FastAPI, Internal Tools, Integration, Automation, Dashboard Development, Drift
Detection, Local Deployment.
Link: https://github.com/mikheooo/job-search (integrations/hermes/ and ai_assistant/hermes_integration.py)
```

---

# Чеклист добавления (порядок сохранён)

1. Contra → профиль → вкладка **Work** → **Add work**.
2. Загрузить изображение по инструкции F для кейса 1 → дождаться диалога **Crop your photo** → **Save**.
3. Вставить **Блок 1** в поле текста → **Next** → завершить шаг.
4. Повторить для кейсов 2 → 3 → 4 в этом порядке.

---

# Что осталось за рамками

| Вопрос | Статус |
| :--- | :--- |
| Публикация в Contra | не выполнялась — по constraint задания |
| Обход bot detection для медиа | не выполнялся; именно поэтому шаги 2–4 делаются вручную |
| Выдуманные скриншоты | не создавались: для кейса 3 указан экран, который нужно снять |
| Поля после шага с медиа | **неизвестны** — мастер не пускал дальше без изображения, поэтому я не знаю, есть ли в Contra отдельные поля под role/skills/link. Блоки написаны так, чтобы работать как единый текст поста с ролью, навыками и ссылкой в конце |
| Английский UI на скриншотах | сейчас все кадры русскоязычные. Если нужен английский интерфейс — скажите, посмотрю, есть ли в дашбордах переключатель языка, и переснимем |

## Источники фактов

- `README.md` (job-search), живые дашборды `localhost:8000` и `localhost:8050`
- `C:\Users\Misha\reels_bot\README.md`, `C:\Users\Misha\reels_bot\.hermes.md`
- `C:\Users\Misha\reels2action\README.md`, `C:\Users\Misha\reels2action\.hermes.md`
- `ai_assistant/hermes_integration.py`, `integrations/hermes/` (в `origin/main`)
- Публичные репозитории `github.com/mikheooo/{job-search,reels_bot,reels2action}` — HTTP 200
