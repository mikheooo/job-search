"""Regression tests for the fail-open handlers found by the BLE001 triage.

docs/ble001_triage.md lists the places where a swallowed exception produced an
"everything is fine" answer on a path that can reach a real submission. Each test
below forces the exception and asserts the handler now fails CLOSED.

These exist so the handlers can never quietly become optimistic again: the
optimistic fallbacks looked reasonable in isolation, which is exactly why they
survived review the first time.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ai_assistant import browser_executor as be
from ai_assistant import db
from ai_assistant import hh_application_queue as hq
from ai_assistant import hh_application_runner as hr
from ai_assistant.hh_questionnaire import submit_questionnaire_response


class _Boom(RuntimeError):
    """Stands in for any failure inside the audited call."""


_SESSION_SUBMIT_ALLOWED: tuple | None = None


@pytest.fixture(autouse=True)
def _restore_submit_allowed():
    """Undo whatever _set_submit_allowed() did, after every test in this file.

    That helper writes straight into os.environ and into config - it cannot use
    monkeypatch, because config.submit_allowed() prefers the value snapshotted
    at import time (finding #8). That also makes it a permanent write: without
    this fixture the last test to call it decides SUBMIT_ALLOWED for the rest
    of the session.

    Measured: adding tests that call _set_submit_allowed(False) turned 74 tests
    in later files (test_step24, test_step25, test_submission_verifier) red
    with 'Submission is disabled by SUBMIT_ALLOWED configuration' - tests that
    pass when run on their own. A leak, not a regression in the product code.

    The baseline is captured ONCE per session, not per test. Capturing it on
    entry looks equivalent and is not: if one test leaks `False`, every later
    test captures `False` as its own baseline and faithfully restores the leak.
    Measured - the two-step leak guard below passed with this fixture disabled
    in a full-file run, because by then the leaked value had become the
    baseline. A repair that adopts the damage is not a repair.
    """
    global _SESSION_SUBMIT_ALLOWED
    from ai_assistant import config

    if _SESSION_SUBMIT_ALLOWED is None:
        _SESSION_SUBMIT_ALLOWED = (
            os.environ.get("SUBMIT_ALLOWED"),
            getattr(config, "_OPERATOR_SUBMIT_ALLOWED", None),
            getattr(config, "SUBMIT_ALLOWED", None),
        )
    try:
        yield
    finally:
        saved_env, saved_operator, saved_flag = _SESSION_SUBMIT_ALLOWED
        if saved_env is None:
            os.environ.pop("SUBMIT_ALLOWED", None)
        else:
            os.environ["SUBMIT_ALLOWED"] = saved_env
        config._OPERATOR_SUBMIT_ALLOWED = saved_operator
        config.SUBMIT_ALLOWED = saved_flag


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

# ---------------------------------------------------------------------------
# Finding #19 - the auto-apply click path ignored both kill switches
# ---------------------------------------------------------------------------
# Three entry points physically click the real hh.ru submit button:
# submit_application(), hh_controlled_submit.controlled_real_submit() and the
# auto-apply runner. All three delegate their gatekeeping to preflight_submission
# - and none of them checked the emergency stop.
#
# Why it was invisible: that path delegates further to check_readonly_gates(),
# which is gates 2-10. Gate 1 (the kill switch and SUBMIT_ALLOWED) lives in
# check_all_gates(), and this path never calls it. So both switches guarded
# execute_hh_submission() and nothing else.
#
# Measured before the fix, with SUBMIT_ALLOWED=false AND
# system_settings.submit_paused=1 - exactly what the Telegram "stop" command
# writes:
#
#     verdict      : SUBMITTED
#     submit_count : 1
#     click_count  : 1
#     dom.clicks   : 1
#
# Found while sweeping HHSubmissionGates.check_all_gates after finding #18.

def _set_submit_allowed(value: bool) -> None:
    """Flip SUBMIT_ALLOWED for the duration of a single test.

    monkeypatch.setenv is not enough here: `config.submit_allowed()` gives
    precedence to the value snapshot at import time (_OPERATOR_SUBMIT_ALLOWED),
    so an explicit "false" from the surrounding shell outranks anything a test
    sets later. Measured: with SUBMIT_ALLOWED=false exported,
    `monkeypatch.setenv("SUBMIT_ALLOWED", "true")` still evaluates to False.
    Patch every source the function consults, and the frozen module constant
    it falls back to.
    """
    from ai_assistant import config

    text = "true" if value else "false"
    os.environ["SUBMIT_ALLOWED"] = text
    config._OPERATOR_SUBMIT_ALLOWED = text
    config.SUBMIT_ALLOWED = value


def _fresh_apply_session():
    """Auto-apply is one-shot per vacancy for the whole process.

    Without this the later tests in this block return BLOCKED_DUPLICATE before
    they ever reach the kill switch, and pass for the wrong reason.
    """
    from ai_assistant.auto_apply_modes import clear_session_state
    from ai_assistant.hh_human_submission import clear_all_submission_state

    clear_session_state()
    clear_all_submission_state()


def test_auto_apply_does_not_click_while_submission_is_paused():
    """The paused flag is what the Telegram stop command sets."""
    _fresh_apply_session()
    from ai_assistant.auto_apply_modes import ApplyMode, run_auto_apply
    from tests.test_stage21_auto_apply import FakeHH, _pkg, _simple_form

    _set_submit_allowed(True)
    db.set_submit_paused(True)
    try:
        dom = FakeHH(markers=["Вы откликнулись"])
        rep = run_auto_apply(
            _pkg(), dom.evaluate, {}, form=_simple_form(), mode=ApplyMode.REVIEW
        )
    finally:
        db.set_submit_paused(False)

    assert rep.submit_count == 0, rep.stop_reason
    assert rep.click_count == 0, rep.stop_reason
    assert dom.clicks == 0
    assert "kill switch" in (rep.stop_reason or "").lower()


def test_auto_apply_does_not_click_when_submit_allowed_is_off():
    """Counterpart: the second switch, on its own, must block too."""
    _fresh_apply_session()
    from ai_assistant.auto_apply_modes import ApplyMode, run_auto_apply
    from tests.test_stage21_auto_apply import FakeHH, _pkg, _simple_form

    db.set_submit_paused(False)
    _set_submit_allowed(False)
    try:
        dom = FakeHH(markers=["Вы откликнулись"])
        rep = run_auto_apply(
            _pkg(), dom.evaluate, {}, form=_simple_form(), mode=ApplyMode.REVIEW
        )
    finally:
        _set_submit_allowed(True)

    assert rep.submit_count == 0, rep.stop_reason
    assert dom.clicks == 0
    assert "SUBMIT_ALLOWED" in (rep.stop_reason or "")


def test_auto_apply_fails_closed_when_the_kill_switch_cannot_be_read(monkeypatch):
    """Finding #18, applied to the same call on this path."""
    _fresh_apply_session()
    from ai_assistant.auto_apply_modes import ApplyMode, run_auto_apply
    from tests.test_stage21_auto_apply import FakeHH, _pkg, _simple_form

    _set_submit_allowed(True)

    def boom():
        raise _Boom("database is locked")

    monkeypatch.setattr(db, "is_submit_paused", boom)
    dom = FakeHH(markers=["Вы откликнулись"])
    rep = run_auto_apply(
        _pkg(), dom.evaluate, {}, form=_simple_form(), mode=ApplyMode.REVIEW
    )
    assert rep.submit_count == 0, rep.stop_reason
    assert dom.clicks == 0
    assert "kill switch" in (rep.stop_reason or "").lower()


def test_auto_apply_still_clicks_when_both_switches_allow_it():
    """Counter-check: the new gate must not block a legitimately armed submit."""
    _fresh_apply_session()
    from ai_assistant.auto_apply_modes import ApplyMode, run_auto_apply
    from tests.test_stage21_auto_apply import FakeHH, _pkg, _simple_form

    _set_submit_allowed(True)
    db.set_submit_paused(False)
    dom = FakeHH(markers=["Вы откликнулись"])
    rep = run_auto_apply(
        _pkg(), dom.evaluate, {}, form=_simple_form(), mode=ApplyMode.REVIEW
    )
    assert rep.verdict == "SUBMITTED", rep.stop_reason
    assert dom.clicks == 1

# ---------------------------------------------------------------------------
# Finding #20 - the second kill switch was absent from every click path
# ---------------------------------------------------------------------------
# There are two emergency stops: SUBMIT_ALLOWED (config/env) and the
# `submit_paused` row in system_settings - the one the Telegram "stop" command
# writes. Finding #9 wired the first one into submit_application_in_browser for
# non-hh sources; the second was never checked anywhere on that path.
#
# Measured with everything else valid and submit_paused=1: the function walked
# the whole way to the adapters and clicked - status=SUBMITTED, and
# adapter.submit_application() called once.
#
# This is the #19 shape again: not a check that lies, a check that is missing.
# execute_hh_submission has both; the "hh:" branch delegates to it and inherits
# them, every other source lands on the legacy branch and had only one.

def _save_non_hh_vacancy(job_id: str) -> None:
    """Everything the legacy branch demands before it will click.

    A vacancy row (finding #16), an APPROVED review, a READY_FOR_REVIEW
    browser session, READY_TO_APPLY tracking, a queue item and a package.
    Without all of it the test stops on one of those gates and never reaches
    the kill switch - a green test that proves nothing about the stop.
    """
    import json
    from datetime import datetime, timezone

    from ai_assistant.application_queue import QueueItem, save_queue_item
    from ai_assistant.application_review import (
        ApplicationReview,
        ReviewStatus,
        save_application_review,
    )
    from ai_assistant.application_tracking import (
        ApplicationStatus,
        set_application_status,
    )
    from ai_assistant.schema import Vacancy

    sid = f"remoteok:{job_id}"
    url = f"https://remoteok.com/remote-jobs/{job_id}"
    db.save_vacancy(
        Vacancy(
            source="remoteok",
            source_job_id=job_id,
            title="Python Developer",
            company="TestCo",
            description="A perfectly ordinary remote job description",
            job_url=url,
            location="Remote",
        )
    )
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=ReviewStatus.APPROVED,
            form_fingerprint="fp_killswitch",
            review_id=f"rev_{job_id}",
        )
    )
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    save_queue_item(
        QueueItem(
            vacancy_stable_id=sid,
            canonical_id=sid,
            representative_vacancy_stable_id=sid,
            priority_score=80,
            rank=1,
            source="remoteok",
            title="Python Developer",
            vacancy_url=url,
        )
    )
    db.save_application_package(
        sid,
        "killswitch_test",
        json.dumps(
            {
                "cover_letter": "A perfectly good cover letter for this role",
                "answers": [],
                "questions": [],
            }
        ),
    )
    from ai_assistant import browser_executor as _be

    _be.save_browser_session(
        _be.BrowserApplicationSession(
            vacancy_stable_id=sid,
            url=url,
            status=_be.BrowserStatus.READY_FOR_REVIEW,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            form_detected=True,
        )
    )


