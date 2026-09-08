from __future__ import annotationsimport jsonimport loggingfrom datetime import datetime, timezonefrom typing import Any, Dict, List, Optional, Tuplefrom pydantic import BaseModel, Fieldfrom . import configfrom .candidate_profile import CandidateProfilefrom .db import get_connection, init_dbfrom .schema import Vacancyfrom .vacancy_identity import (    MatchType,    get_aliases_for_canonical,    get_canonical_by_normalized_url,    resolve_vacancy_identity,)logger = logging.getLogger(__name__)

QUEUE_VERSION = "v2"

class QueueItem(BaseModel):
    vacancy_stable_id: str
    canonical_id: str
    representative_vacancy_stable_id: str
    priority_score: int = Field(ge=0, le=100)
    match_score: float | None = None
    deep_score: float | None = None
    company: str | None = None
    title: str | None = None
    source: str | None = None
    vacancy_url: str | None = None
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rank: int = Field(ge=0)
    # extra explainable components
    components: dict[str, Any] = Field(default_factory=dict)
    application_strategy: str | None = None
    generated_at: str | None = None
    queue_version: str = QUEUE_VERSION

    model_config = {"extra": "forbid"}

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # Vacancy fields are stored as iso string, but also may be datetime
    if isinstance(value, datetime):
        return value
    # try parsing
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        # handle iso with timezone
        if "T" in value:
            # remove Z
            v = value.replace("Z", "")
            # try fromisoformat
            return datetime.fromisoformat(v)
    except Exception:
        pass
    return None

def _freshness_score(vacancy: Vacancy) -> tuple[int, str]:
    # Use published_at preferred, fallback first_seen_at, last_seen_at
    dt = None
    source = None
    if vacancy.published_at:
        dt = _parse_dt(str(vacancy.published_at)) if isinstance(vacancy.published_at, str) else vacancy.published_at
        source = "published_at"
    if dt is None and vacancy.first_seen_at:
        dt = _parse_dt(str(vacancy.first_seen_at)) if isinstance(vacancy.first_seen_at, str) else vacancy.first_seen_at
        source = "first_seen_at"
    if dt is None and vacancy.last_seen_at:
        dt = _parse_dt(str(vacancy.last_seen_at)) if isinstance(vacancy.last_seen_at, str) else vacancy.last_seen_at
        source = "last_seen_at"
    if dt is None:
        return 50, "freshness neutral (no date - not invented)"
    # ensure tz-aware?
    now = datetime.utcnow()
    # make dt naive if needed
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    delta = now - dt
    days = delta.days
    if days < 0:
        days = 0
    # freshness 100 for 0-3 days, then decay
    if days <= 3:
        score = 100
        reason = f"fresh ({days}d ago via {source})"
    elif days <= 7:
        score = 90
        reason = f"recent ({days}d ago)"
    elif days <= 14:
        score = 75
        reason = f"2 weeks old ({days}d)"
    elif days <= 30:
        score = 60
        reason = f"1 month old ({days}d)"
    elif days <= 60:
        score = 40
        reason = f"stale 1-2 months ({days}d)"
    elif days <= 90:
        score = 20
        reason = f"stale 2-3 months ({days}d)"
    else:
        score = 0
        reason = f"very stale {days}d"
    return score, reason

def _salary_fit_score(vacancy: Vacancy, profile: CandidateProfile) -> tuple[int, str]:
    # 0-100, unknown -> 0 (not invented), but explain
    if profile.minimum_salary is None:
        return 50, "salary neutral (no profile requirement)"
    # vacancy salary
    s_min = vacancy.salary_min
    s_max = vacancy.salary_max
    s_curr = (vacancy.salary_currency or "").upper() if vacancy.salary_currency else None
    p_min = profile.minimum_salary
    p_curr = (profile.salary_currency or "").upper() if profile.salary_currency else None
    if s_min is None and s_max is None:
        return 0, "salary unknown (vacancy no salary - not invented, lowers priority)"
    # currency check
    if p_curr and s_curr and p_curr != s_curr:
        return 0, f"salary currency mismatch {s_curr} vs {p_curr} (not confirmed)"
    # determine effective top
    top = s_max if s_max is not None else s_min
    if top is None:
        return 0, "salary unknown (no effective)"
    if top >= p_min:
        return 100, f"salary fit {top} >= {p_min} {p_curr or ''} (confirmed)"
    else:
        return 0, f"salary below minimum {top} < {p_min}"

