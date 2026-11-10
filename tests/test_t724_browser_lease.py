"""T7.24: the ownership contract - a browser always has an owner, and the owner
always gives it back.

Offline. The world is faked at four seams - the lease file, the runner state file,
process liveness (``pid_alive``) and the CDP probes - so the logic under test is the
real logic and only the outside world is synthetic. No browser is started, nothing
is clicked, nothing is sent.

The shape of the guarantee: T7.23 proved the runner cleans up after *itself*. These
tests cover the other half - that every launch path has an owner, that the owner
gives the browser back at the end of an operation, and that a dead launcher is not
by itself a reason to kill anything.
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

from ai_assistant import hh_browser_lease as lease
from ai_assistant import hh_browser_provider as bp

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "ai_assistant"

# The production callers of ``ensure_hh_browser()`` as of T7.24, and how many call
# sites they held: five modules, seven sites. T7.25 §15 emptied it - no production
# module asks for a browser *by name* any more, they all go through ``hh_operation``
# - and it must stay empty. A new entry is a launch path that skipped the ownership
# contract, which is exactly what this pin exists to catch.
KNOWN_BROWSER_CALLERS: set[str] = set()
KNOWN_BROWSER_CALL_SITES = 0


# ---------------------------------------------------------------------------
# the world
# ---------------------------------------------------------------------------

@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A private lease file and runner-state file, plus a controllable outside."""
    state_file = tmp_path / "runner-state.json"
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bp, "STATE_FILE", state_file)
    monkeypatch.setattr(bp, "CHROME_LOG", tmp_path / "chrome.log")
    monkeypatch.setattr(lease, "LEASE_FILE", tmp_path / "browser-lease.json")

    alive: set[int] = set()
    monkeypatch.setattr(bp, "pid_alive", lambda pid: int(pid) in alive)
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: None)
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: False)
    monkeypatch.setattr(bp, "list_tabs", lambda *a, **k: [])
    monkeypatch.setattr(bp, "close_tab", lambda *a, **k: True)
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: True)
    monkeypatch.setattr(bp, "dedicated_profile_processes", lambda *a, **k: [])
    monkeypatch.setattr(bp, "_release_lease_after_stop", lambda *a, **k: True)
    return {"dir": tmp_path, "alive": alive, "state_file": state_file}


class _FakeSession:
    """A session that records its teardown instead of talking to a browser."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.owned = ["t1", "t2"]
        self.stopped = False

    def close_all_owned_tabs(self):
        closed, self.owned = list(self.owned), []
        return {"closed": closed, "failed": [], "closed_count": len(closed),
                "failed_count": 0}

    def stop(self):
        self.stopped = True
        return {"port_freed": True, "profile_unlocked": True, "owned_processes": 0,
                "status": "STOPPED"}


def _lease(**over):
    base = lease.new_lease(lease.OWNER_CLI, owner_pid=4242)
    base.update(over)
    return base


def _calls_to(path: pathlib.Path, name: str) -> list[ast.Call]:
    """Every ``name(...)`` call in a module. Reads the AST, so a mention in a
    docstring or a comment is not a call - the T7.23 lesson about text searches."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if called == name:
            found.append(node)
    return found


# ---------------------------------------------------------------------------
# §1 - every launch path has an owner
# ---------------------------------------------------------------------------

def test_launch_headless_is_only_ever_called_from_the_provider():
    """The choke point. If a second module could spawn Chrome it could skip the
    lease, and the whole ownership contract would be optional."""
    offenders = []
    for path in PKG.rglob("*.py"):
        if path.name == "hh_browser_provider.py":
            continue
        if _calls_to(path, "launch_headless"):
            offenders.append(str(path.relative_to(REPO)).replace("\\", "/"))
    assert offenders == [], (
        "these modules start Chrome without going through the provider, so no "
        f"lease can be registered: {offenders}"
    )


def test_the_provider_registers_a_lease_when_it_spawns():
    """Spawning and registering are one act, not two that could drift apart."""
    import inspect

    source = (PKG / "hh_browser_provider.py").read_text(encoding="utf-8")
    assert "_register_lease_at_spawn(" in source
    assert "owner_type" in inspect.signature(bp.launch_headless).parameters
    assert "owner_type" in inspect.signature(bp.ensure_headless_browser).parameters


