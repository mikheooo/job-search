# Попутно найденные проблемы (Audit Follow-up)

В этом файле фиксируются аномалии и баги, найденные в процессе устранения замечаний аудита, которые не входят в исходное задание.

| № | Файл и строка | Описание проблемы | Оценка влияния |
|---|---------------|-------------------|----------------|
| 1 | `ai_assistant/application_tracking.py:167-168` | При переходе вакансии в статус `APPLIED` неявно вызывается `complete_review()`, переводящий ревью в статус `COMPLETED`. Из-за этого повторная проверка ревью через `get_application_review()` возвращает не `APPROVED`, что может приводить к неожиданному поведению внешних проверок. | Низкая. **Разобрано 2026-09-08: это задуманный lifecycle, а не баг.** `complete_review()` вызывается во **всех трёх** точках перехода в `APPLIED` (`set_application_status` — и create, и update; `transition_application`), то есть поведение последовательное. Менять его нельзя: инвариант нужен проверке целостности (`application_integrity.py:172` считает `APPROVED` валидным только при трекинге `READY_TO_APPLY`/`SUBMITTED`). Поведение закреплено тестами в `tests/test_application_review.py`. Реальный риск был в мёртвом хелпере `is_review_approved()`, который после `APPLIED` возвращает `False`, — ему дописан docstring с предупреждением. |
| 2 | `tests/test_stage31_watcher.py:435` | Тест использовал синтетический нечисловой идентификатор вакансии (`submit-flow-1` из адаптера `himalayas` с дефолтным `source`), имитируя при этом отправку на `hh.ru`. В follow-up к фазе 1 тест переведён на реалистичный числовой HH ID (`136591579`, `source="hh"`), а в `ai_assistant/hh_submission.py` закреплена строгая числовая валидация: для `source == "hh"` строковые ID строго fail-closed (`GATE_VACANCY_MATCH`). | Закрыто в follow-up к Фазе 1. |
| 3 | `ai_assistant/vacancy_identity.py:27-33` (`normalize_url`) | Набор `TRACKING_PARAMS` не включал параметры `from` и `hhtmfrom` (типичные для поисковой выдачи HeadHunter, например `?from=search_snippet&hhtmFrom=vacancy_search_list`). Из-за этого две одинаковые вакансии, полученные по разным ссылкам с отличающимися `from=...`, давали разные `normalized_url`, что создаёт риск дублирования вакансий в базе данных. | ~~Средняя~~ **Закрыто 2026-09-08**: в `TRACKING_PARAMS` добавлены `from`, `hhtmfrom`, `hhtmfromlabel`, `hhtmfrompage` (ключи сравниваются в нижнем регистре, поэтому `hhtmFrom` покрывается). Тесты в `tests/test_vacancy_identity.py`. Важно: правка влияет только на новые вычисления — ранее сохранённые `normalized_url` с этими параметрами остаются в БД и не склеиваются автоматически (см. примечание ниже). |
| 4 | `ai_assistant/vacancy_identity.py:311-343` (`get_canonical_by_normalized_url`) и `:347-366` (`get_all_canonical_vacancies`) | Столбцы читались со сдвигом на один: `first_seen_at=row[4]` (это `location`), `last_seen_at=row[5]` (это `first_seen_at`). `get_canonical_by_id()` читал правильно. | Низкая (даты канонических вакансий в памяти были перепутаны; на дедупликацию не влияло). **Закрыто 2026-09-08**: сдвиг устранён, добавлен регрессионный тест. |

**Примечание к №3 (данные):** повторный прогон `sync_identity_from_vacancies()` пересчитает `normalized_url`, но существующие дубли сам не склеит — для уже сохранённых строк вернётся `PROBABLE`, а не `EXACT`, и потребуется ручное слияние. Решение о склейке существующих данных остаётся за владельцем.


## Профиль производительности тестового набора (Slowest Tests Profile)

По результатам полного регрессионного прогона pytest (`--durations=20`, 1 465 тестов, общее время 609.13s / 10 мин 09 сек):

| Место | Длительность | Тест | Категория / Причина длительности |
|:-----:|:------------:|:-----|:---------------------------------|
| 1 | 15.25s | `tests/test_stage52_real_autonomous_agent.py::test_daemon_error_resilience_and_recovery` | Интеграционный тест демона с циклами ожидания и восстановления после ошибок |
| 2 | 4.34s | `tests/test_stage17d_pipeline.py::test_cli_prepare_integrates_form_step` | E2E CLI пайплайн с подготовкой анкеты, интеграцией форм и записью в БД |
| 3 | 3.65s | `tests/test_stage46_application_runner.py::test_confirm_submit_executes_one_and_halts` | Тест раннера контролируемой подачи: прогон стейт-машины, навигации и гейтов |
| 4 | 3.61s | `tests/test_stage47_state_machine_cleanup.py::test_successful_runner_flow_transitions_cleanly_to_submitted` | Прогон полного жизненного цикла переходов стейт-машины отклика |
| 5 | 3.60s | `tests/test_stage50_submit_selected_vacancy.py::test_stage50_execution_with_confirmation` | Эмуляция отправки выбранной вакансии с подтверждением |
