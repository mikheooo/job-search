"""T7.25: every production browser path goes through an owned operation.

T7.24 built the contract (``hh_operation``) and measured the gap: ten production
call sites took a browser and none of them owned the shutdown. T7.25 is the
migration, and these pins are what keep it migrated. They answer four questions:

* **§14** is there a production call site that obtains a browser without an owner?
  The answer is computed from the AST by ``tools/.t725_launch_map.py`` and must be
  zero - not "it records a lease now", zero.
* **§8** is the set of modules allowed to touch the raw launch primitives short and
  explicit? An allowlist, checked by name, so widening it is a deliberate act.
* **§9** does importing ``tools/inspect_conversation_detail.py`` start anything? It
  used to launch Chrome at import time; now an import must be inert.
* **§11/§12** can a second component start a second Chrome on one profile, and does
  an operation that raises still give the browser back?

Offline. Nothing here starts a browser, clicks, submits or sends.
"""

from __future__ import annotations

import ast
import importlib
import os
import pathlib
import subprocess
import sys

import pytest

from ai_assistant import hh_browser_lease as lease
from ai_assistant import hh_browser_provider as bp

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "ai_assistant"


def test_release_attached_session_leaves_shared_browser_running(monkeypatch):
    class Attached:
        reused = True
        owned = ['my-tab']
        def __init__(self):
            self.closed = False
        def close_all_owned_tabs(self):
            self.closed = True
            return {'closed_count': 1, 'failed': []}
        def stop(self):
            pytest.fail('an attached session must not close the shared browser')
    session = Attached()
    monkeypatch.setattr(lease, 'read_lease', lambda: {'session_id': 'shared'})
    monkeypatch.setattr(lease, 'active_operations', lambda _: [])
    heartbeats = []
    monkeypatch.setattr(lease, 'heartbeat', lambda sid: heartbeats.append(sid))
    result = lease.release(session_id='shared', session=session)
    assert session.closed and result['tabs_closed'] == 1
    assert result['stopped'] is False and heartbeats == ['shared']


@pytest.mark.parametrize('status', ['HH_AUTH_REQUIRED', 'PAGE_NOT_READY'])
def test_auth_failure_does_not_stop_attached_browser(monkeypatch, status):
    monkeypatch.setattr(bp, 'launch_headless', lambda **kwargs: {'ok': True, 'reused': True})
    session = bp.HeadlessBrowserSession(require_auth=True)
    monkeypatch.setattr(session, 'heartbeat', lambda: None)
    monkeypatch.setattr(session, 'check_auth', lambda: {'status': status, 'message': 'test', 'auth': {}})
    closed = []
    monkeypatch.setattr(session, 'close_all_owned_tabs', lambda: closed.append(True))
    monkeypatch.setattr(session, 'stop', lambda: pytest.fail('must not stop shared Chrome'))
    result = session.start()
    assert result['ok'] is False and result['status'] == status and closed == [True]

LAUNCH_PRIMITIVES = ("ensure_hh_browser", "ensure_headless_browser", "launch_headless")
OWNING_MANAGERS = ("hh_operation", "managed_headless_session")

# §8: the only modules allowed to call a launch primitive. They *are* the launch
# layer - the primitives' own implementation - so a call here is the machinery,
# not a production path that forgot an owner.
LAUNCH_LAYER_ALLOWLIST = (
    "ai_assistant/hh_browser_lease.py",
    "ai_assistant/hh_browser_provider.py",
    "ai_assistant/hh_browser_launcher.py",
)

# The components T7.25 §3-§7 migrated, one per owner type / purpose.
MIGRATED_COMPONENTS = (
    "ai_assistant/cli.py",
    "ai_assistant/hh_application_runner.py",
    "ai_assistant/hh_autonomous_agent.py",
    "ai_assistant/hh_message_watcher.py",
    "ai_assistant/hh_post_submit_verifier.py",
)


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _owning_lines(tree: ast.Module) -> set[int]:
    """Every line covered by a ``with hh_operation(...)`` / ``managed_...``."""
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and _call_name(expr) in OWNING_MANAGERS:
                covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return covered


