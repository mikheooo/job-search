"""FastAPI backend application for Job-Search Dashboard."""

from __future__ import annotations

import atexit
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import config
from ..application_queue import list_queue
from ..application_review import approve_review, get_application_review, reject_review
from ..application_tracking import (
    get_application_status,
    transition_application,
)
from ..cli import SOURCES, collect
from ..db import (
    _row_to_vacancy,
    get_application_package,
    get_connection,
    get_deep_analysis,
    get_vacancy_by_id,
    init_db,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    if not is_loopback_host(host):
        logging.getLogger(__name__).warning(
            "Binding to non-loopback host %s. Enforcing dashboard token authentication for all reads and mutations.",
            host,
        )
    # T7.24 §6: the UI is a long-lived owner, so it must give up its browser on the
    # way out - normal exit, exception and Ctrl+C all end here. ``atexit`` covers
    # the paths that skip the ASGI shutdown (a hard kill of the process).
    atexit.register(_release_ui_browser, "lifespan/atexit")
    try:
        yield
    finally:
        _release_ui_browser("shutdown")


def _release_ui_browser(why: str) -> dict[str, Any]:
    """Release the browser this UI owns, and only what it owns.

    Never raises: a shutdown hook that can throw turns a clean stop into a crash.
    """
    try:
        from .. import hh_browser_lease as lease

        result = lease.release_owner(lease.OWNER_UI)
        result["why"] = why
        if result.get("released"):
            logging.getLogger(__name__).info("released the HH browser: %s", result)
        return result
    except Exception as exc:  # noqa: BLE001 - a hook may not fail a shutdown
        logging.getLogger(__name__).debug("browser release failed: %s", exc)
        return {"released": False, "why": why, "error": f"{type(exc).__name__}: {exc}"}


app = FastAPI(title="Job Search Hub", version="1.0.0", lifespan=lifespan)

def _dashboard_cors_origins() -> list:
    raw = os.getenv("DASHBOARD_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # Same-origin dashboard needs no CORS; default allows loopback on 8000 and 8001.
    return ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:8001", "http://127.0.0.1:8001"]


from .auth import (
    MUTATING_METHODS as _MUTATING_METHODS,
    dashboard_require_auth_for_reads as _dashboard_require_auth_for_reads,
    dashboard_token_ok as _dashboard_token_ok,
    get_dashboard_token as _dashboard_token,
    is_loopback_host,
    request_dashboard_token as _request_dashboard_token,
    require_dashboard_token_middleware as require_dashboard_token,
)

__all__ = [
    "_MUTATING_METHODS",
    "_dashboard_require_auth_for_reads",
    "_dashboard_token",
    "_dashboard_token_ok",
    "_request_dashboard_token",
    "app",
    "is_loopback_host",
    "require_dashboard_token",
]

# Re-apply middleware
app.middleware("http")(require_dashboard_token)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_dashboard_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


STATIC_DIR = Path(__file__).parent / "static"


class MoveStatusRequest(BaseModel):
    vacancy_stable_id: str
    new_status: str
    note: str | None = None


class ReviewRequest(BaseModel):
    action: str  # "approve" | "reject"
    note: str | None = None


class CollectRequest(BaseModel):
    sources: list[str] | None = None


class ResolveTaskRequest(BaseModel):
    task_id: int
    status: str = "RESOLVED"
    note: str | None = None


@app.get("/api/autopilot/status")
def get_autopilot_status() -> dict[str, Any]:
    init_db()
    conn = get_connection()
    c = conn.cursor()

    # 1. Blockers / Actions required from user
    c.execute("""
        SELECT id, task_key, priority, title, description, action_url, status, created_at, updated_at
        FROM user_action_tasks
        WHERE status = 'PENDING'
        ORDER BY id DESC
    """)
    cols = ["id", "task_key", "priority", "title", "description", "action_url", "status", "created_at", "updated_at"]
    blockers = [dict(zip(cols, r)) for r in c.fetchall()]

    # 2. Submitted proposals (Kwork / freelance)
    c.execute("""
        SELECT id, gig_id, source, title, budget_text, proposal_pitch, status, updated_at
        FROM top_gig_proposals
        WHERE status = 'SUBMITTED'
        ORDER BY updated_at DESC
    """)
    p_cols = ["id", "gig_id", "source", "title", "budget_text", "proposal_pitch", "status", "updated_at"]
    submitted_gigs = [dict(zip(p_cols, r)) for r in c.fetchall()]

    # 3. Thailand prepared applications
    thailand_apps = []
    root_dir = Path(__file__).resolve().parents[2]
    th_file = root_dir / "data" / "thailand_applications.json"
    if th_file.exists():
        try:
            thailand_apps = json.loads(th_file.read_text(encoding="utf-8"))
        except Exception:
            thailand_apps = []

    # 4. Hot employer communications (HH)
    hot_chats = []
    try:
        c.execute("""
            SELECT conversation_id, sender, snippet, classification, status, created_at
            FROM hh_message_events
            WHERE classification IN ('NEEDS_REPLY', 'INTERVIEW_INVITATION', 'RECRUITER_QUESTION')
            ORDER BY id DESC LIMIT 5
        """)
        m_cols = ["conversation_id", "sender", "snippet", "classification", "status", "created_at"]
        hot_chats = [dict(zip(m_cols, r)) for r in c.fetchall()]
    except Exception:
        hot_chats = []

    # 5. Model roles & Live queue tasks (from user_action_tasks)
    roles = {
        "worker": {"name": "CodeBuddy", "model": "hy4-preview (Tencent Hunyuan 4, Free)", "status": "READY"},
        "qa": {"name": "Antigravity", "model": "gemini-3.8-flash-high", "status": "ACTIVE"},
        "reviewer": {"name": "OpenCode", "model": "muse-spark-1.3 (Free)", "status": "READY"},
        "manager": {"name": "Antigravity Lead", "model": "gemini-3.8-flash-high", "status": "ACTIVE"}
    }

    queue_tasks: list[dict[str, Any]] = []
    try:
        c.execute("""
            SELECT id, task_key, priority, title, description, action_url, status, created_at, updated_at
            FROM user_action_tasks
            ORDER BY CASE status WHEN 'ACTIVE' THEN 1 WHEN 'IN_PROGRESS' THEN 2 WHEN 'QUEUED' THEN 3 ELSE 4 END, id DESC
            LIMIT 20
        """)
        t_cols = ["id", "task_key", "priority", "title", "description", "action_url", "status", "created_at", "updated_at"]
        db_tasks = [dict(zip(t_cols, r)) for r in c.fetchall()]
        for t in db_tasks:
            queue_tasks.append({
                "id": str(t["id"]),
                "title": t["title"] or t["task_key"] or f"Task #{t['id']}",
                "status": t["status"] or "QUEUED",
                "details": t["description"] or "",
            })
    except Exception:
        queue_tasks = []

    conn.close()

    return {
        "blockers": blockers,
        "submitted_gigs": submitted_gigs,
        "thailand_applications": thailand_apps,
        "hot_chats": hot_chats,
        "roles": roles,
        "queue_tasks": queue_tasks
    }


@app.post("/api/autopilot/resolve-task")
def resolve_autopilot_task(req: ResolveTaskRequest) -> dict[str, Any]:
    init_db()
    conn = get_connection()
    c = conn.cursor()
    from datetime import datetime
    now_iso = datetime.utcnow().isoformat()
    c.execute("UPDATE user_action_tasks SET status = ?, updated_at = ? WHERE id = ?", (req.status, now_iso, req.task_id))
    conn.commit()
    conn.close()
    return {"status": "success", "task_id": req.task_id, "new_status": req.status}


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM vacancies")
    total_vacancies = cursor.fetchone()[0]

    cursor.execute("SELECT source, COUNT(*) FROM vacancies GROUP BY source")
    by_source = dict(cursor.fetchall())

    cursor.execute("SELECT state, COUNT(*) FROM vacancies GROUP BY state")
    by_state = dict(cursor.fetchall())

    cursor.execute("SELECT COUNT(*) FROM application_packages")
    total_packages = cursor.fetchone()[0]

    cursor.execute("SELECT status, COUNT(*) FROM application_tracking GROUP BY status")
    by_tracking = dict(cursor.fetchall())

    cursor.execute("SELECT status, COUNT(*) FROM vacancy_eligibility GROUP BY status")
    by_eligibility = dict(cursor.fetchall())

    bf_metrics = {}
    try:
        from ..business_funnel import get_business_funnel_metrics
        bf_metrics = get_business_funnel_metrics()
    except Exception:
        pass

    return {
        "total_vacancies": total_vacancies,
        "by_source": by_source,
        "by_state": by_state,
        "total_packages": total_packages,
        "tracking_status": by_tracking,
        "eligibility": by_eligibility,
        "business_funnel": bf_metrics,
    }


@app.get("/api/business-funnel")
def get_business_funnel() -> dict[str, Any]:
    from ..business_funnel import get_business_funnel_metrics
    return get_business_funnel_metrics()


@app.get("/api/funnel-analytics")
def get_detailed_funnel_analytics() -> dict[str, Any]:
    from ..funnel_analytics import format_funnel_report, get_funnel_metrics
    from ..business_funnel import get_business_funnel_metrics
    metrics = get_funnel_metrics()
    business_funnel = get_business_funnel_metrics()
    return {
        "metrics": metrics,
        "business_funnel": business_funnel,
        "formatted_report": format_funnel_report(),
    }


@app.get("/api/top-gigs")
def get_top_gigs() -> list[dict[str, Any]]:
    init_db()
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT gig_id, source, title, budget_text, contact, deeplink, url, proposal_pitch, status, created_at
        FROM top_gig_proposals
        ORDER BY id ASC
    """)
    rows = c.fetchall()
    conn.close()
    cols = ["gig_id", "source", "title", "budget_text", "contact", "deeplink", "url", "proposal_pitch", "status", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


@app.get("/api/vacancies")
def get_vacancies(
    limit: int = Query(50, ge=1, le=500),
    source: str | None = None,
    search: str | None = None,
    min_score: float | None = None,
    eligibility: str | None = None,
) -> list[dict[str, Any]]:
    init_db()
    from ..db import get_all_vacancy_eligibilities
    elig_map = get_all_vacancy_eligibilities()

    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT v.*, ve.status as elig_status, ve.reasons_json as elig_reasons FROM vacancies v LEFT JOIN vacancy_eligibility ve ON v.stable_id = ve.vacancy_stable_id WHERE 1=1"
    params: list[Any] = []

    if source:
        query += " AND v.source = ?"
        params.append(source)
    if search:
        query += " AND (v.title LIKE ? OR v.company LIKE ? OR v.description LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    if min_score is not None:
        query += " AND v.match_score >= ?"
        params.append(min_score)
    if eligibility and eligibility.lower() != "all":
        query += " AND LOWER(COALESCE(ve.status, 'unknown')) = ?"
        params.append(eligibility.lower().strip())

    query += " ORDER BY v.published_at DESC, v.first_seen_at DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    results = []
    for r in rows:
        vac = _row_to_vacancy(r)
        e_info = elig_map.get(vac.stable_id(), {})
        d = {
            "stable_id": vac.stable_id(),
            "source": vac.source,
            "title": vac.title,
            "company": vac.company,
            "location": vac.location,
            "salary_min": vac.salary_min,
            "salary_max": vac.salary_max,
            "salary_currency": vac.salary_currency,
            "job_url": vac.job_url,
            "published_at": str(vac.published_at) if vac.published_at else None,
            "match_score": getattr(r, "match_score", None) if hasattr(r, "match_score") else (r[20] if len(r) > 20 else None),
            "match_decision": getattr(r, "match_decision", None) if hasattr(r, "match_decision") else (r[21] if len(r) > 21 else None),
            "eligibility_status": e_info.get("status", "unknown"),
            "eligibility_reasons": e_info.get("reasons", []),
        }
        results.append(d)
    return results


@app.get("/api/queue")
def get_queue_items(top: int = 50) -> list[dict[str, Any]]:
    init_db()
    from ..db import get_vacancy_eligibility
    items = list_queue(limit=top)
    results = []
    for it in items:
        review = get_application_review(it.vacancy_stable_id)
        review_status = review.status.value if (review and hasattr(review.status, "value")) else (str(review.status) if review else "PENDING")
        e_info = get_vacancy_eligibility(it.vacancy_stable_id) or {}
        results.append({
            "vacancy_stable_id": it.vacancy_stable_id,
            "priority_score": it.priority_score,
            "rank": it.rank,
            "title": it.title,
            "company": it.company,
            "source": it.source,
            "job_url": it.vacancy_url,
            "match_score": it.match_score,
            "deep_score": it.deep_score,
            "review_status": review_status,
            "reasons": it.reasons,
            "warnings": it.warnings,
            "eligibility_status": e_info.get("status", "eligible"),
            "eligibility_reasons": e_info.get("reasons", []),
        })
    return results


@app.get("/api/package/{vacancy_stable_id}")
def get_package_detail(vacancy_stable_id: str) -> dict[str, Any]:
    init_db()
    pkg_tuple = get_application_package(vacancy_stable_id)
    vac_row = get_vacancy_by_id(vacancy_stable_id)
    vac = _row_to_vacancy(vac_row) if vac_row else None
    deep_row = get_deep_analysis(vacancy_stable_id)
    review = get_application_review(vacancy_stable_id)

    pkg_data = None
    if pkg_tuple:
        _sid, version, raw_json, updated_at = pkg_tuple
        try:
            pkg_data = json.loads(raw_json)
        except Exception:
            pkg_data = {"raw": raw_json}

    deep_data = None
    if deep_row:
        if isinstance(deep_row, tuple):
            fit_score = deep_row[2] if len(deep_row) > 2 else None
            recommendation = deep_row[3] if len(deep_row) > 3 else None
            raw_analysis = deep_row[4] if len(deep_row) > 4 else None
            parsed_analysis = {}
            if raw_analysis:
                try:
                    parsed_analysis = json.loads(raw_analysis) if isinstance(raw_analysis, str) else raw_analysis
                except Exception:
                    parsed_analysis = {}
            deep_data = {
                "fit_score": fit_score if fit_score is not None else parsed_analysis.get("fit_score"),
                "recommendation": recommendation or parsed_analysis.get("recommendation"),
                "pros": parsed_analysis.get("pros", []),
                "cons": parsed_analysis.get("cons", []),
                "summary": parsed_analysis.get("summary", ""),
            }
        elif isinstance(deep_row, dict):
            deep_data = {
                "fit_score": deep_row.get("fit_score"),
                "recommendation": deep_row.get("recommendation"),
                "pros": deep_row.get("pros", []),
                "cons": deep_row.get("cons", []),
                "summary": deep_row.get("summary", ""),
            }
        else:
            deep_data = {
                "fit_score": getattr(deep_row, "fit_score", None),
                "recommendation": getattr(deep_row, "recommendation", None),
                "pros": getattr(deep_row, "pros", []),
                "cons": getattr(deep_row, "cons", []),
                "summary": getattr(deep_row, "summary", ""),
            }

    return {
        "vacancy": {
            "stable_id": vacancy_stable_id,
            "title": vac.title if vac else None,
            "company": vac.company if vac else None,
            "location": vac.location if vac else None,
            "description": vac.description if vac else None,
            "job_url": vac.job_url if vac else None,
            "source": vac.source if vac else None,
            "salary_min": vac.salary_min if vac else None,
            "salary_max": vac.salary_max if vac else None,
            "salary_currency": vac.salary_currency if vac else None,
        } if vac else None,
        "deep_analysis": deep_data,
        "package": pkg_data,
        "review": {
            "status": review.status.value if hasattr(review.status, "value") else str(review.status),
            "note": review.note,
        } if review else None,
    }


@app.post("/api/review/{vacancy_stable_id}")
def review_package(vacancy_stable_id: str, req: ReviewRequest) -> dict[str, Any]:
    init_db()
    from datetime import datetime

    from ..application_review import (
        REVIEW_VERSION,
        ApplicationReview,
        ReviewStatus,
        get_application_review,
        save_application_review,
    )
    from ..db import _row_to_vacancy, get_application_package, get_vacancy_by_id

    action = req.action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Invalid action, must be 'approve' or 'reject'")

    rev = get_application_review(vacancy_stable_id)
    if not rev:
        vac_row = get_vacancy_by_id(vacancy_stable_id)
        vac = _row_to_vacancy(vac_row) if vac_row else None
        pkg_row = get_application_package(vacancy_stable_id)
        pkg_json = {}
        if pkg_row and pkg_row[2]:
            try:
                pkg_json = json.loads(pkg_row[2])
            except Exception:
                pkg_json = {}
        track = get_application_status(vacancy_stable_id)
        now = datetime.utcnow().isoformat()
        rev = ApplicationReview(
            vacancy_stable_id=vacancy_stable_id,
            company=vac.company if vac else "",
            title=vac.title if vac else "",
            source=vac.source if vac else "",
            vacancy_url=vac.job_url if vac else "",
            final_url=vac.job_url if vac else "",
            match_score=getattr(track, "match_score", None),
            deep_score=getattr(track, "deep_score", None),
            application_strategy=pkg_json.get("application_strategy"),
            resume_summary=pkg_json.get("resume_summary"),
            tailored_skills=pkg_json.get("tailored_skills", []),
            relevant_experience=pkg_json.get("relevant_experience", []),
            cover_letter=pkg_json.get("cover_letter"),
            status=ReviewStatus.PENDING_REVIEW,
            note=None,
            created_at=now,
            updated_at=now,
            review_version=REVIEW_VERSION,
        )
        save_application_review(rev)

    try:
        if action == "approve":
            approve_review(vacancy_stable_id, note=req.note or "Approved via Web UI", force=True)
            return {"status": "success", "message": "Вакансия успешно утверждена (APPROVED)"}
        else:
            reject_review(vacancy_stable_id, note=req.note or "Rejected via Web UI")
            return {"status": "success", "message": "Вакансия отклонена (REJECTED)"}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logging.exception("Failed to review package for %s", vacancy_stable_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/applications/move")
def move_app_status(req: MoveStatusRequest) -> dict[str, Any]:
    init_db()
    try:
        rec = transition_application(req.vacancy_stable_id, req.new_status, note=req.note)
        return {"status": "success", "record": rec}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/collect")
def run_collect(req: CollectRequest | None = None) -> dict[str, Any]:
    init_db()
    sources = req.sources if (req and req.sources) else list(SOURCES.keys())
    try:
        new_count = collect(sources)
        return {"status": "success", "new_vacancies": new_count, "sources": sources}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class HhPreflightRequest(BaseModel):
    vacancy_id: str


@app.get("/api/hh/ownership")
def hh_ownership() -> dict[str, Any]:
    """Who owns the dedicated headless browser right now - or nobody.

    Read-only and cheap: it answers from the lease and a socket probe, so the UI can
    show "no browser running" without starting one to find out.
    """
    from .. import hh_browser_lease as lease

    return {
        "status": "success",
        "owner_type": lease.OWNER_UI,
        "summary": lease.ownership_summary(),
    }


@app.post("/api/hh/preflight")
def hh_preflight(req: HhPreflightRequest) -> dict[str, Any]:
    """Read-only HH operation: search, read one vacancy, run the preflight.

    The browser is acquired here and released before this returns - the UI process
    being alive is not a reason for Chrome to be alive (§4). Nothing is clicked,
    typed or sent.
    """
    from .. import hh_browser_lease as lease
    from ..hh_readonly_operation import operation_report

    if not req.vacancy_id.strip():
        raise HTTPException(status_code=400, detail="vacancy_id is required")
    try:
        report = operation_report(
            req.vacancy_id.strip(), owner_type=lease.OWNER_UI,
            purpose=f"ui-preflight:{req.vacancy_id.strip()}",
        )
    except lease.BrowserBusy as exc:
        # §9: another operation holds the browser. Say so; never start a second one.
        raise HTTPException(status_code=409, detail=f"browser busy: {exc}") from exc
    except Exception as exc:  # report the failure, do not crash the UI
        logging.getLogger(__name__).exception("HH preflight failed for %s", req.vacancy_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "success", "report": report}


@app.get("/api/review-list")
def get_review_list(
    limit: int = Query(100, ge=1, le=500),
    category: str | None = Query(None, description="remote_confirmed | remote_unclear | local_thailand"),
    direction: str | None = None,
    title_only: bool = True,
    max_age_days: int | None = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    """The manual-review list: bands A and B for remote work, plus Thailand-local.

    This is the middle mode. It is read-only by construction -- the module behind
    it imports no queue writer -- so opening this view can never approve a
    vacancy. Band C is absent: neither an automatic application nor a place in
    the list of suitable vacancies. Band B is shown on purpose so its conditions
    can be checked by hand; it is never queued on the strength of a board label.
    """
    init_db()
    from ..eligibility_review import (
        CATEGORY_LABELS,
        DIRECTION_NAMES,
        build_review_list,
        summarise,
    )

    # An unknown filter is refused rather than passed through. It would filter
    # every row out and return an empty list, and an empty list here is
    # indistinguishable from "there are no suitable vacancies" -- the kind of
    # silent loss this project treats as a defect. The known sets are read from
    # the module so they cannot drift away from what the list actually defines.
    if category is not None and category not in CATEGORY_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown category {category!r}; known: {sorted(CATEGORY_LABELS)}",
        )
    if direction is not None and direction not in DIRECTION_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown direction {direction!r}; known: {sorted(DIRECTION_NAMES)}",
        )

    categories = (category,) if category else None
    directions = (direction,) if direction else None
    items = build_review_list(
        limit=limit,
        categories=categories,
        directions=directions,
        title_only=title_only,
        max_age_days=max_age_days,
    )
    return {
        "items": items,
        "summary": summarise(items),
        "category_labels": CATEGORY_LABELS,
        "auto_queue_note": (
            "Только категория remote_confirmed допускается к очереди по географии; "
            "remote_unclear и local_thailand требуют ручной проверки условий."
        ),
    }


@app.get("/")
def serve_index() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Job Search UI is loading...</h1>", status_code=200)
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


HUB_STATIC_FILE = Path(__file__).resolve().parent.parent / "hub" / "static" / "hub.html"


@app.get("/hub", response_class=HTMLResponse)
def serve_hub() -> HTMLResponse:
    if not HUB_STATIC_FILE.exists():
        return HTMLResponse("<h1>Job Search Hub is loading...</h1>", status_code=200)
    return HTMLResponse(content=HUB_STATIC_FILE.read_text(encoding="utf-8"))


# Wire Hub & Interview API routes
from ..hub.api import router as hub_router

app.include_router(hub_router)

