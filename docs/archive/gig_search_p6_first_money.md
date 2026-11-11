| # | first_money_score | project | budget | live bids | age | capability | client risk | decision |
|---:|---:|---|---|---:|---|---|---|---|
| 1 | 52.7 | [Add Noon & Extra Scrapers -- 2](https://www.freelancer.com/projects/python/add-noon-extra-scrapers) | USD 30-250 | 125 | 21.3 h | STRONG | MEDIUM | APPLY_FIRST |
| 2 | 54.8 | [eCommerce Product Data Scraping - MG](https://www.freelancer.com/projects/automation/ecommerce-product-data-scraping) | EUR 250-750 | 118 | 24.4 h | STRONG | LOW | APPLY_FIRST |
| 3 | 51.3 | [Startup Funding Data Collection in Python](https://www.freelancer.com/projects/python/startup-funding-data-collection-python) | USD 250-750 | 66 | 24.0 h | STRONG | MEDIUM | APPLY_FIRST |

# GIG-SEARCH P6 — First Money Run

Проверено: 2026-10-03T14:43:21.006371+00:00. Свежий сбор: 100 проектов. APPLY_FIRST: 3; BACKUP: 7.

Заявки не отправлялись. До подачи откройте страницу ещё раз: статус, бюджет и bids могут измениться. Цена и срок — рекомендации, а не факты объявления.

Ручная QA проводится отдельным чтением живой публичной страницы после генерации отклика. Используются текст объявления и публичные данные проекта; авторизация и возможность отправить заявку с вашего аккаунта не проверены.

## 1. PROJECT — Add Noon & Extra Scrapers -- 2

**LIVE STATUS:** OPEN · verified_at: 2026-10-03T14:41:35Z

**PROJECT URL:** [Add Noon & Extra Scrapers -- 2](https://www.freelancer.com/projects/python/add-noon-extra-scrapers)

**WHY THIS ONE:** Самый узкий объём: два существующих файла, работающий эталон и уже испытанные акторы. Высокая конкуренция снижает шанс ответа, но задачу можно быстрее довести до оплаченной сдачи.

**ACTUAL REQUIREMENTS:**

- Flask / Python 3.10+, Render.com; Apify Starter paid.
- Исправить noon_provider.py и extra_provider.py по рабочему amazon_provider.py.
- Акторы memo23/noon-scraper и boring_internet_explorer/extra-scraper уже работают в Console; проблема в Python API inputs.
- iphone 15 → 20+ продуктов у каждого провайдера; два исправленных файла, скриншоты и push в GitHub.

**CAPABILITY FIT:** STRONG. STRONG: профиль подтверждает Python и REST API как PROFESSIONAL; в репозитории есть рабочий публичный сбор и обработка API. Flask/Apify — переносимые навыки, отдельный прошлый опыт Apify не заявляется. Это ограниченное исправление inputs, а не разработка платформы.

**CLIENT INFO:** Риск MEDIUM: способ оплаты подтверждён клиентом; отзывов нет — истории работы на площадке не видно; аккаунт свежий: 1 дн.; депозит внесён; профиль клиента не заполнен полностью; сильных сигналов 1, фоновых 2 (4 пунктов). Публичный HTML snapshot: country=Saudi Arabia, rating=None, reviews=0, payment_verified=True, member_since=2026-10-02T14:28:29Z; jobs_posted=None, hire_rate=None. None означает unknown. Owner history не раскрывается анонимным API; клиентские поля могут быть кешированы.

**MONEY:** бюджет USD 30-250; оценка объёма P6 8 ч; эффективная ставка середины бюджета $17.5/ч. Рекомендуем $180; после комиссии 10% остаётся $162, около $20.25/ч при 8 ч (диапазон $16.20–27/ч при 6–10 ч), до налогов/вывода. P5 исходно оценил 24.4 ч; P6 уточняет объём двух файлов по ТЗ. Часы — инженерная оценка, не измеренная длительность.

**LIVE COMPETITION:** discovery=115; live=125; delta=10. QA: 2026-10-03T14:41:35.533203+00:00

**RISKS:**

- Repository access, client Apify credentials and paid actor runs must be available; do not expose tokens in screenshots.
- Actor response changes or an unrelated Render deployment problem would expand the quoted two-file repair.
- 125 competing bids at preparation; reply or award is uncertain.

**RECOMMENDED BID:** 180 USD. Цена за исправление двух готовых провайдеров, тесты и сдачу; внутри $30–250. Оценка 8 ч, диапазон 6–10 ч; срок включает запас на доступы.

**DELIVERY TIME:** 3 days после согласования объёма и получения исходных данных/доступов.

- Day 1: input mapping and Noon acceptance — USD 60
- Day 2: Extra acceptance and regression checks — USD 60
- Day 3: both screenshots, repository push and handover — USD 60

**TECHNICAL APPROACH:**

1. Compare the existing provider calls with the working amazon_provider.py reference and successful Apify Console input JSON.
2. Correct Noon input mapping, run the iphone 15 acceptance query, and verify at least 20 products.
3. Correct Extra input mapping and verify the same acceptance query without changing unrelated providers.
4. Check error handling and existing Flask integration, capture both result screenshots, and push the two corrected files to the repository.

**PROPOSAL:**

```text
Hello,

Your Apify actors already work in the Console, so I would focus on the Python input mismatch rather than rebuild your scraper. I would compare noon_provider.py and extra_provider.py against amazon_provider.py, map the exact actor input fields, and keep the existing Flask integration intact.

Acceptance would be your stated test: searching for iphone 15 returns at least 20 products from each provider. I would deliver both corrected files, screenshot evidence, and the changes pushed to your repository.

My proposed fixed price is USD 180, with delivery in three days after repository access and current actor inputs are available. The milestones are input mapping and Noon, Extra and regression checks, then screenshots and handover. Additional actors or unrelated deployment changes would be quoted separately.

Could you share the failing Python response and the successful Console input JSON for each actor?
```

**CLARIFYING QUESTION:** Could you share the failing Python response and the successful Console input JSON for each actor?

**PROOF IDEA:** 15–20 minutes: a small synthetic actor-input mapping and normalized product response example. No client credentials, paid actor run, or full provider repair before award.

**QA: PASS.** Отдельно повторно открыт публичный project URL после подготовки пакета; HTML: C:\Users\Misha\Documents\job-search\artifacts\p6\qa_fl_40746706.html. Анонимный GET API: https://www.freelancer.com/api/projects/0.1/projects/40746706/?full_description=true&job_details=true, сохранён manual_qa_observations.json. status=active, frontend=open; bids=125; currency=USD; budget=30.0–250.0. Текст не изменился относительно генерации, две приведённые цитаты есть буквально. Отклик 139 слов; технологии сверены по тексту, опыта/портфолио не придумано; цена и граница объёма проверены вручную. API актуальнее SEO HTML; браузер Neo недоступен, экран и возможность подачи с аккаунта не проверены.

**Score breakdown:**

- capability: 82.00 × 24; capability_fit=strong (покрытие 0.888)
- competition: 8.00 × 18; very_hard (125 live ставок)
- freshness: 72.00 × 14; under_24h
- scope_clarity: 59.00 × 12; ясность ТЗ 0.59 (5 пунктов работы)
- rate: 35.71 × 12; $17.5/ч
- effort: 95.00 × 8; 8 ч — короткий заказ
- quick_win: 90.00 × 4; результат достижим за 8 ч
- scope_creep: 64.00 × 2; неизвестных пунктов: 3; ясность ТЗ 0.59
- action_score: 36.60 × 6; P5 action_score, unchanged formula

Client multiplier: MEDIUM (множитель 0.92)

## 2. PROJECT — eCommerce Product Data Scraping - MG

**LIVE STATUS:** OPEN · verified_at: 2026-10-03T14:41:42Z

**PROJECT URL:** [eCommerce Product Data Scraping - MG](https://www.freelancer.com/projects/automation/ecommerce-product-data-scraping)

**WHY THIS ONE:** Один конкретный сайт, понятная проверка по образцу и воспроизводимый локальный результат. Реальный текст ограничивает данные описаниями/характеристиками/изображениями и задаёт месяц на сдачу.

**ACTUAL REQUIREMENTS:**

- Целевой сайт vernicispray.es; только title, description, specification fields и соответствующие изображения, без цены/рейтинга/отзывов.
- Категории или поиск, robots.txt и rate limits; CSV либо JSON с явными названиями полей.
- Повторный запуск через смену списка URL/категорий, локальные инструкции/setup.
- Acceptance: сравнение sample descriptions/spec tables с live pages и корректно открывающийся export.
- Текст: €250–500 и deadline 1 month; платформа: €250–750. Bid €450 подходит обоим.

**CAPABILITY FIT:** STRONG. STRONG: подтверждённый Python PROFESSIONAL и существующий сбор HTML/структурированных данных в ai_assistant/gig/sources/freelancer.py. Доступный публичный homepage подтверждён отдельным чтением. Requests/BeautifulSoup — предложенный путь из разрешённых клиентом вариантов, прошлый опыт именно этого магазина не заявляется.

**CLIENT INFO:** Риск LOW: способ оплаты подтверждён клиентом; отзывов: 1; рейтинг клиента 5; аккаунт с 2020-11-20; депозит внесён; сильных сигналов 0, фоновых 0 (0 пунктов). Публичный HTML snapshot: country=Spain, rating=5.0, reviews=1, payment_verified=True, member_since=2020-11-20T15:58:12Z; jobs_posted=None, hire_rate=None. None означает unknown. Owner history не раскрывается анонимным API; клиентские поля могут быть кешированы.

**MONEY:** бюджет EUR 250-750; оценка объёма P6 24 ч; эффективная ставка середины бюджета $23.45/ч. €450 → €405 после 10% комиссии. При наблюдаемом API FX 1 EUR=$1.1257 это около $455.91, или $19.00/ч при 24 ч (18–30 ч: $15.20–25.33/ч). P5 исходно 54.4 ч; P6 ограничен одним сайтом и согласованным списком. FX — текущий snapshot, до налогов/вывода.

**LIVE COMPETITION:** discovery=117; live=118; delta=1. QA: 2026-10-03T14:41:42.395468+00:00

**RISKS:**

- Full catalogue count and category layouts are unknown; agree the initial list before fixing scope.
- Dynamic pages, blocks or changed markup may require additional handling; public homepage access does not prove every product page is extractable.
- Images are linked without editing; a complete downloaded image archive would need separate scope agreement.
- 118 competing bids at preparation; the cached client snapshot is not a payment guarantee.

**RECOMMENDED BID:** 450 EUR. Внутри и платформенного €250–750, и более узкого €250–500 из текста. Оценка 24 ч (18–30); цена за один сайт и согласованный список, без бессрочного сервиса.

**DELIVERY TIME:** 7 days после согласования объёма и получения исходных данных/доступов.

- Days 1–2: sample extraction and agreed schema — EUR 90
- Days 3–5: configurable scraper and CSV/JSON export — EUR 270
- Days 6–7: sample acceptance, fixes and local setup guide — EUR 90

**TECHNICAL APPROACH:**

1. Agree the product sample, field mapping and initial category/URL count for vernicispray.es.
2. Check robots.txt and public access, then implement Python navigation with rate limiting and retries.
3. Extract full titles, descriptions, all specification fields and matching image links; exclude prices, ratings and reviews.
4. Export labelled CSV/JSON and expose a configurable URL/category input list for local reruns.
5. Compare sample output with live product descriptions/spec tables, validate the export, and supply setup instructions.

**PROPOSAL:**

```text
Hello,

I would build a configurable Python scraper for vernicispray.es that captures complete descriptions, specification fields, and each product’s matching image links. Prices, ratings, and reviews would be excluded, as requested. The export would use clearly labelled CSV or JSON fields and preserve the source text without rewriting it.

I would first agree the field mapping on a small product sample, then implement category or URL-list navigation with rate limits and robots.txt checks. You would be able to rerun the scraper locally by changing the input list, using the included setup instructions. Final validation would compare exported fields against those same live product pages.

I propose EUR 450 and seven days, within your stated one-month deadline. The quote covers one site and the agreed input list; recurring hosting and site redesigns are separate. How many categories and product URLs should the initial acceptance run cover?
```

**CLARIFYING QUESTION:** How many categories and product URLs should the initial acceptance run cover?

**PROOF IDEA:** 15–20 minutes: two synthetic product rows and a field-to-page mapping sketch showing description/spec/image columns. Label the rows synthetic; do not deliver a full free catalogue.

**QA: PASS.** Отдельно повторно открыт публичный project URL после подготовки пакета; HTML: C:\Users\Misha\Documents\job-search\artifacts\p6\qa_fl_40746421.html. Анонимный GET API: https://www.freelancer.com/api/projects/0.1/projects/40746421/?full_description=true&job_details=true, сохранён manual_qa_observations.json. status=active, frontend=open; bids=118; currency=EUR; budget=250.0–750.0. Текст не изменился относительно генерации, две приведённые цитаты есть буквально. Отклик 145 слов; технологии сверены по тексту, опыта/портфолио не придумано; цена и граница объёма проверены вручную. API актуальнее SEO HTML; браузер Neo недоступен, экран и возможность подачи с аккаунта не проверены.

**Score breakdown:**

- capability: 82.00 × 24; capability_fit=strong (покрытие 0.88)
- competition: 8.00 × 18; very_hard (118 live ставок)
- freshness: 48.00 × 14; 1_3_days
- scope_clarity: 54.00 × 12; ясность ТЗ 0.54 (6 пунктов работы)
- rate: 52.71 × 12; $23.45/ч
- effort: 80.00 × 8; 24 ч — умеренный объём
- quick_win: 90.00 × 4; результат достижим за 24 ч
- scope_creep: 88.00 × 2; неизвестных пунктов: 1; ясность ТЗ 0.54
- action_score: 39.90 × 6; P5 action_score, unchanged formula

Client multiplier: LOW: способ оплаты подтверждён клиентом; отзывов: 1; рейтинг клиента 5; аккаунт с 2020-11-20; депозит внесён; сильных сигналов 0, фоновых 0 (0 пунктов)

## 3. PROJECT — Startup Funding Data Collection in Python

**LIVE STATUS:** OPEN · verified_at: 2026-10-03T14:41:44Z

**PROJECT URL:** [Startup Funding Data Collection in Python](https://www.freelancer.com/projects/python/startup-funding-data-collection-python)

**WHY THIS ONE:** Меньше конкурентов, чем у двух других задач; Python-обработка и проверяемый CSV. Объём широк, поэтому цена/срок относятся к предложенной первой партии 100 записей, с явной фиксацией неизвестных полей.

**ACTUAL REQUIREMENTS:**

- USA, Europe including UK, Australia; недавнее funding, желательно 30–90 дней с согласованием периода.
- Открытые объявления/сайты/новости; очистка и дедупликация; 16 полей, включая source URL и публичные decision-maker данные.
- Clean Excel/CSV, source URLs, документация, coverage/limitations/unavailable fields; reusable Python script по согласованному scope.
- В отклике указать sources, estimated records, cost/timeline; не выдумывать funding portfolio.

**CAPABILITY FIT:** STRONG. STRONG: Python PROFESSIONAL, Pandas BASIC в профиле; существующий код сбора, нормализации и дедупликации. Решение — Python + проверка открытых первоисточников. Funding-domain опыт не подтверждён и не заявляется; платные базы и закрытый LinkedIn не включены.

**CLIENT INFO:** Риск MEDIUM: способ оплаты НЕ подтверждён; отзывов нет — истории работы на площадке не видно; аккаунт с 2025-02-22; депозит не внесён; сильных сигналов 1, фоновых 2 (4 пунктов). Публичный HTML snapshot: country=India, rating=None, reviews=0, payment_verified=False, member_since=2025-02-22T07:39:46Z; jobs_posted=None, hire_rate=None. None означает unknown. Owner history не раскрывается анонимным API; клиентские поля могут быть кешированы.

**MONEY:** бюджет USD 250-750; оценка объёма P6 30 ч; эффективная ставка середины бюджета $16.67/ч. $475 → $427.50 после 10% комиссии, $14.25/ч при 30 ч (24–36 ч: $11.88–17.81/ч). P5 исходно 65.2 ч; P6 оценивает предложенную первую партию 100 компаний, не бессрочную базу. До налогов/вывода.

**LIVE COMPETITION:** discovery=64; live=66; delta=2. QA: 2026-10-03T14:41:44.813131+00:00

**RISKS:**

- 100 companies and 90 days are proposed boundaries to agree before work; public source coverage has not yet been measured.
- Some employee counts, total funding or LinkedIn profiles are unavailable; report missing fields instead of fabricating them.
- Respect source terms/access restrictions; no paywalled database or logged-in LinkedIn scraping is promised.
- The public client snapshot shows no reviews and payment unverified; use a funded platform milestone before work.
- 66 competing bids at preparation; recurring collection is separate from this first fixed batch.

**RECOMMENDED BID:** 475 USD. $475 внутри $250–750; оценка 30 ч (24–36) для первой согласованной партии 100 компаний. Число записей — предложенная граница цены, не опубликованная гарантия клиента.

**DELIVERY TIME:** 7 days после согласования объёма и получения исходных данных/доступов.

- Days 1–2: sources, schema and ten sample records — USD 95
- Days 3–5: first 100 clean records and reusable script — USD 285
- Days 6–7: source QA, coverage report and documentation — USD 95

**TECHNICAL APPROACH:**

1. Agree the proposed first batch of 100 companies, 90-day window, region split and openly accessible sources; no paid database assumption.
2. Validate the 16-field schema using ten sample rows with announcement verification URLs.
3. Collect public company/investor announcements and accessible funding news with Python; record only publicly available company and decision-maker fields.
4. Clean and deduplicate with Pandas; export Excel/CSV and retain traceable source URLs.
5. Review source/date accuracy and missing-field coverage, then deliver the reusable script and short collection/setup documentation.

**PROPOSAL:**

```text
Hello,

I propose an initial batch of 100 recently funded companies across the USA, Europe including the UK, and Australia, covering the last 90 days. I would use public company and investor announcements plus openly accessible funding news, keeping a verification URL for each record.

The dataset would follow your 16-field schema, remove duplicate companies, and leave unavailable public fields clearly marked rather than guess them. I would use Python with BeautifulSoup and Pandas for extraction, cleanup, and Excel/CSV exports, with a reusable collection script and short documentation included in this scope.

The proposed price is USD 475 with delivery in seven days: agree sources and ten sample rows, complete the first batch, then validate sources and document coverage. Automated updates beyond this batch would be scoped separately. I would approach the funding domain through source verification; no previous funding-data portfolio is claimed. Should the 100 records be split equally across the three regions or weighted toward one?
```

**CLARIFYING QUESTION:** Should the 100 records be split equally across the three regions or weighted toward one?

**PROOF IDEA:** 15–20 minutes: two explicitly synthetic company rows with the 16-field schema, missing-value rules and a source-verification checklist. No free 100-record research batch.

**QA: PASS.** Отдельно повторно открыт публичный project URL после подготовки пакета; HTML: C:\Users\Misha\Documents\job-search\artifacts\p6\qa_fl_40746459.html. Анонимный GET API: https://www.freelancer.com/api/projects/0.1/projects/40746459/?full_description=true&job_details=true, сохранён manual_qa_observations.json. status=active, frontend=open; bids=66; currency=USD; budget=250.0–750.0. Текст не изменился относительно генерации, две приведённые цитаты есть буквально. Отклик 158 слов; технологии сверены по тексту, опыта/портфолио не придумано; цена и граница объёма проверены вручную. API актуальнее SEO HTML; браузер Neo недоступен, экран и возможность подачи с аккаунта не проверены.

**Score breakdown:**

- capability: 82.00 × 24; capability_fit=strong (покрытие 0.839)
- competition: 26.00 × 18; hard (66 live ставок)
- freshness: 72.00 × 14; under_24h
- scope_clarity: 25.00 × 12; ясность ТЗ 0.25 (8 пунктов работы)
- rate: 33.34 × 12; $16.67/ч
- effort: 80.00 × 8; 30 ч — умеренный объём
- quick_win: 90.00 × 4; результат достижим за 30 ч
- scope_creep: 88.00 × 2; неизвестных пунктов: 1; ясность ТЗ 0.25
- action_score: 43.40 × 6; P5 action_score, unchanged formula

Client multiplier: MEDIUM (множитель 0.92)

## BACKUP

- [Wayfair Closeout Discount Scraper -- 2](https://www.freelancer.com/projects/beautifulsoup/wayfair-closeout-discount-scraper-40747946) — 56.1; STRONG; recommended bid is not economically viable after platform fee; Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use
- [White-Label LLM Web Interface](https://www.freelancer.com/projects/web-development/white-label-llm-web-interface) — 53.7; LEARNABLE; capability_fit=learnable — backup only; Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use
- [Full-Stack Web Dev for Education Management System](https://www.freelancer.com/projects/python/full-stack-web-dev-for) — 51.0; LEARNABLE; capability_fit=learnable — backup only; Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use
- [Comprehensive Hotel Data Scrape](https://www.freelancer.com/projects/data-scraping/comprehensive-hotel-data-scrape) — 48.2; STRONG; apply-first list is capped; Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use
- [Remote IT Back Office Coordinator](https://www.freelancer.com/projects/accounting/remote-back-office-coordinator) — 49.0; STRONG; Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use
- [Python Crawler for Building Case Archive](https://www.freelancer.com/projects/data-extraction/python-crawler-for-building-case) — 43.4; PROVEN; backup list is capped; Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use
- [Nationwide Cosmetic & Dental Clinic List](https://www.freelancer.com/projects/data-collection/nationwide-cosmetic-dental-clinic-list) — 42.2; STRONG; recommended bid is not economically viable after platform fee; backup list is capped; Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use

## SKIP

- Expert AdsPower Developer (RPA Plus / API / MCP) for Social Media Automation — FAIL manual QA: required AdsPower RPA Plus / MCP experience is not confirmed; API adjacency alone is insufficient. Capability revised UNKNOWN for this scope.
- Freelance Bidder & Portfolio Manager Needed -- 3 — recommended bid is not economically viable after platform fee; FAIL manual QA: bidder/portfolio-management role is not a bounded technical first-money deliverable; net bid under viable floor.
- TikTok Phone-Username Privacy Audit — recommended bid is not economically viable after platform fee; FAIL manual QA: deliverability of phone-to-username lookup not established; proposed price under viable floor.
- Hindi Python Video Course Creation — recommended bid is not economically viable after platform fee; FAIL manual QA: Hindi teaching and video production capability unconfirmed; Python alone is insufficient. Net price below viable floor.
- Sanitized USA Marketing Leads — recommended bid is not economically viable after platform fee; FAIL manual QA: broad lead quality requirements and net bid under viable floor.
- Bangladeshi Doctors List with BMDC registration Number — proposal not ready or unsafe; recommended bid is not economically viable after platform fee; FAIL manual QA: professional registry coverage unverified, automatic proposal not ready and net bid under viable floor.
- Pokemon Autobuyer Bot Development — FAIL manual QA: unknown game-specific purchasing environment, accounts and anti-bot handling; capability not established for the complete scope.
- Streamlining Data with Python Analysis — client_risk=HIGH; proposal not ready or unsafe
- PDF Text & Image Extraction — client_risk=HIGH; proposal not ready or unsafe
- Copy Website Text Into Excel — client_risk=HIGH

## Money Funnel Metrics

| Metric | Value |
|---|---:|
| projects_seen | 100 |
| projects_live_verified | 20 |
| apply_first_count | 3 |
| proposals_ready | 3 |
| proposals_submitted | 0 |
| client_replies | 0 |
| interviews | 0 |
| awards | 0 |
| revenue_usd | 0.0 |
| reply_rate | undefined (no submissions) |
| award_rate | undefined (no submissions) |
| interview_rate | undefined (no submissions) |
| revenue_per_10_bids | undefined (no submissions) |

Выручка означает записанный реальный платёж, а не awarded budget. Нулевые события воронки не заменяются прогнозом.

## Источники, границы и воспроизведение

Новый сбор в этом P6: 100 публичных проектов по семи уже существующим направлениям, 107 GET-запросов на этапе коллекции. 20 лучших перепроверены через публичную project page и анонимный API; финальные три открыты повторно отдельным QA после подготовки откликов. `fresh_collection.json` — новый сбор этого запуска; `--resume-collection` переиспользует его, а страницы/API всё равно читает заново. Браузер Neo не отвечал на локальном endpoint; публичные GET не подтверждают экран, доступность bidding, баланс или квоту аккаунта.

В SEO HTML обнаружены устаревшие bids (например, Noon 20–115 против API 125). Итоговые status/bids/budget/currency/description/skills/age берутся из публичного API, client history — отдельно помеченный HTML snapshot. API owner history не отдаёт. `bids_at_discovery` берётся из ранее сохранённого наблюдения базы и не гарантирует самый первый исторический bid count; сохранён неизменно при повторных проверках. `found_via` сохранён как provenance и не используется как требование.

Формулы P5 opportunity/action, FX, дедупликации и оценки трудоёмкости не переписаны; их исходные файлы сверены с pre-edit checkpoint. P6 отдельно хранит оценку узкого согласуемого объёма и исходную P5 оценку; компонент action_score использует P5 до ручной оценки. First-money score — эвристика, не вероятность выигрыша. Порог 48 унаследован, не выдаётся за эмпирически откалиброванный; конкуренция остаётся высокой.

В таблице рекомендуем первым Noon/Extra из-за короткой сдачи, хотя у магазина чуть выше суммарный score. Резерв не является READY: каждый требует своей QA и новой live-проверки. В частности, Wayfair сейчас не проходит экономику автоматического bid; гостиницы имеют широкий глобальный объём, остальные резервные задачи требуют уточнения scope/навыков.

[Официальные комиссии Freelancer](https://www.freelancer.com/feesandcharges): fixed-price 10% или минимум $5, hourly 10%. Расчёты выше относятся к комиссии платформы; налоги, вывод, конвертация и расходы клиентских сервисов не включены. Перед реальной подачей нужна повторная проверка страницы и требований аккаунта: официальный fee page указывает минимальный доступный баланс $20; доступные bids и баланс здесь не проверены.

Локальный журнал `gig_submissions.sqlite`: ровно три READY; actual_bid, actual_delivery, submitted_at и bids_at_submission отсутствуют. Реальная отправка, ответы, интервью, awards и revenue равны нулю. Доли без отправок математически undefined, это не прогноз и не успешная воронка.

Воспроизведение: `.venv\Scripts\python.exe -m ai_assistant.gig.first_money_run --limit 100 --output artifacts/p6`; для текущего сбора и явных оценок — дополнительно `--resume-collection --review-file artifacts/p6/scope_reviews.json`. Генератор создаёт предварительную очередь; финальный PASS требует отдельной ручной QA, не следует автоматически из score.

Доказательства: `artifacts/p6/final_run.json`, `manual_qa_observations.json`, `qa_review.json`, `reviewed_packs.json`, `scope_reviews.json`, `funnel_metrics.json`, `p5_preservation.json`; HTML обеих проверок сохранён отдельно. QA FAIL для ложных совпадений записан в qa_review.json; они исключены из готовой очереди.

## Регрессия и классификация failures

| Проверка | До изменений P6 | Финальный результат |
|---|---|---|
| `pytest tests/gig -q` | 498 passed, 1 failed | **533 passed**, 0 failed, 121.21 s |
| `pytest tests -q` | 3512 passed, 3 failed | **3547 passed, 2 failed**, 671.08 s |
| Дополнительные live/API/gate probes | — | 34 passed, 1.66 s |

- **EXISTING / RELATED, исправлено:** `tests/gig/test_first_money.py::TestStaleCannotBeApplyFirst::test_live_verified_flag_is_recorded` до изменений сравнивал ISO string с datetime. Приведён к уже существующему сериализованному контракту; текущая Gig-регрессия проходит.
- **EXISTING / UNRELATED, осталось:** `tests/test_candidate_master_resume.py::test_a_project_sync_does_not_touch_resume_or_interview_claims`: baseline и финальный запуск фиксируют изменение metadata/source_sha256 project claim при sync. Модули candidate/master_resume этим P6 не менялись.
- **EXISTING / UNRELATED, осталось:** `tests/test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches`: baseline и финальный запуск показывают те же 75 flagged references при лимите 18 в `docs/ble001_triage.md`. Этот документ и checker этим P6 не менялись.
- **NEW:** 0 измеренных failures. Полный набор не объявляется зелёным: два перечисленных существующих падения остаются.

Логи baseline: `artifacts/p6_baseline_gig.txt`, `artifacts/p6_baseline_full.txt`; финальные: `artifacts/p6/gig_release.txt`, `artifacts/p6/full_release.txt`. Собственный patch относительно pre-edit checkpoint: `artifacts/p6/implementation_delta.patch`. Сохранение P5 проверено побайтово для action.py, scoring.py, currency.py, dedup.py, assessment.py; исходные пользовательские изменения сохранены, отправка заявок и production recovery не выполнялись.
