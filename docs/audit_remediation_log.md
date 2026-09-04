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
- Коммит: `fix(safety): harden hh submission gates and add comprehensive gate test suite` (`c38f9e1`).

### 1.6. Сетевой периметр Web UI и авторизация
- **Сетевой периметр:**
  - Проверено и зафиксировано: в `ai_assistant/cli.py` (`ui_cmd` и парсер аргументов `ui`) дефолтный хост установлен в безопасный локальный адрес `127.0.0.1` (порт 8000), доступна перегрузка через флаг `--host`.
- **Авторизация мутирующих эндпоинтов:**
  - `ai_assistant/config.py`: добавлена конфигурационная переменная `DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()`.
  - `ai_assistant/ui/app.py`: добавлено middleware `require_dashboard_token_on_mutation`:
    - Перехватывает все мутирующие HTTP-методы (`POST`, `PUT`, `PATCH`, `DELETE`).
    - Если `DASHBOARD_TOKEN` не задан в окружении / конфигурации -> возвращает `503 Service Unavailable` с телом `{"detail": "DASHBOARD_TOKEN not configured"}`.
    - Если `DASHBOARD_TOKEN` задан: извлекает токен из заголовка `Authorization: Bearer <token>` либо `X-API-Key: <token>`.
    - Сравнение токена выполняется через `secrets.compare_digest` для защиты от атак по времени (timing attacks). При неверном или отсутствующем токене возвращает `401 Unauthorized` с телом `{"detail": "Unauthorized: invalid or missing dashboard token"}`.
    - Все read-only эндпоинты (`GET /api/stats`, `GET /api/vacancies`, `GET /api/queue`, `GET /api/package/{id}`, `GET /`) работают свободно без авторизации.
  - `ai_assistant/ui/static/index.html`:
    - В верхний заголовок добавлен компактный инпут для токена (`🔑 DASHBOARD_TOKEN`).
    - Токен автоматически сохраняется в `localStorage.dashboard_token` и восстанавливается при загрузке страницы.
    - Функция `getAuthHeaders()` автоматически проставляет заголовок `Authorization: Bearer <token>` во все мутирующие запросы (`/api/review/...`, `/api/collect`).
    - Реализована понятная обработка ответов 401 и 503 с информативными подсказками пользователю.
- **Тесты (`tests/test_ui.py`):**
  - `test_ui_get_endpoints_accessible_without_token`: GET-эндпоинты открыты без авторизации даже при заданном `DASHBOARD_TOKEN`;
  - `test_ui_mutating_endpoints_without_dashboard_token_returns_503`: мутирующие вызовы без настроенного токена отдают 503;
  - `test_ui_mutating_endpoints_with_dashboard_token_missing_auth_returns_401`: мутирующие вызовы без заголовка авторизации отдают 401;
  - `test_ui_mutating_endpoints_with_wrong_token_returns_401`: неверный токен (как Bearer, так и X-API-Key) отдает 401;
  - `test_ui_mutating_endpoints_with_valid_token_succeeds`: валидный токен успешно проходит авторизацию;
  - `test_ui_default_host_is_localhost`: дефолтный хост при запуске — `127.0.0.1`.
- **Проверки:**
  - `pytest tests/test_ui.py` -> 14 passed (100%).
  - `ruff check tests/test_ui.py` -> All checks passed (0 errors).
- Коммиты:
  - `4681e6f` fix(security): bind web dashboard to localhost and add bearer token auth for mutating endpoints
  - `86c7ec5` fix(safety): preserve legacy preflight submission compatibility for non-numeric test vacancies

### 1.7. Контрольная точка Фазы 1
- **Полный регрессионный прогон Pytest:**
  - Результат: **1 428 passed, 0 failed, 0 errors** за 712.79s (11 мин 52 сек).
  - Сравнение с Baseline: было 1 408 passed за 659.74s (10 мин 59 сек).
  - Прирост: +20 новых тестов (11 гейтов отправки, 3 дедупликации очереди, 6 авторизации и хоста UI). Время прогона стабильно, регрессий нет.

- **Сводный список выполненных коммитов Фазы 1:**
  1. `ea81c7c` fix(security): remove exposed telegram bot token from tests and audit brief
  2. `cc5979a` fix(production): sync hermes integration and reconcile legacy audit mismatch
  3. `0c81d65` fix(syntax): resolve all undefined names (F821)
  4. `ea64534` fix(cleanup): remove redefined functions and duplicate imports (F811)
  5. `c38f9e1` fix(safety): harden hh submission gates and add comprehensive gate test suite
  6. `4681e6f` fix(security): bind web dashboard to localhost and add bearer token auth for mutating endpoints
  7. `86c7ec5` fix(safety): preserve legacy preflight submission compatibility for non-numeric test vacancies

