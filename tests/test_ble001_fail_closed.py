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
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ai_assistant import browser_executor as be
from ai_assistant import db
from ai_assistant import hh_application_queue as hq
from ai_assistant import hh_application_runner as hr
from ai_assistant import hh_human_submission as hcs
from ai_assistant.application_review_gate import (
    GateStatus,
    HumanReviewGate,
    HumanReviewStore,
)
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
    # What matters is that we never report HEALTHY and that the inconclusive
    # probe says so out loud. It used to be able to escalate to UNHEALTHY via a
    # later rule on the same broken connection; finding #47 then stopped rule B
    # from rewriting any inconclusive verdict, so UNKNOWN now survives.
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
# The same module treated this call as fatal 400 lines below: the pre-click
# check, then spelled `if db.is_submit_paused():`, had no guard at all - same
# call, opposite policy. Finding #21 later unified it behind one helper.

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
    fingerprint: str = "",
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
        "fingerprint": fingerprint,
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


# ---------------------------------------------------------------------------
# Finding #29: the runner reported REAL HH SUBMIT: 1 for a run that had no
# executor at all, and post-submit verification then classified the PAGE, not
# this run - writing SUBMITTED and announcing it when nothing had been clicked.
# ---------------------------------------------------------------------------

def _runner_ready_app(vid: str = "135112049"):
    """A vacancy + package + review + application that can_submit() accepts."""
    from ai_assistant.application_review import (
        ApplicationReview,
        ReviewStatus,
        compute_review_fingerprint,
        save_application_review,
    )
    from ai_assistant.application_tracking import ApplicationStatus, set_application_status
    from ai_assistant.schema import Vacancy

    sid = f"hh:{vid}"
    app_id = f"app_{vid}"
    letter = ("Здравствуйте! Меня заинтересовала позиция. Обладаю многолетним опытом "
              "разработки сервисов на Python, PostgreSQL, FastAPI и Docker, готов к интервью.")
    db.init_db()
    db.save_vacancy(Vacancy(
        source="hh", source_job_id=vid, title="Старший разработчик Python",
        company="TechCorp Solutions",
        description="Разработка нагруженных сервисов на Python, PostgreSQL, FastAPI, Docker.",
        job_url=f"https://hh.ru/vacancy/{vid}", location="Remote",
    ))
    pkg = {"vacancy_stable_id": sid, "cover_letter": letter,
           "title": "Старший разработчик Python", "employer": "TechCorp Solutions",
           "validation_status": "VALID"}
    db.save_application_package(sid, "v1", json.dumps(pkg))
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid, status=ReviewStatus.APPROVED,
        form_fingerprint=compute_review_fingerprint(sid, pkg), review_id=f"rev_{vid}",
    ))
    db.save_hh_application({
        "application_id": app_id, "vacancy_stable_id": sid, "vacancy_id": vid,
        "title": "Старший разработчик Python", "employer": "TechCorp Solutions",
        "draft": letter, "state": "READY_TO_SUBMIT",
    })
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    return app_id, sid


def _no_executor_but_verifier_sees_a_page(monkeypatch, *, page_shows_chat: bool):
    """No executor for the runner, yet the verifier's own retry reads a page.

    This is the divergence the old code turned into a fake submission: the
    runner asks for the *vacancy* tab and gets nothing, while the verifier falls
    back to any hh.ru tab and succeeds.
    """
    from ai_assistant import cli, hh_browser_launcher, hh_vacancy_navigator, telegram_notifier

    notifications: list[str] = []

    def fake_ev(script):
        return json.dumps({
            "url": "https://hh.ru/vacancy/135112049", "title": "Старший разработчик Python",
            "is_chat": page_shows_chat, "has_responded_success": False,
            "has_submit_btn": False, "has_apply_btn": False,
        })

    def resolve(endpoint, target=None, *a, **kw):
        return fake_ev if target == "hh.ru" else None

    monkeypatch.setattr(cli, "_default_hh_cdp_url", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(cli, "_resolve_hh_evaluate", resolve)
    monkeypatch.setattr(hh_browser_launcher, "ensure_hh_browser", lambda **kw: {"ok": True})
    monkeypatch.setattr(hh_vacancy_navigator, "ensure_open_vacancy_tab", lambda *a, **kw: None)
    monkeypatch.setattr(telegram_notifier, "send_post_submit_notification",
                        lambda **kw: notifications.append(kw.get("title", "?")))
    return notifications


def test_runner_without_an_executor_reports_zero_real_submits(monkeypatch):
    """Measured before the fix: REAL HH SUBMIT: 1 with no click and no submit."""
    from ai_assistant.hh_application_runner import run_application

    app_id, _sid = _runner_ready_app()
    _set_submit_allowed(True)
    _no_executor_but_verifier_sees_a_page(monkeypatch, page_shows_chat=False)

    res = run_application(app_id, confirm_submit=True, evaluate_fn=None)

    assert res.real_hh_submit == 0, res
    assert "NOT submitted" in res.reason, res.reason


def test_runner_without_an_executor_does_not_mark_the_vacancy_submitted(monkeypatch):
    """The worst variant: the page already shows a chat, so the old code wrote
    SUBMITTED - and the duplicate guard then skipped that vacancy forever."""
    from ai_assistant.hh_application_runner import run_application

    app_id, sid = _runner_ready_app()
    _set_submit_allowed(True)
    _no_executor_but_verifier_sees_a_page(monkeypatch, page_shows_chat=True)

    res = run_application(app_id, confirm_submit=True, evaluate_fn=None)

    assert res.final_application_state != "SUBMITTED", res
    assert (db.get_hh_application(app_id) or {}).get("state") != "SUBMITTED"
    assert db.get_submission_claim(sid) is None or db.get_submission_claim(sid)["status"] != "SUBMITTED"


def test_runner_without_an_executor_sends_no_submission_notification(monkeypatch):
    """The old code sent "Application submitted" to Telegram for a run that
    never touched a browser. This is an outward claim, so it gets its own test."""
    from ai_assistant.hh_application_runner import run_application

    app_id, _sid = _runner_ready_app()
    _set_submit_allowed(True)
    notifications = _no_executor_but_verifier_sees_a_page(monkeypatch, page_shows_chat=True)

    run_application(app_id, confirm_submit=True, evaluate_fn=None)

    assert notifications == [], notifications


def test_runner_without_an_executor_does_not_verify_a_submit_it_never_made(monkeypatch):
    """Step 6 used to run anyway, which is the mechanism that produced SUBMITTED."""
    from ai_assistant.hh_application_runner import run_application

    app_id, _sid = _runner_ready_app()
    _set_submit_allowed(True)
    _no_executor_but_verifier_sees_a_page(monkeypatch, page_shows_chat=True)

    res = run_application(app_id, confirm_submit=True, evaluate_fn=None)

    assert res.post_submit_verification.value == "NOT_RUN", res
    assert res.navigation.value == "PASS"  # the dry URL check still ran


def test_runner_with_an_executor_still_reaches_verification(monkeypatch):
    """Counter-check: the new guard must not fire when a browser exists."""
    from types import SimpleNamespace

    from ai_assistant import hh_application_runner as hr
    from ai_assistant import hh_submission

    app_id, _sid = _runner_ready_app("135112050")
    _set_submit_allowed(True)
    # The navigation pre-check is not what this test is about; the runner
    # imports it at module level, so patch it there.
    monkeypatch.setattr(
        hr, "verify_and_navigate_hh_vacancy",
        lambda **kw: SimpleNamespace(ok=True, status="OK", reason="nav ok"))
    monkeypatch.setattr(
        hh_submission, "execute_hh_submission",
        lambda **kw: SimpleNamespace(status="OK", submit_count=1, reason="submitted",
                                     gate_check_result=None))
    monkeypatch.setattr(
        hr, "verify_hh_submitted_application",
        lambda *a, **kw: SimpleNamespace(
            verification_verdict="PASS", evidence_text="responded-success", hh_status="responded-success",
            vacancy_url="https://hh.ru/vacancy/135112050", timestamp="2026-09-14T00:00:00", reason="ok"))

    res = hr.run_application(app_id, confirm_submit=True, evaluate_fn=lambda js: "{}")

    assert "No browser executor" not in res.reason, res.reason
    assert res.real_hh_submit == 1, res


def test_questionnaire_already_responded_does_not_invent_a_submit_count(monkeypatch):
    """Sibling of #29: the ALREADY_RESPONDED path claimed submit_count = 1 while
    click_count stayed 0 - a submit with no click."""
    from types import SimpleNamespace

    from ai_assistant import hh_vacancy_navigator
    from ai_assistant.hh_questionnaire import submit_questionnaire_response

    monkeypatch.setattr(
        hh_vacancy_navigator, "verify_and_navigate_hh_vacancy",
        lambda **kw: SimpleNamespace(ok=False, status="ALREADY_RESPONDED",
                                     reason="Application already sent"))

    qid = _save_questionnaire("q29_already_responded")
    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_personal": "123456789012"},
        confirm_submit=True, evaluate_fn=lambda js: "{}")

    assert res.verdict == "ALREADY_SUBMITTED", res
    assert res.submit_count == 0, res
    assert res.click_count == 0, res


# ---------------------------------------------------------------------------
# BLE001 finding #30 - the submission audit trail must not drop a category it
# could not read. Before the fix each of the five queries sat in
# `except Exception: pass`, so a schema drift removed whole categories from the
# trail; when all five failed the function returned [] and the CLI printed
# "No audit events found." with exit code 0 - a vacancy with a submitted and
# VERIFIED application looked like one where nothing had ever happened.
# ---------------------------------------------------------------------------

_BLE30_VAC = "hh:999000111"


def _ble30_db(monkeypatch, tmp_path, *, seed=True):
    """A temp DB holding one complete submission history."""
    from ai_assistant import config

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "ble30.db"), raising=False)
    db.init_db()
    if not seed:
        return
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO application_submissions (vacancy_stable_id, submission_id, executor_version,"
        " submission_json, status, submitted_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (_BLE30_VAC, "sub-1", "v1", '{"submission_id": "sub-1"}', "SUBMITTED",
         "2026-09-01T10:00:00", "2026-09-01T10:00:00", "2026-09-01T10:00:00"),
    )
    cur.execute(
        "INSERT INTO submission_verifications (vacancy_stable_id, submission_id, verification_version,"
        " verification_status, verification_json, verified_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (_BLE30_VAC, "sub-1", "v1", "VERIFIED", '{"success_signal": "chat visible"}',
         "2026-09-01T10:05:00", "2026-09-01T10:05:00", "2026-09-01T10:05:00"),
    )
    cur.execute(
        "INSERT INTO application_reviews (vacancy_stable_id, review_json, status, note,"
        " created_at, updated_at, review_version) VALUES (?,?,?,?,?,?,?)",
        (_BLE30_VAC, "{}", "APPROVED", "ok", "2026-09-01T09:00:00", "2026-09-01T09:00:00", "v1"),
    )
    cur.execute(
        "INSERT INTO browser_preparations (vacancy_stable_id, url, status, final_url, page_title,"
        " site, form_detected, fields_json, warnings_json, screenshot_path, created_at, updated_at,"
        " executor_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_BLE30_VAC, "https://hh.ru/vacancy/999000111", "PREPARED", "", "", "hh.ru", 1, "[]", "[]",
         "", "2026-09-01T08:00:00", "2026-09-01T08:00:00", "v1"),
    )
    cur.execute(
        "INSERT INTO application_status_history (vacancy_stable_id, old_status, new_status,"
        " changed_at, note) VALUES (?,?,?,?,?)",
        (_BLE30_VAC, None, "SUBMITTED", "2026-09-01T10:00:01", "submitted"),
    )
    conn.commit()
    conn.close()


def _ble30_drift(table, column):
    """Simulate schema drift: rename a column the audit query depends on.

    `init_db()` does not undo this - the table exists, so CREATE TABLE IF NOT
    EXISTS is a no-op and the query fails with "no such column", which is what a
    half-applied migration or a rolled-back release looks like.
    """
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(f"ALTER TABLE {table} RENAME COLUMN {column} TO {column}_renamed")
    conn.commit()
    conn.close()


def test_audit_trail_emits_no_marker_when_everything_is_readable(monkeypatch, tmp_path):
    """Counter-check: the marker must not become a permanent fixture."""
    _ble30_db(monkeypatch, tmp_path)
    from ai_assistant.submission_recovery import AUDIT_INCOMPLETE, get_submission_audit

    events = get_submission_audit(_BLE30_VAC)
    assert [e["type"] for e in events] == [
        "BROWSER_PREPARE", "REVIEW", "SUBMISSION", "TRACKING", "VERIFICATION"]
    assert all(e["type"] != AUDIT_INCOMPLETE for e in events)


def test_audit_trail_names_the_category_it_could_not_read(monkeypatch, tmp_path):
    _ble30_db(monkeypatch, tmp_path)
    _ble30_drift("application_submissions", "submission_json")

    from ai_assistant.submission_recovery import AUDIT_INCOMPLETE, get_submission_audit

    events = get_submission_audit(_BLE30_VAC)
    markers = [e for e in events if e["type"] == AUDIT_INCOMPLETE]
    assert len(markers) == 1, events
    assert "SUBMISSION" in markers[0]["detail"], markers[0]
    assert markers[0]["status"] == "INCOMPLETE"
    # The readable categories must still be there - the fix must not blank the
    # whole trail just because one query failed.
    assert [e["type"] for e in events if e["type"] != AUDIT_INCOMPLETE] == [
        "BROWSER_PREPARE", "REVIEW", "TRACKING", "VERIFICATION"]


def test_audit_trail_is_never_silently_empty_when_every_query_fails(monkeypatch, tmp_path):
    """The old code returned [] here, and the CLI called that "no events"."""
    _ble30_db(monkeypatch, tmp_path)
    for table, column in [
        ("application_reviews", "note"),
        ("browser_preparations", "url"),
        ("application_submissions", "submission_json"),
        ("submission_verifications", "verification_json"),
        ("application_status_history", "note"),
    ]:
        _ble30_drift(table, column)

    from ai_assistant.submission_recovery import AUDIT_INCOMPLETE, get_submission_audit

    events = get_submission_audit(_BLE30_VAC)
    assert events, "an unreadable trail must not come back empty"
    assert len(events) == 5, events
    assert all(e["type"] == AUDIT_INCOMPLETE for e in events)


def test_audit_cli_exits_nonzero_when_the_trail_is_incomplete(monkeypatch, tmp_path, capsys):
    _ble30_db(monkeypatch, tmp_path)
    _ble30_drift("submission_verifications", "verification_json")

    from ai_assistant import cli

    code = cli.submissions_audit(_BLE30_VAC)
    out = capsys.readouterr().out
    assert code == 2, "an incomplete trail must not exit 0"
    assert "INCOMPLETE" in out
    assert "No audit events found." not in out


