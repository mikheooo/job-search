# T7.1 — реальная удалённая вакансия и резюме, понятное работодателю

Дата отчёта: 2026-09-24 (UTC). Задача: подготовить **черновик** резюме под одну реальную
удалённую ИТ-вакансию, используя уже существующие KB, master-резюме, Job-to-Candidate Brief и
результаты T7. Ничего не отправлено, T8 не начат.

Всё, что описано ниже, лежит в
`artifacts/applications/weworkremotely-ignition-inc-mac-msp-help-desk-guru-work-from-home/`.
Инструмент — `tools/t7_preview_adapt.py` (тот же, что делал T7 preview; второй системы
адаптации не заводилось, в него добавлены объявленные редакционные операции).

---

## 0. Коротко

| что | результат |
| --- | --- |
| выбранная вакансия | Ignition, Inc. — Mac MSP Help Desk Guru (work from home), WeWorkRemotely |
| формат | удалённый, заявлен в тексте прямо; подтверждён классификатором проекта |
| работа из Таиланда | **запрета нет, подтверждения тоже нет** — см. §1.4 |
| зарплата | $50–65k/год названы **только** для кандидатов в США; для остальных «по местному рынку» ⇒ условие «от $1000/мес» **не подтверждено** |
| главное ограничение вакансии | рабочие часы Eastern или Pacific (для Таиланда это ночная смена) |
| резюме | переписано редакционно: 68 строк master → 44 строки черновика, 26 исключено, 3 строки переписаны, 3 объединены, 2 добавлены |
| хронология | Speedcast / Germsat вынесен отдельной записью *March 2018 – March 2021*; сноска удалена |
| внутренние пометки в публичном тексте | 0 (в T7 preview их было 2) |
| профильные тесты | `tests/test_candidate_job_brief.py` — 135 passed |
| полная регрессия | **вердикта нет** (`UNKNOWN`): 3 попытки, `--junitxml` не оставлен; причина измерена — удаление файла ~70 мс × 88 DDL на свежую базу, см. §5.3 |
| известное падение | `tests/test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches` — `assert 24 <= 18`, не скрыто и порог не поднят |

---

## 1. Выбор вакансии

### 1.1 Что искали

Направления: IT Support / Application Support; System Administrator; Bitrix24 / CRM Support или
Administrator. Условия: полное описание обязанностей и требований; подтверждённый удалённый
формат; нет установленного запрета на работу из Таиланда; подходящий язык работы и основные
обязанности; зарплата от $1000 в месяц, если она указана в сопоставимых единицах и периоде.

Прежняя московская вакансия AGIMA (`habrcareer:1000168221`) в качестве основного результата не
использована: удалённый формат для неё не подтверждён, описание в базе — 272 символа.

### 1.2 Что в базе есть и чего в ней нет

`state.db`: 2218 вакансий. Источники: habrcareer 679, himalayas 641, weworkremotely 413,
remoteok 387, vacancies_json 83, hh 14, x 1.

Два измерения, которые определяют всё остальное:

* **Bitrix24/CRM в удалённом формате в базе нет.** Слово Bitrix/Битрикс встречается ровно в 4
  вакансиях — все habrcareer, все Москва, длины описаний 272, 259, 223 и 185 символов. Полного
  описания обязанностей и требований там нет, удалённый формат не заявлен.
* **Русскоязычной удалённой ИТ-вакансии в базе нет.** Описаний на кириллице длиной ≥ 800
  символов всего 7, и все — разработка (Python/AI/QA) из `hh`.

Поэтому выбранная вакансия — англоязычная, и перевод резюме не требовался: язык вакансии
английский, язык черновика английский.

### 1.3 Рассмотренные вакансии и причины отказа

