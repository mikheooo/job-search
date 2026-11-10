"""Regression tests for the SQLite temp-file failure blocking the digest."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_assistant import db


def _seed_vacancies(count: int) -> None:
    db.init_db()
    conn = db.get_connection()
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        seen = (base + timedelta(minutes=index)).isoformat()
        rows.append(
            (
                f"stable-{index}",
                "hh",
                f"hh-{index}",
                f"Engineer {index}",
                f"Company {index}",
                "Description",
                "Remote",
                "",
                "",
                None,
                None,
                None,
                "full-time",
                f"https://hh.ru/vacancy/{index}",
                None,
                seen,
                seen,
                seen,
                "NEW",
                "{}",
            )
        )
    conn.executemany(
        """
        INSERT INTO vacancies (
            stable_id, source, source_job_id, title, company, description,
            location, country_restrictions, timezone_restrictions,
            salary_min, salary_max, salary_currency, employment_type,
            job_url, application_url, published_at, first_seen_at, last_seen_at,
            state, raw_data
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _temp_store_directory(conn) -> str:
    row = conn.execute("PRAGMA temp_store_directory").fetchone()
    return row[0] if row else ""


def test_configure_sqlite_temp_storage_uses_connection_memory():
    conn = db.get_connection()
    try:
        before = _temp_store_directory(conn)
        db._configure_sqlite_temp_storage(conn)
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert _temp_store_directory(conn) == before
    finally:
        conn.close()


def test_large_backlog_query_survives_unusable_system_temp(monkeypatch, tmp_path):
    _seed_vacancies(1200)

    missing = tmp_path / "missing-system-temp"
    for name in ("TMP", "TEMP", "SQLITE_TMPDIR"):
        monkeypatch.setenv(name, str(missing))

    rows = db.list_undigested_vacancies(limit=1200)

    assert len(rows) == 1200


def test_large_query_leaves_process_temp_directory_unchanged():
    _seed_vacancies(50)
    conn = db.get_connection()
    try:
        before = _temp_store_directory(conn)
    finally:
        conn.close()

    db.list_undigested_vacancies(limit=50)

    conn = db.get_connection()
    try:
        after = _temp_store_directory(conn)
    finally:
        conn.close()
    assert after == before
