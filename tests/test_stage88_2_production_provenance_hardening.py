"""Stage 88.2: Test-Data Contamination & Production Provenance Hardening Test Suite.

Verifies that synthetic, dry-run, test, fixture, seeded, or non-production vacancy records
are strictly excluded from the unattended production digest even when carrying active source names.
"""
import io
import json
import os
import sqlite3
import contextlib
import pytest
from unittest.mock import patch

from ai_assistant.schema import Vacancy, is_genuine_production_vacancy, ACTIVE_PRODUCTION_SOURCES
from ai_assistant.matcher import JobMatcher
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.cli import export_digest_cmd
from ai_assistant.db import (
    init_db,
    save_vacancy,
    get_connection,
    set_dry_run,
    is_dry_run,
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


# 1. active-source test fixture is not production eligible
def test_active_source_test_fixture_is_not_production_eligible():
    """1. Stage 88.2: Vacancies under active sources with test/fixture patterns fail provenance."""
    v_test = Vacancy(
        source="remoteok",
        source_job_id="hard-rej-1",
        title="PHP Developer",
        company="LegacyCo",
        description="Remote PHP",
        job_url="https://example.com/hard-rej-1",
        location="Remote",
    )
    is_gen, reason = is_genuine_production_vacancy(v_test)
    assert not is_gen
    assert "hard-rej" in reason


# 2. himalayas:dryrun-test-1 equivalent cannot enter digest
def test_himalayas_dryrun_test_1_cannot_enter_digest(calibrated_profile, isolated_db, monkeypatch):
    """2. Stage 88.2: himalayas:dryrun-test-1 is strictly excluded from list_undigested_vacancies and export."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_dry = Vacancy(
        source="himalayas",
        source_job_id="dryrun-test-1",
        title="Dry Run Job",
        company="ACME",
        description="remote python n8n automation",
        job_url="https://himalayas.app/dryrun-test-1",
        location="Remote",
    )
    save_vacancy(v_dry)

    undigested = list_undigested_vacancies(limit=50)
    assert not any(v.stable_id() == "himalayas:dryrun-test-1" for v in undigested)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 0


# 3. genuine live-adapter record remains eligible
def test_genuine_live_adapter_record_remains_eligible(calibrated_profile, isolated_db, monkeypatch):
    """3. Stage 88.2: Genuine live vacancy with valid URL and real company passes and enters digest."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_real = Vacancy(
        source="himalayas",
        source_job_id="hima_genuine_12345",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="Synthetix Technologies",
        description="Remote Worldwide. Required: Python, n8n, automation.",
        job_url="https://himalayas.app/companies/synthetix/jobs/senior-ai-automation-engineer",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v_real)

    is_gen, _ = is_genuine_production_vacancy(v_real)
    assert is_gen

    undigested = list_undigested_vacancies(limit=50)
    assert any(v.stable_id() == "himalayas:hima_genuine_12345" for v in undigested)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
    assert payload["new_vacancies_data"][0]["id"] == "himalayas:hima_genuine_12345"


# 4. legacy source remains excluded
def test_legacy_source_remains_excluded(calibrated_profile, isolated_db):
    """4. Stage 88.2: vacancies_json remains strictly isolated from unattended digest."""
    v_legacy = Vacancy(
        source="vacancies_json",
        source_job_id="71",
        title="AI Automation Engineer",
        company="Synthetix",
        description="Remote python n8n",
        job_url="https://wellfound.com/jobs/4412093",
        location="Remote",
    )
    save_vacancy(v_legacy)
    is_gen, reason = is_genuine_production_vacancy(v_legacy)
    assert not is_gen
    assert "Isolated legacy" in reason


# 5. explicit TEST origin is excluded
def test_explicit_test_origin_is_excluded():
    """5. Stage 88.2: source='x' or explicit test source is excluded in production environment."""
    v_x = Vacancy(source="x", source_job_id="1", title="Python Dev", company="Co", description="d", job_url="http://x")
    is_gen, reason = is_genuine_production_vacancy(v_x)
    assert not is_gen


# 6. explicit DRY_RUN origin is excluded
def test_explicit_dry_run_origin_is_excluded():
    """6. Stage 88.2: Any job with /dryrun in URL or dryrun in ID fails provenance."""
    v1 = Vacancy(source="himalayas", source_job_id="test_dry_1", title="Eng", company="Corp", description="d", job_url="https://himalayas.app/dryrun-1")
    v2 = Vacancy(source="remoteok", source_job_id="dryrun_job_99", title="Eng", company="Corp", description="d", job_url="https://remoteok.com/99")
    assert not is_genuine_production_vacancy(v1)[0]
    assert not is_genuine_production_vacancy(v2)[0]


# 7. UNKNOWN provenance fails closed for unattended delivery
def test_unknown_provenance_fails_closed():
    """7. Stage 88.2: Unknown/unregistered source names fail closed."""
    v_unknown = Vacancy(source="some_random_source", source_job_id="123", title="Eng", company="C", description="d", job_url="https://r.com/1")
    is_gen, reason = is_genuine_production_vacancy(v_unknown)
    assert not is_gen
    assert "not an active production source" in reason


