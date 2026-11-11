# Реестр известных падений тестов (Known Failures Triage)

Дата составления: 2026-11-11  
Ветка: `audit/2026-11-11`  
Окружение: Python 3.11.15, `pytest -q -n auto`

---

## 1. Сводка (Executive Summary)

- **Исходный baseline падений:** 154 теста
- **Устранено на этапах SEC-001, SEC-002, BUG-001, TEST-A, TEST-B, TEST-C, TEST-D:** 89 тестов
- **Текущее состояние:**
  - Успешно пройдено: **4 225** тестов (98.5%)
  - Пропущено (skip): **14** тестов
  - Падений в текущем прогоне: **65** тестов

Все 65 падений классифицированы по трем стандартным категориям аудита:
1. `[LEGACY_MOCK]` — моки или сигнатуры вызовов в тестах отстали от актуального боевого кода.
2. `[DRIFT_FIXTURE]` — эталонные данные (резюме, профиль кандидата, счетчики строк/клеймов) изменились в репозитории, а жестко зашитые ассерты в старых тестах ожидали прошлые значения.
3. `[REAL_BEHAVIOR_CHANGE]` — ужесточение мер безопасности (fail-closed SUBMIT_ALLOWED, аутентификация, валидация зарплат, сегрегация роутинга), выявившее необходимость явной конфигурации тестового контекста.

---

## 2. Детальная классификация 65 падений

### Группа 1: Изоляция SUBMIT_ALLOWED и Browser Submission Gates (13 тестов)
**Категория:** `[REAL_BEHAVIOR_CHANGE]`  
**Корневая причина:** Введение глобального `SUBMIT_ALLOWED=false` по умолчанию в `tests/conftest.py` блокирует любые попытки сабмита в тестах верхнеуровневым предохранителем `SubmitResult.status == BLOCKED (reason='Submission is disabled by SUBMIT_ALLOWED configuration')`. Тесты, проверяющие внутреннюю логику сабмита через `MockBrowserAdapter`, ожидают `SUBMITTED`, либо проверку специфических валидаций (remote-only, questionnaire, duplicate), которые должны срабатывать при включенном флаге сабмита.

**Затронутые тесты:**
1. `tests/test_submission_verifier.py::test_stage30e_confirmation_gate_and_verification_suite`
2. `tests/test_submission_verifier.py::test_stage30f_ambiguous_post_submit_verification_invariants`
3. `tests/test_submission_verifier.py::test_stage30g_flow_classification_and_routing`
4. `tests/test_submission_verifier.py::test_stage30l_verification_decision_matrix_and_idempotency_suite`
5. `tests/test_submission_verifier.py::test_stage30n_orchestration_e2e_safety_gate_suite`
6. `tests/test_submission_verifier.py::test_stage30o_queue_to_apply_orchestrator_integration_suite`
7. `tests/test_submission_verifier.py::test_stage30p_authenticated_session_and_account_aware_gate_suite`
8. `tests/test_submission_verifier.py::test_stage30r_real_single_application_confirm_submit_suite`
9. `tests/test_submission_verifier.py::test_stage30s_hh_post_submit_questionnaire_handling_suite`
10. `tests/test_browser_executor.py::test_stage30t_office_vacancy_submit_blocked_even_if_approved`
11. `tests/test_browser_executor.py::test_stage30t_remote_vacancy_passes_gate`
12. `tests/test_browser_executor.py::test_stage30t_tutorplace_exact_reproduction_blocked`
13. `tests/test_step25_unified_submission.py::test_path_a_browser_executor_dry_run`
14. `tests/test_ble001_fail_closed.py::test_leak_guard_step_2_sees_the_value_restored` (проверка парного сброса переменной между двумя зависимыми тестами шага)

**План устранения:**
- В соответствующих тестах передачи мок-браузера подключать фикстуру `submit_allowed` / `monkeypatch.setenv("SUBMIT_ALLOWED", "true")`.
- Для `test_leak_guard_step_2_sees_the_value_restored`: скоординировать сохранение базового состояния без конфликта с глобальным авто-сбросом.

---

