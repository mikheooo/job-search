"""End-to-end pipeline tests over the committed fixture dataset (§1, §8).

The global conftest redirects config.DB_FILE into tmp_path, so `persist=True`
runs here never touch the real state.db.
"""
from __future__ import annotations

import pytest

from ai_assistant.gig.models import RejectionReason
from ai_assistant.gig.pipeline import run_fixture_pipeline
from ai_assistant.gig.store import gig_stats

from gig_testutils import raw_gig


@pytest.fixture()
def run_result(fixture_payload, tmp_path):
    return run_fixture_pipeline(persist=True)


class TestFixturePipelineEndToEnd:
    def test_processes_full_dataset(self, run_result, fixture_payload):
        assert run_result.total == len(fixture_payload)
        assert run_result.errors == 0

    def test_meets_p1_volume(self, run_result):
        """ТЗ §Acceptance: через pipeline проходят минимум 20-30 gigs."""
        assert 20 <= run_result.total <= 30
        assert run_result.accepted >= 15

    def test_stats_counts_add_up(self, run_result):
        stats = run_result.stats()
        assert stats["total"] == stats["accepted"] + stats["rejected"] + stats["duplicates"]

    def test_ranked_gigs_sorted_by_opportunity_score(self, run_result):
        scores = [g.opportunity_score for g in run_result.ranked_gigs]
        assert scores == sorted(scores, reverse=True)
        assert all(s is not None and 0 <= s <= 100 for s in scores)

    def test_persisted_rows(self, run_result):
        stats = gig_stats()
        assert stats["total"] == run_result.total
        assert stats["by_source"] == {"fixture": run_result.total}

    def test_accepted_gigs_have_full_assessment(self, run_result):
        for gig in run_result.ranked_gigs:
            assert gig.opportunity_score is not None
            assert gig.score_breakdown.get("components"), gig.title
            assert gig.can_deliver.value in {"high", "medium", "low", "unknown"}
            assert gig.payment_readiness.value in {"ready", "possible", "blocked", "unknown"}
            assert gig.estimated_effort_hours and gig.estimated_effort_hours >= 2.0
            assert gig.ai_automation_potential is not None and 0 <= gig.ai_automation_potential <= 100

    def test_rejected_gigs_have_machine_readable_reasons(self, run_result):
        enum_values = {m.value for m in RejectionReason}
        for gig in run_result.rejected_gigs:
            assert gig.rejection_reasons, gig.title
            assert all(r in enum_values for r in gig.rejection_reasons), gig.rejection_reasons
            assert gig.opportunity_score is None
            assert gig.score_breakdown.get("rejected_before_scoring") is True

    def test_rejections_cover_all_spec_reasons(self, run_result):
        """Фикстура должна упражнять все причины из ТЗ §3."""
        seen = set()
        for gig in run_result.rejected_gigs:
            seen.update(gig.rejection_reasons)
        expected = {
            RejectionReason.PAYMENT_UNAVAILABLE.value,
            RejectionReason.COUNTRY_RESTRICTION.value,
            RejectionReason.ACCESS_UNAVAILABLE.value,
            RejectionReason.UNDELIVERABLE.value,
            RejectionReason.SCAM_RISK.value,
            RejectionReason.DUPLICATE.value,
            RejectionReason.PRICE_MISSING.value,
        }
        assert expected <= seen


class TestIdempotencyAndDedup:
    def test_second_run_all_duplicates(self, run_result):
        second = run_fixture_pipeline(persist=True)
        assert second.total == run_result.total
        assert second.accepted == 0
        assert second.duplicates == run_result.total

    def test_stats_stable_after_rerun(self, run_result):
        run_fixture_pipeline(persist=True)
        stats = gig_stats()
        assert stats["total"] == run_result.total
        assert stats["accepted"] == run_result.accepted


class TestDryRun:
    def test_no_persist_writes_nothing(self):
        result = run_fixture_pipeline(persist=False)
        assert result.accepted > 0
        assert gig_stats()["total"] == 0


class TestOfflineProfileIndependence:
    def test_pipeline_runs_with_empty_profile(self):
        """Контур не должен требовать реальный профиль кандидата."""
        result = run_fixture_pipeline(persist=False, profile={})
        assert result.total >= 20
        assert result.errors == 0
        assert result.accepted > 0


class TestRawProcessing:
    def test_process_raw_without_db(self):
        from ai_assistant.gig.pipeline import GigPipeline

        pipeline = GigPipeline(persist=False)
        gig = pipeline.process_raw(raw_gig())
        assert gig.opportunity_score is not None
        assert gig.rejection_reasons == []
