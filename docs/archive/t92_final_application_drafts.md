# T9.2 — ФИНАЛЬНЫЕ ПЯТЬ ЧЕРНОВИКОВ ОТПРАВКИ

**Дата:** 2026-10-03 · **К отчёту** `docs/t92_final_sendable_five_report.md`
**Статус:** **черновики. Ничего не отправлено. `EXTERNAL SENDS = 0`.**
**Проверено:** 2026-10-03, `verify_final.py` + честный статус ролей:
- **SEND NOW = 3** (Sticker Mule, Прорыв, Лига)
- **STRETCH SEND = 1** (Иви · `MANDATORY_ETL_3Y = NOT PROVEN`, `VERDICT = STRETCH APPLY`)
- **SPECULATIVE SEND = 1** (n8n #311286 · `PAGE_LIVE`, `THREAD_ACTIVE`, `ROLE_STATUS = NOT VERIFIED`, `ACTION = SPECULATIVE APPLY`)
- **BLOCKED = 0**
- **TOTAL READY FOR USER DECISION = 5**
**Аудит формулировок:** `tools/.t92/draft_qa.py` → `5/5 READY`, 31 claim, **0 UNSUPPORTED**

---

## Контакты кандидата (едины для всех отправок)

- **Имя:** Mikhail Kolesnikov / Михаил Колесников
- **Email:** `mikhailthaiban@gmail.com`
- **Telegram:** `@brokeassh`
- **Локация:** Таиланд, Паттайя · **UTC+7 (ICT)**
- **Языки:** русский — native; английский — **B1 (Intermediate)**, честно и без завышения
- **Профиль:** AI Automation Engineer · 15+ лет в IT

---

## Правила, по которым написаны все пять

1. **Ни одного неподдержанного утверждения.** Каждый claim прогнан через аудит: `SUPPORTED_BY_KB` или `NOT_A_CLAIM`. Формулировка **не усиливается** только потому, что лучше продаёт.
2. **Опыт не округляется вверх.** «15+ лет в IT» — из KB (`experience_statement`). Стаж в роли AI Automation Engineer не превращается в «четыре года».
3. **Ограничения называются прямо.** GetCourse, Airflow, ClickHouse, Kubernetes, Kafka, Vault, Debezium, Retell/Vapi/Twilio, Go/TypeScript/GCP — где опыта нет, это сказано словами, а не умолчано.
4. **Специальные инструкции вакансии исполнены** (§10) — см. таблицу ниже.
5. **Коротко.** Цель — отправить сегодня, а не написать эссе. Лимит 220 слов / 1600 знаков.

---

# №1 — Sticker Mule · «AI agent engineer»

**Куда:** https://remoteok.com/remote-jobs/remote-ai-agent-engineer-sticker-mule-1137399
**Язык:** английский. **Оплата по странице:** $150,000–$250,000/год + $20,000 signing bonus
**Формат:** полностью удалённо
**Специнструкция:** **`Please mention the word HUMOUR when applying`** — исполнена в первой строке
**Аудит:** 153 слова, 1015 знаков, `COMPENSATION_OK`, `READY`

> Hi — applying for the AI agent engineer role.
>
> HUMOUR — as requested.
>
> I have 15+ years of IT experience, and I have spent the last few years building AI automation: n8n workflows, Python services, and LLM-powered systems in production.
>
> At Sayama Travel I integrated OpenAI and Google Gemini into corporate chats and CRM workflows — incoming-request processing, lead classification and multilingual response generation — and built a voice-message pipeline using Whisper for transcription followed by AI summarization into CRM records.
>
> I build agents that run unattended. My own job-search system collects vacancies, scores them against a profile and submits applications behind explicit safety gates, defaulting to human review. That is agent work with real consequences, so retries, validation and logging matter to me.
>
> My working stack is Python, PostgreSQL, Docker and Linux. I have not shipped Go, TypeScript or GCP.
>
> I am in Thailand (UTC+7) and work remotely.
>
> Mikhail Kolesnikov — mikhailthaiban@gmail.com

**Claim-аудит (7):** 6 × `SUPPORTED_BY_KB`, 1 × `NOT_A_CLAIM` (отказ от Go/TypeScript/GCP), 0 × `UNSUPPORTED`.

**Что убрано против T9.1** (см. §5 брифа):
- ~~«the last four years I've been shipping LLM-powered systems into production, not prototypes»~~ — стаж округлён вверх; в KB роль AI Automation Engineer с 2022-11 по 2026-04.
- ~~«Google Workspace»~~ в перечне систем, к которым подключены пайплайны — в KB нет.
- ~~«I'm comfortable with a 4-day-week, low-meeting culture»~~ — в KB нет.

---

# №2 — Прорыв · «Технический специалист онлайн-школы / AI Automation Engineer»

**Куда:** https://hh.ru/vacancy/137336564
**Язык:** русский. **Оплата по странице:** от 100 000 ₽/мес до вычета налогов
**Формат:** удалённо, полная занятость · **Флор по опыту:** 1+ год — перекрыт с запасом
**Аудит:** 128 слов, 996 знаков, `COMPENSATION_OK`, `READY`

> Здравствуйте!
>
> Откликаюсь на вакансию технического специалиста / AI Automation Engineer.
>
> Что у меня есть по вашим задачам:
> — Bitrix24: администрировал self-hosted портал, настраивал интеграции, бизнес-процессы, роботов и триггеры; делал n8n-воркфлоу, связывающие Bitrix24, Google Sheets, Telegram-каналы и внешние базы данных.
> — API и webhooks: интеграции OpenAI и Google Gemini в корпоративные чаты и CRM — обработка входящих обращений, классификация лидов, ответы на разных языках. Голосовые сообщения расшифровывал через Whisper и складывал результат в сделки Bitrix24.
> — Инфраструктура: Linux и Docker (systemd, логи, мониторинг), развёртывание на VPS, бэкапы, VPN, доступы.
> — Стек: Python, REST APIs, webhooks, PostgreSQL, MS SQL, Bash/PowerShell.
>
> С GetCourse я не работал — говорю прямо. Опыт с Bitrix24 и интеграциями переносится, но продукт придётся изучить.
>
> Готов оперативно подключаться к критическим ситуациям. Удалённо, Таиланд (UTC+7).
>
> Михаил Колесников, mikhailthaiban@gmail.com

**Claim-аудит (7):** 6 × `SUPPORTED_BY_KB`, 1 × `NOT_A_CLAIM` (GetCourse), 0 × `UNSUPPORTED`.

---

# №3 — Онлайн-кинотеатр Иви · «Разработчик ETL / Data Engineer»

**Куда:** https://career.habr.com/vacancies/1000167966
**Язык:** русский. **Оплата по странице:** «Зарплата не указана» ⇒ `COMPENSATION_UNKNOWN`, не выдумывается
**Формат:** «Можно удалённо»
**Статус проверки:**
- `MANDATORY_ETL_3Y = NOT PROVEN` (в Candidate KB есть доказательства построения асинхронных ETL-пайплайнов на Python: asyncio, Telethon/MTProto, external APIs, ARQ + Redis, PostgreSQL, но нет подтверждения именно 3+ лет разработки ETL-процедур; опыт Python/automation в ETL-стаж не придумывается)
- `VERDICT = STRETCH APPLY`
- **Статус:** **`STRETCH SEND`**
**Аудит:** 92 слова, 628 знаков, `READY`

> Здравствуйте!
>
> Откликаюсь на вакансию «Разработчик ETL / Data Engineer».
>
> Что могу принести:
> — Python и ETL: строю асинхронные пайплайны на asyncio, Telethon/MTProto и внешних API — сбор, нормализация и загрузка данных. В проекте reels_analyzer это ARQ + Redis, PostgreSQL (asyncpg/SQLAlchemy) и Docker.
> — SQL и реляционные СУБД: PostgreSQL и MS SQL.
> — Git.
>
> Чего у меня нет: Airflow и ClickHouse в продакшене я не использовал. Мой ETL — это Python и собственные пайплайны, а не Airflow. Готов закрыть это быстро, но не хочу создавать ложного впечатления.
>
> Удалённо, Таиланд (UTC+7).
>
> Михаил Колесников, mikhailthaiban@gmail.com

**Claim-аудит (5):** 4 × `SUPPORTED_BY_KB`, 1 × `NOT_A_CLAIM` (Airflow/ClickHouse), 0 × `UNSUPPORTED`.

---

# №4 — Лига Цифровой Экономики · «Системный администратор Linux (Fintech / Инфраструктура)»

**Куда:** https://hh.ru/vacancy/137784849
**Язык:** русский. **Оплата по странице:** «Уровень дохода не указан» ⇒ `COMPENSATION_UNKNOWN`
**Формат:** «удалённо или гибрид» — удалённая работа допускается (см. контроль №10)
**Флор по опыту:** 5 лет в системном администрировании — закрывается 2009–2018
**Аудит:** 117 слов, 918 знаков, `READY`

> Здравствуйте!
>
> Откликаюсь на вакансию системного администратора Linux (Fintech / Инфраструктура).
>
> По вашим требованиям:
> — Системное администрирование: с 2009 по 2018 год работал с Windows Server и Linux — Active Directory, Exchange, SQL, Hyper-V, сети, IP-телефония, мониторинг, ITSM. С 2021 по 2022 год — поддержка и администрирование Bitrix24.
> — Linux и Docker: администрировал Linux и Docker-инфраструктуру (systemd, логи, мониторинг), настраивал VPN, бэкапы и сетевую безопасность.
> — Автоматизация: n8n-воркфлоу и автоматизация процессов, Python и Bash/PowerShell, SQL (PostgreSQL, MS SQL).
> — Технический английский — intermediate.
>
> Чего у меня нет: Kubernetes, Kafka, HashiCorp Vault, Debezium и HTTP-серверы (Nginx, Tomcat) в продакшене я не администрировал. Не хочу выдавать желаемое за действительное.
>
> Удалённая работа из другого города подходит. Таиланд (UTC+7).
>
> Михаил Колесников, mikhailthaiban@gmail.com

**Claim-аудит (6):** 5 × `SUPPORTED_BY_KB`, 1 × `NOT_A_CLAIM` (Kubernetes/Kafka/Vault/Debezium/Nginx), 0 × `UNSUPPORTED`.

---

# №5 — n8n community · «Technical AI Automation Specialist — Long-Term Team Collaboration»

**Куда:** https://community.n8n.io/t/hiring-technical-ai-automation-specialist-long-term-team-collaboration/311286
**Язык:** английский. **Оплата:** не указана ⇒ `COMPENSATION_UNKNOWN`; в треде прямо сказано
*«There is no guaranteed fixed monthly workload at this stage»* и *«We are NOT asking anyone to work for free»*.
**Формат:** удалённо, long-term
**Статус проверки:**
- `PAGE_LIVE`
- `THREAD_ACTIVE` (`closed=false`, тред не закрыт)
- `ROLE_STATUS = NOT VERIFIED` (`thread active` и `closed=false` **не являются** доказательством `ROLE_OPEN_VERIFIED`; автор объявления Ahmed_Refaei не проявлял подтверждённой активности найма после исходного поста — 172 отклика без ответов автора)
- `ACTION = SPECULATIVE APPLY`
- **Статус:** **`SPECULATIVE SEND`**
**Специнструкция:** у поста есть **чеклист из 12 пунктов** — исполнен ниже по пунктам (требование ссылок закрыто реальными ссылками на GitHub и Portfolio из профиля кандидата)
**Аудит:** 219 слов, 1564 знака, `READY`

> Hi — replying to your Technical AI Automation Specialist post.
>
> Mikhail Kolesnikov, AI automation engineer, Thailand (UTC+7), 15+ years of IT experience.
>
> Skills. n8n; Python (aiogram, asyncio, Telethon); REST APIs and webhooks; OpenAI, Google Gemini and Anthropic APIs; PostgreSQL and MS SQL; Docker and Linux; prompt engineering and RAG.
>
> n8n. I build n8n workflows connecting CRM systems, Google Sheets, Telegram channels and external databases.
>
> Retell / Vapi / Twilio. I have not used these. My voice work is Whisper transcription plus AI summarization into CRM records — same problem, different stack.
>
> Projects I personally built. (1) reels_analyzer — a Telegram bot plus asynchronous worker (ARQ + Redis, PostgreSQL, Docker) that analyses short video, checks claims against web sources and returns evidence-backed results. (2) job-search — a Python system that collects vacancies, scores them against a profile and submits applications behind explicit safety gates, defaulting to human review. Architecture, integrations, the async pipeline and deployment were mine end to end.
>
> Compensation. From $1,000/month. Model. Remote contract. Availability. Full-time. Timezone. UTC+7 — I can hold a fixed overlap window.
>
> AI receptionist. For your example flow (call, intent, qualify, book, CRM, notify, handoff) I would scope a first version at roughly 2–3 weeks, with CRM and calendar integrations as the schedule risk.
>
> Demos. GitHub: https://github.com/mikheooo | Portfolio: https://mikheooo.github.io/portfolio/
>
> Mikhail Kolesnikov — mikhailthaiban@gmail.com

**Claim-аудит (6):** 5 × `SUPPORTED_BY_KB`, 1 × `NOT_A_CLAIM` (Retell/Vapi/Twilio), 0 × `UNSUPPORTED`.

**Чеклист вакансии ↔ ответ:**

| Что просит пост | Где в драфте |
|---|---|
| A short introduction | строка 3 |
| Your strongest technical skills | «Skills.» |
| Your experience with n8n | «n8n.» |
| Your experience with Retell / Vapi / Twilio | «Retell / Vapi / Twilio.» — прямо сказано, что опыта нет |
| 2–3 AI automation projects you personally built | «Projects I personally built.» — два, с оговоркой «personally built» |
| Links to demos / Loom / GitHub / screenshots | «Demos. GitHub: https://github.com/mikheooo \| Portfolio: https://mikheooo.github.io/portfolio/» — реальные ссылки из Candidate KB/profile |
| What you personally handled in those projects | «Architecture, integrations, the async pipeline and deployment were mine end to end.» |
| Your expected compensation / rate | «From $1,000/month» (это **его** цифра из KB `preference_condition`, не выдумка о вакансии) |
| Your preferred working model | «Remote contract.» |
| Your availability | «Full-time.» |
| Your timezone | «UTC+7» |
| Roughly how long to build a production-ready AI receptionist | «roughly 2–3 weeks» |

**Открытая слабость этой строки (честно):** статус вакансии `ROLE_STATUS = NOT VERIFIED`. Оплата в вакансии не названа; пост прямо предупреждает, что гарантированного объёма работы на этом этапе нет; тред собрал **172 ответа**, а автор поста не ответил ни одному соискателю. Это спекулятивный отклик (`SPECULATIVE APPLY`), который не выдаётся за подтверждённо открытую позицию. См. §9 отчёта.

---

## Сводка спец-инструкций (§10)

| № | Вакансия | Инструкция | Исполнена |
|---|---|---|---|
| 1 | Sticker Mule | `Please mention the word HUMOUR when applying` | ✅ `HUMOUR` — отдельной строкой в начале |
| 2 | Прорыв | нет | ✅ |
| 3 | Иви | нет | ✅ |
| 4 | Лига | нет | ✅ |
| 5 | n8n community | чеклист из 12 пунктов | ✅ по пунктам, см. таблицу выше |

**Негативный контроль:** `tools/.t92/negative_controls.py` №7 — тот же текст без `HUMOUR` ⇒ `NOT READY`. Контроль проходит.

---

## Чего в этих драфтах НЕТ

Ни одного из следующих утверждений — все они были в черновиках T9.1 и сняты:

- «the last four years I've been shipping LLM-powered systems into production»
- «managed a team of twelve engineers»
- «grew revenue by 40 percent»
- «worked with international clients for five years»
- «10 years of n8n experience» — в KB у n8n **нет** длительности вообще
- «Google Workspace» как система, к которой подключены пайплайны
- «I'm comfortable with a 4-day-week, low-meeting culture»

Правило §6: **`UNSUPPORTED` не отправляется.** Нельзя усиливать формулировку просто потому, что она лучше продаёт.