def test_every_production_browser_caller_is_a_known_one():
    """A new caller must be added here on purpose - that is the review moment."""
    callers: set[str] = set()
    sites = 0
    for path in PKG.rglob("*.py"):
        calls = _calls_to(path, "ensure_hh_browser")
        if calls:
            callers.add(str(path.relative_to(REPO)).replace("\\", "/"))
            sites += len(calls)
    assert callers == KNOWN_BROWSER_CALLERS, (
        "the set of modules asking for a browser changed; decide who owns the "
        f"shutdown for the new one: {sorted(callers ^ KNOWN_BROWSER_CALLERS)}"
    )
    assert sites == KNOWN_BROWSER_CALL_SITES


def _owning_operations(path: pathlib.Path) -> list[int]:
    """Lines of ``with hh_operation(...)`` / ``with managed_headless_session(...)``.

    AST, not text: a docstring that names ``hh_operation`` is not an operation, and
    the T7.23 lesson was precisely that a text search answers a different question.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            expr = item.context_expr
            if not isinstance(expr, ast.Call):
                continue
            func = expr.func
            called = (
                func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            )
            if called in ("hh_operation", "managed_headless_session"):
                lines.append(node.lineno)
    return lines


# The modules T7.25 §3-§7 moved onto the contract. Each must hold at least one
# owning operation; the set is here so a *new* browser user has to be added on
# purpose rather than discovered later.
OWNING_PRODUCTION_USERS = (
    "ai_assistant/cli.py",
    "ai_assistant/hh_application_runner.py",
    "ai_assistant/hh_autonomous_agent.py",
    "ai_assistant/hh_message_watcher.py",
    "ai_assistant/hh_post_submit_verifier.py",
)


def test_every_production_browser_user_goes_through_the_ownership_contract():
    """T7.24's companion pin, inverted.

    T7.24 asserted "nothing in production owns the shutdown yet, which is the
    point", and said it should be replaced by one asserting the new owner releases
    the browser. This is that replacement: every production module that drives a
    browser must do it inside ``hh_operation`` (or T7.23's
    ``managed_headless_session``), so the release is in a ``finally``.
    """
    offenders = [
        rel for rel in OWNING_PRODUCTION_USERS if not _owning_operations(REPO / rel)
    ]
    assert offenders == [], (
        "these modules drive a browser without an owning operation, so nothing "
        f"releases it: {offenders}"
    )


# ---------------------------------------------------------------------------
# §5 - the stale rule: four facts, never one dead parent
# ---------------------------------------------------------------------------

def test_a_dead_owner_with_a_live_operation_is_not_stale(world):
    world["alive"].add(os.getpid())
    lease.write_lease(_lease(owner_pid=4242, last_activity_at="2000-01-01T00:00:00",
                             operations=[{"op_id": "a", "pid": os.getpid(),
                                          "owner_type": lease.OWNER_UI,
                                          "purpose": "working"}]))
    state = lease.lease_state(instance_is_ours=True)
    assert state["state"] == lease.STATE_ACTIVE
    assert state["stale"] is False


def test_a_dead_owner_with_no_activity_at_all_is_stale(world):
    lease.write_lease(_lease(owner_pid=4242, last_activity_at="2000-01-01T00:00:00"))
    state = lease.lease_state(instance_is_ours=True)
    assert state["state"] == lease.STATE_STALE
    assert state["stale"] is True


def test_a_dead_owner_with_a_fresh_heartbeat_is_not_stale(world):
    lease.write_lease(_lease(owner_pid=4242))
    state = lease.lease_state(instance_is_ours=True)
    assert state["state"] == lease.STATE_LIVE
    assert state["stale"] is False


def test_a_dead_parent_alone_never_justifies_a_kill(world):
    """``DETACHED_PROCESS`` means the launcher is *expected* to be gone - it is the
    normal case, not the suspicious one."""
    lease.write_lease(_lease(owner_pid=4242))
    state = lease.lease_state(instance_is_ours=True)
    assert state["owner_alive"] is False
    assert state["stale"] is False
    assert any("dead" in reason for reason in state["reasons"])


def test_an_instance_that_is_not_ours_is_never_stale(world):
    """We do not reclaim a browser we cannot prove is our own."""
    lease.write_lease(_lease(owner_pid=4242, last_activity_at="2000-01-01T00:00:00"))
    state = lease.lease_state(instance_is_ours=False)
    assert state["stale"] is False
    assert state["state"] != lease.STATE_STALE


def test_no_lease_at_all_is_absent_not_stale(world):
    state = lease.lease_state()
    assert state["state"] == lease.STATE_ABSENT
    assert state["stale"] is False


def test_a_crashed_operation_does_not_keep_the_lease_looking_busy(world):
    """A counter incremented on acquire would still read 1 here, forever."""
    lease.write_lease(_lease(operations=[{"op_id": "gone", "pid": 999_999,
                                          "owner_type": lease.OWNER_UI,
                                          "purpose": "crashed"}]))
    assert lease.active_operations(lease.read_lease()) == []
    assert lease.lease_state(instance_is_ours=True)["active_operations"] == 0


def test_each_fact_of_the_stale_rule_is_reported_separately(world):
    lease.write_lease(_lease(owner_pid=4242, last_activity_at="2000-01-01T00:00:00"))
    reasons = lease.lease_state(instance_is_ours=True)["reasons"]
    assert len(reasons) == 4
    assert any("dead" in r for r in reasons)
    assert any("no active operation" in r for r in reasons)
    assert any("stale" in r for r in reasons)
    assert any("dedicated headless" in r for r in reasons)


# ---------------------------------------------------------------------------
# §9 - concurrency: never a second Chrome on the same profile
# ---------------------------------------------------------------------------

def test_a_second_operation_gets_busy_and_starts_nothing(world, monkeypatch):
    world["alive"].add(4242)
    lease.write_lease(_lease(owner_pid=4242, operations=[
        {"op_id": "a", "pid": 4242, "owner_type": lease.OWNER_CLI, "purpose": "first"}]))
    built: list[dict] = []
    monkeypatch.setattr(bp, "HeadlessBrowserSession", lambda **kw: built.append(kw))

    busy = lease.hh_operation(lease.OWNER_UI, purpose="second")
    with pytest.raises(lease.BrowserBusy), busy:
        raise AssertionError("the body must not run when the browser is busy")

    assert built == [], "a second browser was constructed on a busy profile"
    assert lease.read_lease()["operations"][0]["purpose"] == "first"


def test_the_same_process_re_entering_reuses_one_browser(world):
    world["alive"].add(os.getpid())
    lease.write_lease(_lease(owner_pid=os.getpid(), operations=[
        {"op_id": "a", "pid": os.getpid(), "owner_type": lease.OWNER_UI,
         "purpose": "first"}]))
    claim = lease.acquire(lease.OWNER_UI, purpose="second")
    assert claim["ok"] is True
    assert claim["status"] == lease.STATUS_REUSED


def test_a_live_session_without_operations_is_adopted_not_relaunched(world):
    lease.write_lease(_lease(owner_pid=4242))
    before = lease.read_lease()["session_id"]
    claim = lease.acquire(lease.OWNER_UI, purpose="x")
    assert claim["status"] == lease.STATUS_REUSED
    assert claim["lease"]["session_id"] == before


def test_no_lease_means_the_caller_becomes_the_owner(world):
    claim = lease.acquire(lease.OWNER_UI, purpose="x")
    assert claim["status"] == lease.STATUS_ACQUIRED
    assert lease.read_lease()["owner_type"] == lease.OWNER_UI


# ---------------------------------------------------------------------------
# §4 / §7 / §10 - the browser lives exactly as long as the operation
# ---------------------------------------------------------------------------

def test_the_last_operation_closes_the_browser_and_clears_the_lease(world):
    session = _FakeSession()
    lease.write_lease(_lease(owner_pid=os.getpid()))
    report = lease.release(lease.read_lease()["session_id"], session=session)
    assert report["stopped"] is True
    assert session.stopped is True
    assert report["tabs_closed"] == 2
    assert report["port_freed"] is True
    assert lease.read_lease() is None


def test_a_release_while_another_operation_runs_leaves_the_browser_up(world):
    world["alive"].add(4242)
    lease.write_lease(_lease(owner_pid=os.getpid(), operations=[
        {"op_id": "other", "pid": 4242, "owner_type": lease.OWNER_CLI,
         "purpose": "still working"}]))
    session = _FakeSession()
    report = lease.release(lease.read_lease()["session_id"], session=session)
    assert report["stopped"] is False
    assert session.stopped is False
    assert report["remaining_operations"] == 1
    assert lease.read_lease() is not None


def test_a_release_without_a_session_enumerates_the_endpoint(world, monkeypatch):
    """No registry to consult. Reporting "0 closed" would read as "nothing to
    close" - so enumerate the endpoint and say how many there were."""
    monkeypatch.setattr(bp, "list_tabs", lambda *a, **k: [{"id": "t1"}, {"id": "t2"}])
    closed: list[str] = []
    monkeypatch.setattr(bp, "close_tab",
                        lambda url, tid, **k: closed.append(tid) or True)
    lease.write_lease(_lease(owner_pid=os.getpid()))
    report = lease.release(lease.read_lease()["session_id"])
    assert report["tabs_seen_on_endpoint"] == 2
    assert report["tabs_closed"] == 2
    assert closed == ["t1", "t2"]


