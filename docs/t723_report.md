# T7.23 — ЖЁСТКАЯ УБОРКА И СТРУКТУРНЫЙ PREFLIGHT

Задача закрывала два оставшихся риска перед массовой headless-работой:

1. гарантия, что **после любого исхода** раннер не оставляет ни вкладок, ни процессов,
   ни занятого порта, ни замка профиля;
2. запрет отправлять, если **материальные структурные поля вакансии не прочитаны** до POST.

**Отправлено в этой задаче: НОЛЬ.** Ни отклика, ни сообщения работодателю, ни напоминания,
ни письма EvaTeam, ни `FULL`, ни `git push`. Всё, что здесь есть, — чтение и измерения.

---

## §A. CLEANUP OWNERSHIP — принадлежность вкладок

Раньше сессия помнила **список id** вкладок, которые открыла. Из этого нельзя ответить на
вопрос «а эта вкладка чья?», и нельзя доказать, что чужая не закрыта: при нескольких
открытых вкладках hh.ru «та самая вкладка hh.ru» — не определённый объект.

Теперь принадлежность **регистрируется при создании** и никогда не выводится задним числом.
`OwnedTab` (`ai_assistant/hh_browser_provider.py:841`) несёт четыре поля:

| поле | смысл |
|---|---|
| `target_id` | id цели CDP |
| `purpose` | зачем вкладку открыли |
| `url` | что в ней открыто |
| `created_at` | когда |

Словарь назначений (`hh_browser_provider.py:833`): `work`, `vacancy`, `form`, `search`,
`auth`, `extra`. Назначение — не украшение: «осталось три вкладки» это число, «осталось три
вкладки с формой отклика» это баг-репорт.

API сессии:

| метод | что делает |
|---|---|
| `register_tab(target_id, purpose, url)` | регистрирует владение |
| `ownership_registry()` | отдаёт реестр словарями (для отчёта и теста) |
| `ensure_work_tab(url, purpose)` | **одна** рабочая вкладка, создаётся один раз |
| `repurpose_work_tab(purpose, url)` | меняет назначение той же вкладки |
| `open_extra_tab(url, purpose)` | отдельная вкладка — только осознанно |
| `close_all_owned_tabs()` | закрывает **только свои**; неудачные id **остаются** в реестре |
| `foreign_tabs()` / `leftover_tabs()` | сообщает о чужих, **не закрывает** их |

**Одна вкладка на всю работу.** Реальный маршрут `поиск → вакансия → форма → следующая
вакансия` идёт через `ensure_work_tab()` + `repurpose_work_tab()`. Подтверждено живьём:
десять вакансий подряд — `distinct_tabs_used: 1` (§C).

`close_all_owned_tabs()` возвращает `{"closed", "failed", "closed_count", "failed_count"}`.
Неудачно закрытая вкладка **остаётся** в реестре, поэтому «0 вкладок осталось» — это
измерение, а не пожелание: если бы закрытие не сработало, счётчик был бы не 0.

`close_all_tabs()` оставлен как делегат — старые вызовы закрывают только свои.

---

## §B. EXCEPTION TESTS — уборка при любом исходе

Гарантия — контекстный менеджер `managed_headless_session` (`hh_browser_provider.py:1578`):

```python
try:
    start = session.start()
    yield session, start
finally:
    report = cleanup_report(session)     # stop() → port → profile lock → процессы
    session.cleanup = report
    on_cleanup(report)
```

`finally` не различает исходов — он один и тот же для PASS, BLOCKED, отказа авторизации,
ошибки разбора, таймаута, исключения, закрытой вакансии, гео-отказа и ошибки в строке перед
отправкой. Если падает сам отчёт (`cleanup_report`), ошибка **не подменяет** исход: она
записывается в `session.cleanup` как `{"all_clear": False, "error": ...}`. Если падает
`on_cleanup`, это тоже не ломает прогон — репортёр не имеет права стать причиной падения.

`cleanup_report` (`hh_browser_provider.py:1551`) возвращает **измеренные** пять условий, а не
выведенные из «`stop()` вернул»:

| поле | смысл |
|---|---|
| `owned_tabs_left` | сколько своих вкладок ещё живо |
| `headless_processes` | сколько процессов держат **точный** профиль |
| `port_free` | порт 9111 свободен |
| `profile_lock_free` | замок профиля свободен |
| `all_clear` | все четыре выше вместе |

### Четыре чекпоинта

Авария **вызвана намеренно** (`WorkflowAbort`), в четырёх местах реального маршрута. Кнопка
отправки **измерена, но не нажата**: `mouse_events_dispatched = 0` во всех прогонах.

| чекпоинт | шаги до аварии | OWNED_TABS_LEFT | JOBSEARCH_HEADLESS_PROCESSES | PORT_9111 | PROFILE_LOCK | POST_SENT |
|---|---|---|---|---|---|---|
| `after_search` | search | **0** | **0** | **FREE** | **FREE** | **0** |
| `after_vacancy_read` | search, vacancy | **0** | **0** | **FREE** | **FREE** | **0** |
| `after_form_open` | search, vacancy, form | **0** | **0** | **FREE** | **FREE** | **0** |
| `before_submit` | search, vacancy, form, «строка перед кликом» | **0** | **0** | **FREE** | **FREE** | **0** |

Во всех четырёх `cleanup.all_clear = true`, `profile.unlocked = true`,
`profile.profile_holder_pids = []`, `mouse_events_dispatched = 0`.
Улики: `tools/.t723/cleanup_after_search.json`, `cleanup_after_vacancy_read.json`,
`cleanup_after_form_open.json`, `cleanup_before_submit.json` (28.09 17:12–17:14 ICT).

### Негативный контроль — иначе PASS ничего не значит

Тест на уборку, который ни разу не видел утечки, — это тест, который не умеет падать. Поэтому
тот же маршрут прогнан **без** `managed_headless_session` (то есть без `finally`), с той же
аварией перед отправкой:

| измерение | с предохранителем | без предохранителя |
|---|---|---|
| `owned_tabs_left` | 0 | **1** (`purpose: "form"`, url формы отклика) |
| `headless_processes` | 0 | **16** |
| `port_9111_listening` | false | **true** |
| `mouse_events_dispatched` | 0 | 0 |
| `post_sent` | 0 | 0 |

Улика: `tools/.t723/noguard_before_submit.json`. Утечка **реальна**, и именно `finally`
делает четыре чекпоинта чистыми. После наблюдения мусор убран руками — эксперимент ничего не
оставил (`headless_processes_after_manual_cleanup: 0`, `port_9111_free_after: true`).

---

## §C. TAB LEAK TEST — десять вакансий, одна вкладка

Прочитаны **десять живых вакансий подряд**, без отправки:

`137341877`, `136987052`, `137651780`, `137651781`, `137851362`, `136958672`,
`137574152`, `137852162`, `136034910`, `137656446`.

| измерение | значение |
|---|---|
| `targets_before` | 2 |
| `targets_peak` | **2** |
| `targets_after` | **0** |
| `distinct_tabs_used` | **1** (`7902BA85EB37A7E0B5F6B5FC762A6E5B`) |
| `owned_targets_after` | **0** |
| `headless_processes_after` | **0** |
| `port_9111_free` | **true** |
| `mouse_events_dispatched` | 0 |
| `post_sent` | 0 |

`targets_peak = targets_before = 2` — то есть за десять вакансий не появилось **ни одной**
новой вкладки. Это не «10 вакансий → 10 висящих вкладок»: одна рабочая вкладка
переназначалась (`search → vacancy → vacancy → …`).

Улика: `tools/.t723/leak_10.json` (28.09 17:18 ICT), `verdict: PASS`.

---

## §D. STALE INSTANCE RECOVERY — зависший инстанс

Смоделирован настоящий сценарий: раннер убит на середине, а наш Chrome остался жить. Порядок
восстановления — и порядок здесь и есть свойство безопасности
(`stale_instance_recovery`, `hh_browser_provider.py:1446`):

1. **Доказать.** Процесс относится к выделенному профилю **точно** по аргументу
   `--user-data-dir`, разобранному из командной строки (CIM). Совпадение — по нормализованному
   пути, не по подстроке.
2. **Попросить.** Штатное закрытие через CDP (`quit_browser`) — пробуется первым даже тогда,
   когда выглядит безнадёжно.
