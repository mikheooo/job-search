"""Gig Search: домен фриланс-заказов.

Отдельный контур рядом с Job Search (вакансии). Gig НЕ является
разновидностью Vacancy: у заказов другая модель (бюджет, клиент,
конкуренция, payment readiness), другой pipeline и своё хранилище.

Состав пакета:
- models      — датакласс Gig + энумы;
- normalizer  — сырой dict источника -> нормализованный Gig;
- storage     — persistence в SQLite (таблицы gigs / gig_events);
- dedup       — дедупликация гигов;
- hard_gates  — жёсткие фильтры до scoring с machine-readable причинами;
- assessment  — can_deliver / ai_automation_potential / payment_readiness /
                effort / effective_rate / client_quality / risk;
- scoring     — прозрачный breakdown -> opportunity_score;
- pipeline    — оркестрация source -> normalize -> dedup -> gates ->
                assessment -> scoring -> ranked gigs;
- sources     — адаптеры источников (base protocol + fixture).

Safety: контур gig — prepare-only. Никаких откликов, сообщений и
мутаций внешних систем; только сбор, оценка и локальное хранение.
"""
from __future__ import annotations

from .models import (
    BudgetType,
    CanDeliver,
    Gig,
    GigStatus,
    PaymentReadiness,
    RejectionReason,
    RiskLevel,
    SourceMode,
)

__all__ = [
    "BudgetType",
    "CanDeliver",
    "Gig",
    "GigStatus",
    "PaymentReadiness",
    "RejectionReason",
    "RiskLevel",
    "SourceMode",
]