def test_an_exception_inside_an_operation_still_releases_the_browser(world):
    """§10's crash, at the level where the guarantee is made."""
    session = _FakeSession()
    crash = lease.hh_operation(lease.OWNER_UI, purpose="crash", session=session)
    with pytest.raises(RuntimeError, match="boom"), crash:
        raise RuntimeError("boom")
    assert session.stopped is True
    assert lease.read_lease() is None


def test_a_normal_exit_also_releases_the_browser(world):
    session = _FakeSession()
    with lease.hh_operation(lease.OWNER_UI, purpose="fine", session=session) as handle:
        assert handle["session"] is session
    assert session.stopped is True
    assert lease.read_lease() is None
    assert handle["release_report"]["stopped"] is True


def test_a_failing_release_does_not_replace_the_original_exception(world, monkeypatch):
    def _explode(*a, **k):
        raise OSError("the release itself failed")

    monkeypatch.setattr(lease, "release", _explode)
    session = _FakeSession()
    crash = lease.hh_operation(lease.OWNER_UI, purpose="crash", session=session)
    with pytest.raises(RuntimeError, match="the real problem"), crash:
        raise RuntimeError("the real problem")


def test_release_on_exit_can_leave_the_browser_for_the_next_step(world):
    session = _FakeSession()
    with lease.hh_operation(lease.OWNER_UI, purpose="chain",
                            release_on_exit=False, session=session):
        pass
    assert session.stopped is False
    assert lease.read_lease() is not None