| вакансия | почему не подошла |
| --- | --- |
| **Platform.sh / Upsun — IT Systems Engineer** (weworkremotely, «Anywhere in the World») | прямо ограничена географией: «we are currently focused on hiring for this role in Canada, France, Germany, Spain, and the United Kingdom» + «we welcome applications from all qualified candidates who are legally authorized to work in these countries». **Установленный запрет** на работу из Таиланда |
| **Porkbun — Live Technical Support Representative** (weworkremotely) | «Applicants must be based in the USA and eligible for employment»; в анкете: «Are you based in the USA and eligible for employment (Yes/No)? Если вы отвечаете NO, мы не можем рассматривать вас» |
| **Koast.ai — Head of Technical Support** (weworkremotely) | жёсткое требование «You have run Meta ads. This is a hard requirement»; «Remote (US hours)»; роль руководителя поддержки |
| **Sparix Global — MSD 365 CRM Lead** (weworkremotely) | 10+ лет архитектуры Microsoft CE, .NET/React/SSIS, сертификация FastTrack — не тот профиль и не тот уровень |
| **Keeper Security — B2B Customer Support Representative** | «100% remote position from **select locations**» — какие это локации, не сказано; продуктовая поддержка B2B, а не ИТ-поддержка; зарплата не названа. География неизвестна, а не свободна |
| **VXI Global Solutions — US Remote Technical Support Advisor** | «US Remote» в самом названии |
| **himalayas: Business Systems Support Analyst, Senior Cyber Systems Administrator, Technical Support (Колумбия), Intermediate Software Support Engineer (ЮАР)** | поле `country_restrictions` заполнено: United States / Colombia / South Africa и т. п. |
| **Squarespace, Assignr, Veriswap, Cribl, Mercury** | клиентская поддержка продукта или архитектурные роли, а не ИТ-поддержка и не системное администрирование |

### 1.4 Выбранная вакансия

* **Название:** Ignition, Inc.: Mac MSP Help Desk Guru (work from home)
* **Стабильный id:** `weworkremotely:ignition-inc-mac-msp-help-desk-guru-work-from-home`
* **Ссылка:** https://weworkremotely.com/remote-jobs/ignition-inc-mac-msp-help-desk-guru-work-from-home
* **Источник:** weworkremotely · опубликована 2026-05-28, в базе с 2026-08-21 · Full Time
* **Локация в базе:** Anywhere in the World, California
* **Ограничения по странам:** поле `country_restrictions` пусто
* **Ограничения по часовым поясам:** поле `timezone_restrictions` пусто
* **Зарплата в базе:** не указана (поля пусты)
* **Язык работы:** в тексте не назван
* **Полное описание:** `vacancy.md` в папке артефактов — текст из `state.db` дословно, 9362 символа

Проверка удалённого формата и условий — цитатами из вакансии, каждая проверена на то, что она
действительно есть в тексте (`vacancy.md`, раздел «Условия»):

| цитата | что устанавливает |
| --- | --- |
| «This position is a remote, work-from-home, full-time role» | удалённый формат заявлен прямо |
| «Type & location: Full-time, work from home, fully remote» | то же, ещё раз, в блоке условий |
| «Can work standard business hours in Eastern or Pacific time zones (we currently need both)» | **жёсткое** требование к рабочим часам |
| «Applicants outside the USA will be compensated according to your local employment market» | кандидатов вне США рассматривают, но оплату для них не называют |
| «For applicants closer to the junior side who are located in the USA, $50,000…» | сумма относится только к находящимся в США |
| «Have proven Apple aptitude» | жёсткое требование: владение экосистемой Apple |
| «Have at least 2 years of IT support experience on the job» | жёсткое требование: 2+ года поддержки |

**Чего текст вакансии не устанавливает** (это и есть ответ на «не выдавай отсутствие запрета за
подтверждение»):

* **Права работать из Таиланда.** Ограничения по странам в тексте нет, поле
  `country_restrictions` пусто — но это отсутствие запрета, а не подтверждение. Отдельного
  подтверждения, что кандидата из Таиланда рассмотрят, в вакансии нет.
* **Оплаты для Таиланда.** $50 000–$65 000 в год названы только для кандидатов в США; для
  кандидатов вне США сказано «по местному рынку». Условие «от $1000 в месяц» этой вакансией **не
  подтверждено**.
* **Рабочих часов.** Требование Eastern или Pacific — требование, а не пожелание: для Таиланда
  (UTC+7) это ночная смена (9:00–17:00 ET = 20:00–04:00 ICT, PT — 23:00–07:00 ICT).
