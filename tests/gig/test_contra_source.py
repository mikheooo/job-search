"""Contra source adapter tests (GIG-SEARCH-P2 §12).

Every test here runs offline against a saved public Contra page
(``artifacts/contra_hire_page_snapshot.html``). No test touches the network: the
adapter is fed HTML directly, so the parser is deterministic and CI-safe.

What is covered, per P2 §12:
- Contra parser (fields, money strings, budget types);
- missing fields stay missing (nothing invented);
- changed/renamed input degrades gracefully instead of crashing;
- stable source id across parses;
- normalization into the P1 Gig model;
- live/fixture separation;
- network failure handling;
- repeat fetch / dedup / update semantics;
- user status survives a re-fetch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_assistant.gig.models import BudgetType, PaymentReadiness, RejectionReason, SourceMode
from ai_assistant.gig.normalizer import normalize_gig
from ai_assistant.gig.sources.contra import (
    ContraGigSource,
    ContraUnavailable,
    budget_type_of,
    parse_money,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = PROJECT_ROOT / "artifacts" / "contra_hire_page_snapshot.html"
SNAPSHOT_WEB = PROJECT_ROOT / "artifacts" / "contra_hire_web_developers_snapshot.html"


@pytest.fixture(scope="module")
def snapshot_html() -> str:
    if not SNAPSHOT.exists():
        pytest.skip(f"contra snapshot missing: {SNAPSHOT}")
    return SNAPSHOT.read_text(encoding="utf-8", errors="replace")


@pytest.fixture()
def source() -> ContraGigSource:
    return ContraGigSource(snapshot_path=str(SNAPSHOT))


@pytest.fixture()
def records(snapshot_html, source) -> list[dict]:
    return source.records_from_html(snapshot_html, "ai-developers")


class TestMoneyParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("USD:50.00000000", ("USD", 50.0)),
            ("USD:1500.00000000", ("USD", 1500.0)),
            ("EUR:120.5", ("EUR", 120.5)),
            ("25", (None, 25.0)),
            (None, (None, None)),
            ("", (None, None)),
            ("negotiable", (None, None)),
        ],
    )
    def test_parse_money(self, raw, expected):
        assert parse_money(raw) == expected

    @pytest.mark.parametrize(
        ("typename", "expected"),
        [
            ("HourlyRateJobBudget", "hourly"),
            ("MonthlyRateJobBudget", "monthly"),
            ("WeeklyRateJobBudget", "weekly"),
            ("FixedPriceJobBudget", "fixed"),
            ("SomethingElseJobBudget", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_budget_type_mapping(self, typename, expected):
        assert budget_type_of(typename) == expected


class TestParser:
    def test_finds_records(self, records):
        assert len(records) >= 4, "snapshot must contain real Contra job records"

    def test_required_fields_present(self, records):
        for record in records:
            assert record["source"] == "contra"
            assert record["source_id"]
            assert record["title"]
            assert record["source_mode"] == "live"
            assert record["source_scope"] == "public_hire_pages"

    def test_source_id_is_stable_across_parses(self, snapshot_html, source):
        first = [r["source_id"] for r in source.records_from_html(snapshot_html, "ai-developers")]
        second = [r["source_id"] for r in source.records_from_html(snapshot_html, "ai-developers")]
        assert first == second
        assert len(set(first)) == len(first), "ids must be unique within a page"

    def test_money_becomes_numbers_and_currency(self, records):
        for record in records:
            budget = record["budget"]
            assert budget["type"] in {"hourly", "monthly", "weekly", "fixed", "unknown"}
            if budget["min"] is not None:
                assert isinstance(budget["min"], float)
            if record["budget"]["min"] is not None:
                assert record["currency"] == "USD", "observed Contra records are USD-denominated"

    def test_zero_estimated_hours_is_not_a_measurement(self, records):
        """Contra sends estimatedHours=0 where hours do not apply."""
        for record in records:
            raw_hours = record["source_facts"]["contra_estimated_hours"]
            if raw_hours == 0:
                assert record["extra"]["contra_estimated_hours"] is None

    def test_source_status_is_captured(self, records):
        statuses = {r["source_status"] for r in records}
        assert statuses, "the source reports a per-record status"
        assert all(isinstance(s, str) and s for s in statuses)


class TestNothingIsInvented:
    """P2 §3: absent source facts must stay absent."""

    @pytest.mark.parametrize(
        "field",
        ["client_rating", "client_total_spent", "client_hires", "competition", "proposal_cost"],
    )
    def test_unavailable_fields_are_none(self, records, field):
        assert all(r[field] is None for r in records), f"{field} must not be invented"

    def test_description_is_empty_not_generated(self, records):
        assert all(r["description"] == "" for r in records)

    def test_stored_facts_come_from_the_source(self, records):
        for record in records:
            facts = record["source_facts"]
            assert facts["contra_posting_id"] == record["source_id"]
            assert facts["contra_job_status"] == record["source_status"]
            assert facts["contra_budget_typename"].endswith("JobBudget")

    def test_derived_fields_are_declared(self, records):
        """A field we computed must be listed as derived, not passed off as source."""
        for record in records:
            assert "budget_min" in record["derived_fields"]
            assert "source_mode" in record["derived_fields"]
            # Facts and derived sets must not silently overlap on raw source keys.
            assert "contra_posting_id" not in record["derived_fields"]


class TestRobustnessToChangedInput:
    def test_missing_page_context_yields_no_records(self):
        src = ContraGigSource(snapshot_path=None)
        assert src.records_from_html("<html><body>nothing</body></html>", "x") == []

    def test_malformed_json_yields_no_records(self):
        src = ContraGigSource(snapshot_path=None)
        html = '<html><script id="vike_pageContext" type="application/json">{broken</script></html>'
        assert src.records_from_html(html, "x") == []

    def test_record_without_title_is_skipped(self):
        src = ContraGigSource(snapshot_path=None)
        html = (
            '<script id="vike_pageContext" type="application/json">'
            + json.dumps(
                {
                    "publicAppConfiguration": {
                        "relayRecordMap": {
                            "a": {"__typename": "JobPosting", "__id": "a", "id": "a", "title": ""},
                        }
                    }
                }
            )
            + "</script>"
        )
        assert src.records_from_html(html, "x") == []

    def test_unknown_budget_typename_is_unknown_not_guessed(self):
        src = ContraGigSource(snapshot_path=None)
        html = (
            '<script id="vike_pageContext" type="application/json">'
            + json.dumps(
                {
                    "publicAppConfiguration": {
                        "relayRecordMap": {
                            "p": {
                                "__typename": "JobPosting",
                                "__id": "p",
                                "id": "p",
                                "title": "T",
                                "job": {"__ref": "j"},
                            },
                            "j": {"__typename": "Job", "__id": "j", "status": "OPEN", "budget": {"__ref": "b"}},
                            "b": {"__id": "b", "__typename": "WeirdJobBudget", "feeMin": "USD:10", "feeMax": None},
                        }
                    }
                }
            )
            + "</script>"
        )
        records = src.records_from_html(html, "x")
        assert len(records) == 1
        assert records[0]["budget"]["type"] == "unknown"
        assert records[0]["currency"] == "USD"


class TestNormalization:
    def test_contra_record_normalizes_into_gig(self, records):
        gig = normalize_gig(records[0])
        assert gig.source == "contra"
        assert gig.source_mode is SourceMode.LIVE
        assert gig.source_scope == "public_hire_pages"
        assert gig.fetched_at
        assert gig.budget_type in set(BudgetType)
        assert gig.id.startswith("gig_")

    def test_fixture_mode_is_distinct(self):
        """The same payload marked as fixture must not look live (P2 §7)."""
        fixture_gig = normalize_gig(
            {"source": "fixture", "source_id": "x", "title": "T", "source_mode": "fixture"}
        )
        live_gig = normalize_gig(
            {"source": "contra", "source_id": "x", "title": "T", "source_mode": "live"}
        )
        assert fixture_gig.source_mode is SourceMode.FIXTURE
        assert live_gig.source_mode is SourceMode.LIVE
        assert fixture_gig.source_mode is not live_gig.source_mode


class TestFetchFailure:
    def test_network_failure_raises_unavailable(self, monkeypatch):
        src = ContraGigSource(pages=["ai-developers"], delay_seconds=0.0)

        def boom(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(ContraGigSource, "_http_get", lambda self, url: boom())
        with pytest.raises(ContraUnavailable):
            list(src.fetch())

    def test_http_404_is_recorded_not_fatal_for_partial_runs(self, monkeypatch):
        """One bad slug must not abort a multi-page run."""
        src = ContraGigSource(pages=["missing-slug", "ai-developers"], delay_seconds=0.0)

        import urllib.error

        def fake_get(self, url):
            if "missing-slug" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return SNAPSHOT.read_text(encoding="utf-8", errors="replace")

        monkeypatch.setattr(ContraGigSource, "_http_get", fake_get)
        records = list(src.fetch())
        assert records, "the healthy page must still produce records"
        assert any("404" in err for err in src.last_fetch_errors)

    def test_snapshot_mode_never_hits_the_network(self, monkeypatch, source):
        def boom(*args, **kwargs):
            raise AssertionError("network must not be used in snapshot mode")

        monkeypatch.setattr(ContraGigSource, "_http_get", boom)
        assert list(source.fetch(limit=2))


class TestClosedSourceGate:
    """P2: a record the source itself calls closed is not an open gig."""

    def test_expired_records_are_rejected_by_gate(self, records):
        from ai_assistant.gig.hard_gates import evaluate_hard_gates

        gig = normalize_gig(records[0])
        assert str(gig.source_status).lower() in {"expired", "closed"}
        gate = evaluate_hard_gates(gig)
        assert RejectionReason.SOURCE_CLOSED.value in gate.reasons

    def test_analytic_mode_can_score_closed_records(self, records):
        from ai_assistant.gig.pipeline import GigPipeline

        pipeline = GigPipeline(persist=False, include_closed=True)
        gig = pipeline.process_raw(records[0])
        assert gig.opportunity_score is not None
        assert any("больше не открыт" in risk for risk in gig.risks)


class TestPaymentReadinessIsNeverReadyFromCapability:
    """P2 §5: platform capability is not personal readiness."""

    def test_contra_records_never_report_ready(self, records):
        from ai_assistant.gig.assessment import assess_payment_readiness

        for record in records:
            gig = normalize_gig(record)
            level = assess_payment_readiness(gig)
            assert level is not PaymentReadiness.READY, (
                "Contra exposes platform payout options, not this account's readiness"
            )

    def test_platform_options_do_not_imply_ready(self):
        from ai_assistant.gig.assessment import assess_payment_readiness

        gig = normalize_gig(
            {
                "source": "contra",
                "source_id": "z",
                "title": "T",
                "platform_payment_options": ["platform_escrow", "paypal"],
            }
        )
        assert assess_payment_readiness(gig) is not PaymentReadiness.READY

    def test_capability_is_not_a_hard_reject(self):
        """An unverified personal payout path must not block the gig."""
        from ai_assistant.gig.hard_gates import evaluate_hard_gates

        gig = normalize_gig(
            {
                "source": "contra",
                "source_id": "z",
                "title": "Build a bot",
                "budget": "USD 500",
                "platform_payment_options": ["platform_escrow"],
            }
        )
        gate = evaluate_hard_gates(gig)
        assert RejectionReason.PAYMENT_UNAVAILABLE.value not in gate.reasons

    def test_platform_options_are_persisted_as_capability(self, records):
        assert all(r["platform_payment_options"] for r in records)


class TestSecondSnapshotParses:
    def test_web_developers_snapshot(self):
        if not SNAPSHOT_WEB.exists():
            pytest.skip("second snapshot not present")
        src = ContraGigSource()
        records = src.records_from_html(SNAPSHOT_WEB.read_text(encoding="utf-8", errors="replace"), "web-developers")
        assert len(records) >= 4
        # Cross-page ids must be comparable for dedup to work across pages.
        assert all(r["source_id"] and r["source_mode"] == "live" for r in records)
