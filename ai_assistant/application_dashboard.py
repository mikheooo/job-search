from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from . import config
from .db import get_connection, init_db
from .application_tracking import ApplicationStatus, get_application_status, list_applications, get_application_history
from .application_queue import generate_queue, get_queue_item, QUEUE_VERSION
from .application_review import get_application_review, ReviewStatus
from .browser_executor import get_browser_session, BrowserStatus
from .vacancy_identity import (
    resolve_vacancy_identity,
    get_canonical_by_id,
    get_canonical_by_normalized_url,
    get_all_canonical_vacancies,
    get_aliases_for_canonical,
    normalize_url,
    normalize_company,
    normalize_title,
    MatchType,
    IdentityMatch,
    CanonicalVacancy,
)
from .db import (
    get_connection, init_db,
    list_vacancies, get_deep_analysis, get_application_package,
    get_submission, get_all_submissions,
    get_verification, list_verifications,
    _row_to_vacancy,
)

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    PREPARE_BROWSER = "PREPARE_BROWSER"
    REVIEW_APPLICATION = "REVIEW_APPLICATION"
    SUBMIT_WITH_CONFIRMATION = "SUBMIT_WITH_CONFIRMATION"
    VERIFY_SUBMISSION = "VERIFY_SUBMISSION"
    REVIEW_SUBMISSION = "REVIEW_SUBMISSION"
    RECONCILE_TO_APPLIED = "RECONCILE_TO_APPLIED"
    NO_ACTION = "NO_ACTION"


@dataclass
class QueueSummary:
    vacancy_stable_id: str
    canonical_id: str
    rank: int
    priority_score: int
    match_score: Optional[float]
    deep_score: Optional[float]
    company: str
    title: str
    status: str
    warnings: List[str] = field(default_factory=list)
    alias_count: int = 1


@dataclass
class ActionItem:
    canonical_id: str
    representative_vacancy_stable_id: str
    alias_count: int
    company: str
    title: str
    current_status: str
    action: ActionType
    reason: str
    priority: int = 0
    match_score: Optional[float] = None
    deep_score: Optional[float] = None


@dataclass
class ApplicationDashboard:
    generated_at: str
    total_vacancies: int = 0
    total_aliases: int = 0
    suppressed_exact_aliases: int = 0
    probable_duplicates: int = 0
    # Pipeline counts (canonical level)
    discovered: int = 0
    analyzed: int = 0
    ready_to_apply: int = 0
    pending_review: int = 0
    approved: int = 0
    submitted: int = 0
    verified: int = 0
    applied: int = 0
    rejected: int = 0
    interview: int = 0
    offer: int = 0
    withdrawn: int = 0
    # Submission verification counts
    blocked: int = 0
    ambiguous: int = 0
    failed: int = 0
    # Queue stats
    queue_size: int = 0
    top_priority: int = 0
    average_match: float = 0.0
    average_deep: float = 0.0
    average_priority: float = 0.0
    # Action items
    action_items: List[ActionItem] = field(default_factory=list)


def _get_all_tracking() -> List[Any]:
    """Get all application tracking records."""
    return list_applications(limit=10000)


def _get_all_queue_items() -> List[Any]:
    """Get all queue items with summary data."""
    items = generate_queue(top_n=1000, status_filter="READY_TO_APPLY")
    summaries = []
    for item in items:
        warnings = []
        if item.application_strategy:
            warnings.extend([str(w) for w in item.warnings])
        summaries.append(QueueSummary(
            vacancy_stable_id=item.vacancy_stable_id,
            canonical_id=item.canonical_id,
            rank=item.rank or 0,
            priority_score=item.priority_score or 0,
            match_score=item.match_score,
            deep_score=item.deep_score,
            company=item.company or "",
            title=item.title or "",
            status="READY_TO_APPLY",
            warnings=item.warnings if isinstance(item.warnings, list) else [str(w) for w in item.warnings],
            alias_count=1,  # Will be updated when we group by canonical
        ))
    return summaries