def _unowned_production_launch_paths() -> list[str]:
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel in LAUNCH_LAYER_ALLOWLIST:
            continue
        tree = _parse(path)
        covered = _owning_lines(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) in LAUNCH_PRIMITIVES
                and node.lineno not in covered
            ):
                offenders.append(f"{rel}:{node.lineno} {_call_name(node)}")
    return offenders


# ---------------------------------------------------------------------------
# §14 - the verdict, computed two ways
# ---------------------------------------------------------------------------

def test_no_production_launch_path_is_unowned():
    """The §15 verdict, in the suite rather than only in a report."""
    offenders = _unowned_production_launch_paths()
    assert offenders == [], (
        "these production call sites obtain a browser with no owning operation, so "
        f"nothing guarantees it is ever released: {offenders}"
    )


def test_the_map_tool_agrees_and_exits_zero():
    """The tool is what the report quotes, so it gets its own pin. A report that
    quotes a number the suite cannot reproduce is a claim, not a measurement."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "tools" / ".t725_launch_map.py"), "--expect-zero"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PRODUCTION UNOWNED LAUNCH PATHS                                : 0" in proc.stdout
    assert "owning operations in production" in proc.stdout


# ---------------------------------------------------------------------------
# §8 - the allowlist
# ---------------------------------------------------------------------------

def test_only_the_launch_layer_may_call_a_launch_primitive():
    """A short explicit allowlist, checked by name. Adding a module here is the
    review moment: it means a new place can start a browser outside the contract."""
    callers = set()
    for path in sorted(PKG.rglob("*.py")):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in LAUNCH_PRIMITIVES:
                callers.add(path.relative_to(REPO).as_posix())
                break
    assert callers <= set(LAUNCH_LAYER_ALLOWLIST), (
        f"modules outside the launch layer call a launch primitive: "
        f"{sorted(callers - set(LAUNCH_LAYER_ALLOWLIST))}"
    )


def test_the_allowlist_modules_are_the_launch_layer_and_exist():
    """A pin on the allowlist itself, so it cannot quietly grow or point at a file
    that no longer exists."""
    assert set(LAUNCH_LAYER_ALLOWLIST) == {
        "ai_assistant/hh_browser_lease.py",
        "ai_assistant/hh_browser_provider.py",
        "ai_assistant/hh_browser_launcher.py",
    }
    for rel in LAUNCH_LAYER_ALLOWLIST:
        assert (REPO / rel).is_file(), rel


def test_no_production_module_imports_a_launch_primitive():
    """Importing one is how a call site gets written. Nothing outside the launch
    layer may even name these three in an import statement."""
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel in LAUNCH_LAYER_ALLOWLIST:
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name in LAUNCH_PRIMITIVES:
                    offenders.append(f"{rel}:{node.lineno} {alias.name}")
    assert offenders == [], (
        "these modules import a launch primitive; the contract is hh_operation, "
        f"not a browser handed out by name: {offenders}"
    )


# ---------------------------------------------------------------------------
# §9 - an import must be inert
# ---------------------------------------------------------------------------

def test_importing_the_inspection_tool_starts_nothing(tmp_path, monkeypatch):
    """It used to launch Chrome at module scope, so a test collector, an editor
    indexer or a REPL completion started a browser on the dedicated profile and
    left it running. An import must now be inert: no Chrome, no 9111, no lease."""
    monkeypatch.setattr(lease, "LEASE_FILE", tmp_path / "browser-lease.json")
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bp, "STATE_FILE", tmp_path / "runner-state.json")

    def _refuse(*_args, **_kwargs):
        raise AssertionError("importing the tool started a browser")

    monkeypatch.setattr(bp, "launch_headless", _refuse)
    monkeypatch.setattr(bp.HeadlessBrowserSession, "start", _refuse)

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    sys.modules.pop("tools.inspect_conversation_detail", None)
    mod = importlib.import_module("tools.inspect_conversation_detail")

    assert callable(mod.main)
    assert not (tmp_path / "browser-lease.json").exists()
    assert not (tmp_path / "runner-state.json").exists()


def test_the_inspection_tool_keeps_its_work_behind_a_main_guard():
    """``def main()`` alone is not the guarantee - the *call* has to be behind the
    guard, or an import runs it."""
    tree = _parse(REPO / "tools" / "inspect_conversation_detail.py")
    guarded = [
        node for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and getattr(node.test.left, "id", None) == "__name__"
    ]
    assert guarded, "no `if __name__ == \"__main__\"` guard at module level"
    module_level_calls = [
        node for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert module_level_calls == [], (
        "the module still runs a call at import time: "
        f"{[ast.dump(n.value.func) for n in module_level_calls]}"
    )


# ---------------------------------------------------------------------------
# §11 - one profile, one browser
# ---------------------------------------------------------------------------

@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A private lease file and a controllable outside world (no sockets)."""
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bp, "STATE_FILE", tmp_path / "runner-state.json")
    monkeypatch.setattr(bp, "CHROME_LOG", tmp_path / "chrome.log")
    monkeypatch.setattr(lease, "LEASE_FILE", tmp_path / "browser-lease.json")
    monkeypatch.setattr(bp, "pid_alive", lambda pid: True)
    monkeypatch.setattr(bp, "cdp_version", lambda *a, **k: None)
    monkeypatch.setattr(bp, "port_is_listening", lambda *a, **k: False)
    monkeypatch.setattr(bp, "list_tabs", lambda *a, **k: [])
    monkeypatch.setattr(bp, "close_tab", lambda *a, **k: True)
    monkeypatch.setattr(bp, "quit_browser", lambda *a, **k: True)
    monkeypatch.setattr(bp, "wait_port_free", lambda *a, **k: True)
    monkeypatch.setattr(bp, "dedicated_profile_processes", lambda *a, **k: [])
    monkeypatch.setattr(bp, "_release_lease_after_stop", lambda *a, **k: True)
    return tmp_path