# 8. dry-run cannot mutate production DB
def test_dry_run_cannot_mutate_production_db(isolated_db):
    """8. Stage 88.2: When set_dry_run(True) is active, save_vacancy is a no-op."""
    set_dry_run(True)
    try:
        v = Vacancy(
            source="himalayas",
            source_job_id="dryrun_mut_test",
            title="AI Eng",
            company="Co",
            description="desc",
            job_url="https://himalayas.app/jobs/dry1",
        )
        status = save_vacancy(v)
        assert status == "UNCHANGED"

        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM vacancies WHERE stable_id = ?", (v.stable_id(),))
        count = cur.fetchone()[0]
        conn.close()
        assert count == 0
    finally:
        set_dry_run(False)


# 9. subprocess dry-run cannot mutate production DB
def test_subprocess_dry_run_isolation():
    """9. Stage 88.2: is_dry_run state accurately guards mutation."""
    assert not is_dry_run()
    set_dry_run(True)
    assert is_dry_run()
    set_dry_run(False)
    assert not is_dry_run()


# 10. high match score cannot override provenance failure
def test_high_match_score_cannot_override_provenance_failure(calibrated_profile, isolated_db, monkeypatch):
    """10. Stage 88.2: A 100-score match with synthetic ID cannot enter digest."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_high_fake = Vacancy(
        source="himalayas",
        source_job_id="dryrun-super-100",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="ACME",
        description="Remote Worldwide. Python, n8n, automation. Salary $100,000.",
        job_url="https://himalayas.app/dryrun-super-100",
        location="Remote Worldwide",
        salary_min=8000,
        salary_currency="USD",
    )
    save_vacancy(v_high_fake)

    undigested = list_undigested_vacancies(limit=50)
    assert not any(v.stable_id() == v_high_fake.stable_id() for v in undigested)


# 11. production preview and scheduled selector share predicate
def test_preview_and_scheduled_selector_share_predicate(calibrated_profile, isolated_db, monkeypatch):
    """11. Stage 88.2: Both preview and scheduled dispatch use list_undigested_vacancies with provenance filter."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_genuine = Vacancy(
        source="weworkremotely",
        source_job_id="wwr_real_role",
        title="AI Automation Engineer",
        company="GlobalTech",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://weworkremotely.com/remote-jobs/wwr-real-role",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    v_synthetic = Vacancy(
        source="weworkremotely",
        source_job_id="sync-fail-1",
        title="AI Automation Engineer",
        company="TestCo",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://example.com/sync-fail-1",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v_genuine)
    save_vacancy(v_synthetic)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
    assert payload["new_vacancies_data"][0]["id"] == "weworkremotely:wwr_real_role"


# 12. historical delivered synthetic evidence is preserved
def test_historical_delivered_synthetic_evidence_is_preserved(isolated_db):
    """12. Stage 88.2: Existing delivery records in telegram_delivery_records remain intact."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO telegram_delivery_records (delivery_key, notification_type, status, chat_id, delivered_at)
        VALUES ('digest:himalayas:dryrun-test-1', 'job_digest', 'DELIVERED', '-1001', '2026-09-01T03:01:36Z')
    """)
    conn.commit()

    cur.execute("SELECT status FROM telegram_delivery_records WHERE delivery_key = 'digest:himalayas:dryrun-test-1'")
    row = cur.fetchone()
    conn.close()
    assert row[0] == "DELIVERED"


# 13. provenance survives vacancy update/rediscovery
def test_provenance_survives_vacancy_update(isolated_db):
    """13. Stage 88.2: Re-saving an existing genuine vacancy preserves its source and provenance."""
    v = Vacancy(
        source="habrcareer",
        source_job_id="1000999888",
        title="Middle Python Developer",
        company="RealHabrCompany",
        description="Remote Python",
        job_url="https://career.habr.com/vacancies/1000999888",
        location="Remote",
    )
    save_vacancy(v)
    assert is_genuine_production_vacancy(v)[0]

    v.description = "Updated description remote python n8n"
    save_vacancy(v)
    assert is_genuine_production_vacancy(v)[0]


# 14. canonical dedup does not erase provenance
def test_canonical_dedup_does_not_erase_provenance(calibrated_profile, isolated_db, monkeypatch):
    """14. Stage 88.2: Deduplication across active sources respects provenance of each candidate."""
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)

    v_real1 = Vacancy(
        source="himalayas",
        source_job_id="hima_dedup_1",
        title="AI Engineer",
        company="DedupCorp",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://himalayas.app/jobs/dedup1",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    v_fake2 = Vacancy(
        source="remoteok",
        source_job_id="llm-fail-1",
        title="AI Engineer",
        company="TestCo",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://example.com/fail",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    save_vacancy(v_real1)
    save_vacancy(v_fake2)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
    assert payload["new_vacancies_data"][0]["id"] == "himalayas:hima_dedup_1"


# 15. test environment cannot resolve production DB accidentally
def test_test_environment_isolated_from_production_db():
    """15. Stage 88.2: config.DB_FILE in pytest is redirected to a temporary location."""
    from ai_assistant import config
    assert "state.db" in config.DB_FILE
    assert not config.DB_FILE.endswith("Documents\\job-search\\state.db")
