"""Hard gates: жёсткие фильтры гигов до scoring.

Каждый отказ обязан иметь machine-readable причину из ``RejectionReason``.
Правило fail-closed: при явном признаке непроходимости заказ отклоняется, а
не "оценивается с пониженным баллом".

Отличие от ``assessment``: gates отвечают на вопрос "можно ли вообще
рассматривать заказ", assessment — "насколько он хорош".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import BudgetType, Gig, PaymentReadiness, RejectionReason

# --- Эвристики источников ------------------------------------------------

#: Страны/регионы, на которые у нас нет права работать (санкционные и
#: явно недоступные направления). Список намеренно короткий и явный.
_BLOCKED_CLIENT_COUNTRIES = {
    "russia",
    "russian federation",
    "россия",
    "belarus",
    "беларусь",
    "iran",
    "иран",
    "north korea",
    "syria",
}

#: Формулировки ограничения по стране/резидентству в тексте заказа.
_COUNTRY_RESTRICTION_PATTERNS = (
    r"\b(?:us|usa|united states)\s+(?:citizens?|residents?|only)\b",
    r"\bonly\s+(?:us|usa|uk|eu|canada|australia)[\s-]based\b",
    r"\bmust\s+be\s+(?:based|located|resident)\s+in\b",
    r"\bmust\s+have\s+(?:a\s+)?(?:valid\s+)?(?:us|uk|eu)\s+(?:work\s+)?(?:permit|visa|authorization)\b",
    r"\b(?:us|uk|eu|canada)\s+work\s+authorization\s+required\b",
    r"\btax\s+residen(?:cy|t)\s+in\b",
    r"\bгражданств[оа]\s+(?:сша|великобритании|е[сc]|канады)\b",
    r"\bтолько\s+(?:для\s+)?(?:резидент|граждан)\w*\b",
)

#: Признаки скама / спама.
_SCAM_PATTERNS = (
    r"\bregistration\s+fee\b",
    r"\bpay\s+(?:a\s+)?(?:small\s+)?(?:fee|deposit)\b",
    r"\bupfront\s+(?:payment|fee|deposit)\b",
    r"\btraining\s+fee\b",
    r"\bsend\s+(?:us\s+)?(?:your\s+)?(?:id|passport|ssn)\b",
    r"\bwhatsapp\s+only\b",
    r"\btelegram\s+only\s+contact\b",
    r"\bguaranteed\s+\d{3,}\s*(?:usd|eur|\$)\s*(?:per|/)\s*(?:day|week)\b",
    r"\bno\s+experience\s+needed\b.*\b(?:earn|salary)\b.*\b(?:daily|weekly)\b",
    r"\bпредоплат\w*\b",
    r"\bоплатите\s+(?:взнос|комиссию)\b",
)

#: Требования, которые мы заведомо не исполняем.
_UNDELIVERABLE_PATTERNS = (
    r"\b(?:must|required\s+to)\s+(?:be\s+)?(?:available|online)\s+(?:during|in)\s+\d{1,2}[:.]?\d{0,2}\s*(?:am|pm)?\s*(?:est|pst|cst|utc)\s+daily\b",
    r"\bphone\s+(?:calls?|support)\s+(?:8|eight)\s+hours\s+(?:a|per)\s+day\b",
    r"\bvideo\s+shopping\b",
    r"\b(?:in-person|on-site|onsite)\s+(?:work|meetings?)\s+(?:required|mandatory)\b",
    r"\bmust\s+own\s+(?:a\s+)?(?:drone|truck|vehicle|physical\s+store)\b",
    r"\bphysical\s+(?:presence|attendance)\s+required\b",
)

#: Признаки, что доступ к площадке/заказу у нас отсутствует.
_ACCESS_BLOCKED_PATTERNS = (
    r"\binvite[\s-]only\b",
    r"\bclosed\s+to\s+new\s+freelancers\b",
    r"\bnot\s+(?:open|available)\s+in\s+your\s+(?:country|region)\b",
    r"\bregion\s+(?:blocked|restricted)\b",
)

#: Явное условие оплаты, которое мы не сможем получить.
_PAYMENT_BLOCKED_PATTERNS = (
    r"\bpayment\s+(?:in|via)\s+(?:crypto\s+)?(?:only\s+)?(?:usdt|btc|bitcoin|ethereum)\s+only\b",
    r"\bno\s+escrow\b.*\b(?:no|without)\s+(?:upfront|milestone)\b",
    r"\bpaid\s+(?:only\s+)?(?:in\s+)?(?:equity|shares|exposure)\b",
    r"\bunpaid\s+(?:test\s+)?(?:task|trial)\s+of\s+\d+\s*hours?\b",
    r"\bwork\s+for\s+free\b",
    r"\bоплата\s+только\s+(?:крипт|криптовалют)\w*\b",
)


#: Статусы источника, означающие "заказ больше не открыт" (P2).
#: Contra использует EXPIRED и CLOSED для архивных записей.
_CLOSED_SOURCE_STATUSES = {
    "expired",
    "closed",
    "cancelled",
    "canceled",
    "filled",
    "deleted",
    "archived",
    "completed",
    # Phase 4C.4: kwork stop/user_stop = not accepting proposals (verified).
    "stopped",
    "user_stopped",
    # Phase 4D.2: legacy stored tokens, same verified meaning.
    "stop",
    "user_stop",
}


@dataclass
class GateResult:
    """Результат проверки gates для одного гига."""

    rejected: bool = False
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def add(self, reason: RejectionReason, detail: str) -> None:
        self.rejected = True
        if reason.value not in self.reasons:
            self.reasons.append(reason.value)
        self.details.setdefault(reason.value, []).append(detail)

    def to_dict(self) -> dict[str, Any]:
        return {"rejected": self.rejected, "reasons": list(self.reasons), "details": dict(self.details)}


def _matches(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        found = re.search(pattern, text, flags=re.IGNORECASE)
        if found:
            return found.group(0).strip()
    return None


def evaluate_hard_gates(
    gig: Gig,
    *,
    duplicate_reason: str | None = None,
    min_budget: float | None = None,
    reject_closed_source: bool = True,
) -> GateResult:
    """Прогоняет все hard gates.

    ``min_budget`` — порог осмысленности цены (в валюте заказа); ``None``
    означает "не проверять дискретность" (например, для почасовых).

    ``reject_closed_source`` (P2): по умолчанию заказ, который источник сам
    помечает закрытым, отклоняется — его нельзя взять. Аналитический режим
    (``False``) пропускает такие записи в scoring, чтобы можно было посмотреть
    и отранжировать весь live-набор; признак закрытости при этом остаётся виден
    в ``source_status`` и в рисках.
    """
    result = GateResult()
    text = f"{gig.title}\n{gig.description}".lower()

    # 0. Дубликат — проверяется pipeline'ом до gates, но остаётся gate'ом.
    if duplicate_reason:
        result.add(RejectionReason.DUPLICATE, duplicate_reason)

    # 0b. Источник сам сказал, что заказ закрыт (P2). Это факт источника, а не
    #     наша догадка: без гейта архивная выдача выглядела бы как доступные
    #     заказы. Причина machine-readable и видна в gig_events.
    if reject_closed_source and gig.source_status and str(gig.source_status).strip().lower() in _CLOSED_SOURCE_STATUSES:
        result.add(
            RejectionReason.SOURCE_CLOSED,
            f"source reports status={gig.source_status}",
        )

    # 1. Доступ к площадке/заказу.
    access_hit = _matches(text, _ACCESS_BLOCKED_PATTERNS)
    if access_hit:
        result.add(RejectionReason.ACCESS_UNAVAILABLE, access_hit)

    # 2. Оплата: явно установленный блок или неподходящий способ оплаты.
    if gig.payment_readiness is PaymentReadiness.BLOCKED:
        result.add(RejectionReason.PAYMENT_UNAVAILABLE, "source-reported payment_readiness=blocked")
    payment_hit = _matches(text, _PAYMENT_BLOCKED_PATTERNS)
    if payment_hit:
        result.add(RejectionReason.PAYMENT_UNAVAILABLE, payment_hit)

    # 3. Ограничение по стране/резидентству.
    restriction_hit = _matches(text, _COUNTRY_RESTRICTION_PATTERNS)
    if restriction_hit:
        result.add(RejectionReason.COUNTRY_RESTRICTION, restriction_hit)
    if gig.client_country and gig.client_country.strip().lower() in _BLOCKED_CLIENT_COUNTRIES:
        result.add(RejectionReason.COUNTRY_RESTRICTION, f"client country: {gig.client_country}")

    # 4. Физически невыполнимые требования.
    undeliverable_hit = _matches(text, _UNDELIVERABLE_PATTERNS)
    if undeliverable_hit:
        result.add(RejectionReason.UNDELIVERABLE, undeliverable_hit)

    # 5. Цена отсутствует или бессмысленна — только там, где источник это
    #    позволяет определить (политика источника: у fixture/Upwork-подобных
    #    заказов бюджет обязателен, у hourly он может быть диапазоном).
    if gig.budget_type is BudgetType.HOURLY:
        if gig.budget_min is None and gig.budget_max is None:
            result.add(RejectionReason.PRICE_MISSING, "hourly gig without any rate")
        elif max(gig.budget_min or 0.0, gig.budget_max or 0.0) <= 0:
            result.add(RejectionReason.PRICE_MISSING, f"non-positive hourly rate: {gig.budget_text}")
    elif min_budget is not None:
        if gig.budget_min is None and gig.budget_max is None:
            result.add(RejectionReason.PRICE_MISSING, "no budget published")
        else:
            ceiling = max(gig.budget_max or 0.0, gig.budget_min or 0.0)
            if ceiling < float(min_budget):
                result.add(
                    RejectionReason.PRICE_MISSING,
                    f"budget ceiling {ceiling:g} below policy floor {float(min_budget):g}",
                )
    elif gig.budget_min is None and gig.budget_max is None:
        result.add(RejectionReason.PRICE_MISSING, "no budget published")

    # 6. Скам/спам.
    scam_hit = _matches(text, _SCAM_PATTERNS)
    if scam_hit:
        result.add(RejectionReason.SCAM_RISK, scam_hit)

    return result
