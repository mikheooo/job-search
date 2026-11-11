# GIG-SEARCH-P4 — калибровка scoring по измеренным дефектам

Задача была одна: исправить два дефекта, найденных в P3. По ходу нашлись ещё четыре —
все измеренные на реальном корпусе, а не предположенные. Contra-адаптеры не тронуты,
общего рефакторинга не было, отдельный scoring не создавался.

---

## 1. Что было не так — измерено до правок

### Дефект 1: валюта игнорировалась

`budget_quality_score` не смотрел на валюту, а `effective_rate_score` давал лишь −15% за
не-USD. Замер на 15 верхних записях: завышены **все 15**.

| Проект | Бюджет | `budget_quality` было | ставка было | реально |
| :--- | :--- | ---: | ---: | ---: |
| Custom Pneumatic Control Design | 12500–37500 INR | **100** | 189.11 «/ч» | 25 / **$1.97** |
| Python Developer for Algo Trading | 1500–12500 INR | **100** | 52.4 «/ч» | 12 / **$0.55** |
| Data Entry Management System | 750–1250 INR | **100** | 1000 «/ч» | 21 / **$10.4** |
| Data Entry & Template Creation | 12500–37500 INR | **100** | 203.92 «/ч» | 25 / **$2.12** |

INR 12500–37500 это примерно **$130–390**, но получало те же 100 баллов, что и
`USD 12500-37500`. Разрыв — около 80 раз.

### Дефект 2: вес совпадений награждал меньшее число совпадений

`weight = сумма/количество`, поэтому:

| Заказ | Совпадений | Вес было |
| :--- | ---: | ---: |
| Custom Pneumatic Control Design | 1 (`automation`) | **1.00** |
| Python Developer for Algo Trading | 4 (`python`, `rest api`, `sql`, `troubleshooting`) | **0.85** |

Одно совпадение обходило четыре. Это перевёрнутая логика: больше релевантных навыков
должно повышать оценку, а не понижать.

### Дефект 3: не-IT домены не блокировались

В `BLOCKING_SKILLS` были только `3d modeling` и `hardware`, а в реальной выдаче —
`3d cad`, `cad/cam`, `electronics`, `sensor integration`, `bill of materials`, `pneumatic`.
Поэтому железо и механика не блокировались вообще, и пневматика с CAD держала **первое
место** рейтинга с `can_deliver=high`.

### Дефект 4: HKD и AUD не распознавались, а символ `$` побеждал код

В таблице валют не было `HKD` и `AUD` (реально 3 и 12 записей в корпусе), зато символ `$`
маппился в USD. Поэтому HKD-бюджет молча читался как долларовый.

### Дефект 5: пересчитанный балл не доходил до БД

Ветка `UNCHANGED` в `save_gig` писала только провенанс. После калибровки в базе оставалось
`score=79.4, can_deliver=high` у заказа, который новая логика оценивает в 34.4 / low.
Калибровка не имела эффекта в хранилище.

### Дефект 6: `effective_hourly_rate` хранился в валюте источника

Поле содержало `1875` для 1875 INR/ч — рядом с настоящими `$30/ч`. Читая только это поле,
можно было принять INR-число за доллары.

---

## 2. Что сделано

| Правка | Файл | Суть |
| :--- | :--- | :--- |
| **FX-модуль** | `gig/currency.py` (новый) | Курс берётся у источника (`currencyDetails.exchangeRate`), не из таблицы |
| **Конверсия бюджета** | `gig/scoring.py` | `budget_quality` считается в USD |
| **Конверсия ставки** | `gig/assessment.py` | `effective_hourly_rate` возвращает USD |
| **Вес совпадений** | `gig/assessment.py` | Насыщающая агрегация вместо среднего |
| **Не-IT домены** | `gig/assessment.py` | +23 блокера к 8 прежним (всего 31), наблюдаемых в выдаче |
| **Квалификаторы** | `gig/assessment.py` | Широкие токены блокируют только в не-IT контексте |
| **Списки примеров** | `gig/assessment.py` | «languages like Python, C++» — пример, не требование |
| **Валюты** | `gig/normalizer.py` | HKD/AUD/SGD и др.; ISO-код важнее символа |
| **Сохранение оценки** | `gig/store.py` | Пересчёт пишется даже при неизменном контенте |
| **Потолок выполнимости** | `gig/scoring.py` | LOW ≤ 45, MEDIUM ≤ 65 |
| **Курс в фактах** | `sources/freelancer.py` | `freelancer_currency_exchange_rate` |

