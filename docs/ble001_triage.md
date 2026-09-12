# BLE001 triage — 369 голых `except Exception`

Дата: 2026-09-08. Цель: понять, где `except Exception` реально маскирует ошибку,
а где это оправданный верхнеуровневый предохранитель. **Автофикса не было** —
только оценка и план.

> **Статус 2026-09-09:** все шаги 0-3 плана выполнены.
>
> - Шаги 0-2: создан `ruff.toml`, закрыты находки №1-6, добавлены регрессионные
>   тесты `tests/test_ble001_fail_closed.py` (8 штук). Полный сьют:
>   **1531 passed** (было 1523). Backlog ruff: **710 → 708** (S110 −2,
>   BLE001 −2; LOG015 вернулся к 42 после перевода новых логов с root-логгера
>   на модульный). BLE001 остался 367 — правило срабатывает на сам
>   `except Exception`, а меняли не его, а поведение тела.
> - Шаг 3: политика зон зафиксирована в `ruff.toml` (`[lint.per-file-ignores]`)
>   и в тесте-гарде `tests/test_ble001_zone_policy.py` (15 тестов).
>   Backlog ruff: **708 → 599** — это BLE001, снятый с зелёной зоны.
>   **S110 не игнорируется нигде**: пережить плохую итерацию можно, проглотить
>   её молча — нет.
>
> - Находка №7 (`browser_executor.py`) проверена и закрыта: guarding на
>   `observed_control_count` не существовало (ключ писался и не читался),
>   упавшая экстракция проходила гейты как «чистая пустая форма». Снапшот
>   получил явный `error` / `error_reason`, флаг доведён до review gate.
>   Тестов в `tests/test_ble001_fail_closed.py` теперь 15.
>
> - Находки №8 и №9 закрыты. №8: kill-switch `SUBMIT_ALLOWED` не выключался из
>   окружения — `load_dotenv(override=True)` затирал переменную значением из
>   `.env` до того, как гейт её читал. №9 (найдена попутно): **не-HH источники
>   обходили вообще все 11 гейтов** и физически кликали Submit по унаследованной
>   ветке. Полный сьют после правок: **зелёный**, оба упавших теста прошли.
>   Тестов в файле теперь 20.
>
> - Находка №10 закрыта: `PlaywrightBrowserAdapter.open()` глотал исключение
>   `connect_over_cdp` и молча уходил в headless-launch, то есть экстракция
>   шла в браузере, который hh.ru палит мгновенно, а отправка — в настоящем.
>   Тестов в файле теперь 35.
>
> - **Проверка «а работает ли оно на самом деле» (2026-09-09).** Зелёные тесты
>   приняты не были: написан `tools/e2e_pipeline_probe.py`, который гонит
>   настоящий пайплайн по настоящему браузеру (без кликов и отправок).
>   Вскрылись находки №11-13, все закрыты:
>     - №11: `blocked=True` **на каждой странице hh.ru** — подстрока `captcha`
>       находится в i18n-бандле (`error.signup.captcha.invalid`). Ловушка уже
>       была описана в `hh_extractor._detect_blocked`, но `open()` её
>       переизобрёл.
>     - №12: `CDPBrowserAdapter` (путь отправки!) всё ещё ходил в CDP через
>       `urlopen`, который honour'ит `http_proxy`.
>     - №13: резолвер трактовал таймаут как «браузер мёртв» и прыгал в другой
>       профиль. Ужесточение, а не наблюдавшийся баг: 9110 выбирался потому,
>       что `.env` его и прибивает (`HH_CDP_URL`), — резолвер отработал верно.
>   Тестов в файле теперь 42. Регрессии проверены мутациями (см. ниже).
>
> - Находка №12 доведена до конца: sweep `urlopen` по пакету нашёл ещё 5
>   CDP-вызовов через прокси (`hh_vacancy_navigator` — навигация на вакансию,
>   `prefill_execute` — префилл формы). Плюс статический гард-тест на весь
>   класс. Тестов в файле теперь 43.
>
> - Находка №14 закрыта (2026-09-10): `cli.py` отвечал на вопрос «каким
>   браузером едем» сам — модульной константой, замороженной на импорте. Это
>   вторая, ослабленная копия `resolve_cdp_url()`. Замерено: с заданным
>   `CDP_URL` CLI ехал на 9110, а адаптеры — на прибитый порт. Два браузера
>   это два профиля Chrome, и путь отправки вёл тот, где нет сессии hh.ru.
>   Тестов в файле теперь 48.
>
> - **Снято собственное неверное наблюдение про `apply_link`.** Селектор цел:
>   я смотрел на вакансию, по которой отклик уже отправлен, а там hh.ru
>   меняет «Откликнуться» на «Чат». На свежей вакансии кнопка находится.
>   E2E-проба теперь тоже отбрасывает уже отвеченные — и сразу вместо 0
>   вопросов стала извлекать 6.
>
> - Находка №15 закрыта: `check_live_page()` умеет сверять заголовок страницы
>   с ожидаемым, на это был зелёный тест, но единственный продакшн-вызов не
>   передавал `expected_title`, и шаг просто пропускался. Проверка была,
>   защиты не было. Повод — три ссылки в `vacancies.json`, которые ведут на
>   совсем другие вакансии (AI-роль → «Продавец (Чебоксары)»); сейчас их
>   спасает только то, что все три в архиве. Тестов в файле теперь 51.

## Метод

Прогнали `ruff check ai_assistant/ --select BLE001` (369 находок), затем для
каждого обработчика прочитали тело и задали один вопрос:

> Сохраняет ли обработчик хоть какой-то след ошибки — лог, `raise`, или имя
> исключения (`as e`), попавшее в отчёт/словарь/возвращаемое значение?

Если нет — ошибка выброшена, и только там может прятаться настоящая дыра.
(Первая версия эвристики считала «молчаливыми» все `except Exception as e:
report.reason = f"...{e}"` — это была ошибка, текст там сохраняется. Версия 2
это исправила.)

## Итоговые цифры

| Класс | Шт. | % | Что делать |
|---|---:|---:|---|
| **A** — ошибка сохранена (лог / `raise` / `e` попал в отчёт) | 166 | 45% | не трогать |
| **B** — ошибка выброшена | 203 | 55% | разбирать |
| из B: тело — только `pass` / `continue` | 80 | | подозрительно |
| из B: голое `except Exception:` (объект даже не привязан) | 198 | | имя ошибки потеряно навсегда |
| из B: `as e`, но `e` не используется | 5 | | просто мусор |
| из B: подменяет ошибку фабрикованным дефолтом | 30 | | **главный источник дыр** |
| из B: на критичном пути (submit/apply/send/gate/verify) | 11 | | **приоритет** |

По файлам: `cli.py` 67, `browser_executor.py` 59, `db.py` 29, `hh_submission.py` 16,
`watcher.py` 13, `hh_autonomous_agent.py` 12, `hh_message_watcher.py` 11,
`hh_message_reply.py` 10, `runner.py` 10, `hh_browser_launcher.py` 9,
`submission_recovery.py` 9, `application_queue.py` 8, `application_review.py` 8.

---

## Настоящие дыры

### 1. `browser_executor.py:683` — фабрикует «форма найдена» (высокий)

`CDPBrowserAdapter.inspect_page` при любом исключении возвращает:

```python
return {"form_detected": True, "fields": [..., "linkedin"], "apply_button": True}
```

То есть **признаётся в успехе на странице, которую не смог прочитать**.
Хуже того: в словаре нет ключей `captcha` и `login_required`. А вызывающие
(`browser_executor.py:1943-1976`, `2613-2637`, `2871-2872`) читают ровно эти ключи:

```python
form_detected = bool(inspect.get("form_detected") or flow_info.get("has_form"))
...
elif inspect.get("captcha") or flow_info.get("captcha"):   # -> None, falsy
elif login_req and auth_state == "NOT_AUTHENTICATED":       # -> None, falsy
elif inspect.get("login_required"):                         # -> None, falsy
elif not form_detected:                                     # -> True, пропуск
else:
    status = BrowserStatus.FORM_DETECTED                    # <- сюда и попадаем
```

Итог: упавший CDP-осмотр проходит все проверки блокировки и сообщает
`FORM_DETECTED` / пропускает «Submit button not found». Классический fail-open.
`CDPBrowserAdapter` живой — инстанцируется в `browser_executor.py:2284`, `2845`
и `cli.py:794`.

Зеркальный случай `PlaywrightBrowserAdapter.inspect_page:1130` делает всё
правильно — возвращает `form_detected: False` и честно блокируется.

### 2. `hh_application_queue.py:251` + `hh_application_runner.py:107` — двухслойный fail-open (высокий)

```python
# hh_application_queue.py:251
except Exception:
    audit_state = "SAFE_TO_SUBMIT" if q_state in (READY_TO_SUBMIT, SUBMITTED) else "NEEDS_CORRECTION"

# hh_application_runner.py:107
except Exception:
    audit_status = RunnerPreCheckStatus.PASS if target_app.audit_state == "SAFE_TO_SUBMIT" else FAIL
```

Первый слой: если аудит анкеты упал — пишем `SAFE_TO_SUBMIT` на основании
*сохранённого* состояния. Второй слой: если живой аудит упал — берём
*сохранённый* `audit_state` и превращаем его в `PASS`.

То есть **значение, которое первый слой сфантазировал при своей ошибке, второй
слой читает как истину и превращает в PASS**. Упавший аудит отмывается в
разрешение на отправку. По отдельности каждый слой выглядит как «ну, деграднул
аккуратно», вместе — дыра.

### 3. `db.py:2049` — reconcile молча стирает список вакансий (высокий)

`reconcile_digest_attempt`:

```python
try:
    p = json.loads(row[0])
    vac_list = p.get("vacancies", [])
except Exception:
    pass          # vac_list остаётся []
...
updated_payload = json.dumps({"vacancies": vac_list, "reconciled_to": new_status, ...})
cur.execute("UPDATE ... SET status = ?, delivered_at = ?, payload = ?", (new_status, ..., updated_payload, ...))
# вакансии из vac_list не обновляются — список пуст, цикл не выполняется
return True
```

