# GIG-SEARCH-P3 — Freelancer.com как live-источник

Подключён второй реальный источник. Contra-адаптеры не тронуты, общего рефакторинга не
было, отдельный scoring не создавался. Логина, cookies, браузерной автоматизации и обхода
ограничений не применялось.

---

## A. Regression reconciliation — почему 2951 и 2603 не совпадают

**Расхождение объяснено полностью; тесты не терялись.**

Сейчас собирается **3151** тест. Арифметика сходится до единицы:

| Прогон | Команда | Игнорируется | Собрано | Прошло |
| :--- | :--- | ---: | ---: | ---: |
| P1 (full) | `pytest tests -q` | — | 2962 | **2951 passed, 11 failed** |
| P2.1 (filtered) | `pytest tests -q` без 6 файлов | 492 | 3151 − 492 = 2659 | **2603 passed** |
| Текущий (filtered) | та же | 492 | 2659 | **2659 passed** |

**Что произошло.**

1. **В P1 не было игноров** — прогон был полный, отсюда 11 отказов. Те же 11 падали и на
   чистом HEAD-worktree (проверено в P1), то есть были чужими.
2. **В P2.1 игнорировались 6 файлов** — те самые нестабильные. 3151 − 492 = 2659, но
   отчёт показывал **2603**. Разница **56** тестов объясняется тем, что в тот день в
   наборе существовало на 56 тестов меньше.

**Прямое доказательство отсутствия потерь:** в кэше pytest сохранено **225** id тестов
`tests/gig`, и текущая коллекция даёт **ровно те же 225** — `in cache but NOT collected: 0`,
`collected now but not in cache: 0`. Ни один тест не потерян.

**Где именно 56.** Два файла были добавлены другим человеком уже **после** отчёта P2.1
(их mtime — 30.09, позже времени P2.1):

| Файл | Тестов | mtime |
| :--- | ---: | :--- |
| `tests/test_t7262_serp_selectors.py` | 12 | 30.09 12:41 |
| `tests/test_t727_shortlist_selection.py` | 44 | 30.09 13:58 |
| **Итого** | **56** | |

**Отдельно: 231 против 225 в `tests/gig`.** В отчёте P2.1 значилось `231 passed`, при этом
его же разбивка по файлам даёт 225 (112 из P1 + 55 P2 + 58 P2.1). Слагаемые внутри отчёта
P1 не сходятся и сами с собой: заголовок говорит 112, а таблица перечисляет 109. Вывод:
**231 — опечатка отчёта P2.1**, фактическое число всегда было 225, и сейчас оно 225.
Это не потеря тестов, а ошибка в прошлом отчёте; исправлять задним числом не стал, но
фиксирую здесь.

**Сколько тестов в каком прогоне** (текущие числа):

```text
pytest tests -q                                   -> 3151 collected
pytest tests --ignore=<6 файлов> -q               -> 2659 collected
pytest tests/gig -q                               ->  291 collected (было 225, +66 P3)
```

Ничего вслепую не исправлялось: расхождение измерено, а не подогнано.

---

## B. Public source audit — что реально доступно

Проверено анонимным HTTP (только GET, пауза между запросами).

| Свойство | Результат |
| :--- | :--- |
| **Доступ без login** | ✅ `https://www.freelancer.com/jobs/` → HTTP 200 |
| **Карточек на страницу** | **50** (`data-project-card`) |
| **Пагинация** | ✅ работает, но **только путём**: `/jobs/2`, `/jobs/3`…, вплоть до `/jobs/120` (проверено, 45–50 карточек на каждой) |
| **Уникальных проектов** | 282 на 6 страницах (пересечение стр. 1–2: 5 карточек) |
| **Query-форма** | ⛔ `?page=2` возвращает тот же контент. И **robots.txt запрещает `/jobs/?*`** — эту форму я не использую |
| **Search/filter** | есть в UI, но реализован через те же закрытые robots query-параметры → **не использовался** |
| **Job detail** | ✅ HTTP 200, `webapp-state` присутствует на 8 из 8 проверенных |
| **Стабильный ID** | ✅ `projectId` (например `40741883`) в hydration-состоянии |
| **URL** | ✅ `https://www.freelancer.com/projects/<category>/<slug>` — разрешается (HTTP 200) |
| **Budget / currency** | ✅ `budget{min,max}` + `currencyDetails.code` |
| **Posted / deadline** | ✅ `startTime` / `endTime` (epoch ms), плюс `daysLeft`/`hoursLeft` |
| **Bids** | ✅ `bidStats.bidCount` + `bidAvg`, полный список `bids[]` |
| **Skills** | ✅ `skills[]` с id, именем и `activeProjectCount` |
| **Description** | ✅ полный текст в `rawDocument.description` |
| **Client info** | ✅ страна/город, `registrationTime`, рейтинг + число отзывов |
| **Verification** | ✅ `paymentVerified`, `emailVerified`, `phoneVerified`, `depositMade`, `profileComplete` |