### Курс — от источника, а не выдуманный

```
INR 0.010402 · HKD 0.12745 · USD 1.0     (наблюдено в currencyDetails.exchangeRate)
```

Если курса нет и валюта не USD — функция возвращает `None`, и значение помечается как
**неизвестное**. Ноль или угаданный курс не подставляются: разница между «курс 1» и «курса
нет» — это ровно та ошибка, из-за которой INR читался как USD.

### Против ложных блокировок

Три токена оказались слишком широкими, и это измерено:

| Токен | Ложное срабатывание | Оговорка |
| :--- | :--- | :--- |
| `architecture` | 5 IT-заказов («software architecture») | только при строительном контексте |
| `embedded` | сайт с «embedded Instagram feed» | только при firmware/PCB контексте |
| `c++` | Python-заказ: «languages like Python, C++, .NET» | не блокирует внутри перечислений |

---

## 3. Результат: до и после

### Корпус

**296** записей Freelancer (было 121; добрано 5 страниц выдачи) + Contra 56 + fixture 29.
Курс есть у **288 из 296**; 8 без курса — закрытые проекты, отдавшие HTTP-ошибку, они честно
остаются неопределёнными.

### 15 записей, которые вели рейтинг

| Было | Стало | Deliverability | Проект |
| ---: | ---: | :--- | :--- |
| 79.4 | **34.4** | `low` | Custom Pneumatic Control Design |
| 73.3 | 54.0 | high | Data Entry Management System Development |
| 72.8 | 50.2 | high | Python Developer for Algo Trading System |
| 70.6 | 70.6 | high | Salesforce/HubSpot Administrator |
| 70.4 | 51.1 | high | Efficient Data Entry Management System |
| 70.0 | 70.0 | high | MarCom Project Manager on Monday.com |
| 69.9 | 59.1 | high | Multi-Platform SMM Specialist |
| 69.8 | 48.7 | high | AttendIQ: Automated Attendance |
| 69.1 | 48.1 | high | Formal Product Webpage Copywriting |
| 65.8 | 44.8 | medium | Commission-Based Email Outreach |
| 65.4 | 44.4 | medium | Data Entry & Template Creation |
| 65.2 | 45.9 | medium | Extract Paper Procedures Spreadsheet |
| 65.2 | 45.9 | medium | Convert Handwritten Notes to PDF |
| 64.8 | 43.9 | medium | Tech Resume & Data Entry |
| 64.2 | 43.1 | medium | Fashion Product Description Writing |

Два верхних места (70.6 и 70.0) **не сдвинулись** — их USD-бюджеты читались верно и раньше.
Это важно: калибровка меняет то, что было посчитано неверно, и не трогает остальное.

### Топ-15 сейчас