- **Раздел «Требует решения человека» (Requires Human Decision):**

  #### 1. Обязательные действия владельца системы (что сломано или заблокировано прямо сейчас):
  1. **Флаг `SUBMIT_ALLOWED` в `.env`:**
     - **Текущее состояние:** Предохранитель по умолчанию выключен (`SUBMIT_ALLOWED=false`).
     - **Что заблокировано:** Любая реальная отправка откликов (включая команды с флагом `--confirm-submit`) падает с ошибкой `GATE_SUBMIT_ALLOWED: Submission is disabled by SUBMIT_ALLOWED configuration`.
     - **Действие человека:** Принять решение о готовности к реальной подаче. Если подача разрешена — явно прописать в `.env`: `SUBMIT_ALLOWED=true`. Если система должна работать только в режиме dry-run — оставить `false`.
  2. **Секрет `DASHBOARD_TOKEN` в `.env`:**
     - **Текущее состояние:** По умолчанию переменная пустая.
     - **Что заблокировано:** Все мутирующие эндпоинты веб-дашборда (`POST /api/reviews/{id}/approve`, `POST /api/reviews/{id}/reject`, `POST /api/collect`) возвращают HTTP 503 Service Unavailable (`Dashboard mutating endpoints disabled: DASHBOARD_TOKEN is not configured on server`). Пользователь не может подтверждать или отклонять ревью через веб-интерфейс.
     - **Действие человека:** Сгенерировать криптографически стойкий токен (например, `openssl rand -hex 24`), прописать его в `.env` (`DASHBOARD_TOKEN=<token>`), и передавать его в заголовке `Authorization: Bearer <token>` при работе с UI / API.
  3. **Ротация скомпрометированного `TELEGRAM_BOT_TOKEN`:**
     - **Текущее состояние:** Старый токен `8217526633:AAHqReznT2DYvTg67zzftLR0iWSbowfb3rg` был удалён из активного кода тестов и документации, но находится в открытом доступе в истории коммитов Git (начиная с `f3a08f1`).
     - **Что уязвимо / заблокировано:** Старый токен скомпрометирован и может быть перехвачен третьими лицами для чтения чатов или отправки сообщений от имени бота. Без настройки валидного токена в `.env` не работает доставка алертов и Telegram-интерфейс откликов.
     - **Действие человека:**
       1. Немедленно открыть официальный бот [@BotFather](https://t.me/BotFather) в Telegram.
       2. Выполнить `/revoke` для скомпрометированного бота для аннулирования старого токена, затем сгенерировать новый токен через `/token`.
       3. Записать новый токен в файл `.env` (`TELEGRAM_BOT_TOKEN=<новый_токен>`).
       4. После ротации выполнить очистку Git-истории (через `git-filter-repo` или BFG Repo-Cleaner) на сервере/форках.

  #### 2. Доменные правила, закрытые в коде (не требуют вмешательства):
  - `GATE_SUBMIT_ALLOWED`: реализован как единственный глобальный kill-switch, предотвращающий непреднамеренную отправку.
  - `GATE_NOT_ALREADY_APPLIED`: реализован белый список допустимых к повтору статусов (`FAILED`, `BLOCKED`, `FAIL_CLOSED`, `GATE_BLOCKED`, `CANCELLED`, `DRY_RUN`); любые завершенные или неоднозначные статусы (`SUBMITTED`, `CONFIRMED`, `AMBIGUOUS_POST_SUBMIT`, `VERIFIED`) блокируют повтор.
  - `GATE_URL_DOMAIN`: строгая проверка `host == "hh.ru"` или `host.endswith(".hh.ru")` исключает подмену домена и SSRF.
  - `GATE_VACANCY_MATCH`: для вакансий HeadHunter (`source == "hh"`) обязательно строго числовой `source_job_id` (`isdigit()`), при отсутствии или нечисловом формате — строгий fail-closed. Legacy-preflight унифицирован и делегирует проверки в `HHSubmissionGates.check_all_gates`.

  #### 3. Попутно обнаруженные нецелевые проблемы:
  - Зафиксированы в `docs/audit_followup.md`:
    1. `ai_assistant/application_tracking.py:167-168`: при переводе статуса в `APPLIED` вызывается сайд-эффект `complete_review()`, меняющий статус ревью на `COMPLETED`.
    2. `tests/test_stage31_watcher.py:435`: синтетический нечисловой ID `submit-flow-1` заменен на реалистичный числовой HH ID `136591579`, устраняя искусственное расхождение мока с боевым HH.
    3. `ai_assistant/vacancy_identity.py:27-33`: параметр `from` / `hhtmfrom` не вырезается в `normalize_url`, что создаёт риск дублирования вакансий в БД.




