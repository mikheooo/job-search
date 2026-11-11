# T9 — FIRST MONEY RUN

**Дата:** 2026-10-02 · **Ветка:** `chore-ruff-cleanup` · **HEAD:** `98be2ea`
**Задача:** не зелёные тесты, а **минимум 5 реальных актуальных возможностей, на которые Миша может откликнуться прямо сейчас**.

**Итог одной строкой:** конвейер отдаёт `application_queue = 0` не из-за бага, а потому что
единственная разрешающая географическая полоса (A) в живых данных не встречается; настоящие
возможности лежат **вне** этого конвейера — на досках и в n8n-сообществе, и их пять.

> **Об этом документе.** Все числа здесь получены прогоном инструментов из `tools/.t9/` и
> воспроизводимы. Восемь собственных ошибок были найдены и исправлены **внутри этой задачи**; они
> перечислены в разделе 8 и в разделе 12, потому что отчёт, который прячет свои исправления, —
> это отчёт, которому нельзя верить. Ни одна цифра не взята из прошлых прогонов «по памяти».

---

## 1. Что считалось успехом и что получилось

| критерий | результат |
|---|---|
| Найти ≥ 5 реальных актуальных возможностей | **5** (TOP-5, раздел 7) |
| Объяснить `application_queue = 0` | **доказано замером** (раздел 3) |
| Построить измеренный Funnel Report | **построен** (раздел 4) |
| Ввести Practical Money Score с видимыми весами | **введён**, 10 компонент (раздел 5) |
| TOP-20 + TOP-5 APPLY NOW | **есть** (разделы 6, 7) |
| Проверить актуальность каждой TOP-5 | **проверено**, `NOT VERIFIED` там, где недоказуемо (раздел 8) |
| Черновики откликов на каждую TOP-5 | **5 × (short + full + CV) + 1 запасной** (раздел 9) |
| `EXTERNAL SENDS = 0` | **0** (раздел 10) |

---

## 2. Снимок состояния (снят до любых правок)

`state.db` открыт в режиме `mode=ro`; скрипт — `tools/.t9/snapshot.py`, сырые числа —
`tools/.t9/snapshot_raw.json`.

| величина | значение |
|---|---|
| вакансий всего | **2529** (все `state = NEW`) |
| по источникам | himalayas 785, habrcareer 759, weworkremotely 472, remoteok 392, **vacancies_json 83**, hh 37, x 1 |
| `match_decision` | SKIP 2378, REVIEW 116, **APPLY 35** |
| `match_score ≥ 70` | 74 |
| **`application_queue`** | **0 строк** |
| `application_reviews` | 20 (APPROVED 8, PENDING_REVIEW 6, COMPLETED 5, REJECTED 1) |
| `application_tracking` | 1794 (REJECTED 1673, ANALYZED 34, DISCOVERED 33, ANALYSIS_FAILED 21, **READY_TO_APPLY 13**, APPLIED 10, SUBMITTED 8, WITHDRAWN 2) |
| `vacancy_eligibility` | 2533 (ineligible 1466, **unknown 1039**, eligible 24, eligible_with_warning 4) |
| `application_submissions` | 21 (SUBMITTED 13, v1 8) |
| `deep_analysis` | 75 · `application_packages` 35 |
| последняя доставка дайджеста | **2026-09-29T03:03:42Z**, `message_id 562`, 5 вакансий |
| последний приём вакансий | `first_seen_at` max = **2026-09-29T03:00:20Z** → корпусу 3 дня |
| `system_settings` | `submit_paused = 0` |

**Профиль кандидата** (`candidate_profile.json` + `candidate_kb.json`, kb_version 28): Михаил
Колесников, Паттайя, `remote_required = true`, минимум **$1000/мес**, ICT +7, языки ru/en (B1),
стаж: it_support 11 л, system_admin 9 л, application_support 5 л, automation 3.5 г, python 3.5 г,
ai_llm 2 г. Ядро: python, n8n, automation, ai agents, rest api, webhooks, telegram, active
directory, sql. Исключено: 1c, php, ruby, java developer, c++, c#, senior lead.

**Подтверждено пользователем (KB, `CONFIRMED_BY_USER`)** — и это ограничивает черновики:
- `goal_direction`: «Приоритет: любая подходящая IT-работа, чтобы быстрее получить доход.»
- `preference_condition`: «Минимальный доход: от $1000 в месяц.»
- `experience_note`: **«С мая 2026 года я разрабатывал только собственные проекты, без оплачиваемых заказов.»**
- `chronology_note`: Speedcast/Germsat март 2018 — март 2021 подтверждён.

**Посторонний писатель.** Пока шёл замер, в проекте работал другой поток (`scratch_p5_collect.py`,
сбор гигов с Freelancer.com, пишет в `gigs`/`gig_events`). Вакансий он не касается: перепроверено —
`vacancies` 2529, `application_queue` 0, `first_seen_at` max без изменений. Не тронут.

---

## 3. Почему `application_queue = 0` — доказательство замером

Не «фильтр, наверное, слишком строгий». Прогон `tools/.t9/gate_walk.py` повторяет **ровно те же
ворота в том же порядке**, что `application_queue.generate_queue()` (строки 619–671), и
записывает первую, на которой вакансия падает. Сырой результат — `tools/.t9/gate_walk.json`.

### 3.1 Очередь строится только из `READY_TO_APPLY`

`generate_queue(status_filter="READY_TO_APPLY")` берёт **не** 2529 вакансий, а 13 строк
`application_tracking` со статусом `READY_TO_APPLY`. Это первое ограничение — и оно не дефект,
а замысел.

### 3.2 Все 13 падают на воротах eligibility

```
11 × G1_ELIGIBILITY_UNKNOWN      (geo band B)
 2 × G1_ELIGIBILITY_INELIGIBLE   (geo band C)
 0 × прошли
```

Код (`application_queue.py:636`):

```python
if str(e_status).lower() in ("ineligible", "unknown") or not geo_band_permits_auto_send(...)[0]:
    continue
```

`unknown` — это **не** «не смогли прочитать». Это вердикт классификатора: полоса **B** означает
«доступность работы из Таиланда не установлена». `geo_band_permits_auto_send`
(`eligibility.py:1388`) пропускает **только полосу A**:

```
band A → разрешено;  band B → «география не установлена»;  band C → «исключено»
```

### 3.3 Полоса A в живых данных не встречается

| выборка | строк | полоса A |
|---|---|---|
| весь корпус, пересчитано живьём | 2529 | **2** |
| 13 строк `READY_TO_APPLY` | 13 | **0** |
| свежий поиск HH сегодня, авторизованная сессия | 20 | **0** |
| живые вакансии T7.26/27 (историческая сверка) | 723 | **0** |

Те две полосы A во всём корпусе — это:

| sid | что это | почему не возможность |
|---|---|---|
| `hh:128659037` | Qulix Systems, «QA Automation Engineer (Java)» | трекинг `WITHDRAWN`; роль — QA на Java, не профиль |
| `weworkremotely:koast-ai-head-of-operations-koast-ai` | Koast.ai, «Head of Operations» | операционная роль, не инженерная |

### 3.4 Причина, одной фразой

**`application_queue = 0`, потому что очередь питается 13 строками `READY_TO_APPLY`, и у всех
тринадцати полоса B или C. Полоса A требует, чтобы работодатель своими словами разрешил работать
из Таиланда; ни одна живая вакансия этого не делает. Предохранитель работает правильно — просто
он же и есть причина нуля.**

Это не тот ноль, который «честен, потому что гейт прошёл»: гейт **не** прошёл. Это ноль
работающего предохранителя на входе, который никто не проверял на реальном потоке.

---

## 4. Funnel Report — измеренный, с причинами отсева

Скрипт `tools/.t9/funnel.py` (сырые причины — `tools/.t9/funnel.json`), минимальный балл 60,
свежесть 90 дней.

| стадия | вход | вышло | отсеяно | главные причины отсева |
|---|---:|---:|---:|---|
| S0 RAW | — | **2529** | — | всё, что лежит в `vacancies` |
| S1 обязательные поля | 2529 | 2529 | 0 | `title` / `job_url` / `description` заполнены у всех; `company` пуст у 452 |
| S2 дедупликация (точная) | 2529 | **2519** | 10 | 320 EXACT-алиасов в 310 группах; PROBABLE намеренно не схлопывается |
| S3 жёсткие фильтры | 2519 | **994** | 1525 | 449 «band B ineligible», 508 «не remote», 139+129 «band C», 42 `senior lead`, 35 `c#`, 34 `php`, 33 `c++`, 21 `1c`, 21 «adult» |
| S4 соответствие роли | 994 | **104** | 890 | 829 «role family не совпал», 12 «альтернативная роль», 45 «нет навыков» |
| S5 минимальный балл ≥ 60 | 104 | **99** | 5 | баллы 0–49 |
| S6 свежесть ≤ 90 дней | 99 | **98** | 1 | > 90 дней |
| S7 eligibility | 98 | **2** | 96 | **96 × `unknown` (band B)** |
| S8 полоса A | 2 | **2** | 0 | — |

**Читается так.** Конвейер отсеивает не по зарплате и не по свежести: 1525 отсева на жёстких
фильтрах — это в первую очередь география и формат, 890 — несовпадение роли, и затем **96 из 98
оставшихся убивает eligibility**, потому что полоса B не разрешает автоотправку. Полоса A даёт
ровно 2 строки, и обе — не возможности (см. 3.3).