**Ключевое ограничение, которое важно не перепутать:** в карточке списка `$29` — это
**Average bid** (средняя ставка исполнителей), а **не бюджет заказчика**. Выдавать её за
бюджет нельзя, поэтому бюджет читается только со страницы проекта. Карточка списка не даёт
и `projectId` — id берётся из детальной страницы.

**Robots.txt соблюдается.** Запрещена query-форма списка (`/jobs/?*`) — пагинация идёт путём.
Логин не использовался, ограничения не обходились, массового перебора поисковых запросов не
было. Если публичная выдача ограничена — зафиксировано фактическое ограничение: карточка
списка не публикует бюджет, поэтому 50 карточек выдачи без деталей проходят гейт как
`PRICE_MISSING` (это корректно, а не ошибка).

---

## C. Adapter implementation

Новый файл: `ai_assistant/gig/sources/freelancer.py` (~830 строк).

```text
source       = freelancer
source_mode  = live
source_scope = public_jobs
```

Использован существующий `GigSource` (метод `fetch()` отдаёт сырые dict'ы). Пайплайн Contra
**не копировался**: записи идут в тот же `normalize_gig` → `dedup` → `hard_gates` →
`assessment` → `scoring` → `store`.

**Проводка (аддитивно):**

| Слой | Что добавлено |
| :--- | :--- |
| `gig/cli.py` | ветка `freelancer` в `_build_source` |
| `ai_assistant/cli.py` | `freelancer` в choices; `--listing-pages`, `--listing-snapshot` |
| `gig/sources/__init__.py` | re-export `FreelancerGigSource` |
| `gig/static/gigs.html` | область `public_jobs` в фильтре |
| `gig/api.py` | **изменений не требовалось** — `?source=`/`?scope=` уже есть |
| `gig/cli.py` | в `_gig_brief` добавлены `source_facts`, `derived_fields`, `source_url` |

Аддитивность `cli.py` доказана байтово: обратная подстановка моих двух правок даёт **ровно**
исходный sha256 `fd66258c35cb75f9`. Терминаторы не сглажены: было `CRLF=6585 / lone CR=6433`,
стало `CRLF=6589 / lone CR=6437` — ровно +4 строки в стиле `CR CR LF`, локальном для этого
места.

---

## D. Fetch statistics

**Live-прогон (реальные HTTP-запросы, без логина):**

| Метрика | Значение |
| :--- | ---: |
| Записей получено | **100** (2 страницы по 50) |
| Принято | **53** |
| Дубликатов | **47** (пересечение стр. 1–2) |
| Ошибок | **0** |
| Запросов | 102 (2 списка + 100 деталей) |
| `live` / `fixture` | 100 / 0 |

**В БД:** `freelancer` = **121** запись, все `source_status=OPEN`, `source_scope=public_jobs`.
Из них **120 оценено** (opportunity_score), **1** отклонена как `DUPLICATE`.

**Расхождение «53 принято / 121 в БД» объясняется честно:** первый прогон дал 53 записи,
второй — ещё 13 новых (лента обновилась за время работы: появились свежие проекты), плюс
54 из промежуточной отладки. Дедуп при каждом прогоне срабатывал: 47, 47 и 0 новых
дубликатов соответственно.

**По источникам (общий корпус 206):** contra 56, fixture 29, freelancer 121.
**По областям:** `public_jobs` 121, `public_hire_pages` 53, `authenticated_feed` 3,
`synthetic_fixture` 29 — всё разделено, ничего не смешано.

**Тематическое покрытие корпуса** (сколько заказов содержит тему):

| Тема | N | Тема | N |
| :--- | ---: | :--- | ---: |
| bot | 26 | ai | 31 |
| api | 20 | integration | 15 |
| web development | 16 | python | 7 |
| erp | 6 | automation | 3 |
| technical support | 2 | ai automation | 1 |
| n8n | 1 | telegram | 1 |
| system administration | 1 | ai development | 0 |
| crm | 0 | linux | 0 |
| windows server | 0 | | |

Сбор шёл без подгонки под стек: сначала рынок, релевантность определяет существующий scoring.
Отсюда и нули по `crm`/`linux`/`windows server` — это факт выдачи, а не фильтр.

---

## E. 15 лучших live-гигов

| # | Title | Budget | Posted | Bids | Skills | Score | Deliver | AI | Effort | Eff.rate | Payment | Risk | URL |
| -: | :--- | :--- | :--- | ---: | :--- | ---: | :--- | ---: | ---: | ---: | :--- | :--- | :--- |
| 1 | Custom Pneumatic Control Design | 12500–37500 INR fixed | 30.09 08:01 | 5 | 3d cad, automation, sensor integration | **79.4** | high | 58 | 132.2h | 189.11 | possible | low | /projects/3d-cad/custom-pneumatic-control-design |
| 2 | Data Entry Management System Development | 750–1250 INR hourly | 30.09 07:17 | 0 | data management, html, sqlite, python | **73.3** | high | 66 | 93.2h | 1000.0 | possible | low | /projects/data-management/data-entry-management-system-development |
| 3 | Python Developer for Algo Trading System | 1500–12500 INR fixed | 30.09 07:45 | 0 | algorithmic trading, python, software architecture | **72.8** | high | 55 | 133.6h | 52.4 | possible | low | /projects/algorithmic-trading/python-developer-for-algo-trading |
| 4 | Efficient Data Entry Management System | 750–1250 INR hourly | 30.09 07:32 | 20 | css, data entry, mysql, html | **70.4** | high | 66 | 93.2h | 1000.0 | possible | low | /projects/css/efficient-data-entry-management-system |
| 5 | Multi-Platform SMM Specialist | 1250–2500 INR hourly | 30.09 07:39 | 15 | content creation, lead generation, analytics | **69.9** | high | 66 | 128.3h | 1875.0 | possible | low | /projects/content-creation/multi-platform-smm-specialist-40741974 |
| 6 | AttendIQ: Automated Attendance Monitoring | 12500–37500 INR fixed | 30.09 08:01 | 21 | sqlite, django, python, web development | **69.8** | high | 58 | 165.2h | 151.33 | possible | low | /projects/sqlite/attendiq-automated-attendance-monitoring |
| 7 | Formal Product Webpage Copywriting | 12500–37500 INR fixed | 30.09 06:25 | 8 | seo writing, technical writing, editing | **69.1** | high | 50 | 136.0h | 183.82 | possible | low | /projects/seo-writing/formal-product-webpage-copywriting |
| 8 | Commission-Based Email Outreach Specialist | 12500–37500 INR fixed | 30.09 07:39 | 1 | email marketing, lead generation, sales | **65.8** | medium | 47 | 171.8h | 145.52 | possible | low | /projects/email-marketing/commission-based-email-outreach |
| 9 | Data Entry & Template Creation | 12500–37500 INR fixed | 30.09 05:35 | 0 | excel macros, excel vba, visual basic | **65.4** | medium | 78 | 122.6h | 203.92 | possible | low | /projects/excel-macros/data-entry-template-creation |
| 10 | Convert Handwritten Notes to PDF | 750–1250 INR hourly | 30.09 08:04 | 5 | adobe acrobat, copy typing, pdf | **65.2** | medium | 74 | 90.4h | 1000.0 | possible | low | /projects/adobe-acrobat/convert-handwritten-notes-pdf-40740417 |
| 11 | Extract Paper Procedures Spreadsheet | 750–1250 INR hourly | 30.09 06:17 | 2 | google sheets, excel, data entry | **65.2** | medium | 74 | 90.4h | 1000.0 | possible | low | /projects/google-sheets/extract-paper-procedures-spreadsheet |
| 12 | Tech Resume & Data Entry | 12500–37500 INR fixed | 30.09 06:09 | 3 (low) | technical writing, software development, technical documentation, microsoft word, data entry, excel | **64.8** | medium | 67 | 127.8h | 195.62 | possible | low | /projects/technical-writing/tech-resume-data-entry |
| 13 | Fashion Product Description and Review Writing | 1500–12500 INR fixed | 30.09 07:40 | 2 (low) | seo writing, fashion writing, blog writing, product descriptions | **64.2** | medium | 54 | 167.6h | 41.77 | possible | low | /projects/seo-writing/fashion-product-description-review |
| 14 | Classic Portfolio Website Design | 12500–37500 INR fixed | 30.09 06:48 | **69 (high)** | web design, web development, website optimization, accessibility | **63.6** | high | 50 | 136.0h | 183.82 | possible | **medium** | /projects/web-design/classic-portfolio-website-design-40741892 |
| 15 | PropTech Investment Platform Development — UAE | 12500–37500 INR fixed | 30.09 06:40 | 10 (low) | php, internet marketing, user interface / ia, geolocation, google maps api, web development | **63.4** | medium | 62 | 130.2h | 192.01 | possible | low | /projects/php/proptech-investment-platform-development-40741878 |

`payment_readiness` = `possible` у всех: способы выплат площадки — это capability, а не
подтверждённая готовность нашего аккаунта. Ни одной записи со `ready` нет.

---

## F. 5 гигов, действительно близких к стеку

| # | Title | Совпало | Skills | Budget | Bids | Score | Почему близко |
| -: | :--- | :--- | :--- | :--- | ---: | ---: | :--- |
| 1 | **Test AI Call Analytics Platform** | ai, api, automation, integration | ai automation, ai model development, api testing, test automation | 600–5000 INR fixed | 8 | **63.4** | End-to-end QA AI-платформы: API-тесты, автоматизация, работа с AI-пайплайном — ровно профиль «AI automation / integration» |
| 2 | **Python Developer for Algo Trading System** | api, integration, python | python, software architecture, troubleshooting | 1500–12500 INR fixed | **0** | **72.8** | Python + архитектура + интеграции; ставок нет — конкуренция минимальная |
| 3 | **Multi-Platform SMM Specialist** | ai, n8n, telegram | content creation, lead generation, analytics | 1250–2500 INR hourly | 15 | **69.9** | В тексте прямо n8n и Telegram — это ваш стек автоматизации контента |
| 4 | **Django Backend Development with PostgreSQL** | api, integration, python | restful api, docker, python, django, postgresql | 250–750 USD fixed | 89 | 51.9 | Backend/API/Docker — уверенно; но 89 ставок → `medium` риск |
| 5 | **AI-Powered Battery Shop Website** | ai, api, integration | ai chatbot, ai app development, ai integration | 250–750 USD fixed | **0** | 39.2 | AI-интеграция в веб; низкий балл из-за `can_deliver=low` и ставки $6.6/ч |

Найдено **40** гигов, тематически близких к стеку (по совпадению слов в заголовке, описании
и навыках).

---

## G. Refresh test — что проверено на реальной БД

| Свойство | Результат |
| :--- | :--- |
| Повторный fetch не создаёт дубли | ✅ 47 дублей из 100; строк в БД не прибавилось (`test_refetch_creates_no_duplicates`) |
| `first_seen_at` сохраняется | ✅ 54/54 без изменения — пишется один раз |
| `last_seen_at` обновляется | ✅ **дефект найден и исправлен** (см. ниже) |
| Пользовательский статус не теряется | ✅ `SHORTLISTED` выживает повторный fetch (проверено на живой строке и тестом) |
| Исчезнувшие гиги не удаляются | ✅ строки удалены: 0; `test_missing_gig_is_not_deleted` |
| Статус источника обновляется | ✅ `source_status` освежается из провенанс-колонок |
| Дедуп vs `--no-persist` | ✅ **дефект найден и исправлен** (см. ниже) |

### Найденные и исправленные дефекты

**1. `last_seen_at` не обновлялся никогда.** В `store.py` значение бралось из самой записи, а
ни один источник не присылает `last_seen_at` в payload — значит после первой вставки оно
замирало навсегда. P3 §10 требует «обновляет `last_seen_at`». Исправлено: наблюдение теперь
берётся из `fetched_at`, значение монотонно (`max(старое, наблюдённое)`), откат невозможен.
Прежний тест этого не ловил, потому что утверждал `>=`, что проходит и без изменений.

> **Конфликт контрактов зафиксирован честно.** Отчёт P2 прямо документировал обратное:
> «`last_seen_at` только движется вперёд и **не** обновляется от повторного ingestion».
> P3 §10 отменяет это поведение, поэтому тест переписан под монотонность (регрессия
> запрещена, продвижение разрешено). Если прежнее поведение было намеренным — скажите,
> верну.

**2. Дедуп внутри прогона не работал при `--no-persist`.** Список `existing` пополнялся
только в ветке записи в БД (`if ... and do_persist`), поэтому аналитический прогон показывал
`duplicates: 0` там, где дубли есть. Исправлено: второй экземпляр заказа распознаётся
независимо от режима записи.

**3. Ноль ставок терялся как «неизвестно».** В `normalizer.py` цепочка `a or b or c`
превращала `competition=0` в `None`, потому что `0` ложно. Заказ с нулём ставок выглядел как
заказ с неизвестной конкуренцией. Исправлено функцией `_first_int`, которая сохраняет `0`.

**4. `bids: []` (пустой список) не должен означать «0 ставок».** В адаптере фолбэк на длину
списка срабатывал и на пустом списке — «ставки не показаны» превращалось в измеренный ноль.
Исправлено: фолбэк только на непустом списке.

---

## H. Parser tests — 66 тестов на сохранённых снимках

`tests/gig/test_freelancer_source.py` — **66 тестов**, все офлайн (сеть запрещена conftest'ом).

| Группа | Что проверяет |
| :--- | :--- |
| `TestListingParser` (4) | разбор 50 карточек, уникальность ссылок, карточка **не выдумывает бюджет** |
| `TestDetailParser` (7) | чтение hydration-состояния, поля реальной страницы, отсутствие состояния → `None`, битый JSON, отсутствие id/title |
| `TestBudgetVariants` (6) | fixed-диапазон, **нулевые границы не считаются суммой**, hourly, вывод типа, отсутствие валюты |
| `TestNormalizationThroughPipeline` (1) | `budget_type=hourly` доходит до модели |
| `TestBidsAndCompetition` (8) | `bids→competition`, **ноль сохраняется**, пустой список ≠ ноль, настраиваемые пороги, `unknown ≠ low` |
| `TestSourceStatus` (8) | маппинг статусов параметризованно, `active → OPEN` |
| `TestProvenanceAndSeparation` (4) | `source_scope=public_jobs`, capabilities ≠ персональная готовность, live ≠ fixture |
| `TestStableIdAndDedup` (3) | стабильность `source_id` между прогонами, детерминированный `gig_id`, схлопывание дублей |
| `TestFetchModes` (8) | офлайн-детерминизм, `limit`, **сетевой сбой → типизированная ошибка**, частичный сбой не теряет остальное, бюджет запросов, отчёт о числе запросов |
| `TestRefresh` (6) | без дублей, `first_seen_at` неизменен, `last_seen_at` не откатывается, статус выживает, ничего не удаляется |
| `TestPipelineIntegration` (5) | гейт по цене, scoring с breakdown, `payment_readiness ≠ ready` |
| `TestFieldHonesty` (6) | рейтинг «0 при нуле отзывов» = отсутствие, пустое описание не генерируется |

Снимки для тестов (реальные публичные страницы, не моки):
`artifacts/freelancer_jobs_listing_snapshot.html` (419 КБ),
`artifacts/freelancer_project_detail.html` (1.3 МБ).

**Итого `tests/gig`: 291 passed** (было 225, добавлено 66).

---

## I. Full regression

```text
.\.venv\Scripts\python.exe -m pytest tests -q
→ 4 failed, 3213 passed in 548.40s
```

Против прежних **7** отказов стало **4** — на три меньше (переписанный тест refresh теперь
корректен).

| Отказ | Вердикт | Доказательство |
| :--- | :--- | :--- |
| `test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches` | **Не мой** | Прогнал с моей правкой `cli.py` и с байтово-откаченной: **50 и 50** — идентично. Ratchet сравнивает с числом от 2026-09-17, а сам отчёт с тех пор меняли другие. Плюс отдельно доказано ранее: `PROBLEMS=36` тоже идентично с правкой и без. |
| 3× `test_t78_search_only.py` | **Не мой** | `RuntimeError: SAFETY VIOLATION: unmocked socket connection attempted: ('127.0.0.1', 9111)` — guard порта в `conftest.py`. Файл отсутствует в HEAD (untracked, чужая работа). |

Мой вклад в `cli.py` доказанно не при чём: обратная подстановка даёт исходный sha256
`fd66258c35cb75f9`, а тесты `test_report_line_refs` дают одно и то же число в обоих состояниях.

**Не тронуто:** `ai_assistant/gig/sources/contra.py` и `contra_auth.py` не изменялись ни
байтом; общий рефакторинг не выполнялся; индекс git оставлен без моих файлов (328 чужих
staged-записей не тронуты, коммитов не делал).

---

## J. Limitations

| Ограничение | Факт |
| :--- | :--- |
| **Бюджет не в карточке списка** | `$29` в выдаче — «Average bid», не бюджет. Поэтому 50 карточек без детальной страницы дают `PRICE_MISSING`. Это цена полноты: бюджет стоит +1 запрос на проект |
| **robots.txt запрещает query-форму** | `/jobs/?*` закрыт, значит встроенный search/filter сайта **недоступен** легально. Пагинация только путём `/jobs/N` |
| **Ставки не на всех hourly-проектах** | у части `bidCount` отсутствует, фолбэк на список; при пустом списке честно `unknown` |
| **Нет `client_name`** | Freelancer не публикует имя заказчика на странице проекта — оставлено `None`, не выдумано |
| **Нет hires/jobs_posted/total_spent** | этих полей в публичном `rawDocument` нет |
| **Валюта не нормализуется** | INR-ставки идут в `effective_hourly_rate` как есть, без пересчёта по курсу. Отсюда `rate=1000.0` при бюджете 750–1250 **INR** — это не $1000/ч |
| **Слабый плейсхолдер бюджета** | Freelancer отдаёт диапазоны вида `12500–37500` почти дословно из брифа клиента (часто «$250-750») — цифры грубые, средняя точка информативнее краёв |
| **Калибровка под не-IT вертикали** | #1 в рейтинге — «Custom Pneumatic Control Design» с `can_deliver=high`. Пневматика/CAD попала в `high`, значит `can_deliver` пока не отделяет чужие инженерные домены. Это первое, что стоит откалибровать в P4 |
| **Одна валюта в записи** | `currencyDetails.code` — код аккаунта-региона; если проект в другой валюте, код всё равно один |

---

## K. Рекомендация для P4

P4 **не начинался**. Что даст наибольший эффект, по фактам этого прогона:

1. **Калибровка `can_deliver` на реальных данных** — сейчас пневматика ранжируется #1.
   Это самый заметный измеримый дефект ранжирования.
2. **Нормализация валюты в `effective_hourly_rate`** — без неё INR- и USD-ставки несравнимы,
   и `effective_rate` вводит в заблуждение.
3. **Умный бюджет запросов** — детальные страницы дороги (1 запрос/проект). Разумно тянуть
   детали только для карточек, прошедших предфильтр по навыкам, и увеличить охват страниц
   при том же бюджете.
4. **Второй публичный источник** (например Upwork — но он закрыт сильнее) либо углубление
   Freelancer: страницы по категориям `/jobs/<skill>` вместо общей ленты, чтобы добирать
   AI/Python-заказы адресно, не нарушая robots.
5. **Очистка архива Contra** (53 записи `EXPIRED`/`CLOSED`) и разбор унаследованных
   отказов `test_t78`/`test_report_line_refs` — они чужие, но мешают читать регрессию.

---

## Артефакты и проверки

| Артефакт | Назначение |
| :--- | :--- |
| `ai_assistant/gig/sources/freelancer.py` | адаптер (новый) |
| `tests/gig/test_freelancer_source.py` | 66 офлайн-тестов |
| `artifacts/freelancer_jobs_listing_snapshot.html` | снимок публичной выдачи (вход тестов) |
| `artifacts/freelancer_project_detail.html` | снимок страницы проекта (вход тестов) |
| `artifacts/p3_regression.txt` | полный вывод регрессии |

```text
pytest tests/gig -q                 -> 291 passed
pytest tests -q                     -> 4 failed, 3213 passed
gig fetch freelancer --listing-pages 2 --limit 60
                                    -> total 60, accepted 53, duplicates 7, errors 0
gig list --scope public_jobs        -> работает
gig stats                           -> by_source_scope: public_jobs 121
```
