# Архитектура проекта job-search

## 1. Что это

Персональная система автоматизации поиска работы: собирает удалённые вакансии и
проектные заказы, нормализует и дедуплицирует их, оценивает по профилю кандидата
(`candidate_profile.json`), готовит пакет отклика (резюме + сопроводительное
письмо + ответы на скрининг), ведёт жизненный цикл отклика в SQLite и доставляет
дайджест в Telegram. Всё, что касается внешних действий (браузер, HH, почта),
закрыто предохранителями: по умолчанию режим REVIEW, AUTO включается только
явными kill-switch-ами, факты никогда не выдумываются.

Ключевая идея: **один канонический конвейер** (`adapters → normalizer → matcher →
state.db → digest`) и **несколько независимых точек входа** (ежедневный дайджест,
HH-автопилот, realtime-демон сообщений, Telegram-пульт, ручной CLI).

## 2. Стек

| Слой | Технологии |
|------|------------|
| Язык / среда | Python 3.11, venv `.venv`, Windows 11 + Task Scheduler |
| Данные | SQLite (`state.db`, WAL), JSON-файлы профиля и артефактов |
| Сеть | `requests`, `urllib`, `feedparser` (RSS), Telegram Bot API |
| LLM | `openai`-совместимый клиент (`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`) + детерминированный offline-fallback |
| Браузер | Playwright / headless Chrome, raw-CDP через `websockets` |
| Веб-UI | FastAPI + uvicorn (`ai_assistant/ui/app.py`, команда `cli ui`) |
| Тесты | pytest, ~1400 offline-тестов, сокеты и urllib заблокированы фикстурой |
| Конфиг | `.env` + `ai_assistant/config.py` (kill-switch `SUBMIT_ALLOWED` читается до `load_dotenv`) |

## 3. Карта модулей

### Сбор и оценка
| Модуль | Роль |
|--------|------|
| `ai_assistant/adapters/*` | Himalayas, RemoteOK, WeWorkRemotely, Habr Career (RSS/HTTP) |
| `scripts/run_multi_source_discovery.py` | Дополнительные источники: Hirify, GeekJob, Bangkok ATS, LinkedIn Bangkok, Telegram-каналы |
| `ai_assistant/normalizer.py` | Приведение сырых записей к схеме `Vacancy` |
| `ai_assistant/vacancy_identity.py` | Канонический ID, алиасы, дедупликация |
| `ai_assistant/eligibility.py` | Гео/remote-допустимость вакансии |
| `ai_assistant/remote_filter.py` | Строгая проверка «100% remote» |
| `ai_assistant/matcher.py` | `JobMatcher`: жёсткие ограничения + взвешенный скор, `decision_class`, `role_priority` |
| `ai_assistant/job_analyzer.py` | Глубокий LLM-анализ + fallback без выдумывания фактов |
| `ai_assistant/watcher.py` | Один цикл конвейера до ворот human review (сам не отправляет) |

### Подготовка отклика
| Модуль | Роль |
|--------|------|
| `application_prep.py` | Пакет отклика: адаптация резюме + письмо |
| `application_qa.py` | Truth-only ответы на скрининг, валидация |
| `application_queue.py` | Приоритетная очередь |
| `application_review.py` | Запись ревью и статусы |
| `hh_extractor.py`, `prefill_plan.py`, `prefill_execute.py` | Извлечение формы HH и React-safe заполнение |
| `hh_submission.py` | 11 жёстких гейтов + один клик отправки |
| `hh_application_runner.py` | Ручной прогон `next --confirm-submit` |

### HH-автономность
| Модуль | Роль |
|--------|------|
| `hh_autopilot.py` | Цикл: поиск → бриф → скрининг → отклик → сверка |
| `hh_autonomous_agent.py` | Агент полного цикла + обработка входящих диалогов |
| `hh_message_reply.py` | Read-only чтение диалогов, truth-only черновики |
| `hh_message_watcher.py` | Наблюдение за новыми сообщениями |
| `hh_browser_provider.py`, `hh_browser_launcher.py` | Headless-сессия, CDP, проверка авторизации |
| `hh_chat_monitor.py` | Скан переговоров и алерты |
| `hh_resume_bumper.py` | Поднятие резюме в выдаче HH |