def _get_all_canonical_groups() -> Dict[str, List[Tuple[Any, str]]]:
    """
    Get all tracking records grouped by canonical_id.
    Returns dict: canonical_id -> List[(tracking_record, vacancy_stable_id)]
    """
    all_tracking = _get_all_tracking()
    canonical_groups: Dict[str, List[Tuple[Any, str]]] = {}
    
    for track in all_tracking:
        # Resolve canonical identity for this vacancy
        vacancy_stable_id = track.vacancy_stable_id
        
        # Get canonical_id from database or resolve
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT canonical_id FROM vacancy_aliases WHERE vacancy_stable_id=?", (vacancy_stable_id,))
        row = cur.fetchone()
        conn.close()
        
        if row:
            canonical_id = row[0]
        else:
            # Resolve canonical identity
            from .vacancy_identity import resolve_vacancy_identity
            from .db import get_vacancy_by_id, _row_to_vacancy
            
            vac_row = get_vacancy_by_id(vacancy_stable_id)
            if vac_row:
                vac = _row_to_vacancy(vac_row)
                result = resolve_vacancy_identity(vac)
                canonical_id = result.canonical_id
            else:
                canonical_id = f"unknown_{vacancy_stable_id}"
        
        if canonical_id not in canonical_groups:
            canonical_groups[canonical_id] = []
        canonical_groups[canonical_id].append((track, vacancy_stable_id))
    
    return canonical_groups


def _get_canonical_status(canonical_id: str, aliases: List[Tuple[Any, str]]) -> str:
    """
    Determine the effective lifecycle status for a canonical vacancy.
    Priority: OFFER > INTERVIEW > APPLIED > VERIFIED > SUBMITTED > APPROVED > PENDING_REVIEW > READY_TO_APPLY > ANALYZED > DISCOVERED > REJECTED > WITHDRAWN
    """
    # Status priority (higher = more advanced)
    status_priority = {
        "OFFER": 11,
        "INTERVIEW": 10,
        "APPLIED": 9,
        "VERIFIED": 8,
        "SUBMITTED": 7,
        "APPROVED": 6,
        "PENDING_REVIEW": 5,
        "READY_TO_APPLY": 4,
        "ANALYZED": 3,
        "DISCOVERED": 2,
        "REJECTED": 1,
        "WITHDRAWN": 0,
    }
    
    best_status = "DISCOVERED"
    best_priority = -1
    
    for track, _ in aliases:
        status = track.status.value if hasattr(track.status, 'value') else str(track.status)
        priority = status_priority.get(status, -1)
        if priority > best_priority:
            best_priority = priority
            best_status = status
    
    return best_status


def _get_canonical_alias_count(canonical_id: str) -> int:
    """Get number of aliases for a canonical vacancy."""
    from .vacancy_identity import get_aliases_for_canonical
    aliases = get_aliases_for_canonical(canonical_id)
    return len(aliases)


def _get_canonical_representative(canonical_id: str, aliases: List[Tuple[Any, str]]) -> Tuple[Any, str]:
    """
    Select representative vacancy for a canonical group.
    Priority: 1. READY_TO_APPLY, 2. highest priority_score, 2. highest deep_score, 3. highest match_score, 4. stable_id alphabetical
    """
    # Priority: READY_TO_APPLY first
    ready_aliases = []
    for track, sid in aliases:
        status = track.status.value if hasattr(track.status, 'value') else str(track.status)
        if status == "READY_TO_APPLY":
            ready_aliases.append((track, sid))
    
    if ready_aliases:
        return ready_aliases[0]
    
    # If no READY_TO_APPLY, return first
    return aliases[0]


