"""Доменные модели Gig Search.

Ключевые принципы:
1. ``Gig`` — самостоятельная сущность, не наследник ``Vacancy`` и не связан с
   таблицей ``vacancies``.
2. Неизвестное значение всегда выражается как ``None`` (для чисел/строк) или
   член ``UNKNOWN`` (для энумов). Никаких выдуманных дефолтов, которые потом
   невозможно отличить от реальных данных.
3. Оценочные поля (``can_deliver``, ``payment_readiness``, ``risk_level``,
   ``opportunity_score`` и т.д.) заполняются отдельными модулями
   (assessment/scoring) и по умолчанию пусты.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BudgetType(str, Enum):
    """Тип бюджета заказа.

    ``MONTHLY``/``WEEKLY`` добавлены в P2 под реальную выдачу Contra
    (``MonthlyRateJobBudget`` / ``WeeklyRateJobBudget``). Аддитивно: старые
    значения и их семантика не изменены, но периодические ставки больше не
    склеиваются с разовым фиксом, потому что это разные единицы измерения.
    """

    FIXED = "fixed"
    HOURLY = "hourly"
    MONTHLY = "monthly"
    WEEKLY = "weekly"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Any) -> "BudgetType":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls.UNKNOWN
        text = str(raw).strip().lower()
        aliases = {
            "fixed": cls.FIXED,
            "fixed_price": cls.FIXED,
            "fixed-price": cls.FIXED,
            "fixedpricejobbudget": cls.FIXED,
            "project": cls.FIXED,
            "one_off": cls.FIXED,
            "hourly": cls.HOURLY,
            "hour": cls.HOURLY,
            "per_hour": cls.HOURLY,
            "per-hour": cls.HOURLY,
            "hourlyratejobbudget": cls.HOURLY,
            "rate": cls.HOURLY,
            "monthly": cls.MONTHLY,
            "monthlyratejobbudget": cls.MONTHLY,
            "month": cls.MONTHLY,
            "weekly": cls.WEEKLY,
            "weeklyratejobbudget": cls.WEEKLY,
            "week": cls.WEEKLY,
            "": cls.UNKNOWN,
        }
        return aliases.get(text, cls.UNKNOWN)


class SourceMode(str, Enum):
    """Откуда взялся гиг: реальный источник или тестовая фикстура.

    Требование P2 §7: нельзя смешивать тестовые данные с реальными так, чтобы
    пользователь не видел разницы. Режим — свойство источника, а не догадка.
    """

    LIVE = "live"
    FIXTURE = "fixture"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Any) -> "SourceMode":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls.UNKNOWN
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.UNKNOWN


class CanDeliver(str, Enum):
    """Оценка выполнимости заказа (не только по историческому резюме)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Any) -> "CanDeliver":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls.UNKNOWN
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.UNKNOWN


class PaymentReadiness(str, Enum):
    """Готовность канала оплаты. Независимо от общего match score.

    P2 §5: ``READY`` требует подтверждённой готовности *нашего* аккаунта
    получать деньги, а не наличия способов выплат у площадки.
    """

    READY = "ready"
    POSSIBLE = "possible"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Any) -> "PaymentReadiness":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls.UNKNOWN
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.UNKNOWN