### Доставка и обратная связь
| Модуль | Роль |
|--------|------|
| `cli.py` | Единая точка входа, `export_digest_cmd`, `production-*` |
| `integrations/hermes/job_search_fetcher.py` | Канонический fetcher + диспетчер дайджеста |
| `telegram_notifier.py` | Текст дайджеста + inline-клавиатура `fb:*` |
| `telegram_feedback.py` | Обработка callback-кнопок, provenance, идемпотентность |
| `telegram_control_bot.py` | Пульт `@jobsassistbot`, long-polling |
| `runner.py` | Лок, ротация логов, circuit breaker, маскировка секретов |

## 4. Хранилища

`state.db` (SQLite, WAL) — единственный источник правды:

- `vacancies` — канонические вакансии, `match_score`, `match_decision`
- `deep_analysis`, `application_packages`, `application_queue`, `application_reviews`
- `application_tracking`, `application_status_history` — жизненный цикл
- `vacancy_eligibility`, `canonical_vacancies`, `vacancy_aliases` — гео и дедуп
- `application_submissions`, `submission_verifications`, `submission_claims`
- `hh_applications`, `hh_application_transitions`, `hh_questionnaires`, `hh_message_events`
- `telegram_delivery_records` — идемпотентность доставки
- `telegram_feedback_records` — аудит нажатий кнопок
- `autonomous_cycle_runs`, `autonomous_notifications`, `interview_events`

Файлы: `candidate_profile.json`, `candidate_kb.json`, `resume.md`,
`vacancies.json` (legacy-снимок), `logs/`, `artifacts/`, `tools/.t722*/`.

## 5. Точки входа и расписание

| Задача планировщика | Период | Скрипт | Итог |
|---|---|---|---|
| `JobSearch_Daily_Digest` (+ Hermes cron) | ежедневно 10:00 | `scripts/run_job_search_production.ps1` | дайджест в `@remotejobd` |
| `JobSearch_HH_Autopilot` | каждые 4 часа | `scripts/run_hh_autopilot_cycle.py` | отклики на HH + алерты |
| `JobSearch_HH_Realtime` | каждые 30 с (демон) | `scripts/run_hh_realtime_daemon.py` | автоответы на сообщения |
| `JobSearch_MultiSource` | каждые 2 часа | `scripts/run_multi_source_discovery.py` | пополнение `state.db` |
| `JobSearch_Telegram_Bot` | long-polling | `ai_assistant/telegram_control_bot.py` | пульт управления |
| `JobSearch_Gig_Autopilot` | каждые 2 часа | `scripts/run_gig_autopilot_cycle.py` | карточки заказов (см. §12) |

---

## 6. Поток A — ежедневный производственный дайджест