def test_audit_cli_exits_zero_on_a_healthy_trail(monkeypatch, tmp_path, capsys):
    """Counter-check: the new exit code must not fire on readable data."""
    _ble30_db(monkeypatch, tmp_path)

    from ai_assistant import cli

    code = cli.submissions_audit(_BLE30_VAC)
    out = capsys.readouterr().out
    assert code == 0
    assert "VERIFICATION" in out
    assert "INCOMPLETE" not in out


def test_audit_cli_still_reports_a_genuinely_empty_history(monkeypatch, tmp_path, capsys):
    """A vacancy with no history keeps its own answer, and still exits 0."""
    _ble30_db(monkeypatch, tmp_path, seed=False)

    from ai_assistant import cli

    code = cli.submissions_audit(_BLE30_VAC)
    out = capsys.readouterr().out
    assert code == 0
    assert "No audit events found." in out


# ---------------------------------------------------------------------------
# BLE001 finding #31: the duplicate-submission guard read "cannot look" as
# "nothing there". get_submission_evidence() logged a warning for every source
# that raised and carried on with an empty value, so a database it could not
# read produced is_already_applied == False and can_submit() == (True, None) -
# and the two guards in browser_executor let the run continue towards a second
# application.
# ---------------------------------------------------------------------------

_BLE31_VAC = "hh:999000222"


def _ble31_break_reads(monkeypatch):
    """Make every source get_submission_evidence() reads raise."""

    def boom(*a, **k):
        raise _Boom("database is locked")

    from ai_assistant import submission_state as ss

    monkeypatch.setattr(db, "get_connection", boom)
    monkeypatch.setattr(db, "get_all_submissions", boom)
    monkeypatch.setattr(db, "get_hh_application_by_vacancy", boom)
    monkeypatch.setattr(db, "get_submission_claim", boom)
    monkeypatch.setattr(ss, "get_application_status", boom)


def _ble31_fresh_db(monkeypatch, tmp_path, name="ble31.db"):
    from ai_assistant import config

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / name), raising=False)
    db.init_db()


def test_evidence_records_every_source_it_could_not_read(monkeypatch, tmp_path):
    from ai_assistant.submission_state import get_submission_evidence

    _ble31_fresh_db(monkeypatch, tmp_path)
    _ble31_break_reads(monkeypatch)

    ev = get_submission_evidence(_BLE31_VAC)

    assert len(ev.read_errors) == 5, ev.read_errors
    for name in ("application_tracking", "application_submissions",
                 "submission_verifications", "hh_applications", "submission_claims"):
        assert any(name in err for err in ev.read_errors), name
    assert ev.is_already_applied is True
    can, reason = ev.can_submit()
    assert can is False
    assert "could not be read" in reason
    assert "database is locked" in reason


def test_evidence_is_silent_about_read_errors_on_a_healthy_database(monkeypatch, tmp_path):
    """Counter-check: a working database produces no read errors at all."""
    from ai_assistant.submission_state import get_submission_evidence

    _ble30_db(monkeypatch, tmp_path)  # one complete submission history
    ev = get_submission_evidence(_BLE30_VAC)

    assert ev.read_errors == []
    assert ev.is_already_applied is True
    can, reason = ev.can_submit()
    assert can is False
    # ...and the refusal names the evidence, not a broken read.
    assert "could not be read" not in reason
    assert "SUBMITTED" in reason


def test_evidence_stays_permissive_when_there_is_genuinely_nothing(monkeypatch, tmp_path):
    """Counter-check: an empty history is not a failure.

    This is the whole point of the fix - "I could not read the evidence" and
    "there is no evidence" must not collapse into one answer.
    """
    from ai_assistant.submission_state import get_submission_evidence

    _ble30_db(monkeypatch, tmp_path, seed=False)
    ev = get_submission_evidence(_BLE31_VAC)

    assert ev.read_errors == []
    assert ev.is_already_applied is False
    assert ev.can_submit() == (True, None)


def test_duplicate_guard_blocks_when_evidence_is_unreadable(monkeypatch, tmp_path):
    _ble31_fresh_db(monkeypatch, tmp_path, "ble31_guard.db")
    # init_db() itself goes through db.get_connection(), which we are about to
    # break; the guard under test sits after it.
    monkeypatch.setattr(be, "init_db", lambda: None)
    _ble31_break_reads(monkeypatch)

    res = be.submit_application_in_browser(_BLE31_VAC, dry_run=True)

    assert res.status == "BLOCKED"
    assert "Cannot verify whether this vacancy was already applied" in res.error
    assert "database is locked" in res.error


def test_duplicate_guard_still_lets_a_clean_vacancy_through(monkeypatch, tmp_path):
    """Counter-check: the new branch blocks unreadable evidence, not everything."""
    _ble31_fresh_db(monkeypatch, tmp_path, "ble31_clean.db")

    res = be.submit_application_in_browser(_BLE31_VAC, dry_run=True)

    assert "Cannot verify whether this vacancy was already applied" not in res.error
    # It got past the guard and stopped on the missing vacancy row instead.
    assert "Vacancy not found" in res.error


# ---------------------------------------------------------------------------
# BLE001 finding #32: in preflight_submission(), the "is this the vacancy the
# human approved?" check sat behind a try/except that fell back to "". An empty
# value skips the comparison, so a review entry whose "gate" field was not a
# mapping silently disabled the check. Measured with the same mismatched
# vacancy: a dict gave FAIL_CLOSED, a string gave READY_TO_SUBMIT.
# ---------------------------------------------------------------------------


class _StubReviewStore:
    def __init__(self, entry):
        self._entry = entry

    def get(self, review_id):
        return self._entry


def _ble32_preflight(monkeypatch, tmp_path, gate_value, package_vac="hh:136591579"):
    import json as _json

    from ai_assistant import config
    from ai_assistant import hh_submission as hs
    from ai_assistant.hh_submission import clear_submitted_reviews, preflight_submission

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "ble32.db"), raising=False)
    db.init_db()
    clear_submitted_reviews()
    _set_submit_allowed(True)

    url = f"https://hh.ru/applicant/vacancy_response?vacancyId={package_vac.split(':')[1]}"

    def evaluate(expression: str) -> str:
        if expression == hs._URL_JS:
            return _json.dumps({"url": url})
        if expression == hs._SUBMIT_BTN_JS:
            return _json.dumps({"found": True, "disabled": False, "tag": "BUTTON"})
        raise _Boom(f"unexpected expression: {expression[:60]}")

    fingerprint = "f" * 64
    entry = {
        "state": "HUMAN_APPROVED",
        "fingerprint": fingerprint,
        "review_id": "rid-1",
        "gate": gate_value,
    }
    package = SimpleNamespace(vacancy_stable_id=package_vac, validation_status="VALID")
    plan = SimpleNamespace(status="VALID", unresolved=[])
    orchestration = SimpleNamespace(
        verdict="VERIFIED", failed_operations=0, skipped_operations=0, errors=[])
    return preflight_submission(
        _StubReviewStore(entry), "rid-1", fingerprint, package, plan, orchestration, evaluate)


def test_preflight_fails_closed_when_the_review_gate_field_is_unreadable(monkeypatch, tmp_path):
    from ai_assistant.hh_submission import SubmissionStatus

    rep = _ble32_preflight(monkeypatch, tmp_path, '{"vacancy_stable_id": "hh:999999999"}')

    assert rep.status == SubmissionStatus.FAIL_CLOSED
    assert "unreadable" in rep.reason
    assert rep.submit_count == 0


def test_preflight_still_catches_a_mismatch_in_a_well_formed_field(monkeypatch, tmp_path):
    """Counter-check: the honest case keeps its own, more precise refusal."""
    from ai_assistant.hh_submission import SubmissionStatus

    rep = _ble32_preflight(monkeypatch, tmp_path, {"vacancy_stable_id": "hh:999999999"})

    assert rep.status == SubmissionStatus.FAIL_CLOSED
    assert "mismatch" in rep.reason


def test_preflight_tolerates_a_review_that_never_had_gate_data(monkeypatch, tmp_path):
    """Counter-check: an absent field is legitimate - reviews predate gate data.

    Only "present but unreadable" is an anomaly; absence must not block a
    submission that is otherwise ready.
    """
    from ai_assistant.hh_submission import SubmissionStatus

    rep = _ble32_preflight(monkeypatch, tmp_path, None)

    assert rep.status == SubmissionStatus.READY_TO_SUBMIT


# ---------------------------------------------------------------------------
# BLE001 finding #33: an application_queue row whose JSON cannot be parsed was
# invisible to everything. list_queue() dropped it with a bare `continue`,
# get_queue_item() answered None - the same answer as "not in the queue" - and
# the integrity audit, which validates verification JSON for exactly this
# reason, had no equivalent check for the queue. Measured on a 2-row queue with
# one corrupt row: audit errors 0 -> 1, while the report's own queue_items
# count said 2 and the audit had only been able to read 1.
# ---------------------------------------------------------------------------

_BLE33_GOOD = "hh:999000311"
_BLE33_BAD = "hh:999000312"


def _ble33_queue_item(vac, rank):
    from ai_assistant.application_queue import QUEUE_VERSION, QueueItem

    return QueueItem(
        vacancy_stable_id=vac,
        canonical_id=f"canon_{vac}",
        representative_vacancy_stable_id=vac,
        priority_score=80,
        rank=rank,
        company="Acme",
        title="Dev",
        source="hh",
        queue_version=QUEUE_VERSION,
    )


def _ble33_seed(monkeypatch, tmp_path, *, corrupt=True):
    """A two-row v2 queue, optionally with one row's JSON corrupted."""
    from ai_assistant import config
    from ai_assistant.application_queue import QUEUE_VERSION, save_queue_item

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "ble33.db"), raising=False)
    db.init_db()
    save_queue_item(_ble33_queue_item(_BLE33_GOOD, 0))
    save_queue_item(_ble33_queue_item(_BLE33_BAD, 1))
    if corrupt:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE application_queue SET queue_json=? WHERE vacancy_stable_id=?",
            ("{'broken': ", _BLE33_BAD),
        )
        conn.commit()
        conn.close()
    return QUEUE_VERSION


def test_list_queue_names_the_row_it_could_not_read(monkeypatch, tmp_path, caplog):
    import logging

    from ai_assistant.application_queue import list_queue

    version = _ble33_seed(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING, logger="ai_assistant.application_queue"):
        items = list_queue(queue_version=version)

    # The readable rows still come back...
    assert [i.vacancy_stable_id for i in items] == [_BLE33_GOOD]
    # ...and the dropped one is named instead of vanishing in silence.
    assert _BLE33_BAD in caplog.text
    assert "could not be read" in caplog.text


def test_get_queue_item_says_so_when_the_row_is_unreadable(monkeypatch, tmp_path, caplog):
    import logging

    from ai_assistant.application_queue import get_queue_item

    version = _ble33_seed(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING, logger="ai_assistant.application_queue"):
        item = get_queue_item(_BLE33_BAD, version)

    assert item is None
    assert _BLE33_BAD in caplog.text
    assert "unreadable" in caplog.text


def test_integrity_audit_reports_unreadable_queue_json(monkeypatch, tmp_path):
    from ai_assistant.application_integrity import run_integrity_audit

    _ble33_seed(monkeypatch, tmp_path)
    rep = run_integrity_audit(scope="full")

    bad = [i for i in rep.issues if i.code == "INVALID_QUEUE_JSON"]
    assert [i.vacancy_stable_id for i in bad] == [_BLE33_BAD]
    assert rep.error_count >= 1


def test_integrity_audit_is_quiet_on_a_healthy_queue(monkeypatch, tmp_path):
    """Counter-check: readable rows must not produce this issue."""
    from ai_assistant.application_integrity import run_integrity_audit

    _ble33_seed(monkeypatch, tmp_path, corrupt=False)
    rep = run_integrity_audit(scope="full")

    assert [i for i in rep.issues if i.code == "INVALID_QUEUE_JSON"] == []


def test_list_queue_still_returns_every_readable_row(monkeypatch, tmp_path):
    """Counter-check: the reader is not allowed to become stricter."""
    from ai_assistant.application_queue import list_queue

    version = _ble33_seed(monkeypatch, tmp_path, corrupt=False)
    items = list_queue(queue_version=version)

    assert {i.vacancy_stable_id for i in items} == {_BLE33_GOOD, _BLE33_BAD}


# ---------------------------------------------------------------------------
# Finding #34 - a vacancy with screening questions cleared all 11 gates
# ---------------------------------------------------------------------------
# Gate 8 reads `form_snapshot["fields"]`, which execute_hh_submission builds as
# `pkg_data.get("questions") or []` - the application package, never the page.
# On a vacancy with screening questions nothing fills that list, so the loop ran
# over an empty set and reported "All questions resolved" while the page was
# holding questions nobody had read.
#
# Measured before the fix, on a fresh vacancy whose screening form was not
# filled: the live check answered READY and check_all_gates returned passed=True
# with "All 11 gates passed successfully", gate 8 "All questions resolved". The
# click then fell through to the "Откликнуться" link (the popup's submit button
# does not exist yet), clicked it and reported {ok: true} for a click that
# cannot have submitted anything.
#
# The control count below is the real one: artifacts/hh_manual_form_snapshot.json
# is a live hh.ru response form with 48 controls, 17 distinct names, 11 questions.

def _ble34_live(controls: int, unanswered: int = 0):
    """A live-page result whose page holds `controls` answer controls."""
    from ai_assistant.hh_live_page_checks import LivePageResult

    return LivePageResult(
        is_ok=True,
        status="READY",
        reason="Live page verified and ready for submission",
        current_url="https://hh.ru/vacancy/1",
        numeric_id_match=True,
        title_matched=True,
        has_apply_btn=True,
        screening_control_count=controls,
        screening_unanswered_count=unanswered,
    )


def test_a_page_holding_an_unfilled_form_is_not_ready_to_submit(monkeypatch):
    """The page holds the form; the package holds no questions. That is not
    "no questions", it is "questions nobody read"."""
    from ai_assistant.hh_submission import GateName, HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs("999000801")
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=False,
        candidate_profile=profile, live_page_result=_ble34_live(48, 11),
    )

    assert res.passed is False, res.reason
    assert res.failed_gate == GateName.GATE_NO_UNKNOWN_QUESTIONS
    record = res.gate_results[GateName.GATE_NO_UNKNOWN_QUESTIONS.value]
    assert record["passed"] is False
    assert "48" in record["reason"]
    assert "All questions resolved" not in record["reason"]


def test_a_vacancy_without_a_form_is_still_ready(monkeypatch):
    """Counter-check: no controls on the page means no form, and a vacancy
    without screening questions must keep sailing through."""
    from ai_assistant.hh_submission import HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs("999000802")
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=False,
        candidate_profile=profile, live_page_result=_ble34_live(0, 0),
    )

    assert res.passed is True, res.reason


