# GIG-SEARCH-P2.1 — Contra Authenticated Access

**Результат: AUTH_SUCCESS.** Вывод P2 («Contra не подходит, публичная лента закрыта») был
преждевременным — его сняли, как и требовал п. «Важно».

| Ответ на запрос | Значение |
| :--- | :--- |
| **AUTH_SUCCESS / AUTH_FAILED** | **AUTH_SUCCESS** |
| **/jobs accessible** | **yes** — без редиректа на `/log-in`, страница ленты отрендерилась |
| **open gigs found** | **3** доступно бесплатному тарифу; сайт заявляет **233** всего |
| **примеры** | 3 актуальных (см. §5) — именно столько читаемо, а не 10 |
| **изменения адаптера** | новый модуль `contra_auth.py` + маршрут `gig fetch contra-auth` (см. §6) |
| **причина блокировки** | авторизация не блокировала; ограничение — **тариф Pro**, не логин |

---

## 1. Реализация — что добавлено

| Компонент | Файл | Назначение |
| :--- | :--- | :--- |
| **Авторизованный источник** | `ai_assistant/gig/sources/contra_auth.py` (новый, 400 строк) | Читает ленту `/jobs` через **вашу** сессию: вызывает ту же операцию, что и сама страница (`JobFeed_jobFeedPaginationQuery`), и парсит ответ. Только `query`, ни одной `mutation`. |
| **CLI-маршрут** | `ai_assistant/cli.py`, `gig/cli.py` | `gig fetch contra-auth [--preset ALL] [--sort NEWEST]` — источник проходит через тот же pipeline P1. |
| **Разделение поверхностей** | `store.py`, `cli.py`, `api.py`, `gigs.html` | Фильтры `--scope` / `--exclude-scope`, `/api/gigs?scope=`, гистограмма `by_source_scope`, бейджи **LIVE FEED** / **ARCHIVE** / **FIXTURE**. Требование §9 закрыто явно, а не «по умолчанию не видно». |
| **Тесты** | `tests/gig/test_contra_auth_source.py` (50), `tests/gig/test_contra_scope_separation.py` (8) | Офлайн, на сохранённом реальном ответе ленты. |
| **Фикстура** | `artifacts/contra_jobs_feed_response.json` | Реальный ответ авторизованной ленты (только записи заказов, **без токенов сессии**). |
| **Скриншот-доказательство** | `artifacts/gig_p2_1_contra_jobs_authenticated.png` | Авторизованная `/jobs` с тарифной границей, как требует п.7. |

**Что переиспользовано:** `Gig`, `GigSource`, `normalize_gig`, `dedup`, `hard_gates`,
`scoring`, `pipeline`, `store`, `api`, `cli`, UI. Браузерный транспорт — существующий
`make_cdp_evaluate` проекта, а не своя реализация CDP.

---

## 2. Как получена сессия (и что НЕ делалось)

1. Запущен **видимый** Chrome с выделенным постоянным профилем
   `%LOCALAPPDATA%\JobSearchContra\User Data` и CDP-портом 9113.
2. Открыта `https://contra.com/log-in` — **вход выполнили вы вручную**
   (вход через Google OAuth).
3. Сессия проверена по факту: вкладка стала `contra.com/mikhail_bs409nwy/work` («Work by
   Mikhail Kolesnikov») — то есть профиль загрузился авторизованно.

Не делалось (п.3 и ТЗ §2): автоматический логин, обход CAPTCHA, обход anti-bot,
чужие cookies, эмуляция отклика, подача заявок. Первое, что я увидел в браузере, была
открытая вами страница согласия Google OAuth — я **не трогал** форму и не навигировал, пока
вы не закончили.

---

## 3. Проверка `/jobs` после входа (п.4)

**URL**: `https://contra.com/jobs` · **редирект на `/log-in`: отсутствует** · **HTTP: 200**,
страница отрендерилась.

Что видно авторизованно (скриншот `artifacts/gig_p2_1_contra_jobs_authenticated.png`):

