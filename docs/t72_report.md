# T7.2 — распознавание опыта IT-поддержки и извлечение требований

**Дата отчёта:** 2026-09-24 18:09 UTC
**Ветка:** `chore-ruff-cleanup`, HEAD `98be2ea`
**Вакансия разбора:** `weworkremotely:ignition-inc-mac-msp-help-desk-guru-work-from-home`
**Изменённые файлы:** `ai_assistant/candidate/job_brief.py`, `tests/test_candidate_job_brief.py`,
`ai_assistant/cli.py` (байтовая правка), `tools/cr_payloads/*` (2 новых)

Задача T7.1 (ускорение SQLite) не повторялась. T8 не начинался. Отклики не отправлялись.

---

## 1. Кратко: какие ошибки устранены

Замерено до правок на реальной строке Ignition (9 362 символа, **0 переводов строки**;
разделитель абзацев в оригинале — неразрывный пробел U+2800).

### Дефект 1 — опыт поддержки не распознавался при другой формулировке

`"Have at least 2 years of IT support experience on the job"` не давало **ни одного** факта KB.
Причина: требование не называет ни одной технологии, а общих слов с обязанностью
`"Provided technical support and troubleshooting for business users."` нет ни одного
(`IT support` ↔ `technical support`). Матчер возвращал пустой список, и подтверждённая
обязанность выглядела как отсутствие опыта.

**Исправлено:** объявленная таблица профессиональных эквивалентов
(`_PROFESSIONAL_EQUIVALENTS`, три записи: `it-support`, `user-support`, `troubleshooting`),
каждая с явными шаблонами обеих сторон, обоснованием и **якорем** — id конкретного факта KB
(`duty-94b646ace9`). Порог `_MIN_DOMAIN_HITS` **остался равен 2** — глобального ослабления нет.

### Дефект 2 — жёсткие требования Ignition терялись целиком

Четыре жёстких требования приходили **одним сегментом на 440 символов** (в вакансии между
ними нет ни знаков препинания, ни переводов строки). Сегмент упоминал `Pacific time`,
поэтому `_SOFT_NOISE_RE` уводил **весь блок** в контекст — вместе с «2 years of IT support».
Заодно ограничение в 400 символов в `segment()` молча отбрасывало остаток текста.

**Исправлено:** `segment()` понимает U+2800 как разделитель; многословные заголовки ищутся
раньше коротких; блок требований разрезается по словарю открывающих глаголов и по
`label — content`; переполнение отдаётся через `_cap_segment`, а не отбрасывается.

### Дефект 3 — реклама компании попадала в требования

До правки из 5 «требований» Ignition четыре были рекламой, **два из них — обязательными**:

| Что попадало | Класс до |
| --- | --- |
| `Apple-first TL;DR ignitionit.com is a seasoned, Apple-first MSP …` | обязательно |
| `About ignitionit.com We're one of the oldest Apple Technical Partners …` | обязательно |
| `As you grow, there's a clear path into infrastructure administration …` | **A (подтверждено)** |
| `If your profile lines up … 60-minute TestGorilla assessment` | пожелание |

**Исправлено:** расширен `_HARD_NOISE_RE` (адрес сайта, `we're hiring`, `in business for`,
`one of the oldest`, `certified … partners`, `we'll reach out`, `… assessment`) и добавлены
обещания роста (`as you grow`, `clear path into`, `career path`, `growth opportunities`,
`room to grow`). Отдельно: `As you grow …` называло реальный термин KB (`infrastructure`)
и было **единственным** классом A во всём Brief — то есть справка выглядела так, будто
у кандидата есть подтверждённое совпадение, а подтверждено было обещание работодателя.

### Дефект 4 — незнакомые названия исчезали из требований

