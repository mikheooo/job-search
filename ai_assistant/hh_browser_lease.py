"""T7.24 — the ownership contract for the dedicated headless browser.

T7.23 taught the runner to clean up after *itself*: ``managed_headless_session``
wraps a workflow in ``try/finally`` and the teardown is measured. That is a
guarantee about one process, and it is not enough, because T7.24 found a live
browser on the dedicated profile whose launcher had been dead for hours.

The gap it exposes is ownership, not cleanup. ``launch_headless()`` spawns Chrome
with ``DETACHED_PROCESS`` - deliberately, so the browser survives the caller - and
then returns. Nothing records who asked for it, so nothing can ever answer "is
anyone still using this?". Every production caller of ``ensure_hh_browser()`` was
in that position: a browser starts, and no component owns its shutdown.

This module makes ownership an object instead of a convention:

* **a lease** - one JSON record naming the session, its owner, the profile, the
  port and when it was last touched;
* **operations** - the lease holds a list, so "is someone working right now?" is
  answered by liveness of the operation's pid rather than by a counter that a
  crash would strand above zero;
* **a stale rule** that needs *four* independent facts, never one dead parent;
* **``hh_operation()``** - acquire, work, release, with the release in a
  ``finally``.

The rule the task states, and the reason this exists: there must be no path where
``launch_headless()`` runs, the browser lives, and nobody is answerable for
shutting it down.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
import uuid
from typing import Any

from . import hh_browser_provider as bp

# Who is asking. Recorded, never inferred: a caller that does not declare itself is
# ``unknown``, which is the truth, not a guess dressed up as an answer.
OWNER_CLI = "cli"
OWNER_UI = "ui"
OWNER_AUTONOMOUS = "autonomous"
OWNER_WATCHER = "watcher"
OWNER_TOOL = "tool"
OWNER_UNKNOWN = "unknown"

OWNER_TYPES = (
    OWNER_CLI, OWNER_UI, OWNER_AUTONOMOUS, OWNER_WATCHER, OWNER_TOOL, OWNER_UNKNOWN,
)

LEASE_FILE = bp.STATE_DIR / "browser-lease.json"

# How long a heartbeat keeps a lease "fresh". A browser whose owner is alive but
# silent for longer than this is suspicious, not dead - see ``lease_state``.
FRESHNESS_SECONDS = 180.0

STATE_ABSENT = "ABSENT"
STATE_LIVE = "LIVE"
STATE_STALE = "STALE"
STATE_ACTIVE = "ACTIVE"

STATUS_ACQUIRED = "ACQUIRED"
STATUS_REUSED = "REUSED"
STATUS_BUSY = "BUSY"


class BrowserBusy(RuntimeError):
    """Another operation already holds the browser. Start nothing, wait or reuse."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def new_session_id(owner_type: str, owner_pid: int) -> str:
    return f"{owner_type}-{owner_pid}-{_stamp()}-{uuid.uuid4().hex[:6]}"