def build_dashboard() -> ApplicationDashboard:
    """Build complete application dashboard from existing data (canonical-aware)."""
    init_db()
    
    dashboard = ApplicationDashboard(
        generated_at=datetime.utcnow().isoformat(),
    )
    
    # Get all tracking records
    all_tracking = _get_all_tracking()
    dashboard.total_vacancies = len(all_tracking)
    
    # Group by canonical_id
    canonical_groups = _get_all_canonical_groups()
    dashboard.total_aliases = len(all_tracking)
    
    # Count canonical opportunities and suppressed aliases
    suppressed_exact = 0
    probable_duplicates = 0
    
    # Group canonicals by their match type
    exact_duplicate_canonicals = 0
    probable_duplicate_canonicals = 0
    
    # First, we need to get canonical vacancies and their aliases to determine match types
    from .vacancy_identity import get_all_canonical_vacancies, get_aliases_for_canonical, MatchType
    
    all_canonical = get_all_canonical_vacancies()
    canonical_alias_counts: Dict[str, int] = {}
    canonical_exact_alias_counts: Dict[str, int] = {}
    canonical_probable_alias_counts: Dict[str, int] = {}
    
    for canon in all_canonical:
        aliases = get_aliases_for_canonical(canon.canonical_id)
        canonical_alias_counts[canon.canonical_id] = len(aliases)
        exact_count = sum(1 for a in aliases if a['match_type'] == MatchType.EXACT.value)
        probable_count = sum(1 for a in aliases if a['match_type'] == MatchType.PROBABLE.value)
        canonical_exact_alias_counts[canon.canonical_id] = exact_count
        canonical_probable_alias_counts[canon.canonical_id] = probable_count
    
    # Calculate suppressed exact aliases (EXACT aliases beyond the first)
    for canon_id, alias_count in canonical_alias_counts.items():
        exact_count = canonical_exact_alias_counts.get(canon_id, 0)
        if exact_count > 1:
            suppressed_exact += (exact_count - 1)
    
    probable_duplicates = sum(canonical_probable_alias_counts.values())
    
    # Now process canonical groups for dashboard counts
    status_counts = {}
    match_scores = []
    deep_scores = []
    priority_scores = []
    
    for canonical_id, aliases in canonical_groups.items():
        # Get canonical status (highest priority among aliases)
        canonical_status = _get_canonical_status(canonical_id, aliases)
        status_counts[canonical_status] = status_counts.get(canonical_status, 0) + 1
        
        # Get match/deep scores from the first alias
        if aliases:
            track, _ = aliases[0]
            if track.match_score is not None:
                match_scores.append(track.match_score)
            if track.deep_score is not None:
                deep_scores.append(track.deep_score)
    
    # Map to dashboard fields
    dashboard.discovered = status_counts.get("DISCOVERED", 0)
    dashboard.analyzed = status_counts.get("ANALYZED", 0)
    dashboard.ready_to_apply = status_counts.get("READY_TO_APPLY", 0)
    dashboard.pending_review = status_counts.get("PENDING_REVIEW", 0)
    dashboard.approved = status_counts.get("APPROVED", 0)
    dashboard.submitted = status_counts.get("SUBMITTED", 0)
    dashboard.verified = status_counts.get("VERIFIED", 0)
    dashboard.applied = status_counts.get("APPLIED", 0)
    dashboard.rejected = status_counts.get("REJECTED", 0)
    dashboard.interview = status_counts.get("INTERVIEW", 0)
    dashboard.offer = status_counts.get("OFFER", 0)
    dashboard.withdrawn = status_counts.get("WITHDRAWN", 0)
    
    # Suppressed exact aliases and probable duplicates
    dashboard.suppressed_exact_aliases = suppressed_exact
    dashboard.probable_duplicates = probable_duplicates
    
    # Averages
    dashboard.average_match = sum(match_scores) / len(match_scores) if match_scores else 0.0
    dashboard.average_deep = sum(deep_scores) / len(deep_scores) if deep_scores else 0.0
    
    # Queue stats
    queue_items = _get_all_queue_items()
    dashboard.queue_size = len(queue_items)
    dashboard.top_priority = max((q.priority_score for q in queue_items), default=0)
    dashboard.average_priority = sum(q.priority_score for q in queue_items) / len(queue_items) if queue_items else 0.0
    
    # Submission verification counts
    all_verifications = list_verifications(limit=10000)
    ver_status_counts = {}
    for ver in all_verifications:
        status = ver.verification_status.value if hasattr(ver.verification_status, 'value') else str(ver.verification_status)
        ver_status_counts[status] = ver_status_counts.get(status, 0) + 1
    
    dashboard.blocked = ver_status_counts.get("BLOCKED", 0)
    dashboard.ambiguous = ver_status_counts.get("AMBIGUOUS", 0)
    dashboard.failed = ver_status_counts.get("FAILED", 0)
    
    # Build action items at canonical level
    dashboard.action_items = _build_canonical_action_items(canonical_groups)
    
    return dashboard


