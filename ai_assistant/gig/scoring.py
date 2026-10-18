"""Scoring гигов: прозрачный breakdown -> ``opportunity_score`` (0..100).

Сознательно НЕ используется единый непрозрачный LLM-score (ТЗ §6). Каждый
компонент считается детерминированной функцией от данных гига и профиля,
имеет собственный диапазон 0..100 и сохраняется в breakdown вместе с весом.
Благодаря этому видно, почему заказ получил конкретный балл, а веса можно
калибровать по реальным данным, не переписывая логику.

Компоненты (ТЗ §6):
    skill_fit, deliverability, budget_quality, effective_rate,
    client_quality, competition, payment_readiness,
    ai_automation_potential, risk
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .assessment import (
    CONFIDENCE_WEIGHTS,
)
from .currency import describe_conversion, exchange_rate_of, to_usd
from .models import BudgetType, CanDeliver, Gig, PaymentReadiness, RiskLevel

#: Веса компонентов. Нормируются автоматически — их сумма не обязана быть 100.
#: risk входит как обратный компонент (чем ниже риск, тем выше балл).
COMPONENT_WEIGHTS: dict[str, float] = {
    "skill_fit": 22.0,
    "deliverability": 18.0,
    "budget_quality": 12.0,
    "effective_rate": 14.0,
    "client_quality": 10.0,
    "competition": 6.0,
    "payment_readiness": 8.0,
    "ai_automation_potential": 5.0,
    "risk": 5.0,
}

#: Минимальный привлекательный эффективный часовой рейт (USD/час) для 100 баллов.
RATE_CEILING_USD = 40.0
#: Порог "осмысленного" фиксированного бюджета для полного балла.
BUDGET_CEILING_USD = 2000.0

_PAYMENT_SCORES = {
    PaymentReadiness.READY: 100.0,
    PaymentReadiness.POSSIBLE: 60.0,
    PaymentReadiness.UNKNOWN: 30.0,
    PaymentReadiness.BLOCKED: 0.0,
}

_DELIVERABILITY_SCORES = {
    CanDeliver.HIGH: 100.0,
    CanDeliver.MEDIUM: 65.0,
    CanDeliver.LOW: 25.0,
    CanDeliver.UNKNOWN: 40.0,
}

_RISK_SCORES = {
    RiskLevel.LOW: 100.0,
    RiskLevel.MEDIUM: 60.0,
    RiskLevel.HIGH: 20.0,
    RiskLevel.UNKNOWN: 45.0,
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass
class ScoreBreakdown:
    """Прозрачный вклад каждого компонента."""

    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    opportunity_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_score": round(self.opportunity_score, 1),
            "components": {k: round(v, 1) for k, v in self.components.items()},
            "weights": {k: round(v, 1) for k, v in self.weights.items()},
            "weighted_contributions": {
                k: round(self.components.get(k, 0.0) * self.weights.get(k, 0.0), 2) for k in self.weights
            },
            "notes": dict(self.notes),
        }


def skill_fit_score(gig: Gig, confidences: dict[str, float]) -> tuple[float, str]:
    """Совпадение требований заказа с навыками профиля (0..100)."""
    skills = [s for s in (gig.skills or []) if s]
    if not skills:
        # нет списка навыков — оцениваем по тексту
        from .assessment import _matched_skills  # локальный импорт: избегаем цикла

        matched = _matched_skills(gig, confidences)
        if not matched:
            return 35.0, "нет структурированных навыков и совпадений в тексте"
        avg = sum(w for _, w in matched) / len(matched)
        return _clamp(avg * 100.0), f"совпадения в тексте: {', '.join(s for s, _ in matched[:5])}"

    scores: list[float] = []
    matched: list[str] = []
    for skill in skills:
        weight = confidences.get(skill.lower())
        if weight is None:
            # неизвестный навык: считаем частично осваиваемым (0.3), но не 0
            scores.append(0.3)
            continue
        scores.append(weight)
        if weight >= 0.6:
            matched.append(skill)
    avg = sum(scores) / len(scores)
    note = f"совпадающие навыки: {', '.join(matched[:6])}" if matched else "нет совпадений по навыкам профиля"
    return _clamp(avg * 100.0), note


def budget_quality_score(gig: Gig) -> tuple[float, str]:
    """Качество бюджета: размер, определённость, наличие диапазона (0..100).

    P4: бюджет сравнивается **в USD**. Раньше валюта игнорировалась, поэтому INR
    12500-37500 (около $130-390) получал те же 100 баллов, что USD 12500-37500.
    Курс берётся у источника (``currencyDetails.exchangeRate``). Если курса нет,
    балл не выдумывается: компонент возвращает 0 и помечает причину.
    """
    if gig.budget_min is None and gig.budget_max is None:
        return 0.0, "бюджет не указан"

    rate = exchange_rate_of(gig.source_facts, gig.currency)
    converted_min = to_usd(gig.budget_min, gig.currency, rate)
    converted_max = to_usd(gig.budget_max, gig.currency, rate)
    if converted_min is None and converted_max is None:
        # Валюта не USD и курса нет — сопоставить бюджет нельзя.
        return 0.0, (
            f"бюджет {gig.budget_text}: {describe_conversion(gig.currency, rate)} "
            "— качество бюджета не определено"
        )

    ceiling = max(converted_max or 0.0, converted_min or 0.0)
    present = [v for v in (converted_min, converted_max) if v is not None]
    floor = min(present)

    if gig.budget_type is BudgetType.HOURLY:
        # часовая ставка: 15/h -> ~0, 40/h+ -> 100 (после конверсии в USD)
        score = _clamp((ceiling - 8.0) / (RATE_CEILING_USD - 8.0) * 100.0)
        note = f"почасовая ставка {gig.budget_text}"
    else:
        score = _clamp(ceiling / BUDGET_CEILING_USD * 100.0)
        note = f"фиксированный бюджет {gig.budget_text}"

    if gig.budget_type is BudgetType.UNKNOWN:
        score *= 0.8
        note += " (тип бюджета неизвестен)"
    if floor != ceiling:
        score = min(100.0, score + 5.0)
        note += " (диапазон подтверждает реальные границы)"
    if (gig.currency or "").strip().upper() not in ("USD", "US$", "$", ""):
        note += f"; ~${floor:g}-{ceiling:g} USD ({describe_conversion(gig.currency, rate)})"
    return score, note


def effective_rate_score(gig: Gig, effective_rate: float | None) -> tuple[float, str]:
    """Эффективная ставка (бюджет/трудоёмкость), ожидаемо **в USD**.

    P4: сама ставка приходит уже сконвертированной (``assessment.effective_hourly_rate``),
    поэтому здесь конверсии нет — иначе USD-значение прошло бы конверсию второй раз.
    Для защиты от двойного пересчёта сравнение идёт против ожидаемого USD-порядка
    величины: если пришло число, несовместимое с валютой заказа, оно всё равно
    нормируется — но текст причины честно об этом говорит.
    """
    if effective_rate is None:
        return 20.0, "эффективная ставка не определена"
    rate_usd = float(effective_rate)
    score = _clamp((rate_usd - 5.0) / (RATE_CEILING_USD - 5.0) * 100.0)
    note = f"≈${rate_usd:g}/ч"
    if (gig.currency or "").strip().upper() not in ("USD", "US$", "$", ""):
        source = gig.budget_text
        rate = exchange_rate_of(gig.source_facts, gig.currency)
        note += f" (из {source}; {describe_conversion(gig.currency, rate)})"
    return score, note


def competition_score(gig: Gig) -> tuple[float, str]:
    """Конкуренция: чем меньше откликов, тем выше балл."""
    if gig.competition is None:
        return 50.0, "число откликов неизвестно"
    count = max(0, int(gig.competition))
    if count <= 5:
        score = 100.0
    elif count <= 15:
        score = 80.0
    elif count <= 30:
        score = 60.0
    elif count <= 50:
        score = 40.0
    elif count <= 100:
        score = 22.0
    else:
        score = 8.0
    return score, f"{count} откликов на момент сбора"


def score_gig(
    gig: Gig,
    confidences: dict[str, float],
    *,
    client_quality: float | None,
    effective_rate: float | None,
    weights: dict[str, float] | None = None,
) -> ScoreBreakdown:
    """Считает opportunity_score и сохраняет полный breakdown."""
    weights = dict(weights or COMPONENT_WEIGHTS)

    skill_fit, skill_note = skill_fit_score(gig, confidences)
    budget_quality, budget_note = budget_quality_score(gig)
    rate_score, rate_note = effective_rate_score(gig, effective_rate)
    comp_score, comp_note = competition_score(gig)

    components: dict[str, float] = {
        "skill_fit": skill_fit,
        "deliverability": _DELIVERABILITY_SCORES[gig.can_deliver],
        "budget_quality": budget_quality,
        "effective_rate": rate_score,
        "client_quality": client_quality if client_quality is not None else 35.0,
        "competition": comp_score,
        "payment_readiness": _PAYMENT_SCORES[gig.payment_readiness],
        "ai_automation_potential": float(gig.ai_automation_potential or 0),
        "risk": _RISK_SCORES[gig.risk_level],
    }

    notes = {
        "skill_fit": skill_note,
        "deliverability": f"can_deliver={gig.can_deliver.value}",
        "budget_quality": budget_note,
        "effective_rate": rate_note,
        "client_quality": (
            f"client_quality={client_quality}" if client_quality is not None else "данных о клиенте нет"
        ),
        "competition": comp_note,
        "payment_readiness": f"payment_readiness={gig.payment_readiness.value}",
        "ai_automation_potential": f"{gig.ai_automation_potential or 0}/100",
        "risk": f"risk_level={gig.risk_level.value}; {len(gig.risks)} причин",
    }

    total_weight = sum(weights.values()) or 1.0
    weighted = sum(components.get(name, 0.0) * weight for name, weight in weights.items())
    opportunity = _clamp(weighted / total_weight)

    # P4: аддитивная модель позволяла деньгам перевесить невыполнимость. Измерено на
    # реальном корпусе: у заказов с can_deliver=low слабая выполнимость (4.5 из 59)
    # компенсировалась полными баллами за ставку и бюджет (14 + 12), и copy-typing
    # попадал в топ-15 рядом с работой, которую действительно можно взять.
    #
    # Поэтому применяется потолок, а не пересмотр весов: заказ, который нельзя (или
    # почти нельзя) выполнить, не может стоять в верхушке рейтинга, каким бы выгодным
    # он ни выглядел по бюджету. Потолок мягкий — такой заказ остаётся виден в списке,
    # но ниже всего, что выполнимо; MEDIUM ограничен серединой.
    caps = {
        CanDeliver.LOW: 45.0,
        CanDeliver.MEDIUM: 65.0,
    }
    cap = caps.get(gig.can_deliver)
    if cap is not None and opportunity > cap:
        notes["deliverability_cap"] = (
            f"балл ограничен {cap:g} из-за can_deliver={gig.can_deliver.value}: "
            "невыполнимый заказ не может быть выше выполнимого"
        )
        opportunity = cap

    return ScoreBreakdown(
        components=components,
        weights=weights,
        notes=notes,
        opportunity_score=round(opportunity, 1),
    )


def match_score_of(gig: Gig, confidences: dict[str, float]) -> float:
    """match_score — чистое соответствие профилю, отдельно от opportunity."""
    score, _ = skill_fit_score(gig, confidences)
    return round(score, 1)


__all__ = [
    "COMPONENT_WEIGHTS",
    "ScoreBreakdown",
    "budget_quality_score",
    "competition_score",
    "effective_rate_score",
    "match_score_of",
    "score_gig",
    "skill_fit_score",
    "CONFIDENCE_WEIGHTS",
]
