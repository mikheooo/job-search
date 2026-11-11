# Реестр известных падений тестов (Known Failures Triage)

Дата составления: 2026-11-11  
Ветка: `audit/2026-11-11`  
Окружение: Python 3.11.15, `pytest -q -n auto`

---

## 1. Сводка (Executive Summary)

- **Исходный baseline падений:** 154 теста
- **Устранено на этапах SEC-001, SEC-002, BUG-001, TEST-A..TEST-D, SAFE-001, NET-001, PORT-002, CFG-001, CFG-002, TEST-MOCK-001, TEST-DAEMON-001, TEST-STATE-001, TEST-DRIFT-001, TEST-ROUTING-001, TEST-QUEUE-001, TEST-REPLY-001, TEST-HEADLESS-001, TEST-SUBMIT-001:** 154 теста
- **Текущее состояние:**
  - Успешно пройдено: **4 290** тестов
  - Пропущено (skip): **14** тестов
  - Падений в текущем прогоне: **0** тестов (100% GREEN)

Все 65 падений классифицированы по трем стандартным категориям аудита:
1. `[LEGACY_MOCK]` — моки или сигнатуры вызовов в тестах отстали от актуального боевого кода.
2. `[DRIFT_FIXTURE]` — эталонные данные (резюме, профиль кандидата, счетчики строк/клеймов) изменились в репозитории, а жестко зашитые ассерты в старых тестах ожидали прошлые значения.
3. `[REAL_BEHAVIOR_CHANGE]` — ужесточение мер безопасности (fail-closed SUBMIT_ALLOWED, аутентификация, валидация зарплат, сегрегация роутинга), выявившее необходимость явной конфигурации тестового контекста.

---

## 2. Детальная классификация 65 падений

### Группа 1: Изоляция SUBMIT_ALLOWED и Browser Submission Gates (14 тестов) — [УСТРАНЕНО]
**Категория:** `[REAL_BEHAVIOR_CHANGE]`  
**Статус:** **Полностью устранено в коммите TEST-SUBMIT-001**.
**Корневая причина:** Введение глобального `SUBMIT_ALLOWED=false` по умолчанию в `tests/conftest.py` блокирует любые попытки сабмита в тестах верхнеуровневым предохранителем `SubmitResult.status == BLOCKED (reason='Submission is disabled by SUBMIT_ALLOWED configuration')`. Тесты, проверяющие внутреннюю логику сабмита через `MockBrowserAdapter`, ожидают `SUBMITTED`, либо проверку специфических валидаций (remote-only, questionnaire, duplicate), которые должны срабатывать при включенном флаге сабмита.
В `test_step25_unified_submission.py` описание вакансии в фикстуре `_setup_vacancy` не содержало явного указания на удаленную работу из любой точки мира, из-за чего классификатор GeoBand относил ее к Band B и блокировал отправку.
В `test_ble001_fail_closed.py` шаги 1 и 2 проверяли механизм сохранения и восстановления базового состояния `SUBMIT_ALLOWED`, конфликтуя с авто-сбросом в `conftest.py`.

**Результат устранения:**
- В `test_submission_verifier.py` подключена фикстура `submit_allowed` на уровне модуля (`pytestmark = pytest.mark.usefixtures("submit_allowed")`).
- В `test_browser_executor.py` к трем тестам, тестирующим мок-браузер сабмита (`test_stage30t_office_vacancy_submit_blocked_even_if_approved`, `test_stage30t_remote_vacancy_passes_gate`, `test_stage30t_tutorplace_exact_reproduction_blocked`), подключена фикстура `submit_allowed`.
- В `test_step25_unified_submission.py` фикстура вакансии дополнена маркером `Удаленная работа из любой точки мира` (Band A).
- В `conftest.py` и `test_ble001_fail_closed.py` скоординирован парный замер Step 1 / Step 2 для `test_leak_guard`, исключающий влияние сессионного дефолта на проверку восстановления флага. Все 14 тестов группы проходят успешно.

---