**Исправлено:** распознавание имён по форме, а не по словарю KB: `macOS`/`iOS` (шаблон
«строчная + заглавная»), списки в скобках, фразы, начинающиеся с известного термина KB,
продукты с версией. Теперь Ignition называет `macOS, iOS, Apple Business Manager, MDM,
Jamf, Intune, Iru, Workspace ONE, Google Workspace, Microsoft 365, Okta, Entra ID, Yubikey,
SSO, MFA` и сообщает их как «не подтверждено доступными фактами» — а не молчит.

### Дефект 5 — длительность опыта смешивалась с наличием опыта

`"at least 2 years of IT support experience"` — две независимые проверки в одной строке.
**Исправлено:** `_duration_split` понижает A/B до **C** и печатает половины раздельно:
`Подтверждено: … Не подтверждено: Вакансия называет срок 2+ лет; … общий IT-стаж сюда не
подставляется.` Ни общий стаж 15+ лет, ни `years_experience=3`, ни длительность другой роли,
ни стаж по технологии не подставляются.

### Дефект 6 — условия найма считались навыками кандидата

**Исправлено:** новое ведро `VacancyFacts.conditions` и ось категорий
(`TECHNOLOGY / DUTY / CONDITION / DURATION / CERTIFICATE`). График, часовой пояс, удалёнка,
право на работу, гражданство, домашний офис и **зарплата** уходят в отдельный блок и
никогда не сопоставляются с KB. Два ложных срабатывания найдены замером и закрыты:
`shifts?` ловил **глагол** в «our priorities **shift**» (культурный абзац объявлялся
обязательным условием работы), а обрезка текста рубила слова посередине
(`Apple at the core — macOS, iOS, Apple Bu`) — теперь `_clip_words` режет по границе слова.

### Дефект 7 — не было проверяемого ручного fallback

Его не существовало вовсе. **Добавлено:** `VacancyInput.manual_requirements` и флаг CLI
`--requirement` (повторяемый). Ручные требования **добавляются** к авторазбору, а не заменяют
его, и каждое проверяется на дословное присутствие в описании вакансии — выдумать требование
через fallback нельзя.

---

## 2. Полный список извлечённых требований Ignition

Получено автоматически из полного описания (`--vacancy … --details`).

### 2.1 Требования (оцениваются против KB)

| № | Требование работодателя | Обязательно | Категории | Класс | Что подтверждено | Доказательство (KB) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Have proven Apple aptitude — you actually use and like the Apple ecosystem | да | CERTIFICATE | E (не подтверждено) | нет подтверждения | — |
| 2 | Have at least 2 years of IT support experience on the job | да | DUTY, DURATION | C (частично) | наличие — оплачиваемой работой; длительность — нет | `duty-94b646ace9` — Provided technical support and troubleshooting for business users. (Bitrix24 Technical Support Specialist) |
| 3 | In a typical week, you might: Onboard a new hire at a fast-growing startup: provision their Mac … | нет | TECHNOLOGY | C (частично) | нет фактов для: «Google Workspace», «Microsoft 365» | — |
| 4 | Apple at the core — macOS, iOS, Apple Business Manager, and MDM (Jamf / Iru / Intune / Workspace ONE …) | нет | TECHNOLOGY, DUTY | E (не подтверждено) | нет фактов для: «Apple Business Manager», «Intune», «Iru», «Jamf», «MDM», «Workspace ONE», «iOS», «macOS» | — |
| 5 | Yes, PCs too — about one-third of our users are driving Windows Google Workspace and Microsoft 365 … | нет | TECHNOLOGY | C (частично) | нет фактов для: «Google Workspace», «Microsoft 365» | — |
| 6 | SSO, MFA, and security keys (Okta, Entra ID, Yubikey, and friends) … | нет | TECHNOLOGY | E (не подтверждено) | нет фактов для: «Entra ID», «MFA», «Okta», «SSO», «Yubikey» | — |

Полное пояснение к строке 2:

