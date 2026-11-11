# T7.28 дефект A - запись прогона (2026-09-30)

## Что сделано и проверено

Верифицированная ревизия `ai_assistant/cli.py`:
  sha256  dcd39c11d71bb813c283b37808fa0b6376368e9cdfb9aefea183f4091381da0a
  bytes   315217
  снимок  tools/.t728/cli.py.defect_a_verified.py
  до      tools/.t728/cli.py.before_T7.28A.py
          sha256 13fdb64641bdfbe458000b75a41dc21859044813520bbe83e6db948ff14afe47

Структура переводов строк сохранена: `CR CR LF` 6437 -> 6557 (+120 строк,
ни одна существующая не переписана), `LF` 6589 -> 6686.

## Вердикты (из junit и из живого прогона, не из $?)

PASS  tests/test_t728_message_date.py         51 passed
PASS  tests/test_stage30d_diagnose.py         80 passed
PASS  оба файла вместе                       131 passed
PASS  живой triage (headless, SUBMIT_ALLOWED=false)
      -> {"status": "READ-ONLY", "conversation_count": 0, "items": [], "errors": []}
      Ноль доказан: гейт прошёл, сессия подтверждена, переписка не открыта.
PASS  живой негативный контроль вердикта
      authenticated=True                  -> rc=0 READ-ONLY
      authenticated=False, verified=True  -> rc=1 BLOCKED ("not authenticated ... log in")
      authenticated=False, verified=False -> rc=1 BLOCKED ("could not verify ... retry")
PASS  негативный контроль «убрать гейт»          -> 4 теста краснеют
PASS  негативный контроль «проба в chatik-фрейм» -> 2 теста краснеют
PASS  ruff: дельта по правилам vs снимок = 0
      (27 I001, 13 LOG015, 2 F841, 1 PLR2044, 1 S110, 1 S112 - до и после одинаково)
PASS  ссылки в docs/ble001_triage.md: мой вклад = 0
      out of range: 0; flagged for reading: 75 (порог теста 18, был красным и до меня).
      Из 75 на cli.py ссылаются 13. Замер
      `tools/.t728/attribute_line_refs.py` (сравнение со снимком S0, без подмены
      файлов): attributable to this edit = 0, already stale before it = 12,
      ok-both = 1. Переводить адреса не нужно.

ПОЛНЫЙ ПРОГОН tests/  (tools/.t728/junit_full_a_run3.xml)
         tests=3268  failures=4  errors=0  skipped=0  time=2311.562 с (38м32с)

         Все 4 падения — из списка «не трогать», ни одно не связано с дефектом A:

         FAIL tests.test_report_line_refs::test_the_report_has_no_new_symbol_mismatches
              "the report grew new symbol mismatches ... flagged for reading: 75"
              Порог теста 18. Красный и на чистом HEAD (проверено отдельно),
              к правке отношения не имеет — см. атрибуцию ссылок выше
              (мой вклад = 0).

         FAIL tests.test_t78_search_only::{test_search_only_cycle_writes_nothing_outward,
              test_an_unknown_mode_is_treated_as_search_only,
              test_an_existing_approval_survives_a_search_only_cycle}
              Все три падают одинаково: assert res.status == "SEARCH_ONLY",
              получено 'ERROR'. Падают внутри run_autonomous_cycle, к
              hh_message_triage не относятся; красные с T7.26.2, свойство
              покрыто в test_t726_search_only_headless. Не чинились сознательно.

         Прогоны №1 и №2 вердикта не дали (убит по таймауту / PermissionError на
         занятом junit-файле + 128 ошибок сбора) => UNKNOWN, не «зелено».
         Прогон №3: на 35+ минуте замер CPU дал 5.6 с CPU за 6 с (94 %) на pid
         pytest => это не зависание, набор просто долгий. junit пишется только в
         конце сессии, поэтому до его появления вердикта нет.


## Живая находка (главное в этом дефекте)

Пробу сессии нельзя ставить в chatik-iframe. Один и тот же сеанс:

  gate(_resolve_hh_evaluate())     -> authenticated=True,  verified=True
  gate(_resolve_chatik_evaluate()) -> authenticated=False, verified=False
      reason: "the page returned something unrelated to the session check:
               {'error': 'chatik frame not found', ...}"

Причина: chatik-iframe не содержит навигации профиля
(`mainmenu_applicantProfile`, `mainmenu_messages`, `a[href*="/applicant/..."]`),
а при неоткрытой переписке отдаёт сторожок `chatik frame not found`.
Первая версия правки поставила гейт сюда и отказала ЗДОРОВОЙ сессии.
Watcher так и не делает: `_resolve_hh_evaluate` для проверки,
`_resolve_chatik_evaluate` для чтения.

## Что осталось

1. Полный `tests/` — вердикт получен (см. выше), 4 падения разобраны и отнесены
   к списку «не трогать». Вопрос закрыт.
2. Коммит не сделан. `git push` запрещён.
3. `cli.py` переписывается параллельной сессией: за сессию файл менялся из-под
   правки не менее трёх раз. Проверенная ревизия `dcd39c11…` (315217 байт)
   подтверждена хешем уже после прогона; перед любой следующей работой с файлом
   сверять хеш заново.

---

## Инцидент: переводы строк (моя ошибка, найдена и исправлена)

**Что произошло.** Скрипты негативного контроля делали
`pathlib.Path(...).read_text()` -> `... .write_text()`. На `cli.py` это
разрушительно: файл хранит 6437 строк как `CR CR LF` и 152 как `CR LF`, а
текстовый режим читает `CR CR LF` как ДВА перевода строки и пишет обратно
`CR LF CR LF`. Один круг «прочитать-записать» вставил в файл **6508 фантомных
пустых строк**:

```
              CR CR LF   CR LF alone   LF      bytes
до                6437           152   6589     310145
после инцидента      0         13097  13097     321482
```

Тесты при этом оставались зелёными (пустые строки Python безразличны), AST
парсился, живой прогон работал — то есть **зелёный набор не поймал порчу**.
Поймал только прямой замер байтов.

**Почему это важно, а не косметика.** Проект держит два счётчика строк, и они
расходятся именно на таких файлах: `grep`/`wc` считают по `LF`, а
`splitlines()`/редакторы — ещё и по голому `CR`. `docs/ble001_triage.md`
объявляет систему счёта для `cli.py`; после порчи объявленные адреса стали
означать другое место. Плюс это правка на 6508 строк в файле под FREEZE.

**Как исправлено.** Файл восстановлен из чистого снимка и правка наложена заново.
В патчер добавлен **структурный инвариант**: вставляемые строки получают тот же
терминатор (`CR CR LF`), а после сборки проверяется, что число `CR CR LF` выросло
ровно на число добавленных строк. Если хоть одна существующая строка переписана —
счётчик упадёт, и патчер откажется писать (`return 5`).

```
ok  terminator preserved: CR CR LF 6437 -> 6557 (+120 lines added, none rewritten)
```

**Грабли.** `read_text`/`write_text` на `cli.py` запрещены. Только
`read_bytes`/`write_bytes` или `shutil.copy2`. Это ровно тот случай, о котором
уже есть правило в памяти проекта («голые `\r` — только байтами»), — и я на него
наступил, потому что писал служебный скрипт, а не правку.

