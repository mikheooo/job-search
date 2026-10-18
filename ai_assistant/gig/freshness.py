"""Gig freshness, protection & submission linkage (Phase 4C §5/§9).

Computed model only, mirroring Phase 4B vacancy_lifecycle for vacancies:
NOTHING here writes to any database, sends messages, or performs network
checks. No new productive statuses are introduced: ``GigStatus`` stays
NEW/SHORTLISTED/SKIPPED, interaction states are derived read-only.

Freshness states: ACTIVE_SEEN / STALE_CANDIDATE / CLOSED_CONFIRMED /
UNKNOWN / ARCHIVE_CANDIDATE. Gigs move faster than vacancies, hence tighter
default windows (active 3d, stale 14d, archive 60d). CLOSED_CONFIRMED comes
ONLY from explicit ``source_status`` evidence, never from age alone.

Interaction states (§5B target model, derived): NOT_APPLIED /
PROPOSAL_PREPARED / PROPOSAL_SUBMITTED / CLIENT_REPLIED / NEGOTIATING /
AWARDED / COMPLETED / REJECTED / CANCELLED. Mapping from (gig.status,
submission row, events) is documented in ``interaction_state()``. States
with no data source (IN_PROGRESS) are intentionally unmapped.

CLI: ``python -m ai_assistant.gig.freshness --dry-run [--limit N]
[--source SRC]``. Read-only (mode=ro open). No other mode exists.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from typing import Any

logger = logging.getLogger("gig_freshness")

ACTIVE_WINDOW_DAYS = 3
STALE_WINDOW_DAYS = 14
ARCHIVE_AFTER_DAYS = 60

#: source_status values that count as explicit closure evidence.
CLOSED_WORDS = (
    "expired", "closed", "cancelled", "canceled", "filled",
    "deleted", "archived", "completed", "frozen",
    # Phase 4C.4: kwork stop/user_stop = not accepting proposals.
    "stopped", "user_stopped",
    # Phase 4D.2: legacy stored tokens from before the STOPPED mapping.
    # Verified same meaning (isWantActive=False, expiry passed).
    "stop", "user_stop",
)

#: Submission statuses that keep a gig protected (hiring context alive).
#: REJECTED / CLOSED are terminal-from-our-side.
TERMINAL_SUBMISSION = {"REJECTED", "CLOSED"}


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


def link_submission(gig_source: str, gig_source_id: str,
                    tracker_path=None) -> dict[str, Any] | None:
    """Link a gig to its submission row (read-only).

    Submissions key on ``(source, project_id)`` with ``project_id`` in
    ``fl_<digits>`` form for Freelancer; gig ``source_id`` uses the same
    form (``fl_<digits>``), otherwise exact match. Returns the submission
    as a plain dict, or None. Ambiguity cannot occur (UNIQUE constraint).
    """
    from ai_assistant.gig.tracker import SubmissionTracker

    tracker = SubmissionTracker(tracker_path) if tracker_path else SubmissionTracker()
    rec = tracker.get(str(gig_source_id), str(gig_source))
    if rec is None:
        return None
    return {
        "project_id": rec.project_id,
        "source": rec.source,
        "status": rec.status,
        "submitted_at": rec.submitted_at,
        "proposal_created_at": rec.proposal_created_at,
        "revenue_usd": rec.revenue_usd,
    }


def is_gig_protected(gig_id: str, conn, tracker_path=None) -> tuple[bool, list[str]]:
    """Fail-closed protection: SHORTLISTED or live submission context."""
    try:
        return _is_gig_protected_inner(gig_id, conn, tracker_path)
    except Exception as e:  # noqa: BLE001 - unverifiable means protected
        logger.warning("Gig protection check failed for %s: %s", gig_id, e)
        return True, [f"check-failed:{type(e).__name__}"]


def _is_gig_protected_inner(gig_id: str, conn, tracker_path) -> tuple[bool, list[str]]:
    cur = conn.cursor()
    cur.execute("SELECT status, source, source_id FROM gigs WHERE id = ?", (gig_id,))
    row = cur.fetchone()
    if row is None:
        return True, ["gig-row-missing"]
    status, source, source_id = str(row[0]), str(row[1]), str(row[2])
    if status == "SHORTLISTED":
        return True, ["human-shortlisted"]
    if status not in ("NEW", "SKIPPED"):
        return True, [f"unknown-gig-status:{status}"]
    sub = link_submission(source, source_id, tracker_path)
    if sub is None:
        return False, ["no-submission"]
    if str(sub.get("status")) in TERMINAL_SUBMISSION:
        return False, [f"submission-terminal:{sub.get('status')}"]
    return True, [f"submission-active:{sub.get('status')}"]


def interaction_state(gig_status: str, submission: dict[str, Any] | None) -> str:
    """Derive the §5B interaction state without storing it.

    NOT_APPLIED: NEW gig, no submission. PROPOSAL_PREPARED: submission row
    with proposal_created_at but no submitted_at. PROPOSAL_SUBMITTED:
    submitted, awaiting reply. CLIENT_REPLIED / NEGOTIATING(=INTERVIEW):
    passthrough of tracker states. AWARDED: passthrough. COMPLETED:
    revenue_usd recorded. REJECTED / CANCELLED(=CLOSED): passthrough.
    IN_PROGRESS has no data source and stays unmapped by design.
    """
    if submission is None:
        return "NOT_APPLIED"
    st = str(submission.get("status") or "")
    if st == "READY":
        return "PROPOSAL_PREPARED" if submission.get("proposal_created_at") else "NOT_APPLIED"
    mapping = {
        "SUBMITTED": "PROPOSAL_SUBMITTED",
        "CLIENT_REPLIED": "CLIENT_REPLIED",
        "INTERVIEW": "NEGOTIATING",
        "AWARDED": "AWARDED",
        "REJECTED": "REJECTED",
        "CLOSED": "CANCELLED",
    }
    if st in mapping:
        return mapping[st]
    if submission.get("revenue_usd") not in (None, 0):
        return "COMPLETED"
    return "NOT_APPLIED"


def classify_gig(
    row: dict[str, Any],
    conn,
    *,
    now: datetime.datetime | None = None,
    source_healthy: bool | None = None,
    tracker_path=None,
    active_days: int = ACTIVE_WINDOW_DAYS,
    stale_days: int = STALE_WINDOW_DAYS,
    archive_days: int = ARCHIVE_AFTER_DAYS,
) -> dict[str, Any]:
    """Compute freshness + interaction for one gig row. Read-only."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    gid = str(row.get("id") or "")
    reasons: list[str] = [f"source={row.get('source') or '?'}"]

    status_word = str(row.get("source_status") or "").strip().lower()
    if status_word in CLOSED_WORDS:
        protected, prot = is_gig_protected(gid, conn, tracker_path)
        reasons.append(f"closed-evidence:{status_word}")
        reasons.append("protected" if protected else "unprotected")
        sub = link_submission(str(row.get("source")), str(row.get("source_id")), tracker_path)
        return {"id": gid, "freshness": "CLOSED_CONFIRMED",
                "protected": protected,
                "interaction": interaction_state(str(row.get("status")), sub),
                "reasons": reasons}

    age = _age_days(row.get("first_seen_at"), row.get("last_seen_at"), now)
    if age is None:
        reasons.append("unreadable-dates")
        protected, prot = is_gig_protected(gid, conn, tracker_path)
        reasons.extend(prot)
        sub = link_submission(str(row.get("source")), str(row.get("source_id")), tracker_path)
        return {"id": gid, "freshness": "UNKNOWN", "protected": protected,
                "interaction": interaction_state(str(row.get("status")), sub),
                "reasons": reasons}
    reasons.append(f"age_days={age:.1f}")

    if source_healthy is False:
        reasons.append("source-unhealthy:age-not-conclusive")
        protected, prot = is_gig_protected(gid, conn, tracker_path)
        reasons.extend(prot)
        sub = link_submission(str(row.get("source")), str(row.get("source_id")), tracker_path)
        return {"id": gid, "freshness": "UNKNOWN", "protected": protected,
                "interaction": interaction_state(str(row.get("status")), sub),
                "reasons": reasons}

    state = "ACTIVE_SEEN" if age <= active_days else "STALE_CANDIDATE"
    reasons.append(f"seen-within-{active_days}d" if state == "ACTIVE_SEEN"
                   else f"not-seen>{active_days}d-closure-unconfirmed")
    protected, prot = is_gig_protected(gid, conn, tracker_path)
    reasons.extend(prot)
    if state == "STALE_CANDIDATE" and age > archive_days and not protected:
        state = "ARCHIVE_CANDIDATE"
        reasons.append(f"age>{archive_days}d-unprotected-and-source-healthy")
    sub = link_submission(str(row.get("source")), str(row.get("source_id")), tracker_path)
    return {"id": gid, "freshness": state, "protected": protected,
            "interaction": interaction_state(str(row.get("status")), sub),
            "reasons": reasons}


