"""Dedup tests: source+source_id, canonical URL, near-identical text, reposts.

Gig dedup must be fully separate from vacancy dedup (GIG-SEARCH-P1 §7).
"""
from __future__ import annotations

from ai_assistant.gig.dedup import canonical_url, find_duplicate
from ai_assistant.gig.models import Gig
from ai_assistant.gig.normalizer import normalize_gig


def _gig(**kw):
    payload = {"source": "fixture", "source_id": "d1", "url": "https://x.test/j/1", "title": "T", "description": "D"}
    payload.update(kw)
    return normalize_gig(payload)


class TestCanonicalUrl:
    def test_strips_www_tracking_and_fragment(self):
        a = canonical_url("https://WWW.Example.test/j/1?utm_source=mail&b=2#a")
        b = canonical_url("https://example.test/j/1?b=2")
        assert a == b

    def test_keeps_meaningful_query(self):
        a = canonical_url("https://example.test/j?id=7")
        b = canonical_url("https://example.test/j?id=8")
        assert a != b


class TestFindDuplicate:
    def test_same_source_id_is_duplicate(self):
        gig = _gig()
        dup, reason = find_duplicate(gig, [_gig()])
        assert dup is not None
        assert "source+source_id" in reason

    def test_same_canonical_url_is_duplicate(self):
        existing = _gig(source_id="old", url="https://www.x.test/j/1?utm_source=t")
        gig = _gig(source_id="new", url="https://x.test/j/1")
        dup, reason = find_duplicate(gig, [existing])
        assert dup is not None
        assert "canonical URL" in reason

    def test_repost_same_content_same_source(self):
        existing = _gig(source_id="old")
        gig = _gig(source_id="new")
        dup, reason = find_duplicate(gig, [existing])
        assert dup is not None

    def test_cross_source_same_content_is_not_duplicate(self):
        """Одинаковый заказ на двух площадках — законное пересечение."""
        existing = _gig(source="upwork", source_id="old", url="https://upwork.test/j/old")
        gig = _gig(source="fixture", source_id="new", url="https://x.test/j/new")
        dup, _ = find_duplicate(gig, [existing])
        assert dup is None

    def test_different_gigs_not_duplicate(self):
        existing = _gig(
            source_id="old",
            url="https://x.test/j/old",
            title="Design a logo",
            description="Vector brand identity",
        )
        gig = _gig(
            source_id="new",
            url="https://x.test/j/new",
            title="Build an n8n automation",
            description="Webhook ETL pipeline",
        )
        dup, _ = find_duplicate(gig, [existing])
        assert dup is None

    def test_gig_dataclass_directly_supported(self):
        gig = Gig(source="fixture", source_id="d1")
        dup, reason = find_duplicate(gig, [Gig(source="fixture", source_id="d1")])
        assert dup is not None
        assert "source+source_id" in reason
