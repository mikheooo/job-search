"""Unified hub backend (Phase 4D): Job + Gig on port 8000.

Mounted into the existing dashboard app (ui/app.py) under ``/api/hub/*``
plus the ``/hub`` page. Reuses storage layers, introduces no new statuses,
performs no migrations, sends nothing. Mutations ride the existing
dashboard-token middleware (POST always requires the token).

Three independent dimensions are kept separate everywhere:
1. listing freshness (computed: vacancy_lifecycle / gig freshness),
2. interaction status (application_tracking / submissions),
3. human mark (GigStatus SHORTLISTED/SKIPPED).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import config
from ..db import get_connection, init_db

router = APIRouter(prefix="/api/hub", tags=["hub"])

_ALLOWED_GIG_STATUS = ("NEW", "SHORTLISTED", "SKIPPED")


def _interview_auth(request: Request) -> None:
    """Owner gate for ALL interview endpoints, including history GETs.

    Phase 5A.1: private candidate data must never ride the global
    "reads are public" default. Same token scheme as ui/app (Bearer/X-API-Key,
    compare_digest). 503 when no token is configured at all, 401 otherwise.
    Local duplicate of the ui/app logic to avoid a ui<->hub import cycle.
    """
    import secrets

    configured = (os.getenv("DASHBOARD_TOKEN", "").strip()
                  or getattr(config, "DASHBOARD_TOKEN", "").strip())
    if not configured:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN not configured")
    auth_header = request.headers.get("Authorization", "")
    token = (auth_header[7:].strip() if auth_header.startswith("Bearer ")
             else request.headers.get("X-API-Key", "").strip())
    if not token or not secrets.compare_digest(token, configured):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing dashboard token")


class GigStatusBody(BaseModel):
    status: str
    note: str | None = None


class KworkImportBody(BaseModel):
    url: str


def _db_stats() -> dict[str, Any]:
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        out: dict[str, Any] = {}
        out["vacancies_total"] = cur.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
        out["tracking_by_status"] = dict(
            cur.execute("SELECT status, COUNT(*) FROM application_tracking GROUP BY status").fetchall())
        out["gigs_total"] = cur.execute("SELECT COUNT(*) FROM gigs").fetchone()[0]
        out["gigs_by_status"] = dict(
            cur.execute("SELECT status, COUNT(*) FROM gigs GROUP BY status").fetchall())
        out["gigs_by_source"] = dict(
            cur.execute("SELECT source, COUNT(*) FROM gigs GROUP BY source").fetchall())
        out["interviews_total"] = cur.execute("SELECT COUNT(*) FROM interview_events").fetchone()[0]
        out["queue_total"] = cur.execute("SELECT COUNT(*) FROM application_queue").fetchone()[0]
        return out
    finally:
        conn.close()


def _submissions_stats() -> dict[str, Any]:
    try:
        from ..gig.tracker import SubmissionTracker

        tracker = SubmissionTracker()
        rows = tracker.list(limit=10000)
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        return {"total": len(rows), "by_status": by_status}
    except Exception:
        return {"total": 0, "by_status": {}, "unavailable": True}


@router.get("/overview")
def hub_overview() -> dict[str, Any]:
    """Cross-system counters. Zeros are real zeros; missing subsystems are
    flagged instead of shown as zero."""
    stats = _db_stats()
    subs = _submissions_stats()
    tracking = stats.pop("tracking_by_status")
    return {
        "vacancies_total": stats["vacancies_total"],
        "tracking_by_status": tracking,
        "interviews_total": stats["interviews_total"],
        "queue_total": stats["queue_total"],
        "gigs_total": stats["gigs_total"],
        "gigs_by_status": stats["gigs_by_status"],
        "gigs_by_source": stats["gigs_by_source"],
        "submissions_total": subs["total"],
        "submissions_by_status": subs["by_status"],
        "submissions_unavailable": subs.get("unavailable", False),
    }


# Phase 4D.2: test/synthetic rows hidden from the default listing (never
# deleted). Explicit opt-in via include_test=true.
TEST_GIG_IDS = frozenset({"gig_gate2test"})
TEST_GIG_SCOPES = frozenset({"synthetic_fixture"})


@router.get("/gigs")
def hub_gigs(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = None,
    source: str | None = None,
    freshness: str | None = None,
    include_rejected: bool = False,
    exclude_scope: str | None = None,
    include_test: bool = False,
    order: str = Query("opportunity_score"),
) -> dict[str, Any]:
    """Gig list with optional computed-freshness filter (read-only)."""
    from ..gig.cli import _gig_brief
    from ..gig.freshness import classify_gig
    from ..gig.store import list_gigs

    init_db()
    order_map = {"opportunity_score": "opportunity_score",
                 "published_at": "published_at",
                 "effective_hourly_rate": "effective_hourly_rate"}
    gigs = list_gigs(limit=min(limit * 4, 2000), status=status, source=source,
                     include_rejected=include_rejected, exclude_scope=exclude_scope,
                     order_by=order_map.get(order, "opportunity_score"))
    if not include_test:
        gigs = [g for g in gigs
                if g.id not in TEST_GIG_IDS and g.source_scope not in TEST_GIG_SCOPES
                and (exclude_scope is None or g.source_scope != exclude_scope)]
    conn = get_connection()
    try:
        out = []
        for gig in gigs:
            row = {"id": gig.id, "source": gig.source, "source_id": gig.source_id,
                   "first_seen_at": gig.first_seen_at,
                   "last_seen_at": gig.last_seen_at,
                   "status": gig.status.value,
                   "source_status": gig.source_status}
            item = classify_gig(row, conn)
            if freshness and item["freshness"] != freshness.upper():
                continue
            brief = _gig_brief(gig)
            brief["freshness"] = item["freshness"]
            brief["interaction"] = item.get("interaction")
            out.append(brief)
            if len(out) >= limit:
                break
        return {"gigs": out, "count": len(out)}
    finally:
        conn.close()


@router.get("/gigs/{gig_id}")
def hub_gig_detail(gig_id: str) -> dict[str, Any]:
    from ..gig.cli import _gig_brief
    from ..gig.freshness import classify_gig, link_submission
    from ..gig.store import get_gig, list_gig_events

    init_db()
    gig = get_gig(gig_id)
    if gig is None:
        raise HTTPException(status_code=404, detail="gig not found")
    conn = get_connection()
    try:
        item = classify_gig({"id": gig.id, "source": gig.source,
                             "source_id": gig.source_id,
                             "first_seen_at": gig.first_seen_at,
                             "last_seen_at": gig.last_seen_at,
                             "status": gig.status.value,
                             "source_status": gig.source_status}, conn)
    finally:
        conn.close()
    brief = _gig_brief(gig)
    brief["freshness"] = item["freshness"]
    brief["interaction"] = item.get("interaction")
    brief["submission"] = link_submission(gig.source, gig.source_id)
    brief["events"] = list_gig_events(gig.id)
    return brief


@router.post("/gigs/{gig_id}/status")
def hub_gig_set_status(gig_id: str, body: GigStatusBody) -> dict[str, Any]:
    """Human mark only (SHORTLISTED/SKIPPED/NEW). Same record semantics as
    the :8001 API and Telegram callbacks (single gig, audited event)."""
    from ..gig.store import set_gig_status

    if body.status not in _ALLOWED_GIG_STATUS:
        raise HTTPException(status_code=422, detail="invalid gig status")
    init_db()
    updated = set_gig_status(gig_id, body.status, note=body.note)
    if updated is None:
        raise HTTPException(status_code=404, detail="gig not found")
    return {"status": "success", "gig_id": updated.id,
            "gig_status": updated.status.value}


@router.get("/applications")
def hub_applications(kind: str = Query("job", pattern="^(job|gig)$"),
                     status: str | None = None,
                     limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """Unified applications view with separated record types.

    kind=job: application_tracking rows (filter REJECTED is labeled as
    such, never as an employer refusal). kind=gig: submission rows
    (READY is preparation, never a sent proposal).
    """
    if kind == "gig":
        from ..gig.tracker import SubmissionTracker

        tracker = SubmissionTracker()
        rows = tracker.list(status=status, limit=limit)
        return {"kind": "gig", "items": [
            {"project_id": r.project_id, "source": r.source, "title": r.title,
             "status": r.status, "submitted_at": r.submitted_at,
             "proposal_created_at": r.proposal_created_at,
             "actual_bid": r.actual_bid, "revenue_usd": r.revenue_usd}
            for r in rows]}
    from ..application_tracking import list_applications

    init_db()
    recs = list_applications(status=status, limit=limit)
    items = []
    for r in recs:
        items.append({
            "vacancy_stable_id": r.vacancy_stable_id,
            "company": r.company, "title": r.title, "status": r.status.value
            if hasattr(r.status, "value") else str(r.status),
            "updated_at": r.updated_at, "applied_at": r.applied_at,
        })
    return {"kind": "job", "items": items}


@router.get("/interviews")
def hub_interviews(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    """Interview invitations only as recorded. No invented dates."""
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, conversation_id, vacancy_stable_id, company,"
            " vacancy_title, invitation_text, invitation_url, action_required,"
            " status, detected_at FROM interview_events"
            " ORDER BY detected_at DESC LIMIT ?", (limit,)).fetchall()
        cols = ["id", "conversation_id", "vacancy_stable_id", "company",
                "vacancy_title", "invitation_text", "invitation_url",
                "action_required", "status", "detected_at"]
        return {"items": [dict(zip(cols, r)) for r in rows]}
    finally:
        conn.close()


@router.get("/system")
def hub_system() -> dict[str, Any]:
    """Component health from local signals only (no marketplace probing).

    A component is reported by evidence (heartbeat age, log mtime, last
    DB writes), never inferred from a PID file alone.
    """
    import datetime

    root = Path(config.PROJECT_ROOT)
    logs = root / "logs"

    def _file_info(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"present": False}
        try:
            age = (datetime.datetime.now(datetime.timezone.utc) -
                   datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)).total_seconds()
        except Exception:
            age = None
        return {"present": True, "age_seconds": age,
                "mtime": datetime.datetime.fromtimestamp(
                    path.stat().st_mtime, tz=datetime.timezone.utc).isoformat()}

    def _heartbeat() -> dict[str, Any]:
        import json as _json

        info = _file_info(logs / "telegram_control_bot.heartbeat")
        if not info.get("present"):
            return {"alive": False, "reason": "no-heartbeat-file"}
        try:
            data = _json.loads((logs / "telegram_control_bot.heartbeat").read_text(
                encoding="utf-8"))
            ts = data.get("timestamp", "")
            try:
                dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                age = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
            except Exception:
                age = None
            return {"alive": bool(age is not None and age < 300),
                    "age_seconds": age, "pid": data.get("pid"),
                    "code_rev": data.get("code_rev")}
        except Exception as exc:
            return {"alive": False, "reason": f"unreadable: {type(exc).__name__}"}

    hb = _heartbeat()
    return {
        "telegram_bot": hb,
        "dashboard_token_configured": bool(
            (os.getenv("DASHBOARD_TOKEN", "").strip()
             or getattr(config, "DASHBOARD_TOKEN", "").strip())),
        "hh_browser_mode": os.getenv("HH_BROWSER_MODE", "headless"),
        "logs": {
            "hh_autopilot": _file_info(logs / "hh_autopilot.log"),
            "hh_monitor": _file_info(logs / "hh_monitor.log"),
            "control_bot": _file_info(logs / "telegram_control_bot.log"),
            "multi_source": _file_info(logs / "multi_source_discovery.log"),
        },
        "db": _db_stats(),
    }


@router.post("/kwork/import")
def hub_kwork_import(body: KworkImportBody) -> dict[str, Any]:
    """Owner-only (dashboard token via middleware) Kwork link import.

    Same owner-only, duplicate-safe mechanism as the Telegram /kwork path:
    validate -> dedup -> pipeline -> save NEW. No submissions, no sends.
    """
    from ..gig import kwork_import as ki

    links = ki.extract_kwork_links(body.url)
    if not links:
        raise HTTPException(status_code=422, detail="not a kwork project URL")
    return ki.import_links(links, persist=True)


class InterviewHintBody(BaseModel):
    question: str
    vacancy_stable_id: str | None = None
    role_title: str | None = None
    lang: str = "ru"
    session_id: int | None = None
    use_profile: bool = False


class InterviewQuestionsBody(BaseModel):
    vacancy_stable_id: str | None = None
    role_title: str | None = None
    lang: str = "ru"
    kinds: str = "technical,architectural,behavioral,experience"
    session_id: int | None = None
    use_profile: bool = False


class InterviewEvaluateBody(BaseModel):
    question: str
    answer: str
    vacancy_stable_id: str | None = None
    role_title: str | None = None
    lang: str = "ru"
    session_id: int | None = None
    use_profile: bool = False


class InterviewFastBody(BaseModel):
    question: str
    vacancy_stable_id: str | None = None
    role_title: str | None = None
    lang: str = "ru"
    session_id: int | None = None
    use_profile: bool = False


def _interview_ctx(vacancy_stable_id: str | None, role_title: str | None,
                   use_profile: bool = False) -> tuple[str, dict]:
    """Build prompt context honoring the consent flag.

    use_profile=False (default): fully synthetic — role title only, no
    profile/KB/vacancy reads at all. use_profile=True: real context.
    Returns (text, usage_dict) where usage_dict states exactly what was
    included, for the pre-send preview.
    """
    from ..interview import context as ictx

    if not use_profile:
        text = ("[CANDIDATE CONTEXT — synthetic mode, no personal data]\n"
                + (f"Manual role: {ictx.sanitize(role_title, 200)}"
                   if role_title else "No role specified."))
        usage = {"profile": False, "kb_verified": 0, "kb_checkable": 0,
                 "vacancy": False, "chars": len(text)}
        return text, usage
    ctx = ictx.build_context(vacancy_stable_id, role_title)
    text = ictx.context_to_text(ctx)
    claims = ctx.get("claims", {})
    usage = {"profile": bool(ctx.get("profile", {}).get("has_profile")),
             "kb_verified": len(claims.get("verified", [])),
             "kb_checkable": len(claims.get("checkable", [])),
             "vacancy": bool(ctx.get("vacancy", {}).get("has_vacancy")),
             "chars": len(text)}
    ctx["context_json_chars"] = len(text)
    return text, usage


def _interview_conn():
    from ..db import init_db
    from ..interview import sessions as isess

    init_db()
    conn = get_connection()
    isess.ensure_tables(conn)
    return conn


def _require_interview_session(conn, session_id: int | None) -> None:
    """Fail closed on a dangling session_id (no FK in schema).

    Without this, a POST with an unknown session_id silently stores an
    orphan turn row that no history read can reach. Matches the 404
    behaviour of GET/DELETE /interview/sessions/{id}.
    """
    if session_id is None:
        return
    from ..interview import sessions as isess

    if isess.get_session(conn, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")


@router.post("/interview/hint")
def hub_interview_hint(request: Request, body: InterviewHintBody) -> dict[str, Any]:
    """Quick hint for a live interview question. No HH/Gig/Telegram effects."""
    _interview_auth(request)
    from ..interview import assistant as iasst
    from ..interview import sessions as isess

    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="empty question")
    if body.lang not in ("ru", "en"):
        raise HTTPException(status_code=422, detail="lang must be ru|en")
    if body.session_id is not None:
        conn = _interview_conn()
        try:
            _require_interview_session(conn, body.session_id)
        finally:
            conn.close()
    context_text, ctx_usage = _interview_ctx(body.vacancy_stable_id, body.role_title,
                                                 body.use_profile)
    system, user = iasst.hint_prompts(question[:2000], context_text, body.lang)
    res = iasst.call_model(system, user)
    if not res["ok"]:
        return {"ok": False, "state": "error", "error": res["error"],
                "retryable": res.get("retryable", False)}
    data = res["data"]
    missing = str(data.get("missing_info") or "").strip()
    state = "need-info" if missing else "ready"
    conn = _interview_conn()
    try:
        sid = body.session_id
        if sid is None:
            sid = isess.create_session(conn, body.vacancy_stable_id, body.role_title)
        isess.save_turn(conn, sid, "hint", question, data)
    finally:
        conn.close()
    return {"ok": True, "state": state, "session_id": sid, "hint": data,
            "context_used": ctx_usage}


@router.post("/interview/fast")
def hub_interview_fast(request: Request, body: InterviewFastBody) -> dict[str, Any]:
    """Fast mode: short answer + 3 theses, exactly ONE model call.

    Never triggers the detailed generation automatically. Returns elapsed
    seconds measured server-side (no latency promises beyond measurement).
    """
    _interview_auth(request)
    from ..interview import assistant as iasst
    from ..interview import sessions as isess

    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="empty question")
    if body.lang not in ("ru", "en"):
        raise HTTPException(status_code=422, detail="lang must be ru|en")
    if body.session_id is not None:
        conn = _interview_conn()
        try:
            _require_interview_session(conn, body.session_id)
        finally:
            conn.close()
    context_text, ctx_usage = _interview_ctx(body.vacancy_stable_id, body.role_title,
                                             body.use_profile)
    system, user = iasst.fast_prompts(question[:2000], context_text, body.lang)
    res = iasst.call_model(system, user)
    if not res["ok"]:
        return {"ok": False, "state": "error", "error": res["error"],
                "retryable": res.get("retryable", False)}
    data = res["data"]
    conn = _interview_conn()
    try:
        sid = body.session_id
        if sid is None:
            sid = isess.create_session(conn, body.vacancy_stable_id, body.role_title)
        isess.save_turn(conn, sid, "fast", question, data)
    finally:
        conn.close()
    return {"ok": True, "state": "ready", "session_id": sid,
            "elapsed": res.get("elapsed"), "fast": data,
            "context_used": ctx_usage}


@router.post("/interview/transcribe")
async def hub_interview_transcribe(request: Request,
                                   lang: str = Query("ru"),
                                   engine: str | None = Query(None)) -> dict[str, Any]:
    """Transcribe uploaded audio with a LOCAL engine only (Phase 5B-1).

    Body: raw audio bytes (browsers send audio/webm; multipart NOT required).
    Free/offline: the stt module auto-detects faster-whisper/vosk only when
    already installed with a local model — nothing is downloaded, no paid
    APIs. With no usable engine the answer is an honest
    ``{"ok": False, "error": "stt-unavailable"}`` (HTTP 200, same contract as
    model errors); the UI then falls back to browser recognition or typing.
    Input problems (bad lang, empty/oversize audio) are 422, like other routes.
    No session write: transcription is not a coach turn. No HH/Gig/Telegram
    effects.
    """
    _interview_auth(request)
    from ..interview import stt as istt

    if lang not in istt.SUPPORTED_LANGS:
        raise HTTPException(status_code=422, detail="lang must be ru|en")
    try:
        audio = await request.body()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"unreadable body: {type(exc).__name__}")
    res = istt.transcribe_audio(bytes(audio or b""), lang,
                                request.headers.get("content-type"),
                                engine)
    if not res.get("ok"):
        code = res.get("error", "engine-error")
        if code in (istt.ERR_EMPTY, istt.ERR_TOO_LARGE, istt.ERR_LANG):
            raise HTTPException(status_code=422, detail=code)
        return {"ok": False, "error": code,
                "retryable": res.get("retryable", False),
                "detail": res.get("detail", ""),
                "engines": istt.detect_engines()}
    return res


class InterviewDetectBody(BaseModel):
    segments: list[str]
    lang: str = "ru"
    already_sent: list[str] = []


@router.post("/interview/detect")
def hub_interview_detect(request: Request, body: InterviewDetectBody) -> dict[str, Any]:
    """Propose interviewer questions from transcript pieces (Phase 5B-2).

    Pure rule-based detection over ready transcripts: no model calls, no
    network, no database reads or writes (stateless — cross-call dedup rides
    ``already_sent`` norms owned by the page). The user confirms or edits
    every candidate before anything reaches fast/hint, so the AI never runs
    on every speech fragment. No HH/Gig/Telegram effects.
    """
    _interview_auth(request)
    from ..interview import questions_detect as qdet

    if body.lang not in ("ru", "en"):
        raise HTTPException(status_code=422, detail="lang must be ru|en")
    cleaned = [(s or "").strip() for s in (body.segments or [])]
    cleaned = [s for s in cleaned if s]
    if not cleaned:
        raise HTTPException(status_code=422, detail="empty-transcript")
    if len(cleaned) > 50:
        raise HTTPException(status_code=422, detail="too-many-segments")
    if sum(len(s) for s in cleaned) > 10000:
        raise HTTPException(status_code=422, detail="transcript-too-large")
    sent = {qdet.normalize(x) for x in (body.already_sent or []) if x}
    det = qdet.QuestionDetector()
    cands: list[dict[str, Any]] = []
    for seg in cleaned:
        cands.extend(det.add_segment(seg, body.lang))
    # Batch mode: the posted window is complete, so settle trailing fragments.
    cands.extend(det.flush_stale(body.lang, force=True))
    fresh = [c for c in cands if c["norm"] not in sent]
    return {"ok": True, "candidates": fresh, "lang": body.lang,
            "segments": len(cleaned)}


@router.post("/interview/warmup")
def hub_interview_warmup(request: Request) -> dict[str, Any]:
    """Managed STT warmup for interview prep (Phase 5B-4c).

    Loads the resident local model (CUDA first, CPU fallback) and runs one
    short silence inference so kernel autotune happens now, not on the first
    live chunk. No microphone use, no audio recorded or stored (silence is
    synthesized in memory). Returns Loading/Ready/Error states for the UI.
    No session writes. No HH/Gig/Telegram effects.
    """
    _interview_auth(request)
    from ..interview import stt as istt

    return istt.warmup()


@router.get("/interview/context")
def hub_interview_context(request: Request) -> dict[str, Any]:
    """\"My experience\" screen data: what the AI would use, labeled.

    Counts + short labeled items only (no full resume dump). Owner-only.
    """
    _interview_auth(request)
    from ..interview import context as ictx

    profile = ictx.load_profile_summary()
    claims = ictx.load_verified_claims()
    return {
        "profile": {"has_profile": profile.get("has_profile", False),
                    "roles": profile.get("roles", [])[:10],
                    "core_skills": profile.get("core_skills", [])[:30],
                    "years_experience": profile.get("years_experience")},
        "verified": [{"id": c.get("id"), "kind": c.get("kind"),
                      "text": str(c.get("text", ""))[:300]}
                     for c in claims.get("verified", [])[:20]],
        "checkable": [{"id": c.get("id"), "kind": c.get("kind"),
                       "text": str(c.get("text", ""))[:300]}
                      for c in claims.get("checkable", [])[:20]],
        "has_kb": claims.get("has_kb", False),
    }


@router.post("/interview/questions")
def hub_interview_questions(request: Request, body: InterviewQuestionsBody) -> dict[str, Any]:
    """Prep-mode: typical questions for the role. No side effects."""
    _interview_auth(request)
    from ..interview import assistant as iasst
    from ..interview import sessions as isess

    role = (body.role_title or "").strip()
    if not role and not body.vacancy_stable_id:
        raise HTTPException(status_code=422, detail="role_title or vacancy required")
    if body.lang not in ("ru", "en"):
        raise HTTPException(status_code=422, detail="lang must be ru|en")
    if body.session_id is not None:
        conn = _interview_conn()
        try:
            _require_interview_session(conn, body.session_id)
        finally:
            conn.close()
    context_text, ctx_usage = _interview_ctx(body.vacancy_stable_id, role or None,
                                                 body.use_profile)
    system, user = iasst.questions_prompts(role or "selected vacancy",
                                           context_text, body.lang, body.kinds)
    res = iasst.call_model(system, user)
    if not res["ok"]:
        return {"ok": False, "state": "error", "error": res["error"],
                "retryable": res.get("retryable", False)}
    conn = _interview_conn()
    try:
        sid = body.session_id
        if sid is None:
            sid = isess.create_session(conn, body.vacancy_stable_id, role or None)
        isess.save_turn(conn, sid, "questions",
                        f"prep:{role or body.vacancy_stable_id}", res["data"])
    finally:
        conn.close()
    return {"ok": True, "state": "ready", "session_id": sid,
            "questions": res["data"].get("questions", []),
            "context_used": ctx_usage}


@router.post("/interview/evaluate")
def hub_interview_evaluate(request: Request, body: InterviewEvaluateBody) -> dict[str, Any]:
    """Grade a practice answer (STAR where fitting). No side effects."""
    _interview_auth(request)
    from ..interview import assistant as iasst
    from ..interview import sessions as isess

    question = (body.question or "").strip()
    answer = (body.answer or "").strip()
    if not question or not answer:
        raise HTTPException(status_code=422, detail="question and answer required")
    if body.lang not in ("ru", "en"):
        raise HTTPException(status_code=422, detail="lang must be ru|en")
    if body.session_id is not None:
        conn = _interview_conn()
        try:
            _require_interview_session(conn, body.session_id)
        finally:
            conn.close()
    context_text, ctx_usage = _interview_ctx(body.vacancy_stable_id, body.role_title,
                                                 body.use_profile)
    system, user = iasst.evaluate_prompts(question[:2000], answer[:4000],
                                          context_text, body.lang)
    res = iasst.call_model(system, user)
    if not res["ok"]:
        return {"ok": False, "state": "error", "error": res["error"],
                "retryable": res.get("retryable", False)}
    conn = _interview_conn()
    try:
        sid = body.session_id
        if sid is None:
            sid = isess.create_session(conn, body.vacancy_stable_id, body.role_title)
        isess.save_turn(conn, sid, "evaluate", question,
                        {"answer": answer[:4000], "grade": res["data"]})
    finally:
        conn.close()
    return {"ok": True, "state": "ready", "session_id": sid,
            "evaluation": res["data"], "context_used": ctx_usage}


@router.get("/interview/sessions")
def hub_interview_sessions(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    _interview_auth(request)
    from ..interview import sessions as isess

    conn = _interview_conn()
    try:
        return {"items": isess.list_sessions(conn, limit)}
    finally:
        conn.close()


@router.get("/interview/sessions/{session_id}")
def hub_interview_session(request: Request, session_id: int) -> dict[str, Any]:
    _interview_auth(request)
    from ..interview import sessions as isess

    conn = _interview_conn()
    try:
        data = isess.get_session(conn, session_id)
    finally:
        conn.close()
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    return data


@router.delete("/interview/sessions/{session_id}")
def hub_interview_session_delete(request: Request, session_id: int) -> dict[str, Any]:
    _interview_auth(request)
    from ..interview import sessions as isess

    conn = _interview_conn()
    try:
        ok = isess.delete_session(conn, session_id)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": True, "session_id": session_id}


__all__ = ["router"]