class _CountingSession:
    """A session that counts how many times a browser would have been started."""

    started = 0
    stopped = 0

    def __init__(self, **_kwargs):
        self.owned = ["t1"]

    def start(self):
        type(self).started += 1
        return {"ok": True, "status": "RUNNING"}

    def close_all_owned_tabs(self):
        closed, self.owned = list(self.owned), []
        return {"closed": closed, "failed": [], "closed_count": len(closed),
                "failed_count": 0}

    def stop(self):
        type(self).stopped += 1
        return {"port_freed": True, "profile_unlocked": True, "owned_processes": 0,
                "status": "STOPPED"}


def test_a_second_operation_in_the_same_process_adopts_the_live_session(world, monkeypatch):
    """§11, the in-process half: A active + B opens must not make a second Chrome.

    A second ``hh_operation`` from the *same* pid is not an error - it adopts the
    live lease and reports ``REUSED``. That is deliberately the mechanism the
    migrated wrappers rely on when they re-enter their own function once
    ``evaluate_fn`` is set: the outer call owns the browser, the inner call does
    the work on that same one, and the count stays at one.
    """
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _CountingSession)

    with lease.hh_operation(lease.OWNER_CLI, purpose="search") as first:
        assert first["status"] == lease.STATUS_ACQUIRED

        with lease.hh_operation(lease.OWNER_WATCHER, purpose="messages") as second:
            assert second["status"] == lease.STATUS_REUSED, (
                "a second operation did not adopt the live session"
            )
            assert second["lease"]["session_id"] == first["lease"]["session_id"], (
                "a second operation opened a second session on the same profile"
            )

        assert lease.read_lease() is not None, (
            "the inner exit released the browser while the outer operation still held it"
        )

    assert lease.read_lease() is None