### Группа 2: Сигнатуры Telegram моков (reply_markup) в дайджестах (8 тестов)
**Категория:** `[LEGACY_MOCK]`  
**Корневая причина:** Боевая функция `send_to_telegram` была расширена параметром `reply_markup` для инлайн-кнопок одобрения/отклонения. Локальные вспомогательные функции `mock_send` / `mock_send_to_telegram` в этих тестовых файлах были объявлены без `reply_markup=None` или `**kwargs`, что вызывает `TypeError: got an unexpected keyword argument 'reply_markup'`.

**Затронутые тесты:**
1. `tests/test_stage79_digest_delivery_e2e.py::test_successful_delivery_marks_digest_items_delivered`
2. `tests/test_stage79_digest_delivery_e2e.py::test_failed_delivery_does_not_mark_anything_delivered`
3. `tests/test_stage79_digest_delivery_e2e.py::test_retry_after_failure_returns_same_vacancies`
4. `tests/test_stage80_digest_crash_consistency.py::test_attempt_is_persisted_before_telegram_send`
5. `tests/test_stage80_digest_crash_consistency.py::test_confirmed_success_transitions_attempt_to_delivered`
6. `tests/test_stage80_digest_crash_consistency.py::test_post_send_db_failure_does_not_allow_blind_resend`
7. `tests/test_stage81_stale_digest_attempts.py::test_stale_attempt_does_not_block_unrelated_new_vacancies`
8. `tests/test_stage81_stale_digest_attempts.py::test_concurrent_workers_exact_single_telegram_send`

**План устранения:**
- Добавить `reply_markup=None, **kwargs` в сигнатуры локальных тестовых моков `mock_send` / `mock_send_to_telegram` в файлах `test_stage79_digest_delivery_e2e.py`, `test_stage80_digest_crash_consistency.py`, `test_stage81_stale_digest_attempts.py`.

---

### Группа 3: Дрифт базы знаний кандидата и резюме (7 тестов)
**Категория:** `[DRIFT_FIXTURE]`  
**Корневая причина:** Файлы `resume.md` и профиль кандидата обновлялись в ходе разработки (добавлена 1 обязанность в роль Avantis, добавлены телеком-исключения `['базовые станции', 'инженер ran', 'bng/bras']` в профиль). Тесты проверяли фиксированные константы (123 claims вместо 124; 18 duties вместо 19; 2 обязанности в Avantis вместо 3).

**Затронутые тесты:**
1. `tests/test_candidate_interview.py::test_a_full_interview_leaves_every_resume_claim_untouched` (124 vs 123)
2. `tests/test_candidate_interview.py::test_a_resume_merge_still_refreshes_every_resume_claim` (124 vs 123)
3. `tests/test_candidate_resume_parser.py::test_avantis_role_pin` (3 vs 2)
4. `tests/test_candidate_resume_parser.py::test_every_duty_is_bound_to_a_real_role` (19 vs 18)
5. `tests/test_candidate_resume_parser.py::test_todays_measured_totals` (124 vs 123)
6. `tests/test_candidate_resume_parser.py::test_known_line_numbers_are_exact` (KeyError заголовка/номеров строк)
7. `tests/test_profile_render.py::test_the_live_kb_reproduces_the_live_profile` (несовпадение списка `excluded_roles`)

**План устранения:**
- Синхронизировать константы счетчиков и эталонные срезы в тестах парсера резюме с актуальным состоянием `resume.md`.

---

### Группа 4: Watchdog и сигнатуры демона сообщений HH (6 тестов)
**Категория:** `[LEGACY_MOCK]` / `[REAL_BEHAVIOR_CHANGE]`  
**Корневая причина:** 
1. Функция `fetch_hh_conversations_list_readonly` вызывается в `test_hh_realtime_daemon_watchdog.py` с аргументом `timeout_seconds=...`, отсутствующим в сигнатуре метода.
2. `test_stage32b_browser_lifecycle.py::test_watcher_stops_if_browser_fails_launch` пытается открыть сокет `('127.0.0.1', 9111)`, перехватываемый сокетным предохранителем в `conftest.py`.
3. `test_process_messages_empty_cdp_payload_is_an_error`: ожидает `'ERROR'`, возвращает `'BLOCKED'`.

