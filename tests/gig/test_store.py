"""Store tests: persistence, idempotency, status events — on isolated DB."""
from __future__ import annotations

import pytest

from ai_assistant.gig.models import make_gig_id
from ai_assistant.gig.normalizer import normalize_gig
from ai_assistant.gig.store import (
    find_gig_by_source_id,
    find_gig_by_url,
    get_gig,
    gig_stats,
    list_gig_events,
    list_gigs,
    record_gig_event,
    save_gig,
    set_gig_status,
)

from gig_testutils import raw_gig


@pytest.fixture()
def stored_gig():
    gig = normalize_gig(raw_gig())
    result = save_gig(gig)
    return gig, result


class TestSaveGig:
    def test_insert_then_unchanged(self, stored_gig):
        gig, result = stored_gig
        assert result == "INSERTED"
        again = normalize_gig(raw_gig())
        assert save_gig(again) == "UNCHANGED"

    def test_content_change_updates(self, stored_gig):
        gig, _ = stored_gig
        changed = normalize_gig(raw_gig(budget="USD 900"))
        changed.created_at = gig.created_at
        assert save_gig(changed) == "UPDATED"

    def test_requires_id(self):
        from ai_assistant.gig.models import Gig

        with pytest.raises(ValueError):
            save_gig(Gig(source="", source_id=""))


class TestLookup:
    def test_get_by_id(self, stored_gig):
        gig, _ = stored_gig
        loaded = get_gig(gig.id)
        assert loaded is not None
        assert loaded.title == gig.title
        assert loaded.skills == gig.skills
        assert loaded.budget_min == gig.budget_min
        assert loaded.payment_readiness.value == gig.payment_readiness.value

    def test_by_source_id_and_url(self, stored_gig):
        gig, _ = stored_gig
        assert find_gig_by_source_id("fixture", "test-1") is not None
        assert find_gig_by_url(gig.url) is not None
        assert find_gig_by_source_id("fixture", "missing") is None
        assert find_gig_by_url("") is None


class TestStatusAndEvents:
    def test_set_status_writes_event(self, stored_gig):
        gig, _ = stored_gig
        updated = set_gig_status(gig.id, "SHORTLISTED", note="looks good")
        assert updated is not None and updated.status.value == "SHORTLISTED"
        events = list_gig_events(gig.id)
        assert any(e["event_type"] == "STATUS_CHANGED" for e in events)
        assert '"note": "looks good"' in events[0]["detail_json"].replace("'", '"') or "looks good" in events[0]["detail_json"]

    def test_unknown_gig_returns_none(self):
        assert set_gig_status("gig_missing", "SHORTLISTED") is None

    def test_reingest_preserves_human_status(self, stored_gig):
        gig, _ = stored_gig
        set_gig_status(gig.id, "SKIPPED")
        changed = normalize_gig(raw_gig(budget="USD 950"))
        save_gig(changed)
        reloaded = get_gig(gig.id)
        assert reloaded is not None and reloaded.status.value == "SKIPPED"


class TestListAndStats:
    def test_list_filters(self, stored_gig):
        gig, _ = stored_gig
        assert len(list_gigs()) == 1
        assert len(list_gigs(status="SHORTLISTED")) == 0
        set_gig_status(gig.id, "SHORTLISTED")
        assert len(list_gigs(status="SHORTLISTED")) == 1
        assert len(list_gigs(source="fixture")) == 1
        assert len(list_gigs(source="other")) == 0

    def test_stats_counts(self, stored_gig):
        gig, _ = stored_gig
        record_gig_event(gig.id, "SCORED", {"opportunity_score": 50.0})
        stats = gig_stats()
        assert stats["total"] == 1
        assert stats["accepted"] == 1
        assert stats["by_source"] == {"fixture": 1}
