from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any, Tuple

from pydantic import BaseModel, Field

from . import config
from .db import get_connection, init_db

# Keep consistent with spec order
class ApplicationStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    READY_TO_APPLY = "READY_TO_APPLY"
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    WITHDRAWN = "WITHDRAWN"

# Allowed transitions per spec
_ALLOWED: Dict[ApplicationStatus, List[ApplicationStatus]] = {
    ApplicationStatus.DISCOVERED: [ApplicationStatus.ANALYZED],
    ApplicationStatus.ANALYZED: [ApplicationStatus.READY_TO_APPLY],
    ApplicationStatus.READY_TO_APPLY: [ApplicationStatus.SUBMITTED],
    ApplicationStatus.SUBMITTED: [ApplicationStatus.VERIFIED, ApplicationStatus.READY_TO_APPLY],
    ApplicationStatus.VERIFIED: [ApplicationStatus.APPLIED, ApplicationStatus.SUBMITTED],
    ApplicationStatus.APPLIED: [ApplicationStatus.REJECTED, ApplicationStatus.INTERVIEW],
    ApplicationStatus.INTERVIEW: [ApplicationStatus.OFFER, ApplicationStatus.REJECTED],
    # OFFER, REJECTED are terminal except WITHDRAWN via universal rule
    ApplicationStatus.OFFER: [],
    ApplicationStatus.REJECTED: [],
    ApplicationStatus.WITHDRAWN: [],
}

# Statuses that are considered manual / should not be auto-overwritten by sync
MANUAL_STATUSES = {
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.VERIFIED,
    ApplicationStatus.APPLIED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.OFFER,
    ApplicationStatus.WITHDRAWN,
}

def _now_iso() -> str:
    return datetime.utcnow().isoformat()

class ApplicationRecord(BaseModel):
    vacancy_stable_id: str
    status: ApplicationStatus
    company: Optional[str] = None
    title: Optional[str] = None
    source: Optional[str] = None
    vacancy_url: Optional[str] = None
    match_score: Optional[float] = None
    deep_score: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    applied_at: Optional[str] = None
    last_status_change_at: Optional[str] = None
    notes: Optional[str] = None

    model_config = {"use_enum_values": False}

class HistoryRecord(BaseModel):
    id: int
    vacancy_stable_id: str
    old_status: Optional[str]
    new_status: str
    changed_at: str
    note: Optional[str] = None

def _is_valid_transition(old: ApplicationStatus, new: ApplicationStatus) -> bool:
    if old == new:
        return True  # idempotent, allowed
    # universal WITHDRAWN from any except already WITHDRAWN
    if new == ApplicationStatus.WITHDRAWN and old != ApplicationStatus.WITHDRAWN:
        return True
    allowed = _ALLOWED.get(old, [])
    return new in allowed

def _row_to_record(row: Tuple) -> ApplicationRecord:
    # row order matches table columns
    return ApplicationRecord(
        vacancy_stable_id=row[0],
        status=ApplicationStatus(row[1]),
        company=row[2],
        title=row[3],
        source=row[4],
        vacancy_url=row[5],
        match_score=row[6],
        deep_score=row[7],
        created_at=row[8],
        updated_at=row[9],
        applied_at=row[10],
        last_status_change_at=row[11],
        notes=row[12],
    )

def get_application_status(vacancy_stable_id: str) -> Optional[ApplicationRecord]:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT vacancy_stable_id, status, company, title, source, vacancy_url, match_score, deep_score, created_at, updated_at, applied_at, last_status_change_at, notes FROM application_tracking WHERE vacancy_stable_id=?", (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return _row_to_record(row)
    return None