def dry_run_report(conn, *, limit: int = 5000, source: str | None = None,
                   now: datetime.datetime | None = None,
                   source_health: dict[str, bool] | None = None,
                   tracker_path=None) -> dict[str, Any]:
    """Classify gigs without modifying anything."""
    cur = conn.cursor()
    sql = ("SELECT id, source, source_id, url, title, budget_min, budget_max,"
           " currency, published_at, first_seen_at, last_seen_at, status,"
           " source_status FROM gigs")
    args: tuple = ()
    if source:
        sql += " WHERE source = ?"
        args = (source,)
    sql += " ORDER BY last_seen_at DESC NULLS LAST LIMIT ?"
    cols = ["id", "source", "source_id", "url", "title", "budget_min",
            "budget_max", "currency", "published_at", "first_seen_at",
            "last_seen_at", "status", "source_status"]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    buckets: dict[str, list[dict[str, Any]]] = {}
    interactions: dict[str, int] = {}
    for r in cur.execute(sql, args + (limit,)).fetchall():
        rec = dict(zip(cols, r))
        healthy = (source_health or {}).get(str(rec["source"])) if source_health else None
        item = classify_gig(rec, conn, now=now, source_healthy=healthy,
                            tracker_path=tracker_path)
        interactions[item["interaction"]] = interactions.get(item["interaction"], 0) + 1
        buckets.setdefault(item["freshness"], []).append({
            "id": item["id"], "source": rec["source"], "title": rec["title"],
            "budget": [rec["budget_min"], rec["budget_max"], rec["currency"]],
            "last_seen_at": rec["last_seen_at"], "gig_status": rec["status"],
            "interaction": item["interaction"], "protected": item["protected"],
            "reasons": item["reasons"],
        })
    return {"mode": "dry-run", "writes": 0,
            "buckets": {k: len(v) for k, v in buckets.items()},
            "interactions": interactions,
            "items": buckets,
            "policy": {"active_days": ACTIVE_WINDOW_DAYS,
                       "stale_days": STALE_WINDOW_DAYS,
                       "archive_days": ARCHIVE_AFTER_DAYS}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gig freshness dry-run (read-only).")
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--source", type=str, default=None)
    args = parser.parse_args(argv)

    import sqlite3 as _sqlite3

    from ai_assistant import config as _config

    conn = _sqlite3.connect("file:" + _config.DB_FILE + "?mode=ro", uri=True)
    try:
        report = dry_run_report(conn, limit=args.limit, source=args.source)
    finally:
        conn.close()
    print(f"mode={report['mode']} writes={report['writes']}")
    for bucket, n in sorted(report["buckets"].items()):
        print(f"{bucket}: {n}")
    print("interactions:", report["interactions"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
