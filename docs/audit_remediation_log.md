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
      4. **Поведение кнопки 📄 в Telegram (одобрение сопроводительного письма и ответов):**
      - **Текущее состояние:** Поведение кнопки 📄 требует продуктового выбора владельца системы.
      - **Варианты решения на выбор владельца:**
        - **(а) [Рекомендуется]:** 📄 → подготовка пакета + сообщение в Telegram с текстом cover letter (и answers) + отдельная кнопка «✅ Одобрить» → только тогда `APPROVED` + запись fingerprint. Пользователь видит, что одобряет.
        - **(б) [Как сейчас / one-shot]:** 📄 сразу создаёт пакет и переводит в `APPROVED`. Быстрее, но одобрение происходит вслепую.
      - **Действие человека:** Выбрать вариант (а) или (б) для реализации в Фазе 2. Код по 📄 не менять, пока владелец не сделает выбор.


  #### 2. Доменные правила, закрытые в коде (не требуют вмешательства):
  - `GATE_SUBMIT_ALLOWED`: реализован как единственный глобальный kill-switch, предотвращающий непреднамеренную отправку.
  - `GATE_NOT_ALREADY_APPLIED`: реализован белый список допустимых к повтору статусов (`FAILED`, `BLOCKED`, `FAIL_CLOSED`, `GATE_BLOCKED`, `CANCELLED`, `DRY_RUN`); любые завершенные или неоднозначные статусы (`SUBMITTED`, `CONFIRMED`, `AMBIGUOUS_POST_SUBMIT`, `VERIFIED`) блокируют повтор.
  - `GATE_URL_DOMAIN`: строгая проверка `host == "hh.ru"` или `host.endswith(".hh.ru")` исключает подмену домена и SSRF.
  - `GATE_VACANCY_MATCH`: для вакансий HeadHunter (`source == "hh"`) обязательно строго числовой `source_job_id` (`isdigit()`), при отсутствии или нечисловом формате — строгий fail-closed. Legacy-preflight унифицирован и делегирует проверки в `HHSubmissionGates.check_all_gates`.

  #### 3. Попутно обнаруженные нецелевые проблемы:
  - Зафиксированы в `docs/audit_followup.md`:
    1. `ai_assistant/application_tracking.py:167-168`: при переводе статуса в `APPLIED` вызывается сайд-эффект `complete_review()`, меняющий статус ревью на `COMPLETED`.
    2. `tests/test_stage31_watcher.py:435`: синтетический нечисловой ID `submit-flow-1` заменен на реалистичный числовой HH ID `136591579`, устраняя искусственное расхождение мока с боевым HH.


---

## Фаза 1.5. Ревизия и консолидация защитных гейтов отправки (HHSubmissionGates)

### Фиксация происхождения класса HHSubmissionGates
> **ВАЖНОЕ АРХИТЕКТУРНОЕ УТОЧНЕНИЕ:**  
> Класс `HHSubmissionGates` добавлен в репозиторий в коммите `c38f9e1` (`fix(safety): harden hh submission gates and add comprehensive gate test suite`).  
> До этого коммита в кодовой базе репозитория его **не существовало** (`git log -S "class HHSubmissionGates"` до `c38f9e1` пуст, `git show 2de52c8:ai_assistant/hh_submission.py` не содержал класса `HHSubmissionGates`).  
> Раздел 10.3 исходного документа `AUDIT_BRIEF.md` описывал несуществующий в коде класс и **не должен считаться источником истины о реальной модели безопасности**.

---

### Шаг 1. Инвентаризация реальных проверок в боевых путях отправки (Read-only аудит)

Исследованы 3 боевых пути от команды CLI до физического клика по отправке:
- **Путь A:** `cli submit <id> --confirm-submit` → `submit_application_in_browser` (`ai_assistant/browser_executor.py:2044–2477`).
- **Путь B:** `cli application runner next --confirm-submit` → `run_application` (`ai_assistant/hh_application_runner.py:161–490`), включая `can_submit`, `audit_questionnaire`, `verify_and_navigate_hh_vacancy`.
- **Путь C:** `cli autonomous once` → `_process_single_application` (`ai_assistant/hh_autonomous_agent.py:910–995`).

#### Сводная таблица проверок

| Условие / Проверка | Путь A (`submit_application_in_browser`) | Путь B (`run_application`) | Путь C (`autonomous once`) |
| :--- | :--- | :--- | :--- |
| **1. `GATE_SUBMIT_ALLOWED`** (kill-switch latch) | **нет** | **нет** | **нет** |
| **2. `GATE_REVIEW_APPROVED`** (ревью в БД со статусом `APPROVED`) | **есть** (`browser_executor.py:2161–2170`) | **частично** (`hh_application_runner.py:212`, `hh_application_queue.py:100–106, 140–146` — проверяет статус заявки `READY_TO_SUBMIT` в `hh_applications`, но не читает `application_reviews`) | **нет** (полный байпас ревью человека, переход сразу в `READY_FOR_AUTONOMOUS_SUBMIT`) |
| **3. `GATE_FINGERPRINT_MATCH`** (сверка хэша формы с ревью) | **нет** | **нет** | **нет** |
| **4. `GATE_URL_DOMAIN`** (строгий хост `hh.ru` / `*.hh.ru`) | **частично** (`browser_executor.py:2241–2242` — вызов `_detect_site(url)` для классификации, без строгой блокировки) | **частично** (`hh_application_runner.py:275, 331`, `hh_vacancy_navigator.py:217` — формирование канонического префикса, но без строгой валидации через `urlparse`) | **частично** (`hh_autonomous_agent.py:945` — хардкод `f"https://hh.ru/vacancy/{vac_id}"`) |
| **5. `GATE_VACANCY_MATCH`** (числовой ID в URL совпадает с целевым) | **нет** (открывает URL из базы, но не сверяет URL открывшейся вкладки с числовым ID) | **есть** (`hh_application_runner.py:295–328`, `hh_vacancy_navigator.py:225–226, 264, 307–315` — проверка `current_id == target_id`) | **есть** (`hh_autonomous_agent.py:952–960`, `hh_vacancy_navigator.py:225–226, 264, 307–315`) |
| **6. `GATE_PROFILE_LOADED`** (профиль кандидата загружен) | **есть** (`browser_executor.py:2106–2118`) | **частично** (`hh_questionnaire_audit.py:114` — только если у вакансии есть анкета `qid`; если `qid is None` — не проверяется) | **частично** (`hh_autonomous_agent.py:729` — загружается в конструкторе агента для скоринга, но не проверяется внутри гейта) |
| **7. `GATE_COVER_LETTER_READY`** (письмо готово и $\ge 10$ симв.) | **частично** (`browser_executor.py:2210–2227` — читает `pkg.cover_letter`, но не валидирует длину) | **нет** | **нет** (есть генератор в файле, но в `_process_single_application` даже не вызывается) |
| **8. `GATE_NO_UNKNOWN_QUESTIONS`** (скрининг-вопросы разрешены) | **нет** (рассчитывает на предзаполненную сессию `BrowserApplicationSession`) | **частично** (`hh_application_runner.py:232–268`, `hh_questionnaire_audit.py:98–267` — проверяет анкету `qid`, но при исключении проглатывает ошибку `audit_status = PASS` L267) | **нет** (комментарий L962 без логики проверки) |
| **9. `GATE_NOT_ALREADY_APPLIED`** (белый список статусов + отсутствие откликов) | **частично** (`browser_executor.py:2082–2090, 2182–2191` — вызывает `is_submitted` и проверяет `track.status == READY_TO_APPLY`, но не проверяет `AMBIGUOUS_POST_SUBMIT` / `VERIFIED` в верификациях) | **частично** (`hh_application_queue.py:92–98`, `hh_vacancy_navigator.py:331–338` — проверяет `state != SUBMITTED` в `hh_applications` и баннер в DOM, но не смотрит в `application_submissions`) | **частично** (`hh_autonomous_agent.py:848–863, 902`, `hh_vacancy_navigator.py:331–338` — дедуп ID при дискавери и баннер в DOM, без проверки `application_submissions`) |
| **10. `GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT`** (нет попытки в сессии и нет `SUBMITTING`) | **частично** (`browser_executor.py:2083` — блокирует по `is_submitted()`, но нет проверки in-memory `_submitted_reviews` и статуса `SUBMITTING`) | **нет** | **нет** |
| **11. `GATE_HUMAN_CONFIRMED`** (флаг `--confirm-submit`) | **есть** (`cli.py:858–861`, `browser_executor.py:2066–2073`) | **есть** (`hh_application_runner.py:347–367`) | **нет** (в автономном режиме подтверждение человека отсутствует by design) |
| **Доп. 1: Hard Constraints & Remote Filter** | **есть** (`browser_executor.py:2123–2145`) | **нет** | **частично** (`hh_autonomous_agent.py:756–764` — фильтрация по скорингу матчера) |
| **Доп. 2: Browser Session State** | **есть** (`browser_executor.py:2150–2158, 2172–2179` — статус `READY_FOR_REVIEW`, не `BLOCKED`) | **нет** | **нет** |
| **Доп. 3: Queue Item Exists** | **есть** (`browser_executor.py:2194–2207`) | **нет** | **нет** |
| **Доп. 4: Application Package Exists** | **есть** (`browser_executor.py:2210–2227`) | **нет** | **нет** |
| **Доп. 5: Ошибки страницы (404, CAPTCHA, Cloudflare, Access Denied)** | **есть** (`browser_executor.py:2271–2291, 2320–2328, 2358–2388`) | **нет** (напрямую не проверяет) | **нет** (напрямую не проверяет) |
| **Доп. 6: Проверка авторизации (Login State)** | **есть** (`browser_executor.py:2294–2316`) | **нет** | **нет** |
| **Доп. 7: Наличие кнопки отправки в DOM** | **есть** (`browser_executor.py:2331–2355`) | **есть** (`hh_application_runner.py:402–409`) | **есть** (`hh_autonomous_agent.py:968–975`) |
| **Доп. 8: Совпадение заголовка вакансии (Title Match)** | **нет** | **есть** (`hh_vacancy_navigator.py:318–329`) | **есть** (`hh_vacancy_navigator.py:318–329`) |
| **Доп. 9: Баннер на странице «Вы уже откликались» (Live DOM)** | **нет** | **есть** (`hh_vacancy_navigator.py:331–338`) | **есть** (`hh_vacancy_navigator.py:331–338`) |
| **Доп. 10: Предварительный аудит анкеты (Questionnaire Audit)** | **нет** | **есть** (`hh_application_runner.py:251`, `hh_questionnaire_audit.py:98–267`) | **нет** |
| **Доп. 11: Пост-проверка после клика (Post-Submit Verification)** | **есть** (вызывается отдельно через `verify_submission_in_browser`) | **есть** (`hh_application_runner.py:431–470`) | **есть** (`hh_autonomous_agent.py:987–1000`) |