**Затронутые тесты:**
1. `tests/test_hh_realtime_daemon_watchdog.py::test_fetch_timeout_is_an_error_and_does_not_fall_through`
2. `tests/test_hh_realtime_daemon_watchdog.py::test_fetch_outer_timeout_does_not_wait_out_a_hung_evaluate`
3. `tests/test_hh_realtime_daemon_watchdog.py::test_fetch_empty_cdp_payload_is_not_an_empty_inbox`
4. `tests/test_hh_realtime_daemon_watchdog.py::test_fetch_chatik_list_still_returns_conversations`
5. `tests/test_hh_realtime_daemon_watchdog.py::test_fetch_non_transport_chatik_failure_falls_back_to_dom`
6. `tests/test_hh_realtime_daemon_watchdog.py::test_process_messages_empty_cdp_payload_is_an_error`
7. `tests/test_stage32b_browser_lifecycle.py::test_watcher_stops_if_browser_fails_launch`

**План устранения:**
- Привести сигнатуру `fetch_hh_conversations_list_readonly` к поддержке параметра `timeout_seconds` (или обновить вызовы в тестах).
- Замокировать сетевой вызов проверки порта в `test_stage32b_browser_lifecycle.py`.

---

### Группа 5: Переходы состояний в State Machine Cleanup (2 теста)
**Категория:** `[REAL_BEHAVIOR_CHANGE]`  
**Корневая причина:** Метод `save_hh_application` при начальном создании заявки фиксирует событие инициализации перехода (`None -> READY_TO_SUBMIT`), после чего выполнение раннера фиксирует второй переход (`READY_TO_SUBMIT -> SUBMITTED`). Ассерты `assert len(transitions) == 1` не учитывали запись начального перехода.

**Затронутые тесты:**
1. `tests/test_stage46_application_runner.py::test_autonomous_mock_cycle_new_to_submitted_zero_human_confirm`
2. `tests/test_stage47_state_machine_cleanup.py::test_successful_runner_flow_transitions_cleanly_to_submitted`

**План устранения:**
- Проверить контракт аудита переходов `list_hh_application_transitions`: отфильтровать или учесть начальный транзит `from_state is None`.

---

### Группа 6: Маршрутизация Telegram и переменные окружения (4 теста)
**Категория:** `[LEGACY_MOCK]` / `[REAL_BEHAVIOR_CHANGE]`  
**Корневая причина:** Изоляция каналов оповещений (`TELEGRAM_JOBS_CHAT_ID` vs `TELEGRAM_CHAT_ID`) и фикстуры токенов.

**Затронутые тесты:**
1. `tests/test_job_telegram_isolation.py::test_digest_sender_uses_jobs_route`
2. `tests/test_job_telegram_isolation.py::test_digest_sender_fails_closed_without_jobs_route`
3. `tests/test_medium_hardening.py::test_telegram_channel_empty_fails_closed`
4. `tests/test_medium_hardening.py::test_telegram_channel_reads_env`

**План устранения:**
- Скорректировать фикстуры окружения в тестах для изоляции конфигурации каналов Telegram.

---

### Группа 7: Политика сабмита, очередь заявок и адаптеры вакансий (7 тестов)
**Категория:** `[REAL_BEHAVIOR_CHANGE]` / `[LEGACY_MOCK]`  
**Корневая причина:** 
1. `test_hh_submit_policy.py`: глобальное состояние паузы или неинициализированный профиль возвращает `reasons=['paused']`.
2. `test_application_queue.py`: генерация очереди с валидацией зарплат и фильтрацией статусов.
3. `test_adapter_habr_career.py`: мок `feedparser.parse` возвращает 0 вакансий из-за структуры словарей RSS.
4. `test_stage86_match_quality.py::test_negative_evidence_in_gaps`.