3. **Только потом.** Завершить **только** доказанные pid'ы. Процесс, который держит наш
   профиль, но **не** headless, **не** убивается: это может быть окно с видимой поверхностью, и
   закрыть чужое окно хуже, чем оставить зависший инстанс человеку. Он попадает в `refused`.

### Прогон 1 — штатный путь (CDP)

| поле | значение |
|---|---|
| `holders_count` (доказано точным `--user-data-dir`) | 12 |
| `holders_all_headless` | true |
| `recovery.cdp_shutdown_attempted` | **true** |
| `recovery.status` | **`RECOVERED_BY_CDP`** |
| `recovery.terminated` | **`[]`** |
| `recovery.refused` | `[]` |
| `recovery.holders_after` | `[]` |
| `processes_after` | **0** |
| `port_9111_free` | **true** |
| `foreign_chrome_touched` | **false** |

Улика: `tools/.t723/stale.json` (28.09 17:20 ICT), `verdict: PASS`.

### Прогон 2 — шаг 3 принудительно (CDP недоступен)

Штатный путь был сделан невозможным (восстановление направлено на мёртвый порт 9112), чтобы
шаг 3 не остался доказанным **только фейком**:

| поле | значение |
|---|---|
| `cdp_url` | `http://127.0.0.1:9112` (мёртвый) |
| `recovery.cdp_shutdown_attempted` | **false** |
| `recovery.status` | **`RECOVERED_BY_TERMINATION`** |
| `recovery.terminated` | **12 pid'ов** — и все доказанные, все headless |
| `recovery.refused` | `[]` |
| `recovery.holders_after` | `[]` |
| `processes_after` | **0** |
| `port_9111_free` | **true** |
| `foreign_chrome_touched` | **false** |

Улика: `tools/.t723/stale_forced.json` (28.09 17:21 ICT), `verdict: PASS`.

### Чего раннер НЕ делает

- **`taskkill /IM chrome.exe` не вызывается.** Единственный примитив завершения —
  `terminate_pid(pid)`, и он открывает handle на **один названный pid** через `OpenProcess` и
  вызывает `TerminateProcess`. Проверяется тестом по AST: ищем **вызовы** `subprocess` с
  аргументом-строкой, содержащей `taskkill`/`pkill`/`killall`/`/im `, — их нет; и считаем
  **вызовы** `TerminateProcess` — ровно один, внутри `terminate_pid`.
- **Чужие окна не трогаются.** `FOREIGN_PROFILE_MARKERS = ("browserclaw", "browseros",
  "chrome-cdp-profile")` — отказ в любую сторону. `_normalise_path` — точное равенство, поэтому
  профиль, который лишь **содержит** наш путь, не совпадает.
- **Живость процесса** проверяется не `os.kill(pid, 0)` (на Windows это `TerminateProcess`, то
  есть проверка, которая убивает), а `OpenProcess` + `GetExitCodeProcess` (259 = STILL_ACTIVE).

### Почему первое измерение `stale` врало — и как починено

Первый прогон дал `verdict: FIX` при `foreign_chrome_touched: true`, хотя `terminated: []`:
восстановление отработало правильно, а **измерение** было неверным. Оно сравнивало
pid-множества **всех** `chrome.exe` до и после. У чужих окон renderer-, GPU- и utility-процессы
рождаются и умирают сами по себе: за прогон их число прошло `44 → 33`, и обычная суета
превратилась в «мы кого-то тронули».

Починка — сравнивать **стабильных свидетелей**: корневые процессы браузеров (без `--type=`),
которые живут ровно столько, сколько живёт их окно, и сверять не только pid, но и
`CreationDate` (защита от переиспользования pid). `CIM`-запрос теперь тянет `CreationDate`.

| измерение | до | после |
|---|---|---|
| что сравнивалось | все `chrome.exe` | корневые процессы чужих браузеров |
| `foreign_roots_before` / `after` | — | 2 / 2 |
| `foreign_roots_missing` | — | `[]` |
| `foreign_chrome_touched` | **true** (ложно) | **false** |
| `verdict` | `FIX` | **`PASS`** |