def _build_canonical_action_items(canonical_groups: Dict[str, List[Tuple[Any, str]]]) -> List[ActionItem]:
    """Build action items at canonical level."""
    actions = []
    
    # Map verifications by canonical_id
    all_verifications = list_verifications(limit=10000)
    ver_by_canonical: Dict[str, str] = {}
    for ver in all_verifications:
        ver_status = ver.verification_status.value if hasattr(ver.verification_status, 'value') else str(ver.verification_status)
        # Need to get canonical_id for this verification
        from .db import get_submission
        sub_row = get_submission(ver.vacancy_stable_id, ver.submission_id)
        if sub_row:
            try:
                sub_json = json.loads(sub_row[3]) if sub_row[3] else {}
                submission_id = sub_json.get("submission_id")
                # Find canonical_id for this submission
                from .vacancy_identity import get_aliases_for_canonical
                # We need to find which canonical this submission belongs to
                # For simplicity, use the first alias's canonical_id
                pass
            except Exception:
                pass
    
    # Map queue items by canonical_id
    queue_items = _get_all_queue_items()
    queue_by_canonical: Dict[str, Any] = {}
    for q in queue_items:
        canonical_id = q.canonical_id
        if canonical_id not in queue_by_canonical or q.priority_score > queue_by_canonical[canonical_id].priority_score:
            queue_by_canonical[canonical_id] = q
    
    # Process each canonical group
    for canonical_id, aliases in canonical_groups.items():
        # Skip if canonical has terminal state
        canonical_status = _get_canonical_status(canonical_id, aliases)
        terminal_statuses = {"APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"}
        if canonical_status in terminal_statuses:
            continue
        
        # Get verification status
        # Find the most recent verification for this canonical
        ver_status = None
        for ver in list_verifications(limit=10000):
            # Check if this verification belongs to an alias in this canonical group
            for track, sid in aliases:
                if track.vacancy_stable_id == ver.vacancy_stable_id:
                    ver_status = ver.verification_status.value if hasattr(ver.verification_status, 'value') else str(ver.verification_status)
                    break
            if ver_status:
                break
        
        # Determine action
        representative_track, representative_sid = _get_canonical_representative(canonical_id, aliases)
        representative_status = representative_track.status.value if hasattr(representative_track.status, 'value') else str(representative_track.status)
        
        action = _determine_canonical_action(
            canonical_id=canonical_id,
            tracking_status=representative_status,
            verification_status=ver_status,
            aliases=aliases,
        )
        
        if action != ActionType.NO_ACTION:
            # Get priority from queue
            queue_item = None
            # Find queue item for this canonical
            for track, sid in aliases:
                if sid in {q.vacancy_stable_id for q in _get_all_queue_items()}:
                    for q in _get_all_queue_items():
                        if q.vacancy_stable_id == sid:
                            queue_item = q
                            break
                if queue_item:
                    break
            
            priority = queue_item.priority_score if queue_item else 0
            match_score = queue_item.match_score if queue_item else None
            deep_score = queue_item.deep_score if queue_item else None
            
            # Get company and title from representative
            company = ""
            title = ""
            for track, sid in aliases:
                if track.company:
                    company = track.company
                    title = track.title
                    break
            
            actions.append(ActionItem(
                canonical_id=canonical_id,
                representative_vacancy_stable_id=representative_sid,
                alias_count=len(aliases),
                company=company,
                title=title,
                current_status=representative_status,
                action=action,
                reason=_action_reason(action, representative_status, ver_status),
                priority=priority,
                match_score=match_score,
                deep_score=deep_score,
            ))
    
    # Sort by priority descending
    actions.sort(key=lambda a: a.priority, reverse=True)
    return actions