Самый важный и самый защищённый путь: сбор → скоринг → отбор → идемпотентная
доставка в канал.

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Планировщик<br/>10:00 ежедневно
    participant PS as run_job_search_production.ps1
    participant CLI as ai_assistant.cli<br/>production-run
    participant RUN as runner.py<br/>run_production_pipeline
    participant F as job_search_fetcher.py<br/>канонический fetcher
    participant W as watcher.py<br/>Watcher
    participant ADP as Адаптеры<br/>Himalayas, RemoteOK, WWR, Habr
    participant NET as Внешние сайты и RSS
    participant DB as state.db<br/>SQLite WAL
    participant M as matcher.py<br/>JobMatcher
    participant LLM as LLM API<br/>OpenAI-совместимый
    participant TG as Telegram Bot API
    participant CH as Канал @remotejobd

    rect rgba(32, 56, 96, 0.45)
    Note over SCH,CH: Блок 1 — запуск, блокировки, предохранители
    SCH->>PS: срабатывание триггера
    PS->>CLI: .venv python -m ai_assistant.cli production-run
    CLI->>RUN: run_production_pipeline(dry_run)
    RUN->>RUN: ротация job_search_production.log
    RUN->>RUN: SingleInstanceLock job_search.lock
    RUN->>RUN: чтение failure_tracker.json
    alt circuit открыт и это не dry-run
        RUN-->>CLI: exit 5 PRODUCTION_CIRCUIT_OPEN
        CLI-->>SCH: код возврата 5, доставки нет
    else circuit закрыт
        RUN->>F: subprocess python job_search_fetcher.py
    end
    end

    rect rgba(28, 74, 58, 0.45)
    Note over F,LLM: Блок 2 — сбор, нормализация, скоринг
    F->>F: SingleInstanceLock job_search_fetcher.lock
    F->>W: poll_once(iteration=1, dry_run)
    W->>ADP: fetch_vacancies()
    ADP->>NET: HTTP и RSS запросы
    NET-->>ADP: сырые вакансии
    ADP-->>W: список Vacancy
    W->>W: normalize_vacancy и normalize_url
    W->>DB: save_vacancy INSERT / UPDATED / UNCHANGED
    W->>DB: vacancy_eligibility, application_tracking
    W->>M: match(vacancy)
    M-->>W: score, decision, decision_class, role_priority
    W->>LLM: analyze_job_deep, промпт с профилем
    LLM-->>W: DeepAnalysisResult
    Note over W,LLM: при сбое LLM — детерминированный fallback без выдуманных фактов
    W->>DB: deep_analysis, application_packages, application_queue
    W->>DB: application_reviews со статусом PENDING_REVIEW
    end

    rect rgba(84, 60, 30, 0.45)
    Note over F,CH: Блок 3 — отбор дайджеста и идемпотентная доставка
    F->>DB: score_unscored_vacancies заполняет match_score
    F->>CLI: export_digest_cmd(limit=5, min_score=60)
    CLI->>DB: list_undigested_vacancies
    CLI->>CLI: is_genuine_production_vacancy, отсев dryrun/test/mock
    CLI->>M: match по каждому кандидату
    CLI->>CLI: сортировка decision_class, role_priority, score, свежесть
    CLI->>CLI: diversity max 2 на компанию, max 4 на role family
    CLI-->>F: JSON telegram_post, new_vacancies_data, reply_markup
    F->>DB: record_digest_attempt, статус ATTEMPTING, batch_key
    F->>TG: sendMessage Markdown + inline keyboard fb:*
    alt HTTP 200
        TG->>CH: публикация дайджеста
        TG-->>F: message_id
        F->>DB: mark_digest_delivered, статус DELIVERED
    else подтверждённая ошибка
        F->>DB: record_digest_failed, статус FAILED, повтора нет
    else таймаут или неопределённый ответ
        F->>DB: record_digest_ambiguous, статус AMBIGUOUS, повтора нет
    end
    F-->>RUN: код возврата
    RUN->>RUN: extract_adapter_failures, record_success или record_failure
    RUN-->>CLI: 0 healthy, 4 сбой адаптера, 5 circuit, 99 crash
    RUN->>RUN: release lock
    end
```

---

## 7. Поток B — HH Autopilot: поиск, скрининг, отклик

Отдельная ветка, работающая через живую сессию браузера. В отличие от потока A,
здесь есть реальный клик «Отправить» — он выполняется под постоянной
авторизацией владельца, а не под per-vacancy подтверждением (см. §14).

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Планировщик<br/>каждые 4 часа
    participant R as run_hh_autopilot_cycle.py
    participant BUMP as hh_resume_bumper
    participant AP as hh_autopilot.py<br/>run_autopilot_cycle
    participant RO as hh_readonly_operation<br/>поиск через CDP
    participant BR as HeadlessBrowserSession<br/>Playwright CDP
    participant HH as hh.ru
    participant T7 as tools/.t722_send.py
    participant QA as application_qa<br/>hh_extractor, prefill_plan
    participant CL as smart_experience_generator
    participant DB as state.db
    participant TG as @jobsassistbot

    rect rgba(32, 56, 96, 0.45)
    Note over SCH,RO: Блок 1 — подъём резюме и поиск вакансий
    SCH->>R: срабатывание триггера
    R->>R: acquire_lock logs/autopilot.lock
    R->>BUMP: bump_hh_resumes()
    BUMP->>HH: поднятие резюме в выдаче
    R->>AP: run_autopilot_cycle(max_submits=8, read_limit=45)
    AP->>DB: get_known_vacancy_ids, отсев уже обработанных
    AP->>RO: search_operation_report(8 запросов, remote)
    RO->>BR: аренда браузера и вкладки
    BR->>HH: поиск и чтение карточек
    HH-->>RO: карточки и preflight-данные
    RO-->>AP: raw_report со списком вакансий
    end

    rect rgba(70, 44, 84, 0.45)
    Note over AP,TG: Блок 2 — поочерёдный отсев каждой вакансии
    loop по каждой найденной вакансии
        AP->>AP: check_salary_floor, минимум 100 000 RUB
        AP->>T7: brief_vacancy(vid), чтение формы
        T7->>HH: открыть страницу вакансии
        HH-->>T7: DOM формы и applicant state
        alt обнаружен тест работодателя
            AP->>TG: алерт «требуется тест», вакансия пропущена
        else обнаружена скрининг-анкета
            AP->>CL: generate_tailored_letter
            AP->>QA: build_questions_from_controls
            QA->>QA: QuestionAnswerGenerator по профилю и candidate_kb
            alt все ответы подтверждены фактами
                QA-->>AP: prefill_plan со статусом VALID
            else есть неподтверждённые вопросы
                QA-->>AP: REQUIRES_REVIEW
                AP->>TG: алерт «анкета требует внимания»
            end
        end
        AP->>AP: is_vacancy_eligible_for_auto_apply, проверка 8 условий
    end
    end

    rect rgba(30, 78, 66, 0.45)
    Note over AP,TG: Блок 3 — отклик, сверка, уведомление
    AP->>AP: select_best_resume_for_vacancy
    AP->>CL: сопроводительное письмо под вакансию
    AP->>AP: preflight.json, SUBMIT_ALLOWED=true на время клика
    AP->>T7: run_submit(vid, resume, letter, prefill_plan)
    T7->>BR: React-safe заполнение полей
    BR->>HH: ввод значений без навигации на vacancy_response
    T7->>HH: ровно один клик «Отправить»
    HH-->>T7: подтверждение в DOM
    T7-->>AP: verdict CLICKED, clicked=1
    AP->>DB: reconcile и application_tracking
    AP->>TG: алерт «автопилот отправил отклик»
    AP->>HH: scan_hh_negotiations, проверка переговоров
    AP-->>R: сводка цикла
    R->>R: release_lock
    end
```

