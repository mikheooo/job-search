"""T7.21: the explicit HH browser mode and the headless launcher.

Everything here is offline. No Chrome is started, no port is opened, no HH page
is read: the launcher's three outside-world calls (``subprocess.Popen``,
``cdp_version``, ``port_is_listening``) and its liveness check are monkeypatched,
and the fake ``Popen`` records the argv so the space in ``User Data`` can be
asserted on the argument list rather than on a string that may have been split.

The live smoke test is separate (``tools/.t721_headless_dryrun.py``); a unit
test that reached hh.ru would be a unit test that fails when the network does.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ai_assistant import hh_browser_provider as bp
from ai_assistant import hh_browser_launcher as hl

REPO = pathlib.Path(__file__).resolve().parents[1]
PROFILE = str(bp.headless_profile_dir())


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    """Keep the launcher's bookkeeping out of the repository.

    Autouse as of T7.24: ``launch_headless`` now registers a **lease** as well as the
    runner state, and it does so on the reuse path too - so a test that fakes a live
    endpoint would otherwise write a real ``browser-lease.json`` naming a browser that
    does not exist.
    """
    from ai_assistant import hh_browser_lease as lease

    state = tmp_path / "runner-state.json"
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bp, "STATE_FILE", state)
    monkeypatch.setattr(bp, "CHROME_LOG", tmp_path / "chrome.log")
    monkeypatch.setattr(lease, "LEASE_FILE", tmp_path / "browser-lease.json")
    monkeypatch.delenv(bp.MODE_ENV, raising=False)
    monkeypatch.delenv(bp.HEADLESS_CDP_ENV, raising=False)
    monkeypatch.delenv(bp.HEADLESS_PROFILE_ENV, raising=False)
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(bp, "find_headless_chrome", lambda: r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    return tmp_path


class _FakeProc:
    def __init__(self, pid: int = 4242, exit_code: int | None = None):
        self.pid = pid
        self.returncode = exit_code

    def poll(self):
        return self.returncode


def _record_popen(monkeypatch, proc=None):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return proc or _FakeProc()

    monkeypatch.setattr(bp.subprocess, "Popen", fake_popen)
    return calls


# ---------------------------------------------------------------------------
# 1. mode selection
# ---------------------------------------------------------------------------

def test_mode_defaults_to_headless_when_nothing_is_configured(sandbox):
    assert bp.browser_mode() == bp.MODE_HEADLESS


@pytest.mark.parametrize("raw,expected", [
    ("headless", bp.MODE_HEADLESS),
    ("HEADLESS", bp.MODE_HEADLESS),
    ("  BrowserOS ", bp.MODE_BROWSEROS),
])
def test_mode_is_read_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv(bp.MODE_ENV, raw)
    assert bp.browser_mode() == expected


def test_an_unknown_mode_is_refused_not_guessed(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "chromium")
    with pytest.raises(bp.UnknownBrowserMode):
        bp.browser_mode()


def test_empty_mode_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "   ")
    assert bp.browser_mode() == bp.MODE_HEADLESS


# ---------------------------------------------------------------------------
# 2. endpoint resolution
# ---------------------------------------------------------------------------

def test_headless_mode_ignores_the_browseros_address(monkeypatch):
    """HH_CDP_URL names BrowserOS. Reading it in headless mode would drag every
    headless workflow back onto the user's own profile - the exact swap the mode
    exists to remove."""
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    monkeypatch.setenv("HH_CDP_URL", "http://127.0.0.1:9110")
    monkeypatch.delenv("CDP_URL", raising=False)
    assert bp.resolve_browser_endpoint() == bp.HEADLESS_CDP_URL


def test_browseros_mode_keeps_the_legacy_endpoint(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "browseros")
    monkeypatch.setenv("HH_CDP_URL", "http://127.0.0.1:9444")
    monkeypatch.delenv("CDP_URL", raising=False)
    assert bp.resolve_browser_endpoint() == "http://127.0.0.1:9444"


def test_an_explicit_endpoint_wins_over_the_mode(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    assert bp.resolve_browser_endpoint("http://127.0.0.1:7777") == "http://127.0.0.1:7777"


def test_the_generic_cdp_pin_wins_over_headless(monkeypatch):
    """An explicit endpoint is a decision, not silence (finding #13's lesson)."""
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    monkeypatch.setenv("CDP_URL", "http://127.0.0.1:9223")
    assert bp.resolve_browser_endpoint() == "http://127.0.0.1:9223"


def test_the_launcher_reexports_the_mode_aware_resolver(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    monkeypatch.delenv("CDP_URL", raising=False)
    assert hl.resolve_browser_endpoint() == bp.HEADLESS_CDP_URL


def test_legacy_resolver_still_answers_its_own_question(monkeypatch):
    """resolve_cdp_url() is untouched: the eight pins in test_ble001_fail_closed
    are about *it*, and the mode lives one level above."""
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    monkeypatch.setenv("CDP_URL", "http://127.0.0.1:9223")
    assert hl.resolve_cdp_url() == "http://127.0.0.1:9223"


# ---------------------------------------------------------------------------
# 3. argv
# ---------------------------------------------------------------------------

def test_argv_is_a_list_and_keeps_the_profile_path_together(sandbox):
    """The measured T7.20 failure: `--user-data-dir=...\\User Data` arrived as
    two arguments and a stray `...\\JobSearchHeadless\\User` directory appeared.
    As a list, the whole flag is one element and no shell can split it."""
    argv = bp.headless_argv(r"C:\chrome.exe")

    assert isinstance(argv, list)
    profile_args = [a for a in argv if a.startswith("--user-data-dir=")]
    assert profile_args == [f"--user-data-dir={PROFILE}"], profile_args
    assert "User Data" in profile_args[0]
    assert not any(a == "Data" for a in argv), argv


def test_argv_carries_the_required_headless_flags(sandbox):
    argv = bp.headless_argv(r"C:\chrome.exe")
    for flag in ("--headless=new", "--remote-debugging-address=127.0.0.1",
                 f"--remote-debugging-port={bp.HEADLESS_CDP_PORT}",
                 "--no-first-run", "--no-default-browser-check",
                 f"--window-size={bp.HEADLESS_WINDOW_SIZE}"):
        assert flag in argv, f"{flag} missing from {argv}"
    assert argv[0] == r"C:\chrome.exe"
    assert argv[-1] == "about:blank"


def test_argv_follows_the_endpoint_port(sandbox):
    argv = bp.headless_argv(r"C:\chrome.exe", cdp_url="http://127.0.0.1:9333")
    assert "--remote-debugging-port=9333" in argv


def test_argv_never_weakens_the_sandbox(sandbox):
    argv = bp.headless_argv(r"C:\chrome.exe")
    assert "--no-sandbox" not in argv
    assert "--disable-web-security" not in argv


# ---------------------------------------------------------------------------
# 4/5. refusals: foreign browser on the port, profile already held
# ---------------------------------------------------------------------------

def test_a_foreign_browser_on_our_port_is_refused(sandbox, monkeypatch):
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: {"Browser": "Chrome/153.0"})
    calls = _record_popen(monkeypatch)

    res = bp.launch_headless()

    assert res["ok"] is False
    assert "not a headless Chrome" in res["error"]
    assert calls == [], "must not spawn anything over a browser we do not own"


def test_our_own_instance_is_reused_not_duplicated(sandbox, monkeypatch):
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: {
        "Browser": "HeadlessChrome/153.0.8010.53", "webSocketDebuggerUrl": "ws://x"})
    calls = _record_popen(monkeypatch)

    res = bp.launch_headless()

    assert res["ok"] is True and res["reused"] is True
    assert calls == [], "a second instance would fight over the same profile"


