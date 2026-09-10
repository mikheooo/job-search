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
from typing import ClassVar

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


# ---------------------------------------------------------------------------
# Finding #8 - the kill-switch must be turnable OFF from the environment
# ---------------------------------------------------------------------------
# config.py did `load_dotenv(..., override=True)` at import, which overwrites a
# real environment variable with the .env value. The gate then read
# `env OR config.SUBMIT_ALLOWED`, and config had already frozen .env's value -
# so `SUBMIT_ALLOWED=false` in the shell could never disarm an armed .env.
# config.submit_allowed() re-reads at call time and lets an explicit "off" win.

def test_submit_allowed_off_from_env_disables(monkeypatch):
    from ai_assistant import config

    monkeypatch.setenv("SUBMIT_ALLOWED", "false")
    assert config.submit_allowed() is False


def test_submit_allowed_on_from_env_enables(monkeypatch):
    from ai_assistant import config

    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    assert config.submit_allowed() is True


def test_submit_allowed_off_wins_over_stale_dotenv_value(monkeypatch):
    """An operator's 'false' must not be re-armed by a stale .env 'true'."""
    from ai_assistant import config

    monkeypatch.setenv("SUBMIT_ALLOWED", "false")
    monkeypatch.setattr(config, "SUBMIT_ALLOWED", True, raising=False)
    assert config.submit_allowed() is False


# ---------------------------------------------------------------------------
# Finding #9 - non-HH sources bypassed every gate, including the kill-switch
# ---------------------------------------------------------------------------
# submit_application_in_browser routes only "hh:*" ids through
# execute_hh_submission (the 11 gates). Every other source - habr_career,
# himalayas, remoteok, weworkremotely - fell through to the legacy branch and
# called adapter.submit_application() directly. The kill-switch is now checked
# for all sources, before anything touches the browser.