def test_non_hh_source_is_blocked_by_the_db_kill_switch(monkeypatch):
    # Finding #16 refuses an unknown vacancy before the kill switch is even
    # reached. Save the row, so this test fails on the stop and nothing else.
    _save_non_hh_vacancy("777")
    _set_submit_allowed(True)
    db.set_submit_paused(True)
    try:
        res = be.submit_application_in_browser(
            "remoteok:777", confirm_submit=True, dry_run=False
        )
    finally:
        db.set_submit_paused(False)
    assert res.status == "BLOCKED", res.error
    assert "kill switch" in (res.error or "").lower()


def test_non_hh_source_is_not_blocked_when_the_kill_switch_is_off(monkeypatch):
    """Counter-check: this must not become a gate that always trips."""
    _save_non_hh_vacancy("778")
    _set_submit_allowed(True)
    db.set_submit_paused(False)
    res = be.submit_application_in_browser(
        "remoteok:778", confirm_submit=True, dry_run=False
    )
    assert "kill switch" not in (res.error or "").lower()


def test_non_hh_source_fails_closed_when_the_kill_switch_cannot_be_read(monkeypatch):
    """Finding #18 on this same call site: unreadable is not 'not engaged'."""
    _save_non_hh_vacancy("779")
    _set_submit_allowed(True)

    def boom():
        raise _Boom("database is locked")

    monkeypatch.setattr(db, "is_submit_paused", boom)
    res = be.submit_application_in_browser(
        "remoteok:779", confirm_submit=True, dry_run=False
    )
    assert res.status == "BLOCKED", res.error
    assert "kill switch" in (res.error or "").lower()