def test_a_profile_held_by_a_live_instance_is_refused(sandbox, monkeypatch):
    bp.write_state({"pid": 999, "cdp_url": bp.HEADLESS_CDP_URL})
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: None)
    monkeypatch.setattr(bp, "pid_alive", lambda pid: pid == 999)
    calls = _record_popen(monkeypatch)

    res = bp.launch_headless()

    assert res["ok"] is False
    assert "already held by a live instance" in res["error"]
    assert calls == []


def test_profile_in_use_also_reads_the_port(sandbox, monkeypatch):
    monkeypatch.setattr(bp, "read_state", lambda: {})
    monkeypatch.setattr(bp, "port_is_listening", lambda port, **k: port == 9111)
    assert bp.profile_in_use() is True


def test_a_missing_chrome_is_reported(sandbox, monkeypatch):
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: None)
    monkeypatch.setattr(bp, "profile_in_use", lambda *a, **k: False)
    monkeypatch.setattr(bp, "find_headless_chrome", lambda: None)
    res = bp.launch_headless()
    assert res["ok"] is False and "CHROME_PATH" in res["error"]


# ---------------------------------------------------------------------------
# 6. launching
# ---------------------------------------------------------------------------

def test_launch_waits_for_cdp_and_records_the_pid(sandbox, monkeypatch):
    seen = {"n": 0}

    def fake_version(endpoint, timeout=2.0):
        seen["n"] += 1
        return {"Browser": "HeadlessChrome/153"} if seen["n"] >= 2 else None

    monkeypatch.setattr(bp, "cdp_version", fake_version)
    monkeypatch.setattr(bp, "profile_in_use", lambda *a, **k: False)
    calls = _record_popen(monkeypatch, _FakeProc(pid=777))

    res = bp.launch_headless(timeout=5.0)

    assert res["ok"] is True and res["started"] is True and res["pid"] == 777
    assert calls, "Chrome was never spawned"
    assert bp.read_state()["pid"] == 777
    assert calls[0]["kwargs"].get("stdin") is not None, "stdio must be detached"


