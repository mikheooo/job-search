"""Tests for db_session and db_cursor context managers in ai_assistant.db."""

import sqlite3
import pytest

from ai_assistant import config, db
from ai_assistant.db import db_cursor, db_session


@pytest.fixture
def temp_db(tmp_path):
    orig = config.DB_FILE
    db_file = str(tmp_path / "test_session.db")
    config.DB_FILE = db_file
    db.init_db()
    yield db_file
    config.DB_FILE = orig


def test_db_session_commits_on_clean_exit(temp_db):
    """Normal execution with commit=True writes data to disk."""
    with db_session() as conn:
        conn.execute(
            "INSERT INTO processed_vacancies (id, content_hash, processed_at, status) VALUES (?, ?, ?, ?)",
            ("vac_1", "hash_1", "2026-11-10T12:00:00Z", "PROCESSED"),
        )

    # Verify through a fresh connection
    with db_session(commit=False) as conn:
        row = conn.execute("SELECT content_hash FROM processed_vacancies WHERE id = ?", ("vac_1",)).fetchone()
        assert row is not None
        assert row[0] == "hash_1"


def test_db_session_rolls_back_on_exception(temp_db):
    """An exception inside db_session triggers rollback, leaving DB unmodified."""
    with pytest.raises(ValueError, match="simulated failure"):
        with db_session() as conn:
            conn.execute(
                "INSERT INTO processed_vacancies (id, content_hash, processed_at, status) VALUES (?, ?, ?, ?)",
                ("vac_fail", "hash_fail", "2026-11-10T12:00:00Z", "PROCESSED"),
            )
            raise ValueError("simulated failure")

    with db_session(commit=False) as conn:
        row = conn.execute("SELECT content_hash FROM processed_vacancies WHERE id = ?", ("vac_fail",)).fetchone()
        assert row is None


def test_db_session_always_closes_connection(temp_db):
    """Connection is closed on exit, even when an exception is raised."""
    conn_ref = None
    with pytest.raises(RuntimeError):
        with db_session() as conn:
            conn_ref = conn
            raise RuntimeError("boom")

    assert conn_ref is not None
    # Operating on a closed sqlite3 connection raises ProgrammingError
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        conn_ref.execute("SELECT 1")


def test_db_cursor_yields_cursor_and_executes(temp_db):
    """db_cursor provides a cursor and commits upon exit."""
    with db_cursor() as cursor:
        cursor.execute(
            "INSERT INTO processed_vacancies (id, content_hash, processed_at, status) VALUES (?, ?, ?, ?)",
            ("vac_cursor", "hash_cursor", "2026-11-10T12:00:00Z", "PROCESSED"),
        )

    with db_cursor(commit=False) as cursor:
        cursor.execute("SELECT content_hash FROM processed_vacancies WHERE id = ?", ("vac_cursor",))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "hash_cursor"


def test_db_session_commit_false_does_not_commit(temp_db):
    """When commit=False, uncommitted statements are not written."""
    with db_session(commit=False) as conn:
        conn.execute(
            "INSERT INTO processed_vacancies (id, content_hash, processed_at, status) VALUES (?, ?, ?, ?)",
            ("vac_no_commit", "hash_no_commit", "2026-11-10T12:00:00Z", "PROCESSED"),
        )

    with db_session(commit=False) as conn:
        row = conn.execute("SELECT content_hash FROM processed_vacancies WHERE id = ?", ("vac_no_commit",)).fetchone()
        assert row is None


def test_no_database_lock_after_exception(temp_db):
    """After an exception, the lock is released so subsequent writers succeed immediately."""
    with pytest.raises(ZeroDivisionError):
        with db_session() as conn:
            conn.execute(
                "INSERT INTO processed_vacancies (id, content_hash, processed_at, status) VALUES (?, ?, ?, ?)",
                ("vac_err", "hash_err", "2026-11-10T12:00:00Z", "PROCESSED"),
            )
            _ = 1 / 0

    # Next write should proceed without timeout or lock error
    with db_session() as conn:
        conn.execute(
            "INSERT INTO processed_vacancies (id, content_hash, processed_at, status) VALUES (?, ?, ?, ?)",
            ("vac_ok", "hash_ok", "2026-11-10T12:00:00Z", "PROCESSED"),
        )

    with db_session(commit=False) as conn:
        assert conn.execute("SELECT id FROM processed_vacancies WHERE id = ?", ("vac_ok",)).fetchone() is not None
