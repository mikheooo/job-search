"""Core SQLite database connection and session management."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from ai_assistant import config
from ai_assistant.db_schema import apply_schema, schema_fingerprint

logger = logging.getLogger("storage.core")

_DRY_RUN = False


def set_dry_run(enabled: bool) -> None:
    """Toggle global dry-run mode."""
    global _DRY_RUN
    _DRY_RUN = bool(enabled)


def is_dry_run() -> bool:
    """Return whether dry-run mode is currently active."""
    return _DRY_RUN


def get_connection(db_file: str | None = None) -> sqlite3.Connection:
    """Open SQLite with writer-friendly concurrency setup (WAL + busy_timeout=30s)."""
    target = db_file or config.DB_FILE
    conn = sqlite3.connect(target, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


@contextmanager
def db_session(*, commit: bool = True, db_file: str | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager for SQLite database operations with auto-commit/auto-rollback."""
    conn = get_connection(db_file=db_file)
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def db_cursor(*, commit: bool = True, db_file: str | None = None) -> Iterator[sqlite3.Cursor]:
    """Context manager yielding a cursor from a managed db_session."""
    with db_session(commit=commit, db_file=db_file) as conn:
        yield conn.cursor()


def init_db(db_file: str | None = None) -> None:
    """Initialize database schema safely using managed connection."""
    with db_session(commit=True, db_file=db_file) as conn:
        apply_schema(conn)