```
Get immediate access to all 233 job postings by upgrading to Pro
For you 3    All 233    My jobs          Sort by newest
[LIMITED] Get priority access to all jobs before everyone else.  Upgrade to Pro
Boxem · 2d       Website Designer              $3,000 - $6,000 · One-time · 3 weeks delivery
Nexuscai · 1d    UI/UX Designer for ... App    $2,000 - $3,000 · One-time · 2 weeks delivery
Delve Deep LTD   Kajabi Retreat Landing Page   $600 · One-time · 1 week delivery
```

Ключевое: **заказы актуальные** (`2d`, `1d` назад; `createdAt` 28.09.2026) и в статусе
`OPEN` — в отличие от архива P2, где всё было `EXPIRED`/`CLOSED` (февраль–март 2025).

### Данные API

Страница сама использует persisted Relay-операцию:

```
POST https://contra.com/api/?operationName=JobFeed_jobFeedPaginationQuery
Body: {"doc_id":"bd0d3aa180ccca2b973c48ab245fab2a",
       "operationName":"JobFeed_jobFeedPaginationQuery",
       "variables":{"after":null,"filter":{},"first":150,
                    "preset":"ALL","search":null,"sort":"NEWEST"}}
```

Ответ: `jobFeed.count = 233`, `jobFeed.edges.length = 3`.

---

## 4. Поля list/detail (п.4)

**В ленте доступно** (проверено на живых записях):

| Поле Contra | Наше поле | Пример |
| :--- | :--- | :--- |
| `job.id` (base64 `["Job",98049]`) | `source_id` = `job_98049` | стабильный, читаемый |
| `job.title` | `title` | `Website Designer` |
| `job.slug` | `source_facts.contra_slug` | `LLQcBvbr-website-designer` |
| `job.createdAt` | `published_at` | `2026-09-28T14:32:31.318Z` |
| `job.budget.feeMin/feeMax` (`"USD:3000.00000000"`) | `budget_min` / `budget_max` / `currency` | `3000 – 6000`, `USD` |
| `job.budget.__typename` | `budget_type` | `FixedPriceJobBudget` → `fixed` |
| `job.duration` `{amount, interval}` | `source_facts.contra_duration` | `{2, WEEK}` |
| `job.organization.name` | `client_name` | `Boxem`, `Nexuscai` |
| `job.roles.edges[].name` | `source_facts.contra_roles` | `["UI Designer", "Web Designer"]` |
| `job.tools.edges[].name` | `skills` | `["figma"]` |
| `job.timezonePrimaryUtcOffset` | `source_facts` | `-7` |
| `node.visitorDidApply` / `visitorWasMatched` | `source_facts` | `false` / `false` |

**Не доступно — остаётся `null`/пусто, ничего не выдумано** (п. «не выдумывать»):

| Поле | Почему |
| :--- | :--- |
| `description` | лента не отдаёт тело заказа (в записи нет ключа `description`) |
| **URL самого заказа** | `/jobs/<slug>` даёт **404 даже авторизованно**; проверены также `/job/<slug>`, `/postings/<slug>`, `/jobs/<numeric>` — все 404. Поэтому `url` пустой, а `source_url` = страница ленты |
| `client_rating`, `client_total_spent`, `client_hires` | лента не раскрывает |
| `competition` (число откликов) | лента не раскрывает |
| `proposal_cost` | лента не раскрывает |

**Про статус**: лента возвращает только открытые заказы, поэтому `source_status = "OPEN"`
проставляется явно — не оставляется пустым и не выводится догадкой.

---

## 5. Примеры актуальных gigs

Доступно ровно **3** записи (см. §7), поэтому показаны все три, а не выдуманные «10».

| # | Title | Client | Budget | Score | can_deliver | AI pot. | Effort | Rate/h | Risk | Payment | Статус |
| -: | :--- | :--- | :--- | ---: | :--- | ---: | ---: | ---: | :--- | :--- | :--- |
| 1 | Website Designer | Boxem | USD 3000-6000 | 55.5 | low | 50 | 115.6h | 38.93 | low | possible | **OPEN** |
| 2 | UI/UX Designer for a Retro-Style Personal Growth App (Mobile, Figma) | Nexuscai | USD 2000-3000 | 48.6 | low | 50 | 115.6h | 21.63 | low | possible | **OPEN** |
| 3 | Kajabi Retreat Landing Page Specialist | Delve Deep LTD | USD 600 | 36.9 | low | 50 | 44.8h | 13.39 | low | possible | **OPEN** |