def test_a_foreign_owner_makes_the_next_operation_busy(world, monkeypatch):
    """§11, the cross-process half: while another process works, B refuses.

    B must not "wait politely and then start anyway". ``hh_operation`` raises
    ``BrowserBusy`` on entry, before anything is launched, which is the only shape
    that makes "never two Chromes on one ``--user-data-dir``" true rather than
    merely likely.
    """
    _CountingSession.started = 0
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _CountingSession)

    # A lease held by somebody else, with their operation in flight.
    foreign_pid = os.getpid() + 1
    rec = lease.new_lease(lease.OWNER_AUTONOMOUS, owner_pid=foreign_pid, purpose="cycle")
    rec["operations"] = [
        {
            "op_id": "foreign-op",
            "pid": foreign_pid,
            "owner_type": lease.OWNER_AUTONOMOUS,
            "purpose": "cycle",
            "started_at": lease._now(),
        }
    ]
    lease.write_lease(rec)

    with (
        pytest.raises(lease.BrowserBusy),
        lease.hh_operation(lease.OWNER_CLI, purpose="search"),
    ):
        pass  # pragma: no cover - the raise happens on entry

    assert _CountingSession.started == 0, (
        "a browser was launched while another process held the lease"
    )
    assert lease.read_lease() is not None, "the foreign owner's lease was clobbered"


def test_two_sequential_operations_do_not_leave_a_browser_behind(world, monkeypatch):
    """§11, the other half: after both operations the root count is zero."""
    _CountingSession.started = 0
    _CountingSession.stopped = 0
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _CountingSession)

    for _ in range(2):
        with lease.hh_operation(lease.OWNER_CLI, purpose="search") as op:
            op["session"].start()

    assert _CountingSession.started == 2
    assert _CountingSession.stopped == 2
    assert lease.read_lease() is None


def test_ten_polls_do_not_produce_ten_live_browsers(world, monkeypatch):
    """§6's number, made checkable: the watcher acquires and releases per poll, so
    ten polls are ten *sequential* operations, never ten concurrent ones."""
    _CountingSession.started = 0
    _CountingSession.stopped = 0
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _CountingSession)

    live = 0
    peak = 0
    for _ in range(10):
        with lease.hh_operation(lease.OWNER_WATCHER, purpose="messages") as op:
            op["session"].start()
            live += 1
            peak = max(peak, live)
        live -= 1

    assert peak == 1, "more than one browser was live at the same time"
    assert _CountingSession.stopped == 10
    assert lease.read_lease() is None


# ---------------------------------------------------------------------------
# §12 - the crash matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", MIGRATED_COMPONENTS)
def test_each_component_owns_its_browser(rel):
    """Every migrated component must hold an owning operation. Combined with the
    release-on-error pin below, that is the crash matrix: the component's browser
    work is inside the operation, and the operation releases on every exit."""
    assert _owning_lines(_parse(REPO / rel)), (
        f"{rel} drives a browser with no owning operation"
    )


@pytest.mark.parametrize(
    "owner, purpose",
    [
        (lease.OWNER_CLI, "messages"),
        (lease.OWNER_CLI, "application"),
        (lease.OWNER_AUTONOMOUS, "cycle"),
        (lease.OWNER_WATCHER, "messages"),
        (lease.OWNER_CLI, "verify"),
    ],
)
def test_an_operation_that_raises_still_gives_the_browser_back(world, monkeypatch, owner, purpose):
    """The crash row of §12: whatever the component does inside, the release runs.

    Parameterised over the owner/purpose pairs the five components actually use,
    so the matrix is the real table and not a single generic case.
    """
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _CountingSession)
    _CountingSession.started = 0
    _CountingSession.stopped = 0

    class _Boom(RuntimeError):
        pass

    with (
        pytest.raises(_Boom),
        lease.hh_operation(owner, purpose=purpose) as op,
    ):
        op["session"].start()
        raise _Boom("the component died mid-operation")

    assert _CountingSession.stopped == 1, "the browser was not given back"
    assert lease.read_lease() is None, "the lease outlived the operation"


def test_a_release_that_raises_does_not_replace_the_original_error(world, monkeypatch):
    """§7: an error during release must not become the error the caller sees."""
    monkeypatch.setattr(bp, "HeadlessBrowserSession", _CountingSession)

    def _explode(self):
        raise OSError("the teardown could not talk to the browser")

    monkeypatch.setattr(_CountingSession, "close_all_owned_tabs", _explode)

    class _Boom(RuntimeError):
        pass

    with (
        pytest.raises(_Boom, match="the real cause"),
        lease.hh_operation(lease.OWNER_CLI, purpose="verify") as op,
    ):
        op["session"].start()
        raise _Boom("the real cause")
