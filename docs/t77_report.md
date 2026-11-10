# T7.7 — GEO SAFETY HOTFIX + APPLICATION QUEUE GUARD

Дата: **2026-09-25**. Ветка: `chore-ruff-cleanup`.
Контекст: T7.6 (HH.ru через BrowserOS, CDP 9110) завершён. Поиск T7.6 не повторялся,
автономный цикл не запускался, T8 не начат.

---

## 0. Главный результат

Два ложных географических допуска, найденных в T7.6, закрыты **и в классификаторе, и
в действующих воротах отправки**:

| строка | было | стало |
|---|---|---|
| `hh:137574152` Servers.com, «Technical Support Engineer (Serbia)» | **band A, eligible** | **band C, ineligible** |
| `hh:137448071` YCLIENTS, «Специалист по сопровождению и автоматизации CRM» | **band B, unknown** | **band C, ineligible** |

Настоящее worldwide-разрешение не пострадало: семь живых формулировок «сотрудник может
работать откуда угодно» по-прежнему дают band A (§3).

Очередь откликов: **1 запись**, `hh:128659037` (Qulix Systems, QA Automation Engineer
(Java)). Она географически «чистая» (band A по явному `Worldwide`), причина — не в
географии. Предложено точное действие, **не применено** (§4.4).

Географический предохранитель поставлен на **пути отправки** — обе точки
`submit_application_in_browser` — и на ворота очереди. На подготовку пакета
(`prepare_application_in_browser`) он **не** поставлен: подготовка ничего не отправляет, а
закрыть её значило бы лишить вакансию с неустановленной географией единственной дороги к
человеку (§4.1). Замер этого решения — 26 падений против 0.

Записи `vacancy_eligibility`, устаревшие после правки, обновлены: **119 из 2351**
(§4.2). Резервная копия базы — `state.db.bak-20260925-t77-geo-refresh`.

---

## 1. Воспроизведение двух дефектов на реальных данных

Инструмент: `tools/.t77_geo_probe.py` (только чтение; оценка не сохраняется).
Снимок «до» — `.t77_before.json`, «после» — `.t77_after.json`.

### 1.1 Servers.com, `hh:137574152`

**Оценка до исправления**

```
BAND         : A   evidence=employer  conflict=False
eligibility  : eligible   geo_scope=worldwide
band reasons :
    - BAND_A: the employer's own text allows work from any country and nothing contradicts it
eligibility reasons :
    - ELIGIBLE: Confirmed remote-eligible for candidate in Thailand
pattern hits :
    employer_worldwide: ['anywhere in the world', 'из любой точки мира']
    employer_restriction: []
```

**Исходные фрагменты объявления** (текст вакансии, EN-блок, затем RU-блок):

```
We are looking for a technical support engineer in Serbia.
...
We will welcome you to our team if you want to learn:

system administration of servers from anywhere in the world;
diagnose hardware, software and network server issues;
...
```

```
Ищем в команду инженера технической поддержки в Сербии.
...
Ждем тебя в нашей команде, если ты хочешь научиться:

администрировать серверы из любой точки мира;

диагностировать аппаратные, программные и сетевые неполадки серверов;
```

Заголовок вакансии: `Technical Support Engineer (Serbia)`.

**Разбор.** Оборот «из любой точки мира» относится к **серверам** — откуда к ним есть
доступ. Он ничего не говорит о том, где может жить сотрудник. При этом та же вакансия
трижды называет страну самой позиции: в заголовке, в английском теле и в русском теле.
Ни одно из трёх утверждений модуль не читал.

### 1.2 YCLIENTS, `hh:137448071`

**Оценка до исправления**

```
BAND         : B   evidence=none  conflict=False
eligibility  : unknown   geo_scope=unknown
band reasons :
    - BAND_B: remote, but the employer states nothing about where the candidate may be located
pattern hits :
    employer_worldwide: []
    employer_restriction: []          <-- ограничение не найдено ни одним шаблоном
    generic_worldwide: []
```

