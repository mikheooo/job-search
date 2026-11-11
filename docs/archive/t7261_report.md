# T7.26.1 — LIVE VALIDATION OF SEARCH_ONLY HEADLESS

Дата: **2026-09-30** (GMT+7). Задача — **validation-only**: ни одной правки продакшн-кода.
Ни одного отклика, ни одного сообщения.

**ИТОГ: `FIX`.** Живая проверка нашла **один реальный функциональный дефект** ровно в одной
строке. Всё остальное звено — владение браузером, авторизация, навигация, чтение вакансии,
structured preflight, geo-полоса, уборка — живьём **работает**.

---

## §A. FILE FREEZE BASELINE

`git rev-parse HEAD` = `98be2ea10cb1f8d5042d9caafce5509266f3f71a`.
Полный `git status` и снимок — `tools/.t7261/baseline.txt`. Авторитетный baseline (2026-09-30 11:04:57):

| файл | sha256 | размер | mtime |
|---|---|---|---|
| `ai_assistant/cli.py` | `62ea9a81d3623729dcad688710fc22b2a5f9606adb866ebe714516e6774c8bc5` | 309 121 B | 2026-09-29 16:25:16 |
| `ai_assistant/hh_autonomous_agent.py` | `c8338f7e8a39bfbec7e13d5bb6cff4cff3b75e8d47008b6943b92856596311fb` | 83 200 B | 2026-09-29 16:32:24 |
| `ai_assistant/hh_readonly_operation.py` | `156bf29acbc68e64d0618b75f2f7888b3964adbe71be751212b50fe1a45092ad` | 22 064 B | 2026-09-29 16:26:29 |
| `ai_assistant/hh_structured_preflight.py` | `da1f2ecd2eb986caa54d94026fb4e096b9b062f4792244f9ce1f1e73f91e7438` | 23 971 B | 2026-09-29 16:22:20 |
| `ai_assistant/hh_vacancy_navigator.py` | `9adf8f2b5b24de7619261bab3d17e71e1fd28a989d8e02271ed100b44b9b137e` | 19 619 B | 2026-09-29 16:32:22 |
| `ai_assistant/hh_browser_provider.py` | `847ded874c270c30cf7c86fbe9d56ff28bfe62c8ba895f9d36d45604163ee2af` | 73 737 B | 2026-09-29 14:06:04 |
| `ai_assistant/hh_browser_lease.py` | `6312f61dc17f67cb998a630485444f16da868dd2d1d9668d922d3cfdaddab939` | 22 510 B | 2026-09-29 13:40:50 |

Старые версии не восстанавливались, `.t725_reapply_cli.py` не запускался, чужая работа не
откатывалась. `cli.py` на момент старта был стабилен уже **~19 часов** (mtime 29.09 16:25) — то,
что T7.26 принял за «чужую сессию», к этому моменту просто лежало на диске.

**§2 FREEZE WATCH:** `tools/.t7261/freeze_check.sh` прогонялся **перед и после каждого** из трёх
циклов, плюс после контрфактуального прогона, плюс в конце. Результат во всех восьми точках:
`FREEZE STABLE: all 7 files unchanged`. `EXTERNAL_MODIFICATION_DETECTED` **не срабатывал ни разу**.

---

## §B. LIVE CYCLE 1 — IT Support + Bitrix24

Отдельный процесс, `HH_BROWSER_MODE=headless`, `SUBMIT_ALLOWED=false`.

```
env: HH_BROWSER_MODE='headless' SUBMIT_ALLOWED(os.environ)='true'
     submit_allowed(effective)=False endpoint=http://127.0.0.1:9111
```

Гейт проверен **правильным** способом: `.env` содержит `SUBMIT_ALLOWED=true` и
`load_dotenv(override=True)` затирает `os.environ`, но `config.py` снимает снимок оператора
**до** dotenv, поэтому `config.submit_allowed()` = `all("false","true")` = **False**. Kill-switch
действительно выключен. (Первая версия моего раннера читала `os.environ` уже после подмены и
отказалась запускаться — ошибка была в раннере, не в проекте.)