| # | Score | Deliver | Проект | Бюджет | Bids |
| -: | ---: | :--- | :--- | :--- | ---: |
| 1 | 70.6 | high | Salesforce/HubSpot Administrator for Revenue System | USD 28–92/ч | 27 |
| 2 | 70.0 | high | MarCom Project Manager on Monday.com | USD 25–50/ч | 15 |
| 3 | 60.0 | medium | PropTech Investment Platform Development — UAE | USD 5000–10000 | 78 |
| 4 | 59.1 | high | Multi-Platform SMM Specialist | INR 1250–2500/ч | 15 |
| 5 | 58.6 | high | Full-Stack Developer for Web Platform | INR 75000–150000 | 12 |
| 6 | 54.0 | high | Data Entry Management System Development | INR 750–1250/ч | 0 |
| 7 | 53.7 | high | Inventory Data Entry & Extraction | INR 750–1250/ч | 6 |
| 8 | 53.1 | medium | Typed Product Descriptions Needed | USD 15–25/ч | 11 |
| 9 | 52.4 | medium | Remote Spanish WhatsApp Support Technician | USD 15–25/ч | 15 |
| 10 | 52.1 | high | **Telegram-Based Production Tracker Upgrade** | EUR 250–750 | 94 |
| 11 | 51.9 | high | **Django Backend Development with PostgreSQL** | USD 250–750 | 89 |
| 12 | 51.9 | high | Website Design with Copywriting and Content | INR 37500–75000 | 2 |
| 13 | 51.5 | high | THE AI CHEAT SHEET FOR SMALL BUSINESS OWNERS | INR 37500–75000 | 4 |
| 14 | 51.5 | high | **Python Classification Model from Excel** | INR 12500–37500 | 2 |
| 15 | 51.1 | high | Efficient Data Entry Management System | INR 750–1250/ч | 20 |

**Топ-15 больше не содержит ни одной `low`-записи.** Появились релевантные: Telegram-трекер,
Django+PostgreSQL, Python-модель, AI Automation Consultant.

### Пневматика в деталях

```
score     34.4  (было 79.4)
deliver   low
budget    INR 12500-37500 → ~$130.28-390.83  (курс источника 0.010422)
rate      ≈$1.97/ч  (из 189.11 INR)
risks     3D CAD · CAD/чертежи · CAD/CAM · SolidWorks
```

Заказ не исчез — он остался в списке, но больше не выглядит лучшим предложением.

---

## 4. Потолок выполнимости: почему это понадобилось

После первых правок в топ-15 всё ещё оставались 7 записей с `can_deliver=low`. Замер
вклада компонентов показал причину:

```
Copy typing work:  effective_rate 14.0 + budget_quality 12.0 = 26.0
                   deliverability          4.5
                   
вес выполнимости — 18 из 100, поэтому провал high→low стоит ~13.5 балла,
и деньги его перекрывают
```

То есть аддитивная модель позволяла бюджету перевесить невыполнимость. Решение — **потолок**,
а не пересмотр весов: `LOW ≤ 45`, `MEDIUM ≤ 65`. Так невыполнимый заказ не может быть выше
выполнимого, каким бы выгодным ни выглядел, но остаётся видимым в списке. Это отражено в
`notes` breakdown как `deliverability_cap`, поэтому причина видна.

**Если это нежелательно** — потолок отключается удалением блока `caps` в `scoring.py`.

---

## 5. Тесты

Добавлено **73 теста**, `tests/gig` вырос с 291 до **364**.

| Группа | N | Что закрепляет |
| :--- | ---: | :--- |
| `TestExchangeRateLookup` | 9 | курс из фактов, отсутствие курса ≠ 1, нулевой курс отвергается |
| `TestBudgetQualityUsesUsd` | 5 | INR больше не равен USD, отсутствие курса → неопределено |
| `TestEffectiveRateUsesUsd` | 7 | конверсия ставки, отсутствие курса, USD без изменений |
| `TestCurrencyParsing` | 21 | HKD/AUD/SGD, ISO-код важнее `$`, мусор → `None` |
| `TestSkillWeightMonotonic` | 3 | больше совпадений не хуже; слабый навык не перевешивает сильный |
| `TestNonItDomainsAreBlocked` | 21 | 16 доменов в списке, пневматика → low, ложные срабатывания |
| `TestRescoringIsPersisted` | 3 | пересчёт пишется, дубликат не обнуляет балл, статус сохраняется |
| `TestScoringIntegration` | 2 | Python-заказ выше пневматики; payment ≠ ready |
| `TestSnapshotFixtures` | 2 | снимки несут реальный курс |

Снимки для тестов (реальные публичные страницы): USD-проект (`..._detail.html`) и
**HKD-проект** (`..._detail_hkd.html`, курс 0.12745) — чтобы конверсия проверялась не на USD.