Чужие корни в прогоне — оба на месте: Chrome Миши (`chrome.exe --no-startup-window
/prefetch:5`, pid 12912) и BrowserOS (`...\BrowserClaw\Application\chrome.exe
--profile-directory="Profile 1"`, pid 7608). Оба выжили.

### Попутный дефект: грязное значение улики

Командная строка Chrome встречается в двух стилях кавычек:
`--user-data-dir="C:\...\User Data"` и `"--user-data-dir=C:\...\User Data"`. Во втором ленивая
ветка разбора тянулась до следующего ` --` и **забирала закрывающую кавычку**: значение улики
приходило как `C:\...\User Data"`. На совпадение это не влияло (`_normalise_path` кавычки
срезает), но значение — это и есть доказательство «процесс наш», и читаться оно должно чисто.
Починено в `user_data_dir_from_command_line`, покрыто тестом на третий стиль кавычек.

---

### Живой инстанс в природе — найден 29.09 11:26, НЕ тронут

Перед сдачей задачи я перепроверил, что за собой ничего не оставил, и **не** получил ноль: на
порту 9111 слушает живой headless Chrome, 14 процессов держат выделенный профиль. Разбор:

| факт | значение |
|---|---|
| корневой pid | 26152 |
| запущен | **2026-09-29 11:26:24 ICT** (`--headless=new`, порт 9111, наш профиль) |
| launcher (pid 15180) | **мёртв** — браузер пережил того, кто его запустил |
| что открыто | `https://hh.ru/applicant/profile/me` |
| рядом работает | `python -m ai_assistant.cli ui --host 0.0.0.0 --port 8000` (pid 11084) |
| чей | **не мой** |

Это **не след моей работы**: все прогоны T7.23 закончились 28.09 в 17:21, полный прогон тестов — в
18:01, и с тех пор я не запускал ничего. Корневой процесс создан **на 17 часов позже**.

**Это ровно тот случай, под который построен §D:** процесс держит **точный** выделенный профиль,
headless, а его launcher'а уже нет. `stale_instance_recovery()` его бы вычистил. Я этого **не
делал**: рядом поднят веб-UI проекта, и отличить «зависший инстанс» от «сессия, которой прямо
сейчас пользуются» извне нельзя. Уборка здесь — решение Миши, а не моё.

Что это меняет в отчёте: `HEADLESS PROCESSES AFTER: 0` — измерение **на момент окончания прогонов
T7.23** (28.09 17:21, улики `tools/.t723/*.json`), и оно верно. Сегодняшний инстанс — чужой и
принадлежит другому событию.

---

## §E. STRUCTURED PREFLIGHT — что читается до решения

`ai_assistant/hh_structured_preflight.py` (547 строк) — чистая логика, без браузера и без I/O,
поэтому весь шлагбаум проверяется офлайн.

`STRUCTURED_JS` читает `<template id="HH-Lux-InitialState">` → `JSON.parse` и возвращает три
блока: `short_vacancy` (из `applicantVacancyResponseStatuses[vid].shortVacancy`),
`vacancy` (из `vacancyView.vacancyFull.vacancy`) и `applicant`.

**Обязательные поля** (`REQUIRED_STRUCTURED_FIELDS`):

`workExperience`, `civilLawContracts`, `acceptLaborContract`, `workFormats`, `workSchedule`,
`employment`, `compensation`, `area`, `test`, `vacancyTests`, `responseAllowed`,
`responseImpossible`.

`missing_structured_fields()` говорит, каких **не было в полезной нагрузке**; `None` означает
«ключ отсутствовал» и **никогда** не используется для вывода «у вакансии такого условия нет» —
это разные утверждения.

### Два запрета на вывод из молчания

| запрещено | почему |
|---|---|
| «оформление не указано», пока не проверены **и** `acceptLaborContract`, **и** `civilLawContracts` | `acceptLaborContract: false` значит «ТК РФ не предлагают», **не** «про оформление не сказано». Это ровно дефект T7.22 |
| «опыт не указан», пока не проверен `workExperience` | в T7.22 стояло «в SSR не заполнено», а там было `between1And3` |

