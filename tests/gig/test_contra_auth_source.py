"""Tests for the authenticated Contra feed source (GIG-SEARCH-P2.1).

Every test runs offline against a saved real response
(``artifacts/contra_jobs_feed_response.json``), captured once from the user's own
logged-in session. No test opens a browser or reads the network, so the parsing
contract is deterministic and CI-safe.

The split matters: ``records_from_payload`` is a pure function of the feed JSON,
and that is what these tests pin. The CDP plumbing that obtains the JSON is a thin
adapter over the project's existing transport.

Covered:
- field extraction, money/currency, budget types, duration, roles/tools;
- stable source_id derivation from Contra's base64 Relay id;
- absent fields stay absent (no description, no per-job URL, no client metrics);
- provenance: source_scope=authenticated_feed, source_status=OPEN;
- graceful degradation on changed/garbage input and GraphQL errors;
- the plan limit is surfaced (feed count vs returned edges), never hidden;
- authenticated feed is distinguishable from the archived /hire surface;
- fetch() raises a typed error when the session is unreadable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_assistant.gig.models import BudgetType, PaymentReadiness, SourceMode
from ai_assistant.gig.normalizer import normalize_gig
from ai_assistant.gig.sources.contra_auth import (
    SOURCE_SCOPE,
    ContraAuthenticatedSource,
    ContraAuthUnavailable,
    budget_type_of,
    parse_money,
    source_id_of,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "artifacts" / "contra_jobs_feed_response.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    if not FIXTURE.exists():
        pytest.skip(f"authenticated feed fixture missing: {FIXTURE}")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def source() -> ContraAuthenticatedSource:
    """A source whose transport is never called (we feed payloads directly)."""

    def never_called(_expr: str) -> str:
        raise AssertionError("transport must not be used in parsing tests")

    return ContraAuthenticatedSource(never_called)


@pytest.fixture()
def records(source, payload) -> list[dict]:
    return source.records_from_payload(payload, fetched_at="2026-09-30T00:00:00Z")


class TestMoneyAndIds:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("USD:2000.00000000", ("USD", 2000.0)),
            ("USD:600.00000000", ("USD", 600.0)),
            ("EUR:10.5", ("EUR", 10.5)),
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
            ("FixedPriceJobBudget", "fixed"),
            ("HourlyRateJobBudget", "hourly"),
            ("MonthlyRateJobBudget", "monthly"),
            ("WeeklyRateJobBudget", "weekly"),
            ("Nonsense", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_budget_type_mapping(self, typename, expected):
        assert budget_type_of(typename) == expected

    def test_source_id_is_stable_and_readable(self):
        # Contra sends ["Job",98049] base64-encoded.
        assert source_id_of("WyJKb2IiLDk4MDQ5XQ==") == "job_98049"
        # Same input twice -> same id (dedup depends on it).
        assert source_id_of("WyJKb2IiLDk4MDQ5XQ==") == source_id_of("WyJKb2IiLDk4MDQ5XQ==")

    def test_unparsable_id_falls_back_to_raw_value(self):
        assert source_id_of("not-base64!!") == "not-base64!!"
        assert source_id_of("") == ""


class TestParser:
    def test_returns_records(self, records):
        assert records, "the saved feed response must contain job records"

    def test_required_fields(self, records):
        for r in records:
            assert r["source"] == "contra"
            assert r["source_id"]
            assert r["title"]
            assert r["source_mode"] == "live"
            assert r["source_scope"] == SOURCE_SCOPE

    def test_budget_and_currency_extracted(self, records):
        for r in records:
            assert r["budget"]["type"] in {"fixed", "hourly", "monthly", "weekly", "unknown"}
            if r["budget"]["min"] is not None:
                assert isinstance(r["budget"]["min"], float)
                assert r["currency"] == "USD"

    def test_published_at_from_created_at(self, records):
        for r in records:
            assert r["published_at"], "createdAt must become published_at"

    def test_roles_and_tools_captured_as_skills_and_facts(self, records):
        for r in records:
            assert isinstance(r["skills"], list)
            assert "contra_roles" in r["source_facts"]
            assert "contra_tools" in r["source_facts"]

    def test_client_name_from_organization(self, records):
        named = [r for r in records if r["client_name"]]
        assert named, "at least one record names its organization"

    def test_source_status_is_open(self, records):
        """The authenticated feed returns only currently-open postings."""
        for r in records:
            assert r["source_status"] == "OPEN"


class TestNothingIsInvented:
    @pytest.mark.parametrize(
        "field",
        ["client_rating", "client_total_spent", "client_hires", "competition", "proposal_cost"],
    )
    def test_unavailable_fields_stay_none(self, records, field):
        assert all(r[field] is None for r in records)

    def test_no_description_is_generated(self, records):
        assert all(r["description"] == "" for r in records)

    def test_no_per_job_url_is_invented(self, records):
        """Contra's /jobs/<slug> 404s even authenticated, so url must stay empty."""
        assert all(not r["url"] for r in records)

    def test_source_url_records_where_it_was_read(self, records):
        assert all(r["source_url"] == "https://contra.com/jobs" for r in records)

    def test_derived_fields_declared(self, records):
        for r in records:
            assert "source_id" in r["derived_fields"]
            assert "source_status" in r["derived_fields"]
            assert "contra_job_id" not in r["derived_fields"]