**Исходный фрагмент объявления**

```
Полный рабочий день, график 5\2 с 10:00-19:00 по мск, удаленный формат работы -
строго в пределах Российской Федерации.
```

**Разбор.** `_EMPLOYER_RESTRICTION_PATTERNS` знал `только на территории РФ`,
`гражданство РФ`, `налоговый резидент РФ` — но не полное название и не форму
«в пределах …». Прямое ограничение читалось как «работодатель ничего не сказал».

### 1.3 Оценки после исправления

```
hh:137574152  BAND C   ineligible
    - GEO_NOTE: the text contains a worldwide phrase, but it describes what the work is
      done on rather than where the candidate may live ('…system administration of servers
      from anywhere in the world; …'), so it is not read as permission
    - BAND_C: the employer's own text states where the position is located
      ('looking for a technical support engineer in Serbia'), which is not the candidate's country

hh:137448071  BAND C   ineligible
    - BAND_C: the employer's own text restricts the location or work authorization
      ('в пределах Российской Федерации')
```

---

## 2. Что именно изменено

### 2.1 A / B / C — как теперь различаются формулировки

| | что это | пример | полоса |
|---|---|---|---|
| **A** | прямое разрешение **сотруднику** | «Work from anywhere», «you may work from any country», «Сотрудник может работать из любой страны» | A |
| **B** | неясная география, в том числе оборот о **предмете работы** | «администрировать серверы из любой точки мира», «manage your clients from anywhere», «watch the infrastructure from anywhere» | B |
| **C** | прямое ограничение другой страной или названное место позиции | «строго в пределах Российской Федерации», «(Serbia)», «Location: UK» | C |

### 2.2 Объект работы вместо сотрудника (`ai_assistant/eligibility.py`)

Добавлен страж `_OBJECT_OF_WORK_TAIL_RE` + `_employee_worldwide_match()`. Правило: если
непосредственно перед worldwide-оборотом стоит существительное — предмет работы
(серверы, оборудование, системы, сети, клиенты, базы данных, инфраструктура, заявки,
обращения…), оборот относится к предмету, а не к человеку, и **разрешением не является**.

Свойства, заложенные намеренно:

* страж смотрит на **окно перед** совпадением и требует, чтобы существительное стояло
  вплотную (допускаются только наречие и предлог: «servers from», «серверы из»);
* отклонённое совпадение **не молчит**: в `geo_band_reasons` попадает строка `GEO_NOTE`
  с процитированным фрагментом. Молчаливое подавление выглядело бы как «текст ничего не
  говорит» — это ровно та ошибка, которую чинит T7.7;
* если в тексте есть и оборот о предмете, и настоящее разрешение, берётся второе:
  `_employee_worldwide_match()` пропускает отклонённое совпадение и продолжает поиск.

### 2.3 Ограничение РФ полным названием (`ai_assistant/eligibility.py`)

Введён `_RF_ALT = (?:рф|российск\w*\s+федераци\w*|росси[яи])`, и все РФ-шаблоны переведены
на него. Добавлена форма рабочей **территории**:

```python
rf"\bв\s+пределах\s+{_RF_ALT}\b",
```

Голое `на территории РФ` **не добавлено** намеренно: описание компании («работаем на
территории РФ») не должно становиться ограничением места работы сотрудника. Это прямое
требование задачи.

### 2.4 Названное место самой позиции (`ai_assistant/eligibility.py`)

`_EMPLOYER_ROLE_LOCATION_PATTERNS` + `_TITLE_PLACE_RE` читают утверждение о месте
**позиции**, а не о правилах:

* `Technical Support Engineer (Serbia)` — заголовок, скобка в конце;
* `We are looking for a technical support engineer in Serbia.`;
* `Ищем в команду инженера технической поддержки в Сербии.`;
* `The position is based in Serbia.` / `Location: Serbia` / `Место работы: Сербия`.

