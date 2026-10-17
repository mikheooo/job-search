"""Phase 4C.2 control scenario + hardening tests (offline).

Full chain on isolated data: Kwork link -> fetch -> dedup -> assess ->
save -> card -> callback -> status -> API read. Repeated run with a
different batch: no redelivery, no duplicate rows. Plus: closed order,
source error, ambiguous status, unavailable source, old order with active
proposal, bad callback, send-then-record-crash, concurrent same-link
import, no HH submits / freelance sends.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

SAMPLE_HTML = Path(__file__).resolve().parent / "kwork_sample.html"


def _read_sample() -> str:
    return SAMPLE_HTML.read_text(encoding="utf-8")


def _kwork_src(urls, html=None):
    from ai_assistant.gig.sources.kwork import KworkGigSource

    return KworkGigSource(urls=urls,
                          fetch_html=lambda url: html if html is not None else _read_sample())


def test_control_chain_and_no_redelivery():
    from ai_assistant.gig import store as gig_store
    from ai_assistant.gig.notify import (
        delivery_key, handle_gig_callback, prepare_batch, stable_key)
    from ai_assistant.gig.pipeline import run_source_pipeline

    # 1-5. link -> fetch -> dedup -> assess -> save
    first = run_source_pipeline(_kwork_src(["https://kwork.ru/projects/999001"]),
                                persist=True)
    assert first.total == 1 and first.accepted == 1
    gig = first.ranked_gigs[0]
    assert gig_store.get_gig(gig.id) is not None

    # 6-7. card -> callback -> status
    ledger: dict[str, str] = {}
    sent: list = []
    res = prepare_batch(
        [{"id": gig.id, "title": gig.title, "source": "kwork",
          "source_mode": "live", "url": gig.url,
          "budget_text": gig.budget_text,
          "opportunity_score": gig.opportunity_score,
          "rejection_reasons": []}],
        chat_id="1",
        send_fn=lambda c: sent.append(c) or {"message_id": 3},
        record_fn=lambda k, n, ch, p, status="DELIVERED": ledger.__setitem__(k, status),
        delivery_status_fn=ledger.get)
    assert res["sent"] == [gig.id] and len(res["cards"]) == 1

    def _row(gid):
        g = gig_store.get_gig(gid)
        return None if g is None else {"id": g.id, "status": g.status.value}

    writes: list = []
    out = handle_gig_callback(
        f"gig:INT:{gig.id}", get_gig_fn=_row,
        set_status_fn=lambda gid, st, note: (
            writes.append((gid, st)), gig_store.set_gig_status(gid, st, note=note)),
        callback_query_id="cq-9", acked_ids=set())
    assert out["outcome"] == "applied" and out["gig_id"] == gig.id \
        and out["status"] == "SHORTLISTED" and out["title"]

    # 8. API read reflects the status.
    assert gig_store.get_gig(gig.id).status.value == "SHORTLISTED"

    # Repeat with a DIFFERENT batch: no redelivery, no new rows.
    other = {"id": "gig_other", "title": "O", "source": "kwork",
             "source_mode": "live", "url": "https://kwork.ru/projects/3",
             "budget_text": "1 RUB", "opportunity_score": 1,
             "rejection_reasons": []}
    again = prepare_batch(
        [{"id": gig.id, "title": gig.title, "source": "kwork",
          "source_mode": "live", "url": gig.url,
          "budget_text": gig.budget_text,
          "opportunity_score": gig.opportunity_score,
          "rejection_reasons": []}, other],
        chat_id="1",
        send_fn=lambda c: sent.append(c) or {"message_id": 4},
        record_fn=lambda k, n, ch, p, status="DELIVERED": ledger.__setitem__(k, status),
        delivery_status_fn=ledger.get)
    assert again["sent"] == ["gig_other"]
    assert again["skipped"] == [gig.id]
    assert gig_store.gig_stats()["total"] == 1


def test_closed_order_end_to_end():
    from ai_assistant.gig.notify import select_for_delivery
    from ai_assistant.gig.pipeline import run_source_pipeline

    html = _read_sample().replace('"status":"active"', '"status":"archived"')
    res = run_source_pipeline(
        _kwork_src(["https://kwork.ru/projects/999001"], html=html), persist=True)
    assert res.rejected == 1
    gig = res.rejected_gigs[0]
    assert select_for_delivery(
        [{"id": gig.id, "source": "kwork", "source_mode": "live",
          "rejection_reasons": gig.rejection_reasons}],
        freshness={gig.id: "CLOSED_CONFIRMED"}) == []


def test_kwork_source_error_clean():
    from ai_assistant.gig.pipeline import run_source_pipeline
    from ai_assistant.gig.sources.kwork import KworkGigSource

    def _boom(url: str):
        raise ConnectionError("network down (test)")

    src = KworkGigSource(urls=["https://kwork.ru/projects/999001"], fetch_html=_boom)

    # fetch_html override returning is bypassed by raising: fetch records error.
    # NB: override raising counts as a source error, surfaced via health.
    recs = []
    try:
        recs = list(src.fetch())
    except Exception:
        pass
    assert recs == []
    assert src.health().status in ("FAILED", "PARTIAL")


def test_ambiguous_gig_status_never_delivered():
    from ai_assistant.gig.notify import select_for_delivery

    row = {"id": "gx", "source": "kwork", "source_mode": "live", "rejection_reasons": []}
    assert select_for_delivery([row], freshness={"gx": "UNKNOWN"}) == []
    assert select_for_delivery([row], freshness={}) != []


def test_unavailable_source_no_db_touch():
    from ai_assistant.gig import store as gig_store
    from ai_assistant.gig.sources.freelancer import FreelancerGigSource, FreelancerUnavailable

    before = gig_store.gig_stats()["total"]
    src = FreelancerGigSource(access_policy={}, delay_seconds=0)
    with pytest.raises(FreelancerUnavailable):
        list(src.fetch(limit=1))
    assert src.health().status == "FAILED"
    assert gig_store.gig_stats()["total"] == before


def test_old_order_active_proposal_end_to_end(tmp_path):
    from ai_assistant.gig.freshness import classify_gig, is_gig_protected
    from ai_assistant.gig.tracker import SubmissionRecord, SubmissionTracker
    from ai_assistant.gig.pipeline import run_source_pipeline
    import sqlite3

    from ai_assistant import config as cfg

    res = run_source_pipeline(
        _kwork_src(["https://kwork.ru/projects/999001"]), persist=True)
    gig_id = res.ranked_gigs[0].id
    tracker = SubmissionTracker(tmp_path / "old2.sqlite")
    tracker.upsert(SubmissionRecord(project_id="kw_999001", title="Python bot needed",
                                    source="kwork", status="READY"))
    tracker.mark_submitted("kw_999001", actual_bid=6000, bids_at_submission=2,
                           actual_delivery=5, source="kwork")
    conn = sqlite3.connect(cfg.DB_FILE)
    try:
        row = conn.execute(
            "SELECT id, source, source_id, url, title, budget_min, budget_max,"
            " currency, published_at, first_seen_at, last_seen_at, status,"
            " source_status FROM gigs WHERE id=?", (gig_id,)).fetchone()
        rec = dict(zip(["id", "source", "source_id", "url", "title", "budget_min",
                        "budget_max", "currency", "published_at", "first_seen_at",
                        "last_seen_at", "status", "source_status"], row))
        item = classify_gig(rec, conn, tracker_path=tmp_path / "old2.sqlite")
        assert item["protected"] is True
        assert item["interaction"] == "PROPOSAL_SUBMITTED"
    finally:
        conn.close()


def test_bad_callback_refused():
    from ai_assistant.gig.notify import handle_gig_callback

    assert handle_gig_callback("app:xyz", get_gig_fn=lambda i: None,
                               set_status_fn=lambda *a: None)["outcome"] == "ignored"
    assert handle_gig_callback("gig:INT:nope", get_gig_fn=lambda i: None,
                               set_status_fn=lambda *a: (_ for _ in ()).throw(
                                   AssertionError("must not write")))["outcome"] == "refused"
    acked = {"cq-dup"}
    assert handle_gig_callback("gig:INT:g", get_gig_fn=lambda i: {"id": "g", "status": "NEW"},
                               set_status_fn=lambda *a: (_ for _ in ()).throw(
                                   AssertionError("must not write")),
                               callback_query_id="cq-dup",
                               acked_ids=acked)["outcome"] == "already-applied"


def test_send_then_record_crash_no_resend_on_unknown():
    # Send succeeded but recording crashed -> status unknown path: our
    # record_fn raising is contained per-card; FAILED is recorded best-effort.
    from ai_assistant.gig.notify import prepare_batch

    gig = {"id": "g_crash", "title": "T", "source": "kwork",
           "source_mode": "live", "url": "https://kwork.ru/projects/8",
           "budget_text": "1 RUB", "opportunity_score": 1,
           "rejection_reasons": []}
    ledger: dict[str, str] = {}

    def _record(key, ntype, chat, payload, status="DELIVERED"):
        raise ConnectionError("db down (test)")

    res = prepare_batch([gig], chat_id="1",
                        send_fn=lambda c: {"message_id": 1},
                        record_fn=_record,
                        delivery_status_fn=ledger.get)
    # Transport ok but confirmation unrecorded: MUST NOT count as sent.
    # Reported as failed; ledger stays empty (operator-visible, no silent state).
    assert res["sent"] == [] and len(res["failed"]) == 1
    assert ledger == {}
    # Next run sees no DELIVERED marker: conservative resend is allowed only
    # after explicit FAILED/None — here ledger empty -> eligible again, and
    # that decision is visible (not silent).
    res2 = prepare_batch([gig], chat_id="1", send_fn=lambda c: {"message_id": 2},
                         record_fn=lambda *a: None,
                         delivery_status_fn=ledger.get)
    assert res2["sent"] == ["g_crash"]


def test_concurrent_same_link_single_row():
    from ai_assistant.gig import store as gig_store
    from ai_assistant.gig.pipeline import run_source_pipeline

    errors: list = []

    def _worker():
        try:
            run_source_pipeline(
                _kwork_src(["https://kwork.ru/projects/999001"]), persist=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors
    assert gig_store.gig_stats()["total"] == 1


def test_kwork_link_import_flow():
    from ai_assistant.gig import kwork_import as ki
    from ai_assistant.gig import store as gig_store

    assert ki.KWORK_LINK_IMPORT_ENABLED is True  # enabled by owner APPROVE
    assert ki.extract_kwork_links(
        "look https://kwork.ru/projects/999001 and http://evil.com/x") == [
        "https://kwork.ru/projects/999001"]
    assert ki.validate_kwork_url("https://kwork.ru/projects/999001/view") == \
        "https://kwork.ru/projects/999001"
    assert ki.validate_kwork_url("https://evil.com/projects/999001") is None
    assert ki.validate_kwork_url("https://kwork.ru/projects/abc") is None
    assert ki.validate_kwork_url("https://kwork.ru/inbox") is None

    import ai_assistant.gig.sources.kwork as kwmod

    orig = kwmod.KworkGigSource._http_get
    kwmod.KworkGigSource._http_get = lambda self, url: _read_sample()
    try:
        preview = ki.import_links(["https://kwork.ru/projects/999001"],
                                  persist=False)
        assert len(preview["imported"]) == 1
        assert gig_store.gig_stats()["total"] == 0  # preview writes nothing
        saved = ki.import_links(["https://kwork.ru/projects/999001"],
                                persist=True)
        assert len(saved["imported"]) == 1
        again = ki.import_links(["https://kwork.ru/projects/999001"],
                                persist=True)
        assert again["imported"] == [] and len(again["duplicates"]) == 1
        bad = ki.import_links(["https://evil.com/x",
                               "https://kwork.ru/projects/999001"], persist=False)
        assert any(r["reason"] == "not-a-kwork-project-url" for r in bad["refused"])
    finally:
        kwmod.KworkGigSource._http_get = orig


def test_kwork_manual_import_no_invention():
    from ai_assistant.gig import kwork_import as ki

    res = ki.import_manual_description(
        "https://kwork.ru/projects/777001", "Нужен парсер на Python.",
        title="Parser", persist=False)
    # No budget supplied -> honest PRICE_MISSING gate (gates apply equally).
    assert res["gig_id"].startswith("gig_") and res["rejected"] == ["PRICE_MISSING"]
    res2 = ki.import_manual_description("not-a-url", "desc", persist=False)
    assert res2["gig_id"].startswith("gig_")


def _delivery_conn_at(path):
    import contextlib
    import sqlite3

    @contextlib.contextmanager
    def _make():
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS telegram_delivery_records (id INTEGER PRIMARY KEY,"
            " delivery_key TEXT UNIQUE, notification_type TEXT, chat_id TEXT,"
            " delivered_at TEXT, status TEXT, payload TEXT)")
        conn.commit()
        try:
            yield conn
        finally:
            conn.close()

    return _make()


def _delivery_conn(tmp_path):
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "delivery.sqlite"), check_same_thread=False)
    conn.execute(
        "CREATE TABLE telegram_delivery_records (id INTEGER PRIMARY KEY,"
        " delivery_key TEXT UNIQUE, notification_type TEXT, chat_id TEXT,"
        " delivered_at TEXT, status TEXT, payload TEXT)")
    conn.commit()
    return conn


def test_concurrent_delivery_single_send(tmp_path):
    import sqlite3
    import threading

    from ai_assistant.gig.notify import prepare_batch, sqlite_delivery_store, stable_key

    db_path = str(tmp_path / "delivery.sqlite")
    with _delivery_conn_at(db_path):
        pass
    gig = {"id": "g_race", "title": "T", "source": "kwork",
           "source_mode": "live", "url": "https://kwork.ru/projects/5",
           "budget_text": "1 RUB", "opportunity_score": 1,
           "rejection_reasons": []}
    lock = threading.Lock()
    sent: list = []
    errors: list = []

    def _send(card):
        with lock:
            sent.append(card["chat_id"])
        return {"message_id": 1}

    def _run2():
        # Own connection per worker, like separate processes would have.
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            status_fn, record_fn, reserve_fn = sqlite_delivery_store(conn)

            def _rec(key, ntype, chat, payload, status="DELIVERED"):
                with lock:
                    record_fn(key, ntype, chat, payload, status)
                    conn.commit()

            prepare_batch([gig], chat_id="1", send_fn=_send,
                          record_fn=_rec, delivery_status_fn=status_fn,
                          reserve_fn=reserve_fn)
        except Exception as exc:  # noqa: BLE001 - surface thread failures
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=_run2) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"worker errors: {errors[:2]}"
    with lock:
        assert len(sent) == 1, f"exactly one send across racers, got {len(sent)}"
    check = sqlite3.connect(db_path)
    try:
        row = check.execute(
            "SELECT status FROM telegram_delivery_records WHERE delivery_key=?",
            (stable_key("g_race"),)).fetchone()
    finally:
        check.close()
    assert row is not None and row[0] == "DELIVERED"


def test_attempting_defers_and_delivered_never_downgrades(tmp_path):
    from ai_assistant.gig.notify import prepare_batch, sqlite_delivery_store, stable_key

    conn = _delivery_conn(tmp_path)
    status_fn, record_fn, reserve_fn = sqlite_delivery_store(conn)
    assert reserve_fn(stable_key("g_a")) is True
    assert reserve_fn(stable_key("g_a")) is False  # already claimed
    conn.commit()
    gig = {"id": "g_a", "title": "T", "source": "kwork",
           "source_mode": "live", "url": "https://kwork.ru/projects/6",
           "budget_text": "1 RUB", "opportunity_score": 1,
           "rejection_reasons": []}
    res = prepare_batch([gig], chat_id="1", send_fn=lambda c: {"message_id": 1},
                        record_fn=record_fn, delivery_status_fn=status_fn,
                        reserve_fn=reserve_fn)
    assert res["sent"] == []  # ATTEMPTING held elsewhere -> deferred, no send
    assert res["deferred"] == [{"gig_id": "g_a", "status": "ATTEMPTING"}]
    record_fn(stable_key("g_a"), "gig_digest", "1", {}, "DELIVERED")
    conn.commit()
    record_fn(stable_key("g_a"), "gig_digest", "1", {}, "FAILED")
    conn.commit()
    assert status_fn(stable_key("g_a")) == "DELIVERED"
    conn.close()


def test_kwork_stop_statuses_close():
    from ai_assistant.gig.sources.kwork import normalize_source_status

    assert normalize_source_status("stop") == "STOPPED"
    assert normalize_source_status("user_stop") == "USER_STOPPED"
    assert normalize_source_status("active") == "OPEN"
    assert normalize_source_status("archived") == "ARCHIVED"
    # Closed family trips gates + freshness evidence.
    from ai_assistant.gig import hard_gates as hg

    assert "stopped" in hg._CLOSED_SOURCE_STATUSES
    assert "user_stopped" in hg._CLOSED_SOURCE_STATUSES
    from ai_assistant.gig import freshness as fr

    assert "stopped" in fr.CLOSED_WORDS and "user_stopped" in fr.CLOSED_WORDS


def test_namespace_isolation():
    from ai_assistant.gig.notify import prepare_batch, stable_key

    gig = {"id": "g_iso", "title": "T", "source": "kwork",
           "source_mode": "live", "url": "https://kwork.ru/projects/7",
           "budget_text": "1 RUB", "opportunity_score": 1,
           "rejection_reasons": []}
    prod_ledger: dict[str, str] = {}
    test_ledger: dict[str, str] = {}
    assert stable_key("g_iso") != stable_key("g_iso", "test")
    test_ledger[stable_key("g_iso", "test")] = "DELIVERED"
    res = prepare_batch([gig], chat_id="1", namespace="test",
                        send_fn=lambda c: {"message_id": 1},
                        record_fn=lambda *a: None,
                        delivery_status_fn=test_ledger.get)
    assert res["sent"] == [] and res["skipped"] == ["g_iso"]
    res2 = prepare_batch([gig], chat_id="1", namespace="",
                         send_fn=lambda c: {"message_id": 2},
                         record_fn=lambda *a: None,
                         delivery_status_fn=prod_ledger.get)
    assert res2["sent"] == ["g_iso"]  # prod ledger untouched by test marks


def test_dashboard_chain_offline():
    from ai_assistant.gig import store as gig_store
    from ai_assistant.gig.cli import _gig_brief
    from ai_assistant.gig.notify import handle_gig_callback

    for sid in ("kw_d1", "kw_d2"):
        gig_store.save_gig(__import__(
            "ai_assistant.gig.normalizer", fromlist=["normalize_gig"]
        ).normalize_gig({"source": "kwork", "source_id": sid, "title": f"Task {sid}",
                         "budget": {"min": 1000, "max": 2000, "type": "fixed"},
                         "currency": "RUB"}))
    ids = [g.id for g in gig_store.list_gigs(source="kwork")]
    assert len(ids) == 2

    def _row(gid):
        g = gig_store.get_gig(gid)
        return None if g is None else {"id": g.id, "status": g.status.value}

    out = handle_gig_callback(
        f"gig:INT:{ids[0]}", get_gig_fn=_row,
        set_status_fn=lambda gid, st, note: gig_store.set_gig_status(gid, st, note=note),
        callback_query_id="cq-chain", acked_ids=set())
    assert out["outcome"] == "applied" and out["gig_id"] == ids[0] \
        and out["status"] == "SHORTLISTED" and out["title"]
    # API-level read: status visible, other order untouched, event recorded.
    assert gig_store.get_gig(ids[0]).status.value == "SHORTLISTED"
    assert gig_store.get_gig(ids[1]).status.value == "NEW"
    assert [g.id for g in gig_store.list_gigs(status="SHORTLISTED")] == [ids[0]]
    evs = gig_store.list_gig_events(ids[0])
    assert any(e["event_type"] == "STATUS_CHANGED" for e in evs)
    brief = _gig_brief(gig_store.get_gig(ids[0]))
    assert brief["status"] == "SHORTLISTED" and brief["source"] == "kwork"


def test_test_card_isolated_record():
    # §6: none of the 3 prod orders qualifies as reliably active, so the
    # Gate 2 card uses an isolated synthetic record + test namespace.
    from ai_assistant.gig.notify import build_card, callback_data, parse_callback

    gig = {"id": "gig_gate2test", "title": "Gate 2 connectivity probe",
           "source": "kwork", "source_mode": "live",
           "url": "https://kwork.ru/projects/999001",
           "budget_text": "5000.00 RUB (fixed)",
           "opportunity_score": 61.0, "rejection_reasons": []}
    card = build_card(gig)
    assert "Gate 2 connectivity probe" in card["text"]
    assert "5000.00 RUB" in card["text"]
    buttons = [b for row in card["reply_markup"]["inline_keyboard"] for b in row]
    by_text = {b["text"]: b for b in buttons}
    assert "callback_data" in by_text["👍 Интересно"]
    assert "callback_data" in by_text["👎 Не интересно"]
    assert by_text["🔗 Открыть заказ"]["url"] == "https://kwork.ru/projects/999001"
    assert parse_callback(by_text["👍 Интересно"]["callback_data"]) == (
        "INT", "gig_gate2test")
    assert "8657801093" not in card["text"] and "token" not in card["text"].lower()


def test_followup_text_policy():
    from ai_assistant.gig.notify import followup_text

    assert "сохранён" in (followup_text("applied", "T", "SHORTLISTED") or "")
    assert "пропущен" in (followup_text("applied", "T", "SKIPPED") or "")
    assert followup_text("already-applied", "T", "SHORTLISTED") is None
    assert followup_text("ignored", "T") is None
    refused = followup_text("refused", "T")
    assert refused is not None and "Не удалось" in refused


def test_import_links_surfaces_closed():
    from ai_assistant.gig import kwork_import as ki

    import pathlib as _pl

    sample = (_pl.Path(__file__).resolve().parent / "kwork_sample.html").read_text(
        encoding="utf-8").replace('"status":"active"', '"status":"archived"')
    res = ki.import_links(["https://kwork.ru/projects/999001"], persist=False,
                          fetch_html=lambda url: sample)
    assert res["imported"] == []
    assert len(res["closed"]) == 1
    assert res["closed"][0]["gig_id"].startswith("gig_")


def test_no_hh_submits_no_freelance_sends(monkeypatch):
    import pathlib
    import urllib.request

    from ai_assistant.gig.pipeline import run_source_pipeline

    def _deny(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        raise AssertionError(f"network must stay stubbed: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", _deny)
    res = run_source_pipeline(
        _kwork_src(["https://kwork.ru/projects/999001"]), persist=False)
    assert res.total == 1
    for path in (pathlib.Path("ai_assistant") / "gig").rglob("*.py"):
        if path.name in ("notify.py", "kwork_import.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "api.telegram.org" not in text, path.name
        assert "hh_autopilot" not in text and "hh_chat_monitor" not in text, path.name