def test_a_form_whose_questions_were_collected_is_still_ready(monkeypatch):
    """Counter-check: the new branch must not block the case it is not about.
    The page holds a form AND the package carries the questions, answered."""
    from ai_assistant.hh_submission import HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs("999000803")
    snapshot = dict(snapshot)
    snapshot["fields"] = [
        {"type": "number", "label": "Сколько лет коммерческого опыта?",
         "required": True, "value": "5"},
    ]
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=False,
        candidate_profile=profile, live_page_result=_ble34_live(48, 0),
    )

    assert res.passed is True, res.reason
    assert res.gate_results["no_unknown_questions"]["passed"] is True


def _ble34_click_setup(monkeypatch, vid: str, live_controls: int):
    """Reach the click: gates stubbed green (they have their own tests above)."""
    from ai_assistant import hh_live_page_checks
    from ai_assistant.hh_submission import GateCheckResult, HHSubmissionGates

    sid, _url, _snapshot, _profile = _gate18_inputs(vid)
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)
    monkeypatch.setattr(db, "get_vacancy_by_id", lambda vacancy_id: ("row",))
    monkeypatch.setattr(
        db, "_row_to_vacancy", lambda row: SimpleNamespace(title="Python Developer")
    )
    monkeypatch.setattr(
        hh_live_page_checks, "check_live_page", lambda *a, **kw: _ble34_live(live_controls)
    )
    monkeypatch.setattr(
        HHSubmissionGates, "check_all_gates",
        classmethod(lambda cls, *a, **kw: GateCheckResult(passed=True, reason="stub: gates green")),
    )
    return sid


def test_the_click_refuses_a_page_holding_an_unfilled_form(monkeypatch):
    """Second layer: the guard reads the page at the moment of the mutation."""
    from ai_assistant.hh_submission import execute_hh_submission

    sid = _ble34_click_setup(monkeypatch, "999000804", live_controls=0)

    def stub(js: str) -> str:
        if "hh_submit_click" in js:
            return json.dumps({
                "ok": False,
                "refused": True,
                "reason": ("unanswered_screening_questions: 11 answer field(s) of "
                           "the application form are not filled"),
                "unanswered_count": 11,
            })
        return "{}"

    res = execute_hh_submission(sid, evaluate_fn=stub, human_confirmed=True)

    assert res.status == "FAILED_SAFE", res.reason
    assert res.submit_count == 0
    assert "unanswered_screening_questions" in res.reason
    # A refusal we made ourselves is not an ambiguous outcome: nothing was
    # clicked, so nothing may be recorded as "maybe applied".
    assert res.status != "AMBIGUOUS"
    claim = db.get_submission_claim(sid)
    assert claim is not None and claim["status"] == "FAILED_SAFE", claim


def test_the_click_still_fires_when_the_page_has_no_form(monkeypatch):
    """Counter-check: the guard must not become a way to block every submit."""
    from ai_assistant.hh_submission import execute_hh_submission

    sid = _ble34_click_setup(monkeypatch, "999000805", live_controls=0)

    def stub(js: str) -> str:
        if "hh_submit_click" in js:
            return json.dumps({"ok": True})
        return "{}"

    res = execute_hh_submission(sid, evaluate_fn=stub, human_confirmed=True)

    assert res.submit_count == 1, res.reason
    assert res.status in ("SUBMITTED", "AMBIGUOUS"), res.status


def _strip_js_comments(js: str) -> str:
    """Drop whole-line JS comments: a comment quoting the old behaviour must
    not satisfy a pin."""
    return "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))


def _click_js_template() -> str:
    """The click JS as written inside execute_hh_submission, comments stripped.

    Still carries the placeholder: the guard is injected when the function
    runs, not at import.
    """
    import inspect

    from ai_assistant.hh_submission import execute_hh_submission

    src = inspect.getsource(execute_hh_submission)
    marker = '_inject_screening_guard("""// hh_submit_click'
    assert marker in src, (
        "execute_hh_submission no longer builds the click JS through "
        "_inject_screening_guard - the shared guard would not be injected"
    )
    js = src.split(marker, 1)[1].split('"""', 1)[0]
    return _strip_js_comments(js)


def _click_js_source() -> str:
    """What the browser is actually handed: template + shared guard."""
    from ai_assistant.hh_live_page_checks import _inject_screening_guard

    return _strip_js_comments(_inject_screening_guard(_click_js_template()))


def test_the_screening_guard_is_one_block_shared_by_both_js_snippets():
    """The guard is a Python string that Python never runs, so every test that
    exercises the flow stubs the JS out entirely.

    That is how a mutation which broke the selector in one of the two copies
    survived the whole suite: the pin asserted the fragment "name^='task_'",
    and the textarea/select half of the selector still contained it. So pin the
    full literal, and pin that there is only one copy of it left to break.
    """
    from ai_assistant import hh_live_page_checks as live

    guard = _strip_js_comments(live._SCREENING_GUARD_JS)

    # Both halves of the selector, spelled out - a pin on "name^='task_'"
    # alone is satisfied after the radio/checkbox half has been broken.
    assert ("input[type='radio'][name^='task_'], input[type='checkbox'][name^='task_']"
            in guard)
    assert "textarea[name^='task_'], select[name^='task_']" in guard
    assert guard.count("name^='task_'") == 4
    # The "Свой вариант" companion is not a question of its own; without the
    # allNames rule the real 11-question form reports 17.
    assert "allNames[n.slice(0, -5)]" in guard

    live_js = _strip_js_comments(live._INSPECT_LIVE_PAGE_JS)
    click_tmpl = _click_js_template()
    click_js = _click_js_source()

    # One block, interpolated into both snippets, and no second copy of the
    # selector in either of them.
    assert guard in live_js
    assert guard in click_js
    assert live_js.count("name^='task_'") == 4
    assert click_js.count("name^='task_'") == 4
    # The live snippet is injected at import, the click snippet when the
    # function runs - so the marker is gone from the first and still present in
    # the second, and the call site really does go through the injector.
    assert live._SCREENING_GUARD_PLACEHOLDER not in live._INSPECT_LIVE_PAGE_JS
    assert live._SCREENING_GUARD_PLACEHOLDER in click_tmpl
    assert live._SCREENING_GUARD_PLACEHOLDER not in click_js


def test_injecting_the_screening_guard_fails_loudly_without_a_placeholder():
    """A snippet with no marker must raise: the placeholder is a bare JS
    identifier, so the un-injected JS would still parse and would fail only in
    the browser, on a live submission."""
    from ai_assistant.hh_live_page_checks import _inject_screening_guard

    with pytest.raises(ValueError):
        _inject_screening_guard("(() => { return JSON.stringify({ ok: true }); })()")


def test_the_click_guard_refuses_before_it_clicks():
    import re

    js = _click_js_source()

    assert "unfilledNames" in js
    assert "if (unfilledNames.length) {" in js
    assert "refused: true" in js
    assert "submitBtn.click()" in js
    # The refusal must RETURN, not merely be built: a guard that works out the
    # answer and then clicks anyway is the bug this whole finding is about.
    assert re.search(r"if \(unfilledNames\.length\) \{\s*return JSON\.stringify\(\{", js)
    # A refusal that arrives after the click is not a refusal.
    assert js.index("refused: true") < js.index("submitBtn.click()")


def test_the_live_inspection_reports_the_answer_controls():
    """The gate's input has to exist. Pin the keys it reads."""
    from ai_assistant import hh_live_page_checks as live

    js = _strip_js_comments(live._INSPECT_LIVE_PAGE_JS)

    assert "screening_control_count: answerFields.length" in js
    assert "screening_unanswered_count: unfilledNames.length" in js
    assert "screening_unanswered_sample: unfilledNames.slice(0, 5)" in js


def test_the_live_inspection_script_does_not_say_click():
    """Finding #35: a test double may decide what a script is by looking for
    substrings in it. A comment containing the word "click" inside the
    inspection JS made two doubles answer the inspection with the submit
    payload.

    This is the cheapest guard against that: keep the word out of the
    inspection script. It is not a style rule - measured, that word is what
    broke it. If the inspection ever genuinely needs it, teach the doubles to
    recognise the script by its own marker first, the way
    tests/test_stage46_application_runner.py now does.
    """
    from ai_assistant import hh_live_page_checks as live

    js = live._INSPECT_LIVE_PAGE_JS

    assert "click" not in js.lower(), (
        "the live-inspection script mentions 'click'; a double that dispatches "
        "on that substring answers the inspection with the submit payload "
        "(finding #35)"
    )
    # The marker is what a double should dispatch on instead.
    assert "hh_live_page_inspect" in js


def test_check_live_page_carries_the_control_counts_into_its_result():
    """The gate reads LivePageResult, so the inspection's counts must land
    there. Without this the branch could be fed by nothing and every other test
    in this section would still pass - they build LivePageResult by hand."""
    from ai_assistant.hh_live_page_checks import check_live_page

    page = json.dumps({
        "ok": True,
        "url": "https://hh.ru/vacancy/136591579",
        "title": "Senior AI Automation Engineer",
        "is_404": False, "is_captcha": False, "is_access_denied": False,
        "is_login_required": False, "already_responded": False,
        "has_submit_btn": False, "submit_btn_disabled": False,
        "has_apply_btn": True, "has_response_modal": False,
        "screening_control_count": 48, "screening_unanswered_count": 11,
    })

    res = check_live_page(
        lambda js: page,
        expected_vacancy_id="hh:136591579",
        expected_title="Senior AI Automation Engineer",
    )

    assert res.screening_control_count == 48
    assert res.screening_unanswered_count == 11
    # The page itself is still the right page: this is data, not a verdict.
    assert res.is_ok is True
    assert res.status == "READY"


# ---------------------------------------------------------------------------
# Finding #36 - the pass message claimed a number it never counted
# ---------------------------------------------------------------------------
# "All 11 gates passed successfully" was a constant. Measured on a green path:
# gate_results held exactly the 11 members of GateName, so the sentence was
# true - but nothing tied the number to the results, and a gate declared in
# GateName and never evaluated would still have been reported as passed. Same
# question as #34, one level up: is this number a count, or a claim?

def test_the_pass_message_counts_the_gates_that_actually_ran(monkeypatch):
    from ai_assistant.hh_submission import GateName, HHSubmissionGates

    sid, url, snapshot, profile = _gate18_inputs("999000806")
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(db, "is_submit_paused", lambda: False)

    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=False,
        candidate_profile=profile,
    )

    assert res.passed is True, res.reason
    declared = {g.value for g in GateName}
    assert set(res.gate_results) == declared, (
        "a gate declared in GateName was never evaluated, so the pass message "
        "would claim more than was checked. Missing: "
        f"{sorted(declared - set(res.gate_results))}"
    )
    # Derived from the results, not typed in: adding a 12th gate without
    # evaluating it breaks the line above, and evaluating it moves this number.
    assert res.reason == f"All {len(res.gate_results)} gates passed successfully", res.reason


# ---------------------------------------------------------------------------
# Finding #38 - a function named "extract" wrote to the database
# ---------------------------------------------------------------------------
# extract_hh_questionnaire_from_snapshot() ended with
#     db.init_db()
#     db.save_hh_questionnaire(quest.model_dump())
# Measured cost: a probe whose whole purpose was to compare a live form against
# the stored one created a row in the production state.db
# (quest_1c2be2caaff3bfff, created 2026-09-16T04:43:56, referenced by nothing),
# and four test files leaned on the side effect without ever saying so - renaming
# the callers to the persisting name turned 16 tests red, which is how the
# contract became visible.
#
# The comparison half of the questionnaire flow needs a read that records
# nothing, so the two jobs now have two names: extract_* is pure, discover_*
# records. These two tests hold that split down - without them the next person
# can put the save back and only the probe would notice.


def _one_question_snapshot(vid: str = "hh:999000901") -> dict:
    return {
        "vacancy_stable_id": vid,
        "title": "Probe vacancy",
        "employer": "Probe employer",
        "questions": [
            {
                "id": "q1_where",
                "text": "Where do you work from?",
                "type": "radio",
                "required": True,
                "options": ["Office", "Remote"],
            }
        ],
    }


def test_extracting_a_questionnaire_does_not_write_to_the_database():
    """Reading a form must not record it: the comparison path depends on that."""
    from ai_assistant.hh_questionnaire import extract_hh_questionnaire_from_snapshot

    db.init_db()  # the isolation fixture points DB_FILE at a fresh file
    before = db.list_hh_questionnaires()
    quest = extract_hh_questionnaire_from_snapshot(_one_question_snapshot())

    assert quest is not None, "the snapshot has one question, extraction must work"
    after = db.list_hh_questionnaires()
    assert len(after) == len(before), (
        "extract wrote to the database: "
        f"{len(before)} rows before, {len(after)} after. A read that records "
        "cannot be used to compare a live form against the stored one."
    )
    assert db.get_hh_questionnaire(quest.questionnaire_id) is None, (
        "extract persisted the questionnaire it was asked to only read"
    )


def test_discovering_a_questionnaire_does_record_it():
    """The counter-check: the persisting half must still persist."""
    from ai_assistant.hh_questionnaire import discover_hh_questionnaire_from_snapshot

    quest = discover_hh_questionnaire_from_snapshot(_one_question_snapshot())

    assert quest is not None
    row = db.get_hh_questionnaire(quest.questionnaire_id)
    assert row is not None, (
        "discover is the half that records, and it recorded nothing - a rename "
        "that quietly drops the write is worse than the surprise it replaced"
    )
    assert row["status"] == "NEEDS_HUMAN_REVIEW", row["status"]


# ---------------------------------------------------------------------------
# Finding #40 - binding a questionnaire the obvious way rewinds the state machine
# ---------------------------------------------------------------------------
# Option B needs to attach a questionnaire to an application. The obvious call is
#     db.save_hh_application({"application_id": app_id, "questionnaire_id": qid})
# and it is wrong. save_hh_application() is an upsert: most columns are written
# through COALESCE(excluded.x, existing.x) so a partial dict leaves them alone,
# but four columns are written as a bare `x = excluded.x`: state, error,
# last_transition_reason and updated_at. Measured on a full row: a partial dict
# rewound state READY_TO_SUBMIT -> NEW and erased both error ("previous failure"
# -> NULL) and last_transition_reason ("11 gates passed" -> NULL). The COALESCE
# columns all survived - which is exactly what makes the bare ones easy to miss.
#
# That is not cosmetic - an application rewound to NEW can be picked up and
# processed again. db.set_hh_application_questionnaire() does a targeted UPDATE
# instead, and record_questionnaire_for_vacancy() uses it.