# ---------------------------------------------------------------------------
# Finding #21 - the third kill switch was honoured by one function only
# ---------------------------------------------------------------------------
# There are three emergency stops in this codebase:
#   1. SUBMIT_ALLOWED (config/env)
#   2. system_settings.submit_paused (the Telegram "stop" command)
#   3. the STOP_SUBMITS file, listed FIRST in hh_submit_policy.evaluate()'s
#      stop sources.
#
# Findings #9, #18, #19 and #20 chased the first two across every click path.
# The third one was checked in exactly one place - hh_submit_policy.evaluate(),
# which is autonomous-runner gates - and by nothing else. Every other stop in
# every other path was spelled `db.is_submit_paused()`.
#
# Measured before the fix: with data/STOP_SUBMITS present and everything else
# valid, submit_application() walked all the way to the real submit button and
# clicked - clicked=True, status SUBMISSION_UNKNOWN.
#
# The cause is not a missing `if`. It is that four call sites each spelled out
# their own list of stops, so adding a stop meant editing four places and the
# one that got missed was invisible. Fix: one predicate,
# hh_submission.submission_halt_reason(), owns all three stops and fails closed
# on a read error. Every killing path asks it and nothing spells out its own.

def _stop_file_cwd(tmp_path):
    """A working directory holding data/STOP_SUBMITS - the file's real location.

    The relative paths "data/STOP_SUBMITS" and "STOP_SUBMITS" resolve against
    the process CWD, which is how the file is meant to be used (touch it next
    to the app). Tests therefore chdir, and tmp_path makes that safe.
    """
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "STOP_SUBMITS").write_text("STOP", encoding="utf-8")
    return tmp_path


def test_halt_reason_is_none_when_no_stop_is_engaged(monkeypatch, tmp_path):
    """Counter-check: the predicate must not become a latch that always trips."""
    from ai_assistant import config
    from ai_assistant.hh_submission import submission_halt_reason

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(True)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)
    assert config.submit_allowed() is True
    assert submission_halt_reason() is None


def test_halt_reason_reports_the_stop_file(monkeypatch, tmp_path):
    from ai_assistant.hh_submission import submission_halt_reason

    monkeypatch.chdir(_stop_file_cwd(tmp_path))
    _set_submit_allowed(True)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)
    reason = submission_halt_reason()
    assert reason is not None
    assert "STOP_SUBMITS" in reason


def test_halt_reason_reports_the_db_flag(monkeypatch, tmp_path):
    from ai_assistant.hh_submission import submission_halt_reason

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(True)
    monkeypatch.setattr(db, "is_submit_paused", lambda: True)
    assert "submit_paused" in (submission_halt_reason() or "")


def test_halt_reason_reports_submit_allowed_off(monkeypatch, tmp_path):
    from ai_assistant.hh_submission import submission_halt_reason

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(False)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)
    assert "SUBMIT_ALLOWED" in (submission_halt_reason() or "")


def test_halt_reason_fails_closed_when_the_db_cannot_be_read(monkeypatch, tmp_path):
    """Finding #18's rule applies to the shared predicate too."""
    from ai_assistant.hh_submission import submission_halt_reason

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(True)

    def boom():
        raise _Boom("database is locked")

    monkeypatch.setattr(db, "is_submit_paused", boom)
    reason = submission_halt_reason()
    assert reason is not None
    assert "Cannot read" in reason


def test_check_all_gates_refuses_while_the_stop_file_is_present(monkeypatch, tmp_path):
    """Gate 1 knew two stops. The file was not one of them."""
    from ai_assistant.hh_submission import HHSubmissionGates

    monkeypatch.chdir(_stop_file_cwd(tmp_path))
    _set_submit_allowed(True)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        vacancy_stable_id="hh:999000123",
        current_url="https://hh.ru/applicant/vacancy_response?vacancyId=999000123",
        form_snapshot={"fingerprint": "x", "cover_letter": "y"},
        human_confirmed=True,
        dry_run=False,
    )
    assert res.passed is False
    assert "STOP_SUBMITS" in res.reason


def test_gates_still_pass_with_clean_switches(monkeypatch, tmp_path):
    """Counter-check: adding the stop must not turn gate 1 into a wall."""
    from ai_assistant.hh_submission import HHSubmissionGates

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(True)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        vacancy_stable_id="hh:999000123",
        current_url="https://hh.ru/applicant/vacancy_response?vacancyId=999000123",
        form_snapshot={"fingerprint": "x", "cover_letter": "y"},
        human_confirmed=True,
        dry_run=True,
    )
    assert "STOP_SUBMITS" not in (res.reason or "")


def test_non_hh_source_is_blocked_by_the_stop_file(monkeypatch, tmp_path):
    """The non-hh branch went through SUBMIT_ALLOWED and submit_paused only."""
    _save_non_hh_vacancy("881")
    monkeypatch.chdir(_stop_file_cwd(tmp_path))
    _set_submit_allowed(True)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = be.submit_application_in_browser(
        "remoteok:881", confirm_submit=True, dry_run=False
    )
    assert res.status == "BLOCKED", res.error
    assert "STOP_SUBMITS" in (res.error or "")