def _determine_canonical_action(
    canonical_id: str,
    tracking_status: str,
    verification_status: Optional[str],
    aliases: List[Tuple[Any, str]],
) -> ActionType:
    """Determine required action for a canonical vacancy based on its state."""
    
    # Check browser preparation status for any alias
    browser_prepared = False
    for track, sid in aliases:
        browser_sess = get_browser_session(sid)
        if browser_sess and browser_sess.status == BrowserStatus.READY_FOR_REVIEW:
            browser_prepared = True
            break
    
    # Check review status for any alias
    review_status = None
    for track, sid in aliases:
        review = get_application_review(sid)
        if review and hasattr(review.status, 'value'):
            review_status = review.status.value
            break
        elif review:
            review_status = str(review.status)
            break
    
    # Check if any alias is in terminal state
    terminal_statuses = {"APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"}
    has_terminal = False
    for track, _ in aliases:
        status = track.status.value if hasattr(track.status, 'value') else str(track.status)
        if status in terminal_statuses:
            return ActionType.NO_ACTION
    
    # Decision tree
    # Get representative tracking status
    representative_status = tracking_status  # This should be the canonical status
    
    if representative_status == "READY_TO_APPLY":
        if not browser_prepared:
            return ActionType.PREPARE_BROWSER
        # Check review status
        review = None
        for track, sid in aliases:
            review = get_application_review(sid)
            if review:
                break
        review_status = review.status.value if review and hasattr(review.status, 'value') else (str(review.status) if review else None)
        if review_status in ("PENDING_REVIEW", "READY_FOR_REVIEW"):
            return ActionType.REVIEW_APPLICATION
        return ActionType.NO_ACTION
    
    if representative_status in ("PENDING_REVIEW", "READY_FOR_REVIEW"):
        return ActionType.REVIEW_APPLICATION
    
    if representative_status == "APPROVED":
        # Check if ready for submit
        browser_prepared = False
        for track, sid in aliases:
            browser_sess = get_browser_session(sid)
            if browser_sess and browser_sess.status == BrowserStatus.READY_FOR_REVIEW:
                browser_prepared = True
                break
        if browser_prepared:
            return ActionType.SUBMIT_WITH_CONFIRMATION
        return ActionType.NO_ACTION
    
    if representative_status == "SUBMITTED":
        if not verification_status:
            return ActionType.VERIFY_SUBMISSION
        if verification_status in ("AMBIGUOUS", "FAILED", "BLOCKED"):
            return ActionType.REVIEW_SUBMISSION
        if verification_status == "VERIFIED":
            return ActionType.RECONCILE_TO_APPLIED
        return ActionType.NO_ACTION
    
    if representative_status == "VERIFIED":
        return ActionType.RECONCILE_TO_APPLIED
    
    return ActionType.NO_ACTION


def _action_reason(action: ActionType, tracking_status: str, verification_status: Optional[str]) -> str:
    """Get human-readable reason for action."""
    reasons = {
        ActionType.PREPARE_BROWSER: f"READY_TO_APPLY but browser not prepared",
        ActionType.REVIEW_APPLICATION: f"Status is {tracking_status} - human review required",
        ActionType.SUBMIT_WITH_CONFIRMATION: "APPROVED + browser READY_FOR_REVIEW - ready for controlled submit",
        ActionType.VERIFY_SUBMISSION: "SUBMITTED but no verification performed",
        ActionType.REVIEW_SUBMISSION: f"SUBMITTED + {verification_status or 'UNKNOWN'} verification - manual review required",
        ActionType.RECONCILE_TO_APPLIED: "VERIFIED but tracking not yet APPLIED - run reconcile",
        ActionType.NO_ACTION: "No action required",
    }
    return reasons.get(action, "")