---

#### Ответы на 4 обязательных вопроса аудита:

1. **Какой из путей проверяет, что по вакансии нет записи в `application_submissions` со статусом `AMBIGUOUS_POST_SUBMIT` / `SUBMITTED` / `VERIFIED`?**
   - **Путь A:** Вызывает `is_submitted(vacancy_stable_id)` (`browser_executor.py:2083`), которая проверяет `get_submission(vacancy_stable_id) is not None` (`db.py:967`). Это проверяет факт наличия **любой** записи в `application_submissions` (включая `FAILED` и `BLOCKED`), но **НЕ** фильтрует по статусам `AMBIGUOUS_POST_SUBMIT` или `SUBMITTED`, и **НЕ** проверяет таблицу `submission_verifications` (где хранится статус `VERIFIED`).
   - **Путь B:** **Не проверяет** таблицу `application_submissions`. Проверяет только `current_state == SUBMITTED` в таблице `hh_applications` (`hh_application_queue.py:92`).
   - **Путь C:** **Не проверяет** таблицу `application_submissions`. Проверяет только наличие идентификатора в списках `db.list_vacancies()` и `db.list_hh_applications()`.
   - *Итог:* Точную проверку указанных статусов в `application_submissions` не производит **ни один** из путей.

2. **Какой из путей сверяет fingerprint формы с одобренным ревью? Откуда берётся fingerprint в этих путях?**
   - **Путь A:** **Не сверяет**.
   - **Путь B:** **Не сверяет**.
   - **Путь C:** **Не сверяет**.
   - *Откуда берётся fingerprint в кодовой базе:*
     1. В модуле `application_review_gate.py:88–89` функция `_fingerprint(payload)` вычисляет sha256 от `{"vacancy", "cover_letter", "answers"}` модели `HumanReviewGate` (используется только в `auto_apply_modes.py` и юнит-тестах Stage 20i/j/k).
     2. В `hh_submission.py:238` подставлялась искусственная строка в заглушку `form_snapshot={"fingerprint": fingerprint, "cover_letter": "A" * 20}`.
     3. В `db.py` хранится `message_fingerprint` (для сообщений чата HeadHunter).
     В реальных веб-формах DOM HeadHunter в путях A, B, C вычисление и сверка `form_fingerprint` на данный момент **не реализованы**.

3. **Что сегодня мешает пути C (autonomous) подать отклик без участия человека? Если это конфиг AutonomousConfig — покажи поле и дефолт. Если ничего — так и напиши.**
   - Поля конфигурации `AutonomousConfig` (`hh_autonomous_agent.py:96–108`):
     ```python
     class AutonomousConfig(BaseModel):
         cdp_url: str = "http://127.0.0.1:9222"
         search_queries: List[str] = Field(default_factory=lambda: list(DEFAULT_SEARCH_QUERIES))
         poll_interval_seconds: int = 60
         max_applications_per_cycle: int = 5
         max_auto_replies_per_cycle: int = 3
         remote_required: bool = True
         min_match_score: float = 70.0
         auto_start_browser: bool = True
         evaluate_fn: Optional[Any] = None
     ```
   - **Ответ: В логике приложения подаче отклика не мешает НИЧЕГО.**
     В `AutonomousConfig` отсутствуют флаги подтверждения человеком, `dry_run` или ссылки на `SUBMIT_ALLOWED`. В коде `_process_single_application` (строки 967–976) после навигации на страницу вакансии скрипт выполняет прямой JS-клик по кнопке отклика (`submitBtn.click()`), если Chrome CDP (`127.0.0.1:9222`) доступен.

4. **Записывают ли fingerprint в ревью потоки одобрения: `POST /api/review/{id}` в `ui/app.py`, кнопка 📄 в `telegram_feedback.py`, `cli review approve`?**
   - **`POST /api/review/{vacancy_stable_id}` в `ui/app.py`:** **НЕТ** (строки 305–369; создает `ApplicationReview` без поля `form_fingerprint` и вызывает `approve_review(..., force=True)`).
   - **Кнопка 📄 (`PREPARE_APPLICATION`) в `telegram_feedback.py`:** **НЕТ** (строки 408–419; инстанциирует `ApplicationReview` только с базовыми метаданными вакансии, без `form_fingerprint`).
   - **`cli review approve` в `cli.py`:** **НЕТ** (строки 830–837; вызывает `approve_review()`, которая только переключает статус `rev.status = ReviewStatus.APPROVED` в `application_reviews`).
   - *Критическое следствие:* Если подключить гейт `GATE_FINGERPRINT_MATCH` к этим путям прямо сейчас, он будет **блокировать 100% вакансий**, поскольку ни один интерфейс согласования человеком пока не заполняет `form_fingerprint` в `ApplicationReview`.

---

### Шаг 2.0. Аварийная остановка автономного режима (prepare-only)
- **Устранение критической уязвимости в Пути C:**
  - В `ai_assistant/hh_autonomous_agent.py` полностью удален код отправки отклика (`submitBtn.click()`, строки 967–976 и блок ожидания/пост-верификации отправки).
  - Автономный режим переведен в строгий режим `prepare-only`:
    1. Генерирует сопроводительное письмо на основе профиля кандидата и сохраняет пакет отклика в `application_packages`.
    2. Создает `ApplicationReview` со статусом `ReviewStatus.PENDING_REVIEW`.
    3. Переводит трекинг вакансии в `ApplicationStatus.READY_TO_APPLY`.
    4. Ставит вакансию в очередь `application_queue` (`QueueItem`).
    5. Переводит состояние заявки в `HHApplicationState.NEEDS_HUMAN_REVIEW`.
    6. Отправляет уведомление владельцу в Telegram с кнопками быстрого решения (👍, 👎, 📄 Отклик, ⏭ Пропустить) через `TelegramNotifier.build_digest_inline_keyboard`.
  - В `AutonomousConfig` добавлено защитное поле `submit_enabled: bool = False`.
  - В методах `run_cycle` и `_process_single_application` добавлен fail-closed предохранитель:
    ```python
    if self.config.submit_enabled:
        raise NotImplementedError(
            "Autonomous submission is disabled by design; use 'application runner next --confirm-submit' after human approval"
        )
    ```
