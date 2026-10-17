"""Scope separation between the authenticated feed and the P2 archive (P2.1 §9).

The requirement is explicit: archived public /hire records must not be mixed with
authenticated live jobs. Both come from ``source=contra`` and ``source_mode=live``,
so the discriminator is ``source_scope``. These tests pin that it works - in
storage, in filtering, and in the brief served to CLI/API/UI.

Offline: payloads are built in-memory and the source transport is never called.
"""
from __future__ import annotations

import json

import pytest

from ai_assistant.gig.models import SourceMode
from ai_assistant.gig.pipeline import run_source_pipeline
from ai_assistant.gig.sources.contra_auth import ContraAuthenticatedSource
from ai_assistant.gig.store import gig_stats, list_gigs

AUTHED_SCOPE = "authenticated_feed"
ARCHIVE_SCOPE = "public_hire_pages"


def _authed_payload(job_id: str, title: str) -> dict:
    return {
        "data": {
            "jobFeed": {
                "count": 233,
                "edges": [
                    {
                        "node": {
                            "id": f"feed_{job_id}",
                            "visitorDidApply": False,
                            "job": {
                                "id": job_id,
                                "title": title,
                                "slug": f"slug-{title}",
                                "createdAt": "2026-09-28T23:27:37.566Z",
                                "budget": {
                                    "__typename": "FixedPriceJobBudget",
                                    "feeMin": "USD:2000.00000000",
                                    "feeMax": "USD:3000.00000000",
                                },
                                "duration": {"amount": 2, "interval": "WEEK"},
                                "organization": {"name": "Nexuscai"},
                                "roles": {"edges": [{"node": {"name": "UI Designer"}}]},
                                "tools": {"edges": [{"node": {"name": "Figma"}}]},
                            },
                        }
                    }
                ],
            }
        }
    }


@pytest.fixture()
def authed_records() -> list[dict]:
    src = ContraAuthenticatedSource(lambda _e: "")
    return src.records_from_payload(_authed_payload("WyJKb2IiLDk4MDQ5XQ==", "Authed Job"))


class TestScopeIsRecorded:
    def test_authenticated_records_carry_their_scope(self, authed_records):
        assert authed_records[0]["source_scope"] == AUTHED_SCOPE

    def test_stored_scope_survives_round_trip(self, authed_records):
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.store import get_gig, save_gig

        gig = normalize_gig(authed_records[0])
        save_gig(gig)
        reloaded = get_gig(gig.id)
        assert reloaded.source_scope == AUTHED_SCOPE
        assert reloaded.source_status == "OPEN"


class TestScopesAreFilterable:
    def test_archive_and_feed_are_filterable_apart(self, authed_records):
        """Both surfaces stored, then read back separately."""
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.store import save_gig

        archived = normalize_gig({
            "source": "contra", "source_id": "archive-1", "title": "Archived Job",
            "budget": "USD 1200", "source_mode": "live",
            "source_scope": ARCHIVE_SCOPE, "source_status": "EXPIRED",
        })
        save_gig(archived)
        save_gig(normalize_gig(authed_records[0]))

        scope_stats = gig_stats()["by_source_scope"]
        assert scope_stats.get(AUTHED_SCOPE, 0) >= 1
        assert scope_stats.get(ARCHIVE_SCOPE, 0) >= 1

        current = list_gigs(limit=50, source="contra", source_scope=AUTHED_SCOPE)
        assert current and all(g.source_scope == AUTHED_SCOPE for g in current)

        archive = list_gigs(limit=50, source="contra", source_scope=ARCHIVE_SCOPE)
        assert archive and all(g.source_scope == ARCHIVE_SCOPE for g in archive)

        # No id may appear in both views.
        assert {g.id for g in current}.isdisjoint({g.id for g in archive})

    def test_exclude_scope_hides_the_archive(self, authed_records):
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.store import save_gig

        save_gig(normalize_gig(authed_records[0]))
        save_gig(normalize_gig({
            "source": "contra", "source_id": "archive-2", "title": "Archived Job 2",
            "budget": "USD 900", "source_mode": "live",
            "source_scope": ARCHIVE_SCOPE, "source_status": "EXPIRED",
        }))

        working_view = list_gigs(limit=100, source="contra", exclude_scope=ARCHIVE_SCOPE)
        assert working_view, "current postings must remain visible"
        assert all(g.source_scope != ARCHIVE_SCOPE for g in working_view)

    def test_fixture_scope_is_a_third_distinct_class(self, authed_records):
        from ai_assistant.gig.pipeline import run_fixture_pipeline
        from ai_assistant.gig.normalizer import normalize_gig
        from ai_assistant.gig.store import save_gig

        save_gig(normalize_gig(authed_records[0]))
        run_fixture_pipeline(persist=True)

        stats = gig_stats()["by_source_scope"]
        assert stats.get(AUTHED_SCOPE, 0) >= 1
        assert stats.get("synthetic_fixture", 0) >= 20
        assert AUTHED_SCOPE != "synthetic_fixture"


class TestBriefExposesTheSplit:
    def test_brief_marks_authenticated_feed(self, authed_records):
        from ai_assistant.gig.cli import _gig_brief
        from ai_assistant.gig.normalizer import normalize_gig

        brief = _gig_brief(normalize_gig(authed_records[0]))
        assert brief["source_scope"] == AUTHED_SCOPE
        assert brief["is_authenticated_feed"] is True
        assert brief["source_status"] == "OPEN"
        assert json.dumps(brief)  # must stay serializable for API/UI

    def test_brief_does_not_mark_archive_as_feed(self):
        from ai_assistant.gig.cli import _gig_brief
        from ai_assistant.gig.normalizer import normalize_gig

        archived = normalize_gig({
            "source": "contra", "source_id": "archive-3", "title": "Old",
            "source_mode": "live", "source_scope": ARCHIVE_SCOPE, "source_status": "EXPIRED",
        })
        brief = _gig_brief(archived)
        assert brief["is_authenticated_feed"] is False
        assert brief["source_scope"] == ARCHIVE_SCOPE


class TestPipelineKeepsThemApart:
    def test_pipeline_records_scope_through_the_run(self):
        class _Source:
            key = "contra"
            source_mode = "live"
            source_scope = AUTHED_SCOPE

            def fetch(self, limit=None):
                yield from authed_payload_records()

        def authed_payload_records():
            src = ContraAuthenticatedSource(lambda _e: "")
            return src.records_from_payload(_authed_payload("WyJKb2IiLDk4MDQ3XQ==", "Pipeline Job"))

        result = run_source_pipeline(_Source(), persist=True)
        assert result.total == 1
        assert result.live == 1
        stored = list_gigs(limit=10, source_scope=AUTHED_SCOPE)
        assert stored
        assert stored[0].source_mode is SourceMode.LIVE
