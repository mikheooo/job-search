# Документация job-search — Канонический индекс (Docs Index)

Добро пожаловать в единую точку входа документации проекта `job-search`. Проект представляет собой автоматизированную персональную систему мониторинга вакансий, подготовки откликов, решения тестовых анкет и безопасного взаимодействия с платформами поиска работы (HeadHunter, Freelance/Gig биржи, Email).

---

## 1. Архитектура и текущее состояние

- **[ARCHITECTURE.md](../ARCHITECTURE.md)** — Сквозная архитектура системы: пайплайны, компоненты сбора, раннеры, интеграция с браузером и протоколы согласования.
- **[PROJECT_STATE.md](../PROJECT_STATE.md)** — Текущий статус проекта, пройденные этапы (Stages), реестр реализованных возможностей и журнал изменений.
- **[README.md](../README.md)** — Руководство по быстрому старту, установке и базовым командам.
- **Кроссплатформенный запуск:**
  ```bash
  python -m ai_assistant --help
  ```

---

## 2. Конфигурация и сетевой периметр

- **[environment_variables.md](environment_variables.md)** — **Полный канонический реестр переменных окружения**. Все переменные синхронизированы с `.env.example` и проверяются автоматическим тестом `tests/test_env_inventory.py`.
- **[dashboard-network-perimeter.md](dashboard-network-perimeter.md)** — Архитектура безопасности сетевого периметра веб-дашборда, политика привязки к `127.0.0.1` (loopback-only) и требования обязательной аутентификации токеном.
- **[automation_alerts.md](automation_alerts.md)** — Спецификация системы алертов и мониторинга фоновых процессов.

---

## 3. Безопасность и Fail-Closed защита (Safety & Outbound Governance)

- **Центральный предохранитель (`ai_assistant/outbound_governor.py`)** — Единый шлюз проверки всех исходящих мутаций (`guard_outbound` / `outbound_governor`). Защищает все каналы:
  - HH Submissions (`ai_assistant/hh_submission.py`, `ai_assistant/hh_autonomous_agent.py`)
  - External Form Submissions (`ai_assistant/external_form_filler.py`)
  - Email Outreach (`ai_assistant/email_outreach.py`)
  - Telegram Notifications (`ai_assistant/telegram/transport.py`, `ai_assistant/telegram_notifier.py`, `integrations/hermes/job_search_fetcher.py`)
- **Политики одобрения (`SubmitApproval`)** — Строгая валидация сопроводительных писем и ответов (`ai_assistant/hh_submit_policy.py`). Запрет шаблонов-заглушек (`{{...}}`, `[ВСТАВИТЬ]`, `TODO`, `<PLACEHOLDER>`).
- **[ble001_triage.md](ble001_triage.md)** — Реестр мест обработки исключений (BLE001), классификация и гарантии fail-closed поведения. Контролируется регрессионным тестом `tests/test_report_line_refs.py`.

---

## 4. Контроль качества, тестирование и аудит

- **[audit_remediation_log.md](audit_remediation_log.md)** — Журнал устранения находок внешнего статического аудита (2026-09-04 — 2026-11-12).
- **[audit/known_failures.md](audit/known_failures.md)** — Реестр триажа известных падений и платформенных различий.
- **[audit/accepted_risks.md](audit/accepted_risks.md)** — Реестр принятых рисков безопасности Git-истории (отозванный токен бота, синтетические mock-ключи в тестах, клиентские токены во внешних HTML-снапшотах).
- **CI / GitHub Actions (`.github/workflows/ci.yml`)** — Двухуровневый пайплайн непрерывной интеграции:
  1. *Audit & Safety Gate (Blocking)*: Gitleaks scan, Ruff blocking rules (`F821, F811, B023, SIM222, RUF034, E722, PLW1510, ASYNC210, ASYNC251`), Pip-audit, Pytest green subset.
  2. *Full Test Suite (Informational)*: Полный кроссплатформенный запуск тестового набора.

---

## 5. Память проекта и детальные контексты

- **[project_memory.md](project_memory.md)** — Краткосрочная и долговременная оперативная память проекта.
- **[memory_details.md](memory_details.md)** — Детализированные решения по управлению браузерными сессиями, дедупликации и гейтам.

---

## 6. Архив исторических отчётов (Archive)

Все исторические промежуточные отчёты по стадиям разработки (Stage/Task reports с сентября по октябрь 2026 г.) перемещены в изолированную директорию **[docs/archive/](archive/)**:
- Отчёты по этапам парсинга резюме и базы знаний кандидата (`t1_` — `t5_`)
- Отчёты по качеству описаний вакансий (`t6_1_`, `t6_2_`)
- Отчёты по интеграции и тестированию откликов (`t7_`, `t71_` — `t729_`)
- Отчёты по разработке Gig Search и фриланс-откликов (`gig_search_p1_` — `gig_search_p8_`, `contra_`, `freelancer_`)
- Завершённые аудиты предыдущих итераций (`audit_2026-10-19.md`, `audit_followup.md`, `stage0_`, `stage30c_`).