> Подтверждено: сама деятельность подтверждена оплачиваемой работой: Bitrix24 Technical Support Specialist.
> Не подтверждено: Вакансия называет срок 2+ лет; стаж именно по этому требованию в KB не измеряется,
> и общий IT-стаж сюда не подставляется. Профессиональная эквивалентность: «it-support» — «IT support»
> в вакансии и «technical support» в обязанности — одна деятельность: поддержка пользователей на
> первой линии. Вакансия называет её по области (IT), обязанность — по способу (technical).
> Источник: `duty-94b646ace9` (Bitrix24 Technical Support Specialist).

### 2.2 Обязательные условия работы (не навыки кандидата)

| № | Условие |
| --- | --- |
| 1 | Work from home |
| 2 | Full-time (Eastern or Pacific time zones) |
| 3 | This position is a remote, work-from-home, full-time role |
| 4 | Can work standard business hours in Eastern or Pacific time zones (we currently need both) |
| 5 | Have a quiet home workspace and reliable broadband |
| 6 | Compensation & logistics Base salary: … located in the USA, $50,000–$65,000/yr … |
| 7 | Type & location: Full-time, work from home, fully remote |
| 8 | Eastern or Pacific time zones |

### 2.3 Что заведомо НЕ является требованием

`Apple-first TL;DR ignitionit.com …`, `About ignitionit.com We're one of the oldest Apple
Technical Partners …`, `As you grow, there's a clear path into …`,
`We move fast, priorities shift …`, `… 60-minute TestGorilla assessment` — все уходят в
контекст и не появляются ни среди требований, ни среди условий.

---

## 3. Обновлённый Job-to-Candidate Brief (телефонный вид)

```
ВАКАНСИЯ: Ignition, Inc.: Mac MSP Help Desk Guru (work from home)
Работодатель: не указано
Зарплата: не указано — сравнить нельзя, минимум кандидата $1000
Формат: удалённо | География: Anywhere in the World, California
Уровень: junior, mid, senior

ЧАСТИЧНО:
  • [обязательно] Have at least 2 years of IT support experience on the job
      — Provided technical support and troubleshooting for business… (Bitrix24 Technical Support Specialist)
      — срок 2+ лет по этому требованию в KB не измеряется: общий IT-стаж сюда не подставляется
  • [пожелание] In a typical week, you might: Onboard a new hire at a fast-growing startup: provision their Mac, set up their
      — не подтверждено доступными фактами: «Google Workspace», «Microsoft 365» (названий нет в KB; это ограничение справки, а не отсутствие навыка)
  • [пожелание] Yes, PCs too — about one-third of our users are driving Windows Google Workspace and Microsoft 365 admin
      — не подтверждено доступными фактами: «Google Workspace», «Microsoft 365» (названий нет в KB; это ограничение справки, а не отсутствие навыка)

ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ БЕЗ ПОДТВЕРЖДЕНИЯ:
  • Have proven Apple aptitude — you actually use and like the Apple ecosystem

Пожелания без подтверждения: Apple at the core — macOS, iOS, Apple, SSO, MFA, and security keys (Okta

Отсутствие подтверждения в доступных источниках — не доказательство отсутствия навыка.

ЧТО УТОЧНИТЬ (влияет на отклик):
  • Есть ли подтверждение по пункту «Have proven Apple aptitude — you actually use and like the…»? В KB подтверждения нет.

ФИЛЬТРЫ ПОИСКА ПРОТИВОРЕЧАТ ПОДТВЕРЖДЁННЫМ ПРЕДПОЧТЕНИЯМ (T6 их не меняет):
  • candidate_profile.json → minimum_salary: сейчас 1500 USD → нужно 1000 USD (подтверждено на интервью)
  • candidate_profile.json → excluded_roles, must_avoid_conditions: сейчас Bitrix24 в списке исключений → нужно рассматривать наравне с другими вакансиями (подтверждено на интервью)
  • candidate_profile.json → years_experience / years_of_experience: сейчас 3 → нужно не подставлять; утверждено только «15+ лет в IT»
  • hh_autonomous_agent.DEFAULT_SEARCH_QUERIES: сейчас Python Developer, … → нужно запросы также по: IT Support, System Administration, Bitrix24/CRM

ОБЯЗАТЕЛЬНЫЕ УСЛОВИЯ РАБОТЫ (не навыки кандидата):
  • Work from home
  • Full-time (Eastern or Pacific time zones)
  • This position is a remote, work-from-home, full-time role
  • Can work standard business hours in Eastern or Pacific time zones (we currently need both)
  • Have a quiet home workspace and reliable broadband
  • Compensation & logistics Base salary: For applicants closer to the junior side who are located in the USA, $50,000–$65,000/yr, depending on
  • Type & location: Full-time, work from home, fully remote
  • Eastern or Pacific time zones

В вакансии не указано: язык работы, зарплата, работодатель.
Общий опыт в IT: 15+ лет — подтверждено пользователем
В профиле years_experience=3; что именно означает это поле, не установлено, поэтому оно не используется ни как общий стаж, ни как стаж в специализации.
```

