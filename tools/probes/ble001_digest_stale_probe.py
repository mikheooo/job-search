#!/usr/bin/env python
"""Probe: what does an unreadable digest timestamp do to the stale evaluation?

BLE001 triage, finding #50. In ``db.list_digest_attempts`` the age of a batch is
computed from ``delivered_at``, falling back to a payload ``attempted_at``:

    attempted_at_str = r[3] or payload.get("attempted_at")
    try:
        att_dt = datetime.datetime.fromisoformat(attempted_at_str.replace("Z", "+00:00"))
        ...
        age_seconds = max(0, int((now_dt - att_dt).total_seconds()))
    except Exception:
        pass

    is_stale = (persisted_status == "ATTEMPTING" and age_seconds >= stale_threshold_seconds)

MEASURED BEFORE THE FIX (2026-09-19, by the first version of this probe - the
table is the pre-fix evidence and is NOT something this file still reproduces,
because the code below it has since been fixed). ``age_seconds`` started at 0 and
stayed 0 when the timestamp could not be read, and that zero fed ``is_stale``:

    shape                age_s  age_min  stale effective    health     stale_cnt  crit
    readable col 3h      10800    180.0   True STALE        UNHEALTHY          1     1
    empty col+payload    10800    180.0   True STALE        UNHEALTHY          1     1
    empty col, no key        0      0.0  False ATTEMPTING   DEGRADED           0     0
    malformed col            0      0.0  False ATTEMPTING   DEGRADED           0     0

So an ATTEMPTING batch whose timestamp could not be read was reported as fresh:
``cli.py`` printed "Age: 0.0 min" with no [STALE] marker, and
``get_production_health`` counted it as ``attempting`` rather than ``stale``.
Rule A raises UNHEALTHY only on ``stale_cnt > 0`` (db.py:2381), and
``attempting_cnt`` is written to metrics but read by no rule at all - so the one
row an operator most needs to see raised nothing.

The fix sets ``age_seconds = None``, keeps the exception text in ``age_error``,
and treats an un-ageable ATTEMPTING row as stale (fail-closed). The zero is gone
and the critical rule fires.

WHAT THIS PROBE DOES NOT SHOW. Reachability. Measured 2026-09-19:
  * the writer (``record_digest_attempt``) sets ``delivered_at`` and a payload
    ``attempted_at`` together with ``status='ATTEMPTING'``, both from one
    ``now_iso`` = ``datetime.now(timezone.utc).isoformat()``;
  * every other INSERT/UPDATE of this table in ``db.py`` passes ``now_iso`` for
    ``delivered_at`` as well - no call site writes a literal or an empty string;
  * the column is declared ``delivered_at TEXT NOT NULL`` (db_schema.py:469), so
    the NULL shape is not merely unwritten, it is forbidden by the schema;
  * the only other writer of this table's status is
    ``update_telegram_delivery_status``, whose single caller sets
    ``CLEANED_TEST_RECORD``, never ATTEMPTING;
  * production holds 13 digest_batch rows - 6 DELIVERED, 7 FAILED, no ATTEMPTING -
    and none with an empty ``delivered_at``.
So the compound shape (ATTEMPTING AND an unreadable timestamp) needs a corrupted
or legacy row. This is a hardening fix, not a repair of an observed production
failure.

Nothing here touches production: DB_FILE is redirected to a temp directory
*before* ai_assistant.config is imported, and the redirect is asserted.

Usage:  ./.venv/Scripts/python.exe tools/ble001_digest_stale_probe.py
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ble001_digest_stale_")
os.environ["DB_FILE"] = os.path.join(_TMP, "probe.db")
os.environ["VACANCIES_FILE"] = os.path.join(_TMP, "vacancies.json")
os.environ["LOGS_DIR"] = os.path.join(_TMP, "logs")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_assistant import config, db

NOW = datetime.datetime(2026, 9, 19, 12, 0, 0, tzinfo=datetime.timezone.utc)
THREE_HOURS_AGO = (NOW - datetime.timedelta(hours=3)).isoformat()

# (label, delivered_at column, payload attempted_at). ``None`` for the payload
# means the key is absent on purpose - that is the second half of the fallback.
SHAPES: list[tuple[str, str, str | None]] = [
    ("readable col 3h", THREE_HOURS_AGO, THREE_HOURS_AGO),
    ("empty col+payload", "", THREE_HOURS_AGO),
    ("empty col, no key", "", None),
    ("malformed col", "{not a date", None),
]

# The NULL shape is deliberately not measured: db_schema.py:469 declares
# ``delivered_at TEXT NOT NULL``. Attempting to insert it raises IntegrityError,
# which the first version of this probe did.
NULL_SHAPE_NOTE = "delivered_at is NOT NULL in db_schema.py - the shape cannot exist"


def _reset_rows() -> None:
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM telegram_delivery_records")
        conn.commit()
    finally:
        conn.close()


def _insert_attempting(
    index: int, delivered_at: str, payload_attempted_at: str | None
) -> None:
    payload: dict = {"vacancies": ["hh:1"], "chat_id": "-100"}
    if payload_attempted_at is not None:
        payload["attempted_at"] = payload_attempted_at
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, 'digest_batch', ?, ?, 'ATTEMPTING', ?)
            """,
            (f"digest_batch:probe-{index}", "-100", delivered_at, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


def _measure_one(index: int, delivered_at: str, payload_attempted_at: str | None) -> dict:
    _reset_rows()
    _insert_attempting(index, delivered_at, payload_attempted_at)

    attempts = db.list_digest_attempts(limit=50, now_dt=NOW)
    att = attempts[0] if attempts else {}
    health = db.get_production_health(now_dt=NOW, storage_dir=_TMP)
    alerts = health.get("alerts", [])
    criticals = [a["message"] for a in alerts if a.get("severity") == "CRITICAL"]
    return {
        "age_seconds": att.get("age_seconds"),
        "age_minutes": att.get("age_minutes"),
        "age_unknown": att.get("age_unknown"),
        "age_error": att.get("age_error"),
        "stale": att.get("stale"),
        "effective_status": att.get("effective_status"),
        "requires_reconciliation": att.get("requires_reconciliation"),
        "health": health.get("health"),
        "stale_count": health["metrics"].get("stale_count"),
        "attempting_count": health["metrics"].get("attempting_count"),
        "critical_alerts": len(criticals),
        # Every alert, so the health word is attributed to the rule that set it
        # rather than assumed to come from the row under test.
        "all_alerts": [f"{a.get('severity')}: {a.get('message')}" for a in alerts],
        "hermes": (health.get("hermes_integration") or {}).get("status"),
    }


def _null_shape_is_forbidden() -> bool:
    """Confirm by measurement that the NULL shape cannot be inserted."""
    _reset_rows()
    try:
        _insert_attempting(99, None, None)  # type: ignore[arg-type]
    except sqlite3.IntegrityError as exc:
        return "NOT NULL" in str(exc)
    finally:
        _reset_rows()
    return False


def measure() -> int:
    print(f"DB_FILE = {config.DB_FILE}")
    if "ble001_digest_stale_" not in config.DB_FILE:
        raise SystemExit("refusing to run: DB_FILE is not the temp copy")

    db.init_db()
    print(f"now_dt = {NOW.isoformat()}   (stale threshold is 60 min by default)")

    print()
    print("one ATTEMPTING digest_batch row, four shapes of its timestamp")
    print("-" * 104)
    print(
        f"{'shape':<19} {'age_s':>6} {'age_min':>8} {'stale':>6} "
        f"{'effective':<11} {'reconcile':>10} {'health':<10} {'stale_cnt':>9} {'crit':>5}"
    )
    print("-" * 104)

    rows: dict[str, dict] = {}
    for index, (label, delivered_at, payload_attempted_at) in enumerate(SHAPES):
        m = _measure_one(index, delivered_at, payload_attempted_at)
        rows[label] = m
        print(
            f"{label:<19} {m['age_seconds']!s:>6} {m['age_minutes']!s:>8} "
            f"{m['stale']!s:>6} {m['effective_status']!s:<11} "
            f"{m['requires_reconciliation']!s:>10} {m['health']!s:<10} "
            f"{m['stale_count']!s:>9} {m['critical_alerts']:>5}"
        )

    control = rows["readable col 3h"]
    fallback_ok = rows["empty col+payload"]
    empty_no_key = rows["empty col, no key"]
    malformed = rows["malformed col"]

    print()
    print("=== what the four shapes say ===")
    print(
        f"  1. readable col 3h      : age {control['age_seconds']}s, stale={control['stale']}, "
        f"counted as {control['effective_status']}, stale_count={control['stale_count']}"
    )
    print(
        f"  2. empty col+payload    : age {fallback_ok['age_seconds']}s, stale={fallback_ok['stale']} "
        f"(the payload fallback works when there is a payload timestamp)"
    )
    for label, m in (("3. empty col, no key", empty_no_key), ("4. malformed col", malformed)):
        print(
            f"  {label:<23} : age={m['age_seconds']}, unknown={m['age_unknown']}, "
            f"stale={m['stale']}, counted as {m['effective_status']}, "
            f"stale_count={m['stale_count']}, crit={m['critical_alerts']}"
        )
        print(f"      reason kept: {m['age_error']}")

    null_forbidden = _null_shape_is_forbidden()
    print()
    print(f"  NULL shape (not measured) : {NULL_SHAPE_NOTE}; insert refused: {null_forbidden}")

    print()
    print("=== why the health word says what it says ===")
    print("  attempting_count is counted but no rule reads it - so an ATTEMPTING row")
    print("  that is not stale raises nothing by itself. Alert lists, verbatim:")
    for label, m in (("readable col 3h", control), ("empty col, no key", empty_no_key)):
        print(f"  [{label}] health={m['health']} hermes={m['hermes']}")
        for line in m["all_alerts"]:
            print(f"      {line}")

    control_ok = (
        control["stale"] is True and control["stale_count"] == 1 and control["critical_alerts"] >= 1
    )
    fallback_works = fallback_ok["age_seconds"] == 10800 and fallback_ok["stale"] is True
    fails_closed = all(
        m["age_seconds"] is None
        and m["age_unknown"] is True
        and m["age_error"]
        and m["stale"] is True
        and m["effective_status"] == "STALE"
        and m["stale_count"] == 1
        and m["critical_alerts"] >= 1
        for m in (empty_no_key, malformed)
    )

    print()
    print("=== verdict ===")
    print(f"  the stale rule works when the timestamp is readable : {control_ok}")
    print(f"  the payload fallback works when the column is empty : {fallback_works}")
    print(f"  an unreadable timestamp fails closed (STALE + critical): {fails_closed}")
    print(f"  the NULL shape is refused by the schema : {null_forbidden}")
    print(
        "  NOTE: reachability is NOT shown - every writer passes now_iso for the"
        " column, and production holds no ATTEMPTING row."
    )
    ok = control_ok and fallback_works and fails_closed and null_forbidden
    return 0 if ok else 1


def main() -> int:
    ok = measure()
    print()
    print(
        "RESULT:",
        "fail-closed confirmed (hardening fix, no observed production failure)"
        if ok == 0
        else "FAIL - the measurement did not reproduce the documented behaviour",
    )
    return ok


if __name__ == "__main__":
    sys.exit(main())
