"""Candidate context builder with honesty labels (Phase 5A §3).

VERIFIED — CONFIRMED_BY_USER claims + profile core skills.
CHECKABLE — DERIVED_FROM_SOURCE claims (candidate must verify before citing).
REJECTED claims are never used.
HYPOTHETICAL / RECONSTRUCTED are LLM-side labels requested in prompts;
this module never invents employers, dates, projects or achievements.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("interview_context")

MAX_VACANCY_CHARS = 3000
MAX_PROFILE_SKILLS = 60
MAX_CLAIMS = 40

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text: Any, limit: int) -> str:
    """Strip control chars, collapse whitespace, cap length."""
    cleaned = _CONTROL_RE.sub(" ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned


def load_profile_summary(profile_path: str | Path | None = None) -> dict[str, Any]:
    """Best-effort profile summary. Missing file -> has_profile False."""
    try:
        from ai_assistant.candidate_profile import load_candidate_profile
    except Exception:
        return {"has_profile": False}
    try:
        profile = load_candidate_profile(profile_path) if profile_path else load_candidate_profile()
    except Exception as exc:
        logger.warning("profile load failed: %s", type(exc).__name__)
        return {"has_profile": False}
    skills = list(dict.fromkeys(
        list(getattr(profile, "core_skills", []) or []) +
        list(getattr(profile, "secondary_skills", []) or []) +
        list(getattr(profile, "skills", []) or [])))[:MAX_PROFILE_SKILLS]
    return {
        "has_profile": True,
        "roles": list(getattr(profile, "desired_roles", []) or [])[:10],
        "core_skills": list(getattr(profile, "core_skills", []) or [])[:30],
        "all_skills": skills,
        "years_experience": getattr(profile, "years_experience", None),
        "languages": list(getattr(profile, "languages", []) or [])[:10],
    }


def load_verified_claims(kb_path: str | Path | None = None,
                         limit: int = MAX_CLAIMS) -> dict[str, Any]:
    """KB claims split by honesty label. Missing KB -> empty lists."""
    out = {"verified": [], "checkable": [], "has_kb": False}
    try:
        from ai_assistant.candidate.kb import CandidateKnowledgeBase, ClaimStatus
    except Exception:
        return out
    try:
        kb = CandidateKnowledgeBase.load(kb_path) if kb_path else None
        if kb is None:
            from ai_assistant.candidate.kb import DEFAULT_KB_PATH

            kb = CandidateKnowledgeBase.load(DEFAULT_KB_PATH)
    except Exception as exc:
        logger.warning("KB load failed: %s", type(exc).__name__)
        return out
    out["has_kb"] = True
    for claim in list(getattr(kb, "claims", {}).values())[: limit * 2]:
        status = str(getattr(claim, "status", ""))
        entry = {"id": str(getattr(claim, "claim_id", "?")),
                 "kind": str(getattr(claim, "kind", "")),
                 "text": sanitize(getattr(claim, "value", ""), 300)}
        if status == ClaimStatus.CONFIRMED_BY_USER:
            out["verified"].append(entry)
        elif status == ClaimStatus.DERIVED_FROM_SOURCE:
            out["checkable"].append(entry)
        if len(out["verified"]) + len(out["checkable"]) >= limit:
            break
    return out


def load_vacancy_context(stable_id: str | None) -> dict[str, Any]:
    """Vacancy title/company/description (capped, sanitized) or manual-role mode."""
    if not stable_id:
        return {"has_vacancy": False}
    try:
        from ai_assistant.db import get_connection, get_vacancy_by_id
    except Exception:
        return {"has_vacancy": False}
    try:
        vac = get_vacancy_by_id(stable_id)
    except Exception as exc:
        logger.warning("vacancy load failed: %s", type(exc).__name__)
        return {"has_vacancy": False}
    if vac is None:
        return {"has_vacancy": False}
    if isinstance(vac, dict):
        get = vac.get
    elif isinstance(vac, (tuple, list)):
        try:
            conn = get_connection()
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(vacancies)").fetchall()]
            finally:
                conn.close()
            get = dict(zip(cols, vac)).get
        except Exception:
            return {"has_vacancy": False}
    else:
        get = lambda k: getattr(vac, k, None)
    return {
        "has_vacancy": True,
        "stable_id": stable_id,
        "title": sanitize(get("title"), 200),
        "company": sanitize(get("company"), 200),
        "description": sanitize(get("description"), MAX_VACANCY_CHARS),
    }


def build_context(vacancy_stable_id: str | None = None,
                  role_title: str | None = None) -> dict[str, Any]:
    """Assemble the full prompt context (sizes capped for cost control)."""
    profile = load_profile_summary()
    claims = load_verified_claims()
    vacancy = load_vacancy_context(vacancy_stable_id)
    return {
        "profile": profile,
        "claims": claims,
        "vacancy": vacancy,
        "manual_role": sanitize(role_title, 200) if role_title else None,
        "context_json_chars": 0,  # filled by caller after serialization
    }


def context_to_text(ctx: dict[str, Any]) -> str:
    """Compact text rendering for prompts (deterministic, capped)."""
    lines = ["[CANDIDATE CONTEXT — verified facts only where labeled]"]
    prof = ctx.get("profile", {})
    if prof.get("has_profile"):
        lines.append(f"Roles: {', '.join(prof.get('roles', [])[:8]) or '?'}")
        lines.append(f"Core skills: {', '.join(prof.get('core_skills', [])[:20]) or '?'}")
        if prof.get("years_experience") is not None:
            lines.append(f"Experience: {prof['years_experience']} years")
    else:
        lines.append("No candidate profile on file.")
    claims = ctx.get("claims", {})
    for item in (claims.get("verified", [])[:15]):
        lines.append(f"VERIFIED: {item.get('text', '')[:220]}")
    for item in (claims.get("checkable", [])[:10]):
        lines.append(f"CHECKABLE (verify before citing): {item.get('text', '')[:220]}")
    vac = ctx.get("vacancy", {})
    if vac.get("has_vacancy"):
        lines.append(f"Vacancy: {vac.get('title')} @ {vac.get('company')}")
        lines.append(f"Description: {(vac.get('description') or '')[:1500]}")
    elif ctx.get("manual_role"):
        lines.append(f"Manual role: {ctx['manual_role']} (no vacancy record)")
    text = "\n".join(lines)
    return text[:6000]


__all__ = ["build_context", "context_to_text", "load_profile_summary",
           "load_verified_claims", "load_vacancy_context", "sanitize"]
