"""Tests for Horizon 3.3: Single Source of Truth Lifecycle Store.

Verifies that application_tracking is the single owner of the vacancy lifecycle,
automatically keeping vacancies.state and hh_applications.state in lockstep sync.
"""
from __future__ import annotations

import tempfile
import shutil
from pathlib import Path

import pytest

import ai_assistant.config as config
from ai_assistant import db
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
    transition_application,
    sync_all_vacancy_states_from_tracking,
)
from ai_assistant.schema import Vacancy


def _create_vacancy(sid: str = "v1", title: str = "Backend Engineer") -> Vacancy:
    return Vacancy(
        source="test",
        source_job_id=sid,
        title=title,
        company="TechCorp",
        description="Python backend role",
        job_url=f"https://example.com/{sid}",
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=100000,
        salary_max=150000,
        salary_currency="USD",
        employment_type="Full Time",
    )


def _get_vacancy_state(sid: str) -> str | None:
    with db.db_session(commit=False) as conn:
        row = conn.execute("SELECT state FROM vacancies WHERE stable_id = ?", (sid,)).fetchone()
        return row[0] if row else None


@pytest.fixture
def isolated_db():
    tmp = tempfile.mkdtemp()
    orig_db = config.DB_FILE
    db_file = str(Path(tmp) / "lifecycle_test.db")
    config.DB_FILE = db_file
    db.init_db()
    try:
        yield db_file
    finally:
        config.DB_FILE = orig_db
        shutil.rmtree(tmp, ignore_errors=True)


def test_set_status_syncs_vacancies_state(isolated_db):
    """Setting tracking status immediately reflects in vacancies.state."""
    vac = _create_vacancy(sid="sync1")
    db.save_vacancy(vac)
    sid = vac.stable_id()

    # Initial vacancy state is NEW
    assert _get_vacancy_state(sid) == "NEW"

    # Set to DISCOVERED -> vacancies.state becomes DISCOVERED
    set_application_status(sid, ApplicationStatus.DISCOVERED, company=vac.company, title=vac.title)
    assert _get_vacancy_state(sid) == "DISCOVERED"

    # Set to READY_TO_APPLY -> vacancies.state becomes READY_TO_APPLY
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    assert _get_vacancy_state(sid) == "READY_TO_APPLY"

    # Set to APPLIED -> vacancies.state becomes APPLIED
    set_application_status(sid, ApplicationStatus.APPLIED)
    assert _get_vacancy_state(sid) == "APPLIED"

    # Set to REJECTED -> vacancies.state becomes REJECTED
    set_application_status(sid, ApplicationStatus.REJECTED)
    assert _get_vacancy_state(sid) == "REJECTED"


def test_transition_syncs_both_vacancies_and_hh_applications(isolated_db):
    """Transitioning application updates both vacancies and hh_applications."""
    vac = _create_vacancy(sid="hh_sync1")
    db.save_vacancy(vac)
    sid = vac.stable_id()

    # Create associated HH application
    with db.db_session(commit=True) as conn:
        conn.execute(
            """INSERT INTO hh_applications (
                application_id, vacancy_stable_id, title, employer, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("app_hh_1", sid, vac.title, vac.company, "DISCOVERED"),
        )

    # Initialize tracking to DISCOVERED
    set_application_status(sid, ApplicationStatus.DISCOVERED, company=vac.company, title=vac.title)
    assert _get_vacancy_state(sid) == "DISCOVERED"

    # Transition to ANALYZED
    transition_application(sid, ApplicationStatus.ANALYZED)
    assert _get_vacancy_state(sid) == "ANALYZED"

    # Transition to READY_TO_APPLY -> hh_applications becomes READY_TO_SUBMIT
    transition_application(sid, ApplicationStatus.READY_TO_APPLY)
    assert _get_vacancy_state(sid) == "READY_TO_APPLY"
    with db.db_session(commit=False) as conn:
        hh_app = conn.execute("SELECT state FROM hh_applications WHERE vacancy_stable_id = ?", (sid,)).fetchone()
        assert hh_app[0] == "READY_TO_SUBMIT"

    # Transition to SUBMITTED -> hh_applications becomes SUBMITTED, vacancies becomes APPLIED
    transition_application(sid, ApplicationStatus.SUBMITTED)
    assert _get_vacancy_state(sid) == "APPLIED"
    with db.db_session(commit=False) as conn:
        hh_app = conn.execute("SELECT state FROM hh_applications WHERE vacancy_stable_id = ?", (sid,)).fetchone()
        assert hh_app[0] == "SUBMITTED"

    # Transition to VERIFIED -> still APPLIED / SUBMITTED
    transition_application(sid, ApplicationStatus.VERIFIED)
    assert _get_vacancy_state(sid) == "APPLIED"

    # Transition to APPLIED -> still APPLIED
    transition_application(sid, ApplicationStatus.APPLIED)
    assert _get_vacancy_state(sid) == "APPLIED"

    # Transition to REJECTED -> both become REJECTED
    transition_application(sid, ApplicationStatus.REJECTED)
    assert _get_vacancy_state(sid) == "REJECTED"
    with db.db_session(commit=False) as conn:
        hh_app = conn.execute("SELECT state FROM hh_applications WHERE vacancy_stable_id = ?", (sid,)).fetchone()
        assert hh_app[0] == "REJECTED"


def test_batch_sync_resolves_divergent_states(isolated_db):
    """sync_all_vacancy_states_from_tracking resolves historic drift between tables."""
    vac1 = _create_vacancy(sid="drift1")
    vac2 = _create_vacancy(sid="drift2")
    db.save_vacancy(vac1)
    db.save_vacancy(vac2)
    s1, s2 = vac1.stable_id(), vac2.stable_id()

    set_application_status(s1, ApplicationStatus.DISCOVERED)
    set_application_status(s2, ApplicationStatus.APPLIED)

    # Intentionally corrupt vacancies.state to simulate legacy drift
    with db.db_session(commit=True) as conn:
        conn.execute("UPDATE vacancies SET state = 'NEW' WHERE stable_id IN (?, ?)", (s1, s2))

    assert _get_vacancy_state(s1) == "NEW"
    assert _get_vacancy_state(s2) == "NEW"

    # Run batch sync
    synced_count = sync_all_vacancy_states_from_tracking()
    assert synced_count >= 2

    # Verify drift resolved
    assert _get_vacancy_state(s1) == "DISCOVERED"
    assert _get_vacancy_state(s2) == "APPLIED"
