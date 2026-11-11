# REPITON

REPITON — AI-тренер произношения. Он учит говорить так, чтобы носитель языка правильно понял слово, а не просто подтверждает, что Speech-to-Text угадал текст.

Первая проверяемая версия работает только с тайскими словами и короткими фразами. Основной цикл:

`Listen → Repeat → Analyze → Explain → Retry → Master`

## Статус

Этап **Foundation / MVP Design** завершён. Stage 1 offline feasibility harness реализован как исполняемый baseline. После внешней проверки недостаточное покрытие было связано с абсолютным порогом громкости. Gain-invariant quality rule заморозили без подбора численного порога и один раз проверили на финальных 1 218 записях шести новых дикторов. Coverage вырос до `0.902`, но tone macro-F1 упал до `0.702`, а low-tone recall — до `0.318`. Stage 1 остаётся **NO-GO**: текущая модель неустойчива между дикторами, а learner/error gold evidence отсутствует.

## Принципы

- ASR-попадание не равно правильному произношению.
- Измеримые акустические признаки отделены от правил оценки и текста объяснения.
- LLM не слушает аудио и не придумывает фонетический диагноз.
- Система умеет отказаться от оценки, если запись или измерение ненадёжны.
- Порог `Master` калибруется на данных; `90` — стартовая продуктовая гипотеза, не доказанный стандарт.
- Языково-независимый core не содержит тайских правил.

## Документация

- [Проект и решения](PROJECT.md)
- [Архитектура](docs/architecture.md)
- [Pronunciation Engine](docs/pronunciation-engine.md)
- [Тайская фонетика и тон](docs/thai-pronunciation.md)
- [Модель данных](docs/data-model.md)
- [MVP, UX и acceptance criteria](docs/mvp.md)
- [Roadmap](docs/roadmap.md)
- [Аудит технологий](docs/technology-audit.md)
- [Статус Stage 1](docs/stage-1-status.md)
- [Протокол сбора данных](docs/data-collection-protocol.md)
- [Чек-лист рецензирования манифеста](docs/manifest-review-guide.md)
- [ADR](docs/decisions/)
- [Train-only model selection](docs/decisions/0005-nested-speaker-model-selection.md)
- [Reject regularized LDA](docs/decisions/0006-reject-regularized-lda.md)
- [Tone interval hypothesis](docs/decisions/0007-tone-bearing-rhyme-interval.md)
- [Seal Stage 1 NO-GO Checkpoint](docs/decisions/0008-seal-stage-1-no-go.md)
- [Post-Stage-1 Product Prototype Governance](docs/adr/0009-post-stage-1-product-prototype-governance.md)
- [Voiced-rhyme experiment](docs/experiments/2026-09-08-voiced-rhyme.md)
- [External-speaker holdout](docs/experiments/2026-09-08-external-holdout.md)
- [Sealed-final evaluation](docs/experiments/2026-09-08-sealed-final.md)
- [Stage 1B Evidence Package Protocol](docs/experiments/2026-09-08-stage-1b-evidence-package.md)
- [Stage 1B.1 Data Source Search & Decision](docs/experiments/2026-09-08-fresh-data-source-search.md)
- [Stage 1B.2 Data Acquisition Launch Package](docs/data-acquisition/README.md)
- [Stage 1B.4 Free Development Dataset Provenance](docs/experiments/2026-09-08-free-development-dataset.md)
- [Stage 1C.0 Free Low-vs-Mid Acoustic Exploration](docs/experiments/2026-09-08-free-low-mid-acoustic-exploration.md)
- [Stage 1C.1 Controlled Low/Mid Development Pre-Registration](docs/experiments/2026-09-08-stage-1c1-preregistration.md)
- [Stage 1C.2 Boundary Trimming Evaluation](docs/experiments/2026-09-08-stage-1c2-boundary-trimming.md)
- [Stage 1C.3 Interior F0 Tracking Investigation](docs/experiments/2026-09-08-stage-1c3-interior-f0-artifacts.md)
- [Stage 1C.4 Pitch Tracker Parameter Evaluation](docs/experiments/2026-09-08-stage-1c4-pitch-tracker-parameters.md)
- [Stage 1C.5 Matched Low-vs-Mid Acoustic Separability](docs/experiments/2026-09-08-stage-1c5-matched-low-mid.md)
- [Stage 1C.6 Contour Range Feature Evaluation](docs/experiments/2026-09-08-stage-1c6-contour-range-feature.md)
- [Stage 1C Pre-registration Template](docs/experiments/stage-1c-preregistration-template.md)

## Локальный запуск

