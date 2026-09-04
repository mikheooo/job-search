import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.ui.app import app


@pytest.fixture(autouse=True)
def isolated_db():
    tmp_dir = tempfile.mkdtemp()
    db_file = os.path.join(tmp_dir, "test_ui_state.db")
    orig_db = config.DB_FILE
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db
    shutil.rmtree(tmp_dir, ignore_errors=True)


client = TestClient(app)


def test_ui_stats_endpoint():
    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert "total_vacancies" in data
    assert "by_source" in data
    assert "by_state" in data


def test_ui_vacancies_endpoint():
    response = client.get("/api/vacancies?limit=5")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_ui_index_html_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text or "Job-Search Hub" in response.text


def test_ui_queue_endpoint():
    response = client.get("/api/queue")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_ui_review_validation(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token-val")
    # Invalid action should return 400
    response = client.post(
        "/api/review/nonexistent_123",
        json={"action": "invalid_action"},
        headers={"Authorization": "Bearer test-token-val"},
    )
    assert response.status_code == 400


def test_ui_package_detail_endpoint():
    # Verify package detail unpacking from db.get_application_package
    vac = Vacancy(
        source="test",
        source_job_id="101",
        title="AI Automation Engineer",
        company="TechCorp",
        description="LLM / Python / Agents",
        job_url="https://example.com/101",
    )
    db.save_vacancy(vac)
    sid = vac.stable_id()
    db.save_application_package(sid, "v1", '{"cover_letter": "Tailored cover letter text", "form": null}')

    response = client.get(f"/api/package/{sid}")
    assert response.status_code == 200
    data = response.json()
    assert data["vacancy"]["title"] == "AI Automation Engineer"
    assert data["vacancy"]["company"] == "TechCorp"
    assert data["package"]["cover_letter"] == "Tailored cover letter text"
    assert data["deep_analysis"] is None


def test_ui_package_detail_with_sqlite_deep_analysis_tuple():
    """Regression test: verify get_package_detail correctly unpacks SQLite deep_analysis tuple."""
    vac = Vacancy(
        source="test",
        source_job_id="102",
        title="Senior AI Engineer",
        company="AI Labs",
        description="LLM / Agents / PyTorch",
        job_url="https://example.com/102",
    )
    db.save_vacancy(vac)
    sid = vac.stable_id()
    db.save_application_package(sid, "v1", '{"cover_letter": "AI Labs cover letter"}')
    db.save_deep_analysis(
        sid,
        "v1",
        88,
        "RECOMMENDED",
        '{"pros": ["Strong Python", "LLM experience"], "cons": ["Remote timezone shift"], "summary": "Strong fit for AI role"}'
    )

    response = client.get(f"/api/package/{sid}")
    assert response.status_code == 200
    data = response.json()
    assert data["vacancy"]["title"] == "Senior AI Engineer"
    assert data["deep_analysis"] is not None
    assert data["deep_analysis"]["fit_score"] == 88
    assert data["deep_analysis"]["recommendation"] == "RECOMMENDED"
    assert data["deep_analysis"]["pros"] == ["Strong Python", "LLM experience"]
    assert data["deep_analysis"]["cons"] == ["Remote timezone shift"]
    assert data["deep_analysis"]["summary"] == "Strong fit for AI role"


def test_ui_package_detail_with_mocked_sqlite_tuple(monkeypatch):
    """Regression test: explicitly mock get_deep_analysis to return raw SQLite tuple."""
    vac = Vacancy(
        source="test",
        source_job_id="103",
        title="Lead Python Developer",
        company="GlobalTech",
        description="Python backend",
        job_url="https://example.com/103",
    )
    db.save_vacancy(vac)
    sid = vac.stable_id()

    # Raw SQLite tuple: (vacancy_stable_id, analyzer_version, fit_score, recommendation, analysis_json, analyzed_at)
    raw_tuple = (
        sid,
        "v2",
        95,
        "APPLY_NOW",
        '{"pros": ["FastAPI", "AsyncIO"], "cons": [], "summary": "Perfect technical fit"}',
        "2026-08-28T12:00:00",
    )
    import ai_assistant.ui.app as app_module
    monkeypatch.setattr(app_module, "get_deep_analysis", lambda _sid: raw_tuple)

    response = client.get(f"/api/package/{sid}")
    assert response.status_code == 200
    data = response.json()
    assert data["deep_analysis"]["fit_score"] == 95
    assert data["deep_analysis"]["recommendation"] == "APPLY_NOW"
    assert data["deep_analysis"]["pros"] == ["FastAPI", "AsyncIO"]
    assert data["deep_analysis"]["cons"] == []
    assert data["deep_analysis"]["summary"] == "Perfect technical fit"


def test_ui_get_endpoints_accessible_without_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "production-token-123")
    endpoints = ["/api/stats", "/api/vacancies?limit=5", "/api/queue", "/"]
    for ep in endpoints:
        resp = client.get(ep)
        assert resp.status_code == 200, f"GET {ep} failed with {resp.status_code}"