def test_stop_order_blocks_even_a_dry_run(monkeypatch, tmp_path):
    """A dry run mutates nothing, so it may skip SUBMIT_ALLOWED - not a stop order.

    README and Step 2.4 ("исправлен баг блокировки при dry_run=True") both
    document that a dry run passes with SUBMIT_ALLOWED off. The first draft of
    this fix made every stop outrank dry_run and broke two tests; the stop file
    and the Telegram pause are a different thing from the release latch, and
    they do outrank it.
    """
    from ai_assistant.hh_submission import HHSubmissionGates

    monkeypatch.chdir(_stop_file_cwd(tmp_path))
    _set_submit_allowed(False)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        vacancy_stable_id="hh:999000123",
        current_url="https://hh.ru/applicant/vacancy_response?vacancyId=999000123",
        form_snapshot={"fingerprint": "x", "cover_letter": "y"},
        human_confirmed=True,
        dry_run=True,
    )
    assert res.passed is False
    assert "STOP_SUBMITS" in res.reason


def test_db_stop_order_blocks_even_a_dry_run(monkeypatch, tmp_path):
    from ai_assistant.hh_submission import HHSubmissionGates

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(False)
    monkeypatch.setattr(db, "is_submit_paused", lambda: True)

    res = HHSubmissionGates.check_all_gates(
        vacancy_stable_id="hh:999000123",
        current_url="https://hh.ru/applicant/vacancy_response?vacancyId=999000123",
        form_snapshot={"fingerprint": "x", "cover_letter": "y"},
        human_confirmed=True,
        dry_run=True,
    )
    assert res.passed is False
    assert "submit_paused" in res.reason


def test_submit_allowed_is_still_bypassed_by_a_dry_run(monkeypatch, tmp_path):
    """Counter-check on the documented contract this fix must not break.

    Pinned by tests/test_hh_submission_gates.py::test_gate_submit_allowed and
    README line 83. If someone later makes SUBMIT_ALLOWED outrank dry_run, this
    fails and tells them why before they ship it.

    Asserts on gate 1 alone, not on the overall verdict: this vacancy has no
    review in the database, so `passed` would be False for a reason that has
    nothing to do with the release latch. A red test must be red about its own
    subject (finding #20's lesson, third time).
    """
    from ai_assistant.hh_submission import HHSubmissionGates

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(False)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        vacancy_stable_id="hh:999000123",
        current_url="https://hh.ru/applicant/vacancy_response?vacancyId=999000123",
        form_snapshot={"fingerprint": "x", "cover_letter": "y"},
        human_confirmed=True,
        dry_run=True,
    )
    gate1 = res.gate_results["submit_allowed"]
    assert gate1["passed"] is True, gate1["reason"]
    assert "dry-run" in gate1["reason"]


def test_halt_reason_can_ignore_the_release_latch(monkeypatch, tmp_path):
    """include_submit_allowed=False is what the dry-run path asks for."""
    from ai_assistant.hh_submission import submission_halt_reason

    monkeypatch.chdir(tmp_path)
    _set_submit_allowed(False)
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    assert submission_halt_reason() is not None
    assert submission_halt_reason(include_submit_allowed=False) is None


_LEAK_GUARD_BASELINE = None


def test_leak_guard_step_1_leaves_submit_allowed_off():
    """Step 1 of 2. Regression guard for a leak that turned 74 tests red.

    _set_submit_allowed() writes into os.environ and into config on purpose -
    config.submit_allowed() prefers the value snapshotted at import time
    (finding #8), so monkeypatch.setenv alone does nothing and the helper has
    to patch all three. The cost is that the write is permanent: whichever test
    runs last decides SUBMIT_ALLOWED for every test file after it.

    Measured without the autouse fixture: 74 failures in test_step24,
    test_step25 and test_submission_verifier, all reporting
    'Submission is disabled by SUBMIT_ALLOWED configuration' - and all green
    when those files run on their own. A leak, not a product bug, and an easy
    one to blame on the product.

    This step deliberately ENDS with the latch off and does not clean up. Step
    2 is what proves the fixture puts it back.

    It also FORCES the latch on before measuring. Reading the baseline instead
    looked fine and was useless: with the fixture disabled the leak has already
    happened by the time this test runs, so the leaked `False` becomes the
    baseline and step 2 compares False with False and passes. Measured - the
    mutation run showed exactly that. Force the known-good state first, then
    the comparison means something.
    """
    global _LEAK_GUARD_BASELINE
    from ai_assistant import config

    _set_submit_allowed(True)
    _LEAK_GUARD_BASELINE = config.submit_allowed()
    assert _LEAK_GUARD_BASELINE is True

    _set_submit_allowed(False)
    assert config.submit_allowed() is False


def test_leak_guard_step_2_sees_the_value_restored():
    """Step 2 of 2: must run right after step 1, and must see the latch back on.

    If the autouse fixture is dropped, step 1's `False` is still in place here
    and this fails. Ordering matters, hence the step_1/step_2 names.
    """
    from ai_assistant import config

    assert _LEAK_GUARD_BASELINE is True, "step 1 did not run"
    assert config.submit_allowed() is True, (
        "SUBMIT_ALLOWED leaked out of a test in this file; the autouse "
        "_restore_submit_allowed fixture is not doing its job"
    )