def get_dashboard_show(vacancy_stable_id: str) -> Optional[Dict[str, Any]]:
    """Get detailed view for a single vacancy (canonical-aware)."""
    init_db()
    
    # Resolve canonical identity first
    from .db import get_vacancy_by_id
    from .db import _row_to_vacancy
    from .vacancy_identity import resolve_vacancy_identity, get_aliases_for_canonical
    
    row = get_vacancy_by_id(vacancy_stable_id)
    if not row:
        return None
    from .db import _row_to_vacancy
    vac = _row_to_vacancy(row)
    
    # Resolve canonical identity
    result = resolve_vacancy_identity(vac)
    canonical_id = result.canonical_id
    
    # Get all aliases for this canonical
    aliases = get_aliases_for_canonical(canonical_id)
    
    detail = {
        "canonical_id": canonical_id,
        "vacancy_stable_id": vacancy_stable_id,
        "company": vac.company,
        "title": vac.title,
        "job_url": vac.job_url,
        "source": vac.source,
        "location": vac.location,
        "salary_min": vac.salary_min,
        "salary_max": vac.salary_max,
        "salary_currency": vac.salary_currency,
        "employment_type": vac.employment_type,
    }
    
    # Get canonical vacancy info
    from .vacancy_identity import get_canonical_by_id
    canon = get_canonical_by_id(canonical_id)
    if canon:
        detail["canonical"] = {
            "canonical_id": canon.canonical_id,
            "normalized_url": canon.normalized_url,
            "normalized_company": canon.normalized_company,
            "normalized_title": canon.normalized_title,
            "location": canon.location,
            "first_seen_at": canon.first_seen_at,
            "last_seen_at": canon.last_seen_at,
        }
    
    # Aliases
    aliases = get_aliases_for_canonical(canonical_id)
    detail["aliases"] = []
    for alias in aliases:
        detail["aliases"].append({
            "vacancy_stable_id": alias['vacancy_stable_id'],
            "source": alias['source'],
            "source_url": alias['source_url'],
            "match_type": alias['match_type'],
            "confidence": alias['confidence'],
        })
    detail["alias_count"] = len(aliases)
    
    # Tracking (effective status)
    from .vacancy_identity import get_aliases_for_canonical
    from .application_tracking import get_application_status, ApplicationStatus
    
    canonical_groups = _get_all_canonical_groups()
    canonical_id = result.canonical_id
    aliases = canonical_groups.get(canonical_id, [])
    
    # Determine effective status
    effective_status = _get_canonical_status(canonical_id, canonical_groups.get(canonical_id, []))
    detail["tracking"] = {
        "effective_status": effective_status,
        "alias_statuses": [
            {
                "vacancy_stable_id": sid,
                "status": track.status.value if hasattr(track.status, 'value') else str(track.status)
            }
            for track, sid in canonical_groups.get(canonical_id, [])
        ],
    }
    
    # Deep analysis
    deep_row = get_deep_analysis(vacancy_stable_id)
    if deep_row:
        detail["deep_analysis"] = {
            "analyzer_version": deep_row[1],
            "fit_score": deep_row[2],
            "recommendation": deep_row[3],
            "analyzed_at": deep_row[5],
        }
    
    # Queue
    queue_item = get_queue_item(vacancy_stable_id, queue_version=QUEUE_VERSION)
    if queue_item:
        detail["queue"] = {
            "rank": queue_item.rank,
            "priority_score": queue_item.priority_score,
            "match_score": queue_item.match_score,
            "deep_score": queue_item.deep_score,
            "reasons": queue_item.reasons,
            "warnings": queue_item.warnings,
            "application_strategy": queue_item.application_strategy,
            "components": queue_item.components,
        }
    
    # Application package
    pkg_row = get_application_package(vacancy_stable_id)
    if pkg_row:
        detail["application_package"] = {
            "generator_version": pkg_row[1],
            "created_at": pkg_row[3],
            "prepared": True,
        }
        try:
            pkg_data = json.loads(pkg_row[2]) if pkg_row[2] else {}
            detail["application_package"].update({
                "resume_adaptation": pkg_data.get("resume_adaptation"),
                "cover_letter": pkg_data.get("cover_letter"),
                "tailored_skills": pkg_data.get("tailored_skills"),
            })
        except Exception:
            pass
    
    # Browser preparation
    browser_sess = get_browser_session(vacancy_stable_id)
    if browser_sess:
        detail["browser"] = {
            "status": browser_sess.status.value if hasattr(browser_sess.status, 'value') else str(browser_sess.status),
            "form_detected": browser_sess.form_detected,
            "fields_detected": browser_sess.fields_detected,
            "fields_filled": browser_sess.fields_filled,
            "screenshot_path": browser_sess.screenshot_path,
            "final_url": browser_sess.final_url,
            "page_title": browser_sess.page_title,
        }
    
    # Review
    review = get_application_review(vacancy_stable_id)
    if review:
        detail["review"] = {
            "status": review.status.value if hasattr(review.status, 'value') else str(review.status),
            "note": review.note,
            "application_strategy": review.application_strategy,
        }
    
    # Submissions
    all_subs = get_all_submissions(vacancy_stable_id)
    if all_subs:
        detail["submissions"] = []
        for sub in all_subs:
            sub_data = json.loads(sub[3]) if sub[3] else {}
            detail["submissions"].append({
                "submission_id": sub[1],
                "executor_version": sub[2],
                "status": sub[4],
                "submitted_at": sub[5],
                "created_at": sub[6],
                "updated_at": sub[7],
                "submission_json": sub_data,
            })
        detail["submissions_count"] = len(all_subs)
        last = all_subs[-1]
        last_data = json.loads(last[3]) if last[3] else {}
        detail["last_submission"] = {
            "submission_id": last[1],
            "status": last[4],
            "submitted_at": last[5],
        }
    
    # Verifications
    ver = get_verification(vacancy_stable_id, detail.get("last_submission", {}).get("submission_id") if detail.get("last_submission") else None)
    if ver:
        detail["verification"] = {
            "status": ver.verification_status.value if hasattr(ver.verification_status, 'value') else str(ver.verification_status),
            "verified_at": ver.verified_at,
            "success_signal": ver.success_signal,
            "final_url": ver.final_url,
            "page_title": ver.page_title,
            "screenshot_path": ver.screenshot_path,
            "evidence": ver.evidence,
            "warnings": ver.warnings,
        }
    
    # Tracking history
    history = get_application_history(vacancy_stable_id)
    if history:
        detail["timeline"] = []
        for h in history:
            detail["timeline"].append({
                "changed_at": h.changed_at,
                "old_status": h.old_status,
                "new_status": h.new_status,
                "note": h.note,
            })
    
    # Action (for this specific alias)
    action = _determine_action(
        vacancy_stable_id,
        detail.get("tracking", {}).get("effective_status", "UNKNOWN"),
        detail.get("verification", {}).get("status"),
        None,
        None
    )
    detail["action"] = {
        "action": action.value,
        "reason": _action_reason(action, tracking_status=detail.get("tracking", {}).get("effective_status", ""), verification_status=detail.get("verification", {}).get("status")),
    }
    
    return detail


