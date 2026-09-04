# Журнал устранения находок аудита (Audit Remediation Log)

**Дата старта:** 2026-09-04  
**Ветка:** `audit-remediation`  
**Базовый коммит:** `2de52c8` (`wip: stabilization changes from working tree`)

---

## Фаза 0. Подготовка и фиксация Baseline

### 0.1. Статус рабочей копии перед началом
- Зафиксированы и исследованы изменения в файлах:
  - `PROJECT_STATE.md`, `README.md`, `ai_assistant/cli.py`, `ai_assistant/db.py`, `ai_assistant/runner.py`, `tests/test_stage83_production_operations.py` (стабилизация Stage 92: прерыватель цепи `ConsecutiveFailureTracker`, persistent circuit breaker, код выхода 5, тесты 15–18);
  - `ai_assistant/external_form_solver.py`, `ai_assistant/hh_autonomous_agent.py` (добавлен домен `kakdela.hh.ru`);
  - `docs/competitor_implementation_audit_2026-09-03.md`, `AUDIT_BRIEF.md`, `AUDIT_BRIEF.txt` (материалы аудита).
- Все изменения признаны осмысленными и закоммичены в исходную ветку перед ответвлением:
  `wip: stabilization changes from working tree` (`2de52c8`).
- Создана новая рабочая ветка: `audit-remediation`.

### 0.2. Baseline-метрики
- **Git status:** Чистый (`audit-remediation`).
- **Pytest (полный прогон):** 1 408 passed, 0 failed, 0 errors за 659.74s (10 мин 59 сек).
- **Ruff check (`ai_assistant/`):** 2 773 ошибки:
  - `UP006` (non-pep585): 844
  - `UP045` (non-pep604): 604
  - `BLE001` (blind-except): 342
  - `F401` (unused-import): 225
  - `UP035` (deprecated-import): 153
  - `I001` (unsorted-imports): 138
  - `DTZ003` (datetime-utcnow): 70
  - `S110` (try-except-pass): 68
  - `F811` (redefined-while-unused): 65
  - `LOG015` (root-logger-call): 42
  - `F841` (unused-variable): 36
  - `F821` (undefined-name): 31
  - `F541` (f-string-missing-placeholders): 29
  - `B009` (get-attr-with-constant): 27
  - `SIM102` (collapsible-if): 25
  - Прочие: 75
- **Mypy (`ai_assistant/`):** Found 652 errors in 35 files (checked 66 source files).

---

## Фаза 1. Критично

### 1.1. Утечка токена в коде
- Выполнен поиск всех вхождений строк `8217526633` и `AAHqReznT2DYvTg67zzftLR0iWSbowfb3rg` по репозиторию:
  - `tests/test_stage89_2_external_runtime_persistence.py`: тест `test_secrets_masked_in_status_and_logs` переписан с использованием заведомо фейкового токена `000000000:FAKE_TOKEN_FOR_MASKING_TEST_xxxxxxxx` через `monkeypatch.setenv`. Реальный токен полностью удален.
  - `AUDIT_BRIEF.md` и `AUDIT_BRIEF.txt`: реальный токен заменен на `<REDACTED>`.
  - `PROJECT_STATE.md`: префикс ID заменен на `<REDACTED_BOT_ID>:...`.
- Выполнен поиск по шаблонам секретов (`TELEGRAM_BOT_TOKEN`, `sk-`, `Bearer `): реальных активных секретов в кодовой базе `*.py` не обнаружено.
- **ВНИМАНИЕ (Требует решения человека):** Реальный токен присутствует в истории Git начиная с коммита `f3a08f1`. Владельцу необходимо отозвать и перевыпустить токен в `@BotFather`, после чего при необходимости очистить историю Git (например, через `git-filter-repo` / BFG). Сама git-история в рамках данной работы не переписывалась для сохранения целостности веток.
- Коммит: `fix(security): remove exposed telegram bot token from tests and audit brief` (`ea81c7c`).

### 1.2. Восстановление продакшна (Hermes + целостность БД)
- Выполнена команда `python -m ai_assistant.cli hermes sync`:
  - Восстановлен хук маршрутизации колбэков Hermes (`HERMES_CALLBACK_HOOK_MARKER`) в локальном адаптере Hermes (`%LOCALAPPDATA%\hermes\hermes-agent\plugins\platforms\telegram\adapter.py`).
  - Статус интеграции Hermes перешел в `HEALTHY`.