`read_employment()` читает **оба** источника: `acceptLaborContract: true` → ТК РФ;
`civilLawContracts[].civilLawContractsElement` → словарём
`INDIVIDUAL_ENTREPRENEUR → GPH_IP («Договор ГПХ с ИП»)`,
`SELF_EMPLOYED → GPH_SELF_EMPLOYED`. Незнакомое значение **всё равно считается указанным**
(`GPH_OTHER`), потому что незнакомое — это не отсутствующее.

`read_experience()` переводит `noExperience` / `between1And3` / `between3And6` / `moreThan6`
в «нет опыта» / «1–3 года» / «3–6 лет» / «более 6 лет».

`structured_preflight()` собирает вердикт `CLEAR` / `USER_DECISION_REQUIRED` / `BLOCKED`,
разделяя `blockers` («не отправлять, не подходит») и `user_decision_required` («не отправлять,
сначала человек») — им нужны разные слова в отчёте и разная обработка в очереди.
`auto_send_permitted` истинно **только** при `CLEAR`.

### Проверка на реальной полезной нагрузке

Записанный живой payload T7.22 прогнан через шлагбаум
(`tools/.t723_annotate_dream_team.py`, только чтение):

| поле | значение |
|---|---|
| `read_complete` | **true** |
| `fields_missing` | `[]` — прочитано **12 из 12** |
| Оформление | `Договор ГПХ с ИП` |
| Опыт | `1–3 года` (`workExperience = between1And3`) |
| Вердикт | **`USER_DECISION_REQUIRED`** |
| `auto_send_permitted` | **false** |

**Ретроспективный вывод:** новый шлагбаум **не дал бы** T7.22 отправить отклик автоматически.
Оба открытых вопроса — про форму занятости и про сравнение валюты — он называет вслух.

---

## §F. EMPLOYMENT GATE — форма занятости

`employment_gate(reading, thailand_settled=False)`:

| состояние | решение | код |
|---|---|---|
| форма названа (ТК РФ / ГПХ с ИП / ГПХ с самозанятым / ГПХ иной) и `thailand_settled=False` | `USER_DECISION_REQUIRED` | `EMPLOYMENT_NEEDS_THAILAND_ANSWER` |
| форма названа и `thailand_settled=True` | `CLEAR` | `EMPLOYMENT_SETTLED` |
| форма не названа нигде | `CLEAR` | `EMPLOYMENT_NOT_STATED` |

Логика: работодатель, назвавший форму оформления, обычно предполагает **резидента РФ**. Пока
это не подтверждено письменно, названная форма — открытый вопрос, а не молчаливый пропуск.
`thailand_settled` становится `True` только после письменного ответа работодателя, а не после
нашего предположения.

**Dream Team:** `Оформление: Договор ГПХ с ИП` → `USER_DECISION_REQUIRED`. Ничего не
отменяется, факт сохраняется как есть (§H). Если работодатель ответит — вопрос «возможна ли
работа из Таиланда **при такой форме**» разбирается отдельно.

---

## §G. SALARY GATE — минимальный доход

`salary_gate(snapshot, profile)` возвращает явную проверку минимума. Настроенный минимум:
`ai_assistant/candidate_profile.json → minimum_salary: 1000, salary_currency: "USD"`.

| код | решение | когда |
|---|---|---|
| `SALARY_NO_REQUIREMENT` | `CLEAR` | минимум в профиле не задан — сравнивать не с чем |
| `SALARY_UNKNOWN` | `CLEAR` | вакансия суммы не назвала. **Неизвестно ≠ ниже минимума** |
| `CURRENT_FX_REQUIRED` | `USER_DECISION_REQUIRED` | валюты разные — ответ зависит от курса на сегодня, а курс не угадывается |
| `SALARY_BELOW_MINIMUM` | **`BLOCKED`** | сумма **явно ниже** минимума → автоотправки нет |
| `GROSS_NET_BASIS_UNVERIFIED` | `USER_DECISION_REQUIRED` | вакансия gross, а базис минимума не объявлен |
| `SALARY_OK` | `CLEAR` | одна валюта, не ниже, базы сопоставимы |

Правила, зашитые намеренно:

- **Аргумент «мы уже отправляли на меньшую зарплату» не используется.** Он не имеет отношения к
  настроенному минимуму.