def test_cdp_startup_timeout_fails_closed(sandbox, monkeypatch):
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: None)
    monkeypatch.setattr(bp, "profile_in_use", lambda *a, **k: False)
    _record_popen(monkeypatch, _FakeProc(pid=888))

    res = bp.launch_headless(timeout=0.5)

    assert res["ok"] is False
    assert "did not answer" in res["error"]
    assert res["pid"] == 888


def test_a_chrome_that_dies_immediately_is_reported(sandbox, monkeypatch):
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: None)
    monkeypatch.setattr(bp, "profile_in_use", lambda *a, **k: False)
    _record_popen(monkeypatch, _FakeProc(pid=1, exit_code=21))

    res = bp.launch_headless(timeout=2.0)

    assert res["ok"] is False
    assert "exited with code 21" in res["error"]


# ---------------------------------------------------------------------------
# 7. the authentication gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evidence,authenticated", [
    ({"login_card_visible": True, "applicant_ui_present": False}, False),
    ({"login_input_present": True}, False),
    ({"profile_link_href": "/account/login?role=applicant", "applicant_ui_present": True}, False),
    ({"hhrole": "anonymous", "applicant_ui_present": True}, False),
    ({"applicant_ui_present": True, "h1": "Михаил Колесников"}, True),
    ({}, False),
    ({"error": "Unexpected end of JSON input"}, False),
])
def test_the_auth_decision_reads_evidence(evidence, authenticated):
    got = bp.decide_authentication(evidence)
    assert got["authenticated"] is authenticated, got


def test_an_unreadable_page_is_not_a_pass():
    got = bp.decide_authentication({"error": "boom"})
    assert got["authenticated"] is False and got["verified"] is False


def test_require_hh_auth_returns_the_actionable_status():
    gate = bp.require_hh_auth(lambda js: json.dumps({"login_card_visible": True}))
    assert gate["status"] == bp.HH_AUTH_REQUIRED
    assert gate["message"] == bp.HH_AUTH_REQUIRED_MESSAGE
    assert "JobSearchHeadless" in gate["message"]