def test_binding_a_questionnaire_the_obvious_way_rewinds_the_state_machine():
    """Documents the trap that made a narrow db function necessary.

    If save_hh_application() is ever fixed to COALESCE the bare columns, THIS
    TEST WILL FAIL - and that failure is an improvement, not a regression.
    Change it to assert the row survives intact, and
    record_questionnaire_for_vacancy() may then use either call.
    """
    db.init_db()
    db.save_hh_application({
        "application_id": "app_hh_999555",
        "vacancy_stable_id": "hh:999555",
        "title": "Title that must survive",
        "state": "READY_TO_SUBMIT",
        "error": "previous failure",
        "last_transition_reason": "11 gates passed",
    })
    before = db.get_hh_application("app_hh_999555")
    assert before["state"] == "READY_TO_SUBMIT"
    assert before["error"] == "previous failure"
    assert before["last_transition_reason"] == "11 gates passed"

    # the obvious way to attach a questionnaire
    db.save_hh_application({
        "application_id": "app_hh_999555",
        "questionnaire_id": "quest_abc",
    })

    after = db.get_hh_application("app_hh_999555")
    assert after["questionnaire_id"] == "quest_abc"
    assert after["title"] == "Title that must survive", (
        "COALESCE protected the title - that is what makes the state column easy"
        " to overlook"
    )
    assert after["state"] == "NEW", (
        f"the state column is no longer rewound by a partial upsert"
        f" (it came back as {after['state']!r}). If that is a deliberate fix,"
        " update this test rather than deleting it."
    )
    # state is the loud one, but it is not the only bare column: a partial dict
    # also erases the recorded error and the reason for the last transition.
    assert after["error"] is None, (
        f"error survived as {after['error']!r} - the bare-column trap is narrower"
        " than this test claims, so update the comment above"
    )
    assert after["last_transition_reason"] is None, (
        f"last_transition_reason survived as {after['last_transition_reason']!r}"
    )


def test_the_narrow_binding_call_leaves_the_state_machine_alone():
    """The counter-check: the call the collection path actually uses."""
    db.init_db()
    db.save_hh_application({
        "application_id": "app_hh_999556",
        "vacancy_stable_id": "hh:999556",
        "state": "READY_TO_SUBMIT",
    })

    updated = db.set_hh_application_questionnaire("app_hh_999556", "quest_abc")

    assert updated is True
    after = db.get_hh_application("app_hh_999556")
    assert after["questionnaire_id"] == "quest_abc"
    assert after["state"] == "READY_TO_SUBMIT", after["state"]

# ---------------------------------------------------------------------------
# Finding #41 - a questionnaire's `answers` dict did not reach the database
# ---------------------------------------------------------------------------
# HHQuestionnaire has an `answers: dict[str, Any]` field, and its name is the
# obvious place to put answers. It used to be a dead channel. The three lines
# that filled answers_json were inlined in TWO functions - save_hh_questionnaire
# and save_hh_application - and were wrong in two ways:
#     * a dict passed as `answers_json` was serialised from data["answers"],
#       the *other* field, so the caller's value was thrown away and "{}" was
#       stored. "{}" is not NULL, so on an update it wins the upsert's
#       COALESCE(excluded.answers_json, hh_questionnaires.answers_json) and
#       erases the answers already stored;
#     * a dict passed as `answers` never reached the column at all:
#       HHQuestionnaire.model_dump() sends `answers` with no `answers_json` key,
#       and the old guard (`if answers_json is not None and ...`) was False in
#       exactly that case.
# Both call sites now share db._serialise_answers_json(). The invariant the fix
# had to preserve is the COALESCE one above: an empty or absent `answers` must
# keep writing NULL, because "{}" is not NULL and would wipe stored answers on a
# questions-only re-discovery. Measured both ways in
# tools/hh_row_upsert_probe.py.


def test_a_questionnaire_answers_dict_reaches_the_database():
    """#41: every documented shape of `answers` must reach the column."""
    db.init_db()
    questions = [{
        "question_id": "q1", "text": "Where?", "question_type": "radio",
        "required": True, "options": ["A", "B"],
    }]
    expected = {"q1": "A"}

    # the shape HHQuestionnaire.model_dump() produces: `answers`, no answers_json
    db.save_hh_questionnaire({
        "questionnaire_id": "quest_ans_dict",
        "questions": questions,
        "answers": expected,
    })
    row = db.get_hh_questionnaire("quest_ans_dict")
    assert row["questions"], (
        "the questions channel stopped working too - that is a different bug"
    )
    assert row["answers"] == expected, (
        f"the model_dump() shape stored {row['answers']!r}. This is the shape the"
        " questionnaire is actually written in, so an empty result here means the"
        " answers a human gave are silently lost."
    )

    # a dict in answers_json must not be serialised from the OTHER field
    db.save_hh_questionnaire({
        "questionnaire_id": "quest_ans_dict_json",
        "questions": questions,
        "answers_json": expected,
    })
    assert db.get_hh_questionnaire("quest_ans_dict_json")["answers"] == expected, (
        "a dict passed as answers_json was serialised from `answers` instead -"
        " that is the first half of #41 coming back"
    )

    # the channel that always worked
    db.save_hh_questionnaire({
        "questionnaire_id": "quest_ans_str",
        "questions": questions,
        "answers_json": '{"q1": "A"}',
    })
    assert db.get_hh_questionnaire("quest_ans_str")["answers"] == expected, (
        "the JSON-string channel broke - that one is load-bearing"
    )


def test_an_empty_answers_dict_does_not_wipe_stored_answers():
    """#41's counter-check: the fix must not break the COALESCE invariant.

    The seed goes through `answers_json` as a JSON string - the one channel that
    worked before the fix too - so this test measures the COALESCE behaviour
    alone, not the channel repair. A naive fix (serialise whatever is in
    `answers`, empty dict included) would store "{}", and "{}" is not NULL, so
    COALESCE(excluded.answers_json, hh_questionnaires.answers_json) would pick it
    and a questions-only re-discovery would erase stored answers.
    """
    db.init_db()
    questions = [{
        "question_id": "q1", "text": "Where?", "question_type": "radio",
        "required": True, "options": ["A", "B"],
    }]
    expected = {"q1": "A"}

    db.save_hh_questionnaire({
        "questionnaire_id": "quest_ans_keep",
        "questions": questions,
        "answers_json": '{"q1": "A"}',
    })
    assert db.get_hh_questionnaire("quest_ans_keep")["answers"] == expected, (
        "the seed did not land, so this test cannot tell a wipe from a no-op"
    )

    # re-discovery: the same questionnaire, questions only, no answers at all
    db.save_hh_questionnaire({
        "questionnaire_id": "quest_ans_keep",
        "questions": questions,
    })
    assert db.get_hh_questionnaire("quest_ans_keep")["answers"] == expected, (
        "a questions-only re-save erased stored answers - the absent payload was"
        " serialised as something that is not NULL, so COALESCE let it win"
    )

    # an explicitly empty dict is the same case, not an exception
    db.save_hh_questionnaire({
        "questionnaire_id": "quest_ans_keep",
        "questions": questions,
        "answers": {},
    })
    assert db.get_hh_questionnaire("quest_ans_keep")["answers"] == expected, (
        "an explicit empty `answers` dict erased stored answers - {} must"
        " serialise to NULL so that COALESCE keeps the stored value"
    )

# ---------------------------------------------------------------------------
# Finding #42 - the result must distinguish "compared" from "not compared"
# ---------------------------------------------------------------------------
# submit_questionnaire_response() takes current_dom_fingerprint, and
# validate_human_answers() silently SKIPS the "form changed -> stop" comparison
# when it is None. No production caller passes one:
#   - hh_application_runner.py:532 answers from the database and never reads the
#     form page, so it has no fingerprint to pass;
#   - the two orchestrator methods that could pass one
#     (record_questionnaire_answers, execute_confirmed_submit) have no callers
#     outside tests (verified with git grep across ai_assistant/, tools/, *.py).
# So the invariant cannot fire in production - and before this the result object
# did not say so: "compared and matched" and "never compared" were the same
# object, which is finding #37's shape pointed the other way.
#
# The flag is NOT the fix. The fix is a live read at the submit point, which
# needs a probe on a real questionnaire. The flag is what keeps the gap visible
# instead of silent.


def test_a_submit_that_could_not_compare_the_fingerprint_says_so():
    """Measured: no fingerprint -> comparison skipped, and the result admits it."""
    qid = _save_questionnaire("q42_no_fingerprint")

    res = submit_questionnaire_response(
        questionnaire_id=qid, human_answers={"q_personal": "123456789012"},
        confirm_submit=True)

    assert res.fingerprint_checked is False, (
        "the result claims the fingerprint was compared, but none was supplied"
    )
    # and the submit took the ordinary path: the flag is observability, not a
    # new gate. A reader must not mistake "not compared" for "blocked".
    assert res.submit_count == 0
    assert res.status == "READY_TO_SUBMIT", res.status


def test_a_supplied_fingerprint_is_reported_as_compared(monkeypatch):
    """Counter-check, and the link between the flag and real behaviour: when a
    fingerprint IS supplied the comparison actually runs, so a mismatch stops
    the submit and the flag reads True."""
    _silence_telegram(monkeypatch)
    from ai_assistant.hh_questionnaire import (
        HHQuestionItem,
        compute_questionnaire_fingerprint,
    )

    question = {"question_id": "q_personal", "text": "Ваш ИНН?",
                "question_type": "text", "required": True, "options": []}
    fp = compute_questionnaire_fingerprint([HHQuestionItem(**question)])

    matching = _save_questionnaire("q42_match", [question], fingerprint=fp)
    res = submit_questionnaire_response(
        questionnaire_id=matching, human_answers={"q_personal": "123456789012"},
        confirm_submit=True, current_dom_fingerprint=fp)
    assert res.fingerprint_checked is True
    assert res.status == "READY_TO_SUBMIT", res.status

    changed = _save_questionnaire("q42_changed", [question], fingerprint=fp)
    res = submit_questionnaire_response(
        questionnaire_id=changed, human_answers={"q_personal": "123456789012"},
        confirm_submit=True, current_dom_fingerprint="a_different_fingerprint")
    assert res.fingerprint_checked is True, (
        "the flag must mean 'the comparison ran' - and here it ran: it is what"
        " stopped the submit"
    )
    assert res.status == "NEEDS_HUMAN_REVIEW", res.status
    assert res.submit_count == 0, "the stop must survive the new field"


# ---------------------------------------------------------------------------
# Finding #44 -- the candidate-profile fallback
#
# Three byte-identical inline blocks in browser_executor.py loaded the profile
# with a fallback that was silent twice over: a configured path that failed to
# read was swallowed and replaced by whatever the default search found, and when
# nothing was found at all the hard-coded default came back -- with no name,
# email or phone. Measured: seven attributes come back empty (name, email,
# phone_ru, phone_th, github, linkedin, portfolio), which through
# _get_profile_value_truth() costs eight form fields their values. The same
# profile feeds the hard-constraint gate on the submit path.
#
# The fallback is deliberate -- refusing to work because a file is missing would
# be a worse failure on this path -- so the pins below assert VISIBILITY, not
# refusal. A test that demanded an exception would be pinning the wrong repair.
# ---------------------------------------------------------------------------

_PROFILE_CONTACT_FIELDS = (
    "name", "email", "phone_ru", "phone_th", "github", "linkedin", "portfolio",
)


def _force_profile_fallback(monkeypatch, tmp_path):
    """Make every profile search location miss, so the fallback is reached."""
    from ai_assistant import candidate_profile as cp

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CANDIDATE_PROFILE", raising=False)
    monkeypatch.delenv("CANDIDATE_PROFILE_FILE", raising=False)
    monkeypatch.setattr(cp, "DEFAULT_PROFILE_PATHS", [tmp_path / "no_such_profile.json"])
    return cp


def test_a_missing_profile_is_reported_instead_of_silently_defaulted(
        monkeypatch, tmp_path, caplog):
    """Measured before the fix: 0 warnings. After: 1."""
    cp = _force_profile_fallback(monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING, logger=cp.__name__):
        profile = cp.load_candidate_profile()

    assert profile is not None, (
        "the fallback itself must stay: refusing to run because a file is missing"
        " would be a worse failure than running with a thin profile"
    )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "load_candidate_profile() fell back to the built-in default and said nothing."
        " That default carries no name, email or phone (seven attributes measured"
        " empty, eight form fields lose their value), so the silence is the defect."
    )
    assert "built-in default" in warnings[0].getMessage(), warnings[0].getMessage()


def test_the_built_in_default_is_the_one_without_contact_details(
        monkeypatch, tmp_path):
    """The premise of the finding, pinned so it cannot be argued away later."""
    cp = _force_profile_fallback(monkeypatch, tmp_path)
    fallback = cp.load_candidate_profile()

    for field in _PROFILE_CONTACT_FIELDS:
        assert getattr(fallback, field, None) in (None, "", [], {}), (
            f"the built-in default now carries {field!r}. If that is deliberate,"
            f" the warning text and this pin both need revisiting."
        )


def test_an_explicit_readable_path_still_returns_the_real_profile(tmp_path, caplog):
    """Counter-check: the helper must not turn a working path into a fallback."""
    from ai_assistant.candidate_profile import load_candidate_profile

    real = tmp_path / "real_profile.json"
    real.write_text(
        json.dumps({
            "name": "Test Human",
            "email": "test@example.com",
            "phone_ru": "+70000000000",
        }),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="ai_assistant.candidate_profile"):
        profile = load_candidate_profile(str(real))

    assert profile.name == "Test Human"
    assert profile.email == "test@example.com"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a readable explicit path produced a warning"
    )


def test_the_profile_helper_reports_a_configured_path_it_could_not_read(
        monkeypatch, tmp_path, caplog):
    """The other half of the silence: the configured path fails, the old code
    swapped in another profile without a word."""
    from ai_assistant import candidate_profile as cp
    from ai_assistant import config

    broken = tmp_path / "broken_profile.json"
    broken.write_text("{ not json at all", encoding="utf-8")
    monkeypatch.setattr(config, "CANDIDATE_PROFILE_FILE", str(broken))
    monkeypatch.setattr(cp, "DEFAULT_PROFILE_PATHS", [tmp_path / "no_such_profile.json"])

    with caplog.at_level(logging.WARNING, logger="ai_assistant.browser_executor"):
        profile = be._load_profile()

    assert profile is not None
    messages = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "could not be read" in messages, (
        "the configured profile path failed to read and the swap went unrecorded;"
        f" warnings seen: {messages!r}"
    )
    assert str(broken) in messages