- **Gross и net не смешиваются.** `RUR` нормализуется в `RUB`; `gross` и базис минимума
  сверяются. У `CandidateProfile` **нет** атрибута `salary_gross` (сегодня `None`), поэтому
  gross-вакансия с необъявленным базисом минимума честно уходит в `USER_DECISION_REQUIRED`, а
  не «проходит».
- **Разные валюты не конвертируются молча** — ни по какому курсу.

**Dream Team:** `85000 RUR` против минимума `1000 USD` → `CURRENT_FX_REQUIRED`,
`auto_send_permitted: false`. Оба гейта — employment и salary — независимо запрещают автоотправку.

---

## §H. DREAM TEAM — аннотация, без второй отправки

Отклик `hh:137149827` **не переотправлялся** и не отменялся. Запись `SUBMITTED` не менялась,
новая запись не создавалась: аннотация читает БД и ничего в неё не пишет
(`read_submission_fact()`, `changed_by_this_task: false`).

История в БД: `DISCOVERED → ANALYZED → READY_TO_APPLY → SUBMITTED`.

Человекочитаемая аннотация — `artifacts/applications/hh-137149827/preflight.md`
(+ машинная `preflight.json`), рядом с `vacancy.md` и письмом. Сохранены пять фактов:

| факт | значение | источник |
|---|---|---|
| ОФОРМЛЕНИЕ | **`Договор ГПХ с ИП`** | `shortVacancy.civilLawContracts = ["INDIVIDUAL_ENTREPRENEUR"]` |
| ОПЫТ | **`between1And3`** (1–3 года) | `shortVacancy.workExperience` |
| ЗАРПЛАТА | **до 85 000 ₽ gross** | `shortVacancy.compensation` (`to: 85000`, `currencyCode: "RUR"`, `gross: true`) |
| ТАИЛАНД | **`UNKNOWN`** | работодатель о локации кандидата не говорит ничего |
| ВОПРОС ПРО ИП ЗАДАН ДО ОТПРАВКИ? | **НЕТ** | текст отправленного письма |

Последний факт **доказывается**, а не вспоминается: `letter_asked_about_ip()` ищет в тексте
письма маркеры `ип`, `гпх`, `самозанят…`, `оформлени…`, `трудовой договор` — по **границам
слов**, иначе двухбуквенное `ип` сработало бы внутри «тип», «принцип», «архитип». Маркеров не
найдено: письмо спрашивает про Таиланд и про выезды в Москву, про форму оформления — нет.

Форма занятости **не блокер** уже отправленного отклика: удалёнка подтверждена, тестов и
вопросов нет, отклик был разрешён, ГПХ — обычная форма работы с удалёнщиком и **не** означает
«жить в РФ». Но это материальное условие, и если работодатель ответит «удалённо можно, но нужен
ИП» — это отдельный раунд, разбирать его надо отдельно, не отменяя уже отправленное.

---

## §I. TESTS

| файл | тестов | о чём |
|---|---|---|
| `tests/test_t723_structured_preflight.py` | 36 | §6–§8: чтение структуры и оба гейта |
| `tests/test_t723_cleanup.py` | 33 | §1–§5: уборка, экономия вкладок, восстановление |
| `tests/test_t721_headless_provider.py` | 55 | провайдер целиком (T7.21 + правки T7.23) |

Покрыто по списку задачи:

- `civilLawContracts = INDIVIDUAL_ENTREPRENEUR` → `GPH_IP` / «Договор ГПХ с ИП»;
- `acceptLaborContract = false` **не** значит «оформление не указано» (регрессия T7.22 отдельным
  тестом);
- `workExperience = between1And3` → «1–3 года»;
- «оформление неизвестно» — **только** когда пусты **все** структурные источники;
- незнакомое значение формы **всё равно** считается указанным;
- зарплата ниже минимума **блокирует** автоотправку;
- gross/net сохраняются и не смешиваются; разные валюты → отказ от конвертации;
- уборка при исключении — параметризованно по **четырём** чекпоинтам, для путей «raise» и «pass»;
- негативный контроль: **без** предохранителя вкладки остаются (`test_the_guard_is_what_closes_the_tabs`);
- утечка вкладок на десяти последовательных вакансиях (§C, живьём);
- восстановление зависшего инстанса: сначала CDP, потом завершение; отказ при не-headless;
- чужой Chrome не убивается никогда: `test_browseros_and_other_profiles_are_never_matched`,
  `test_a_profile_that_merely_contains_ours_does_not_match`,
  `test_foreign_chrome_processes_are_never_killed`, AST-тест на отсутствие команды завершения по
  имени образа и AST-тест «завершение идёт только через `terminate_pid`»;