| факт | измерение |
|---|---|
| owned headless поднялся | `LEASE status=ACQUIRED owner_pid=25588`, endpoint `127.0.0.1:9111` |
| HH AUTH | **PASS** — `authenticated: true`, `applicant UI is rendered and no login card is present`, `h1: Михаил Колесников`, `hhrole: applicant`, `ready: complete` |
| реальные HH queries | **2 выполнены** (`IT Support`, `Bitrix24`), `status=OK` |
| discovered_count | **0** |
| вакансий прочитано | **0** |

Per-query:

| QUERY | RESULT COUNT | UNIQUE COUNT | READ COUNT |
|---|---|---|---|
| IT Support | 0 | 0 | 0 |
| Bitrix24 | 0 | 0 | 0 |

После цикла: `OWNED_TABS 0 / HEADLESS_PROCESSES 0 / PORT_9111 FREE / PROFILE_LOCK FREE / LEASE ABSENT` → **CLEAN**.
Release: `stopped: true`, `tabs_closed: 1`, `port_freed: true`, `profile_unlocked: true`, `owned_processes: 0`.

---

## §C. LIVE CYCLE 2 — System Administrator + CRM Support

Отдельный новый процесс.

| факт | измерение |
|---|---|
| owned headless | `LEASE status=ACQUIRED owner_pid=7704` |
| HH AUTH | **PASS** — `h1: Михаил Колесников`, `hhrole: applicant`, `login_input_present: false` |
| queries | `System Administrator` → 0/0, `CRM Support` → 0/0 |
| discovered_count / read | **0 / 0** |
| CLEAN после | **YES** |

**SESSION PERSISTENCE = PASS**: второй процесс вошёл в тот же аккаунт **без ручного логина** —
профиль `AppData\Local\JobSearchHeadless\User Data` несёт `hhtoken`, и обе проверки auth
подтвердили applicant-UI.

---

## §D. LIVE CYCLE 3 — штатный набор CandidateProfile

Запущена **буквальная CLI-команда** (§5 задачи):

```
HH_BROWSER_MODE=headless SUBMIT_ALLOWED=false \
  .venv/Scripts/python.exe -m ai_assistant.cli autonomous once --mode SEARCH_ONLY --json
```

Штатный набор — **30 запросов** из `DEFAULT_SEARCH_QUERIES` (IT Support … специалист по сопровождению).
Длительность 4 м 54 с.

```
status=SEARCH_ONLY   discovered_count=0   matched_count=0   read=0
applied_count=0  verified_count=0  messages_checked=0  auto_replies_count=0
interviews_detected=0  unanswered_questions_count=0  errors=[]
applications_processed=[]
summary: SEARCH_ONLY cycle: 30 queries [IT Support=0, Technical Support Engineer=0, ...
         специалист по сопровождению=0], 0 unique vacancies, 0 read in full, matched 0.
```

**Все 30 запросов вернули 0.** Итого по трём циклам выполнено **34 запуска запросов**, и ни один
не дал ни одной карточки. Freeze стабилен до и после; после цикла **CLEAN**.

---

## §E. ЖИВАЯ ВЫБОРКА ВАКАНСИЙ

Прямые живые чтения (через **тот же** продакшн-приём `_wait_for_page` + `structured_js` +
`structured_preflight` + `_geo_band`) — **12 уникальных вакансий**, `tools/.t7261/sample_table.md`.
Оговорка честности: id взяты диагностическим зондом из SERP, потому что SEARCH_ONLY сам до
карточек не доходит (§ дефект ниже). Это **не** результат трёх циклов.