**Отдельно: источник, который нельзя считать данными.** `vacancies_json` — это ручной сид-файл
`vacancies.json` (103 записи, собраны 2026-07-04 … 2026-08-19). В `state.db` у **65 из 83** его
строк стоит одинаковая зарплата `salary_min=4000 / salary_max=5000 USD`, тогда как сам сид говорит
«не указана», «180 000 – 260 000 ₽» или «Phase 0+1 quote + retainer ongoing». Ни у одного другого
источника такой картины нет. **Это подстановка, а не данные**, и Practical Money Score её не
использует (см. N7 в разделе 14).

**Второй дефект того же источника, найденный при отборе.** Сид содержит **одну вакансию дважды**:
треды `298577` и `299818` — это один и тот же оффер, опубликованный два раза (совпадают
компенсация, требования, обязанности и блок «To Apply»; во втором добавлена строка про Discord).
Сохранённые обрезки описаний в корпусе отличаются, поэтому дедупликация по стабильному id и по
тексту БД его не видит. На первом прогоне ранжирования обе копии попали в таблицу, и одна из них —
в TOP-5, то есть одна вакансия была предъявлена как две возможности. Лечение — в разделе 12.

---

## 5. Practical Money Score — веса видны, ничего скрытого

Реализация: `tools/.t9/money_score.py`. Десять компонент, каждая 0–10, вклад = вес × балл / 10.
Итог 0–100. **Никакого модельного суждения**: каждый компонент считается по названному правилу, и
рядом с каждым лежит строка-улика, по которой число можно пересчитать руками и не согласиться.

| компонент | вес | как измеряется |
|---|---:|---|
| Candidate Match | 18 | балл матчера (0–100) ÷ 10 |
| Salary / Budget | 14 | объявленная оплата против порога $1000; для сида — по тексту сида, не по БД |
| Remote Compatibility | 12 | `remote_mode` + полоса |
| Location Compatibility | 10 | полоса A = 10, B = 6, C = 2 |
| Skill Coverage | 12 | доля названных в вакансии навыков, которые есть в KB |
| Entry Barrier | 8 | тестовое задание / портфолио / виза / грейд выше |
| Application Effort | 6 | сколько шагов стоит канал отклика |
| Freshness | 6 | возраст; **для форумных лидов — дата последнего поста, а не дата сида** |
| Response Probability | 8 | прямой работодатель vs агрегатор, возраст, покрытие навыков |
| Time-to-Money | 6 | тип занятости |

Сумма весов = 100 (проверяется `assert` в модуле).

**Что было сломано и исправлено (раздел 12).** Компонент зарплаты читал числа неверно: шаблон
`(\d[\d\s ]{2,})` спотыкался о запятую в `$1,500/мес` и возвращал **500**, из-за чего оффер на
1.5× порога получал **0/10** вместо 4/10 — и, как следствие, не попадал в TOP-5. Теперь разбор
явный: разделители тысяч `,` и пробел, суффикс `k`, и **период берётся из текста** (мес / год →
÷12 / час → конвертация не выдумывается). Из диапазона берётся **нижняя граница** — сознательно
пессимистично: сид пишет «125 000 – 400 000 ₽/мес», и верхняя граница удвоила бы балл на числе,
которого заказчик может и не заплатить. Строка-улика теперь цитирует поле, а не пересказывает его
(«employment_type='full time'», а не «full-time salaried»).

---

## 6. TOP-20

Полная таблица — `tools/.t9/final_ranked.json` (90 строк). Здесь — двадцать первых, **после**
отсева `GONE`/`CLOSED`, после схлопывания дубликата-перепоста и после слияния наблюдений
по правилу раздела 8.3.

| # | Должность | Компания | Источник | Зарплата | Remote | Match | Money | Главный риск | URL |
|---|---|---|---|---|---|---|---|---|---|
| 1 | AI agent engineer | Sticker Mule | RemoteOK / WWR | **$150 000–250 000/год** + $20k бонус | полностью | 67 | **76.3** | Go/TypeScript/GraphQL/GCP в требованиях; «writes clearly in English» при B1 | https://remoteok.com/remote-jobs/remote-ai-agent-engineer-sticker-mule-1137399 |
| 2 | **[HIRING] N8N AI Automation Developer (Remote)** | частный работодатель (n8n Community) | n8n-форум | **$1 500/мес (Wise)** — сказано в посте | полностью | 87 | **76.1** | просят **Loom-видео 5–7 мин**; английский B1 | https://community.n8n.io/t/hiring-n8n-ai-automation-developer-remote/298577 |
| 3 | Remote Backend Django Engineer – AI & Algorithmic Systems | Spotter Labs | himalayas | не указана | да | 75 | 74.5 | himalayas отдаёт 403 — **NOT VERIFIED** | https://himalayas.app/companies/spotter-labs/jobs/remote-backend-django-engineer-ai-algorithmic-systems-7271859842 |
| 4 | **Технический специалист онлайн-школы / AI Automation Engineer (EdTech, GetCourse, Bitrix24)** | **Прорыв** | **hh** | **от 100 000 ₽/мес** | да | 66 | **74.5** | форма ГПХ + полоса B; РФ-рынок | https://hh.ru/vacancy/137336564 |
| 5 | QA Automation Engineer (Java) | Qulix Systems | hh | не указана | да | 72 | 74.4 | трекинг `WITHDRAWN`; роль не профильная | https://hh.ru/vacancy/128659037 |
| 6 | Системный администратор Linux (Fintech) | Лига Цифровой Экономики | hh | не указана | да | 76 | 73.5 | K8s/Kafka/Gravitee — сильно выше профиля | https://hh.ru/vacancy/137784849 |
| 7 | **N8n Builder Wanted – AI Automation Agency** | AI Automation Agency (Индия) | n8n-форум | не указана (за проект) | да | 87 | **73.3** | ставка не названа; агентство в Индии | https://community.n8n.io/t/n8n-builder-wanted-ai-automation-agency-india/297943 |
| 8 | AI Sales Automation Engineer (n8n / Agentic) | Private employer (Reddit) | Reddit | $30/час (~$4 800/мес) по сиду | да | 77 | 73.3 | Reddit отдаёт JS-шелл без `<h1>` — **SUSPECT** | https://www.reddit.com/r/forhire/comments/1qyv06v/hiring_ai_sales_automation_engineer_n8n_agentic/ |
| 9 | **Seeking a reliable n8n developer for ongoing project-based work** | n8n Community | n8n-форум | не указана (за проект) | да | 78 | **71.6** | нет ставки; работа в sandbox заказчика | https://community.n8n.io/t/seeking-a-reliable-n8n-developer-for-ongoing-project-based-work-build-maintain/296828 |
| 10 | **[Hiring] n8n Automation Specialist — Paid, Long-Term, Remote (E-Commerce/Shopify)** | n8n Community | n8n-форум | не указана (paid, long-term) | да | 77 | **71.5** | OP просит JavaScript code nodes | https://community.n8n.io/t/hiring-n8n-automation-specialist-paid-long-term-remote-e-commerce-shopify/284836 |
| 11 | AQA Тестировщик (Python) | ITK academy | habrcareer | 75 000–90 000 ₽ | да | 73 | 71.3 | **ниже порога** ($825); junior; QA | https://career.habr.com/vacancies/1000168633 |
| 12 | Инженер по ИИ-автоматизации (n8n + Python) | AI-Lab Global | hh (сид) | 250 000–350 000 ₽ по сиду | да | 87 | 70.9 | сид от 2026-08-18; HH блокирует — **NOT VERIFIED** | https://hh.ru/vacancy/135112049 |
| 13 | Инженер AI-автоматизации (n8n / Python / LLM) | DataMind Studio | hh (сид) | 200 000–320 000 ₽ по сиду | да | 87 | 70.9 | сид от 2026-08-18; **NOT VERIFIED** | https://hh.ru/vacancy/135248190 |
| 14 | AI Python Engineer | CRT | hh | не указана | да | 85 | 70.3 | 34 дня; **NOT VERIFIED** | https://hh.ru/vacancy/135838069 |
| 15 | Middle Python Developer (Backend + Web Scraping) | AbeloHost B.V. | hh | не указана | да | 85 | 70.3 | 34 дня; **NOT VERIFIED** | https://hh.ru/vacancy/135893265 |
| 16 | Hiring Remote n8n Experts for AI Automation Solutions | A_Has | n8n-форум | не указана (paid remote) | да | 88 | 70.0 | тред живой, но 10 дней тишины | https://community.n8n.io/t/hiring-remote-n8n-experts-for-ai-automation-solutions/176584 |
| 17 | Hiring n8n Developer AI Quoting Agent, Swiss Plumbing/HVAC | n8n Community | n8n-форум | не указана (paid, long-term) | да | 87 | 69.9 | 10 дней тишины; ниша plumbing/HVAC | https://community.n8n.io/t/hiring-n8n-developer-ai-quoting-agent-swiss-plumbing-hvac-paid-long-term/302292 |
| 18 | HIRING: Freelance/Agency AI Automation Engineer (n8n / Make / APIs) | n8n Community | n8n-форум | не указана | да | 88 | 69.8 | тред обновлён сегодня; 143 поста — высокая конкуренция | https://community.n8n.io/t/hiring-freelance-agency-ai-automation-engineer-n8n-make-apis-remote/275368 |
| 19 | Разработчик ETL / Data Engineer | Онлайн-кинотеатр Иви | habrcareer | не указана | да | 72 | 69.8 | 29 дней; роль ETL, не агентная | https://career.habr.com/vacancies/1000167966 |
| 20 | Python Backend Developer (AI Agents Systems) | V4Scale | hh (сид) | не указана | да | 76 | 68.9 | сид от 2026-08-18; **NOT VERIFIED** | https://hh.ru/vacancy/134622712 |