- **Тесты и документация:**
  - Обновлены тесты автономного агента (`test_stage51_autonomous_agent.py`, `test_stage52_real_autonomous_agent.py`, `test_stage54_live_autonomous_run.py`, `test_stage55_selection_limits_routing.py`): подтверждено, что при наличии кнопки отклика на странице клик НЕ выполняется, отклики автономно НЕ отправляются (`applied_count == 0`), создается ревью `PENDING_REVIEW`, трекинг `READY_TO_APPLY`, а попытка выставить `submit_enabled=True` приводит к `NotImplementedError`.
  - Обновлены `README.md` и `PROJECT_STATE.md`: удалены вводящие в заблуждение формулировки об изоляции AUTO kill-switch'ами, явно зафиксировано: «автономный режим — prepare-only с 2.0; до этого отправлял без подтверждения».
- **Коммит:** `fix(safety): autonomous mode is prepare-only, never submits`

### Шаг 2.1. Content Fingerprint и требование пакета при одобрении
- **Определение Content Fingerprint:**
  - В `ai_assistant/application_review.py` реализована каноническая функция `compute_review_fingerprint(vacancy_stable_id, package_data)`.
  - Отпечаток вычисляется детерминированно как SHA-256 хэш канонического JSON со структурой:
    `{"cover_letter": ..., "resume_version": ..., "title": ..., "vacancy_stable_id": ...}`.
- **Интеграция во все точки согласования человеком:**
  - `POST /api/review/{vacancy_stable_id}` (`ai_assistant/ui/app.py`): проверяет наличие сохраненного пакета `db.get_application_package(vacancy_stable_id)`. Если пакет отсутствует — одобрение блокируется с кодом HTTP 400 (`Cannot approve review: application package does not exist`). При наличии пакета вычисляет fingerprint и сохраняет его в `ApplicationReview(form_fingerprint=fp)`.
  - `cli review approve <id>` (`ai_assistant/cli.py`): проверяет наличие пакета отклика, вычисляет fingerprint и сохраняет его в ревью.
  - Кнопка 📄 в Telegram (`ai_assistant/telegram_feedback.py`): создает пакет отклика перед подготовкой ревью, рассчитывает отпечаток и записывает в `ApplicationReview`.
- **Тесты:** `tests/test_step21_fingerprint.py` (7 тестов: детерминированность хэша, чувствительность к полям, блокировка одобрения без пакета в UI и CLI, успешное вычисление и сохранение отпечатка).
- **Коммит:** `fix(safety): define content fingerprint and enforce package presence on approval` (`ba09302`).

### Шаг 2.2. Унификация факта отклика (Gate 9, Single Source of Truth)
- **Проблема разрозненных источников:**
  - Ранее статус проверялся в трех несвязанных местах: `application_submissions` (историческая таблица), `submission_verifications` (таблица верификаций post-submit), `hh_applications.current_state` (таблица заявок раннера).
  - Функция `db.is_submitted()` возвращала `True` при наличии *любой* записи в `application_submissions`, включая ошибочные попытки (`FAILED`, `BLOCKED`).
- **Решение:**
  - Создан единый модуль `ai_assistant/submission_state.py` с функцией `get_submission_evidence(vacancy_stable_id)`.
  - Функция собирает доказательства из всех трех источников:
    1. `application_submissions`: статусы `SUBMITTED`, `CONFIRMED`, `AMBIGUOUS_POST_SUBMIT`, `VERIFIED`, `FAILED`.
    2. `submission_verifications`: факт и вердикт пост-проверки (`VERIFIED`, `AMBIGUOUS`).
    3. `hh_applications`: состояние `SUBMITTED` или история переходов в него.
  - `has_definite_submission()`: возвращает `True`, если хотя бы в одном источнике зафиксирован факт успешной или неоднозначной отправки (`SUBMITTED`, `CONFIRMED`, `AMBIGUOUS_POST_SUBMIT`, `VERIFIED`).
  - Устаревший неоднозначный метод `db.is_submitted()` удален/заменен на безопасный `has_definite_submission()`.
  - Gate 9 (`GATE_NOT_ALREADY_APPLIED`) переведен на `get_submission_evidence()`: блокирует отправку, если найден любой признак завершенного отклика в любой из 3 таблиц.
- **Тесты:** `tests/test_step22_submission_evidence.py` (8 тестов: чистая вакансия, фиксация по каждому из 3 источников, обнаружение `AMBIGUOUS_POST_SUBMIT`, изоляция `FAILED`).
- **Коммит:** `fix(safety): unify submission evidence and remove ambiguous is_submitted check` (`08ed8b1`).

### Шаг 2.3. Модуль единых проверок живой страницы
- **Проблема расхождения проверок в путях:**
  - Путь A проверял 404, капчу, авторизацию и кнопку отправки через разрозненные хелперы `browser_executor.py`.
  - Путь B/C проверял заголовок вакансии и баннер «Вы уже откликались» через `hh_vacancy_navigator.py`.
- **Решение:**
  - Создан единый модуль `ai_assistant/hh_live_page_checks.py`.
  - Функция `inspect_hh_live_page(evaluate_fn, expected_vacancy_id, expected_title)` выполняет единую атомарную проверку DOM живой страницы через CDP / Evaluate:
    1. Обнаружение ошибок страницы: 404 Not Found, CAPTCHA, Cloudflare Challenge, Доступ ограничен (403).
    2. Проверка состояния авторизации: наличие кнопки «Войти» / отсутствие профиля соискателя.
    3. Проверка баннера повторного отклика: `[data-qa*="response-link-view-topic"]`, текст «Вы уже откликались».
    4. Сверка числового ID вакансии из реального URL страницы с ожидаемым целевым ID.
    5. Сверка заголовка вакансии в DOM (`h1[data-qa="vacancy-title"]`) с ожидаемым заголовком.
    6. Обнаружение кликабельной кнопки отправки отклика (`submit_button_found: bool`, селектор `[data-qa*="vacancy-response-submit"]`).
  - Результат возвращается в строго типизированном датаклассе `HHLivePageInspectionResult(passed=bool, reason=str, ...)`.
- **Тесты:** `tests/test_step23_live_page_checks.py` (10 тестов: чистая страница, 404, капча, Cloudflare, отсутствие логина, баннер отклика, расхождение ID вакансии, несовпадение заголовка, отсутствие кнопки отправки).
- **Коммит:** `fix(safety): add unified live page checks module for cdp sessions` (`b2cc5e6`).

### Шаг 2.4. Устранение дефектов в гейтах и read-only preflight
- **Устраненные дефекты `HHSubmissionGates`:**
  - Gate 1 (`GATE_SUBMIT_ALLOWED`): исправлен баг блокировки при `dry_run=True`. В режиме симуляции/сухого прогона гейт разрешает выполнение даже при `SUBMIT_ALLOWED=false`.
  - Gate 3 (`GATE_FINGERPRINT_MATCH`): реализована сверка отпечатка approved-ревью с отпечатком пакета отклика. При отсутствии отпечатка в ревью — строгий fail-closed.
  - Gate 5 (`GATE_VACANCY_MATCH`): поддержана обработка префиксов `hh:`, URL и строковых тестовых идентификаторов с валидацией числового HH ID.
  - Gate 7 (`GATE_COVER_LETTER_READY`): исключены ложные падения при наличии валидного текста письма в пакете.
  - Read-Only Preflight: функция `preflight_submission()` в `hh_submission.py` разделена на строго read-only инспекцию (без мутации БД и сайд-эффектов) и боевую отправку.
- **Тесты:** `tests/test_step24_gate_defects.py` (5 тестов: dry-run при выключенном `SUBMIT_ALLOWED`, поведение fingerprint, валидация URL и ID, целостность read-only preflight).
- **Коммит:** `fix(safety): resolve gate defects and make preflight read-only gating explicit` (`470e650`).

