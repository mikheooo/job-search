"""Questionnaire storage domain operations."""
from __future__ import annotations

import json
import logging
from typing import Any

from ai_assistant.storage.core import db_session, init_db

logger = logging.getLogger("storage.questionnaires")


def get_hh_questionnaire(questionnaire_id: str) -> dict[str, Any] | None:
    """Retrieve an HH questionnaire by ID."""
    init_db()
    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM hh_questionnaires WHERE questionnaire_id = ?", (questionnaire_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def list_hh_questionnaires(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """List stored HH questionnaires."""
    init_db()
    query = "SELECT * FROM hh_questionnaires"
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