Строки 5 (отозвана) и 11 (ниже порога) оставлены в таблице **намеренно** — чтобы было видно, что
ранжирование их не прячет; раздел 14 показывает, что в TOP-5 они не проходят. Строка BGStaff
(`habrcareer:1000168402`), которая была здесь в первом прогоне, **исключена**: она измерена как
`CLOSED` («вакансия в архиве»), и таблица такие строки не содержит.

---

## 7. TOP-5 — APPLY NOW

Отбор механический, без вкусовщины: строки с `verified_role_open = true` (канал подтверждён живым
**и** полоса A/B **и** свежесть ≤ 14 дней), отсортированные по Practical Money Score. Всего таких
строк **33**; ниже — пять первых.

### №1 — Sticker Mule, «AI agent engineer»

| поле | значение |
|---|---|
| Должность | AI agent engineer |
| Компания | Sticker Mule (Нью-Йорк, commerce + производство + AI) |
| Источник | RemoteOK (та же вакансия есть на WWR, `weworkremotely:sticker-mule-ai-agent-engineer`) |
| URL | https://remoteok.com/remote-jobs/remote-ai-agent-engineer-sticker-mule-1137399 |
| Зарплата / бюджет | **$150 000–250 000 в год** + $20 000 подписного бонуса, 4 недели отпуска — **сказано на странице**. В строке БД стоит `150000` без периода; компонент это прямо оговаривает («row records no pay period; assumed monthly»). На балл это не влияет: и 12.5×, и 150× порога дают 10/10 |
| Remote | полностью, «Anywhere in the World, New York» |
| Локация | мир; работодатель не ограничивает |
| Почему подходит | вакансия ровно про то, что Миша делает четыре месяца: строить агентов, которые работают сами. «Identify where agents can help and build them accordingly», «Measure results and remove agents that don't deliver» — это Reels Analyzer и Job Search AI дословно |
| Что совпадает | агенты в проде (4 своих проекта), LLM-стек (OpenAI / Gemini / Anthropic), Python, Postgres, Docker, интеграции с внешними API |
| Что не совпадает / неизвестно | в требованиях **Go, TypeScript, GraphQL, GCP** — этого в KB нет; «Writes clearly in English» при английском **B1**; Grok в списке моделей не использовался |
| Риски | (1) языковой барьер — B1 против «writes clearly»; (2) Go/TS — придётся признать пробел, не выдумывать; (3) $150k+ привлекает сильных кандидатов, конкуренция высокая |
| Почему стоит откликнуться сейчас | вакансия от 2026-09-16, **сегодня подтверждена дважды** — страница RemoteOK отдаёт `<h1>Remote AI agent engineer`, страница WWR — `<h1>AI agent engineer`, и она присутствует в текущем листинге WWR; при такой зарплате даже один отклик стоит десяти на 1000 ₽ |
| Practical Money Score | **76.3 / 100** (match 12.06 + salary 14.0 + remote 9.6 + location 6.0 + skills 12.0 + barrier 6.4 + effort 4.2 + freshness 4.2 + response 4.8 + ttm 3.0) |
| Рекомендация | **APPLY NOW** |

### №2 — n8n Community #298577, «[HIRING] N8N AI Automation Developer (Remote)»

| поле | значение |
|---|---|
| Должность | N8N AI Automation Developer |
| Компания / клиент | частный работодатель через n8n Community; контакт — Discord `ymagic.hiring` (указан в перепосте) |
| Источник | n8n Community, раздел Jobs |
| URL | https://community.n8n.io/t/hiring-n8n-ai-automation-developer-remote/298577 |
| Зарплата / бюджет | **$1 500 USD в месяц, выплаты исключительно через Wise** — сказано в первом посте |
| Remote | да, полностью |
| Локация | не ограничена |
| Почему подходит | требования перечислены списком и почти все закрыты KB: n8n, AI-автоматизация, LLM (ChatGPT/Claude/Gemini), prompt engineering, интеграции API. В «nice to have» — Python/JavaScript, векторные БД, RAG, AI-агенты и **автоматизация видео/контента/соцсетей** — последнее у Миши в проде (Reels Analyzer) |
| Что совпадает | n8n, LLM, prompt engineering, API/webhooks, RAG, AI-агенты, видео-пайплайны, Python |
| Что не совпадает / неизвестно | «Good written and spoken English» — при B1 это риск; векторные БД в проде не подтверждены (в KB `postgresql` — PROJECT) |
| Риски | (1) **для отклика требуется Loom-видео 5–7 минут** про 1–3 проекта, самый сложный свой системы и почему ты подходишь — это реальный барьер, и он назван в посте, а не выведен; (2) английский; (3) Wise как единственный канал выплат |
| Почему стоит откликнуться сейчас | тред обновлён **2026-09-26** (5 дней), не закрыт, 68 сообщений, 4877 просмотров; ставка названа — переписка начинается не с торга |
| Practical Money Score | **76.1 / 100** (match 15.66 + salary 5.6 + remote 9.6 + location 6.0 + skills 12.0 + barrier 6.4 + effort 2.4 + freshness 6.0 + response 6.4 + ttm 6.0) |
| Рекомендация | **APPLY NOW** (Loom записать; это единственное настоящее препятствие) |

> **Оговорка по этому треду.** `barrier` = 8/10 с уликой «no barrier named in the excerpt» —
> компонент читает обрезок описания из корпуса, а не полный текст поста, поэтому Loom-видео он не
> увидел. Балл от этого завышен. Внесено в раздел 11.

### №3 — Прорыв, «Технический специалист онлайн-школы / AI Automation Engineer (EdTech, GetCourse, Bitrix24)»

| поле | значение |
|---|---|
| Должность | Технический специалист онлайн-школы / AI Automation Engineer |
| Компания | Прорыв (онлайн-школа) |
| Источник | HH.ru |
| URL | https://hh.ru/vacancy/137336564 |
| Зарплата / бюджет | **от 100 000 ₽/мес до вычета налогов** (≈ $1 100) — сказано на карточке |
| Remote | **да**, `work_formats` = REMOTE (единственный из 20 сегодняшних чтений с чистым remote) |
| Локация | Москва в поле доски, но формат — удалённо |
| Почему подходит | вакансия называет три вещи, которые Миша делал в Sayama Travel одновременно: **Bitrix24**, **GetCourse**, **AI-автоматизация** образовательной школы. Это не «похоже» — это тот же класс задач |
| Что совпадает | Bitrix24 (4 года, в проде), AI-интеграции в CRM, автоматизация бизнес-процессов, техподдержка пользователей, Python |
| Что не совпадает / неизвестно | GetCourse в KB не зафиксирован как отдельный навык — опыт есть по классу задач, но не подтверждён записью; форма оформления — «Договор ГПХ (вид не распознан)» |
| Риски | (1) **полоса B** — работодатель нигде не говорит про работу из Таиланда; (2) ГПХ с российским заказчиком при жизни в Таиланде требует отдельного решения по оформлению; (3) рынок РФ, ставка в рублях |
| Почему стоит откликнуться сейчас | вакансия прочитана **сегодня, 2026-10-02, авторизованной headless-сессией** (полный текст, 20 вакансий за один проход, `auth_ok = True`); `response_allowed = true`, тестового задания нет (`hasTests: false`) |
| Practical Money Score | **74.5 / 100** (match 11.88 + salary 5.6 + remote 9.6 + location 6.0 + skills 12.0 + barrier 6.4 + effort 6.0 + freshness 6.0 + response 8.0 + ttm 3.0) |
| Рекомендация | **APPLY NOW** (с явным вопросом про оформление из Таиланда) |

### №4 — n8n Community #297943, «N8n Builder Wanted – AI Automation Agency»

| поле | значение |
|---|---|
| Должность | n8n-специалист для агентства, работа за проект, расчёт на постоянное сотрудничество |
| Компания / клиент | Saif, Хайдарабад, Индия — строит AI-automation агентство; он ведёт клиентов, продажи и скоуп, исполнитель собирает автоматизации |
| Источник | n8n Community, раздел Jobs |
| URL | https://community.n8n.io/t/n8n-builder-wanted-ai-automation-agency-india/297943 |
| Зарплата / бюджет | не объявлена: «Paid per project (fair rates, no lowballing)». Первым числом якорить нечем — ставку надо спросить |
| Remote | да, полностью; «flexible — work on your own schedule» |
| Локация | не ограничена |
| Почему подходит | требования минимальны и закрыты целиком: **hands-on n8n**, **AI-агенты (OpenAI, webhooks, APIs)**, коммуникация. Это дословно ядро KB — и это единственная из TOP-5, где в требованиях нет ничего, чего у Миши нет |
| Что совпадает | n8n, AI-агенты, OpenAI, webhooks, REST API, Python |
| Что не совпадает / неизвестно | ставка не названа вообще; юридически это фриланс с индийским заказчиком — вопрос оформления и способа выплат открыт |
| Риски | (1) ставки нет — и в треде уже 71 сообщение, включая предложения с конкретными цифрами ($500–900 за workflow), то есть якорить придётся аккуратно; (2) поток проектов зависит от одного человека; (3) Индия как контрагент — уточнить канал выплат |
| Почему стоит откликнуться сейчас | **последний пост — 2026-09-26 (6 дней)**, тред не закрыт, 71 сообщение, 2469 просмотров; в требованиях нет ни одного пробела — это самая «чистая» по совпадению строка всего TOP-20 |
| Practical Money Score | **73.3 / 100** (match 15.66 + salary 2.8 + remote 9.6 + location 6.0 + skills 12.0 + barrier 6.4 + effort 2.4 + freshness 6.0 + response 6.4 + ttm 6.0) |
| Рекомендация | **APPLY NOW** (первым сообщением — вопрос про ставку и формат расчёта) |

### №5 — n8n Community #296828, «Seeking a reliable n8n developer for ongoing project-based work»