* **Языка.** В тексте назван не язык, а требования к общению («reads, and writes clearly»), без
  уровня. В резюме стоит English B1 — единственная подтверждённая оценка.
* **Уровня и грейда.** «junior-to-mid» и «senior candidates can be slotted higher» — не
  обязательство ни по грейду, ни по оплате.

Отдельно: **в этой вакансии нет Bitrix24/CRM**, поэтому Bitrix24 в резюме не выносится вперёд —
он остаётся в списке навыков и в записи Avantis, потому что это оплачиваемый опыт поддержки.

---

## 2. Job-to-Candidate Brief

Артефакты: `brief_short.txt`, `brief_details.md`, `evidence_gap.md`,
`unconfirmed_requirements.md`.

### 2.1 Что справка говорит

Требований разобрано: 14 (4 обязательных, 10 пожеланий). Классы:

* **A — подтверждено опытом работы: 0.**
* **B — подтверждено собственным проектом: 0.**
* **C — частично: 1** — «Google Workspace and Microsoft 365 admin consoles» (навык есть в
  резюме, к роли не привязан).
* **D — нужно уточнение: 0.**
* **E — не подтверждено: 13.**

Это **не** значит, что кандидат не подходит. Это значит, что справка не увидела связи. Ниже —
измеренная причина, а не догадка.

### 2.2 Почему справка так говорит — измеренная причина

Справка распознаёт технологии по словарю из 57 названий, и этот словарь собран только из
**навыков, стеков проектов и названий ролей кандидата**. Обязанности в него не входят — это
сделано намеренно (T6.2: класс A достижим только через `duty`, а голый `skill` — это класс D).

Второй словарь — слова рабочей области, собранные только из обязанностей. Обязанность должна
разделить с требованием **не меньше двух** таких слов (`job_brief._MIN_DOMAIN_HITS = 2`), и порог
поставлен осознанно: при одном общем слове обязанность, где просто упомянута безопасность,
подтверждала требование о сертификате по безопасности.

Итог по этой вакансии: **все 14 требований остановились на одном общем слове или не нашли ни
одного.** Замер (`evidence_gap.md`):

| требование | что справка увидела | чего не хватило |
| --- | --- | --- |
| «Have at least 2 years of IT support experience on the job» | слово `support` → обязанность `duty-94b646ace9` «Provided technical support and troubleshooting for business users» | 1 слово |
| «Slack, Zoom, conference-room AV, networking basics (Meraki)…» | слово `networking` → обязанность `duty-39d5320cd4` «Worked across Windows Server and Linux infrastructure…» | 1 слово |
| «SSO, MFA, and security keys (Okta, Entra ID, Yubikey…)» | слово `security` → обязанность `duty-464bce9700` | 1 слово |
| остальные 11 | ничего: ни названия технологии, ни слова рабочей области | — |

То есть «не подтверждено» здесь означает «справке нечем сопоставлять», и это ограничение
справки, а не отсутствие навыка. Отдельно: единственный честный артефакт про Apple — проект
WHUBN (Swift 6, Apple MapKit) — справка не видит вообще, потому что ни «Apple», ни «MapKit» нет
ни в одном из двух словарей.

Это отдельный дефект, требующий своего решения (что именно из текста обязанности считать
названием технологии и как не вернуть при этом дефект одного общего слова). Здесь он **не
исправлялся**: T7.1 — задача про результат, а не про матчер.

### 2.3 Четыре корзины, как просили