44 строки — читается с телефона. Ни одного класса **A** и ни одного класса **B**: ни одно
требование Ignition не закрыто полностью.

---

## 4. Сравнение классификации до и после

### 4.1 Счётчики по шести вакансиям

| Вакансия | До | После | Изменение |
| --- | --- | --- | --- |
| ignition | 5 (обязательных 2) | 6 (обязательных 2) | −3 рекламы, +2 жёстких требования, +2 блока технологий |
| it-support | 4 (обязательных 3) | 3 (обязательных 2) | −1 условие найма (США), класс A→C по сроку |
| sysadmin | 18 (обязательных 13) | 16 (обязательных 11) | −2 условия (гражданство), класс A→C по сроку |
| bitrix24 | 9 (обязательных 9) | 9 (обязательных 9) | без изменений |
| python | 13 (обязательных 1) | 14 (обязательных 2) | +1 обязательное требование, ранее терявшееся |
| ai-automation | 14 (обязательных 5) | 14 (обязательных 5) | без изменений |

Все сдвиги объяснены построчно в §4.2–4.4. Ложных совпадений не вернулось.

### 4.2 Ignition: что исчезло и что появилось

| Было | Стало | Почему |
| --- | --- | --- |
| `Apple-first TL;DR …` (обязательно) | контекст | реклама компании — не требование |
| `About ignitionit.com …` (обязательно) | контекст | то же |
| `As you grow, there's a clear path into …` (**A**) | контекст | обещание роста, а не требование сейчас |
| `… TestGorilla assessment` (пожелание) | контекст | этап найма, не требование |
| — | `Have at least 2 years of IT support experience on the job` (обязательно, **C**) | блок был съеден фильтром компенсации |
| — | `Have proven Apple aptitude …` (обязательно, E) | то же |
| — | `Can work standard business hours in Eastern or Pacific time zones …` | → в условия работы |
| — | `Have a quiet home workspace and reliable broadband` | → в условия работы |
| — | `Apple at the core — macOS, iOS, Apple Business Manager, and MDM …` | блок технологий разрезан по `label — content` |
| — | `Yes, PCs too — … Google Workspace and Microsoft 365 …` | то же |
| — | `SSO, MFA, and security keys (Okta, Entra ID, Yubikey …)` | то же |

### 4.3 Изменения класса (не количества)

| Вакансия | Требование | Было | Стало | Почему |
| --- | --- | --- | --- | --- |
| it-support | `Do you have two or more years of live technical support experience …` | A | **C** | назван срок 2+ лет, KB длительность не измеряет |
| sysadmin | `At least five (5) years of Technical Lead experience …` | A | **C** | то же, срок 5+ лет |
| ignition | `Have at least 2 years of IT support experience …` | (отсутствовало) | **C** | наличие подтверждено, длительность — нет |