# ---------------------------------------------------------------------------
# Finding #22 - the CDP adapter reported success without a click, and the
# legacy branch recorded SUBMITTED on that word alone
# ---------------------------------------------------------------------------
# Two layers, each of which looks reasonable alone:
#
#   layer 1  CDPBrowserAdapter.submit_application() sent the click script,
#            read the answer into `res`, and then returned
#            {"success": True, "details": res} - unconditionally. The JS
#            answers {clicked: false, error: "button not found"} on a page
#            with no apply button. Nobody read it.
#
#   layer 2  submit_application_in_browser() legacy branch checked only
#            submit_result["success"], then wrote a submissions row with
#            status='SUBMITTED', moved tracking to SUBMITTED, and returned
#            status=SUBMITTED. It had `details` in hand and ignored it.
#
# Measured before the fix, adapter returning the CDP answer for a missing
# button:
#
#   SubmitResult.status              : SUBMITTED
#   DB submissions row               : (..., '{"success": true, "details":
#                                       {"clicked": false, "error": "button
#                                       not found"}}', 'SUBMITTED', ...)
#   tracking status                  : SUBMITTED
#   is_already_applied (blocks retry): True
#
# Nothing was clicked, the system said it was, and the duplicate guard then
# refused a real retry forever. The evidence that no click happened was stored
# in the same row that claimed success.
#
# This is finding #2's shape again - two layers that degrade "gracefully" and
# together turn an exception-shaped failure into permission - and finding #1's
# asymmetry: the Playwright adapter reads its own result and only reports
# success after finding a confirmation; CDP did not.

class _ScriptedAdapter:
    """Minimal adapter whose submit_application() returns a scripted answer."""

    def __init__(self, submit_answer: dict):
        self._submit_answer = submit_answer
        self.submit_calls = 0
        self._url = "https://remoteok.com/remote-jobs/999"

    def open(self, url):
        self._url = url
        return {"url": url, "title": "Python Developer", "site": "remoteok"}

    def inspect_page(self):
        return {"form_detected": True, "fields": [], "apply_button": True}

    def evaluate(self, js):
        import json as _json

        if "hh_live_page_inspect" in js:
            return _json.dumps({
                "ok": True, "url": self._url, "title": "Python Developer",
                "is_404": False, "is_captcha": False, "login_required": False,
                "body_text": "Apply now",
            })
        if "document.body" in js:
            return _json.dumps({"text": "Apply now"})
        return _json.dumps({"ok": True})

    def get_current_url(self):
        return self._url

    def get_title(self):
        return "Python Developer"

    def screenshot(self, path):
        return None

    def close(self):
        return None

    def submit_application(self):
        self.submit_calls += 1
        return dict(self._submit_answer)


# --- layer 1: the CDP adapter itself ----------------------------------------

def _cdp_adapter_with_js_reply(reply: dict):
    """A CDPBrowserAdapter whose websocket answers with `reply`."""
    import json as _json
    import sys as _sys
    import types as _types

    from ai_assistant.browser_executor import CDPBrowserAdapter

    class _WS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, payload):
            return None

        async def recv(self):
            return _json.dumps({"result": {"result": {"value": reply}}})

    mod = _types.ModuleType("websockets")
    mod.connect = lambda *a, **k: _WS()
    saved = _sys.modules.get("websockets")
    _sys.modules["websockets"] = mod

    adapter = CDPBrowserAdapter("http://127.0.0.1:9222")
    adapter.ws_url = "ws://127.0.0.1:9222/devtools/page/1"
    return adapter, saved


def _restore_websockets(saved):
    import sys as _sys

    if saved is None:
        _sys.modules.pop("websockets", None)
    else:
        _sys.modules["websockets"] = saved


def test_cdp_adapter_does_not_report_success_when_the_button_was_missing():
    """Measured before the fix: success=True while clicked=False."""
    adapter, saved = _cdp_adapter_with_js_reply(
        {"clicked": False, "error": "button not found"}
    )
    try:
        res = adapter.submit_application()
    finally:
        _restore_websockets(saved)

    assert res.get("success") is False, res
    assert "button not found" in (res.get("error") or "")


def test_cdp_adapter_still_reports_success_when_the_click_happened():
    """Counter-check: the honest answer must survive."""
    adapter, saved = _cdp_adapter_with_js_reply(
        {"clicked": True, "text": "Откликнуться"}
    )
    try:
        res = adapter.submit_application()
    finally:
        _restore_websockets(saved)

    assert res.get("success") is True, res


def test_cdp_adapter_refuses_success_on_an_unreadable_answer():
    """A reply that is not a dict is not a click either."""
    adapter, saved = _cdp_adapter_with_js_reply(None)
    try:
        res = adapter.submit_application()
    finally:
        _restore_websockets(saved)

    assert res.get("success") is False, res


# --- layer 2: the legacy branch ---------------------------------------------

def test_legacy_branch_refuses_to_record_submitted_without_a_click():
    """Measured before the fix: SUBMITTED recorded, retry blocked forever."""
    _save_non_hh_vacancy("991")
    _set_submit_allowed(True)
    adapter = _ScriptedAdapter(
        {"success": True, "details": {"clicked": False, "error": "button not found"}}
    )

    res = be.submit_application_in_browser(
        "remoteok:991", confirm_submit=True, dry_run=False, adapter=adapter
    )

    assert res.status == "FAILED", res.status
    assert "click" in (res.error or "").lower()
    assert db.get_submission("remoteok:991") is None

    from ai_assistant.submission_state import get_submission_evidence

    evidence = get_submission_evidence("remoteok:991")
    assert evidence.is_already_applied is False, evidence.blocked_reasons


def test_legacy_branch_still_records_submitted_when_the_click_happened():
    """Counter-check: an adapter that did click must still be believed."""
    _save_non_hh_vacancy("992")
    _set_submit_allowed(True)
    adapter = _ScriptedAdapter(
        {"success": True, "details": {"clicked": True, "text": "Apply"}}
    )

    res = be.submit_application_in_browser(
        "remoteok:992", confirm_submit=True, dry_run=False, adapter=adapter
    )

    assert res.status == "SUBMITTED", (res.status, res.error)
    assert db.get_submission("remoteok:992") is not None


