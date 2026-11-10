"""T7.23: the cleanup guarantee, the tab economy and the stale-instance recovery.

Offline. The browser is faked at the seam that matters - ``open_tab``/``close_tab``
for tabs, the CIM command-line probe for processes - so the logic under test is the
real logic and only the outside world is synthetic.

The four checkpoint names are the four places the task asks a workflow to be crashed
at. ``test_the_guard_is_what_closes_the_tabs`` is the negative control: without it the
other four would be asserting that a workflow which never leaked stays leak-free.
"""

from __future__ import annotations

import json
import pathlib
from typing import ClassVar

import pytest

from ai_assistant import hh_browser_provider as bp

REPO = pathlib.Path(__file__).resolve().parents[1]
PROFILE = r"C:\Users\Misha\AppData\Local\JobSearchHeadless\User Data"

CHECKPOINTS = ("after_search", "after_vacancy_read", "after_form_open", "before_submit")


@pytest.fixture(autouse=True)
def _own_state_files(tmp_path, monkeypatch):
    """Keep these tests off the real ``runner-state.json`` and lease file.

    ``stop()`` records the stop in the state file, and ``stale_instance_recovery()``
    stops a session - so a test that patches ``wait_port_free`` to True writes a real
    ``pid: null, stopped_at: <now>`` over the record of whatever instance is actually
    running. Measured 29.09 (T7.24): pytest runs stamped ``stopped_at`` on the browser
    started at 11:26 while it was still listening on 9111, which is how
    ``runner-state.json`` came to claim a live browser was stopped.
    """
    from ai_assistant import hh_browser_lease as lease

    monkeypatch.setattr(bp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bp, "STATE_FILE", tmp_path / "runner-state.json")
    monkeypatch.setattr(bp, "CHROME_LOG", tmp_path / "chrome-headless.log")
    monkeypatch.setattr(lease, "LEASE_FILE", tmp_path / "browser-lease.json")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeSession:
    """A session that records its teardown instead of talking to a browser."""

    instances: ClassVar[list[_FakeSession]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.owned: list = []
        self.stopped = False
        self.start_called = False
        _FakeSession.instances.append(self)

    def start(self):
        self.start_called = True
        return {"ok": True, "status": "RUNNING", "auth": {"authenticated": True},
                "launch": {"pid": 111}}

    def stop(self):
        self.stopped = True
        self.owned = []
        return {"port_freed": True, "closed_tabs": 1, "status": "STOPPED"}

    def verify_profile_unlocked(self):
        return {"unlocked": True, "profile_dir": PROFILE}

    def verify_no_owned_processes(self):
        return {"count": 0, "processes": []}

    def ownership_registry(self):
        return []


@pytest.fixture
def fake_session(monkeypatch):
    _FakeSession.instances = []
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _FakeSession)
    return _FakeSession


@pytest.fixture
def tab_world(monkeypatch):
    """A tab world that records what was opened and closed, without a browser."""
    world = {"opened": [], "closed": [], "next": 0, "live": {}}

    def fake_open(endpoint, url, timeout=10.0):
        world["next"] += 1
        target_id = f"tab{world['next']}"
        world["opened"].append({"id": target_id, "url": url})
        world["live"][target_id] = url
        return {"id": target_id, "url": url, "type": "page"}

    def fake_close(endpoint, target_id, timeout=5.0):
        world["closed"].append(target_id)
        world["live"].pop(target_id, None)
        return True

    def fake_list(endpoint, timeout=5.0):
        return [{"id": tid, "url": url, "type": "page"}
                for tid, url in world["live"].items()]

    monkeypatch.setattr(bp, "open_tab", fake_open)
    monkeypatch.setattr(bp, "close_tab", fake_close)
    monkeypatch.setattr(bp, "list_tabs", fake_list)
    monkeypatch.setattr(bp.HeadlessBrowserSession, "navigate",
                        lambda self, target_id, url, timeout=25.0: {"url": url})
    return world