Это то самое изменение поведения, которого требует §3 задачи. Оно закреплено тестом
`test_no_year_threshold_is_ever_answered_as_fully_confirmed`, который проверяет **все шесть**
вакансий: ни одно требование, называющее срок, не остаётся в классах A или B.

### 4.4 Изменения в ведре условий

| Вакансия | Условие | Откуда |
| --- | --- | --- |
| ignition | 8 записей (график, часовой пояс, домашний офис, зарплата, формат) | было в требованиях/контексте |
| it-support | `Applicants must be based in the USA and eligible for employment` | было обязательным требованием |
| sysadmin | `Citizenship Status Required: …`, `US citizenship may be required …` | были обязательными требованиями |
| bitrix24 | `Можно удалённо` | было требованием |
| python, ai-automation | — | условий нет |

### 4.5 Два старых теста изменены — и почему

Задача прямо запрещает переписывать тесты ради зелёного результата без объяснения изменения
поведения. Изменены ровно два, оба — следствие §3, и оба получили в коде документацию причины:

* `test_a_duty_is_class_a_commercial_experience` — из текста вакансии убран порог «two years»,
  чтобы тест продолжал проверять **своё** утверждение («обязанность = класс A»). Рядом добавлен
  новый `test_a_year_threshold_splits_presence_from_duration`, который проверяет то же
  требование **с** порогом и ожидает C.
* `test_a_spelled_out_threshold_does_not_borrow_the_total_it_tenure` — ожидаемый класс
  изменён с A на C, добавлены проверки обеих половин и того, что утверждённый стаж
  «15+ лет» не подставлен. Правило «не подставлять чужой стаж» не ослаблено.

---

## 5. Тесты и известные ограничения

### 5.1 Профильные тесты

```
tests/test_candidate_job_brief.py  169 passed  (было 135; +34 новых)
```

Покрыто: эквивалентности (`it-support`, `user-support`, `troubleshooting`) с якорями и
обоснованиями; неизменность `_MIN_DOMAIN_HITS`; разделение наличия и длительности; категории;
ручной fallback (включая отказ на выдуманном требовании и на слишком короткой строке);
негативные случаи §6 задачи: `SQL ≠ PostgreSQL`, `Bash/PowerShell ≠ Python`,
`Windows Server ≠ macOS`, `Swift/MapKit ≠ опыт Mac MSP`, общий IT-стаж ≠ два года по продукту,
личный проект ≠ оплачиваемый опыт, работа с безопасностью ≠ сертификат,
`Docker ≠ GitLab CI/Jenkins/GitHub Actions`.

### 5.2 Полная регрессия

```
собрано: 2482 теста (было 2448; +34)
прошло:  2481
упало:   1  — предсуществующее, не связано с T7.2 (см. 5.3)
ошибок:  0
```

Прогон пришлось разделить на две половины: одиночный запуск **убивается сторожем безопасного
удаления** на ~72 % (тот же сторож, что описан в конвенциях проекта). Каждая половина
завершилась и оставила junit-улику:

| Прогон | Файлы | Тесты | Итог | Улика |
| --- | --- | --- | --- | --- |
| голова (до `test_stage31_watcher.py`) | 61 | 1740 | 1739 passed, 1 deselected | `.pytest_t72_head.xml` |
| хвост (с `test_stage31_watcher.py`) | 66 | 742 | 742 passed | `.pytest_t72_tail.xml` |

1739 + 742 + 1 deselected = **2482** — покрыт весь набор. Убитый одиночный прогон junit не
оставил, поэтому его результат — `UNKNOWN`, а не «падений не было».

### 5.3 `test_report_line_refs` — отдельно

```
flagged for reading: 24
out of range:        0
порог:               18   → тест красный
```

**24 при пороге 18 — это ровно базис, замеренный до T7.2** (и он был красным ещё до T1–T6.1).
T7.2 ратчет **не поднял** и порог **не менял**. Все 24 записи — из известных классов
ложных срабатываний, перечисленных в самом тесте; `out of range: 0`.

### 5.4 Известные ограничения