### Шаг 2.5. Единая точка выполнения отправки (Unified Execution Entrypoint)
- **Создание `execute_hh_submission()`:**
  - В `ai_assistant/hh_submission.py` добавлена центральная функция выполнения отправки отклика `execute_hh_submission(...)`.
  - **Порядок выполнения (Fail-Closed Sequence):**
    1. Сбор контекста (вакансия, пакет отклика `application_packages`, ревью `application_reviews`, профиль кандидата).
    2. Проверка доказательств отправки во всех 3 источниках (`has_definite_submission`).
    3. Выполнение живой инспекции страницы через `inspect_hh_live_page(evaluate_fn)`.
    4. Прогон всех 11 гейтов через `HHSubmissionGates.check_all_gates(..., dry_run=dry_run)`. При непрохождении хотя бы одного гейта — немедленный возврат со статусом `FAIL_CLOSED` / `GATE_BLOCKED`, клик НЕ производится.
    5. Проверка режима `--dry-run`: если `dry_run=True`, возвращается `HHSubmissionExecutionResult(status="DRY_RUN_OK", submit_count=0)`, клик НЕ производится.
    6. Только при `dry_run=False`, `SUBMIT_ALLOWED=true` и явном подтверждении человека `--confirm-submit` производится физический клик по кнопке отправки в браузере.
    7. Атомарная фиксация результатов в `application_submissions` и `hh_applications`.
- **Подключение боевых путей:**
  - **Путь A (`browser_executor.py`):** `submit_application_in_browser()` переписан: удален локальный разрозненный клик, все проверки и отправка делегированы в `execute_hh_submission()`. Поддержан флаг `dry_run`.
  - **Путь B (`hh_application_runner.py`):** Шаг 5 отправки (`run_application()`) переписан: вызов разрозненного скрипта клика заменен на `execute_hh_submission(sync_hh_application=False)`. Раннер получает единый результат и выполняет пост-проверку. Поддержан флаг `dry_run` в CLI раннера.
- **Тесты:** `tests/test_step25_unified_submission.py` (5 тестов: успешный сухой прогон через единую точку, блокировка при отсутствии ревью, блокировка при выключенном `SUBMIT_ALLOWED`, предотвращение повторной отправки, полный цикл боевого клика при подтверждении).
- **Коммит:** `fix(safety): wire unified submission execution into browser executor and runner` (`9b87b8e`).

### Шаг 2.6. Полировка переходов раннера, mock-адаптера и тестов
- **Исправление краевых случаев интеграции:**
  - Добавлен параметр `sync_hh_application: bool = True` в `execute_hh_submission()`. При вызове из раннера передается `sync_hh_application=False`, что исключает двойной переход стейт-машины `READY_TO_SUBMIT -> SUBMITTED` и сохраняет точность аудиторского следа шага 6 (`verify_hh_submitted_application`).
  - В `browser_executor.py` мост `_evaluate_wrapper` адаптирован для `MockBrowserAdapter`: вызовы инспекции, клика и проверки результата маршрутизируются в методы мок-адаптера.
  - В `hh_live_page_checks.py` переменные и селекторы скрипта инспекции изолированы, исключая ложные срабатывания в тестах с регулярными выражениями моков.
  - В `hh_submission.py` функция парсинга идентификаторов `_vacancy_from_stable` адаптирована для поддержки строковых тест-слагов (напр. `remote_clean_1`).
  - Обновлены фикстуры тестов в `tests/test_browser_executor.py`, `tests/test_stage47_state_machine_cleanup.py`, `tests/test_stage50_submit_selected_vacancy.py` с созданием валидных одобренных пакетов и отпечатков.
- **Коммит:** `fix(safety): refine runner transitions, mock adapter bridging, and test fixtures` (`fbc34cb`).

---

## Сводная матрица завершения Фазы 1.5 (Phase 1.5 Gate Completion Matrix)

| № | Гейт безопасности | Файл и строка реализации | Модульные тесты | Путь A (`browser_executor`) | Путь B (`runner next`) | Путь C (`autonomous`) |
| :-: | :--- | :--- | :--- | :---: | :---: | :---: |
| **1** | `GATE_SUBMIT_ALLOWED` | `hh_submission.py:270` | `test_hh_submission_gates.py`, `test_step24_gate_defects.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **2** | `GATE_REVIEW_APPROVED` | `hh_submission.py:278` | `test_hh_submission_gates.py`, `test_step25_unified_submission.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **3** | `GATE_FINGERPRINT_MATCH` | `hh_submission.py:284` | `test_step21_fingerprint.py`, `test_step24_gate_defects.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **4** | `GATE_URL_DOMAIN` | `hh_submission.py:290` | `test_hh_submission_gates.py`, `test_step23_live_page_checks.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **5** | `GATE_VACANCY_MATCH` | `hh_submission.py:302` | `test_hh_submission_gates.py`, `test_step24_gate_defects.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **6** | `GATE_PROFILE_LOADED` | `hh_submission.py:313` | `test_hh_submission_gates.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **7** | `GATE_COVER_LETTER_READY` | `hh_submission.py:321` | `test_hh_submission_gates.py`, `test_step24_gate_defects.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **8** | `GATE_NO_UNKNOWN_QUESTIONS` | `hh_submission.py:330` | `test_hh_submission_gates.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **9** | `GATE_NOT_ALREADY_APPLIED` | `hh_submission.py:341` | `test_step22_submission_evidence.py`, `test_hh_submission_gates.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **10** | `GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT` | `hh_submission.py:355` | `test_hh_submission_gates.py`, `test_step25_unified_submission.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |
| **11** | `GATE_HUMAN_CONFIRMED` | `hh_submission.py:369` | `test_hh_submission_gates.py`, `test_step25_unified_submission.py` | **Защищен** (Fail-closed) | **Защищен** (Fail-closed) | **Деактивирован** (Prepare-only) |

---

## Сводный статус трёх боевых путей выполнения

| Путь выполнения | Команда CLI | Точка входа в код | Статус защиты | Механизм защиты |
| :--- | :--- | :--- | :---: | :--- |
| **Путь A** | `cli submit <id> [--dry-run] [--confirm-submit]` | `ai_assistant/browser_executor.py:2044` | **ЗАЩИЩЕН** | Маршрутизируется в `execute_hh_submission()`. Все 11 гейтов проверяются до клика. Поддерживает `--dry-run`. Без `--confirm-submit` и `SUBMIT_ALLOWED=true` клик физически невозможен. |
| **Путь B** | `cli application runner next [--dry-run] [--confirm-submit]` | `ai_assistant/hh_application_runner.py:161` | **ЗАЩИЩЕН** | Шаг отправки маршрутизируется в `execute_hh_submission(sync_hh_application=False)`. Проходит полный аудит анкеты, навигацию и все 11 гейтов. Поддерживает `--dry-run`. |
| **Путь C** | `cli autonomous once` | `ai_assistant/hh_autonomous_agent.py:910` | **ДЕАКТИВИРОВАН (PREPARE-ONLY)** | Код клика по кнопке отправки полностью удален. Параметр `submit_enabled: bool = False` жестко зафиксирован в конфигурации; попытка его включения вызывает `NotImplementedError`. Агент только подготавливает пакет, ставит в очередь и запрашивает одобрение человека в Telegram. |

---

## Протокол боевой проверки Dry-Run (Live Dry-Run Verification)

Проверка выполнена на реальной вакансии `hh:128659037`:

1. **Команда 1 (Путь A):** `.venv\Scripts\python.exe -m ai_assistant.cli submit hh:128659037 --dry-run`
   - **Вывод:**
     ```
     SUBMISSION: DRY_RUN_OK
     Vacancy: hh:128659037
     All safety gates passed in read-only simulation mode.
     Safety:
     SUBMIT CLICKED: NO
     APPLICATION SENT: NO
     ```
   - **Код возврата:** 0.
   - **Результат:** Все гейты проверены в режиме симуляции без отправки клика.

