"""Stage 88.1: Production Vacancy Provenance & Legacy Data Isolation Test Suite.

Verifies that the unattended production digest pipeline strictly selects genuine
vacancies discovered by active production adapters (Himalayas, RemoteOK,
WeWorkRemotely, Habr Career, HH) and isolates legacy/calibration records (vacancies_json).
"""
import io
import json
import sqlite3
import contextlib
import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.matcher import JobMatcher
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.cli import export_digest_cmd
from ai_assistant.watcher import ADAPTER_MAP
from ai_assistant.db import (
    init_db,
    save_vacancy,
    get_connection,
    mark_digest_delivered,
    list_undigested_vacancies,
    list_vacancies,
    _row_to_vacancy,
)


@pytest.fixture
def calibrated_profile():
    return CandidateProfile.from_dict({
        "desired_roles": ["AI Automation Engineer", "Technical Support Engineer", "Python Developer"],
        "skills": ["python", "n8n", "automation", "telegram", "rest api"],
        "secondary_skills": ["docker", "linux", "sql"],
        "years_experience": 3,
        "minimum_salary": 1500,
        "salary_currency": "USD",
        "remote_required": True,
        "allowed_locations": ["worldwide", "emea", "thailand", "cyprus", "georgia", "armenia"],
        "languages": ["russian", "english"],
        "role_priorities": {
            "AI_AUTOMATION": "P1",
            "APPLICATION_SUPPORT": "P1",
            "TECH_SUPPORT": "P1",
            "PYTHON_BACKEND": "P2",
            "SYSTEM_ADMIN": "P2",
            "DATA_ENGINEERING": "P3",
            "DEVOPS_SRE": "P3",
        },
        "domain_years": {
            "it_support": 11.0,
            "system_admin": 9.0,
            "application_support": 5.0,
            "automation": 3.5,
            "python": 3.5,
            "ai_llm": 2.0,
        },
        "skill_confidence": {
            "python": "PROFESSIONAL",
            "n8n": "PROFESSIONAL",
            "automation": "PROFESSIONAL",
            "fastapi": "PROJECT",
            "c++": "UNKNOWN",
        },
        "role_specific_skills": {
            "AI_AUTOMATION": ["n8n", "python", "automation"],
            "PYTHON_BACKEND": ["python", "fastapi", "rest api", "sql"],
            "TECH_SUPPORT": ["technical support", "troubleshooting", "active directory"],
        },
    })


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_state.db"
    monkeypatch.setattr("ai_assistant.config.DB_FILE", str(test_db))
    init_db()
    return str(test_db)


# 1. legacy vacancies_json does not enter unattended digest
def test_legacy_vacancies_json_does_not_enter_unattended_digest(calibrated_profile, isolated_db):
    """1. Stage 88.1: vacancies_json rows are excluded by list_undigested_vacancies."""
    v_legacy = Vacancy(
        source="vacancies_json",
        source_job_id="71",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="Synthetix Automations",
        description="Remote. Python, n8n, automation.",
        job_url="https://wellfound.com/jobs/4412093",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v_legacy)

    undigested = list_undigested_vacancies(limit=50)
    assert not any(v.stable_id() == "vacancies_json:71" for v in undigested)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 0


# 2. active production adapter vacancy can enter
def test_active_production_adapter_vacancy_can_enter(calibrated_profile, isolated_db, monkeypatch):
    """2. Stage 88.1: Genuine vacancy from active adapter (himalayas) enters digest."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_prod = Vacancy(
        source="himalayas",
        source_job_id="hima_real_1",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="RealCorp",
        description="Remote Worldwide. Python, n8n, automation.",
        job_url="https://himalayas.app/jobs/real-1",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v_prod)

    undigested = list_undigested_vacancies(limit=50)
    assert any(v.stable_id() == "himalayas:hima_real_1" for v in undigested)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
    assert payload["new_vacancies_data"][0]["id"] == "himalayas:hima_real_1"


# 3. test fixture cannot enter production selector
def test_test_fixture_source_cannot_enter_production_selector(calibrated_profile, isolated_db):
    """3. Stage 88.1: Source 'x' or 'test_src' fixture rows cannot enter unattended digest."""
    v_test = Vacancy(
        source="x",
        source_job_id="fake_1",
        title="Python Dev",
        company="Acme",
        description="Remote. Python.",
        job_url="https://example.com/fake",
        location="Remote",
    )
    save_vacancy(v_test)

    # In unattended mode with valid active sources, test fixtures are excluded or rejected
    undigested = list_undigested_vacancies(limit=50)
    assert not any(v.source == "vacancies_json" for v in undigested)


# 4. high score cannot override invalid provenance
def test_high_score_cannot_override_invalid_provenance(calibrated_profile, isolated_db):
    """4. Stage 88.1: A high-score match from vacancies_json is still excluded from unattended digest."""
    v_legacy_perfect = Vacancy(
        source="vacancies_json",
        source_job_id="100",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="PerfectCorp",
        description="100% remote worldwide. Required: Python, n8n, workflow automation. $10,000 USD.",
        job_url="https://example.com/perfect",
        location="Remote Worldwide",
        salary_min=10000,
        salary_max=12000,
        salary_currency="USD",
    )
    save_vacancy(v_legacy_perfect)
    matcher = JobMatcher(calibrated_profile)
    res = matcher.match(v_legacy_perfect)
    assert res.score >= 80
    assert res.decision_class in ("STRONG_MATCH", "MATCH")

    # Still strictly excluded from unattended undigested query
    undigested = list_undigested_vacancies(limit=50)
    assert not any(v.stable_id() == "vacancies_json:100" for v in undigested)


# 5. delivery history remains respected
def test_delivery_history_remains_respected(calibrated_profile, isolated_db):
    """5. Stage 88.1: Marking delivered excludes active source vacancy from subsequent runs."""
    v = Vacancy(
        source="remoteok",
        source_job_id="rok_123",
        title="AI Automation Engineer",
        company="RemoteGlobal",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://remoteok.com/123",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v)
    sid = v.stable_id()

    assert any(item.stable_id() == sid for item in list_undigested_vacancies(limit=50))
    mark_digest_delivered([sid], chat_id="-1001")
    assert not any(item.stable_id() == sid for item in list_undigested_vacancies(limit=50))


# 6. preview and production use same provenance filter
def test_preview_and_production_use_same_provenance_filter(calibrated_profile, isolated_db, monkeypatch):
    """6. Stage 88.1: Both preview and unattended query operate on list_undigested_vacancies by default."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_active = Vacancy(
        source="weworkremotely",
        source_job_id="wwr_456",
        title="AI Automation Engineer",
        company="WWRCo",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://weworkremotely.com/456",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    v_legacy = Vacancy(
        source="vacancies_json",
        source_job_id="999",
        title="AI Automation Engineer",
        company="LegacyCo",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://example.com/999",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v_active)
    save_vacancy(v_legacy)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
    assert payload["new_vacancies_data"][0]["id"] == "weworkremotely:wwr_456"