1. **Слитый сегмент в it-support.** Текст
   `Experience with domains, DNS, or hosting You have a steady Internet connection and a quiet
   room to work out of …` остаётся одной записью в ведре условий: пункты списка не разделены
   ничем, а заголовка-метки у этого перечня нет. **Требование не потеряно** — «Experience with
   domains, DNS, or hosting» присутствует отдельной записью среди требований (класс A).
   Расширять словарь открывающих глаголов на `You have / You work / You are` я не стал:
   это непроверяемая поверхность (в этом конкретном случае она всё равно не сработала бы —
   ограничение на длину заголовка в `_split_requirement_block`).
2. **Дубли в ведре условий Ignition.** `Work from home`, `Full-time (Eastern or Pacific time
   zones)`, `Type & location: Full-time, work from home, fully remote` и `Eastern or Pacific
   time zones` — четыре записи об одном и том же факте из метаданных вакансии. Не ложь,
   но шум.
3. **`Google Workspace` не разрешается в канонический термин.** Токены `google` и `workspace`
   помечены как общие, поэтому в KB находится только навык `Google Workspace APIs`, и в
   пояснении появляется «есть близкая технология». Название при этом честно попадает в
   `unrecognised`.
4. **Формулировка вопроса при отсутствии технологии.** Для требования без распознанной
   технологии вопрос теперь строится по тексту требования и обрезается по границе слова
   (`Есть ли подтверждение по пункту «…»?`). Раньше читалось как «Есть ли опыт с
   «Have proven Apple aptitude — you actually use and like the A»?» — обрыв посреди слова.
5. **Матчер не переписывался.** Вторая система классификации и вторая база кандидата не
   создавались; всё сделано в существующем `job_brief.py`.

---

## 6. Что по-прежнему НЕ подтверждено

Новые данные этого не установили, и Brief это прямо показывает.

### 6.1 macOS / MSP и специализированные инструменты Apple

Ни одного факта KB для `macOS`, `iOS`, `Apple Business Manager`, `MDM`, `Jamf`, `Intune`,
`Iru`, `Workspace ONE`, `Google Workspace`, `Microsoft 365`, `Okta`, `Entra ID`, `Yubikey`,
`SSO`, `MFA`. Класс строк 4 и 6 — **E**, строк 3 и 5 — **C** (близкая технология есть,
точного совпадения нет). Это **ограничение справки**, а не отсутствие навыка — и именно
так это сформулировано в Brief.

Обязательное `Have proven Apple aptitude …` — класс **E**, попадает в блок
«ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ БЕЗ ПОДТВЕРЖДЕНИЯ» и в «ЧТО УТОЧНИТЬ».

### 6.2 Длительность опыта по отдельным требованиям

`Have at least 2 years of IT support experience on the job` — наличие подтверждено,
**длительность нет**. KB не измеряет стаж по конкретному требованию; общий IT-стаж
(15+ лет), `years_experience=3`, длительность другой роли и стаж по технологии
не подставляются — это и есть причина класса C.

### 6.3 Допустимость работы из Таиланда

Не установлена. В описании стоит `Anywhere in the World, California`, но зарплата
`$50,000–$65,000/yr` явно оговорена **для находящихся в США** (`located in the USA`).
Отдельного подтверждения права на работу из Таиланда в вакансии нет; Brief это не
додумывает.

### 6.4 График

`Can work standard business hours in Eastern or Pacific time zones (we currently need both)` —
**обязательное условие** (CONDITION), а не навык. Совместимость с Паттайей (GMT+7) в Brief
не оценивается: это решение кандидата, а не вывод справки.

### 6.5 Зарплата

`Compensation & logistics Base salary: … $50,000–$65,000/yr …` — условие работы, не навык.
Строка «Зарплата» в Brief по-прежнему `не указано`: в структурном поле вакансии суммы нет,
поэтому сравнение с минимумом кандидата невозможно, и Brief говорит об этом прямо
(«сравнить нельзя»), а не подставляет число.