def test_require_hh_auth_passes_a_logged_in_page():
    gate = bp.require_hh_auth(lambda js: json.dumps({"applicant_ui_present": True}))
    assert gate["status"] == bp.HH_AUTH_OK and gate["message"] == ""


def test_a_failing_evaluate_is_auth_required_not_a_crash():
    def boom(js):
        raise RuntimeError("no open tab matching 'hh.ru'")

    gate = bp.require_hh_auth(boom)
    assert gate["status"] == bp.HH_AUTH_REQUIRED
    assert gate["auth"]["verified"] is False


# ---------------------------------------------------------------------------
# 8. the session stops itself on HH_AUTH_REQUIRED
# ---------------------------------------------------------------------------

class _FakeSession(bp.HeadlessBrowserSession):
    """The session with the browser and the page replaced by fakes."""

    def __init__(self, launch_ok=True, auth=None):
        super().__init__(require_auth=True)
        self._launch_ok = launch_ok
        self._auth = auth
        self.stopped = False

    def start(self):
        import ai_assistant.hh_browser_provider as provider

        launch = {"ok": self._launch_ok, "started": True, "reused": False,
                  "pid": 5, "error": None if self._launch_ok else "boom"}
        if not self._launch_ok:
            self.status = "LAUNCH_FAILED"
            self.message = "boom"
            return {"ok": False, "status": self.status, "message": self.message,
                    "launch": launch}
        if self._auth is not None:
            gate = provider.decide_authentication(self._auth)
            if not gate["authenticated"]:
                self.stop()
                self.status = provider.HH_AUTH_REQUIRED
                return {"ok": False, "status": provider.HH_AUTH_REQUIRED,
                        "message": provider.HH_AUTH_REQUIRED_MESSAGE, "auth": gate}
            return {"ok": True, "status": "RUNNING", "auth": gate}
        return {"ok": True, "status": "RUNNING", "auth": None}

    def stop(self):
        self.stopped = True
        self.status = "STOPPED"
        return {"closed_tabs": 0, "port": 9111, "port_freed": True, "status": "STOPPED"}


def test_the_session_stops_safely_when_auth_is_required():
    session = _FakeSession(auth={"login_card_visible": True})
    res = session.start()
    assert res["status"] == bp.HH_AUTH_REQUIRED
    assert res["message"] == bp.HH_AUTH_REQUIRED_MESSAGE
    assert session.stopped is True, "an unauthenticated session must not be left running"


def test_the_session_stays_up_when_the_session_is_valid():
    session = _FakeSession(auth={"applicant_ui_present": True})
    res = session.start()
    assert res["ok"] is True and res["status"] == "RUNNING"
    assert session.stopped is False


def test_a_launch_failure_does_not_report_running():
    session = _FakeSession(launch_ok=False)
    res = session.start()
    assert res["ok"] is False and res["status"] == "LAUNCH_FAILED"


# ---------------------------------------------------------------------------
# 9. clean shutdown
# ---------------------------------------------------------------------------

def test_stop_closes_tabs_then_the_browser_and_waits_for_the_port(sandbox, monkeypatch):
    session = bp.HeadlessBrowserSession()
    session.opened_tabs = ["A", "B", "C"]
    closed = []
    monkeypatch.setattr(bp, "close_tab", lambda ep, tid, **k: closed.append(tid) or True)
    monkeypatch.setattr(bp, "quit_browser", lambda ep, **k: True)
    monkeypatch.setattr(bp, "port_is_listening", lambda port, **k: False)
    # T7.23: stop() now measures the real world - it enumerates the processes that
    # hold the dedicated profile. Pin that to "nobody", or a live headless run
    # elsewhere on the machine turns this into STOP_TIMEOUT.
    monkeypatch.setattr(bp, "dedicated_profile_processes", lambda *a, **k: [])

    res = session.stop()

    assert sorted(closed) == ["A", "B", "C"]
    assert res["closed_tabs"] == 3
    assert res["port_freed"] is True and res["status"] == "STOPPED"