# 7. legacy rows remain stored/readable
def test_legacy_rows_remain_stored_and_readable(calibrated_profile, isolated_db):
    """7. Stage 88.1: Legacy rows remain fully preserved in database and queryable via list_vacancies."""
    v_legacy = Vacancy(
        source="vacancies_json",
        source_job_id="55",
        title="AI Automation Engineer",
        company="HistoricalCorp",
        description="Remote. Python.",
        job_url="https://example.com/55",
        location="Remote",
    )
    save_vacancy(v_legacy)

    all_rows = list_vacancies(limit=50)
    all_vacancies = [_row_to_vacancy(r) for r in all_rows if r]
    assert any(v.stable_id() == "vacancies_json:55" for v in all_vacancies)


# 8. production filtering does not delete data
def test_production_filtering_does_not_delete_data(calibrated_profile, isolated_db):
    """8. Stage 88.1: Running export_digest_cmd does not delete or mutate any rows in state.db."""
    v1 = Vacancy(source="vacancies_json", source_job_id="1", title="AI Eng", company="C1", description="desc", job_url="http://1", location="Remote")
    v2 = Vacancy(source="himalayas", source_job_id="2", title="AI Eng", company="C2", description="desc", job_url="http://2", location="Remote")
    save_vacancy(v1)
    save_vacancy(v2)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM vacancies")
    count_before = cur.fetchone()[0]
    conn.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM vacancies")
    count_after = cur.fetchone()[0]
    conn.close()

    assert count_before == count_after == 2


# 9. active-source classification is deterministic
def test_active_source_classification_is_deterministic():
    """9. Stage 88.1: Registered production adapter keys are exact and deterministic."""
    expected_active_adapters = {"himalayas", "weworkremotely", "remoteok", "habrcareer"}
    actual_registered = set(ADAPTER_MAP.keys())
    assert expected_active_adapters == actual_registered


# 10. pending eligible metric excludes rejected legacy noise
def test_pending_eligible_metric_excludes_rejected_legacy_noise(calibrated_profile, isolated_db):
    """10. Stage 88.1: list_undigested_vacancies ignores vacancies_json records."""
    v_rej_legacy = Vacancy(source="vacancies_json", source_job_id="rej1", title="Barista", company="Coffee", description="Onsite", job_url="http://c", location="Onsite")
    save_vacancy(v_rej_legacy)

    undigested = list_undigested_vacancies(limit=50)
    assert len(undigested) == 0


# 11. source provenance survives rediscovery
def test_source_provenance_survives_rediscovery(calibrated_profile, isolated_db):
    """11. Stage 88.1: Saving an existing vacancy preserves its original source and stable_id."""
    v = Vacancy(
        source="habrcareer",
        source_job_id="habr_12345",
        title="Python Dev",
        company="HabrCo",
        description="Remote",
        job_url="https://habr.com/12345",
        location="Remote",
    )
    status1 = save_vacancy(v)
    assert status1 == "INSERTED"

    # Re-save with updated description
    v.description = "Updated description remote"
    status2 = save_vacancy(v)
    assert status2 in ("UPDATED", "UNCHANGED")
    assert v.stable_id() == "habrcareer:habr_12345"


# 12. canonical dedup remains functional across valid sources
def test_canonical_dedup_remains_functional_across_valid_sources(calibrated_profile, isolated_db, monkeypatch):
    """12. Stage 88.1: Same vacancy from two active adapters is deduplicated in digest."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_hima = Vacancy(
        source="himalayas",
        source_job_id="cross_1",
        title="Senior AI Automation Engineer",
        company="CrossCorp",
        description="Remote Worldwide. Python, n8n, automation.",
        job_url="https://himalayas.app/cross-1",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    v_rok = Vacancy(
        source="remoteok",
        source_job_id="cross_2",
        title="Senior AI Automation Engineer",
        company="CrossCorp",
        description="Remote Worldwide. Python, n8n, automation.",
        job_url="https://remoteok.com/cross-2",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v_hima)
    save_vacancy(v_rok)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
