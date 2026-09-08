"""Regression tests for the fail-open handlers found by the BLE001 triage.

docs/ble001_triage.md lists the places where a swallowed exception produced an
"everything is fine" answer on a path that can reach a real submission. Each test
below forces the exception and asserts the handler now fails CLOSED.

These exist so the handlers can never quietly become optimistic again: the
optimistic fallbacks looked reasonable in isolation, which is exactly why they
survived review the first time.
"""
from __future__ import annotations

from types import SimpleNamespace

from ai_assistant import browser_executor as be
from ai_assistant import db
from ai_assistant import hh_application_queue as hq
from ai_assistant import hh_application_runner as hr


class _Boom(RuntimeError):
    """Stands in for any failure inside the audited call."""


# ---------------------------------------------------------------------------
# Finding #1 - CDPBrowserAdapter.inspect_page
# ---------------------------------------------------------------------------

def test_cdp_inspect_page_fails_closed_without_ws_url():
    adapter = be.CDPBrowserAdapter("http://127.0.0.1:9222")
    adapter.ws_url = None
    res = adapter.inspect_page()
    assert res["form_detected"] is False
    assert res["apply_button"] is False
    assert res["inspection_failed"] is True


def test_cdp_inspect_page_fails_closed_on_exception(monkeypatch):
    adapter = be.CDPBrowserAdapter("http://127.0.0.1:9222")
    adapter.ws_url = "ws://127.0.0.1:9222/devtools/page/1"

    def _boom(coro):
        coro.close()  # otherwise pytest warns about a never-awaited coroutine
        raise _Boom("cdp died")

    monkeypatch.setattr(adapter, "_sync_run", _boom)
    res = adapter.inspect_page()
    assert res["form_detected"] is False
    assert res["apply_button"] is False
    assert res["inspection_failed"] is True


def test_cdp_inspect_page_fails_closed_on_empty_result(monkeypatch):
    adapter = be.CDPBrowserAdapter("http://127.0.0.1:9222")
    adapter.ws_url = "ws://127.0.0.1:9222/devtools/page/1"

    def _empty(coro):
        coro.close()
        return {}

    monkeypatch.setattr(adapter, "_sync_run", _empty)
    res = adapter.inspect_page()
    assert res["form_detected"] is False
    assert res["inspection_failed"] is True


def test_cdp_inspect_page_defines_every_key_callers_branch_on():
    """Callers branch on .get() of these four.

    The old optimistic fallback omitted `captcha` and `login_required`, so
    .get() returned None and a failed inspection walked past the captcha, login
    and missing-form checks in one go.
    """
    adapter = be.CDPBrowserAdapter("http://127.0.0.1:9222")
    adapter.ws_url = None
    res = adapter.inspect_page()
    for key in ("form_detected", "apply_button", "captcha", "login_required"):
        assert key in res, f"missing {key}: a caller .get() would read it as falsy"


# ---------------------------------------------------------------------------
# Finding #2 - two-layer fail-open across queue and runner
# ---------------------------------------------------------------------------

def test_queue_audit_failure_is_never_safe_to_submit(monkeypatch):
    monkeypatch.setattr(db, "list_hh_applications", lambda limit=200: [{
        "application_id": "app-1",
        "vacancy_stable_id": "hh:123",
        "title": "Python Developer",
        "employer": "Acme",
        "state": "READY_TO_SUBMIT",
        "questionnaire_id": "q-1",
    }])
    # Questionnaire present and looking ready - exactly the condition that used
    # to convert a crashed audit into SAFE_TO_SUBMIT.
    monkeypatch.setattr(db, "get_hh_questionnaire", lambda qid: {"status": "READY_TO_SUBMIT"})
    monkeypatch.setattr(hq, "can_submit", lambda app_id: SimpleNamespace(allowed=True, reason=""))

    def _boom(*args, **kwargs):
        raise _Boom("audit crashed")

    monkeypatch.setattr(hq, "audit_questionnaire", _boom)

    items = hq.get_controlled_application_queue()
    assert len(items) == 1
    assert items[0].audit_state == "NEEDS_CORRECTION"


def test_runner_precheck_fails_when_audit_crashes(monkeypatch):
    item = hq.HHQueueItem(
        application_id="app-1",
        vacancy_id="123",
        vacancy_title="Python Developer",
        company="Acme",
        application_state="READY_TO_SUBMIT",
        questionnaire_id="q-1",
        questionnaire_state="READY_TO_SUBMIT",
        # The laundered value: the queue said SAFE even though its own audit
        # crashed. The runner used to read this straight back as PASS.
        audit_state="SAFE_TO_SUBMIT",
        can_submit_allowed=True,
        can_submit_reason="",
        last_updated="n/a",
    )
    monkeypatch.setattr(hr, "get_controlled_application_queue", lambda filter_mode=None: [item])
    monkeypatch.setattr(hr, "resolve_hh_vacancy_url", lambda vacancy_id: "https://hh.ru/vacancy/123")

    def _boom(*args, **kwargs):
        raise _Boom("audit crashed")

    monkeypatch.setattr(hr, "audit_questionnaire", _boom)

    res = hr.preview_next_application()
    assert res.pre_submit_audit == hr.RunnerPreCheckStatus.FAIL


# ---------------------------------------------------------------------------
# Finding #3 - db.reconcile_digest_attempt
# ---------------------------------------------------------------------------

def test_reconcile_refuses_unreadable_payload():
    db.init_db()
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO telegram_delivery_records "
        "(delivery_key, notification_type, chat_id, delivered_at, status, payload) "
        "VALUES (?, 'digest_batch', ?, ?, 'ATTEMPTING', ?)",
        ("batch-unreadable", "-1", "2026-01-01T00:00:00+00:00", "{not json"),
    )
    conn.commit()
    conn.close()

    assert db.reconcile_digest_attempt("batch-unreadable", "DELIVERED") is False

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT status, payload FROM telegram_delivery_records WHERE delivery_key = ?",
        ("batch-unreadable",),
    )
    row = cur.fetchone()
    conn.close()

    assert row[0] == "ATTEMPTING", "batch must not be marked delivered on a read failure"
    assert row[1] == "{not json", "payload must not be overwritten with an empty list"


# ---------------------------------------------------------------------------
# Finding #4 - db.get_production_health duplicate-key probe
# ---------------------------------------------------------------------------

def test_health_is_unknown_when_duplicate_check_itself_fails(monkeypatch):
    db.init_db()

    class _BrokenCursor:
        def execute(self, sql, *args):
            if "GROUP BY delivery_key" in sql:
                raise _Boom("duplicate probe failed")
            return self

        def fetchall(self):
            return []

        def fetchone(self):
            return (0,)

        def close(self):
            pass

    class _BrokenConn:
        def cursor(self):
            return _BrokenCursor()

        def execute(self, sql, *args):
            # ensure_schema() goes through conn.execute, not conn.cursor()
            return self.cursor().execute(sql, *args)

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(db, "get_connection", lambda *a, **kw: _BrokenConn())

    res = db.get_production_health()
    # Downstream probes may escalate UNKNOWN to UNHEALTHY on the same broken
    # connection; what matters is that we never report HEALTHY and that the
    # inconclusive probe says so out loud.
    assert res["health"] != "HEALTHY", "a broken probe must not report HEALTHY"
    assert any("Duplicate delivery key check failed" in a["message"] for a in res["alerts"])
