"""Shared fixtures for the Gig Search contour tests.

The global conftest already redirects config.DB_FILE into tmp_path and
blocks the network, so these fixtures only need to provide builders for
raw gig payloads and a persisted-profile helper.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_FILE = PROJECT_ROOT / "artifacts" / "gig_fixture_sample.json"


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


@pytest.fixture()
def raw_gig_factory():
    return raw_gig


@pytest.fixture()
def fixture_payload() -> list[dict[str, Any]]:
    """The committed 28-gig fixture dataset."""
    data = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("gigs") or []
    assert len(data) >= 20, "fixture dataset must keep at least 20 gigs"
    return data


@pytest.fixture()
def profile_file(tmp_path: Path) -> Path:
    """A small candidate profile for assessment tests."""
    profile = {
        "core_skills": ["python", "n8n", "automation", "rest api", "sql"],
        "secondary_skills": ["docker", "postgresql", "fastapi"],
        "skill_confidence": {"python": "PROFESSIONAL", "n8n": "PROFESSIONAL", "react": "UNKNOWN"},
    }
    path = tmp_path / "candidate_profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path