**Подтверждённый опыт работы** (оплачиваемые роли, claim'ы с `role_id`):

* поддержка пользователей и диагностика — `duty-94b646ace9` (Avantis, Bitrix24 Technical Support
  Specialist, апрель 2021 – август 2022);
* администрирование self-hosted Bitrix24, интеграции, бизнес-процессы, роботы — `duty-d87a2fdc28`
  (Avantis);
* Windows Server, Active Directory, Exchange, SQL, Hyper-V, сети, IP-телефония, мониторинг,
  ITSM — `duty-39d5320cd4` (ранний опыт, 2009–2018);
* автоматизация рутинных задач Bash и PowerShell — `duty-0ce6eed193` (ранний опыт);
* Linux/Docker, VPN, бэкапы, сетевая безопасность — `duty-464bce9700` (Sayama Travel);
* Bitrix24-процессы, n8n, интеграции OpenAI/Gemini — `duty-5588820a08`, `duty-60e928bbbf`,
  `duty-7c606ae226` (Sayama Travel).

**Собственные проекты** (не оплачивались, `experience_note-7d59a994c6`):

* асинхронные Python-пайплайны, n8n, Telegram-боты, AI/LLM-пайплайны — `duty-b54fefb3bf`,
  `duty-0512d75ae3`, `duty-c10be2ad60`, `duty-56204a9718`, `duty-2a6c4a25f1`;
* WHUBN — iOS-приложение на Swift 6 и Apple MapKit — `project_summary-612fa8b5bb`,
  `project_bullet-19994f2a95`, `project_bullet-351f1af6ac`, `project_bullet-2e63e97c09`.

**Навыки, заявленные в резюме без привязки к должности** (класс D по логике справки; из резюме
не удалены, но и не подаются как многолетний опыт): Google Workspace APIs, REST APIs, webhooks,
Git, Docker, SQL, AI agents, OpenAI API, Google Gemini API, prompt engineering, Python, n8n,
Linux, PowerShell, Bash.

**Требования без подтверждения:** все 14 требований вакансии — список и причина по каждому в
`unconfirmed_requirements.md` и `evidence_gap.md`. Ключевые: Apple-экосистема (macOS, Apple
Business Manager, MDM, Jamf/Intune), Okta/Entra ID, Meraki, Google Workspace и Microsoft 365
**как администратор**, домены/DNS/хостинг, рабочие часы Eastern/Pacific.

---

## 3. Адаптированное резюме

Файл: `resume_adapted.en.md`. Ниже — полный текст, как он лежит в артефакте.

```markdown
Mikhail Kolesnikov
Application Support & IT Support | Systems & Network Administration | Bitrix24 / CRM Automation | Python Automation & AI Projects
Thailand · Remote · mikhailthaiban@gmail.com · Telegram: @brokeassh

## PROFESSIONAL SUMMARY

IT support and systems specialist with 15+ years in IT: user support and troubleshooting, Windows Server and Active Directory, Exchange email, networking and connectivity diagnostics, and Bitrix24/CRM administration. Automates routine administrative work with PowerShell, Bash, Python and n8n. English B1, fully remote, ICT (+7) timezone.

## CORE SKILLS

- **IT Support & Systems Administration:** Windows Server, Active Directory, Exchange, Hyper-V, Networking, IP Telephony, IT Support, Bitrix24
- **Automation & delivery:** PowerShell, Bash, n8n, Linux, Docker, Git, SQL
- **Python & integrations:** Python, Google Workspace APIs, REST APIs, webhooks
- **AI & LLM:** AI agents, OpenAI API, Google Gemini API, prompt engineering

## EMPLOYMENT HISTORY

### AI Automation Engineer | Sayama Travel

*Thailand | November 2022 – April 2026*

- Administered Linux and Docker infrastructure (systemd services, logs, monitoring); configured VPN infrastructure and maintained backups and network security.
- Automated business processes around Bitrix24, including workflows, robots, triggers, telephony, email, analytics, and lead-generation channels.
- Automated incoming-request handling and internal workflows with n8n and AI APIs (OpenAI, Google Gemini), connecting Bitrix24, corporate chat and Google Sheets.

### Bitrix24 Technical Support Specialist | Avantis

*Moscow | April 2021 – August 2022*

- Provided technical support and troubleshooting for business users.
- Administered self-hosted Bitrix24; configured integrations, business processes, automation robots, and system workflows.

### IT Infrastructure & Support | Speedcast / Germsat

*March 2018 – March 2021*

### Earlier IT Infrastructure & Support | Intravision; Slavyanka; Russian Post

*2009 – 2018*

- Automated routine administrative tasks with Bash and PowerShell.
- Worked across Windows Server and Linux infrastructure, Active Directory, Exchange, SQL, Hyper-V, networking, IP telephony, monitoring, API/web-service testing, and ITSM systems.

## INDEPENDENT PROJECTS

### Own projects in this period

*Thailand | May 2026 – Present*

No paid client work in this period.

- Build my own automation systems: asynchronous Python pipelines, n8n workflows, Telegram bots, and AI/LLM pipelines (RAG, prompt engineering, AI agents).

### WHUBN — Implemented

WHUBN is a local-first iOS app that reconstructs where a person's life happened from permitted location evidence such as photo metadata and location-history imports.

- Built with Swift 6 and Apple MapKit, backed by a high-throughput SQLite engine and deterministic segmentation of location records.

## EDUCATION

- **Sakhalin State University** — Technological Institute, Technology and Entrepreneurship, Higher Education, 2005

## LANGUAGES

- English — B1 (Intermediate)
- Russian — Native

## CONTACT INFORMATION

- mikhailthaiban@gmail.com
- Telegram: @brokeassh
- Thailand
- Remote
```

### 3.1 Что именно сделано — редакционные операции

Адаптация **редакционная**, а не вычитание строк. Все операции объявлены в профиле вакансии в
`tools/t7_preview_adapt.py`; полная карта — `adaptation_map.md`.

* **Переписано — 3 строки.**
  * *Сводка.* Было: «IT specialist with 15+ years in IT: technical support, systems
    administration, Bitrix24/CRM, automation, and my own projects built with AI tools. API
    integration… Telegram bots, n8n/Make workflows, and Linux/Docker infrastructure.» Стало:
    поддержка пользователей, Windows Server/AD, Exchange, сети, Bitrix24 — вперёд, личные
    проекты и LLM-стек убраны. Стоит на `duty-94b646ace9`, `duty-d87a2fdc28`, `duty-39d5320cd4`,
    `duty-0ce6eed193`, `duty-60e928bbbf`, `language-f3534ca421`, `language_level-f536acbf66`.
  * *Заголовок раннего блока.* Убрано имя Speedcast (он теперь отдельная запись).
  * *«No paid client work in this period (confirmed by the candidate).»* → без внутренней
    пометки о том, кто это подтвердил.
* **Объединено — 3 группы.**
  * Пункты Sayama об интеграциях (15, 17, 18) → один пункт; набор claim'ов — объединение
    наборов источников, считается инструментом, потерять claim нельзя.
  * Пять пунктов о личных проектах (32–36) → один.
  * Три пункта WHUBN (55–57) → один, без внутренних имён классов.
* **Сокращено внутри строк навыков.** `Automation & delivery`: убраны MS SQL, Make, PostgreSQL.
  `Python & integrations`: убраны MTProto, Telegram Bot API, Telethon, aiogram, asyncio.
  `AI & LLM`: оставлены 4 названия вместо 13. Ни один навык не добавлен; каждый убранный — это
  выбор по релевантности вакансии, и он записан в карту.
* **Исключено — 26 строк:** Job Search AI (целиком), Reels Analyzer (целиком), Repiton
  (прототип, закрытый решением NO-GO — в резюме для работодателя не идёт), пункт про
  Telegram-боты и MTProto, подробности архитектуры личных проектов.
* **Порядок.** Группы навыков: IT Support & Systems Administration → Automation & delivery →
  Python & integrations → AI & LLM. Внутри Avantis пункт о поддержке пользователей поднят выше
  пункта об администрировании.

### 3.2 Хронология: что исправлено

* **Speedcast / Germsat — отдельная запись** с периодом *March 2018 – March 2021*
  (`chronology_note-451eec4688`, `role_employer-ab93224052`).
* **Сноска внутри блока «2009 – 2018» удалена** (строка master 27). Двойного показа больше нет.
* **Остальные ранние работодатели — без назначенных дат.** Блок называется «Earlier IT
  Infrastructure & Support | Intravision; Slavyanka; Russian Post» и несёт период `2009 – 2018`
  из KB (`role_start-6da0482626`, `role_end-27a202567d`). Точных дат по каждой из трёх компаний в
  источниках нет, поэтому они и не назначены.
* **Обязанности ранних работодателей не разделены по компаниям.** В KB это одна роль с двумя
  обязанностями, привязки к конкретному работодателю нет. Поэтому запись Speedcast несёт только
  название роли, компанию и подтверждённый период, а обязанности остались в сводном блоке — им
  не приписаны ничьи неподтверждённые достижения. В markdown пункты принадлежат ближайшему
  заголовку выше, поэтому пустая запись Speedcast читается однозначно.

### 3.3 Внутреннее, что убрано из публичного текста

Проверка встроена в инструмент и падает, если что-то из этого попадает в текст. В черновике — 0
совпадений. Для сравнения: черновик T7 preview даёт 2 совпадения («подтверждено кандидатом»,
«подтверждён кандидатом»).

Убрано и не публикуется: пометки о том, что факт подтверждён кандидатом; claim'ы, их
идентификаторы и статусы; внутренние предохранители («REVIEW», «kill-switch», «review gate»);
вердикты закрытых проектов (`STAGE_1_FINAL_NO_GO`, `TRACK B`, `BLOCKED BY…`); метрики
экспериментов (macro-F1, recall); внутренние имена классов и модулей личных проектов.
Ни один выдуманный клиент, ни одна выдуманная оплата и ни одно коммерческое внедрение в резюме
не добавлены.

---

## 4. Карта источников и неподтверждённые требования

* `source_map.md` — каждый claim черновика: вид, статус, откуда, ссылка `файл:строка`, значение
  и колонка «в тексте» (`да` / `нет — сокращено`). Провенанс берётся тем же кодом, что и в
  Job-to-Candidate Brief.
* Там же — отдельный раздел **«Переписанные предложения: чем они стоят»**: для каждой из 3
  переписанных строк перечислены claim'ы, на которых стоит новая формулировка. Ни один
  переписанный текст не стоит на пустом месте.
* Там же — строки без claim'ов: заголовок (из `POSITIONING_LABELS`) и названия разделов. Это
  оформление, а не утверждения о кандидате.
* Там же — утверждённые редакционные решения из `APPROVED_EDITORIAL_DECISIONS`, которые видны в
  черновике.
* `unconfirmed_requirements.md` — требования без подтверждения и вопросы, которые они поднимают.
* `evidence_gap.md` — то, что справка видела и отклонила, и то, чего она не увидела совсем.

Проверки, которые инструмент выполняет сам и на которых падает:

1. каждая строка master-резюме либо оставлена, либо исключена с причиной, либо объединена —
   молча потерять строку нельзя;
2. каждый claim добавленной/переписанной/объединённой строки есть в master-резюме;
3. каждое объявленное требование и каждая цитата об условиях — дословная подстрока текста
   вакансии;
4. ни одно неподтверждённое требование не заявлено в черновике как опыт;
5. ни один пункт личного проекта не стоит вне раздела личных проектов;
6. в публичном тексте нет внутренних обозначений;
7. master-резюме не изменилось за время прогона (sha256 до/после).

Отдельно зафиксировано: для этой вакансии проверка №4 **пуста** — вакансия не назвала ни одного
названия из словаря KB, поэтому «спрошено: 0». Инструмент теперь сам об этом сообщает, чтобы
«проверено и чисто» не выглядело как «нечего было проверять».

---

## 5. Тесты

### 5.1 Профильные

```
.venv/Scripts/python.exe -m pytest tests/test_candidate_job_brief.py -q
→ 135 passed in 24.49s
```

Старый профиль инструмента проверен отдельно на неизменность: `resume_adapted.ru.md` для
`habrcareer:1000168221` воспроизводится побайтово (sha256 `7e228da0…923d4`), как и
`brief_short.txt`, `brief_details.md`, `unconfirmed_requirements.md`.

```
uvx ruff@0.16.6 check tools/t7_preview_adapt.py
→ All checks passed!
```

### 5.2 Известное падение — не скрыто, порог не поднят

```
.venv/Scripts/python.exe -m pytest tests/test_report_line_refs.py -q
→ 1 failed, 11 passed in 6.58s
→ tests/test_report_line_refs.py:228: assert 24 <= 18
   ("flagged for reading: 24"), при этом "out of range: 0"
```

Это ратчет, а не цель: он требует, чтобы число symbol-mismatch'ей в `docs/ble001_triage.md` не
росло выше 18. Сейчас 24. Причина — ссылки в отчёте привязаны к номерам строк и разъехались с
кодом, который менялся после замера 2026-09-17 (в том числе в T5–T7). Порог **не поднят**, ссылки
не переписаны: это отдельная работа со своим замером, и в T7.1 она не делалась. `out of range: 0`
означает, что ни одна ссылка не вышла за конец файла.

### 5.3 Полная регрессия — вердикта нет

Полный прогон запускался трижды:

| попытка | как | результат |
| --- | --- | --- |
| 1 | в фоне | убит через 16 м 51 с на ~96 тестах |
| 2 | в foreground, таймаут 10 мин | убит по таймауту на 76 тестах |
| 3 | в фоне, старт 14:42:11 | на 15:01:45 — 180 тестов (5%), `--junitxml` ещё нет |

**Вердикт: UNKNOWN.** `--junitxml` пишется не по ходу, поэтому незавершённый прогон не оставляет
улик вообще. Нет файла — нет вердикта, и «падений не было» из этого не следует.

#### Почему прогон не сходится — измерено, а не предположено

Пропускная способность прогона №3 — **9,2 теста в минуту** (180 тестов за 1173 с) против ~120 в
T6.1 (2379 тестов за ~20 минут). При такой скорости полный сьют требует **~5 часов**: старт в
14:42 закончился бы около 20:00.

Причина разобрана по частям и воспроизведена:

1. **Удаление файла в этом окружении стоит ~70 мс.** Замер — 10 циклов «создать + удалить» в
   свежем каталоге:
   ```
   TEMP fresh dir:   65.3 ms/delete
   TEMP again:       70.4 ms/delete
   repo fresh dir:  1561.9 ms/delete     <-- внутри репозитория в 20 раз дороже
   repo again:      1506.2 ms/delete
   ```
   Запись при этом дешёвая (`100 small files: 0.24s`, `20 x write+fsync: 0.07s`), значит дорого
   именно удаление, а не диск как таковой.
2. **`apply_schema()` — 88 DDL-операторов, и каждый идёт своей транзакцией.** Python-овский
   `sqlite3` не открывает транзакцию под DDL, поэтому каждая `CREATE TABLE` создаёт и удаляет
   файл `-journal`. При 70 мс на удаление это ~6 секунд на свежую базу:
   ```
   apply_schema как сейчас (каждый DDL своей txn): 6.01s
   apply_schema внутри одного BEGIN..COMMIT:        0.08s
   speedup: 71.5x
   ```
3. **Тесты создают свежую базу на каждый тест** (`global_test_isolation` → `tmp_path`), поэтому
   платят эти секунды повторно. 88 удалений × 70 мс на тест — это и есть 9 тестов в минуту.

Фон и sandbox тут ни при чём: тот же замер в фоне дал 54,27 с против 49,78 с в foreground (9%
разницы), а sandbox вне репозитория медленнее не делает.

**Это не следствие T7.1.** `ai_assistant/db.py` и `ai_assistant/db_schema.py` в T7.1 не менялись.
Тот же код 2026-09-22 проходил 1994 теста за 1285 с (`.pytest_ckb_full_01.xml`, 21 минута).

**Что с этим делать — не делалось, это вне T7.1.** Обернуть `apply_schema(conn)` в один
`BEGIN…COMMIT`: замеренный выигрыш 71,5×, свежая база 6,01 с → 0,08 с. Это правка уровня БД,
ей нужны свои пины и мутации, а не побочный эффект задачи про резюме. Отдельно стоит выяснить,
почему удаление **внутри репозитория** стоит 1,5 с — это похоже на сторож удалений, а не на
файловую систему. Попутная наблюдённость: `C:` заполнен на 98% (свободно 11 ГБ), в
`%TEMP%\pytest-of-Misha` накопилось 261 каталог и 12 817 файлов от убитых прогонов.

**Чего это не отменяет:** профильные тесты прошли полностью (5.1), известное падение
воспроизведено и показано (5.2). Полная регрессия остаётся незакрытым пунктом, и называть её
успешной нельзя.

> **Дописано 2026-09-24 после T7.1.** Прогон №3 здесь не «идёт»: он умер на 426 тестах (14%)
> через 51 м 45 с, `--junitxml` не оставил — вердикта нет. Причина из этого раздела разобрана
> до конца и устранена минимальной правкой `db._ensure_schema_on`: 90 DDL-операторов шли
> каждый своей транзакцией, то есть 88 созданий и удалений journal-файла на свежую базу.
> Замеры, тесты и мутации — в `docs/t71_schema_report.md`. Скорость сьюта вернулась к
> ~116 тестам в минуту вместо 9.

---

## 6. Ограничения и что осталось

1. **Полная регрессия не измерена** (§5.3). Прогон №3 идёт в фоне, вердикта пока нет. Чтобы он
   укладывался в разумное время, нужна правка `apply_schema` (один `BEGIN…COMMIT`, замеренный
   выигрыш 71,5×) — отдельная задача со своими пинами и мутациями, в T7.1 не делалась.
2. **Ратчет ссылок `24 <= 18`** — отдельная работа: обновить якоря в `docs/ble001_triage.md`.
3. **Матчер не видит работу, описанную словами.** `_MIN_DOMAIN_HITS = 2` + словарь только из
   навыков кандидата дают 0 классов A/B на вакансии, где у кандидата очевидно есть ответ
   (`duty-94b646ace9`, `duty-39d5320cd4`). Это дефект справки, замерен в `evidence_gap.md` и
   **не исправлялся** — заслуживает отдельной задачи с мутациями, как T7 часть 1.
4. **Автоматическое извлечение требований не работает на этом типе текста.** Описания в
   `state.db` лежат одной строкой без переводов строк; у Ignition пункты «The hard requirements»
   склеены, и автоматический разбор выдал 5 «требований», 4 из которых — рекламный текст про
   компанию. Поэтому требования объявлены дословными цитатами из вакансии, и инструмент
   проверяет, что каждая цитата действительно есть в тексте. Это не второй матчер: классификация
   и доказательства — код справки, объявлен только список строк.
5. **Bitrix24/CRM в удалённом формате в базе нет** — направление из задания закрыть нечем.
6. **География и оплата по выбранной вакансии неизвестны**, а не подтверждены (§1.4).
7. Черновик не использован ни для одного отклика; ничего не отправлено, T8 не начат.

**Следующий шаг — за вами:** посмотреть резюме, и если правки нужны, они делаются в профиле
вакансии в `tools/t7_preview_adapt.py` (объявленной операцией) и перепрогоном, а не правкой
артефакта руками.

---

## 7. Заморозка: что осталось нетронутым

sha256 (первые 16 знаков), снято 2026-09-24 15:0x UTC:

```
artifacts/master_resume.md       177f574be8446162
artifacts/master_resume.ru.md    2c4c76ff9c6fed07
resume.md                        7ae200cdd7251d3c
candidate_kb.json                fafcedb07c8016e9
```

Ничего из этого T7.1 не переписывал: `master_resume.md` / `master_resume.ru.md` генерируются
`master_resume.py`, но не этим инструментом; `resume.md` и `candidate_kb.json` открывались только на
чтение; `state.db` тоже (география вакансии читалась запросом
`sqlite3.connect("file:state.db?mode=ro", uri=True)`).

Артефакты T7.1 (`artifacts/applications/weworkremotely-…/`), первые 16 знаков sha256:

```
resume_adapted.en.md            81bfc6f6aba0a6e8
adaptation_map.md               68774d924205326c
source_map.md                   b7ff5719ad3e2967
brief_short.txt                 4a7a688f9bc5bbb0
brief_details.md                035ed97e5f37d365
unconfirmed_requirements.md     306a9ec38b249776
evidence_gap.md                 cd5361b1a19c481e
vacancy.md                      8f26e3d7185d5f30
```

Артефакт T7 preview не сдвинулся: `artifacts/archive/t710_preview/habrcareer-1000168221/resume_adapted.ru.md`
= `7e228da06d981b28…` — тот же хеш, что и до правок инструмента. (T7.12: файл перенесён в архив, содержимое не менялось.)