def get_dashboard_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent lifecycle events across all vacancies."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT vacancy_stable_id, old_status, new_status, changed_at, note
        FROM application_status_history
        ORDER BY changed_at DESC, id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    
    events = []
    for row in rows:
        events.append({
            "vacancy_stable_id": row[0],
            "old_status": row[1] or "NONE",
            "new_status": row[2],
            "changed_at": row[3],
            "note": row[4] or "",
        })
    return events


def get_dashboard_queue() -> List[QueueSummary]:
    """Get queue summary for dashboard."""
    return _get_all_queue_items()


def get_dashboard_actions_only() -> List[ActionItem]:
    """Get only action items from dashboard."""
    dashboard = build_dashboard()
    return dashboard.action_items


# Legacy functions for backward compatibility
def _build_action_items(
    all_tracking: List[Any],
    queue_items: List[QueueSummary],
    all_verifications: List[Any]
) -> List[ActionItem]:
    """Legacy function for backward compatibility - builds action items at individual vacancy level."""
    actions = []
    
    # Map verifications by vacancy
    ver_by_vacancy = {}
    for ver in all_verifications:
        ver_status = ver.verification_status.value if hasattr(ver.verification_status, 'value') else str(ver.verification_status)
        ver_by_vacancy[ver.vacancy_stable_id] = ver_status
    
    # Map queue items by vacancy
    queue_by_vacancy = {q.vacancy_stable_id: q for q in queue_items}
    
    # Map tracking by vacancy
    tracking_by_vacancy = {}
    for track in all_tracking:
        tracking_by_vacancy[track.vacancy_stable_id] = track
    
    # Process each tracking record
    for track in all_tracking:
        vid = track.vacancy_stable_id
        status = track.status.value if hasattr(track.status, 'value') else str(track.status)
        company = track.company or ""
        title = track.title or ""
        
        # Get verification status if exists
        ver_status = ver_by_vacancy.get(vid)
        
        # Determine action
        action = _determine_action(vid, status, ver_status, queue_by_vacancy.get(vid), track)
        
        if action != ActionType.NO_ACTION:
            # Get priority from queue
            queue_item = queue_by_vacancy.get(vid)
            priority = queue_item.priority_score if queue_item else 0
            
            actions.append(ActionItem(
                canonical_id="",  # legacy field
                representative_vacancy_stable_id=vid,
                alias_count=1,
                company=company,
                title=title,
                current_status=status,
                action=action,
                reason=_action_reason(action, status, ver_status),
                priority=priority,
                match_score=queue_item.match_score if queue_item else None,
                deep_score=queue_item.deep_score if queue_item else None,
            ))
    
    # Sort by priority descending
    actions.sort(key=lambda a: a.priority, reverse=True)
    return actions