Breakdown (компоненты):

| # | skill_fit | deliver | budget | rate | client | comp | payment | ai | risk |
| -: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 30.0 | 25.0 | 100.0 | 96.9 | 35.0 | 50.0 | 60.0 | 50.0 | 100.0 |
| 2 | 30.0 | 25.0 | 100.0 | 47.5 | 35.0 | 50.0 | 60.0 | 50.0 | 100.0 |
| 3 | 30.0 | 25.0 | 100.0 | 19.7 | 35.0 | 50.0 | 60.0 | 50.0 | 100.0 |

**`why`** (для #1): `skill_fit = 30` — заказ дизайнерский, вне моего стека; бюджет отличный
(`budget_quality = 100`) и эффективная ставка высокая (`effective_rate = 96.9`); но
`deliverability = 25` (низкая выполнимость) и `client_quality = 35` — данных о клиенте
лента не даёт, это нейтральная заглушка, а не измерение; `risk = 100` (рисков не выявлено,
статус `OPEN`). Для #2 и #3 причина та же, разница только в ставке.

Заявки не подавались, кнопка `Apply` ни разу не нажата, выбор за пользователя не делался.

---

## 6. Что нужно было доработать в адаптере P2

P2-адаптер (`contra.py`, архивные `/hire`) **не переделывался** — он продолжает работать.
Добавлен **отдельный** источник, потому что у поверхностей разная природа:

| Аспект | Адаптер P2 (архив) | Адаптер P2.1 (лента) |
| :--- | :--- | :--- |
| Как получает данные | HTTP GET + разбор `vike_pageContext` | GraphQL-запрос через CDP в вашей сессии |
| `source_scope` | `public_hire_pages` | `authenticated_feed` |
| `source_status` | `EXPIRED` / `CLOSED` (гейт `SOURCE_CLOSED`) | `OPEN` (гейт не срабатывает) |
| URL записи | нет (404) | нет (404) — **то же ограничение** |
| Транспорт | `urllib` | существующий `make_cdp_evaluate` |

Изменения, которые потребовались:

1. **Синхронный XHR вместо `fetch`.** Транспорт проекта оценивает выражения с
   `awaitPromise: False`, поэтому `async`-выражение возвращало непрозрачный объект.
   Переписано на синхронный `XMLHttpRequest`, который отдаёт строку сразу.
2. **Стабильный `source_id`.** Contra шлёт base64 Relay-id; добавлен `source_id_of()`,
   который декодирует его в `job_98049`. Без этого дедуп между запусками не сходился бы.
3. **Отдельный `source_scope`** — иначе актуальные заказы слились бы с архивом в одном
   рабочем списке (требование §9).
4. **Явный `source_status = "OPEN"`** — лента отдаёт только открытые, и это записано
   фактом, а не оставлено пустым.
5. **Фильтры `scope` / `exclude_scope`** в store/CLI/API/UI.

Багов, найденных прогоном, — ноль: в отличие от P2, здесь `url` сразу оставлен пустым, поэтому
конфликт уникального индекса по `url` не повторился.

---

## 7. Причина, по которой доступно только 3, а не 20+

Это **не** сбой авторизации и **не** дефект адаптера. Это тарифная граница Contra:

| Проверка | Результат |
| :--- | :--- |
| Сайт заявляет всего открытых заказов | **233** |
| API `jobFeed.count` под `preset=ALL` | **233** (реальное число) |
| API `jobFeed.edges` — сколько отдаёт | **3** |
| Пагинация (`after: <cursor>`) | `count: 233`, `edges: 0` — дальше не отдаёт |
| UI при открытой вкладке «All 233» | рендерится **3** карточки (3 кнопки Apply) |
| Paywall-текст (дословно) | «Get immediate access to all 233 job postings by upgrading to Pro» |
| `preset=FOR_YOU` | тоже 3 |
| `first: 500` вместо `150` | тоже 3 (лимит не про размер страницы) |
| `preset` вне списка / `null` | GraphQL error «An unexpected error occurred.» |

Прочие проверенные варианты: `search=<term>` возвращает `count` (например, `automation` → 8,
`ai` → 60, `support` → 37), но `edges` в них — **0**. То есть фильтр по поиску отдаёт только
счётчик, не записи.

**Что я сознательно не стал делать.** Я не стал систематически перебирать сотни поисковых
терминов, чтобы по кусочкам собрать все 233 записи в обход лимита. Формально это дало бы
«20+ актуальных gigs», но это обход коммерческого ограничения продукта, а не работа с ним —
и это ровно то, что ТЗ запрещает духом (§2: не обходить защиты; здесь защита — тариф).
Объём, который тариф отдаёт честно, — 3 записи, и отчёт называет именно его.

**Следствие для п.8**: «read-only fetch минимум 20 актуальных gigs» выполнено **не полностью**
— доступно 3. Технически pipeline готов и прогнал их полностью; ограничение внешнее.

---

## 8. Сохранение сессии (п.5)

Проверено **фактически**, а не по наличию файла:

1. Chrome полностью закрыт.
2. Chrome перезапущен на **том же** профиле (`JobSearchContra`), без какого-либо участия входа.
3. `/jobs` открылась: **редиректа на `/log-in` нет**, отрендерились те же 3 заказа.

Состояние cookie-хранилища профиля (значения не читались, только факт и срок):

| Cookie | Срок |
| :--- | :--- |
| `AuthSession` | persisting, ~7 дней |
| `contra_authenticated` | persisting, ~1 день |
| `contra_web_app_service`, `_cfuvid` | session-only (не переживают перезапуск) |
| остальные (аналитика, intercom) | persisting, 90–400 дней |

Вывод: сессию подхватывает `AuthSession`; новый логин нужен только когда Contra сама её
инвалидирует. Требование выполнено.

---

## 9. Разделение архива и актуальных заказов (§9)

Три разных класса в `by_source_scope` после прогона:

```text
authenticated_feed  = 3     # актуальные открытые заказы (этот этап)
public_hire_pages   = 53    # архив P2 (EXPIRED/CLOSED), не удалён
synthetic_fixture   = 29    # тестовый датасет P1
```

Разделение действует везде:

```powershell
gig list --scope authenticated_feed                 # только актуальные
gig list --exclude-scope public_hire_pages          # скрыть архив явно
# GET /api/gigs?scope=authenticated_feed
# GET /api/gigs?exclude_scope=public_hire_pages
```

Проверено smoke-тестом: `scope=authenticated_feed` → 3 строки (все `OPEN`),
`scope=public_hire_pages` → только архивы (`EXPIRED`,`CLOSED`), множества id не пересекаются.
В UI — бейджи **LIVE FEED** / **ARCHIVE** / **FIXTURE** и отдельный фильтр «Область источника».

---

## 10. Тесты

```
.\.venv\Scripts\python.exe -m pytest tests/gig -q
→ 231 passed
```

Из них P2.1:

```
tests/gig/test_contra_auth_source.py        → 50 passed
tests/gig/test_contra_scope_separation.py   →  8 passed
```

Покрыто: парсинг полей, money/currency/типы бюджетов, стабильность `source_id`, отсутствующие
поля не выдумываются, деградация на битом входе, GraphQL-ошибки, лимит тарифа виден в данных
(`contra_feed_total_count`), `OPEN` не гейтится как закрытый, архив по-прежнему гейтится,
`payment_readiness` никогда не `ready`, разделение областей, `fetch()` бросает типизированную
ошибку при недоступной сессии. **Ни один тест не открывает браузер и не ходит в сеть** —
работают на сохранённой фикстуре.

## 11. Regression

```
.\.venv\Scripts\python.exe -m pytest tests -q  (без известных нестабильных browser-файлов)
→ 2603 passed, 0 failed
```

P1 (112) и P2 (55) тесты `tests/gig` зелёные; архивный адаптер `contra.py` не изменён и
продолжает работать. Пути P2.1 добавлены аддитивно.

Исключённые при прогоне файлы — те же, что и в P2, и по тем же причинам (чужая
незакоммиченная работа: `test_t78_search_only.py`, `test_ble001_fail_closed.py`,
`test_stage32b_browser_lifecycle.py`, `test_t77_geo_context.py`, `test_t725_ownership.py`,
`test_report_line_refs.py`).

---

## 12. Ограничения

1. **Тариф возвращает 3 из 233 записей.** Главное ограничение. 20+ актуальных заказов
   публично недоступны; систематический обход лимита поиском я не применял (см. §7).
2. **Нет описания заказа** в ленте — оценки `can_deliver` и `ai_automation_potential`
   строятся по заголовку, ролям и инструментам, поэтому консервативны.
3. **Нет URL конкретного заказа** — все проверенные маршруты (`/jobs/<slug>`, `/job/<slug>`,
   `/postings/<slug>`, `/jobs/<numeric>`) отдают 404 даже авторизованно. Открыть заказ можно
   только из ленты в браузере.
4. **Нет данных о клиенте и конкуренции** — `client_quality` (35) и `competition` (50) в
   breakdown являются нейтральными заглушками, а не измерениями.
5. **Сессия зависит от вашего профиля.** Если Contra инвалидирует `AuthSession` или профиль
   будет очищен, потребуется новый ручной вход. Автоматизации входа нет и не будет.
6. **Хрупкость к изменениям Contra.** Привязка к persisted-операции (`doc_id`
   `bd0d3aa180ccca2b973c48ab245fab2a`) и именам `JobFeed`/`Job`/`*JobBudget`. При их изменении
   источник вернёт типизированную ошибку (не молчаливый ноль) — это проверено тестами.
7. **Веса scoring не откалиброваны** (наследие P1/P2): `deliverability = 25` и
   `client_quality = 35` у всех трёх записей одинаковы по причине отсутствия данных, а не
   потому что заказы одинаковы.

---

## 13. Что это меняет в выводе P2

P2 заключил: «публичный Contra не отдаёт открытых заказов» — это было верно **для анонимного
доступа**, но вывод «Contra не подходит» оказался преждевременным:

| | P2 (анонимно) | P2.1 (авторизованно) |
| :--- | :--- | :--- |
| `/jobs` | редирект на логин | **открывается** |
| Заказы | витринные `EXPIRED`/`CLOSED` | **актуальные `OPEN`**, 1–2 дня назад |
| Число доступных | 54 архивных | 233 заявлено, **3 доступно тарифом** |
| Пригодность | нулевая (архив) | **ограничена тарифом**, но заказы реальные и свежие |

Итог: источник **пригоден** — он отдаёт свежие открытые заказы с бюджетом и клиентом.
Ограничение — коммерческое (Pro), и оно честно зафиксировано, а не обойдено.

---

## 14. P3 — предложение (не начинать без отдельной задачи)

1. **Решение о тарифе Contra Pro — за вами.** Это единственный способ получить все 233.
   Я не могу и не буду обходить лимит.
2. **Добавить второй источник с публичной лентой** (не Upwork/Freelancer/Fiverr — исключены
   ТЗ). Архитектура готова: `GigSource.fetch()` + провенанс + refresh + разделение областей.
3. **Калибровка scoring** на 3–10 реальных заказах: `estimated_effort_hours` и веса. Сейчас
   `client_quality`/`competition` — заглушки, ставка упирается в потолок.
4. **Описание заказа**: если Contra отдаёт его на detail-странице (в браузере, не через API),
   можно добавить read-only чтение detail через тот же CDP-транспорт — без обхода лимита
   ленты.
5. **Уборка:** решить судьбу архива `public_hire_pages` (53 записи); закрыть унаследованные
   падающие тесты из чужой незакоммиченной работы (порт 9111).
