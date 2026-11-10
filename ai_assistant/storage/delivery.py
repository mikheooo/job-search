"""Production delivery and autonomous metrics storage domain operations."""
from __future__ import annotations

import json
import logging
from typing import Any

from ai_assistant.storage.core import db_session, init_db

logger = logging.getLogger("storage.delivery")


def list_autonomous_cycle_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Retrieve recent autonomous agent cycle executions."""
    init_db()
    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM autonomous_cycle_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def list_interview_events(limit: int = 20) -> list[dict[str, Any]]:
    """Retrieve detected employer interview events."""
    init_db()
    with db_session(commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM interview_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
