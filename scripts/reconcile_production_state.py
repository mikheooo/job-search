"""Reconcile known production integrity drift without external side effects.

The command is local-only: it reads and updates SQLite, creates a timestamped
backup first, and never imports browser, Telegram, or auto-apply runtimes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


KNOWN_VERIFICATION_STATUSES = {"VERIFIED", "FAILED", "AMBIGUOUS", "BLOCKED"}


def _update_review(cur: sqlite3.Cursor, sid: str, new_status: str, note: str, now: str) -> None:
    row = cur.execute(
        "SELECT review_json, note FROM application_reviews WHERE vacancy_stable_id=?",
        (sid,),
    ).fetchone()
    if not row:
        return
    payload = json.loads(row[0]) if row[0] else {"vacancy_stable_id": sid}
    payload["status"] = new_status
    payload["note"] = note
    payload["updated_at"] = now
    cur.execute(
        "UPDATE application_reviews SET review_json=?, status=?, note=?, updated_at=? "
        "WHERE vacancy_stable_id=?",
        (json.dumps(payload, ensure_ascii=False), new_status, note, now, sid),
    )


def _normalize_verification(row: sqlite3.Row) -> tuple[str, str, str] | None:
    from ai_assistant.submission_verifier import SubmissionVerification

    version = str(row["verification_version"] or "v1")
    status = str(row["verification_status"] or "").upper()
    if version in KNOWN_VERIFICATION_STATUSES and status not in KNOWN_VERIFICATION_STATUSES:
        version, status = str(row["verification_status"] or "v1"), version
    if status not in KNOWN_VERIFICATION_STATUSES:
        return None

    try:
        raw_payload = json.loads(row["verification_json"]) if row["verification_json"] else {}
    except Exception:
        raw_payload = {"legacy_message": str(row["verification_json"] or "")}

    try:
        parsed = SubmissionVerification.model_validate(raw_payload)
        if parsed.verification_status.value == status and parsed.verification_version == version:
            return None
    except Exception:
        pass

    verified_at = row["verified_at"] or row["created_at"] or dt.datetime.now(dt.timezone.utc).isoformat()
    evidence = raw_payload if isinstance(raw_payload, dict) else {"legacy_payload": raw_payload}
    payload = {
        "vacancy_stable_id": row["vacancy_stable_id"],
        "submission_id": row["submission_id"],
        "verification_status": status,
        "evidence": evidence,
        "final_url": None,
        "page_title": None,
        "success_signal": None,
        "screenshot_path": None,
        "verified_at": verified_at,
        "warnings": ["Reconciled legacy verification payload during production stabilization"],
        "flow_type": None,
        "source_url": None,
        "application_url": None,
        "application_domain": None,
        "redirect_chain": [],
        "is_external_application": False,
        "verification_strategy": None,
        "verification_version": version,
    }
    return version, status, json.dumps(payload, ensure_ascii=False)


def reconcile(db_path: Path, apply_changes: bool) -> dict[str, object]:
    db_path = db_path.resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    blocked = [r[0] for r in cur.execute(
        "SELECT DISTINCT r.vacancy_stable_id FROM application_reviews r "
        "JOIN browser_preparations b ON b.vacancy_stable_id=r.vacancy_stable_id "
        "WHERE r.status='APPROVED' AND b.status='BLOCKED'"
    )]
    applied = [r[0] for r in cur.execute(
        "SELECT r.vacancy_stable_id FROM application_reviews r "
        "JOIN application_tracking t ON t.vacancy_stable_id=r.vacancy_stable_id "
        "WHERE r.status='APPROVED' AND t.status='APPLIED'"
    )]
    verification_repairs = []
    rows = cur.execute("SELECT * FROM submission_verifications").fetchall()
    for row in rows:
        normalized = _normalize_verification(row)
        if normalized:
            verification_repairs.append((row, normalized))

    backup_path = None
    if apply_changes:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.pre-stabilization-{stamp}.bak")
        shutil.copy2(db_path, backup_path)
        try:
            cur.execute("BEGIN IMMEDIATE")
            for sid in blocked:
                _update_review(
                    cur,
                    sid,
                    "PENDING_REVIEW",
                    "Approval reopened because persisted browser preparation is BLOCKED",
                    now,
                )
            for sid in applied:
                _update_review(
                    cur,
                    sid,
                    "COMPLETED",
                    "Review consumed after tracking reached APPLIED",
                    now,
                )
            for row, (version, status, payload) in verification_repairs:
                cur.execute(
                    "UPDATE submission_verifications SET verification_version=?, "
                    "verification_status=?, verification_json=?, updated_at=? "
                    "WHERE vacancy_stable_id=? AND submission_id=? AND verification_version=?",
                    (
                        version,
                        status,
                        payload,
                        now,
                        row["vacancy_stable_id"],
                        row["submission_id"],
                        row["verification_version"],
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    conn.close()
    return {
        "applied": apply_changes,
        "backup": str(backup_path) if backup_path else None,
        "blocked_reviews_reopened": blocked,
        "applied_reviews_completed": applied,
        "verification_json_repaired": [
            {"vacancy_stable_id": row["vacancy_stable_id"], "submission_id": row["submission_id"]}
            for row, _ in verification_repairs
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="state.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(reconcile(Path(args.db), args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