| # | id | title | employer | salary | g/n | cur | exp | employment | workSchedule | formats | area | archived | resp | band | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 137920030 | Инженер технической поддержки / Support engineer | БПС Инновационные Программные Решения | не указана | – | – | between1And3 | ТК РФ | fullDay | HYBRID | Москва/RU | None | True | C | USER_DECISION_REQUIRED |
| 2 | 137910130 | IT Support Manager | Fersol | не указана | – | – | moreThan6 | ТК РФ | fullDay | ON_SITE | Москва/RU | None | True | C | USER_DECISION_REQUIRED |
| 3 | 136810027 | Support Engineer / Специалист по сопровождению (B2B) | НТ Прогресс | from 90000 | net | RUR | noExperience | ТК РФ | fullDay | ON_SITE, HYBRID | Москва/RU | None | True | C | USER_DECISION_REQUIRED |
| 4 | 137244284 | Head of Localization (Europe) | Betby | from 3000 to 5000 | net | EUR | between3And6 | ГПХ (вид не распознан) | remote | REMOTE | Москва/RU | None | True | C | USER_DECISION_REQUIRED |
| 5 | 137302319 | Python Developer (AD Robot) | Интерактивное агентство Это Легко | не указана | – | – | between3And6 | ГПХ (вид не распознан) | remote | REMOTE | Москва/RU | None | True | **B** | USER_DECISION_REQUIRED |
| 6 | 137901073 | AML / Compliance Officer | ПИПЛ.найм | не указана | – | – | between3And6 | ГПХ (вид не распознан) | remote | REMOTE | Москва/RU | None | True | **B** | USER_DECISION_REQUIRED |
| 7 | 137680382 | Senior Full Stack PHP-разработчик | Система Геткурс | не указана | – | – | moreThan6 | ГПХ (вид не распознан) | remote | REMOTE | Москва/RU | None | True | C | USER_DECISION_REQUIRED |
| 8 | 137894809 | Куратор клиентского опыта | Айсмарт | не указана | – | – | between1And3 | ГПХ (`INDIVIDUAL_PERSON`) | remote | REMOTE | Москва/RU | None | True | **B** | USER_DECISION_REQUIRED |
| 9 | 137875784 | Саппорт / Специалист по внедрению | Sycret.ru | from 90000 to 120000 | net | RUR | between1And3 | ГПХ (вид не распознан) | fullDay | HYBRID | Москва/RU | None | True | C | USER_DECISION_REQUIRED |
| 10 | 137869752 | Online school client support manager (Italy) | Novakid Inc | to 1300 | gross | USD | between1And3 | ГПХ (вид не распознан) | remote | REMOTE | Москва/RU | None | True | **B** | USER_DECISION_REQUIRED |
| 11 | 137799774 | Content Tech Specialist | Novakid Inc | to 1500 | gross | USD | noExperience | ГПХ (вид не распознан) | remote | REMOTE | Москва/RU | None | True | **B** | USER_DECISION_REQUIRED |
| 12 | 137866560 | Директор по эксплуатации и инфраструктуре | (видимое имя = целая фраза, `name`/`id` = null) | from 400000 to 650000 | gross | RUR | between3And6 | ТК РФ | fullDay | HYBRID | Москва/RU | None | True | C | USER_DECISION_REQUIRED |

Ни одна из этих вакансий **не отправлялась**.

---

## §F. STRUCTURED PREFLIGHT ЖИВЬЁМ

`REQUIRED_STRUCTURED_FIELDS` — это ключи **snapshot** (`short_vacancy` / `vacancy` / `applicant`),
и проверять надо `preflight["fields_missing"]`, а не ключи возвращаемого словаря.

По всем 12 вакансиям:

| поле | результат |
|---|---|
| `workExperience` | present ×12 |
| `civilLawContracts` | present ×12 |
| `acceptLaborContract` | present ×12 |
| `workFormats` | present ×12 |
| `workSchedule` | present ×12 |
| `employment` | present ×12 |
| `compensation` | present ×12 |
| `area` | present ×12 |
| `test` | present ×12 |
| **`vacancyTests`** | **None ×12** — реально отсутствует в payload, показан как missing |
| `responseAllowed` | present ×12 |
| `responseImpossible` | present ×12 |
| **`archived`** (T7.26 §6) | **None ×12** — страница не заявила, `False` по умолчанию не подставляется |
| **`employer`** (T7.26 §6) | present ×12 (`visibleName` + `name` + `id`) |

`fields_missing = ['vacancyTests']`, `fields_read = 11/12`, `read_complete = False` — по всем 12.
Отсутствие показано как отсутствие, догадкой не заменено. Вердикт по всем 12:
`USER_DECISION_REQUIRED`, `auto_send_permitted = False` — то есть
`USER_DECISION_REQUIRED` **не** превращается в автоматическое разрешение.