---

## 7. Решение по публичному черновику

**Новый публичный черновик резюме под Ignition не создавался.**

Причина: после исправления разбора видно, что **существенные обязательные требования не
подтверждены** — `Have proven Apple aptitude …` (класс E, единственное обязательное
требование помимо поддержки), а весь стек Apple/MSP (macOS, MDM, Jamf, Apple Business
Manager) не имеет ни одного факта в KB. Второе обязательное требование закрыто лишь
наполовину: наличие опыта поддержки подтверждено, длительность — нет.

Подгонять резюме под Ignition ради роста числа совпадений задача запрещает; артефакты T7.1
(`artifacts/applications/weworkremotely-ignition-inc-mac-msp-help-desk-guru-work-from-home/`)
**не перезаписывались**. Существующий `brief_short.txt` там остаётся черновиком T7.1 и
отражает прежнее, дефектное чтение вакансии — при следующем осмысленном шаге его нужно
перегенерировать, но не сейчас.

---

## 8. Ограничения задачи, которые соблюдены

| Запрет | Состояние |
| --- | --- |
| исходное и master-резюме | не изменялись |
| Candidate KB и статусы фактов | не изменялись (`candidate_kb.json` не открывался на запись) |
| профиль кандидата | не изменялся |
| действующие фильтры | не изменялись |
| рабочая `state.db` | не изменялась: `mtime` 2026-09-24 10:01, `-wal`/`-journal` отсутствуют, чтение только `mode=ro` |
| правила отправки откликов | не изменялись |
| реальные отклики | не запускались |
| платные API | не вызывались |
| `git push` | не выполнялся |
| T8 | не начинался |

### Приборы и служебные файлы

`ai_assistant/cli.py` правился **только байтами** через `tools/cr_safe_insert.py`
(`--expect-sha256`, dry-run, затем `--apply`, обе вставки проверены по дельте байт,
голым CR и CRLF). Перепись `tools/bare_cr_census.py` до/после:

| | до | после |
| --- | --- | --- |
| голых CR | 6422 | 6424 (+2 — по одному на строку) |
| CRLF | 6267 | 6269 (+2) |
| голых LF | 0 | 0 |
| байт | 299 427 | 299 749 (+322 = 246 + 76) |

Добавлены два payload-файла: `tools/cr_payloads/cli_candidate_job_brief_requirement_arg.txt`
и `tools/cr_payloads/cli_candidate_job_brief_requirement_dispatch.txt`.

`ruff 0.16.6` — ноль находок в `ai_assistant/candidate/job_brief.py`,
`tests/test_candidate_job_brief.py`, `tools/cr_safe_insert.py`. В `ai_assistant/cli.py`
находок 20, и **ни одна не указывает на вставленные строки** (проверено выборкой по номерам
5019/5020 и 5562/5563); чистить чужие находки в этом файле T7.2 не поручали.

junit-улики `.pytest_*` не удалялись. Временные файлы замеров удалены
(`.t72_*.py`, `.t72_*.json`, `.t72_*.txt`, `.pytest_t72_full.started`).

**Оставлены как улики:**

| Файл | Что это |
| --- | --- |
| `.pytest_t72_head.xml` | полная регрессия, голова: 1739 passed, 0 failures |
| `.pytest_t72_tail.xml` | полная регрессия, хвост: 742 passed, 0 failures |
| `.pytest_t72_ratchet.xml` | `test_report_line_refs`: 24 при пороге 18 |
| `.pytest_t72_focus1..11.xml` | профильные прогоны `test_candidate_job_brief.py` |
| `.t72_baseline.json` | снимок **до** правок по шести вакансиям — источник колонки «До» в §4.1. После правок воспроизводится заново нельзя, поэтому файл сохранён |

Колонка «После» в §4.1 воспроизводится командой
`python -m ai_assistant.cli candidate-job-brief --vacancy <stable_id> --json`.