| поле | значение |
|---|---|
| Должность | n8n developer, contract / per-project, с расчётом на постоянное сотрудничество |
| Компания / клиент | небольшая AI-консалтинговая и внедренческая фирма (OP — её основатель) |
| Источник | n8n Community, раздел Jobs |
| URL | https://community.n8n.io/t/seeking-a-reliable-n8n-developer-for-ongoing-project-based-work-build-maintain/296828 |
| Зарплата / бюджет | не объявлена; «contract, per-project basis», «recurring projects are likely» |
| Remote | да, полностью |
| Локация | не ограничена |
| Почему подходит | OP строит «AI agents, agentic workflows, RAG systems, and integrations» — это дословно стек Миши; n8n назван ядром поставки |
| Что совпадает | n8n, AI-агенты, RAG, интеграции, документированный JSON-хендовер, работа в sandbox |
| Что не совпадает / неизвестно | ставка не названа → «RATE NEGOTIABLE», но и не подтверждена; требуется самостоятельно оценивать объём |
| Риски | (1) оплата не объявлена — первый вопрос в переписке; (2) работа через sandbox заказчика, без доступа к прод-данным (это же и плюс: чистые границы); (3) в треде 162 сообщения — высокая конкуренция |
| Почему стоит откликнуться сейчас | **последний пост — сегодня, 2026-10-02**, тред не закрыт, 162 сообщения, 4361 просмотр. Это живой канал, а не архив |
| Practical Money Score | **71.6 / 100** (match 14.04 + salary 2.8 + remote 9.6 + location 6.0 + skills 12.0 + barrier 6.4 + effort 2.4 + freshness 6.0 + response 6.4 + ttm 6.0) |
| Рекомендация | **APPLY NOW** |

---

## 8. Проверка актуальности

### 8.1 Что именно проверялось

Для каждой строки: URL отвечает; вакансия не закрыта; должность совпадает с записью; компания
существует; зарплата не выдумана; remote/локация не выдуманы; требования взяты из источника;
дата не искажена. **Непроверяемое пишется `NOT VERIFIED`, не выдумывается.**

Инструменты: `tools/.t9/verify.py` (канонический прогон — `verify_final.json`; предыдущие прогоны
`verify_fix.json`, `verify_full.json`, `verify.json`, `verify_hh_only.json`, `recheck_habr.json`,
`liveness.json` оставлены на диске как история наблюдений), активность форума — `n8n_threads.py`.

### 8.2 Правила по доскам — потому что «200 OK» ничего не значит

| доска | что делает | вердикт |
|---|---|---|
| weworkremotely | на удалённую вакансию отдаёт **свою главную** («We Work Remotely: Advanced Remote Job Search») | главная вместо вакансии = **GONE** |
| habrcareer | держит страницу живой, но тело заменяет на «Вакансия в архиве … на неё нельзя откликнуться» | **CLOSED** |
| hh.ru | на обычный HTTP-клиент отдаёт «VPN мешает работе сайта» | **NOT VERIFIED** (нужен браузер) |
| himalayas | 403 на любой запрос | **NOT VERIFIED** |
| reddit | отдаёт JS-шелл без `<h1>` | **SUSPECT** |
| remoteok | отдаёт настоящую страницу вакансии | **LIVE** по `<h1>` |
| linkedin | на удалённую вакансию 302-редиректит на страницу поиска (`?trk=expired_jd_redirect`) | **GONE** |
| arc.dev | на удалённую вакансию 302-редиректит в категорию (`/remote-jobs/back-end`) | **GONE** |
| community.n8n.io | отдаёт страницу треда; активность берётся из `<topic>.json` (Discourse API) | **LIVE** по `<h1>` + `closed`/`last_posted_at` |

### 8.3 Четыре собственные ошибки, найденные и исправленные здесь же

Отчёт, который не показывает свои ошибки, врёт по умолчанию. Все четыре — мои, все найдены
контролем, а не удачей.

**(а) Ложный зелёный на hh.ru.** Первая версия инструмента ставила `LIVE` строке, у которой
`<h1>` = «VPN мешает работе сайта». Это не вакансия — это заглушка. Исправлено: добавлен список
блокирующих маркеров, и заблокированная страница теперь `NOT VERIFIED`. Так 9 строк HH перестали
быть «живыми».

**(б) Потеря вердикта `CLOSED` при повторном прогоне.** `habrcareer:1000168402` (BGStaff) на
первом прогоне был измерен как `CLOSED` («вакансия в архиве»). Повторный прогон в тот же файл
попал на таймаут TLS и записал `NOT VERIFIED` — то есть **«не смогли прочитать» превратилось в
пропуск**. Это ровно тот fail-open, который запрещён правилами проекта. Исправлено двумя вещами:
опцией `--out`, чтобы прогон не затирал предыдущее наблюдение, и правилом слияния (см. ниже).

**(в) 26 живых тредов n8n были прочитаны как «captcha-стена».** Правило искало блокирующие маркеры
по **сырому HTML**. Discourse кладёт свои настройки внутрь `<script>`, и там есть буквальная строка
`"discourse_captcha_enabled": false` — то есть **captcha выключена**. Мой чек принял имя настройки
за стену и уронил 26 строк в `NOT VERIFIED`, хотя страницы загрузились с правильным `<title>` и
правильным `<h1>`. Исправлено: маркеры ищутся только в **видимом тексте** (после вырезания
`<script>`/`<style>`). Контроль — N10.

**(г) 6 удалённых вакансий были прочитаны как живые.** Правило «HTTP 200 + есть `<h1>`» — не
доказательство. LinkedIn на удалённую вакансию отдаёт 302 на страницу поиска и оставляет
`<h1>9,000+ Virginia Military Institute Jobs in United States</h1>`; arc.dev редиректит в
категорию; две строки RemoteOK редиректят на **другую вакансию**. Все шесть выглядели живыми.
Исправлено правилом: **сегмент URL самой вакансии обязан остаться в итоговом URL** — легитимные
редиректы его сохраняют (hh кладёт id в `backUrl`, у n8n при переименовании треда сохраняется id),
а потеря сегмента означает, что доска отдала не эту вакансию. Контроль — N9.

**И пятая, не про актуальность, а про слияние.** Пессимистичное слияние наблюдений («берём худший
вердикт») спасало от (б), но заодно **выбрасывало хорошее измерение**, если более ранний прогон
случайно упал по сети: из 15 живых строк 14 были перебиты артефактом первого, заведомо сломанного
прохода. Правило переписано: **«не смог прочитать» — это не вердикт, а его отсутствие.**
`NOT VERIFIED` исключается, пока есть хоть одно читаемое наблюдение; среди читаемых берётся самый
пессимистичный. Так `CLOSED` (архив) остаётся `CLOSED`, а `LIVE` не превращается в `NOT VERIFIED`
из-за таймаута.

### 8.4 Итог по TOP-5

| # | строка | вердикт | чем подтверждено |
|---|---|---|---|
| 1 | Sticker Mule | **LIVE** | RemoteOK отдаёт `<h1>Remote AI agent engineer`; WWR — `<h1>AI agent engineer`, `$150,000–$250,000` видно в тексте; присутствует в текущем листинге WWR |
| 2 | n8n #298577 | **LIVE** | страница треда отдаёт `<h1>[HIRING] N8N AI Automation Developer (Remote)`; `last_posted_at = 2026-09-26T23:04:32Z`, `closed = false`, 68 постов, 4877 просмотров; `$1,500 USD per month` — в первом посте |
| 3 | Прорыв, hh 137336564 | **LIVE** | прочитана **сегодня** авторизованной headless-сессией; полный текст, `response_allowed = true` |
| 4 | n8n #297943 | **LIVE** | `<h1>N8n Builder Wanted – AI Automation Agency (India)`; `last_posted_at = 2026-09-26T13:12:35Z`, `closed = false`, 71 пост, 2469 просмотров |
| 5 | n8n #296828 | **LIVE** | `<h1>Seeking a reliable n8n developer for ongoing project-based work…`; `last_posted_at` — сегодня, `closed = false`, 162 поста, 4361 просмотр |

**Чего проверить не удалось — и потому написано прямо:** для №2, №4, №5 подтверждено, что **тред
живой**, но **не** подтверждено, что конкретная роль из первого поста ещё открыта. Это
`ROLE STATUS: NOT VERIFIED`. Для №3 полоса B: работодатель нигде не говорит про Таиланд —
`GEOGRAPHY: NOT VERIFIED`.

---

## 9. Черновики откликов

Только факты из Candidate KB (`resume.md` → `candidate_kb.json`, kb_version 28) и четыре
подтверждённых проекта: **Reels Analyzer**, **Job Search AI**, **WHUBN**, **REPITON**.
Резюме-мастер **не изменялось**.

> **Общее правило для всех черновиков.** С мая 2026 года оплачиваемых заказов не было
> (`experience_note`, CONFIRMED_BY_USER). Ни один черновик не должен делать вид, что было.

---

### 9.1 Sticker Mule — AI agent engineer

**Short application (поле «cover letter»):**

> I build AI agents that run on their own and do real work — four of them are in production right
> now. Reels Analyzer is a Telegram bot and async worker pipeline (Python, ARQ, Redis, PostgreSQL,
> Docker) that takes short-form video, extracts transcripts and visual evidence, fact-checks the
> claims against web sources and publishes the result. Job Search AI collects vacancies from five
> boards, scores them against a candidate profile and drives a gated application flow end to end.
> I have not used Go or TypeScript in production, and I would rather say that up front than have you
> find out in a pairing session. Everything else in the role — agents that operate unsupervised,
> measuring whether they deliver, wiring them to third-party and internal tools — is what I do
> daily. My English is B1 and improving; I write clearly and I am comfortable being told when I do
> not.

