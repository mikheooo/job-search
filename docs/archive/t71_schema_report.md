# T7.1-доп — граница транзакции в `ensure_schema`

Дата отчёта: 2026-09-24 (UTC). Повод: T7.1 не смог довести полную регрессию —
инициализация свежей SQLite-базы занимала секунды вместо миллисекунд.

Задача: подтвердить причину измерением, убрать её минимальной правкой, **не меняя**
состав таблиц, индексов, ограничений, порядок миграций и боевые данные.

---

## 0. Коротко

| что | результат |
| --- | --- |
| прежний полный прогон | **умер, не завершился**: 426 тестов (14%) за 51 м 45 с, `--junitxml` не оставлен ⇒ вердикта нет |
| подтверждённая причина | `apply_schema` — **90 DDL-операторов, и каждый фиксировался своей транзакцией**; sqlite3 не открывает транзакцию под DDL |
| journal-циклов на свежую базу | **88 → 2** |
| время `ensure_schema()` на свежей базе | **3,08 с → 0,09 с** (среднее из 3 прогонов) |
| время самого `apply_schema` | **2,87 с → 0,03 с** |
| что изменено | `ai_assistant/db.py::_ensure_schema_on` — вызов одного помощника; сам помощник `_apply_schema_once` в конце файла: `BEGIN`, отметка о схеме в ту же транзакцию и до DDL, `commit()` |
| DDL, схема, миграции | **не тронуты**: `schema_fingerprint()` остался `8f20bd75835d6658` |
| новые тесты | 14 passed (`tests/test_db_schema_transaction.py`) |
| соседние тесты (все файлы, трогающие `init_db`) | 272 passed in 45,90s |
| ратчет ссылок `docs/ble001_triage.md` | **24 — ровно базис**, не хуже; правка сделана несдвигающей строки (§2) |
| **полная регрессия** | **2448 тестов, 1 падение (известный ратчет), 0 ошибок, 9 м 38 с** — см. §5 |
| боевая `state.db` | **не открывалась на запись** (mtime не менялся, `-wal`/`-journal` рядом нет) |

---

## 1. Аудит: что именно тормозило

### 1.1 Состав `apply_schema`

```
DDL всего: 90  (CREATE 88, ALTER 2)
  первый:   CREATE TABLE IF NOT EXISTS processed_vacancies (...)
  последний: CREATE INDEX IF NOT EXISTS idx_tg_feedback_created ON telegram_feedback_records(created_at)
  PRAGMA внутри: 0
  executescript внутри: 0
```

Единственный вызов в проде — `ai_assistant/db.py:100`, внутри `_ensure_schema_on`.
Все 626 вызовов `init_db()` и прямой `ensure_schema()` идут через него, поэтому
правка одной функции накрывает всё.

### 1.2 Счётчик транзакций — это journal-файл, а не `schema_version`

Первый инструмент оказался негодным и это стоит зафиксировать: **`PRAGMA
schema_version` считает изменения схемы, а не коммиты** — он дал 86 в обоих режимах.
Настоящий счётчик — появление `<база>-journal` (наблюдатель за каталогом, опрос 1 мс):

```
autocommit (как было): journal создан 86 раз, удалён 86 раз, время 2.87s
один BEGIN..COMMIT:    journal создан  1 раз,  удалён  1 раз,  время 0.03s
режим журнала: delete
```

### 1.3 Ошибка на промежуточном DDL

Сбой вбрасывался на 50-й DDL-оператор через прокси соединения:

```
с BEGIN (как после правки): DDL дошло до 50, осталось таблиц 0,  индексов 0
без транзакции (как было):  DDL дошло до 50, осталось таблиц 14, индексов 29
```

То есть до правки падение в середине оставляло **полусобранную схему**, и это не
теория, а замер. Отметка о схеме в обоих случаях не появлялась — `apply_schema`
до неё не доходил.

### 1.4 Повторный вызов

```
повторный apply_schema на готовой базе: 0.007s, journal создан 0 раз
```

`CREATE TABLE IF NOT EXISTS`, который ничего не меняет, не пишет страниц, поэтому
journal не создаётся вовсе. Стоимость пропорциональна числу **реальных** изменений,
а не числу операторов.

---

## 2. Что изменено

Одна функция — `ai_assistant/db.py::_ensure_schema_on` — и один новый помощник в
конце того же файла. Состав DDL не тронут, поэтому `schema_fingerprint()` (хеш
исходного текста `apply_schema`) остался прежним и боевые базы не увидят
«изменения схемы».

Место вызова (было 6 строк — осталось 6):

