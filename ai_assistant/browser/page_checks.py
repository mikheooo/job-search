"""Page status and site detection heuristics."""
from __future__ import annotations

from urllib.parse import urlparse

_BLOCK_TEXT_MARKERS = ("access denied", "login required")


def _detect_page_blocked(html: str, body_text: str, title: str) -> bool:
    """Is this page actually a block page? (BLE001 finding #11)

    A captcha only counts when it sits in an active challenge container.
    """
    from ai_assistant.hh_extractor import _detect_blocked

    low_title = (title or "").lower()
    if "404" in low_title or "page not found" in low_title:
        return True
    dom = _detect_blocked(html or "", body_text or "")
    if dom.get("captcha") or dom.get("cloudflare"):
        return True
    low_body = (body_text or "").lower()
    return any(m in low_body for m in _BLOCK_TEXT_MARKERS)


def _detect_site(url: str) -> str:
    """Extract host / netloc from URL."""
    try:
        return urlparse(url).netloc
    except Exception:
        return url.split("/")[2] if "://" in url else url