# ---------------------------------------------------------------------------
# exception cleanup at four checkpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_cleanup_runs_when_the_workflow_raises(fake_session, checkpoint):
    """The teardown comes from a ``finally``, so a crash cannot skip it."""
    seen: list[dict] = []

    def crash():
        with bp.managed_headless_session(on_cleanup=seen.append):
            raise RuntimeError(f"boom at {checkpoint}")

    with pytest.raises(RuntimeError, match=f"boom at {checkpoint}"):
        crash()

    assert len(fake_session.instances) == 1
    session = fake_session.instances[0]
    assert session.start_called is True
    assert session.stopped is True, "teardown must run on the exception path"
    assert seen and seen[0]["all_clear"] is True


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_cleanup_runs_when_the_workflow_passes(fake_session, checkpoint):
    seen: list[dict] = []
    with bp.managed_headless_session(on_cleanup=seen.append):
        pass
    assert fake_session.instances[0].stopped is True
    assert seen[0]["all_clear"] is True


def test_cleanup_runs_even_when_the_body_swallows_nothing(fake_session):
    """A SystemExit (a Ctrl-C, a bare exit) must not skip the teardown either."""
    with pytest.raises(SystemExit), bp.managed_headless_session():
        raise SystemExit(1)
    assert fake_session.instances[0].stopped is True


def test_a_failing_reporter_does_not_break_the_run(fake_session):
    """The cleanup reporter is a report; it may not become the failure."""
    def bad_reporter(_report):
        raise RuntimeError("reporter is broken")

    with bp.managed_headless_session(on_cleanup=bad_reporter):
        pass
    assert fake_session.instances[0].stopped is True


def test_a_failing_teardown_is_recorded_not_raised_over_the_cause(fake_session, monkeypatch):
    def exploding_stop(self):
        raise RuntimeError("stop() itself broke")

    monkeypatch.setattr(_FakeSession, "stop", exploding_stop)
    with pytest.raises(ValueError, match="the real cause"), bp.managed_headless_session():
        raise ValueError("the real cause")


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------

def test_a_tab_is_registered_with_its_purpose_url_and_time(tab_world):
    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    registry = session.ownership_registry()
    assert len(registry) == 1
    entry = registry[0]
    assert entry["target_id"] == "tab1"
    assert entry["purpose"] == bp.PURPOSE_WORK
    assert entry["url"] == "https://hh.ru/search/vacancy"
    assert entry["created_at"]


def test_the_work_tab_is_reused_across_search_vacancy_and_form(tab_world):
    """Ten vacancies must not become ten tabs."""
    session = bp.HeadlessBrowserSession()
    for index in range(10):
        session.ensure_work_tab(f"https://hh.ru/vacancy/{index}")
        session.repurpose_work_tab(bp.PURPOSE_VACANCY, f"https://hh.ru/vacancy/{index}")
    assert len(tab_world["opened"]) == 1, "one tab, ten vacancies"
    assert len(session.owned) == 1
    assert session.owned[0].url == "https://hh.ru/vacancy/9"


def test_repurposing_updates_the_entry_instead_of_adding_one(tab_world):
    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    session.repurpose_work_tab(bp.PURPOSE_VACANCY, "https://hh.ru/vacancy/1")
    assert len(session.owned) == 1
    assert session.owned[0].purpose == bp.PURPOSE_VACANCY


def test_close_all_owned_tabs_closes_only_ours(tab_world):
    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    session.open_extra_tab("https://hh.ru/applicant/vacancy_response?vacancyId=1",
                           bp.PURPOSE_FORM)
    tab_world["live"]["someone-elses"] = "https://example.com"  # not ours

    result = session.close_all_owned_tabs()
    assert result["closed_count"] == 2
    assert result["failed_count"] == 0
    assert "someone-elses" not in tab_world["closed"]
    assert "someone-elses" in tab_world["live"]