**Наблюдение (пре-существующее, не T7.26):** при `civilLawContracts: [{}]` (пустой объект) и
`acceptLaborContract: false` ридер `flatten_civil_law_contracts` отдаёт `["{}"]` и форма
называется «Договор ГПХ (вид не распознан)». То есть **пустой** объект становится *названной*
формой. Направление гейта при этом консервативное (человек, не авто-отправка), и это поведение
T7.22 — в этой задаче не правилось.

---

## §G. NAVIGATION ЖИВЬЁМ

По всем 12 навигациям (SERP → вакансия, вакансия → вакансия):

| проверка | результат |
|---|---|
| стартовая URL | `https://hh.ru/search/vacancy?text=...&area=1&order_by=publication_time` |
| конечная URL | `https://hh.ru/vacancy/<id>` — **совпала с целевой**, редиректа не было |
| `about:blank` не принят | условие settle требует `href != "about:blank"`; во всех 12 возвращён целевой href |
| `readyState` | `complete` ×12 |
| SSR / vacancy marker | `#HH-Lux-InitialState` = `true` ×12; `[data-qa="vacancy-description"]` = `true` ×12 |
| timeout | **не сработал** ни разу |
| fixed sleep как критерий | **не использовался** — критерий `URL изменилась + документ отрисован` |

Замер сразу после `navigate`: `{"href":"https://hh.ru/vacancy/<id>","ready":"complete"}` — документ
уже целевой, поэтому settle подтверждает его, а не предыдущий. Отдельно: на **SERP** документ
после `complete` продолжает гидрироваться (20 карточек → 50 за ~3 с) — это важно для §10 и
отдельно от дефекта селектора.

---

## §H. CLEANUP

После **каждого** из трёх циклов и после контрфактуального прогона:

```
OWNED_TABS 0 / HEADLESS_PROCESSES 0 / PORT_9111 FREE / PROFILE_LOCK FREE / LEASE ABSENT
```

Финальное состояние — **CLEAN**.

**Один инцидент, о котором честно:** контрфактуальный прогон №1 был прерван **хуком окружения**
(`SAFE_DELETE_BULK_CONFIRM_REQUIRED`, `count: 268`, `threshold: 50`, цель
`artifacts/headless-poc/browser-lease.json`) — релиз не смог удалить файл lease, и остался
зависший lease: `owner_pid 18388 (dead)`, активных операций 0, `instance_is_ours: false`.
Контракт назвал его `LIVE` (heartbeat свежий), не `STALE` — по своей же логике «нечего
отбирать, живого нашего инстанса нет». Убран **через контракт**: `clear_lease(session_id)` → `True`,
`state ABSENT`. Повторный прогон после этого прошёл полностью, включая корректный релиз.
Процессов, порта и блокировки профиля не оставалось ни на секунду — только запись lease.

---

## §I. NO-WRITE PROOF

`tools/.t7261/db_snapshot.py`, до и после живых прогонов:

| метрика | before | after | изменение |
|---|---|---|---|
| `application_submissions_total` | 21 | 21 | **0** |
| `tracking_submitted` | 8 | 8 | **0** |
| `status_history_submitted` | 8 | 8 | **0** |
| `status_history_max_id` | 1866 | 1866 | **0** |
| `hh_applications_total` | 18 | 18 | **0** |
| `hh_message_events_total` | 36 | 36 | **0** |
| `hh_message_events_from_me` | 0 | 0 | **0** |
| `autonomous_notifications_total` | 1 | 1 | **0** |
| `autonomous_cycle_runs_total` | 7 | 9 | **+2** ← разрешённый штатный лог циклов |

**APPLICATIONS SENT = 0. MESSAGES SENT = 0.** Изменилась только таблица, которую архитектура
SEARCH_ONLY и должна писать (собственный лог прогона).

---

## §J. TESTS

`tools/.t7261` — 151 passed за 41 с:

```
tests/test_t726_search_only_headless.py
tests/test_t725_ownership.py
tests/test_t724_browser_lease.py
tests/test_t723_structured_preflight.py
tests/test_stage41_vacancy_navigation.py
tests/test_stage44_second_vacancy_flow.py
tests/test_stage48_next_suitable_vacancy.py
tests/test_stage49_fetch_and_select_vacancy.py
```