**Затронутые тесты:**
1. `tests/test_application_queue.py::test_unknown_salary_doesnt_invent_value`
2. `tests/test_application_queue.py::test_only_ready_to_apply_enters_queue`
3. `tests/test_application_queue.py::test_repeated_generation_is_idempotent`
4. `tests/test_hh_submit_policy.py::test_policy_all_checks_pass`
5. `tests/test_hh_submit_policy.py::test_the_fingerprint_record_change_does_not_change_the_verdict`
6. `tests/test_adapter_habr_career.py::test_habr_career_adapter_fetches_and_parses`
7. `tests/test_stage86_match_quality.py::test_negative_evidence_in_gaps`

**План устранения:**
- Сбросить флаг паузы перед тестами политики сабмита.
- Скорректировать структуру фикстуры `SAMPLE_HABR_RSS` / мока `feedparser`.

---

### Группа 8: Outbound Safety, аудит ответов и дедупликация (14 тестов)
**Категория:** `[REAL_BEHAVIOR_CHANGE]` / `[LEGACY_MOCK]`  
**Корневая причина:** Механизмы дедупликации нотификаций и статусы ответов (`REPLY_AMBIGUOUS` vs `FAILED`, `DUPLICATE_SUPPRESSED`).

**Затронутые тесты:**
1. `tests/test_p0_integration_lifecycle.py::TestP0IntegrationLifecycle::test_full_lifecycle_e2e`
2. `tests/test_p0_stabilization.py::test_notification_dispatcher_suppresses_duplicate_alerts`
3. `tests/test_stage20d_snapshot.py::test_review_answers_not_in_browser_payload`
4. `tests/test_phase2_1_submission_crash_safety.py::test_recovery_runner_does_not_blindly_retry_ambiguous`
5. `tests/test_stage59_autonomous_reply_telegram_audit.py::test_auto_reply_missing_in_hh_blocks_notification`
6. `tests/test_stage61_telegram_reply_forensic_audit.py::test_stage61_generated_not_sent_blocks_notification`
7. `tests/test_stage53_reply_visibility.py::test_failed_reply_does_not_create_sent_notification`
8. `tests/test_stage55_selection_limits_routing.py::test_stage55_recruiter_routing_to_exact_conversation`
9. `tests/test_stage80_digest_crash_consistency.py::test_confirmed_failure_remains_retryable`
10. `tests/test_outbound_safety_all_paths.py::TestOutboundSafetyAllPaths::test_path3_realtime_daemon_autonomous_reply_disabled_flag`
11. `tests/test_outbound_safety_all_paths.py::TestOutboundSafetyAllPaths::test_path3_realtime_daemon_autonomous_reply_governor_guard`

**План устранения:**
- Выверить соответствие статусов `REPLY_AMBIGUOUS` и `FAILED` в контракте forensic audit.
- Актуализировать структуру словаря диспетчера алертов при дедупликации (`status='DUPLICATE_SUPPRESSED'`).

---

### Группа 9: Headless CDP Provider & Autopilot Regex (4 теста)
**Категория:** `[LEGACY_MOCK]` / `[REAL_BEHAVIOR_CHANGE]`  
**Корневая причина:** 
1. Отсутствие вспомогательного скрипта `tools/.t721_headless_dryrun.py` (был удален или не закоммичен).
2. Ошибка запуска CDP provider без указания `HH_CDP_URL`.
3. Регулярные выражения стоп-слов автоответов (`FORBIDDEN_PATTERNS`).

**Затронутые тесты:**
1. `tests/test_t721_headless_provider.py::test_a_failing_evaluate_is_auth_required_not_a_crash`
2. `tests/test_t721_headless_provider.py::test_the_dry_run_tool_never_clicks`
3. `tests/test_t721_headless_provider.py::test_browseros_mode_still_reaches_9110_when_it_is_up`
4. `tests/test_telegram_routing_and_autopilot_unlock.py::TestAutoReplyFlagsAndGates::test_auto_forbidden_re_strictly_matches_critical_topics`
5. `tests/test_telegram_routing_and_autopilot_unlock.py::TestAutoReplyFlagsAndGates::test_is_safe_for_auto_reply_evaluation`

**План устранения:**
- Создать/восстановить безопасную заглушку `tools/.t721_headless_dryrun.py`.
- Дополнить регулярные выражения критических стоп-тем автоответов паттерном для тестовых заданий (`тестовое задание`).
