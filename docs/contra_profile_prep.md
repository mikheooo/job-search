# CONTRA-PROFILE-PREP — аудит и подготовленные данные

Задача: подготовить профиль Contra, ничего не публикуя без подтверждения. Новый source adapter
не создавался, от Contra не отказываемся. Identity verification и wallet **не автоматизировались**.

---

## 1. Текущее состояние профиля (read-only)

**Профиль**: `contra.com/mikhail_bs409nwy` — Mikhail Kolesnikov, «ai developer», Bangkok, Thailand,
статус **Available**.

**Заполнение: 22 %**. Discoverability score: **24**.

### Что УЖЕ заполнено

| Поле | Значение |
| :--- | :--- |
| Имя | Mikhail Kolesnikov |
| Headline | `ai developer` |
| Локация | Bangkok, Thailand |
| Доступность | `Available` (включено) |
| «Get In Touch» | включено |
| Email | mikhailthaiban@gmail.com (подтверждён входом через Google) |
| Currency аккаунта | определяется как USD |

### Чего НЕ хватает — точный чеклист с дашборда

Дашборд `/independent/home` показывает блок «Finish setting up your workspace **4**» —
4 незакрытых пункта из 6. Ссылки ведут на конкретные места:

| # | Задача | Куда ведёт | Состояние |
| -: | :--- | :--- | :--- |
| 1 | **Add 4 pieces of work to your profile** | `/mikhail_bs409nwy/work?shareYourWork=true` | ⛔ 0 работ (пустое состояние: «Share quick snapshots…»), нужно 4 |
| 2 | **Add your rate** | `/mikhail_bs409nwy/about?addRate=true` | ⛔ ставка не указана |
| 3 | **Share a social link** | `/mikhail_bs409nwy/about?addLinks=true` | ⛔ на профиле кнопка «Add social links» |
| 4 | **Verify identity & add a payout account** | `/independent/wallet?addPayoutMethod=true` | ⛔ не сделано — **не автоматизирую** |
| 5 | **Add a profile one-liner** | — | ⛔ есть только headline «ai developer» |
| 6 | **Add a profile cover image or video** | — | ⛔ обложки нет |

Разделы профиля (`Work / Posts / Products / Reviews / Services / About`) существуют как вкладки,
но **пусты**. Кнопки `Add work`, `Add social links` присутствуют и доступны.

### Что уже есть на странице `settings` (не требует действий)

Email подтверждён, Birthdate, Calendar Link, Personal Information (адрес, legal entity, телефон),
Tax Information, Domains — поля существуют; Identity Verification имеет кнопку `Verify Identity`,
которая **остаётся за вами**.

---

## 2. Важное уточнение про лимит заказов

Чеклист профиля и лимит ленты — **две разные вещи**, и это важно не перепутать:

| | Что говорит |
| :--- | :--- |
| Профиль 22 % | «Complete profile» + «Finish setting up your workspace 4» |
| Вкладка заказов | «Get immediate access to all **233** job postings by **upgrading to Pro**» |
| Плашка над лентой | «[LIMITED] Get priority access to all jobs before everyone else. **Upgrade to Pro**» |
| Факт из P2.1 | `count: 233`, `edges: 3` — независимо от `preset`, `first`, пагинации |

То есть лимит **3 из 233** явно привязан к подписке **Pro**, а не к проценту профиля.
Заполнение профиля улучшит discoverability и даст клиентам видеть вас, но **не обязано**
открыть остальные 230 заказов — это проверяется опытом после ваших действий (шаг 6 задачи), а
не предполагается заранее. Что именно откроет заполнение профиля — выясним повторной проверкой.

---

## 3. Подготовленные данные (на подтверждение)

Всё ниже составлено **только из ваших реальных материалов**: `resume.md`, `candidate_profile.json`,
портфолио `mikheooo.github.io/portfolio` и публичных репозиториев `github.com/mikheooo`.
Ничего не выдумано. **Пока ничего не отправлено в Contra.**