Такая находка даёт **C** на том же приоритете, что и явное ограничение: она отвечает на
тот же вопрос («где работодатель ждёт этого человека?»), только сформулирована как место,
а не как правило. В `_PLACE_NAME_ALT` добавлена Сербия (`serbia|серби\w*`).

### 2.5 Что осознанно **не** менялось

* `_WORLDWIDE_GEO_PATTERNS` (строка 603 дублирует «из любой точки мира») — этот список
  питает `geo_scope`, а `geo_scope == WORLDWIDE` сам по себе ничего не разрешает:
  решение принимает полоса. Правка расширила бы радиус без пользы. Записано в §6.
* Порог `test_report_line_refs` — не поднимался (§5.2).
* Зарплатный нормализатор, `AutonomousConfig.cdp_url`, автоответы автономного цикла —
  не трогались, зафиксированы как отдельные риски (§6).
* Резюме, Candidate KB, биография — не изменялись.

---

## 3. Настоящая worldwide-вакансия не пострадала

Страж сужает **оборот**, а не разрешение. Проверено на 16 контрольных формулировках
(прогон в §5.1, файл `tests/test_t77_geo_context.py`):

| формулировка | полоса |
|---|---|
| `Work from anywhere in the world.` | **A** |
| `Work from anywhere, on your schedule.` | **A** |
| `We hire globally and you may work from any country.` | **A** |
| `You can work from any location.` | **A** |
| `Remote worldwide.` | **A** |
| `Сотрудник может работать из любой страны.` | **A** |
| `Мы нанимаем по всему миру.` | **A** |
| `Administer servers from anywhere in the world. You may work from anywhere.` (оба оборота) | **A** |
| `Learn to administer servers from anywhere in the world.` (только предмет) | B |
| `Ждем тебя, если хочешь научиться: администрировать серверы из любой точки мира.` | B |
| `Headquarters: Oregon USA URL: … We build tools.` (адрес штаб-квартиры) | B |
| `Our head office is in Warsaw. We are a fully distributed team.` | B |
| `Location: Remote Join us as an advisor.` | B |

Отдельно проверены живые строки T7.4 — VXI (`band C`, конфликт сохранён), Porkbun
(`band C`, причина «must be based in the USA»), Habr Career (`band B`, структурный маркер
подтверждает только формат). Проверки T7.4 сохранены и продублированы в файле T7.7,
чтобы будущая правка здесь не отменила их молча.

Замечание о «Headquarters»: шаблоны места позиции намеренно **не** реагируют на
`Headquarters: Oregon USA` — нужен либо глагол роли («looking for … in X»), либо
`position/role/vacancy/job … based in X`, либо `Location:`/`Место работы:`, либо скобка в
заголовке. Адрес штаб-квартиры сам по себе ограничением не становится.

---

## 4. Действующая очередь

### 4.1 Где новая оценка используется

| ворота | что читают | пересчитывается? |
|---|---|---|
| `application_queue.generate_queue` (статус-фильтр) | сохранённая запись `vacancy_eligibility` | **да, с T7.7** — строка 636 и 697 файла `application_queue.py` требуют свежую полосу |
| `application_queue.generate_queue` → `_hard_constraints` | текст вакансии | да (всегда) |
| `browser_executor.submit_application_in_browser` (обе точки) | текст вакансии | **да, с T7.7** |
| `browser_executor.prepare_application_in_browser` | текст вакансии | **нет — и это осознанно, см. ниже** |
| `watcher.py` → `_hard_constraints` | текст вакансии | да (анализ, не отправка) |

Добавлена **одна именованная функция** `eligibility.geo_band_permits_auto_send()`: только
band A разрешает отправку без человека. B — «доступность из Таиланда не установлена»,
C — «исключено». Обе отказывают.

