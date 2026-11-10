# T7.26.2 — FIX HH SERP SELECTORS + LIVE CONFIRMATION

**Дата:** 2026-09-30 · **Вердикт:** `PASS`
**Правленый файл:** `ai_assistant/hh_readonly_operation.py`
`156bf29acbc68e64…` (22 064 B) → `1a2c073b65ff3d97dd11af4ca059b26d5dacbea1d9e84ec61256234840f03e9e` (23 808 B)
`cli.py` **не тронут** (`fd66258c…`, 309 777 B, mtime 11:36:01 — тот же, что был на входе в задачу).

---

## 1. Что исправлено

### 1.1 Контейнер карточек — два места

Дефект T7.26.1: `COUNT_RESULTS_JS` и `READ_CARDS_JS` искали `[data-qa="serp-item"]`, которого на
живой странице нет. Измерено заново (30.09, `IT Support`, `area=1`, `order_by=publication_time`):

| селектор | найдено |
|---|---|
| `[data-qa="serp-item"]` | **0** |
| `[data-qa="vacancy-serp__vacancy"]` | **20** |
| `a[data-qa="serp-item__title"]` | 20 (жив — это *другое* значение атрибута) |

Оба места теперь берут селектор из **одной** константы:

```python
CARD_CONTAINER_SELECTORS = (
    '[data-qa="vacancy-serp__vacancy"]',   # живой, измеренный
    '[data-qa="serp-item"]',               # фолбэк на случай отката DOM
)
CARD_CONTAINER_SELECTOR = ", ".join(CARD_CONTAINER_SELECTORS)
```

Скрипты собираются из неё подстановкой, а не копипастой — **устаревшая копия и была причиной
слепоты**: строка дублировалась в двух блоках, и оба остались в прошлом.

### 1.2 Зарплата — дефект оказался двойным

Задача предполагала «устаревший селектор». Замер показал хуже: **`data-qa` на сумме нет вообще**.

Реальная разметка (дамп `tools/.t7262/salary_dom_probe.py`, карточка с зарплатой):

```html
<div class="compensation-labels--vwum2s12fQUurc2J compensation-labels_magritte--pbBIkJ7Ww24ZILKz">
  <span class="magritte-text___pbpft_...">от&nbsp;<data value="90000">90 000</data>&nbsp;<data value="RUB">₽</data>&nbsp;за&nbsp;месяц, на руки</span>
  <div class="magritte-tag___WdGxk"><div class="magritte-tag__label___YHV-o"><data value="0" data-qa="vacancy-serp__vacancy-work-experience-noExperience">Без опыта</data></div></div>
  <div class="magritte-tag___WdGxk"><div class="magritte-tag__label___YHV-o"><span data-qa="vacancy-serp__vacancy-compensation-frequency-TWICE_PER_MONTH">Выплаты: Два раза в&nbsp;месяц</span></div></div>
</div>
```

Факты:

- сумма — **первый прямой ребёнок** блока `compensation-labels`, `SPAN` **без `data-qa`**;
- единственный `data-qa` в блоке — `vacancy-serp__vacancy-compensation-frequency-TWICE_PER_MONTH`
  на `SPAN` с текстом **«Выплаты: Два раза в месяц»**, то есть это **не зарплата**;
- `[data-qa="vacancy-serp__vacancy-compensation"]` не существует;
- у карточки **без** зарплаты первым ребёнком идёт `DIV`-тег, а не `SPAN`.

Отсюда второй, более неприятный дефект: старый фолбэк `[data-qa*="compensation"]` матчил **частоту
выплат** и возвращал её как зарплату — не `None`, а **уверенно неверное значение**. Он убран.

```python
SALARY_SELECTOR = (
    '[data-qa="vacancy-serp__vacancy-compensation"], '   # если HH вернёт старую разметку
    '[class*="compensation-labels"] > span'              # измеренный живой вариант
)
```

Бизнес-логика зарплаты не менялась: по-прежнему `innerText.trim()` одного элемента.

---

## 2. Живое подтверждение селектора зарплаты (§2 задания)

Кросс-проверка: значение из **карточки SERP** (наш селектор) против значения из **страницы
вакансии** (`vacancy.compensation`, структурный payload). Совпали на **всех 10** прочитанных:

| vacancy_id | карточка (SERP DOM) | страница (payload) | сходится |
|---|---|---|---|
| 137920030 | `None` | `{"noCompensation": true}` | ✓ |
| 137910130 | `None` | `{"noCompensation": true}` | ✓ |
| 136810027 | `от 90 000 ₽ за месяц, на руки` | `from 90000, RUR, gross=false` | ✓ |
| 137244284 | `3 000 – 5 000 € за месяц, на руки` | `from 3000 to 5000, EUR, gross=false` | ✓ |
| 137302319 | `None` | `{"noCompensation": true}` | ✓ |
| 137901073 | `None` | `{"noCompensation": true}` | ✓ |
| 137680382 | `None` | `{"noCompensation": true}` | ✓ |
| 137894809 | `None` | `{"noCompensation": true}` | ✓ |
| 137875784 | `90 000 – 120 000 ₽ за месяц, на руки` | `from 90000 to 120000, RUR, gross=false` | ✓ |
| 137869752 | `до 1 300 $ за месяц, до вычета налогов` | `to 1300, USD, gross=true` | ✓ |

Сходится и **gross/net**: «на руки» ↔ `gross=false`, «до вычета налогов» ↔ `gross=true`. В отдельном
цикле по Bitrix24 прочитано ещё 10 карточек, **9 из них с зарплатой** (₽ и $, диапазоны и «от»),
например `170 000 – 200 000 ₽ за месяц, на руки`, `от 5 000 $ за месяц, на руки`.

Итого: **23 прочитанные карточки, 13 с зарплатой — все значения верные, ни одного «Выплаты: …».**

---

## 3. Регрессионный тест (§3 задания)

`tests/test_t7262_serp_selectors.py` — **12 тестов, все зелёные** (1.8–6.7 с).

Почему старый набор не мог поймать дефект: `_FakeSession.evaluate_on` возвращал карточки по
**идентичности строки** `ro.READ_CARDS_JS`. Мок совпадал по объекту, а устаревший селектор объект
как раз и сохраняет. Ни один селектор ни разу не проверялся против DOM.

Новый файл разделён:

- **контракт** (без браузера): живой атрибут обязан быть первым; старый остаётся только фолбэком;
  **оба** скрипта собраны из одной константы; подстановочных плейсхолдеров не осталось; вредный
  `[data-qa*="compensation"]` в зарплатном селекторе отсутствует;
- **DOM** (настоящий браузер, настоящий документ): фикстура повторяет живую разметку, включая
  ловушку — `serp-item__title` жив, `serp-item` мёртв. Прогоняются **production**-скрипты.

**Негативные контроли** (без них тест, который всегда зелёный, ничего не доказывает):

- `test_the_old_selector_finds_nothing_in_the_live_dom` — старый селектор на той же странице даёт 0;
- `test_the_legacy_reader_reads_zero_cards` — старый контейнер, подставленный обратно **в production-скрипт**,
  даёт `count=0, cards=[]`, новый на том же документе — 2;
- `test_the_old_salary_fallback_would_have_reported_the_frequency_note` — старый зарплатный фолбэк
  возвращает «Выплаты: Два раза в месяц» и **не содержит** «90 000».

DOM-часть пропускается (не падает), если chromium недоступен: это регрессионная сеть, а не причина
ломать чужую машину. Playwright — штатная зависимость проекта (`browser_executor.py`, `matcher.py`),
новых зависимостей не добавлено.

---

## 4. Живые прогоны (§4 задания)

Все прогоны: `HH_BROWSER_MODE=headless`, `SUBMIT_ALLOWED=false` **в командной строке**
(`config.submit_allowed()` → `False`; `os.environ` при этом показывает `'true'` из `.env` — это
нормально и разобрано в T7.26.1).

### 4.1 IT Support + Bitrix24 (`tools/.t7262/cycle_it_bitrix.json`)

| запрос | result_count | extracted | unique_count | status |
|---|---|---|---|---|
| IT Support | **50** | 50 | **50** | OK |
| Bitrix24 | **50** | 50 | **50** | OK |

Цикл: `discovered_count=100`, `matched_count=2`, `read_count=10`, `errors=[]`.
AUTH `ok=True` (`Михаил Колесников`, `hhrole: applicant`), lease `ACQUIRED` → релиз
`stopped=true, tabs_closed=1, port_freed=true, profile_unlocked=true, owned_processes=0`.

Чтения выбираются из **объединения** в порядке обнаружения, поэтому все 10 пришли из первого
запроса. Чтобы это не читалось как «Bitrix24 читать нечего», сделан отдельный прогон:

### 4.2 Bitrix24 отдельно (`tools/.t7262/cycle_bitrix_only.json`)

| запрос | result_count | extracted | unique_count | status |
|---|---|---|---|---|
| Bitrix24 | **20** | 20 | **20** | OK |

`read_count=10`, релиз чистый. Карточки Bitrix24 читаются end-to-end.

