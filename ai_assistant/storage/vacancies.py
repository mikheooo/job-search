"""Vacancy storage domain operations."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ai_assistant.schema import Vacancy
from ai_assistant.storage.core import db_session, init_db

logger = logging.getLogger("storage.vacancies")


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def get_vacancy_by_id(stable_id: str) -> dict[str, Any] | None:
    """Fetch raw vacancy record by its stable ID."""
    init_db()
    with db_session(commit=False) as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM vacancies WHERE stable_id = ?", (stable_id,))
        row = cursor.fetchone()
        if not row:
            return None
        col_names = [d[0] for d in cursor.description]
        return dict(zip(col_names, row))


def list_vacancies(
    limit: int = 50,
    offset: int = 0,
    state: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """List vacancies with optional filtering and pagination."""
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if source:
        clauses.append("source = ?")
        params.append(source)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT * FROM vacancies {where} ORDER BY first_seen_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def count_vacancies_by_status() -> dict[str, int]:
    """Return counts of vacancies grouped by status."""
    init_db()
    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT state, COUNT(*) FROM vacancies GROUP BY state")
        return dict(cursor.fetchall())


def update_vacancy_state(stable_id: str, new_state: str) -> bool:
    """Update state of a vacancy."""
    init_db()
    with db_session(commit=True) as conn:
        cur = conn.execute("UPDATE vacancies SET state = ? WHERE stable_id = ?", (new_state, stable_id))
        return cur.rowcount > 0