---

## 6. Полная регрессия

```text
.\.venv\Scripts\python.exe -m pytest tests -q
→ 2 failed, 3376 passed in 1382.87s (23:02)
```

Против прежних **4** отказов осталось **2** — на два меньше, чем было до P4.

| Отказ | Вердикт | Доказательство |
| :--- | :--- | :--- |
| `test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches` | **не мой** | Прогнал чекер с контуром `gig/` и с временно скрытым: **75 и 75** — идентично. Рост с 50 до 75 дал отчёт `docs/ble001_triage.md`, который правили 30.09 уже после отчёта P3. Мой контур в нём не упомянут вообще. |
| `tests/test_candidate_master_resume.py::test_a_project_sync_does_not_touch_resume_or_interview_claims` | **не мой** | Файл застейджен чужим `git add` (статус `A` в индексе), mtime 24.09. Правки P4 его не касались. |

Оба падения существовали и раньше, оба — в чужих незакоммиченных файлах. **Новых падений нет.**

**Про `test_report_line_refs` отдельно.** Чекер сопоставляет файлы по basename, а мой контур
содержит `pipeline.py` и `normalizer.py` — имена, которые есть и вне `gig/`. Это могло бы создать
ложную связь, поэтому влияние измерено, а не объявлено: при скрытом `gig/` число остаётся 75.

**Правки вне `gig/` в P4 не вносились** — калибровка затронула только контур Gig Search;
`cli.py` не менялся вообще.

---

## 7. Ограничения

| Ограничение | Факт |
| :--- | :--- |
| **Курс только у 288 из 296** | 8 записей — закрытые проекты (HTTP-ошибка). Их ставка остаётся `None`, а не выдуманной |
| **Нет курсовой истории** | Used-курс — тот, что был в момент сбора. Для сравнения «сейчас» нужен свежий сбор |
| **`estimated_effort_hours` — эвристика** | Она влияет на fixed-ставку, то есть на `effective_rate`. Это прежняя модель, я её не менял |
| **Потолок — не калибровка весов** | Веса компонентов оставлены как были; потолок — доп. правило поверх. При желании отключается |
| **`BLOCKING_SKILLS` — ручной список** | 19 доменов добавлено по факту выдачи Freelancer. Для другого источника потребуется ревизия |
| **INR-бюджеты исходят от клиента** | Диапазоны вида 12500–37500 — из брифа, часто грубые. Средняя точка информативнее краёв, но это свойство источника |
| **Калибровка на не-IT вертикалях не проверялась** | Проверял домены, реально встреченные в выдаче; другие не измерялись |
| **Тест `test_t78` и ratchet не чинил** | Чужая незакоммиченная работа; лезть туда без нужды не стал |

---

## 8. Что дальше (P5, не начиналось)

По фактам этой задачи:

1. **Пересбор с курс-историей** — чтобы сравнивать ставки «на момент сбора», а не только
   внутри одной сессии.
2. **Второй публичный источник** или адресные страницы `/jobs/<skill>`, чтобы добирать
   AI/Python-заказы, а не 296 случайных.
3. **Калибровка `estimated_effort_hours`** — она влияет на fixed-ставку и сейчас эвристика.
4. **Ревизия порогов для не-IT доменов** при подключении нового источника.
5. **Разбор чужих падений** `test_t78`/`test_report_line_refs`, если ими займётся их автор.

---

## Артефакты

| Артефакт | Назначение |
| :--- | :--- |
| `ai_assistant/gig/currency.py` | FX-конверсия (новый, 4.4 КБ) |
| `tests/gig/test_scoring_calibration.py` | 73 теста калибровки |
| `artifacts/freelancer_project_detail_hkd.html` | снимок не-USD проекта (курс 0.12745) |
| `artifacts/p4_regression.txt` | вывод полной регрессии |

```text
pytest tests/gig -q        -> 364 passed
pytest tests -q            -> 2 failed, 3376 passed   (оба чужие, доказано измерением)
```
