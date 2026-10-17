"""CLI dispatch tests: gig run/list/show/stats/set-status end to end."""
from __future__ import annotations

import argparse
import json

import pytest

from ai_assistant.gig.cli import run_gig_command
from ai_assistant.gig.pipeline import run_fixture_pipeline

from gig_testutils import raw_gig


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


@pytest.fixture()
def seeded_db():
    return run_fixture_pipeline(persist=True)


def _run(args) -> int:
    return run_gig_command(args)


class TestGigRunCommand:
    def test_run_json_outputs_stats(self, seeded_db, capsys):
        rc = _run(_ns(gig_command="run", dataset=None, limit=None, no_persist=True, min_budget=None, json=True))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["stats"]["accepted"] >= 15
        assert payload["ranked_gigs"]

    def test_run_dry_run_persists_nothing(self, capsys):
        rc = _run(_ns(gig_command="run", dataset=None, limit=None, no_persist=True, min_budget=None, json=True))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["persisted"] is False


class TestGigListShowStats:
    def test_list_json(self, seeded_db, capsys):
        rc = _run(_ns(gig_command="list", limit=5, status=None, source=None, include_rejected=False, order="opportunity_score", json=True))
        assert rc == 0
        gigs = json.loads(capsys.readouterr().out)
        assert len(gigs) == 5
        assert {"title", "opportunity_score", "score_breakdown"} <= set(gigs[0])

    def test_show_detail_with_breakdown(self, seeded_db, capsys):
        from ai_assistant.gig.store import list_gigs

        gig = list_gigs(limit=1)[0]
        rc = _run(_ns(gig_command="show", gig_id=gig.id, json=True))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == gig.id
        assert payload["score_breakdown"]["components"]

    def test_show_unknown_gig_fails(self, seeded_db, capsys):
        rc = _run(_ns(gig_command="show", gig_id="gig_missing", json=False))
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_stats_command(self, seeded_db, capsys):
        rc = _run(_ns(gig_command="stats", json=True))
        assert rc == 0
        stats = json.loads(capsys.readouterr().out)
        assert stats["total"] >= 20
        assert "DUPLICATE" in stats["by_rejection_reason"]

    def test_stats_text_mode(self, seeded_db, capsys):
        rc = _run(_ns(gig_command="stats", json=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "pipeline statistics" in out


class TestGigSetStatus:
    def test_set_status_roundtrip(self, seeded_db, capsys):
        from ai_assistant.gig.store import list_gigs

        gig = list_gigs(limit=1)[0]
        rc = _run(_ns(gig_command="set-status", gig_id=gig.id, status="SHORTLISTED", note=None))
        assert rc == 0
        assert "SHORTLISTED" in capsys.readouterr().out

    def test_unknown_dispatch_is_usage_error(self):
        rc = _run(_ns(gig_command="bogus"))
        assert rc == 2