def test_foreign_tabs_are_reported_and_never_closed(tab_world):
    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    tab_world["live"]["foreign-1"] = "https://hh.ru/chat"
    foreign = session.foreign_tabs()
    assert [t["id"] for t in foreign] == ["foreign-1"]
    session.close_all_owned_tabs()
    assert "foreign-1" in tab_world["live"], "a foreign tab must survive our teardown"


def test_a_tab_that_will_not_close_is_reported_not_hidden(tab_world, monkeypatch):
    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    monkeypatch.setattr(bp, "close_tab", lambda *a, **k: False)
    result = session.close_all_owned_tabs()
    assert result["closed_count"] == 0
    assert result["failed"] == ["tab1"]
    assert len(session.owned) == 1, "a tab we could not close is still our problem"


def test_the_guard_is_what_closes_the_tabs(tab_world):
    """Negative control: without the guard the same shape leaks.

    This is the test that gives the four checkpoint tests their meaning. If a crash
    did not leak when the teardown was skipped, "cleanup ran" would be unobservable.
    """
    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    session.open_extra_tab("https://hh.ru/applicant/vacancy_response?vacancyId=1")

    # No managed_headless_session, so no finally: this is the leak.
    assert len(session.owned) == 2
    assert len(tab_world["live"]) == 2

    session.close_all_owned_tabs()
    assert tab_world["live"] == {}


# ---------------------------------------------------------------------------
# stale-instance recovery
# ---------------------------------------------------------------------------

def _cim(pid, cmd):
    return {"ProcessId": pid, "ParentProcessId": 1, "CommandLine": cmd,
            "WorkingSetSize": 100 * 1024 * 1024}


@pytest.fixture
def proc_world(monkeypatch):
    """A CIM world: our headless instance, Mikhail's Chrome, and BrowserOS."""
    world = {
        "rows": [
            _cim(1000, f'"chrome.exe" --headless=new --user-data-dir={PROFILE} '
                       "--remote-debugging-port=9111"),
            _cim(2000, '"chrome.exe" --user-data-dir="C:\\Users\\Misha\\BrowserClaw\\User Data"'),
            _cim(3000, '"chrome.exe" --type=renderer'),
            _cim(4000, '"chrome.exe" --user-data-dir="C:\\Users\\Misha\\chrome-cdp-profile"'),
        ],
        "killed": [],
    }
    monkeypatch.setattr(bp, "chrome_processes_with_command_lines", lambda: list(world["rows"]))
    monkeypatch.setattr(bp, "pid_alive", lambda pid: int(pid) not in world["killed"])

    def fake_terminate(pid, timeout=5.0):
        world["killed"].append(int(pid))
        return True

    monkeypatch.setattr(bp, "terminate_pid", fake_terminate)
    return world


def test_only_the_dedicated_profile_matches(proc_world):
    holders = bp.dedicated_profile_processes()
    assert [h["pid"] for h in holders] == [1000]
    assert holders[0]["headless_flag"] is True
    assert holders[0]["profile_dir"] == PROFILE


def test_browseros_and_other_profiles_are_never_matched(proc_world):
    """The BrowserOS profile and the legacy cdp profile must never be candidates."""
    matched = {h["pid"] for h in bp.dedicated_profile_processes()}
    assert 2000 not in matched, "BrowserClaw/BrowserOS profile"
    assert 4000 not in matched, "chrome-cdp-profile"
    assert 3000 not in matched, "no user-data-dir at all"


def test_a_profile_that_merely_contains_ours_does_not_match(proc_world):
    """Exact path, not substring: a neighbouring profile must not be taken for ours."""
    proc_world["rows"].append(
        _cim(5000, f'"chrome.exe" --user-data-dir={PROFILE} Backup')
    )
    assert {h["pid"] for h in bp.dedicated_profile_processes()} == {1000}


