from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field

from . import config
from .db import get_connection, init_db

REVIEW_VERSION = "v1"

class ReviewStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class ApplicationReview(BaseModel):
    vacancy_stable_id: str
    company: Optional[str] = None
    title: Optional[str] = None
    source: Optional[str] = None
    vacancy_url: Optional[str] = None
    final_url: Optional[str] = None
    match_score: Optional[float] = None
    deep_score: Optional[float] = None
    priority_score: Optional[float] = None
    rank: Optional[int] = None
    application_strategy: Optional[str] = None
    resume_summary: Optional[str] = None
    tailored_skills: List[str] = Field(default_factory=list)
    relevant_experience: List[str] = Field(default_factory=list)
    cover_letter: Optional[str] = None
    fields_filled: List[str] = Field(default_factory=list)
    fields_skipped: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    screenshot_path: Optional[str] = None
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    note: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    review_version: str = REVIEW_VERSION

    model_config = {"use_enum_values": False}

def _now() -> str:
    return datetime.utcnow().isoformat()

def _ensure_table():
    init_db()

def save_application_review(review: ApplicationReview) -> None:
    _ensure_table()
    conn = get_connection()
    cur = conn.cursor()
    # Ensure table exists (also via init_db, but check)
    cur.execute("SELECT sql FROM sqlite_master WHERE type=\"table\" AND name=\"application_reviews\"")
    # Already ensured via init_db
    cur.execute(
        """INSERT INTO application_reviews (vacancy_stable_id, review_json, status, note, created_at, updated_at, review_version)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(vacancy_stable_id) DO UPDATE SET
               review_json=excluded.review_json,
               status=excluded.status,
               note=excluded.note,
               updated_at=excluded.updated_at,
               review_version=excluded.review_version
        """,
        (
            review.vacancy_stable_id,
            review.model_dump_json(),
            review.status.value,
            review.note,
            review.created_at or _now(),
            review.updated_at or _now(),
            review.review_version,
        ),
    )
    conn.commit()
    conn.close()