def test_a_port_that_stays_busy_is_not_reported_as_freed(sandbox, monkeypatch):
    session = bp.HeadlessBrowserSession()
    monkeypatch.setattr(bp, "close_tab", lambda *a, **k: True)
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: True)
    monkeypatch.setattr(bp, "dedicated_profile_processes", lambda *a, **k: [])

    res = session.stop()

    assert res["port_freed"] is False and res["status"] == "STOP_TIMEOUT"


def test_the_work_tab_is_reused_instead_of_opened_per_read(sandbox, monkeypatch):
    session = bp.HeadlessBrowserSession()
    opened = []

    def fake_open(endpoint, url, **k):
        opened.append(url)
        return {"id": f"tab{len(opened)}", "url": url}

    monkeypatch.setattr(bp, "open_tab", fake_open)
    first = session.ensure_work_tab("https://hh.ru/a")
    session.navigate = lambda *a, **k: {}          # no real navigation
    second = session.ensure_work_tab("https://hh.ru/b")

    assert first == second == "tab1"
    assert len(opened) == 1, "one working tab, not one per read"


# ---------------------------------------------------------------------------
# 10. BrowserOS fallback, and headless not depending on it
# ---------------------------------------------------------------------------

def test_browseros_mode_still_reaches_9110_when_it_is_up(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "browseros")
    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.delenv("HH_CDP_URL", raising=False)
    monkeypatch.setattr(
        hl, "probe_cdp",
        lambda url, timeout=1.5: hl.CDP_ALIVE if url == hl.BROWSEROS_CDP_URL else hl.CDP_DEAD,
    )
    assert bp.resolve_browser_endpoint() == hl.BROWSEROS_CDP_URL


def test_headless_mode_does_not_care_whether_browseros_is_up(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    monkeypatch.delenv("CDP_URL", raising=False)
    monkeypatch.setattr(hl, "probe_cdp", lambda *a, **k: hl.CDP_DEAD)
    assert bp.resolve_browser_endpoint() == bp.HEADLESS_CDP_URL


# ---------------------------------------------------------------------------
# 11. the launcher's mode routing
# ---------------------------------------------------------------------------

def test_ensure_hh_browser_routes_to_headless_without_an_explicit_endpoint(monkeypatch):
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    called = {}
    monkeypatch.setattr(bp, "ensure_headless_browser",
                        lambda **kw: called.update(kw) or {"ok": True, "headless": True})
    res = hl.ensure_hh_browser()
    assert res.get("headless") is True
    assert called.get("cdp_url") is None


def test_ensure_hh_browser_keeps_the_legacy_path_for_an_explicit_endpoint(monkeypatch):
    """An explicit endpoint is a decision; the historical path (and the profile
    it names) stays reachable."""
    monkeypatch.setenv(bp.MODE_ENV, "headless")
    monkeypatch.setattr(hl, "is_cdp_reachable", lambda url, timeout=1.5: True)
    monkeypatch.setattr(hl, "get_cdp_version_info", lambda *a, **k: {"Browser": "Chrome/1"})
    monkeypatch.setattr(hl, "get_cdp_targets",
                        lambda *a, **k: [{"type": "page", "url": "https://hh.ru/chat/1"}])
    res = hl.ensure_hh_browser(cdp_url="http://127.0.0.1:9222")
    assert res["ok"] is True and res["reused"] is True


# ---------------------------------------------------------------------------
# 12. the submit guard is untouched and still closed
# ---------------------------------------------------------------------------

def test_the_provider_cannot_click_or_type():
    """T7.21 moves *reading* onto the headless browser. Nothing here may grow a
    write path: no click, no key event, no form submission."""
    src = (REPO / "ai_assistant" / "hh_browser_provider.py").read_text(encoding="utf-8")
    for forbidden in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent",
                      "Input.insertText", "execute_hh_submission", "form.submit"):
        assert forbidden not in src, f"{forbidden} is a write path and does not belong here"