**Full application:**

> Dear Sticker Mule team,
>
> You are hiring someone to build, run and manage a team of AI agents. That is the work I have been
> doing for the last four months, on my own systems, with real users.
>
> **What I have built.** Reels Analyzer: a Telegram bot that accepts video URLs and queues them for
> processing through Redis and ARQ, with PostgreSQL as the job store. The pipeline extracts
> transcripts, pulls visual evidence, checks each claim against external sources through Exa, and
> publishes the result to a channel — with QA controls on the fact-checking so a wrong claim does
> not go out. Job Search AI: a system that collects vacancies from habrcareer, RemoteOK, We Work
> Remotely and Himalayas, scores them against a structured candidate profile, prepares tailored
> applications and — behind explicit safety gates — can fill and submit HH.ru forms. Everything that
> touches the outside world is gated: the default mode requires a human decision, and automated
> modes are strictly opt-in. WHUBN is a local-first iOS app in Swift 6 that reconstructs where a
> person's life happened from permitted location evidence.
>
> **How I work with models.** OpenAI, Google Gemini and Anthropic APIs in production; prompt
> engineering, RAG, transcription and summarisation, classification, and cost-aware routing between
> models. Python is my main language, with asyncio, aiogram and Telethon for the asynchronous and
> messaging side; PostgreSQL, Docker and Linux are the operational base.
>
> **Where I am honest.** Your requirements list Go, TypeScript, GraphQL and GCP. I have not shipped
> production code in Go or TypeScript, and my GCP experience is limited. I have worked with GraphQL
> only as a consumer. I am not going to claim otherwise. If the role needs those on day one, I am
> not the right hire. If the core of the job is designing agents that operate unsupervised, deciding
> where they help, connecting them to internal and third-party tools, and removing the ones that do
> not earn their keep — that is exactly what I have been doing, and I would like to talk.
>
> My English is B1. I write clearly, I ask when I do not understand, and I am in ICT (+7), which
> gives a useful overlap with both Europe and the US East Coast.
>
> Mikhail Kolesnikov
> mikhailthaiban@gmail.com · Telegram @brokeassh · github.com/mikheooo

**Что изменить в CV (рекомендации, резюме не менялось):**
1. Поднять **проекты выше опыта**: в этой вакансии агенты важнее хронологии. Блок «Reels Analyzer /
   Job Search AI / WHUBN» поставить сразу под SUMMARY, с одной строкой результата на каждый.
2. В SUMMARY убрать «15+ years of IT experience» из первого предложения — для этой роли это
   читается как «сеньор из инфраструктуры», а не «агентный инженер». Начать с «Builds AI agents
   that run unsupervised in production».
3. **Не добавлять** Go/TypeScript/GraphQL/GCP ни в один раздел. Пробел называется в письме.
4. Добавить одну строку про **gated-архитектуру** Job Search AI — она показывает инженерную
   зрелость в теме «агенты, которые делают что-то во внешнем мире», и это ровно то, что просит
   вакансия.
5. Английский: не менять «B1» на что-то другое.

---

### 9.2 n8n Community #298577 — N8N AI Automation Developer, $1 500/мес

**Главное про этот отклик:** OP просит **Loom-видео 5–7 минут**. Это не формальность, а фильтр —
без видео отклик, скорее всего, не рассмотрят. Письмо ниже рассчитано на сопровождение видео.

**Short application (ответ в тред / DM):**

> Hi — n8n + LLM automation is what I do. Recent: a Telegram bot and async worker pipeline that
> takes short-form video, transcribes it, fact-checks the claims against web sources and publishes
> the result (Python, ARQ, Redis, PostgreSQL, Docker); and a job-search automation system that
> collects vacancies from five boards, scores them against a candidate profile and prepares tailored
> applications behind explicit approval gates. Both are n8n/API/webhook work with prompt engineering
> and cost-aware model routing across OpenAI, Gemini and Anthropic. The "nice to have" list is
> mostly things I have shipped: Python, RAG, AI agents, and video/content automation. English B1,
> fully remote from Thailand (ICT +7). Loom video with the walkthrough is here: [LINK].

**Full application:**

> Hi,
>
> You are looking for someone to design and build AI-powered workflows in n8n, with LLMs, prompt
> engineering and API integrations. That is the centre of what I do.
>
> **What I have built.** Reels Analyzer — a Telegram bot that accepts video URLs and queues them for
> processing (Redis + ARQ async task queue, PostgreSQL job store, Docker). The pipeline extracts
> transcripts, pulls visual evidence, checks each claim against external sources through Exa with QA
> controls, and publishes to a channel. Job Search AI — vacancy collection from habrcareer, RemoteOK,
> We Work Remotely and Himalayas, scoring against a structured candidate profile, tailored
> application preparation, and a gated submit path with read-only verification afterwards.
> Everything that touches the outside world is behind an explicit gate by design.
>
> **n8n and integrations.** n8n workflows connecting databases, AI agents, Google Workspace APIs,
> CRM (Bitrix24), notification channels and external services; LLM integration into corporate chat
> and CRM workflows for incoming-request processing, lead classification and multilingual response
> generation; a voice-message pipeline using Whisper for transcription followed by AI summarisation
> and storage into CRM deal records.
>
> **Against your requirements.** n8n — yes, core. AI automation systems — yes, in production. LLMs
> (ChatGPT, Claude, Gemini) — all three in production. Prompt engineering — daily. API and
> third-party integrations — REST, webhooks, Telegram Bot API, Google Workspace, MTProto. Python —
> yes, with asyncio. JavaScript — I have used it but Python is where I am strong.
>
> **Straight answers to two things.** My English is B1 — written work is not a problem, live calls
> are slower and I will say so rather than pretend. Vector databases and RAG: I have built RAG
> pipelines, but I have used PostgreSQL rather than a dedicated vector store.
>
> Based in Thailand (ICT, +7), fully remote. I am comfortable with the $1,500/month and Wise.
>
> Mikhail Kolesnikov · mikhailthaiban@gmail.com · Telegram @brokeassh

**План Loom-видео (5–7 минут, как просит OP):**
1. **0:00–1:00** — кто и где: Паттайя, ICT +7, четыре месяца своих агентных проектов.
2. **1:00–3:00** — Reels Analyzer: экран, пайплайн видео → Whisper → разбор → публикация; показать,
   что это прод, а не демо.
3. **3:00–5:00** — Job Search AI как «самый сложный свой системы»: пять досок, скоринг, ворота
   безопасности; отдельно — почему по умолчанию требуется решение человека.
4. **5:00–6:30** — n8n напрямую: Bitrix24, Google Workspace, LLM-интеграции, webhooks.
5. **6:30–7:00** — честно: английский B1, векторные БД не в проде. Ровно как в письме.

**Что изменить в CV:**
1. Добавить **видео/контент-автоматизацию** как отдельный пункт проекта: в вакансии это «nice to
   have», и у Миши есть работающий пайплайн, а не курс. Сейчас в резюме это размазано по duty.
2. Про **векторные БД** не добавлять ничего: в KB `postgresql` — PROJECT, отдельной векторной БД
   нет. В письме это сказано прямо.
3. Английский — оставить B1.
4. Убрать из SUMMARY «15+ years of IT experience» в начало: для этой роли важнее «ships AI
   workflows», а инфраструктурный стаж читается как нерелевантный.

---

### 9.3 Прорыв — Технический специалист онлайн-школы / AI Automation Engineer

**Короткий отклик (сопроводительное на HH):**

> Здравствуйте! Три вещи из вашей вакансии я делал одновременно и в проде четыре года: Bitrix24,
> автоматизация бизнес-процессов и внедрение AI в работу школы/отдела. В Sayama Travel я
> настраивал в Bitrix24 бизнес-процессы, роботов, триггеры, телефонию, e-mail и каналы
> лидогенерации; встраивал OpenAI и Gemini в корпоративные чаты и CRM — обработка входящих
> заявок, классификация лидов, генерация ответов на нескольких языках; собирал пайплайн
> голосовых сообщений через Whisper с AI-суммаризацией в карточку сделки. Плюс Telegram-боты на
> Python (aiogram) и n8n-сценарии, связывающие Bitrix24, Google Sheets, Telegram-каналы и внешние
> базы. Сейчас я в Таиланде и работаю удалённо — подскажите, как у вас решается оформление при
> работе из-за границы. Telegram: @brokeassh.

**Полное сопроводительное:**

> Здравствуйте!
>
> Ваша вакансия — это три навыка, которые у меня сходятся в одной точке: Bitrix24, AI-автоматизация
> и техническая поддержка образовательного/клиентского контура.
>
> **Bitrix24.** В Sayama Travel (ноябрь 2022 — апрель 2026) я автоматизировал бизнес-процессы
> вокруг Bitrix24: рабочие процессы, роботы, триггеры, телефонию, e-mail, аналитику и каналы
> лидогенерации. До этого, в Avantis (апрель 2021 — август 2022), администрировал self-hosted
> Bitrix24: интеграции, бизнес-процессы, роботы автоматизации, системные сценарии, и обеспечивал
> техподдержку бизнес-пользователей. То есть и «сделать», и «поддержать» — обе стороны.
>
> **AI в рабочем контуре.** Встраивал OpenAI и Google Gemini в корпоративные чаты и CRM-сценарии:
> обработка входящих заявок, классификация лидов, генерация ответов на нескольких языках. Собрал
> пайплайн голосовых сообщений: Whisper → AI-суммаризация → запись в карточку сделки Bitrix24.
> Строил n8n-сценарии, связывающие Bitrix24, Google Sheets, Telegram-каналы и внешние базы.
>
> **Автоматизация и поддержка.** Разрабатывал Telegram-ботов и асинхронные пайплайны на Python
> (aiogram, Telethon, MTProto). Администрировал Linux и Docker (systemd, логи, мониторинг),
> настраивал VPN-инфраструктуру, вёл резервное копирование и сетевую безопасность. Ранний опыт —
> Windows Server, Active Directory, Exchange, SQL, Hyper-V, сети, IP-телефония, ITSM.
>
> **Про GetCourse.** Отдельной записью в моём профиле этого инструмента нет — я работал с класса
> задач онлайн-школ (воронки, интеграции, автоматизация выдачи и сопровождения учеников) через
> Bitrix24 и внешние сервисы. Готов разобраться в GetCourse в вашем контуре; врать про опыт,
> которого нет, не буду.
>
> **Организационное.** Сейчас я живу в Таиланде и работаю удалённо (ICT, +7), русский — родной.
> В вакансии указано оформление по договору ГПХ — подскажите, как у вас решается оформление при
> работе из-за границы: это единственный вопрос, который мне нужно закрыть до отклика.
>
> Михаил Колесников
> mikhailthaiban@gmail.com · Telegram @brokeassh · +66 81 503 6090