def test_legacy_branch_still_believes_an_adapter_without_click_details():
    """Counter-check for the honest adapters that report no `details`.

    PlaywrightBrowserAdapter returns {"success": True, "before_screenshot": ...,
    "after_screenshot": ...} - no `details` key at all, because it verified the
    confirmation text itself. The new guard must not touch that path.
    """
    _save_non_hh_vacancy("993")
    _set_submit_allowed(True)
    adapter = _ScriptedAdapter({"success": True, "message": "confirmed on page"})

    res = be.submit_application_in_browser(
        "remoteok:993", confirm_submit=True, dry_run=False, adapter=adapter
    )

    assert res.status == "SUBMITTED", (res.status, res.error)


# ---------------------------------------------------------------------------
# Finding #23: a questionnaire that parked the application told nobody
#
# Measured before the fix: submit_questionnaire_response() stopped before
# submit (submit_count 0) and created ZERO rows in autonomous_notifications.
# The notification existed and was tested - nothing in production called it.
# ---------------------------------------------------------------------------

def _save_questionnaire(
    qid: str,
    questions: list[dict] | None = None,
    *,
    vacancy_stable_id: str | None = "hh:135112049",
) -> str:
    from ai_assistant.hh_questionnaire import HHQuestionStatus

    db.init_db()
    db.save_hh_questionnaire({
        "questionnaire_id": qid,
        "vacancy_stable_id": vacancy_stable_id,
        "title": "Python Developer",
        "employer": "TestCo",
        "questions": questions if questions is not None else [
            {"question_id": "q_personal", "text": "Ваш ИНН?", "question_type": "text",
             "required": True, "options": []},
        ],
        "answers": {},
        "status": HHQuestionStatus.NEEDS_HUMAN_REVIEW.value,
    })
    return qid


def _notifications_of_type(kind: str) -> list[dict]:
    return [n for n in db.list_autonomous_notifications(limit=200)
            if n.get("notification_type") == kind]


def _silence_telegram(monkeypatch) -> list[dict]:
    """Catch the delivery instead of letting it reach the network."""
    from ai_assistant import telegram_notifier

    sent: list[dict] = []

    class _Stub:
        def deliver_notification(self, notif_type, details, delivery_key=None):
            sent.append({"type": notif_type, "details": details, "key": delivery_key})
            return True

    monkeypatch.setattr(telegram_notifier, "get_telegram_notifier", lambda: _Stub())
    return sent


def _questionnaire_executor(*, click_ok: bool = True, reason: str = ""):
    """Mirrors FakeSubmitCDP in test_stage34: {'ok': true} for every other call."""

    def _evaluate(expr: str) -> str:
        if "click" in expr or "vacancy-response-submit" in expr:
            if click_ok:
                return json.dumps({"ok": True})
            return json.dumps({"ok": False, "reason": reason or "Submit button disabled"})
        return json.dumps({"ok": True})

    return _evaluate


def test_questionnaire_blocked_by_a_missing_answer_notifies_the_human(monkeypatch):
    """Measured before the fix: submit_count 0 and ZERO notification rows."""
    sent = _silence_telegram(monkeypatch)
    qid = _save_questionnaire("q23_missing")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={}, confirm_submit=True)

    assert res.submit_count == 0, "the stop must survive the notification"
    rows = _notifications_of_type("UNANSWERED_QUESTION_BLOCKED")
    assert len(rows) == 1, "the human was never told the application was parked"
    assert "q_personal" in rows[0]["message"]
    assert rows[0]["title"].startswith("MANUAL QUESTION REQUIRED")
    assert [s["type"] for s in sent] == ["UNANSWERED_QUESTION_BLOCKED"], sent


def test_questionnaire_changed_on_the_page_notifies_the_human(monkeypatch):
    """Changed DOM is the other case a human has to act on."""
    _silence_telegram(monkeypatch)
    qid = _save_questionnaire("q23_changed")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_personal": "123456789012"},
        confirm_submit=True, current_dom_fingerprint="a_different_fingerprint")

    assert res.submit_count == 0
    assert res.status == "NEEDS_HUMAN_REVIEW"
    assert len(_notifications_of_type("UNANSWERED_QUESTION_BLOCKED")) == 1


def test_unknown_question_id_is_a_caller_bug_and_notifies_nobody(monkeypatch):
    """Counter-check: a bad key in the answers dict is not a question a human
    can answer - waking someone up for it would be noise, not signal."""
    sent = _silence_telegram(monkeypatch)
    qid = _save_questionnaire("q23_unknown_qid")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_nonexistent": "x"}, confirm_submit=True)

    assert res.submit_count == 0
    assert _notifications_of_type("UNANSWERED_QUESTION_BLOCKED") == []
    assert sent == []


def test_a_valid_questionnaire_notifies_nobody(monkeypatch):
    """Counter-check: the notification is not a wall - a clean questionnaire
    submits and stays quiet."""
    sent = _silence_telegram(monkeypatch)
    qid = _save_questionnaire("q23_valid")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_personal": "123456789012"},
        confirm_submit=True, evaluate_fn=_questionnaire_executor())

    assert res.verdict == "SUBMITTED"
    assert _notifications_of_type("UNANSWERED_QUESTION_BLOCKED") == []
    assert sent == []


def test_a_broken_notifier_does_not_break_the_stop(monkeypatch):
    """The notification is best-effort; the safety outcome is not."""
    from ai_assistant import telegram_notifier

    qid = _save_questionnaire("q23_broken_notifier")

    def _explode(*_a, **_k):
        raise _Boom("telegram is down")

    monkeypatch.setattr(telegram_notifier, "get_telegram_notifier", _explode)

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={}, confirm_submit=True)

    assert res.submit_count == 0
    assert res.status == "BLOCKED"
    # the record is written before delivery, so the audit trail survives
    assert len(_notifications_of_type("UNANSWERED_QUESTION_BLOCKED")) == 1


# ---------------------------------------------------------------------------
# Finding #25: the questionnaire reported SUBMITTED with no click at all
#
# Measured before the fix, with evaluate_fn=None (no browser):
#   verdict SUBMITTED, submit_count 1, click_count 1, status SUBMITTED in the DB
# - the click count was simply invented, and the one-shot invariant then
# refused every later attempt for that vacancy.
# ---------------------------------------------------------------------------

