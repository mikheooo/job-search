from __future__ import annotations

import hashlib
import json
import sqlite3
import warnings
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field, model_validator

from . import config
from .db import get_connection, init_db

REVIEW_VERSION = "v1"

class ReviewStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"


def compute_review_fingerprint(vacancy_stable_id: str, package: Any) -> str:
    """Compute sha256 fingerprint from canonical JSON of {answers, cover_letter, vacancy_stable_id}.
    
    Answers are sorted by question_id.
    """
    pkg_data: Dict[str, Any] = {}
    if isinstance(package, tuple) and len(package) >= 3:
        raw_json = package[2]
        try:
            pkg_data = json.loads(raw_json) if raw_json else {}
        except Exception:
            pkg_data = {}
    elif isinstance(package, str):
        try:
            pkg_data = json.loads(package)
        except Exception:
            pkg_data = {}
    elif isinstance(package, dict):
        pkg_data = package
    elif hasattr(package, "model_dump"):
        pkg_data = package.model_dump()
    elif hasattr(package, "__dict__"):
        pkg_data = package.__dict__

    cover_letter = pkg_data.get("cover_letter") if isinstance(pkg_data, dict) else getattr(package, "cover_letter", "")
    cover_letter = str(cover_letter or "")

    raw_answers = pkg_data.get("answers") if isinstance(pkg_data, dict) else getattr(package, "answers", [])
    if not isinstance(raw_answers, list):
        raw_answers = []

    normalized_answers = []
    for ans in raw_answers:
        if isinstance(ans, dict):
            qid = str(ans.get("question_id", ""))
            a = ans.get("answer")
            normalized_answers.append({"answer": str(a) if a is not None else "", "question_id": qid})
        elif hasattr(ans, "question_id"):
            qid = str(getattr(ans, "question_id", ""))
            a = getattr(ans, "answer", None)
            normalized_answers.append({"answer": str(a) if a is not None else "", "question_id": qid})
        else:
            normalized_answers.append({"answer": "", "question_id": str(ans)})

    sorted_answers = sorted(normalized_answers, key=lambda x: x["question_id"])

    payload = {
        "answers": sorted_answers,
        "cover_letter": cover_letter,
        "vacancy_stable_id": vacancy_stable_id,
    }
    canonical_str = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


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
    form_fingerprint: Optional[str] = None
    review_id: Optional[str] = None

    model_config = {"use_enum_values": False}

    @model_validator(mode="before")
    @classmethod
    def _migrate_fingerprint(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("form_fingerprint") and data.get("fingerprint"):
                data["form_fingerprint"] = data["fingerprint"]
        return data

    @property
    def fingerprint(self) -> Optional[str]:
        """Deprecated alias for form_fingerprint. Use form_fingerprint instead."""
        warnings.warn(
            "ApplicationReview.fingerprint is deprecated, use form_fingerprint",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.form_fingerprint

    @fingerprint.setter
    def fingerprint(self, value: Optional[str]) -> None:
        warnings.warn(
            "ApplicationReview.fingerprint is deprecated, use form_fingerprint",
            DeprecationWarning,
            stacklevel=2,
        )
        self.form_fingerprint = value

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
    if sess.status not in [BrowserStatus.READY_FOR_REVIEW, BrowserStatus.COMPLETED, BrowserStatus.BLOCKED, BrowserStatus.FORM_DETECTED]:
        raise ValueError(f"Browser status {sess.status} is not READY_FOR_REVIEW/FORM_DETECTED/BLOCKED - cannot review.")

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
        form_fingerprint=compute_review_fingerprint(vacancy_stable_id, pkg_json),
    )
    save_application_review(review)
    return review

def approve_review(vacancy_stable_id: str, note: str | None = None, force: bool = False) -> ApplicationReview:
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

    # Check package exists and compute fingerprint
    from .db import get_application_package
    pkg_row = get_application_package(vacancy_stable_id)
    if not pkg_row:
        raise ValueError(f"Application package not found for {vacancy_stable_id}: сначала подготовьте пакет")
    fp = compute_review_fingerprint(vacancy_stable_id, pkg_row)

    # Check browser status
    if not force:
        from .browser_executor import get_browser_session, BrowserStatus
        sess = get_browser_session(vacancy_stable_id)
        if sess and sess.status == BrowserStatus.BLOCKED:
            raise ValueError(f"Cannot approve: browser status {sess.status} is BLOCKED. BLOCKED cannot be approved.")
    # Check tracking still READY
    from .application_tracking import get_application_status, ApplicationStatus
    track = get_application_status(vacancy_stable_id)
    if track and track.status not in [ApplicationStatus.READY_TO_APPLY, ApplicationStatus.DISCOVERED, ApplicationStatus.ANALYZED]:
        raise ValueError(f"Cannot approve: tracking status {track.status} is not READY_TO_APPLY")

    if rev.status == ReviewStatus.APPROVED:
        modified = False
        if not rev.form_fingerprint or rev.form_fingerprint != fp:
            rev.form_fingerprint = fp
            modified = True
        if note and note != rev.note:
            rev.note = note
            modified = True
        if modified:
            rev.updated_at = _now()
            save_application_review(rev)
        return rev  # idempotent

    if rev.status == ReviewStatus.REJECTED:
        raise ValueError("Cannot approve: review already REJECTED")
    if rev.status == ReviewStatus.COMPLETED:
        raise ValueError("Cannot approve: review already COMPLETED")

    # Safety: never change tracking to APPLIED, never call browser submit
    rev.status = ReviewStatus.APPROVED
    rev.form_fingerprint = fp
    if note:
        rev.note = note
    rev.updated_at = _now()
    save_application_review(rev)

    # Sync validation_status in application_packages table upon human approval
    try:
        from .db import save_application_package
        if pkg_row and pkg_row[2]:
            pkg_data = json.loads(pkg_row[2])
            pkg_data["validation_status"] = "VALID"
            save_application_package(vacancy_stable_id, pkg_row[1], json.dumps(pkg_data, ensure_ascii=False))
    except Exception:
        pass

    return rev

def is_review_approved(vacancy_stable_id: str) -> bool:
    rev = get_application_review(vacancy_stable_id)
    return rev is not None and rev.status == ReviewStatus.APPROVED


def complete_review(vacancy_stable_id: str, note: str | None = None) -> Optional[ApplicationReview]:
    """Mark an approved review as consumed by a terminal application state."""
    rev = get_application_review(vacancy_stable_id)
    if not rev or rev.status == ReviewStatus.COMPLETED:
        return rev
    if rev.status != ReviewStatus.APPROVED:
        return rev
    rev.status = ReviewStatus.COMPLETED
    rev.note = note or rev.note or "Review consumed by completed application lifecycle"
    rev.updated_at = _now()
    save_application_review(rev)
    return rev


def reopen_review_after_browser_block(vacancy_stable_id: str, note: str | None = None) -> Optional[ApplicationReview]:
    """Revoke an approval when the latest browser preparation is blocked."""
    rev = get_application_review(vacancy_stable_id)
    if not rev or rev.status != ReviewStatus.APPROVED:
        return rev
    rev.status = ReviewStatus.PENDING_REVIEW
    rev.note = note or "Approval reopened because browser preparation is BLOCKED"
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
    if rev.status == ReviewStatus.COMPLETED:
        raise ValueError(f"Cannot reject: review already COMPLETED")
    rev.status = ReviewStatus.REJECTED
    rev.note = note
    rev.updated_at = _now()
    save_application_review(rev)
    return rev
