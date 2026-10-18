"""Протокол источника гигов.

Каждый источник возвращает сырые dict'ы (см. ``normalizer.normalize_gig``).
Core ничего не знает о том, как именно источник добыл данные — это позволяет
позже подключить Upwork/Freelancer/Contra/Fiverr, не переделывая pipeline
(ТЗ §8).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol


class GigSource(Protocol):
    """Контракт адаптера источника."""

    #: Уникальный ключ источника ('fixture', 'upwork', ...).
    key: str

    def fetch(self, limit: int | None = None) -> Iterator[dict]:
        """Возвращает поток сырых заказов. Не выполняет мутаций."""
        ...


#: Диагностические состояния прогона источника (Phase 4C §3).
#: SUCCESS — есть записи, ошибок нет; PARTIAL — есть записи и ошибки;
#: FAILED — записей нет; AUTH_REQUIRED — нужна ручная сессия; RATE_LIMITED —
#: источник ответил 429; UNKNOWN — прогона ещё не было. Отсутствие новых
#: заказов само по себе НЕ SUCCESS: нулевой выхлоп без доказательства
#: корректности ответа считается FAILED.
HEALTH_SUCCESS = "SUCCESS"
HEALTH_PARTIAL = "PARTIAL"
HEALTH_FAILED = "FAILED"
HEALTH_AUTH_REQUIRED = "AUTH_REQUIRED"
HEALTH_RATE_LIMITED = "RATE_LIMITED"
HEALTH_UNKNOWN = "UNKNOWN"


@dataclass
class SourceHealth:
    """Итог одного прогона источника."""

    status: str = HEALTH_UNKNOWN
    produced: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "produced": self.produced,
                "errors": list(self.errors)}


def summarize_health(produced: int, errors: list[str] | tuple[str, ...]) -> SourceHealth:
    """Единое правило: ошибки 429 -> RATE_LIMITED, auth-подсказки ->
    AUTH_REQUIRED, иначе produced>0 -> PARTIAL/SUCCESS, 0 -> FAILED."""
    errs = list(errors or [])
    blob = " ".join(errs).lower()
    if any(k in blob for k in ("429", "too many requests", "rate limit", "rate-limited")):
        return SourceHealth(status=HEALTH_RATE_LIMITED, produced=produced, errors=errs)
    if any(k in blob for k in ("login", "log-in", "unauthorized", "401", "403",
                               "auth required", "no logged-in", "captcha",
                               "expired", "session")):
        return SourceHealth(status=HEALTH_AUTH_REQUIRED, produced=produced, errors=errs)
    if produced > 0:
        return SourceHealth(
            status=HEALTH_SUCCESS if not errs else HEALTH_PARTIAL,
            produced=produced, errors=errs)
    return SourceHealth(status=HEALTH_FAILED, produced=0, errors=errs)
