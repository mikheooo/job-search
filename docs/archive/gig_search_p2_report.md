# GIG-SEARCH-P2 — Contra Live Source

Первый **реальный источник** подключён к существующему контуру Gig Search. Fixture P1
сохранён и продолжает работать. Новый pipeline не создавался: используется тот же
`Gig → normalize → dedup → gates → assessment → scoring → store → UI/API/CLI`.

**Главный вывод, который меняет исходную постановку:** публичный Contra **не публикует
открытые заказы**. Его лента `/jobs` находится за логином, а единственная публичная
поверхность с записями о заказах — курируемые маркетинговые страницы `/hire/<role>`, где
все записи уже `EXPIRED`/`CLOSED`. Реальный адаптер построен, работает и отдаёт **53
уникальных реальных заказа Contra**, но **ни один из них нельзя взять в работу** — это
архив. Подробности и доказательства в §3 и §10.

---

## 1. Implementation — что добавлено

| Компонент | Файл | Назначение |
| :--- | :--- | :--- |
| **Contra source adapter** | `ai_assistant/gig/sources/contra.py` (новый, 430 строк) | Читает публичные `/hire/<role>` страницы Contra, разбирает встроенный relay-кэш, отдаёт сырые записи в существующий pipeline. Никакого логина, обхода защит, чужих cookies, отправки заявок. |
| **Provenance-поля** | `ai_assistant/gig/models.py` (аддитивно) | `source_mode` (`live`/`fixture`), `source_scope`, `source_url`, `fetched_at`, `source_status`, `first_seen_at`, `last_seen_at`, `platform_payment_options`, `source_facts`, `derived_fields`. |
| **Новые типы бюджетов** | `models.BudgetType` | `monthly`, `weekly` — реальная выдача Contra (`MonthlyRateJobBudget`/`WeeklyRateJobBudget`); раньше склеивались с `fixed`, что искажало ставку. |
| **Гейт закрытых заказов** | `hard_gates.py`, `models.RejectionReason.SOURCE_CLOSED` | Запись, которую источник сам помечает `EXPIRED`/`CLOSED`, отклоняется с machine-readable причиной. Это защита от того, чтобы архив выглядел в рабочем списке как доступный заказ. |
| **Payment readiness** | `assessment.py` (правило P2 §5) | `platform_payment_options` — это capability площадки, а не готовность нашего аккаунта. Само по себе наличие способов выплат **никогда** не даёт `ready`; персональная верификация не выполнена и заказ из-за неё не отклоняется. |
| **Аддитивная миграция** | `db_schema.py` | 10 новых колонок в `gigs` + индекс `idx_gigs_source_mode` (ALTER TABLE с `IF NOT EXISTS`-семантикой, как для `application_queue`). Существующие колонки не тронуты. |
| **Refresh-семантика** | `store.py` | `first_seen_at` пишется один раз; `last_seen_at` только движется вперёд и **не** обновляется от повторного ingestion; провенанс освежается без изменения контента и без понижения статуса. |
| **CLI** | `ai_assistant/cli.py`, `gig/cli.py` | `gig fetch contra` (или `fixture`) с `--limit/--pages/--delay/--max-pages/--snapshot/--include-closed/--json`; `gig list --source-mode`. |
| **API / UI** | `gig/api.py`, `gig/static/gigs.html` | Фильтр `source_mode` в `/api/gigs`, раздельная статистика `by_source_mode`, бейдж **LIVE**/**FIXTURE** и статус источника в таблице и карточке. |
| **Test fixtures** | `artifacts/contra_hire_page_snapshot.html`, `..._web_developers_snapshot.html` | Сохранённые публичные страницы Contra — реальные данные, на которых парсер тестируется детерминированно и офлайн. |
| **Тесты** | `tests/gig/test_contra_source.py` (43), `tests/gig/test_contra_refresh.py` (12) | Парсер, отсутствующие поля, устойчивость к изменению входа, стабильность id, сетевые сбои, дедуп, повторный fetch, сохранение статуса, live/fixture. |

**Переиспользовано из P1 без изменений:** `Gig`, `GigSource`, `normalize_gig`, `dedup`,
`hard_gates`, `scoring`, `pipeline`, `store`, `api`, `cli`, UI. Новых pipeline-ов не появилось.

---

## 2. Live source — какие URL и scope используются

```
source       = contra
source_mode  = live
source_scope = public_hire_pages
source_url   = https://contra.com/hire/<role>     # страница, где запись реально наблюдалась
```

Читается только публичная часть:

| Поверхность | Статус | Что делает адаптер |
| :--- | :--- | :--- |
| `/hire/<role>` (курируемые страницы, 34 214 URL в sitemap) | **публично доступно** | **читает** — здесь лежат реальные `JobPosting` записи |
| `/jobs` (лента заказов) | за логином | **не читает** — редирект на `/log-in?redirectTo=/jobs` |
| `/contra-labs-network/jobs`, `/contra-labs-network/tasks` | за логином | **не читает** |
| личный кабинет, отклики, Pro-функции | аккаунт | **не трогает** |

Соблюдено из ТЗ §2: нет автоматического логина, нет обхода CAPTCHA/anti-bot, нет чужих
cookies, нет эмуляции отклика, нет подачи заявок. `robots.txt` Contra проверен и разрешает
эти пути (`Disallow` только для `/@internal` и redirect-параметров); адаптер всё равно
проверяет его перед первым запросом и уважает запрет. Запросы вежливые: один HTTP-запрос
на страницу, пауза между страницами (по умолчанию 1 с), фиксированный User-Agent, жёсткий
лимит страниц за прогон.

**Почему не сессия пользователя.** Вы предложили использовать вход в браузере. Я проверил:
in-app браузер стартует с чистым профилем (вход не переживает переоткрытие), а Chrome с
постоянным профилем за логин-стеной тоже не имеет сессии Contra — cookie для `contra.com`
нет ни в одном профиле на машине. Дополнительно сам ТЗ P2 §2 предписывает собирать только
публичную часть, поэтому адаптер построен на публичных данных — это и есть требуемое
поведение, а не компромисс.

---

## 3. Fields — что Contra реально даёт, а что нет

**Даёт публично** (реальные значения из собранных записей):

| Источник (Contra) | Поле у нас | Пример |
| :--- | :--- | :--- |
| `JobPosting.title` | `title` | `Founding Developer (Automations, AI, etc)` |
| `JobPosting.id` (base64 relay-id) | `source_id` | `WyJKb2JQb3N0aW5nIiw0NDQwOTQ0XQ==` |
| `JobPosting.approvedAt` | `published_at` | `2025-02-11T14:23:26.019Z` |
| `Job.status` | `source_status` | `EXPIRED`, `CLOSED` |
| `budget.feeMin` / `feeMax` (`"USD:50.00000000"`) | `budget_min` / `budget_max` / `currency` | `50.0 – 55.0`, `USD` |
| `budget.__typename` | `budget_type` | `HourlyRateJobBudget` → `hourly` |
| `budget.estimatedHours` | `extra.contra_estimated_hours` | `5` (часто `0` = «не применимо») |
| `JobPosting.slug`, `analyticsId`, `hiringAsIndividual`, `timezonePrimaryUtcOffset` | `source_facts` | сохраняются дословно |
| `ProductizedService`-записи (услуги, не заказы) | — | **не** берутся: это не заказы |

**Не даёт публично — остаётся `null`/`unknown`, ничего не выдумано** (ТЗ §3):

| Поле | Почему нет |
| :--- | :--- |
| `description` | Contra не публикует тело заказа анонимно → пустая строка, не сгенерированный текст |
| `client_rating` | нет объекта рейтинга в публичной записи |
| `client_total_spent`, `client_hires`, `client_jobs_posted` | не раскрываются анонимно |
| `competition` (число откликов) | не раскрывается анонимно |
| `proposal_cost` | не раскрывается анонимно |
| `client_name`, `client_country` | `organization` в публичных записях `null` |
| `skills`, `category` | не раскрываются анонимно |
| URL самого заказа | публичной страницы заказа нет: `/jobs/<slug>` отдаёт 404 без сессии → `url` остаётся пустым, а не подменяется страницей листинга |

**Разделение фактов и расчётов** (ТЗ §4): `source_facts` содержит только значения от Contra;
`derived_fields` перечисляет то, что посчитали мы (`budget_min`, `budget_max`, `budget_type`,
`published_at`, `source_mode`, `source_scope`, `fetched_at`). На вопрос «это Contra сказала
или наша система посчитала?» всегда есть однозначный ответ.

---

## 4. Fetch result

Прогон от 29.09.2026 по 15 курируемым публичным страницам Contra:

```text
fetched:     60
new:         53     # уникальных записей после дедупа
updated:     0      # новых версий существующих записей не было
duplicates:   7     # те же заказы, встречающиеся на нескольких /hire страницах
rejected:     0
accepted:    53
errors:       0
```

С дефолтным гейтом (закрытые записи отклоняются) тот же прогон даёт:

```text
fetched: 60   accepted: 0   rejected: 60   duplicates: 0   errors: 0
```

то есть **все** публично доступные записи Contra помечены источником как неактуальные.
Это результат самого источника, а не эффект нашей фильтрации: причина отказа —
`SOURCE_CLOSED`, machine-readable и видимая в `gig_events`.

Хранилище после прогона:

```text
total:      82
by source:  contra=53, fixture=29
by mode:    live=53, fixture=29
```

---

## 5. Top gigs — 10 лучших live-результатов

| # | Title | Budget | Score | can_deliver | AI pot. | Effort | Rate/h | Risk | Payment | Source status |
| -: | :--- | :--- | ---: | :--- | ---: | ---: | ---: | :--- | :--- | :--- |
| 1 | Front-End Software Engineer (UI/UX) for Web App Dev | USD 5000-10000 | 55 | low | 50 | 115.6h | 64.88 | medium | possible | EXPIRED |
| 2 | Motion Graphic Designer for Medical Education Project | USD 5000-10000 | 55 | low | 50 | 115.6h | 64.88 | medium | possible | CLOSED |
| 3 | Design Course Creator | USD 4000-8000 | 55 | low | 50 | 115.6h | 51.90 | medium | possible | EXPIRED |
| 4 | Freelancer Needed for Brand Kit and Identity Development | USD 4500-6500 | 55 | low | 50 | 115.6h | 47.58 | medium | possible | EXPIRED |
| 5 | Brand Designer | USD 75-150/h | 55 | low | 50 | 28.9h | 112.50 | medium | possible | EXPIRED |
| 6 | Product Design - SaaS Dashboard | USD 70-140/h | 55 | low | 50 | 28.9h | 105.00 | medium | possible | EXPIRED |
| 7 | UI/UX Product Designer | USD 40-80/h | 55 | low | 50 | 28.9h | 60.00 | medium | possible | EXPIRED |
| 8 | Jitter Expert for Healthcare SaaS Website Redesign | USD 75-120/h | 55 | low | 50 | 28.9h | 97.50 | medium | possible | EXPIRED |
| 9 | Salesforce UX Designer for Client Redesign Project | USD 40-60/h | 55 | low | 50 | 28.9h | 50.00 | medium | possible | EXPIRED |
| 10 | Founding Developer (Automations, AI, etc) | USD 50-55/h | 55 | low | 50 | 28.9h | 52.50 | medium | possible | EXPIRED |

**Честное чтение этих чисел.** Все десять — дизайнерские/контентные заказы с
`skill_fit = 35` и `can_deliver = low`: это ровно тот результат, который обязан быть, потому
что **публичный набор Contra состоит почти целиком из профилей дизайнеров** (`/hire/ai-developers`,
`/hire/web-developers` и т.п. — маркетинговые страницы под услуги дизайна). Заказы, релевантные
моему стеку, в публичном наборе единичны: из 53 записей под автоматизацию/Python/AI/API
подходят 14 по заголовку, и среди них нет ни одной открытой:

| Title | Budget | Score | source_status |
| :--- | :--- | ---: | :--- |
| Founding Developer (Automations, AI, etc) | USD 50-55/h | 55 | EXPIRED |
| Senior AI Developer | USD 4000-5000 | 55 | EXPIRED |
| AI Full-Stack Engineer/Product Manager | USD 1500-3000 | 45 | EXPIRED |
| Website for construction AI app | USD 2500-5000 | 52 | EXPIRED |
| Content Marketer for AI platform that serves DTC | USD 50-70/h | 55 | EXPIRED |

**why** (для топ-1): совпадений по навыкам профиля нет (дизайн вне стека) → `skill_fit=35`;
бюджет отличный (`budget_quality=100`), эффективная ставка высокая (`effective_rate=100`),
но `deliverability=25` (низкая выполнимость) и нет истории клиента (`client_quality=35` —
данных просто нет, не выдуманы); источник сообщает `EXPIRED` → риск повышен до `medium`.

Короткий `why` по остальным: тот же профиль причин — крупный бюджет и высокая ставка
уравновешены низкой выполнимостью, отсутствием данных о клиенте и закрытым статусом источника.

Заявки не подавались, выбор за пользователя не делался (ТЗ §11).

---

## 6. Persistence — результат повторного fetch

Проверено тестами и прогоном:

| Проверка | Результат |
| :--- | :--- |
| Повторный `gig fetch contra` | `new: 0`, `duplicates: 60`, `errors: 0` — **дубли не создаются** |
| `opportunity_score` после повторного fetch | не меняется |
| Статус `SHORTLISTED`/`SKIPPED`, выставленный человеком | **сохраняется** (тесты: `test_shortlisted_status_survives_refetch`, `test_skipped_status_survives_refetch`) |
| Запись исчезла из текущей выдачи | **не удаляется**; `last_seen_at` просто перестаёт двигаться (`test_gig_missing_from_current_page_is_not_deleted`) |
| `first_seen_at` | пишется один раз и не меняется |
| `last_seen_at` | только движется вперёд, не откатывается на более старом наблюдении |
| Провенанс при повторном fetch | освежается, контент и статус не затронуты |

---

## 7. Failure test — что происходит при недоступности Contra

```
gig fetch contra   (сеть недоступна)
→ [gig] contra unavailable: contra: no gigs fetched (...)
  stored gigs preserved: total=82 accepted=72 rejected=9
→ exit code 1
```

Проверено тестами:
- `test_network_failure_raises_unavailable` — сетевой сбой даёт `ContraUnavailable`;
- `test_http_404_is_recorded_not_fatal_for_partial_runs` — один битый slug не срывает прогон по остальным страницам (ошибка логируется, здоровые страницы обрабатываются);
- БД **не очищается**, ранее сохранённые гиги остаются доступны через UI/API/CLI (ТЗ §9).

Ограничение запросов (ТЗ §9): fetch полностью отделён от отображения — UI/API читают только
БД и никогда не ходят в Contra. Вежливость: пауза между страницами, лимит страниц, один
запрос на страницу, проверка `robots.txt`.

---

## 8. Tests — команды и результаты

```
.\.venv\Scripts\python.exe -m pytest tests/gig -q
→ 167 passed
```

Из них P2:

```
.\.venv\Scripts\python.exe -m pytest tests/gig/test_contra_source.py -q
→ 43 passed

.\.venv\Scripts\python.exe -m pytest tests/gig/test_contra_refresh.py -q
→ 12 passed

.\.venv\Scripts\python.exe -m pytest tests/test_db_schema_transaction.py tests/gig -q
→ 181 passed
```

Покрытие по ТЗ §12:

| Требование | Тест |
| :--- | :--- |
| Contra parser | `TestParser` (7 тестов) |
| отсутствующие поля | `TestNothingIsInvented` (7, параметризовано) |
| изменение HTML/input | `TestRobustnessToChangedInput` (4: нет контекста, битый JSON, пустой title, незнакомый тип бюджета) |
| normalization | `TestNormalization` |
| stable source ID | `test_source_id_is_stable_across_parses` |
| повторный fetch | `TestRepeatFetch` (3) |
| dedup | `test_second_fetch_creates_no_duplicates`, `test_gig_missing_from_current_page_is_not_deleted` |
| update existing gig | `test_repeat_fetch_does_not_change_opportunity_score`, `test_first_seen_is_immutable_and_last_seen_advances` |
| сохранение пользовательского status | `test_shortlisted_status_survives_refetch`, `test_skipped_status_survives_refetch` |
| network failure | `TestFetchFailure` (3) |
| live/fixture separation | `test_live_and_fixture_are_stored_apart`, `test_fixture_mode_is_distinct`, `test_stats_report_by_source_mode` |
| payment readiness ≠ ready | `TestPaymentReadinessIsNeverReadyFromCapability` (4) |
| закрытые источником заказы | `TestClosedSourceGate` (2) |

**Parser-тесты не делают ни одного сетевого запроса** — работают на сохранённых снимках
(`artifacts/contra_*_snapshot.html`), что требуется ТЗ §12. Отдельный тест
(`test_snapshot_mode_never_hits_the_network`) закрепляет это правило.

---

## 9. Regression

```
.\.venv\Scripts\python.exe -m pytest tests -q
→ 2551 passed, 5 failed   (прогон без известных нестабильных browser-файлов)
```

Все 5 отказов разобраны:

| Тест | Вердикт |
| :--- | :--- |
| `test_db_schema_transaction.py::test_fingerprint_pin` | **Ожидаемо, обновлено**: пин отпечатка DDL отражает правки схемы — я добавил 10 колонок в `gigs` и индекс. Пин обновлён `40770fb60c84e711 → c7bed813533fba15`. Тест снова зелёный. |
| `test_db_schema_transaction.py::test_schema_dump_matches_stored_reference` | **Ожидаемо, обновлено**: слепок схемы 92 → 93 объекта (`gigs` пересобран + `idx_gigs_source_mode`). Тест снова зелёный. |
| `test_t78_search_only.py::*` (3 теста) | **Не связаны с P2.** Тест отсутствует в HEAD (untracked, чужая незакоммиченная работа). Падает из-за guard'а conftest, который блокирует обращение к **порту 9111** — это CDP-порт HH-headless проекта, к которому t78 и обращается. Измерено: с моей и с HEAD-версией `db_schema.py` результат идентичен (3 failed, 8 passed). |

Отдельно: браузер, который я открывал для проверки сессии, работал на порту **9112** и
закрыт; он не мог повлиять на 9111.

**Gig P1 не сломан:** `tests/gig` — 167 passed (112 исходных P1 + 55 новых). Существующие
29 fixture-гигов сохранили свои данные; после перепрогона им проставлен `source_mode=fixture`.

---

## 10. Limitations — ограничения публичного источника Contra

1. **Открытых заказов в публичном доступе нет.** Это главное ограничение и результат
   разведки, а не дефект реализации. Все 53 публично доступные записи помечены источником
   как `EXPIRED` (43) или `CLOSED` (10). Дата публикации: февраль 2025 (6), март 2025 (46),
   октябрь 2025 (1).
2. **Лента `/jobs` за логином.** Анонимный запрос получает пустую SPA-оболочку, браузер
   редиректится на `/log-in?redirectTo=/jobs`. Обходить это ТЗ §2 запрещает, и я не обходил.
3. **21 заказ — недостижимо в текущей постановке.** ТЗ §10 просит минимум 20 уникальных
   реальных заказов. Публично доступно 53 уникальных записи — но все архивные. Дефицита
   данных нет; дефицит **актуальных** данных принципиален.
4. **Данные о клиенте и конкуренции отсутствуют** в публичных записях: нет рейтинга,
   расходов, найма, числа откликов, стоимости отклика. Поэтому `client_quality` и
   `competition` в breakdown — нейтральные заглушки (35 и 50), а не измерение.
5. **Нет описания заказа.** Contra не публикует тело заказа анонимно. Это заметно влияет
   на `can_deliver` и `ai_automation_potential`: по заголовку судить о способе доставки
   результата трудно, оценки консервативны. LLM-достраивание описания сознательно не
   применялось (ТЗ §3 запрещает выдуманные факты).
6. **Нет URL конкретного заказа** — публичной страницы заказа не существует (`/jobs/<slug>`
   = 404 без сессии). В `url` пусто; в `source_url` — страница, где запись наблюдалась.
   Это же выявило дефект: уникальный индекс по `url` конфликтовал на пустых строках
   (исправлено — пустой url хранится как `NULL`).
7. **Нестабильная разметка.** Парсер привязан к `<script id="vike_pageContext" type="application/json">`
   и к именам `JobPosting`/`Job`/`*JobBudget`. Contra может это изменить; тесты
   `TestRobustnessToChangedInput` фиксируют, что при изменении парсер деградирует мягко
   (пустой результат), а не падает.
8. **Состав публичного набора перекошен.** Курируемые страницы — под дизайн/контент; поэтому
   топ скоринга занят дизайном. Это свойство источника, а не фильтра: адаптер берёт всё
   доступное, релевантность определяет scoring (ТЗ §6).
9. **Capability ≠ готовность оплаты.** `platform_payment_options` (escrow, bank transfer,
   paypal, wise) — это возможности площадки. Персональная верификация выплат не выполнялась,
   поэтому `payment_readiness` = `possible`/`unknown` и **никогда** `ready` (ТЗ §5).
10. **Веса scoring по-прежнему не откалиброваны** (наследие P1). Видно на живых данных: все
    топ-записи получают одинаковые 55 из-за одинакового профиля причин; `effective_rate`
    и `budget_quality` упираются в потолок, тогда как `client_quality`/`competition` — заглушки.
    Это ровно тот сигнал, который должен использоваться для калибровки.

---

## Проверка через UI/API/CLI

```powershell
# Реальный fetch (вежливый, только публичные страницы)
.\.venv\Scripts\python.exe -m ai_assistant.cli gig fetch contra

# Офлайн-прогон на сохранённом снимке (детерминированно, без сети)
.\.venv\Scripts\python.exe -m ai_assistant.cli gig fetch contra --snapshot artifacts/contra_hire_page_snapshot.html

# Только реальные заказы, отдельно от фикстуры
.\.venv\Scripts\python.exe -m ai_assistant.cli gig list --source-mode live

# UI/API
.\.venv\Scripts\python.exe -m uvicorn ai_assistant.gig.api:app --port 8001
#   → http://127.0.0.1:8001/                (бейдж LIVE/FIXTURE, фильтр источника)
#   → http://127.0.0.1:8001/api/gigs?source_mode=live
```

Живой smoke-тест подтверждён:

```
LIVE     -> rows=5, is_live=True, mode=live, scope=public_hire_pages
            platform_options=platform_escrow,bank_transfer,paypal,wise
            first_seen=2026-09-29T09:33:29Z
FIXTURE  -> rows=3, mode=fixture
STATS    -> {"fixture":29,"live":53}
UI page  -> 200, LIVE badge = True, source-mode filter = True
```

---

## Acceptance — сверка

| # | Критерий | Статус |
| -: | :--- | :--- |
| 1 | Реальный `Contra` source adapter | ✅ `ai_assistant/gig/sources/contra.py` |
| 2 | Использует публичные live данные | ✅ `/hire/<role>`, без логина и обходов |
| 3 | Fixture P1 продолжает работать | ✅ 29 fixture-гигов, 112 P1-тестов зелёные |
| 4 | Реальные и fixture различимы | ✅ `source_mode` + бейдж LIVE/FIXTURE + фильтры API/CLI/UI |
| 5 | Live данные проходят существующий pipeline | ✅ 60 записей → 53 уникальных, 0 ошибок |
| 6 | Повторный fetch без дублей | ✅ `new: 0`, `duplicates: 60` |
| 7 | Исчезновение из выдачи не удаляет историю | ✅ тест + сохранённые `first/last_seen_at` |
| 8 | Отсутствующие факты не выдумываются | ✅ 7 параметризованных тестов; `description`, рейтинг, spend, hires, competition, proposal_cost = `None` |
| 9 | `payment_readiness` не `ready` без проверки | ✅ 4 теста; в БД 0 записей со `ready` |
| 10 | Доступ через UI/API/CLI | ✅ `/api/gigs?source_mode=`, `gig list/fetch`, страница Gigs |
| 11 | Parser покрыт deterministic tests | ✅ 43 теста офлайн на снимках |
| 12 | Job Search regression на прежнем уровне | ✅ 2551 passed; 5 отличий объяснены поимённо (§9) |

**Не делалось** (ТЗ «Не делать в P2»): автоматические proposals и отклики, login automation,
автоматизация аккаунта Contra, Upwork/Freelancer/Fiverr, агрегатор источников, редизайн
дашборда, автономная доставка.

---

## P3 — предложение следующего минимального этапа

Ничего из этого **не начато**.

**P3 (минимальный): перейти к источнику с действительно публичной лентой открытых заказов.**

Смысл: P2 доказал, что архитектура готова принимать реальные источники (адаптер, провенанс,
refresh-семантика, live/fixture, payment-дисциплина — всё работает и покрыто тестами). Но
Contra не даёт актуальных данных публично, поэтому **ценность P3 не в коде, а в выборе
источника**. Шаги:

1. **Выбрать источник с публичной лентой.** Критерии: открытые заказы видны анонимно;
   бюджет/описание в наличии; доступ без логина и без обхода защит; правила сайта разрешают
   чтение. Кандидатов нужно оценить отдельно (не Upwork/Freelancer/Fiverr — они исключены ТЗ).
2. **Второй адаптер через тот же протокол** `GigSource.fetch()` — правки core не нужны,
   провенанс и refresh уже есть.
3. **Калибровка scoring на реальных данных.** Теперь это можно сделать предметно: собрать
   10–15 фактически выполненных заказов, пересчитать `estimated_effort_hours`, веса и порог
   `effective_rate`; сейчас `client_quality`/`competition` — заглушки, а ставка упирается в потолок.
4. **FX-нормировка ставок** (наследие P1) — сейчас сравнение идёт в валюте заказа.
5. **Оценить Contra Labs как отдельный канал.** У Contra есть программа `contra-labs-network`
   с задачами (`/contra-labs-network/tasks`) — публично она за логином, но это единственный
   видимый путь к реальным задачам; решение о доступе за вами.
6. **Уборка по P2:** решить судьбу архива 53 записей Contra (оставить как референс набора
   или удалить); закрыть три отказа `test_t78_search_only.py`, которые унаследованы от чужой
   незакоммиченной работы (порт 9111).

P3 намеренно **не** включает отклики, proposals, CRM и агрегацию всех площадок.
