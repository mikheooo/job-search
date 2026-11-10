# Stage 0 — Candidate Intelligence: архитектурный аудит и план

**Статус:** аудит и проектирование. Код, БД и production-данные не менялись. Коммитов нет.
**Дата:** 2026-09-22
**Репозиторий:** `C:\Users\Misha\Documents\job-search`, ветка `chore-ruff-cleanup` (HEAD `98be2ea`)
**Метод:** чтение кода и данных репозитория; все числа в отчёте — из замеров, а не из памяти.

---

## 1. Текущее состояние

### 1.1. Размер и состав

| Показатель | Значение |
|---|---|
| Python-модулей в `ai_assistant/` | 63 |
| Тестовых функций в `tests/` | 1829 (grep-счёт) в 120 файлах |
| Последний полный прогон по `PROJECT_STATE.md` | 1408 passed, 0 failed |
| Таблиц в `state.db` | 27 |
| Известный красный | ратчет отчёта `docs/ble001_triage.md`: 24 при пороге 18 (пин `tests/test_report_line_refs.py`) |

### 1.2. Три источника правды о кандидате

Это центральный факт аудита. Кандидат описан **трижды**, и описания расходятся.

| # | Источник | Объём | Кто читает |
|---|---|---|---|
| 1 | `resume.md` | 3434 символа | `job_analyzer.get_resume_text()` → письма, deep-анализ |
| 2 | `candidate_profile.json` | 6384 байта | `candidate_profile.load_candidate_profile()`, матчер |
| 2b | `ai_assistant/candidate_profile.json` | 6141 байт | второй путь в `DEFAULT_PROFILE_PATHS` |
| 3 | хардкод в `ai_assistant/candidate_profile.py:447+` | — | fallback, когда файлов нет |

**Загрузка профиля** (`ai_assistant/candidate_profile.py:404`):

```python
DEFAULT_PROFILE_PATHS = [
    Path("candidate_profile.json"),
    Path("ai_assistant/candidate_profile.json"),
    Path(__file__).parent / "candidate_profile.json",
]
```

Побеждает первый существующий. Сейчас это корневая копия. Копия в пакете **семантически идентична**
(сравнение через `json.tool` — различий нет), но хранится в другой разметке. Это отложенная
«раздвоенная правда»: правка одной копии молча разойдётся со второй.