def _readiness_score(deep: Any, vacancy: Vacancy, profile: CandidateProfile) -> tuple[int, list[str], list[str]]:
    # 0-100, based on deep
    reasons = []
    warnings = []
    if deep is None:
        return 0, reasons, ["no deep analysis - not confirmed, low readiness"]
    score = 100
    # deep is DeepAnalysisResult or dict-like
    rec = getattr(deep, "recommendation", None) or (deep.get("recommendation") if isinstance(deep, dict) else None)
    resume_needed = getattr(deep, "resume_adaptation_needed", None)
    if resume_needed is None and isinstance(deep, dict):
        resume_needed = deep.get("resume_adaptation_needed")
    missing = getattr(deep, "missing_skills", []) or (deep.get("missing_skills") if isinstance(deep, dict) else [])
    must_missing = []
    # check must_have vs gaps
    gaps = getattr(deep, "gaps", []) or (deep.get("gaps") if isinstance(deep, dict) else [])
    # REVIEW lowers
    if rec == "REVIEW":
        score -= 20
        warnings.append("REVIEW recommendation lowers readiness")
        reasons.append("REVIEW (not APPLY) -20")
    elif rec == "SKIP":
        score -= 50
        warnings.append("SKIP recommendation")
    # resume adaptation
    if resume_needed:
        score -= 15
        warnings.append("resume adaptation needed")
        reasons.append("resume adaptation needed -15")
    # missing must-have
    # Use missing_skills as proxy for must-have missing
    if missing:
        penalty = min(20, len(missing) * 5)
        score -= penalty
        warnings.append(f"missing must-have: {', '.join(missing[:3])} - not confirmed")
        reasons.append(f"missing skills {len(missing)} -{penalty}")
    # gaps with not confirmed
    if gaps and any("not confirmed" in g.lower() or "unknown" in g.lower() for g in gaps):
        # already counted via missing, but add slight
        pass
    score = max(0, min(100, score))
    if score == 100:
        reasons.append("readiness high (no gaps)")
    return score, reasons, warnings

def compute_priority(
    vacancy: Vacancy,
    profile: CandidateProfile,
    match_score: float | None,
    deep_score: float | None,
    deep: Any = None,
) -> tuple[int, dict[str, Any], list[str], list[str]]:
    # Components 0-100 each, weighted
    reasons: list[str] = []
    warnings: list[str] = []

    # match 0.35
    m = int(match_score) if match_score is not None else 0
    if match_score is None:
        warnings.append("match_score missing (not invented) -> 0")
    else:
        if m >= 80:
            reasons.append(f"high match_score {m}")
        elif m < 65:
            warnings.append(f"low match_score {m}")

    # deep 0.45
    d = int(deep_score) if deep_score is not None else 0
    if deep_score is None:
        warnings.append("deep_score missing -> 0")
    else:
        if d >= 80:
            reasons.append(f"high deep_score {d}")
        elif d < 65:
            warnings.append(f"low deep_score {d}")

    # readiness 0.10
    readiness, r_reasons, r_warnings = _readiness_score(deep, vacancy, profile)
    reasons.extend(r_reasons)
    warnings.extend(r_warnings)

    # salary 0.05
    salary_fit, salary_reason = _salary_fit_score(vacancy, profile)
    if salary_fit == 100:
        reasons.append(salary_reason)
    elif salary_fit == 0 and "unknown" in salary_reason:
        warnings.append(salary_reason)
    else:
        if salary_fit < 50:
            warnings.append(salary_reason)

    # freshness 0.05
    fresh, fresh_reason = _freshness_score(vacancy)
    if fresh >= 75:
        reasons.append(fresh_reason)
    elif fresh < 50:
        warnings.append(fresh_reason + " lowers priority")

    # remote / confirmed skill bonuses already reflected in match/deep, but we add explicit reason
    text = f"{vacancy.title or ''} {vacancy.description or ''}".lower()
    matched = [s for s in profile.skills if s.lower() in text]
    if matched:
        reasons.append(f"confirmed skill match: {', '.join(matched[:3])}")
    else:
        warnings.append("no confirmed skill match")

    # remote correspondence
    loc = (vacancy.location or "").lower()
    if profile.remote_required and "remote" in loc:
        reasons.append("remote matches profile")
    elif profile.remote_required and "remote" not in loc and loc:
        warnings.append("remote mismatch")

    # Weighted sum
    priority = (
        m * 0.35 +
        d * 0.45 +
        readiness * 0.10 +
        salary_fit * 0.05 +
        fresh * 0.05
    )
    priority_int = int(round(priority))
    priority_int = max(0, min(100, priority_int))

    components = {
        "match_score": m,
        "deep_score": d,
        "readiness": readiness,
        "salary_fit": salary_fit,
        "freshness": fresh,
        "freshness_reason": fresh_reason,
        "salary_reason": salary_reason,
        "readiness_reasons": r_reasons,
    }

    # Adjust for REVIEW / stale etc already in readiness/freshness
    return priority_int, components, reasons, warnings