Отправляющие пути вызывают `matcher._hard_constraints_for_send()` — это
`_hard_constraints` плюс географический предохранитель. Анализ продолжает вызывать
`_hard_constraints()`: **band B обязан оставаться читаемым**, иначе `eligibility_review`
(список ручного просмотра) опустеет. Два пути — два имени, и это зафиксировано тестом
`test_analysis_is_not_narrowed_by_the_send_gate`.

Географический предохранитель **не** обёрнут в `if remote_required` (в отличие от
соседней проверки формата): вопрос «может ли этот человек работать из Таиланда» не
зависит от предпочтения «только удалёнка», и предохранитель не должен исчезать вместе с
этим предпочтением. В боевом профиле `remote_required: True`, поэтому поведение то же.

**Подготовка (`prepare`) намеренно НЕ закрыта географией.** Это решение принято по
замеру, а не по вкусу. `prepare_application_in_browser` не отправляет ничего: он открывает
страницу, находит поля и собирает пакет со статусом `READY_FOR_REVIEW`. Отправляет
`submit_application_in_browser`, и именно там предохранитель стоит (обе точки). Если
закрыть подготовку, то вакансия с неустановленной географией не сможет дойти **до
человека вообще** — а список ручного рассмотрения T7.5 и мост одобрения
(`application_reviews.APPROVED`) существуют ровно для этого случая. Ворота очереди при
этом всё равно не пускают band B/C в очередь, поэтому закрывать подготовку сверх этого —
потеря возможности, а не приобретение безопасности. Замер до решения: с предохранителем
в подготовке падали **26** тестов, все с сообщением «geo band B is not auto-sendable»;
после переноса — **0**. На строке 2072 файла `browser_executor.py` оставлен комментарий,
чтобы это не «починили» обратно.

### 4.2 Устаревшие сохранённые записи — обновлены

`vacancy_eligibility` пишется один раз при сохранении вакансии и больше не пересматривается.
119 строк несли вердикт, вынесенный **до** правки. Пока `eligibility_review` читает
именно эту запись, список ручного просмотра показывал бы московские офисные вакансии как
«удалёнка, география неясна», а Servers.com — как band A.

Инструмент `tools/.t77_refresh_eligibility.py` (сначала сухой прогон) пересчитал
**только те строки, у которых изменилась полоса** — 119 из 2351:

| переход | строк | причина |
|---|---|---|
| `B → C` | 115 | названное место позиции |
| `A → C` | 4 | 1× Servers.com (объект работы + Сербия), 3× `Location:`/`(US)` в чужой стране |

Разбор совпавших мест — все настоящие:

```
    80  '(Москва)'            6  '(Санкт-Петербург)'     3  '(US)'      2  'Location: South Africa'
     2  'Location: Philippines'  1  'Location: UK'       1  'Location: Germany'   1  '(Mexico)'
     1  'looking for a technical support engineer in Serbia'
     1  'role will be remote, and based in the United States'
     1  'role based in the UK'  1  'looking for someone located in the UK'
     1  'looking for a confident and highly motivated Regional Sales Manager … in the Netherlands'
```

По источникам: habr career 86, himalayas 14, weworkremotely 10, remoteok 7, hh 1.
Ложных срабатываний не найдено; ни одна support/admin-вакансия в изменения не попала.

Повторный замер после записи: `band changed: 0`.
Резервная копия: `state.db.bak-20260925-t77-geo-refresh` (29 425 664 байта).
Статусы `application_tracking` не изменялись.

### 4.3 Почему это не «правка данных вместо правки кода»

Правка кода (§2) закрывает допуск **в воротах**: они пересчитывают полосу из текста и не
зависят от сохранённой записи. Обновление записей — вторая, независимая часть: оно
нужно, чтобы **список ручного просмотра** перестал противоречить воротам. Оставшееся
расхождение зафиксировано как риск (§6.4).

### 4.4 Qulix Systems, `hh:128659037` — состояние и предложение

**Фактическое состояние (2026-09-25)**

