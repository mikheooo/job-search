"""Нормализация сырых данных источника в модель ``Gig``.

Правила:
- неизвестное -> ``None`` / ``UNKNOWN``, никогда не выдумываем значение;
- числа парсятся из строк вида "$1,500", "20-40 USD/hr", "1200 EUR";
- skills приводятся к нижнему регистру и дедуплицируются с сохранением
  порядка (порядок важен для детерминированных тестов и explainability).

Модуль не знает про сеть, БД и LLM — только преобразование dict -> Gig.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import (
    BudgetType,
    CanDeliver,
    Gig,
    GigStatus,
    PaymentReadiness,
    RiskLevel,
    SourceMode,
    make_gig_id,
)

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "usd": "USD",
    "us$": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "₽": "RUB",
    "rub": "RUB",
    "руб": "RUB",
    "inr": "INR",
    "₹": "INR",
    "uah": "UAH",
    "грн": "UAH",
    # P4: валюты, реально встречающиеся в выдаче источников, но отсутствовавшие здесь.
    # Без них ISO-код не распознавался, и при парсинге строки «$…» валюта молча
    # становилась USD — то есть HKD- или AUD-бюджет выглядел долларовым.
    "aud": "AUD",
    "a$": "AUD",
    "cad": "CAD",
    "c$": "CAD",
    "hkd": "HKD",
    "hk$": "HKD",
    "sgd": "SGD",
    "s$": "SGD",
    "nzd": "NZD",
    "chf": "CHF",
    "jpy": "JPY",
    "¥": "JPY",
    "cny": "CNY",
    "sek": "SEK",
    "pln": "PLN",
    "brl": "BRL",
    "mxn": "MXN",
    "zar": "ZAR",
    "aed": "AED",
    "php": "PHP",
    "idr": "IDR",
    "myr": "MYR",
    "try": "TRY",
    "ngn": "NGN",
    "pkr": "PKR",
    "bdt": "BDT",
}

#: ISO-4217 коды, которые мы знаем и считаем валютой. Нужен, чтобы голый трёхбуквенный
#: код в поле ``currency`` распознавался без символа рядом.
_ISO_CODES: frozenset[str] = frozenset(
    code for code in _CURRENCY_SYMBOLS.values()
) | frozenset({
    "DKK", "NOK", "CZK", "HUF", "RON", "ILS", "KRW", "TWD", "THB", "VND",
    "ARS", "CLP", "COP", "PEN", "UYU", "KZT", "UAH", "GEL", "EGP", "MAD",
})

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

#: Слова, указывающие на почасовую оплату.
_HOURLY_HINTS = ("/hr", "per hour", "hourly", "/h ", "p/h", "в час")
#: Слова, указывающие на фиксированную оплату.
_FIXED_HINTS = ("fixed", "fixed-price", "fixed price", "one-off", "budget:")


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    match = _NUMBER_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _to_int(raw: Any) -> int | None:
    value = _to_float(raw)
    if value is None:
        return None
    return int(value)


def _first_int(*candidates: Any) -> int | None:
    """Первое числовое значение из кандидатов, где ``0`` сохраняется как ``0``.

    Использовать ``a or b`` здесь нельзя: ноль ставок — это измерение («ставок
    пока нет»), а не отсутствие данных. Цепочка ``or`` превращала его в ``None``
    и заказ с нулём ставок выглядел как заказ с неизвестной конкуренцией.
    """
    for raw in candidates:
        if raw is None or isinstance(raw, bool):
            continue
        value = _to_int(raw)
        if value is not None:
            return value
    return None


def parse_currency(*candidates: Any) -> str | None:
    """Определяет ISO-код валюты по ISO-коду, символам или словам. Иначе ``None``.

    P4: сначала проверяется точный ISO-код (``HKD``, ``AUD``), и только потом символы.
    Раньше порядок был обратным, и строка вида ``"HK$499"`` могла свестись к USD
    из-за символа ``$``. Точный код всегда важнее символа.
    """
    # Проход 1: точный ISO-код в любом из кандидатов.
    for raw in candidates:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        # Точный трёхбуквенный код в любом месте строки ("USD", "USD 500",
        # "HK$499 HKD", "paid in EUR"). Ищем все совпадения и берём то, что
        # действительно является известным кодом валюты: так ``IHK`` из
        # "HK$499" не принимается за валюту.
        for match in re.finditer(r"\b([A-Za-z]{3})\b", text):
            code = match.group(1).upper()
            if code in _ISO_CODES:
                return code
        # Форма "HKD:499" — код приклеен к числу, \b не сработает.
        match = re.match(r"^([A-Za-z]{3})(?=[^A-Za-z]|$)", text)
        if match and match.group(1).upper() in _ISO_CODES:
            return match.group(1).upper()

    # Проход 2: символы и слова.
    for raw in candidates:
        if raw is None:
            continue
        text = str(raw).strip().lower()
        if not text:
            continue
        for token, code in _CURRENCY_SYMBOLS.items():
            if token in text:
                return code
    return None


def parse_budget_range(raw: Any) -> tuple[float | None, float | None, BudgetType]:
    """Парсит бюджет из строки/числа/dict.

    Возвращает ``(min, max, budget_type)``. Диапазон "20-40" -> (20, 40),
    одиночное значение -> (v, v). Если данных нет -> (None, None, UNKNOWN).
    """
    if raw is None:
        return None, None, BudgetType.UNKNOWN
    if isinstance(raw, dict):
        low = _to_float(raw.get("min") or raw.get("budget_min") or raw.get("amount_min"))
        high = _to_float(raw.get("max") or raw.get("budget_max") or raw.get("amount_max"))
        btype = BudgetType.parse(raw.get("type") or raw.get("budget_type"))
        if btype is BudgetType.UNKNOWN:
            btype = BudgetType.FIXED if (low is not None or high is not None) else BudgetType.UNKNOWN
        return low, high, btype

    text = str(raw).strip()
    if not text:
        return None, None, BudgetType.UNKNOWN

    lowered = text.lower()
    btype = BudgetType.UNKNOWN
    if any(h in lowered for h in _HOURLY_HINTS):
        btype = BudgetType.HOURLY
    elif any(h in lowered for h in _FIXED_HINTS):
        btype = BudgetType.FIXED

    numbers = [float(n.replace(",", "")) for n in _NUMBER_RE.findall(text.replace(",", ""))]
    if not numbers:
        return None, None, btype
    if btype is BudgetType.UNKNOWN:
        btype = BudgetType.FIXED
    if len(numbers) >= 2 and re.search(r"[-–—]|\bto\b", lowered):
        return numbers[0], numbers[1], btype
    return numbers[0], numbers[0], btype


def normalize_skills(raw: Any) -> list[str]:
    """Список навыков в нижнем регистре, без дублей, с сохранением порядка."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,;|/\n]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        token = str(part).strip().lower()
        # убираем хвосты вида "python (3 years)"
        token = re.sub(r"\s*\([^)]*\)\s*$", "", token).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def normalize_published_at(raw: Any) -> str | None:
    """Приводит дату публикации к ISO-8601 UTC. Непонятное -> ``None``."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(raw, (int, float)):
        # эвристика: unix seconds vs ms
        value = float(raw)
        if value > 1e11:
            value /= 1000.0
        try:
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    text = str(raw).strip()
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_gig(raw: dict[str, Any]) -> Gig:
    """Сырой dict источника -> нормализованный ``Gig``.

    Обязательные для идентичности поля — ``source`` и ``source_id``.
    Отсутствие любых прочих полей допустимо и приводит к ``None``/``UNKNOWN``.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"normalize_gig ожидает dict, получено {type(raw)!r}")

    source = str(raw.get("source") or "").strip().lower()
    source_id = str(raw.get("source_id") or raw.get("id") or "").strip()
    if not source or not source_id:
        raise ValueError("normalize_gig: обязательны непустые source и source_id")

    budget_min, budget_max, budget_type = parse_budget_range(
        raw.get("budget") if raw.get("budget") is not None else {
            "min": raw.get("budget_min"),
            "max": raw.get("budget_max"),
            "type": raw.get("budget_type"),
        }
    )

    currency = parse_currency(
        raw.get("currency"),
        raw.get("budget"),
        raw.get("budget_text"),
    )

    published = normalize_published_at(raw.get("published_at") or raw.get("posted_at"))

    client = raw.get("client") if isinstance(raw.get("client"), dict) else {}
    client_name = raw.get("client_name") or client.get("name")
    client_country = raw.get("client_country") or client.get("country")
    client_rating = _to_float(
        raw.get("client_rating") if raw.get("client_rating") is not None else client.get("rating")
    )
    client_jobs_posted = _to_int(
        raw.get("client_jobs_posted") if raw.get("client_jobs_posted") is not None else client.get("jobs_posted")
    )
    client_hires = _to_int(
        raw.get("client_hires") if raw.get("client_hires") is not None else client.get("hires")
    )
    client_total_spent = _to_float(
        raw.get("client_total_spent") if raw.get("client_total_spent") is not None else client.get("total_spent")
    )

    gig = Gig(
        id=str(raw.get("gig_id") or "") or make_gig_id(source, source_id),
        source=source,
        source_id=source_id,
        url=str(raw.get("url") or raw.get("link") or "").strip(),
        # P2 provenance (unknown when the source does not declare it)
        source_mode=SourceMode.parse(raw.get("source_mode")),
        source_scope=(str(raw["source_scope"]).strip() or None) if raw.get("source_scope") else None,
        source_url=(str(raw["source_url"]).strip() or None) if raw.get("source_url") else None,
        fetched_at=normalize_published_at(raw.get("fetched_at")) if raw.get("fetched_at") else None,
        source_status=(str(raw["source_status"]).strip() or None) if raw.get("source_status") else None,
        platform_payment_options=[
            str(item).strip() for item in (raw.get("platform_payment_options") or []) if str(item).strip()
        ],
        source_facts=dict(raw.get("source_facts") or {}),
        derived_fields=[str(item) for item in (raw.get("derived_fields") or [])],
        title=str(raw.get("title") or "").strip(),
        description=str(raw.get("description") or raw.get("details") or "").strip(),
        category=(str(raw["category"]).strip() or None) if raw.get("category") else None,
        skills=normalize_skills(raw.get("skills") or raw.get("tags")),
        budget_min=budget_min,
        budget_max=budget_max,
        currency=currency,
        budget_type=budget_type,
        published_at=published,
        client_name=(str(client_name).strip() or None) if client_name else None,
        client_country=(str(client_country).strip() or None) if client_country else None,
        client_rating=client_rating,
        client_jobs_posted=client_jobs_posted,
        client_hires=client_hires,
        client_total_spent=client_total_spent,
        competition=_first_int(
            raw.get("competition"), raw.get("proposals"), raw.get("bids")
        ),
        proposal_cost=_to_float(raw.get("proposal_cost") or raw.get("connects_cost")),
        # Источник может уже сообщать явную непроходимость оплаты — уважаем это.
        payment_readiness=PaymentReadiness.parse(raw.get("payment_readiness")),
        can_deliver=CanDeliver.parse(raw.get("can_deliver")),
        risk_level=RiskLevel.parse(raw.get("risk_level")),
        risks=list(raw.get("risks") or []),
        status=GigStatus.parse(raw.get("status")),
        created_at=_utcnow(),
        updated_at=_utcnow(),
        raw_data=dict(raw),
    )
    return gig


def content_hash(gig: Gig) -> str:
    """Хэш содержимого для обнаружения перепубликации того же заказа."""
    import hashlib

    skill_key = ",".join(sorted(gig.skills))
    payload = "|".join(
        [
            gig.title.lower().strip(),
            gig.description.lower().strip()[:4000],
            skill_key,
            str(gig.budget_min),
            str(gig.budget_max),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
