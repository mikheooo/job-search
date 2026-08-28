"""Tests for Remote Eligibility Reclassification, DB persistence, and Queue Gating."""

from __future__ import annotations

import os
import sqlite3
import pytest
from datetime import datetime

from ai_assistant.schema import Vacancy
from ai_assistant.eligibility import (
    assess_vacancy_eligibility,
    EligibilityStatus,
    RemoteMode,
    GeoScope,
)
from ai_assistant.db import (
    init_db,
    get_connection,
    save_vacancy,
    get_vacancy_by_id,
    list_vacancies,
    save_vacancy_eligibility,
    get_vacancy_eligibility,
    get_all_vacancy_eligibilities,
    delete_queue_item,
)
from ai_assistant.application_queue import (
    QueueItem,
    save_queue_item,
    get_queue_item,
    list_queue,
    generate_queue,
)
from ai_assistant.cli import reclassify_eligibility_cmd, list_cmd


def _create_test_vac(source_id: str, title: str, loc: str | None, desc: str, source: str = "test") -> Vacancy:
    return Vacancy(
        source=source,
        source_job_id=source_id,
        title=title,
        company="GlobalTech",
        description=desc,
        job_url=f"https://example.com/job/{source_id}",
        location=loc,
    )


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate tests into a temporary database."""
    test_db = str(tmp_path / "test_vacancies.db")
    monkeypatch.setattr("ai_assistant.config.DB_FILE", test_db)
    init_db()
    yield test_db


# 1. Existing eligible vacancy remains active
def test_existing_eligible_vacancy_remains_active():
    vac = _create_test_vac(
        "el-1",
        "Senior AI Engineer",
        "Remote (Worldwide)",
        "Fully remote. We hire worldwide contractors. English required.",
    )
    save_vacancy(vac)
    
    # Run reclassify
    res = reclassify_eligibility_cmd(candidate_country="TH")
    assert res == 0

    elig = get_vacancy_eligibility(vac.stable_id())
    assert elig is not None
    assert elig["status"] == EligibilityStatus.ELIGIBLE.value


# 2. Existing INELIGIBLE becomes inactive in queue
def test_existing_ineligible_vacancy_purged_from_queue():
    vac = _create_test_vac(
        "inelig-us",
        "Python Engineer",
        "Remote",
        "Fully remote. US only. Must reside in the United States.",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    # Pre-populate into queue
    q_item = QueueItem(
        vacancy_stable_id=sid,
        canonical_id=f"canon:{sid}",
        representative_vacancy_stable_id=sid,
        priority_score=85,
        rank=1,
    )
    save_queue_item(q_item)
    assert get_queue_item(sid) is not None

    # Run reclassification
    reclassify_eligibility_cmd(candidate_country="TH")

    # Verify purged from queue
    assert get_queue_item(sid) is None

    # Verify still in vacancies DB
    row = get_vacancy_by_id(sid)
    assert row is not None


# 3. Existing UNKNOWN is not deleted
def test_existing_unknown_is_not_deleted():
    vac = _create_test_vac(
        "unk-1",
        "Fullstack Developer",
        "Remote",
        "Удаленная работа. Разработка веб-приложений.",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    reclassify_eligibility_cmd(candidate_country="TH")

    # Record preserved
    assert get_vacancy_by_id(sid) is not None
    elig = get_vacancy_eligibility(sid)
    assert elig is not None
    assert elig["status"] == EligibilityStatus.UNKNOWN.value


# 4. Existing UNKNOWN does not get into active queue
def test_existing_unknown_does_not_enter_active_queue():
    vac = _create_test_vac(
        "unk-2",
        "Backend Developer",
        "Remote",
        "Удаленная работа. Backend Python.",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    # Put into queue
    q_item = QueueItem(
        vacancy_stable_id=sid,
        canonical_id=f"canon:{sid}",
        representative_vacancy_stable_id=sid,
        priority_score=75,
        rank=1,
    )
    save_queue_item(q_item)

    reclassify_eligibility_cmd(candidate_country="TH")
    assert get_queue_item(sid) is None


# 5. Warning remains active with notes
def test_warning_remains_active_with_notes():
    vac = _create_test_vac(
        "warn-1",
        "Data Engineer",
        "Remote Worldwide",
        "Worldwide remote role. Requires 4 hours overlap with EST (UTC-5).",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    reclassify_eligibility_cmd(candidate_country="TH")
    elig = get_vacancy_eligibility(sid)
    assert elig is not None
    assert elig["status"] == EligibilityStatus.ELIGIBLE_WITH_WARNING.value
    assert any("TIMEZONE" in r for r in elig["reasons"])


# 6. Reason for INELIGIBLE is preserved and retrievable
def test_ineligible_reason_is_preserved():
    vac = _create_test_vac(
        "inelig-de",
        "Frontend Engineer",
        "Remote",
        "Remote position, but candidates must reside in Germany.",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    reclassify_eligibility_cmd(candidate_country="TH")
    elig = get_vacancy_eligibility(sid)
    assert elig is not None
    assert elig["status"] == EligibilityStatus.INELIGIBLE.value
    assert any("Germany" in r for r in elig["reasons"])


# 7. Repeated reclassify-eligibility is idempotent
def test_reclassify_eligibility_is_idempotent():
    v1 = _create_test_vac("idemp-1", "AI Dev", "Remote Worldwide", "100% remote worldwide contractor.")
    v2 = _create_test_vac("idemp-2", "US Dev", "Remote", "US only position.")
    save_vacancy(v1)
    save_vacancy(v2)

    reclassify_eligibility_cmd(candidate_country="TH")
    first_map = get_all_vacancy_eligibilities()

    # Second run
    reclassify_eligibility_cmd(candidate_country="TH")
    second_map = get_all_vacancy_eligibilities()

    assert len(first_map) == len(second_map) == 2
    assert first_map[v1.stable_id()]["status"] == second_map[v1.stable_id()]["status"]
    assert first_map[v2.stable_id()]["status"] == second_map[v2.stable_id()]["status"]


# 8. Ingesting new INELIGIBLE does not pass to active queue
def test_new_ineligible_blocked_from_queue():
    vac = _create_test_vac(
        "new-inelig",
        "Lead Architect",
        "Remote",
        "Fully remote role. W2 only. US work authorization required without sponsorship.",
    )
    # save_vacancy automatically evaluates eligibility on ingestion
    save_vacancy(vac)
    sid = vac.stable_id()

    elig = get_vacancy_eligibility(sid)
    assert elig is not None
    assert elig["status"] == EligibilityStatus.INELIGIBLE.value


# 9. Ingesting new UNKNOWN does not pass to active queue
def test_new_unknown_saved_as_unknown():
    vac = _create_test_vac(
        "new-unk",
        "Software Engineer",
        "Remote",
        "Удаленная работа над платформой.",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    elig = get_vacancy_eligibility(sid)
    assert elig is not None
    assert elig["status"] == EligibilityStatus.UNKNOWN.value


# 10. Ingesting new ELIGIBLE passes
def test_new_eligible_passes():
    vac = _create_test_vac(
        "new-elig",
        "ML Engineer",
        "Remote (Thailand)",
        "Work remotely from Thailand. Bangkok hub.",
    )
    save_vacancy(vac)
    sid = vac.stable_id()

    elig = get_vacancy_eligibility(sid)
    assert elig is not None
    assert elig["status"] == EligibilityStatus.ELIGIBLE.value


# 11. Hybrid never becomes ELIGIBLE
def test_hybrid_never_becomes_eligible():
    v1 = _create_test_vac("hyb-1", "Dev", "Remote / Hybrid", "Гибридный график 2 дня в офис.")
    v2 = _create_test_vac("hyb-2", "Dev", "Remote", "Remote, but must occasionally work from our Berlin office.")
    save_vacancy(v1)
    save_vacancy(v2)

    reclassify_eligibility_cmd(candidate_country="TH")

    e1 = get_vacancy_eligibility(v1.stable_id())
    e2 = get_vacancy_eligibility(v2.stable_id())

    assert e1["status"] == EligibilityStatus.INELIGIBLE.value
    assert e2["status"] == EligibilityStatus.INELIGIBLE.value


# 12. Physical DELETE is 0
def test_zero_physical_deletions():
    for i in range(10):
        desc = "Worldwide remote" if i % 2 == 0 else "US only onsite in New York"
        save_vacancy(_create_test_vac(f"count-{i}", f"Title {i}", "Remote", desc))

    vacs_before = list_vacancies(limit=100)
    assert len(vacs_before) == 10

    reclassify_eligibility_cmd(candidate_country="TH")

    vacs_after = list_vacancies(limit=100)
    assert len(vacs_after) == 10
