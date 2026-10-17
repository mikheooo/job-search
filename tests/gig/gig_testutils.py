"""Shared helpers for gig tests (plain module, imported by file path)."""
from __future__ import annotations

from typing import Any


def raw_gig(**overrides: Any) -> dict[str, Any]:
    """A minimal valid raw payload with overridable fields."""
    payload: dict[str, Any] = {
        "source": "fixture",
        "source_id": "test-1",
        "url": "https://example-freelance.test/jobs/test-1",
        "title": "Build a webhook automation with n8n",
        "description": "Connect our CRM to Google Sheets via API. Budget: USD 500.",
        "budget": "USD 500",
        "skills": ["n8n", "webhooks", "google sheets"],
        "client": {"name": "Acme", "rating": 4.9, "total_spent": 25000, "hires": 12, "jobs_posted": 20},
        "competition": 5,
        "published_at": "2026-09-20T10:00:00Z",
    }
    payload.update(overrides)
    return payload