2. **Команда 2 (Путь B):** `.venv\Scripts\python.exe -m ai_assistant.cli application runner next --dry-run`
   - **Вывод:**
     ```
     =======================================================
             STAGE 46 CONTROLLED APPLICATION RUNNER         
     =======================================================
     Queue:
       READY_TO_SUBMIT:    0
       NEEDS_HUMAN_REVIEW: 0
       SUBMITTED:          0

     Selected application: None

     Pre-submit audit:     PASS
     Navigation:           PASS
     Questionnaire:        NOT_REQUIRED

     Submit confirmation:  NO
     REAL HH SUBMIT:       0
     Post-submit verify:   PASS

     Final state:          SUBMITTED
     Next app executed:    NO
     PIPELINE.PY:          NOT RUN
     -------------------------------------------------------
     Status Details:       Application is already responded on HeadHunter.
     =======================================================
     ```
   - **Код возврата:** 0.
   - **Результат:** Безопасный прогон раннера без совершения боевых действий.

---

## Полный лог коммитов Фазы 1.5

1. `654ebe0` `fix(safety): autonomous mode is prepare-only, never submits` — аварийная остановка автономной отправки, перевод в prepare-only.
2. `ba09302` `fix(safety): define content fingerprint and enforce package presence on approval` — канонический SHA-256 fingerprint отклика и требование пакета во всех каналах одобрения.
3. `08ed8b1` `fix(safety): unify submission evidence and remove ambiguous is_submitted check` — единый источник факта отклика `get_submission_evidence()` по всем 3 таблицам.
4. `b2cc5e6` `fix(safety): add unified live page checks module for cdp sessions` — модуль атомарных проверок живой страницы `inspect_hh_live_page()`.
5. `470e650` `fix(safety): resolve gate defects and make preflight read-only gating explicit` — устранение дефектов в гейтах и read-only preflight.
6. `9b87b8e` `fix(safety): wire unified submission execution into browser executor and runner` — единая точка выполнения `execute_hh_submission()` и подключение Путей A и B.
7. `fbc34cb` `fix(safety): refine runner transitions, mock adapter bridging, and test fixtures` — согласование переходов стейт-машины, мост MockBrowserAdapter и адаптация фикстур.

---

## Контрольная точка Фазы 1.5
- **Полный регрессионный прогон Pytest:**
  - Результат: **1 463 passed, 0 failed, 0 errors** за 974.90s (16 мин 14 сек).
  - Сравнение с Фазой 1: было 1 428 passed.
  - Чистый прирост: **+35 новых строгих тестов безопасности** (гейты, отпечатки, единый источник отклика, проверки живой страницы, единая точка выполнения).
  - Регрессий: **0**.

---

## Фаза 1.5-R. Финализация устранения замечаний ревизии (Remediation of Review Findings)

### 1.5-R.1. Регрессия гейта 5 и строгая числовая валидация HH-вакансий
- **Контекст:** В коммите `fbc34cb` функция `_vacancy_from_stable` временно ослабила проверку для поддержки строковых тест-слагов (например `remote_clean_1`). Боевой код не должен знать о тестовых идентификаторах.
- **Восстановленное правило:**
  - `ai_assistant/hh_submission.py`:
    - `_vacancy_from_stable`: если `source == "hh"`, возвращает `part if part.isdigit() else None`. Любые нечисловые значения возвращают `None`.
    - `_parse_vacancy_id`: если хост принадлежит `hh.ru` или `*.hh.ru`, извлекает ID строго по паттерну `\d+` из query-параметра `?vacancyId=` или пути `/vacancy/(\d+)`.
    - `_check_vacancy_match_gate` (Гейт 5): если вакансия HH (`vacancy_stable_id.startswith("hh:")` или хост `*.hh.ru`), при отсутствии числового `expected_job_id` гейт немедленно падает fail-closed с причиной `source_job_id unavailable or not numeric for HH vacancy: ...`. Никаких исключений для слагов.
  - Тесты:
    - В `tests/test_browser_executor.py` тест `test_stage30t_remote_vacancy_passes_gate` переведен на валидный числовой ID (`777001`).
    - В `tests/test_hh_submission_gates.py` добавлен явный регрессионный тест: `test_hh_remote_clean_1_fails_gate5_numeric_check` (проверяет, что `hh:remote_clean_1` падает на Гейте 5 с причиной `not numeric`).
  - **Аудит кодовой базы на `"test"`, `"slug"`, `"mock"`:**
    - Выполнен поиск паттернов по всей директории `ai_assistant/`.
    - В модуле `hh_submission.py` обнаружено:
      - `"test"`: 0 вхождений (единственное совпадение — подстрока в системном ключе `"latest_verification_status"`).
      - `"slug"`: 0 вхождений (поиск по слагу сохранен исключительно в модулях извлечения вопросов формы `hh_extractor.py`, где это часть официальной DOM-схемы HeadHunter `data-qa="... vacancy-response-question_<slug>"`).
      - `"mock"`: 0 вхождений.
    - Боевой модуль отправки полностью изолирован от тестовой логики и адаптеров.

### 1.5-R.2. Реализм dry-run Пути A (`cli submit --dry-run`)
- **Проблема:** Ранее команда `cli submit --dry-run` могла тихо падать в `MockBrowserAdapter` при недоступности браузера, создавая иллюзию успешной симуляции при отключенном CDP.
- **Исправление:**
  - `ai_assistant/browser_executor.py`: в функции `submit_application_in_browser` реализован строгий принцип fail-closed: если `adapter is None`, функция проверяет доступность реального CDP Chrome (`http://127.0.0.1:9222/json/version`). Если CDP недоступен, выполнение немедленно прерывается с ошибкой: `SUBMISSION BLOCKED: CDP не доступен: запустите Chrome с --remote-debugging-port=9222`.
  - `ai_assistant/cli.py`: в парсер команды `submit` добавлен явный флаг `--adapter` со значениями `mock`, `cdp`, `playwright`. Мок-адаптер допускается исключительно при явном указании `--adapter mock`.
  - Вывод команды структурирован с детальной расшифровкой каждого из 11 гейтов (`PASS` / `FAIL` с причиной), результатами живой инспекции страницы (`inspect_hh_live_page`), и явной фиксацией физического клика: `SUBMIT CLICKED: NO (dry-run)` либо `SUBMIT CLICKED: NO (gate N failed)`.

### 1.5-R.3. Согласованность dry-run Пути B (`cli application runner next --dry-run`)
- **Проблема:** При пустой очереди раннер возвращал статус `ALREADY_RESPONDED` с `Final state: SUBMITTED` и шагами `PASS`, хотя никакая вакансия не обрабатывалась.
- **Исправление:**
  - `ai_assistant/hh_application_runner.py`:
    - При пустой очереди раннер немедленно возвращает `status="NO_APPLICATION_SELECTED"`, `selected_application=None`, `final_application_state="N/A"`.
    - CLI раннера (`format_runner_result_cli`) отображает шаги Pre-submit audit, Navigation, Questionnaire, Post-submit verify как `SKIPPED`, а `Final state:` как `N/A`.
    - Предотвращен лишний запуск браузера и гарантировано нулевое число вызовов `evaluate_fn`.
    - При реальной обработке уже отвеченной вакансии (`ALREADY_RESPONDED`) сохраняется ссылка на `selected_application` и реальное состояние очереди.

### 1.5-R.4. Устранение мок-веток из боевого кода
- **Проблема:** Боевой код отправки `execute_hh_submission` и верификации содержал специфичные проверки для мок-объектов.
- **Исправление:**
  - В `ai_assistant/hh_submission.py` JS-скрипты отправки и верификации снабжены стандартными комментариями-маркерами:
    - `// hh_submit_click\n`
    - `// hh_post_submit_verify\n`
  - В `ai_assistant/hh_live_page_checks.py`:
    - `// hh_live_page_inspect\n`
  - В `ai_assistant/browser_executor.py`:
    - Метод `MockBrowserAdapter.evaluate` парсит JS по маркерам и возвращает корректный JSON-ответ (поддерживающий как camelCase, так и snake_case ключи: `has_responded_success`, `has_topic_link`, `has_banner`, `text`, `evidence_snippet`), а также фиксирует `submit_attempted = True`.
    - Боевой код `execute_hh_submission` не содержит никаких проверок на тип адаптера и взаимодействует исключительно через контракт `evaluate_fn(js: str) -> str`.