---

## 8. Поток C — realtime-демон сообщений HH

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Планировщик<br/>каждый час, keep-alive
    participant D as run_hh_realtime_daemon.py
    participant L as logs/autopilot.lock
    participant BR as HeadlessBrowserSession<br/>require_auth=true
    participant HH as hh.ru/chat
    participant AG as hh_autonomous_agent<br/>AutonomousJobAgent
    participant MR as hh_message_reply<br/>read-only
    participant KB as candidate_kb.json
    participant DB as state.db
    participant TG as @jobsassistbot

    SCH->>D: запуск процесса
    D->>D: acquire_daemon_lock, PID-проверка
    loop каждые 30 секунд
        D->>L: проверка активного автопилота
        alt автопилот держит lock
            D->>BR: остановить сессию и уступить браузер
        else автопилот не активен
            D->>BR: старт или переиспользование сессии
            BR->>HH: открыть hh.ru/chat
            D->>AG: _process_messages()
            AG->>MR: fetch_hh_conversations_list_readonly
            MR->>HH: чтение списка диалогов
            HH-->>MR: диалоги и последние сообщения
            MR-->>AG: HHDialog и HHMessage
            AG->>AG: classify_hh_conversation_detailed
            alt NEEDS_REPLY и факт подтверждён
                AG->>KB: поиск подтверждённых фактов
                AG->>AG: генерация черновика без выдумывания
                alt HH_AUTONOMOUS_REPLY_ENABLED=true
                    AG->>HH: ввод текста и клик отправки
                else флаг выключен
                    AG->>DB: сохранить только черновик
                end
            else HUMAN_REVIEW
                AG->>TG: уведомление владельцу
            else INTERVIEW_INVITATION
                AG->>TG: HIGH-приоритет, приглашение на интервью
            end
            AG->>DB: hh_message_events, autonomous_notifications
        end
        opt раз в 10 минут
            D->>HH: scan_hh_negotiations
            D->>TG: алерты по новым карточкам переговоров
        end
    end
