"""API tests: the standalone Gig Search FastAPI app (§9).

Uses in-process TestClient (loopback is allowed by the global conftest) and
an isolated DB via config.DB_FILE redirect.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_assistant.gig.api import app
from ai_assistant.gig.pipeline import run_fixture_pipeline
from ai_assistant.ui.auth import get_dashboard_token


@pytest.fixture()
def client(seeded_db):
    token = get_dashboard_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with TestClient(app, headers=headers) as test_client:
        yield test_client


@pytest.fixture()
def seeded_db():
    return run_fixture_pipeline(persist=True)


class TestGigApi:
    def test_list_gigs(self, client):
        resp = client.get("/api/gigs", params={"limit": 10})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["stats"]["total"] >= 20
        assert len(payload["gigs"]) == 10
        brief = payload["gigs"][0]
        for field in (
            "title",
            "source",
            "budget",
            "opportunity_score",
            "can_deliver",
            "ai_automation_potential",
            "estimated_effort_hours",
            "effective_hourly_rate",
            "competition",
            "client_quality",
            "payment_readiness",
            "risk_level",
            "why_matched",
            "why_risky",
            "url",
        ):
            assert field in brief, field

    def test_gig_detail_includes_events(self, client):
        gigs = client.get("/api/gigs", params={"limit": 1}).json()["gigs"]
        gig_id = gigs[0]["id"]
        resp = client.get(f"/api/gigs/{gig_id}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["score_breakdown"]["components"]
        assert isinstance(detail["events"], list)

    def test_detail_404(self, client):
        assert client.get("/api/gigs/gig_missing").status_code == 404

    def test_status_update(self, client):
        gigs = client.get("/api/gigs", params={"limit": 1}).json()["gigs"]
        gig_id = gigs[0]["id"]
        resp = client.post(f"/api/gigs/{gig_id}/status", json={"status": "SKIPPED", "note": "not interested"})
        assert resp.status_code == 200
        assert resp.json()["gig_status"] == "SKIPPED"
        detail = client.get(f"/api/gigs/{gig_id}").json()
        assert detail["status"] == "SKIPPED"

    def test_status_update_unauthorized_without_token(self, seeded_db):
        """Mutations without token return 401 when DASHBOARD_TOKEN is configured (GIG-001)."""
        with TestClient(app) as unauthed_client:
            gigs = unauthed_client.get("/api/gigs", params={"limit": 1}).json()["gigs"]
            gig_id = gigs[0]["id"]
            resp = unauthed_client.post(
                f"/api/gigs/{gig_id}/status",
                json={"status": "SKIPPED"},
            )
            assert resp.status_code == 401
            assert "Unauthorized" in resp.json()["detail"]

    def test_status_update_503_when_token_not_configured(self, monkeypatch, seeded_db):
        """Mutations return 503 when DASHBOARD_TOKEN is not configured (GIG-001)."""
        monkeypatch.setenv("DASHBOARD_TOKEN", "")
        monkeypatch.setattr("ai_assistant.config.DASHBOARD_TOKEN", "")
        with TestClient(app) as unauthed_client:
            gigs = unauthed_client.get("/api/gigs", params={"limit": 1}).json()["gigs"]
            gig_id = gigs[0]["id"]
            resp = unauthed_client.post(
                f"/api/gigs/{gig_id}/status",
                json={"status": "SKIPPED"},
            )
            assert resp.status_code == 503
            assert "DASHBOARD_TOKEN not configured" in resp.json()["detail"]

    def test_status_validation(self, client):
        gigs = client.get("/api/gigs", params={"limit": 1}).json()["gigs"]
        resp = client.post(f"/api/gigs/{gigs[0]['id']}/status", json={"status": "APPLIED"})
        # APPLIED — P2-статус: контур P1 его не принимает.
        assert resp.status_code == 422

    def test_stats_endpoint(self, client):
        resp = client.get("/api/gigs-stats")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 20

    def test_order_and_filters(self, client):
        resp = client.get("/api/gigs", params={"order": "effective_hourly_rate", "limit": 5})
        assert resp.status_code == 200
        rates = [g["effective_hourly_rate"] or 0 for g in resp.json()["gigs"]]
        assert rates == sorted(rates, reverse=True)

    def test_gigs_page_served_or_placeholder(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "gig" in resp.text.lower()
