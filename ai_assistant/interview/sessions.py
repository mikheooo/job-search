"""Interview session storage (Phase 5A §8).

Additive-only schema (CREATE TABLE IF NOT EXISTS, no destructive ops).
``ensure_tables(conn)`` works on any sqlite connection (tests use scratch).
Production wiring calls it on the hub path, same convention as init_db().
"""
from __future__ import annotations

import datetime
import json
from typing import Any


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ensure_tables(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interview_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_stable_id TEXT,
            role_title TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interview_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            question TEXT NOT NULL,
            answer_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_interview_turns_session ON interview_turns(session_id)")
    conn.commit()


def create_session(conn, vacancy_stable_id: str | None = None,
                   role_title: str | None = None) -> int:
    ensure_tables(conn)
    cur = conn.execute(
        "INSERT INTO interview_sessions (vacancy_stable_id, role_title, created_at, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (vacancy_stable_id, role_title, _now(), _now()))
    conn.commit()
    return int(cur.lastrowid)


def save_turn(conn, session_id: int, kind: str, question: str,
              answer: dict[str, Any]) -> int:
    ensure_tables(conn)
    cur = conn.execute(
        "INSERT INTO interview_turns (session_id, kind, question, answer_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (session_id, kind, question,
         json.dumps(answer, ensure_ascii=False), _now()))
    conn.execute("UPDATE interview_sessions SET updated_at = ? WHERE id = ?",
                 (_now(), session_id))
    conn.commit()
    return int(cur.lastrowid)


def list_sessions(conn, limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT id, vacancy_stable_id, role_title, created_at, updated_at"
        " FROM interview_sessions ORDER BY updated_at DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) FROM interview_turns WHERE session_id = ?",
                         (r[0],)).fetchone()[0]
        out.append({"id": r[0], "vacancy_stable_id": r[1], "role_title": r[2],
                    "created_at": r[3], "updated_at": r[4], "turns": n})
    return out


def get_session(conn, session_id: int) -> dict[str, Any] | None:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT id, vacancy_stable_id, role_title, created_at, updated_at"
        " FROM interview_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    turns = conn.execute(
        "SELECT id, kind, question, answer_json, created_at FROM interview_turns"
        " WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    return {"id": row[0], "vacancy_stable_id": row[1], "role_title": row[2],
            "created_at": row[3], "updated_at": row[4],
            "turns": [{"id": t[0], "kind": t[1], "question": t[2],
                       "answer": json.loads(t[3]), "created_at": t[4]} for t in turns]}


def delete_session(conn, session_id: int) -> bool:
    ensure_tables(conn)
    conn.execute("DELETE FROM interview_turns WHERE session_id = ?", (session_id,))
    cur = conn.execute("DELETE FROM interview_sessions WHERE id = ?", (session_id,))
    conn.commit()
    return cur.rowcount > 0


__all__ = ["create_session", "delete_session", "ensure_tables",
           "get_session", "list_sessions", "save_turn"]