**Что изменить в CV:**
1. **Поднять Bitrix24 в заголовок**: сейчас в `headline` стоит «AI Engineer | AI Automation
   Engineer | Python Developer», а для этой вакансии первое слово должно быть про Bitrix24 и
   поддержку. Рекомендация: отдельная версия резюме с заголовком «AI Automation Engineer |
   Bitrix24 | Техническая поддержка».
2. Про **GetCourse** — не добавлять в резюме, пока нет реального опыта. В письме это названо
   прямо.
3. Добавить строку про **телефонию и e-mail в Bitrix24**: в KB это есть (`duty`), в резюме — одной
   фразой среди прочих, а вакансия про онлайн-школу, где телефония и рассылки — ежедневная работа.
4. Указать **remote** и **ICT +7** явно в шапке: вакансия удалённая, и это снимает первый вопрос.

---

### 9.4 n8n Community #297943 — N8n Builder Wanted (AI Automation Agency)

**Short application (ответ в тред):**

> Hi Saif — I build n8n-based agent systems and I am happy to work on a per-project basis. Recent
> work: a Telegram bot and async worker pipeline (Python, ARQ, Redis, PostgreSQL, Docker) that
> analyses short-form video, extracts transcripts, fact-checks claims against web sources and
> publishes the result; and a job-search automation system that collects vacancies from five boards,
> scores them against a candidate profile and prepares tailored applications behind explicit
> approval gates. Both are n8n / OpenAI / webhook work. What I would want to agree before the first
> project: how you price (fixed per project or hourly) and how payment is sent. Fully remote from
> Thailand (ICT +7). Happy to jump on a call.

**Full application:**

> Hi Saif,
>
> Your model — you handle clients, scoping and sales, I build and deliver — is exactly the division
> of labour I want. I have been building automations and AI agents on my own systems for the last
> four months, and I would rather spend that time on delivery than on finding clients.
>
> **What I build.** Reels Analyzer: a Telegram bot plus asynchronous worker (Python, aiogram, ARQ,
> Redis, PostgreSQL, Docker) that takes video URLs, extracts transcripts and visual evidence,
> fact-checks each claim against external sources through Exa with QA controls, and publishes the
> result. Job Search AI: vacancy collection from habrcareer, RemoteOK, We Work Remotely and
> Himalayas; scoring against a structured candidate profile; tailored application preparation; and a
> gated submit path with a read-only verification step afterwards. WHUBN: a local-first iOS app in
> Swift 6 with a high-throughput SQLite persistence layer.
>
> **n8n specifically.** n8n workflows connecting databases, AI agents, Google Workspace APIs, CRM
> systems (Bitrix24), notification channels and external services — including robots, triggers,
> telephony and email flows around Bitrix24, and LLM integration into corporate chat and CRM
> workflows for incoming-request processing, lead classification and multilingual response
> generation. OpenAI, webhooks and REST APIs are daily tools.
>
> **Stack.** Python (asyncio, aiogram, Telethon), REST APIs and webhooks, n8n, Make, Docker, Linux,
> Bash, PowerShell, SQL and PostgreSQL, OpenAI / Gemini / Anthropic APIs, Whisper.
>
> **Practical.** Fully remote, based in Thailand (ICT, +7). English B1 — I write clearly and I ask
> when something is unclear. For a first collaboration I would keep it bounded: one workflow, clear
> input and output, test data first, and a paid milestone before the full build.
>
> Two questions so I can price honestly: what does a typical client project look like, and how do you
> prefer to price — fixed fee per project, or hourly?
>
> Mikhail Kolesnikov · mikhailthaiban@gmail.com · Telegram @brokeassh · github.com/mikheooo

**Что изменить в CV:**
1. Добавить в SUMMARY строку про **готовность работать за проект с фиксированным скоупом и
   milestone-оплатой** — это то, что OP описал как свою модель, и в резюме этого нет.
2. Показать **Make** наравне с n8n: в KB `make` = PROFESSIONAL, но в резюме он в одной строке с
   n8n без деталей. Для агентства, которое продаёт «n8n/Make», это плюс.
3. Вынести в отдельную строку **webhooks и REST API**: вакансия называет их прямо, а в резюме они
   внутри общего списка.
4. Ставку в резюме не указывать — её не назвал OP, и первым числом якорить хуже, чем спросить.

---

### 9.5 n8n Community #296828 — ongoing project-based n8n work

**Short application (ответ в тред):**

> Hi — I build n8n-based agent systems and I am comfortable working inside a sandbox boundary.
> Recent work: a Telegram bot and async worker pipeline (Python, ARQ, Redis, PostgreSQL, Docker)
> that analyses short-form video, extracts transcripts, fact-checks claims against web sources and
> publishes results; and a job-search automation system that collects vacancies from five boards,
> scores them against a candidate profile and prepares tailored applications behind explicit
> approval gates. Both hand off as documented, version-controlled artefacts. I keep credentials and
> client data out of the export and return the workflow with retry and error paths plus maintenance
> notes. Happy to start with a small scoped project so you can judge the handoff quality.

**Full application:**

> Hi,
>
> Your structure — build and fully test in a sandbox you provide, hand back version-controlled
> documented JSON, you keep production credentials — is the right boundary, and it is close to how I
> already work. On my own systems everything that touches the outside world sits behind an explicit
> gate, and the default mode requires a human decision rather than guessing.
>
> **Relevant work.** Reels Analyzer: a Telegram bot plus asynchronous worker (Python, aiogram, ARQ,
> Redis, PostgreSQL, Docker) that takes video URLs, extracts transcripts and visual evidence,
> fact-checks each claim against external sources through Exa with QA controls, and publishes the
> result. Job Search AI: vacancy collection from habrcareer, RemoteOK, We Work Remotely and
> Himalayas; scoring against a structured candidate profile; tailored application preparation; and a
> gated submit path with a read-only verification step afterwards. WHUBN: a local-first iOS app in
> Swift 6 with a high-throughput SQLite persistence layer and deterministic segmentation of location
> events into canonical stays.
>
> **n8n specifically.** I have built n8n workflows connecting databases, AI agents, Google Workspace
> APIs, CRM systems (Bitrix24), notification channels and external services — including robots,
> triggers, telephony and email flows around Bitrix24, and LLM integration into corporate chat and
> CRM workflows for incoming-request processing, lead classification and multilingual response
> generation.
>
> **Stack.** Python (asyncio, aiogram, Telethon), REST APIs and webhooks, n8n, Make, Docker, Linux,
> Bash, PowerShell, SQL and PostgreSQL, OpenAI / Gemini / Anthropic APIs, Whisper for transcription.
>
> **Practical.** Fully remote, based in Thailand (ICT, +7). English B1 — I write clearly and I am
> straightforward when I need something explained. Available to start with a small scoped project.
>
> Two questions so I can scope honestly: what does a typical first project look like, and how do you
> prefer to price — per workflow or per hour?
>
> Mikhail Kolesnikov · mikhailthaiban@gmail.com · Telegram @brokeassh

**Что изменить в CV:**
1. Добавить в SUMMARY явную фразу про **sandbox/version-controlled handoff** — это то, что OP
   выделил как главное, и в резюме этого нет.
2. Показать **Make** наравне с n8n: в KB `make` = PROFESSIONAL, но в резюме он в одной строке с n8n
   без деталей. В этой переписке Make — плюс.
3. Вынести в отдельную строку **RAG**: в резюме он есть в CORE SKILLS одной позицией среди прочих;
   для этого заказчика RAG — отдельная услуга.
4. Ставку не указывать: OP её не назвал, а первым числом заякорить хуже, чем спросить.

---

### 9.6 Запасной (№6 по деньгам) — n8n Community #284836, n8n Automation Specialist (E-Commerce / Shopify)

Пул APPLY NOW содержит 33 строки; это шестая из них. Черновик готов, чтобы не начинать с нуля,
если первые пять по каким-то причинам не откроются.

**Short application:**

> Hi — I have built exactly this class of pipeline, in the other direction. Reels Analyzer: a
> Telegram bot plus async worker (Python, ARQ, Redis, PostgreSQL, Docker) that takes short-form
> video, extracts transcripts, pulls visual evidence, fact-checks claims against web sources and
> publishes the result. The transcription and segmentation side is the part I know best — Whisper
> for transcription, then chunking and per-segment processing. I also build n8n workflows that
> connect Google Drive, LLM APIs (Claude, OpenAI, Gemini) and external services, and I am
> comfortable in n8n Code nodes. JavaScript is not my strongest language — Python is — and I would
> rather tell you now. Fully remote from Thailand (ICT +7), English B1.

**Full application:**

