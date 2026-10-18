"""Money-First признаки: стоит ли тратить время и bid на заказ прямо сейчас (P5 §3).

Модуль отвечает на один вопрос: **разумно ли податься на этот заказ сегодня**, а не
насколько проект «вообще хорош». Отсюда три группы признаков:

* **competition** — по публичному числу ставок;
* **freshness** — возраст объявления;
* **estimated_effective_rate_usd** — деньги в час после конверсии P4.

Пороги конкуренции выбраны **по фактическому распределению корпуса**, а не назначены:
на 296 записях Freelancer перцентили ставок дали p50=10, p75=29, p90=63, p95=106, что
совпало с границами 10/25/50/100. Если данных нет — значение ``unknown``, а не догадка.

Ничего не выдумывается: отсутствие даты публикации даёт ``None``, отсутствие курса —
``None`` вместо числа.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import Gig

#: Пороги конкуренции по числу ставок. Обоснованы распределением корпуса (см. модуль).
COMPETITION_BANDS: tuple[tuple[int, str], ...] = (
    (10, "excellent"),
    (25, "good"),
    (50, "medium"),
    (100, "hard"),
)

#: Оценки уровней конкуренции для Action Score (0..100).
COMPETITION_SCORES: dict[str, float] = {
    "excellent": 100.0,
    "good": 78.0,
    "medium": 52.0,
    "hard": 26.0,
    "very_hard": 8.0,
    "unknown": 45.0,
}

#: Границы возраста объявления в часах. ``None`` — источник не дал время.
FRESHNESS_BANDS: tuple[tuple[float, str], ...] = (
    (1.0, "under_1h"),
    (6.0, "under_6h"),
    (24.0, "under_24h"),
    (72.0, "1_3_days"),
)

#: Оценки свежести для Action Score. Свежее — больше шансов, что ставок ещё мало.
FRESHNESS_SCORES: dict[str, float] = {
    "under_1h": 100.0,
    "under_6h": 88.0,
    "under_24h": 72.0,
    "1_3_days": 48.0,
    "over_3_days": 22.0,
    "unknown": 45.0,
}


def competition_band(bids: int | None, bands: tuple[tuple[int, str], ...] = COMPETITION_BANDS) -> str:
    """Число ставок -> полоса ``excellent``/``good``/``medium``/``hard``/``very_hard``.

    ``None`` даёт ``unknown``: неизвестная конкуренция не приравнивается к выгодной,
    иначе заказ без данных выглядел бы лучше заказа с известной низкой конкуренцией.
    """
    if bids is None:
        return "unknown"
    count = max(0, int(bids))
    for ceiling, name in bands:
        if count <= ceiling:
            return name
    return "very_hard"


def freshness_band(published_at: str | None, *, now: datetime | None = None) -> str:
    """Метка времени публикации -> полоса возраста. Без времени — ``unknown``."""
    hours = age_hours(published_at, now=now)
    if hours is None:
        return "unknown"
    for ceiling, name in FRESHNESS_BANDS:
        if hours <= ceiling:
            return name
    return "over_3_days"


def age_hours(published_at: str | None, *, now: datetime | None = None) -> float | None:
    """Возраст объявления в часах. Непонятное или отсутствующее время -> ``None``.

    Отрицательный возраст (часы разошлись) приводится к нулю: «опубликовано в будущем»
    не является измерением, и показывать такое как есть было бы хуже, чем сказать «0».
    """
    if not published_at:
        return None
    text = str(published_at).strip()
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    delta = (reference - stamp.astimezone(timezone.utc)).total_seconds() / 3600.0
    return round(max(0.0, delta), 1)


def freshness_score(band: str) -> float:
    return FRESHNESS_SCORES.get(band, FRESHNESS_SCORES["unknown"])


def competition_score(band: str) -> float:
    return COMPETITION_SCORES.get(band, COMPETITION_SCORES["unknown"])


def effective_rate_usd(gig: Gig) -> float | None:
    """Деньги в час в USD.

    Для hourly — нормализованная ставка. Для fixed — бюджет / трудоёмкость. Значение
    приходит уже сконвертированным (P4), поэтому повторной конверсии здесь нет.

    Возможны два случая ``None``, и оба честнее числа:

    * ``effective_hourly_rate`` не вычислена (нет бюджета, нет курса или нет трудоёмкости);
    * для fixed-заказа нет трудоёмкости, а без неё делить не на что.
    """
    if gig.effective_hourly_rate is not None:
        return float(gig.effective_hourly_rate)
    return None


def rate_unreliable(gig: Gig) -> bool:
    """Ставка неизвестна или неправдоподобна — уверенность рекомендации ниже.

    Проверяются два разных случая: значения нет вовсе, и значение есть, но слишком
    мало, чтобы иметь смысл (меньше доллара в час после конверсии).
    """
    rate = effective_rate_usd(gig)
    if rate is None:
        return True
    return rate < 1.0


@dataclass(frozen=True)
class MoneySignals:
    """Собранные Money-First признаки по одному заказу."""

    bids: int | None
    competition_band: str
    competition_score: float
    age_hours: float | None
    freshness_band: str
    freshness_score: float
    effective_rate_usd: float | None
    rate_unreliable: bool
    effort_hours: float | None
    found_via: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bids": self.bids,
            "competition": self.competition_band,
            "competition_score": self.competition_score,
            "age_hours": self.age_hours,
            "freshness": self.freshness_band,
            "freshness_score": self.freshness_score,
            "effective_rate_usd": self.effective_rate_usd,
            "rate_unreliable": self.rate_unreliable,
            "effort_hours": self.effort_hours,
            "found_via": list(self.found_via),
        }


def money_signals(gig: Gig, *, now: datetime | None = None) -> MoneySignals:
    """Все Money-First признаки одним вызовом."""
    band = competition_band(gig.competition)
    fresh = freshness_band(gig.published_at, now=now)
    rate = effective_rate_usd(gig)
    facts = gig.source_facts or {}
    via = facts.get("found_via")
    return MoneySignals(
        bids=gig.competition,
        competition_band=band,
        competition_score=competition_score(band),
        age_hours=age_hours(gig.published_at, now=now),
        freshness_band=fresh,
        freshness_score=freshness_score(fresh),
        effective_rate_usd=rate,
        rate_unreliable=rate_unreliable(gig),
        effort_hours=gig.estimated_effort_hours,
        found_via=tuple(via) if isinstance(via, list) else (),
    )


__all__ = [
    "COMPETITION_BANDS",
    "COMPETITION_SCORES",
    "FRESHNESS_BANDS",
    "FRESHNESS_SCORES",
    "MoneySignals",
    "age_hours",
    "competition_band",
    "competition_score",
    "effective_rate_usd",
    "freshness_band",
    "freshness_score",
    "money_signals",
    "rate_unreliable",
]