| место | значение |
|---|---|
| `application_queue` | 1 запись, `priority_score=28`, `rank=7`, `generated_at=2026-09-21T22:54:14`, `queue_version=v2` |
| `application_tracking` | `READY_TO_APPLY`, `match_score=73.0`, `applied_at=None` |
| `application_reviews` | **`APPROVED`** (обновлено 2026-09-04T11:26:25) |
| `application_packages` | `validation_status=VALID` |
| `application_submissions` | записей по этой вакансии нет |
| `vacancy_eligibility` | `eligible`, band **A**, `geo_scope=worldwide` |

**Почему она остаётся в очереди при профессиональном несоответствии**

Корень — в `matcher.evaluate_hard_requirements`, шаг 1: `excluded_roles` проверяются как
**подстрока** в `title + description`. В профиле есть `"java developer"`, а в вакансии
этой подстроки нет: технология названа как технология —

```
приглашаем Middle - Senior QA Automation Engineer (Java)
…
Практический опыт разработки автотестов на Java от 3 лет.
```

Заголовок при этом — `QA Automation Engineer (Java)`. Проверка на подстроку не срабатывает,
`match_score` остаётся 72–73, статус — `READY_TO_APPLY`, и вакансия попадает в очередь.

География здесь **не** причина: `country_restrictions=['Worldwide']`, band A — честная.

**Опасность сегодня ограничена:** отправка ворот не имеет — `submit_enabled=False`,
автономный цикл на отправку падает с `NotImplementedError`, а реальный путь требует
`--confirm-submit`. То есть Qulix — это запись в списке, а не готовая к отправке заявка.
Но именно этот список человек и просматривает.

**Что существующий механизм может**

`excluded_roles` + `_hard_constraints` уже стоят в `generate_queue` (строки 647 и 708) и
в отправляющих точках `browser_executor`. Достаточно **уточнить формулировки в профиле** —
новый механизм не нужен. Строка причины будет «Excluded role keyword matched: …» — это
утверждение о **форме работы**, а не о том, что у кандидата нет Java. Ложного заявления
о навыках не возникает.

**Точное предлагаемое действие (НЕ применено)**

Добавить в `excluded_roles` файла `ai_assistant/candidate_profile.json` три фразы,
называющие **форму должности**, а не технологию:

```json
"excluded_roles": ["1c", "php", "ruby", "java developer", "c++", "c#", "senior lead", "1с",
                   "qa automation engineer", "sdet", "автотестировщик"]
```

Замер на 2351 живой вакансии (2026-09-25):

| фраза | совпадений | что попадает |
|---|---|---|
| `qa automation engineer` | 5 | QA Automation Engineer (Java), QA Automation Engineer / SDET, Proxify AB Senior QA Automation Engineer, SDET/QA Automation Engineer, **hh:128659037** |
| `sdet` | 3 | те же два SDET-заголовка + SDET / QA Automation Architect |
| `автотестировщик` | 1 | «Автотестировщик в крауд-тестирование» |
| **объединение** | **7** | все семь — должности разработки/архитектуры автотестов |
| support/admin среди них | **0** | — |

Контроль в обратную сторону (почему нельзя проще):

| фраза | совпадений | почему нельзя |
|---|---|---|
| `qa automation` | 13 | лишние: Senior Data Engineer, Lemon.io .NET/React Native/DevOps/React — упоминают в теле |
| `java` | 166 | лишние: «Инженер по сопровождению/Support Engineer (Трайб CVM)» и другие support-роли |

То есть грубый запрет на Java исключил бы ровно те позиции, ради которых проект
существует, — подтверждение предупреждения задачи.

**Второе действие, тоже требующее решения пользователя:** существующая запись
`application_reviews` стоит в `APPROVED` с 2026-09-04. Само по себе уточнение профиля
её не снимет; очередь (сгенерированную 2026-09-21) придётся перегенерировать, а
одобрение — переоткрыть в `PENDING_REVIEW` с пометкой (этот механизм уже применялся к
`vacancies_json:71`: «Approval reopened because persisted browser preparation is BLOCKED»).

