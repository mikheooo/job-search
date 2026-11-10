"""Reconcile headless submissions into state.db and application artifacts."""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys
import time
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import db
from ai_assistant.application_tracking import ApplicationStatus, set_application_status


def reconcile(
    vid: str,
    resume_title: str = "Специалист по внедрению и сопровождению CRM",
    confirmed: bool = True,
    post_reason: str = "",
) -> dict[str, Any]:
    """Reconcile a vacancy submission into application_submissions and application_tracking."""
    db.init_db()
    sid = f"hh:{vid}"
    app_dir = pathlib.Path(PROJECT_ROOT) / "artifacts" / "applications" / f"hh-{vid}"
    letter_path = app_dir / "cover_letter.ru.txt"

    if not letter_path.exists():
        return {
            "ok": False,
            "error": f"cover letter missing at {letter_path}",
        }

    # Check for existing submission to avoid duplicates
    conn = db.get_connection()
    try:
        existing = conn.execute(
            "SELECT submission_id FROM application_submissions WHERE vacancy_stable_id = ?",
            (sid,),
        ).fetchone()
        if existing:
            return {
                "ok": True,
                "status": "ALREADY_RECORDED",
                "submission_id": existing[0],
            }
    finally:
        conn.close()

    letter_text = letter_path.read_text(encoding="utf-8")
    letter_sha = hashlib.sha256(letter_text.strip().encode("utf-8")).hexdigest()[:16]

    # Copy screenshot if available
    src_shot = pathlib.Path(PROJECT_ROOT) / "artifacts" / "headless-poc" / f"t722_after_{vid}.png"
    dest_shot = app_dir / "submission_success.png"
    if src_shot.exists():
        dest_shot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src_shot, dest_shot)

    now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    sub_id = f"hh-assisted-{vid}-{time.strftime('%Y%m%dT%H%M%S')}"

    # Determine submission status
    submission_status = "SUBMITTED" if confirmed else "AMBIGUOUS"
    return_status = "SUBMITTED" if confirmed else "SUBMITTED_UNVERIFIED"

    # Update state.db
    set_application_status(
        sid,
        ApplicationStatus.SUBMITTED if confirmed else "AMBIGUOUS",
        notes=f"Headless assisted submission verified={confirmed} at {now_ts}: {post_reason}",
    )

    payload = {
        "submission_id": sub_id,
        "channel": "headless_browser",
        "vacancy_stable_id": sid,
        "vacancy_id": vid,
        "resume_id": "287960707",
        "resume_title": resume_title,
        "cover_letter_sha256_16": letter_sha,
        "cover_letter_chars": len(letter_text),
        "submitted_at": now_ts,
        "confirmed": confirmed,
        "post_confirmed": confirmed,
        "record_status": submission_status,
        "post_reason": post_reason,
        "proof_screenshot": str(dest_shot) if dest_shot.exists() else None,
    }

    db.save_submission(
        sid,
        json.dumps(payload, ensure_ascii=False),
        submission_status,
        submitted_at=now_ts,
        created_at=now_ts,
        submission_id=sub_id,
    )

    # Update preflight.json if present
    pf_path = app_dir / "preflight.json"
    if pf_path.exists():
        try:
            pf = json.loads(pf_path.read_text(encoding="utf-8"))
            pf["preflight_verdict"] = submission_status
            pf["submission"] = {
                "status": submission_status,
                "submission_id": sub_id,
                "resume_id": "287960707",
                "letter_sha": letter_sha,
                "submitted_at": now_ts,
                "proof_screenshot": str(dest_shot) if dest_shot.exists() else None,
            }
            pf_path.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "ok": True,
        "status": return_status,
        "submission_id": sub_id,
    }


if __name__ == "__main__":
    vid = sys.argv[1] if len(sys.argv) > 1 else "138065543"
    print(reconcile(vid))