def new_lease(
    owner_type: str,
    owner_pid: int | None = None,
    profile: str | pathlib.Path | None = None,
    cdp_port: int | None = None,
    purpose: str = "",
) -> dict[str, Any]:
    """A fresh lease record. ``owner_pid`` defaults to this process - the truth."""
    pid = int(owner_pid if owner_pid is not None else os.getpid())
    owner = owner_type if owner_type in OWNER_TYPES else OWNER_UNKNOWN
    port = int(cdp_port or bp.HEADLESS_CDP_PORT)
    return {
        "session_id": new_session_id(owner, pid),
        "owner_type": owner,
        "owner_pid": pid,
        "started_at": _now(),
        "last_activity_at": _now(),
        "profile": str(profile or bp.headless_profile_dir()),
        "cdp_port": port,
        "cdp_url": f"http://127.0.0.1:{port}",
        "purpose": purpose,
        "operations": [],
    }


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def read_lease() -> dict[str, Any] | None:
    try:
        data = json.loads(LEASE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_lease(lease: dict[str, Any]) -> None:
    """Atomic write: a reader must never see half a lease."""
    try:
        bp.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LEASE_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(lease, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, LEASE_FILE)
    except OSError:
        pass  # bookkeeping must never be the thing that breaks a run


def clear_lease(session_id: str | None = None) -> bool:
    """Remove the lease - but only one we are entitled to remove.

    ``session_id`` is checked when given, so a late release from a session that has
    already been superseded cannot delete its replacement's lease.
    """
    current = read_lease()
    if current is None:
        return False
    if session_id is not None and current.get("session_id") != session_id:
        return False
    try:
        LEASE_FILE.unlink()
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# operations: crash-safe activity
# ---------------------------------------------------------------------------

def active_operations(lease: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Operations whose **process is still alive**.

    Not a counter. A counter incremented on acquire and decremented on release
    stays above zero forever after a crash, and then a dead owner looks busy - the
    exact failure this module is meant to remove. Liveness of the operation's own
    pid cannot get stuck.
    """
    ops = (lease or {}).get("operations") or []
    alive: list[dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        pid = int(op.get("pid") or 0)
        if pid > 0 and bp.pid_alive(pid):
            alive.append(op)
    return alive


def register_operation(
    session_id: str, owner_type: str, purpose: str = "", pid: int | None = None,
) -> dict[str, Any]:
    """Add this process as an active operation on the lease."""
    op = {
        "op_id": uuid.uuid4().hex[:12],
        "pid": int(pid if pid is not None else os.getpid()),
        "owner_type": owner_type if owner_type in OWNER_TYPES else OWNER_UNKNOWN,
        "purpose": purpose,
        "started_at": _now(),
    }
    lease = read_lease()
    if lease is None or lease.get("session_id") != session_id:
        return op  # the lease moved under us; the operation is still ours to hold
    lease["operations"] = active_operations(lease) + [op]
    lease["last_activity_at"] = _now()
    write_lease(lease)
    return op


def unregister_operation(op_id: str) -> None:
    lease = read_lease()
    if lease is None:
        return
    keep = [o for o in (lease.get("operations") or [])
            if isinstance(o, dict) and o.get("op_id") != op_id]
    lease["operations"] = keep
    lease["last_activity_at"] = _now()
    write_lease(lease)


def heartbeat(session_id: str | None = None) -> bool:
    """Say "still here" without claiming an operation."""
    lease = read_lease()
    if lease is None:
        return False
    if session_id is not None and lease.get("session_id") != session_id:
        return False
    lease["last_activity_at"] = _now()
    write_lease(lease)
    return True


# ---------------------------------------------------------------------------
# the stale rule
# ---------------------------------------------------------------------------

def _parse(value: str | None) -> float | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


def seconds_since(value: str | None, now: float | None = None) -> float | None:
    stamp = _parse(value)
    if stamp is None:
        return None
    return max(0.0, (now if now is not None else time.time()) - stamp)


def lease_state(
    lease: dict[str, Any] | None = None,
    now: float | None = None,
    freshness: float = FRESHNESS_SECONDS,
    instance_is_ours: bool | None = None,
) -> dict[str, Any]:
    """Is this lease live, active, stale - and *why*, fact by fact.

    ``stale`` needs all four facts the task names, and each one is reported
    separately so a report can show which held and which did not:

    1. the owner process is dead;
    2. no active operation (a live operation pid is activity, not a number);
    3. no fresh heartbeat;
    4. the instance is exactly our dedicated headless one.

    A dead parent on its own is **not** staleness. ``DETACHED_PROCESS`` means the
    launcher is *expected* to exit while the browser keeps working, so "parent
    dead" is the normal case, not the suspicious one.
    """
    if lease is None:
        lease = read_lease()
    if not lease:
        return {"state": STATE_ABSENT, "reasons": ["no lease recorded"],
                "stale": False, "owner_alive": False, "active_operations": 0,
                "heartbeat_age": None}

    owner_pid = int(lease.get("owner_pid") or 0)
    owner_alive = owner_pid > 0 and bp.pid_alive(owner_pid)
    ops = active_operations(lease)
    age = seconds_since(lease.get("last_activity_at"), now)
    fresh = age is not None and age <= freshness

    if instance_is_ours is None:
        port = int(lease.get("cdp_port") or bp.HEADLESS_CDP_PORT)
        info = bp.cdp_version(f"http://127.0.0.1:{port}")
        instance_is_ours = bool(info) and bp._is_ours(info)

    reasons: list[str] = []
    if owner_alive:
        reasons.append(f"owner pid {owner_pid} is alive")
    else:
        reasons.append(f"owner pid {owner_pid} is dead")
    if ops:
        reasons.append(f"{len(ops)} active operation(s)")
    else:
        reasons.append("no active operation")
    if fresh:
        reasons.append(f"heartbeat {age:.0f}s old (fresh)")
    else:
        reasons.append(
            f"heartbeat {age:.0f}s old (stale)" if age is not None else "no heartbeat"
        )
    if instance_is_ours:
        reasons.append("the instance is our dedicated headless Chrome")
    else:
        reasons.append("the instance is not ours (or is not running)")

    if ops:
        state = STATE_ACTIVE
    elif owner_alive or fresh:
        state = STATE_LIVE
    elif instance_is_ours:
        state = STATE_STALE
    else:
        state = STATE_LIVE  # nothing to reclaim: no live instance of ours

    return {
        "state": state,
        "stale": state == STATE_STALE,
        "reasons": reasons,
        "owner_pid": owner_pid,
        "owner_alive": owner_alive,
        "owner_type": lease.get("owner_type"),
        "active_operations": len(ops),
        "active_operation_detail": ops,
        "heartbeat_age": age,
        "heartbeat_fresh": fresh,
        "instance_is_ours": bool(instance_is_ours),
        "session_id": lease.get("session_id"),
    }


# ---------------------------------------------------------------------------
# acquire / release
# ---------------------------------------------------------------------------

def acquire(
    owner_type: str,
    purpose: str = "",
    profile: str | pathlib.Path | None = None,
    cdp_port: int | None = None,
    freshness: float = FRESHNESS_SECONDS,
) -> dict[str, Any]:
    """Claim the browser for one operation, or say why not.

    Three outcomes, and the middle one is the point of §9:

    * ``ACQUIRED`` - no live lease; this process becomes the owner;
    * ``REUSED`` - a live lease with no active operation; the existing session is
      adopted instead of starting a second Chrome on the same profile;
    * ``BUSY`` - a live lease with someone else's operation in flight. The caller
      must not start anything.
    """
    state = lease_state(freshness=freshness)
    if state["state"] in (STATE_ACTIVE, STATE_LIVE):
        lease = read_lease() or {}
        if state["state"] == STATE_ACTIVE:
            mine = [
                o for o in state["active_operation_detail"]
                if int(o.get("pid") or 0) == os.getpid()
            ]
            if not mine:
                return {
                    "ok": False, "status": STATUS_BUSY, "lease": lease, "state": state,
                    "reason": (
                        "another operation holds the browser: "
                        + ", ".join(
                            f"{o.get('owner_type')}/{o.get('purpose')} (pid {o.get('pid')})"
                            for o in state["active_operation_detail"]
                        )
                    ),
                }
        lease["last_activity_at"] = _now()
        write_lease(lease)
        op = register_operation(lease["session_id"], owner_type, purpose)
        return {"ok": True, "status": STATUS_REUSED, "lease": lease, "op": op,
                "state": state, "reason": "adopted a live session"}

    lease = new_lease(owner_type, profile=profile, cdp_port=cdp_port, purpose=purpose)
    write_lease(lease)
    op = register_operation(lease["session_id"], owner_type, purpose)
    return {"ok": True, "status": STATUS_ACQUIRED, "lease": lease, "op": op,
            "state": state, "reason": "no live session; this process owns the browser"}


def release(
    session_id: str | None = None,
    op_id: str | None = None,
    stop_browser: bool = True,
    profile: str | pathlib.Path | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Drop this operation; stop the browser only when nobody else is working.

    The ``finally`` half of the contract. Returns what it did, measured, because a
    release that only says "released" is the T7.22 mistake in a new place.

    ``session`` is the operation's own ``HeadlessBrowserSession``. It matters: a
    freshly constructed session owns no tabs, so closing *its* registry would close
    nothing and leak every tab the operation opened. When the session is given, its
    tabs are the ones closed; when it is not, the browser is still stopped, but the
    report says so instead of pretending the tabs were handled.
    """
    report: dict[str, Any] = {
        "session_id": session_id, "op_id": op_id, "stopped": False,
        "reason": "", "remaining_operations": 0, "tabs_closed": None,
        "tabs_owned_by_session": None, "tabs_seen_on_endpoint": None,
    }
    if op_id:
        unregister_operation(op_id)

    lease = read_lease()
    if lease is None:
        report["reason"] = "no lease left to release"
        return report

    remaining = active_operations(lease)
    report["remaining_operations"] = len(remaining)
    if remaining:
        report["reason"] = (
            f"{len(remaining)} operation(s) still active; leaving the browser up"
        )
        heartbeat(lease.get("session_id"))
        return report

    if not stop_browser:
        report["reason"] = "stop_browser=False; browser left running with no operation"
        return report

    if session is not None and getattr(session, "reused", False):
        # An attached session does not own the shared Chrome process. Close
        # only the tabs it created; another workload may be using the browser.
        try:
            report["tabs_owned_by_session"] = len(session.owned)
            tabs = session.close_all_owned_tabs()
            report["tabs_closed"] = tabs.get("closed_count")
            report["tabs_that_would_not_close"] = tabs.get("failed")
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        report["reason"] = "attached session released; shared browser left running"
        heartbeat(lease.get("session_id"))
        return report

    endpoint = lease.get("cdp_url") or bp.headless_cdp_url()
    if bp.endpoint_is_foreign_browser(endpoint):
        # T7.25 §2: a browser this operation did not start is not its to close.
        # The legacy BrowserOS endpoint (9110) is a *different* Chrome carrying
        # the user's own windows, and `stop()` quits it - so an operation that
        # was pointed at a foreign endpoint gives up its lease and leaves the
        # browser alone. The lease is still cleared: this operation is over, and
        # a lease nobody holds would make the next stale check lie.
        report["reason"] = (
            f"{endpoint} is a Chrome that is not ours; refusing to close it. "
            "The lease is cleared, the browser is left running"
        )
        report["refused_foreign_endpoint"] = endpoint
        clear_lease(lease.get("session_id"))
        return report

    try:
        if session is not None:
            owned = session
            report["tabs_owned_by_session"] = len(owned.owned)
            tabs = owned.close_all_owned_tabs()
            report["tabs_closed"] = tabs.get("closed_count")
            report["tabs_that_would_not_close"] = tabs.get("failed")
        else:
            # No registry to consult. Do not report "0 closed" as if that meant "nothing
            # to close" - enumerate what the endpoint actually carries, close it, and say
            # how many there were.
            seen = bp.list_tabs(endpoint)
            report["tabs_seen_on_endpoint"] = len(seen)
            report["tabs_closed"] = sum(
                1 for t in seen if t.get("id") and bp.close_tab(endpoint, t["id"])
            )
            owned = bp.HeadlessBrowserSession(
                cdp_url=endpoint, profile_dir=profile or lease.get("profile"),
            )

        shutdown = owned.stop()
    except Exception as exc:  # noqa: BLE001 - a teardown that raised still ends the operation
        # T7.25 §7/§12: a release that blows up must not turn into an exception in
        # the caller's ``finally``, where it would replace the real cause. It is
        # reported instead. The lease is deliberately **not** cleared here: the
        # browser may still be running, and a lease that names who is answerable
        # for it is worth more than a tidy file.
        report.update({
            "stopped": False,
            "error": f"{type(exc).__name__}: {exc}",
            "reason": "the teardown raised; the lease is kept because the browser may remain",
        })
        return report

    report.update({
        "stopped": True,
        "port_freed": shutdown.get("port_freed"),
        "profile_unlocked": shutdown.get("profile_unlocked"),
        "owned_processes": shutdown.get("owned_processes"),
        "status": shutdown.get("status"),
        "reason": "last operation finished; browser closed and the lease cleared",
    })
    clear_lease(lease.get("session_id"))
    return report


@contextlib.contextmanager
def hh_operation(
    owner_type: str,
    purpose: str = "",
    release_on_exit: bool = True,
    freshness: float = FRESHNESS_SECONDS,
    profile: str | pathlib.Path | None = None,
    cdp_port: int | None = None,
    session: Any = None,
    session_kwargs: dict[str, Any] | None = None,
):
    """One HH operation, with the browser's lifetime tied to it.

    This is the §4 rule made executable: the browser exists while an operation
    exists, and not one second longer. ``finally`` is what makes it true for a
    return, an exception and a Ctrl+C alike.

    Raises ``BrowserBusy`` rather than starting a second Chrome on the same
    profile - two browsers on one ``--user-data-dir`` is not a race that ends well.

    ``session`` is an optional already-built ``HeadlessBrowserSession``; the
    operation then uses it and the release closes *its* tabs. If none is given the
    handle carries a session the caller may start.

    ``session_kwargs`` tunes the session this function builds - ``require_auth``
    and ``cdp_url`` are the two callers actually use. They are passed straight
    through, so a read-only caller can keep its own auth handling and a caller
    with an explicit endpoint can keep that endpoint, without either of them
    reaching for a second ownership model.
    """
    claim = acquire(owner_type, purpose=purpose, profile=profile, cdp_port=cdp_port,
                    freshness=freshness)
    if not claim["ok"]:
        raise BrowserBusy(claim["reason"])

    op = claim["op"]
    if session is not None:
        owned = session
    else:
        kwargs: dict[str, Any] = dict(session_kwargs or {})
        kwargs.setdefault("cdp_url", claim["lease"].get("cdp_url"))
        kwargs.setdefault("profile_dir", profile)
        kwargs.setdefault("owner_type", owner_type)
        owned = bp.HeadlessBrowserSession(**kwargs)
    handle = {
        "owner_type": owner_type,
        "purpose": purpose,
        "status": claim["status"],
        "lease": claim["lease"],
        "op": op,
        "session": owned,
        "release_report": None,
    }
    try:
        heartbeat(claim["lease"]["session_id"])
        yield handle
    finally:
        try:
            handle["release_report"] = release(
                claim["lease"]["session_id"], op["op_id"],
                stop_browser=release_on_exit, profile=profile, session=owned,
            )
        except Exception as exc:  # noqa: BLE001 - a release may not replace the cause
            handle["release_report"] = {
                "stopped": False,
                "error": f"{type(exc).__name__}: {exc}",
            }


def release_owner(owner_type: str, **kwargs: Any) -> dict[str, Any]:
    """Release the browser on behalf of a long-lived owner that is going away.

    This is the §6 shutdown hook: a UI or daemon that is exiting gives up whatever
    it still holds. It touches **only** a lease whose ``owner_type`` is this
    owner's - a daemon shutting down has no business closing a browser that a CLI
    run is using, and "it was the last one alive" is not the same as "it was mine".
    """
    current = read_lease()
    if current is None:
        return {"released": False, "reason": "no lease to release",
                "owner_type": owner_type}
    if current.get("owner_type") != owner_type:
        return {"released": False, "owner_type": owner_type,
                "reason": (
                    f"the live lease belongs to {current.get('owner_type')!r}, "
                    f"not to {owner_type!r}"
                ),
                "lease_owner": current.get("owner_type")}
    report = release(current.get("session_id"), op_id=None, **kwargs)
    return {"released": bool(report.get("stopped")), "owner_type": owner_type,
            "report": report, "reason": report.get("reason")}


def owner_pid_alive(lease: dict[str, Any] | None) -> bool:
    pid = int((lease or {}).get("owner_pid") or 0)
    return pid > 0 and bp.pid_alive(pid)


def ownership_summary() -> dict[str, Any]:
    """One dict for a report: the lease, who is alive, and the stale verdict."""
    lease = read_lease()
    state = lease_state(lease)
    return {
        "lease": lease,
        "lease_file": str(LEASE_FILE),
        "lease_exists": lease is not None,
        "state": state,
        "owned_tabs": None,
        "browser_running": bp.port_is_listening(
            int((lease or {}).get("cdp_port") or bp.HEADLESS_CDP_PORT)
        ),
    }