def test_questionnaire_without_an_executor_never_reports_submitted():
    qid = _save_questionnaire("q25_no_executor")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_personal": "123456789012"},
        confirm_submit=True, evaluate_fn=None)

    assert res.verdict != "SUBMITTED", res.verdict
    assert res.submit_count == 0
    assert res.click_count == 0
    assert res.status == "READY_TO_SUBMIT"
    assert (db.get_hh_questionnaire(qid) or {}).get("status") != "SUBMITTED"


def test_a_missing_executor_does_not_brick_the_one_shot_invariant():
    """The old bug marked the questionnaire SUBMITTED, and the one-shot
    invariant then refused every later attempt forever."""
    qid = _save_questionnaire("q25_not_bricked")
    answers = {"q_personal": "123456789012"}

    submit_questionnaire_response(
        questionnaire_id=qid, human_answers=answers,
        confirm_submit=True, evaluate_fn=None)

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers=answers,
        confirm_submit=True, evaluate_fn=_questionnaire_executor())

    assert res.verdict == "SUBMITTED", res.reason
    assert res.submit_count == 1


def test_questionnaire_still_submits_when_the_click_happened():
    """Counter-check: the guard is not a wall."""
    qid = _save_questionnaire("q25_click_ok")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_personal": "123456789012"},
        confirm_submit=True, evaluate_fn=_questionnaire_executor())

    assert res.verdict == "SUBMITTED"
    assert res.submit_count == 1
    assert res.click_count == 1
    assert (db.get_hh_questionnaire(qid) or {}).get("status") == "SUBMITTED"


def test_questionnaire_refuses_submitted_when_the_page_says_no_click():
    qid = _save_questionnaire("q25_no_click")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_personal": "123456789012"},
        confirm_submit=True,
        evaluate_fn=_questionnaire_executor(click_ok=False,
                                            reason="Submit button not found"))

    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert (db.get_hh_questionnaire(qid) or {}).get("status") != "SUBMITTED"
    assert "Submit button not found" in res.reason


def test_the_blocked_question_type_is_actually_routed_to_telegram():
    """Counter-check on the wiring, not on the notification call.

    The tests above stub the notifier out, so they would stay green if
    UNANSWERED_QUESTION_BLOCKED were dropped from the notifier's allowlist -
    the notification would be saved and then silently filtered as "routine",
    which is the very failure #23 was about. This calls the REAL notifier; the
    network is blocked by conftest, so nothing leaves the machine.
    """
    from ai_assistant.telegram_notifier import get_telegram_notifier

    try:
        res = get_telegram_notifier().deliver_notification(
            notif_type="UNANSWERED_QUESTION_BLOCKED",
            details={"company": "TestCo", "vacancy_title": "Python Developer",
                     "unanswered_question": "Ваш ИНН?", "application_id": None},
            delivery_key="q23_routing_probe",
        )
    except Exception as e:  # noqa: BLE001 - the network is blocked on purpose
        res = {"delivered": False, "reason": f"raised: {type(e).__name__}"}

    reason = str(res.get("reason", ""))
    assert "routine" not in reason, res
    assert "not routed" not in reason, res


# ---------------------------------------------------------------------------
# Finding #28: the HH session check answered "authenticated" when it could not
# tell, and the message watcher read "cannot tell" as "no new messages".
# ---------------------------------------------------------------------------

_AUTH_JS_MARKER = "account/login"  # unique to the in-page auth-check script
_EMPTY_CHAT_LIST = json.dumps({
    "url": "https://hh.ru/applicant/negotiations",
    "title": "Чаты",
    "conversations": [],
})


def _auth_ev(auth_reply, *, list_reply=None):
    """An evaluate_fn that answers the auth script and the chat list apart.

    Only the auth script is forced to misbehave, so a cycle that survives it
    still has a working page underneath - which is exactly the situation the
    old fail-open turned into a clean, blind report.
    """
    def ev(js):
        if _AUTH_JS_MARKER in js:
            return auth_reply()
        return list_reply if list_reply is not None else _EMPTY_CHAT_LIST
    return ev


def _watch_cycle(monkeypatch, ev):
    """Run a real watcher cycle down the production path.

    The watcher resolves its own CDP evaluate function only when none is
    passed, and that is the only path that runs the session check at all -
    so the check can only be exercised from here, not from the unit level.
    """
    from ai_assistant import cli
    from ai_assistant import hh_browser_launcher as hbl
    from ai_assistant.hh_message_watcher import (
        HHMessageWatcherConfig,
        run_message_watcher_cycle,
    )

    monkeypatch.setattr(cli, "_resolve_hh_evaluate", lambda **kw: ev)
    monkeypatch.setattr(hbl, "ensure_hh_browser", lambda **kw: {"ok": True})
    cfg = HHMessageWatcherConfig(
        cdp_url="http://127.0.0.1:9222",
        auto_start_browser=False,
        batch_limit=5,
    )
    return run_message_watcher_cycle(cfg)


def _boom():
    raise _Boom("CDP target closed")


# --- the check itself -----------------------------------------------------

def test_auth_check_that_cannot_read_the_page_never_claims_authenticated():
    """The old code returned authenticated=True 'assuming session active'."""
    from ai_assistant.hh_browser_launcher import check_hh_session_authenticated

    info = check_hh_session_authenticated(_auth_ev(_boom))

    assert info["authenticated"] is False, info
    assert info["verified"] is False, info


def test_auth_check_refuses_a_payload_that_does_not_answer_the_question():
    """A page that navigated mid-check replies with something unrelated."""
    from ai_assistant.hh_browser_launcher import check_hh_session_authenticated

    info = check_hh_session_authenticated(
        _auth_ev(lambda: json.dumps({"url": "https://hh.ru/x"})))

    assert info["authenticated"] is False, info
    assert info["verified"] is False, info
    assert "unrelated" in info["reason"], info