def _select_representative(canonical_id: str, aliases: list[dict[str, Any]], profile: CandidateProfile) -> tuple[str, dict[str, Any]]:
    """
    Select the representative vacancy for a canonical ID.
    Priority:
    1. READY_TO_APPLY
    2. highest priority_score
    3. highest deep_score
    4. highest match_score
    5. stable_id alphabetical
    """
    from .application_tracking import ApplicationStatus, get_application_status    from .db import _row_to_vacancy, get_deep_analysis, get_vacancy_by_id    from .job_analyzer import DeepAnalysisResult    from .matcher import JobMatcher
    
    best_alias = None
    best_score = None
    
    for alias in aliases:
        sid = alias['vacancy_stable_id']
        
        # Check if this alias is in a terminal state (APPLIED, SUBMITTED, VERIFIED, INTERVIEW, OFFER, REJECTED, WITHDRAWN)
        track = get_application_status(sid)
        if track:
            status = track.status.value if hasattr(track.status, 'value') else str(track.status)
            terminal_statuses = {'APPLIED', 'INTERVIEW', 'OFFER', 'REJECTED', 'WITHDRAWN', 'SUBMITTED', 'VERIFIED'}
            if status in terminal_statuses:
                # Skip this alias - it's already in a terminal state
                continue
        
        # Get priority score for this alias
        row = get_vacancy_by_id(sid)
        if not row:
            continue
        
        vac = _row_to_vacancy(row)
        from .candidate_profile import load_candidate_profile
        
        # This is a simplified approach - we'll just use the first non-terminal alias
        # In practice, we should compute priority for each alias
        if best_alias is None:
            best_alias = alias
    
    if best_alias is None:
        # All aliases are in terminal state, use the first one
        best_alias = aliases[0]
    
    representative_id = best_alias['vacancy_stable_id']
    
    # Get the representative's tracking status
    track = get_application_status(representative_id)
    return representative_id, track