### 3.1. Social link — 3 кандидата (нужно выбрать 1+, можно все)

| Куда | URL | Почему |
| :--- | :--- | :--- |
| **GitHub** | `https://github.com/mikheooo` | 8 публичных репозиториев с кодом — самое сильное доказательство для клиента |
| **LinkedIn** | `https://www.linkedin.com/in/mikheooo` | стандартный профессиональный профиль |
| **Portfolio** | `https://mikheooo.github.io/portfolio/` | ваш сайт с описанием проектов и скриншотами |

Рекомендую начать с **GitHub** (первым) — он ведёт к проверяемому коду.

### 3.2. Profile one-liner — 3 варианта

Headline «ai developer» слишком короткий. Кандидаты (все подтверждаются резюме):

- **A (рекомендую)**: `AI Automation Engineer — Python, LLM agents, n8n/Make workflows, Telegram bots and API integrations.`
- **B (короче)**: `I build AI automation: LLM workflows, async Python pipelines, API integrations.`
- **C (акцент на боль клиента)**: `Turn manual workflows into AI automation — Python, n8n, LLM APIs, Telegram bots.`

### 3.3. Rate — нужно ваше решение

Ваш профиль задаёт **пол `$1000/мес**. Contra просит **ставку** (обычно $/час и/или проект).
Считая от вашего пола: `$1000 / ~160 ч ≈ $6.25/ч` — это **минимум выживания**, а не позиционирование.
Поэтому предлагаю варианты, а не «одну правильную цифру»:

| Вариант | Ставка | Логика | Риск |
| :--- | :--- | :--- | :--- |
| Осторожный | **$15–20/ч** | чуть выше вашего пола, легко получить первые заказы и отзывы | недооценка опыта |
| **Сбалансированный (рекомендую)** | **$25–35/ч** | соответствует вашим реальным заказам: в P2.1 видели `USD 50–75/ч` у automation-ролей; в P2 `USD 25–50/ч` у support | может отсечь дешёвых клиентов |
| Амбициозный | **$40–55/ч** | близко к верхней границе наблюдаемых ставок Contra | меньше откликов на старте без отзывов |

Для проектной ставки предлагаю **(рекомендую)** вилку под ваши типовые задачи:

| Тип задачи | Вилка |
| :--- | :--- |
| Telegram-бот / небольшая автоматизация | `$300–700` |
| n8n/Make workflow + интеграции API | `$500–1200` |
| LLM-пайплайн (RAG, обработка контента) | `$800–2500` |
| Поддержка инфраструктуры / админ (месяц) | `$400–900` |

**Требуется ваше решение**: почасовая ставка и/или проектная вилка.

### 3.4. Четыре work items — по реальным проектам

Выбраны из ваших публичных репозиториев; каждый со ссылкой на исходники.

**Work item 1 — Job Search Hub**
- **Заголовок**: `Job Search Hub — multi-source vacancy pipeline with review-first dashboard`
- **Кратко**: собирает, нормализует, фильтрует и оценивает вакансии из нескольких источников; генерирует пакеты для отклика; показывает каждую стадию в дашборде с ручным подтверждением.
- **Стек**: Python, Playwright/CDP, Pydantic, SQLite, LLM APIs, dashboard
- **Почему подходит**: демонстрирует ровно то, что покупают на Contra — end-to-end автоматизацию с API-интеграциями и контролем человека.
- **Источник**: `github.com/mikheooo/job-search` (публичный; в портфолио: «592 vacancies collected», «27 application packages», «42 tracked applications»)

**Work item 2 — Reels Analyzer**
- **Заголовок**: `Reels Analyzer — Telegram pipeline that turns short video into verified action tasks`
- **Кратко**: анализирует короткие видео, извлекает визуальные доказательства и утверждения, проверяет источники, формирует структурированные задачи и отправляет их в таскборд для исполнения с ИИ.
- **Стек**: aiogram, Redis + ARQ, PostgreSQL, Gemini, Exa, FFmpeg
- **Источник**: `github.com/mikheooo/reels_bot`

**Work item 3 — Reels2Action**
- **Заголовок**: `Reels2Action — async workers turning Reels/TikTok/Shorts into structured tasks`
- **Кратко**: отдельный Telegram-бот и асинхронные воркеры, превращающие короткие видео в структурированные задачи с управлением состоянием.
- **Стек**: Python, aiogram, ARQ, Redis, asyncpg, Docker
- **Источник**: `github.com/mikheooo/reels2action`

**Work item 4 — AI Video Factory / Voicable AI (выбрать один)**
- **Заголовок**: `AI Video Factory — automated video generation pipeline`
- **Стек**: Python
- **Источник**: `github.com/mikheooo/ai-video-factory`
- альтернатива: `Voicable AI demo` (`github.com/mikheooo/voicable-ai-demo`)

> Все 4 пункта — ваши **реальные** репозитории. Текстовые описания составлены по фактическому
> содержанию резюме и портфолио; если описание проекта должно звучать иначе — скажите, поправлю.

### 3.5. Cover image

Обложки нет. Варианты: (а) скриншот дашборда Job Search Hub (он уже есть в вашем портфолио),
(б) нейтральная обложка с названием/стеком. Требует вашего выбора — картинку сгенерировать могу,
но публиковать не буду без подтверждения.

---

## 4. Что я НЕ делал и не буду делать

| Запрещено задачей | Статус |
| :--- | :--- |
| Публиковать / менять что-либо без подтверждения | ✅ ничего не отправлено: страница только читалась |
| Identity verification | ⛔ **не автоматизирую** — кнопка `Verify Identity` остаётся вам |
| Wallet / payout setup | ⛔ **не автоматизирую** — `/independent/wallet?addPayoutMethod=true` остаётся вам |
| Отправка заявок (Apply) | ✅ ни разу не нажата |
| Обход CAPTCHA / anti-bot / логина | ✅ не применялось |

Проверка P2.1 показала: `Apply` нигде не нажимался, в аккаунте ничего не изменено.

---

## 5. План: что предлагаю сделать (ждёт подтверждения)

**Шаги, которые могу выполнить я (после вашего «да»):**
1. Внести **4 work items** (§3.4) через тот же браузер — это форма, но я заполню её **только по вашему явному согласию** и покажу скриншот результата.
2. Добавить **social links** (§3.1) — предложенный набор.
3. Указать **rate** (§3.3) — по выбранному вами варианту.
4. Добавить **one-liner** (§3.2) — по выбранному варианту.

**Шаги, которые делаете только вы вручную:**
5. **Verify identity** (загрузка документа, возможно с проверкой лица).
6. **Payout account** в wallet.
7. **Обложка** — загрузка выбранного изображения (если решите сделать).

**После ваших шагов я повторю проверку (шаг 6 задачи):**
- `/jobs` — доступность и редирект;
- наличие `OPEN` заказов (было 3 из 233);
- доступность detail заказа (сейчас `/jobs/<slug>` → 404);
- стабильность URL / `source_id` (сейчас `source_id` = `job_<n>` — стабилен).

---

## 6. Итог аудита (кратко)

| Вопрос | Ответ |
| :--- | :--- |
| Профиль доступен read-only | ✅ да, сессия живёт, CDP 9113 |
| Заполнение | **22 %**, discoverability 24 |
| Заполнено | имя, headline, локация, доступность, email, «Get In Touch» |
| Не хватает | **4 работы**, rate, social link, one-liner, обложка, + verify identity/payout |
| Подготовлено мной | 4 work items, 3 social-ссылки, 3 one-liner, 3 варианта ставки + проектные вилки |
| Отправлено в Contra | **ничего** — ждём вашего подтверждения |
| Identity/wallet | не автоматизировались |
