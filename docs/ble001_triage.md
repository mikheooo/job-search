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

### Чего делать НЕ надо

- Не включать BLE001 как error в CI до шагов 1-2 — 369 находок разом
  парализуют любую правку.
- Не автофиксить. У ruff нет фикса для BLE001, и слава богу: правильный ответ
  зависит от того, fail-open это или fail-closed, а это машина не определит.
