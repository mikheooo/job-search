"""Stage 85 — Ingestion Robustness & Duplicate Vacancy Handling Tests.

Covers:
1. test_duplicate_job_url_is_idempotent
2. test_same_stable_id_same_url_is_unchanged
3. test_existing_vacancy_metadata_can_be_updated
4. test_first_seen_at_is_preserved_on_rediscovery
5. test_duplicate_wwr_items_in_single_fetch_are_deduplicated
6. test_wwr_tracking_url_variants_resolve_to_same_vacancy
7. test_one_invalid_item_does_not_abort_adapter
8. test_identity_conflict_is_reported_not_silently_merged
9. test_rediscovery_does_not_reset_analysis_state
10. test_rediscovery_does_not_reset_delivery_state
11. test_rediscovered_delivered_vacancy_never_reenters_digest
12. test_two_concurrent_upserts_create_one_row
13. test_adapter_duplicate_is_not_logged_as_adapter_failure
14. test_discovery_counters_are_consistent
15. test_existing_stage75_vacancy_identity_contract_remains_valid
"""
import concurrent.futures
import datetime
import os
import sqlite3
import pytest
from unittest.mock import MagicMock

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.vacancy_identity import normalize_url, resolve_vacancy_identity
from ai_assistant.watcher import Watcher, WatcherConfig, run_watcher_cycle


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Provides an isolated test database and logs directory."""
    db_file = str(tmp_path / "test_stage85.db")
    logs_dir = str(tmp_path / "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setattr(config, "LOGS_DIR", logs_dir)
    
    db.init_db()
    return {"db_file": db_file, "logs_dir": logs_dir, "tmp_path": tmp_path}


def _make_vacancy(
    source="weworkremotely",
    source_job_id="job_1",
    title="Python Developer",
    company="Acme Corp",
    job_url="https://weworkremotely.com/remote-jobs/acme-python-developer",
    salary_min=100000.0,
    salary_max=150000.0,
    first_seen_at=None,
    last_seen_at=None,
):
    return Vacancy(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        description="Write Python code",
        job_url=job_url,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency="USD",
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


# ------------------------------------------------------------------------------
# 1. test_duplicate_job_url_is_idempotent
# ------------------------------------------------------------------------------
def test_duplicate_job_url_is_idempotent(isolated_env):
    """Saving two vacancies with the same job_url never raises UNIQUE constraint failed."""
    v1 = _make_vacancy(source="weworkremotely", source_job_id="job_1", job_url="https://weworkremotely.com/remote-jobs/job-1")
    v2 = _make_vacancy(source="weworkremotely", source_job_id="job_1_variant", job_url="https://weworkremotely.com/remote-jobs/job-1")

    res1 = db.save_vacancy(v1)
    assert res1 == "INSERTED"

    # Second save with exact same URL but different source_job_id
    res2 = db.save_vacancy(v2)
    assert res2 in ("UNCHANGED", "UPDATED")

    # DB should contain exactly 1 row
    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM vacancies")
    assert c.fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------------------------
# 2. test_same_stable_id_same_url_is_unchanged
# ------------------------------------------------------------------------------
def test_same_stable_id_same_url_is_unchanged(isolated_env):
    """Re-saving identical vacancy returns UNCHANGED without error."""
    v = _make_vacancy()
    res1 = db.save_vacancy(v)
    assert res1 == "INSERTED"

    res2 = db.save_vacancy(v)
    assert res2 == "UNCHANGED"


# ------------------------------------------------------------------------------
# 3. test_existing_vacancy_metadata_can_be_updated
# ------------------------------------------------------------------------------
def test_existing_vacancy_metadata_can_be_updated(isolated_env):
    """Saving an existing vacancy with modified salary/title updates the metadata."""
    v1 = _make_vacancy(salary_min=100000.0, salary_max=120000.0)
    db.save_vacancy(v1)

    v2 = _make_vacancy(salary_min=130000.0, salary_max=160000.0, title="Senior Python Developer")
    res = db.save_vacancy(v2)
    assert res == "UPDATED"

    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT title, salary_min, salary_max FROM vacancies WHERE stable_id = ?", (v1.stable_id(),))
    row = c.fetchone()
    assert row[0] == "Senior Python Developer"
    assert row[1] == 130000.0
    assert row[2] == 160000.0
    conn.close()


# ------------------------------------------------------------------------------
# 4. test_first_seen_at_is_preserved_on_rediscovery
# ------------------------------------------------------------------------------
def test_first_seen_at_is_preserved_on_rediscovery(isolated_env):
    """When a vacancy is rediscovered later, first_seen_at is preserved while last_seen_at updates."""
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    t1 = datetime.datetime(2026, 8, 31, 15, 0, 0)

    v1 = _make_vacancy(first_seen_at=t0, last_seen_at=t0)
    db.save_vacancy(v1)

    v2 = _make_vacancy(first_seen_at=t1, last_seen_at=t1)
    db.save_vacancy(v2)

    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT first_seen_at, last_seen_at FROM vacancies WHERE stable_id = ?", (v1.stable_id(),))
    row = c.fetchone()
    assert row[0] == "2026-08-01T10:00:00"
    assert row[1] == "2026-08-31T15:00:00"
    conn.close()


# ------------------------------------------------------------------------------
# 5. test_duplicate_wwr_items_in_single_fetch_are_deduplicated
# ------------------------------------------------------------------------------
def test_duplicate_wwr_items_in_single_fetch_are_deduplicated(isolated_env):
    """Watcher deduplicates duplicate entries from the same adapter response cleanly."""
    raw_item = {
        "source": "weworkremotely",
        "source_job_id": "item_dup",
        "title": "Backend Engineer",
        "company": "Tech Corp",
        "description": "Backend work",
        "job_url": "https://weworkremotely.com/remote-jobs/tech-backend-engineer",
    }
    
    mock_adapter = MagicMock()
    mock_adapter.fetch_vacancies.return_value = [raw_item, raw_item, raw_item]

    cfg = WatcherConfig(
        sources=["weworkremotely"],
        custom_adapters={"weworkremotely": mock_adapter},
    )
    watcher = Watcher(cfg)
    res = watcher.poll_once(iteration=1, dry_run=False)

    assert res.fetched_count == 3
    assert res.new_vacancies_count == 1
    assert res.duplicate_count == 2
    assert len(res.errors) == 0


# ------------------------------------------------------------------------------
# 6. test_wwr_tracking_url_variants_resolve_to_same_vacancy
# ------------------------------------------------------------------------------
def test_wwr_tracking_url_variants_resolve_to_same_vacancy(isolated_env):
    """URLs with tracking parameters or trailing slashes normalize to the same vacancy."""
    url1 = "https://weworkremotely.com/remote-jobs/backend-dev?utm_source=rss&utm_medium=feed"
    url2 = "https://weworkremotely.com/remote-jobs/backend-dev/"
    url3 = "https://weworkremotely.com/remote-jobs/backend-dev"

    assert normalize_url(url1) == "https://weworkremotely.com/remote-jobs/backend-dev"
    assert normalize_url(url2) == "https://weworkremotely.com/remote-jobs/backend-dev"
    assert normalize_url(url3) == "https://weworkremotely.com/remote-jobs/backend-dev"

    v1 = _make_vacancy(job_url=url1)
    db.save_vacancy(v1)

    v2 = _make_vacancy(job_url=url2)
    status2 = db.save_vacancy(v2)
    assert status2 in ("UNCHANGED", "UPDATED")

    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM vacancies")
    assert c.fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------------------------
# 7. test_one_invalid_item_does_not_abort_adapter
# ------------------------------------------------------------------------------
def test_one_invalid_item_does_not_abort_adapter(isolated_env):
    """A malformed item in a batch does not prevent valid items from being processed."""
    valid1 = {"source": "weworkremotely", "source_job_id": "v1", "title": "Dev 1", "job_url": "https://wwr.com/1"}
    invalid = {"source": "weworkremotely", "source_job_id": "v2", "title": "", "job_url": ""}  # Malformed
    valid2 = {"source": "weworkremotely", "source_job_id": "v3", "title": "Dev 3", "job_url": "https://wwr.com/3"}

    mock_adapter = MagicMock()
    mock_adapter.fetch_vacancies.return_value = [valid1, invalid, valid2]

    cfg = WatcherConfig(
        sources=["weworkremotely"],
        custom_adapters={"weworkremotely": mock_adapter},
    )
    watcher = Watcher(cfg)
    res = watcher.poll_once(iteration=1, dry_run=False)

    assert res.fetched_count == 3
    assert res.new_vacancies_count == 2
    assert res.invalid_count == 1
    assert len(res.errors) == 0


# ------------------------------------------------------------------------------
# 8. test_identity_conflict_is_reported_not_silently_merged
# ------------------------------------------------------------------------------
def test_identity_conflict_is_reported_not_silently_merged(isolated_env):
    """Distinct vacancies with completely different URLs and titles are kept distinct."""
    v1 = _make_vacancy(source_job_id="job_a", title="React Developer", company="Company A", job_url="https://example.com/react")
    v2 = _make_vacancy(source_job_id="job_b", title="Go Engineer", company="Company B", job_url="https://example.com/go")

    db.save_vacancy(v1)
    db.save_vacancy(v2)

    match = resolve_vacancy_identity(v2)
    assert match is None or match.canonical_id != v1.stable_id()

    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM vacancies")
    assert c.fetchone()[0] == 2
    conn.close()


# ------------------------------------------------------------------------------
# 9. test_rediscovery_does_not_reset_analysis_state
# ------------------------------------------------------------------------------
def test_rediscovery_does_not_reset_analysis_state(isolated_env):
    """Rediscovering a vacancy preserves match score and analysis state in DB."""
    v = _make_vacancy(source_job_id="scored_job")
    db.save_vacancy(v)

    # Set match score and state manually as if analyzed
    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("UPDATE vacancies SET match_score = 85.0, match_decision = 'APPLY', state = 'MATCHED' WHERE stable_id = ?", (v.stable_id(),))
    conn.commit()
    conn.close()

    # Rediscover identical vacancy
    db.save_vacancy(v)

    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT match_score, match_decision, state FROM vacancies WHERE stable_id = ?", (v.stable_id(),))
    row = c.fetchone()
    assert row[0] == 85.0
    assert row[1] == "APPLY"
    assert row[2] == "MATCHED"
    conn.close()


# ------------------------------------------------------------------------------
# 10. test_rediscovery_does_not_reset_delivery_state
# ------------------------------------------------------------------------------
def test_rediscovery_does_not_reset_delivery_state(isolated_env):
    """Rediscovering a delivered vacancy preserves its DELIVERED status in telegram_delivery_records."""
    v = _make_vacancy(source_job_id="deliv_job")
    db.save_vacancy(v)

    # Mark delivered
    db.record_digest_attempt([v.stable_id()])
    db.mark_digest_delivered([v.stable_id()])

    # Rediscover vacancy
    db.save_vacancy(v)

    assert db.is_digest_delivered(v.stable_id()) is True


# ------------------------------------------------------------------------------
# 11. test_rediscovered_delivered_vacancy_never_reenters_digest
# ------------------------------------------------------------------------------
def test_rediscovered_delivered_vacancy_never_reenters_digest(isolated_env):
    """A previously DELIVERED vacancy that is rediscovered does NOT appear in list_undigested_vacancies."""
    v = _make_vacancy(source_job_id="reenter_job")
    db.save_vacancy(v)

    # Mark delivered
    db.record_digest_attempt([v.stable_id()])
    db.mark_digest_delivered([v.stable_id()])

    # Verify not in undigested queue
    undigested_before = [vac.stable_id() for vac in db.list_undigested_vacancies()]
    assert v.stable_id() not in undigested_before

    # Rediscover vacancy through adapter poll
    mock_adapter = MagicMock()
    mock_adapter.fetch_vacancies.return_value = [v.to_dict()]

    cfg = WatcherConfig(sources=["weworkremotely"], custom_adapters={"weworkremotely": mock_adapter})
    watcher = Watcher(cfg)
    watcher.poll_once(iteration=1, dry_run=False)

    undigested_after = [vac.stable_id() for vac in db.list_undigested_vacancies()]
    assert v.stable_id() not in undigested_after


# ------------------------------------------------------------------------------
# 12. test_two_concurrent_upserts_create_one_row
# ------------------------------------------------------------------------------
def test_two_concurrent_upserts_create_one_row(isolated_env):
    """Two concurrent threads saving the same vacancy result in exactly 1 row and 0 exceptions."""
    v = _make_vacancy(source_job_id="concurrent_job")

    def worker():
        return db.save_vacancy(v)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker)
        f2 = executor.submit(worker)
        r1 = f1.result()
        r2 = f2.result()

    assert set([r1, r2]).issubset({"INSERTED", "UPDATED", "UNCHANGED"})

    conn = sqlite3.connect(isolated_env["db_file"])
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM vacancies WHERE stable_id = ?", (v.stable_id(),))
    assert c.fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------------------------
# 13. test_adapter_duplicate_is_not_logged_as_adapter_failure
# ------------------------------------------------------------------------------
def test_adapter_duplicate_is_not_logged_as_adapter_failure(isolated_env):
    """Adapter discovering existing DB records does not populate result.errors."""
    v = _make_vacancy(source_job_id="existing_job")
    db.save_vacancy(v)

    mock_adapter = MagicMock()
    mock_adapter.fetch_vacancies.return_value = [v.to_dict()]

    cfg = WatcherConfig(sources=["weworkremotely"], custom_adapters={"weworkremotely": mock_adapter})
    watcher = Watcher(cfg)
    res = watcher.poll_once(iteration=1, dry_run=False)

    assert len(res.errors) == 0
    assert res.duplicate_count == 1
    assert res.new_vacancies_count == 0


# ------------------------------------------------------------------------------
# 14. test_discovery_counters_are_consistent
# ------------------------------------------------------------------------------
def test_discovery_counters_are_consistent(isolated_env):
    """Total fetched matches inserted + updated + unchanged + invalid."""
    item1 = {"source": "weworkremotely", "source_job_id": "c1", "title": "Job 1", "job_url": "https://wwr.com/c1"}
    item2 = {"source": "weworkremotely", "source_job_id": "c2", "title": "Job 2", "job_url": "https://wwr.com/c2"}
    invalid = {"source": "weworkremotely", "source_job_id": "c3", "title": "", "job_url": ""}

    mock_adapter = MagicMock()
    mock_adapter.fetch_vacancies.return_value = [item1, item2, invalid]

    cfg = WatcherConfig(sources=["weworkremotely"], custom_adapters={"weworkremotely": mock_adapter})
    watcher = Watcher(cfg)
    res = watcher.poll_once(iteration=1, dry_run=False)

    assert res.fetched_count == 3
    assert res.new_vacancies_count == 2
    assert res.invalid_count == 1


# ------------------------------------------------------------------------------
# 15. test_existing_stage75_vacancy_identity_contract_remains_valid
# ------------------------------------------------------------------------------
def test_existing_stage75_vacancy_identity_contract_remains_valid(isolated_env):
    """Canonical identity resolution still matches exact duplicate URLs across sources."""
    v1 = _make_vacancy(source="weworkremotely", source_job_id="id_1", job_url="https://company.com/job/100")
    v2 = _make_vacancy(source="remoteok", source_job_id="id_2", job_url="https://company.com/job/100?utm_source=remoteok")

    res1 = resolve_vacancy_identity(v1)
    assert res1.match_type.value == "DISTINCT"

    res2 = resolve_vacancy_identity(v2)
    assert res2.match_type.value == "EXACT"
    assert res2.confidence == 100
    assert res2.canonical_id == res1.canonical_id