**Хардкод-fallback** (`candidate_profile.py:447`) срабатывает, когда не найдено ни одного файла.
Он не молчит — пишет `logger.warning` (исправление BLE001 finding #44), — но всё равно возвращает
профиль **без имени, email и телефона**. Формы готовятся с пустыми контактами, и понять почему
можно только по строке в логе.

### 1.3. Измеренные противоречия между источниками

**Опыт.** Резюме: `resume.md:7` — «AI Automation Engineer with **15+ years** of IT experience».
Профиль: `candidate_profile.json:52-59` — `domain_years.it_support = **11.0**`.
`PROJECT_STATE.md` §5 — «Experience Baseline (Total **11+ Years** in Tech)».
Два разных числа, оба подаются как факт.

**Bitrix24.** Резюме продаёт его как основной опыт: роль в Sayama Travel (`resume.md:26-31`) и
отдельная должность «Bitrix24 Technical Support Specialist | Avantis» (`resume.md:33-36`).
Профиль в это же время держит `bitrix` в `excluded_roles` (`candidate_profile.json:219`) и в
`must_avoid_conditions` (`:240`). А `PROJECT_STATE.md` §8 относит Bitrix к технологиям, упоминание
которых в вопросах рекрутёра переводит ответ в `HUMAN_REVIEW` — то есть считает его
**непроверенным**. Три источника — три несовместимые позиции по одному слову.

**Годы опыта.** `candidate_profile.json:184-185` — `years_experience: 3` и
`years_of_experience: 3`, при `domain_years.it_support: 11.0` в том же файле. Семантика поля не
зафиксирована: это «годы в Python» или «годы всего»? Читатель вынужден догадываться.

### 1.4. Что уже структурировано в профиле

- `provenance` (`candidate_profile.json:103-113`) — **9 ключей**, значения `CONFIRMED` / `DEFAULT`.
  Покрывает: имя, общий стаж, python/automation, application support, языки, локацию, remote,
  зарплатный порог. Больше ничего.
- `skill_confidence` (`:114-143`) — **28 навыков** в тирах `PROFESSIONAL` / `PROJECT` / `BASIC` /
  `TRANSFERABLE`. **Ни один навык не имеет ссылки на доказательство**, даты или источника.
- `role_specific_skills` (`:144-172`) — ядро/вторичное по 7 семействам ролей.
- `role_priorities`, `role_family_seniority`, `domain_years` — калибровки матчера.

Навыки из резюме, которых нет в `skill_confidence`: Anthropic API, RAG, prompt engineering,
transcription, summarization, classification, fact-checking, MTProto, Google Workspace APIs,
Bitrix24, MS SQL, Hyper-V, Exchange, telephony. То есть профиль и резюме описывают **разных**
кандидатов по объёму навыков.

### 1.5. Замер: резюме обрезается, и обрезается существенно

| Место | Срез | Файл | Потеряно |
|---|---|---|---|
| Письмо | `resume_text[:2500]` | `ai_assistant/application_prep.py:159` | **934 символа, 27%** |
| Deep-анализ | `resume_text[:3000]` | `ai_assistant/job_analyzer.py:126` | 434 символа |

При срезе в 2500 символов обрывается ровно на `resume.md` — «Administered Linux and Docker
infrastructure (systemd services, logs, monitoring); c…». За границей остаются:

- вся роль **Bitrix24 Technical Support Specialist | Avantis** (апрель 2021 — август 2022);
- весь блок **Earlier IT Infrastructure & Support Experience** (2009–2018): Windows Server,
  Active Directory, Exchange, SQL, Hyper-V, сети, IP-телефония, ITSM;
- **Education** и **Languages**.

То есть генератор писем физически не видит 11 лет раннего опыта и образование, а `SkillConfidence`
для `active directory` при этом стоит `PROFESSIONAL` (`candidate_profile.json:123`). Резюме и
профиль тянут в разные стороны, и промпт получает только одну сторону.

### 1.6. Что делает текущий «страж правды»

`application_prep.py:123-136` — системный промпт: «You MUST NOT invent candidate experience.
Truth sources ONLY: CandidateProfile, resume text, vacancy, DeepAnalysisResult… NEVER invent
technologies, roles, years, companies, achievements».

`application_prep.py:281` — проверка на выдуманные технологии:

```python
allowed_vocab = set(s.lower() for s in profile.skills) \
              | set(resume_text.lower().split()) \
              | set((vacancy.title + " " + vacancy.description).lower().split())
```

**Ключевой вывод аудита:** этот страж сверяет письмо **с резюме**, но ничто не сверяет **само
резюме**. Слово из `resume.md` автоматически попадает в разрешённый словарь. Поэтому «15+ years»
из резюме беспрепятственно уедет в письмо, хотя профиль говорит 11. Проект построил слой
**принуждения** к правде и не построил слоя **проверки** правды на стороне кандидата.

### 1.7. Состояние адаптации резюме

Флаг существует в двух видах:

- `DeepAnalysisResult.resume_adaptation_needed` / `.resume_adaptation_reasons`
  (`job_analyzer.py:37-38`) — поле, которое может заполнить LLM.
- Эвристика `job_analyzer.py:291`: `resume_needed = len(missing) > 0 or len(gaps) > 2`,
  с причинами вида «Highlight/clarify missing skills not confirmed».

**Действия по этому флагу нет.** Единственное потребление — штраф к готовности
(`application_queue.py:147-150`): `score -= 15`, `warnings.append("resume adaptation needed")`.
Адаптация резюме не реализована: флаг есть, реализации нет.

Это согласуется с `PROJECT_STATE.md` §11, где в списке **отложенного** по Stage 92 прямо стоит
«**reviewed resume-variant routing**». Проект уже опознал этот пробел сам.

### 1.8. Чего нет вообще

- **Таблиц про кандидата нет.** 27 таблиц: вакансии, отклики, отклик-пакеты, аудиты, доставки,
  фидбек, вопросы анкет. Кандидат живёт только в файлах.
- **Аудита глазами HR / техруководителя / ATS нет.** Слово `ATS` в коде означает другое:
  `browser_executor.py:46` — `EXTERNAL_ATS` это **детекция внешней ATS-платформы** (Greenhouse,
  Lever) для маршрутизации браузера. К скринингу резюме отношения не имеет.
- **Извлечения данных из резюме нет.** `get_resume_text()` возвращает сырой markdown.
- **Пробелов (gaps) как сущности нет.** `gaps` живут только внутри `DeepAnalysisResult` по
  конкретной вакансии и не накапливаются по кандидату.
- **Подтверждения фактов пользователем нет.** `provenance` статичен, механизма перевода
  `DEFAULT → CONFIRMED` через действие человека не существует.

---

## 2. Готовые механизмы — переиспользуем, не дублируем

Всё перечисленное **уже реализовано** и трогать не нужно.

| Область | Компонент | Файл | Что даёт |
|---|---|---|---|
| Загрузка профиля | `CandidateProfile`, `load_candidate_profile` | `ai_assistant/candidate_profile.py` | dataclass + `from_dict` / `to_dict` / `from_json_file`, env-override `CANDIDATE_PROFILE` |
| Сопоставление | `JobMatcher.match()` → `MatchResult` | `ai_assistant/matcher.py` | `decision_class`, `role_priority`, score, `SkillConfidenceLevel` |
| Классификация | `classify_role_family`, `calculate_role_compatibility`, `extract_seniority`, `extract_required_years`, `evaluate_hard_requirements` | `ai_assistant/matcher.py` | переиспользуемая аналитика по вакансии |
| Калибровка | `apply_preference_adjustment` | `ai_assistant/matcher.py:1292` | ограниченная ±8.0 поправка, не мутирует факты |
| Deep-анализ | `analyze_deep` → `DeepAnalysisResult` | `ai_assistant/job_analyzer.py:25` | fit_score, recommendation, gaps, missing_skills, `resume_adaptation_needed` |
| Загрузка резюме | `get_resume_text()` | `ai_assistant/job_analyzer.py:55` | поиск по `RESUME_CANDIDATES` + синтез из профиля |
| Письма | `generate_application_package` | `ai_assistant/application_prep.py` | LLM + truth-guard + детерминированный fallback |
| Ответы на вопросы | `QuestionAnswerGenerator` | `ai_assistant/application_qa.py` | PROFILE-ответы только из подтверждённых данных |
| Портфолио | упоминание в промпте письма | `application_prep.py:128`, тест `tests/test_portfolio_integration.py` | URL портфолио как доказательство |
| Отклики | `application_queue`, `application_review_gate`, `hh_*` | `ai_assistant/` | 11 гейтов, fail-closed, `SUBMIT_ALLOWED` |
| Поиск вакансий | `Watcher` + 4 адаптера | `ai_assistant/watcher.py`, `ai_assistant/adapters/` | himalayas, weworkremotely, remoteok, habr_career |
| Telegram | `telegram_bot`, `telegram_feedback`, `telegram_notifier` | `ai_assistant/` | дайджест, inline-кнопки, owner-gate, идемпотентность |
| CLI | ~60 команд | `ai_assistant/cli.py` | `analyze-deep`, `prepare`, `queue`, `review`, `feedback`, `coverage`, … |
| Хранение версий | версия + JSON-блоб | `deep_analysis`, `application_packages` в `db_schema.py:70,85` | готовый паттерн для версионируемых артефактов |

**Важное для интеграции:** `CandidateProfile.from_dict` читает все поля через `data.get(...)` и
**игнорирует незнакомые ключи** (проверено: `extra`/`setattr` в модуле отсутствуют). Значит
дополнить `candidate_profile.json` новыми секциями можно **без правки загрузчика** — обратная
совместимость сохраняется автоматически.

---

## 3. Пробелы

| # | Пробел | Доказательство | Цена |
|---|---|---|---|
| G1 | Три расходящихся источника правды о кандидате | §1.2, §1.3 | письма и анкеты могут утверждать разное в разных местах |
| G2 | Резюме обрезается на 27% | §1.5, замер | 11 лет опыта и образование невидимы генератору |
| G3 | Страж правды не проверяет сам источник | §1.6 | завышение в резюме проходит насквозь |
| G4 | Нет извлечения структуры из резюме | §1.8 | нет основы для KB, аудита и вариантов |
| G5 | Нет KB с подтверждёнными фактами и пробелами | §1.8 | нельзя ответить «что я могу доказать?» |
| G6 | Нет аудита HR / техлида / ATS | §1.8 | неизвестно, как резюме читается с другой стороны |
| G7 | Адаптация резюме объявлена, но не сделана | §1.7 | −15 к готовности за то, чего нет |
| G8 | Нет режима Job→Candidate | §1.8 | под конкретную вакансию нечего предъявить, кроме сырого текста |
| G9 | Нет механизма подтверждения фактов человеком | §1.8 | новые факты негде закрепить, `provenance` статичен |
| G10 | Дубликат `candidate_profile.json` в двух местах | §1.2 | отложенное расхождение |

---

## 4. Архитектура Candidate Intelligence

### 4.1. Принцип

Один источник правды — **Candidate Knowledge Base (KB)**. Резюме, профиль и производные
артефакты становятся **выходом** KB, а не входом. Направление чтения разворачивается:

```
СЕЙЧАС:   resume.md ─┐
          profile.json ├──► промпт письма ──► LLM ──► письмо
          (хардкод)  ─┘

ПОСЛЕ:    resume.md ──► [извлечение] ──► KB (claims + gaps)
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
             master_resume.md      profile.json          аудит ATS/HR/Tech
             (генерируется)        (генерируется)         (отчёт)
                    │
                    └──► промпт письма (только CONFIRMED_BY_USER)
```

### 4.2. Модель claim (атомарный факт)

```
claim_id        стабильный идентификатор (hash от kind+normalized_value)
kind            skill | role | date_range | achievement | language | education
                | contact | constraint | metric
value           как заявлено («15+ years of IT experience»)
normalized      нормализованное значение («years_it: 11.0»)
status          CONFIRMED_BY_USER   — подтверждено человеком, единственный источник для писем
                DERIVED_FROM_SOURCE — извлечено из документа, ещё не подтверждено
                CONTRADICTED        — источники расходятся
                GAP                 — требуется, но не найдено
evidence[]      {source_file, line, quote} — обязательна для каждого claim
confidence      переиспользуем SkillConfidenceLevel (PROFESSIONAL/PROJECT/BASIC/TRANSFERABLE)
first_seen, last_updated, user_confirmed_at
```

**Инвариант, реализующий требование «не выдумывать опыт»:** письма и анкеты используют **только**
`CONFIRMED_BY_USER`. `DERIVED_FROM_SOURCE` можно показывать пользователю как предложение, но нельзя
подставлять в исходящий текст. Никакого автоматического повышения статуса.

### 4.3. Модули

```
ai_assistant/candidate/
  resume_parser.py     markdown → ResumeClaim[]            (детерминированно, без LLM)
  kb.py                модель KB, load/save/merge, версионирование
  kb_verify.py         сверка источников → CONTRADICTED
  audit_ats.py         ревьюер ATS
  audit_hr.py          ревьюер HR
  audit_tech.py        ревьюер техруководителя
  master_resume.py     KB → master_resume.md
  profile_render.py    KB → candidate_profile.json
  job_to_candidate.py  вакансия → JobFitBrief (режим Job→Candidate)
  confirm.py           переходы статусов при подтверждении человеком
```

### 4.4. Три ревьюера

| Ревьюер | Что проверяет | Выход |
|---|---|---|
| **ATS** | разбираемость (заголовки, даты, контакты), покрытие ключевых слов целевого семейства ролей, отсутствие стандартных секций, плотность ключевых слов | findings с severity |
| **HR** | ясность роли в первых строках, сигнал сеньорности, разрывы в датах, remote/локация, уровень языка, наличие зарплатных ожиданий | findings |
| **Техлид** | глубина против модных слов, утверждения без контекста проекта, технологии без подтверждения, **внутренние противоречия** | findings |

Формат finding: `severity`, `claim_id`, `evidence`, `suggested_action`. **Findings — это
предложения.** Ни одно не применяется автоматически: изменение резюме требует подтверждения.

### 4.5. Два режима

- **Candidate → Jobs** — **уже существует**: профиль → `JobMatcher` → дайджест. KB его усиливает,
  добавляя к каждой вакансии `claims_to_highlight`, но не меняет логику решения.
- **Job → Candidate** — новый: вакансия → `JobFitBrief`:
  - `claims_to_highlight[]` — подтверждённые факты, релевантные вакансии;
  - `gaps_to_address[]` — чего требуют и чего нет в KB;
  - `questions_for_user[]` — что спросить у Михаила, чтобы закрыть пробел;
  - `resume_variant_suggestion` — какая версия резюме уместна;
  - `unconfirmed_to_avoid[]` — что **нельзя** утверждать в письме.

Этот же brief закрывает G7: `resume_adaptation_needed` перестаёт быть штрафом и становится
входом в реальную адаптацию.

---

## 5. Минимальная интеграция с существующей архитектурой

Принцип: **добавляем, не переписываем.** Ни одна существующая команда не меняет поведения, пока
изменение не одобрено отдельно.

| Точка | Что делаем | Риск |
|---|---|---|
| `candidate_profile.json` | добавляем секции `claims`, `gaps` — загрузчик их игнорирует (проверено) | нулевой |
| `candidate_profile.py` | **не трогаем** | — |
| `db_schema.py` | **не трогаем.** KB на Stage 0 — версионируемый JSON, как `candidate_profile.json` | нулевой |
| `ai_assistant/cli.py` | добавляем новые подкоманды, существующие не меняем | низкий |
| `application_prep.py:159` | **единственная правка чтения**: `resume_text[:2500]` → блок фактов из KB | средний, отдельная задача T7 |
| `application_prep.py:281` | словарь для truth-guard берём из `CONFIRMED_BY_USER`, а не из слов резюме | средний, T7 |
| `job_analyzer.py:126` | то же, что `application_prep.py:159` | средний, T7 |
| `application_queue.py:147` | `resume_adaptation_needed` получает источник — `JobFitBrief` | низкий, T6 |
| `RESUME_CANDIDATES` | **не меняем** на Stage 0 | — |
| Telegram | на Stage 0 **не трогаем**: подтверждение фактов идёт через CLI | низкий |

Почему без БД: кандидат — это единичная сущность, а не поток. Версионируемый JSON + git даёт
историю бесплатно и не требует миграции. Таблица понадобится, когда пробелы надо будет
джойнить с вакансиями; это осознанно отложено.

---

## 6. План Stage 0: последовательные задачи

Порядок задан зависимостями. Каждая задача самодостаточна и проверяема.

### T1 — Извлечение структуры из резюме (только чтение)

- **Файлы:** новый `ai_assistant/candidate/resume_parser.py`, `ai_assistant/candidate/__init__.py`; тест `tests/test_candidate_resume_parser.py`.
- **Изменения:** парсер markdown → `ResumeClaim[]`. Разделы по заголовкам, роли/компании/даты из
  строк вида `### Роль | Компания` + `*Локация | Месяц Год - Месяц Год*`, пункты — в claims.
  Каждый claim несёт `evidence` с файлом и номером строки. Без LLM.
- **Тесты:** 8+ тестов на реальном `resume.md` (регрессия на зафиксированных фактах), на пустом
  файле, на файле без дат, на битой разметке. Плюс пин: парсер обязан найти роль Avantis —
  ту самую, что сейчас отрезается.
- **Приёмка:** `python -m ai_assistant.cli candidate-kb extract --dry-run` печатает claims, ничего
  не записывая. Число найденных ролей и дат совпадает с ручным пересчётом. Ни один файл не изменён
  (проверка по mtime и хешам).
- **Зависимости:** нет. **Это первая задача.**

### T2 — Хранилище KB и CLI подтверждения

- **Файлы:** `ai_assistant/candidate/kb.py`, `ai_assistant/candidate/confirm.py`, `candidate_kb.json` (новый), тест `tests/test_candidate_kb.py`.
- **Изменения:** загрузка/сохранение/слияние claims, версионирование (`kb_version`), команды
  `candidate-kb show [--status]`, `candidate-kb confirm <id>`, `candidate-kb reject <id>`.
- **Тесты:** слияние не теряет claim; повторный `confirm` идемпотентен; `reject` не удаляет запись,
  а меняет статус; статус никогда не повышается без явного вызова.
- **Приёмка:** `extract` создаёт `candidate_kb.json`; все claims в статусе `DERIVED_FROM_SOURCE`;
  `confirm` переводит ровно один claim в `CONFIRMED_BY_USER`; инвариант «нет авто-повышения»
  покрыт тестом, который падает при попытке.
- **Зависимости:** T1.

### T3 — Сверка источников и противоречия

- **Файлы:** `ai_assistant/candidate/kb_verify.py`, тест `tests/test_candidate_kb_verify.py`.
- **Изменения:** сравнение claims из резюме, `candidate_profile.json` и хардкод-базлайна;
  расхождения → `CONTRADICTED` с перечислением источников.
- **Тесты:** обязательные регрессионные пины на **уже найденные** противоречия: «15+ years» vs
  `domain_years.it_support = 11.0`; `bitrix` в опыте vs `bitrix` в `excluded_roles`;
  `years_experience: 3` vs `it_support: 11.0`. Каждый пин должен падать, если сверку отключить.
- **Приёмка:** `candidate-kb verify` печатает минимум 3 противоречия, каждое со ссылками на оба
  источника. Ноль ложных срабатываний на совпадающих фактах.
- **Зависимости:** T1, T2.

### T4 — Аудит глазами ATS / HR / техлида

- **Файлы:** `ai_assistant/candidate/audit_ats.py`, `audit_hr.py`, `audit_tech.py`, тест `tests/test_candidate_audit.py`.
- **Изменения:** три ревьюера → findings; команда `candidate-audit --mode {ats,hr,tech,all}`;
  отчёт в `artifacts/candidate_audit_<дата>.md`.
- **Тесты:** ATS-ревьюер ловит отсутствие контактов и нестандартные заголовки; HR-ревьюер ловит
  разрыв дат и отсутствие зарплатных ожиданий; техлид ловит технологию без контекста проекта.
  Отдельный пин: техлид **обязан** пометить `CONTRADICTED`-claims из T3.
- **Приёмка:** отчёт содержит findings с severity; **ни один** finding не изменяет ни одного файла —
  проверяется хешами до/после прогона.
- **Зависимости:** T2, T3.

### T5 — Master-резюме

- **Файлы:** `ai_assistant/candidate/master_resume.py`, `artifacts/master_resume.md` (генерируемый), тест `tests/test_candidate_master_resume.py`.
- **Изменения:** KB → полное резюме без обрезки. В письме идёт ссылка на факты, а не срез текста.
- **Тесты:** master содержит **все** роли из `resume.md`, включая Avantis и блок 2009–2018
  (пин против G2); в master нет ни одного claim со статусом ниже `DERIVED_FROM_SOURCE`; длина не
  ограничена.
- **Приёмка:** `candidate-master build` создаёт файл; ручная сверка — ни один факт из `resume.md`
  не потерян; число ролей и дат совпадает с T1.
- **Зависимости:** T2.

### T6 — Режим Job→Candidate

- **Файлы:** `ai_assistant/candidate/job_to_candidate.py`, тест `tests/test_job_to_candidate.py`.
- **Изменения:** `JobFitBrief` из вакансии + KB + `DeepAnalysisResult`; команда
  `job-to-candidate <vacancy_stable_id>`; `application_queue` берёт `resume_adaptation_needed`
  отсюда.
- **Тесты:** brief не предлагает к подсветке ни одного неподтверждённого claim; пробелы
  формулируются как вопросы к пользователю, а не как утверждения; вакансия без пересечений даёт
  честный пустой `claims_to_highlight`.
- **Приёмка:** на реальной вакансии из `state.db` brief содержит все пять полей; ни одно поле не
  содержит выдуманного факта (проверяется сверкой с KB).
- **Зависимости:** T2, T3.

### T7 — Интеграция: письма и deep-анализ читают KB

- **Файлы:** правки `ai_assistant/application_prep.py` (строки 139–163, 279–282), `ai_assistant/job_analyzer.py` (строка 126).
- **Изменения:** обрезок `[:2500]` / `[:3000]` заменяется на блок подтверждённых фактов из KB;
  truth-guard (`allowed_vocab`) строится из `CONFIRMED_BY_USER`, а не из слов резюме.
- **Тесты:** регрессия существующих тестов писем и deep-анализа (обязательна, они уже есть);
  новый пин: письмо **не может** содержать неподтверждённый claim (мутационная проверка —
  подсунуть KB claim `UNCONFIRMED` и убедиться, что он не попадает в промпт).
- **Приёмка:** существующий набор тестов писем зелёный; в промпте присутствует Avantis; ни один
  `CONFIRMED_BY_USER`-факт не потерян из-за обрезки.
- **Зависимости:** T5, T6. **Самая рискованная задача — делать последней.**

### T8 — Обратная генерация профиля и снятие дубликата

- **Файлы:** `ai_assistant/candidate/profile_render.py`, тест `tests/test_profile_render.py`.
- **Изменения:** KB → `candidate_profile.json`; вторая копия `ai_assistant/candidate_profile.json`
  удаляется из `DEFAULT_PROFILE_PATHS` (после явного одобрения).
- **Тесты:** round-trip KB → профиль → `CandidateProfile` даёт те же значения, что текущий файл;
  ни одно существующее поле не теряется.
- **Приёмка:** `candidate-profile render --dry-run` показывает пустой диф против текущего
  `candidate_profile.json` — то есть KB описывает профиль полностью.
- **Зависимости:** T2, T5.

**Сводка зависимостей:**

```
T1 ──► T2 ──┬──► T3 ──┬──► T4
            │         │
            ├──► T5 ──┤
            │         │
            └──► T6 ──┴──► T7
            └──► T8
```

---

## 7. Риски

| Риск | Почему реален | Что делаем |
|---|---|---|
| KB становится **четвёртым** источником правды | §1.2 — источников уже три, и они расходятся | KB единственный писатель; `profile.json` и `master_resume.md` — генерируемые (T5, T8). До завершения T8 KB не участвует в отправке |
| LLM выдумывает claims при извлечении | соблазн «улучшить» резюме | T1 — детерминированный парсер без LLM. LLM может только **предлагать** claims со статусом `DERIVED_FROM_SOURCE`, и они не попадают в письма |
| Подтверждение фактов становится рутиной, поток встанет | у Михаила 28 навыков, десятки claims | `DERIVED_FROM_SOURCE` достаточно для отображения; блокируется только **исходящий текст**. Плюс T7 требует подтверждения лишь для того, что реально идёт в письмо |
| Молчаливый fallback размножается | уже есть один (BLE001 #44, `candidate_profile.py:447`) | новый код обязан падать явно при отсутствии KB, а не подставлять умолчания. Пин на это в T2 |
| Правка промптов ломает существующий поток откликов | `application_prep` и `job_analyzer` в горячем пути | T7 — последняя задача, отдельным коммитом, с полным прогоном существующих тестов писем |
| Обрезка 2500 символов уходит в прод незаметно | сейчас она **уже** действует | до T7 не менять; после — пин, что Avantis присутствует в промпте |
| Уже красный ратчет отчёта (24 при 18) | новый отчёт может добавить отметок | новые документы не ссылаются на строки кода форматом `файл.py:NNNN` без необходимости; проверять `tools/report_line_refs.py --symbols` после T4 |
| Сползание в авто-отклики | «адаптация резюме» легко превращается в «самому отправить» | вне Stage 0. `SUBMIT_ALLOWED` и 11 гейтов не трогаются |

---

## 8. Первая задача реализации — T1

**Почему именно она.** T1 — фундамент: без структурированного извлечения нет ни KB (T2), ни
сверки (T3), ни аудита (T4). И она же даёт первый измеримый результат: сегодня генератор писем
не видит роль Avantis и 11 лет раннего опыта (§1.5), потому что резюме режется на 2500 символов.
T1 делает эти факты **видимыми как данные** — и это проверяется пин-тестом, а не глазами.

**Почему она безопасна.** Только чтение: парсер читает `resume.md` и печатает claims. Ни одного
байта в `state.db`, `candidate_profile.json` или `resume.md` не меняется. Критерий приёмки
включает проверку хешей файлов до и после.

**Что конкретно сделать:**

1. Создать `ai_assistant/candidate/__init__.py` и `ai_assistant/candidate/resume_parser.py`.
2. Реализовать `parse_resume(text: str, source: str) -> list[ResumeClaim]` — разделы, роли,
   компании, локации, диапазоны дат, пункты-достижения, образование, языки, контакты.
3. Каждый claim: `kind`, `value`, `normalized`, `evidence={source_file, line, quote}`,
   `status=DERIVED_FROM_SOURCE`.
4. Добавить в `ai_assistant/cli.py` подкоманду `candidate-kb extract --dry-run` — печать без записи.
5. Написать `tests/test_candidate_resume_parser.py`: регрессия на реальном `resume.md` + краевые
   случаи. Обязательные пины: роль **Avantis** найдена; блок **2009–2018** найден; **Education**
   и **Languages** найдены; контакты (`mikhailthaiban@gmail.com`, `@brokeassh`) найдены.

**Критерий приёмки:**

- `candidate-kb extract --dry-run` печатает claims, сгруппированные по ролям, с номерами строк;
- число найденных ролей и диапазонов дат совпадает с ручным пересчётом по `resume.md`;
- все четыре пина зелёные; при отключении парсера ролей они краснеют (мутационная проверка);
- хеши `resume.md`, `candidate_profile.json`, `state.db` до и после прогона совпадают;
- существующий набор тестов не затронут.

**Оценка объёма:** один модуль (~200 строк), один тест-файл (~150 строк), одна подкоманда CLI.

---

## 9. Уже реализовано — дублировать не нужно

Отдельно, чтобы не изобретать заново:

1. **Профиль кандидата** — `CandidateProfile` с `from_dict`/`to_dict`/`from_json_file`, env-override,
   `DEFAULT_PROFILE_PATHS`. Расширяется новыми ключами без правки кода.
2. **Матчер** — `JobMatcher`, `MatchResult`, 7 семейств ролей, `role_priorities`,
   `role_family_seniority`, `SkillConfidenceLevel`, `evaluate_hard_requirements`,
   `extract_required_years`, `apply_preference_adjustment`.
3. **Deep-анализ** — `DeepAnalysisResult` с `gaps`, `missing_skills`, `why_fit`,
   `resume_adaptation_needed` и причинами; персистентность в таблице `deep_analysis`.
4. **Генерация писем** — LLM + truth-guard + детерминированный `_fallback_cover_letter`,
   проверка на выдуманные технологии, опциональное упоминание портфолио.
5. **Ответы на вопросы анкет** — `QuestionAnswerGenerator` со truth-only режимом и разбором
   резюме регулярками (имя, email, телефон, годы).
6. **Отклики** — `application_queue` с оценкой готовности, `application_review_gate`,
   `application_packages`, 11 гейтов отправки, fail-closed семантика, `SUBMIT_ALLOWED`.
7. **Поиск вакансий** — `Watcher`, 4 адаптера, нормализация, `vacancy_identity`, дедупликация,
   провенанс (`is_genuine_production_vacancy`).
8. **Telegram** — дайджест с inline-кнопками, `TelegramFeedbackProcessor`, owner-gate,
   идемпотентность, структурированные причины, темпоральное вытеснение фидбека.
9. **Обратная связь и калибровка** — `feedback coverage/analytics/provenance/simulate`,
   разделение факта и предпочтения, неизменяемость фактического профиля.
10. **CLI и инфраструктура** — ~60 команд, `hermes status/sync`, `production-health`,
    `audit --tracked`, единый лог с маскировкой секретов.

**Ничего из этого переписывать не предлагается.** Все задачи T1–T8 — добавление новых модулей и
ровно одна правка чтения в горячем пути (T7).

---

## 10. Что дальше

Реализация **не начата** и не будет начата до утверждения плана. Открытые вопросы к Михаилу:

1. **Согласовать порядок T1–T8** и первую задачу.
2. **Решение по трём противоречиям из §1.3** — какая версия верна: 15 или 11 лет; Bitrix — опыт
   или то, чего избегаем; что означает `years_experience: 3`. Это решает человек, не система.
3. **Подтвердить отказ от БД на Stage 0** в пользу версионируемого JSON.
4. **Подтвердить, что Telegram на Stage 0 не трогаем** — подтверждение фактов идёт через CLI.