def test_browser_executor_loads_the_profile_in_exactly_one_place():
    """Finding #44 as a class, the way test_no_second_copy_of_cdp_resolution()
    handles #14: three byte-identical inline copies is how the silence got
    triplicated, so the fix is one owner and the pin counts the copies.

    Substring matching would fire on the comments above, so walk the AST and
    require that every call to load_candidate_profile() sits inside _load_profile.
    """
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "ai_assistant" / "browser_executor.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def _called_name(node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        return None

    helper = next(
        (
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_load_profile"
        ),
        None,
    )
    assert helper is not None, "_load_profile() is gone; the profile load has no owner"

    inside_helper = [
        n for n in ast.walk(helper)
        if isinstance(n, ast.Call) and _called_name(n) == "load_candidate_profile"
    ]
    outside_helper = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and _called_name(n) == "load_candidate_profile"
        and not (helper.lineno <= n.lineno <= (helper.end_lineno or helper.lineno))
    ]
    call_sites = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _called_name(n) == "_load_profile"
    ]

    assert not outside_helper, (
        "a call to load_candidate_profile() lives outside _load_profile() -- that is"
        f" how the fallback went silent three times over. Lines:"
        f" {[n.lineno for n in outside_helper]}"
    )
    assert len(inside_helper) == 4, (
        f"_load_profile() should make four attempts (explicit path, no config path,"
        f" configured path, fallback); found {len(inside_helper)}"
    )
    assert len(call_sites) == 3, (
        f"expected three call sites (prepare once, submit twice), found"
        f" {len(call_sites)} at lines {[n.lineno for n in call_sites]}"
    )

# ---------------------------------------------------------------------------
# Finding #46 - an unreadable questionnaire must not audit as safe to submit
# ---------------------------------------------------------------------------
# db._row_to_questionnaire() fell back to questions=[] when the stored
# questions_json would not parse, and every gate that answers "may we submit?"
# iterates that list. An empty list satisfies all of them without looking at
# anything. Measured on a row with questions_json = "{not json", answers = "{}":
#
#   audit_questionnaire     -> SAFE_TO_SUBMIT, 0 items
#   validate_human_answers  -> ok=True, "All questionnaire requirements satisfied"
#   hh_submit_policy gate 5 -> checks_passed += "questionnaire_complete"
#
# Control, readable questions and the same empty answers:
#
#   audit_questionnaire     -> NEEDS_CORRECTION, 1 item
#   validate_human_answers  -> ok=False, BLOCKED
#   hh_submit_policy gate 5 -> checks_failed += "questionnaire_complete"
#
# The audit verdict is not decorative: hh_application_runner.py reads
# overall == "SAFE_TO_SUBMIT" straight back as RunnerPreCheckStatus.PASS. The
# reader now reports the decode failure in questions_error / answers_error and
# all three gates refuse on it. The corrupt-answers case is pinned as well: it
# was already fail-closed, and it must stay that way.

_READABLE_QUESTIONS = [
    {
        "question_id": "q1",
        "text": "How many years of Python experience do you have?",
        "question_type": "text",
        "options": [],
        "required": True,
    }
]


def _insert_questionnaire_row(qid: str, questions_json: str, answers_json: str) -> None:
    """Write a hh_questionnaires row straight to SQLite, bypassing the writer.

    save_hh_questionnaire() only ever receives json.dumps() output, so a
    corrupt column cannot be produced through it - but a row already in the
    database can hold anything, and that is the row the gates read.
    """
    db.init_db()
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO hh_questionnaires (
            questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
            fingerprint, questions_json, answers_json, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            qid,
            f"hh:{qid}",
            f"conv-{qid}",
            "Python Developer",
            "Acme",
            "fp",
            questions_json,
            answers_json,
            "READY_TO_SUBMIT",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )
    conn.commit()
    conn.close()


def _policy_checks_for(qid: str) -> tuple[list[str], list[str], list[str]]:
    """Run hh_submit_policy.evaluate and return its questionnaire gate outcome.

    Reasons are returned as well: the failure *name* alone cannot tell "the gate
    refused because it could not read the questions" from "the gate refuses
    everything now", and the counter-check below depends on the difference.
    """
    from ai_assistant.hh_submit_policy import evaluate

    decision = evaluate(
        {
            "application_id": "app-46",
            "vacancy_stable_id": f"hh:{qid}",
            "questionnaire_id": qid,
            "title": "Python Developer",
            "employer": "Acme",
        }
    )
    passed = [c for c in decision.checks_passed if c.startswith("questionnaire")]
    failed = [c for c in decision.checks_failed if c.startswith("questionnaire")]
    return passed, failed, list(decision.reasons)


def test_the_reader_reports_an_unreadable_questions_column():
    _insert_questionnaire_row("q46-reader-bad", "{not json", "{}")
    _insert_questionnaire_row("q46-reader-ok", json.dumps(_READABLE_QUESTIONS), "{}")

    broken = db.get_hh_questionnaire("q46-reader-bad")
    assert broken["questions"] == []
    assert broken["questions_error"], (
        "an unreadable questions_json must not be reported as an empty question"
        " list with no explanation - that is the whole defect"
    )

    healthy = db.get_hh_questionnaire("q46-reader-ok")
    assert healthy["questions"], "the readable row must still be read"
    assert healthy["questions_error"] is None, (
        "a row that parsed cleanly must not be flagged"
    )


def test_a_genuinely_empty_question_list_is_not_flagged():
    """Counter-check: the guard must not fire on an honestly empty list."""
    _insert_questionnaire_row("q46-empty", "[]", "{}")

    data = db.get_hh_questionnaire("q46-empty")
    assert data["questions"] == []
    assert data["questions_error"] is None, (
        "questions_json = '[]' parsed fine - that is an empty questionnaire,"
        " not an unreadable one"
    )


def test_unreadable_questions_never_audit_as_safe_to_submit():
    from ai_assistant.hh_questionnaire_audit import audit_questionnaire

    _insert_questionnaire_row("q46-audit-bad", "{not json", "{}")
    report = audit_questionnaire("q46-audit-bad")

    assert report.overall.value == "NEEDS_CORRECTION", (
        "an unreadable question list produced SAFE_TO_SUBMIT, which"
        " hh_application_runner reads back as a PASS"
    )
    assert report.items, (
        "the refusal must carry a reason: a report with no items and no verdict"
        " change is how the original defect stayed invisible"
    )
    assert report.items[0].source_of_truth == "hh_questionnaires.questions_json", (
        "the item must point at the unreadable column - otherwise this pin would"
        " also pass if the audit had simply been made to refuse everything"
    )


def test_readable_questions_without_answers_still_need_correction():
    """Counter-check: the control case must keep its original verdict.

    Without this, the pin above would also pass if audit_questionnaire had been
    changed to return NEEDS_CORRECTION unconditionally.
    """
    from ai_assistant.hh_questionnaire_audit import audit_questionnaire

    _insert_questionnaire_row("q46-audit-ctl", json.dumps(_READABLE_QUESTIONS), "{}")
    report = audit_questionnaire("q46-audit-ctl")

    assert report.overall.value == "NEEDS_CORRECTION"
    assert len(report.items) == 1
    assert report.items[0].verdict.value == "REVIEW"
    assert report.items[0].source_of_truth == "Missing", (
        "this is the unanswered-question path; the refusal path uses"
        " source_of_truth='hh_questionnaires.questions_json'"
    )


def test_unreadable_questions_block_answer_validation():
    from ai_assistant.hh_questionnaire import (
        HHQuestionnaire,
        validate_human_answers,
    )

    _insert_questionnaire_row("q46-validate", "{not json", "{}")
    quest = HHQuestionnaire(**db.get_hh_questionnaire("q46-validate"))
    res = validate_human_answers(quest, {})

    assert res.ok is False, (
        "an empty question list made every rule in validate_human_answers pass"
        " vacuously and reported 'All questionnaire requirements satisfied'"
    )
    assert res.status == "NEEDS_HUMAN_REVIEW"
    assert "could not be read" in res.reason


def test_readable_questions_without_answers_still_block_validation():
    """Counter-check for the validation pin."""
    from ai_assistant.hh_questionnaire import (
        HHQuestionnaire,
        validate_human_answers,
    )

    _insert_questionnaire_row("q46-validate-ctl", json.dumps(_READABLE_QUESTIONS), "{}")
    quest = HHQuestionnaire(**db.get_hh_questionnaire("q46-validate-ctl"))
    res = validate_human_answers(quest, {})

    assert res.ok is False
    assert res.status == "BLOCKED"
    assert "q1" in res.reason


def test_unreadable_questions_fail_the_submit_policy_gate():
    _insert_questionnaire_row("q46-policy-bad", "{not json", "{}")
    passed, failed, reasons = _policy_checks_for("q46-policy-bad")

    assert "questionnaire_complete" in failed
    assert "questionnaire_complete" not in passed, (
        "gate 5 recorded a passed check for a questionnaire it never read"
    )
    assert any("questions_unreadable" in r for r in reasons), (
        f"gate 5 must say the questions were unreadable; got {reasons}"
    )


def test_readable_questions_without_answers_fail_the_submit_policy_gate():
    """Counter-check for the gate-5 pin."""
    _insert_questionnaire_row("q46-policy-ctl", json.dumps(_READABLE_QUESTIONS), "{}")
    passed, failed, reasons = _policy_checks_for("q46-policy-ctl")

    assert "questionnaire_complete" in failed
    assert "questionnaire_complete" not in passed
    assert any("missing_answer_for_q1" in r for r in reasons), (
        f"the control must fail for the missing answer, not for a parse error;"
        f" got {reasons}"
    )


def test_unreadable_answers_still_fail_closed():
    """The answers column was already fail-closed - keep it that way.

    Losing the answers makes every question look unanswered, so all three gates
    refused even before this fix. The reader now says why instead of reporting
    an empty dict, but the verdict must not move.
    """
    from ai_assistant.hh_questionnaire_audit import audit_questionnaire

    _insert_questionnaire_row("q46-bada", json.dumps(_READABLE_QUESTIONS), "{not json")

    data = db.get_hh_questionnaire("q46-bada")
    assert data["answers"] == {}
    assert data["answers_error"]

    assert audit_questionnaire("q46-bada").overall.value == "NEEDS_CORRECTION"
    passed, failed, _reasons = _policy_checks_for("q46-bada")
    assert "questionnaire_complete" in failed
    assert "questionnaire_complete" not in passed

def test_a_fully_answered_questionnaire_still_audits_as_safe():
    """Positive control: the audit must still say SAFE when it really is.

    Every other pin here asserts a refusal, and a refusal is also what an audit
    that refuses everything would produce. This one is what makes the others
    mean something: a readable questionnaire with an answer for its single
    free-text question comes out SAFE_TO_SUBMIT, so "made to always refuse" is
    a mutation these tests catch.
    """
    from ai_assistant.hh_questionnaire_audit import audit_questionnaire

    _insert_questionnaire_row(
        "q46-audit-ok",
        json.dumps(
            [
                {
                    "question_id": "q1",
                    "text": "Please describe your motivation for this position",
                    "question_type": "text",
                    "options": [],
                    "required": True,
                }
            ]
        ),
        json.dumps({"q1": "I want to build automation for a living."}),
    )

    report = audit_questionnaire("q46-audit-ok")
    assert report.overall.value == "SAFE_TO_SUBMIT", (
        f"a readable, answered questionnaire must still be safe to submit;"
        f" got {report.overall.value} with items"
        f" {[(i.question_id, i.verdict.value) for i in report.items]}"
    )

# ---------------------------------------------------------------------------
# Finding #47 - db.get_production_health: three probes that printed a crashed
# read as a measured number
# ---------------------------------------------------------------------------
# Block 2 of get_production_health was fixed under finding #4: a duplicate-key
# probe that crashes may not leave health at HEALTHY. Three other probes in the
# same function kept the older shape -- `except Exception: pass` with the metric
# pre-filled to 0 -- so a failed COUNT printed as "Delivery Records Total: 0"
# while health stayed HEALTHY. "Could not read" and "read a zero" are different
# facts, and only one of them is a measurement. The CLI prints these numbers, so
# the fake zero was the whole story the operator saw.

# Unique to block 4: block 2's duplicate probe also counts rows, but its SQL
# starts with "SELECT delivery_key", so this prefix cannot catch it by accident.
_RECORD_COUNTS_SQL = "SELECT COUNT(*) FROM telegram_delivery_records"


class _HealthProbeCursor:
    """Cursor that fails only the statement a test names."""

    def __init__(self, fail_on: str):
        self.fail_on = fail_on

    def execute(self, sql, *args):
        if self.fail_on in sql:
            raise _Boom(f"{self.fail_on} failed")
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)

    def close(self):
        pass


class _HealthProbeConn:
    def __init__(self, fail_on: str):
        self.fail_on = fail_on

    def cursor(self):
        return _HealthProbeCursor(self.fail_on)

    def execute(self, sql, *args):
        # ensure_schema() goes through conn.execute, not conn.cursor()
        return self.cursor().execute(sql, *args)

    def commit(self):
        pass

    def close(self):
        pass


def _health_with_broken_connection(fail_on: str, monkeypatch, storage_dir):
    db.init_db()
    monkeypatch.setattr(
        db, "get_connection", lambda *a, **kw: _HealthProbeConn(fail_on)
    )
    return db.get_production_health(storage_dir=str(storage_dir))


def test_health_is_unknown_when_record_counts_cannot_be_read(monkeypatch, tmp_path):
    res = _health_with_broken_connection(_RECORD_COUNTS_SQL, monkeypatch, tmp_path)

    assert res["health"] != "HEALTHY", (
        "a COUNT that crashed is not a count of zero; health must not stay HEALTHY"
    )
    assert any("record count" in a["message"].lower() for a in res["alerts"]), (
        f"the failed probe has to name itself; alerts were {res['alerts']}"
    )
    assert res["metrics"]["total_delivery_records"] is None, (
        "0 is a measurement; the read failed, so the metric has no value"
    )
    assert res["metrics"]["record_counts_error"], (
        "the reader has to keep the reason, not just drop the number"
    )


def test_record_counts_are_still_numbers_when_the_query_works(monkeypatch, tmp_path):
    """Control: the refusal must not cost the honest path its measurement."""
    db.init_db()
    res = db.get_production_health(storage_dir=str(tmp_path))

    assert res["metrics"]["total_delivery_records"] == 0
    assert isinstance(res["metrics"]["job_digest_records"], int)
    assert isinstance(res["metrics"]["digest_batch_records"], int)
    assert "record_counts_error" not in res["metrics"]
    assert not any("record count" in a["message"].lower() for a in res["alerts"])


def test_health_is_unknown_when_pending_vacancies_cannot_be_listed(
    monkeypatch, tmp_path
):
    db.init_db()

    def _boom(*args, **kwargs):
        raise _Boom("undigested listing failed")

    monkeypatch.setattr(db, "list_undigested_vacancies", _boom)
    res = db.get_production_health(storage_dir=str(tmp_path))

    assert res["health"] != "HEALTHY", (
        "an unreadable pending count is not a pending count of zero"
    )
    assert any("undigested" in a["message"].lower() for a in res["alerts"]), (
        f"alerts were {res['alerts']}"
    )
    assert res["metrics"]["pending_undigested_vacancies_count"] is None


