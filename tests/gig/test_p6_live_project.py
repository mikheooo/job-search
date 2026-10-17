"""Authoritative P6 observations must not inherit cached page claims."""
import pytest
from ai_assistant.gig.live_project import overlay_public_project, project_api_url


def observed(**changes):
    result = {"id": 42, "description": "Current brief", "title": "Current title",
              "jobs": [{"name": "Python"}], "budget": {"minimum": 30, "maximum": 250},
              "currency": {"code": "EUR", "exchange_rate": 1.12},
              "bid_stats": {"bid_count": 32}, "frontend_project_status": "open",
              "status": "active", "submitdate": 1791020000, "type": "fixed"}
    result.update(changes)
    return overlay_public_project({"source_id": "fl_42", "competition": 4,
                                   "description": "Cached", "currency": "USD"}, result,
                                  observed_at="2026-10-03T14:00:00+00:00",
                                  api_url=project_api_url("fl_42"))


def test_live_api_replaces_cached_competition_currency_and_brief():
    row = observed()
    assert row["competition"] == 32 and row["currency"] == "EUR"
    assert row["description"] == "Current brief"
    assert row["source_facts"]["freelancer_page_bids_cached"] == 4
    assert row["source_facts"]["freelancer_currency_exchange_rate"] == 1.12


@pytest.mark.parametrize("frontend,status,expected", [("open", "pending", "UNKNOWN"),
    ("", "active", "UNKNOWN"), ("closed", "active", "CLOSED"),
    ("awarded", "active", "AWARDED"), ("open", "active", "OPEN")])
def test_api_requires_explicit_open_and_active(frontend, status, expected):
    assert observed(frontend_project_status=frontend, status=status)["source_status"] == expected


def test_absent_live_fields_clear_stale_values_and_zero_bids_are_real():
    row = observed(description=None, currency=None, bid_stats={"bid_count": 0})
    assert row["description"] == "" and row["currency"] is None
    assert row["competition"] == 0
    assert observed(bid_stats=None)["competition"] is None


def test_api_identity_mismatch_fails_instead_of_reusing_page():
    with pytest.raises(ValueError, match="identity"):
        observed(id=43)


@pytest.mark.parametrize("project_id", ["", "fl_../42", "garbage"])
def test_api_url_rejects_non_numeric_identity(project_id):
    with pytest.raises(ValueError):
        project_api_url(project_id)