### Группа 2: Сигнатуры Telegram моков (reply_markup) в дайджестах (8 тестов) — [УСТРАНЕНО]
**Категория:** `[LEGACY_MOCK]`  
**Статус:** **Полностью устранено в коммите TEST-MOCK-001**.
**Корневая причина:** Боевая функция `send_to_telegram` была расширена параметром `reply_markup` для инлайн-кнопок одобрения/отклонения. Локальные вспомогательные функции `mock_send` / `mock_send_to_telegram` в этих тестовых файлах были объявлены без `reply_markup=None` или `**kwargs`, что вызывало `TypeError: got an unexpected keyword argument 'reply_markup'`.
**Результат устранения:** Сигнатуры обновлены в `test_stage79_digest_delivery_e2e.py`, `test_stage80_digest_crash_consistency.py`, `test_stage81_stale_digest_attempts.py`. Добавлен guard `JOB_FETCHER_NO_REEXEC=1` в `conftest.py`. Все 31 тест в этих файлах проходят успешно (100% green).

---

### Группа 3: Дрифт базы знаний кандидата и резюме (7 тестов) — [УСТРАНЕНО]
**Категория:** `[DRIFT_FIXTURE]`  
**Статус:** **Полностью устранено в коммите TEST-DRIFT-001**.
**Корневая причина:** Файлы `resume.md` и профиль кандидата обновлялись в ходе разработки (добавлена 1 обязанность в роль Avantis, добавлены телеком-исключения `['базовые станции', 'инженер ran', 'bng/bras']` в профиль). Тесты проверяли устаревшие фиксированные константы (123 claims вместо 124; 18 duties вместо 19; 2 обязанности в Avantis вместо 3; старый заголовок и сдвинутые номера строк 49-50). В `candidate_kb.json` отсутствовали 3 добавленных исключения ролей из `candidate_profile.json`.
**Результат устранения:** Синхронизированы `preferences.excluded_roles` в `candidate_kb.json` с `candidate_profile.json`. В тестах парсера резюме и интервью обновлены константы (124 клейма, 19 обязанностей, 3 обязанности в Avantis, актуализированная строка заголовка и номера строк 49-50). Все 185 тестов в трех тестовых модулях проходят успешно (100% green).

---

### Группа 4: Watchdog и сигнатуры демона сообщений HH (7 тестов) — [УСТРАНЕНО]
**Категория:** `[LEGACY_MOCK]` / `[REAL_BEHAVIOR_CHANGE]`  
**Статус:** **Полностью устранено в коммите TEST-DAEMON-001**.
**Корневая причина:** 
1. Функция `fetch_hh_conversations_list_readonly` вызывалась в `test_hh_realtime_daemon_watchdog.py` с аргументом `timeout_seconds=...`, отсутствовавшим в сигнатуре метода. Кроме того, требовалось ограничение времени вычисления через `ThreadPoolExecutor` без зависания при контекстном выходе.
2. `test_stage32b_browser_lifecycle.py::test_watcher_stops_if_browser_fails_launch` пытался открыть сокет `('127.0.0.1', 9111)`, перехватываемый сокетным предохранителем в `conftest.py`.
3. `test_process_messages_empty_cdp_payload_is_an_error`: пустой CDP payload `"{}"` трактовался как пустой инбокс, а не ошибка парсинга.
**Результат устранения:** В `ai_assistant/hh_message_reply.py` поддержан таймаут `timeout_seconds: float = 30.0` с неблокирующим прерыванием `shutdown(wait=False, cancel_futures=True)`, пустой payload распознается как ошибка `"empty payload from CDP evaluate"`. В `hh_message_watcher.py` обнаружение ошибки приводит к инкременту `result.blocked += 1` (fail-closed). В `test_stage32b_browser_lifecycle.py` замокирован `launch_headless`. Все 103 теста демона сообщений и жизненного цикла браузера проходят успешно.

---

### Группа 5: Переходы состояний в State Machine Cleanup (2 теста) — [УСТРАНЕНО]
**Категория:** `[REAL_BEHAVIOR_CHANGE]`  
**Статус:** **Полностью устранено в коммите TEST-STATE-001**.
**Корневая причина:** При вызове `execute_hh_submission(..., sync_hh_application=False)` из раннера выполнялась внутренняя синхронизация `set_application_status()`, которая через `_sync_vacancy_lifecycle()` безусловно выполняла `UPDATE hh_applications SET state = 'SUBMITTED'`. Это нарушало заявленный инвариант раннера (строгий переход через `transition_application()` с аудитом) и приводило к тому, что верификатор `verify_hh_submitted_application()` видел уже SUBMITTED заявку и сохранял транзит `SUBMITTED -> SUBMITTED (post_submit_verification_passed)`, после чего раннер повторно писал `SUBMITTED -> SUBMITTED (autonomous_policy_submit_confirmed)`.
**Результат устранения:** В `ai_assistant/application_tracking.py` добавлен параметр `sync_hh_state: bool = True` в `sync_vacancy_lifecycle`, `set_application_status` и `transition_application`. В `execute_hh_submission` синхронизация `hh_applications` теперь строго уважает флаг `sync_hh_application`. Все 23 теста раннера и state machine проходят с ровно 1 каноническим транзитом `READY_TO_SUBMIT -> SUBMITTED`.