### 4.3 Штатный `autonomous once --mode SEARCH_ONLY --json` (`tools/.t7262/cli_stock_search_only.out`)

Тот же профиль, тот же набор из 30 запросов, что в T7.26.1 — и результат несопоставим:

| | T7.26.1 (`cycle_runs.id=9`) | **T7.26.2 (`id=12`)** |
|---|---|---|
| запросов | 30 | 30 |
| discovered_count | **0** | **508** |
| matched_count | 0 | 2 |
| read_count | 0 | 10 |
| errors | `[]` | `[]` |

Ни одного нуля по запросам: `IT Support=50, Technical Support Engineer=50, Support Engineer=50,
Application Support=23, Application Support Engineer=3, Help Desk=50, Service Desk=20,
System Administrator=20, IT Administrator=20, Windows Server Administrator=20,
Active Directory Administrator=20, Bitrix24=20, Bitrix24 Administrator=20, Bitrix24 Support=11,
CRM Administrator=20, CRM Support=50, IT Infrastructure Engineer=20, IT Automation Engineer=20,
Integrations Engineer=20, Python Developer=20, Backend Python Developer=20, AI Engineer=20,
AI Agent Engineer=20, Python FastAPI=20, Automation Engineer Python=20,
специалист технической поддержки=20, инженер технической поддержки=20, системный администратор=20,
администратор Bitrix24=20, специалист по сопровождению=20`.

---

## 5. Уборка (§5 задания)

После **каждого** прогона, и после всех сразу:

```
OWNED_TABS                : 0
HEADLESS_PROCESSES        : 0  []
PORT_9111_FREE            : True
PROFILE_LOCK_FREE         : True
LEASE_ABSENT              : True  (state ABSENT)
CLEAN                     : True
```

### Невидимость

Первый зонд (`window_probe.ps1`, по всем `chrome.exe`) дал `visible_chrome_windows=1` — но это
**личный Chrome оператора**: 47–49 процессов и одно видимое окно. Приписывать это раннеру нельзя.

Переделанный зонд (`tools/.t7262/window_probe2.ps1`) сначала отбирает процессы по командной строке
с выделенным профилем `JobSearchHeadless`, и только у них смотрит `MainWindowHandle`:

```
t=  5s all_chrome= 49 our_chrome= 17 our_visible_windows= 0 foreground_is_ours=False title='ChatGPT'
t= 15s all_chrome= 49 our_chrome= 18 our_visible_windows= 0 foreground_is_ours=False title='ChatGPT'
t= 30s all_chrome= 50 our_chrome= 17 our_visible_windows= 0 foreground_is_ours=False title='ChatGPT'
t= 35s all_chrome= 34 our_chrome=  0 our_visible_windows= 0 foreground_is_ours=False title='ChatGPT'
SUMMARY samples=14 samples_with_our_chrome=6 total_our_visible_windows=0 samples_foreground_was_ours=0
```

**VISIBLE WINDOWS = 0** (6 замеров поймали наш браузер живым — все с нулём видимых окон),
**FOCUS STOLEN = NO** (14/14 foreground не наш; во всех замерах — `ChatGPT`).
Автоматизации мыши Windows в задаче нет.

---

## 6. Безопасность и доказательство «ничего не записано» (§6 задания)

Снимок БД до/после — сравнение с `tools/.t7261/db_after.json` (конец T7.26.1):

| величина | конец T7.26.1 | сейчас | Δ |
|---|---|---|---|
| `application_submissions_total` | 21 | 21 | **0** |
| `tracking_total` | 1794 | 1794 | **0** |
| `tracking_submitted` | 8 | 8 | **0** |
| `tracking_max_updated_at` | 2026-09-29T03:01:05 | без изменений | — |
| `status_history_total` | 1865 | 1865 | **0** |
| `status_history_submitted` | 8 | 8 | **0** |
| `status_history_max_id` | 1866 | 1866 | **0** |
| `hh_applications_total` | 18 | 18 | **0** |
| `hh_message_events_total` | 36 | 36 | **0** |
| `hh_message_events_from_me` | **0** | **0** | **0** |
| `autonomous_notifications_total` | 1 | 1 | **0** |
| `autonomous_cycle_runs_total` | 9 | 12 | +3 *(разрешённый лог)* |

`tracking_max_updated_at` — **29.09**, то есть до сегодняшних прогонов: в трекинг не записано ничего.

**+3 разобраны построчно** (не «примерно»): `id=10` — контрфакт самого T7.26.1 (11:29, `discovered=40`),
`id=11` и `id=12` — два моих цикла. У обоих `applied_count=0`, `messages_checked=0`,
`auto_replies_count=0`, `errors=[]`.

