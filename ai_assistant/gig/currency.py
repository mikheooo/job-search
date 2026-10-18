"""Конвертация валют для сопоставимости бюджетов и ставок.

ПРОБЛЕМА, КОТОРУЮ ЭТО РЕШАЕТ
----------------------------
Freelancer публикует бюджеты в валюте заказчика: INR, USD, AUD, GBP. Scoring сравнивал
числа напрямую, поэтому `12500-37500 INR` (примерно $130-390) получал тот же балл, что
`12500-37500 USD`, — завышение примерно в 80 раз. Прежний «штраф 15% за не-USD валюту»
эту разницу не покрывал.

ОТКУДА БЕРЁТСЯ КУРС
-------------------
Не из выдуманной таблицы. Freelancer сам отдаёт курс в каждом проекте:
``rawDocument.currencyDetails.exchangeRate`` (наблюдено: INR 0.010402, USD 1.0).
Адаптер кладёт его в ``source_facts`` как ``freelancer_currency_exchange_rate``, и
конверсия использует именно это число — проверяемое и привязанное к конкретной записи.

ЧЕГО МОДУЛЬ НЕ ДЕЛАЕТ
---------------------
Не угадывает. Если курса нет и валюта не USD, конверсия **невозможна**: функции
возвращают ``None``, и вызывающий обязан пометить значение как неизвестное, а не
подставить ноль или выдуманный курс. Ноль здесь означал бы «бюджета нет», что неверно.
"""
from __future__ import annotations

from typing import Any

#: Ключ, под которым адаптер источника сохраняет курс относительно USD.
RATE_FACT_KEYS: tuple[str, ...] = (
    "freelancer_currency_exchange_rate",
    "currency_exchange_rate",
    "exchange_rate",
)

#: Валюты, для которых конверсия не нужна.
BASE_CURRENCIES: frozenset[str] = frozenset({"USD", "US$", "$"})


def _to_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def exchange_rate_of(source_facts: dict[str, Any] | None, currency: str | None) -> float | None:
    """Курс валюты записи к USD, взятый у источника.

    Возвращает ``1.0`` для USD, курс из ``source_facts`` когда он там есть, иначе
    ``None``. ``None`` — это «курс неизвестен», а не «курс равен единице»: разница
    между ними и есть та ошибка, из-за которой INR читался как USD.
    """
    code = (currency or "").strip().upper()
    if code in BASE_CURRENCIES:
        return 1.0
    if not code:
        return None
    facts = source_facts or {}
    for key in RATE_FACT_KEYS:
        rate = _to_float(facts.get(key))
        if rate is not None:
            return rate
    return None


def to_usd(amount: float | None, currency: str | None, rate: float | None) -> float | None:
    """Переводит сумму в USD по курсу.

    Без курса и без USD-валюты вернёт ``None``: неизвестность не превращается в число.
    """
    if amount is None:
        return None
    effective = 1.0 if (currency or "").strip().upper() in BASE_CURRENCIES else rate
    if effective is None:
        return None
    return round(float(amount) * float(effective), 2)


def describe_conversion(currency: str | None, rate: float | None) -> str:
    """Короткая пометка для score breakdown: чем именно конвертировали."""
    code = (currency or "").strip().upper() or "?"
    if code in BASE_CURRENCIES:
        return "USD, конверсия не требуется"
    if rate is None:
        return f"валюта {code} без курса — ставка не сопоставлена"
    return f"валюта {code}, курс источника {rate:g} к USD"


__all__ = [
    "BASE_CURRENCIES",
    "RATE_FACT_KEYS",
    "describe_conversion",
    "exchange_rate_of",
    "to_usd",
]
