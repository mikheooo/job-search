"""Отдельное API контура Gig Search.

Самодостаточное FastAPI-приложение: не меняет существующий дашборд
``ai_assistant/ui/app.py`` и не смешивает гиги с вакансиями (ТЗ §9).

Запуск (в дополнение к основному дашборду, на своём порту):

    .\\.venv\\Scripts\\python.exe -m uvicorn ai_assistant.gig.api:app --port 8001

Эндпоинты:
- ``GET  /api/gigs``                — ранжированный список + статистика;
- ``GET  /api/gigs/{gig_id}``      — один гиг с полным score breakdown;
- ``POST /api/gigs/{gig_id}/status`` — NEW/SHORTLISTED/SKIPPED (человек).

Контур prepare-only: никаких откликов и внешних отправок.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from ..db import init_db
from ..ui.auth import require_dashboard_token_middleware
from .cli import _gig_brief
from .models import Gig, GigStatus
from .store import get_gig, gig_stats, list_gig_events, list_gigs, set_gig_status

app = FastAPI(title="Gig Search API (GIG-SEARCH-P1)", version="1.0.0")

_STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"

# Token authorization for mutating methods and remote reads (GIG-001)
app.middleware("http")(require_dashboard_token_middleware)

# По умолчанию только loopback: API читает локальную БД и ничем наружу не торгует.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8001", "http://127.0.0.1:8001", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GigStatusRequest(BaseModel):
    status: str  # NEW | SHORTLISTED | SKIPPED
    note: str | None = None


@app.get("/api/gigs")
def get_gigs(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = None,
    source: str | None = None,
    source_mode: str | None = Query(None, pattern="^(live|fixture|unknown)$"),
    scope: str | None = None,
    exclude_scope: str | None = Query(
        None,
        description=(
            "Exclude a source scope (e.g. public_hire_pages) so the working view "
            "shows only current postings while the P2 archive stays stored"
        ),
    ),
    include_rejected: bool = False,
    order: str = Query(
        "opportunity_score",
        pattern="^(opportunity_score|published_at|effective_hourly_rate)$",
    ),
) -> dict[str, Any]:
    init_db()
    gigs = list_gigs(
        limit=limit,
        status=status,
        source=source,
        include_rejected=include_rejected,
        order_by=order,
        source_mode=source_mode,
        source_scope=scope,
        exclude_scope=exclude_scope,
    )
    return {"stats": gig_stats(), "gigs": [_gig_brief(g) for g in gigs]}


@app.get("/api/gigs/{gig_id}")
def get_gig_detail(gig_id: str) -> dict[str, Any]:
    init_db()
    gig: Gig | None = get_gig(gig_id)
    if gig is None:
        raise HTTPException(status_code=404, detail=f"gig not found: {gig_id}")
    brief = _gig_brief(gig)
    brief["events"] = list_gig_events(gig.id)
    return brief


@app.post("/api/gigs/{gig_id}/status")
def post_gig_status(gig_id: str, req: GigStatusRequest) -> dict[str, Any]:
    init_db()
    # P1 принимает только три статуса: неизвестное значение — ошибка клиента,
    # а не молчаливый сброс в NEW (GigStatus.parse не валидирует).
    if req.status not in {s.value for s in GigStatus}:
        raise HTTPException(status_code=422, detail=f"invalid gig status: {req.status}")
    updated = set_gig_status(gig_id, req.status, note=req.note)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"gig not found: {gig_id}")
    return {"status": "success", "gig_id": updated.id, "gig_status": updated.status.value}


@app.get("/api/gigs-stats")
def get_gig_stats() -> dict[str, Any]:
    init_db()
    return gig_stats()


@app.get("/")
def serve_gigs_page() -> HTMLResponse:
    page = _STATIC_DIR / "gigs.html"
    if not page.exists():
        return HTMLResponse("<h1>Gigs UI is loading...</h1>", status_code=200)
    return HTMLResponse(content=page.read_text(encoding="utf-8"))


@app.get("/favicon.ico", response_model=None)
def serve_favicon() -> FileResponse | HTMLResponse:
    fav = _STATIC_DIR / "favicon.ico"
    if fav.exists():
        return FileResponse(fav, media_type="image/x-icon")
    return HTMLResponse("", status_code=204)
