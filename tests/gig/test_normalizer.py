"""Normalizer tests: raw dict -> Gig, unknown-safe, currency/budget parsing."""
from __future__ import annotations

import pytest

from ai_assistant.gig.normalizer import (
    content_hash,
    normalize_gig,
    normalize_published_at,
    normalize_skills,
    parse_budget_range,
    parse_currency,
)


class TestBudgetParsing:
    def test_dollar_string(self):
        low, high, btype = parse_budget_range("$1,500")
        assert (low, high) == (1500.0, 1500.0)
        assert btype.value == "fixed"

    def test_hourly_range(self):
        low, high, btype = parse_budget_range("20-40 USD/hr")
        assert (low, high) == (20.0, 40.0)
        assert btype is not None and btype.value == "hourly"

    def test_empty_is_unknown_not_zero(self):
        low, high, btype = parse_budget_range(None)
        assert low is None and high is None
        assert btype.value == "unknown"


class TestCurrency:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("$1,500", "USD"), ("1200 EUR", "EUR"), ("£30/h", "GBP"), ("50 000 руб", "RUB"), (None, None), ("", None)],
    )
    def test_parse_currency(self, raw, expected):
        assert parse_currency(raw) == expected


class TestSkills:
    def test_split_and_dedupe_preserving_order(self):
        assert normalize_skills("Python, N8N; python (3 years) | Webhooks") == ["python", "n8n", "webhooks"]

    def test_none_is_empty_list(self):
        assert normalize_skills(None) == []


class TestPublishedAt:
    def test_iso_z(self):
        # Модуль канонизирует UTC-время в суффиксом Z.
        assert normalize_published_at("2026-09-20T10:00:00+00:00") == "2026-09-20T10:00:00Z"

    def test_unix_ms(self):
        assert normalize_published_at(1758352800000).startswith("2025-")

    def test_garbage_is_none(self):
        assert normalize_published_at("not a date") is None


class TestNormalizeGig:
    def test_requires_source_and_source_id(self):
        with pytest.raises(ValueError):
            normalize_gig({"title": "no identity"})
        with pytest.raises(TypeError):
            normalize_gig("not a dict")  # type: ignore[arg-type]

    def test_client_nested_fields(self):
        gig = normalize_gig(
            {
                "source": "fixture",
                "source_id": "c1",
                "client": {"rating": 4.8, "total_spent": 10000, "hires": 3, "jobs_posted": 4},
            }
        )
        assert gig.client_rating == 4.8
        assert gig.client_total_spent == 10000
        assert gig.client_hires == 3

    def test_unknown_fields_stay_none(self):
        gig = normalize_gig({"source": "fixture", "source_id": "u1"})
        assert gig.title == ""
        assert gig.budget_min is None
        assert gig.currency is None
        assert gig.published_at is None

    def test_content_hash_stable_and_sensitive(self):
        a = normalize_gig({"source": "s", "source_id": "1", "title": "Build a bot", "budget": "$100"})
        b = normalize_gig({"source": "s", "source_id": "2", "title": "Build a bot", "budget": "$100"})
        c = normalize_gig({"source": "s", "source_id": "3", "title": "Build a bot", "budget": "$200"})
        assert content_hash(a) == content_hash(b)
        assert content_hash(a) != content_hash(c)
