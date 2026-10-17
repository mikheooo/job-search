"""Unit tests for gig domain models (GIG-SEARCH-P1 §2)."""
from __future__ import annotations

import pytest

from ai_assistant.gig.models import (
    BudgetType,
    CanDeliver,
    Gig,
    GigStatus,
    PaymentReadiness,
    RejectionReason,
    RiskLevel,
    make_gig_id,
)
from ai_assistant.gig.normalizer import normalize_gig


class TestEnums:
    def test_budget_type_aliases(self):
        assert BudgetType.parse("fixed_price") is BudgetType.FIXED
        assert BudgetType.parse("per-hour") is BudgetType.HOURLY
        assert BudgetType.parse(None) is BudgetType.UNKNOWN
        assert BudgetType.parse("nonsense") is BudgetType.UNKNOWN

    def test_payment_readiness_values(self):
        assert {m.value for m in PaymentReadiness} == {"ready", "possible", "blocked", "unknown"}

    def test_can_deliver_values(self):
        assert {m.value for m in CanDeliver} == {"high", "medium", "low", "unknown"}

    def test_status_parse_is_uppercase(self):
        assert GigStatus.parse("shortlisted") is GigStatus.SHORTLISTED
        assert GigStatus.parse(None) is GigStatus.NEW

    def test_rejection_reasons_match_spec(self):
        expected = {
            "PAYMENT_UNAVAILABLE",
            "COUNTRY_RESTRICTION",
            "ACCESS_UNAVAILABLE",
            "UNDELIVERABLE",
            "SCAM_RISK",
            "DUPLICATE",
        }
        # PRICE_MISSING is our machine-readable spelling of "цена отсутствует".
        assert expected <= {m.value for m in RejectionReason}
        assert "PRICE_MISSING" in {m.value for m in RejectionReason}

    def test_future_statuses_documented_not_accepted(self):
        from ai_assistant.gig.models import FUTURE_GIG_STATUSES

        assert "APPLIED" in FUTURE_GIG_STATUSES
        with pytest.raises(ValueError):
            GigStatus("APPLIED")


class TestGigId:
    def test_deterministic_and_normalised(self):
        a = make_gig_id("Fixture", " fx-1 ")
        b = make_gig_id("fixture", "fx-1")
        assert a == b
        assert a.startswith("gig_")

    def test_post_init_fills_id(self):
        gig = Gig(source="fixture", source_id="fx-9")
        assert gig.id == make_gig_id("fixture", "fx-9")


class TestBudgetText:
    def test_hourly_range(self):
        gig = Gig(source="s", source_id="1", budget_type=BudgetType.HOURLY, budget_min=20, budget_max=30, currency="USD")
        assert gig.budget_text == "USD 20-30/h"

    def test_missing_budget_is_honest_unknown(self):
        gig = Gig(source="s", source_id="1")
        assert gig.budget_text == "unknown"

    def test_single_value(self):
        gig = Gig(source="s", source_id="1", budget_min=500, budget_max=500, currency="USD")
        assert gig.budget_text == "USD 500"


class TestTruthfulness:
    def test_no_invented_defaults(self):
        gig = Gig(source="s", source_id="1")
        assert gig.currency is None
        assert gig.client_rating is None
        assert gig.payment_readiness is PaymentReadiness.UNKNOWN
        assert gig.can_deliver is CanDeliver.UNKNOWN
        assert gig.risk_level is RiskLevel.UNKNOWN
        assert gig.opportunity_score is None

    def test_to_dict_roundtrip_fields(self):
        gig = normalize_gig({"source": "s", "source_id": "1", "title": "T", "skills": ["Python", "python"]})
        data = gig.to_dict()
        assert data["skills"] == ["python"]
        assert data["status"] == "NEW"
        assert data["budget_type"] == "unknown"