**APPLICATIONS SENT = 0. MESSAGES SENT = 0.**

### Внешние правки во время проверки

`cli.py` в ходе задачи **не менялся** (`fd66258c…`, 309 777 B, mtime 11:36:01 — как на входе).
Чужая правка `gig`-сессии, зафиксированная пост-закрытием T7.26.1, осталась на месте и **не
откатывалась**. Единственное изменение в freeze-списке, кроме неё, — мой намеренный фикс.

---

## 7. Требуемый результат (§7 задания)

```
IT SUPPORT:
  result_count = 50
  unique_count = 50
  read_count   = 10   (чтения берутся из объединения в порядке обнаружения;
                       в цикле «IT Support + Bitrix24» все 10 пришли из IT Support)

BITRIX24:
  result_count = 50   (в двухзапросном цикле) / 20 (в отдельном цикле)
  unique_count = 50   / 20
  read_count   = 10   (отдельный цикл по Bitrix24: read 10 из 20, 9 из 10 — с зарплатой)

DEFAULT SEARCH_ONLY:
  queries         = 30 (штатный CandidateProfile)
  discovered_count = 508
  matched_count    = 2
  read_count       = 10

SALARY SELECTOR:
  FIXED
  evidence = data-qa на сумме отсутствует; сумма — первый прямой SPAN-ребёнок блока
             compensation-labels; старый фолбэк [data-qa*="compensation"] матчил «Выплаты:
             Два раза в месяц» и возвращал это как зарплату. Кросс-проверка карточка↔страница
             сходится на 10/10 (включая gross/net). 23 прочитанные карточки, 13 с зарплатой,
             все значения верные.

CLEANUP:
  tabs         = 0
  processes    = 0
  port         = FREE (9111)
  profile_lock = FREE
  lease        = ABSENT

VISIBLE WINDOWS: 0   (по нашим процессам, 6/6 замеров с живым браузером)
FOCUS STOLEN:    NO  (14/14)
APPLICATIONS SENT: 0
MESSAGES SENT:     0

VERDICT: PASS
```

---

## 8. Побочные находки (вне объёма, не правились)

### 8.1 Три устаревших теста в `test_t78_search_only.py` — красные ДО этой задачи

`test_search_only_cycle_writes_nothing_outward`, `test_an_unknown_mode_is_treated_as_search_only`,
`test_an_existing_approval_survives_a_search_only_cycle` падают на предохранителе conftest
(`SAFETY VIOLATION: unmocked socket connection attempted: ('127.0.0.1', 9111)`).

Причина: они подсовывают `AutonomousConfig(evaluate_fn=mock_cdp, ...)`, а SEARCH_ONLY с T7.26 идёт
через **owned**-операцию и `evaluate_fn` не вызывает вовсе — тест доходит до настоящего браузера.
Это регрессия **T7.26**, не T7.26.2.

**Доказано A/B, а не заявлено.** Домиграционный файл восстановлен обратным патчем и сверен по хешу:
`8d53c503…` → после исправления трансляции `\n` в `stdout` → **`156bf29a…`**, побайтово то же, что
до правки. С ним те же три теста падают ровно так же (3 failed, 8 passed). С моим фиксом — то же
самое. **Моя правка к падениям отношения не имеет.**

Свойство «SEARCH_ONLY ничего не пишет наружу» при этом покрыто в наборе T7.26
(`test_the_search_segment_never_submits_and_never_sends`: AST-скан на запрещённые вызовы +
`submitted == 0` / `messages_sent == 0`), так что дыра не в покрытии, а в трёх мёртвых дублях.
Чинить их — отдельная задача про тестовый набор.

### 8.2 `format_label` в карточке — косметическая неточность

`[class*="magritte-tag"]` как фолбэк матчит **тег опыта**, поэтому у карточки без
`vacancy-label-*` в `format_label` попадает `«Опыт 1-3 года»`. На решение о полосе это не влияет
(полоса считается из `vacancy.workFormats` страницы, не из карточки). Вне объёма — не правил.

---

## 9. Тесты

| набор | результат |
|---|---|
| `tests/test_t7262_serp_selectors.py` (новый) | **12 passed** |
| `test_t726_search_only_headless` + `test_t725_ownership` + `test_t724_browser_lease` + `test_t723_structured_preflight` + `test_t78_search_only` | **127 passed, 3 failed** — все три устаревшие, A/B-доказано |

Прогон **без** `SUBMIT_ALLOWED` в окружении (иначе ломается ~97 тестов отправки) — как требует
конвенция проекта.