def test_submit_allowed_false_still_halts_submission(monkeypatch):
    from ai_assistant import config, hh_submission

    monkeypatch.setattr(hh_submission, "_stop_file_paths", lambda: [])
    monkeypatch.setattr(config, "_OPERATOR_SUBMIT_ALLOWED", "false")
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")

    reason = hh_submission.submission_halt_reason()
    assert reason and "SUBMIT_ALLOWED" in reason


def test_submit_allowed_true_does_not_bypass_the_other_stops(monkeypatch, tmp_path):
    from ai_assistant import config, hh_submission

    monkeypatch.setattr(config, "_OPERATOR_SUBMIT_ALLOWED", "true")
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    stop_file = tmp_path / "STOP_SUBMITS"
    stop_file.write_text("stop", encoding="utf-8")
    monkeypatch.setattr(hh_submission, "_stop_file_paths", lambda: [str(stop_file)])
    reason = hh_submission.submission_halt_reason()
    assert reason and "kill switch" in reason


def test_the_dry_run_tool_never_clicks():
    src = (REPO / "tools" / ".t721_headless_dryrun.py").read_text(encoding="utf-8")
    for forbidden in ("dispatchMouseEvent", "dispatchKeyEvent", "insertText",
                      "execute_hh_submission", ".click("):
        assert forbidden not in src, f"the dry run must stay read-only: {forbidden}"
    assert "vacancy_response?vacancyId=" in src, "the form preflight moved; re-measure"


# ---------------------------------------------------------------------------
# T7.22: the auth gate must wait for the document, not judge an empty tab
# ---------------------------------------------------------------------------
# Three separate live failures shared one root cause, so they get one home here:
# the gate reads a page that has not loaded yet, and an unloaded page is
# indistinguishable from a logged-out one. On ``about:blank`` it is worse - the
# read *errors*, because ``document.cookie`` is denied for an opaque origin.

def _page(**over):
    payload = {
        "url": "https://hh.ru/applicant/profile/me", "path": "/applicant/profile/me",
        "login_card_visible": False, "login_input_present": False,
        "applicant_ui_present": True, "profile_link_href": None,
        "h1": "Михаил Колесников", "hhrole": "applicant", "ready": "complete",
    }
    payload.update(over)
    return json.dumps(payload)


def _scripted(reads: list, nav_href: str = bp.AUTH_URL):
    """An ``evaluate_fn`` that answers the three calls ``settle_hh_page`` makes.

    The three are told apart by *identity*, not by substring. ``_AUTH_JS`` mentions
    both ``readyState`` and ``location.href``, so a substring test hands the evidence
    read the readiness payload and the gate then judges a page it never actually read
    -- which is precisely the failure these tests exist to pin.
    """
    seen = {"reads": 0}

    def evaluate(js: str) -> str:
        if js == bp._READY_JS:
            return json.dumps({"href": nav_href, "ready": "complete"})
        if js == bp._AUTH_JS:
            value = reads[min(seen["reads"], len(reads) - 1)]
            seen["reads"] += 1
            return value
        return "navigating"

    return evaluate, seen


def test_settle_hh_page_waits_out_an_unreadable_document():
    """The first read lands on ``about:blank``, where reading cookies throws.

    That error is not "logged out", and it is not a verdict. The helper has to ask
    again. Measured on T7.22's first launch: the gate answered "the page could not
    be read: Failed to read the 'cookie' property" for a session that was fine.
    """
    evaluate, seen = _scripted([
        json.dumps({"error": "Failed to read the 'cookie' property from 'Document'"}),
        _page(),
    ])
    evidence = bp.settle_hh_page(evaluate, attempts=4, delay=0)
    assert seen["reads"] >= 2, "the helper must re-read, not judge the first answer"
    assert evidence is not None
    assert bp.decide_authentication(evidence)["authenticated"] is True


def test_settle_hh_page_returns_the_best_page_it_saw():
    """A readable page with no evidence still beats ``None``: the gate can say why."""
    evaluate, _ = _scripted([
        _page(applicant_ui_present=False, hhrole=None, path="/", url="https://hh.ru/"),
    ])
    evidence = bp.settle_hh_page(evaluate, attempts=2, delay=0)
    assert evidence is not None
    decision = bp.decide_authentication(evidence)
    assert decision["authenticated"] is False
    assert decision["verified"] is False