def test_ui_mutating_endpoints_without_dashboard_token_returns_503(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "")

    # POST to review
    r1 = client.post("/api/review/nonexistent_123", json={"action": "approve"})
    assert r1.status_code == 503
    assert r1.json()["detail"] == "DASHBOARD_TOKEN not configured"

    # POST to move
    r2 = client.post("/api/applications/move", json={"vacancy_stable_id": "v1", "new_status": "APPLIED"})
    assert r2.status_code == 503
    assert r2.json()["detail"] == "DASHBOARD_TOKEN not configured"

    # POST to collect
    r3 = client.post("/api/collect", json={})
    assert r3.status_code == 503
    assert r3.json()["detail"] == "DASHBOARD_TOKEN not configured"


def test_ui_mutating_endpoints_with_dashboard_token_missing_auth_returns_401(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secure-dash-token")

    r1 = client.post("/api/review/nonexistent_123", json={"action": "approve"})
    assert r1.status_code == 401
    assert "Unauthorized" in r1.json()["detail"]

    r2 = client.post("/api/collect", json={})
    assert r2.status_code == 401
    assert "Unauthorized" in r2.json()["detail"]


def test_ui_mutating_endpoints_with_wrong_token_returns_401(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secure-dash-token")

    # Wrong Bearer token
    r1 = client.post(
        "/api/review/nonexistent_123",
        json={"action": "approve"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r1.status_code == 401
    assert "Unauthorized" in r1.json()["detail"]

    # Wrong X-API-Key
    r2 = client.post(
        "/api/collect",
        json={},
        headers={"X-API-Key": "wrong-token"},
    )
    assert r2.status_code == 401
    assert "Unauthorized" in r2.json()["detail"]


def test_ui_mutating_endpoints_with_valid_token_succeeds(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secure-dash-token")

    # Bearer token passes auth (action invalid -> 400, not 401/503)
    r1 = client.post(
        "/api/review/nonexistent_123",
        json={"action": "invalid_action"},
        headers={"Authorization": "Bearer secure-dash-token"},
    )
    assert r1.status_code == 400
    assert "Invalid action" in r1.json()["detail"]

    # X-API-Key passes auth (action invalid -> 400, not 401/503)
    r2 = client.post(
        "/api/review/nonexistent_123",
        json={"action": "invalid_action"},
        headers={"X-API-Key": "secure-dash-token"},
    )
    assert r2.status_code == 400
    assert "Invalid action" in r2.json()["detail"]


def test_ui_default_host_is_localhost():
    import inspect

    from ai_assistant.cli import ui_cmd

    # Check function default
    sig = inspect.signature(ui_cmd)
    assert sig.parameters["host"].default == "127.0.0.1"

    # Check CLI parser argument default
    import argparse
    # Parse --help or simulate parser
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    ui_parser = subparsers.add_parser("ui")
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=8000)
    parsed = parser.parse_args(["ui"])
    assert parsed.host == "127.0.0.1"