def test_health_is_unknown_when_hermes_status_cannot_be_read(monkeypatch, tmp_path):
    from ai_assistant import hermes_integration

    db.init_db()

    def _boom(*args, **kwargs):
        raise _Boom("hermes status failed")

    monkeypatch.setattr(
        hermes_integration, "get_hermes_integration_status", _boom
    )
    res = db.get_production_health(storage_dir=str(tmp_path))

    assert res["health"] != "HEALTHY", (
        "an unreadable integration check must not read as a healthy integration"
    )
    assert res["hermes_integration"]["status"] == "UNKNOWN", res.get(
        "hermes_integration"
    )
    assert any("hermes" in a["message"].lower() for a in res["alerts"]), (
        f"alerts were {res['alerts']}"
    )


def test_a_readable_hermes_drift_is_still_reported_as_drift(monkeypatch, tmp_path):
    """Control: UNKNOWN is for a check that could not run, not for a bad answer."""
    from ai_assistant import hermes_integration

    db.init_db()
    monkeypatch.setattr(
        hermes_integration,
        "get_hermes_integration_status",
        lambda *a, **kw: {"status": "DRIFTED"},
    )
    res = db.get_production_health(storage_dir=str(tmp_path))

    assert res["hermes_integration"]["status"] == "DRIFTED"
    assert res["health"] == "DEGRADED", (
        "a readable DRIFTED status is a known bad answer, not an unknown one"
    )


def test_an_inconclusive_probe_is_not_downgraded_to_degraded(monkeypatch, tmp_path):
    """'Could not check' must not be rewritten as 'checked, minor problem'.

    Rule B below the probes sets DEGRADED whenever failures are recorded, and it
    used to do that on any health but UNHEALTHY -- so an UNKNOWN raised by the
    duplicate-key probe (finding #4) was silently rewritten into DEGRADED, and
    cli.py then exited 1 instead of 2.
    """
    from ai_assistant.runner import ConsecutiveFailureTracker

    db.init_db()
    ConsecutiveFailureTracker(storage_dir=str(tmp_path)).record_failure("isolated")

    res = _health_with_broken_connection(
        "GROUP BY delivery_key", monkeypatch, tmp_path
    )

    assert res["health"] == "UNKNOWN", (
        f"an inconclusive probe was downgraded to {res['health']}"
    )


# ---------------------------------------------------------------------------
# Finding #49 - measured, not a hole: an unreadable answers column on the
# APPLICATION row closes the gate instead of opening it
# ---------------------------------------------------------------------------
# Finding #46 gave _row_to_questionnaire a questions_error / answers_error marker.
# Its neighbour _row_to_application (db.py:1132) got nothing:
#
#     answers = {}
#     if row[8]:
#         try:
#             answers = json.loads(row[8])
#         except Exception:
#             answers = {}
#
# so "no answers stored" and "answers stored but unreadable" are the same shape.
# A missing marker is only a defect if a consumer treats the two differently, and
# both consumers put the answers in the BODY of a loop over the questionnaire's
# questions - the same asymmetry as #46, where losing the answers closes the gate
# and losing the questions opens it:
#
#     hh_submit_policy.py:309       app.get("answers") or quest.answers or {}
#     hh_application_runner.py:531  quest_data.get("answers") or app.get("answers") or {}
#
# Measured 2026-09-19 with tools/ble001_app_answers_probe.py: unreadable, NULL and
# an empty dict all give the reader {}, gate 5 refuses, the validator refuses with
# the SAME reason (missing_answer_for_q1), and a complete column passes. The
# address therefore closes as "measured, not a hole", and these pins keep it that
# way - a future consumer that reports the answers as a fact would need the marker
# this reader does not have.

_Q49_QID = "q49-answers"
_Q49_QUESTIONS = [
    {
        "question_id": "q1",
        "text": "How many years of Python experience do you have?",
        "question_type": "text",
        "options": [],
        "required": True,
    }
]


def _q49_setup(monkeypatch, tmp_path) -> None:
    """A readable questionnaire with one required question and no stored answers."""
    from ai_assistant import config

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "ble49.db"), raising=False)
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hh_questionnaires (
                questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
                fingerprint, questions_json, answers_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _Q49_QID,
                f"hh:{_Q49_QID}",
                "conv-49",
                "Python Developer",
                "Acme",
                "fp",
                json.dumps(_Q49_QUESTIONS),
                None,
                "READY_TO_SUBMIT",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _q49_application(app_id: str, answers_json) -> dict:
    """Insert an application row with this exact answers_json, then read it back.

    save_hh_application() serialises the column itself, so a corrupt value cannot
    be produced through it - but a row already in the database can hold anything,
    and that is the row the reader reads. What comes back is the reader's own
    output, so these pins measure the real path and not a hand-built app dict.
    """
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hh_applications (
                application_id, vacancy_stable_id, title, employer, state,
                draft, questionnaire_id, answers_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                app_id,
                f"hh:{_Q49_QID}",
                "Python Developer",
                "Acme",
                "READY_TO_SUBMIT",
                "",
                _Q49_QID,
                answers_json,
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db.get_hh_application(app_id)


def test_an_unreadable_answers_column_on_the_application_fails_closed(monkeypatch, tmp_path):
    from ai_assistant.hh_questionnaire import HHQuestionnaire, validate_human_answers
    from ai_assistant.hh_submit_policy import evaluate

    _q49_setup(monkeypatch, tmp_path)
    app = _q49_application("app-49-bad", "{not json")

    assert app["answers"] == {}, "the reader is expected to fall back to an empty dict"
    assert "answers_error" not in app, (
        "this reader has no marker for the unreadable column - if one is added,"
        " replace this pin with one that asserts the marker rather than deleting it"
    )

    decision = evaluate(app)
    assert "questionnaire_complete" in decision.checks_failed, (
        "an unreadable answers column must not look like a satisfied questionnaire"
    )
    assert "questionnaire_complete" not in decision.checks_passed

    quest = HHQuestionnaire(**db.get_hh_questionnaire(_Q49_QID))
    res = validate_human_answers(quest, app["answers"])
    assert res.ok is False
    assert res.status == "BLOCKED"
    assert "q1" in res.reason


def test_a_complete_answers_column_on_the_application_still_passes(monkeypatch, tmp_path):
    """Positive control: without this, "refuses everything" satisfies the pin above."""
    from ai_assistant.hh_questionnaire import HHQuestionnaire, validate_human_answers
    from ai_assistant.hh_submit_policy import evaluate

    _q49_setup(monkeypatch, tmp_path)
    app = _q49_application("app-49-ok", json.dumps({"q1": "5 years"}))

    assert app["answers"] == {"q1": "5 years"}

    decision = evaluate(app)
    assert "questionnaire_complete" in decision.checks_passed
    assert "questionnaire_complete" not in decision.checks_failed

    quest = HHQuestionnaire(**db.get_hh_questionnaire(_Q49_QID))
    assert validate_human_answers(quest, app["answers"]).ok is True


def test_the_questionnaires_own_answers_survive_a_corrupt_application_column(monkeypatch, tmp_path):
    """The other direction: a corrupt application column must not close a gate either.

    Both consumers fall back to the questionnaire's stored answers when the
    application's column yields nothing, so the corrupt column is inert - it can
    neither open a gate nor close one that the questionnaire would open.
    """
    from ai_assistant.hh_submit_policy import evaluate

    _q49_setup(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE hh_questionnaires SET answers_json = ? WHERE questionnaire_id = ?",
            (json.dumps({"q1": "5 years"}), _Q49_QID),
        )
        conn.commit()
    finally:
        conn.close()

    app = _q49_application("app-49-fallback", "{not json")

    assert app["answers"] == {}

    decision = evaluate(app)
    assert "questionnaire_complete" in decision.checks_passed, (
        "the questionnaire's own answers are the source of truth on this path"
    )


# ---------------------------------------------------------------------------
# Finding #50 - an ATTEMPTING batch whose timestamp cannot be read was reported
# as fresh and never tripped the critical rule
# ---------------------------------------------------------------------------
# list_digest_attempts computed the age like this:
#
#     attempted_at_str = r[3] or payload.get("attempted_at")
#     age_seconds = 0
#     try:
#         att_dt = datetime.datetime.fromisoformat(attempted_at_str.replace("Z", "+00:00"))
#         ...
#         age_seconds = max(0, int((now_dt - att_dt).total_seconds()))
#     except Exception:
#         pass
#
#     is_stale = (persisted_status == "ATTEMPTING" and age_seconds >= stale_threshold_seconds)
#
# age_seconds started at 0 and stayed 0 when the timestamp could not be read.
# That is finding #47's class - a failed read printed as a measured zero - but
# here the zero fed a rule rather than a display line. Two consumers:
#
#     cli.py:4029                  printed "Age: 0.0 min" with no [STALE] marker
#     db.get_production_health     counted the row as `attempting`, and Rule A
#                                  raises UNHEALTHY only on stale_cnt > 0
#
# and attempting_cnt is written into metrics but read by no rule at all, so the
# one row an operator most needs to see raised nothing.
#
# Measured 2026-09-19 with tools/ble001_digest_stale_probe.py: before the fix an
# unreadable timestamp gave age 0 / ATTEMPTING / stale_count 0 / no critical
# alert; after it, age is None, the row is STALE and the critical rule fires.
# The first pin holds the fix; the other three hold what the fix must not break.

_Q50_CHAT = "-1004399255305"


def _q50_setup(monkeypatch, tmp_path) -> None:
    from ai_assistant import config

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "ble50.db"), raising=False)
    monkeypatch.setattr(config, "DIGEST_ATTEMPT_STALE_MINUTES", 60, raising=False)
    db.init_db()