> Hi,
>
> You need an n8n workflow that takes competitor scripts and prompts, generates short-form video,
> transcribes it into 8-second increments, and outputs ready-to-post videos. I have built the
> analysis half of that chain in production, and the transcription and segmentation part is where I
> am strongest.
>
> **Reels Analyzer.** A Telegram bot accepts video URLs and queues them for processing — Redis plus
> ARQ for the async task queue, PostgreSQL as the job and task store, all in Docker. The pipeline
> extracts transcripts and visual evidence from each video, checks every claim against external
> sources through Exa with QA controls, and publishes the result to a channel. Building that meant
> working with video ingestion, transcription with Whisper, segment-level processing and
> deterministic chunking — the same mechanics your 8-second segmentation needs.
>
> **n8n and integrations.** n8n workflows connecting databases, AI agents, Google Workspace APIs,
> CRM systems and external services; LLM integration into chat and CRM workflows; a voice-message
> pipeline using Whisper for transcription followed by AI summarisation and storage into CRM deal
> records. Google Drive, Claude and OpenAI APIs are all familiar ground.
>
> **Honest gaps.** You mention JavaScript Code nodes. Python is my strong language; I can write
> JavaScript but I would not claim to be fast in it. I have not used Veo or Google Labs video
> generation — my video work has been on the analysis side, not generation.
>
> **Practical.** Fully remote, based in Thailand (ICT, +7). English B1. Available to start with a
> small scoped piece so you can judge the output before committing to the full build.
>
> Mikhail Kolesnikov · mikhailthaiban@gmail.com · Telegram @brokeassh · github.com/mikheooo

**Что изменить в CV:**
1. Добавить в Reels Analyzer **одну строку про сегментацию и транскрипцию** с числами (модель,
   длина отрезков) — сейчас в резюме сказано «extracts transcripts», и это не показывает глубину.
2. Про **видео-генерацию** не добавлять ничего: её не было.
3. Про **JavaScript** — не добавлять в навыки. В письме сказано прямо.
4. Вынести **Whisper** из общего списка в отдельный пункт проекта: вакансия про транскрипцию, и это
   должно быть видно сразу.

---

## 10. EXTERNAL SENDS = 0

Задача не отправила наружу ничего. Проверено по каждому каналу:

| канал | сколько | чем подтверждено |
|---|---:|---|
| откликов на вакансии | **0** | не вызывался `submit`, `submit-next`, `application submit`; `application_submissions` до и после — 21 строка |
| сообщений работодателям | **0** | `hh message send` не вызывался; `hh_message_events` — 38 строк, без новых |
| нажатий «Откликнуться» | **0** | headless-сессия запускалась только в режиме чтения (`hh_readonly_operation`, `submitted = 0`, `messages_sent = 0` в отчёте операции) |
| писем | **0** | `email` / `gmail` не вызывались |
| контактов с рекрутерами | **0** | — |
| изменений внешнего профиля | **0** | — |
| публикации резюме | **0** | — |
| исходящего в Telegram | **0** | `telegram test` не вызывался; `telegram_delivery_records` — 58 строк, последняя от 2026-09-29 |

Отчёт операции HH, которую я запускал, сам это фиксирует:
`"read_only": true, "mouse_events_dispatched": 0, "submitted": 0, "messages_sent": 0`.

Все сетевые обращения этой задачи — **HTTP GET на публичные страницы вакансий и на JSON-API
форума**, плюс один сеанс чтения на hh.ru. Ни одного POST, ни одной формы, ни одного сообщения.

---

## 11. Deferred technical debt

Записано, **не исправлено** — потому что не блокирует TOP-5.

1. **`vacancies_json`: 65 из 83 строк несут выдуманную зарплату 4000–5000 USD.** Настоящая причина
   — импортёр сида. Пока не исправлено, любая аналитика по зарплате для этого источника врёт.
   Правильное лечение: либо парсить текст сида при импорте, либо писать `NULL`, а не подстановку.
2. **Сид содержит одну вакансию дважды** (`298577` и `299818`) и расходится сам с собой по
   зарплате: у одной копии `salary` = «$1,500/мес», у другой — «не указана». Дедупликация теперь
   ловит это по телу поста, но **сам сид остаётся грязным**; лечение — на стороне импортёра.
3. **Компонент `barrier` читает обрезок описания, а не полный текст поста.** Поэтому Loom-видео в
   №2 он не увидел и поставил 8/10. Нужно либо хранить полный текст, либо брать его с источника.
4. **`application_queue` нечем проверить на здоровой нагрузке.** Гейт, который всегда молчит,
   ничего не доказывает. Нужен тест, который подаёт заведомо полосу A и проверяет, что строка
   доходит до очереди. Сейчас такого теста нет.
5. **Верификация не детерминирована при сетевом сбое.** Наблюдения пишутся в отдельные файлы
   (`--out`), но всё ещё перезаписываются по прогону. Лечение — **append-файл**: каждое наблюдение
   дописывается, ничего не затирается, вердикт считается по всей истории.
6. **Правило слияния вердиктов живёт в `rank.py`, а не в пакете.** Это измерительный инструмент,
   но логика («`NOT VERIFIED` не вердикт, а его отсутствие») — продуктовая по смыслу, и её место в
   `ai_assistant/`, с тестами. Сейчас она не покрыта тестами вообще.
7. **`vacancy_eligibility` содержит 4 строки-сироты** (нет соответствующей вакансии), столько же в
   `application_tracking`. Мусор, на решения не влияет.
8. **`company` пуст у 452 вакансий (18%).** Для дедупликации и для карточек это ограничение.
9. **Корпус не обновлялся с 2026-09-29.** Дневной прогон сломан (T7.29); доставка не объявлена
   восстановленной. TOP-5 собран на живых источниках, а не на корпусе, — именно поэтому.
10. **Открытые пункты T7.29:** три живые проверки (догон пропущенного окна, реальный HH `CHECKED`,
    реальный тап `fb:`); `sync` Hermes привязан к маркеру, поэтому правка тела хука не
    распространится; дубль крона `b1edb5ae094c`; хук канбана из стэша
    `3aafcc80ffb4a229f0f2ba360a6dc574d8a004d2`.
11. **Посторонний поток в проекте:** `scratch_p5_collect.py` (сбор гигов с Freelancer.com) работал
    одновременно со мной. Вакансий не касается, но это второй писатель в `state.db`.

---

## 12. Изменения в коде

**Продуктовый код не менялся.** Ни `ai_assistant/`, ни `cli.py`, ни тесты. `cli.py` не тронут.

Всё написанное — **измерительные инструменты** в `tools/.t9/`, вне пакета:

| файл | зачем |
|---|---|
| `snapshot.py`, `snapshot_cohorts.py` | read-only снимок |
| `gate_walk.py` | проход по воротам очереди |
| `funnel.py` | Funnel Report |
| `liveness.py`, `verify.py`, `diag.py` | проверка актуальности |
| `live_boards.py` | живое чтение WWR / RemoteOK |
| `n8n_activity.py`, `n8n_threads.py` | активность тредов через Discourse API |
| `money_score.py`, `rank.py` | Practical Money Score и ранжирование |
| `negative_controls.py` | негативные контроли |

Внутри задачи изменены пять вещей, и каждая — прямое следствие измеренного дефекта:

| изменение | дефект, который оно закрывает |
|---|---|
| `verify.py`: маркеры ищутся в **видимом тексте**, а не в сыром HTML | 26 живых тредов n8n читались как «captcha-стена» (8.3в) |
| `verify.py`: правило **потери сегмента URL** → `GONE` | 6 удалённых вакансий читались как `LIVE` (8.3г) |
| `verify.py`: опция `--out` | повторный прогон затирал предыдущее наблюдение (8.3б) |
| `rank.py`: правило слияния «`NOT VERIFIED` — не вердикт, а его отсутствие» | пессимистичный merge выбрасывал хорошее измерение (8.3, пятый пункт) |
| `rank.py`: дедупликация по слогу URL **и по телу первого поста** | одна вакансия попадала в таблицу дважды, один раз — в TOP-5 (раздел 4) |
| `money_score.py`: разбор зарплаты (запятая/пробел/`k`, период из текста, нижняя граница диапазона) | оффер на 1.5× порога получал 0/10 вместо 4/10 (раздел 5) |
| `n8n_threads.py`: отпечаток тела первого поста | дубликат-перепост был невидим данным (раздел 4) |

Проверка, что изменения не сломали продукт: **продуктовый код не тронут**, поэтому тесты проекта
не затронуты. Полный прогон `tests/` в этой задаче не запускался — вердикт T9 основан на
измерениях конвейера и живых источников, а не на тестах, и это прямо записано в разделе 16.

---

## 13. QA

### 13.1 Vacancy QA

| проверка | результат |
|---|---|
| обязательные поля | `title`, `job_url`, `description` заполнены у всех 2529 |
| дубликаты | 320 EXACT-алиасов → 10 схлопываний; **плюс 1 перепост-дубликат в n8n-тредах**, схлопнут по телу поста |
| сироты | 4 строки eligibility и 4 строки tracking без вакансии |
| архивные | 2 подтверждённых архива на habr (BGStaff, TRM) — исключены |
| мёртвые URL | **22 `GONE`** (404/410, «доска отдала главную», редирект на чужую страницу) + 2 `CLOSED` |
| выдуманные данные | 65 подстановочных зарплат в сиде — найдены и не использованы |

### 13.2 Candidate QA

