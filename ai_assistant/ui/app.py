"""FastAPI backend application for Job-Search Dashboard."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from ..db import (
    init_db,
    get_connection,
    list_vacancies,
    get_vacancy_by_id,
    get_deep_analysis,
    get_application_package,
    _row_to_vacancy,
)
from ..application_tracking import (
    list_applications,
    get_application_status,
    transition_application,
    ApplicationStatus,
)
from ..application_queue import generate_queue, list_queue
from ..application_review import approve_review, reject_review, get_application_review
from ..candidate_profile import load_candidate_profile
from ..matcher import JobMatcher
from ..adapters.himalayas import HimalayasAdapter
from ..adapters.weworkremotely import WeWorkRemotelyAdapter
from ..adapters.remoteok import RemoteOkAdapter
from ..adapters.habr_career import HabrCareerAdapter
from ..cli import collect, SOURCES


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Job Search Hub", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"


class MoveStatusRequest(BaseModel):
    vacancy_stable_id: str
    new_status: str
    note: Optional[str] = None


class ReviewRequest(BaseModel):
    action: str  # "approve" | "reject"
    note: Optional[str] = None


class CollectRequest(BaseModel):
    sources: Optional[List[str]] = None


@app.get("/api/stats")
def get_stats() -> Dict[str, Any]:
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

    return {
        "total_vacancies": total_vacancies,
        "by_source": by_source,
        "by_state": by_state,
        "total_packages": total_packages,
        "tracking_status": by_tracking,
        "eligibility": by_eligibility,
    }


@app.get("/api/vacancies")
def get_vacancies(
    limit: int = Query(50, ge=1, le=500),
    source: Optional[str] = None,
    search: Optional[str] = None,
    min_score: Optional[float] = None,
    eligibility: Optional[str] = None,
) -> List[Dict[str, Any]]:
    init_db()
    from ..db import get_all_vacancy_eligibilities
    elig_map = get_all_vacancy_eligibilities()

    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT v.*, ve.status as elig_status, ve.reasons_json as elig_reasons FROM vacancies v LEFT JOIN vacancy_eligibility ve ON v.stable_id = ve.vacancy_stable_id WHERE 1=1"
    params: List[Any] = []

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
def get_queue_items(top: int = 50) -> List[Dict[str, Any]]:
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
def get_package_detail(vacancy_stable_id: str) -> Dict[str, Any]:
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
def review_package(vacancy_stable_id: str, req: ReviewRequest) -> Dict[str, Any]:
    init_db()
    from ..application_review import (
        get_application_review,
        approve_review,
        reject_review,
        ApplicationReview,
        ReviewStatus,
        save_application_review,
        REVIEW_VERSION,
    )
    from ..application_tracking import get_application_status
    from ..db import get_vacancy_by_id, get_application_package, _row_to_vacancy
    from datetime import datetime

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
            return {"status": "success", "message": f"Вакансия успешно утверждена (APPROVED)"}
        else:
            reject_review(vacancy_stable_id, note=req.note or "Rejected via Web UI")
            return {"status": "success", "message": f"Вакансия отклонена (REJECTED)"}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logging.exception("Failed to review package for %s", vacancy_stable_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/applications/move")
def move_app_status(req: MoveStatusRequest) -> Dict[str, Any]:
    init_db()
    try:
        rec = transition_application(req.vacancy_stable_id, req.new_status, note=req.note)
        return {"status": "success", "record": rec}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/collect")
def run_collect(req: Optional[CollectRequest] = None) -> Dict[str, Any]:
    init_db()
    sources = req.sources if (req and req.sources) else list(SOURCES.keys())
    try:
        new_count = collect(sources)
        return {"status": "success", "new_vacancies": new_count, "sources": sources}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def serve_index() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Job Search UI is loading...</h1>", status_code=200)
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