```diff
-    apply_schema(conn)
-    conn.execute(
-        "INSERT OR REPLACE INTO _schema_meta (id, fingerprint, applied_at) VALUES (1, ?, ?)",
-        (fingerprint, datetime.now(timezone.utc).isoformat()),
-    )
-    conn.commit()
+    # Одна транзакция на всю схему, а не 90 отдельных: почему — в _apply_schema_once.
+    try:
+        _apply_schema_once(conn, fingerprint)
+    except Exception:
+        conn.rollback()
+        raise
```

Сам помощник `_apply_schema_once(conn, fingerprint)` добавлен **в конец файла**:
`BEGIN` (если транзакция ещё не открыта), затем отметка о схеме, затем
`apply_schema(conn)`, затем `commit()`.

Четыре решения, каждое с причиной:

1. **`BEGIN` вместо autocommit.** Явная граница не зависит от того, как Python
   решит обращаться с транзакциями.
2. **Отметка о схеме — в ту же транзакцию и до DDL.** Так её нельзя увидеть без
   самой схемы, а падение в середине откатывает и то, и другое. Если бы отметка
   писалась после, между двумя коммитами существовало бы окно, где схема есть,
   а отметки нет (безопасно, но лишний journal-цикл).
3. **`BEGIN` оставлен, хотя выглядит избыточно.** Мутация это показала: если
   убрать `BEGIN`, но оставить `INSERT` перед DDL, транзакцию всё равно откроет
   сам `sqlite3` — неявный `BEGIN` перед DML. Все 14 тестов такую мутацию
   **не поймали**. Это поведение исчезает при `isolation_level = None`, поэтому
   полагаться на него нельзя, и явный `BEGIN` остаётся.
4. **Помощник вынесен в конец файла — и это не косметика.** `docs/ble001_triage.md`
   ссылается на `db.py` **по номерам строк**: 93 ссылки, из них 91 — на строки
   ниже 100-й. Любая вставка выше них сдвигает все адреса и раздувает ратчет
   ссылок. Замерено:

   ```
   db.py из HEAD, 3041 строка          -> flagged for reading: 24  out of range: 0
   db.py с правкой в середине, 3064    -> flagged for reading: 55  out of range: 0
   db.py с правкой, 3041 + хвост       -> flagged for reading: 24  out of range: 0
   ```

   То есть правка «на месте» подняла бы известный красный ратчет с 24 до 55 —
   то есть сделала бы хуже метрику, которую в этой задаче просили не трогать.
   Вынесенный в хвост помощник не двигает ни одну существующую строку: все
   3041 исходных строк остались на своих местах, а ратчет — на своём базисе 24.
   `getattr(conn, "in_transaction", False)` вместо прямого обращения — по той же
   логике «не создавать новых требований»: часть тестов подсовывает неполную
   заглушку соединения, и падать на ней нельзя.

---

## 3. Замеры до и после

Свежая база, три прогона на вариант, один и тот же диск и процесс:

| вариант | время, с | journal-циклов |
| --- | --- | --- |
| до (как было) | 3,32 / 3,31 / 2,59 — среднее **3,08** | 88 |
| после (одна транзакция) | 0,13 / 0,08 / 0,06 — среднее **0,09** | 2 |

**Ускорение 34,3× по среднему** (по отдельным прогонам 25×…43×). На самом
`apply_schema`, без обвязки `_schema_meta`, замер дал 2,87 с → 0,03 с, то есть 90×.

Обещанных заранее 71,5× здесь нет и быть не могло: та цифра была получена на
28-табличной синтетике, а у реальной схемы 90 операторов, из которых 86 реально
меняют базу, плюс два фиксированных journal-цикла на `_schema_meta`.

Сопутствующее:

```
настоящий db.ensure_schema(): первый 0,08 с, повторный 0,003 с
устаревший fingerprint -> переприменение: 0,03 с, отметка восстановлена: True
```

---

## 4. Тесты

Новый файл `tests/test_db_schema_transaction.py`, 14 тестов, все проходят.
Оракул «как было» — локальная копия прежней реализации; схема сравнивается с
ней и со слепком `tests/db_schema_reference.json` (84 объекта: 26 таблиц,
58 индексов), снятым **до** правки.