```

---

## 9. Поток D — обратная связь из Telegram-дайджеста

```mermaid
sequenceDiagram
    autonumber
    participant U as Пользователь<br/>владелец 392046103
    participant TG as Telegram<br/>клиент и Bot API
    participant GW as Hermes Gateway<br/>или telegram_control_bot
    participant FP as telegram_feedback<br/>TelegramFeedbackProcessor
    participant DB as state.db
    participant RV as application_reviews
    participant TR as application_tracking
    participant Q as application_queue

    U->>TG: нажатие кнопки под вакансией
    Note over TG: callback_data вида fb:INT:hh:136551280<br/>или fb:RSN:act:reason:hash
    TG->>GW: callback_query через getUpdates
    GW->>FP: process_callback_query(callback_query)
    FP->>FP: проверка from.id == TELEGRAM_OWNER_ID
    alt не владелец
        FP-->>GW: отказ, доступ запрещён
    else владелец
        FP->>DB: проверка callback_query_id на повтор
        alt повторное нажатие
            FP-->>GW: idempotent no-op
        else новое нажатие
            FP->>DB: декодирование hash и поиск вакансии
            FP->>DB: is_genuine_production_vacancy
            FP->>DB: проверка DELIVERED в telegram_delivery_records
            FP->>DB: проверка терминального статуса
            alt проверки не пройдены
                FP-->>GW: fail-closed с пояснением
            else проверки пройдены
                FP->>RV: INTERESTED или REJECTED или APPROVED
                FP->>TR: ANALYZED или READY_TO_APPLY или WITHDRAWN
                FP->>Q: постановка в очередь при «Отклик»
                FP->>DB: запись в telegram_feedback_records
                FP-->>GW: успех
            end
        end
    end
    GW->>TG: answerCallbackQuery с коротким тостом
    TG-->>U: подтверждение действия
```

Важно: кнопка «📄 Отклик» **не отправляет** заявку. Она переводит запись в
`READY_TO_APPLY` и ставит её в очередь; отправка остаётся за потоком B или E.

---

## 10. Поток E — ручной human-in-the-loop через CLI

```mermaid
sequenceDiagram
    autonumber
    participant U as Пользователь
    participant CLI as ai_assistant.cli
    participant ADP as Адаптеры
    participant DB as state.db
    participant M as JobMatcher
    participant BR as browser_executor<br/>read-only
    participant HH as hh.ru
    participant G as HHSubmissionGates<br/>11 гейтов
    participant V as submission_verifier

    U->>CLI: collect habrcareer himalayas
    CLI->>ADP: fetch_vacancies()
    ADP-->>CLI: вакансии
    CLI->>DB: save_vacancy
    U->>CLI: analyze и analyze-deep
    CLI->>M: match и LLM-анализ
    M-->>CLI: score и decision
    CLI->>DB: deep_analysis
    U->>CLI: prepare-applications
    CLI->>DB: application_packages, application_queue
    U->>CLI: review list и review approve
    CLI->>DB: application_reviews APPROVED
    U->>CLI: browser prepare
    CLI->>BR: чтение формы без кликов
    BR->>HH: DOM страницы вакансии
    HH-->>BR: поля и fingerprint
    BR-->>CLI: browser_preparations
    U->>CLI: submit --confirm-submit
    CLI->>G: последовательная проверка 11 гейтов
    alt любой гейт не пройден
        G-->>CLI: fail-closed, 0 мутаций
    else все гейты пройдены
        G->>HH: один клик отправки
        HH-->>G: подтверждение
        G->>DB: application_submissions
        CLI->>V: verify, только чтение
        V->>HH: проверка факта отклика
        V-->>DB: submission_verifications VERIFIED
    end
    U->>CLI: dashboard, audit, submissions
    CLI->>DB: агрегаты и история переходов
```

11 гейтов: `SUBMIT_ALLOWED`, `REVIEW_APPROVED`, `FINGERPRINT_MATCH`,
`URL_DOMAIN`, `VACANCY_MATCH`, `PROFILE_LOADED`, `COVER_LETTER_READY`,
`NO_UNKNOWN_QUESTIONS`, `NOT_ALREADY_APPLIED`, `NO_PREVIOUS_SUBMISSION_ATTEMPT`,
`HUMAN_CONFIRMED`.

---

## 11. Поток F — мультиисточниковый сбор (каждые 2 часа)

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Планировщик<br/>каждые 2 часа
    participant S as run_multi_source_discovery.py
    participant A1 as Habr Career
    participant A2 as Hirify API
    participant A3 as GeekJob
    participant A4 as Bangkok ATS<br/>Greenhouse и Lever
    participant A5 as LinkedIn Bangkok
    participant A6 as Telegram-каналы
    participant DB as state.db
    participant TG as Канал @remotejobd

    SCH->>S: запуск через pythonw, без окна консоли
    S->>S: логирование в logs/multi_source_discovery.log
    par параллельные источники
        S->>A1: fetch_vacancies()
        S->>A2: fetch_vacancies(max_pages=2)
        S->>A3: fetch_all(max_pages=2)
        S->>A4: fetch_all()
        S->>A5: fetch_all()
        S->>A6: fetch_all()
    end
    A1-->>S: вакансии
    A2-->>S: вакансии
    A3-->>S: вакансии
    A4-->>S: вакансии
    A5-->>S: вакансии
    A6-->>S: вакансии и гиги
    S->>S: is_target_role по ключевым словам
    S->>S: is_acceptable_salary, порог 100 000 RUB
    S->>DB: INSERT новых stable_id со статусом DISCOVERED
    opt есть новые записи
        S->>TG: сводка и примеры вакансий
    end
```