**Остановка здесь.** Ни `excluded_roles`, ни статус, ни запись очереди не изменены.

---

## 5. Тесты

### 5.1 Профильные

Новый файл `tests/test_t77_geo_context.py` — 47 проверок, все зелёные:

* оборот о предмете работы (6 формулировок EN/RU) не даёт A;
* отклонённый оборот **попадает в причины** (`GEO_NOTE`), а не исчезает;
* семь настоящих worldwide-разрешений по-прежнему дают A;
* текст с обоими оборотами сохраняет A;
* `Work from anywhere` не ловится на слово «work»;
* семь РФ-формулировок (полное имя и аббревиатура) дают C;
* шесть формулировок места позиции дают C;
* адрес штаб-квартиры не даёт C;
* живые тексты Servers.com и YCLIENTS — band C end-to-end;
* `geo_band_permits_auto_send` пропускает только A;
* `_hard_constraints_for_send` отказывает B и C, пропускает A;
* `_hard_constraints` (анализ) band B **не** сужает;
* **`submit_application_in_browser` (тот, что нажимает) отказывает band B и C** — и в
  причине названа полоса, а не килл-свитч и не дубль;
* **подготовка не закрыта географией** — разбор исходника `prepare_application_in_browser`
  (в нём нет `_hard_constraints_for_send`) и `submit_application_in_browser` (в нём есть);
* `generate_queue` пропускает A, отказывает B и C;
* **устаревшая запись `eligible` не протаскивает band B в очередь** (падает на старом
  поведении);
* VXI / Porkbun / Habr Career из T7.4 сохранены;
* четыре зафиксированных риска (§6).

### 5.2 Известное падение — отдельно

`tests/test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches`

```
distinct file:line references: 301
    flagged for reading: 24
  out of range: 0
assert 24 <= 18
```

Порог **не поднимался**. Падение существовало **до** T7.7: базис, измеренный в T7.5,
— те же 24 при 18. Правки T7.7 ратчет не сдвинули: `eligibility.py` в отчёте не
упоминается вовсе, `matcher.py` — только прозой, а `application_queue.py` и
`browser_executor.py` правились **без изменения числа строк** (в `application_queue.py`
769 голых `\r`, 738 grep-строк — счётчик пинован тестом; правка выполнена байтами через
`tools/.t77_queue_gate_patch.py` с проверкой, что счётчики не изменились и строки
168/180/251/575/617/625–630 не сдвинулись; в `browser_executor.py` отчёт ссылается на
**30** номеров строк, 15 из них ниже 2072, поэтому файл тоже правился построчно —
`git diff --numstat` даёт `5 5`, `wc -l` = 3228 = HEAD).

Замер после всех правок: `tools/report_line_refs.py docs/ble001_triage.md` → **301**
ссылка, 40 голых `\r`, 3 цитаты, **out of range 0**, выход 0. Предупреждение — те же 24.

### 5.3 Профильные и полная регрессия

**Профильные** — 13 файлов (`test_t77_geo_context`, `test_t74_geo_band`, `test_eligibility`,
`test_application_queue`, `test_stage45_application_queue`, `test_candidate_matcher`,
`test_application_integrity`, `test_application_review`, `test_application_tracking`,
`test_application_dashboard`, `test_t75_review_list`, `test_stage20h_review_gate`,
`test_report_line_refs`):

```
280 tests, 279 passed, 1 failed   (junit .pytest_t77_profile.xml, 185.5 с)
```

Единственное падение — известный ратчет из §5.2. `test_t77_geo_context` — 47/47,
`test_t74_geo_band` — 34/34, `test_eligibility` — 12/12.

**Полная регрессия** (`pytest tests/`):

```
2642 tests, 2641 passed, 1 failed   (junit .pytest_t77_regression2.xml, 1126 с)
```

