"""Order-language resolution for freelance gigs (Phase 5D).

The internal Telegram UI stays Russian; the *external* reply to a client must
match the order's own language. Before this module there was no language signal
anywhere in ``ai_assistant/gig/`` — the same Russian pitch template went to
Freelancer.com and Contra (English marketplaces) because ``gig.source`` was
known at generation time but only ever used for the card label.

This module is the single, testable source of truth for "what language should
this order's reply be written in". It is deliberately dependency-free and
network-free; it never guesses content, only the language of the reply.
"""
from __future__ import annotations

import re

# Languages this project can write replies in. Kept small on purpose.
LANG_RU = "ru"
LANG_EN = "en"
SUPPORTED_LANGS = (LANG_RU, LANG_EN)

# Marketplace → language, used as a tie-breaker when the order text is too
# short/ambiguous to detect (e.g. a title that is just a product name).
# Freelancer.com and Contra are English-speaking marketplaces; Kwork is RU.
SOURCE_LANGUAGE_DEFAULTS: dict[str, str] = {
    "freelancer": LANG_EN,
    "contra": LANG_EN,
    "kwork": LANG_RU,
    "tg_freelance_orders": LANG_RU,
    "tg_zakazy_it": LANG_RU,
    "telegram": LANG_RU,
}

# Default when neither text nor source gives a signal. Conservative: the
# original behaviour was Russian, so unknown stays Russian (no regression).
DEFAULT_LANG = LANG_RU

_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN = re.compile(r"[a-z]", re.IGNORECASE)


def detect_text_language(text: str) -> str | None:
    """Return ``"ru"``/``"en"`` by letter counts, or ``None`` when unclear.

    Cyrillic vs Latin letter counting is enough here (same approach already
    used by ``hh_submit_policy.detect_text_language`` and
    ``hh_message_reply.detect_language``). Returns ``None`` — not a guess —
    when there are no letters at all or the two scripts are near-tied, so the
    caller can fall back to the marketplace default instead of mislabelling.
    """
    if not text:
        return None
    cyr = len(_CYRILLIC.findall(text))
    lat = len(_LATIN.findall(text))
    if cyr == 0 and lat == 0:
        return None
    if cyr >= lat * 2:
        return LANG_RU
    if lat >= cyr * 2:
        return LANG_EN
    # Mixed but not decisive: pick the majority script.
    if cyr > lat:
        return LANG_RU
    if lat > cyr:
        return LANG_EN
    return None


def source_default_language(source: str | None) -> str:
    """Marketplace-level language fallback (never ``None``)."""
    key = (source or "").strip().lower()
    if key in SOURCE_LANGUAGE_DEFAULTS:
        return SOURCE_LANGUAGE_DEFAULTS[key]
    for marker, lang in SOURCE_LANGUAGE_DEFAULTS.items():
        if marker and marker in key:
            return lang
    return DEFAULT_LANG


def resolve_order_language(
    title: str,
    description: str = "",
    source: str | None = None,
) -> str:
    """Resolve the reply language for a gig.

    Order of authority:
    1. Detected language of the title+description (the actual order text).
    2. Marketplace default (freelancer/contra → en, kwork/telegram → ru).
    3. Global default (ru).
    """
    detected = detect_text_language(f"{title or ''} {description or ''}")
    if detected in SUPPORTED_LANGS:
        return detected
    return source_default_language(source)


__all__ = [
    "DEFAULT_LANG",
    "LANG_EN",
    "LANG_RU",
    "SOURCE_LANGUAGE_DEFAULTS",
    "SUPPORTED_LANGS",
    "detect_text_language",
    "resolve_order_language",
    "source_default_language",
]