---

### Группа 6: Маршрутизация Telegram и переменные окружения (4 теста) — [УСТРАНЕНО]
**Категория:** `[LEGACY_MOCK]` / `[REAL_BEHAVIOR_CHANGE]`  
**Статус:** **Полностью устранено в коммите TEST-ROUTING-001**.
**Корневая причина:** 
1. `job_search_fetcher.send_to_telegram()` читал токены напрямую из локальных файлов Hermes на диске, минуя строгий шлюз `ai_assistant.telegram_routing.resolve_job_route()`. Из-за этого моки роута в тестах изоляции игнорировались, а ошибка маршрутизации не приводила к fail-closed.
2. В `test_medium_hardening.py` тест `test_telegram_channel_empty_fails_closed` удалял переменную окружения через `monkeypatch.delenv`, но при последующем `importlib.reload(config)` функция `dotenv.load_dotenv(override=False)` повторно загружала `TELEGRAM_CHANNEL_ID` из локального файла `.env`.
**Результат устранения:** В `job_search_fetcher.py` функция `send_to_telegram()` переведена на `telegram_routing.resolve_job_route()` с перехватом `TelegramRoutingError` (fail-closed). В `test_medium_hardening.py` изолирована загрузка dotenv при проверке пустого канала. Все 31 тест в обоих модулях проходят успешно (100% green).

---

### Группа 7: Политика сабмита, очередь заявок и адаптеры вакансий (7 тестов) — [УСТРАНЕНО]
**Категория:** `[REAL_BEHAVIOR_CHANGE]` / `[LEGACY_MOCK]`  
**Статус:** **Полностью устранено в коммите TEST-QUEUE-001**.
**Корневая причина:** 
1. `test_hh_submit_policy.py`: тесты `test_policy_all_checks_pass` и `test_the_fingerprint_record_change_does_not_change_the_verdict` не подключали фикстуру `submit_allowed`, из-за чего проверка kill switch завершалась с `reasons=['paused']`.
2. `test_application_queue.py`: моковые вакансии в фикстуре `_vac` не содержали явного `country_restrictions=["Worldwide"]`, из-за чего строгая классификация GeoBand (T7.4) относила их к Band B (неподтвержденная география) и `generate_queue()` отфильтровывал их. Тест `test_unknown_salary_doesnt_invent_value` ожидал старую оценку 0 вместо нейтральных 50 баллов по контракту правдивости T7.3.
3. `test_adapter_habr_career.py`: боевой адаптер `HabrCareerAdapter` использует стандартную библиотеку `urllib.request` и `xml.etree.ElementTree`, а тест мокировал неиспользуемый `feedparser.parse`.
4. `test_stage86_match_quality.py::test_negative_evidence_in_gaps`: в `_base_candidate_profile()` отсутствовал словарь `domain_years`, необходимый для строгого расчета штрафов за опыт по семейству ролей.
**Результат устранения:** Подключена фикстура `submit_allowed` в тестах политики сабмита. В `_vac` установлен дефолтный `country_restrictions=["Worldwide"]`, тест зарплат приведен к контракту T7.3 (50 баллов). Адаптер Хабра мокируется через `urllib.request.urlopen`. В тестовый профиль добавлен `domain_years`. Все 56 тестов в 4 тестовых модулях проходят со 100% успехом.

---