def set_application_status(
    vacancy_stable_id: str,
    status: ApplicationStatus | str,
    company: Optional[str] = None,
    title: Optional[str] = None,
    source: Optional[str] = None,
    vacancy_url: Optional[str] = None,
    match_score: Optional[float] = None,
    deep_score: Optional[float] = None,
    notes: Optional[str] = None,
    # allow passing vacancy object like dict? we handle caller
) -> ApplicationRecord:
    # Direct set without validation (for sync). Creates or updates.
    if isinstance(status, str):
        status = ApplicationStatus(status)
    init_db()
    now = _now_iso()
    existing = get_application_status(vacancy_stable_id)
    conn = get_connection()
    cur = conn.cursor()
    if existing:
        # update, keep created_at, update updated_at and maybe other fields if provided
        cur.execute(
            """UPDATE application_tracking SET status=?, company=COALESCE(?, company), title=COALESCE(?, title), source=COALESCE(?, source), vacancy_url=COALESCE(?, vacancy_url),
               match_score=COALESCE(?, match_score), deep_score=COALESCE(?, deep_score), updated_at=?, last_status_change_at=?, notes=COALESCE(?, notes)
               WHERE vacancy_stable_id=?""",
            (
                status.value,
                company,
                title,
                source,
                vacancy_url,
                match_score,
                deep_score,
                now,
                now,
                notes,
                vacancy_stable_id,
            ),
        )
        # if moving to APPLIED, set applied_at if not set
        if status == ApplicationStatus.APPLIED:
            cur.execute("UPDATE application_tracking SET applied_at=COALESCE(applied_at, ?) WHERE vacancy_stable_id=?", (now, vacancy_stable_id))
        conn.commit()
        conn.close()
        return get_application_status(vacancy_stable_id)  # type: ignore
    else:
        # create
        applied_at = now if status == ApplicationStatus.APPLIED else None
        cur.execute(
            """INSERT INTO application_tracking (vacancy_stable_id, status, company, title, source, vacancy_url, match_score, deep_score, created_at, updated_at, applied_at, last_status_change_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vacancy_stable_id,
                status.value,
                company,
                title,
                source,
                vacancy_url,
                match_score,
                deep_score,
                now,
                now,
                applied_at,
                now,
                notes,
            ),
        )
        conn.commit()
        conn.close()
        # also create history entry for creation? transition handles history, but set should also log history if needed - we will create history via separate insert if needed
        # For set, we also insert history if not exists? Ensure history for creation
        conn2 = get_connection()
        cur2 = conn2.cursor()
        cur2.execute(
            "INSERT INTO application_status_history (vacancy_stable_id, old_status, new_status, changed_at, note) VALUES (?, ?, ?, ?, ?)",
            (vacancy_stable_id, None, status.value, now, notes),
        )
        conn2.commit()
        conn2.close()
        return get_application_status(vacancy_stable_id)  # type: ignore

def transition_application(
    vacancy_stable_id: str,
    new_status: ApplicationStatus | str,
    note: Optional[str] = None,
    company: Optional[str] = None,
    title: Optional[str] = None,
    source: Optional[str] = None,
    vacancy_url: Optional[str] = None,
    match_score: Optional[float] = None,
    deep_score: Optional[float] = None,
) -> ApplicationRecord:
    if isinstance(new_status, str):
        try:
            new_status = ApplicationStatus(new_status)
        except ValueError:
            raise ValueError(f"Invalid status: {new_status}")
    init_db()
    existing = get_application_status(vacancy_stable_id)
    if not existing:
        raise ValueError(f"No tracking record for {vacancy_stable_id}, cannot transition")
    old_status = existing.status
    if old_status == new_status:
        # idempotent, no duplicate history, return existing
        return existing
    if not _is_valid_transition(old_status, new_status):
        raise ValueError(f"Invalid transition {old_status.value} -> {new_status.value}")
    now = _now_iso()
    conn = get_connection()
    cur = conn.cursor()
    # update tracking
    cur.execute(
        """UPDATE application_tracking SET status=?, updated_at=?, last_status_change_at=?, notes=COALESCE(?, notes),
           company=COALESCE(?, company), title=COALESCE(?, title), source=COALESCE(?, source), vacancy_url=COALESCE(?, vacancy_url),
           match_score=COALESCE(?, match_score), deep_score=COALESCE(?, deep_score)
           WHERE vacancy_stable_id=?""",
        (
            new_status.value,
            now,
            now,
            note,
            company,
            title,
            source,
            vacancy_url,
            match_score,
            deep_score,
            vacancy_stable_id,
        ),
    )
    if new_status == ApplicationStatus.APPLIED:
        cur.execute("UPDATE application_tracking SET applied_at=COALESCE(applied_at, ?) WHERE vacancy_stable_id=?", (now, vacancy_stable_id))
    if new_status == ApplicationStatus.VERIFIED:
        cur.execute("UPDATE application_tracking SET verified_at=COALESCE(verified_at, ?) WHERE vacancy_stable_id=?", (now, vacancy_stable_id))
    # history
    cur.execute(
        "INSERT INTO application_status_history (vacancy_stable_id, old_status, new_status, changed_at, note) VALUES (?, ?, ?, ?, ?)",
        (vacancy_stable_id, old_status.value, new_status.value, now, note),
    )
    conn.commit()
    conn.close()
    return get_application_status(vacancy_stable_id)  # type: ignore


def verify_and_apply(
    vacancy_stable_id: str,
    verification_status: str,
    note: Optional[str] = None,
) -> ApplicationRecord:
    """
    Transition based on verification result:
    - VERIFIED -> SUBMITTED -> VERIFIED -> APPLIED
    - FAILED/AMBIGUOUS/BLOCKED -> SUBMITTED -> READY_TO_APPLY (or stay SUBMITTED for review)
    """
    init_db()
    existing = get_application_status(vacancy_stable_id)
    if not existing:
        raise ValueError(f"No tracking record for {vacancy_stable_id}")
    
    old_status = existing.status
    
    if verification_status == "VERIFIED":
        # SUBMITTED -> VERIFIED -> APPLIED
        if old_status == ApplicationStatus.SUBMITTED:
            transition_application(vacancy_stable_id, ApplicationStatus.VERIFIED, note=note)
            return transition_application(vacancy_stable_id, ApplicationStatus.APPLIED, note=note)
        elif old_status == ApplicationStatus.VERIFIED:
            return transition_application(vacancy_stable_id, ApplicationStatus.APPLIED, note=note)
        else:
            raise ValueError(f"Invalid state for VERIFIED: {old_status.value}")
    elif verification_status in ("FAILED", "AMBIGUOUS", "BLOCKED"):
        # SUBMITTED -> READY_TO_APPLY (for retry) or stay SUBMITTED
        if old_status == ApplicationStatus.SUBMITTED:
            return transition_application(vacancy_stable_id, ApplicationStatus.READY_TO_APPLY, note=note)
        else:
            raise ValueError(f"Invalid state for {verification_status}: {old_status.value}")
    else:
        raise ValueError(f"Unknown verification status: {verification_status}")


def list_applications(status: Optional[str | ApplicationStatus] = None, limit: int = 100, order_by: str = "updated_at") -> List[ApplicationRecord]:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    allowed_order = {"updated_at", "created_at", "status", "match_score", "deep_score"}
    if order_by not in allowed_order:
        order_by = "updated_at"
    if status:
        if isinstance(status, ApplicationStatus):
            status_val = status.value
        else:
            # try enum, fallback to string
            try:
                status_val = ApplicationStatus(str(status)).value
            except Exception:
                status_val = str(status)
        cur.execute(f"SELECT vacancy_stable_id, status, company, title, source, vacancy_url, match_score, deep_score, created_at, updated_at, applied_at, last_status_change_at, notes FROM application_tracking WHERE status=? ORDER BY {order_by} DESC LIMIT ?", (status_val, limit))
    else:
        cur.execute(f"SELECT vacancy_stable_id, status, company, title, source, vacancy_url, match_score, deep_score, created_at, updated_at, applied_at, last_status_change_at, notes FROM application_tracking ORDER BY {order_by} DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [_row_to_record(r) for r in rows]

def get_application_history(vacancy_stable_id: str) -> List[HistoryRecord]:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, vacancy_stable_id, old_status, new_status, changed_at, note FROM application_status_history WHERE vacancy_stable_id=? ORDER BY changed_at ASC, id ASC", (vacancy_stable_id,))
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append(HistoryRecord(id=r[0], vacancy_stable_id=r[1], old_status=r[2], new_status=r[3], changed_at=r[4], note=r[5]))
    return res

def sync_application_tracking(profile_path: Optional[str] = None) -> Dict[str, int]:
    """
    Sync tracking with current pipeline data.
    - new vacancies with matcher APPLY/REVIEW -> DISCOVERED
    - if deep exists -> ANALYZED
    - if package exists -> READY_TO_APPLY
    - Do NOT overwrite manual statuses APPLIED/REJECTED/INTERVIEW/OFFER/WITHDRAWN
    Returns {Created, Updated, Unchanged}
    """
    from .candidate_profile import load_candidate_profile
    from .matcher import JobMatcher
    from .db import list_vacancies, get_deep_analysis, get_application_package
    from .config import BATCH_LIMIT, CANDIDATE_PROFILE_FILE

    init_db()
    # load profile
    if profile_path:
        profile = load_candidate_profile(profile_path)
    else:
        cfg_path = CANDIDATE_PROFILE_FILE if CANDIDATE_PROFILE_FILE and CANDIDATE_PROFILE_FILE.strip() else None
        if cfg_path:
            try:
                profile = load_candidate_profile(cfg_path)
            except Exception:
                from .candidate_profile import load_candidate_profile as lcp
                profile = lcp()
        else:
            from .candidate_profile import load_candidate_profile as lcp
            profile = lcp()

    matcher = JobMatcher(profile)

    rows = list_vacancies(limit=BATCH_LIMIT * 20)
    # Need to reconstruct Vacancy objects for matcher
    from .db import _row_to_vacancy

    created = 0
    updated = 0
    unchanged = 0

    for row in rows:
        vac = _row_to_vacancy(row)
        m = matcher.match(vac)
        if m.decision not in ("APPLY", "REVIEW"):
            # Only track APPLY/REVIEW
            continue

        sid = vac.stable_id()
        existing = get_application_status(sid)

        # Determine desired status based on deep/package
        deep_row = get_deep_analysis(sid)
        pkg_row = get_application_package(sid)

        # Determine target status
        target = ApplicationStatus.DISCOVERED
        if pkg_row:
            target = ApplicationStatus.READY_TO_APPLY
        elif deep_row:
            target = ApplicationStatus.ANALYZED
        else:
            target = ApplicationStatus.DISCOVERED

        # Extract scores
        match_score = float(m.score) if m.score is not None else None
        deep_score = None
        if deep_row:
            # deep_row is tuple: vacancy_stable_id, analyzer_version, fit_score, recommendation, analysis_json, analyzed_at
            deep_score = float(deep_row[2]) if deep_row[2] is not None else None
        # else None

        if not existing:
            # Create stepwise: always start DISCOVERED then promote
            set_application_status(
                vacancy_stable_id=sid,
                status=ApplicationStatus.DISCOVERED,
                company=vac.company,
                title=vac.title,
                source=vac.source,
                vacancy_url=vac.job_url,
                match_score=match_score,
                deep_score=deep_score,
                notes=None,
            )
            created += 1
            if target != ApplicationStatus.DISCOVERED:
                # promote stepwise to target
                try:
                    if target in (ApplicationStatus.ANALYZED, ApplicationStatus.READY_TO_APPLY):
                        transition_application(sid, ApplicationStatus.ANALYZED)
                        if target == ApplicationStatus.READY_TO_APPLY:
                            transition_application(sid, ApplicationStatus.READY_TO_APPLY)
                    else:
                        transition_application(sid, target)
                    # Update scores after promotion
                    set_application_status(sid, target, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, match_score=match_score, deep_score=deep_score)
                except Exception:
                    # fallback set directly
                    set_application_status(sid, target, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, match_score=match_score, deep_score=deep_score)
            else:
                # already DISCOVERED, ensure scores up to date (already set)
                pass
        else:
            # If manual status, don't auto-update
            if existing.status in [s.value for s in MANUAL_STATUSES] or existing.status in MANUAL_STATUSES:
                # Check if existing.status is enum or string
                cur_status = existing.status if isinstance(existing.status, ApplicationStatus) else ApplicationStatus(existing.status)
                if cur_status in MANUAL_STATUSES:
                    unchanged += 1
                    continue

            # Compare desired vs current
            # Only promote, not demote
            order = {
                ApplicationStatus.DISCOVERED: 0,
                ApplicationStatus.ANALYZED: 1,
                ApplicationStatus.READY_TO_APPLY: 2,
            }
            cur_status = existing.status if isinstance(existing.status, ApplicationStatus) else ApplicationStatus(existing.status)
            cur_ord = order.get(cur_status, -1)
            tgt_ord = order.get(target, -1)
            if tgt_ord > cur_ord:
                # Need to transition stepwise
                # Find path
                # For DISCOVERED->ANALYZED->READY
                try:
                    if cur_status == ApplicationStatus.DISCOVERED and target in (ApplicationStatus.ANALYZED, ApplicationStatus.READY_TO_APPLY):
                        transition_application(sid, ApplicationStatus.ANALYZED)
                        if target == ApplicationStatus.READY_TO_APPLY:
                            transition_application(sid, ApplicationStatus.READY_TO_APPLY)
                    elif cur_status == ApplicationStatus.ANALYZED and target == ApplicationStatus.READY_TO_APPLY:
                        transition_application(sid, ApplicationStatus.READY_TO_APPLY)
                    else:
                        # generic valid
                        transition_application(sid, target)
                    # Update scores even if status already higher, we still update scores via transition's coalesce
                    # Already done via transition, but we should also update match/deep scores
                    set_application_status(sid, target, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, match_score=match_score, deep_score=deep_score)
                    updated += 1
                except Exception:
                    # if invalid, just update scores without status change
                    set_application_status(sid, cur_status, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, match_score=match_score, deep_score=deep_score)
                    updated += 1
            else:
                # No status change needed, but update scores if changed
                # Check if scores differ
                if existing.match_score != match_score or existing.deep_score != deep_score:
                    set_application_status(sid, cur_status, company=vac.company, title=vac.title, source=vac.source, vacancy_url=vac.job_url, match_score=match_score, deep_score=deep_score)
                    updated += 1
                else:
                    unchanged += 1

    return {"Created": created, "Updated": updated, "Unchanged": unchanged}