Если payload не распарсился: батч помечается новым статусом (в т.ч. доставленным),
**payload перезаписывается пустым списком** — исходный список вакансий потерян
безвозвратно, ни одна запись вакансии не обновлена, функция возвращает `True`.
Тихая потеря данных плюс ложный успех.

### 4. `db.py:2173` — health-check врёт, когда сам сломан (средне-высокий)

```python
try:
    cur.execute("SELECT delivery_key, COUNT(*) ... HAVING COUNT(*) > 1")
    dups = cur.fetchall()
    if dups:
        health_result["health"] = "UNHEALTHY"
        ... CRITICAL alert "Duplicate delivery keys detected"
except Exception:
    pass
```

Проверка на дубликаты delivery_key — единственная в своём роде. Если запрос
падает, `health` остаётся `HEALTHY` и алерта нет. **Метрика здоровья, которая
показывает «здоров» именно когда не смогла проверить.** Соседние проверки
(`2221`, `2228`, `2308`) грешат тем же, но их последствия мягче.

### 5. `application_queue.py:1142` — опечатка в фильтре превращается в правдоподобный ответ (средний)

```python
try:
    filt = ApplicationStatus(status_filter)
except Exception:
    filt = ApplicationStatus.READY_TO_APPLY
```

`prepare --status=GARBAGE` молча готовит READY_TO_APPLY вместо ошибки.
Ничего не ломает, но выдаёт plausible-looking результат вместо
«нет такого статуса». Ровно тот класс багов, который трудно заметить.

### 6. `application_tracking.py:483` — отклонённый переход статуса теряется (средне-низкий)

При неудачном `transition_application` деградирует до «обновить только оценки,
статус не менять». Как деградация — разумно. Но туда же падают и *невалидные*
переходы, т.е. ошибка в логике переходов будет молча игнорироваться вечно,
без единого следа в логах.

### 7. `browser_executor.py:1276` — пустой снапшот выглядит как чистая форма — **ИСПРАВЛЕНО 2026-09-09**

`extract_application_form` при исключении возвращал пустой снапшот
(`html: ""`, `questions: []`, `controls: []`, `auth_form: false`, `site: "hh.ru"`).
`hh_extractor.extract_application_form` на таком входе получал
`questions = []`, `blocked.captcha = false`, `app_type = unknown` —
т.е. «форма без вопросов, ничто не блокирует».

**Проверка, которую я обещал, сделана, и худшее подтвердилось.** Guarding на
`observed_control_count` не существует: ключ пишется в `hh_extractor.py:519`
и **нигде не читается** — ни в коде, ни в тестах (единственные совпадения вне
исходников: артефакт `artifacts/hh_manual_form_snapshot.json` и этот документ).
Никакой downstream по нему не ветвится.

Дальше по цепочке всё было вакуумно-истинным:

- `application_qa.py` проверяет в `extraction_meta` только `captcha`,
  `cloudflare`, `auth_form` — про «страницу не прочитали» там речи нет;
- `build_review_gate` (`application_review_gate.py`) не считает пустую форму
  причиной для блокировки;
- гейт 8 `no_unknown_questions` (`hh_submission.py:397`) на пустом множестве
  вопросов проходит trivially и рапортует `"All questions resolved"`.

То есть упавшая экстракция шла по тому же пути, что и вакансия реально без
вопросов, и все гейты её благополучно пропускали.