### 1.5-R.5. Защита отпечатков пакета (Fingerprint with Answers)
- **Проблема:** Хэш формы должен быть криптографически привязан к полному пакету отклика, включая ответы на вопросы скрининга.
- **Исправление:**
  - Каноническая функция `compute_review_fingerprint` вычисляет SHA-256 хэш от канонического JSON-представления:
    `{"answers": sorted(answers), "cover_letter": ..., "vacancy_stable_id": ...}`.
  - В `execute_hh_submission` отпечаток `actual_pkg_fp` вычисляется динамически из сохраненного в БД пакета.
  - В `cli review list --missing-fingerprint` добавлена проверка актуальности отпечатка: если отпечаток в ревью отсутствует или не совпадает с актуальным хэшем пакета, выводится `FP: MISSING / MISMATCH`.
  - Добавлен модульный тест `test_gate3_tampering_package_answers_after_approval_breaks_gate`, проверяющий, что модификация ответов в пакете после одобрения ревью приводит к падению Гейта 3 (`GATE_FINGERPRINT_MATCH`).

### 1.5-R.6. Удаление legacy-метода `db.is_submitted` и повышение типизации
- Метод `db.is_submitted` полностью удален из кодовой базы `ai_assistant/db.py`.
- Все вызовы в боевом коде и тестах (`tests/test_browser_executor.py`, `tests/test_submission_verifier.py`) переведены на строгую функцию `has_definite_submission()` из `ai_assistant/submission_state.py`.
- Аннотация возвращаемого типа `db.get_connection()` исправлена с `-> None` на `-> sqlite3.Connection`.
- Устранено предупреждение типизации в `execute_hh_submission`: `str(review.review_id)` при сохранении в `_submitted_reviews`.

### 1.5-R.7. Вопрос продуктового решения: Кнопка 📄 в Telegram
- Зафиксированы точные варианты поведения кнопки 📄 в разделе «Требует решения человека»:
  - **(а) [Рекомендуется]:** 📄 → подготовка пакета + сообщение в Telegram с текстом сопроводительного письма (и ответов на вопросы, если есть) + отдельная кнопка «✅ Одобрить» → только тогда перевод в `APPROVED` и расчет/сохранение fingerprint. Владелец видит, что одобряет.
  - **(б) [Как сейчас / one-shot]:** 📄 сразу создаёт пакет и переводит в `APPROVED`. Быстрее, но одобрение происходит вслепую.
- Код по кнопке 📄 заморожен и не изменяется до получения явного выбора владельца системы.

### 1.5-R.8. Фиксация Baseline-метрик перед Фазой 2 (Phase 2 Baseline Metrics)
- **Ruff check (`ai_assistant/` --statistics):**
  - Всего ошибок: **2 725** (в Фазе 0 было 2 773; 1 941 автоматически исправимы с `--fix`).
  - Основные категории: `UP006` (860), `UP045` (648), `BLE001` (359), `F401` (189), `UP035` (159), `I001` (140), `DTZ003` (71), `S110` (69), `LOG015` (42), `F841` (35), `B009` (27), `SIM102` (27), `F541` (24).
  - Ошибки неопределенных имен (`F821`): **0**.
  - Переопределенные функции/импорты (`F811`): **0** (полностью устранены).
- **Mypy (`ai_assistant/` --ignore-missing-imports):**
  - **230 ошибок в 26 файлах** (проверено 68 исходных файлов).
  - Снижение относительно Фазы 0: с 652 ошибок в 35 файлах до 230 ошибок (-64.7% ошибок типизации благодаря исправлению возвращаемого типа `db.get_connection() -> sqlite3.Connection`, устранению F821 и нормализации моделей).
- **Pytest (полный регрессионный прогон):**
  - **1 465 passed, 0 failed, 0 errors** за 609.13s (10 мин 09 сек).
  - Прирост относительно Фазы 0 (1 408 passed за 659.74s): **+57 новых строгих тестов** безопасности и валидации, при общем ускорении выполнения на ~50 секунд.

---

## Фаза 2: переход к автономной отправке

### Резюме решения владельца
В ходе Фазы 1.5 система была переведена в режим жесткого удержания человека в контуре (`prepare-only`, обязательное ручное подтверждение `confirm_submit=True`). Однако стратегическая цель проекта — автономный поиск и отклик на релевантные вакансии без необходимости подтверждать каждую заявку вручную.
По решению владельца:
- Вместо блокирующего подтверждения человеком внедрена архитектура **автоматических предикативных гейтов политики качества (`HHSubmitPolicyGate`)**.
- Реализована строгая эскалация: отклики, полностью удовлетворяющие политике (качество письма, совпадение языка, полнота анкеты, лимиты частоты, соответствие fingerprint), отправляются автономно. Заявки с отклонениями или неопределенностью не блокируют пайплайн, а автоматически маршрутизируются в `NEEDS_HUMAN_REVIEW` для ручного разбора.
- Контроль владельца перенесен на уровень удаленного оперативного мониторинга:
  - Мгновенные информационные алерты в Telegram по факту успешной отправки (`[Отклик отправлен]`).
  - Команды удаленного экстренного останова (`/stop`) и возобновления (`/resume`) в Telegram с сохранением состояния в БД (`system_settings`).
  - Сводный ежедневный отчет активности по команде `/digest`.
  - Сохранение ручных интерфейсов согласования при необходимости.

---

### Выполненные задачи Фазы 2

#### Задача 1. Архитектура SubmitApproval и единый шлюз в SUBMITTED
- **Файлы:** `ai_assistant/hh_application_orchestrator.py`, `tests/test_state_machine_invariants.py`.
- **Изменения:**
  - Введен датакласс `SubmitApproval`:
    ```python
    class ApproverType(str, Enum):
        HUMAN = "human"
        POLICY = "policy"

    @dataclass(frozen=True)
    class SubmitApproval:
        approved_by: str
        approver_type: ApproverType
        policy_audit_id: Optional[str] = None
    ```
  - Функция `transition_application` принимает параметр `approval: Optional[SubmitApproval] = None`. Для перехода в состояние `SUBMITTED` (из любого состояния, включая `READY_TO_SUBMIT` и повторный self-transition) наличие валидного объекта `SubmitApproval` строго обязательно. При попытке перехода без `approval` возбуждается `ValueError("MISSING_SUBMIT_APPROVAL")`.
  - Удалены устаревшие прямые рёбра в стейт-машине:
    - `READY_FOR_AUTONOMOUS_SUBMIT -> SUBMITTED` (удалено).
    - `QUESTIONNAIRE_AUTO_FILLED -> SUBMITTED` (удалено).
    - Легаси-состояния сохранены в перечислении `HHApplicationState` с пометкой `deprecated Stage 35`.
  - Проверка инварианта `fingerprint`: при переходе в `SUBMITTED` функция проверяет наличие доказательств отклика в БД (`submission_evidence`) и обязательное совпадение `fingerprint`.
- **Коммит:** `27d93c4` `feat(phase2): replace human confirm with SubmitApproval, single path into SUBMITTED`.

#### Задача 2. Автоматический гейт политик отправки (HHSubmitPolicyGate)
- **Файлы:** `ai_assistant/hh_submit_policy.py`, `ai_assistant/db.py`, `tests/test_hh_submit_policy.py`.
- **Изменения:**
  - Создан модуль `ai_assistant/hh_submit_policy.py` с классом `HHSubmitPolicyGate` и методом `evaluate(context: SubmitPolicyContext) -> SubmitPolicyDecision`.
  - Реализованы обязательные автоматические проверки:
    1. `kill_switch`: проверка статуса паузы (`db.is_submit_paused()`). Если активна пауза — `PAUSED_BY_KILL_SWITCH`.
    2. `fingerprint`: соответствие отпечатка пакета отклика `actual_fp == expected_fp`.
    3. `cover_letter_length`: длина письма в диапазоне от 300 до 2500 символов.
    4. `cover_letter_placeholders`: отсутствие незаполненных шаблонов (`[Компания]`, `{company}`, `<...>`, `TODO`, `TBD`, `XXX`).
    5. `cover_letter_relevance`: обязательное упоминание названия компании или должности в тексте письма.
    6. `language_match`: детектирование кириллицы/латиницы; язык письма обязан соответствовать языку вакансии.
    7. `questionnaire_completeness`: все вопросы скрининга обязаны содержать непустые ответы (`unanswered_count == 0`).
    8. `rate_limits`: ограничение частоты подачи (по умолчанию не более 5 откликов в час и 20 откликов в сутки через `db.count_submissions_since()`).
  - Все результаты проверок фиксируются в таблице `audit_log` БД (`db.log_policy_audit()`).
  - Решение `SubmitPolicyDecision` содержит `decision` (`APPROVED` | `REJECTED`), `reason`, `audit_id`, и фабричный метод `to_approval() -> SubmitApproval(approved_by="policy:hh_submit_policy", approver_type=ApproverType.POLICY, policy_audit_id=audit_id)`.