def test_auth_check_refuses_an_unparseable_reply():
    from ai_assistant.hh_browser_launcher import check_hh_session_authenticated

    info = check_hh_session_authenticated(_auth_ev(lambda: "<html>login</html>"))

    assert info["authenticated"] is False, info
    assert info["verified"] is False, info


def test_auth_check_marks_honest_answers_as_verified():
    """`verified` drives the wording of the block, so it must be set."""
    from ai_assistant.hh_browser_launcher import check_hh_session_authenticated

    ok = check_hh_session_authenticated(
        _auth_ev(lambda: json.dumps({"authenticated": True, "reason": "profile found"})))
    out = check_hh_session_authenticated(
        _auth_ev(lambda: json.dumps({"authenticated": False, "reason": "login page"})))

    assert ok["verified"] is True, ok
    assert out["verified"] is True, out


def test_auth_check_still_reports_a_real_login_page():
    """Counter-check: a genuine logout must keep its own, actionable message."""
    from ai_assistant.hh_browser_launcher import check_hh_session_authenticated

    info = check_hh_session_authenticated(_auth_ev(
        lambda: json.dumps({"authenticated": False, "reason": "Browser is on HH login page"})))

    assert info["authenticated"] is False
    assert info["verified"] is True
    assert "login page" in info["reason"]


def test_auth_check_still_passes_a_healthy_session():
    """Counter-check: the guard is not a wall."""
    from ai_assistant.hh_browser_launcher import check_hh_session_authenticated

    for reason in ("Applicant profile navigation element found",
                   "Session active (no login prompt)"):
        info = check_hh_session_authenticated(
            _auth_ev(lambda r=reason: json.dumps({"authenticated": True, "reason": r})))
        assert info["authenticated"] is True, (reason, info)


# --- what a whole cycle reports -------------------------------------------

def test_watcher_reports_an_unverifiable_session_as_blocked(monkeypatch):
    """Measured before the fix: checked=0, blocked=0, errors=0."""
    res = _watch_cycle(monkeypatch, _auth_ev(_boom))

    assert res.blocked == 1, res
    assert len(res.errors) == 1, res
    assert "could not verify" in res.errors[0], res.errors


def test_watcher_does_not_call_a_blind_cycle_clean(monkeypatch):
    res = _watch_cycle(monkeypatch, _auth_ev(
        lambda: json.dumps({"url": "https://hh.ru/x"})))

    assert res.blocked == 1, res
    assert res.conversations_checked == 0


def test_watcher_does_not_swallow_an_auth_check_that_raises(monkeypatch):
    """The watcher used to debug-log this and carry on with a clean report."""
    from ai_assistant import hh_browser_launcher as hbl

    def explode(_ev):
        raise _Boom("check itself is broken")

    monkeypatch.setattr(hbl, "check_hh_session_authenticated", explode)
    res = _watch_cycle(monkeypatch, _auth_ev(lambda: json.dumps({"authenticated": True})))

    assert res.blocked == 1, res
    assert "could not verify" in res.errors[0], res.errors


def test_watcher_still_blocks_a_genuinely_logged_out_session(monkeypatch):
    """Counter-check: the real logout path keeps its own wording."""
    res = _watch_cycle(monkeypatch, _auth_ev(
        lambda: json.dumps({"authenticated": False, "reason": "Browser is on HH login page"})))

    assert res.blocked == 1
    assert "Please log in to HeadHunter" in res.errors[0], res.errors


def test_watcher_still_processes_a_healthy_session(monkeypatch):
    """Counter-check: the fix must not make the watcher block a good session."""
    convs = json.dumps({
        "url": "https://hh.ru/applicant/negotiations",
        "title": "Чаты",
        "conversations": [{
            "conversation_id": "c28_ok",
            "title": "Python Developer",
            "employer": "TechCorp",
            "snippet": "Здравствуйте! Когда вы готовы начать?",
            "is_selected": False,
        }],
    })
    res = _watch_cycle(monkeypatch, _auth_ev(
        lambda: json.dumps({"authenticated": True, "reason": "profile found"}),
        list_reply=convs))

    assert res.blocked == 0, res.errors
    assert res.errors == [], res.errors
    assert res.conversations_checked == 1, res


def test_the_in_page_auth_script_fails_closed_on_its_own_error():
    """Pin the JS branch, which Python cannot execute (it needs a browser).

    The catch block inside check_js is the other half of finding #28 - it used
    to answer authenticated:true with the reason "Check fallback". A real JS
    engine check lives in docs/ble001_triage.md #28; this keeps the contract
    from silently regressing in CI, where no browser is available.
    """
    import inspect

    from ai_assistant.hh_browser_launcher import check_hh_session_authenticated

    src = inspect.getsource(check_hh_session_authenticated)
    catch_branch = src.split("} catch (e) {", 1)[1].split("}", 1)[0]
    # Strip comments: the explanatory comment quotes the OLD literal on purpose,
    # so a naive text search over the whole branch matches its own documentation.
    body = " ".join(
        line.strip() for line in catch_branch.splitlines()
        if line.strip() and not line.strip().startswith("//")
    )

    assert "authenticated: false" in body, body
    assert "authenticated: true" not in body, body


def test_watcher_treats_a_key_less_auth_answer_as_not_authenticated(monkeypatch):
    """The watcher read `auth_info.get("authenticated", True)` - a missing key
    was a pass. Nothing returns a key-less dict today, so this pins the default
    itself rather than waiting for a future caller to trip over it."""
    from ai_assistant import hh_browser_launcher as hbl

    monkeypatch.setattr(hbl, "check_hh_session_authenticated",
                        lambda _ev: {"reason": "no answer in this dict"})

    res = _watch_cycle(monkeypatch, _auth_ev(lambda: json.dumps({"authenticated": True})))

    assert res.blocked == 1, res
    assert len(res.errors) == 1, res
