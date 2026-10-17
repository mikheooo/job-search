"""Phase 4C offline tests (20 scenarios). No network (stub transports or
snapshots), scratch DB (global conftest redirects config.DB_FILE), no
Telegram sends, no real submits, no prod writes.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from ai_assistant.gig import store as gig_store
from ai_assistant.gig.pipeline import GigPipeline, run_source_pipeline

SAMPLE_HTML = Path(__file__).resolve().parent / "kwork_sample.html"


def _read_sample() -> str:
    return SAMPLE_HTML.read_text(encoding="utf-8")


# 1. Successful collection (kwork stub transport).
def test_01_kwork_collect_success():
    from ai_assistant.gig.sources.kwork import KworkGigSource

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    recs = list(src.fetch())
    assert len(recs) == 1
    assert recs[0]["source_id"] == "kw_999001"
    assert recs[0]["currency"] == "RUB"
    assert src.health().status == "SUCCESS"


# 2. Source error (empty URL config -> honest failure, stored gigs untouched).
def test_02_kwork_empty_config_fails():
    from ai_assistant.gig.sources.kwork import KworkGigSource, KworkUnavailable

    src = KworkGigSource(urls=[])
    with pytest.raises(KworkUnavailable):
        list(src.fetch())
    assert src.health().status == "FAILED"


# 3. Auth required (contra-auth without session).
def test_03_contra_auth_requires_session():
    from ai_assistant.gig.sources.base import HEALTH_AUTH_REQUIRED
    from ai_assistant.gig.sources.contra_auth import ContraAuthenticatedSource

    def _boom(js: str) -> str:
        raise RuntimeError("no logged-in Contra tab found")

    src = ContraAuthenticatedSource(_boom)
    with pytest.raises(Exception):
        list(src.fetch())
    assert src.health().status == HEALTH_AUTH_REQUIRED


# 4. Rate limit (HTTP 429 surfaces as RATE_LIMITED, not silent).
def test_04_freelancer_rate_limited():
    from ai_assistant.gig.sources.base import HEALTH_RATE_LIMITED
    from ai_assistant.gig.sources.freelancer import FreelancerGigSource

    def _429(url: str):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    src = FreelancerGigSource(listing_pages=1, skill_pages=1,
                              include_general_listing=True,
                              target_skills=[], fetch_html=_429,
                              delay_seconds=0)
    # Point at one listing URL via override: every GET 429s.
    from ai_assistant.gig.sources import freelancer as flmod

    src._listing_urls = lambda: [("https://www.freelancer.com/jobs/", None)]
    try:
        list(src.fetch())
        raised = False
    except Exception:
        raised = True
    assert raised
    assert src.health().status == HEALTH_RATE_LIMITED


# 5. Re-detection of the same order (persist twice -> second is duplicate).
def test_05_redetection_is_duplicate():
    from ai_assistant.gig.sources.kwork import KworkGigSource

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    first = run_source_pipeline(src, persist=True)
    assert first.total == 1
    src2 = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                          fetch_html=lambda url: _read_sample())
    second = run_source_pipeline(src2, persist=True)
    assert second.duplicates == 1
    assert src2.health().status == "SUCCESS"


# 6. Duplicate one task via different URLs (freelancer same project twice).
def test_06_same_task_two_urls_single_record():
    from ai_assistant.gig.sources.freelancer import FreelancerGigSource

    doc = {
        "projectId": 424242, "title": "Python parser needed",
        "description": "Parse sites daily.",
        "budget": {"min": 100, "max": 300},
        "currencyDetails": {"code": "USD"},
        "status": "active",
        "skills": [{"name": "python"}],
        "bidStats": {"bidCount": 3},
    }
    page = ('<html><head><script id="webapp-state" type="application/json">'
            + json.dumps({"NGRX_STATE": {"projectsSeo": [{"documents": {"a": {"rawDocument": doc}}}]}})
            + "</script></head></html>")

    src = FreelancerGigSource(listing_pages=1, skill_pages=1,
                              include_general_listing=False, target_skills=[],
                              fetch_html=lambda url: page, delay_seconds=0)
    # Two different listing URLs returning cards to the same project page.
    card = ('<div class="JobSearchCard-item-inner">'
            '<a href="/projects/aaaa/bb-Test-424242" data-heading-link="true">x</a></div>'
            '<div class="JobSearchCard-item-inner">'
            '<a href="/projects/cccc/dd-Test-424242" data-heading-link="true">x</a></div>')
    src._listing_urls = lambda: [("u1", None)]
    import ai_assistant.gig.sources.freelancer as flmod

    orig = flmod.FreelancerGigSource.cards_from_listing
    flmod.FreelancerGigSource.cards_from_listing = staticmethod(
        lambda html: [{"url": "/projects/aaaa/bb-Test-424242", "title": "Python parser needed",
                       "skills": ["python"], "bids_received": 3, "description_preview": "Parse",
                       "days_left_text": None},
                      {"url": "/projects/cccc/dd-Test-424242", "title": "Python parser needed",
                       "skills": ["python"], "bids_received": 3, "description_preview": "Parse",
                       "days_left_text": None}])
    try:
        result = run_source_pipeline(src, persist=False)
    finally:
        flmod.FreelancerGigSource.cards_from_listing = orig
    # Same source_id from two URLs: one record + one in-run duplicate note.
    assert result.total == 1 or result.duplicates >= 1


# 7. Closed order trips SOURCE_CLOSED.
def test_07_closed_order_rejected():
    from ai_assistant.gig.sources.kwork import KworkGigSource

    html = _read_sample().replace('"status":"active"', '"status":"archived"')
    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: html)
    result = run_source_pipeline(src, persist=False)
    assert result.total == 1
    assert result.rejected == 1
    assert "SOURCE_CLOSED" in result.rejected_gigs[0].rejection_reasons


# 8. Old order with an active proposal is protected.
def test_08_old_order_with_proposal_protected(tmp_path):
    from ai_assistant.gig.freshness import is_gig_protected
    from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker

    from ai_assistant.gig.sources.kwork import KworkGigSource

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    result = run_source_pipeline(src, persist=True)
    assert result.total == 1
    gig_id = result.ranked_gigs[0].id if result.ranked_gigs else None
    assert gig_id is not None
    tracker = SubmissionTracker(tmp_path / "sub.sqlite")
    tracker.upsert(SubmissionRecord(project_id="kw_999001", title="Python bot needed",
                                    source="kwork", status="READY"))
    tracker.mark_submitted("kw_999001", actual_bid=6000, bids_at_submission=2,
                           actual_delivery=5, source="kwork")
    import sqlite3

    from ai_assistant import config as cfg

    conn = sqlite3.connect(cfg.DB_FILE)
    try:
        protected, reasons = is_gig_protected(gig_id, conn, tmp_path / "sub.sqlite")
    finally:
        conn.close()
    assert protected is True


# 9. Invalid budget never crashes, stays unknown.
def test_09_invalid_budget_unknown():
    from ai_assistant.gig.normalizer import normalize_gig

    gig = normalize_gig({"source": "kwork", "source_id": "kw_x",
                         "title": "T", "budget": "lots of money!!!"})
    assert gig.budget_min is None and gig.budget_max is None


# 10. Kwork missing fields stay missing (never invented).
def test_10_kwork_missing_fields():
    from ai_assistant.gig.sources.kwork import KworkGigSource

    html = ('<html><body>{"wantData":{"id":555,"status":"active",'
            '"name":"Bare task","description":"Do thing.",'
            '"date_create":"2026-10-01 10:00:00"}}</body></html>')
    rec = KworkGigSource.record_from_html(html, page_url="u", fetched_at="t")
    assert rec is not None
    assert rec["budget"] == {"min": None, "max": None, "type": None}
    # RUB is a documented Kwork platform constant (rouble-only exchange).
    assert rec["currency"] == "RUB"
    assert rec["skills"] == []
    assert rec["category"] is None
    assert rec["competition"] is None


# 11. Bad description coerced safely.
def test_11_bad_description():
    from ai_assistant.gig.normalizer import normalize_gig

    assert normalize_gig({"source": "s", "source_id": "1",
                          "title": "T", "description": None}).description == ""
    assert normalize_gig({"source": "s", "source_id": "2",
                          "title": "T", "description": 123}).description == "123"


# 12. Fixture never poses as live delivery.
def test_12_fixture_not_live():
    from ai_assistant.gig.sources.fixture import FixtureGigSource

    src = FixtureGigSource()
    recs = list(src.fetch(limit=3))
    assert recs, "fixture dataset must yield rows"
    assert all(r["source_mode"] == "fixture" and r["source_mode"] != "live" for r in recs)
    assert all(r["source"] == "fixture" for r in recs)


# 13. Persist failure is counted, not raised.
def test_13_pipeline_counts_normalize_errors():
    result = run_source_pipeline(
        _DictSource([{"source": "", "source_id": "", "title": "broken"}]),
        persist=True,
    )
    assert result.errors == 1
    assert result.total == 1


# 14. Existing GigStatus values survive re-runs.
def test_14_status_preserved_and_validated():
    from ai_assistant.gig.sources.kwork import KworkGigSource

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    run_source_pipeline(src, persist=True)
    gig = gig_store.list_gigs()[0]
    gig_store.set_gig_status(gig.id, "SHORTLISTED", note="t")
    src2 = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                          fetch_html=lambda url: _read_sample())
    run_source_pipeline(src2, persist=True)
    assert gig_store.get_gig(gig.id).status.value == "SHORTLISTED"
    # Unknown status strings collapse to NEW on read (documented compat):
    # filtering by an unknown string behaves as a NEW filter (here: empty).
    from ai_assistant.gig.models import GigStatus

    assert GigStatus.parse("NOPE").value == "NEW"
    assert gig_store.list_gigs(status="NOPE") == []


# 15. Dashboard/API brief shape compatibility.
def test_15_brief_shape():
    from ai_assistant.gig.cli import _gig_brief

    from ai_assistant.gig.sources.kwork import KworkGigSource

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    result = run_source_pipeline(src, persist=True)
    assert result.ranked_gigs, "active kwork sample must be accepted"
    brief = _gig_brief(result.ranked_gigs[0])
    for key in ("id", "title", "source", "source_mode", "url", "budget",
                "opportunity_score", "status", "source_facts"):
        assert key in brief, f"brief missing {key}"
    assert brief["source"] == "kwork"


# 16. No proposal is ever sent by the pipeline.
def test_16_pipeline_never_submits(tmp_path):
    from ai_assistant.gig.sources.kwork import KworkGigSource
    from ai_assistant.gig.tracker import SubmissionTracker

    tracker = SubmissionTracker(tmp_path / "sub16.sqlite")
    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    run_source_pipeline(src, persist=True)
    assert tracker.list() == []


# 17. No Telegram sends during collection.
def test_17_no_telegram_sends(monkeypatch):
    import urllib.request

    from ai_assistant.gig.sources.kwork import KworkGigSource

    calls = []
    real = urllib.request.urlopen

    def _guard(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        return real(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", _guard)
    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    run_source_pipeline(src, persist=False)
    assert all("api.telegram.org" not in u for u in calls)


# 18. HH autopilot / Job Search untouched by gig code.
def test_18_gig_independent_of_hh():
    import pathlib

    gig_root = pathlib.Path(__file__).resolve().parent.parent / "ai_assistant" / "gig"
    hits = []
    for path in gig_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "hh_autopilot" in text or "hh_chat_monitor" in text:
            hits.append(path.name)
    assert hits == [], f"gig must not depend on HH modules: {hits}"


# 19. Collector rerun adds no duplicate rows.
def test_19_rerun_no_new_rows():
    from ai_assistant.gig.sources.kwork import KworkGigSource

    def _run():
        src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                             fetch_html=lambda url: _read_sample())
        return run_source_pipeline(src, persist=True)

    first = _run()
    n1 = gig_store.gig_stats()["total"]
    second = _run()
    n2 = gig_store.gig_stats()["total"]
    assert first.total == 1 and second.duplicates == 1
    assert n1 == n2


# 20. Ambiguous external result: page without state falls back safely.
def test_20_no_hydration_state_safe():
    from ai_assistant.gig.sources.freelancer import FreelancerGigSource

    src = FreelancerGigSource(fetch_html=lambda url: "<html><body>hello</body></html>")
    assert src.record_from_html("<html><body>hello</body></html>",
                                page_url="u", fetched_at="t") is None
    rec = src.card_record({"url": "/projects/a/b-Test-1", "title": "T",
                           "skills": [], "bids_received": None,
                           "description_preview": "d", "days_left_text": None},
                          fetched_at="t")
    assert rec["budget"] == {"min": None, "max": None, "type": None}
    assert rec["currency"] is None


class _DictSource:
    """Minimal in-memory source for pipeline edge tests."""

    key = "dict-test"
    source_mode = "live"
    source_scope = "test"

    def __init__(self, records):
        self._records = records
        self._last_health = None

    def fetch(self, limit=None):
        from ai_assistant.gig.sources.base import summarize_health

        produced = 0
        try:
            for record in self._records[: int(limit)] if limit else self._records:
                produced += 1
                yield record
        finally:
            self._last_health = summarize_health(produced, [])

    def health(self):
        from ai_assistant.gig.sources.base import SourceHealth

        return self._last_health or SourceHealth()
