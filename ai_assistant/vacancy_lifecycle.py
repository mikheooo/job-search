"""Vacancy freshness, protection & dry-run archive engine (Phase 4B).

Computed model only: NOTHING here writes to the database, sends Telegram
messages, or performs network checks. There is deliberately NO apply path —
productive archiving requires a separate feature flag + owner approval.

Freshness states (independent from vacancies.state and tracking.status):
- ACTIVE_SEEN: recently observed in its source.
- STALE_CANDIDATE: not observed for a while, closure NOT confirmed.
- CLOSED_CONFIRMED: explicit closure evidence supplied by the caller
  (e.g. HH archived/closedForApplicants flags, source status words).
  Absence from one incomplete pass NEVER implies closure.
- UNKNOWN: freshness cannot be established (source run failed/unknown,
  unreadable dates) — conservative, never archived on age alone.
- ARCHIVE_CANDIDATE: old enough per policy AND not protected AND source
  healthy. Still only a candidate: applying it is out of scope here.

CLI: ``python -m ai_assistant.vacancy_lifecycle --dry-run [--limit N]
[--source SRC] [--json]``. Any other mode is refused.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from typing import Any

logger = logging.getLogger("vacancy_lifecycle")

ACTIVE_WINDOW_DAYS = 7
STALE_WINDOW_DAYS = 30
ARCHIVE_AFTER_DAYS = 90

# Sources whose adapters rewrite/refresh rows each successful run, so "not
# seen" there is weaker evidence than for append-mostly feeds. Unknown
# sources fall back to the default windows (fail-closed direction: longer).
SOURCE_WINDOWS: dict[str, dict[str, int]] = {
    "hirify": {"active": 3, "stale": 14},
    "linkedin_bangkok": {"active": 3, "stale": 14},
    "bangkok_careers": {"active": 3, "stale": 14},
    "geekjob": {"active": 3, "stale": 14},
}

# Tracking statuses that keep a vacancy protected (active hiring context).
# REJECTED / WITHDRAWN are terminal-from-our-side and do NOT protect.
# Everything else (incl. DISCOVERED/ANALYZED/ANALYSIS_FAILED) protects:
# context needed for review/retry must not be swept away. Fail-closed:
# unknown statuses protect too.
UNPROTECTED_TRACKING = {"REJECTED", "WITHDRAWN"}

# HH automation states that are clearly terminal (do not protect alone).
TERMINAL_HH = {"REJECTED", "BLOCKED", "FAILED"}

# Explicit source words that count as closure evidence (caller-supplied).
CLOSED_WORDS = (
    "expired", "closed", "cancelled", "canceled", "filled",
    "deleted", "archived", "completed",
)


def _parse_dt(raw: Any) -> datetime.datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def _age_days(first_seen: Any, last_seen: Any, now: datetime.datetime) -> float | None:
    ref = _parse_dt(last_seen) or _parse_dt(first_seen)
    if ref is None:
        return None
    return (now - ref).total_seconds() / 86400.0


def is_vacancy_protected(stable_id: str, conn) -> tuple[bool, list[str]]:
    """Fail-closed protection check across all hiring-context sources.

    Returns (protected, reasons). Any ambiguity (missing data, unknown
    status, unreadable row) protects. Read-only: SELECTs only.
    A database error itself is treated as unverifiable -> protected.
    """
    try:
        return _is_vacancy_protected_inner(stable_id, conn)
    except Exception as e:
        logger.warning("Protection check failed for %s, failing closed: %s", stable_id, e)
        return True, [f"check-failed:{type(e).__name__}"]


def _is_vacancy_protected_inner(stable_id: str, conn) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    cur = conn.cursor()

    cur.execute(
        "SELECT status FROM application_tracking WHERE vacancy_stable_id = ?",
        (stable_id,),
    )
    row = cur.fetchone()
    if row is None:
        reasons.append("no-tracking-row")
    else:
        status = str(row[0])
        if status in UNPROTECTED_TRACKING:
            reasons.append(f"tracking-terminal:{status}")
        else:
            return True, [f"tracking-active:{status}"]

    cur.execute(
        "SELECT 1 FROM application_queue WHERE vacancy_stable_id = ? LIMIT 1",
        (stable_id,),
    )
    if cur.fetchone():
        return True, ["in-application-queue"]

    for table in ("application_packages", "application_reviews"):
        cur.execute(
            f"SELECT 1 FROM {table} WHERE vacancy_stable_id = ? LIMIT 1",
            (stable_id,),
        )
        if cur.fetchone():
            return True, [f"has-{table}-record"]

    cur.execute(
        "SELECT state FROM hh_applications WHERE vacancy_stable_id = ?",
        (stable_id,),
    )
    hh_states = [str(r[0]) for r in cur.fetchall()]
    if hh_states:
        if any(s not in TERMINAL_HH for s in hh_states):
            return True, [f"hh-nonterminal:{sorted(set(hh_states))}"]
        reasons.append(f"hh-terminal:{sorted(set(hh_states))}")

    cur.execute(
        "SELECT status FROM application_submissions WHERE vacancy_stable_id = ?",
        (stable_id,),
    )
    if cur.fetchone():
        return True, ["has-submission-record"]

    cur.execute(
        "SELECT id FROM interview_events WHERE vacancy_stable_id = ? LIMIT 1",
        (stable_id,),
    )
    if cur.fetchone():
        return True, ["linked-interview-event"]

    # Interview linkage via conversation mapping (vacancy id may be NULL
    # on old rows): any interview event whose conversation maps here.
    try:
        from ai_assistant.employer_events import resolve_vacancy_for_conversation

        cur.execute("SELECT DISTINCT conversation_id FROM interview_events")
        for (conv,) in cur.fetchall():
            if conv and resolve_vacancy_for_conversation(conv, conn) == stable_id:
                return True, ["linked-interview-via-conversation"]
    except Exception as e:  # fail-closed on resolver errors
        logger.debug("interview conversation resolver skipped: %s", e)
        return True, ["interview-linkage-unverifiable"]

    return False, reasons


def classify_vacancy(
    row: dict[str, Any],
    conn,
    *,
    now: datetime.datetime | None = None,
    source_healthy: bool | None = None,
    closed_evidence: dict[str, str] | None = None,
    active_days: int = ACTIVE_WINDOW_DAYS,
    stale_days: int = STALE_WINDOW_DAYS,
    archive_days: int = ARCHIVE_AFTER_DAYS,
) -> dict[str, Any]:
    """Compute freshness for one vacancy row. Pure read path, no writes."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    sid = str(row.get("stable_id") or "")
    source = str(row.get("source") or "")
    windows = SOURCE_WINDOWS.get(source, {})
    active_days = windows.get("active", active_days)
    stale_days = windows.get("stale", stale_days)

    reasons: list[str] = [f"source={source or '?'}"]

    ev = (closed_evidence or {}).get(sid)
    if ev:
        protected, prot = is_vacancy_protected(sid, conn)
        reasons.append(f"closed-evidence:{ev}")
        reasons.append("protected" if protected else "unprotected")
        return {
            "stable_id": sid, "freshness": "CLOSED_CONFIRMED",
            "protected": protected, "reasons": reasons,
        }

    age = _age_days(row.get("first_seen_at"), row.get("last_seen_at"), now)
    if age is None:
        reasons.append("unreadable-dates")
        protected, prot = is_vacancy_protected(sid, conn)
        reasons.extend(prot)
        return {"stable_id": sid, "freshness": "UNKNOWN",
                "protected": protected, "reasons": reasons}
    reasons.append(f"age_days={age:.1f}")

    if source_healthy is False:
        reasons.append("source-unhealthy:age-not-conclusive")
        protected, prot = is_vacancy_protected(sid, conn)
        reasons.extend(prot)
        return {"stable_id": sid, "freshness": "UNKNOWN",
                "protected": protected, "reasons": reasons}

    if age <= active_days:
        state = "ACTIVE_SEEN"
        reasons.append(f"seen-within-{active_days}d")
    elif age <= stale_days:
        state = "STALE_CANDIDATE"
        reasons.append(f"not-seen-{active_days}-{stale_days}d-closure-unconfirmed")
    else:
        state = "STALE_CANDIDATE"
        reasons.append(f"not-seen>{stale_days}d-closure-unconfirmed")

    protected, prot = is_vacancy_protected(sid, conn)
    reasons.extend(prot)
    if state == "STALE_CANDIDATE" and age > archive_days and not protected:
        state = "ARCHIVE_CANDIDATE"
        reasons.append(f"age>{archive_days}d-unprotected-and-source-healthy")
    return {"stable_id": sid, "freshness": state,
            "protected": protected, "reasons": reasons}


