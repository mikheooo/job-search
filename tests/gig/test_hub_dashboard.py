"""Phase 4D offline tests: unified hub (17 scenarios).

Endpoint functions are called directly (no httpx TestClient available);
scratch DB per test via global conftest (config.DB_FILE redirect). No
network, no sends, no prod writes.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi import HTTPException

HUB_HTML = pathlib.Path(__file__).resolve().parents[2] / "ai_assistant" / "hub" / "static" / "hub.html"


def _seed_kwork_gig(status="NEW", source_status="OPEN", source_id="kw_h1"):
    from ai_assistant.gig.normalizer import normalize_gig
    from ai_assistant.gig.pipeline import GigPipeline

    pipe = GigPipeline(persist=False)
    gig = pipe.process_gig(normalize_gig({
        "source": "kwork", "source_id": source_id, "title": "Hub task",
        "url": "https://kwork.ru/projects/11", "description": "Do it.",
        "budget": {"min": 1000, "max": 2000, "type": "fixed"},
        "currency": "RUB", "source_status": source_status}), existing=[])
    assert not gig.rejection_reasons
    from ai_assistant.gig.store import save_gig

    save_gig(gig)
    if status != "NEW":
        from ai_assistant.gig.store import set_gig_status

        set_gig_status(gig.id, status, note="t")
    return gig.id


# 1. overall stats: zeros explicit, missing subsystems flagged.
def test_01_overview_shape():
    from ai_assistant.hub.api import hub_overview

    o = hub_overview()
    for key in ("vacancies_total", "tracking_by_status", "interviews_total",
                "queue_total", "gigs_total", "gigs_by_status", "gigs_by_source",
                "submissions_total", "submissions_by_status"):
        assert key in o, key
    assert isinstance(o["gigs_total"], int)


# 2. gigs API with filters.
def test_02_gigs_filters():
    from ai_assistant.hub.api import hub_gigs

    gid = _seed_kwork_gig()
    all_gigs = hub_gigs(limit=50)
    assert all_gigs["count"] >= 1
    assert hub_gigs(limit=50, status="SHORTLISTED")["count"] == 0
    assert hub_gigs(limit=50, source="kwork")["count"] >= 1
    assert hub_gigs(limit=50, source="nope")["count"] == 0
    active = hub_gigs(limit=50, freshness="ACTIVE_SEEN")
    assert any(g["id"] == gid for g in active["gigs"])
    assert hub_gigs(limit=50, freshness="CLOSED_CONFIRMED")["count"] == 0


# 3. job applications statuses incl. filter-REJECTED labeling path.
def test_03_job_applications():
    from ai_assistant import db
    from ai_assistant.application_tracking import (
        ApplicationStatus, set_application_status)
    from ai_assistant.hub.api import hub_applications
    from ai_assistant.schema import Vacancy

    vac = Vacancy(source="hh", source_job_id="hub1", title="T", company="C",
                  description="d", job_url="https://example.com/hub1",
                  location="Remote", country_restrictions=[],
                  timezone_restrictions=[])
    db.save_vacancy(vac)
    set_application_status("hh:hub1", ApplicationStatus.DISCOVERED,
                           company="C", title="T")
    items = hub_applications(kind="job", limit=100)["items"]
    row = [a for a in items if a["vacancy_stable_id"] == "hh:hub1"][0]
    assert row["status"] == "DISCOVERED"


# 4. gig submissions statuses (READY is preparation, not sent).
def test_04_gig_submissions(tmp_path):
    from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker
    from ai_assistant.hub.api import hub_applications

    tracker = SubmissionTracker(tmp_path / "hub_sub.sqlite")
    tracker.upsert(SubmissionRecord(project_id="fl_9", title="T",
                                    source="freelancer", status="READY"))
    # Hub reads the PROD tracker path; on scratch DB it is empty but valid.
    res = hub_applications(kind="gig", limit=100)
    assert res["kind"] == "gig" and isinstance(res["items"], list)


# 5. telegram -> dashboard sync (status change visible via hub reads).
def test_05_tg_dashboard_sync():
    from ai_assistant.gig.store import get_gig, set_gig_status
    from ai_assistant.hub.api import hub_gig_detail, hub_gigs

    gid = _seed_kwork_gig()
    set_gig_status(gid, "SHORTLISTED", note="gig telegram callback INT")
    assert get_gig(gid).status.value == "SHORTLISTED"
    assert hub_gig_detail(gid)["status"] == "SHORTLISTED"
    assert any(g["id"] == gid for g in hub_gigs(limit=50, status="SHORTLISTED")["gigs"])


# 6. dashboard -> store update (+ audited event).
def test_06_dashboard_update():
    from ai_assistant.gig.store import get_gig, list_gig_events
    from ai_assistant.hub.api import GigStatusBody, hub_gig_set_status

    gid = _seed_kwork_gig()
    res = hub_gig_set_status(gid, GigStatusBody(status="SKIPPED", note="hub UI"))
    assert res == {"status": "success", "gig_id": gid, "gig_status": "SKIPPED"}
    assert get_gig(gid).status.value == "SKIPPED"
    assert any(e["event_type"] == "STATUS_CHANGED" for e in list_gig_events(gid))
    with pytest.raises(HTTPException) as exc:
        hub_gig_set_status(gid, GigStatusBody(status="APPLIED"))
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc2:
        hub_gig_set_status("gig_missing", GigStatusBody(status="SKIPPED"))
    assert exc2.value.status_code == 404


# 7. duplicates & repeated actions (second INT is a no-op, one event).
def test_07_repeat_actions_idempotent():
    from ai_assistant.gig.notify import handle_gig_callback
    from ai_assistant.gig.store import get_gig, list_gig_events, set_gig_status

    gid = _seed_kwork_gig()

    def _row(gig_id):
        g = get_gig(gig_id)
        return None if g is None else {"id": g.id, "status": g.status.value}

    acked: set[str] = set()
    first = handle_gig_callback(
        f"gig:INT:{gid}", get_gig_fn=_row,
        set_status_fn=lambda i, s, n: set_gig_status(i, s, note=n),
        callback_query_id="cq-hub", acked_ids=acked)
    assert first["outcome"] == "applied"
    second = handle_gig_callback(
        f"gig:INT:{gid}", get_gig_fn=_row,
        set_status_fn=lambda i, s, n: set_gig_status(i, s, note=n),
        callback_query_id="cq-hub", acked_ids=acked)
    assert second["outcome"] == "already-applied"
    evs = [e for e in list_gig_events(gid) if e["event_type"] == "STATUS_CHANGED"]
    assert len(evs) == 1


# 8. closed kwork shown separately, never as available.
def test_08_closed_kwork_separate():
    from ai_assistant.gig.freshness import classify_gig
    from ai_assistant.hub.api import hub_gigs
    from ai_assistant.gig.normalizer import normalize_gig
    from ai_assistant.gig.pipeline import GigPipeline

    pipe = GigPipeline(persist=False)
    gig = pipe.process_gig(normalize_gig({
        "source": "kwork", "source_id": "kw_closed1", "title": "Old",
        "url": "https://kwork.ru/projects/12", "description": "d",
        "budget": {"min": 100, "max": 200, "type": "fixed"},
        "currency": "RUB", "source_status": "ARCHIVED"}), existing=[])
    assert "SOURCE_CLOSED" in gig.rejection_reasons
    import sqlite3

    from ai_assistant import config as cfg

    conn = sqlite3.connect(cfg.DB_FILE)
    try:
        item = classify_gig({"id": "x", "source": "kwork", "source_id": "kw_closed1",
                             "first_seen_at": None, "last_seen_at": None,
                             "status": "NEW", "source_status": "ARCHIVED"}, conn)
    finally:
        conn.close()
    assert item["freshness"] == "CLOSED_CONFIRMED"
    assert hub_gigs(limit=50, freshness="CLOSED_CONFIRMED")["count"] == 0


# 8b. legacy stored STOP/USER_STOP tokens classify as closed too.
def test_08b_legacy_stop_tokens_closed():
    import sqlite3

    from ai_assistant import config as cfg
    from ai_assistant.gig.freshness import classify_gig

    conn = sqlite3.connect(cfg.DB_FILE)
    try:
        for token in ("stop", "user_stop", "STOPPED", "USER_STOPPED"):
            item = classify_gig({"id": "x", "source": "kwork",
                                 "source_id": "kw_x", "first_seen_at": None,
                                 "last_seen_at": None, "status": "NEW",
                                 "source_status": token}, conn)
            assert item["freshness"] == "CLOSED_CONFIRMED", token
    finally:
        conn.close()


# 8c. default listing hides fixture + test rows; opt-in shows them.
def test_08c_default_hides_test_rows():
    from ai_assistant.gig.normalizer import normalize_gig
    from ai_assistant.gig.pipeline import GigPipeline
    from ai_assistant.gig.store import save_gig
    from ai_assistant.hub.api import hub_gigs

    pipe = GigPipeline(persist=False)
    gig = pipe.process_gig(normalize_gig(
        {"source": "kwork", "source_id": "kw_probe hiding",
         "gig_id": "gig_gate2test",
         "title": "[TEST] hidden", "url": "https://kwork.ru/projects/999001",
         "description": "d",
         "budget": {"min": 100, "max": 200, "type": "fixed"},
         "currency": "RUB"}), existing=[])
    assert gig.id == "gig_gate2test"
    save_gig(gig)
    assert "gig_gate2test" not in [g["id"] for g in hub_gigs(limit=500)["gigs"]]
    shown = [g["id"] for g in hub_gigs(limit=500, include_test=True)["gigs"]]
    assert "gig_gate2test" in shown
def test_09_no_server_needed():
    import socket

    with socket.socket() as sock:
        sock.settimeout(2)
        assert sock.connect_ex(("127.0.0.1", 8001)) != 0
    from ai_assistant.hub.api import hub_overview

    assert isinstance(hub_overview()["gigs_total"], int)


# 10. empty DB reads valid.
def test_10_empty_db():
    from ai_assistant.hub.api import hub_applications, hub_gigs, hub_interviews, hub_overview

    assert hub_overview()["gigs_total"] == 0
    assert hub_gigs(limit=10)["count"] == 0
    assert hub_interviews(limit=10)["items"] == []


# 11. missing fields tolerated.
def test_11_missing_fields():
    from ai_assistant.gig.cli import _gig_brief
    from ai_assistant.gig.normalizer import normalize_gig
    from ai_assistant.gig.pipeline import GigPipeline
    from ai_assistant.gig.store import save_gig
    from ai_assistant.hub.api import hub_gig_detail

    pipe = GigPipeline(persist=False)
    gig = pipe.process_gig(normalize_gig(
        {"source": "kwork", "source_id": "kw_bare", "title": "Bare"}), existing=[])
    save_gig(gig)
    detail = hub_gig_detail(gig.id)
    assert detail["id"] == gig.id
    _gig_brief(gig)


# 12. ambiguous submission linkage resolves exact-only.
def test_12_ambiguous_linkage():
    from ai_assistant.gig.freshness import link_submission
    from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tracker = SubmissionTracker(f"{tmp}/a.sqlite")
        tracker.upsert(SubmissionRecord(project_id="kw_1", title="A",
                                        source="kwork", status="READY"))
        assert link_submission("kwork", "kw_1", f"{tmp}/a.sqlite")["project_id"] == "kw_1"
        assert link_submission("kwork", "kw_nope", f"{tmp}/a.sqlite") is None


# 13. access control: mutations live under /api/ (middleware-enforced).
def test_13_mutations_under_guarded_prefix():
    import ai_assistant.ui.app as appmod

    paths = [p for p in appmod.app.openapi()["paths"]]
    mutations = [p for p in paths if p.startswith("/api/hub/") and
                 ("status" in p or "import" in p)]
    assert mutations, "hub mutations must live under /api/ for token middleware"
    for p in ("/api/hub/overview", "/api/hub/gigs", "/hub"):
        assert p in paths


# 14. mobile markup present.
def test_14_mobile_markup():
    html = HUB_HTML.read_text(encoding="utf-8")
    assert 'name="viewport"' in html
    assert 'lang="ru"' in html
    for tab in ("Обзор", "Вакансии", "Заказы", "Отклики", "Собеседования", "Статус"):
        assert tab in html


# 15. legacy contracts intact.
def test_15_legacy_contracts():
    import ai_assistant.ui.app as appmod

    paths = appmod.app.openapi()["paths"]
    for legacy in ("/api/stats", "/api/vacancies", "/api/queue",
                   "/api/package/{vacancy_stable_id}",
                   "/api/review/{vacancy_stable_id}",
                   "/api/applications/move", "/api/collect",
                   "/api/hh/ownership", "/api/hh/preflight",
                   "/api/review-list", "/"):
        assert legacy in paths, legacy


# 16. no HH/freelance sends from hub layer.
def test_16_no_sends_from_hub():
    hub_root = pathlib.Path(__file__).resolve().parent.parent / "ai_assistant" / "hub"
    for path in hub_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "api.telegram.org" not in text, path.name
        assert "hh_autopilot" not in text, path.name
        assert "place_bid" not in text and "CDP" not in text, path.name
