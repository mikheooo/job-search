"""Action Score и очередь действий (P5 §4, §6).

ЗАЧЕМ ОТДЕЛЬНЫЙ ПОКАЗАТЕЛЬ
--------------------------
``opportunity_score`` отвечает на вопрос «насколько проект хорош вообще». Для заработка
нужен другой вопрос: **разумно ли податься именно сейчас**. Разница не косметическая:

* старый заказ с идеальным бюджетом и 80 ставками — хороший проект, но подаваться поздно;
* свежий заказ со средним бюджетом и нулём ставок — шанс выше, хотя «качество» ниже.

Поэтому ``action_score`` считается отдельно, старый ``score`` не удаляется и не
переписывается: они отвечают на разные вопросы, и в отчёте видны оба.

ФОРМУЛА ПРОЗРАЧНАЯ, НЕ ML (ТЗ §4)
---------------------------------
Каждый вклад виден в breakdown. Никакой обученной модели: веса фиксированы в коде и
объясняются словами.

ЖЁСТКИЕ ПРАВИЛА (ТЗ §5)
-----------------------
* ``can_deliver=low`` не попадает в APPLY_NOW никогда;
* неизвестная ставка не превращается в число: она снижает уверенность;
* свежесть и конкуренция при отсутствии данных дают середину, а не максимум.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import CanDeliver, Gig, RiskLevel
from .money import money_signals
from .assessment import has_it_signal, is_non_it_domain

#: Веса вкладов Action Score. Нормируются автоматически.
ACTION_WEIGHTS: dict[str, float] = {
    "base_score": 30.0,
    "competition": 20.0,
    "freshness": 16.0,
    "rate": 18.0,
    "skill_fit": 10.0,
    "deliverability": 6.0,
}

#: Ниже этого Action Score заказ не рекомендуется к подаче.
APPLY_NOW_MIN_SCORE = 55.0
#: Ниже этого — тратить время не стоит.
MAYBE_MIN_SCORE = 35.0

#: Причины отказа в machine-readable виде (ТЗ §6).
SKIP_NON_IT_DOMAIN = "non-IT domain"
SKIP_CAN_DELIVER_LOW = "can_deliver low"
SKIP_RATE_TOO_LOW = "effective rate too low"
SKIP_EXCESSIVE_COMPETITION = "excessive competition"
SKIP_STALE = "posting too old"
SKIP_UNDELIVERABLE_REJECTED = "rejected by hard gates"
SKIP_LOW_RATE_UNKNOWN = "rate unknown"

#: Минимальная ставка USD/ч, при которой заказ имеет смысл для подачи.
MIN_VIABLE_RATE_USD = 5.0
#: Ставка ниже этого порога — прямая причина SKIP.
SKIP_RATE_USD = 2.0
#: Ставок больше этого — подаваться заведомо безнадёжно.
SKIP_BIDS = 100
#: Возраст в часах, после которого заказ считается устаревшим и уходит в SKIP.
STALE_HOURS = 72.0
#: Возраст, после которого APPLY_NOW уже не предлагается: заказ виден, но
#: ставки могли разобрать. Это НЕ то же самое, что STALE_HOURS: между 24 и 72
#: часами заказ остаётся в MAYBE, а не выбрасывается.
APPLY_NOW_MAX_AGE_HOURS = 24.0

#: Подпись причины «есть ставки, но заказ уже не первой свежести».
DRAWBACK_AGING = "posting not fresh (over 24h)"
#: Подпись причины «ниже порога APPLY_NOW».
DRAWBACK_BELOW_THRESHOLD = "below the apply-now threshold"


@dataclass
class ActionScore:
    """Action Score с полным breakdown."""

    action_score: float
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_score": self.action_score,
            "components": dict(self.components),
            "weights": dict(self.weights),
            "notes": dict(self.notes),
            "signals": dict(self.signals),
        }


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _rate_score(rate_usd: float | None, effort_hours: float | None) -> tuple[float, str]:
    """Деньги в час -> 0..100.

    Порог осмысленности не выдуман: ниже ``MIN_VIABLE_RATE_USD`` заказ не окупает
    времени, и это отражается в балле, а не скрывается.
    """
    if rate_usd is None:
        return 40.0, "ставка в USD неизвестна — уверенность снижена"
    if rate_usd < MIN_VIABLE_RATE_USD:
        return 5.0, f"${rate_usd:g}/ч ниже порога осмысленности ${MIN_VIABLE_RATE_USD:g}/ч"
    # 5..40 USD/ч -> 0..100, выше 40 — максимум.
    score = _clamp((rate_usd - MIN_VIABLE_RATE_USD) / (40.0 - MIN_VIABLE_RATE_USD) * 100.0)
    note = f"${rate_usd:g}/ч"
    if effort_hours:
        note += f" при оценке {effort_hours:g} ч"
    return score, note


def _skill_fit_component(gig: Gig) -> tuple[float, str]:
    """Сила совпадений навыков: сколько навыков профиля реально совпало.

    Используется уже вычисленный ``match_score`` (доля совпадений), чтобы не заводить
    вторую модель соответствия.
    """
    if gig.match_score is None:
        return 40.0, "совпадение навыков не измерено"
    return _clamp(float(gig.match_score)), f"match={gig.match_score:g}"


def _deliverability_component(gig: Gig) -> tuple[float, str]:
    table = {
        CanDeliver.HIGH: 100.0,
        CanDeliver.MEDIUM: 60.0,
        CanDeliver.LOW: 5.0,
        CanDeliver.UNKNOWN: 40.0,
    }
    return table.get(gig.can_deliver, 40.0), f"can_deliver={gig.can_deliver.value}"


def action_score_of(gig: Gig, *, now: Any = None) -> ActionScore:
    """Считает Action Score и сохраняет каждый вклад в breakdown."""
    signals = money_signals(gig, now=now)

    rate_score, rate_note = _rate_score(signals.effective_rate_usd, signals.effort_hours)
    skill_score, skill_note = _skill_fit_component(gig)
    deliver_score, deliver_note = _deliverability_component(gig)

    components: dict[str, float] = {
        "base_score": _clamp(float(gig.opportunity_score or 0.0)),
        "competition": signals.competition_score,
        "freshness": signals.freshness_score,
        "rate": rate_score,
        "skill_fit": skill_score,
        "deliverability": deliver_score,
    }
    weights = dict(ACTION_WEIGHTS)

    notes: dict[str, str] = {
        "base_score": f"opportunity_score={gig.opportunity_score}",
        "competition": (
            f"{signals.competition_band}"
            + (f" ({signals.bids} ставок)" if signals.bids is not None else " (ставки неизвестны)")
        ),
        "freshness": (
            f"{signals.freshness_band}"
            + (f" ({signals.age_hours:g} ч)" if signals.age_hours is not None else " (время неизвестно)")
        ),
        "rate": rate_note,
        "skill_fit": skill_note,
        "deliverability": deliver_note,
    }

    total_weight = sum(weights.values()) or 1.0
    weighted = sum(components[name] * weights[name] for name in weights)
    score = _clamp(weighted / total_weight)

    # Жёсткие правила ТЗ §5: невыполнимое не может подняться выше половины, иначе
    # деньги перевешивали бы невозможность выполнить работу.
    if gig.can_deliver is CanDeliver.LOW:
        cap = 44.0
        if score > cap:
            notes["deliverability_cap"] = (
                f"ограничено {cap:g}: can_deliver=low не может быть рекомендован к подаче"
            )
            score = cap

    # Неизвестная ставка снижает уверенность, но не выбрасывает заказ.
    if signals.rate_unreliable and score > 60.0:
        notes["rate_uncertainty"] = "ставка не подтверждена: уверенность рекомендации ниже"
        score = 60.0

    return ActionScore(
        action_score=round(score, 1),
        components=components,
        weights=weights,
        notes=notes,
        signals=signals.as_dict(),
    )


@dataclass
class QueueEntry:
    """Запись очереди действий с обязательным объяснением."""

    gig: Gig
    decision: str
    action: ActionScore
    skip_reasons: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.gig.title

    def to_dict(self) -> dict[str, Any]:
        return {
            "gig_id": self.gig.id,
            "title": self.gig.title,
            "decision": self.decision,
            "action_score": self.action.action_score,
            "skip_reasons": list(self.skip_reasons),
            "action": self.action.to_dict(),
        }


@dataclass
class ActionQueue:
    """Три очереди: APPLY_NOW / MAYBE / SKIP (ТЗ §6)."""

    apply_now: list[QueueEntry] = field(default_factory=list)
    maybe: list[QueueEntry] = field(default_factory=list)
    skip: list[QueueEntry] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "APPLY_NOW": len(self.apply_now),
            "MAYBE": len(self.maybe),
            "SKIP": len(self.skip),
        }

    def all_entries(self) -> list[QueueEntry]:
        return self.apply_now + self.maybe + self.skip


def _skip_reasons(gig: Gig, action: ActionScore) -> list[str]:
    """Причины SKIP в machine-readable виде. Порядок — от жёстких к мягким."""
    reasons: list[str] = []
    signals = action.signals

    if gig.rejection_reasons:
        reasons.append(SKIP_UNDELIVERABLE_REJECTED)
    # Не-IT домен: видео, звук, графика, копирайтинг, перевод. Причина проверяется
    # только там, где нет явного IT-признака: заказ может быть про интеграцию
    # видео-сервиса, и выбрасывать его по слову «video» было бы ошибкой.
    non_it, _markers = is_non_it_domain(gig)
    if non_it and not has_it_signal(gig):
        reasons.append(SKIP_NON_IT_DOMAIN)
    if gig.can_deliver is CanDeliver.LOW:
        reasons.append(SKIP_CAN_DELIVER_LOW)

    rate = signals.get("effective_rate_usd")
    if rate is None:
        reasons.append(SKIP_LOW_RATE_UNKNOWN)
    elif rate < SKIP_RATE_USD:
        reasons.append(SKIP_RATE_TOO_LOW)

    bids = signals.get("bids")
    if bids is not None and bids > SKIP_BIDS:
        reasons.append(SKIP_EXCESSIVE_COMPETITION)

    age = signals.get("age_hours")
    if age is not None and age > STALE_HOURS:
        reasons.append(SKIP_STALE)

    if gig.risk_level is RiskLevel.HIGH and SKIP_UNDELIVERABLE_REJECTED not in reasons:
        reasons.append("high risk")
    return reasons


def build_queue(gigs: list[Gig], *, now: Any = None) -> ActionQueue:
    """Раскладывает заказы по трём очередям.

    Правило раскладки намеренно простое и объяснимое:

    * ``APPLY_NOW`` — выполнимо (high), ставка осмысленна, конкуренция не убийственная,
      Action Score выше порога. Жёстко: ``low`` сюда не попадает никогда;
    * ``MAYBE`` — выполнимо, но есть недостаток: высокая конкуренция, низкая ставка,
      старый заказ или неполные данные;
    * ``SKIP`` — тратить время не стоит; причина обязательна.
    """
    queue = ActionQueue()
    for gig in gigs:
        action = action_score_of(gig, now=now)
        reasons = _skip_reasons(gig, action)
        signals = action.signals

        deliverable = gig.can_deliver is CanDeliver.HIGH
        rate = signals.get("effective_rate_usd")
        bids = signals.get("bids")
        age = signals.get("age_hours")

        hard_skip = any(r in reasons for r in (
            SKIP_UNDELIVERABLE_REJECTED, SKIP_CAN_DELIVER_LOW,
            SKIP_RATE_TOO_LOW, SKIP_EXCESSIVE_COMPETITION, SKIP_NON_IT_DOMAIN,
        ))

        if hard_skip or not deliverable:
            queue.skip.append(QueueEntry(gig, "SKIP", action, reasons or [SKIP_CAN_DELIVER_LOW]))
            continue

        # Выполнимо — теперь решаем между APPLY_NOW и MAYBE.
        drawbacks: list[str] = []
        if rate is None:
            drawbacks.append(SKIP_LOW_RATE_UNKNOWN)
        elif rate < MIN_VIABLE_RATE_USD:
            drawbacks.append(SKIP_RATE_TOO_LOW)
        if bids is not None and bids > 50:
            drawbacks.append(SKIP_EXCESSIVE_COMPETITION)
        if age is not None and age > APPLY_NOW_MAX_AGE_HOURS:
            # Не «too old» вообще, а «уже не свежий для немедленного отклика»:
            # такой заказ остаётся в MAYBE, а не уходит в SKIP до 72 часов.
            drawbacks.append(DRAWBACK_AGING)

        if action.action_score >= APPLY_NOW_MIN_SCORE and not drawbacks:
            queue.apply_now.append(QueueEntry(gig, "APPLY_NOW", action, []))
            continue

        # Причина обязательна (ТЗ §6): если ни один недостаток не сработал, значит
        # решает именно балл, и это надо сказать, а не оставлять поле пустым.
        if not drawbacks:
            drawbacks.append(DRAWBACK_BELOW_THRESHOLD)

        if action.action_score >= MAYBE_MIN_SCORE or not drawbacks:
            queue.maybe.append(QueueEntry(gig, "MAYBE", action, drawbacks))
        else:
            queue.skip.append(QueueEntry(gig, "SKIP", action, drawbacks))

    queue.apply_now.sort(key=lambda e: -e.action.action_score)
    queue.maybe.sort(key=lambda e: -e.action.action_score)
    queue.skip.sort(key=lambda e: -e.action.action_score)
    return queue


__all__ = [
    "ACTION_WEIGHTS",
    "APPLY_NOW_MAX_AGE_HOURS",
    "APPLY_NOW_MIN_SCORE",
    "DRAWBACK_AGING",
    "DRAWBACK_BELOW_THRESHOLD",
    "MAYBE_MIN_SCORE",
    "MIN_VIABLE_RATE_USD",
    "SKIP_BIDS",
    "SKIP_CAN_DELIVER_LOW",
    "SKIP_EXCESSIVE_COMPETITION",
    "SKIP_LOW_RATE_UNKNOWN",
    "SKIP_NON_IT_DOMAIN",
    "SKIP_RATE_TOO_LOW",
    "SKIP_STALE",
    "SKIP_UNDELIVERABLE_REJECTED",
    "STALE_HOURS",
    "ActionQueue",
    "ActionScore",
    "QueueEntry",
    "action_score_of",
    "build_queue",
]