Этот поток только пополняет базу. Отбор и доставку дайджеста делает поток A,
поэтому дубли в канал не попадают.

---

## 12. Поток G — гиг-автопилот (деградировавший)

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Планировщик<br/>каждые 2 часа
    participant G as run_gig_autopilot_cycle.py
    participant SRC as Freelancer, Kwork,<br/>Telegram-каналы
    participant P as GigPipeline<br/>normalize, dedup, gates
    participant DB as state.db
    participant TG as Личный чат владельца

    SCH->>G: запуск
    G->>G: acquire_lock logs/gig_autopilot.lock
    G->>SRC: fetch(limit)
    SRC-->>G: сырые заказы
    G->>P: process_raw
    P-->>G: Gig с оценкой и rejection_reasons
    G->>DB: save_gig
    G->>G: отсев non-IT и can_deliver ниже MEDIUM
    G->>G: first_money_score, порог 48
    G->>DB: проверка telegram_delivery_records
    G->>TG: до 3 карточек с готовым питчем
    G->>DB: record_gig_delivery SHORTLISTED
    G->>G: release_lock
```

Статус: пакет `ai_assistant/gig/*` и `ai_assistant/gig_pitch_generator.py`
отсутствуют в рабочем дереве (остались только `__pycache__` и тесты в
`tests/gig/`), поэтому на 2026-10-18 скрипт падает на импорте. Поток описан по
коду и тестам, но фактически не работает.

---

## 13. Инварианты безопасности

- **Truth-only**: любой отсутствующий факт ведёт в `HUMAN_REVIEW`, а не к догадке.
- **Fail-closed**: сомнение блокирует действие, число мутаций — ноль.
- **Один отклик на вакансию**: дедуп по `stable_id` + `submission_claims`.
- **Никаких повторов после неопределённого исхода**: `AMBIGUOUS` требует ручного
  разбора через `digest-attempts recover` или `submissions recover`.
- **Запрет навигации** на `applicant/vacancy_response`: захват только на уже
  открытой вкладке пользователя.
- **REVIEW по умолчанию** для сообщений; AUTO требует kill-switch, allowlist и
  неизменного fingerprint.
- **Почта**: отправка физически отсутствует в коде, только read-only Gmail.
- **Circuit breaker**: 3 подряд живых сбоя → exit 5, снимается только
  `production-control resume`.

## 14. Наблюдения и расхождения (для супервизора)

1. **Документация и код расходятся по автономной отправке.** README и
   `PROJECT_STATE.md` заявляют «prepare-only, отправка только с
   `--confirm-submit`». Фактически `hh_autopilot.submit_vacancy()` сам выставляет
   `SUBMIT_ALLOWED=true` и кликает отправку, а `preflight.json` пишет
   `auto_submit_authorized: true` без `human_confirmed`. Поток B описан по коду.
2. **Отсутствующие модули.** `ai_assistant/hh_readonly_operation.py` и
   `ai_assistant/hh_browser_lease.py` не найдены ни на диске, ни в git. Их
   импортируют `hh_autopilot.run_autopilot_cycle` и
   `run_hh_autopilot_cycle.py`, поэтому поиск вакансий и уступка браузера
   реально падают, а исключение глушится в `{"status": "ERROR"}`.
3. **Два планировщика на один fetcher.** Задача Windows и Hermes cron приходят
   в один и тот же файл; взаимное исключение держится на отдельном
   `job_search_fetcher.lock`, иначе был бы `database is locked`.
4. **Легаси-данные в отборе.** `vacancies_json:*` и тестовые фикстуры
   отсекаются `is_genuine_production_vacancy`, но остаются в базе и влияют на
   агрегаты дашборда.
5. **Дублирование кода отправки.** `run_multi_source_discovery.py` и
   `hh_autopilot.py` шлют в Telegram напрямую через `urllib`, минуя
   `TelegramNotifier`, поэтому у них нет общей идемпотентности и ретрай-политики.
