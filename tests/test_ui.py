import tempfile
import os
import shutil
import pytest

from fastapi.testclient import TestClient

from ai_assistant.ui.app import app
from ai_assistant.schema import Vacancy
from ai_assistant import cli
from ai_assistant import db
import ai_assistant.config as config


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


def test_ui_review_validation():
    # Invalid action should return 400
    response = client.post("/api/review/nonexistent_123", json={"action": "invalid_action"})
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


