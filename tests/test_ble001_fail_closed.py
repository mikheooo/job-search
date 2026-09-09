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


# ---------------------------------------------------------------------------
# Finding #7 - a failed extraction must not look like an empty clean form
# ---------------------------------------------------------------------------
# browser_executor.extract_application_form() used to return an empty snapshot
# (questions=[], controls=[], auth_form=False) both when the page was read
# successfully and when extraction blew up. Downstream gates ask "are all
# questions resolved?", which is vacuously true on an empty set - so a crashed
# extraction walked through them as a clean form. The snapshot now carries an
# explicit error flag; these tests pin that flag on every path that produces it.

def test_snapshot_without_page_is_marked_as_error():
    adapter = be.PlaywrightBrowserAdapter()
    res = adapter.extract_application_form()
    assert res["questions"] == []
    assert res["error"] is True
    assert res["error_reason"] == "page_not_open"


def test_snapshot_marks_error_when_extraction_raises():
    adapter = be.PlaywrightBrowserAdapter()

    class _DeadPage:
        def content(self):
            raise _Boom("page crashed")

        def inner_text(self, selector):
            raise _Boom("page crashed")

    adapter.page = _DeadPage()
    res = adapter.extract_application_form()
    assert res["questions"] == []
    assert res["error"] is True
    assert "extraction_failed" in (res["error_reason"] or "")


def test_mock_adapter_snapshot_is_not_marked_as_error():
    adapter = be.MockBrowserAdapter(simulate={"questions": []})
    assert adapter.extract_application_form()["error"] is False


def test_mock_adapter_can_simulate_extraction_error():
    adapter = be.MockBrowserAdapter(simulate={"error": True, "error_reason": "boom"})
    res = adapter.extract_application_form()
    assert res["error"] is True
    assert res["error_reason"] == "boom"


def test_extractor_propagates_error_flag_into_meta():
    from ai_assistant.hh_extractor import extract_application_form

    form = extract_application_form(
        vacancy_stable_id="hh:1",
        url="https://hh.ru/vacancy/1",
        dom_snapshot={"error": True, "error_reason": "page_not_open"},
    )
    assert form.extraction_meta["error"] is True
    assert form.extraction_meta["error_reason"] == "page_not_open"


def test_extractor_meta_error_defaults_to_false():
    from ai_assistant.hh_extractor import extract_application_form

    form = extract_application_form(
        vacancy_stable_id="hh:1",
        url="https://hh.ru/vacancy/1",
        dom_snapshot={"questions": []},
    )
    assert form.extraction_meta["error"] is False


def test_review_gate_blocks_on_extraction_error():
    from ai_assistant.application_review_gate import GateStatus, build_review_gate
    from ai_assistant.hh_extractor import ApplicationForm, ApplicationType

    pkg = SimpleNamespace(
        validation_status="VALID", answers=[], cover_letter="",
        review_reasons=[], warnings=[], vacancy_stable_id="hh:1",
    )
    plan = SimpleNamespace(status="VALID", unresolved=[])
    orch = SimpleNamespace(
        verdict="VERIFIED", failed_operations=0, skipped_operations=0,
        errors=[], group_checks=[],
    )

    broken = ApplicationForm(
        source="hh", vacancy_stable_id="hh:1",
        application_type=ApplicationType.unknown, questions=[],
        extraction_meta={"error": True, "error_reason": "page_not_open"},
    )
    gate = build_review_gate(pkg, plan, orch, {}, form=broken)
    assert gate.status == GateStatus.BLOCKED
    assert any("form extraction error" in r for r in gate.block_reasons)

    # Sanity: an honestly empty form is NOT blocked by this rule. Otherwise the
    # gate would just block everything and the test above would be decorative.
    clean = ApplicationForm(
        source="hh", vacancy_stable_id="hh:1",
        application_type=ApplicationType.unknown, questions=[],
    )
    gate2 = build_review_gate(pkg, plan, orch, {}, form=clean)
    assert not any("form extraction error" in r for r in gate2.block_reasons)
    assert gate2.status == GateStatus.READY_FOR_HUMAN_REVIEW
