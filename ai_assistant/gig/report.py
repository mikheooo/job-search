"""Генератор отчёта P5 (ТЗ §7).

Главная цель отчёта — чтобы после его чтения можно было сразу решить, куда подаваться,
а не анализировать ещё одну техническую таблицу. Поэтому для APPLY_NOW выводятся блоки
WHY / RISKS / WORK / MONEY, а не только числа.

Модуль только формирует текст; решения принимает ``action.build_queue``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .action import ActionQueue, QueueEntry, build_queue
from .assessment import is_non_it_domain
from .models import BudgetType, CanDeliver, Gig

#: Что реально нужно сделать — по типу задачи. Это подсказка для оценки объёма, а не
#: обещание: формулировки взяты из того, что уже видно в навыках заказа.
WORK_BY_SIGNAL: tuple[tuple[tuple[str, ...], str], ...] = (
    (("scraping", "parser", "парсинг", "scrape"), "Собрать данные: парсер + нормализация"),
    (("api", "rest api", "restful", "webhook"), "Интеграция по API: авторизация, вызовы, обработка ошибок"),
    (("telegram", "bot", "бот"), "Telegram-бот: команды, очередь задач, отчёты"),
    (("excel", "google sheets", "csv", "data entry"), "Обработка таблиц: импорт, чистка, выгрузка"),
    (("automation", "workflow", "n8n", "zapier", "make.com"), "Автоматизация процесса: триггеры, ветвления, логи"),
    (("ai", "llm", "openai", "chatgpt", "gpt"), "LLM-часть: промпты, структурированный вывод, проверка"),
    (("postgresql", "mysql", "sql", "database"), "Схема БД и запросы: миграции, индексы"),
    (("django", "flask", "fastapi"), "Backend: модели, эндпоинты, тесты"),
)

DEFAULT_WORK = "Разобрать требования, собрать MVP, показать результат"


@dataclass
class ReportData:
    """Всё, что нужно для отчёта, собранное заранее."""

    queue: ActionQueue
    total_scored: int
    by_deliverability: dict[str, int]
    bids_distribution: dict[str, int]
    age_distribution: dict[str, int]
    rate_distribution: dict[str, int]
    top_by_score: list[Gig]
    top_by_action: list[tuple[float, Gig]]
    overlap: int


def _work_hint(gig: Gig) -> list[str]:
    from .grounded import analyze, work_hints
    return work_hints(analyze(gig.description, gig.title))


def _money_lines(entry: QueueEntry) -> list[str]:
    gig = entry.gig
    sig = entry.action.signals
    lines: list[str] = []
    if gig.budget_min is not None or gig.budget_max is not None:
        lines.append(f"бюджет: {gig.budget_text}"
                     + (f" ({gig.currency})" if gig.currency else ""))
    else:
        lines.append("бюджет: в источнике не опубликован")
    if sig.get("effort_hours"):
        lines.append(f"оценка трудоёмкости: {sig['effort_hours']:g} ч (эвристика)")
    rate = sig.get("effective_rate_usd")
    lines.append(f"эффективная ставка: ${rate}/ч" if rate is not None
                 else "эффективная ставка: не определена (нет курса или трудоёмкости)")
    if gig.budget_type is BudgetType.HOURLY:
        lines.append("тип: почасовая — ставка берётся как есть")
    return lines


def _why_lines(entry: QueueEntry) -> list[str]:
    gig = entry.gig
    action = entry.action
    why: list[str] = []
    if gig.match_score is not None:
        why.append(f"совпадение навыков: {gig.match_score:g}/100")
    why.append(f"выполнимость: {gig.can_deliver.value}")
    comp = action.signals.get("competition")
    bids = action.signals.get("bids")
    if bids is not None:
        why.append(f"конкуренция {comp}: {bids} ставок")
    else:
        why.append("конкуренция: источник не публикует число ставок")
    fresh = action.signals.get("freshness")
    age = action.signals.get("age_hours")
    if age is not None:
        why.append(f"свежесть {fresh}: {age:g} ч с публикации")
    via = action.signals.get("found_via") or []
    if via:
        why.append(f"найден по направлениям: {', '.join(via)}")
    if gig.effective_hourly_rate is not None:
        why.append(f"ставка ${gig.effective_hourly_rate:g}/ч после конверсии")
    return why


def _risk_lines(entry: QueueEntry) -> list[str]:
    gig = entry.gig
    sig = entry.action.signals
    risks: list[str] = list(gig.risks or [])
    if sig.get("rate_reliable") is False or sig.get("effective_rate_usd") is None:
        risks.append("ставка не подтверждена — прибыльность под вопросом")
    if (sig.get("bids") or 0) > 25:
        risks.append(f"ставок уже {sig['bids']} — отклик может уйти вниз списка")
    if sig.get("age_hours") is not None and sig["age_hours"] > 24:
        risks.append(f"объявление висит {sig['age_hours']:g} ч")
    if gig.client_rating is None and not gig.client_name:
        risks.append("данных о клиенте нет (историю и рейтинг источник не публикует)")
    return risks or ["существенных рисков не найдено"]


def render_entry(entry: QueueEntry, index: int) -> str:
    gig = entry.gig
    sig = entry.action.signals
    age = f"{sig['age_hours']:g} ч" if sig.get("age_hours") is not None else "?"
    rate = f"${sig['effective_rate_usd']}/ч" if sig.get("effective_rate_usd") is not None else "?"
    bids = sig.get("bids") if sig.get("bids") is not None else "?"
    lines = [
        f"### {index}. {gig.title}",
        "",
        f"- **action_score**: {entry.action.action_score}",
        f"- **источник**: {gig.source} ({gig.source_scope})",
        f"- **бюджет/ставка**: {gig.budget_text} · эффективно {rate}",
        f"- **ставок**: {bids} · **возраст**: {age} · **выполнимость**: {gig.can_deliver.value}",
        f"- **ссылка**: {gig.url or '(источник не публикует отдельную страницу)'}",
        "",
        "**WHY**",
    ]
    lines += [f"- {w}" for w in _why_lines(entry)]
    lines += ["", "**RISKS**"]
    lines += [f"- {r}" for r in _risk_lines(entry)]
    lines += ["", "**WORK**"]
    lines += [f"- {w}" for w in _work_hint(gig)]
    lines += ["", "**MONEY**"]
    lines += [f"- {m}" for m in _money_lines(entry)]
    lines += ["", f"**DECISION**: {entry.decision}", ""]
    return "\n".join(lines)


def _table(queue: ActionQueue) -> str:
    head = ("| # | action_score | source | project | budget/rate | bids | age | can_deliver |\n"
            "| -: | ---: | :--- | :--- | :--- | ---: | ---: | :--- |")
    rows = [head]
    for i, e in enumerate(queue.apply_now, 1):
        sig = e.action.signals
        age = f"{sig['age_hours']:g} ч" if sig.get("age_hours") is not None else "?"
        rate = f"${sig['effective_rate_usd']}/ч" if sig.get("effective_rate_usd") is not None else "?"
        bids = sig.get("bids") if sig.get("bids") is not None else "?"
        budget = e.gig.budget_text or "?"
        rows.append(f"| {i} | {e.action.action_score} | {e.gig.source} | "
                    f"{e.gig.title[:60]} | {budget} · {rate} | {bids} | {age} | "
                    f"{e.gig.can_deliver.value} |")
    return "\n".join(rows)


def render_report(data: ReportData) -> str:
    q = data.queue
    out: list[str] = []
    out.append("# P5 — куда подаваться сегодня\n")
    out.append(f"Собрано и оценено заказов: **{data.total_scored}**.\n")
    out.append("## Очередь действий\n")
    out.append(f"- **APPLY_NOW**: {len(q.apply_now)}")
    out.append(f"- **MAYBE**: {len(q.maybe)}")
    out.append(f"- **SKIP**: {len(q.skip)}\n")

    out.append("## APPLY_NOW — подаваться сейчас\n")
    if q.apply_now:
        out.append(_table(q))
        out.append("")
        for i, e in enumerate(q.apply_now, 1):
            out.append(render_entry(e, i))
    else:
        out.append("_Пусто: подходящих заказов в этой выдаче нет._\n")

    out.append("## MAYBE — рассмотреть, но есть недостатки\n")
    if q.maybe:
        out.append("| # | action_score | project | причины сомнения |")
        out.append("| -: | ---: | :--- | :--- |")
        for i, e in enumerate(q.maybe, 1):
            reasons = ", ".join(e.skip_reasons) or "—"
            out.append(f"| {i} | {e.action.action_score} | {e.gig.title[:56]} | {reasons} |")
    else:
        out.append("_Пусто._")
    out.append("")

    out.append("## SKIP — не тратить время\n")
    out.append(f"Всего {len(q.skip)}; причины по категориям:\n")
    counts: dict[str, int] = {}
    for e in q.skip:
        for r in e.skip_reasons or ["(без причины)"]:
            counts[r] = counts.get(r, 0) + 1
    out.append("| причина | заказов |")
    out.append("| :--- | ---: |")
    for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append(f"| {reason} | {n} |")
    out.append("")

    out.append("## Распределения\n")
    for name, dist in (
        ("по выполнимости", data.by_deliverability),
        ("по числу ставок", data.bids_distribution),
        ("по возрасту объявления", data.age_distribution),
        ("по эффективной ставке USD", data.rate_distribution),
    ):
        out.append(f"**{name}**\n")
        out.append("| значение | заказов |")
        out.append("| :--- | ---: |")
        for k, v in dist.items():
            out.append(f"| {k} | {v} |")
        out.append("")

    out.append("## Сравнение: обычный score против action_score\n")
    out.append(f"Пересечение топ-15 по двум метрикам: **{data.overlap} из 15**.\n")
    out.append("**Топ-15 по opportunity_score**\n")
    out.append("| # | score | can_deliver | bids | project |")
    out.append("| -: | ---: | :--- | ---: | :--- |")
    for i, g in enumerate(data.top_by_score, 1):
        out.append(f"| {i} | {g.opportunity_score} | {g.can_deliver.value} | "
                   f"{g.competition if g.competition is not None else '?'} | {g.title[:56]} |")
    out.append("")
    out.append("**Топ-15 по action_score**\n")
    out.append("| # | action | old | can_deliver | bids | project |")
    out.append("| -: | ---: | ---: | :--- | ---: | :--- |")
    for i, (score, g) in enumerate(data.top_by_action, 1):
        out.append(f"| {i} | {score} | {g.opportunity_score} | {g.can_deliver.value} | "
                   f"{g.competition if g.competition is not None else '?'} | {g.title[:52]} |")
    out.append("")
    return "\n".join(out)


def _competition_range(band: str) -> str:
    """Диапазон ставок для полосы — чтобы подпись была понятной без кода."""
    ranges = {
        "excellent": "0–10",
        "good": "11–25",
        "medium": "26–50",
        "hard": "51–100",
        "very_hard": "100+",
        "unknown": "нет данных",
    }
    return ranges.get(band, "?")


def collect_report_data(gigs: list[Gig], *, now: Any = None) -> ReportData:
    """Собирает все данные отчёта, включая оба топ-15 и распределения."""
    from .money import competition_band, freshness_band

    queue = build_queue(gigs, now=now)

    by_deliver: dict[str, int] = {}
    bids_dist: dict[str, int] = {}
    age_dist: dict[str, int] = {}
    rate_dist: dict[str, int] = {}

    def bump(d: dict[str, int], key: str) -> None:
        d[key] = d.get(key, 0) + 1

    for g in gigs:
        bump(by_deliver, g.can_deliver.value)
        # Полосы, а не отдельные значения: иначе 423 заказа дают полусотню строк,
        # и распределение перестаёт читаться.
        band = competition_band(g.competition)
        label = band if band == "unknown" else f"{band} ({_competition_range(band)})"
        bump(bids_dist, label)
        bump(age_dist, freshness_band(g.published_at, now=now))
        rate = g.effective_hourly_rate
        if rate is None:
            bump(rate_dist, "неизвестна")
        elif rate < 2:
            bump(rate_dist, "< $2/ч")
        elif rate < 5:
            bump(rate_dist, "$2–5/ч")
        elif rate < 15:
            bump(rate_dist, "$5–15/ч")
        elif rate < 40:
            bump(rate_dist, "$15–40/ч")
        else:
            bump(rate_dist, "$40+/ч")

    top_score = sorted(gigs, key=lambda g: -(g.opportunity_score or 0))[:15]
    scored = [(action_score_of_entry(g, now), g) for g in gigs]
    top_action = sorted(scored, key=lambda t: -t[0])[:15]
    overlap = len({g.id for g in top_score} & {g.id for _, g in top_action})

    return ReportData(
        queue=queue,
        total_scored=len(gigs),
        by_deliverability=dict(sorted(by_deliver.items(), key=lambda kv: -kv[1])),
        # Порядок полос — смысловой (от лучшего к худшему), а не алфавитный:
        # так распределение читается сразу, без сопоставления подписей.
        bids_distribution={k: bids_dist[k] for k in (
            "excellent (0–10)", "good (11–25)", "medium (26–50)",
            "hard (51–100)", "very_hard (100+)", "unknown",
        ) if k in bids_dist},
        age_distribution={k: age_dist[k] for k in (
            "under_1h", "under_6h", "under_24h", "1_3_days", "over_3_days", "unknown",
        ) if k in age_dist},
        rate_distribution={k: rate_dist[k] for k in (
            "< $2/ч", "$2–5/ч", "$5–15/ч", "$15–40/ч", "$40+/ч", "неизвестна",
        ) if k in rate_dist},
        top_by_score=top_score,
        top_by_action=top_action,
        overlap=overlap,
    )


def action_score_of_entry(gig: Gig, now: Any) -> float:
    from .action import action_score_of
    return action_score_of(gig, now=now).action_score


__all__ = [
    "ReportData",
    "collect_report_data",
    "render_entry",
    "render_report",
]