def test_non_hh_source_is_blocked_by_kill_switch(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")
    res = be.submit_application_in_browser(
        "remoteok:123", confirm_submit=True, dry_run=False
    )
    assert res.status == "BLOCKED"
    assert "SUBMIT_ALLOWED" in (res.error or "")


def test_non_hh_source_not_blocked_by_kill_switch_when_enabled(monkeypatch):
    """Counter-check: with the switch on we must NOT trip this specific gate."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    res = be.submit_application_in_browser(
        "remoteok:123", confirm_submit=True, dry_run=False
    )
    assert "SUBMIT_ALLOWED" not in (res.error or "")


# ---------------------------------------------------------------------------
# Finding #10 - extraction silently degraded to a fingerprintable headless
# ---------------------------------------------------------------------------
# PlaywrightBrowserAdapter.open() tried connect_over_cdp(), and on ANY failure
# quietly launched headless Playwright instead. Measured with
# tools/browser_fingerprint_probe.py, that browser is trivially detectable:
# navigator.webdriver = true, HeadlessChrome in the UA, zero plugins, a
# SwiftShader (software) WebGL renderer. So hh.ru was being read by a browser
# it can spot instantly - and nothing in the pipeline said a word about it,
# because the flag did not exist. Submission ran through the real browser while
# extraction ran through a bot; same pipeline, two different browsers.
#
# The fallback still exists (a hard failure would be worse), but it now leaves a
# flag on the snapshot and in extraction_meta, and both gates fail closed on it.

class _FakePage:
    def __init__(self):
        self.url = "https://hh.ru/vacancy/1"

    def goto(self, url, wait_until=None, timeout=None):
        return None

    def title(self):
        return "Vacancy"

    def content(self):
        return "<html><body>ok</body></html>"


class _FakeContext:
    def new_page(self):
        return _FakePage()


class _FakeBrowser:
    contexts: ClassVar[list] = []

    def new_context(self, **kwargs):
        return _FakeContext()

    def close(self):
        pass


def _patch_playwright(monkeypatch, *, cdp_works):
    import playwright.sync_api as papi

    class _Chromium:
        def connect_over_cdp(self, url):
            if not cdp_works:
                raise RuntimeError("connect_over_cdp refused: connection refused")
            return _FakeBrowser()

        def launch(self, headless=True):
            return _FakeBrowser()

    class _PW:
        def __init__(self):
            self.chromium = _Chromium()

        def stop(self):
            pass

    monkeypatch.setattr(
        papi, "sync_playwright",
        lambda: type("SP", (), {"start": staticmethod(lambda: _PW())})(),
    )


def test_cdp_attach_failure_is_flagged_and_logged(monkeypatch, caplog):
    """A silent fall back to headless is the bug. It must be visible twice:
    once in the log, once on the returned snapshot."""
    import logging

    _patch_playwright(monkeypatch, cdp_works=False)
    adapter = be.PlaywrightBrowserAdapter()
    with caplog.at_level(logging.WARNING, logger="ai_assistant.browser_executor"):
        res = adapter.open("https://hh.ru/vacancy/1")

    assert res["cdp_fallback"] is True
    assert "connect_over_cdp" in (res["cdp_fallback_reason"] or "")
    assert any("headless" in r.getMessage().lower() for r in caplog.records)


def test_successful_cdp_attach_leaves_fallback_flag_clear(monkeypatch):
    """Counter-check: without it the test above would pass for every browser."""
    _patch_playwright(monkeypatch, cdp_works=True)
    adapter = be.PlaywrightBrowserAdapter()
    res = adapter.open("https://hh.ru/vacancy/1")

    assert res["cdp_fallback"] is False
    assert res["cdp_fallback_reason"] is None


def test_snapshot_exposes_cdp_fallback():
    adapter = be.PlaywrightBrowserAdapter()
    adapter._cdp_fallback_reason = "connect_over_cdp(http://127.0.0.1:9222) failed: OSError"
    adapter.page = _FakePage()
    res = adapter.extract_application_form()

    # The flag rides on the snapshot regardless of how the read ended.
    assert res["cdp_fallback"] is True
    assert "connect_over_cdp" in res["cdp_fallback_reason"]


def test_mock_adapter_has_no_cdp_fallback_by_default():
    """The mock drives no browser, so it must never look degraded."""
    res = be.MockBrowserAdapter(simulate={"questions": []}).extract_application_form()
    assert res["cdp_fallback"] is False


def test_mock_adapter_can_simulate_cdp_fallback():
    res = be.MockBrowserAdapter(
        simulate={"cdp_fallback": True, "cdp_fallback_reason": "boom"}
    ).extract_application_form()
    assert res["cdp_fallback"] is True
    assert res["cdp_fallback_reason"] == "boom"


def test_extractor_propagates_cdp_fallback_into_meta():
    from ai_assistant.hh_extractor import extract_application_form

    form = extract_application_form(
        vacancy_stable_id="hh:1",
        url="https://hh.ru/vacancy/1",
        dom_snapshot={"cdp_fallback": True, "cdp_fallback_reason": "cdp dead"},
    )
    assert form.extraction_meta["cdp_fallback"] is True
    assert form.extraction_meta["cdp_fallback_reason"] == "cdp dead"


def test_extractor_meta_cdp_fallback_defaults_to_false():
    from ai_assistant.hh_extractor import extract_application_form

    form = extract_application_form(
        vacancy_stable_id="hh:1",
        url="https://hh.ru/vacancy/1",
        dom_snapshot={"questions": []},
    )
    assert form.extraction_meta["cdp_fallback"] is False


def test_review_gate_blocks_on_cdp_fallback():
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

    degraded = ApplicationForm(
        source="hh", vacancy_stable_id="hh:1",
        application_type=ApplicationType.unknown, questions=[],
        extraction_meta={"cdp_fallback": True, "cdp_fallback_reason": "cdp dead"},
    )
    gate = build_review_gate(pkg, plan, orch, {}, form=degraded)
    assert gate.status == GateStatus.BLOCKED
    assert any("headless browser fallback" in r for r in gate.block_reasons)

    # Sanity: a form read by the real browser is NOT blocked by this rule.
    clean = ApplicationForm(
        source="hh", vacancy_stable_id="hh:1",
        application_type=ApplicationType.unknown, questions=[],
    )
    gate2 = build_review_gate(pkg, plan, orch, {}, form=clean)
    assert not any("headless browser fallback" in r for r in gate2.block_reasons)
    assert gate2.status == GateStatus.READY_FOR_HUMAN_REVIEW


# ---------------------------------------------------------------------------
# Finding #10, part 2 - one resolver, and a proxy must not fake a dead browser
# ---------------------------------------------------------------------------
# The launcher resolved CDP as "9222 -> HH_CDP_URL -> BrowserOS 9110" while
# PlaywrightBrowserAdapter resolved "CDP_URL -> HH_CDP_URL -> 9222" with no
# BrowserOS fallback at all. Two answers to one question. resolve_cdp_url() is
# now the single source of truth for both.

def test_resolve_cdp_url_prefers_cdp_url_env(monkeypatch):
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.setenv("CDP_URL", "http://127.0.0.1:7777")
    monkeypatch.setenv("HH_CDP_URL", "http://127.0.0.1:8888")
    assert hl.resolve_cdp_url(None, allow_browseros=False) == "http://127.0.0.1:7777"


def test_resolve_cdp_url_falls_back_to_hh_cdp_url_env(monkeypatch):
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.setenv("HH_CDP_URL", "http://127.0.0.1:8888")
    assert hl.resolve_cdp_url(None, allow_browseros=False) == "http://127.0.0.1:8888"


def test_resolve_cdp_url_explicit_argument_wins(monkeypatch):
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.setenv("CDP_URL", "http://127.0.0.1:7777")
    assert hl.resolve_cdp_url("http://127.0.0.1:6666", allow_browseros=False) == "http://127.0.0.1:6666"


def test_resolve_cdp_url_treats_plain_default_as_no_decision(monkeypatch):
    """Passing the default explicitly is not a decision - env still wins."""
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.setenv("HH_CDP_URL", "http://127.0.0.1:8888")
    out = hl.resolve_cdp_url(hl.DEFAULT_HH_CDP_URL, allow_browseros=False)
    assert out == "http://127.0.0.1:8888"


def test_resolve_cdp_url_does_not_swap_when_env_pins_the_default(monkeypatch):
    """Regression. `HH_CDP_URL=http://127.0.0.1:9222` is the operator saying
    "use 9222", not silence. Probing anyway and hopping to BrowserOS because
    9110 happens to be up would quietly override a deliberate choice - the same
    fail-open this whole finding is about, just in the resolver itself."""
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.setenv("HH_CDP_URL", hl.DEFAULT_HH_CDP_URL)
    # 9110 answers, 9222 does not: the tempting moment to swap.
    monkeypatch.setattr(
        hl, "is_cdp_reachable",
        lambda url, timeout=1.5: url == hl.BROWSEROS_CDP_URL,
    )
    assert hl.resolve_cdp_url(None, allow_browseros=True) == hl.DEFAULT_HH_CDP_URL


def test_resolve_cdp_url_logs_browseros_swap(monkeypatch, caplog):
    """Swapping browsers swaps profiles. It must never happen quietly."""
    import logging

    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_URL", raising=False)
    monkeypatch.setattr(
        hl, "probe_cdp",
        lambda url, timeout=1.5: hl.CDP_ALIVE if url == hl.BROWSEROS_CDP_URL else hl.CDP_DEAD,
    )
    with caplog.at_level(logging.WARNING, logger="ai_assistant.hh_browser_launcher"):
        out = hl.resolve_cdp_url(None, allow_browseros=True)

    assert out == hl.BROWSEROS_CDP_URL
    assert any("BrowserOS" in r.getMessage() for r in caplog.records)


def test_resolve_cdp_url_does_not_hop_on_slow_chrome(monkeypatch, caplog):
    """Finding #13. A timeout is not an absence.

    Measured: /json/version answers in 0.6-17 ms, but a busy Chrome misses a
    300 ms deadline often enough that the resolver was hopping to BrowserOS -
    a different profile, normally without the hh.ru session - purely at random.
    Slow must mean "stay", only a refused connection means "hop".
    """
    import logging

    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_URL", raising=False)
    monkeypatch.setattr(
        hl, "probe_cdp",
        lambda url, timeout=1.5: hl.CDP_TIMEOUT if url == hl.DEFAULT_HH_CDP_URL else hl.CDP_ALIVE,
    )
    with caplog.at_level(logging.WARNING, logger="ai_assistant.hh_browser_launcher"):
        out = hl.resolve_cdp_url(None, allow_browseros=True)

    assert out == hl.DEFAULT_HH_CDP_URL, "a slow Chrome must not cost us the profile"
    assert not any("BrowserOS" in r.getMessage() for r in caplog.records)


def test_probe_cdp_distinguishes_timeout_from_dead(monkeypatch):
    """probe_cdp() is the routing signal, so its three answers must be distinct.

    is_cdp_reachable() deliberately collapses TIMEOUT into False - fine for a
    health check, wrong for choosing a browser. This pins the third state.
    """
    import urllib.error

    from ai_assistant import hh_browser_launcher as hl

    class _Opener:
        def __init__(self, exc):
            self.exc = exc

        def open(self, *a, **kw):
            raise self.exc

    # Nothing listening -> definitively dead.
    monkeypatch.setattr(
        hl, "_NO_PROXY_OPENER",
        _Opener(urllib.error.URLError(ConnectionRefusedError(10061, "refused"))),
    )
    assert hl.probe_cdp("http://127.0.0.1:1", timeout=0.1) == hl.CDP_DEAD
    assert hl.is_cdp_reachable("http://127.0.0.1:1", timeout=0.1) is False

    # Listening but slow -> timeout, NOT dead.
    monkeypatch.setattr(
        hl, "_NO_PROXY_OPENER",
        _Opener(urllib.error.URLError(TimeoutError("timed out"))),
    )
    assert hl.probe_cdp("http://127.0.0.1:1", timeout=0.1) == hl.CDP_TIMEOUT
    assert hl.is_cdp_reachable("http://127.0.0.1:1", timeout=0.1) is False


def test_is_cdp_reachable_ignores_proxy_env(monkeypatch):
    """urllib honours http_proxy, so with a proxy set a live browser answered
    "502 Bad Gateway" and read as dead - which then triggered the BrowserOS
    swap for no reason. CDP is localhost: always talk to it directly."""
    import http.server
    import threading

    from ai_assistant import hh_browser_launcher as hl

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"Browser": "Chrome/1.2.3", "webSocketDebuggerUrl": "ws://x"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        assert hl.is_cdp_reachable(f"http://127.0.0.1:{port}", timeout=3.0) is True
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Finding #11 - "blocked" was true on every hh.ru page
# ---------------------------------------------------------------------------

# A trimmed copy of what hh.ru actually serves: the i18n bundle contains the
# captcha error string on EVERY response, challenge or not.
_HH_I18N_HTML = (
    '<html><body><h1>Вакансия Python developer</h1>'
    '<script>window.i18n={"error.postlogon.hidden":"работодатель скрыт",'
    '"error.signup.captcha.invalid":"пожалуйста, подтвердите, что вы не робот"}'
    '</script></body></html>'
)


def test_blocked_check_ignores_i18n_captcha_string():
    """Finding #11. Grepping raw HTML for 'captcha' flagged every hh.ru page.

    Measured on https://hh.ru/vacancy/134835019: the only match was at byte
    1908203, inside `error.signup.captcha.invalid`. So `blocked` was True for a
    perfectly readable vacancy - and `blocked` drives real branching
    (browser_executor ~2026/2668/2966), not just logging.
    """
    from ai_assistant import browser_executor as be

    assert be._detect_page_blocked(
        _HH_I18N_HTML, "Вакансия Python developer", "Вакансия Python developer"
    ) is False


def test_blocked_check_still_flags_a_real_captcha():
    """The fix must not turn into a fail-open: a live challenge still blocks."""
    from ai_assistant import browser_executor as be

    challenge = '<div data-qa="captcha" class="bloko-modal">Подтвердите</div>'
    assert be._detect_page_blocked(challenge, "Подтвердите", "hh.ru") is True


def test_blocked_check_still_flags_cloudflare_and_404():
    from ai_assistant import browser_executor as be

    assert be._detect_page_blocked('<div class="cf-challenge">x</div>', "x", "hh") is True
    assert be._detect_page_blocked("<html></html>", "not found", "404 Not Found") is True


def test_blocked_check_flags_visible_access_denied_only():
    """Text markers are matched against visible body text, never raw HTML.

    'access denied' buried in a script must not block; the same words rendered
    on screen must.
    """
    from ai_assistant import browser_executor as be

    hidden = "<script>var m='access denied';</script>"
    assert be._detect_page_blocked(hidden, "Вакансия", "Вакансия") is False
    assert be._detect_page_blocked("<html></html>", "Access Denied", "hh") is True


# ---------------------------------------------------------------------------
# Finding #12 - CDPBrowserAdapter still talked to CDP through the proxy
# ---------------------------------------------------------------------------

def test_cdp_adapter_open_uses_proxy_free_opener(monkeypatch):
    """Finding #9/#12. CDPBrowserAdapter.open() called urllib.request.urlopen,
    which honours http_proxy: with a proxy in the environment the submission
    path got '502 Bad Gateway' from a healthy browser and reported blocked.
    Same bug as the launcher, same fix - one opener, no proxy."""
    import json as _json

    from ai_assistant import browser_executor as be
    from ai_assistant import hh_browser_launcher as hl

    opened: list[str] = []

    class _Resp:
        def read(self):
            return _json.dumps({
                "id": "TAB1",
                "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/TAB1",
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            opened.append(getattr(req, "full_url", str(req)))
            return _Resp()

    monkeypatch.setattr(hl, "_NO_PROXY_OPENER", _Opener())
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    # A proxy that would eat the request if urlopen() were still used.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("CDP_URL", "http://127.0.0.1:9222")

    ad = be.CDPBrowserAdapter("http://127.0.0.1:9222")

    def _run(coro):
        coro.close()  # never awaited on purpose; we only stub the result
        return {"url": "https://hh.ru/vacancy/1", "title": "Python developer"}

    monkeypatch.setattr(ad, "_sync_run", _run)
    res = ad.open("https://hh.ru/vacancy/1")

    assert opened, "open() must go through the proxy-free opener"
    assert res.get("blocked") is False, f"proxy leaked into the CDP call: {res}"


def test_no_cdp_call_goes_through_urlopen():
    """Finding #9/#12 as a class, not as five separate fixes.

    urllib.request.urlopen() honours http_proxy, so any CDP call made with it
    turns a live localhost browser into "502 Bad Gateway" and reads as absent.
    Fixed in browser_executor, hh_vacancy_navigator and prefill_execute - this
    pins that it stays fixed, including in code nobody has written yet.

    One deliberate exception: telegram_notifier talks to api.telegram.org, an
    external host, where honouring a proxy is the correct behaviour. The second
    assertion keeps that exception from rotting once it stops applying.
    """
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[1] / "ai_assistant"
    telegram = pkg / "telegram_notifier.py"

    assert "urllib.request.urlopen" in telegram.read_text(encoding="utf-8"), (
        "the telegram_notifier exception is stale - it no longer uses urlopen, "
        "so drop it from the allowlist instead of keeping a hole open"
    )

    offenders = [
        p.name
        for p in sorted(pkg.glob("*.py"))
        if p.name != "telegram_notifier.py"
        and "urllib.request.urlopen" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "CDP is localhost - use hh_browser_launcher._NO_PROXY_OPENER instead of "
        f"urllib.request.urlopen: {offenders}"
    )


# ---------------------------------------------------------------------------
# Finding #14 - cli.py resolved "which browser" its own way
# ---------------------------------------------------------------------------

def test_cli_cdp_default_follows_the_resolver(monkeypatch):
    """cli.py used to read only HH_CDP_URL, once, at import time. That is a
    second copy of resolve_cdp_url() and it disagreed: with CDP_URL set the
    constant said 9110 while every adapter said the pinned value. Two browsers
    means two Chrome profiles, and the submit path was driving the one that is
    not logged into hh.ru.
    """
    from ai_assistant import cli
    from ai_assistant import hh_browser_launcher as hl

    monkeypatch.setenv("CDP_URL", "http://127.0.0.1:9223")
    monkeypatch.setenv("HH_CDP_URL", "http://127.0.0.1:9110")

    assert cli._default_hh_cdp_url() == "http://127.0.0.1:9223"
    assert cli._default_hh_cdp_url() == hl.resolve_cdp_url()


def test_resolve_hh_evaluate_uses_the_resolved_endpoint(monkeypatch):
    """The same claim one level down: what actually reaches make_cdp_evaluate
    must be the resolved endpoint, not a frozen snapshot."""
    from ai_assistant import cli

    monkeypatch.setenv("CDP_URL", "http://127.0.0.1:9223")
    seen = []

    def _fake_make(cdp, sub):
        seen.append((cdp, sub))
        return lambda *a, **k: None

    monkeypatch.setattr(cli, "make_cdp_evaluate", _fake_make)
    cli._resolve_hh_evaluate(None, "hh.ru")

    assert seen, "_resolve_hh_evaluate built no evaluate_fn"
    assert seen[0][0] == "http://127.0.0.1:9223", f"stale endpoint reached CDP: {seen[0]}"


def test_no_second_copy_of_cdp_resolution():
    """Finding #14 as a class. hh_browser_launcher.resolve_cdp_url() owns the
    question "which browser are we driving"; any other module reading the CDP
    environment itself is a copy that can disagree. One owner, allowed:
    the launcher.
    """
    import ast
    import pathlib

    def _reads_cdp_env(path: pathlib.Path) -> bool:
        """Substring matching would fire on this very comment, so walk the AST
        and look for the actual call: os.getenv("HH_CDP_URL", ...)."""
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts, cur = [], node.func
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                continue
            parts.append(cur.id)
            if ".".join(reversed(parts)) not in ("os.getenv", "os.environ.get"):
                continue
            if any(
                isinstance(a, ast.Constant) and a.value in ("HH_CDP_URL", "CDP_URL")
                for a in node.args
            ):
                return True
        return False

    pkg = pathlib.Path(__file__).resolve().parents[1] / "ai_assistant"
    offenders = [
        path.name
        for path in sorted(pkg.glob("*.py"))
        if path.name != "hh_browser_launcher.py" and _reads_cdp_env(path)
    ]
    assert not offenders, (
        "ask hh_browser_launcher.resolve_cdp_url() instead of reading the env: "
        f"{offenders}"
    )


def test_nothing_reads_the_frozen_cdp_constant():
    """cli._DEFAULT_HH_CDP_URL was a module-level snapshot and four modules
    imported it (hh_application_runner, hh_message_watcher,
    hh_post_submit_verifier, cli itself). It is gone. Keep it gone: re-adding
    the name re-adds a second, divergent answer.
    """
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[1] / "ai_assistant"
    offenders = [
        path.name
        for path in sorted(pkg.glob("*.py"))
        if "_DEFAULT_HH_CDP_URL" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"use cli._default_hh_cdp_url(): {offenders}"


def test_cdp_adapter_is_built_from_the_resolver():
    """`submit --adapter cdp` hardcoded DEFAULT_HH_CDP_URL, i.e. 9222 no matter
    what .env says, so the submit path drove a different profile than everyone
    else. The call site must ask the resolver like the rest of the code.
    """
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[1] / "ai_assistant"
    cli_src = (pkg / "cli.py").read_text(encoding="utf-8")

    assert "CDPBrowserAdapter(DEFAULT_HH_CDP_URL)" not in cli_src, (
        "hardcoded 9222 in the submit path - it ignores .env and CDP_URL"
    )
    assert "CDPBrowserAdapter(_default_hh_cdp_url())" in cli_src, (
        "the submit path must ask the resolver, same as every adapter"
    )

    for name in ("hh_application_runner.py", "hh_message_watcher.py", "hh_post_submit_verifier.py"):
        text = (pkg / name).read_text(encoding="utf-8")
        assert "_default_hh_cdp_url()" in text, f"{name} still uses a frozen endpoint"


# ---------------------------------------------------------------------------
# Finding #15 - the "is this the right vacancy?" check was never wired up
# ---------------------------------------------------------------------------

def test_live_page_blocks_a_different_vacancy():
    """Real evidence, not a synthetic case: vacancies.json advertises
    /vacancy/135489102 as "Инженер по автоматизации процессов (AI Agents /
    n8n / Python)", and hh.ru actually serves "Продавец (Чебоксары)".

    The numeric id cannot catch this - it is read from the URL we just opened,
    so it matches by construction. Only the title can.
    """
    import json

    from ai_assistant.hh_live_page_checks import check_live_page

    payload = {
        "ok": True,
        "url": "https://hh.ru/vacancy/135489102",
        "title": "Продавец (Чебоксары, Гагарина Ю., 17)",
        "has_submit_btn": True,
        "has_apply_btn": True,
    }
    res = check_live_page(
        lambda js: json.dumps(payload),
        expected_vacancy_id="hh:135489102",
        expected_title="Инженер по автоматизации процессов (AI Agents / n8n / Python)",
    )
    assert res.is_ok is False, "a live page for a different job must not be submittable"
    assert res.error_reason == "WRONG_PAGE", res.reason


def test_live_page_accepts_the_matching_vacancy():
    """Counter-check: the same check must not become a new way to block
    everything - a page that really is the vacancy still passes."""
    import json

    from ai_assistant.hh_live_page_checks import check_live_page

    payload = {
        "ok": True,
        "url": "https://hh.ru/vacancy/128659037",
        "title": "QA Automation Engineer (Java)",
        "has_submit_btn": True,
        "has_apply_btn": True,
    }
    res = check_live_page(
        lambda js: json.dumps(payload),
        expected_vacancy_id="hh:128659037",
        expected_title="QA Automation Engineer (Java)",
    )
    assert res.is_ok is True, f"the right page must stay submittable: {res.reason}"
    assert res.title_matched is True


def test_production_passes_expected_title_to_the_live_page_check():
    """check_live_page() silently skips the title step when expected_title is
    None (`else: res.title_matched = True`), and the only production call site
    never passed it - so the wrong-vacancy check existed, was tested, and still
    protected nothing.

    AST, not substring: a substring version would be satisfied by the tests
    above, which is exactly how this stayed invisible.
    """
    import ast
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[1] / "ai_assistant"
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "check_live_page":
                continue
            if not any(k.arg == "expected_title" for k in node.keywords):
                offenders.append(path.name)
    assert not offenders, (
        "check_live_page() without expected_title skips the wrong-vacancy "
        f"check entirely: {offenders}"
    )


def test_submit_refuses_when_the_vacancy_is_unknown(monkeypatch):
    """No row -> no expected title -> check_live_page() skips its title step ->
    whatever page happens to be open passes every check. Refuse instead.

    Reachable in production, not theoretical: browser_executor's submit path
    reads the row as `vac = _row_to_vacancy(row) if row else None` and then
    skips the hard-constraint gate entirely (`if vac:`), so a vacancy missing
    from the DB also skips remote_required today.
    """
    from ai_assistant import hh_live_page_checks
    from ai_assistant.hh_submission import execute_hh_submission

    seen = {}
    monkeypatch.setattr(db, "get_vacancy_by_id", lambda vid: None)
    monkeypatch.setattr(
        hh_live_page_checks,
        "check_live_page",
        lambda *a, **kw: seen.setdefault("called", True),
    )

    res = execute_hh_submission(
        "hh:999000111",
        evaluate_fn=lambda js: "{}",
        dry_run=True,
    )
    assert res.status == "BLOCKED", res.reason
    assert res.submit_count == 0
    assert "999000111" in res.reason
    # The point: we never even looked at the page, because we would not have
    # been able to tell whether it was the right one.
    assert not seen, "must refuse before inspecting a page it cannot identify"


def test_submit_still_inspects_the_page_when_the_vacancy_is_known(monkeypatch):
    """Counter-check: the refusal must not become a new way to block
    everything. A known vacancy still gets its title through to the check."""

    from ai_assistant import hh_live_page_checks
    from ai_assistant.hh_submission import execute_hh_submission

    seen = {}
    monkeypatch.setattr(db, "get_vacancy_by_id", lambda vid: ("row",))
    monkeypatch.setattr(
        db, "_row_to_vacancy", lambda row: SimpleNamespace(title="Python Developer")
    )

    def fake_check(evaluate_fn, expected_vacancy_id=None, expected_title=None, **kw):
        seen["title"] = expected_title
        seen["vid"] = expected_vacancy_id
        return SimpleNamespace(
            is_ok=False,
            already_applied=False,
            error_reason="STUB",
            reason="stub stop",
            current_url="",
            title_matched=False,
        )

    monkeypatch.setattr(hh_live_page_checks, "check_live_page", fake_check)

    res = execute_hh_submission(
        "hh:999000222",
        evaluate_fn=lambda js: "{}",
        dry_run=True,
    )
    assert seen.get("title") == "Python Developer", seen
    assert seen.get("vid") == "hh:999000222", seen
    # Blocked by the stub, not by the "cannot identify" refusal.
    assert res.reason == "stub stop", res.reason


# ---------------------------------------------------------------------------
# Finding #17 - the title check accepted a page whose title did not match
# ---------------------------------------------------------------------------

def _page(title: str) -> dict:
    return {
        "ok": True,
        "url": "https://hh.ru/vacancy/128659037",
        "title": title,
        "has_submit_btn": True,
        "has_apply_btn": True,
    }


def test_live_page_blocks_a_title_that_did_not_match():
    """Measured: expected "HR Generalist" against a page titled "HR Manager"
    scores similarity 0.52 with no shared word. The old code logged a warning
    and fell through with is_ok still True - a failed check that passed.

    These are two different jobs, and the numeric id does not help: it is read
    from the URL we just opened, so it matches by construction.
    """
    import json

    from ai_assistant.hh_live_page_checks import check_live_page

    res = check_live_page(
        lambda js: json.dumps(_page("HR Manager")),
        expected_vacancy_id="hh:128659037",
        expected_title="HR Generalist",
    )
    assert res.is_ok is False, "a non-matching title must not be submittable"
    assert res.error_reason == "WRONG_PAGE", res.reason
    assert res.title_matched is False


def test_live_page_still_checks_short_titles():
    """Measured: "Go Dev" has no word longer than 3 characters, so exp_words was
    empty and `or not exp_words` made the check answer True - at similarity
    0.00, against a page titled "Уборщица". A short title switched the only
    substitution check off completely.
    """
    import json

    from ai_assistant.hh_live_page_checks import check_live_page

    res = check_live_page(
        lambda js: json.dumps(_page("Уборщица")),
        expected_vacancy_id="hh:128659037",
        expected_title="Go Dev",
    )
    assert res.is_ok is False, "a short title must not disable the title check"
    assert res.error_reason == "WRONG_PAGE", res.reason


def test_live_page_accepts_the_same_title_with_a_city_suffix():
    """Counter-check: tightening the rule must not lock out real pages. hh.ru
    routinely appends the address, and "Продавец" vs "Продавец (Чебоксары,
    Гагарина Ю., 17)" is only 0.36 similar - it passes on the shared word, as
    it must.
    """
    import json

    from ai_assistant.hh_live_page_checks import check_live_page

    res = check_live_page(
        lambda js: json.dumps(_page("Продавец (Чебоксары, Гагарина Ю., 17)")),
        expected_vacancy_id="hh:128659037",
        expected_title="Продавец",
    )
    assert res.is_ok is True, f"the right page must stay submittable: {res.reason}"
    assert res.title_matched is True


# ---------------------------------------------------------------------------
# Finding #16 - the hard-constraint gate was skipped for an unknown vacancy
# ---------------------------------------------------------------------------

def test_submit_application_refuses_when_the_vacancy_row_is_missing():
    """`submit_application_in_browser` read the row as
    `vac = _row_to_vacancy(row) if row else None` and then wrapped the whole
    hard-constraint gate in `if vac:` - so a vacancy that is not in the DB
    skipped remote_required and every hard constraint and still went to the
    browser. The sibling entry point in the same module already refused with
    "Vacancy not found"; now both do.
    """
    res = be.submit_application_in_browser("hh:999000333", dry_run=True)
    assert res.status == "BLOCKED", res.error
    assert "Vacancy not found in DB" in (res.error or ""), res.error


def test_submit_application_proceeds_past_the_row_check():
    """Counter-check: the refusal must not swallow everything. A known vacancy
    gets past it and reaches the hard-constraint gate, which is the point."""
    from ai_assistant.schema import Vacancy

    db.save_vacancy(
        Vacancy(
            source="hh",
            source_job_id="999000444",
            title="Python Developer",
            company="TestCo",
            description="Office-based Python job",
            job_url="https://hh.ru/vacancy/999000444",
            location="Moscow",
        )
    )
    res = be.submit_application_in_browser("hh:999000444", dry_run=True)
    assert "Vacancy not found in DB" not in (res.error or ""), res.error


# ---------------------------------------------------------------------------
# Finding #18 - an unreadable kill-switch answered "not paused"
# ---------------------------------------------------------------------------
# Gate 1 read the kill switch as
#
#     try:
#         is_paused = db.is_submit_paused()
#     except Exception:
#         pass
#
# so a lookup that raised left is_paused False: an emergency stop that could
# not be read counted as "not engaged". Worse, the audit record still said
# "SUBMIT_ALLOWED enabled", which is indistinguishable from a real pass -
# nobody reading the log can tell the latch was never checked. Measured: with
# is_submit_paused() raising, check_all_gates returned passed=True, "All 11
# gates passed successfully".
#
# The same module treats this exact call as fatal 400 lines below: the
# pre-click check `if db.is_submit_paused():` has no guard at all, so there a
# read failure aborts the submission. Same call, opposite policy.

def _gate18_inputs(vid: str = "999000555"):
    """A vacancy that clears every gate except gate 1."""
    from ai_assistant.application_review import (
        ApplicationReview,
        ReviewStatus,
        save_application_review,
    )
    from ai_assistant.application_tracking import (
        ApplicationStatus,
        set_application_status,
    )
    from ai_assistant.candidate_profile import CandidateProfile
    from ai_assistant.hh_submission import clear_submitted_reviews

    clear_submitted_reviews()
    db.init_db()
    sid = f"hh:{vid}"
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=ReviewStatus.APPROVED,
            form_fingerprint=f"fp_{vid}",
            review_id=f"rev_{vid}",
        )
    )
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    snapshot = {
        "fingerprint": f"fp_{vid}",
        "cover_letter": "A perfectly good cover letter for this vacancy",
    }
    profile = CandidateProfile(
        desired_roles=["Python Developer"],
        alternative_roles=[],
        skills=["Python"],
        preferred_seniority=[],
        remote_required=False,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=["Russian"],
        employment_types=["Full-time"],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )
    return sid, f"https://hh.ru/vacancy/{vid}", snapshot, profile


def test_gates_refuse_when_the_kill_switch_cannot_be_read(monkeypatch):
    """A kill switch we cannot read is not a kill switch we have checked."""
    from ai_assistant.hh_submission import GateName, HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs()
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")

    def boom():
        raise _Boom("no such table: system_settings")

    monkeypatch.setattr(db, "is_submit_paused", boom)

    res = HHSubmissionGates.check_all_gates(
        sid,
        url,
        snapshot,
        human_confirmed=True,
        dry_run=False,
        candidate_profile=profile,
    )
    assert res.passed is False, res.reason
    assert res.failed_gate == GateName.GATE_SUBMIT_ALLOWED
    assert "kill switch" in res.reason.lower()


def test_gates_do_not_report_an_unread_kill_switch_as_enabled(monkeypatch):
    """The audit trail used to say 'SUBMIT_ALLOWED enabled' either way."""
    from ai_assistant.hh_submission import GateName, HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs("999000556")
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")

    def boom():
        raise _Boom("database is locked")

    monkeypatch.setattr(db, "is_submit_paused", boom)

    res = HHSubmissionGates.check_all_gates(
        sid,
        url,
        snapshot,
        human_confirmed=True,
        dry_run=False,
        candidate_profile=profile,
    )
    record = res.gate_results[GateName.GATE_SUBMIT_ALLOWED.value]
    assert record["passed"] is False
    assert "SUBMIT_ALLOWED enabled" not in record["reason"]


def test_gates_still_pass_when_the_kill_switch_reads_clean(monkeypatch):
    """Counter-check: gate 1 must not simply always fail."""
    from ai_assistant.hh_submission import HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs("999000557")
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        sid,
        url,
        snapshot,
        human_confirmed=True,
        dry_run=False,
        candidate_profile=profile,
    )
    assert res.passed is True, res.reason


def test_gates_still_block_when_the_kill_switch_is_engaged(monkeypatch):
    """Counter-check: a genuinely engaged kill switch still blocks, same gate."""
    from ai_assistant.hh_submission import GateName, HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs("999000558")
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(db, "is_submit_paused", lambda: True)

    res = HHSubmissionGates.check_all_gates(
        sid,
        url,
        snapshot,
        human_confirmed=True,
        dry_run=False,
        candidate_profile=profile,
    )
    assert res.passed is False
    assert res.failed_gate == GateName.GATE_SUBMIT_ALLOWED
    assert "kill switch" in res.reason.lower()