`SUBMIT_ALLOWED` в окружении **не** выставлялся (иначе ломается ~97 тестов отправки); живые
команды получали `SUBMIT_ALLOWED=false` **в командной строке**.

---

## ДЕФЕКТ — ТОЧНАЯ ПРИЧИНА

**Одна строка в `ai_assistant/hh_readonly_operation.py`.**

```python
READ_CARDS_JS = ... document.querySelectorAll('[data-qa="serp-item"]') ...
```

На живой странице HH **узла `data-qa="serp-item"` не существует**. Замер `serp_probe.py` по
`https://hh.ru/search/vacancy?text=IT Support&area=1&order_by=publication_time`:

```
title     : 3 ・ Работа в Москве, поиск персонала и публикация вакансий - hh.ru
body      : Найдено 103 вакансии «IT Support» … 50 вакансий
data-qa="serp-item"            → 0      ← то, что ищет продакшн
data-qa="serp-item__title"     → 50     ← ссылка на вакансию существует
data-qa="vacancy-serp__vacancy"→ 50     ← живой контейнер карточки
captcha   : False    lux_state: True    ready: complete
```

Отсюда вся цепочка: `READ_CARDS_JS` возвращает `count: 0, cards: []` → `seen` пуст →
`unique_count = 0` → цикл чтения не выполняется ни разу → `discovered_count = 0`,
`read_count = 0`. Ровно то, что показали все три цикла (34 запуска запроса).

**Контрфактуальное доказательство** (`tools/.t7261/fix_hypothesis.py`): та же JS-строка
подменяется **только в памяти процесса** (файл на диске не тронут) на
`[data-qa="vacancy-serp__vacancy"], [data-qa="serp-item"]`, после чего запускается **настоящий**
`run_autonomous_cycle` → `_search_only_cycle`:

```
IN-MEMORY PATCH: '[data-qa="serp-item"]' -> '[data-qa="vacancy-serp__vacancy"], [data-qa="serp-item"]'
SEARCH_ONLY query 'IT Support': results=20 unique=20 status=OK
SEARCH_ONLY query 'Bitrix24' : results=20 unique=20 status=OK
SEARCH_ONLY match: Интерактивное агентство Это Легко / Python Developer (AD Robot) (88.0) band=B
SEARCH_ONLY match: НТ Прогресс / Support Engineer ... (B2B) (77.0) band=C
COUNTERFACTUAL CYCLE: status=SEARCH_ONLY discovered=40 matched=2
AUTH ok=True  read_count=10 unique=40 result_count=40
after: {"headless_processes": 0, "port_listening": false, "lease_present": false, "lease_state": "ABSENT"}
release: stopped=true tabs_closed=1 port_freed=true profile_unlocked=true
```

**0 → 40.** Значит дефект — именно эта строка, и ничего больше: владение, авторизация, поиск,
чтение, preflight, geo-полоса, скоринг (`evaluate_candidate_match` дал 88.0 и 77.0), уборка —
всё работает.

**Сопутствующий риск (не проверен, потому что упёрся в дефект):** в том же `READ_CARDS_JS`
зарплата читается селектором `[data-qa="vacancy-serp__vacancy-compensation"]`; в живом DOM
видны `vacancy-serp__vacancy-compensation-frequency-*`, то есть точное имя атрибута могло тоже
смениться. После починки контейнера это надо перепроверить — иначе `salary` в карточке будет
`None` при реально указанной зарплате.

---

## ИТОГОВАЯ ТАБЛИЦА

| пункт | значение |
|---|---|
| SEARCH_ONLY USES HEADLESS | **YES** (owned lease `OWNER_AUTONOMOUS`, endpoint 9111, release после сегмента) |
| HH AUTH | **PASS** (три цикла + 5 зондов, `hhrole: applicant`, без ручного логина) |
| HH SEARCH WORKS | **NO** — 0 карточек на 34 запусках запроса |
| TOTAL UNIQUE DISCOVERED | **0** (контрфакт с исправленной строкой: **40**) |
| VACANCIES READ | **0** через SEARCH_ONLY (живьём прочитано **12** прямым зондом) |
| CYCLE 1 CLEAN | **YES** |
| CYCLE 2 CLEAN | **YES** |
| CYCLE 3 CLEAN | **YES** |
| SESSION PERSISTENCE | **PASS** |
| VISIBLE WINDOWS | **0** (40/40 замеров) |
| FOCUS STOLEN | **NO** (40/40: foreground ни разу не chrome) |
| APPLICATIONS SENT | **0** |
| MESSAGES SENT | **0** |
| PRODUCTION UNOWNED PATHS | **0** |
| CLI.PY MODIFIED BY THIS TASK | **NO** |
| EXTERNAL MODIFICATION DETECTED | **NO** (freeze стабилен в 8/8 точек) |