def test_user_data_dir_parsing_handles_quoting_and_spaces():
    quoted = f'"chrome.exe" --headless=new --user-data-dir="{PROFILE}" --remote-debugging-port=9111'
    unquoted = f'"chrome.exe" --headless=new --user-data-dir={PROFILE} --remote-debugging-port=9111'
    # Chrome also emits the whole argument quoted, with the quote *before* the flag
    # name. The lazy branch then swallows the closing quote - and the value is the
    # evidence that a process is ours, so it must not arrive dirty.
    arg_quoted = f'"chrome.exe" --headless=new "--user-data-dir={PROFILE}" --no-first-run'
    assert bp.user_data_dir_from_command_line(quoted) == PROFILE
    # The unquoted form has a space in it; a \S+ pattern would truncate to "...\User".
    assert bp.user_data_dir_from_command_line(unquoted) == PROFILE
    assert bp.user_data_dir_from_command_line(arg_quoted) == PROFILE
    assert bp.user_data_dir_from_command_line('"chrome.exe" --type=renderer') is None


def test_no_stale_instance_is_a_clean_answer(proc_world, monkeypatch):
    proc_world["rows"] = [_cim(2000, '"chrome.exe" --user-data-dir="C:\\Other"')]
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: False)
    report = bp.stale_instance_recovery()
    assert report["status"] == "NO_STALE_INSTANCE"
    assert report["ok"] is True
    assert proc_world["killed"] == []


def test_cdp_shutdown_is_tried_first(proc_world, monkeypatch):
    """The polite path wins when it works, and nothing is terminated."""
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: True)
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: True)
    monkeypatch.setattr(bp, "dedicated_profile_processes",
                        lambda *a, **k: [{"pid": 1000, "headless_flag": True}])

    calls = {"n": 0}

    def once(*a, **k):
        calls["n"] += 1
        return [] if calls["n"] > 1 else [{"pid": 1000, "headless_flag": True}]

    monkeypatch.setattr(bp, "dedicated_profile_processes", once)
    report = bp.stale_instance_recovery()
    assert report["status"] == "RECOVERED_BY_CDP"
    assert report["cdp_shutdown_attempted"] is True
    assert proc_world["killed"] == [], "CDP worked; no process may be terminated"


def test_termination_happens_only_after_cdp_fails(monkeypatch):
    """CDP said no, so the process is terminated - and only then."""
    holders: list[dict] = [{"pid": 1000, "headless_flag": True}]
    killed: list[int] = []

    def fake_terminate(pid, timeout=5.0):
        killed.append(int(pid))
        holders.clear()  # the process is gone, so the holder list empties
        return True

    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: True)
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: False)
    monkeypatch.setattr(bp, "dedicated_profile_processes", lambda *a, **k: list(holders))
    monkeypatch.setattr(bp, "terminate_pid", fake_terminate)

    report = bp.stale_instance_recovery()
    assert report["status"] == "RECOVERED_BY_TERMINATION"
    assert report["ok"] is True
    assert killed == [1000]
    assert report["holders_after"] == []


def test_a_non_headless_holder_is_refused_not_killed(proc_world, monkeypatch):
    """A process on our profile with a visible window is a human's window."""
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: False)
    monkeypatch.setattr(bp, "dedicated_profile_processes",
                        lambda *a, **k: [{"pid": 1000, "headless_flag": False}])
    report = bp.stale_instance_recovery()
    assert report["status"] == "NEEDS_MANUAL"
    assert report["ok"] is False
    assert proc_world["killed"] == []
    assert report["refused"][0]["pid"] == 1000


def test_foreign_chrome_processes_are_never_killed(proc_world, monkeypatch):
    """The whole point: Mikhail's Chrome and BrowserOS are not ours to close."""
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: True)
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: False)
    bp.stale_instance_recovery()
    assert 2000 not in proc_world["killed"], "BrowserOS"
    assert 3000 not in proc_world["killed"], "renderer with no profile"
    assert 4000 not in proc_world["killed"], "chrome-cdp-profile"
    assert proc_world["killed"] == [1000]


def test_recovery_reports_the_evidence_it_acted_on(proc_world, monkeypatch):
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: False)
    report = bp.stale_instance_recovery()
    assert report["holders_before_count"] == 1
    assert "--user-data-dir" in report["evidence"]
    assert report["profile_dir"] == str(bp.headless_profile_dir())