```powershell
uv sync --extra dev --python 3.11
uv run pytest
uv run python examples/synthetic/generate.py
uv run repiton analyze-audio examples/synthetic/rising.wav --interval-start 0.05 --interval-end 0.75 --speaker-baseline-hz 150
uv run repiton audit-references data/external/ccost/ccost-mvp-30.csv --clips-root data/external/ccost --corpus-root data/external/ccost/extracted/C-COST --json artifacts/ccost-reference-audit.json --html artifacts/ccost-reference-audit.html
uv run repiton audit-references data/external/ccost/ccost-clean-references.csv --clips-root data/external/ccost --corpus-root data/external/ccost/extracted/C-COST --json artifacts/ccost-clean-reference-audit.json --html artifacts/ccost-clean-reference-audit.html
uv run repiton audit-references data/external/ccost/ccost-balanced-references.csv --clips-root data/external/ccost --corpus-root data/external/ccost/extracted/C-COST --measurements-cache artifacts/ccost-balanced-measurements.json --json artifacts/ccost-balanced-reference-audit.json --html artifacts/ccost-balanced-reference-audit.html
uv run repiton analyze-reference-errors artifacts/ccost-balanced-reference-audit.json --references data/external/ccost/ccost-balanced-references.csv --json artifacts/ccost-tone-error-analysis.json --markdown artifacts/ccost-tone-error-analysis.md
uv run repiton audit-references data/external/ccost/ccost-balanced-references.csv --clips-root data/external/ccost --corpus-root data/external/ccost/extracted/C-COST --tone-interval voiced_rhyme --measurements-cache artifacts/ccost-balanced-rhyme-measurements.json --json artifacts/ccost-rhyme-reference-audit.json --html artifacts/ccost-rhyme-reference-audit.html
uv run repiton evaluate-external-holdout data/external/ccost/ccost-external-validation-references.csv --development-csv data/external/ccost/ccost-balanced-references.csv --development-measurements-cache artifacts/ccost-balanced-rhyme-measurements.json --validation-clips-root data/external/ccost --corpus-root data/external/ccost/extracted/C-COST --measurements-cache artifacts/ccost-external-validation-rhyme-measurements.json --json artifacts/ccost-external-validation-audit.json --html artifacts/ccost-external-validation-audit.html

# Однократный sealed-final run (зафиксирован в artifacts/, повторный запуск для тюнинга ЗАПРЕЩЁН):
uv run repiton evaluate-sealed-final data/external/ccost/ccost-sealed-final-references.csv --evaluation-plan artifacts/sealed-final-evaluation-plan.json --development-csv data/external/ccost/ccost-balanced-references.csv --development-measurements-cache artifacts/ccost-balanced-rhyme-gain-invariant-measurements.json --sealed-final-clips-root data/external/ccost --corpus-root data/external/ccost/extracted/C-COST --config config/gain-invariant-v1.json --measurements-cache artifacts/ccost-sealed-final-rhyme-gain-invariant-measurements.json --json artifacts/ccost-sealed-final-audit.json --html artifacts/ccost-sealed-final-audit.html
```

Синтетический пример проверяет только работу кода и не является речевым evidence.
Reference audit также является exploratory consistency check, а не gold evaluation.

## Следующий незавершённый slice

Чекпоинт Stage 1 официально запечатан как **STAGE_1_FINAL_NO_GO** (ADR-0008, Final Stage-1 Gate). Stage 2 **NOT AUTHORIZED**.

В соответствии с ADR-0009 архитектура разделена на два трека:
- **TRACK A — PRODUCT / UX PROTOTYPE**: `ACTIVE` — Milestone: `PRODUCT PROTOTYPE P5 — FREE HTTPS DEPLOYMENT & REAL-DEVICE INSTALLATION GATE` (публичный PWA-прототип: `https://repiton-practice.surge.sh/` на базе статического экспорта и неавторитетного мок-скорера без анализа речи учащегося).
- **TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO` (требует L2 learner speech с независимой native gold-разметкой).

## Product Web Prototype (Track A)

### Local Development
```powershell
cd product/web
npm ci
npm run dev
```

### Local Production Build & Static Server
```powershell
cd product/web
npm run typecheck
npm test
npm run build
npm run start
```

### Local Offline Real-Browser E2E
```powershell
cd product/web
npm run test:e2e
```

### Deployed Production E2E
```powershell
cd product/web
cmd /c "set REPITON_BASE_URL=https://repiton-practice.surge.sh&& npm run test:e2e"
```

### Deployment / Redeploy
```powershell
cd product/web
npm run build
npx surge out repiton-practice.surge.sh
```