def test_settle_hh_page_reads_readiness_without_touching_cookies():
    """The pre-load probe must not read ``document.cookie``.

    That is the whole reason it is a separate expression: the readiness check runs
    while the tab is still on ``about:blank``, where a cookie read is denied.
    """
    assert "cookie" not in bp._READY_JS
    assert "readyState" in bp._READY_JS
    assert "cookie" in bp._AUTH_JS, "the evidence read is the one that wants cookies"


def test_settle_hh_page_waits_for_the_document_to_actually_move():
    """``readyState`` alone is a lie - the tab is already complete on ``about:blank``.

    The wait must also require the URL to move, or it reads the old document.
    """
    states = [
        {"href": "about:blank", "ready": "complete"},
        {"href": "about:blank", "ready": "complete"},
        {"href": "https://hh.ru/applicant/profile/me", "ready": "loading"},
        {"href": "https://hh.ru/applicant/profile/me", "ready": "complete"},
    ]
    seen = {"i": 0, "reads": 0}

    def evaluate(js: str) -> str:
        if js == bp._READY_JS:
            value = states[min(seen["i"], len(states) - 1)]
            seen["i"] += 1
            return json.dumps(value)
        if js == bp._AUTH_JS:
            seen["reads"] += 1
            return _page()
        return "navigating"

    evidence = bp.settle_hh_page(evaluate, attempts=2, delay=0)
    assert evidence is not None
    assert seen["i"] > 1, "it must keep probing until the URL moves off about:blank"
    assert bp.decide_authentication(evidence)["authenticated"] is True


def test_ensure_headless_browser_does_not_judge_a_session_from_an_empty_tab(
    sandbox, monkeypatch
):
    """The same wait, through the legacy-shaped entry point.

    ``ensure_headless_browser`` kept the single-read shape after T7.21 fixed the
    session, so it was the *second* place to answer "could not be read" for a
    healthy session.
    """
    monkeypatch.setattr(
        bp, "cdp_version",
        lambda url, timeout=2.0: {"Browser": "HeadlessChrome/153.0.0.0",
                                  "webSocketDebuggerUrl": "ws://127.0.0.1:9111/x"},
    )
    monkeypatch.setattr(
        bp, "list_tabs",
        lambda url, timeout=5.0: [{"id": "T1", "type": "page", "url": "https://hh.ru/chat"}],
    )
    evaluate, seen = _scripted([
        json.dumps({"error": "Failed to read the 'cookie' property from 'Document'"}),
        _page(url="https://hh.ru/chat", path="/chat"),
    ])
    monkeypatch.setattr(bp, "make_target_evaluate", lambda endpoint, target_id: evaluate)

    facts = bp.ensure_headless_browser()
    assert facts["ok"] is True
    assert facts["authenticated"] is True, facts.get("auth")
    assert seen["reads"] >= 2


def test_the_t722_submit_tool_has_a_configurable_resume_and_one_click():
    """The T7.19 submit tool hard-codes the CRM resume and lives on BrowserOS.

    T7.22 needs the resume decided by the vacancy, and the address decided by the
    mode, so the submit path is its own file - and it must still click exactly once.
    """
    src = (REPO / "ai_assistant" / "hh_cdp_submit.py").read_text(encoding="utf-8")
    assert "--expect-resume" in src, "the resume must be an argument, not a constant"
    assert "resolve_browser_endpoint" in src, "the address must come from the mode"
    assert "9110" not in src, "the headless submit path must not name BrowserOS"
    assert src.count("_click_at(") == 1, "one click site only"
    import ast
    tree = ast.parse(src)
    click_node = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_click_at"), None)
    assert click_node is not None, "_click_at not found in headless submit path"
    click_src = ast.get_source_segment(src, click_node) or ""
    assert "for _ in range" not in click_src, (
        "the click itself must not loop"
    )
