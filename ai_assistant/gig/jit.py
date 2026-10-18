"""JIT verification — перепроверка заказа непосредственно перед подачей (P6 §1–2).

ЗАЧЕМ
-----
Дефект P5 измерен: за считанные часы число ставок выросло у всех проверенных проектов
(MarCom 15→37, Salesforce 27→51, Django 163→196). Значит кэшированное значение
`competition` не годится для решения «подаваться сейчас» — оно описывает момент сбора,
а не момент подачи.

ЧТО ДЕЛАЕТ
----------
Перед тем как заказ попадёт в финальный список, страница открывается заново и из неё
обновляются статус, бюджет, валюта, число ставок, время публикации, описание, навыки и
данные клиента. Результат фиксируется как наблюдение, а не перезапись истории:

    bids_at_discovery   сколько было, когда проект нашли
    bids_live           сколько сейчас
    bids_delta          разница
    verified_at         когда перепроверили
    live_verified       была ли перепроверка в текущем запуске

Ранжирование обязано использовать ``bids_live``, а не ``bids_at_discovery``.

ГРАНИЦЫ
-------
Без успешной перепроверки ``live_verified`` остаётся ``False``, и такой заказ не может
попасть в ``APPLY_FIRST``. Закрытый проект (``source_status`` не OPEN) отбрасывается —
это не «риск», а факт, что подаваться некуда.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .models import Gig

#: Ключ в ``source_facts``, под которым лежит история перепроверок.
JIT_FACT_KEY = "jit_verification"

#: Статусы источника, при которых подаваться нельзя.
CLOSED_STATUSES = frozenset({
    "CLOSED", "EXPIRED", "COMPLETED", "CANCELLED", "CANCELED", "FROZEN",
    "DELETED", "FILLED", "AWARDED",
})


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_iso(value: Any) -> str:
    """Приводит момент времени к строке ISO.

    Дефект, найденный в первом прогоне: ``verified_at`` получал ``datetime``, если
    вызывающий передавал ``now=datetime.now(...)``, и запись переставала
    сериализоваться в JSON. Поле обязано быть строкой независимо от формы входа.
    """
    if value is None:
        return _utcnow()
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


@dataclass
class LiveVerification:
    """Результат перепроверки одного заказа."""

    gig_id: str
    live_verified: bool
    verified_at: str | None = None
    bids_at_discovery: int | None = None
    bids_live: int | None = None
    bids_delta: int | None = None
    status_before: str | None = None
    status_live: str | None = None
    budget_changed: bool = False
    description_changed: bool = False
    skills_changed: bool = False
    client_changed: bool = False
    is_open: bool = True
    error: str | None = None
    #: Поля, реально обновлённые из свежей страницы: (имя, было, стало).
    changes: list[tuple[str, Any, Any]] = field(default_factory=list)

    @property
    def bids_increased(self) -> bool:
        return bool(self.bids_delta and self.bids_delta > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "live_verified": self.live_verified,
            "verified_at": self.verified_at,
            "bids_at_discovery": self.bids_at_discovery,
            "bids_live": self.bids_live,
            "bids_delta": self.bids_delta,
            "status_before": self.status_before,
            "status_live": self.status_live,
            "is_open": self.is_open,
            "budget_changed": self.budget_changed,
            "description_changed": self.description_changed,
            "skills_changed": self.skills_changed,
            "client_changed": self.client_changed,
            "error": self.error,
            "changes": [{"field": f, "before": b, "after": a} for f, b, a in self.changes],
        }


def is_open_status(status: str | None) -> bool:
    """Открыт ли заказ. ``None`` — неизвестно, и это НЕ повод считать его открытым."""
    if status is None:
        return False
    return str(status).strip().upper() in {"OPEN", "ACTIVE"}


def verify_gig(gig: Gig, fresh_record: dict[str, Any] | None, *, now: Any = None) -> LiveVerification:
    """Строит ``LiveVerification`` из свежего сырого record.

    ``fresh_record=None`` означает, что перепроверить не удалось (сеть, закрытая страница).
    В этом случае ``live_verified`` остаётся ``False``, и заказ не попадёт в APPLY_FIRST.
    """
    when = _as_iso(now)
    previous = (gig.source_facts or {}).get(JIT_FACT_KEY) or {}
    discovery_bids = previous.get("bids_at_discovery", gig.competition)

    if fresh_record is None:
        return LiveVerification(
            gig_id=gig.id,
            live_verified=False,
            verified_at=None,
            bids_at_discovery=discovery_bids,
            bids_live=None,
            bids_delta=None,
            status_before=gig.source_status,
            status_live=None,
            is_open=False,
            error="no fresh observation",
        )

    live_bids = fresh_record.get("competition")
    live_status = fresh_record.get("source_status")
    delta = None
    if live_bids is not None and discovery_bids is not None:
        delta = int(live_bids) - int(discovery_bids)

    changes: list[tuple[str, Any, Any]] = []
    budget = fresh_record.get("budget") or {}
    fresh_min, fresh_max = budget.get("min"), budget.get("max")
    budget_changed = (fresh_min, fresh_max) != (gig.budget_min, gig.budget_max)
    if budget_changed:
        changes.append(("budget", (gig.budget_min, gig.budget_max), (fresh_min, fresh_max)))

    fresh_desc = str(fresh_record.get("description") or "")
    desc_changed = bool(fresh_desc) and fresh_desc.strip() != (gig.description or "").strip()
    if desc_changed:
        changes.append(("description", "changed", "changed"))

    fresh_skills = list(fresh_record.get("skills") or [])
    skills_changed = bool(fresh_skills) and sorted(fresh_skills) != sorted(gig.skills or [])
    if skills_changed:
        changes.append(("skills", gig.skills, fresh_skills))

    fresh_currency = fresh_record.get("currency")
    if fresh_currency and fresh_currency != gig.currency:
        changes.append(("currency", gig.currency, fresh_currency))

    fresh_client = fresh_record.get("client_country")
    client_changed = bool(fresh_client) and fresh_client != gig.client_country
    if client_changed:
        changes.append(("client_country", gig.client_country, fresh_client))

    if delta:
        changes.append(("bids", discovery_bids, live_bids))
    if live_status != gig.source_status:
        changes.append(("source_status", gig.source_status, live_status))

    return LiveVerification(
        gig_id=gig.id,
        live_verified=True,
        verified_at=when,
        bids_at_discovery=discovery_bids,
        bids_live=None if live_bids is None else int(live_bids),
        bids_delta=delta,
        status_before=gig.source_status,
        status_live=live_status,
        budget_changed=budget_changed,
        description_changed=desc_changed,
        skills_changed=skills_changed,
        client_changed=client_changed,
        is_open=is_open_status(live_status),
        changes=changes,
    )


def apply_verification(gig: Gig, verification: LiveVerification, fresh_record: dict[str, Any] | None) -> Gig:
    """Переносит свежее наблюдение в объект ``Gig`` для ранжирования.

    Обновляются только те поля, которые действительно пришли из свежей страницы.
    ``competition`` заменяется на ``bids_live``: именно это делает ранжирование
    актуальным, а не устаревшим на часы.
    """
    facts = dict(gig.source_facts or {})
    discovery_via = list(facts.get("found_via") or [])
    facts[JIT_FACT_KEY] = verification.to_dict()
    gig.source_facts = facts

    if fresh_record is None:
        return gig

    for key in list(facts):
        if key.startswith("freelancer_"):
            facts.pop(key)
    facts.update(fresh_record.get("source_facts") or {})
    facts[JIT_FACT_KEY] = verification.to_dict()
    # Retain discovery provenance even when the refresh wasn't a listing fetch.
    facts["found_via"] = sorted(set(discovery_via) | set(facts.get("found_via") or []) |
                                set((gig.raw_data.get("source_facts") or {}).get("found_via") or []))
    facts["description_live"] = fresh_record.get("description") or ""
    facts["skills_live"] = list(fresh_record.get("skills") or [])
    facts["project_status"] = verification.status_live or "UNKNOWN"

    if verification.bids_live is not None:
        gig.competition = verification.bids_live
    gig.source_status = verification.status_live or "UNKNOWN"
    budget = fresh_record.get("budget") or {}
    gig.budget_min = budget.get("min")
    gig.budget_max = budget.get("max")
    if budget.get("type"):
        from .models import BudgetType
        gig.budget_type = BudgetType.parse(budget["type"])
    gig.currency = fresh_record.get("currency")
    gig.published_at = fresh_record.get("published_at")
    fresh_desc = str(fresh_record.get("description") or "")
    gig.description = fresh_desc
    fresh_skills = list(fresh_record.get("skills") or [])
    gig.skills = fresh_skills
    gig.client_country = fresh_record.get("client_country")
    gig.client_rating = fresh_record.get("client_rating")
    gig.client_jobs_posted = fresh_record.get("client_jobs_posted")
    gig.client_hires = fresh_record.get("client_hires")
    from .assessment import effective_hourly_rate
    gig.effective_hourly_rate = effective_hourly_rate(gig, gig.estimated_effort_hours)
    if fresh_record.get("source_url"):
        gig.source_url = fresh_record["source_url"]
    return gig


def live_bids_of(gig: Gig) -> int | None:
    """Актуальное число ставок: ``bids_live`` если есть, иначе значение сбора.

    Возвращает ``None``, если заказ не перепроверен и старых данных нет. Это важно:
    ранжирование не должно подставлять устаревшее значение как свежее.
    """
    facts = gig.source_facts or {}
    jit = facts.get(JIT_FACT_KEY)
    if isinstance(jit, dict):
        if jit.get("live_verified"):
            return jit.get("bids_live")
        # Перепроверка провалилась: устаревшее значение нельзя выдавать за актуальное.
        return None
    return gig.competition


@dataclass
class VerificationBatch:
    """Итог пакетной перепроверки: кто подтверждён, кто закрыт, кто не проверен."""

    verified: list[Gig] = field(default_factory=list)
    closed: list[tuple[Gig, LiveVerification]] = field(default_factory=list)
    unverified: list[tuple[Gig, LiveVerification]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "verified": len(self.verified),
            "closed": len(self.closed),
            "unverified": len(self.unverified),
        }


def verify_batch(
    gigs: list[Gig],
    fetch_record: Callable[[Gig], dict[str, Any] | None],
    *,
    now: Any = None,
    on_progress: Callable[[int, int, Gig], None] | None = None,
) -> VerificationBatch:
    """Перепроверяет список заказов через переданный загрузчик.

    Загрузчик инъецируется, а не импортируется: это позволяет тестировать логику
    детерминированно и офлайн, без сети.
    """
    batch = VerificationBatch()
    total = len(gigs)
    for index, gig in enumerate(gigs, 1):
        try:
            fresh = fetch_record(gig)
        except Exception as exc:                # noqa: BLE001 — сбой одного не рушит партию
            fresh = None
            verification = verify_gig(gig, None, now=now)
            verification.error = f"{type(exc).__name__}: {exc}"
        else:
            verification = verify_gig(gig, fresh, now=now)

        if on_progress is not None:
            on_progress(index, total, gig)

        apply_verification(gig, verification, fresh)
        if not verification.live_verified:
            batch.unverified.append((gig, verification))
            continue
        if verification.is_open:
            batch.verified.append(gig)
        else:
            batch.closed.append((gig, verification))
    return batch


__all__ = [
    "CLOSED_STATUSES",
    "JIT_FACT_KEY",
    "LiveVerification",
    "VerificationBatch",
    "apply_verification",
    "is_open_status",
    "live_bids_of",
    "verify_batch",
    "verify_gig",
]