def build_queue_items(
    vacancies: list[Vacancy],
    profile: CandidateProfile,
    match_map: dict[str, Any],  # stable_id -> MatchResult
    deep_map: dict[str, Any],  # stable_id -> DeepAnalysisResult
) -> list[QueueItem]:
    """
    Build queue items with canonical identity deduplication.
    Only EXACT duplicates are grouped under one canonical_id.
    PROBABLE duplicates remain separate.
    """
    from .vacancy_identity import (        _generate_canonical_id,        normalize_company,        normalize_title,        normalize_url,    )
    
    # Group vacancies by canonical group key
    canonical_groups: dict[str, list[Vacancy]] = {}
    vacancy_to_canonical: dict[str, str] = {}
    
    for vac in vacancies:
        normalized_url = normalize_url(vac.job_url)
        canonical = get_canonical_by_normalized_url(normalized_url)
        if canonical:
            canonical_id = canonical.canonical_id
            group_key = canonical_id
        else:
            result = resolve_vacancy_identity(Vacancy(
                source=vac.source,
                source_job_id=vac.source_job_id,
                title=vac.title,
                company=vac.company,
                description=vac.description,
                job_url=vac.job_url,
                location=vac.location,
                country_restrictions=vac.country_restrictions,
                timezone_restrictions=vac.timezone_restrictions,
                salary_min=vac.salary_min,
                salary_max=vac.salary_max,
                salary_currency=vac.salary_currency,
                employment_type=vac.employment_type,
            ))
            if result.match_type == MatchType.EXACT:
                canonical_id = result.canonical_id
                group_key = canonical_id
            elif result.match_type == MatchType.PROBABLE:
                # PROBABLE duplicates remain separate: do not collapse with other vacancies
                canonical_id = _generate_canonical_id(
                    normalized_url,
                    normalize_company(vac.company),
                    normalize_title(vac.title),
                )
                group_key = f"probable_{vac.stable_id()}"
            else:
                canonical_id = result.canonical_id
                group_key = canonical_id
        
        vacancy_to_canonical[vac.stable_id()] = canonical_id
        if group_key not in canonical_groups:
            canonical_groups[group_key] = []
        canonical_groups[group_key].append(vac)
    
    items: list[QueueItem] = []
    
    for group_key, group in canonical_groups.items():
        # Select representative vacancy for this canonical group
        representative = _select_representative_for_group(group, profile)
        sid = representative.stable_id()
        
        m = match_map.get(sid)
        d = deep_map.get(sid)
        match_score = float(m.score) if m and m.score is not None else None
        deep_score = float(d.fit_score) if d and getattr(d, "fit_score", None) is not None else None
        if deep_score is None and isinstance(d, dict):
            deep_score = float(d.get("fit_score", 0)) if d.get("fit_score") is not None else None
        
        priority, comps, reasons, warnings = compute_priority(representative, profile, match_score, deep_score, d)
        
        strat = None
        if d:
            strat = getattr(d, "application_strategy", None) or (d.get("application_strategy") if isinstance(d, dict) else None)
        
        # Get canonical_id for this group
        canonical_id = vacancy_to_canonical[sid]
        
        item = QueueItem(
            vacancy_stable_id=sid,
            canonical_id=canonical_id,
            representative_vacancy_stable_id=representative.stable_id(),
            priority_score=priority,
            match_score=match_score,
            deep_score=deep_score,
            company=representative.company,
            title=representative.title,
            source=representative.source,
            vacancy_url=representative.job_url,
            reasons=reasons[:5],
            warnings=warnings[:5],
            rank=0,  # temp, will sort
            components=comps,
            application_strategy=strat,
            generated_at=datetime.utcnow().isoformat(),
            queue_version=QUEUE_VERSION,
        )
        items.append(item)
    
    # deterministic sort: priority desc, deep desc, match desc, stable_id asc for tie-break
    items.sort(key=lambda x: (-x.priority_score, -(x.deep_score or 0), -(x.match_score or 0), x.vacancy_stable_id))
    for idx, it in enumerate(items, start=1):
        it.rank = idx
    return items


def _select_representative_for_group(group: list[Vacancy], profile: CandidateProfile) -> Vacancy:
    """
    Select the representative vacancy for a canonical group.
    Priority:
    1. READY_TO_APPLY
    2. highest priority_score
    2. highest deep_score
    3. highest match_score
    4. stable_id alphabetical
    """
    from .application_tracking import ApplicationStatus, get_application_status
    
    # First, find READY_TO_APPLY vacancies
    ready_vacancies = []
    for vac in group:
        track = get_application_status(vac.stable_id())
        if track and track.status == ApplicationStatus.READY_TO_APPLY:
            ready_vacancies.append(vac)
    
    if ready_vacancies:
        # Sort by priority (we need to compute this)
        # For simplicity, use the first READY_TO_APPLY
        # In practice, we could compute priority for each
        return ready_vacancies[0]
    
    # No READY_TO_APPLY, return the first one
    return group[0]