# ---------------------------------------------------------------------------
# §6 - shutdown hooks for long-lived owners
# ---------------------------------------------------------------------------

def test_the_ui_release_hook_never_raises(world):
    from ai_assistant.ui import app as ui

    result = ui._release_ui_browser("test")
    assert result["why"] == "test"
    assert result["released"] is False


def test_the_ui_hook_never_touches_a_lease_that_is_not_its_own(world):
    from ai_assistant.ui import app as ui

    lease.write_lease(_lease(owner_pid=4242, owner_type=lease.OWNER_CLI))
    result = ui._release_ui_browser("shutdown")
    assert result["released"] is False
    assert "not to" in result["reason"] or "belongs to" in result["reason"]
    assert lease.read_lease() is not None


def test_the_ui_hook_releases_its_own_lease(world, monkeypatch):
    from ai_assistant.ui import app as ui

    monkeypatch.setattr(bp, "HeadlessBrowserSession", _FakeSession)
    lease.write_lease(_lease(owner_pid=os.getpid(), owner_type=lease.OWNER_UI))
    result = ui._release_ui_browser("shutdown")
    assert result["released"] is True
    assert lease.read_lease() is None


def test_the_ui_registers_a_release_on_the_way_out():
    """Both paths out of the process must be covered: the ASGI shutdown and the
    ``atexit`` fallback for a hard kill."""
    source = (PKG / "ui" / "app.py").read_text(encoding="utf-8")
    assert "atexit.register(_release_ui_browser" in source
    assert 'finally:\n        _release_ui_browser("shutdown")' in source


def test_the_ui_exposes_the_ownership_and_preflight_routes():
    from ai_assistant.ui.app import HhPreflightRequest, app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/hh/ownership" in paths
    assert "/api/hh/preflight" in paths
    assert HhPreflightRequest(vacancy_id="137149827").vacancy_id == "137149827"


