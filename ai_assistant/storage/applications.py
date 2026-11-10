"""Application tracking and submission storage domain operations."""
from __future__ import annotations

import json
import logging
from typing import Any

from ai_assistant.storage.core import db_session, init_db

logger = logging.getLogger("storage.applications")


def get_hh_application(application_id: str) -> dict[str, Any] | None:
    """Retrieve an HH application by its application_id."""
    init_db()
    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM hh_applications WHERE application_id = ?", (application_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def get_hh_application_by_vacancy(vacancy_id: str) -> dict[str, Any] | None:
    """Retrieve an HH application by vacancy_id."""
    init_db()
    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM hh_applications WHERE vacancy_id = ? ORDER BY created_at DESC LIMIT 1",
            (vacancy_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def list_hh_applications(limit: int = 50, state: str | None = None) -> list[dict[str, Any]]:
    """List stored HH applications."""
    init_db()
    query = "SELECT * FROM hh_applications"
    params: list[Any] = []
    if state:
        query += " WHERE state = ?"
        params.append(state)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