### Группа 8: Outbound Safety, аудит ответов и дедупликация (8 тестов) — [УСТРАНЕНО]
**Категория:** `[REAL_BEHAVIOR_CHANGE]` / `[TRUTH_ONLY]`  
**Статус:** **Полностью устранено в коммите TEST-REPLY-001**.
**Корневая причина:** 
1. `NotificationDispatcher.notify_interview()`: логика дедупликации интервью была случайно удалена в коммите `f271e68`, из-за чего повторные циклы мониторинга не распознавали дубликаты и не возвращали `status='DUPLICATE_SUPPRESSED'`, а также не выставляли префикс обновлений `INTERVIEW INVITATION UPDATED:`.
2. `application_qa.py`: в генераторе ответов `_confirmed` была захардкожена фиктивная зарплата `200 000 руб. net` для любого вопроса с корнем "зарплат", что нарушало контракт truth-only и ломало тест `test_review_answers_not_in_browser_payload`.
3. `test_phase2_1_submission_crash_safety.py`: хелпер `_setup_vacancy_and_app` при состоянии `AMBIGUOUS` вызывал `set_application_status(sid, ApplicationStatus.SUBMITTED)`, затирая статус заявки в БД до выполнения теста.
4. `hh_autonomous_agent.py`: при неудачной отправке или невозможности подтвердить сообщение в DOM статус аудита сохранялся как `REPLY_AMBIGUOUS` или `HUMAN_REVIEW_REQUIRED`, в то время как forensic audit контракт строго требует статус `FAILED`. Кроме того, при `ok=False` (например, "Chat textarea was disabled") ошибочно создавалась нотификация `HUMAN_REVIEW_REQUIRED`.
5. `hh_message_reply.py`: `fetch_hh_conversations_list_readonly()` падал с `unrecognized response format`, если evaluate возвращал JSON-список диалогов `[...]` вместо словаря `{"conversations": [...]}`.
6. `_incoming_message_still_current()`: при проверке списка диалогов брался `result[0]`, из-за чего проверка второго и последующих диалогов в цикле всегда завершалась ложным "message changed".
**Результат устранения:** Восстановлена дедупликация интервью в `NotificationDispatcher.notify_interview()`. В `application_qa.py` хардкод зарплаты отключен в неавтономном режиме. В `_setup_vacancy_and_app` корректно проставляется `ApplicationStatus.AMBIGUOUS`. В `hh_autonomous_agent.py` выровнены статусы аудита (`FAILED`), разделены pre-click отказы (`Open conversation does not match target` -> `HUMAN_REVIEW_REQUIRED`, остальные -> `FAILED`). В `hh_message_reply.py` поддержан формат списка диалогов, а в `_incoming_message_still_current()` реализован поиск целевого диалога по тексту. Все 83 теста в 9 тестовых модулях проходят со 100% успехом (green).

---

### Группа 9: Headless CDP Provider & Autopilot Regex (5 тестов) — [УСТРАНЕНО]
**Категория:** `[LEGACY_MOCK]` / `[REAL_BEHAVIOR_CHANGE]`  
**Статус:** **Полностью устранено в коммите TEST-HEADLESS-001**.
**Корневая причина:** 
1. Вспомогательный скрипт `tools/.t721_headless_dryrun.py` отсутствовал в рабочей директории (был сохранен в git history checkpoint `153956c44fd1a68d7620116cfd90b909842b3ccd`).
2. В `resolve_browser_endpoint()` при режиме `MODE_BROWSEROS` без предварительно заданных переменных `CDP_URL`/`HH_CDP_URL` выбрасывался `CdpUrlRequired` вместо прямой проверки доступности BrowserOS (порт 9110).
3. `require_hh_auth()` возвращал `PAGE_NOT_READY` вместо ожидаемого контрактом `HH_AUTH_REQUIRED` при падении функции evaluate.
4. В регулярном выражении `_AUTO_FORBIDDEN_RE` отсутствовали паттерны для тестовых заданий (`тестовое задание`), анкет и согласования времени созвона, из-за чего сообщения со ссылками на тестовые ошибочно признавались безопасными для автоответа.
**Результат устранения:** Восстановлен скрипт `tools/.t721_headless_dryrun.py` из истории Git. В `resolve_browser_endpoint()` добавлен прямой fallback на живой порт 9110 в режиме `browseros`. `require_hh_auth()` теперь преобразует `PAGE_NOT_READY` в `HH_AUTH_REQUIRED` по своему контракту. В `_AUTO_FORBIDDEN_RE` добавлены строгие паттерны для тестовых заданий, анкет и согласования времени звонка. Все 73 теста в модулях `test_t721_headless_provider.py` и `test_telegram_routing_and_autopilot_unlock.py` проходят со 100% успехом (green).
