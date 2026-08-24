from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from pydantic import BaseModel, Field

from . import config
from .db import get_connection, init_db
from .schema import Vacancy

logger = logging.getLogger(__name__)

IDENTITY_VERSION = "v1"

# Tracking parameters to remove from URLs
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "ref", "source", "campaign", "fbclid", "gclid", "gclsrc", "mc_cid", "mc_eid",
    "_ga", "_gl", "yclid", "msclkid", "dclid", "irgwc", "aff", "aff_id",
    "referrer", "share", "via", "entry", "position", "searchId", "search_id",
    "utm_id", "utm_reader", "utm_name", "utm_social", "utm_social_type"
}


class MatchType(str, Enum):
    EXACT = "EXACT"
    PROBABLE = "PROBABLE"
    DISTINCT = "DISTINCT"


@dataclass
class IdentityMatch:
    canonical_id: Optional[str]
    match_type: MatchType
    confidence: int  # 0-100
    reasons: List[str]
    existing_canonical: Optional[Dict[str, Any]] = None


@dataclass
class CanonicalVacancy:
    canonical_id: str
    normalized_url: str
    normalized_company: str
    normalized_title: str
    location: Optional[str]
    first_seen_at: str
    last_seen_at: str


def normalize_url(url: str) -> str:
    """
    Deterministically normalize a URL by:
    - Removing tracking parameters
    - Normalizing scheme, hostname, path, query order
    - Removing fragment
    - Preserving actual job identifiers
    """
    if not url:
        return ""
    
    try:
        parsed = urlparse(url)
        
        # Normalize scheme and hostname
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        
        # Normalize path (remove trailing slash except root)
        path = parsed.path
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        
        # Parse and filter query parameters
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        filtered_params = {
            k: v for k, v in query_params.items()
            if k.lower() not in TRACKING_PARAMS
        }
        
        # Sort query parameters for deterministic ordering
        sorted_query = urlencode(sorted(filtered_params.items()), doseq=True)
        
        # Remove fragment
        fragment = ""
        
        normalized = urlunparse((scheme, hostname, path, "", sorted_query, fragment))
        return normalized
    except Exception:
        return url


def normalize_company(company: str) -> str:
    """
    Deterministically normalize company name:
    - Remove common suffixes
    - Normalize punctuation and whitespace
    - Lowercase
    """
    if not company:
        return ""
    
    # Normalize unicode
    normalized = unicodedata.normalize("NFKC", company)
    
    # Remove common corporate suffixes
    suffixes = [
        r"\binc\.?\b", r"\bcorp\.?\b", r"\bcorporation\b", r"\bllc\.?\b",
        r"\bltd\.?\b", r"\blimited\b", r"\bco\.?\b", r"\bcompany\b",
        r"\bgmbh\b", r"\bag\b", r"\bsa\b", r"\bpty\.?\b"
    ]
    
    for suffix in suffixes:
        normalized = re.sub(suffix, "", normalized, flags=re.IGNORECASE)
    
    # Normalize punctuation and whitespace
    normalized = re.sub(r"[,\.\']", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip().lower()
    
    return normalized


def normalize_title(title: str) -> str:
    """
    Deterministically normalize job title:
    - Lowercase
    - Normalize unicode
    - Normalize punctuation and whitespace
    """
    if not title:
        return ""
    
    # Normalize unicode
    normalized = unicodedata.normalize("NFKC", title)
    
    # Normalize punctuation
    normalized = re.sub(r"[/\-\|\\]", " ", normalized)
    normalized = re.sub(r"[\(\)\[\]\{\}]", " ", normalized)
    normalized = re.sub(r"[,\.\':;]", " ", normalized)
    
    # Normalize whitespace
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip().lower()
    
    return normalized


def calculate_similarity(a: str, b: str) -> float:
    """
    Calculate deterministic similarity between two strings using SequenceMatcher.
    Returns value 0.0 to 1.0.
    """
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_exact_duplicate(normalized_url: str, existing: CanonicalVacancy) -> bool:
    """Check if URL matches exactly (after normalization)."""
    return normalized_url == existing.normalized_url


def is_probable_duplicate(
    normalized_company: str,
    normalized_title: str,
    location: Optional[str],
    existing: CanonicalVacancy,
    company_threshold: float = 0.95,
    title_threshold: float = 0.85
) -> Tuple[bool, int, List[str]]:
    """
    Check if vacancy is a probable duplicate.
    Returns (is_probable, confidence, reasons).
    """
    reasons = []
    confidence = 0
    
    # Company match (high weight)
    company_sim = calculate_similarity(normalized_company, existing.normalized_company)
    if company_sim >= company_threshold:
        reasons.append(f"Company match: {company_sim:.0%} similarity")
        confidence += 40
    else:
        return False, 0, ["Company similarity below threshold"]
    
    # Title match (high weight)
    title_sim = calculate_similarity(normalized_title, existing.normalized_title)
    if title_sim >= title_threshold:
        reasons.append(f"Title match: {title_sim:.0%} similarity")
        confidence += 40
    else:
        return False, 0, ["Title similarity below threshold"]
    
    # Location match (bonus)
    loc_sim = 0.0
    if location and existing.location:
        loc_sim = calculate_similarity(location.lower().strip(), existing.location.lower().strip())
        if loc_sim >= 0.9:
            reasons.append(f"Location match: {loc_sim:.0%} similarity")
            confidence += 20
        elif loc_sim < 0.5:
            reasons.append(f"Location mismatch: {loc_sim:.0%} similarity")
            confidence -= 10
    elif not location or not existing.location:
        reasons.append("Location not available for comparison")
    
    return confidence >= 70, min(100, max(0, confidence)), reasons


def is_distinct(normalized_company: str, normalized_title: str, existing: CanonicalVacancy) -> bool:
    """Check if vacancy is distinct from existing."""
    company_sim = calculate_similarity(normalized_company, existing.normalized_company)
    title_sim = calculate_similarity(normalized_title, existing.normalized_title)
    return company_sim < 0.9 or title_sim < 0.7


def _generate_canonical_id(normalized_url: str, normalized_company: str, normalized_title: str) -> str:
    """Generate deterministic canonical ID from normalized components."""
    import hashlib
    content = f"{normalized_url}|{normalized_company}|{normalized_title}"
    return "canonical_" + hashlib.sha256(content.encode()).hexdigest()[:16]


def save_canonical_vacancy(canonical: CanonicalVacancy) -> None:
    """Save canonical vacancy to database."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO canonical_vacancies (canonical_id, normalized_url, normalized_company, normalized_title, location, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_id) DO UPDATE SET
            normalized_url=excluded.normalized_url,
            normalized_company=excluded.normalized_company,
            normalized_title=excluded.normalized_title,
            location=excluded.location,
            last_seen_at=excluded.last_seen_at
    """, (
        canonical.canonical_id,
        canonical.normalized_url,
        canonical.normalized_company,
        canonical.normalized_title,
        canonical.location,
        canonical.first_seen_at,
        canonical.last_seen_at,
    ))
    conn.commit()
    conn.close()