class TestPlanLimitIsSurfaced:
    def test_feed_count_reported_separately_from_returned_records(self, source, payload, records):
        """The site reports a total; the plan returns a subset. Both are recorded."""
        feed = payload["data"]["jobFeed"]
        assert source.last_feed_count == feed["count"]
        assert source.last_feed_count >= len(records)
        for r in records:
            assert r["source_facts"]["contra_feed_total_count"] == feed["count"]

    def test_limit_is_not_hidden(self, source, payload):
        """A smaller page must not silently look like the whole feed."""
        total = payload["data"]["jobFeed"]["count"]
        returned = len(payload["data"]["jobFeed"]["edges"])
        if total > returned:
            records = source.records_from_payload(payload)
            assert source.last_feed_count == total
            assert len(records) == returned


class TestRobustness:
    def test_graphql_errors_raise(self, source):
        with pytest.raises(ContraAuthUnavailable):
            source.records_from_payload({"errors": [{"message": "An unexpected error occurred."}]})

    def test_empty_feed_returns_nothing(self, source):
        assert source.records_from_payload({"data": {"jobFeed": {"count": 0, "edges": []}}}) == []

    def test_missing_data_key_is_tolerated(self, source):
        assert source.records_from_payload({}) == []

    def test_record_without_title_is_skipped(self, source):
        payload = {
            "data": {"jobFeed": {"count": 1, "edges": [{"node": {"id": "x", "job": {
                "id": "WyJKb2IiLDE=", "title": "", "budget": {}}}}]}}
        }
        assert source.records_from_payload(payload) == []

    def test_duplicate_records_are_collapsed(self, source):
        job = {"id": "WyJKb2IiLDk4MDQ5XQ==", "title": "T", "budget": {}, "createdAt": "2026-09-28T00:00:00Z"}
        payload = {"data": {"jobFeed": {"count": 2, "edges": [
            {"node": {"id": "a", "job": job}}, {"node": {"id": "b", "job": job}},
        ]}}}
        assert len(source.records_from_payload(payload)) == 1

    def test_unusable_transport_result_raises(self):
        src = ContraAuthenticatedSource(lambda _expr: {"node": None})
        # A dict is accepted as an already-parsed payload; anything unusable must raise.
        assert src.fetch_payload() == {"node": None}
        bad = ContraAuthenticatedSource(lambda _expr: 12345)
        with pytest.raises(ContraAuthUnavailable):
            bad.fetch_payload()

    def test_transport_exception_becomes_typed_error(self):
        def boom(_expr):
            raise RuntimeError("no tab")

        with pytest.raises(ContraAuthUnavailable):
            ContraAuthenticatedSource(boom).fetch_payload()

    def test_non_json_response_raises(self):
        with pytest.raises(ContraAuthUnavailable):
            ContraAuthenticatedSource(lambda _expr: "<html>login</html>").fetch_payload()

    def test_page_side_error_json_is_reported(self):
        def js_error(_expr):
            return json.dumps({"__contra_error": "NetworkError"})

        with pytest.raises(ContraAuthUnavailable):
            ContraAuthenticatedSource(js_error).fetch_payload()