Единственное падение — тот же `test_report_line_refs::test_the_report_has_no_new_symbol_mismatches`.
Код возврата `1` относится к уборке сторожа, а не к тестам; junit записан целиком.

**Промежуточный замер, который стоит показать.** Первый полный прогон после правки
классификатора дал **2640 тестов, 27 падений**. Из них **26** внёс сам T7.7 — все с
сообщением «geo band B is not auto-sendable». Разбор по файлам: 9 в
`test_ble001_fail_closed.py`, 14 в `test_browser_executor.py`, 2 в
`test_stage17d_pipeline.py`, 1 в `test_step25_unified_submission.py`. Причина — предохранитель
был поставлен **и на подготовку пакета**; 25 из 26 падали именно там. Это и есть замер, на
котором основано решение §4.1. После переноса предохранителя на пути отправки осталось
**4** падения — уже на настоящих путях отправки.

**Четыре оставшихся падения и что с ними сделано.** Три — legacy-ветка
`test_ble001_fail_closed.py` (`test_legacy_branch_*`), одно —
`test_step25_unified_submission.py::test_path_a_browser_executor_dry_run`. Их фикстуры были
**географически пустыми**: `location="Remote"`, `country_restrictions=[]`, без единого слова
о стране. Это в точности band B, и отказ на пути отправки — требуемое поведение, а не
регрессия. Предмет этих тестов — механика отправки (проверка клика, килл-свитч, стоп-файл,
идемпотентность, dry-run), а не география, поэтому фикстурам дали **явную** географию:

| файл | что изменено | число строк |
|---|---|---|
| `tests/test_ble001_fail_closed.py`, `_save_non_hh_vacancy` | `country_restrictions=["Worldwide"]` дописан **в ту же строку**, что `location="Remote"` | 8929 = HEAD (файл цитируется отчётом по строкам 2337 и 5655) |
| `tests/test_step25_unified_submission.py`, `_setup_vacancy` | то же поле отдельной строкой с пояснением | 274 → 280 (отчётом не цитируется) |

Ни одно утверждение этих тестов не ослаблено: проверки килл-свитча, стоп-файла,
нечитаемого пакета и дублей остались как были. Полоса A у фикстуры проверена замером
(`assess_vacancy_eligibility` → `GeoBand.A`, «the vacancy declares an explicit scope of
['WORLDWIDE']»), а не предположена.

**Ruff** (`uvx ruff@0.16.6 check`), паритет с HEAD по каждому правленому файлу:

```
eligibility.py              3 → 3      matcher.py                 20 → 20
browser_executor.py        98 → 98     application_queue.py       20 → 20
tests/test_ble001_fail_closed.py  1 → 1   tests/test_step25_unified_submission.py 0 → 0
tests/test_t77_geo_context.py 0        tools/.t77_*.py  — только N999 (точечные имена)
```

---

## 6. Оставшиеся риски

### 6.1 `AutonomousConfig.cdp_url` — мёртвый порт по умолчанию (из §4 задачи)

`AutonomousConfig().cdp_url == "http://127.0.0.1:9222"`, рабочая сессия — на 9110
(`BROWSEROS_CDP_PORT`). Запуск демона без явной конфигурации уйдёт в пустоту.
В рамках географического hotfix **не менялся**. Пин:
`test_recorded_risk_the_autonomous_default_port_is_not_the_live_one`.

### 6.2 Автономный цикл шлёт автоответы при выключенной отправке (из §4 задачи)

