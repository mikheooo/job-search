"""Refresh / persistence semantics for a live source (GIG-SEARCH-P2 §8, §12).

Offline: the source is a snapshot-backed Contra adapter, so the pipeline is
exercised end-to-end without touching the network. These tests pin the four
promises of a safe refresh:

1. a second fetch creates no duplicates;
2. a user status (SHORTLISTED / SKIPPED) survives a re-fetch;
3. a gig that disappears from the current page is NOT deleted - its last_seen_at
   simply stops advancing;
4. first_seen_at is written once and never moves.

Why ``include_closed=True`` here: every record Contra publishes anonymously on
its curated pages is already EXPIRED/CLOSED, so with the default gate they would
all be rejected and there would be no stored rows to test refresh against. The
analytic mode still records the closed status in ``source_status`` and in
``risks``, so nothing is hidden; see test_contra_source.py for the gate itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_assistant.gig.models import GigStatus, SourceMode
from ai_assistant.gig.pipeline import run_source_pipeline
from ai_assistant.gig.sources.contra import ContraGigSource
from ai_assistant.gig.store import get_gig, gig_stats, list_gigs, set_gig_status

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = PROJECT_ROOT / "artifacts" / "contra_hire_page_snapshot.html"


def _source(snapshot: Path = SNAPSHOT) -> ContraGigSource:
    return ContraGigSource(snapshot_path=str(snapshot))


@pytest.fixture()
def first_run():
    return run_source_pipeline(_source(), persist=True, include_closed=True)


def _refetch(limit: int | None = None):
    return run_source_pipeline(_source(), persist=True, include_closed=True, limit=limit)


class _FakeSource:
    """Отдаёт заранее подготовленные сырые записи, чтобы не трогать сеть."""

    key = "contra"
    source_mode = "live"
    source_scope = "public_hire_pages"

    def __init__(self, records: list[dict]):
        self._records = records

    def fetch(self, limit: int | None = None):
        yield from (self._records[:limit] if limit else self._records)


def _snapshot_raw_record() -> dict:
    """Одна реальная запись со снимка Contra — база для проверки монотонности."""
    records = _source().records_from_snapshot()
    assert records, "the Contra snapshot must yield at least one record"
    return records[0]


class TestRepeatFetch:
    def test_first_run_persists_live_gigs(self, first_run):
        assert first_run.total >= 4
        assert first_run.live == first_run.total
        assert first_run.fixture == 0
        stats = gig_stats()
        assert stats["by_source_mode"].get("live", 0) >= 4

    def test_second_fetch_creates_no_duplicates(self, first_run):
        before = gig_stats()["total"]
        second = _refetch()
        assert second.total == first_run.total
        assert second.accepted == 0
        assert second.duplicates == first_run.total
        assert gig_stats()["total"] == before, "re-fetch must not add rows"

    def test_repeat_fetch_does_not_change_opportunity_score(self, first_run):
        first_scores = {g.id: g.opportunity_score for g in list_gigs(limit=100, source="contra", source_mode="live")}
        _refetch()
        second_scores = {g.id: g.opportunity_score for g in list_gigs(limit=100, source="contra", source_mode="live")}
        assert first_scores == second_scores


class TestUserStatusSurvives:
    def test_shortlisted_status_survives_refetch(self, first_run):
        gig = list_gigs(limit=1, source="contra", source_mode="live")[0]
        set_gig_status(gig.id, "SHORTLISTED", note="picked by hand")

        _refetch()

        reloaded = get_gig(gig.id)
        assert reloaded is not None
        assert reloaded.status is GigStatus.SHORTLISTED

    def test_skipped_status_survives_refetch(self, first_run):
        gig = list_gigs(limit=1, source="contra", source_mode="live")[0]
        set_gig_status(gig.id, "SKIPPED")

        _refetch()

        assert get_gig(gig.id).status is GigStatus.SKIPPED


class TestHistoryIsKept:
    def test_gig_missing_from_current_page_is_not_deleted(self, first_run):
        """P2 §8: a shorter fetch must not erase what it no longer returns."""
        stored_ids = {g.id for g in list_gigs(limit=100, source="contra", source_mode="live")}
        assert stored_ids

        shorter = _refetch(limit=1)
        assert shorter.total == 1

        still_there = {g.id for g in list_gigs(limit=100, source="contra", source_mode="live")}
        assert stored_ids <= still_there, "no stored gig may vanish because it was not re-fetched"

    def test_first_seen_is_immutable_and_last_seen_advances(self, first_run):
        gig = list_gigs(limit=1, source="contra", source_mode="live")[0]
        first_seen, last_seen = gig.first_seen_at, gig.last_seen_at
        assert first_seen and last_seen

        # A later observation with a newer fetched_at.
        later = _refetch()
        assert later.total >= 1

        reloaded = get_gig(gig.id)
        assert reloaded.first_seen_at == first_seen, "first_seen_at is written once"
        assert reloaded.last_seen_at >= last_seen, "last_seen_at only moves forward"

    def test_last_seen_does_not_regress_on_an_older_record(self, first_run):
        """Re-ingesting an older observation must not move last_seen_at backwards.

        P3 §10 supersedes the earlier P2 behaviour: last_seen_at now advances on a
        real observation, but it must still be monotonic. This re-ingests a record
        whose fetched_at is explicitly in the past and asserts the stored value
        does not regress.
        """
        gig = list_gigs(limit=1, source="contra", source_mode="live")[0]
        set_gig_status(gig.id, "SHORTLISTED")  # force a write with the stored value
        new_seen = get_gig(gig.id).last_seen_at

        # An observation stamped before the stored one: the newest value must win.
        stale = dict(_snapshot_raw_record())
        stale["fetched_at"] = "2000-01-01T00:00:00Z"
        result = run_source_pipeline(_FakeSource([stale]), persist=True)
        assert result.total == 1

        assert get_gig(gig.id).last_seen_at == new_seen, "last_seen_at must not regress"


class TestProvenancePersisted:
    def test_provenance_fields_round_trip(self, first_run):
        gig = list_gigs(limit=1, source="contra", source_mode="live")[0]
        assert gig.source_mode is SourceMode.LIVE
        assert gig.source_scope == "public_hire_pages"
        assert gig.source_url and "contra.com" in gig.source_url
        assert gig.fetched_at
        assert gig.source_status
        assert gig.platform_payment_options
        assert gig.source_facts
        assert gig.derived_fields
        # No invented per-job URL: Contra does not publish one anonymously.
        assert not gig.url

    def test_payment_readiness_never_ready_in_storage(self, first_run):
        for gig in list_gigs(limit=100, source="contra"):
            assert gig.payment_readiness.value != "ready"

    def test_live_and_fixture_are_stored_apart(self, first_run):
        from ai_assistant.gig.pipeline import run_fixture_pipeline

        run_fixture_pipeline(persist=True)
        stats = gig_stats()
        modes = stats["by_source_mode"]
        assert modes.get("live", 0) >= 4
        assert modes.get("fixture", 0) >= 20

        live_only = list_gigs(limit=100, source_mode="live")
        fixture_only = list_gigs(limit=100, source_mode="fixture")
        assert all(g.source_mode is SourceMode.LIVE for g in live_only)
        assert all(g.source_mode is SourceMode.FIXTURE for g in fixture_only)
        assert {g.id for g in live_only}.isdisjoint({g.id for g in fixture_only})


class TestStatsSurfacesModes:
    def test_stats_report_by_source_mode(self, first_run):
        stats = gig_stats()
        assert "by_source_mode" in stats
        assert stats["by_source_mode"].get("live", 0) >= 4