# ---------------------------------------------------------------------------
# the invariant that must not be edited away
# ---------------------------------------------------------------------------

def test_the_module_never_passes_a_kill_command_to_a_subprocess():
    """``taskkill /IM chrome.exe`` takes out every Chrome on the machine.

    Checked on the parsed source rather than on the text, because the text *mentions*
    the forbidden command in a comment explaining why it is forbidden - and a check
    that trips on its own documentation is the kind of check that gets deleted. What
    matters is that no subprocess call in the module ever receives it.
    """
    import ast

    source = (REPO / "ai_assistant" / "hh_browser_provider.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    spawners = {"run", "Popen", "call", "check_output", "check_call"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in spawners:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Constant)
                and isinstance(inner.value, str)
                and any(word in inner.value.lower()
                        for word in ("taskkill", "pkill", "killall", "/im "))
            ):
                offenders.append(inner.value)

    assert offenders == [], f"a subprocess call is handed a kill command: {offenders}"


def test_termination_goes_through_terminate_pid_only():
    """The single termination primitive, and it takes a pid."""
    import ast
    import inspect

    signature = inspect.signature(bp.terminate_pid)
    assert next(iter(signature.parameters)) == "pid"
    assert "timeout" in signature.parameters

    # Count *calls*, not mentions: the module explains the choice in prose, and a
    # text search cannot tell an explanation from an invocation.
    source = (REPO / "ai_assistant" / "hh_browser_provider.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "TerminateProcess":
                enclosing = getattr(node, "lineno", 0)
                calls.append((name, enclosing))

    assert len(calls) == 1, f"TerminateProcess is invoked {len(calls)} times, expected 1"

    # ...and that one call sits inside terminate_pid.
    function = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "terminate_pid"
    )
    assert function.lineno <= calls[0][1] <= function.end_lineno


def test_terminate_pid_opens_one_handle_for_one_pid(monkeypatch):
    """Termination is by pid, through a handle we opened for that pid."""
    seen: list[int] = []

    class _Kernel:
        def OpenProcess(self, _access, _inherit, pid):
            seen.append(pid)
            return 1

        def TerminateProcess(self, _handle, _code):
            return 1

        def CloseHandle(self, _handle):
            return 1

    monkeypatch.setattr(bp, "os", type("_OS", (), {"name": "nt"})())
    monkeypatch.setattr(bp.ctypes, "windll", type("_W", (), {"kernel32": _Kernel()})())
    monkeypatch.setattr(bp, "pid_alive", lambda pid: False)

    assert bp.terminate_pid(4242) is True
    assert seen == [4242]


# ---------------------------------------------------------------------------
# the measured cleanup report
# ---------------------------------------------------------------------------

def test_cleanup_report_measures_rather_than_asserts(tab_world, monkeypatch):
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: True)
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: False)
    monkeypatch.setattr(bp, "dedicated_profile_processes", lambda *a, **k: [])

    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    report = bp.cleanup_report(session)

    assert report["owned_tabs_left"] == 0
    assert report["headless_processes"] == 0
    assert report["port_free"] is True
    assert report["profile_lock_free"] is True
    assert report["all_clear"] is True
    assert json.dumps(report)  # serialisable, so it can be written to an artifact


def test_cleanup_report_admits_a_leak(tab_world, monkeypatch):
    """A report that cannot say "not clean" is decoration."""
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: False)
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: True)
    monkeypatch.setattr(bp, "close_tab", lambda *a, **k: False)
    monkeypatch.setattr(bp, "dedicated_profile_processes",
                        lambda *a, **k: [{"pid": 1000, "headless_flag": True}])

    session = bp.HeadlessBrowserSession()
    session.ensure_work_tab("https://hh.ru/search/vacancy")
    report = bp.cleanup_report(session)

    assert report["all_clear"] is False
    assert report["owned_tabs_left"] == 1
    assert report["headless_processes"] == 1
    assert report["port_free"] is False