`run_cycle` падает с `NotImplementedError`, если `submit_enabled` — то есть отклик
отправить нельзя. Но шаг 4 (`self._process_messages()`) выполняется безусловно, а
`max_auto_replies_per_cycle == 3`. Цикл **не запускался**; пин проверяет форму кода
(`test_recorded_risk_the_autonomous_cycle_reaches_the_message_agent`).
Отдельно: `_discover_fresh_vacancies` → `_process_single_application` переводит вакансию
в `READY_TO_APPLY`, **не вызывая** ни `_hard_constraints`, ни `assess_vacancy_eligibility`
— то есть автономный путь обходит и географические ворота, и очередь. Сегодня это латентно
(отправка заблокирована), но это вторая, независимая причина не запускать цикл.
**Что именно удерживает этот путь после T7.7:** последний шаг всё равно проходит через
`submit_application_in_browser`, а там стоит `_hard_constraints_for_send`, поэтому вакансия
с полосой B или C не будет отправлена, даже дойдя до `READY_TO_SUBMIT` в обход очереди.
Это и есть причина, по которой предохранитель поставлен на **точку нажатия**, а не только
в ворота очереди: ворота защищают очередь, а точка нажатия защищает сам факт отправки.

### 6.3 `normalize_salary_text` читает «до N» как нижнюю границу (из §4 задачи)

```
normalize_salary_text("до 85 000 ₽") -> salary_min = 85000, salary_max = 85000
normalize_salary_text("up to $5,000") -> salary_min = 5000,  salary_max = 5000
```

«до» — потолок; запись его как пола завышает вакансию в глазах любого фильтра по
минимальной зарплате. Нормализатор **не менялся**. Пин:
`test_recorded_risk_an_upper_bound_is_read_as_a_lower_bound`.

### 6.4 Ворота очереди пересчитывают, но сохранённую запись не переписывают

`generate_queue` сохраняет вердикт только когда записи нет. Свежая оценка **отказывает**
правильно, но сохранённая запись остаётся старой, а её читает `eligibility_review`.
Сегодня расхождение снято разовым обновлением (§4.2); повторно оно возникнет только если
классификатор изменится снова. Переписать «всегда» нельзя без добавления строк в
`application_queue.py` — её grep-счётчик пинован на 738.
Пин: `test_recorded_risk_the_queue_gate_recomputes_but_the_record_stays_stale`.

### 6.5 `geo_scope` остаётся `worldwide` там, где разрешения нет

`_WORLDWIDE_GEO_PATTERNS` содержит тот же оборот «из любой точки мира» и питает
`geo_scope`. Для Servers.com после правки `geo_band == C`, но `geo_scope == worldwide`.
На решение это не влияет (решает полоса), но в отчётах и списках поле читается человеком.
Намеренно не менялось, чтобы не расширять радиус (§2.5).

### 6.6 Страж не распространён на упоминание Таиланда

Страж «объекта работы» применён только к worldwide-семейству. Симметричный случай —
`employer_allows_thailand` («дата-центры в Таиланде» как разрешение сотруднику) — не
закрыт. На живом наборе таких строк не найдено, отдельного замера нет; записано как
непроверенная поверхность.

### 6.7 Раса в `_discover_fresh_vacancies` (из T7.6)

Список уже виденных вакансий собирается из БД и из `hh_applications` **до** обхода
запросов; вакансия, появившаяся между сбором и обработкой, может быть обработана
дважды. Не относится к географии, не менялось.

---

## 7. Инструменты

| файл | назначение | пишет? |
|---|---|---|
| `tools/.t77_geo_probe.py` | оценка полосы на живых строках + исходные фрагменты | нет |
| `tools/.t77_queue_gate_patch.py` | байтовая правка `application_queue.py` без сдвига строк | да, с `--apply` и пином sha256 |
| `tools/.t77_refresh_eligibility.py` | обновление устаревших `vacancy_eligibility` | да, с `--apply` и пином sha256 |

Все три — сухой прогон по умолчанию. Ruff: у инструментов остаётся только `N999`
(точка в имени модуля) — та же принятая норма, что у инструментов T7.5.

---

## 8. Ограничения

* Регрессия запускалась на текущем рабочем дереве; `state.db` — боевая, после обновления
  119 записей (§4.2).
* Отклики и сообщения работодателям не отправлялись; автономный цикл не запускался.
* `git push` не выполнялся; T8 не начат.
* Резюме, Candidate KB и биография не изменялись.
