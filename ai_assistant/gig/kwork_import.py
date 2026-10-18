"""Kwork link import for the Telegram bot (Phase 4C.2 §2).

Flow (code + offline tests only; live wiring needs owner approval):
  user pastes link -> validate kwork.ru/projects/<id> -> duplicate check ->
  public fetch via the allowed adapter -> pipeline (normalize/dedup/gates/
  assessment/scoring) -> assessment card with INT/SKP/Open actions.

Guards: only ``kwork.ru``/``www.kwork.ru`` hosts, ``/projects/<digits>``
paths, max 5 links per message, per-user authorization done by the caller
(control bot already gates on ALLOWED_CHAT_ID), fetch goes through
``KworkGigSource`` (polite delays, robots check, SSRF redirect guard).
Unverified users, arbitrary domains, and non-project URLs are refused
without any fetch. Manual description fallback never invents missing
fields (skills/category/budget stay empty/None).

Nothing here sends Telegram messages or writes to production by itself:
persistence is an explicit ``persist`` flag (default False = preview).
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Callable

logger = logging.getLogger("kwork_import")

KWORK_HOSTS = ("kwork.ru", "www.kwork.ru")
MAX_LINKS_PER_MESSAGE = 5

#: Phase 4C.5: live link-import via bot is OFF until the owner approves.
#: ACTIVATED by owner APPROVE (Phase 4C.5 activation, bot restart).
#: Scope is strictly link import: no digest, no auto-submit, no scheduler.
KWORK_LINK_IMPORT_ENABLED = True

_KWORK_URL_RE = re.compile(r"https?://(?:www\.)?kwork\.ru/projects/(\d+)", re.I)


def extract_kwork_links(text: str) -> list[str]:
    """Find candidate Kwork project URLs in free text (validated later)."""
    return [m.group(0) for m in _KWORK_URL_RE.finditer(str(text or ""))]


def validate_kwork_url(url: str) -> str | None:
    """Return the canonical project URL, or None when not acceptable.

    Accepts only http(s) URLs on kwork.ru with a /projects/<digits> path.
    Everything else (other hosts, other paths, userinfo, ports, weird
    schemes) is refused before any network happens.
    """
    try:
        parts = urllib.parse.urlparse(str(url or "").strip())
    except Exception:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").lower() not in KWORK_HOSTS:
        return None
    if parts.username or parts.password or parts.port:
        return None
    match = re.fullmatch(r"/projects/(\d+)(?:/view)?/?", parts.path or "")
    if not match:
        return None
    from ai_assistant.gig.sources.kwork import canonical_url

    return canonical_url(match.group(1))


def import_links(
    urls: list[str],
    *,
    persist: bool = False,
    fetch_html: Callable[[str], str] | None = None,
    max_links: int = MAX_LINKS_PER_MESSAGE,
) -> dict[str, Any]:
    """Import curated Kwork links through the gig pipeline.

    Returns ``{"imported": [...], "refused": [...], "duplicates": [...]}``.
    ``persist=False`` (default) only assesses (no DB writes). With
    ``persist=True`` accepted gigs are saved as NEW (deduplicated).
    """
    from ai_assistant.gig.pipeline import run_source_pipeline
    from ai_assistant.gig.sources.kwork import KworkGigSource

    accepted: list[str] = []
    refused: list[dict[str, str]] = []
    for raw in urls[:max_links]:
        canon = validate_kwork_url(raw)
        if canon is None:
            refused.append({"url": str(raw)[:120], "reason": "not-a-kwork-project-url"})
            continue
        accepted.append(canon)
    for raw in urls[max_links:]:
        refused.append({"url": str(raw)[:120], "reason": "per-message-link-limit"})

    imported: list[dict[str, Any]] = []
    duplicates: list[str] = []
    closed: list[dict[str, Any]] = []
    fresh: list[str] = []
    if accepted:
        # Pre-check against stored gigs first: no need to fetch what we own,
        # and preview mode (persist=False) would otherwise miss duplicates.
        from ai_assistant.gig.store import find_gig_by_source_id
        from ai_assistant.gig.sources.kwork import extract_project_id

        for canon in accepted:
            pid = extract_project_id(canon)
            if pid and find_gig_by_source_id("kwork", f"kw_{pid}") is not None:
                duplicates.append(f"kwork:kw_{pid}")
            else:
                fresh.append(canon)
    if fresh:
        src = KworkGigSource(urls=fresh, fetch_html=fetch_html,
                             delay_seconds=0.0)
        try:
            result = run_source_pipeline(src, persist=persist)
        except Exception as exc:  # noqa: BLE001 - import must not raise
            return {"imported": [], "refused": refused + [
                {"url": "kwork-source", "reason": f"{type(exc).__name__}: {exc}"}],
                "duplicates": [], "closed": []}
        for gig in result.ranked_gigs:
            imported.append({"gig_id": gig.id, "title": gig.title,
                             "budget": gig.budget_text,
                             "score": gig.opportunity_score,
                             "url": gig.url})
        for gig in result.rejected_gigs:
            reasons = list(gig.rejection_reasons or [])
            if "DUPLICATE" in reasons:
                duplicates.append(gig.id)
            elif "SOURCE_CLOSED" in reasons:
                # Closed orders are reported explicitly so the caller can
                # say "intake finished" instead of showing them as available.
                closed.append({"gig_id": gig.id, "title": gig.title,
                               "url": gig.url})
        for gid, msg in result.error_records:
            refused.append({"url": gid, "reason": msg})
    return {"imported": imported, "refused": refused,
            "duplicates": duplicates, "closed": closed}


def import_manual_description(
    url_or_id: str,
    description: str,
    *,
    title: str = "Kwork order (manual import)",
    persist: bool = False,
) -> dict[str, Any]:
    """Manual fallback: user-supplied description, nothing invented.

    Budget/skills/category stay empty; source_facts marks ``manual_entry``.
    """
    from ai_assistant.gig.normalizer import normalize_gig
    from ai_assistant.gig.pipeline import GigPipeline

    canon = validate_kwork_url(url_or_id)
    digits = None
    if canon is not None:
        import re as _re

        digits = _re.search(r"/projects/(\d+)", canon).group(1)  # type: ignore[union-attr]
    source_id = f"kw_{digits}" if digits else "kw_manual"
    raw = {
        "source": "kwork", "source_id": source_id,
        "url": canon or "", "title": title[:200] or "Kwork order (manual import)",
        "description": str(description or "")[:8000],
        "category": None, "skills": [],
        "budget": {"min": None, "max": None, "type": None},
        "currency": "RUB", "published_at": None, "competition": None,
        "source_mode": "live", "source_scope": "manual_import",
        "source_url": canon or "", "source_status": None,
        "platform_payment_options": [],
        "source_facts": {"manual_entry": True,
                         "kwork_source_surface": "user-provided description (no fetch)"},
        "derived_fields": ["source_id"],
    }
    pipe = GigPipeline(persist=persist)
    gig = pipe.process_gig(normalize_gig(raw), existing=[])
    if persist:
        from ai_assistant.gig.store import find_gig_by_source_id, save_gig

        if find_gig_by_source_id("kwork", source_id) is None:
            save_gig(gig)
    return {"gig_id": gig.id, "title": gig.title,
            "rejected": list(gig.rejection_reasons or []),
            "score": gig.opportunity_score}


__all__ = ["KWORK_HOSTS", "MAX_LINKS_PER_MESSAGE", "extract_kwork_links",
           "import_links", "import_manual_description", "validate_kwork_url"]