def _determine_action(
    vacancy_stable_id: str,
    tracking_status: str,
    verification_status: Optional[str],
    queue_item: Optional[QueueSummary],
    track: Any
) -> ActionType:
    """Legacy function for backward compatibility - determines action for a single vacancy."""
    
    # Check browser preparation status
    browser_sess = get_browser_session(vacancy_stable_id)
    browser_status = browser_sess.status.value if browser_sess and hasattr(browser_sess.status, 'value') else (str(browser_sess.status) if browser_sess else None)
    
    # Check review status
    review = get_application_review(vacancy_stable_id)
    review_status = review.status.value if review and hasattr(review.status, 'value') else (str(review.status) if review else None)
    
    # Decision tree
    if tracking_status == "READY_TO_APPLY":
        if not browser_sess or browser_status != "READY_FOR_REVIEW":
            return ActionType.PREPARE_BROWSER
        if review_status in ("PENDING_REVIEW", "READY_FOR_REVIEW"):
            return ActionType.REVIEW_APPLICATION
        return ActionType.NO_ACTION
    
    if tracking_status in ("PENDING_REVIEW", "READY_FOR_REVIEW"):
        return ActionType.REVIEW_APPLICATION
    
    if tracking_status == "APPROVED":
        # Check if ready for submit
        browser_sess = get_browser_session(vacancy_stable_id)
        if browser_sess and browser_sess.status.value == "READY_FOR_REVIEW":
            return ActionType.SUBMIT_WITH_CONFIRMATION
        return ActionType.NO_ACTION
    
    if tracking_status == "SUBMITTED":
        if not verification_status:
            return ActionType.VERIFY_SUBMISSION
        if verification_status == "AMBIGUOUS":
            return ActionType.REVIEW_SUBMISSION
        if verification_status == "FAILED":
            return ActionType.REVIEW_SUBMISSION
        if verification_status == "BLOCKED":
            return ActionType.REVIEW_SUBMISSION
        if verification_status == "VERIFIED":
            return ActionType.RECONCILE_TO_APPLIED
        return ActionType.NO_ACTION
    
    if tracking_status == "VERIFIED":
        return ActionType.RECONCILE_TO_APPLIED
    
    # Terminal states
    if tracking_status in ("APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"):
        return ActionType.NO_ACTION
    
    return ActionType.NO_ACTION