def dry_run_report(
    conn,
    *,
    limit: int = 5000,
    source: str | None = None,
    now: datetime.datetime | None = None,
    source_health: dict[str, bool] | None = None,
    closed_evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Classify vacancies without modifying anything. Returns the report."""
    cur = conn.cursor()
    sql = ("SELECT stable_id, source, title, company, first_seen_at, last_seen_at,"
           " state FROM vacancies")
    args: tuple = ()
    if source:
        sql += " WHERE source = ?"
        args = (source,)
    sql += " ORDER BY last_seen_at DESC NULLS LAST LIMIT ?"
    rows = cur.execute(sql, args + (limit,)).fetchall()
    cols = ["stable_id", "source", "title", "company",
            "first_seen_at", "last_seen_at", "state"]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        rec = dict(zip(cols, r))
        healthy = (source_health or {}).get(str(rec["source"])) if source_health else None
        item = classify_vacancy(rec, conn, now=now,
                                source_healthy=healthy,
                                closed_evidence=closed_evidence)
        # Report-safe projection: no descriptions, no raw payloads, no secrets.
        buckets.setdefault(item["freshness"], []).append({
            "stable_id": item["stable_id"],
            "source": rec["source"],
            "title": rec["title"],
            "company": rec["company"],
            "last_seen_at": rec["last_seen_at"],
            "vacancy_state": rec["state"],
            "protected": item["protected"],
            "reasons": item["reasons"],
        })
    return {
        "mode": "dry-run",
        "writes": 0,
        "buckets": {k: len(v) for k, v in buckets.items()},
        "items": buckets,
        "policy": {
            "active_days": ACTIVE_WINDOW_DAYS,
            "stale_days": STALE_WINDOW_DAYS,
            "archive_days": ARCHIVE_AFTER_DAYS,
            "source_windows": SOURCE_WINDOWS,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vacancy freshness dry-run (read-only).")
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="Acknowledge read-only mode. No other mode exists.")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=str, default=None,
                        help="Write full JSON report to FILE (utf-8).")
    args = parser.parse_args(argv)

    from ai_assistant import config as _config
    import sqlite3 as _sqlite3

    # Strictly read-only open: even a SELECT-only dry-run must not take a
    # write lock on the production database.
    conn = _sqlite3.connect("file:" + _config.DB_FILE + "?mode=ro", uri=True)
    try:
        report = dry_run_report(conn, limit=args.limit, source=args.source)
    finally:
        conn.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"wrote {args.out}")
    elif args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1)[:200000])
    else:
        print(f"mode={report['mode']} writes={report['writes']}")
        for bucket, n in sorted(report["buckets"].items()):
            print(f"{bucket}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