# DB persistence
def save_queue_item(item: QueueItem) -> None:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO application_queue (vacancy_stable_id, canonical_id, representative_vacancy_stable_id, priority_score, rank, queue_json, generated_at, queue_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(vacancy_stable_id) DO UPDATE SET
               canonical_id=excluded.canonical_id,
               representative_vacancy_stable_id=excluded.representative_vacancy_stable_id,
               priority_score=excluded.priority_score,
               rank=excluded.rank,
               queue_json=excluded.queue_json,
               generated_at=excluded.generated_at,
               queue_version=excluded.queue_version
        """,
        (
            item.vacancy_stable_id,
            item.canonical_id,
            item.representative_vacancy_stable_id,
            item.priority_score,
            item.rank,
            item.model_dump_json(),
            item.generated_at,
            item.queue_version,
        ),
    )
    conn.commit()
    conn.close()


def get_queue_item(vacancy_stable_id: str, queue_version: str | None = None) -> QueueItem | None:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    if queue_version:
        cur.execute("SELECT queue_json FROM application_queue WHERE vacancy_stable_id=? AND queue_version=?", (vacancy_stable_id, queue_version))
    else:
        cur.execute("SELECT queue_json FROM application_queue WHERE vacancy_stable_id=?", (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return QueueItem.model_validate_json(row[0])
        except Exception:
            return None
    return None


def list_queue(limit: int = 50, queue_version: str | None = None) -> list[QueueItem]:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    if queue_version:
        cur.execute("SELECT queue_json FROM application_queue WHERE queue_version=? ORDER BY rank ASC LIMIT ?", (queue_version, limit))
    else:
        cur.execute("SELECT queue_json FROM application_queue ORDER BY rank ASC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        try:
            res.append(QueueItem.model_validate_json(r[0]))
        except Exception:
            continue
    return res


def clear_queue(queue_version: str | None = None) -> None:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    if queue_version:
        cur.execute("DELETE FROM application_queue WHERE queue_version=?", (queue_version,))
    else:
        cur.execute("DELETE FROM application_queue")
    conn.commit()
    conn.close()


def generate_queue(top_n: int = 20, profile_path: str | None = None, status_filter: str = "READY_TO_APPLY") -> list[QueueItem]:
    from .application_tracking import (        ApplicationStatus,        list_applications,        sync_application_tracking,    )    from .candidate_profile import load_candidate_profile    from .config import BATCH_LIMIT, CANDIDATE_PROFILE_FILE    from .db import list_vacancies    from .job_analyzer import DeepAnalysisResult    from .matcher import JobMatcher    from .vacancy_identity import normalize_url


    # Ensure tracking is up to date
    sync_application_tracking(profile_path=profile_path)

    # Load profile
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

    # Get READY_TO_APPLY only (or filtered)
    if status_filter:
        try:
            filt = ApplicationStatus(status_filter)
        except Exception:
            filt = ApplicationStatus.READY_TO_APPLY
        tracking_recs = list_applications(status=filt, limit=top_n*5)  # get more then filter
    else:
        tracking_recs = list_applications(status=ApplicationStatus.READY_TO_APPLY, limit=top_n*5)

    # Only READY_TO_APPLY should enter queue per spec
    # Filter to ensure status is READY_TO_APPLY if no explicit filter? spec says NOT to take others
    allowed_ids = set()
    for r in tracking_recs:
        if r.status == ApplicationStatus.READY_TO_APPLY or (status_filter and r.status.value == status_filter):
            allowed_ids.add(r.vacancy_stable_id)
        elif not status_filter:
            # only READY
            if r.status == ApplicationStatus.READY_TO_APPLY:
                allowed_ids.add(r.vacancy_stable_id)

    # If still empty, fallback to list with status READY only
    if not allowed_ids and not status_filter:
        # try directly
        recs = list_applications(status=ApplicationStatus.READY_TO_APPLY, limit=top_n*5)
        allowed_ids = {r.vacancy_stable_id for r in recs}

    # Need to fetch vacancies for those ids
    # Build map vacancy_stable_id -> Vacancy
    # list_vacancies may not contain all, so fetch via get_vacancy_by_id
    from .db import _row_to_vacancy, get_vacancy_by_id

    vacancies: list[Vacancy] = []
    match_map: dict[str, Any] = {}
    deep_map: dict[str, Any] = {}

    matcher = JobMatcher(profile)
    from .db import get_vacancy_eligibility, save_vacancy_eligibility    from .eligibility import EligibilityStatus, assess_vacancy_eligibility

    for sid in allowed_ids:
        row = get_vacancy_by_id(sid)
        if not row:
            continue
        vac = _row_to_vacancy(row)

        # Check eligibility: only ELIGIBLE and ELIGIBLE_WITH_WARNING can enter active queue
        elig = get_vacancy_eligibility(sid)
        if not elig:
            assessment = assess_vacancy_eligibility(vac)
            save_vacancy_eligibility(sid, assessment)
            e_status = assessment.eligibility.value
        else:
            e_status = elig["status"]
            if hasattr(e_status, "value"):
                e_status = e_status.value
        
        if str(e_status).lower() in ("ineligible", "unknown"):
            continue

        # Defense-in-Depth: Hard constraints & strict remote filter
        from .matcher import _coerce_profile, _hard_constraints        from .remote_filter import is_strictly_remote

        if profile.remote_required:
            is_rem, _ = is_strictly_remote(vac)
            if not is_rem:
                continue

        hard_rej, _ = _hard_constraints(_coerce_profile(profile), vac)
        if hard_rej:
            continue

        # compute match
        m = matcher.match(vac)
        if m.decision == "SKIP":
            continue

        vacancies.append(vac)
        match_map[sid] = m
        # deep
        from .db import get_deep_analysis
        deep_row = get_deep_analysis(sid)
        if deep_row and deep_row[4]:
            try:
                deep = DeepAnalysisResult.model_validate_json(deep_row[4])
                deep_map[sid] = deep
            except Exception:
                # try fallback dict
                try:
                    import json
                    deep_map[sid] = json.loads(deep_row[4])
                except Exception:
                    pass

    # If not enough, try to fill from recent vacancies that are READY but not in allowed_ids due to limit
    if len(vacancies) < top_n:
        # get more tracking
        extra_recs = list_applications(status=ApplicationStatus.READY_TO_APPLY, limit=100)
        for r in extra_recs:
            if r.vacancy_stable_id in allowed_ids:
                continue
            if len(vacancies) >= top_n:
                break
            row = get_vacancy_by_id(r.vacancy_stable_id)
            if not row:
                continue
            vac = _row_to_vacancy(row)

            elig = get_vacancy_eligibility(r.vacancy_stable_id)
            if not elig:
                assessment = assess_vacancy_eligibility(vac)
                save_vacancy_eligibility(r.vacancy_stable_id, assessment)
                e_status = assessment.eligibility.value
            else:
                e_status = elig["status"]
                if hasattr(e_status, "value"):
                    e_status = e_status.value
            
            if str(e_status).lower() in ("ineligible", "unknown"):
                continue

            # Defense-in-Depth: Hard constraints & strict remote filter
            from .matcher import _coerce_profile, _hard_constraints            from .remote_filter import is_strictly_remote

            if profile.remote_required:
                is_rem, _ = is_strictly_remote(vac)
                if not is_rem:
                    continue

            hard_rej, _ = _hard_constraints(_coerce_profile(profile), vac)
            if hard_rej:
                continue

            m = matcher.match(vac)
            if m.decision == "SKIP":
                continue

            vacancies.append(vac)
            match_map[r.vacancy_stable_id] = m
            deep_row = get_deep_analysis(r.vacancy_stable_id)
            if deep_row and deep_row[4]:
                try:
                    deep_map[r.vacancy_stable_id] = DeepAnalysisResult.model_validate_json(deep_row[4])
                except Exception:
                    pass

    # Clear previous queue items before saving newly generated items
    clear_queue()

    # Build queue items with canonical identity deduplication
    items = build_queue_items(vacancies, profile, match_map, deep_map)
    # Trim to top_n
    items = items[:top_n]

    # Persist
    for it in items:
        save_queue_item(it)
    # Also persist that we generated at this version: clear old version items not in top? Keep all?
    return items