- Устранено расхождение целостности для синтетической тестовой записи `vacancies_json:71` (`canonical_2c8a3d5c095eecbb`):
  - Применен скрипт `scripts/reconcile_production_state.py --apply`.
  - Статус ревью вакансии с заблокированной браузерной подготовкой переведен из `APPROVED` в `PENDING_REVIEW` с созданием резервной копии базы.
- Проверки:
  - `python -m ai_assistant.cli audit --tracked` -> `HEALTH: PASS` (0 ошибок, 0 предупреждений).
  - `python -m ai_assistant.cli production-health --json` -> `health: HEALTHY`, 0 алертов, 0 сбоев подряд.
- Коммит: `fix(production): sync hermes integration and reconcile legacy audit mismatch` (`cc5979a`).

### 1.3. Ошибки неопределённых имён (F821)
- Выполнен запуск `ruff check ... --select F821`: найдена 31 ошибка в 3 файлах (`ai_assistant/schema.py`, `ai_assistant/browser_executor.py`, `ai_assistant/cli.py`).
- Устранены причины:
  - `ai_assistant/schema.py`: добавлен импорт `Set` из `typing`.
  - `ai_assistant/browser_executor.py`: добавлен импорт `TYPE_CHECKING` и условный импорт `SubmissionVerification` для аннотации возвращаемого типа `verify_submission_in_browser`.
  - `ai_assistant/cli.py`:
    - Добавлен импорт `Callable` из `typing`.
    - Добавлен отсутствовавший логгер модуля `logger = logging.getLogger(__name__)`.
    - Удален недостижимый мертвый блок кода в `submit_vacancy` после `return 1` (ссылался на необъявленные `top`, `generate_queue`, `get_browser_session`, `BrowserStatus`).
    - Удален недостижимый мертвый блок кода в `dashboard_show_canonical` после `return 0` (ссылался на необъявленный `row`).
- Проверки:
  - `ruff check ai_assistant/ integrations/ scripts/ tools/ --select F821` -> 0 ошибок (All checks passed).
- Коммит: `fix(syntax): resolve all undefined names (F821)` (`0c81d65`).

### 1.4. Переопределённые функции и импорты (F811)
- В `ai_assistant/application_queue.py`:
  - Удалена дублирующая устаревшая функция `build_queue_items` (~L472), перетиравшая каноническую дедупликацию.
  - Исправлен баг в канонической версии `build_queue_items` (~L344): точные дубликаты (EXACT, одинаковый нормализованный URL) схлопываются в один элемент очереди, а вероятные дубликаты (PROBABLE) остаются отдельными элементами очереди и не объединяются ошибочно.
  - Очищены повторяющиеся тройные импорты в `_select_representative`.
  - Добавлены 3 новых модульных теста в `tests/test_application_queue.py`:
    - `test_exact_duplicates_collapse_in_queue`: точные дубликаты схлопываются в один QueueItem;
    - `test_probable_duplicates_stay_separate_in_queue`: вероятные дубликаты остаются отдельными элементами;
    - `test_unmapped_canonical_passes_through_in_queue`: новая немаппированная вакансия генерирует канонический ID и проходит в очередь.
- В `ai_assistant/cli.py`:
  - Удален дубликат `review_reject` (сохранен вариант с обработкой `Exception`).
  - Удален полный дубликат `queue_duplicates`.
- Очищены повторные импорты в файлах `application_dashboard.py`, `application_queue.py`, `cli.py`, `hh_autonomous_agent.py`, `hh_message_watcher.py`, `ui/app.py`.
- Проверки:
  - `ruff check ai_assistant/ integrations/ scripts/ tools/ --select F811` -> 0 ошибок (All checks passed).
  - `pytest tests/test_application_queue.py` -> 17 passed.
- Коммит: `fix(cleanup): remove redefined functions and duplicate imports (F811)` (`ea64534`).