---

## VERDICT: **FIX**

Причина одна и она точная: `READ_CARDS_JS` ищет контейнер `[data-qa="serp-item"]`, которого на
живой странице HH нет; живой контейнер — `[data-qa="vacancy-serp__vacancy"]`. Пока эта строка
такая, SEARCH_ONLY поднимает браузер, авторизуется, делает запросы — и не находит ничего.

Задача была validation-only, поэтому **код не правился** (§13: «остановиться с FIX и описать
точную причину, не начинать большую новую переделку внутри этого задания»). Правка — следующая
задача, и она размером в одну строку (плюс перепроверка селектора зарплаты).

**Остановлено. Ничего не восстанавливалось.**

---

## АДДЕНДУМ (пост-закрытие): EXTERNAL_MODIFICATION_DETECTED

Проверка §15K при закрытии задачи дала `FREEZE STABLE: all 7 files unchanged` (8/8 точек).
**Сразу после закрытия** тот же чекер сработал. Правило §2/§15 — остановиться и доложить,
ничего не восстанавливать; так и сделано.

| факт | значение |
|---|---|
| что изменилось | **только `ai_assistant/cli.py`** |
| было (baseline) | `62ea9a81d3623729…` · 309 121 B · mtime 2026-09-29 16:25:16 |
| стало (первое) | `40ef3962ae340221…` · 309 427 B · mtime 2026-09-30 11:34:17 |
| стало (второе) | `fd66258c35cb75f9…` · 309 777 B · mtime 2026-09-30 11:36:01 |
| остальные 6 файлов | **OK**, побайтово те же, включая дефектный `hh_readonly_operation.py` (`156bf29a…`) |
| синтаксис нового `cli.py` | парсится (`ast.parse` OK) |

**Причина изменения — параллельная сессия по фиче `gig`.** В том же окне (11:30–11:36) правились
`ai_assistant/gig/api.py`, `ai_assistant/gig/cli.py`, `ai_assistant/gig/sources/contra_auth.py`,
`ai_assistant/gig/store.py`, `tests/gig/test_contra_auth_source.py`, `.gitignore`. Дифф
снимка `tools/.t726/cli.py.external_62ea9a81.py` против текущего файла — **3 хунка, +17 / −1**,
все внутри блока `gig`:

- `gig list`: добавлены `--scope`, `--exclude-scope` (≈9873–9880);
- `gig fetch`: в `choices` источника добавлен `"contra-auth"` (≈9907);
- `gig fetch`: добавлены `--preset`, `--sort` (≈9934–9941).

Правки — в районе строки 9 900, то есть **далеко** от путей автономного цикла (`once`-парсер
≈5139, диспатч ≈5702, `autonomous_once_cmd` ≈6305). Дефолт `--mode` остался `SEARCH_ONLY`.
Рост байтов между первым и вторым переписыванием (309 427 → 309 777) идёт по окончаниям строк
(голых `\r`: 13 010 → 13 018), а не по коду: кумулятивный дифф тот же самый.

**Что это меняет для выводов задачи: ничего.** Вердикт **FIX** в силе, дефект на месте и не
исправлен. Уточнение по дефекту: мёртвый селектор `[data-qa="serp-item"]` сидит **в двух**
блоках — `COUNT_RESULTS_JS` (стр. 31) и `READ_CARDS_JS` (стр. 61). Правка следующей задачи
обязана накрыть оба.

**Мир после обнаружения — `CLEAN`:** `OWNED_TABS 0 / HEADLESS_PROCESSES 0 / PORT_9111_FREE True /
PROFILE_LOCK_FREE True / LEASE_ABSENT True`.

Ничего не восстановлено, ничего не откатано, `cli.py` не тронут.