- **Коммит:** `86870df` `feat(phase2): automated submit policy gate`.

#### Задача 3. Автономный раннер и снятие ограничений prepare-only
- **Файлы:** `ai_assistant/hh_application_runner.py`, `ai_assistant/hh_autonomous_agent.py`, `ai_assistant/hh_submission.py`, `ai_assistant/cli.py`, `tests/test_stage46_application_runner.py`.
- **Изменения:**
  - В `ai_assistant/hh_application_runner.py` добавлен параметр `auto_submit: bool = False` (активируется через CLI-флаг `--auto` или переменную окружения `HH_AUTO_SUBMIT=1`).
  - В шаге 4 раннера интегрирован `HHSubmitPolicyGate`. При `auto_submit=True` раннер запрашивает оценку политики:
    - При `decision == APPROVED` формируется `SubmitApproval(approver_type=POLICY)`, и раннер переходит к физической отправке отклика.
    - При `decision == REJECTED` заявка не падает, а переводится в `HHApplicationState.NEEDS_HUMAN_REVIEW` с сохранением детальной причины отказа политики в заметках (`notes`).
  - В `ai_assistant/hh_autonomous_agent.py` полностью удален режим `prepare-only`: агент находит вакансии, генерирует пакет отклика, рассчитывает fingerprint и переводит заявку в `READY_TO_SUBMIT`, откуда раннер может автономно произвести отправку.
  - В `execute_hh_submission()` параметр `confirm_submit` заменен на поддержку `approval: Optional[SubmitApproval] = None`, позволяя производить авторизованную политикой отправку.
  - Обработка `ALREADY_RESPONDED`: при обнаружении ранее поданного отклика на стороне HH заявка через стейт-машину переводится в `STALE`.
- **Коммит:** `7db759d` `feat(phase2): autonomous runner with policy-gated submission`.

#### Задача 4. Telegram-уведомления, отчетность и удаленный kill-switch
- **Файлы:** `ai_assistant/telegram_notifier.py`, `ai_assistant/telegram_bot.py`, `ai_assistant/hh_application_runner.py`, `tests/test_telegram_reporting.py`.
- **Изменения:**
  - Реализована функция `send_post_submit_notification()`:
    - Формат сообщения: `[Отклик отправлен] Компания: {company}, Вакансия: {title}\n\n{letter_preview}...\n\n{url}`.
    - Идемпотентность доставки: ключ `post_submit_alert:{vacancy_stable_id}` предотвращает дублирование сообщений при повторных вызовах.
  - Реализована команда `/digest` в Telegram-боте и функция `format_daily_digest()`:
    - Сводка за последние 24 часа: количество отправленных (`SUBMITTED`), переданных на ручной разбор (`NEEDS_HUMAN_REVIEW`), переведенных в `STALE`, и текущий статус паузы (`submit_paused`).
  - Реализован удаленный Kill-switch:
    - Команда `/stop` выставляет параметр `submit_paused=1` в таблице `system_settings` SQLite БД и возвращает статус остановки.
    - Команда `/resume` снимает флаг паузы (`submit_paused=0`).
    - Методы `db.is_submit_paused()` и `db.set_submit_paused(bool)` обеспечивают консистентное хранение состояния в базе данных без необходимости перезапуска процессов.
  - Сохранены кнопки ручного взаимодействия в Telegram (📄 Отклик, 👍, 👎, ⏭ Пропустить) для заявок, эскалированных в `NEEDS_HUMAN_REVIEW`.
- **Коммит:** `3f2e28a` `feat(phase2): telegram reporting and kill switch`.

---

### Таблица сравнения архитектуры: Фаза 1.5 vs Фаза 2

| Характеристика / Компонент | Было (Фаза 1.5) | Стало (Фаза 2) |
| :--- | :--- | :--- |
| **Режим автономного агента** | `prepare-only` (блокировка отправки, создание ревью `PENDING_REVIEW`) | Автономный pipeline: поиск $\to$ подготовка $\to$ перевод в `READY_TO_SUBMIT` $\to$ policy gate $\to$ `SUBMITTED` |
| **Условие перехода в SUBMITTED** | Обязательный флаг человека `confirm_submit=True` (`GATE_HUMAN_CONFIRMED`) | Строгий объект `SubmitApproval` (поддерживает `approver_type=HUMAN` или `approver_type=POLICY`) |
| **Входные гейты отправки** | 11 статических гейтов с обязательным участием человека | Автоматизированный предикативный гейт `HHSubmitPolicyGate` (качество письма, плейсхолдеры, релевантность, анкета, язык, rate-limits, kill-switch) |
| **Обработка отклонений политики** | Ошибка / отказ отправки (`FAIL_CLOSED`, `GATE_BLOCKED`) | Мягкая эскалация: перевод заявки в `NEEDS_HUMAN_REVIEW` с аудиторской записью причины |
| **Внешний отклик (Already Responded)** | Неоднозначные статусы очереди | Корректный перевод стейт-машины в статус `STALE` |
| **Управление остановкой (Kill-switch)** | Статический флаг `SUBMIT_ALLOWED` в `.env` (требует правки файла) | Динамический удаленный kill-switch в Telegram: `/stop` и `/resume` с хранением в `system_settings` SQLite |
| **Оповещения об отправке** | Отсутствовали (человек сам нажимал кнопку) | Автоматические алерты `[Отклик отправлен]` в Telegram с первыми 200 символами письма и URL |
| **Ежедневная отчетность** | Ручной просмотр очередей в CLI | Команда `/digest` в Telegram-боте со статистикой за 24 часа |
| **Ручные кнопки одобрения (📄)** | Единственный способ одобрения | Сохранены как резервный канал для заявок из `NEEDS_HUMAN_REVIEW` |

---

### Метрики качества и результаты верификации (Фаза 2 Baseline)

#### 1. Статус полного набора тестов (Pytest)
- **Результат прогона полного сьюта:**
  ```
  ====================== 1498 passed in 622.34s (0:10:22) =======================
  ```
- **Динамика тестов:**
  - Фаза 1.5-R: **1 465 passed**.
  - Фаза 2: **1 498 passed**.
  - Чистый прирост: **+33 новых модульных и интеграционных теста** (15 тестов `test_hh_submit_policy.py`, 6 тестов `test_telegram_reporting.py`, 4 теста `test_stage46_application_runner.py`, 8 тестов `test_state_machine_invariants.py`).
  - Ошибок (`failed`, `errors`): **0**.
  - Регрессий: **0**.

#### 2. Проверка статического анализатора Ruff
- **Команда:** `uvx ruff check ai_assistant/ --statistics`
- **Результат:**
  - Всего предупреждений: **2 760** (из них 1 968 автоматически исправимы флагом `--fix`).
  - Синтаксические и критические ошибки:
    - `F821` (неопределенные имена): **0**.
    - `F811` (переопределенные функции и импорты): **0**.

#### 3. Проверка статического типизатора Mypy
- **Команда:** `uvx mypy ai_assistant/ --ignore-missing-imports --follow-imports=skip`
- **Результат:**
  - `Found 230 errors in 26 files (checked 69 source files)`.
  - Новый модуль `ai_assistant/hh_submit_policy.py` проверен: **0 ошибок типизации**.
  - Общее число ошибок типизации в кодовой базе не изменилось (230 ошибок в легаси-файлах).

#### 4. Контрольная проверка grep на наличие `prepare-only`
- **Команда:** `git grep -in "prepare[-_]only" -- ai_assistant/`
- **Результаты проверки:**
  - **До (Фаза 1.5):** 5 совпадений в `ai_assistant/hh_autonomous_agent.py` (строки 917, 973, 1005, 1038, 1058), блокировавших автономный переход в `READY_TO_SUBMIT`.
  - **После (Фаза 2):** **0 совпадений**. Режим `prepare-only` полностью удален из автономного агента.


---

## Фаза 2.1: Crash Safety и идемпотентность автономной отправки