### 1.5. Ревизия submission gates (безопасность отправки откликов)
- **Ответы на 4 доменных вопроса аудита:**
  1. *Должен ли `GATE_SUBMIT_ALLOWED` проверять флаг конфигурации `SUBMIT_ALLOWED`?*  
     **Да.** `SUBMIT_ALLOWED` (`os.getenv("SUBMIT_ALLOWED", "false")` / `config.SUBMIT_ALLOWED`) является главным аппаратным предохранителем (kill-switch). При `SUBMIT_ALLOWED=false` отправка физически блокируется, если не передан флаг `dry_run=True`.
  2. *Почему `GATE_NOT_ALREADY_APPLIED` должен проверять белый список статусов, а не просто `status != 'APPLIED'`?*  
     **Отказ от черного списка.** Проверка только `!= 'APPLIED'` критически небезопасна, так как пропускает вакансии со статусами `SUBMITTED`, `AMBIGUOUS_POST_SUBMIT`, `VERIFIED`, `INTERVIEW`, `REJECTED`, `WITHDRAWN`. Реализован строгий белый список разрешенных предварительных статусов: `ALLOWED_UNSUBMITTED_STATUSES = {"DISCOVERED", "ANALYZED", "READY_TO_APPLY"}`. Дополнительно проверяется отсутствие успешных или неоднозначных записей в таблице `submissions` (`SUBMITTED`, `CONFIRMED`, `AMBIGUOUS_POST_SUBMIT`, `VERIFIED`, `FAILED`).
  3. *Почему `GATE_URL_DOMAIN` недопустимо реализовывать через подстроку `hh.ru in url`?*  
     **Защита от SSRF / фишинга.** Поиск подстроки пропускает вредоносные домены вроде `https://evil-hh.ru`, `https://hh.ru.attacker.com` или `https://google.com/?hh.ru`. Реализована строгая валидация через `urllib.parse.urlparse`, гарантирующая `host == "hh.ru"` или `host.endswith(".hh.ru")`.
  4. *Почему `GATE_VACANCY_MATCH` обязан падать, если `source_job_id` отсутствует или не числовой?*  
     **Принцип fail-closed.** Пропуск проверки при отсутствии ID создает риск отправки данных в случайно открытую вкладку браузера. Если `source_job_id` отсутствует или не содержит цифр, гейт немедленно возвращает `passed=False` с ошибкой `GATE_VACANCY_MATCH`.

- **Реализованные изменения:**
  - `ai_assistant/config.py`: добавлен предохранитель `SUBMIT_ALLOWED = os.getenv("SUBMIT_ALLOWED", "false").strip().lower() in ("1", "true", "yes")`.
  - `ai_assistant/hh_submission.py`:
    - `_parse_vacancy_id`: расширен парсинг как query-параметра `?vacancyId=...`, так и пути `/vacancy/(\d+)`.
    - `preflight_submission`: добавлена строгая проверка хоста через `urlparse` и fail-closed при невалидном `expected_vid`.
    - Добавлены `GateName`, `GateCheckResult`, класс `HHSubmissionGates` с методом `check_all_gates`, реализующим все 11 строгих гейтов:
      1. `GATE_SUBMIT_ALLOWED` (kill-switch);
      2. `GATE_REVIEW_APPROVED` (ревью в БД строго в статусе `APPROVED`);
      3. `GATE_FINGERPRINT_MATCH` (совпадение хэша DOM-формы с ревью);
      4. `GATE_URL_DOMAIN` (строгий домен `hh.ru` / `*.hh.ru`);
      5. `GATE_VACANCY_MATCH` (числовой ID вакансии из URL совпадает с целевой вакансией);
      6. `GATE_PROFILE_LOADED` (профиль кандидата загружен и не пуст);
      7. `GATE_COVER_LETTER_READY` (сопроводительное письмо не пустое и $\ge 10$ символов);
      8. `GATE_NO_UNKNOWN_QUESTIONS` (отсутствуют вопросы скрининга, требующие ручного ответа);
      9. `GATE_NOT_ALREADY_APPLIED` (белый список статусов трекинга + отсутствие записей в `submissions`);
      10. `GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT` (отсутствие попытки в текущей сессии и статуса `SUBMITTING` в БД);
      11. `GATE_HUMAN_CONFIRMED` (явное подтверждение человеком `--confirm-submit` вне dry-run).
  - `ai_assistant/application_review.py`: в модель `ApplicationReview` добавлены поля `form_fingerprint`, `fingerprint`, `review_id`.
  - `tests/test_hh_submission_gates.py`: написан полный набор из 11 изолированных модульных тестов для каждого гейта и сценариев блокировки.
  - Обновлена документация: `README.md` (раздел 4 и таблица переменных окружения) и `PROJECT_STATE.md` (раздел 9).
- **Проверки:**
  - `pytest tests/test_hh_submission_gates.py` -> 11 passed (100%).
  - `pytest tests/test_stage20i_submission.py` -> 20 passed (100%).
  - `pytest tests/test_submission_recovery.py tests/test_submission_verifier.py` -> 60 passed (100%).