| тест | что фиксирует |
| --- | --- |
| `test_ensure_schema_creates_full_schema_from_scratch` | создание с нуля, ключевые таблицы, отметка о схеме |
| `test_schema_matches_the_legacy_path` | схема нового пути == схема прежнего пути |
| `test_schema_dump_matches_stored_reference` | схема == слепок, снятый до правки |
| `test_second_init_is_a_noop` | повторная инициализация не переписывает отметку |
| `test_second_init_writes_nothing` | на готовой базе journal не создаётся вообще |
| `test_stale_fingerprint_reapplies_schema` | устаревшая отметка ведёт к переприменению |
| `test_failure_mid_ddl_leaves_no_partial_schema` | сбой на 50-м DDL: 0 таблиц, 0 индексов, нет отметки |
| `test_failure_before_any_ddl_leaves_empty_db` | сбой на 1-м DDL |
| `test_failed_init_leaves_no_write_lock` | после сбоя база не заперта, другая связь пишет |
| `test_recovery_after_failed_init_gives_full_schema` | докатка после сбоя даёт полную схему |
| `test_ddl_is_one_transaction_not_one_per_statement` | journal ≤ 2, а не по разу на оператор |
| `test_migration_columns_are_added_to_old_table` | `ALTER TABLE` доезжает внутри общей транзакции |
| `test_apply_schema_has_no_transaction_managing_constructs` | в `apply_schema` нет `PRAGMA`/`executescript` |
| `test_fingerprint_pin` | отпечаток DDL не изменился |

### Мутации

| мутация | результат |
| --- | --- |
| `started_here = False` (убрать только `BEGIN`, `INSERT` оставить перед DDL) | **не поймана** — транзакцию открывает неявный `BEGIN` sqlite3 перед DML; отсюда решение (3) в §2 |
| вернуть прежний порядок: без `BEGIN` и `INSERT` после DDL | **поймана**: `test_failure_mid_ddl_leaves_no_partial_schema` (осталась часть схемы) и `test_ddl_is_one_transaction_not_one_per_statement` (journal 24 раза вместо ≤2) |

Наблюдатель journal-файла может **недосчитать** (журнал живёт доли миллисекунды),
но не может пересчитать — поэтому в тесте проверяется только верхняя граница.

### Соседние тесты

Все файлы, трогающие `init_db`/`ensure_schema` (20 файлов: application_*,
deep_analysis, eligibility, hh_*, job_search_mvp, reclassification, stage16,
stage17d, stage30c_*, llm_failure_semantics, phase2_1, browser_executor):

```
272 passed in 45.90s
```

Для сравнения: в T7.1 те же по природе тесты шли со скоростью 7–10 тестов в
минуту (`19 passed in 193.93s`, `51 passed in 347.20s`). Теперь — ~356 в минуту.

### Что правка сломала и как это нашлось

Первая версия правки читала `conn.in_transaction` напрямую. Полная регрессия на
ней дала **3 падения** — и это ровно тот случай, когда полный прогон окупается:

```
FAILURE tests/test_ble001_fail_closed.py::test_health_is_unknown_when_duplicate_check_itself_fails
FAILURE tests/test_ble001_fail_closed.py::test_health_is_unknown_when_record_counts_cannot_be_read
        Database inaccessible: '_HealthProbeConn' object has no attribute 'in_transaction'
FAILURE tests/test_ble001_fail_closed.py::test_an_inconclusive_probe_is_not_downgraded_to_degraded
```

Причина: часть тестов подсовывает вместо соединения **намеренно неполную
заглушку** — `_BrokenConn` (строка 251) и `_HealthProbeConn` (строка 4583) в
`test_ble001_fail_closed.py`. У них нет ни `in_transaction`, ни `__getattr__`.
Это не небрежность тестов, а их суть: они проверяют, что сломанная база не
выглядит здоровой. Ещё два двойника (`_FailingDigestConn`, `CountConn`)
пробрасывают атрибуты через `__getattr__` и правку пережили.

Исправлено в коде, а не в тестах:

```diff
-    started_here = not conn.in_transaction
+    started_here = not getattr(conn, "in_transaction", False)
```

Так правка перестаёт требовать от соединения новый атрибут. **Тесты безопасности
не тронуты** — подгонять их под свою правку было бы подменой проверки.

После этого `tests/test_ble001_fail_closed.py` — **393 passed in 40.50s**.

### Профильные тесты T7

```
tests/test_candidate_job_brief.py      135 passed
tests/test_db_schema_transaction.py     14 passed
uvx ruff@0.16.6 check tests/test_db_schema_transaction.py ai_assistant/db.py
→ по новому файлу All checks passed!; в db.py ни одна находка не указывает
  на строки 96–125, то есть правка ничего не добавила
```

---

## 5. Полная регрессия

Запускалась дважды после правки, отдельно, в фоне со сторожевым файлом, с `--junitxml`.

### 5.1 Прогон на первой версии правки — 3 падения, и это окупилось