class TestFetchContract:
    def test_fetch_yields_parsed_records(self, source, payload):
        source.fetch_payload = lambda: payload  # type: ignore[method-assign]
        got = list(source.fetch())
        assert len(got) >= 1

    def test_fetch_applies_limit(self, source, payload):
        source.fetch_payload = lambda: payload  # type: ignore[method-assign]
        assert len(list(source.fetch(limit=1))) == 1

    def test_fetch_raises_when_nothing_readable(self, source):
        source.fetch_payload = lambda: {"data": {"jobFeed": {"count": 0, "edges": []}}}  # type: ignore[method-assign]
        with pytest.raises(ContraAuthUnavailable):
            list(source.fetch())

    def test_expression_targets_the_site_own_operation(self):
        expr = ContraAuthenticatedSource._feed_expression("ALL", "NEWEST", 150)
        assert "JobFeed_jobFeedPaginationQuery" in expr
        # A query only: no mutation may appear in the expression.
        assert "Mutation" not in expr
        assert "apply" not in expr.lower()


class TestNormalizationAndSeparation:
    def test_normalizes_into_gig_model(self, records):
        gig = normalize_gig(records[0])
        assert gig.source == "contra"
        assert gig.source_mode is SourceMode.LIVE
        assert gig.source_scope == SOURCE_SCOPE
        assert gig.source_status == "OPEN"
        assert gig.budget_type in set(BudgetType)
        assert gig.id.startswith("gig_")

    def test_authenticated_feed_differs_from_archived_hire_scope(self):
        """P2.1 §9: the authenticated feed must not be confused with the /hire archive."""
        authed = normalize_gig({
            "source": "contra", "source_id": "job_1", "title": "T",
            "source_mode": "live", "source_scope": "authenticated_feed", "source_status": "OPEN",
        })
        archived = normalize_gig({
            "source": "contra", "source_id": "WyJKb2JQb3N0aW5nIiw0NDQwOTQ0XQ==", "title": "T",
            "source_mode": "live", "source_scope": "public_hire_pages", "source_status": "EXPIRED",
        })
        assert authed.source_scope != archived.source_scope
        assert authed.source_status == "OPEN"
        assert archived.source_status == "EXPIRED"

    def test_open_record_is_not_gated_as_closed(self, records):
        from ai_assistant.gig.hard_gates import evaluate_hard_gates

        gig = normalize_gig(records[0])
        gate = evaluate_hard_gates(gig)
        assert "SOURCE_CLOSED" not in gate.reasons

    def test_archived_hire_record_is_still_gated(self, records):
        """Regression guard: the P2 rule must keep working for the archive."""
        from ai_assistant.gig.hard_gates import evaluate_hard_gates

        gig = normalize_gig({
            "source": "contra", "source_id": "x", "title": "Archive", "budget": "USD 500",
            "source_status": "EXPIRED", "source_mode": "live", "source_scope": "public_hire_pages",
        })
        assert "SOURCE_CLOSED" in evaluate_hard_gates(gig).reasons

    def test_payment_never_ready_from_platform_capability(self, records):
        from ai_assistant.gig.assessment import assess_payment_readiness

        for record in records:
            assert assess_payment_readiness(normalize_gig(record)) is not PaymentReadiness.READY
