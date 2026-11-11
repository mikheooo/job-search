# REPITON — Project Foundation

Дата фиксации: 2026-09-07.

## Product thesis

REPITON помогает пользователю исправить конкретную причину непонятного произношения. Для первого MVP это тайский lexical tone, долгота гласной и грубые сегментные ошибки в ограниченном словаре.

Ценность будет доказана только если система одновременно:

1. различает правильное слово и правильное произношение;
2. находит тип ошибки по измеряемым признакам;
3. объясняет исправление понятным языком;
4. показывает улучшение на повторной попытке;
5. согласуется с оценкой носителей на заранее размеченном тесте.

## Scope

В MVP входят:

- 20–50 вручную проверенных тайских слов;
- все пять тонов, короткие/долгие гласные и полезные минимальные/почти минимальные пары;
- эталонные записи нескольких носителей, а не единственный «идеальный голос»;
- запись, воспроизведение, два pitch contour и выделение отклонения;
- ASR-проверка произнесённого слова;
- границы слова/слога/фонем, F0, длительность и ограниченная сегментная диагностика;
- детерминированное решение и структурированный результат;
- шаблонное объяснение по умолчанию; LLM-перефразирование только поверх фактов;
- история попыток и схема будущего профиля ошибок.

Не входят: курс, свободный разговор, несколько языков, социальные функции, подписки, marketplace, сложная gamification, собственная foundation model, автоматическое обучение на пользовательских данных.

## Feasibility verdict

**STAGE_1_FINAL_NO_GO.** По итогам однократного sealed-final эксперимента на 1 218 записях 6 независимых дикторов C-COST (macro-F1 = 0.702 < 0.80, low recall = 0.318 < 0.65) и независимой репликации на continuous speech FLEURS (1C.10 / 1C.10R) акустический скорер не показал надёжного обобщения. Продакшн-скорер не валидирован, авторитетная диагностика не авторизована, Stage 2 не авторизован (ADR-0008, Final Stage-1 Gate).

Дальнейшая работа разделена на два независимых трека (ADR-0009):
- **TRACK A — PRODUCT / UX PROTOTYPE**: `ACTIVE` — Milestone: `PRODUCT PROTOTYPE P0 — MVP FOUNDATION` (безопасный неавторитетный mock-прототип для валидации UX и продукта);
- **TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO` (требует L2 learner speech с экспертной gold-разметкой ошибок).

## Decisions

- Python-сервис и offline harness для сигнального/фонетического анализа.
- Praat filtered autocorrelation через Parselmouth как основной F0 extractor; второй extractor — только для проверки расхождений.
- Montreal Forced Aligner Thai как стартовый forced-aligner, но его границы валидируются вручную.
- PyThaiNLP G2P помогает подготовить кандидаты; произношение каждого MVP-слова утверждается носителем/лингвистом и хранится явно.
- Thai-specific правила реализуются как versioned language pack.
- Azure Pronunciation Assessment и Thai ASR допускаются как benchmark/identity signal, но не являются источником истины для tone.
- Сначала offline evidence, затем API и только затем UI.

Подробности: [ADR-0001](docs/decisions/0001-measurement-first-pipeline.md), [ADR-0002](docs/decisions/0002-thai-tone-comparison.md), [ADR-0003](docs/decisions/0003-prototype-stack.md), [ADR-0004](docs/decisions/0004-evaluation-before-productization.md), [ADR-0008](docs/decisions/0008-seal-stage-1-no-go.md), [ADR-0009](docs/adr/0009-post-stage-1-product-prototype-governance.md).

## Top risks

| Риск | Почему критичен | Как снимаем |
|---|---|---|
| Нет репрезентативного Thai L2 corpus | Нельзя честно калибровать ошибки учащихся | Собрать consented gold set; отдельные speaker-held-out splits |
| F0 tracking errors | Octave jumps/creaky voice выглядят как другой тон | quality gate, confidence mask, визуальный аудит, второй extractor |
| Один reference-speaker | Модель учит имитации голоса | multi-speaker distributions и speaker-relative normalization |
| Alignment ошибается на плохой речи | Неверный участок портит tone/length | confidence + abstain + ручной gold alignment |
| Тон зависит от контекста | Изолированные слова не переносятся на фразы | MVP маркирует режим; фразы — отдельный этап |
| Итоговый score скрывает причину | Пользователь не понимает, что исправлять | component scores + evidence + one actionable instruction |
| «Правильная форма» ≠ понятность | Акустическая близость может не отражать восприятие | native-listener intelligibility test |
| Облачный vendor drift/privacy | Поведение/цена/API меняются; аудио чувствительно | adapter boundary, local baseline, retention/consent policy |

## Recommended stack

| Слой | MVP выбор | Причина |
|---|---|---|
| Offline/engine | Python 3.12, NumPy, SciPy, pandas, soundfile | зрелый speech/science ecosystem |
| F0 | Praat/Parselmouth filtered autocorrelation | интерпретируемый исследовательский baseline |
| Alignment | MFA Thai model + curated lexicon | готовая Thai forced alignment база |
| G2P | curated lexicon, PyThaiNLP как помощник | G2P удобен, но не заменяет проверку словаря |
| ASR | adapter: Google/Azure/Whisper benchmark | Thai identity signal и сравнение вариантов |
| API после feasibility | FastAPI + Pydantic | строгие схемы и простой Python boundary |
| Client после feasibility | TypeScript, React/Next.js PWA, Web Audio | быстрый mobile-web prototype |
| Data после feasibility | PostgreSQL + object storage | versioned metadata отдельно от WAV |
| Observability | structured logs + OpenTelemetry | воспроизводимость версии pipeline |

## Open questions

- UNKNOWN: точность F0/aligner на целевой смеси устройств, возрастов и голосов.
- UNKNOWN: допустимые диапазоны каждого тона по структуре слога и контексту.
- UNKNOWN: вес segment/tone/length в общем score.
- UNKNOWN: надёжность Azure phoneme scores именно для Thai learner speech.
- UNKNOWN: объём данных, достаточный для персональной калибровки.
- Нужен тайский фонетист и минимум два независимых native raters для gold set.