| проверка | результат |
|---|---|
| источник истины | `candidate_kb.json`, kb_version 28, `source_sha256 = 7ae200cd…` |
| подтверждено пользователем | 5 записей, включая «с мая 2026 — только свои проекты, без оплаты» |
| навыки в черновиках | все названные навыки есть в KB (`skill_confidence`) |
| навыки **не** названные | Go, TypeScript, GraphQL, GCP, JavaScript-скорость, GetCourse, векторные БД, Veo, Loom — **явно помечены как пробел в каждом черновике** |
| резюме-мастер | **не изменялось** |

### 13.3 Pipeline QA — `1000 pending → 0 application_queue`

`production-health` сообщал «Pending Undigested Vacancies: 1000». Замер: **2529** вакансий, все
`state = NEW`, `processed_vacancies` пуста (0 строк) — то есть «не переварено» всё, а 1000 — это
лимит параметра, а не факт. Дальше:

```
2529 вакансий → 13 READY_TO_APPLY → 0 eligible+band A → 0 в application_queue
```

Причина нуля — раздел 3. Проверено тремя независимыми замерами (13 строк, весь корпус, свежий
HH-поиск): полоса A не встречается.

### 13.4 Ranking QA — вручную, TOP-5 против нижних строк

| пара | почему верхняя выше |
|---|---|
| №1 Sticker Mule (76.3) vs `weworkremotely:ace-ventures-…` (55.2, ранк 73) | у Ace Ventures match 88 — выше, чем у Sticker Mule (67). Но это личный ассистент основателя: `skills` 2.2/10, `barrier` 4/10, `time_to_money` 5/10. Роль, а не балл, опускает строку на 73-е место |
| №3 Прорыв (74.5) vs `hh:137913751` Middle Backend, до 280 000 ₽ | у второй зарплата выше (≈$3 080 против ≈$1 100), но `work_formats` = ON_SITE и полоса C. `remote` = 0/10, `location` = 2/10 — этого хватает, чтобы она не вошла в TOP-5 |
| №2 #298577 (76.1) vs №4 #297943 (73.3) | у обоих match 87 и одинаковые `skills`/`remote`/`location`. Разница — только зарплата: у #298577 она **названа** ($1 500/мес → 4/10), у #297943 не названа вовсе (2/10). Это ровно 2.8 балла, и они решают |
| №5 #296828 (71.6) vs №6 #284836 (71.5) | разрыв 0.1: у #296828 match 78 против 77, у #284836 `skills` тоже 10/10. Порядок здесь почти произволен — обе строки в пуле, и №6 оставлен запасным с готовым черновиком (9.6) |
| любая TOP-5 vs `himalayas` Spotter Labs (74.5, ранк 3) | по баллам выше Прорыва, но `liveness` = `NOT VERIFIED`: himalayas отдаёт 403 на любой запрос. Строка остаётся в TOP-20 как лид, но в APPLY NOW не попадает — **проверяемость важнее балла** |

---

## 14. Негативные контроли

Скрипт `tools/.t9/negative_controls.py` (сырой вывод — `tools/.t9/negative_controls.log`). Каждый
контроль берёт строку **из самого шортлиста** — контроль, который проходит только потому, что
строка не попала в пул, ничего не доказывает.

| № | что проверяется | результат |
|---|---|---|
| N1 | вакансия чужой роли не должна попасть наверх | **прошёл**: Ace Ventures (match 88) — ранк **73** из 90 |
| N2 | архивная вакансия не должна быть APPLY NOW | **прошёл**: BGStaff — `CLOSED` («вакансия в архиве»), **исключена из таблицы**. *Первый прогон контроль провалил — см. 8.3б* |
| N3 | вакансия без проверяемого URL не считается проверенной | **прошёл**: все 9 HH-строк — `NOT VERIFIED` (VPN-заглушка) |
| N4 | тестовая фикстура не должна стать возможностью | **прошёл**: `himalayas:dryrun-test-1` («ACME — Dry Run Job», match 82) — HTTP 404, **исключена** |
| N5 | закрытый тред, который не вакансия, не должен выжить | **прошёл**: `vacancies_json:72` (вопрос про PostgreSQL failover, ingested как «вакансия», match 79) — `closed = true`, **исключён** |
| N6 | мёртвый URL отбрасывается даже при высокой пригодности | **прошёл**: Evaboot (реальный стек-фит, $70–120K) — WWR редиректит на главную, **исключён** |
| N7 | выдуманная зарплата не должна покупать баллы | **прошёл**: у сид-строк `salary` = 2/10, улика — «stored 4000-5000 USD is a placeholder». Если бы читалась БД, все 65 строк получили бы 7–10/10 |
| N8 | одного семантического совпадения мало | **прошёл**: самые высокие match в таблице (95, 88, 88, 88, 88) стоят на ранках 30, 16, 18, 46, 73 — все ниже TOP-5, потому что `freshness`, `salary` и `response` тянут вниз |
| N9 | удалённая вакансия не должна читаться как `LIVE` | **прошёл**: `vacancies_json:44` (LinkedIn редиректит на страницу поиска) — `GONE`, из таблицы исключена. *На прогоне без правила потери сегмента URL строка читалась как `LIVE` — см. 8.3г* |
| N10 | живой тред не должен читаться как заблокированный | **прошёл**: `vacancies_json:20` — `LIVE`, `<h1>N8N AI Automation Developer (Remote)>`. *На прогоне с маркерами по сырому HTML строка была `NOT VERIFIED` — см. 8.3в*. Позже строка исключена из таблицы как дубликат-перепост (раздел 4) — это ожидаемое следствие, а не провал контроля |

**Два контроля сначала провалились, и это их ценность.** N2 показал архивную вакансию в таблице
(таймаут TLS превратил `CLOSED` в `NOT VERIFIED`, а `NOT VERIFIED` таблица пропускала), а N9 и N10
описывают два класса ошибок, которые прошли **все** предыдущие прогоны незамеченными. Все три
исправлены, и контроли теперь воспроизводят именно те строки, на которых ошибки были видны.

---

## 15. Приёмочные критерии A–J

| | критерий | статус | доказательство |
|---|---|---|---|
| A | причина `application_queue = 0` доказана | **да** | раздел 3, три независимых замера |
| B | воронка измерена | **да** | раздел 4, 9 стадий с причинами |
| C | ≥ 5 реальных возможностей **или** отчёт о блокере | **да, 5** | раздел 7, пул APPLY NOW — 33 строки |
| D | URL или `NOT VERIFIED` | **да** | раздел 8; 27 строк `NOT VERIFIED`, ни одной выдуманной ссылки |
| E | прозрачный Practical Money Score | **да** | раздел 5, 10 компонент, сумма весов 100, улика у каждой |
| F | TOP-5 с объяснением | **да** | раздел 7, отбор механический |
| G | черновики на каждую TOP-5 | **да** | раздел 9, 5 × (short + full + CV) + запасной |
| H | никаких выдумок | **да** | раздел 13.2; пробелы названы в каждом письме; зарплата берётся из текста источника |
| I | `EXTERNAL SENDS = 0` | **да** | раздел 10 |
| J | независимый QA | **да** | раздел 13, четыре части + 10 негативных контролей |

---

## 16. Главный тест и вердикт

**Может ли Миша открыть TOP-5 после этого отчёта и начать отправлять реальные отклики на работу,
которая может принести деньги?**

**Да.** Все пять строк проверены сегодня: три — прямой страницей треда с активностью из API
форума, одна — полным чтением авторизованной сессией, одна — страницей доски. У каждой есть URL,
который открывается, и черновик, который можно отправить, поправив под себя. Ни одна не требует
правок в коде проекта.

**Что при этом честно не так:**

- **Очередь приложения пуста и останется пустой**, пока хоть одна живая вакансия не получит полосу
  A. Это не баг — это работающий предохранитель. Но проверки, что он пропускает хоть что-то, в
  проекте нет.
- **Четыре из пяти — это n8n Community, а не доска.** Подтверждено, что тред живой; **не**
  подтверждено, что роль из первого поста ещё открыта. Это честный `ROLE STATUS: NOT VERIFIED`.
- **№2 требует Loom-видео 5–7 минут.** Это не «плюс к заявке», а условие рассмотрения. Без видео
  отклик, скорее всего, не прочитают — план записи есть в разделе 9.2.
- **У №3 полоса B**: работодатель нигде не говорит про работу из Таиланда. Форма ГПХ + жизнь за
  границей — вопрос, который надо задать в первом же сообщении, а не выяснять после оффера.
- **Английский B1** — реальное ограничение в №1, №2, №4 и №5. Оно названо в каждом письме, а не
  спрятано.
- **Корпус не обновлялся с 2026-09-29.** TOP-5 держится на живых источниках; если дневной прогон не
  починить, через две недели он устареет так же, как устарел сид.
- **Тесты проекта в этой задаче не прогонялись.** Продуктовый код не менялся, поэтому регрессии
  неоткуда взяться, но это утверждение, а не измерение — и оно здесь названо, а не умолчано.

**Минимальный следующий шаг для разблокировки конвейера** (не для TOP-5 — он уже разблокирован):
взять одну вакансию, у которой работодатель **своими словами** разрешает работу откуда угодно, и
прогнать её до `application_queue`, доказав, что предохранитель умеет открываться. Без этого
«полоса A» остаётся правилом, которое никто не видел работающим.

---

```
REAL OPPORTUNITIES FOUND = 20      (таблица TOP-20; всего в таблице 90 строк)
APPLY NOW = 5                      (TOP-5, раздел 7; пул APPLY NOW — 33 строки)
VERIFIED LIVE = 55                 (строк с вердиктом LIVE после слияния наблюдений)
APPLICATION DRAFTS READY = 6       (5 на TOP-5 + 1 запасной, раздел 9.6)
EXTERNAL SENDS = 0
```

# T9 VERDICT: READY TO APPLY

**Пять возможностей, проверенных сегодня, с готовыми черновиками. Открывай TOP-5 и откликайся.**