- разбор `--user-data-dir` при трёх стилях кавычек и пробеле в пути.

Линт: `uvx ruff@0.16.6` — по новым файлам **All checks passed**.

### Полный прогон

`pytest -q --junitxml=.pytest_t723.xml` → **2787 collected, 1 failed, 0 errors, 0 skipped**
(1170.2 с). Падение **одно** и оно **пре-существующее** — ратчет
`test_report_line_refs::test_the_report_has_no_new_symbol_mismatches` (24 при пороге 18; читает
**только** `docs/ble001_triage.md`, к T7.23 отношения не имеет). **Порог не поднимался.**

Против T7.22 (2718 collected): **+69** тестов — `test_t723_cleanup.py` 33,
`test_t723_structured_preflight.py` 36. `test_t721_headless_provider.py` остался 55.

**Про `$?`:** вердикт прочитан **из junit**, не из кода возврата. Код возврата поднимает уборка
tmp-каталога pytest, которую блокирует охранник песочницы
(`[SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":327,"threshold":50,"scope":"turn"}`), — это к
тестам не относится.

### Одно падение, которое чуть не списали в регрессию

Первый полный прогон дал **2** падения: ратчет и
`test_t721_headless_provider.py::test_stop_closes_tabs_then_the_browser_and_waits_for_the_port`
(`STOP_TIMEOUT` вместо `STOPPED`). **Это не регрессия кода.** `stop()` теперь меряет реальный мир
(`verify_profile_unlocked` → `dedicated_profile_processes`), а прогон suite шёл **одновременно** с
живыми прогонами `stale` (17:20 и 17:21 ICT), которые держали выделенный профиль, — и `stop()`
честно сказал «профиль занят». Лечение: тест пинит `dedicated_profile_processes → []`
(герметичность). **Правило: живые headless-прогоны не запускать параллельно с pytest.**

---

## §12. ИТОГОВАЯ ТАБЛИЦА

| проверка | значение |
|---|---|
| OWNED TABS AFTER | **0** |
| HEADLESS PROCESSES AFTER | **0** — на конец прогонов T7.23 (28.09 17:21). 29.09 11:26 появился **чужой** живой инстанс; не тронут, разбор в §D |
| PORT 9111 AFTER | **FREE** (на конец прогонов; занят чужим инстансом с 29.09 11:26) |
| PROFILE LOCK AFTER | **FREE** |
| EXCEPTION CLEANUP | **PASS** (4/4 чекпоинта) |
| TAB LEAK TEST | **PASS** (10 вакансий, 1 вкладка) |
| FOREIGN WINDOWS TOUCHED | **NO** |
| STRUCTURED EMPLOYMENT READ | **PASS** |
| STRUCTURED EXPERIENCE READ | **PASS** |
| SALARY GATE | **PASS** |
| APPLICATIONS SENT | **0** |
| VERDICT | **PASS** |

Регрессия: **2787 collected, 1 failed, 0 errors** — падение одно и оно **пре-существующее**
(ратчет `test_report_line_refs`), **+69** тестов против T7.22.

---

## Что не тронуто

- Отклик `hh:137149827` — `SUBMITTED` как был; ни переотправки, ни новой записи, ни отмены.
- Ни одного сообщения работодателю, ни одного напоминания, письма EvaTeam, `FULL`, `git push`.
- Чужие окна: Chrome Миши и BrowserOS живы, ни один чужой процесс не завершён.
- **Чужой живой headless-инстанс на нашем профиле (29.09 11:26, корневой pid 26152, launcher мёртв)
  не тронут** — рядом поднят веб-UI проекта, и отличить зависший инстанс от сессии в работе извне
  нельзя. Разбор — §D.
- `docs/ble001_triage.md` — не менялся (ратчет `test_report_line_refs` не поднимался).