def _q50_insert_attempting(delivered_at: str, payload_attempted_at: str | None = None) -> None:
    """One ATTEMPTING digest_batch row, written straight into the table.

    Raw INSERT, not record_digest_attempt: that writer always stamps a valid
    now_iso, so it cannot produce the shape under test. This is the same reason
    the probe writes its rows directly.
    """
    payload: dict = {"vacancies": ["hh:50"], "chat_id": _Q50_CHAT}
    if payload_attempted_at is not None:
        payload["attempted_at"] = payload_attempted_at
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, 'digest_batch', ?, ?, 'ATTEMPTING', ?)
            """,
            (f"digest_batch:q50-{delivered_at!r}", _Q50_CHAT, delivered_at, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


def _q50_now():
    import datetime

    return datetime.datetime(2026, 9, 19, 12, 0, 0, tzinfo=datetime.timezone.utc)


def test_an_unreadable_digest_timestamp_fails_closed(monkeypatch, tmp_path):
    """An un-ageable ATTEMPTING row is STALE and raises the critical rule."""
    _q50_setup(monkeypatch, tmp_path)
    _q50_insert_attempting("{not a date")
    now = _q50_now()

    attempts = db.list_digest_attempts(limit=10, now_dt=now)
    assert len(attempts) == 1
    att = attempts[0]

    assert att["age_seconds"] is None, "a failed read must not become a measured zero"
    assert att["age_minutes"] is None
    assert att["age_unknown"] is True
    assert att["age_error"], "the reason has to survive for whoever reads this"
    assert att["stale"] is True
    assert att["effective_status"] == "STALE"
    assert att["requires_reconciliation"] is True

    health = db.get_production_health(now_dt=now, storage_dir=str(tmp_path))
    assert health["metrics"]["stale_count"] >= 1
    assert health["health"] == "UNHEALTHY"
    criticals = [a["message"] for a in health["alerts"] if a["severity"] == "CRITICAL"]
    assert any("Stale unresolved digest attempts" in m for m in criticals), criticals


def test_a_readable_recent_digest_timestamp_is_still_not_stale(monkeypatch, tmp_path):
    """The other direction: the fix must not mark every ATTEMPTING row stale."""
    _q50_setup(monkeypatch, tmp_path)
    now = _q50_now()
    import datetime

    _q50_insert_attempting((now - datetime.timedelta(minutes=5)).isoformat())

    att = db.list_digest_attempts(limit=10, now_dt=now)[0]
    assert att["age_seconds"] == 300
    # .get(..., False), not ["age_unknown"]: this control has to pass on the OLD
    # code too, or it proves nothing about the fix. Measured - the first version
    # of this pin died with KeyError on the pre-fix code, which reads as "caught
    # the bug" while actually catching a missing key. See the #50 section of
    # docs/ble001_triage.md.
    assert att.get("age_unknown", False) is False
    assert att.get("age_error") is None
    assert att["stale"] is False
    assert att["effective_status"] == "ATTEMPTING"


def test_an_empty_digest_column_still_falls_back_to_the_payload(monkeypatch, tmp_path):
    """An empty column is not an unreadable one - the documented fallback stands."""
    _q50_setup(monkeypatch, tmp_path)
    now = _q50_now()
    import datetime

    three_hours_ago = (now - datetime.timedelta(hours=3)).isoformat()
    _q50_insert_attempting("", three_hours_ago)

    att = db.list_digest_attempts(limit=10, now_dt=now)[0]
    assert att["age_seconds"] == 10800, "the payload timestamp is the second half of the fallback"
    assert att.get("age_unknown", False) is False
    assert att["stale"] is True


def test_the_cli_reports_an_unknown_digest_age_as_unknown(monkeypatch, tmp_path, capsys):
    """The operator line must not print a number the reader could not measure."""
    from ai_assistant.cli import digest_attempts_cmd

    _q50_setup(monkeypatch, tmp_path)
    _q50_insert_attempting("{not a date")

    assert digest_attempts_cmd(action="list", output_json=False) == 0

    out = capsys.readouterr().out
    assert "Age: unknown (read failed)" in out
    assert "0.0 min" not in out, "the old display printed a zero it never measured"
    assert "[STALE]" in out


# ---------------------------------------------------------------------------
# Finding #51 - a write failure was reported as "another worker has it"
# ---------------------------------------------------------------------------
# The first-time branch of record_digest_attempt read
#
#     try:
#         cur.execute("INSERT INTO telegram_delivery_records ...")
#     except Exception:
#         # Unique constraint race lost
#         conn.close()
#         return None
#
# so a UNIQUE violation on delivery_key - the race the comment names - and a
# locked or readonly database all left through the same door. The docstring
# documents None as "the attempt is already ATTEMPTING, DELIVERED, or
# AMBIGUOUS", and the caller prints exactly that and exits "safely"
# (integrations/hermes/job_search_fetcher.py:222-224). Measured 2026-09-19 with
# tools/ble001_digest_writer_probe.py: OperationalError('database is locked') and
# OperationalError('attempt to write a readonly database') both returned None and
# produced the "another worker has it" line. Nothing was sent and no row was left
# behind, so nothing was stuck - the harm is the diagnosis, which hides a
# database that cannot be written.
#
# delivery_key is declared NOT NULL UNIQUE (db_schema.py:466) and every other
# column of that INSERT is supplied, so an IntegrityError there IS the race and
# nothing else. The handler is now that narrow; everything else propagates, as
# the vacancy-level INSERTs twelve lines below have always done.
# The first two pins hold the fix; the last two hold what it must not break.

_Q51_CHAT = "-1004399255305"


class _FailingDigestCursor:
    """A real cursor that refuses the batch INSERT with a chosen error."""

    def __init__(self, real, error):
        self._real = real
        self._error = error

    def execute(self, sql, params=()):
        if (
            self._error is not None
            and sql.lstrip().upper().startswith("INSERT")
            and "telegram_delivery_records" in sql
        ):
            raise self._error
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FailingDigestConn:
    """Stands in for get_connection(); the batch INSERT blows up."""

    def __init__(self, real, error):
        self._real = real
        self._error = error

    def cursor(self):
        return _FailingDigestCursor(self._real.cursor(), self._error)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _q51_setup(monkeypatch, tmp_path) -> None:
    from ai_assistant import config

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "ble51.db"), raising=False)
    db.init_db()


def _q51_force_insert_error(monkeypatch, error) -> None:
    """Hand the writer a connection whose batch INSERT raises `error`.

    A fresh connection per call, exactly as the real get_connection does:
    init_db() closes the connection it borrows, so a shared one would be dead by
    the time the writer asks for its own.
    """
    import sqlite3

    from ai_assistant import config

    def fake():
        return _FailingDigestConn(sqlite3.connect(config.DB_FILE), error)

    monkeypatch.setattr(db, "get_connection", fake)


def test_a_write_failure_is_not_reported_as_another_worker(monkeypatch, tmp_path):
    """A database that cannot be written must not answer "already claimed"."""
    import sqlite3

    _q51_setup(monkeypatch, tmp_path)
    _q51_force_insert_error(
        monkeypatch,
        sqlite3.OperationalError("attempt to write a readonly database"),
    )

    with pytest.raises(sqlite3.OperationalError):
        db.record_digest_attempt(["hh:q51_readonly"], chat_id=_Q51_CHAT)


def test_the_original_reason_survives_the_raise(monkeypatch, tmp_path):
    """The operator must see the real cause, not a substitute message."""
    import sqlite3

    _q51_setup(monkeypatch, tmp_path)
    _q51_force_insert_error(
        monkeypatch, sqlite3.OperationalError("database is locked")
    )

    with pytest.raises(sqlite3.OperationalError) as caught:
        db.record_digest_attempt(["hh:q51_locked"], chat_id=_Q51_CHAT)
    assert "database is locked" in str(caught.value)


def test_the_unique_race_still_returns_none(monkeypatch, tmp_path):
    """Control: the race the docstring documents must keep its None.

    This one passes on the OLD code too - `except Exception` swallowed the
    IntegrityError as well. It is here so the narrowing cannot overshoot and turn
    the real concurrency gate into a crash.
    """
    import sqlite3

    _q51_setup(monkeypatch, tmp_path)
    _q51_force_insert_error(
        monkeypatch,
        sqlite3.IntegrityError(
            "UNIQUE constraint failed: telegram_delivery_records.delivery_key"
        ),
    )

    assert db.record_digest_attempt(["hh:q51_race"], chat_id=_Q51_CHAT) is None


def test_a_healthy_write_still_returns_a_batch_key(monkeypatch, tmp_path):
    """Control: the happy path is untouched by the narrower handler."""
    _q51_setup(monkeypatch, tmp_path)

    key = db.record_digest_attempt(["hh:q51_healthy"], chat_id=_Q51_CHAT)
    assert key, "a first-time attempt on a healthy database must claim the batch"


# ---------------------------------------------------------------------------
# Finding #52 - gate 6 recorded "language_match" for a vacancy it never read
# ---------------------------------------------------------------------------
# Gate 6 builds the text it compares like this:
#
#     vac_desc = ""
#     vac_title = title                    # the APPLICATION's own title
#     vac_row = db.get_vacancy_by_id(vac_stable_id) if vac_stable_id else None
#     if vac_row:
#         try:
#             ... three branches that set vac_desc / vac_title ...
#         except Exception:
#             pass                         # silently, leaving no trace at all
#
#     vac_text = f"{vac_title} {company} {vac_desc}".strip()
#     lang_vac = detect_text_language(vac_text)
#     lang_letter = detect_text_language(letter_text)
#
#     if both named and different:  checks_failed += "language_match"
#     elif one unknown:             checks_passed += "language_match_undetermined"
#     else:                         checks_passed += "language_match"
#
# The triage read this handler and called the direction safe: "vac_desc is empty,
# so the language comes out unknown and the record is honest
# (language_match_undetermined), not 'matched'". That premise is the bug. When
# vac_desc is empty, vac_title is NOT - it falls back to the APPLICATION's own
# title, and an application title names a language far more often than not.
# Measured 2026-09-19 over the 1996 rows in state.db: building the fallback text
# from title + company alone still names a language for 837 of them (41.9%). So
# for two rows in five the record is not "undetermined" but the false "matched" -
# and that record is copied into SubmitApproval and out into the SUBMITTED
# evidence, where it attests a comparison nobody made.
#
# Three doors leave vac_desc empty, and the bare `except` is only the first:
#
#   1. the row read raises        -> the handler swallows it
#   2. the row matches no branch  -> falls out of the if/elif with no error
#   3. there is no row at all     -> an empty vacancy_stable_id skips the lookup
#                                    entirely, and a stale id finds nothing
#
# Door 3 is not hypothetical: 5 of the 10 rows in hh_applications carry an empty
# vacancy_stable_id. Doors 1 and 2 need a row object that is not the 25-column
# tuple get_vacancy_by_id returns today, so they are hardening, not repair.
#
# The fix splits the verdicts the way #46 split them one gate up: a read that was
# ATTEMPTED and failed closes the gate, while a vacancy that was never on record
# only stops claiming the comparison happened.

_Q52_LETTER_RU = (
    "Здравствуйте! Меня заинтересовала эта позиция, и я хотел бы откликнуться. "
    "У меня большой опыт разработки на Python, проектирования асинхронных сервисов, "
    "работы с PostgreSQL, Docker и FastAPI, а также оптимизации тяжёлых запросов "
    "в высоконагруженных системах."
)
_Q52_APP_TITLE = "Разработчик бэкенда на Python, удалённая работа"
_Q52_COMPANY = "Максима Технологии"


def _q52_setup(monkeypatch, tmp_path) -> None:
    from ai_assistant import config

    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "ble52.db"), raising=False)
    db.init_db()


def _q52_app(sid: str) -> dict:
    """An application whose own title is Russian and whose letter is Russian.

    That is what makes the false record reachable: with no vacancy description
    the gate compares the letter against THIS title, they agree, and the old code
    wrote "language_match" - a passed check - for the vacancy it never read.
    """
    return {
        "application_id": "app_q52",
        "vacancy_stable_id": sid,
        "title": _Q52_APP_TITLE,
        "employer": _Q52_COMPANY,
        "draft": _Q52_LETTER_RU,
    }


def _q52_insert_vacancy(description: str, sid: str = "hh:52") -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO vacancies
                (stable_id, source, source_job_id, title, company, description,
                 job_url, state)
            VALUES (?, 'hh', ?, ?, ?, ?, ?, 'NEW')
            """,
            (sid, sid, _Q52_APP_TITLE, _Q52_COMPANY, description,
             f"https://hh.ru/vacancy/{sid}"),
        )
        conn.commit()
    finally:
        conn.close()


def _q52_verdict(app: dict):
    from ai_assistant.hh_submit_policy import evaluate

    return evaluate(app)


class _Q52UnindexableMapping:
    """Has .keys(), so gate 6 takes the mapping branch - and refuses to be indexed.

    This is door 1: the three readers raise, and the exception used to vanish into
    `except Exception: pass`.
    """

    def keys(self):
        return ["description", "title"]

    def __getitem__(self, key):
        raise TypeError(f"{key!r} cannot be read")


def test_a_vacancy_row_that_cannot_be_read_does_not_claim_a_language_match(
    monkeypatch, tmp_path
):
    """Door 1: the row is there, the read raises, and the gate must close.

    Before the fix the handler swallowed this, vac_desc stayed empty, and the gate
    recorded "language_match" - a PASSED check - because the fallback title is the
    application's own Russian title and the letter is Russian too.
    """
    _q52_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db, "get_vacancy_by_id", lambda sid: _Q52UnindexableMapping(), raising=False
    )

    decision = _q52_verdict(_q52_app("hh:52"))

    assert "language_match" not in decision.checks_passed, (
        "the gate recorded a language comparison it never made"
    )
    assert "language_match" in decision.checks_failed
    assert any("vacancy_row_unreadable" in r for r in decision.reasons), decision.reasons
    assert any("TypeError" in r for r in decision.reasons), (
        f"the swallowed exception has to survive as text; got {decision.reasons}"
    )


def test_a_vacancy_row_of_an_unknown_shape_does_not_claim_a_language_match(
    monkeypatch, tmp_path
):
    """Door 2: nothing raised, no branch matched, and the gate must still close.

    A five-column row - what a narrowed SELECT would return - falls through all
    three branches without an error, so no handler ever sees it.
    """
    _q52_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db,
        "get_vacancy_by_id",
        lambda sid: ("hh:52", "hh", "52", _Q52_APP_TITLE, _Q52_COMPANY),
        raising=False,
    )

    decision = _q52_verdict(_q52_app("hh:52"))

    assert "language_match" not in decision.checks_passed
    assert "language_match" in decision.checks_failed
    assert any("unrecognised row shape" in r for r in decision.reasons), decision.reasons


def test_a_vacancy_that_was_never_read_does_not_claim_a_language_match(
    monkeypatch, tmp_path
):
    """Door 3, first way in: the row is simply not there.

    Not a read failure, so the gate must NOT close - it must only stop claiming
    the comparison happened.
    """
    _q52_setup(monkeypatch, tmp_path)

    decision = _q52_verdict(_q52_app("hh:52"))

    assert "language_match" not in decision.checks_passed
    assert "language_match_without_description" in decision.checks_passed
    assert "language_match" not in decision.checks_failed, (
        "a vacancy that was never on record is not a read failure"
    )


def test_an_empty_vacancy_id_skips_the_lookup_entirely(monkeypatch, tmp_path):
    """Door 3, second way in - the one with a live population.

    5 of the 10 rows in hh_applications carry an empty vacancy_stable_id, so the
    lookup is not even attempted. The stub asserts that, rather than assuming it.
    """
    _q52_setup(monkeypatch, tmp_path)

    def _must_not_be_called(sid):
        raise AssertionError(f"gate 6 looked up {sid!r} for an empty vacancy id")

    monkeypatch.setattr(db, "get_vacancy_by_id", _must_not_be_called, raising=False)

    decision = _q52_verdict(_q52_app(""))

    assert "language_match" not in decision.checks_passed
    assert "language_match_without_description" in decision.checks_passed


def test_a_vacancy_that_was_read_still_records_a_language_match(monkeypatch, tmp_path):
    """Control: the fix must not turn a real comparison into a marker.

    This one passes on the OLD code too. It is here so the new branch cannot
    overshoot and mark every vacancy, read or not.
    """
    _q52_setup(monkeypatch, tmp_path)
    _q52_insert_vacancy(_Q52_LETTER_RU)

    decision = _q52_verdict(_q52_app("hh:52"))

    assert "language_match" in decision.checks_passed
    assert "language_match_without_description" not in decision.checks_passed
    assert "language_match" not in decision.checks_failed


def test_a_vacancy_with_no_description_is_marked_but_does_not_block(
    monkeypatch, tmp_path
):
    """Boundary: the row WAS read - it just carries no description.

    Not a read failure, so it must not close the gate; but the comparison still
    ran against the application's own title, so it must not claim a match either.
    """
    _q52_setup(monkeypatch, tmp_path)
    _q52_insert_vacancy("")

    decision = _q52_verdict(_q52_app("hh:52"))

    assert "language_match_without_description" in decision.checks_passed
    assert "language_match" not in decision.checks_passed
    assert "language_match" not in decision.checks_failed


# ---------------------------------------------------------------------------
# Finding #53: confirm_human_submission() recorded a confirmation for a
# comparison it never ran.
#
# hh_human_submission.confirm_human_submission() is the explicit human
# confirmation layer. Its docstring said it "Checks the same 11 gates as
# preflight plus the approval state", and hh_controlled_submit calls it as
# step 1 before anything else is allowed to happen. It never called a gate
# checker. It checked three things: the fingerprint, the approval state, and
# that the vacancy being confirmed is the one the review approved.
#
# The third check was:
#
#     gate_vacancy = ""
#     try:
#         gate_data = entry.get("gate") or {}
#         gate_vacancy = gate_data.get("vacancy_stable_id") or ""
#     except Exception:
#         gate_vacancy = ""
#     if gate_vacancy and gate_vacancy != vacancy_stable_id:
#         return {"ok": False, ...}
#
# so any "gate" that is not a non-empty mapping skipped the comparison and the
# function went on to record {"ok": True, "reason": "human confirmation
# recorded"} - a passed check for a comparison that never happened. That is
# finding #52's harm in a different module: nothing bad gets sent because of
# it, but a green record is minted.
#
# The report exonerated this site with two claims, and both are wrong:
#   * it cited hh_submission.py:259 - which is not this handler. The sibling
#     copy lives at hh_submission.py:341 and had already been fixed by #32.
#   * it argued "до неё уже прошёл gate_res" - that check is inside
#     execute_hh_submission(), a different function in a different module.
#     confirm_human_submission() has no gate_res in front of it.
#
# Reachability, measured and stated honestly rather than assumed:
#   * HumanReviewStore is the only store. It is in-memory and its save() writes
#     gate.model_dump(), which is always a dict. Nothing in the repo writes a
#     non-mapping into entry["gate"] today.
#   * The click is ALSO masked downstream: controlled_real_submit() refuses
#     unless hh_submission.preflight_submission() returns READY_TO_SUBMIT, and
#     that path carries #32's fail-closed branch. So this is not a hole that
#     clicks.
#   * What leaks is the record, and the docstring's claim. It is fixed on the
#     anomaly rule #32 already established for the sibling copy, so the two
#     copies cannot drift apart - which is exactly what finding #44 was about.
#
# The pin that carries the finding is the FALSY one. "" / [] / 0 are coerced to
# {} by `or {}` without raising, so the except never fires: narrowing it - the
# fix #52 applied to this same shape - does not close that door. The fix has to
# be structural, and these pins hold the structure in place.
# ---------------------------------------------------------------------------

_Q53_FP = "c" * 64
_Q53_APPROVED = "hh:53"
_Q53_WRONG = "hh:53-not-the-approved-one"


@pytest.fixture
def _q53_clean():
    """The confirmations dict is module state, so it outlives a test."""
    hcs.clear_human_confirmations()
    yield
    hcs.clear_human_confirmations()


def _q53_approved_store():
    """A real store holding one really-approved review for _Q53_APPROVED.

    Built through the real state machine rather than by hand: if the store ever
    stops producing this entry shape, these pins should stop being about
    finding #53 and start failing.
    """
    gate = HumanReviewGate(
        review_id="review_q53",
        status=GateStatus.READY_FOR_HUMAN_REVIEW,
        fingerprint=_Q53_FP,
        vacancy_stable_id=_Q53_APPROVED,
    )
    store = HumanReviewStore()
    rid = store.save(gate)
    store.mark_waiting_for_human(rid)
    approved = store.approve_review(rid, _Q53_FP)
    assert approved["ok"] is True, approved
    return store, rid


