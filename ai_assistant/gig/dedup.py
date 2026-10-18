"""Дедупликация гигов — полностью отдельная от дедупа вакансий.

Сигналы дубликата (по ТЗ §7):
1. ``source + source_id`` — уникальный индекс в БД и детерминированный id;
2. canonical URL — нормализованный URL (схема/wwww/tracking-параметры);
3. очень похожие title + description (SequenceMatcher на нормализованном
   тексте, консервативный порог);
4. повторно опубликованный тот же заказ — совпадение content_hash
   (title + description + skills + бюджет) у другого source_id того же
   источника.

Модуль не имеет побочных эффектов: он только выносит вердикт, запись в БД
совершает pipeline через ``store.save_gig`` (unique index) и ``gig_events``.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Gig
from .normalizer import content_hash

#: Порог похожести title (0..1). Высокий, чтобы не склеивать разные заказы.
TITLE_SIMILARITY_THRESHOLD = 0.92
#: Порог комбинированной похожести title+description.
BODY_SIMILARITY_THRESHOLD = 0.90
#: Сколько существующих гигов просматриваем для text-сравнения (срез последних).
DEDUP_SCAN_LIMIT = 1000

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "ref",
    "referrer",
}


def canonical_url(url: str) -> str:
    """Канонический URL: lower scheme/host, без 'www.', без фрагмента и
    трекинг-параметров, отсортированные query-параметры."""
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return str(url).strip()
    if not parts.netloc:
        return str(url).strip()
    scheme = parts.scheme.lower() or "https"
    host = parts.netloc.lower().removeprefix("www.")
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
    query_pairs.sort()
    return urlunsplit((scheme, host, parts.path or "/", urlencode(query_pairs), ""))


def _normalize_text(text: str) -> str:
    lowered = str(text or "").lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[^a-zа-яё0-9$€£₹ ]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def text_similarity(a: str, b: str) -> float:
    """Похожесть двух текстов 0..1 на нормализованных строках."""
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def find_duplicate(gig: Gig, existing: list[Gig]) -> tuple[Gig | None, str]:
    """Ищет дубликат ``gig`` среди ``existing``.

    Возвращает ``(дубликат|None, пояснение)``. Порядок проверок — от самых
    надёжных сигналов к эвристическим.
    """
    # 1. source + source_id (детерминированный id тоже сведён к этой паре).
    for other in existing:
        if other.id == gig.id:
            return other, f"same source+source_id: {gig.source}:{gig.source_id}"

    # 2. canonical URL.
    gig_url = canonical_url(gig.url)
    if gig_url:
        for other in existing:
            if other.url and canonical_url(other.url) == gig_url:
                return other, f"same canonical URL: {gig_url}"

    # 3/4. Похожий текст и повторная публикация (content_hash) — сравниваем
    # только с гигами того же источника: одинаковый заказ на двух разных
    # площадках — законное пересечение, а не дубль.
    gig_hash = content_hash(gig)
    gig_title = _normalize_text(gig.title)
    for other in existing:
        if other.source != gig.source:
            continue
        if other.id == gig.id:
            continue
        if gig_hash and content_hash(other) == gig_hash:
            return other, "reposted: identical content_hash"
        title_ratio = SequenceMatcher(None, gig_title, _normalize_text(other.title)).ratio() if gig_title and other.title else 0.0
        if title_ratio >= TITLE_SIMILARITY_THRESHOLD:
            body_ratio = text_similarity(gig.description, other.description)
            if body_ratio >= BODY_SIMILARITY_THRESHOLD or not gig.description or not other.description:
                return other, f"near-identical title+description (title~{title_ratio:.2f}, body~{body_ratio:.2f})"

    return None, ""


def fingerprint_of(gig: Gig) -> dict[str, str]:
    """Отладочная сводка для объяснения решения дедупа."""
    return {
        "id": gig.id,
        "canonical_url": canonical_url(gig.url),
        "content_hash": content_hash(gig),
    }
