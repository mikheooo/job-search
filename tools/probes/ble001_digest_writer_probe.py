#!/usr/bin/env python
"""Probe: what does the digest-attempt WRITER report when the write fails?

BLE001 triage, finding #51. ``db.record_digest_attempt`` documents its return
value in the docstring:

    Returns batch_key if this process successfully claimed exclusive attempt
    permission.
    Returns None if the attempt is already ATTEMPTING, DELIVERED, or AMBIGUOUS
    (concurrency protection).

The first-time branch narrowed that contract in the comment on its handler:

    try:
        cur.execute('''
        INSERT INTO telegram_delivery_records
        (delivery_key, notification_type, chat_id, delivered_at, status, payload)
        VALUES (?, 'digest_batch', ?, ?, 'ATTEMPTING', ?)
        ''', (batch_key, str(chat_id), now_iso, batch_payload))
    except Exception:
        # Unique constraint race lost
        conn.close()
        return None

``except Exception`` is wider than the comment: a UNIQUE violation on
``delivery_key`` is one way to get here, and a locked or readonly database is
another. Both left through the same door, and the door is documented as
"somebody else already has this batch".

The caller then read that None as a diagnosis, not as an absence
(``integrations/hermes/job_search_fetcher.py:221-224`` - quoted, not executed
by this probe, which exercises ``db`` directly):

    batch_key = record_digest_attempt(vacancy_ids, chat_id=TARGET_CHANNEL)
    if not batch_key:
        print("[STAGE 81] Concurrency lock: Batch is already being attempted or
               completed by another worker. Exiting safely.")
        return

MEASURED BEFORE THE FIX (2026-09-19, by the first version of this probe - the
table is the pre-fix evidence and is NOT what this file still reproduces):

    shape      returned    row left  caller branch
    healthy    batch_key   True      proceeds to send
    race       None        False     concurrency lock: 'another worker has it'
    locked     None        False     concurrency lock: 'another worker has it'
    readonly   None        False     concurrency lock: 'another worker has it'

      shapes whose INSERT failed for a NON-race reason : 2
      of those, the caller prints 'another worker has it': 2 of 2
      of those, a row is left behind (so nothing is stuck): 0 of 2
      shapes where the writer RAISED instead of returning a value : 0 of 4

So a database that could not be written was reported to the operator as
"another worker already has this batch", and the operator read "Exiting safely".
Nothing was sent (the direction was already closed) and no row was left behind
(so nothing was stuck) - the harm is the diagnosis, and it hides a database that
cannot be written. A momentary lock is the case this function exists for; a
readonly file, a full disk or a drifted schema is not momentary, and there every
cycle would print the same reassuring line while the digest never went out.

The fix narrows the handler to ``sqlite3.IntegrityError``. That is the race and
nothing else: ``delivery_key`` is declared ``NOT NULL UNIQUE`` and every other
column of this INSERT is supplied, so a UNIQUE violation is the only
IntegrityError the statement can raise. Everything else now propagates, which is
also what the vacancy-level INSERTs twelve lines below have always done.

WHAT THIS PROBE DOES NOT SHOW. Reachability of a persistently broken database -
the shapes here are injected at the cursor, not produced by a real readonly file
or a real lock held by another process.

Usage:  ./.venv/Scripts/python.exe tools/ble001_digest_writer_probe.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ai_assistant import config, db

CHAT = "-1004399255305"
# One vacancy id per shape, so a shape cannot inherit another's row.
IDS = {
    "healthy": ["hh:probe51_healthy"],
    "race": ["hh:probe51_race"],
    "locked": ["hh:probe51_locked"],
    "readonly": ["hh:probe51_readonly"],
}


class _FailingCursor:
    """A real cursor that refuses the batch INSERT with a chosen error."""

    def __init__(self, real: sqlite3.Cursor, error: BaseException | None):
        self._real = real
        self._error = error

    def execute(self, sql, params=()):
        head = sql.lstrip().upper()
        if (
            self._error is not None
            and head.startswith("INSERT")
            and "telegram_delivery_records" in sql
        ):
            raise self._error
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FailingConn:
    """Mimics ``db.get_connection()``, but the batch INSERT blows up."""

    def __init__(self, real: sqlite3.Connection, error: BaseException | None):
        self._real = real
        self._error = error

    def cursor(self):
        return _FailingCursor(self._real.cursor(), self._error)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _row_exists(vids: list[str]) -> bool:
    key = db.compute_digest_batch_key(vids)
    conn = sqlite3.connect(config.DB_FILE)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM telegram_delivery_records WHERE delivery_key = ?",
            (key,),
        )
        return bool(cur.fetchone()[0])
    finally:
        conn.close()


def run_shape(label: str, error: BaseException | None) -> tuple:
    """Call the writer with the batch INSERT forced to fail (or not)."""
    vids = IDS[label]
    original = db.get_connection

    def fake_get_connection(error=error):
        # A fresh connection per call, exactly as the real get_connection does;
        # init_db() closes the one it borrows, so a shared connection would be
        # dead by the time the writer asks for its own.
        return _FailingConn(sqlite3.connect(config.DB_FILE), error)

    db.get_connection = fake_get_connection
    try:
        try:
            result = db.record_digest_attempt(vids, chat_id=CHAT)
            raised = None
        except Exception as exc:  # noqa: BLE001 - the shape IS the measurement
            result, raised = None, exc
    finally:
        db.get_connection = original

    row = _row_exists(vids)
    if raised is not None:
        # The caller's `batch_key = record_digest_attempt(...)` never completes,
        # so `if not batch_key:` is never reached - there is no branch to print,
        # only the traceback naming the real reason.
        returned = "raised"
        branch = "traceback - never reaches the branch"
    else:
        returned = "batch_key" if result else "None"
        branch = (
            "concurrency lock: 'another worker has it, exiting safely'"
            if not result else "proceeds to send"
        )
    return label, returned, row, branch, error, raised


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ble001_writer51_"))
    config.DB_FILE = str(tmp / "probe51.db")
    db.init_db()

    shapes = [
        ("healthy", None),
        ("race", sqlite3.IntegrityError(
            "UNIQUE constraint failed: telegram_delivery_records.delivery_key")),
        ("locked", sqlite3.OperationalError("database is locked")),
        ("readonly", sqlite3.OperationalError("attempt to write a readonly database")),
    ]

    print(f"db: {config.DB_FILE}")
    print(f"batch INSERT forced to fail per shape; {len(shapes)} shapes\n")
    header = f"{'shape':10s} {'returned':11s} {'row left':9s} {'caller branch':42s} injected error"
    print(header)
    print("-" * len(header))

    rows = []
    for label, error in shapes:
        label_, returned, row, branch, injected, raised = run_shape(label, error)
        rows.append((label_, returned, row, branch, injected, raised))
        shown = f"{type(injected).__name__}: {injected}" if injected else "(none)"
        print(f"{label_:10s} {returned:11s} {row!s:9s} {branch:42s} {shown}")

    print()
    # The documented race is a UNIQUE violation; anything else reaching the same
    # handler is a failure the docstring does not mention.
    wrong = [r for r in rows if r[4] is not None and not isinstance(r[4], sqlite3.IntegrityError)]
    lied = [r for r in wrong if "another worker" in r[3]]
    print("reading:")
    print(f"  shapes whose INSERT failed for a NON-race reason : {len(wrong)}")
    print(f"  of those, the caller prints 'another worker has it': {len(lied)} of {len(wrong)}")
    print(f"  of those, a row is left behind (so nothing is stuck): "
          f"{sum(1 for r in wrong if r[2])} of {len(wrong)}")
    print(f"  shapes where the writer RAISED instead of returning a value : "
          f"{sum(1 for r in rows if r[5] is not None)} of {len(rows)}")
    print()
    if lied:
        print("RESULT: a write failure is still reported as 'another worker has it'.")
        return 1
    print("RESULT: fail-closed confirmed - only the documented race returns None;")
    print("        a database that cannot be written propagates with its own reason.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