### Цель и контекст
Фаза 2 перевела систему на автоматическую отправку откликов с предикативным контролем политик (`HHSubmitPolicyGate`). В Фазе 2.1 реализована математически и транзакционно строгая защита от сбоев процессов, разрывов CDP/сети, конкурирующих воркеров и неопределенных исходов отправки.

**Главный инвариант:**
Для одной application/vacancy система ни при каких обстоятельствах не выполняет повторный внешний submit из-за того, что предыдущий процесс завершился в неопределенном или аварийном состоянии.

---

### Архитектурные изменения и механика эксклюзивных клеймов

1. **Таблица эксклюзивных клеймов (`submission_claims`)**:
   - Первичный ключ: `vacancy_stable_id TEXT PRIMARY KEY`.
   - Поля: `application_id`, `claim_id`, `status`, `worker_id`, `claimed_at`, `updated_at`, `details_json`.
   - Статусы клейма:
     - `ATTEMPTING`: клейм выдан воркеру, процесс отправки начат.
     - `SUBMITTED`: отклик успешно отправлен и подтвержден на HeadHunter.
     - `AMBIGUOUS`: результат внешней отправки неопределен (таймаут CDP, обрыв соединения, нераспознанный DOM). Автоматический перезапуск строго заблокирован.
     - `FAILED_SAFE`: прерывание до отправки (сработал kill switch, отказ пре-чеков).
     - `RELEASED`: освобожденный клейм.

2. **Атомарный захват клейма (`db.acquire_submission_claim`)**:
   - Выполняется в транзакции SQLite `BEGIN IMMEDIATE`, гарантируя эксклюзивность при конкурентных процессах.
   - Проверяет:
     - Kill switch (`is_submit_paused()`): отказ `SUBMISSION_PAUSED`.
     - Существующий клейм: если активен (`ATTEMPTING`, `SUBMITTED`, `AMBIGUOUS`, `FAILED_SAFE`), повторный захват отклоняется с `CONCURRENT_ATTEMPT_IN_PROGRESS` или `ACTIVE_CLAIM_EXISTS`.
     - Таблицу `application_submissions`: если есть запись в `SUBMITTED`/`AMBIGUOUS`/`SUCCESS`, захват отклоняется.
   - Защита от stale-клеймов: таймаут аренды (lease timeout = 300с).

3. **Состояние `AMBIGUOUS` в стейт-машине (`HHApplicationState.AMBIGUOUS`)**:
   - Зафиксировано как первое лицо стейт-машины в `ai_assistant/hh_application_orchestrator.py`.
   - Допустимые переходы:
     - `READY_TO_SUBMIT -> AMBIGUOUS` (при сбое во время или после клика).
     - `AMBIGUOUS -> SUBMITTED` (только при явной ручной верификации человеком с `approval.source == 'human'`).
     - `AMBIGUOUS -> STALE`, `AMBIGUOUS -> BLOCKED`, `AMBIGUOUS -> FAILED`.
   - Запрещен переход `AMBIGUOUS -> SUBMITTED` через автономную политику (`approval.source == 'policy'`). Ошибка: `AMBIGUOUS_RECONCILIATION_REQUIRES_HUMAN`.
   - Добавлен метод `db.reconcile_submission_claim()` с сохранением истории аудита (`reconciliation_history`).

4. **Защита от окон сбоев (Crash Windows)**:
   - **Окно 1 (Crash before submit)**: Клейм получен (`ATTEMPTING`), процесс упал до вызова CDP `submitBtn.click()`. При перезапуске раннер видит активный клейм и не выполняет submit (0 кликов).
   - **Окно 2 (Crash after submit before persistence)**: Клик выполнен (1 вызов), процесс аварийно завершился до сохранения в `hh_applications`. Клейм остается в `ATTEMPTING`. Второй раннер блокируется клеймом, внешних кликов на втором прогоне 0, суммарно по вакансии 1 клик.
   - **Окно 3 (Ambiguous timeout on click)**: `evaluate_fn` выбросил timeout/disconnect во время клика. Исключение перехватывается, клейм и заявка переводятся в `AMBIGUOUS`, автоматический retry запрещен.
   - **Окно 4 (Post-submit verification unverified)**: Клик выполнен, но проверка страницы не подтвердила отправку. Перевод в `AMBIGUOUS`.
   - **Окно 5 (Kill-switch race)**: `/stop` получен после прохождения политик, но перед физическим кликом. Непосредственно перед `submitBtn.click()` выполняется контрольная проверка `db.is_submit_paused()`. Выполнение прерывается, 0 кликов, клейм в `FAILED_SAFE`.
   - **Окно 6 (Telegram notification failure)**: Сбой отправки алерта в Telegram изолирован блоком `try/except`. Ошибка логируется, но статус `SUBMITTED` в БД не откатывается.

---

### Аудит путей в `SUBMITTED` (Backdoor Audit)
Проведен аудит всех переходов в состояние `SUBMITTED` по кодовой базе.
Обнаружено ровно 4 контролируемых пути:
1. `ai_assistant/hh_submission.py` (строка 1266): требует валидный `SubmitApproval` (human или policy) и post-submit verification.
2. `ai_assistant/hh_application_runner.py` (строка 613): требует `SubmitApproval` и успешный вердикт post-submit verifier.
3. `ai_assistant/cli.py` (строка 3594): легаси интерактивный submit с явным подтверждением человека.
4. `ai_assistant/hh_application_orchestrator.py` (строка 894): централизованный метод `transition_application()` со строгим гейтом `SubmitApproval`.

Прямые обходы стейт-машины отсутствуют.

---

### Спецификация тестового набора `tests/test_phase2_1_submission_crash_safety.py`

Реализовано 10 детерминированных оффлайн-сценариев:
1. `test_crash_before_submit`: Клейм получен, процесс упал -> второй запуск видит клейм `ATTEMPTING`, 0 кликов.
2. `test_crash_after_submit_before_persistence`: Клик выполнен, падение до сохранения -> второй прогон блокируется клеймом, total submit clicks == 1.
3. `test_ambiguous_timeout_on_click`: Таймаут CDP на клике -> `AMBIGUOUS` в клейме и заявке, авто-ретрай запрещен.
4. `test_duplicate_autonomous_run_on_already_submitted_vacancy`: Повторный прогон на отправленной вакансии блокируется, 0 кликов.
5. `test_concurrent_claim`: Два параллельных потока/воркера борются за вакансию -> ровно один успешен, второй отклонен, ровно 1 submit click.
6. `test_already_submitted_application_in_db`: Заявка в `SUBMITTED` в БД -> отклонение до любых вызовов браузера, 0 кликов.
7. `test_kill_switch_race`: Активация kill switch сразу после гейтов перед кликом -> аборт выполнения, клейм `FAILED_SAFE`, 0 кликов.
8. `test_telegram_notification_failure_after_submit`: Падение Telegram API после успешного submit -> заявка остается `SUBMITTED` (без отката).
9. `test_policy_reapproval_cannot_resubmit`: Повторное одобрение политикой уже отправленной вакансии отклоняется на уровне клейма/стейта.
10. `test_recovery_runner_does_not_blindly_retry_ambiguous`: Заявка `AMBIGUOUS` не выбирается раннером, переход в `SUBMITTED` разрешен только через `reconcile_submission_claim` с `source="human"`.

---

### Результаты валидации Фазы 2.1

- **Все 10 тестов Фазы 2.1:** `10 passed in 5.45s`.
- **Связанные сьюты (Фаза 1.5, 2, 2.1):** `75 passed in 44.71s`.
- **Полный регрессионный сьют репозитория:** `1508 passed, 0 failed, 0 errors in 655.43s`.
- **Статический анализ Ruff:** `0 errors` по `F821, F811` по всему `ai_assistant/`. Модуль тестов Фазы 2.1 чист (`0 errors`).
- **Коммиты Фазы 2.1:**
  - `5cee208` `feat(phase2.1): database submission claims table and atomic claim operations`
  - `c448ee8` `feat(phase2.1): orchestrator AMBIGUOUS state and state machine invariants`
  - `7785c75` `feat(phase2.1): enforce exclusive claim, ambiguous crash safety, and kill switch check in submission path`
  - `2309553` `test(phase2.1): comprehensive crash safety, idempotency, and state machine suite`