class RiskLevel(str, Enum):
    """Итоговый уровень риска заказа."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Any) -> "RiskLevel":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls.UNKNOWN
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.UNKNOWN


class GigStatus(str, Enum):
    """Минимальные статусы P1.

    Расширенный workflow (PROPOSAL_READY / APPLIED / ... / LOST) намеренно НЕ
    реализуется сейчас, но архитектура (строка в БД + событийная таблица
    ``gig_events``) готова принять новые значения без миграции схемы.
    """

    NEW = "NEW"
    SHORTLISTED = "SHORTLISTED"
    SKIPPED = "SKIPPED"

    @classmethod
    def parse(cls, raw: Any) -> "GigStatus":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls.NEW
        try:
            return cls(str(raw).strip().upper())
        except ValueError:
            return cls.NEW


#: Статусы, которые появятся в P2+ (только документация, не принимаются сейчас).
FUTURE_GIG_STATUSES: tuple[str, ...] = (
    "PROPOSAL_READY",
    "APPLIED",
    "CLIENT_REPLIED",
    "NEGOTIATING",
    "ACCEPTED",
    "IN_PROGRESS",
    "DELIVERED",
    "PAID",
    "LOST",
)


class RejectionReason(str, Enum):
    """Machine-readable причины отклонения заказа до scoring."""

    ACCESS_UNAVAILABLE = "ACCESS_UNAVAILABLE"
    PAYMENT_UNAVAILABLE = "PAYMENT_UNAVAILABLE"
    COUNTRY_RESTRICTION = "COUNTRY_RESTRICTION"
    UNDELIVERABLE = "UNDELIVERABLE"
    PRICE_MISSING = "PRICE_MISSING"
    SCAM_RISK = "SCAM_RISK"
    DUPLICATE = "DUPLICATE"
    #: P2: источник сам сообщает, что заказ больше не открыт (Contra: EXPIRED /
    #: CLOSED). Без этого гейта архивная запись выглядела бы в рабочем списке
    #: как доступный заказ.
    SOURCE_CLOSED = "SOURCE_CLOSED"

    @classmethod
    def parse(cls, raw: Any) -> "RejectionReason | None":
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return None
        try:
            return cls(str(raw).strip().upper())
        except ValueError:
            return None


def make_gig_id(source: str, source_id: str) -> str:
    """Стабильный id гига: детерминирован от (source, source_id).

    Тот же заказ из того же источника получает тот же id при повторном
    ingestion, поэтому дедуп сходится даже без обращения к БД.
    """
    payload = f"{str(source).strip().lower()}::{str(source_id).strip()}"
    return "gig_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class Gig:
    """Нормализованный фриланс-заказ.

    Поля сгруппированы как в ТЗ GIG-SEARCH-P1 §2.
    """

    # --- идентичность ---------------------------------------------------
    id: str = ""
    source: str = ""
    source_id: str = ""
    url: str = ""

    # --- происхождение данных (P2 §2, §4) --------------------------------
    #: live | fixture — источник данных, а не оценка.
    source_mode: SourceMode = SourceMode.UNKNOWN
    #: Границы доступа к источнику, например ``public``.
    source_scope: str | None = None
    #: Публичная страница, на которой запись реально наблюдалась.
    source_url: str | None = None
    #: Время сбора (ISO-8601 UTC).
    fetched_at: str | None = None
    #: Метка статуса самого источника (Contra: EXPIRED/CLOSED/...).
    source_status: str | None = None

    # --- наблюдение во времени (P2 §8) -----------------------------------
    #: Когда гиг впервые попал в наше хранилище.
    first_seen_at: str | None = None
    #: Когда источник в последний раз подтверждал запись.
    last_seen_at: str | None = None

    # --- разделение фактов и расчётов (P2 §4, §5) -------------------------
    #: Возможности площадки по оплате (capability, не персональная готовность).
    platform_payment_options: list[str] = field(default_factory=list)
    #: Поля, дословно взятые у источника: имя -> значение.
    source_facts: dict[str, Any] = field(default_factory=dict)
    #: Какие поля посчитаны нашей системой, а не получены от источника.
    derived_fields: list[str] = field(default_factory=list)

    # --- суть заказа ----------------------------------------------------
    title: str = ""
    description: str = ""
    category: str | None = None
    skills: list[str] = field(default_factory=list)

    # --- бюджет ---------------------------------------------------------
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str | None = None
    budget_type: BudgetType = BudgetType.UNKNOWN
    published_at: str | None = None

    # --- клиент ---------------------------------------------------------
    client_name: str | None = None
    client_country: str | None = None
    client_rating: float | None = None
    client_jobs_posted: int | None = None
    client_hires: int | None = None
    client_total_spent: float | None = None

    # --- конкуренция / стоимость участия ---------------------------------
    competition: int | None = None
    proposal_cost: float | None = None

    # --- оценки (заполняются assessment/scoring) -------------------------
    payment_readiness: PaymentReadiness = PaymentReadiness.UNKNOWN

    can_deliver: CanDeliver = CanDeliver.UNKNOWN
    ai_automation_potential: int | None = None
    estimated_effort_hours: float | None = None
    effective_hourly_rate: float | None = None

    client_quality: float | None = None
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    risks: list[str] = field(default_factory=list)

    match_score: float | None = None
    opportunity_score: float | None = None

    # --- служебное -------------------------------------------------------
    status: GigStatus = GigStatus.NEW
    created_at: str | None = None
    updated_at: str | None = None

    # --- техническое (сверх минимальной модели) --------------------------
    #: Прозрачный breakdown opportunity_score: компонент -> балл/детали.
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    #: Причины reject от hard gates (пусто у прошедших gates).
    rejection_reasons: list[str] = field(default_factory=list)
    #: Короткие причины "почему подходит" / "почему рискованно" для UI/CLI.
    why_matched: list[str] = field(default_factory=list)
    why_risky: list[str] = field(default_factory=list)
    #: Исходная полезная нагрузка источника (для аудита, без секретов).
    raw_data: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        self.source = str(self.source or "").strip().lower()
        self.source_id = str(self.source_id or "").strip()
        self.title = str(self.title or "").strip()
        self.description = str(self.description or "").strip()
        self.budget_type = BudgetType.parse(self.budget_type)
        self.payment_readiness = PaymentReadiness.parse(self.payment_readiness)
        self.can_deliver = CanDeliver.parse(self.can_deliver)
        self.risk_level = RiskLevel.parse(self.risk_level)
        self.status = GigStatus.parse(self.status)
        self.source_mode = SourceMode.parse(self.source_mode)
        if not self.skills:
            self.skills = []
        if not self.id and self.source and self.source_id:
            self.id = make_gig_id(self.source, self.source_id)

    # ------------------------------------------------------------------
    @property
    def is_rejected(self) -> bool:
        return bool(self.rejection_reasons)

    @property
    def budget_text(self) -> str:
        """Человекочитаемый бюджет; честно показывает unknown."""
        currency = (self.currency or "").upper()
        if self.budget_type is BudgetType.HOURLY:
            low, high = self.budget_min, self.budget_max
            if low is None and high is None:
                return "unknown"
            if low is not None and high is not None and low != high:
                return f"{currency} {low:g}-{high:g}/h".strip()
            value = low if low is not None else high
            return f"{currency} {value:g}/h".strip()
        if self.budget_min is None and self.budget_max is None:
            return "unknown"
        if self.budget_min is not None and self.budget_max is not None and self.budget_min != self.budget_max:
            return f"{currency} {self.budget_min:g}-{self.budget_max:g}".strip()
        value = self.budget_min if self.budget_min is not None else self.budget_max
        return f"{currency} {value:g}".strip()

    def to_dict(self) -> dict[str, Any]:
        """Сериализация для API/CLI/тестов."""
        return {
            "id": self.id,
            "source": self.source,
            "source_id": self.source_id,
            "url": self.url,
            "source_mode": self.source_mode.value,
            "source_scope": self.source_scope,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "source_status": self.source_status,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "platform_payment_options": list(self.platform_payment_options),
            "source_facts": dict(self.source_facts),
            "derived_fields": list(self.derived_fields),
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "skills": list(self.skills),
            "budget_min": self.budget_min,
            "budget_max": self.budget_max,
            "currency": self.currency,
            "budget_type": self.budget_type.value,
            "budget_text": self.budget_text,
            "published_at": self.published_at,
            "client_name": self.client_name,
            "client_country": self.client_country,
            "client_rating": self.client_rating,
            "client_jobs_posted": self.client_jobs_posted,
            "client_hires": self.client_hires,
            "client_total_spent": self.client_total_spent,
            "competition": self.competition,
            "proposal_cost": self.proposal_cost,
            "payment_readiness": self.payment_readiness.value,
            "can_deliver": self.can_deliver.value,
            "ai_automation_potential": self.ai_automation_potential,
            "estimated_effort_hours": self.estimated_effort_hours,
            "effective_hourly_rate": self.effective_hourly_rate,
            "client_quality": self.client_quality,
            "risk_level": self.risk_level.value,
            "risks": list(self.risks),
            "match_score": self.match_score,
            "opportunity_score": self.opportunity_score,
            "score_breakdown": dict(self.score_breakdown),
            "rejection_reasons": list(self.rejection_reasons),
            "why_matched": list(self.why_matched),
            "why_risky": list(self.why_risky),
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