**Что сделано (fail-closed, по тому же шаблону, что finding #1):**

1. Снапшот получил явные поля `error: bool` и `error_reason: str | None`.
   Проставлены во всех четырёх точках:
   - `browser_executor.py:1190` — нет страницы → `error=True, "page_not_open"`;
   - `browser_executor.py:1331` — исключение → `error=True, "extraction_failed: <Type>"`;
   - `browser_executor.py:1318` — успех → `error=False`;
   - `MockBrowserAdapter` (`:461`) — проксирует `simulate`, чтобы тест мог
     симулировать и успех, и ошибку.
2. `hh_extractor.py` прокидывает флаг в `extraction_meta["error"]`.
3. `application_qa.py` добавляет `gate_reasons` при `meta["error"]`.
4. `application_review_gate.build_review_gate` блокирует — последний рубеж
   перед отправкой.

Регрессионные тесты: `tests/test_ble001_fail_closed.py`, 7 штук. Тест на гейт
содержит встречную проверку (честная пустая форма **не** блокируется), иначе
он был бы декоративным.

---

## Проверено и признано нормальным

Чтобы не раздувать список:

| Место | Почему ок |
|---|---|
| `browser_executor.py:1368` `pass  # Some submissions don't navigate` | успех определяется скан-контентом (`1377-1384`), а не фактом навигации. Съеденный таймаут ни на что не влияет. Стоит только сузить до `TimeoutError`. |
| `db.py:240` (`save_vacancy_eligibility` при `save_vacancy`) | самовосстанавливается: `application_queue.py:599-603` при отсутствующей записи переоценивает eligibility на месте. |
| `hh_submission.py:259`, `hh_human_submission.py:64` | `entry.get("gate") or {}` бросает только если `entry` вообще не dict; при пустом `gate_vacancy` вторая проверка-vs-approved просто пропускается, но **до** неё уже прошёл `gate_res` (`:250`). Теоретически fail-open, практически недостижимо. |
| `browser_executor.py:150` | `urlparse(str).netloc.lower()` на строке не бросает. Пустой `apply_domain` → не совпадёт с доменом → fail *closed*. |
| Класс A целиком (166 шт.) | текст ошибки сохранён — логи, `raise`, `f"...{e}"` в отчётах. |

---

## План

### Шаг 0. Создать `ruff.toml` — его нет вообще — **СДЕЛАНО**

Конфига действительно не было нигде: ни `ruff.toml`, ни `[tool.ruff]`, ни
`~/.config/ruff`. Проверено `--isolated` — он даёт тот же результат, то есть
710 ошибок были **дефолтом ruff 0.16.6**, а не чьим-то выбором. Это важно:
дефолт 0.16.x сильно шире классического `E4,E7,E9,F` (в нём уже есть BLE, UP,
SIM, RUF, PL, DTZ, LOG, TRY…), но в нём **нет** E501 и D. Backlog целиком
зависел от версии ruff на машине.

Создан `ruff.toml`: 413 правил выгружены из `--show-settings` и зафиксированы
явно, `target-version = "py311"`, `exclude` для `baseline_stage14_snapshot` /
`snapshot_stage15_current` / `graphify-out`. Четыре правила, которые появились
только после явной фиксации версии, осознанно отключены в `ignore` с указанием
причины: `UP017` (47), `E402` (27), `FURB162` (6), `E741` (3). После этого
`ruff check ai_assistant/` даёт ровно **710** — baseline не сдвинут.

### Шаг 1. Закрыть 4 fail-open (≈1 час, каждый правится отдельно) — **СДЕЛАНО**

1. `browser_executor.py:683` — возвращать `form_detected: False, apply_button: False`
   (как делает Playwright-вариант) + `logger.warning`. Словарь должен содержать
   `captcha`/`login_required`, чтобы `.get()` не врал.
2. `hh_application_queue.py:251` — при исключении ставить `NEEDS_CORRECTION`,
   а не `SAFE_TO_SUBMIT`, и логировать.
3. `hh_application_runner.py:107` — при исключении `RunnerPreCheckStatus.FAIL`
   (или отдельный статус `ERROR`), но только не `PASS`.
4. `db.py:2049` — при нераспарсенном payload возвращать `False` и **не**
   перезаписывать payload.

Каждое изменение — с тестом, который заставляет исключение произойти и
проверяет, что результат fail-closed.

### Шаг 2. Health-check и фильтры (≈30 мин) — **СДЕЛАНО**

Реализовано иначе, чем планировалось, в одном месте: для `db.py:2173` введён
`health = "UNKNOWN"` (а не `DEGRADED`), потому что проверка не «прошла с
замечанием», а **не смогла пройти**. `cli.py:4034` трактует всё, кроме
`HEALTHY`/`DEGRADED`, как код 2 — fail-closed сохранён.

Также закрыта находка №6 (`application_tracking.py:483`): в файле не было
логгера вообще, добавлен `logger = logging.getLogger(__name__)`.

**Регрессионные тесты:** `tests/test_ble001_fail_closed.py`, 8 тестов. Каждый
заставляет исключение произойти и проверяет fail-closed. Проверено, что тесты
не декоративные: при возврате прежнего оптимистичного варианта
`test_cdp_inspect_page_fails_closed_on_exception` падает.

5. `db.py:2173` — при падении проверки ставить `health = "UNKNOWN"` и добавлять
   алерт, а не оставлять `HEALTHY`.
6. `application_queue.py:1142` — кидать `ValueError` на неизвестный статус
   (или хотя бы логировать + возвращать пустой список).
7. `application_tracking.py:483` — логировать `logger.warning` с номером
   приложения и целевым статусом.
8. ~~`browser_executor.py:1276` — проверить guarding на `observed_control_count`,
   по результату либо добавить ключ `error: true` в снапшот, либо закрыть.~~
   **СДЕЛАНО 2026-09-09.** Guarding нет (ключ нигде не читается) → добавлен
   `error: true` в снапшот + прокинут до review gate. См. finding #7.

### Шаг 3. Политика на остальные ~190 (автофиксом **не** трогать) — **СДЕЛАНО**

Механически превращать 200 обработчиков в «залогировать и пробросить» —
плохая идея: треть из них верхнеуровневые watchdog-предохранители, где
`pass` — это корректное поведение, и шум в логах вырастет радикально.

Предлагаю вместо массовой правки ввести правило по файлам:

- **Красная зона** (BLE001 запрещён, ruff ругается): всё, что может повлиять на
  отправку или вердикт гейта — `hh_submission.py`, `hh_application_queue.py`,
  `hh_application_runner.py`, `hh_application_orchestrator.py`,
  `submission_*.py`, `prefill_execute.py`, `application_integrity.py`,
  `application_qa.py`.
- **Зелёная зона** (BLE001 в per-file-ignores): `cli.py`, `watcher.py`,
  `runner.py`, `*_watcher.py`, `hh_browser_launcher.py` — там предохранитель
  оправдан, ошибка всё равно уходит пользователю в вывод команды.

Плюс маленький хелпер для тех 80 голых `pass`, чтобы не плодить копипасту:

```python
def swallow(logger, exc: BaseException, where: str) -> None:
    """Явно помеченное «мы это гасим намеренно». BLE001 не срабатывает
    на logging.*, но нам важнее, чтобы причина была записана."""
    logger.debug("%s: %s: %s", where, type(exc).__name__, exc)
```

#### Что сделано 2026-09-09, а что — нет

**Сделано.** Политика зафиксирована в двух местах, и оба проверяются
автоматически:

1. `ruff.toml` → `[lint.per-file-ignores]`. BLE001 снят с зелёной зоны
   (`cli.py`, `runner.py`, `watcher.py`, `*_watcher.py`,
   `hh_browser_launcher.py`). Красная зона **не** в списке исключений — там
   BLE001 остаётся видимым. Backlog: 708 → 599.

2. `tests/test_ble001_zone_policy.py` (15 тестов) — то, чего линтер не умеет:
   - число «широких и молчаливых» хватов в красной зоне заморожено базлайном
     (25 на восемь файлов). Новый молчаливый хват на пути отправки роняет
     тест; исправление — просто понизить число в `RED_BASELINE`, и улучшение
     оказалось запертым;
   - `S110` нигде не в per-file-ignores — молчаливое глотание не легализуем
     ни в одной зоне;
   - ни один файл красной зоны не попал в per-file-ignores;
   - конфиг и список зон не расходятся (переименовал файл — обнови оба).

**Не сделано, и правильно:** хелпер `swallow()` для 80 голых `pass` **не
внедрялся**. Это 80 механических правок ради того, чтобы `pass` стал
`logger.debug(...)` — ценность сомнительна, а риск задеть работающий код
реальный. Если когда-то дойдут руки, начинать не с `cli.py` (там это
осознанно), а с `browser_executor.py` (42 молчаливых) и `db.py` (23) — это
почти треть всех молчаливых хватов в репо.

**Диагноз по зонам (AST-скан, 2026-09-09):**

| Зона | Широких хватов | Из них молчаливых | Что с ними делать |
|---|---:|---:|---|
| RED (путь отправки) | 55 | **25** | не растёт, базлайн в тесте |
| GREEN (CLI, поллеры) | 111 | 36 | BLE001 снят, S110 оставлен |
| NEUTRAL (всё остальное) | 210 | 134 | трогать по случаю, не кампанией |

Худшие в красной зоне: `submission_recovery.py` — 8 молчаливых из 9,
`hh_submission.py` — 9 из 16. Это не значит, что они fail-open: каждый надо
читать. Но если выбирать, куда смотреть следующим разом — начинать оттуда.

### 8. Kill-switch `SUBMIT_ALLOWED` не выключается переменной окружения (высокий) — **ИСПРАВЛЕНО 2026-09-09**

Найдено 2026-09-09 по двум упавшим тестам. Не `except`, но тот же класс
ошибки — «система не может узнать, что её остановили».

```python
# ai_assistant/hh_submission.py:725
submit_allowed = (
    os.getenv("SUBMIT_ALLOWED", "").strip().lower() in ("1", "true", "yes")
    or bool(getattr(config, "SUBMIT_ALLOWED", False))   # ← проблема
)
```

`config.SUBMIT_ALLOWED` вычисляется один раз, **на импорте** `ai_assistant/config.py`:

```python
# ai_assistant/config.py:62
SUBMIT_ALLOWED = os.getenv("SUBMIT_ALLOWED", "false").strip().lower() in ("1","true","yes")
```

А `config.py` первым делом делает `load_dotenv(PROJECT_ROOT/.env, override=True)`.
Итог: если в `.env` лежит `SUBMIT_ALLOWED=true`, то

- `SUBMIT_ALLOWED=false` в окружении **не выключает** гейт:
  вторая часть `or` всё равно даст `True`;
- `SUBMIT_ALLOWED=false` через `monkeypatch.setenv` в тестах — тоже;
- единственный способ выключить — править `.env`.

**Почему это fail-open, а не pedantry:** kill-switch существует именно на тот
случай, когда нужно *быстро* остановить отправку, не пересобирая процесс.
Сейчас он в этом смысле не работает — решение замораживается в момент старта
и не слушается ни окружения, ни БД.

**Что ломает прямо сейчас:** `.env` в корне проекта содержит `SUBMIT_ALLOWED=true`
(файл перезаписан 2026-09-09 00:59). Из-за этого падают
`tests/test_hh_submission_gates.py::test_gate_submit_allowed` и
`tests/test_step25_unified_submission.py::test_execute_hh_submission_blocked_without_submit_allowed`
— оба ожидают, что `SUBMIT_ALLOWED=false` блокирует отправку.

**ИСПРАВЛЕНО 2026-09-09.** Сначала не трогал: правка меняет поведение
продакшн-гейта, а `.env` с `true` похож на осознанную подготовку к отправке.
Сделал позже — но строго в fail-closed сторону: фикс может только *усилить*
запрет, never ослабить. Если окружение молчит, поведение прежнее.

Сделано в `ai_assistant/config.py`:

1. До `load_dotenv(..., override=True)` снимается слепок настоящего окружения —
   `_OPERATOR_SUBMIT_ALLOWED = os.getenv("SUBMIT_ALLOWED")`. Без слепка ничего
   не выходит: `override=True` успевает затоптать переменную значением из `.env`
   ещё до того, как гейт вообще её увидит.
2. `submit_allowed()` перечитывает состояние в момент вызова: явно заданный
   оператором `false` побеждает, `.env` остаётся только fallback.
3. `hh_submission.py:730` зовёт `config.submit_allowed()` вместо
   `env OR замороженный config`.

Оба упавших теста позеленели.

### 9. Не-HH источники обходили вообще все гейты (высокий)

Найдено 2026-09-09 попутно, пока чинил №8.

`browser_executor.submit_application_in_browser()` пускает через
`execute_hh_submission` (там все 11 гейтов) **только** id, начинающиеся с `hh:`:

```python
# ai_assistant/browser_executor.py:2321
if vacancy_stable_id.startswith("hh:"):
    ...  # Path A: execute_hh_submission, затем безусловный return
```

Всё остальное проваливается в унаследованную ветку ниже, которая в итоге зовёт
`use_adapter.submit_application()` (`:2734`) — **реальный клик по Submit** —
без kill-switch, без сверки отпечатка, без проверки review. Не-HH id не
гипотетика: в проекте есть адаптеры `habr_career`, `himalayas`, `remoteok`,
`weworkremotely`.

Проверено экспериментально: с отключённым фиксом вызов
`submit_application_in_browser("remoteok:123", confirm_submit=True)` доходил
до «Vacancy not found», успев сгенерировать `submission_id` — работа началась
при выключенном рубильнике.

**Исправлено:** kill-switch проверяется для **всех** источников, сразу после
проверки `confirm_submit` и до `init_db()` — до того, как что-либо трогает
браузер или базу.

Регрессионные тесты: `tests/test_ble001_fail_closed.py`, 5 штук на №8 и №9
(всего в файле 20). Проверено на недекоративность: при подмене
`if explicit:` на `if False:` падают ровно 3 — два встречных, как и задумано,
проходят.

>
> - Находка №10 закрыта. **Extraction молча деградировал в паливный headless.**
>   `PlaywrightBrowserAdapter.open()` пробовал `connect_over_cdp()` и при любой
>   неудаче молча поднимал `launch(headless=True)`. Замерено
>   `tools/browser_fingerprint_probe.py` (не на слово): Playwright headless —
>   `DETECTED` (`navigator.webdriver=true`, `HeadlessChrome` в UA, 0 плагинов,
>   SwiftShader), живой Хром и BrowserOS — чистые. Итого submit шёл через
>   настоящий браузер, а extract — через бота. Фолбэк остался (жёсткий отказ
>   хуже), но стал громким: warning в лог + флаг `cdp_fallback` в снапшоте и в
>   `extraction_meta`, оба гейта на нём закрываются. Плюс один резолвер CDP
>   (`resolve_cdp_url`) вместо двух, и `is_cdp_reachable` больше не ходит через
>   `http_proxy`. Тестов в файле теперь 35.

### 10. Extraction молча деградировал в паливный headless (высокий) — **ИСПРАВЛЕНО 2026-09-09**

Найдено не статикой, а замером. `PlaywrightBrowserAdapter.open()`:

```python
# ai_assistant/browser_executor.py:1108 (было)
if self.cdp_url:
    try:
        self.browser = self.play.chromium.connect_over_cdp(self.cdp_url)
        self._is_cdp = True
        ...
    except Exception as e:
        self._is_cdp = False        # ← и всё. Ни лога, ни флага.

if not self.page:
    self.browser = self.play.chromium.launch(headless=self.headless)
```

Классический BLE001: исключение проглочено, выполнение продолжается по ветке
«всё нормально». Что именно терялось, стало видно только после замера
отпечатка (`tools/browser_fingerprint_probe.py`, CDP + Playwright):

| браузер                | вердикт    | `navigator.webdriver` | WebGL-рендерер  | плагины |
|------------------------|------------|-----------------------|-----------------|---------|
| Chrome 152 (CDP)       | SUSPICIOUS | false                 | Intel Iris Xe   | 5       |
| BrowserOS 148 (CDP)    | CLEAN      | false                 | Intel Iris Xe   | 5       |
| Playwright headless    | DETECTED   | **true**              | **SwiftShader** | **0**   |

Плюс `HeadlessChrome` в User-Agent и отсутствующий `window.chrome.loadTimes`.
То есть страницу hh.ru читал браузер, который hh.ru опознаёт за миллисекунды —
а пайплайн об этом не знал, потому что флага не существовало. Хуже того, submit
шёл через живой браузер по CDP: **один отклик, два разных браузера.**

Две причины, почему это было не видно:

1. Фолбэк был беззвучным — не тот ли случай, который BLE001 и ловит.
2. Адаптер резолвил CDP как `CDP_URL | HH_CDP_URL | 9222`, а лаунчер — как
   `9222 | HH_CDP_URL | BrowserOS 9110`. Fallback на BrowserOS был только в
   лаунчере, так что при мёртвом 9222 адаптер гарантированно уходил в headless.

**Грабли замера, они же — отдельная находка.** `is_cdp_reachable` использовал
`urllib.request.urlopen`, а тот honour-ит `http_proxy`. С прокси в окружении
живой браузер отвечает `502 Bad Gateway` вместо `connection refused` и читается
как мёртвый — что включает молчаливую подмену браузера. **Достаточно выставить
переменную окружения, чтобы пайплайн поехал на другом профиле.**

**Исправлено:**

- `open()` пишет warning и кладёт `cdp_fallback` / `cdp_fallback_reason` в
  результат; флаг едет в снапшот `extract_application_form()` и в
  `extraction_meta` (как `error` в находке №7).
- `application_qa` и `build_review_gate` закрываются на `cdp_fallback`: форма,
  прочитанная паливным браузером, может оказаться бот-страницей.
- `resolve_cdp_url()` в `hh_browser_launcher` — единственный источник правды
  для лаунчера, `PlaywrightBrowserAdapter` и `CDPBrowserAdapter`. Подмена на
  BrowserOS логируется: это смена профиля, а профиль без сессии hh.ru читает
  совсем другую форму.
  Оговорка, стоившая полчаса: `resolve_cdp_url()` срабатывает **только если не
  задано вообще ничего**. Первая версия проверяла `chosen != DEFAULT_HH_CDP_URL`
  и поэтому считала `HH_CDP_URL=http://127.0.0.1:9222` «не решением» — и всё
  равно прыгала на BrowserOS, если 9110 отвечал. Тот же fail-open, который
  Finding и чинил, только внутри самого фикса. Поймал не новый тест, а старый
  `test_ensure_hh_browser_starts_chrome_when_missing`: его мок скриптует
  `is_cdp_reachable` как `[False, True]`, и лишние два вызова из резолвера
  съели последовательность.
- `hh_browser_launcher` ходит в CDP через opener с `ProxyHandler({})`.

Регрессионные тесты: 15 штук на №10 (всего в файле 35). Проверено на
недекоративность мутациями: запись флага → `None` ломает
`test_cdp_attach_failure_is_flagged_and_logged`, выключение гейта ломает
`test_review_gate_blocks_on_cdp_fallback`; встречные проверки (успешный CDP,
честная пустая форма) при этом проходят.

### 11. `blocked=True` на каждой странице hh.ru

**Файл:** `ai_assistant/browser_executor.py`, `PlaywrightBrowserAdapter.open()`.

```python
content = self.page.content().lower()
blocked = any(x in content for x in ["captcha", "cloudflare", "access denied", "login required"])
```

`content` — это **сырой HTML**. На hh.ru в каждом ответе лежит i18n-бандл, и
единственное совпадение на живой вакансии
(`https://hh.ru/vacancy/134835019`) — байт 1908203:

```
"error.signup.captcha.invalid":"пожалуйста, подтвердите, что вы не&nbsp;робот"
```

Итого `blocked=True` на любой странице, включая совершенно нормальную,
читаемую вакансию.
Это не косметика: `open_res["blocked"]` разбирается в ветвлениях
`browser_executor` (~2026, ~2668, ~2966), а не только логируется.

**Почему это обидно:** в `hh_extractor._detect_blocked` уже написано ровно это
предупреждение — «captcha встречается в JS i18n-бандле, а не как активный
челлендж, поэтому считаем блоком только `data-qa="captcha` или связку
captcha+bloko-modal». Ловушка уже была известна, но `open()` реализовал
наивную версию вторым экземпляром.

**Исправлено:** общий помощник `_detect_page_blocked(html, body_text, title)`,
который зовёт ту же `hh_extractor._detect_blocked`, а текстовые маркеры
(`access denied`, `login required`) ищет в **видимом** `inner_text("body")`,
а не в HTML. 404 по-прежнему ловится по заголовку.

### 12. `CDPBrowserAdapter` ходил в CDP через proxy

**Файл:** `ai_assistant/browser_executor.py`, `CDPBrowserAdapter.open()` /
`close()` — два оставшихся `urllib.request.urlopen`.

Находка №9 чинила эту же багу в `hh_browser_launcher` (5 мест): `urlopen`
honour'ит `http_proxy`, и живой браузер на localhost отвечает
`502 Bad Gateway` — то есть **путь отправки** видел «заблокировано» на
здоровом браузере. В `browser_executor` правка не доехала.

**Исправлено:** оба вызова идут через общий `_NO_PROXY_OPENER`.

**Продолжение — sweep по всему пакету.** Проверка `grep -rn "urlopen" ai_assistant/`
показала, что это был не один файл, а пять вызовов в трёх модулях:

| Модуль | Что вызывало | Путь |
|---|---|---|
| `browser_executor.py` | `/json/new`, `/json/close` | отправка |
| `hh_vacancy_navigator.py` | `/json/list`, `/json/new` ×2 | навигация на вакансию |
| `prefill_execute.py` | `/json/list` ×2 | префилл формы |

Все переведены на `_NO_PROXY_OPENER`. `telegram_notifier.py` **намеренно
оставлен**: он ходит на `api.telegram.org`, внешний хост, там honour'ить
прокси — правильное поведение.

Чтобы класс баги не вернулся в новом коде, добавлен гард-тест
`test_no_cdp_call_goes_through_urlopen`: сканирует `ai_assistant/*.py` и
падает на любом `urllib.request.urlopen` вне allowlist'а. Второе утверждение
следит, чтобы исключение для telegram не протухло (если он перестанет
использовать `urlopen`, тест потребует убрать дырку, а не держать её).
Прецедент такого статического гарда в репо уже есть —
`test_stage30c_2_isolated_world.py`.

### 13. Таймаут ≠ «браузера нет»

**Файл:** `ai_assistant/hh_browser_launcher.py`, `resolve_cdp_url()`.

```python
if not is_cdp_reachable(DEFAULT_HH_CDP_URL, timeout=0.3) and is_cdp_reachable(BROWSEROS_CDP_URL, ...):
    return BROWSEROS_CDP_URL
```

`is_cdp_reachable` сворачивает «медленно» и «нет» в одно `False`. Для health
check это терпимо, для маршрутизации — нет: BrowserOS это **другой профиль**,
обычно без сессии hh.ru. Замер (`tools/`, 10 прогонов): живой Chrome отвечает
за 0.6-17 мс, но 300 мс он не укладывается достаточно часто, чтобы менять
профиль наугад.

**Исправлено:** `probe_cdp()` возвращает три состояния — `CDP_ALIVE` /
`CDP_DEAD` / `CDP_TIMEOUT`. Прыжок только при `CDP_DEAD`; при таймауте —
повтор с запасом 2 с и остаёмся на 9222. `is_cdp_reachable()` теперь делегирует
в `probe_cdp()`, чтобы две реализации не разъехались.

**Честная оговорка.** Эта находка — ужесточение, а не причина увиденного
поведения. Резолвер выбирал 9110 не из-за гонки, а потому что `.env` прибивает
`HH_CDP_URL=http://127.0.0.1:9110`, а явная переменная окружения — это решение
оператора и оно обязательно (собственно, то, что чинила находка №10). Проверка
в E2E-пробе это учитывает: если CDP прибит env, она проверяет, что резолвер
его уважает, а не «должен быть 9222».

### 14. `cli.py` решал «каким браузером едем» самостоятельно (высокий) — **ИСПРАВЛЕНО 2026-09-10**

```python
# ai_assistant/cli.py:1531 (было)
_DEFAULT_HH_CDP_URL = os.getenv("HH_CDP_URL", "http://127.0.0.1:9222")
```

Это вторая копия `hh_browser_launcher.resolve_cdp_url()`, только слабее, и
несовпадения между ними — не теория, а замер:

```
env CDP_URL='http://127.0.0.1:9223' HH_CDP_URL='http://127.0.0.1:9110'
cli._DEFAULT_HH_CDP_URL = http://127.0.0.1:9110
resolve_cdp_url()       = http://127.0.0.1:9223          <-- DIVERGED
```

Три причины расхождения, все реальные:

1. Значение frozen на моменте импорта — то есть до того, как `.env`
   гарантированно загружен (сегодня совпадает только потому, что `cli.py`
   импортирует `.config`, а тот зовёт `load_dotenv`; убери этот импорт — и
   порядок сломается).
2. Читается только `HH_CDP_URL`, а `resolve_cdp_url()` сначала смотрит
   `CDP_URL` — задокументированную переменную.
3. Константа никогда не узнает, что 9222 мёртв, а BrowserOS отвечает.

Чем это грозит, видно на третьем месте в том же файле:

```python
# ai_assistant/cli.py:794 (было) — путь РЕАЛЬНОЙ отправки
adapter = CDPBrowserAdapter(DEFAULT_HH_CDP_URL)   # 9222, независимо от .env
```

`--adapter cdp` ехал в профиль, в котором сессии hh.ru может и не быть, в то
время как вся остальная экстракция шла в 9110. Ровно баг №10, только наоборот
направленный: не «паливный headless вместо настоящего», а «другой настоящий».

**Исправление.** Константа заменена на `_default_hh_cdp_url()`, которая
зовёт `resolve_cdp_url()`. Кэша нет намеренно: оператор (или тест) может
поменять окружение посереди процесса, а устаревший endpoint здесь — это и
есть удаляемый баг. Проба происходит только когда ничего не прибито.

Sweep по классу: тот же замороженный endpoint импортировали ещё три модуля —
`hh_application_runner` (очередь заявок), `hh_message_watcher` (вотчер
сообщений), `hh_post_submit_verifier` (проверка после отправки). Все переведены
на резолвер. Побочно ушёл ставший ненужным модульный `import os` в `cli.py`.

Пин: `test_no_second_copy_of_cdp_resolution` (AST-обход: вне
`hh_browser_launcher` никто не читает `HH_CDP_URL`/`CDP_URL` сам),
`test_nothing_reads_the_frozen_cdp_constant`,
`test_cdp_adapter_is_built_from_the_resolver`. AST, а не подстрока: гард по
подстроке срабатывал на собственный комментарий с этим кодом внутри.

## Исправление собственной ошибки: `apply_link` селектор цел

**Здесь раньше было «наблюдение, не закрытое» про сломанный селектор. Оно было
неверным, и я его снимаю.**

Я писал, будто `a[data-qa='vacancy-response-link-top']` не существует в DOM.
На самом деле я смотрел на вакансию `/vacancy/136551280`, по которой отклик
**уже отправлен**, — а hh.ru в этом случае заменяет «Откликнуться» на «Чат»
(`vacancy-response-link-view-topic`). Проверка на свежей вакансии
(`/vacancy/128659037`, ответа нет):

```
a[data-qa='vacancy-response-link-top']
  href = /applicant/vacancy_response?vacancyId=128659037&employerId=829490&hhtmFrom=vacancy
  text = 'Откликнуться'
```

Селектор работает. Вывод: проверять селектор надо на вакансии **без** отклика,
а E2E-проба брала кандидата из БД не фильтруя уже отвеченные. Пробник это
теперь учитывает (см. ниже).

Побочно из этой же проверки вылезла настоящая дыра — находка №15.

### 15. Проверка «та ли это вакансия» существовала, тестировалась и не работала (высокий) — **ИСПРАВЛЕНО 2026-09-10**

`hh_live_page_checks.check_live_page()` умеет сверять заголовок страницы с
ожидаемым (шаг 7, `expected_title`), и на него **есть тест** — он передаёт
`"Senior Python Developer"` и проходит. Но единственный продакшн-вызов не
передавал ничего:

```python
# ai_assistant/hh_submission.py:969 (было)
live_result = check_live_page(evaluate_fn, expected_vacancy_id=vacancy_stable_id)
```

А при `expected_title=None` модуль делает `else: res.title_matched = True` —
шаг 7 просто не выполняется. Проверка была, тест был зелёный, защиты не было
никакой.

Почему это важно: числовой id, который сверяется до этого, читается **из URL,
который мы только что открыли** — он совпадает по построению. Единственное,
что может заметить подмену, — заголовок.

Подмена не выдуманная. В `vacancies.json` 19 ссылок на hh.ru, три из них
ведут вообще на другие вакансии:

| id | заявлено в файле | на самом деле |
|---|---|---|
| 74 | Инженер по ИИ-автоматизации (n8n + Python) | **Охранник (Чукотка)** |
| 77 | Инженер AI-автоматизации (n8n / Python / LLM) | **Монтажник систем вентиляции** |
| 83 | Инженер по автоматизации процессов (AI Agents / n8n / Python) | **Продавец (Чебоксары)** |

Плюс id=16 — «Вам недоступна эта вакансия».

Сейчас эти три отсекаются, но **по счастливой случайности**: все три в архиве,
а проверка на «вакансия в архиве» стоит раньше. Будь «Продавец» живой
вакансией, пайплайн отправил бы туда сопроводительное письмо про AI-агентов.
Никакой другой гейт этого не ловит.

**Исправление.** `execute_hh_submission()` достаёт ожидаемый заголовок из БД и
передаёт его в `check_live_page()`. Проверка становится активной.

Один тест пришлось поправить по существу, а не «чтобы прошёл»:
`test_stage30t_remote_vacancy_passes_gate` гонял `MockBrowserAdapter`, чей
заголовок по умолчанию — `"Mock Page"`, при вакансии `"AI Builder"`. Мок
симулировал **не ту страницу**, и новая проверка его честно заблокировала.
Моку задали правильный заголовок.

### 15b. Fail-closed: не знаем, куда откликаемся, — не откликаемся

Сначала вместо WARNING жёсткого отказа не было: менял поведение пути
отправки, а решение за Мишей. Решение принято — сделано fail-closed.

Замер сразу показал, что `vacancies` **не единственное** место, где живёт
заголовок. Пять тестов (stage46 х2, stage47 х2, stage50 х1) упали с
`No vacancy row/title for hh:...`. Причина не в проверке: путь раннера и
стейт-машины несёт заголовок в записи заявки (`hh_applications.title`), а
строки в `vacancies` у него нет вообще — заявка создаётся по URL. Отказывать
по отсутствию строки в `vacancies` значило бы блокировать легитимный путь, у
которого заголовок есть.

**Итоговое правило:** заголовок берётся из `vacancies`; нет — из
`hh_applications.title`; нет ни там ни там — жёсткий отказ (`BLOCKED`,
`submit_count=0`) и `logger.error`. До страницы в этом случае не доходим
вообще: смотреть на неё бессмысленно, мы всё равно не сможем сказать, та ли
это вакансия.

Побочно вылез соседний fail-open того же класса — см. раздел 16.

Тестов в файле теперь 58.

### 16. Гейт жёстких ограничений пропускался, если вакансии нет в БД (высокий) — **ИСПРАВЛЕНО 2026-09-10**

```python
# ai_assistant/browser_executor.py:2365 (было)
row = get_vacancy_by_id(vacancy_stable_id)
vac = _row_to_vacancy(row) if row else None
...
# Defense-in-Depth Hard Constraint Gate
if vac:                      # <-- нет строки == нет проверок
    if getattr(profile, "remote_required", False):
        is_rem, rem_reason = is_strictly_remote(vac)
        ...
    hard_reject, hard_reason = _hard_constraints(...)
```

Если строки в `vacancies` нет, `remote_required` и все хард-констрейнты не
проверяются вообще — отправка едет дальше, к браузеру. Это ровно тот класс
ошибки, что и №15: «не знаем — считаем, что всё хорошо».

Хуже то, что в **том же файле** соседняя точка входа (`submit_application`,
строка ~2516) на это же условие отвечает иначе:

```python
if not row:
    return SubmitResult(..., status="BLOCKED", error="Vacancy not found")
```

Две противоположные политики на один и тот же случай, в одном модуле.

**Исправление:** `submit_application_in_browser()` отказывает так же —
`BLOCKED`, `Vacancy not found in DB: <id>`. Гейт больше не под `if vac:`.

Мутация дала наглядное доказательство: с откатом правки тест
`test_submit_application_refuses_when_the_vacancy_row_is_missing` падает не
на ассерте, а на

```
RuntimeError: SAFETY VIOLATION: unmocked socket connection attempted
during pytest: ('127.0.0.1', 9222)
```

То есть без правки код реально полез соединяться с браузером по поводу
вакансии, которой нет в базе.

### 17. Проверка заголовка пропускала страницу, чей заголовок НЕ совпал (высокий) — **ИСПРАВЛЕНО 2026-09-10**

Нашлось, когда я после №16 прочёсывал класс «нет данных — считаем, что всё
хорошо». В `check_live_page()`, шаг 7:

```python
# ai_assistant/hh_live_page_checks.py:266 (было)
if exp_words and any(w in curr_l for w in exp_words) or sim >= 0.6 or not exp_words:
    res.title_matched = True
else:
    logger.warning(...)
    if res.title_similarity < 0.3:      # <-- молча пройти при 0.3..0.6
        res.is_ok = False
        return res
```

Две дыры в одном выражении.

**(а) Короткий заголовок выключал проверку целиком.** `not exp_words` — если
в ожидаемом заголовке все слова короче 4 символов, `exp_words` пуст и
условие истинно. Замер:

| ожидаемый | на странице | similarity | вердикт |
|---|---|---|---|
| `Go Dev` | `Уборщица` | **0.00** | `title_matched = True` |
| `RPA Dev` | `Охранник` | **0.00** | `title_matched = True` |

Совсем разные вакансии, единственная проверка, способная заметить подмену,
сказала «да».

**(б) Несовпадение в зоне 0.3..0.6 молча проходило.** Замер: `HR Generalist`
против `HR Manager` — similarity 0.52, ни одного общего слова. Код записал
WARNING и пошёл дальше: `is_ok` остался `True`, страница принята.

**Исправление:** заголовок, который не совпал, роняет проверку при любой
similarity. `not exp_words` убран.

Контр-замер, что ужесточение не закрывает легитимные страницы: `Продавец`
против `Продавец (Чебоксары, Гагарина Ю., 17)` — similarity всего 0.36, но
общее слово есть, и страница проходит, как и должна. hh.ru постоянно
дописывает адрес к заголовку.

### 18. Kill-switch, который не прочитался, отвечал «не нажат» (высокий) — **ИСПРАВЛЕНО 2026-09-10**

```python
# ai_assistant/hh_submission.py:731 (было)
try:
    from . import db
    is_paused = db.is_submit_paused()
except Exception:
    pass
```

Гейт 1 — `GATE_SUBMIT_ALLOWED`, аварийный стоп перед отправкой. Если чтение
флага падало, `is_paused` оставался `False`, и гейт шёл дальше, как будто стоп
не нажат.

**Замер.** Подменил `db.is_submit_paused()` на бросок исключения, всё остальное
валидно:

```
passed      : True
failed_gate : None
reason      : All 11 gates passed successfully
gate_results[submit_allowed]: {'passed': True, 'reason': 'SUBMIT_ALLOWED enabled'}
```

Хуже самого пропуска — четвёртая строка. В аудите записано «SUBMIT_ALLOWED
enabled»: ровно то же, что пишется, когда стоп честно проверили и он выключен.
По логу невозможно понять, что аварийный стоп вообще не проверялся.

**Противоречие внутри одного файла.** Четырьмя сотнями строк ниже тот же модуль
вызывает ту же функцию вообще без защиты:

```python
# ai_assistant/hh_submission.py:1136 — финальная проверка перед кликом
if db.is_submit_paused():
```

Там сбой чтения роняет отправку. Тот же вызов, противоположная политика, один
экран разницы. `hh_submit_policy.py:141` и `hh_application_runner.py:496` тоже
зовут её без `try`. Гейт 1 был единственным местом, где «не смогли прочитать
стоп» означало «отправляем».

**Исправление.** Не прочиталось — не отправляем; в `details` попадает
`kill_switch_error` с текстом исключения. Причина в ответе отличается от
обычного «выключено SUBMIT_ALLOWED», поэтому разница видна в логе.

**Встречные проверки** (что гейт 1 не стал просто всегда падать): флаг читается
и выключен — все 11 проходят; флаг нажат — тот же гейт блокирует с причиной про
kill switch.

**Мутация.** Откат правки: `test_gates_refuse_when_the_kill_switch_cannot_be_read`
падает на `assert True is False` с `All 11 gates passed successfully` — ловит
именно баг, а не валится на поломанном коде.

Тестов в файле теперь 62. Побочно убрал 5 задвоенных определений тестов — мои
же, от двойного запуска скрипта вставки (№8 и №9 продублировались целиком).
Python берёт последнее определение, так что они были мертвы и ни на что не
влияли, но файл врал о своём размере.

### 19. Путь автоклика не проверял ни один из двух стопов (критический) — **ИСПРАВЛЕНО 2026-09-11**

Три точки физически жмут кнопку отклика на hh.ru:

- `hh_submission.submit_application()`
- `hh_controlled_submit.controlled_real_submit()`
- раннер `auto_apply_modes.run_auto_apply()` (через второй)

Все три отдают гейткипинг в `preflight_submission()` — и **ни одна** не
проверяла аварийный стоп. Замер, всё остальное валидно:

```
SUBMIT_ALLOWED=false, system_settings.submit_paused=1   # ровно то, что пишет
                                                        # команда «стоп» в Telegram
verdict      : SUBMITTED
submit_count : 1
click_count  : 1
dom.clicks   : 1
```

**Почему было не видно.** `preflight_submission()` делегирует дальше в
`check_readonly_gates()` — а это гейты **2-10**. Гейт 1 (`GATE_SUBMIT_ALLOWED`,
он же kill-switch и `SUBMIT_ALLOWED`) живёт в `check_all_gates()`, который этот
путь не вызывает вообще. Оба стопа охраняли `execute_hh_submission()` и больше
ничего.

Масштаб: на этом пути висят 34 теста в пяти файлах, то есть путь рабочий, а не
теоретический.

**Исправление.** `preflight_submission()` проверяет оба стопа первым делом, до
чтения отзыва и до браузера. Сбой чтения стопа — `FAIL_CLOSED` (находка №18).

**Как измеряли ущерб.** Первый прогон дал 2 падения: `test_no_db_writes` в
`test_stage20i` и `test_stage20k`. Эти тесты запрещают **любое** обращение к БД
во время отправки — а стоп живёт в БД. Конфликт настоящий, не ложный: тест
закреплял «отправка не пишет в БД», и его мокается только `get_connection`.
Стоп читается через другое соединение, так что тест теперь мокает
`is_submit_paused` и продолжает доказывать своё: больше в БД никто не ходит.

Второй слой: 3 файла утверждают «клик был» (`test_stage20i`, `test_stage20j`,
`test_stage20k`, `test_stage21_auto_apply`, `test_stage31_watcher`) и падали при
`SUBMIT_ALLOWED=false`. Им добавлена autouse-фикстура, которая явно включает
отправку: дефолт «выключено» — это ровно то, о чём находка.

**Отдельная грабля при замере.** Прогон с `SUBMIT_ALLOWED=false` в шелле
портит результат: `config.submit_allowed()` даёт приоритет значению,
захваченному при импорте (`_OPERATOR_SUBMIT_ALLOWED`, находка №8), поэтому
`monkeypatch.setenv("SUBMIT_ALLOWED", "true")` возвращает `False`. Три теста
`test_ble001_fail_closed.py` падают на этом **без** моих правок. В тестах
находки №19 значение переключается через `_set_submit_allowed()`, который
патчит все три источника разом.

**Мутация.** Удаление блока gate 0 роняет три теста:
`assert 1 == 0` при `submit_count`, с `success markers found: ['Вы откликнулись'];
read-only DOM confirms submission`. Четвёртый (встречный, «оба стопа разрешают —
клик есть») остаётся зелёным, то есть ловятся именно стопы, а не клик вообще.

Тестов в файле теперь 66.

### 20. Второй стоп отсутствовал на всех путях клика (критический) — **ИСПРАВЛЕНО 2026-09-11**

Стопов два, и они разные:

| | где живёт | кто пишет |
|---|---|---|
| `SUBMIT_ALLOWED` | env + `.env` | оператор, руками |
| `submit_paused` | строка в `system_settings` | команда «стоп» в Telegram |

Находка №9 завела **первый** в `submit_application_in_browser()` для не-hh
источников. **Второй** не проверялся там вообще нигде.

Замер (`submit_paused=1`, всё остальное валидно):

```
status      : SubmitStatus.SUBMITTED
click_called: 1
```

Функция дошла до адаптеров и кликнула. Прошло время, пока это удалось
показать: чтобы добраться до клика, нужны вакансия в БД, одобренный отзыв,
сессия `READY_FOR_REVIEW`, `READY_TO_APPLY`, элемент очереди и пакет заявки.
Первые замеры падали на «Vacancy not found», «Review not approved»,
«Queue item not found» — то есть тест был зелёным **не по той причине**.
Хелпер `_save_non_hh_vacancy()` в тестах теперь создаёт всё это разом.

**Исправление.** Проверка `submit_paused` стоит рядом с `SUBMIT_ALLOWED` в той
же функции, для всех источников. Сбой чтения — `BLOCKED` с текстом (находка
№18).

**Мутация.** Удаление проверки: `assert SubmitStatus.SUBMITTED == 'BLOCKED'` —
тест падает ровно на том, что клик состоялся.

Отдельно про качество теста: первая версия мутации падала на
`'vacancy not found in db'`, и это был **не** тот отказ. Тест, который
спотыкается о чужой гейт, ничего не доказывает про свой. Понадобилось два
раунда, чтобы он доходил до клика.

Тестов в файле теперь 69.

### 21. Третий стоп знала одна функция, а кликов — четыре (критический) — **ИСПРАВЛЕНО 2026-09-11**

Стопов не два, а три. Третий — файл `data/STOP_SUBMITS`; в
`hh_submit_policy.evaluate()` он перечислен **первым**:

```python
stop_paths = [stop_file_path, "data/STOP_SUBMITS", "STOP_SUBMITS"]
is_stopped_file = any(p and os.path.exists(p) for p in stop_paths)
```

Единственный потребитель `evaluate()` — `hh_application_runner.py:417`,
внутри `if auto_mode:`. То есть файл останавливал автономного раннера и
**только его**. Находки №9, №18, №19 и №20 гонялись за двумя другими стопами
по всем путям клика; третий там не упоминался ни разу.

Замер до правки — валидная заявка через `submit_application()`, в рабочем
каталоге лежит `data/STOP_SUBMITS`:

```
stop file     : .../data/STOP_SUBMITS exists = True
report.status : SubmissionStatus.SUBMISSION_UNKNOWN
clicks        : 1
RESULT        : *** CLICKED WHILE STOPPED ***
```

Первая проба, к слову, снова упёрлась не в тот гейт — в
`application_exists` в БД. Пришлось достроить заявку, и только тогда стало
видно клик. Это уже третий раз за прочёс, когда зелёный результат означал
«тест не доехал до проверяемого места».

**Причина — не пропущенный `if`.** Шесть мест независимо друг от друга
выписывали свой список стопов: `evaluate()`, `preflight_submission`,
`check_all_gates`, предкликовый заслон в `execute_hh_submission`,
предкликовый заслон в `hh_application_runner`, и не-hh ветка
`submit_application_in_browser`. Добавить стоп означало отредактировать шесть
мест, и пропущенным оказалось то, которое никто не заметил. Пока форма
остаётся такой, четвёртый стоп будет пропущен так же.

**Исправление по существу.** Один предикат
`hh_submission.submission_halt_reason()` владеет всеми стопами и возвращает
причину отказа или `None`:

- файл `STOP_SUBMITS` (`STOP_SUBMITS_FILE` → `data/STOP_SUBMITS` →
  `STOP_SUBMITS`),
- `system_settings.submit_paused`,
- `SUBMIT_ALLOWED`.

Сбой чтения БД — отказ с текстом (правило находки №18), а не «не нажат».
Все пути клика теперь спрашивают только его. `evaluate()` сохранил свой
`stop_file_path` — он передаётся через переменную `STOP_SUBMITS_FILE`.
Ни один путь больше не выписывает стопы сам.

**Стопы не одного рода — и это важно.** Первая версия правки заставила все
стопы главенствовать над `dry_run`. Два теста упали, и они были правы:

- `tests/test_hh_submission_gates.py::test_gate_submit_allowed`
- `tests/test_step25_unified_submission.py::test_execute_hh_submission_dry_run`

`SUBMIT_ALLOWED=false` при `dry_run=True` **обязан** проходить. Это
задокументированный контракт: README строка 83 («must be explicitly enabled
... unless in dry_run mode») и шаг 2.4 из `audit_remediation_log.md` —
«исправлен баг блокировки при `dry_run=True`». Смысл в том, что сухой прогон
не делает ни одной браузерной мутации, поэтому предохранитель, который
сторожит мутацию, к нему неприменим.

Различие такое:

| стоп | что это | сухой прогон |
|---|---|---|
| `submit_paused` | приказ «стой» от оператора | **блокируется** |
| файл `STOP_SUBMITS` | приказ «стой» | **блокируется** |
| `SUBMIT_ALLOWED` | условие допуска «а можно ли вообще» | обходится |

Поэтому у предиката есть параметр `include_submit_allowed`, а гейт 1 зовёт
его с `False`. Проверено пробой: при выключенном `SUBMIT_ALLOWED` dry-run
проходит, при приказе «стой» — не проходит, даже если `SUBMIT_ALLOWED=true`.

**Мутация.** Из предиката убрано распознавание файла — ровно то состояние, в
котором код был до находки. Падают три теста, и один падает ровно на дыре:

```
test_halt_reason_reports_the_stop_file                          FAILED
test_check_all_gates_refuses_while_the_stop_file_is_present      FAILED
test_non_hh_source_is_blocked_by_the_stop_file                   FAILED
    AssertionError: None
    assert <SubmitStatus...: 'SUBMITTED'> == 'BLOCKED'
```

**Встречные проверки.** `test_halt_reason_is_none_when_no_stop_is_engaged`,
`test_gates_still_pass_with_clean_switches`,
`test_submit_application_still_clicks_when_no_stop_is_engaged` (стоп не стал
стеной) и `test_submit_allowed_is_still_bypassed_by_a_dry_run` (контракт шага
2.4 зафиксирован явно, чтобы следующая правка не сломала его молча).

Тестов в файле теперь 81.

**Побочная находка в тестовой обвязке: `SUBMIT_ALLOWED` утекал между файлами.**

Хелпер `_set_submit_allowed()` пишет прямо в `os.environ` и в `config`, и не
может иначе: `config.submit_allowed()` отдаёт приоритет значению, снятому при
импорте (находка №8), поэтому `monkeypatch.setenv` его не переключает. Плата за
это — запись **навсегда**: какой тест вызвал последним, тот и задаёт
`SUBMIT_ALLOWED` для всего остатка сессии.

Мои новые тесты зовут его с `False`. Результат полного прогона:

```
74 failed, 1545 passed
```

Все 74 — в `test_step24_gate_defects.py`, `test_step25_unified_submission.py` и
`test_submission_verifier.py`, все с одной причиной: «Submission is disabled by
SUBMIT_ALLOWED configuration». Каждый из этих файлов **по отдельности зелёный**.
Это утечка, а не регрессия продукта, и свалить её на продукт было бы очень легко.

Лечение: autouse-фикстура, возвращающая три источника на место. Две версии
оказались неверными, и обе — поучительно:

1. **Восстанавливать значение, снятое на входе в тест.** Выглядит как то же
   самое и не работает: если один тест утёк `False`, все последующие снимают
   `False` как свою базовую линию и старательно восстанавливают утечку.
   Ремонт, который принимает повреждение за норму, — не ремонт. Базовая линия
   снимается **один раз за сессию**.
2. **Гард из одного теста, который в конце сам себя подчищает.** Мутация
   (фикстура выключена) показала `87 passed` — гард прибрал за собой и утечку
   не поймал. Гард обязан **оставлять** испорченное состояние, а следующий тест
   — проверять, что оно восстановлено. Плюс он обязан сперва **задать** известное
   состояние: если читать базовую линию на входе, при выключенной фикстуре она
   уже утекла и сравнение `False is False` проходит впустую.

Финальная версия — два упорядоченных теста
(`test_leak_guard_step_1_leaves_submit_allowed_off`,
`test_leak_guard_step_2_sees_the_value_restored`). Мутация с выключенной
фикстурой валит оба: и гард, и `test_step25`.


### 22. CDP-адаптер докладывал успех без клика, а ветка записывала SUBMITTED (критический) — **ИСПРАВЛЕНО 2026-09-12**

Нашлось после №21, когда пошёл проверять не «есть ли проверка», а **кто зовёт
защитные функции вообще**. AST-скан «функции с защитными именами без вызовов вне
тестов» дал восемь кандидатов; эта дыра вылезла по дороге.

**Слой 1 — адаптер.** `CDPBrowserAdapter.submit_application()`:

```python
await ws.send(... click_script ...)
res = json.loads(raw).get("result", {}).get("result", {}).get("value", {})
await asyncio.sleep(2)
return {"success": True, "details": res}      # <- безусловно
```

JS честно отвечает, кликнул ли он: `{clicked: true, text: ...}` или
`{clicked: false, error: "button not found"}`. Ответ читается в `res` и
**выбрасывается**: успех возвращается независимо от него.

Замер (поддельный websocket отдаёт ответ «кнопки нет»):

```
JS said        : {'clicked': False, 'error': 'button not found'}
adapter returns: {'success': True, 'details': {'clicked': False, 'error': 'button not found'}}
reported success: True
actually clicked: False
RESULT: *** SUCCESS REPORTED, NOTHING CLICKED ***
```

`PlaywrightBrowserAdapter.submit_application` устроен правильно: ищет
подтверждение в тексте страницы и возвращает `success=False` с
«No success confirmation found after submit». CDP — выпадающий, ровно как в №1.

**Слой 2 — ветка.** `submit_application_in_browser`, legacy-ветка:

```python
if not submit_result.get("success"):
    return FAILED
# success -> пишем в БД, двигаем трекинг, отдаём SUBMITTED
```

Замер полного пути с адаптером, отдающим ответ CDP для отсутствующей кнопки:

```
SubmitResult.status              : SUBMITTED
SubmitResult.error               : None
DB submissions row               : (..., '{"success": true, "details":
                                    {"clicked": false, "error": "button not
                                    found"}}', 'SUBMITTED', ...)
tracking status                  : SUBMITTED
is_already_applied (blocks retry): True
```

Клика не было. Система сказала, что он был. И в **той же строке БД**, которую
она пометила `SUBMITTED`, лежит доказательство обратного — `"clicked": false`.
Плюс `is_already_applied=True` навсегда закрывает повторную попытку: заявка,
которую на самом деле не отправили, больше не будет отправлена никогда.

Это форма №2 (два слоя, каждый по отдельности «деградировал аккуратно», вместе
дыра) плюс асимметрия №1 (CDP против Playwright).

**Исправление, оба слоя.**

1. Адаптер читает ответ: если `clicked` не `True` — `success=False` и причина
   из ответа. Нечитаемый ответ тоже не успех.
2. Ветка больше не верит слову: если в `details` пришло `clicked: false`, она
   отказывается записывать `SUBMITTED` и возвращает `FAILED`. Оба слоя чинятся
   потому, что третий адаптер может повторить форму №2.

Замер после правки:

```
adapter returns  : {'success': False, 'error': 'Submit click did not happen: button not found', ...}
SubmitResult.status              : FAILED
DB submissions row               : None
tracking status                  : READY_TO_APPLY
is_already_applied (blocks retry): False
```

**Мутации — по слоям, чтобы вина была однозначной.**

| Откат | Падает |
|---|---|
| слой 1: вернуть безусловный `success` | `test_cdp_adapter_does_not_report_success_when_the_button_was_missing`, `test_cdp_adapter_refuses_success_on_an_unreadable_answer` |
| слой 2: выключить проверку `clicked` | `test_legacy_branch_refuses_to_record_submitted_without_a_click` |

**Встречные проверки** (зелёные в обеих мутациях):
`test_cdp_adapter_still_reports_success_when_the_click_happened`,
`test_legacy_branch_still_records_submitted_when_the_click_happened`,
`test_legacy_branch_still_believes_an_adapter_without_click_details` — последний
важен: честные адаптеры (`Playwright`) вообще не отдают ключ `details`, и новая
проверка их не задевает.

Тестов в файле теперь 89.

### 23. Уведомление о заблокированной анкете — мёртвый код (средний) — **ИСПРАВЛЕНО 2026-09-12**

Замер до правки (анкета с обязательным неотвеченным вопросом):

    verdict      : BLOCKED
    status       : BLOCKED
    submit_count : 0
    reason       : Required question(s) without answer: q_personal
    уведомлений до / после : 0 / 0

То есть заявка встала и **никто об этом не узнал**. Инварианты самой анкеты при
этом честные (в `hh_questionnaire.py` все восемь пунктов про «Submit = 0»). Дыра
не в безопасности, а в наблюдаемости: заявка тихо стоит, и узнать об этом можно
только заглянув в CLI или БД.

`NotificationDispatcher.notify_blocking_question()` — единственное место, которое
пишет уведомление типа `UNANSWERED_QUESTION_BLOCKED` и доставляет его в Telegram:

| метод | вызовов в продакшне | в тестах |
|---|---:|---:|
| `notify_interview` | 1 | 1 |
| `notify_reply_sent` | 1 | 1 |
| `notify_external_questionnaire` | 1 | 1 |
| `notify_test_task` | 1 | 1 |
| `notify_blocking_question` | **0** | 3 |

Причём `save_autonomous_notification()` вызывается **только** из этих пяти
методов.

#### Куда именно его подключать — и почему не в «очевидное» место

Очевидный адрес — `solve_questionnaire_autonomously()`: она и возвращает
`unanswered`, и текст уведомления говорит про «unknown personal question». Но у
неё **тоже ноль вызовов в проде** — только тесты. Их убили вместе, поэтому
пропажу уведомления никто и не заметил: подключить уведомление в мёртвой функции
означало бы не подключить ничего.

Боевой путь другой: `hh_application_runner` → `submit_questionnaire_response()`.
Именно там анкета паркуется, и именно там известна **причина** — чего не хватает
и нужен ли человек вообще. Правка:

- новый `_notify_human_question_blocked(quest, val)` в `hh_questionnaire.py`;
- вызов из ветки провала валидации, перед `return report`.

Уведомляем только о том, на что человек может повлиять: `missing_required`,
`invalid_options` и смена DOM. Не уведомляем про `unknown_questions` — это
опечатка вызывающего кода, а не вопрос, на который человек может ответить;
будить человека ради этого было бы шумом.

Уведомление best-effort: обёрнуто в `try/except` и при падении пишет
`logger.warning`. Предохранитель важнее уведомления — `submit_count` остаётся 0
в любом случае.

#### Мутация

| Откат | Падает |
|---|---|
| убрать вызов `_notify_human_question_blocked` | `test_questionnaire_blocked_by_a_missing_answer_notifies_the_human`, `test_questionnaire_changed_on_the_page_notifies_the_human`, `test_a_broken_notifier_does_not_break_the_stop` |

Встречные проверки при этом остаются зелёными:
`test_unknown_question_id_is_a_caller_bug_and_notifies_nobody` и
`test_a_valid_questionnaire_notifies_nobody` — уведомление не стена.

#### Вторая проверка: доходит ли тип до Telegram

Мои тесты подменяют нотификатор целиком, поэтому маршрутизацию они не покрывают.
А у `deliver_notification()` есть allowlist (`telegram_notifier.py:331`), и тип,
которого там нет, **молча** возвращает `"is routine and not routed to Telegram"` —
уведомление сохранилось бы в БД и никого не побеспокоило. Ровно та же форма
провала, что и №23, только этажом ниже.

Проверено: `UNANSWERED_QUESTION_BLOCKED` в allowlist есть. Плюс добавлен
`test_the_blocked_question_type_is_actually_routed_to_telegram`, который зовёт
**настоящий** нотификатор (сеть блокирует conftest, наружу ничего не уходит) и
падает, если тип убрать из списка. Мутация подтвердила: убрать строку из
allowlist → тест падает.

### 24. Перепроверка сканера сирот: два класса ложных срабатываний и пять мёртвых предохранителей (низкий) — **ИЗМЕРЕНО, НЕ ДЫРА**

Сканер `scan_orphan_guards.py` отдавал 8 кандидатов. Прежде чем записывать их
в «мёртвый код», я открыл каждого руками — и обнаружил, что два из восьми
вообще не сироты, а ошибки самого сканера:

1. **Алиасный импорт.** `from .submission_verifier import verify_submission as
   _verify_submission`, вызов — `_verify_submission(...)` (`cli.py:1083`).
   Сканер индексировал вызовы по имени на месте вызова, поэтому определение
   `verify_submission` оставалось «сиротой», хотя зовут его из CLI.
2. **`@property`.** `evidence.blocked_reasons` (`browser_executor.py:2396`,
   `2559`) — это `ast.Attribute` в контексте `Load`, а не `ast.Call`.
   Свойство используется, но в индексе вызовов его не было.

Оба класса лечились в сканере: таблица `asname -> original` для `ImportFrom`
и индексация `Attribute` не только как `Call`. Результат: **8 -> 6**.

Классификация оставшихся шести (каждый открыт руками):

| Функция | Вердикт |
|---|---|
| `notify_blocking_question()` (`hh_autonomous_agent.py:443`) | **настоящая находка, №23** |
| `verify_review_fingerprint()` (`application_review_gate.py:297`) | мёртвый код |
| `invalidate_on_change()` (`application_review_gate.py:282`) | мёртвый код |
| `run_apply_flow_audit()` (`browser_executor.py:3140`) | ручной диагностический инструмент |
| `verify_submission_in_browser()` (`browser_executor.py:2993`) | мёртвая обёртка; CLI зовёт `submission_verifier.verify_submission` напрямую |
| `get_conversation_audit()` (`db.py:1553`) | мёртвый геттер |

Почему первые две мертвы: `HumanReviewStore.__init__` создаёт **новый пустой**
словарь, а `auto_apply_modes.py` делает `store = HumanReviewStore()` ->
`store.save(gate)` -> выбрасывает store на выходе из функции. Состояние
одобрения не переживает собственный вызов, поэтому свежесть одобрения здесь
не проверяется вовсе. В живом пути её проверяет другое: `hh_submission` /
`hh_submit_policy` сверяют `ApplicationReview.form_fingerprint` из БД с
**пересчитанным** `compute_review_fingerprint(...)` (`hh_submission.py:557`).

#### Побочно измерено: две целые ветки отправки без единого вызывающего в проде

- `run_auto_apply()` (`auto_apply_modes.py:279`) — зовут только тесты.
- `execute_confirmed_submit()` (`hh_application_orchestrator.py:792`) — зовут
  только тесты.

Обе содержат политическое/человеческое одобрение и настоящий клик. Обе
проходят через `preflight_submission()` (то есть стопы спрашивают — №21 их
покрыл). Это не дыра сегодня, но это вторая и третья реализации одного и того
же предохранителя, которые могут разойтись с живой.

#### Проверено и признано НЕ дырой: синтезированный fingerprint

В `hh_submission.py:1436` fingerprint для доказательства перехода в SUBMITTED
имеет фолбэк-константу:

    sub_fp = actual_pkg_fp or (form_snapshot.get("fingerprint") if form_snapshot else "") or "submission_verified_fp"

Оркестратор (`hh_application_orchestrator.py:484`) проверяет только наличие
непустой строки, так что константа его устраивает. Похожий фолбэк есть и в
`execute_confirmed_submit`: `f"confirmed_submit_fp_{application_id}"`.

Но это **не дыра**, и вот почему, по замеру: переход происходит *после* клика
и после пост-проверки. Предполётный гейт `_check_fingerprint_gate`
(`hh_submission.py:557`) устроен fail-closed —

    if not expected_fp or not actual_fp or expected_fp != actual_fp: -> passed=False

— то есть отклик без fingerprint до клика не доходит, и фолбэк недостижим.
Это порча audit-записи, а не fail-open. Записано, чтобы следующий проход не
копал здесь второй раз.

### 25. Анкета докладывала SUBMITTED и писала это в БД вообще без клика (критический) — **ИСПРАВЛЕНО 2026-09-12**

Нашлось при правке №23 — рядом с местом, которое я открыл по делу. В
`submit_questionnaire_response()` шаг 3 был обёрнут в `if evaluate_fn is not None:`,
а блок успеха стоял **снаружи**:

    # 3. If evaluate_fn is provided, perform single click submit
    if evaluate_fn is not None:
        ... клик ...
    # Single click executed successfully
    report.submit_count = 1
    report.click_count = 1
    report.verdict = "SUBMITTED"

`evaluate_fn=None` означает «браузера нет». Замер до правки:

    verdict      : SUBMITTED
    submit_count : 1
    click_count  : 1
    status       : SUBMITTED
    reason       : Questionnaire answers submitted successfully with explicit human confirmation
    статус анкеты в БД: SUBMITTED

`click_count=1` при полном отсутствии клика — не погрешность, а выдуманное
доказательство. Дальше срабатывает one-shot инвариант («already submitted»), и
вакансия **навсегда** перестаёт быть отправляемой: настоящий отклик уже не пройдёт.

Достижимо ли: да. `hh_application_runner` пытается добыть исполнителя сам
(строки 306-324) и при неудаче оставляет `evaluate_fn=None`; CLI зовёт
`run_next_application(..., evaluate_fn=None, ...)` напрямую. А раннер считает
успех по `q_res.verdict in ("SUBMITTED", ...) or q_res.submit_count > 0` — то есть
докладывает `real_hh_submit`.

Правка: без исполнителя отправки нет. Ранний `return` со статусом
`READY_TO_SUBMIT` — ответы валидны и подтверждены, но не отправлены; `verdict`
остаётся `BLOCKED`, `submit_count` и `click_count` — 0. Это зеркалит
существующую ветку `confirm_submit=False`, где сделано ровно так же.

Заодно закрыт родственный случай: `if not res.get("ok")` падало с
`AttributeError`, если страница вернула не словарь; теперь `not isinstance(res, dict)`
проверяется явно, и причина отказа попадает в `reason`.

| Откат | Падает |
|---|---|
| вернуть старую форму (`if evaluate_fn is not None` + безусловный успех) | `test_questionnaire_without_an_executor_never_reports_submitted`, `test_a_missing_executor_does_not_brick_the_one_shot_invariant` |

Встречные проверки остаются зелёными:
`test_questionnaire_still_submits_when_the_click_happened` (с рабочим исполнителем
отправка проходит) и `test_questionnaire_refuses_submitted_when_the_page_says_no_click`.

## E2E-проба: `tools/e2e_pipeline_probe.py`

Зелёные тесты — это не «работает». Проба гонит настоящий пайплайн по
настоящему браузеру и ничего не отправляет. Проверяет по порядку: резолвер
выбрал живой браузер → Playwright прицепился по CDP, а не ушёл в headless →
профиль залогинен в hh.ru → живая вакансия открылась и не помечена blocked →
снапшот чистый (`error=False`, `cdp_fallback=False`) → гейт пропускает чистое
чтение и блокирует fallback-чтение (негативный контроль).

Запуск: `.venv/Scripts/python.exe tools/e2e_pipeline_probe.py`, exit 0 = всё
прошло.

Кандидаты берутся с фильтром «ещё не отвечали» (`application_submissions` +
`hh_applications`). Без фильтра проба выбирала вакансию с уже отправленным
откликом и получала вырожденную картину: `questions=0`, `controls=0`,
никакой кнопки отклика — всё зелёное, но проверяемый путь пустой. На свежей
вакансии та же проба даёт `application_type=screening_questions` и **6
вопросов**. Тот же урок, что и с `apply_link`: «зелёно» легко получить на
пути, по которому продакшн не ходит.

### Регрессии на №11-14 проверены мутациями

Тест, который нельзя сломать, — декор. Каждую правку откатывали и смотрели,
что именно падает:

| Откат | Падает | Встречная проверка |
|---|---|---|
| №11: вернуть поиск `captcha` по сырому HTML | `test_blocked_check_ignores_i18n_captcha_string`, `test_blocked_check_flags_visible_access_denied_only` | реальный `data-qa="captcha`, Cloudflare и 404 по-прежнему блокируют → фикс не стал fail-open |
| №12: вернуть `urllib.request.urlopen` | `test_cdp_adapter_open_uses_proxy_free_opener` | — |
| №13: таймаут снова считать «мёртв» | `test_resolve_cdp_url_does_not_hop_on_slow_chrome` | `test_resolve_cdp_url_logs_browseros_swap` проходит → честный DEAD всё ещё прыгает |
| №14: вернуть frozen-константу `os.getenv("HH_CDP_URL", ...)` | `test_cli_cdp_default_follows_the_resolver`, `test_resolve_hh_evaluate_uses_the_resolved_endpoint` | оба падают именно на `CDP_URL`, а не на `HH_CDP_URL` → ловят именно расхождение |
| №14: `CDPBrowserAdapter(DEFAULT_HH_CDP_URL)` обратно | `test_cdp_adapter_is_built_from_the_resolver` | — |
| №14: вотчер снова импортирует `_DEFAULT_HH_CDP_URL` | `test_nothing_reads_the_frozen_cdp_constant` | — |
| №14: модуль читает `HH_CDP_URL` сам | `test_no_second_copy_of_cdp_resolution` | комментарий с этим же текстом гард **не** задевает → он по AST |

Плюс `test_probe_cdp_distinguishes_timeout_from_dead` фиксирует, что три
состояния `probe_cdp()` действительно различимы, а `is_cdp_reachable()` при
этом честно сворачивает их в bool.

### Чего делать НЕ надо

- Не включать BLE001 как error в CI до шагов 1-2 — 369 находок разом
  парализуют любую правку.
- Не автофиксить. У ruff нет фикса для BLE001, и слава богу: правильный ответ
  зависит от того, fail-open это или fail-closed, а это машина не определит.