def get_application_review(vacancy_stable_id: str, review_version: str | None = None) -> Optional[ApplicationReview]:
    _ensure_table()
    conn = get_connection()
    cur = conn.cursor()
    if review_version:
        cur.execute("SELECT review_json FROM application_reviews WHERE vacancy_stable_id=? AND review_version=?", (vacancy_stable_id, review_version))
    else:
        cur.execute("SELECT review_json FROM application_reviews WHERE vacancy_stable_id=?", (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return ApplicationReview.model_validate_json(row[0])
        except Exception:
            try:
                data = json.loads(row[0])
                return ApplicationReview.model_validate(data)
            except Exception:
                return None
    return None

def is_review_created(vacancy_stable_id: str, review_version: str | None = None) -> bool:
    return get_application_review(vacancy_stable_id, review_version) is not None

def list_application_reviews(status: Optional[str] = None, limit: int = 100) -> List[ApplicationReview]:
    _ensure_table()
    conn = get_connection()
    cur = conn.cursor()
    if status:
        cur.execute("SELECT review_json FROM application_reviews WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit))
    else:
        cur.execute("SELECT review_json FROM application_reviews ORDER BY updated_at DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        try:
            res.append(ApplicationReview.model_validate_json(r[0]))
        except Exception:
            continue
    return res

def _get_required_data(vacancy_stable_id: str):
    from .application_tracking import get_application_status, ApplicationStatus
    from .application_queue import get_queue_item
    from .db import get_vacancy_by_id, get_application_package, get_deep_analysis
    from .browser_executor import get_browser_session, BrowserStatus
    from .db import _row_to_vacancy

    track = get_application_status(vacancy_stable_id)
    if not track:
        raise ValueError(f"No tracking for {vacancy_stable_id}")
    if track.status != ApplicationStatus.READY_TO_APPLY:
        raise ValueError(f"Tracking status {track.status} is not READY_TO_APPLY - cannot review. Only READY_TO_APPLY allowed.")

    # queue
    q = get_queue_item(vacancy_stable_id)
    if not q:
        # also try without version
        from .application_queue import list_queue
        # fallback: check if vacancy exists in queue at all
        raise ValueError(f"Queue item not found for {vacancy_stable_id} - not READY_TO_APPLY in queue")

    # package
    pkg_row = get_application_package(vacancy_stable_id)
    if not pkg_row:
        raise ValueError(f"Application package not found for {vacancy_stable_id}")

    # browser
    sess = get_browser_session(vacancy_stable_id)
    if not sess:
        raise ValueError(f"Browser preparation not found for {vacancy_stable_id} - need READY_FOR_REVIEW")
    # Allow BLOCKED for review creation, but not for approve (approve checks separately)
    # Only check existence here, not status, but ensure session exists
    # For review creation, we allow both READY_FOR_REVIEW and BLOCKED, but BLOCKED will have warnings
    if sess.status not in [BrowserStatus.READY_FOR_REVIEW, BrowserStatus.COMPLETED, BrowserStatus.BLOCKED]:
        raise ValueError(f"Browser status {sess.status} is not READY_FOR_REVIEW/BLOCKED - cannot review.")

    # vacancy
    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        raise ValueError(f"Vacancy not found: {vacancy_stable_id}")
    vac = _row_to_vacancy(row)

    # deep
    deep_row = get_deep_analysis(vacancy_stable_id)
    deep = None
    if deep_row and deep_row[4]:
        try:
            deep = json.loads(deep_row[4])
        except Exception:
            deep = None

    # package json
    pkg_json = {}
    if pkg_row and pkg_row[2]:
        try:
            pkg_json = json.loads(pkg_row[2])
        except Exception:
            pkg_json = {}

    return track, q, pkg_json, sess, vac, deep

def create_application_review(vacancy_stable_id: str) -> ApplicationReview:
    # Safety: never call submit/Apply
    _ensure_table()
    # Check if already exists with same version - idempotent
    existing = get_application_review(vacancy_stable_id, REVIEW_VERSION)
    if existing:
        return existing

    track, q, pkg_json, sess, vac, deep = _get_required_data(vacancy_stable_id)

    # Build review from existing data, no LLM
    now = _now()
    # queue rank/priority
    # q is QueueItem
    # pkg_json contains cover_letter etc
    review = ApplicationReview(
        vacancy_stable_id=vacancy_stable_id,
        company=track.company or vac.company,
        title=track.title or vac.title,
        source=track.source or vac.source,
        vacancy_url=track.vacancy_url or vac.job_url,
        final_url=sess.final_url or vac.job_url,
        match_score=track.match_score,
        deep_score=track.deep_score,
        priority_score=getattr(q, "priority_score", None),
        rank=getattr(q, "rank", None),
        application_strategy=pkg_json.get("application_strategy") or getattr(q, "application_strategy", None),
        resume_summary=pkg_json.get("resume_summary"),
        tailored_skills=pkg_json.get("tailored_skills", []),
        relevant_experience=pkg_json.get("relevant_experience", []),
        cover_letter=pkg_json.get("cover_letter"),
        fields_filled=list(sess.fields_filled) if hasattr(sess, "fields_filled") else [],
        fields_skipped=list(sess.fields_skipped) if hasattr(sess, "fields_skipped") else [],
        warnings=list(sess.warnings) if hasattr(sess, "warnings") else [],
        screenshot_path=sess.screenshot_path,
        status=ReviewStatus.PENDING_REVIEW,
        note=None,
        created_at=now,
        updated_at=now,
        review_version=REVIEW_VERSION,
    )
    save_application_review(review)
    return review

def approve_review(vacancy_stable_id: str) -> ApplicationReview:
    _ensure_table()
    rev = get_application_review(vacancy_stable_id, REVIEW_VERSION)
    if not rev:
        # Try without version
        rev = get_application_review(vacancy_stable_id)
        if not rev:
            raise ValueError(f"Review not found for {vacancy_stable_id}")
        # If version mismatch, consider not found for current version
        if rev.review_version != REVIEW_VERSION:
            raise ValueError(f"Review version mismatch for {vacancy_stable_id} - needs recreation")
    # Check browser status
    from .browser_executor import get_browser_session, BrowserStatus
    sess = get_browser_session(vacancy_stable_id)
    if not sess or sess.status not in [BrowserStatus.READY_FOR_REVIEW, BrowserStatus.COMPLETED]:
        raise ValueError(f"Cannot approve: browser status {sess.status if sess else None} is not READY_FOR_REVIEW. BLOCKED cannot be approved.")
    # Check tracking still READY
    from .application_tracking import get_application_status, ApplicationStatus
    track = get_application_status(vacancy_stable_id)
    if not track or track.status != ApplicationStatus.READY_TO_APPLY:
        raise ValueError(f"Cannot approve: tracking status {track.status if track else None} is not READY_TO_APPLY")
    if rev.status == ReviewStatus.APPROVED:
        return rev  # idempotent
    if rev.status == ReviewStatus.REJECTED:
        raise ValueError(f"Cannot approve: review already REJECTED")

    # Safety: never change tracking to APPLIED, never call browser submit
    rev.status = ReviewStatus.APPROVED
    rev.updated_at = _now()
    save_application_review(rev)
    return rev

def reject_review(vacancy_stable_id: str, note: str | None = None) -> ApplicationReview:
    _ensure_table()
    rev = get_application_review(vacancy_stable_id, REVIEW_VERSION)
    if not rev:
        rev = get_application_review(vacancy_stable_id)
        if not rev:
            raise ValueError(f"Review not found for {vacancy_stable_id}")
        if rev.review_version != REVIEW_VERSION:
            raise ValueError(f"Review version mismatch")
    if rev.status == ReviewStatus.REJECTED:
        # idempotent, update note if provided
        if note and note != rev.note:
            rev.note = note
            rev.updated_at = _now()
            save_application_review(rev)
        return rev
    if rev.status == ReviewStatus.APPROVED:
        raise ValueError(f"Cannot reject: already APPROVED")
    rev.status = ReviewStatus.REJECTED
    rev.note = note
    rev.updated_at = _now()
    save_application_review(rev)
    return rev