```
.pytest_t71_schema_full.xml
tests=2448  errors=0  failures=4  time=551.185s  (9 м 11 с)
```

| падение | причина |
| --- | --- |
| `test_ble001_fail_closed.py::test_health_is_unknown_when_duplicate_check_itself_fails` | `conn.in_transaction` на неполной заглушке соединения |
| `test_ble001_fail_closed.py::test_health_is_unknown_when_record_counts_cannot_be_read` | то же, в тексте: `'_HealthProbeConn' object has no attribute 'in_transaction'` |
| `test_ble001_fail_closed.py::test_an_inconclusive_probe_is_not_downgraded_to_degraded` | то же |
| `test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches` | известное, см. §5.2 |

Все три «своих» падения — один корень, и он исправлен в коде (§4). После правки
`tests/test_ble001_fail_closed.py` — 393 passed.

### 5.2 Итоговый прогон — вердикт есть

```
.pytest_t71_schema_final2.xml
tests=2448  errors=0  failures=1  skipped=0  time=577.984s  (9 м 38 с)
```

**Единственное падение — известный ратчет ссылок, и он ровно на своём базисе:**

```
tests/test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches
→ assert 24 <= 18      ("flagged for reading: 24", "out of range: 0")
```

Он был красным до этой задачи, остался красным после, **порог не поднят**, ссылки
не переписаны. Правка специально сделана так, чтобы не сдвинуть его базис (§2,
решение 4): первая версия подняла ратчет с 24 до 55 просто за счёт +23 строк в
середине `db.py`.

### 5.3 Что это дало по времени

| | тестов | время | тестов/мин |
| --- | --- | --- | --- |
| сломанное состояние (T7.1, прогон 14:42) | 426 за 51 м 45 с — **умер** | — | 8,2 |
| исторический ориентир (T6.1, `.pytest_ckb_full_01.xml`) | 1994 | 21 м 25 с | 93 |
| **после правки** | **2448** | **9 м 38 с** | **254** |

`EXIT=1` в обоих прогонах — это сторож массовых удалений, заблокировавший уборку
`%TEMP%\pytest-of-Misha` (`SAFE_DELETE_BULK_CONFIRM_REQUIRED`), а не падение
тестов. Junit при этом записан целиком, поэтому вердикт есть.

Улики не удалялись: `.pytest_t71_schema_full.xml`, `.pytest_t71_schema_final.xml`,
`.pytest_t71_schema_final2.xml`, `.pytest_t71_schema_ble001.xml`,
`.pytest_t71_schema_neighbours.xml`, `.pytest_t71_schema_recheck.xml`.

---

## 6. Ограничения

1. **Правка снимает 86 journal-циклов, а не стоимость удаления файла.** Удаление
   в этом окружении всё ещё стоит ~70 мс (внутри репозитория — ~1,5 с). Свежая
   база теперь стоит 2 удаления вместо 88, но остальная файловая работа тестов
   по-прежнему платит эту цену.
2. **Стоимость `PRAGMA schema_version` как счётчика транзакций** — не работает;
   не использовать для таких замеров (см. §1.2).
3. **`BEGIN` не спасает от сбоя между коммитом `apply_schema` и своим `commit()`.**
   Такого окна сейчас нет (внутренний `commit()` — последний оператор), но если
   в `apply_schema` добавят код после него, окно появится. Тест
   `test_apply_schema_has_no_transaction_managing_constructs` это заметит.
4. **Каталоги pytest и TEMP не чистились** — по условию задачи. `C:` занят на
   98% (свободно ~11 ГБ), в `%TEMP%\pytest-of-Misha` накопилось 261 каталог и
   12 817 файлов от убитых прогонов. Предлагаемая очистка и её обоснование — в
   ответе, а не здесь, и без подтверждения она не делается.
5. Схема, порядок миграций, боевые данные, фильтры, правила отправки, резюме,
   KB и профиль не менялись.

---

## 7. Заморозка

sha256 (первые 16 знаков):

```
state.db                        06be58e839c72f51   26 071 040 байт, mtime 2026-09-24 10:01 (до задачи)
candidate_kb.json               fafcedb07c8016e9
resume.md                       7ae200cdd7251d3c
artifacts/master_resume.md      177f574be8446162
artifacts/master_resume.ru.md   2c4c76ff9c6fed07
tests/db_schema_reference.json  (слепок схемы, снят до правки)
```

Рядом с боевой базой нет `state.db-wal`, `state.db-journal` и `state.db-shm` —
она не открывалась на запись ни разу за задачу. Все замеры и тесты шли на
временных файлах, путь к которым подставлялся через `config.DB_FILE`.