def save_vacancy_alias(
    canonical_id: str,
    vacancy_stable_id: str,
    source: str,
    source_url: str,
    normalized_url: str,
    match_type: MatchType,
    confidence: int
) -> None:
    """Save vacancy alias mapping."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO vacancy_aliases (canonical_id, vacancy_stable_id, source, source_url, normalized_url, match_type, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_id, vacancy_stable_id) DO UPDATE SET
            source=excluded.source,
            source_url=excluded.source_url,
            normalized_url=excluded.normalized_url,
            match_type=excluded.match_type,
            confidence=excluded.confidence
    """, (
        canonical_id,
        vacancy_stable_id,
        source,
        source_url,
        normalized_url,
        match_type.value,
        confidence,
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    conn.close()


def get_canonical_by_id(canonical_id: str) -> Optional[CanonicalVacancy]:
    """Get canonical vacancy by ID."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT canonical_id, normalized_url, normalized_company, normalized_title, location, first_seen_at, last_seen_at
        FROM canonical_vacancies WHERE canonical_id=?
    """, (canonical_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return CanonicalVacancy(
        canonical_id=row[0],
        normalized_url=row[1],
        normalized_company=row[2],
        normalized_title=row[3],
        location=row[4],
        first_seen_at=row[5],
        last_seen_at=row[6],
    )


def get_canonical_by_normalized_url(normalized_url: str) -> Optional[CanonicalVacancy]:
    """Get canonical vacancy by normalized URL."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT canonical_id, normalized_url, normalized_company, normalized_title, location, first_seen_at, last_seen_at
        FROM canonical_vacancies WHERE normalized_url=?
    """, (normalized_url,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return CanonicalVacancy(
        canonical_id=row[0],
        normalized_url=row[1],
        normalized_company=row[2],
        normalized_title=row[3],
        location=row[4],
        first_seen_at=row[4],
        last_seen_at=row[5],
    )


def get_all_canonical_vacancies() -> List[CanonicalVacancy]:
    """Get all canonical vacancies."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT canonical_id, normalized_url, normalized_company, normalized_title, location, first_seen_at, last_seen_at
        FROM canonical_vacancies
    """)
    rows = cur.fetchall()
    conn.close()
    return [CanonicalVacancy(
        canonical_id=row[0],
        normalized_url=row[1],
        normalized_company=row[2],
        normalized_title=row[3],
        location=row[4],
        first_seen_at=row[4],
        last_seen_at=row[5],
    ) for row in rows]


def get_aliases_for_canonical(canonical_id: str) -> List[Dict[str, Any]]:
    """Get all aliases for a canonical vacancy."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT canonical_id, vacancy_stable_id, source, source_url, normalized_url, match_type, confidence, created_at
        FROM vacancy_aliases WHERE canonical_id=?
    """, (canonical_id,))
    rows = cur.fetchall()
    conn.close()
    return [{
        "canonical_id": row[0],
        "vacancy_stable_id": row[1],
        "source": row[2],
        "source_url": row[3],
        "normalized_url": row[4],
        "match_type": row[5],
        "confidence": row[6],
        "created_at": row[7],
    } for row in rows]


def resolve_vacancy_identity(vacancy: Vacancy) -> IdentityMatch:
    """
    Resolve a vacancy to its canonical identity.
    Returns IdentityMatch with canonical_id, match_type, confidence, reasons.
    Does NOT auto-merge PROBABLE duplicates.
    """
    init_db()
    
    # Normalize inputs
    normalized_url = normalize_url(vacancy.job_url)
    normalized_company = normalize_company(vacancy.company)
    normalized_title = normalize_title(vacancy.title)
    location = vacancy.location.strip().lower() if vacancy.location else None
    
    # First check exact URL match
    existing = get_canonical_by_normalized_url(normalized_url)
    if existing:
        return IdentityMatch(
            canonical_id=existing.canonical_id,
            match_type=MatchType.EXACT,
            confidence=100,
            reasons=[f"Normalized URL matches existing canonical vacancy: {existing.canonical_id}"],
            existing_canonical={
                "canonical_id": existing.canonical_id,
                "company": existing.normalized_company,
                "title": existing.normalized_title,
                "location": existing.location,
            }
        )
    
    # Check probable duplicates
    all_canonical = get_all_canonical_vacancies()
    best_match = None
    best_confidence = 0
    best_reasons = []
    
    for canon in all_canonical:
        is_probable, confidence, reasons = is_probable_duplicate(
            normalized_company, normalized_title, location, existing=canon
        )
        if is_probable and confidence > best_confidence:
            best_match = canon
            best_confidence = confidence
            best_reasons = reasons
    
    if best_match:
        return IdentityMatch(
            canonical_id=best_match.canonical_id,
            match_type=MatchType.PROBABLE,
            confidence=best_confidence,
            reasons=best_reasons + ["PROBABLE duplicate - manual review required before merge"],
            existing_canonical={
                "canonical_id": best_match.canonical_id,
                "company": best_match.normalized_company,
                "title": best_match.normalized_title,
                "location": best_match.location,
            }
        )
    
    # Distinct - create new canonical
    canonical_id = _generate_canonical_id(normalized_url, normalized_company, normalized_title)
    now = datetime.utcnow().isoformat()
    
    canonical = CanonicalVacancy(
        canonical_id=canonical_id,
        normalized_url=normalized_url,
        normalized_company=normalized_company,
        normalized_title=normalized_title,
        location=location,
        first_seen_at=now,
        last_seen_at=now,
    )
    save_canonical_vacancy(canonical)
    
    return IdentityMatch(
        canonical_id=canonical_id,
        match_type=MatchType.DISTINCT,
        confidence=100,
        reasons=["New distinct vacancy - no matches found"],
        existing_canonical=None
    )


def sync_identity_from_vacancies() -> Dict[str, int]:
    """
    Sync canonical identity from all existing vacancies.
    Idempotent - can be run multiple times.
    Returns stats: {created, exact_duplicates, probable_duplicates, distinct}
    """
    from .db import list_vacancies
    
    init_db()
    stats = {"created": 0, "exact_duplicates": 0, "probable_duplicates": 0, "distinct": 0}
    
    rows = list_vacancies(limit=10000)
    for row in rows:
        from .db import _row_to_vacancy
        vac = _row_to_vacancy(row)
        
        result = resolve_vacancy_identity(vac)
        
        # Save alias
        save_vacancy_alias(
            canonical_id=result.canonical_id,
            vacancy_stable_id=vac.stable_id(),
            source=vac.source,
            source_url=vac.job_url,
            normalized_url=normalize_url(vac.job_url),
            match_type=result.match_type,
            confidence=result.confidence,
        )
        
        if result.match_type == MatchType.EXACT:
            stats["exact_duplicates"] += 1
        elif result.match_type == MatchType.PROBABLE:
            stats["probable_duplicates"] += 1
        elif result.match_type == MatchType.DISTINCT:
            stats["distinct"] += 1
            stats["created"] += 1
    
    return stats