# ---------------------------------------------------------------------------
# §2 - the record must not lie about who is still running
# ---------------------------------------------------------------------------

def test_a_stop_does_not_mark_another_instance_stopped(world):
    """Measured 29.09: the record for the browser started at 11:26 (root pid 26152)
    carried ``pid: null, stopped_at: 12:20:24`` while that browser was still
    listening on 9111. One shared state file, and a stop from elsewhere overwrote it.
    """
    bp.write_state({"pid": 26152, "cdp_url": "http://127.0.0.1:9111",
                    "profile_dir": str(bp.headless_profile_dir()),
                    "started_at": "2026-09-29T11:26:24"})

    elsewhere = bp.HeadlessBrowserSession(cdp_url="http://127.0.0.1:9199",
                                          profile_dir=world["dir"] / "another profile")
    result = elsewhere.stop()

    assert result["state_record_matched"] is False
    assert bp.read_state()["pid"] == 26152
    assert "stopped_at" not in bp.read_state()


def test_a_stop_still_records_itself_when_the_record_is_its_own(world):
    bp.write_state({"pid": 4242, "cdp_url": bp.headless_cdp_url(),
                    "profile_dir": str(bp.headless_profile_dir()),
                    "started_at": "2026-09-29T11:26:24"})
    session = bp.HeadlessBrowserSession(cdp_url=bp.headless_cdp_url(),
                                        profile_dir=bp.headless_profile_dir())
    result = session.stop()
    assert result["state_record_matched"] is True
    assert bp.read_state()["pid"] is None
    assert "stopped_at" in bp.read_state()


# ---------------------------------------------------------------------------
# the read-only operation stays read-only, and foreign browsers stay foreign
# ---------------------------------------------------------------------------

def test_the_read_only_operation_dispatches_no_input():
    """Read-only by construction: nothing in this module can click, type or send."""
    source = (PKG / "hh_readonly_operation.py").read_text(encoding="utf-8")
    for banned in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent",
                   "Input.insertText", "Input.dispatchDragEvent",
                   "Runtime.terminateExecution"):
        assert banned not in source, f"{banned} must never appear here"


def _string_constants(node: ast.AST) -> list[str]:
    return [sub.value for sub in ast.walk(node)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)]


