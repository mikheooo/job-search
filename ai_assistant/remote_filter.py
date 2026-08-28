"""Strict Remote-Only Vacancy Filter.

Enforces zero-tolerance validation for confirmed fully remote positions.
Rejects hybrid, onsite, office, conditional ('можно удаленно', 'по договоренности'),
probation-delayed, and ambiguous/unknown work formats.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from .schema import Vacancy


# Known sources that exclusively host remote positions (unless explicitly overridden by description)
_EXCLUSIVELY_REMOTE_SOURCES = {"remoteok", "weworkremotely", "himalayas"}


# --- REJECT PATTERNS (Negative Signals) ---
# Any match immediately rejects the vacancy regardless of other mentions.

_REJECT_HYBRID_PATTERNS = [
    r"\bгибрид\w*",  # гибрид, гибридный, гибридная, гибридный график, гибридный формат
    r"\bhybrid\b",
    r"\bhybrid[- ]remote\b",
    r"\bpartially\s+remote\b",
    r"\bpartial\s+remote\b",
    r"\bhybrid\s+(?:working|work|schedule|role|model|position|environment|setup)\b",
    r"\bчастичн\w*\s+удален\w*",  # частично удаленно
    r"\bсмешанн\w*\s+формат\b",  # смешанный формат
]

_REJECT_OFFICE_PATTERNS = [
    r"\bonsite\b",
    r"\bon-site\b",
    r"\bin-office\b",
    r"\bin\s+office\b",
    r"\boffice-based\b",
    r"\boffice\s+based\b",
    r"\boffice\s+only\b",
    r"\b(?:work|working)\s+(?:from|in|at)\s+(?:\w+\s+){0,3}office\b",  # work from our Berlin office, work in office
    r"\bна\s+территории\s+работодателя\b",
    r"\bработ\w*\s+в\s+офис\w*",  # работа в офисе
    r"\bтолько\s+(?:в\s+)?офис\w*",  # только офис / только в офисе
    r"\bв\s+(?:наш|нашем|московском|питерском|спб|берлинском|лондонском)\s+офис\w*",
    r"\bв\s+офис\w*",  # в офис, в офисе, в офисы
    r"\bофисный\s+(?:график|формат|режим)\b",
]

_REJECT_OFFICE_VISITS_PATTERNS = [
    r"посещени\w*\s+(?:нашего\s+)?офис\w*",  # посещение офиса
    r"посещать\s+(?:наш\s+)?офис\w*",  # посещать офис
    r"приезжа\w*\s+(?:в\s+)?(?:наш\s+)?офис\w*",  # приезжать в офис
    r"приезд\w*\s+в\s+офис\w*",  # приезды в офис
    r"присутстви\w*\s+(?:в\s+)?офис\w*",  # присутствие в офисе
    r"присутствовать\s+в\s+офис\w*",  # присутствовать в офисе
    r"(?:1|2|3|4|\d+)\s*(?:дня?|дней|раза?|раз)\s*(?:в\s+неделю|в\s+месяц)?\s*(?:\w+\s+){0,3}(?:в\s+)?офис\w*",
    r"\d+\s*days?\s+(?:a|per)\s+week\s+(?:in|at)\s+(?:the\s+|our\s+)?office",
    r"(?:visit|come\s+to|attend|go\s+to)\s+(?:\w+\s+){0,3}office",
    r"office\s+attendance",
    r"office\s+presence",
    r"office\s+days",
    r"in-office\s+days",
]

_REJECT_CONDITIONAL_PATTERNS = [
    r"можно\s+(?:\w+\s+){0,2}удален\w*",  # «можно удаленно», «можно удалённо»
    r"возможн\w*(?:\s+\w+){0,2}\s+удален\w*",  # «возможна удаленная работа», «возможно удаленно»
    r"возможность\s+(?:\w+\s+){0,2}удален\w*",  # возможность удаленной работы
    r"удален\w*(?:\s+\w+){0,3}\s+по\s+договоренност\w*",  # «удаленно по договоренности»
    r"по\s+договоренност\w*",  # «по договоренности»
    r"удален\w*(?:\s+\w+){0,3}\s+после\s+испытательн\w*",  # «удаленная работа после испытательного срока»
    r"после\s+испытательн\w*(?:\s+\w+){0,3}\s+удален\w*",  # «после испытательного срока удаленка»
    r"remote\s+optional\b",
    r"remote\s+possible\b",
    r"remote\s+considered\b",
    r"remote\s+negotiable\b",
    r"open\s+to\s+remote\b",
    r"remote\s+after\s+probation\b",
    r"remote\s+after\s+trial\b",
]

_COMPILED_REJECT = [
    re.compile(p, re.IGNORECASE)
    for p in (
        _REJECT_HYBRID_PATTERNS
        + _REJECT_OFFICE_PATTERNS
        + _REJECT_OFFICE_VISITS_PATTERNS
        + _REJECT_CONDITIONAL_PATTERNS
    )
]


# --- POSITIVE CONFIRMATION PATTERNS ---
# At least one must match for non-exclusively remote platforms.

_PASS_RUSSIAN_PATTERNS = [
    r"\bудаленн(?:ая|ый|ое|ую|ые|ого|ой)\s+работ\w*",  # удаленная работа
    r"\bудаленно\b",
    r"\bудаленка\b",
    r"\bудаленный\s+(?:формат|режим|график)\b",
    r"\bформат\s*(?:работы)?\s*:\s*(?:полная\s+|полностью\s+)?удален\w*",
    r"\bтолько\s+удален\w*",
    r"\bполная\s+удален\w*",
    r"\bполностью\s+удален\w*",
    r"\bстрого\s+удален\w*",
    r"\bдистанционн(?:ая|ый|ое|ую|ые|ого|ой)\s+работ\w*",
    r"\bдистанционно\b",
    r"\bдистанционный\s+(?:формат|режим|график)\b",
]

_PASS_ENGLISH_PATTERNS = [
    r"\bfully\s+remote\b",
    r"\b100%\s+remote\b",
    r"\b100\s*%\s*remote\b",
    r"\b100-percent\s+remote\b",
    r"\bremote-only\b",
    r"\bremote\s+only\b",
    r"\bstrictly\s+remote\b",
    r"\bentirely\s+remote\b",
    r"\bwork\s+remotely\b",
    r"\bremote\s+(?:work|position|role|job|opportunity)\b",
    r"\bfully\s+distributed\b",
    r"\bremote-first\b",
    r"\bremote\s+first\b",
    r"\ball-remote\b",
    r"\ball\s+remote\b",
    r"\bwork\s+from\s+anywhere\b",
    r"\banywhere\s+in\s+the\s+world\b",
    r"\bworldwide\s+remote\b",
    r"\bglobal\s+remote\b",
    r"\blocation\s*:\s*remote\b",
    r"\bremote\s*(?:\(worldwide\)|\(global\)|\(anywhere\)|worldwide|global|anywhere)\b",
]

_COMPILED_PASS = [
    re.compile(p, re.IGNORECASE)
    for p in (_PASS_RUSSIAN_PATTERNS + _PASS_ENGLISH_PATTERNS)
]

_EXPLICIT_REMOTE_LOCATIONS = {
    "remote",
    "remote worldwide",
    "remote (worldwide)",
    "remote (global)",
    "remote anywhere",
    "remote, worldwide",
    "anywhere",
    "worldwide",
    "global",
    "удаленно",
    "удаленная",
    "удаленная работа",
    "дистанционно",
}


def _sanitize_for_matching(text: str) -> str:
    """Pre-sanitize text, normalizing ё->е, whitespace, and protecting idioms like 'home office'."""
    if not text:
        return ""
    # Normalize Russian letter ё to е for uniform matching
    cleaned = text.replace("ё", "е").replace("Ё", "Е")
    # Normalize 'home office' or 'домашний офис' so it is not caught as a physical onsite office
    cleaned = re.sub(r"\bhome\s+office\b", "home_workspace", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bдомашн\w*\s+офис\w*", "домашнее_рабочее_место", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def classify_work_format(
    title: str = "",
    description: str = "",
    location: Optional[str] = None,
    source: Optional[str] = None,
    employment_type: Optional[str] = None,
    raw_data: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Classify work format with Strict Remote-Only guarantees.

    Returns:
        (True, "Confirmed fully remote") if strictly remote.
        (False, "<Reason for rejection>") if hybrid, office, conditional, or unknown/insufficient data.
    """
    src = (source or "").strip().lower()
    loc_raw = (location or "").strip()
    loc_norm = _sanitize_for_matching(loc_raw).lower()

    # Collect all textual representations
    raw_tags = ""
    if raw_data and isinstance(raw_data, dict):
        tags = raw_data.get("tags") or []
        if isinstance(tags, list):
            raw_tags = " ".join(str(t) for t in tags)

    combined_text = f"{title} {description} {loc_raw} {employment_type or ''} {raw_tags}"
    sanitized = _sanitize_for_matching(combined_text)

    # 1. REJECT CHECK: If ANY reject pattern matches -> REJECT immediately (Zero Tolerance)
    for pattern in _COMPILED_REJECT:
        match = pattern.search(sanitized)
        if match:
            return False, f"Rejected non-remote pattern: '{match.group(0)}'"

    # 2. Check Exclusively Remote Job Boards (RemoteOK, WeWorkRemotely, Himalayas)
    if src in _EXCLUSIVELY_REMOTE_SOURCES:
        return True, f"Confirmed remote from {src} job board"

    # 3. Check Location field explicit remote
    if loc_norm in _EXPLICIT_REMOTE_LOCATIONS:
        return True, f"Confirmed remote location: '{loc_raw}'"

    # 4. Check Positive Remote Patterns in text or location
    for pattern in _COMPILED_PASS:
        match = pattern.search(sanitized)
        if match:
            return True, f"Confirmed fully remote match: '{match.group(0)}'"

    # 5. UNKNOWN / Insufficient Data -> REJECT (Fail-Closed)
    if not loc_raw and not description.strip():
        return False, "Unknown work format: empty location and description"

    if loc_raw and not any(loc_norm.startswith(r) for r in ("remote", "удален", "дистанцион")):
        return False, f"Unknown/non-remote location '{loc_raw}' without confirmed remote description"

    return False, "Unknown/ambiguous work format: lack of confirmed remote-only evidence"


def is_strictly_remote(vacancy: Vacancy) -> Tuple[bool, str]:
    """Check if a Vacancy instance satisfies strict remote-only requirements."""
    if not isinstance(vacancy, Vacancy):
        return False, "Invalid vacancy instance"

    return classify_work_format(
        title=vacancy.title or "",
        description=vacancy.description or "",
        location=vacancy.location,
        source=vacancy.source,
        employment_type=vacancy.employment_type,
        raw_data=vacancy.raw_data,
    )