def _q53_confirm(store, rid, vacancy):
    return hcs.confirm_human_submission(store, rid, _Q53_FP, vacancy)


def test_a_gate_record_that_is_not_a_mapping_does_not_confirm(_q53_clean):
    """Door 1: "gate" holds something that is not a mapping at all.

    Truthy, so `or {}` keeps it and the .get() raises. On the old code the
    except swallowed that and the mismatched vacancy was confirmed anyway.
    """
    store, rid = _q53_approved_store()
    store.get(rid)["gate"] = "approved"

    result = _q53_confirm(store, rid, _Q53_WRONG)

    assert result["ok"] is False, "a gate record nobody could read confirmed a vacancy"
    assert "unreadable" in result["reason"]


def test_a_falsy_gate_record_does_not_confirm(_q53_clean):
    """Door 2: the one a narrowed `except` cannot close.

    "" / [] / 0 are falsy, so `entry.get("gate") or {}` turns them into {} and
    the .get() never raises - the except is not involved at all. Same mismatch
    as door 1, same answer has to come out of it.
    """
    for value in ("", [], 0):
        store, rid = _q53_approved_store()
        store.get(rid)["gate"] = value

        result = _q53_confirm(store, rid, _Q53_WRONG)

        assert result["ok"] is False, f"gate={value!r} confirmed the wrong vacancy"
        assert "unreadable" in result["reason"]


def test_an_unreadable_gate_record_records_no_confirmation(_q53_clean):
    """The actual harm: a confirmation written for a comparison that never ran.

    The return value is only half of it. The confirmation goes into module
    state and is consumed later by submit_with_human_confirmation(), so the
    record itself has to be absent - not merely the reason text different.
    """
    store, rid = _q53_approved_store()
    store.get(rid)["gate"] = "approved"

    _q53_confirm(store, rid, _Q53_WRONG)

    assert rid not in hcs._human_confirmations, "a refused confirmation was still recorded"


def test_a_matching_vacancy_is_still_confirmed(_q53_clean):
    """Control: a real match must still confirm. Passes on the old code too."""
    store, rid = _q53_approved_store()

    result = _q53_confirm(store, rid, _Q53_APPROVED)

    assert result["ok"] is True, result
    assert hcs._human_confirmations[rid] == (_Q53_FP, _Q53_APPROVED)


def test_a_mismatched_vacancy_is_still_refused(_q53_clean):
    """Control: the half of the check that was never broken must keep working.

    Without this pin "refuse everything unreadable" would look green while the
    readable mismatch case quietly regressed. Passes on the old code too.
    """
    store, rid = _q53_approved_store()
    store.get(rid)["gate"] = {"vacancy_stable_id": _Q53_APPROVED}

    result = _q53_confirm(store, rid, _Q53_WRONG)

    assert result["ok"] is False
    assert "vacancy mismatch" in result["reason"]


@pytest.mark.parametrize(
    "gate_value",
    [None, {}, {"vacancy_stable_id": ""}],
    ids=["absent", "empty-dict", "blank-vacancy"],
)
def test_a_review_with_no_vacancy_on_record_still_confirms(_q53_clean, gate_value):
    """Boundary: no vacancy on record means the comparison is not made.

    Three shapes reach it, and they are not equally defensible:

    * `None` - finding #32's stated rule ("reviews written before gate data
      existed"). Not reachable through the current store, which always writes a
      "gate" key, so this half is a specification pin.
    * `{}` and `{"vacancy_stable_id": ""}` - the same skip, inherited from #32
      verbatim, but REACHABLE: HumanReviewGate.vacancy_stable_id defaults to
      "", so a review built from a package and a form that both lack a vacancy
      lands exactly here.

    This pin holds the boundary where it is; it does not bless it. The
    reachable half is finding #54, measured and deliberately left open -
    closing it changes #32's copy too, and that is a decision rather than a
    tidy-up. What the pin does guarantee is that "no vacancy on record" and
    "record that cannot be read" cannot be collapsed into one branch by a later
    edit, because that collapse is what finding #53 was.
    """
    store, rid = _q53_approved_store()
    store.get(rid)["gate"] = gate_value

    result = _q53_confirm(store, rid, _Q53_WRONG)

    assert result["ok"] is True, result


def test_confirm_human_submission_cannot_run_the_gates_it_claimed(_q53_clean):
    """The false claim, pinned by the fact that contradicted it.

    The docstring said the function "Checks the same 11 gates as preflight".
    hh_human_submission imports exactly three names from hh_submission -
    SubmissionReport, SubmissionStatus, clear_submitted_reviews - and no gate
    runner is among them, so that was not an imprecise sentence: it described
    something the module had no way to do. The pin asserts the structural fact.

    The prose itself is deliberately NOT pinned. Two drafts of this test did,
    and both failed against their own fix - the first on the bare phrase "11
    gates", the second because the corrected docstring quoted the old sentence
    in order to explain it. A target that breaks twice on its own correction is
    the wrong target; the claim cannot come back without the code that would
    make it true, and that is what is checked here.
    """
    source = Path(hcs.__file__).read_text(encoding="utf-8")
    assert "check_all_gates" not in source
    assert "check_readonly_gates" not in source


# ===========================================================================
# BLE001 finding #55 - the recovery report called a damaged record "missing"
# ===========================================================================
#
# db.get_verification() answers None in two different situations: there is no
# verification row for this submission, and there is a row that
# SubmissionVerification refuses to parse. inspect_submission_state() could not
# tell them apart, so a submission whose stored record says VERIFIED was
# reported as "Submission exists but no verification performed" - with the
# advice to go and re-check the result on the website.
#
# The row that reaches this state today is the one an older schema wrote: the
# model sets extra="forbid", so a field that has since been removed from
# SubmissionVerification makes every historical row unreadable at once. The
# auditor already knows this - application_integrity._check_verification_json_records
# reads the column by SQL and raises INVALID_VERIFICATION_JSON for the same row
# (its docstring names this exact failure mode as the precedent for finding
# #33). Only the recovery reader was left treating "cannot read" as "is not
# there".
#
# The pins below are the two halves that must both hold: the report must stop
# claiming the record is absent, and it must keep refusing to act on it.

_Q55_SID = "hh:55"
_Q55_SUB = "sub_55"


def _q55_payload(status: str = "VERIFIED") -> dict:
    """A row the current model parses without complaint."""
    from ai_assistant.submission_verifier import VERIFICATION_VERSION

    return {
        "vacancy_stable_id": _Q55_SID,
        "submission_id": _Q55_SUB,
        "verification_status": status,
        "evidence": {"body_marker": True},
        "final_url": "https://hh.ru/applicant/negotiations",
        "page_title": "Отклик отправлен",
        "verified_at": "2026-09-20T00:00:00",
        "warnings": [],
        "verification_version": VERIFICATION_VERSION,
    }


def _q55_unreadable_shapes() -> list[tuple[str, str | None]]:
    """Every stored form the current model refuses, with the reason it refuses.

    All of them are written by SQL, not by save_verification(): the point is a
    row from another version of the schema, which today's writer cannot produce.
    """
    valid = _q55_payload()
    return [
        ("extra-key", json.dumps({**valid, "screenshot_sha256": "deadbeef"})),
        ("dropped-required", json.dumps({k: v for k, v in valid.items() if k != "verification_status"})),
        ("empty-object", "{}"),
        ("null-column", None),
        ("empty-string", ""),
        ("broken-json", '{"verification_status": "VERIFIED"'),
        ("unknown-enum", json.dumps(_q55_payload(status="ACCEPTED"))),
    ]


def _q55_write_verification(raw: str | None) -> None:
    """Put exactly `raw` in the verification column, replacing whatever was there."""
    import sqlite3

    from ai_assistant import config
    from ai_assistant.submission_verifier import VERIFICATION_VERSION

    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM submission_verifications")
        conn.execute(
            """
            INSERT INTO submission_verifications
                (vacancy_stable_id, submission_id, verification_version,
                 verification_status, verification_json, verified_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _Q55_SID, _Q55_SUB, VERIFICATION_VERSION, "VERIFIED", raw,
                "2026-09-20T00:00:00", "2026-09-20T00:00:00", "2026-09-20T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _q55_clear_verifications() -> None:
    """Leave the fixture with no verification row at all - the true "missing" case.

    Distinct from a NULL column: that is a row which exists and says nothing,
    and the two must not be reported the same way.
    """
    import sqlite3

    from ai_assistant import config

    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute(
            "DELETE FROM submission_verifications WHERE vacancy_stable_id=?",
            (_Q55_SID,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def _q55_submitted():
    """A vacancy sitting in SUBMITTED with a submission record and no verification row.

    SUBMITTED is the state that makes the report reach for the verification
    record at all - the branch that produced the false sentence.
    """
    from ai_assistant.application_tracking import (
        ApplicationStatus,
        set_application_status,
        transition_application,
    )
    from ai_assistant.schema import Vacancy

    db.init_db()
    db.save_vacancy(Vacancy(
        source="hh", source_job_id="55", title="Старший разработчик Python",
        company="TechCo",
        description="Python, PostgreSQL, FastAPI.",
        job_url="https://hh.ru/vacancy/55", location="Remote",
    ))
    set_application_status(
        _Q55_SID, ApplicationStatus.DISCOVERED, company="TechCo",
        title="Старший разработчик Python", source="hh",
        vacancy_url="https://hh.ru/vacancy/55",
    )
    transition_application(_Q55_SID, ApplicationStatus.ANALYZED)
    transition_application(_Q55_SID, ApplicationStatus.READY_TO_APPLY)
    transition_application(_Q55_SID, ApplicationStatus.SUBMITTED)
    db.save_submission(
        _Q55_SID,
        json.dumps({"status": "SUBMITTED", "url_after": "https://hh.ru/vacancy/55"}),
        status="SUBMITTED",
        submission_id=_Q55_SUB,
    )
    return _Q55_SID


@pytest.mark.parametrize("shape,raw", _q55_unreadable_shapes(), ids=lambda v: v if isinstance(v, str) and len(v) < 20 else "")
def test_an_unreadable_verification_record_is_not_reported_as_missing(_q55_submitted, shape, raw):
    """The false sentence, from every direction the model can fail.

    "no verification performed" is a statement about the world. The row is
    there; it is the reader that failed. Saying otherwise sends a human to
    re-check a submission that was already verified.
    """
    from ai_assistant.submission_recovery import inspect_submission_state

    _q55_write_verification(raw)
    result = inspect_submission_state(_q55_submitted)

    haystack = " | ".join([result.reason, *result.warnings])
    assert "no verification performed" not in haystack.lower(), (shape, haystack)
    assert "no verification" not in haystack.lower(), (shape, haystack)
    # ...and it must say what actually happened instead of staying silent.
    assert "cannot be read" in result.reason.lower(), (shape, result.reason)


@pytest.mark.parametrize("shape,raw", _q55_unreadable_shapes(), ids=lambda v: v if isinstance(v, str) and len(v) < 20 else "")
def test_an_unreadable_verification_record_does_not_advise_rechecking_the_site(_q55_submitted, shape, raw):
    """The advice, not just the sentence.

    "Run verification to check submission result on the website" is an action
    for a submission with no verification. Pointing it at a damaged record
    invites a second look at a live vacancy to learn what the database already
    knew.
    """
    from ai_assistant.submission_recovery import inspect_submission_state

    _q55_write_verification(raw)
    result = inspect_submission_state(_q55_submitted)

    action = result.recommended_action.lower()
    assert "run verification" not in action, (shape, result.recommended_action)
    assert "on the website" not in action, (shape, result.recommended_action)


def test_a_missing_verification_record_is_still_reported_as_missing(_q55_submitted):
    """The true half. A submission with no verification row must keep saying so.

    Without this the fix could be satisfied by never using the sentence at all,
    which would hide the genuinely unverified submission instead.
    """
    from ai_assistant.submission_recovery import (
        RecoveryStatus,
        inspect_submission_state,
    )

    _q55_clear_verifications()

    result = inspect_submission_state(_q55_submitted)

    assert result.recovery_status == RecoveryStatus.NEEDS_VERIFICATION
    assert "no verification performed" in result.reason.lower(), result.reason
    assert "no verification performed" in " ".join(result.warnings).lower()


def test_a_readable_verification_record_is_still_reported_as_verified(_q55_submitted):
    """The control: nothing about the fix may disturb the working path."""
    from ai_assistant.submission_recovery import (
        RecoveryStatus,
        inspect_submission_state,
    )

    _q55_write_verification(json.dumps(_q55_payload()))
    result = inspect_submission_state(_q55_submitted)

    assert result.recovery_status == RecoveryStatus.NO_ACTION, result.reason
    assert result.last_verification is not None
    assert result.last_verification["verification_status"] == "VERIFIED"
    assert not result.warnings


@pytest.mark.parametrize("shape,raw", _q55_unreadable_shapes(), ids=lambda v: v if isinstance(v, str) and len(v) < 20 else "")
def test_an_unreadable_verification_record_still_fails_reconcile_closed(_q55_submitted, shape, raw):
    """The direction that must not move.

    reconcile_submission_state() is the only place a verification record can
    move the lifecycle to APPLIED, and it is gated on last_verification. A
    record nobody can read must not be able to open that gate - the old code
    got this right by accident (None is falsy) and the fix must keep it.
    """
    from ai_assistant.application_tracking import (
        ApplicationStatus,
        get_application_status,
    )
    from ai_assistant.submission_recovery import reconcile_submission_state

    _q55_write_verification(raw)
    reconcile_submission_state(_q55_submitted)

    track = get_application_status(_q55_submitted)
    assert track.status != ApplicationStatus.APPLIED, (shape, track.status)


@pytest.mark.parametrize("shape,raw", _q55_unreadable_shapes(), ids=lambda v: v if isinstance(v, str) and len(v) < 20 else "")
def test_the_audit_and_the_recovery_report_agree_that_the_record_is_damaged(_q55_submitted, shape, raw):
    """The two readers must not contradict each other.

    The auditor reads the column by SQL and raises INVALID_VERIFICATION_JSON for
    exactly these rows. If the recovery report kept calling the record missing,
    the project would be telling a human two different stories about one row.
    """
    from ai_assistant.application_integrity import run_integrity_audit
    from ai_assistant.submission_recovery import inspect_submission_state

    _q55_write_verification(raw)
    report = run_integrity_audit(scope="full")
    codes = {i.code for i in report.issues if i.vacancy_stable_id == _q55_submitted}

    assert "INVALID_VERIFICATION_JSON" in codes, (shape, codes)
    result = inspect_submission_state(_q55_submitted)
    assert "cannot be read" in result.reason.lower(), (shape, result.reason)