def test_no_kill_by_image_name_anywhere_in_the_runner():
    """``taskkill /IM chrome.exe`` would take Misha's own Chrome with it, so the only
    allowed termination is one named pid.

    The check reads real calls, not text: the provider *documents* the ban in a
    docstring, so a text search flags the very comment that explains it - the T7.23
    lesson, which this test would otherwise walk straight into.
    """
    subprocess_family = {"run", "Popen", "call", "check_output", "system", "popen"}
    offenders = []
    for path in (PKG / "hh_browser_lease.py", PKG / "hh_browser_provider.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called not in subprocess_family:
                continue
            if any("/IM" in value for value in _string_constants(node)):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"an image-wide kill is reachable from {offenders}"

    provider = (PKG / "hh_browser_provider.py").read_text(encoding="utf-8")
    assert "TerminateProcess" in provider, "termination must be one named pid"


def test_a_foreign_profile_path_is_never_our_profile():
    """BrowserOS' bundled Chrome keeps its profile under BrowserClaw - it must not
    be read as ours, however similar the rest of the command line looks."""
    foreign = bp.user_data_dir_from_command_line(
        r'"C:\Program Files\Google\Chrome\Application\chrome.exe" '
        r'--remote-debugging-port=9110 '
        r'--user-data-dir="C:\Users\Misha\AppData\Local\BrowserOS\BrowserClaw\User Data\Profile 1"'
    )
    assert foreign is not None
    assert bp._normalise_path(foreign) != bp._normalise_path(str(bp.headless_profile_dir()))
    assert any(marker in bp._normalise_path(foreign) for marker in bp.FOREIGN_PROFILE_MARKERS)


def test_our_own_profile_path_is_recognised_in_all_three_quoting_styles():
    """The profile path contains a space, so the quoting style is the evidence."""
    ours = str(bp.headless_profile_dir())
    for line in (
        f'chrome.exe --user-data-dir="{ours}" --no-first-run',
        f"chrome.exe --user-data-dir={ours} --no-first-run",
        f'chrome.exe "--user-data-dir={ours}" --no-first-run',
    ):
        parsed = bp.user_data_dir_from_command_line(line)
        assert parsed is not None, line
        assert bp._normalise_path(parsed) == bp._normalise_path(ours), line


def test_only_a_headless_chrome_counts_as_ours():
    assert bp._is_ours({"Browser": "Chrome/140.0", "User-Agent": "HeadlessChrome/140.0"})
    assert not bp._is_ours({"Browser": "Chrome/140.0",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0)"})
    assert not bp._is_ours(None)


def test_the_lease_records_every_field_the_task_names(world):
    record = lease.new_lease(lease.OWNER_UI, owner_pid=4242, purpose="why")
    for field in ("session_id", "owner_type", "owner_pid", "started_at",
                  "last_activity_at", "profile", "cdp_port"):
        assert field in record, f"the lease must name {field}"
    assert record["owner_type"] == lease.OWNER_UI
    assert record["owner_pid"] == 4242
    assert record["purpose"] == "why"


def test_an_undeclared_owner_is_recorded_as_unknown_not_guessed(world):
    record = lease.new_lease("something-new", owner_pid=4242)
    assert record["owner_type"] == lease.OWNER_UNKNOWN


# ---------------------------------------------------------------------------
# the suite is not one of the owners
# ---------------------------------------------------------------------------
# Both guarantees below were violated on 29.09 (T7.24) and both are the same shape:
# a test reached the *live* runner's own state. They are asserted here rather than
# in conftest.py because a test is what has to fail if the guard regresses.

CDP_BLOCKLIST = {9110, 9111, 9010, 9210, 9222, 9223, 9999}


def test_no_test_can_reach_the_live_cdp_browser():
    """9111 carries a real browser with a real HH session, so the suite must not be
    able to talk to it.

    Measured 29.09: it could. The network guard's CDP blocklist predated the T7.21
    move to port 9111, so a pytest process connected to the live browser, took the
    reuse path, and registered itself as that browser's owner.
    """
    import socket

    with pytest.raises(RuntimeError, match="SAFETY VIOLATION"):
        socket.create_connection(("127.0.0.1", bp.HEADLESS_CDP_PORT), timeout=0.2)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, \
            pytest.raises(RuntimeError, match="SAFETY VIOLATION"):
        sock.connect(("127.0.0.1", bp.HEADLESS_CDP_PORT))


def test_the_guard_blocks_cdp_ports_not_loopback_as_such():
    """The block has to be about CDP ports; blocking loopback wholesale would break
    every in-process ASGI client in the suite. So an ordinary closed loopback port
    must reach the real socket stack and be refused there, not by the guard."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    # Nothing is listening now, and an ephemeral port is not one of the CDP ports.
    assert port not in CDP_BLOCKLIST, "unlucky ephemeral port; rerun"

    with pytest.raises(OSError) as caught:
        socket.create_connection(("127.0.0.1", port), timeout=0.5)
    assert "SAFETY VIOLATION" not in str(caught.value), (
        "a non-CDP loopback port must not be blocked by the guard"
    )


def test_the_network_guard_names_the_headless_port_in_its_source():
    """A behaviour test can only prove today's port. This pins the source so that
    moving ``HEADLESS_CDP_PORT`` forces a matching edit in the guard."""
    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert f"{{9110, {bp.HEADLESS_CDP_PORT}" in source, (
        "tests/conftest.py's CDP blocklist must name HEADLESS_CDP_PORT explicitly"
    )


def test_no_test_writes_the_real_headless_record():
    """The runner's record, log and lease live in the repository, beside the artifacts
    of the browser that is actually running - so a test must never write them.

    Measured 29.09: six separate pytest runs stamped ``stopped_at`` into
    ``runner-state.json`` while root pid 26152 was still listening on 9111.
    """
    real = (REPO / "artifacts" / "headless-poc").resolve()
    for path in (bp.STATE_FILE, bp.CHROME_LOG, lease.LEASE_FILE):
        resolved = pathlib.Path(path).resolve()
        assert real not in resolved.parents, f"{resolved} is the live runner's record"
