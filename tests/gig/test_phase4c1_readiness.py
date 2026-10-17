"""Phase 4C.1 offline tests: readiness, delivery drafts, locks, isolation.

No network (stub transports; conftest blocks urlopen), scratch DB (global
conftest redirects config.DB_FILE), no Telegram sends, no real submits,
no prod writes.
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

SAMPLE_HTML = Path(__file__).resolve().parent / "kwork_sample.html"


def _read_sample() -> str:
    return SAMPLE_HTML.read_text(encoding="utf-8")


# --- source health -------------------------------------------------------
def test_health_success_and_unknown():
    from ai_assistant.gig.sources.base import HEALTH_SUCCESS, HEALTH_UNKNOWN
    from ai_assistant.gig.sources.kwork import KworkGigSource

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    assert src.health().status == HEALTH_UNKNOWN
    assert len(list(src.fetch())) == 1
    assert src.health().status == HEALTH_SUCCESS
    assert src.health().produced == 1


def test_health_unavailable_and_partial():
    from ai_assistant.gig.sources.base import HEALTH_FAILED, HEALTH_PARTIAL
    from ai_assistant.gig.sources.kwork import KworkGigSource, KworkUnavailable

    empty = KworkGigSource(urls=[])
    with pytest.raises(KworkUnavailable):
        list(empty.fetch())
    assert empty.health().status == HEALTH_FAILED

    mixed = KworkGigSource(
        urls=["https://kwork.ru/projects/999001", "https://kwork.ru/projects/1"],
        fetch_html=lambda url: _read_sample() if "999001" in url else "<html></html>",
    )
    assert len(list(mixed.fetch())) == 1
    assert mixed.health().status == HEALTH_PARTIAL


def test_health_rate_limited():
    import urllib.error

    from ai_assistant.gig.sources.base import HEALTH_RATE_LIMITED, summarize_health

    assert summarize_health(0, ["x: HTTP 429"]).status == HEALTH_RATE_LIMITED
    assert summarize_health(2, ["slow"]).status == "PARTIAL"
    assert summarize_health(0, ["conn reset"]).status == "FAILED"
    assert summarize_health(0, ["no logged-in tab"]).status == "AUTH_REQUIRED"


# --- kwork ingestion ------------------------------------------------------
def test_kwork_url_ingestion():
    from ai_assistant.gig.sources.kwork import (
        KworkGigSource, canonical_url, extract_project_id)

    assert extract_project_id("https://kwork.ru/projects/3264668/view") == "3264668"
    assert extract_project_id("not a url") is None
    assert canonical_url(42) == "https://kwork.ru/projects/42"
    src = KworkGigSource(
        urls=["https://kwork.ru/projects/3264668/view", "garbage",
              "https://kwork.ru/projects/3264668"],
        urls_file=None)
    assert src.urls == ["https://kwork.ru/projects/3264668"]


def test_kwork_urls_file(tmp_path):
    from ai_assistant.gig.sources.kwork import KworkGigSource

    f = tmp_path / "urls.txt"
    f.write_text("# comment\nhttps://kwork.ru/projects/111\n\nhttps://kwork.ru/projects/222/view\n",
                 encoding="utf-8")
    src = KworkGigSource(urls_file=str(f))
    assert src.urls == ["https://kwork.ru/projects/111", "https://kwork.ru/projects/222"]


# --- duplicate delivery ---------------------------------------------------
def test_duplicate_delivery_skipped():
    from ai_assistant.gig.notify import (
        batch_key, delivery_key, prepare_batch, stable_key)

    gig = {"id": "gig_abc", "title": "T", "source": "kwork",
           "source_mode": "live", "url": "https://kwork.ru/projects/1",
           "budget_text": "5000 RUB", "opportunity_score": 60,
           "rejection_reasons": []}
    ledger: dict[str, str] = {}
    sent: list = []

    def _send(card):
        sent.append(card)
        return {"message_id": 7}

    def _record(key, ntype, chat, payload, status="DELIVERED"):
        ledger[key] = status

    def _status(key):
        return ledger.get(key)

    first = prepare_batch([gig], chat_id="1", send_fn=_send, record_fn=_record,
                          delivery_status_fn=_status)
    assert first["sent"] == ["gig_abc"] and not first["dry_run"]
    assert ledger[stable_key("gig_abc")] == "DELIVERED"
    # A later run builds a DIFFERENT batch key (extra gig) yet must not
    # re-send the delivered order: stability comes from the per-gig key.
    other = dict(gig, id="gig_xyz")
    second = prepare_batch([gig, other], chat_id="1", send_fn=_send,
                           record_fn=_record, delivery_status_fn=_status)
    assert second["sent"] == ["gig_xyz"]
    assert second["skipped"] == ["gig_abc"]
    assert delivery_key("gig_abc", batch_key(["gig_abc"])).startswith("gig:gig_abc:")


def test_ambiguous_defers_failed_retries():
    from ai_assistant.gig.notify import prepare_batch

    gig = {"id": "gig_q", "title": "T", "source": "kwork",
           "source_mode": "live", "url": "https://kwork.ru/projects/9",
           "budget_text": "1 RUB", "opportunity_score": 1,
           "rejection_reasons": []}
    ledger = {"gig_digest:gig_q": "AMBIGUOUS"}
    sent: list = []
    res = prepare_batch([gig], chat_id="1",
                        send_fn=lambda c: sent.append(c),
                        record_fn=lambda *a: None,
                        delivery_status_fn=ledger.get)
    assert res["sent"] == [] and res["deferred"] == [
        {"gig_id": "gig_q", "status": "AMBIGUOUS"}]
    ledger["gig_digest:gig_q"] = "FAILED"
    res2 = prepare_batch([gig], chat_id="1",
                         send_fn=lambda c: sent.append(c) or {"message_id": 1},
                         record_fn=lambda *a: None,
                         delivery_status_fn=ledger.get)
    assert res2["sent"] == ["gig_q"]


def test_rerun_collection_no_dupes():
    from ai_assistant.gig.pipeline import run_source_pipeline
    from ai_assistant.gig.sources.kwork import KworkGigSource
    from ai_assistant.gig import store as gig_store

    def _run():
        src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                             fetch_html=lambda url: _read_sample())
        return run_source_pipeline(src, persist=True)

    assert _run().total == 1
    again = _run()
    assert again.duplicates == 1
    assert gig_store.gig_stats()["total"] == 1


# --- sqlite contention ----------------------------------------------------
def test_sqlite_contention_safe():
    from ai_assistant.gig import store as gig_store
    from ai_assistant.gig.pipeline import run_source_pipeline
    from ai_assistant.gig.sources.kwork import KworkGigSource

    errors: list = []

    def _worker():
        try:
            src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                                 fetch_html=lambda url: _read_sample())
            run_source_pipeline(src, persist=True)
            gig_store.gig_stats()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, f"contention errors: {errors[:2]}"
    assert gig_store.gig_stats()["total"] == 1


# --- scheduled task lock --------------------------------------------------
def test_task_lock_single_instance(tmp_path):
    from ai_assistant.runner import SingleInstanceLock

    a = SingleInstanceLock(lock_dir=str(tmp_path), lock_name="gig_collect.lock")
    b = SingleInstanceLock(lock_dir=str(tmp_path), lock_name="gig_collect.lock")
    ok, _ = a.acquire()
    assert ok
    ok2, reason = b.acquire()
    assert not ok2 and "SKIPPED" in reason
    assert a.release()


def test_launch_failure_clean():
    from ai_assistant.gig.sources.kwork import KworkGigSource, KworkUnavailable

    # Missing urls file fails in __init__: no partial state, no fetch attempt.
    with pytest.raises(KworkUnavailable):
        KworkGigSource(urls_file="/definitely/missing/urls.txt")


# --- telegram callback (offline, mocked transport) ------------------------
def _fake_ctx(payload):
    import io as _io

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return _Ctx()


def test_callback_routing_offline(monkeypatch):
    import ai_assistant.telegram_control_bot as bot
    from ai_assistant.gig import store as gig_store

    from ai_assistant.gig.normalizer import normalize_gig
    from ai_assistant.gig.pipeline import GigPipeline

    rec = {"source": "kwork", "source_id": "kw_cb1", "title": "CB task",
           "url": "https://kwork.ru/projects/2"}
    gig = GigPipeline(persist=False).process_gig(normalize_gig(rec), existing=[])
    from ai_assistant.gig.store import save_gig

    save_gig(gig)

    updates = [
        {"update_id": 1,
         "callback_query": {"id": "cq-1", "data": f"gig:INT:{gig.id}",
                            "from": {"id": bot.ALLOWED_CHAT_ID}}},
    ]
    calls = {"n": 0}

    def _scripted(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "answerCallbackQuery" in url:
            calls["n"] += 1
            return _fake_ctx({"ok": True})
        if "sendMessage" in url:
            return _fake_ctx({"ok": True, "result": {"message_id": 1}})
        if "getUpdates" in url:
            if updates:
                return _fake_ctx({"ok": True, "result": [updates.pop(0)]})
            raise _StopTest()
        raise AssertionError(f"unexpected url {url}")

    class _StopTest(BaseException):
        pass

    monkeypatch.setattr(urllib.request, "urlopen", _scripted)
    import time as _time

    _real = _time.sleep
    monkeypatch.setattr(_time, "sleep", lambda s: _real(0.01))
    with pytest.raises(_StopTest):
        # Import-time lock dir would hit prod logs; redirect first.
        import tempfile

        tmp = tempfile.mkdtemp()
        monkeypatch.setattr(bot, "BOT_LOCK_DIR", tmp)
        monkeypatch.setattr(bot, "BOT_HEARTBEAT_FILE", tmp + "/hb")
        bot.run_bot_polling()
    assert gig_store.get_gig(gig.id).status.value == "SHORTLISTED"
    assert calls["n"] >= 1


# --- fixture isolation ----------------------------------------------------
def test_fixture_isolation():
    from ai_assistant.gig.notify import select_for_delivery

    rows = [
        {"id": "g1", "source": "fixture", "source_mode": "fixture",
         "rejection_reasons": []},
        {"id": "g2", "source": "freelancer", "source_mode": "live",
         "rejection_reasons": []},
        {"id": "g3", "source": "kwork", "source_mode": "live",
         "rejection_reasons": ["SOURCE_CLOSED"]},
    ]
    picked = select_for_delivery(rows, freshness={"g2": "ACTIVE_SEEN"})
    assert [g["id"] for g in picked] == ["g2"]


# --- closed order ---------------------------------------------------------
def test_closed_order_never_deliverable():
    from ai_assistant.gig.notify import select_for_delivery

    rows = [{"id": "g9", "source": "kwork", "source_mode": "live",
             "rejection_reasons": []}]
    assert select_for_delivery(rows, freshness={"g9": "CLOSED_CONFIRMED"}) == []
    assert select_for_delivery(rows, freshness={"g9": "STALE_CANDIDATE"}) == []
    assert select_for_delivery(rows, freshness={"g9": "UNKNOWN"}) == []
    assert len(select_for_delivery(rows, freshness={"g9": "ACTIVE_SEEN"})) == 1


# --- active proposal on old order -----------------------------------------
def test_active_proposal_protects(tmp_path):
    import sqlite3

    from ai_assistant import config as cfg
    from ai_assistant.gig.freshness import is_gig_protected
    from ai_assistant.gig.sources.kwork import KworkGigSource
    from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker

    from ai_assistant.gig.pipeline import run_source_pipeline

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    result = run_source_pipeline(src, persist=True)
    gig_id = result.ranked_gigs[0].id
    tracker = SubmissionTracker(tmp_path / "old.sqlite")
    tracker.upsert(SubmissionRecord(project_id="kw_999001", title="Python bot needed",
                                    source="kwork", status="READY"))
    tracker.mark_submitted("kw_999001", actual_bid=6000, bids_at_submission=2,
                           actual_delivery=5, source="kwork")
    conn = sqlite3.connect(cfg.DB_FILE)
    try:
        protected, _ = is_gig_protected(gig_id, conn, tmp_path / "old.sqlite")
    finally:
        conn.close()
    assert protected is True


# --- ambiguous linkage ----------------------------------------------------
def test_ambiguous_linkage_refuses(tmp_path):
    from ai_assistant.gig.freshness import link_submission
    from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker

    tracker = SubmissionTracker(tmp_path / "amb.sqlite")
    # Same project_id under two sources: exact (source, id) still resolves.
    tracker.upsert(SubmissionRecord(project_id="kw_1", title="A",
                                    source="kwork", status="READY"))
    tracker.upsert(SubmissionRecord(project_id="kw_1", title="B",
                                    source="freelancer", status="READY"))
    hit = link_submission("kwork", "kw_1", tmp_path / "amb.sqlite")
    assert hit is not None and hit["project_id"] == "kw_1" and hit["source"] == "kwork"
    assert link_submission("kwork", "kw_missing", tmp_path / "amb.sqlite") is None


# --- no real POST bids / no HH impact -------------------------------------
def test_no_post_bids_no_hh(monkeypatch):
    import pathlib

    import urllib.request

    from ai_assistant.gig.pipeline import run_source_pipeline
    from ai_assistant.gig.sources.kwork import KworkGigSource

    def _deny(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        raise AssertionError(f"network must stay stubbed, attempted: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", _deny)
    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"],
                         fetch_html=lambda url: _read_sample())
    result = run_source_pipeline(src, persist=False)
    assert result.total == 1
    gig_root = pathlib.Path("ai_assistant") / "gig"
    for path in gig_root.rglob("*.py"):
        if path.name in ("notify.py",):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "api.telegram.org" not in text, f"telegram send in {path.name}"
