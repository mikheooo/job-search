"""T7.21: which browser the HH workflows drive, and how we start our own.

WHY THIS IS A SEPARATE MODULE
-----------------------------
``docs/ble001_triage.md`` cites ``hh_browser_launcher.py:229``, so that file is
edited by *appending only* (the reference must keep pointing at
``check_hh_session_authenticated``). Everything new about browser choice lives
here, next to ``autonomous_mode.py``, which was split out of the agent for
exactly the same reason.

THE TWO MODES
-------------
``HH_BROWSER_MODE`` names the browser, explicitly:

* ``headless`` (default) - our own Chrome, ``--headless=new``, the dedicated
  profile ``...\\JobSearchHeadless\\User Data``, CDP ``127.0.0.1:9111``. No
  window on the desktop, no dependence on the user having started anything.
* ``browseros`` - the historical path: whatever ``CDP_URL`` / ``HH_CDP_URL``
  says, else 9222, with the documented hop to BrowserOS on 9110. Kept as a
  fallback and for debugging.

There is deliberately **no** "is 9110 up?" probing here. Probing to choose a
browser swaps profiles (finding #13: BrowserOS is a different Chrome profile,
usually without the hh.ru session), so the choice is configuration, not luck.

WHY HEADLESS IS THE DEFAULT AND YET AN EXPLICIT PIN STILL WINS
--------------------------------------------------------------
An explicit endpoint is a decision, not silence - that is the whole of
``test_resolve_cdp_url_does_not_swap_when_env_pins_the_default``. So the order
is: explicit argument, then the generic ``CDP_URL`` pin, then the mode. In
``headless`` mode ``HH_CDP_URL`` is **not** consulted: that variable names the
BrowserOS address (``.env`` pins it to 9110) and reading it would drag every
headless workflow back onto BrowserOS.

AUTHENTICATION IS A GATE, NOT A GUESS
-------------------------------------
A stale profile is not an error to work around. It is ``HH_AUTH_REQUIRED`` with
a message a human can act on, and the runner stops - no auto-login, no cookie
copying, no borrowing the BrowserOS profile.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
try:
    import ctypes
except (ImportError, AttributeError):
    ctypes = None  # type: ignore[assignment]
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# --- mode vocabulary --------------------------------------------------------

MODE_ENV = "HH_BROWSER_MODE"
MODE_HEADLESS = "headless"
MODE_BROWSEROS = "browseros"
MODES: tuple[str, ...] = (MODE_HEADLESS, MODE_BROWSEROS)
DEFAULT_MODE = MODE_HEADLESS

# --- the dedicated headless instance ---------------------------------------

HEADLESS_CDP_PORT = 9111
HEADLESS_CDP_URL = f"http://127.0.0.1:{HEADLESS_CDP_PORT}"
HEADLESS_CDP_ENV = "HH_HEADLESS_CDP_URL"
HEADLESS_PROFILE_ENV = "HH_HEADLESS_PROFILE"
def _default_headless_profile_dir() -> str:
    env = os.environ.get(HEADLESS_PROFILE_ENV) or os.environ.get("HH_HEADLESS_PROFILE")
    if env and env.strip():
        return env
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return str(Path(local) / "JobSearchHeadless" / "User Data")
    return str(Path.home() / "AppData" / "Local" / "JobSearchHeadless" / "User Data")


HEADLESS_PROFILE_DIR = _default_headless_profile_dir()
HEADLESS_WINDOW_SIZE = "1440,900"

# The UA says HeadlessChrome/<version>. Used only to tell *our* instance from a
# stranger that happens to hold the port - never to fingerprint anything.
HEADLESS_UA_MARKER = "HeadlessChrome"

CDP_STARTUP_TIMEOUT = 30.0
CDP_SHUTDOWN_TIMEOUT = 20.0

HH_AUTH_REQUIRED = "HH_AUTH_REQUIRED"
HH_AUTH_REQUIRED_MESSAGE = "Нужен ручной вход в профиль JobSearchHeadless."
HH_AUTH_OK = "OK"
HH_PAGE_NOT_READY = "PAGE_NOT_READY"

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "artifacts" / "headless-poc"
STATE_FILE = STATE_DIR / "runner-state.json"
CHROME_LOG = STATE_DIR / "chrome-headless.log"

_WS_MAX_SIZE = 64 * 1024 * 1024


class UnknownBrowserMode(ValueError):
    """``HH_BROWSER_MODE`` held something that is not a mode.

    Refusing beats guessing: a typo that silently fell back to BrowserOS would
    drive the user's own profile from a "headless" run.
    """


class HHProfileBusy(RuntimeError):
    """The dedicated profile is already held by a live instance of ours."""


# ---------------------------------------------------------------------------
# mode and endpoints
# ---------------------------------------------------------------------------

def normalise_mode(value: Any) -> str:
    """Accept a plain string; anything else is not a mode."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def browser_mode() -> str:
    """The configured HH browser mode, or the default. Never probes anything."""
    raw = os.getenv(MODE_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_MODE
    mode = normalise_mode(raw)
    if mode not in MODES:
        raise UnknownBrowserMode(
            f"{MODE_ENV}={raw!r} is not one of {MODES}; "
            "refusing to guess which browser to drive"
        )
    return mode


def is_headless_mode() -> bool:
    return browser_mode() == MODE_HEADLESS


def headless_cdp_url() -> str:
    return (os.getenv(HEADLESS_CDP_ENV) or "").strip() or HEADLESS_CDP_URL


def headless_profile_dir() -> Path:
    raw = (os.getenv(HEADLESS_PROFILE_ENV) or "").strip()
    return Path(raw) if raw else Path(HEADLESS_PROFILE_DIR)


def resolve_browser_endpoint(preferred: str | None = None) -> str:
    """The one answer to "which CDP endpoint does this workflow drive?".

    Order (see the module docstring for why):
    1. an explicit argument that is not merely the legacy default;
    2. the generic ``CDP_URL`` pin - the operator's own choice of endpoint;
    3. the mode: ``headless`` -> our 9111, ``browseros`` -> the legacy resolver
       (``HH_CDP_URL`` / 9222, including the logged hop to 9110).
    """
    from .hh_browser_launcher import DEFAULT_HH_CDP_URL, cdp_url_pin, resolve_cdp_url

    mode = browser_mode()
    if mode == MODE_BROWSEROS:
        # The legacy resolver stays the owner of CDP_URL / HH_CDP_URL. It does
        # not hop ports unless HH_CDP_ALLOW_FALLBACK is set outside production.
        # In browseros mode, when explicitly selected without pinned CDP_URL/HH_CDP_URL,
        # fallback to BrowserOS endpoint (9110) is allowed if it is alive.
        try:
            return resolve_cdp_url(preferred, allow_browseros=True)
        except Exception:
            from .hh_browser_launcher import BROWSEROS_CDP_URL, CDP_ALIVE, probe_cdp
            if probe_cdp(BROWSEROS_CDP_URL) == CDP_ALIVE:
                return BROWSEROS_CDP_URL
            raise

    explicit = preferred if preferred and preferred != DEFAULT_HH_CDP_URL else None
    if explicit:
        return explicit
    pinned = cdp_url_pin()
    if pinned:
        return pinned
    return headless_cdp_url()


# ---------------------------------------------------------------------------
# launching
# ---------------------------------------------------------------------------

def headless_argv(
    chrome_exe: str,
    cdp_url: str | None = None,
    profile_dir: str | Path | None = None,
    window_size: str = HEADLESS_WINDOW_SIZE,
) -> list[str]:
    """The argv for our headless instance, as a list.

    A list, not a string: the profile path contains a space, and both a shell
    and PowerShell's ``Start-Process -ArgumentList`` split it (measured in
    T7.20 - a stray ``...\\JobSearchHeadless\\User`` directory was the
    fingerprint). Handing argv to ``subprocess`` as argv keeps it one argument.
    """
    endpoint = cdp_url or headless_cdp_url()
    port = urllib.parse.urlparse(endpoint).port or HEADLESS_CDP_PORT
    profile = Path(profile_dir) if profile_dir else headless_profile_dir()
    return [
        str(chrome_exe),
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={window_size}",
        "about:blank",
    ]


def find_headless_chrome() -> str | None:
    """Google Chrome from the standard locations. Never BrowserOS' bundled copy.

    The BrowserOS executable is deliberately *not* a candidate: falling back to
    it would put our headless run inside the user's BrowserClaw installation,
    which T7.21 forbids.
    """
    env_path = os.getenv("CHROME_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)")):
        if root:
            candidates.append(os.path.join(root, r"Google\Chrome\Application\chrome.exe"))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def port_is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Is something accepting TCP connections on this port?

    A socket probe, not ``curl``: curl goes through ``http_proxy`` and a dead
    upstream answers 502 *with exit code 0*, which T7.20 showed reads as
    "alive". An unanswerable probe returns False - the readiness wait after a
    launch is what turns that into a loud failure rather than a silent reuse.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    """Is this process still running? Query-only - never a signal.

    ``os.kill(pid, 0)`` is *not* usable on Windows: Python forwards any signal
    other than CTRL_C/CTRL_BREAK to TerminateProcess, so a liveness check would
    kill the process it asked about.
    """
    if pid <= 0:
        return False
    if os.name == "nt" and ctypes is not None and hasattr(ctypes, "windll"):
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        alive = bool(
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ) and exit_code.value == 259  # STILL_ACTIVE
        ctypes.windll.kernel32.CloseHandle(handle)
        return alive
    if os.name == "nt":
        # Windows fallback when ctypes is unavailable
        try:
            import subprocess
            out = subprocess.check_output(
                ["tasklist.exe", "/FI", f"PID eq {int(pid)}", "/NH"],
                timeout=5,
            ).decode("utf-8", errors="ignore")
            return str(int(pid)) in out
        except (subprocess.SubprocessError, OSError, ValueError) as e_tl:
            logger.debug("tasklist fallback failed: %s", e_tl)
            return False
    try:
        os.kill(pid, 0)
    except (OSError, SystemError):
        return False
    return True


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(data: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # a bookkeeping failure must not stop the run
        logger.debug("could not write %s: %s", STATE_FILE, exc)


def clear_state() -> None:
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def profile_in_use(profile_dir: str | Path | None = None) -> bool:
    """Is the dedicated profile held by an instance of ours?

    Windows Chrome leaves no ``SingletonLock`` (that is the POSIX mechanism -
    the profile directory here contains only ``Default/LOCK``, a LevelDB lock
    that is always present), so the authority is our own record: the PID the
    launcher wrote, still alive, plus the CDP port answering.
    """
    state = read_state()
    recorded = state.get("pid")
    if recorded and pid_alive(int(recorded)):
        return True
    endpoint = (state.get("cdp_url") or headless_cdp_url())
    port = urllib.parse.urlparse(endpoint).port or HEADLESS_CDP_PORT
    return port_is_listening(port)


def is_endpoint_reachable(endpoint: str, timeout: float = 1.5) -> bool:
    """Health check for an endpoint - not a routing decision.

    The three-way ``probe_cdp`` (alive / dead / timeout) stays the routing
    signal; this collapses it to a boolean the way a caller who already knows
    which browser it wants needs.
    """
    from .hh_browser_launcher import is_cdp_reachable

    return is_cdp_reachable(endpoint, timeout=timeout)


def cdp_version(endpoint: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """``/json/version`` of the endpoint, or None. Proxy-free by construction."""
    from .hh_browser_launcher import _NO_PROXY_OPENER

    try:
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/json/version",
            headers={"User-Agent": "job-search-runner"},
        )
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - unreachable is an answer, not a crash
        return None


def _is_ours(info: dict[str, Any] | None) -> bool:
    return bool(info) and HEADLESS_UA_MARKER.lower() in json.dumps(info).lower()


def endpoint_is_foreign_browser(endpoint: str | None, timeout: float = 2.0) -> bool:
    """True when a Chrome answers at ``endpoint`` and it is **not** ours.

    ``False`` covers both "it is ours" and "nothing is there" - the two cases
    where closing it is not somebody else's window. The release path asks this
    before quitting a browser: the legacy BrowserOS endpoint (9110) is a
    *different* Chrome carrying the user's own windows, and "the caller named
    that endpoint" is not the same as "this operation owns it".
    """
    if not endpoint:
        return False
    info = cdp_version(endpoint, timeout=timeout)
    return bool(info) and not _is_ours(info)


def _release_lease_after_stop(port: int, profile: str | Path) -> bool:
    """Drop the lease for this port+profile, and only for this one.

    A lease naming a different profile or port is somebody else's bookkeeping; a
    shutdown of our browser is not a reason to delete it.
    """
    try:
        from . import hh_browser_lease as lease

        existing = lease.read_lease()
        if existing is None:
            return False
        if int(existing.get("cdp_port") or 0) != int(port):
            return False
        if _normalise_path(existing.get("profile") or "") != _normalise_path(str(profile)):
            return False
        return lease.clear_lease(existing.get("session_id"))
    except Exception:  # bookkeeping must not break a shutdown
        logger.debug("could not clear the browser lease", exc_info=True)
        return False


def _register_lease_at_spawn(
    owner_type: str | None, profile: Path, port: int, purpose: str = "",
) -> dict[str, Any] | None:
    """Record who asked for this browser, at the only place that spawns one.

    T7.24. ``launch_headless`` is the choke point: every path that can start our
    Chrome goes through it, so registering the lease here is what makes "no
    unowned browser" true by construction rather than by convention. A caller
    that does not name itself is recorded as ``unknown`` - which is the truth.

    Lazy import: ``hh_browser_lease`` imports this module, so importing it at the
    top would be a cycle.
    """
    try:
        from . import hh_browser_lease as lease

        existing = lease.read_lease()
        state = lease.lease_state(existing, instance_is_ours=True)
        if existing and state["state"] in (lease.STATE_LIVE, lease.STATE_ACTIVE):
            existing["last_activity_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if owner_type and existing.get("owner_type") == lease.OWNER_UNKNOWN:
                existing["owner_type"] = owner_type
            lease.write_lease(existing)
            return existing
        fresh = lease.new_lease(
            owner_type or lease.OWNER_UNKNOWN, profile=str(profile), cdp_port=port,
            purpose=purpose,
        )
        lease.write_lease(fresh)
        return fresh
    except Exception:  # bookkeeping must not stop a launch
        logger.debug("could not register the browser lease", exc_info=True)
        return None


def launch_headless(
    cdp_url: str | None = None,
    profile_dir: str | Path | None = None,
    chrome_exe: str | None = None,
    open_url: str = "about:blank",
    timeout: float = CDP_STARTUP_TIMEOUT,
    owner_type: str | None = None,
) -> dict[str, Any]:
    """Start our own headless Chrome and wait until CDP answers.

    Refuses in three cases, loudly and without touching anything: the profile is
    held by a live instance of ours, the port is taken by something that is not
    a headless Chrome, or the executable is missing.

    T7.24: starting a browser also registers a **lease** naming the owner
    (``owner_type``/pid/profile/port). ``DETACHED_PROCESS`` below means the
    browser outlives the caller by design, so without a lease there would be no
    record of who is answerable for shutting it down.
    """
    endpoint = cdp_url or headless_cdp_url()
    port = urllib.parse.urlparse(endpoint).port or HEADLESS_CDP_PORT
    profile = Path(profile_dir) if profile_dir else headless_profile_dir()

    result: dict[str, Any] = {
        "ok": False, "started": False, "reused": False, "pid": None,
        "cdp_url": endpoint, "profile_dir": str(profile), "error": None,
    }

    existing = cdp_version(endpoint)
    if existing:
        if _is_ours(existing):
            result.update(ok=True, reused=True, version_info=existing)
            # T7.24: adopting an existing browser is also a claim on it. Registering
            # only on the spawn path would leave every *reuse* unowned - and reuse is
            # the common case once one instance is up.
            result["lease"] = _register_lease_at_spawn(owner_type, profile, port)
            return result
        result["error"] = (
            f"{endpoint} answers but is not a headless Chrome "
            f"({existing.get('Browser')!r}); refusing to drive or replace it"
        )
        return result

    if profile_in_use(profile):
        result["error"] = (
            f"the dedicated profile {profile} is already held by a live instance "
            f"(pid {read_state().get('pid')}); refusing to start a second one"
        )
        return result

    exe = chrome_exe or find_headless_chrome()
    if not exe:
        result["error"] = (
            "Google Chrome was not found in the standard locations; "
            "set CHROME_PATH to chrome.exe"
        )
        return result

    argv = headless_argv(exe, endpoint, profile)
    if open_url and open_url != "about:blank":
        argv[-1] = open_url

    creationflags = 0
    if os.name == "nt" and ctypes is not None and hasattr(ctypes, "windll"):
        creationflags = 0x00000200 | 0x00000008  # NEW_PROCESS_GROUP | DETACHED_PROCESS

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = CHROME_LOG.open("ab")
    except OSError:
        log = subprocess.DEVNULL  # type: ignore[assignment]

    try:
        proc = subprocess.Popen(
            argv,                       # argv as argv - the path has a space
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=creationflags,
            close_fds=os.name != "nt",
        )
    except OSError as exc:
        result["error"] = f"could not spawn Chrome: {type(exc).__name__}: {exc}"
        return result
    finally:
        if hasattr(log, "close"):
            log.close()  # the child keeps its own handle

    result["pid"] = proc.pid
    result["started"] = True
    write_state({"pid": proc.pid, "cdp_url": endpoint, "profile_dir": str(profile),
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    result["lease"] = _register_lease_at_spawn(owner_type, profile, port)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            result["error"] = (
                f"Chrome exited with code {proc.returncode} before CDP answered; "
                f"see {CHROME_LOG}"
            )
            return result
        info = cdp_version(endpoint, timeout=0.5)
        if info:
            result["version_info"] = info
            result["ok"] = True
            return result
        time.sleep(0.4)

    result["error"] = (
        f"Chrome started (pid {proc.pid}) but CDP {endpoint} did not answer "
        f"within {timeout}s; see {CHROME_LOG}"
    )
    return result


# ---------------------------------------------------------------------------
# transport helpers (the same CDP transport the project already uses)
# ---------------------------------------------------------------------------

def _run_sync(coro):
    """Run a coroutine from sync code, tolerating an already-running loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _ws_call(ws_url: str, method: str, params: dict[str, Any] | None = None,
             timeout: float = 20.0) -> dict[str, Any]:
    """One request/response over a page or browser WebSocket."""
    payload = {"id": 1, "method": method, "params": params or {}}

    async def _run() -> dict[str, Any]:
        import sys
        _p = r"C:\Users\Misha\.agent-reach-venv\Lib\site-packages"
        if _p not in sys.path:
            sys.path.insert(0, _p)
        import websockets

        async with websockets.connect(
            ws_url, max_size=_WS_MAX_SIZE, open_timeout=timeout, close_timeout=timeout
        ) as ws:
            await ws.send(json.dumps(payload))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout)
                msg = json.loads(raw)
                if msg.get("id") == payload["id"]:
                    return msg

    return _run_sync(_run())


def make_target_evaluate(endpoint: str, target_id: str) -> Callable[[str], str]:
    """An evaluate_fn bound to *one* tab, by target id.

    Deliberately not ``prefill_execute.make_cdp_evaluate(cdp_url, "hh.ru")``:
    that resolves the tab by URL substring and returns the first hh.ru match. With
    a search tab plus three vacancy tabs open, that is a read which silently
    answers about the wrong page - the class of mistake T7.19 was bitten by. The
    method is the same (``Runtime.evaluate``); only the addressing is exact.
    """
    def _evaluate(expression: str) -> str:
        tab = next((t for t in list_tabs(endpoint) if t.get("id") == target_id), None)
        if not tab or not tab.get("webSocketDebuggerUrl"):
            raise RuntimeError(f"tab {target_id} is gone")
        msg = _ws_call(
            tab["webSocketDebuggerUrl"],
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        result = (msg.get("result") or {}).get("result") or {}
        if "value" not in result:
            raise RuntimeError(f"unexpected evaluate result: {str(result)[:200]}")
        value = result["value"]
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return "" if value is None else str(value)

    return _evaluate


def open_tab(endpoint: str, url: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """Open a tab via ``/json/new`` - the same call the existing adapters use."""
    from .hh_browser_launcher import _NO_PROXY_OPENER

    target = f"{endpoint.rstrip('/')}/json/new?{url}"
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(target, method=method)
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - older Chromium wants GET
            continue
    return None


def close_tab(endpoint: str, target_id: str, timeout: float = 5.0) -> bool:
    """Close one tab. The pattern is the one ``CDPBrowserAdapter.close()`` uses."""
    from .hh_browser_launcher import _NO_PROXY_OPENER

    try:
        _NO_PROXY_OPENER.open(
            f"{endpoint.rstrip('/')}/json/close/{target_id}", timeout=timeout
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def list_tabs(endpoint: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    from .hh_browser_launcher import get_cdp_targets

    return get_cdp_targets(endpoint, timeout=timeout)


def screenshot(endpoint: str, target_id: str, out_path: str | Path,
               timeout: float = 30.0) -> dict[str, Any]:
    """``Page.captureScreenshot`` on one tab, written to ``out_path``."""
    tab = next((t for t in list_tabs(endpoint) if t.get("id") == target_id), None)
    if not tab or not tab.get("webSocketDebuggerUrl"):
        return {"ok": False, "error": f"no websocket for target {target_id}"}
    try:
        msg = _ws_call(
            tab["webSocketDebuggerUrl"],
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    data = (msg.get("result") or {}).get("data")
    if not data:
        return {"ok": False, "error": f"no screenshot data: {str(msg)[:200]}"}
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(data))
    return {"ok": True, "path": str(path), "bytes": path.stat().st_size}


def quit_browser(endpoint: str, timeout: float = 2.0) -> bool:
    """Ask the whole browser to shut down.

    ``Browser.close`` tears the connection down as it exits, so this sends and
    returns without waiting for a reply - waiting hangs on a socket that is
    going away (measured in T7.20).
    """
    info = cdp_version(endpoint)
    ws_url = (info or {}).get("webSocketDebuggerUrl")
    if not ws_url:
        return False

    async def _run() -> None:
        import websockets

        try:
            async with websockets.connect(ws_url, max_size=_WS_MAX_SIZE) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
                try:
                    await asyncio.wait_for(ws.recv(), timeout=timeout)
                except Exception:  # noqa: BLE001 - the socket dies with the process
                    pass
        except Exception:  # noqa: BLE001
            pass

    try:
        _run_sync(_run())
    except Exception:  # noqa: BLE001
        pass
    return True


def wait_port_free(port: int, timeout: float = CDP_SHUTDOWN_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_is_listening(port):
            return True
        time.sleep(0.3)
    return not port_is_listening(port)


# ---------------------------------------------------------------------------
# the authentication gate
# ---------------------------------------------------------------------------

# Evidence, not a vibe. Every branch below reads the *page*, and the decision is
# made in Python so it can be tested without a browser. The shapes are the ones
# measured in T7.20: an unauthenticated visitor gets no redirect - a 200 with a
# rendered login card (394x636, height > 0) - so "no redirect" proves nothing.
_AUTH_JS = """(() => {
  try {
    const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0);
    const card = document.querySelector('[data-qa="applicant-login-card"]');
    const phone = document.querySelector('[data-qa="applicant-login-input-phone"]');
    const profileLink = document.querySelector('[data-qa="mainmenu_profile-link"]');
    const applicantUi = document.querySelector(
      '[data-qa="applicant-profile"], [data-qa="resume-list"], [data-qa="resume-title"], ' +
      'a[href*="/applicant/resumes"], [data-qa="mainmenu_applicantProfile"]'
    );
    const h1 = document.querySelector('h1');
    const cookies = (document.cookie || '').split(';').map(s => s.trim()).filter(Boolean);
    const role = cookies.find(s => s.startsWith('hhrole=')) || '';
    const names = cookies.map(s => s.split('=')[0]);
    return JSON.stringify({
      url: location.href,
      path: location.pathname,
      login_card_visible: visible(card),
      login_input_present: !!phone,
      applicant_ui_present: !!applicantUi,
      profile_link_href: profileLink ? profileLink.getAttribute('href') : null,
      h1: h1 ? h1.innerText.trim().slice(0, 120) : null,
      hhrole: role ? role.split('=')[1] : null,
      cookie_names: names,
      ready: document.readyState
    });
  } catch (e) {
    return JSON.stringify({error: String(e && e.message || e)});
  }
})()"""


def decide_authentication(evidence: dict[str, Any]) -> dict[str, Any]:
    """Turn page evidence into an answer. Pure - no browser, no I/O.

    Fail-closed: anything that is not positive evidence of a logged-in
    applicant reads as "not authenticated", including a page that could not be
    read at all.
    """
    if not isinstance(evidence, dict) or evidence.get("error"):
        return {
            "authenticated": False, "verified": False,
            "reason": f"the page could not be read: {str(evidence)[:200]}",
            "evidence": evidence,
        }

    if evidence.get("login_card_visible") or evidence.get("login_input_present"):
        return {
            "authenticated": False, "verified": True,
            "reason": "the HH login card is rendered on the page",
            "evidence": evidence,
        }

    href = evidence.get("profile_link_href") or ""
    if "account/login" in href:
        return {
            "authenticated": False, "verified": True,
            "reason": f"the profile menu points at the login page ({href})",
            "evidence": evidence,
        }

    role = evidence.get("hhrole")
    if role and str(role).lower() == "anonymous":
        return {
            "authenticated": False, "verified": True,
            "reason": "hhrole=anonymous",
            "evidence": evidence,
        }

    if evidence.get("applicant_ui_present"):
        return {
            "authenticated": True, "verified": True,
            "reason": "applicant UI is rendered and no login card is present",
            "evidence": evidence,
        }

    return {
        "authenticated": False, "verified": False,
        "reason": "no applicant UI and no login card: the page proves nothing either way",
        "evidence": evidence,
    }


def check_hh_authentication(evaluate_fn: Callable[[str], str]) -> dict[str, Any]:
    """Read the applicant page and answer "is this session usable?"."""
    try:
        raw = evaluate_fn(_AUTH_JS)
        evidence = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:  # noqa: BLE001
        return {
            "authenticated": False, "verified": False,
            "reason": f"the page could not be read: {type(exc).__name__}: {exc}",
            "evidence": None,
        }
    return decide_authentication(evidence)


def auth_gate(auth: dict[str, Any]) -> dict[str, Any]:
    """Wrap a decision into OK / ``HH_AUTH_REQUIRED`` with an actionable message."""
    if auth.get("authenticated"):
        return {"status": HH_AUTH_OK, "message": "", "auth": auth}
    if not auth.get("verified"):
        reason = auth.get("reason") or "page unverified or unrendered"
        return {
            "status": HH_PAGE_NOT_READY,
            "message": f"HH applicant page unverified: {reason}",
            "auth": auth,
        }
    return {
        "status": HH_AUTH_REQUIRED,
        "message": HH_AUTH_REQUIRED_MESSAGE,
        "auth": auth,
    }


def require_hh_auth(evaluate_fn: Callable[[str], str]) -> dict[str, Any]:
    """The gate itself: OK, or ``HH_AUTH_REQUIRED`` with an actionable message."""
    gate = auth_gate(check_hh_authentication(evaluate_fn))
    if gate.get("status") == HH_PAGE_NOT_READY:
        return {
            "status": HH_AUTH_REQUIRED,
            "message": gate.get("message") or HH_AUTH_REQUIRED_MESSAGE,
            "auth": gate.get("auth"),
        }
    return gate


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------

AUTH_URL = "https://hh.ru/applicant/profile/me"

# Deliberately cookie-free: this is the expression used *before* a real document has
# loaded, and on ``about:blank`` any read of ``document.cookie`` throws.
_READY_JS = "JSON.stringify({href: location.href, ready: document.readyState})"


def settle_hh_page(
    evaluate_fn: Callable[[str], str],
    url: str = AUTH_URL,
    attempts: int = 10,
    delay: float = 0.6,
    ready_timeout: float = 25.0,
) -> dict[str, Any] | None:
    """Point a tab at ``url``, wait for the document, then read it.

    Two separate waits, and both are needed:

    1. **Load.** ``readyState == "complete"`` *and* the URL has moved off the page we
       were on. ``readyState`` alone is a lie - a fresh tab is already complete on
       ``about:blank`` - so a poll that only checks it reads the old document. The
       session used to get this from ``navigate()``; when T7.22 moved the gate into
       this helper, that wait was dropped and the gate answered "the page proves
       nothing either way" for a session that was fine.
    2. **Render.** HH paints the applicant UI after load, so evidence is polled for
       separately.

    Reading straight after a tab appears is worse than misleading: on ``about:blank``
    ``document.cookie`` is denied outright, so the read errors rather than merely
    answering wrong. Measured twice - once in T7.21's session gate, once in
    ``ensure_headless_browser``, which kept the single-read shape.
    """
    try:
        before = json.loads(evaluate_fn(_READY_JS)).get("href") or ""
    except Exception:  # noqa: BLE001 - an unreadable tab has no URL yet
        before = ""

    try:
        evaluate_fn(f"location.href = {json.dumps(url)}")
    except Exception:  # noqa: BLE001 - the assignment can land mid-navigation
        pass

    needs_change = before != url
    deadline = time.time() + ready_timeout
    loaded = False
    while time.time() < deadline:
        try:
            state = json.loads(evaluate_fn(_READY_JS))
        except Exception:  # noqa: BLE001 - mid-navigation reads fail
            state = None
        if (
            isinstance(state, dict)
            and state.get("ready") in ("complete", "interactive")
            and state.get("href") not in ("", "about:blank")
            and (not needs_change or state.get("href") != before)
        ):
            if state.get("ready") == "complete" or (deadline - time.time()) < 8.0:
                loaded = True
                break
        time.sleep(delay)

    # Do not classify the previous tab after navigation failed or timed out.
    # Its login card (or applicant UI) describes the old document, not the
    # requested authentication check.
    if not loaded:
        return None

    last: dict[str, Any] | None = None
    for _ in range(max(1, attempts)):
        try:
            candidate = json.loads(evaluate_fn(_AUTH_JS))
        except Exception as exc:  # noqa: BLE001 - a mid-navigation read fails
            candidate = {"error": f"{type(exc).__name__}: {exc}"}
        if isinstance(candidate, dict) and not candidate.get("error"):
            last = candidate
            if "apply_agreement" in str(candidate.get("path") or ""):
                try:
                    evaluate_fn('(()=>{const b=document.querySelector("[data-qa=account-form-apply-agreement-apply]");if(b)b.click();})()')
                    time.sleep(2.0)
                except Exception:
                    pass
            if (
                candidate.get("login_card_visible")
                or candidate.get("applicant_ui_present")
                or candidate.get("profile_link_href")
                or str(candidate.get("path") or "").startswith("/applicant")
            ):
                return candidate
        time.sleep(delay)
    return last


# ---------------------------------------------------------------------------
# T7.23: tab ownership
# ---------------------------------------------------------------------------

# What a tab was opened *for*. Recorded so a leak can be read, not just counted:
# "three tabs left" is a number, "three vacancy tabs left" is a bug report.
PURPOSE_WORK = "work"
PURPOSE_VACANCY = "vacancy"
PURPOSE_FORM = "form"
PURPOSE_SEARCH = "search"
PURPOSE_AUTH = "auth"
PURPOSE_EXTRA = "extra"


@dataclass
class OwnedTab:
    """One tab this session opened, and why.

    Ownership is recorded at creation, never inferred later. Inferring it (by URL
    substring, or by "it was there when we started") is how a runner ends up
    closing the user's own tab: with several hh.ru tabs open, "the hh.ru tab" is
    not a well-defined object.
    """

    target_id: str
    purpose: str
    url: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "purpose": self.purpose,
            "url": self.url,
            "created_at": self.created_at,
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class HeadlessBrowserSession:
    """One headless Chrome, one reused working tab, closed on the way out.

    The point of the class is hygiene (T7.21 section 6): T7.20 left 20 Chrome
    processes and 2.6 GB behind because every probe opened a tab and nothing
    closed them. Here every tab this session opens is remembered and closed -
    ``tab()`` closes its own, ``stop()`` closes the rest and then the browser.

    T7.23 tightened this from "remember the ids" to **ownership with provenance**:
    every tab is registered with a purpose, a URL and a timestamp, foreign tabs are
    never closed, and one working tab is reused across search -> vacancy -> form
    instead of a tab per vacancy.
    """

    def __init__(
        self,
        cdp_url: str | None = None,
        profile_dir: str | Path | None = None,
        chrome_exe: str | None = None,
        open_url: str = "about:blank",
        require_auth: bool = True,
        startup_timeout: float = CDP_STARTUP_TIMEOUT,
        auth_attempts: int = 10,
        owner_type: str | None = None,
    ):
        self.cdp_url = cdp_url or headless_cdp_url()
        self.profile_dir = Path(profile_dir) if profile_dir else headless_profile_dir()
        self.chrome_exe = chrome_exe
        self.open_url = open_url
        self.require_auth = require_auth
        self.startup_timeout = startup_timeout
        self.auth_attempts = auth_attempts
        # T7.24: who is answerable for the browser this session adopts. Recorded on
        # the lease at ``start()`` - a session that starts or reuses a browser without
        # naming an owner would be exactly the unowned-browser hole this task closes.
        self.owner_type = owner_type

        self.pid: int | None = None
        self.started = False
        self.reused = False
        self.owned: list[OwnedTab] = []
        self.work_tab: str | None = None
        self.status = "NOT_STARTED"
        self.message = ""
        self.lease: dict[str, Any] | None = None

    @property
    def opened_tabs(self) -> list[str]:
        """The ids of the tabs we own. Kept as a property so the T7.21 callers and
        tests that read ``session.opened_tabs`` keep working; the authority is now
        ``self.owned``, which also carries why each tab exists."""
        return [tab.target_id for tab in self.owned]

    @opened_tabs.setter
    def opened_tabs(self, target_ids: list[str]) -> None:
        """Seed the registry from bare ids.

        T7.21's tests assign ``opened_tabs`` directly to set up a teardown. The ids
        carry no provenance, so the entries are marked as such rather than invented
        as ``work`` - a registry that guesses a purpose is worse than one that admits
        it does not know.
        """
        self.owned = [
            OwnedTab(target_id=str(tid), purpose=PURPOSE_EXTRA, url="", created_at=_now())
            for tid in target_ids
        ]

    def register_tab(self, target_id: str, purpose: str = PURPOSE_EXTRA,
                     url: str = "") -> OwnedTab:
        """Take ownership of a tab we just opened. Never called for a foreign tab."""
        tab = OwnedTab(target_id=target_id, purpose=purpose, url=url, created_at=_now())
        self.owned.append(tab)
        return tab

    def ownership_registry(self) -> list[dict[str, Any]]:
        """The ownership registry, as data - for a report or a leak check."""
        return [tab.to_dict() for tab in self.owned]

    # -- lifecycle ---------------------------------------------------------

    def heartbeat(self) -> bool:
        """Say "this session is still in use", so a stale check stays honest."""
        try:
            from . import hh_browser_lease as lease

            current = lease.read_lease()
            if current is None:
                return False
            port = urllib.parse.urlparse(self.cdp_url).port or HEADLESS_CDP_PORT
            if int(current.get("cdp_port") or 0) != int(port):
                return False
            return lease.heartbeat(current.get("session_id"))
        except Exception:  # a heartbeat is not worth failing a run
            logger.debug("could not heartbeat the browser lease", exc_info=True)
            return False

    def start(self) -> dict[str, Any]:
        """Launch (or attach), open the working tab, and check authentication."""
        launch = launch_headless(
            cdp_url=self.cdp_url,
            profile_dir=self.profile_dir,
            chrome_exe=self.chrome_exe,
            open_url=self.open_url,
            timeout=self.startup_timeout,
            owner_type=self.owner_type,
        )
        self.started = bool(launch.get("started"))
        self.reused = bool(launch.get("reused"))
        self.pid = launch.get("pid")
        # T7.24: the lease is the record of who is answerable for this browser.
        self.lease = launch.get("lease")

        if not launch.get("ok"):
            self.status = "LAUNCH_FAILED"
            self.message = str(launch.get("error") or "unknown launch failure")
            return {"ok": False, "status": self.status, "message": self.message,
                    "launch": launch}

        self.status = "RUNNING"
        self.heartbeat()
        if self.require_auth:
            gate = self.check_auth()
            if gate["status"] == HH_AUTH_REQUIRED:
                if self.reused:
                    self.close_all_owned_tabs()
                else:
                    self.stop()
                self.status = HH_AUTH_REQUIRED
                self.message = gate["message"]
                return {"ok": False, "status": HH_AUTH_REQUIRED,
                        "message": gate["message"], "auth": gate["auth"],
                        "launch": launch}
            elif gate["status"] != HH_AUTH_OK:
                if self.reused:
                    self.close_all_owned_tabs()
                else:
                    self.stop()
                self.status = gate["status"]
                self.message = gate["message"]
                return {"ok": False, "status": gate["status"],
                        "message": gate["message"], "auth": gate["auth"],
                        "launch": launch}
            return {"ok": True, "status": "RUNNING", "auth": gate["auth"],
                    "launch": launch}
        return {"ok": True, "status": "RUNNING", "auth": None, "launch": launch}

    def check_auth(self) -> dict[str, Any]:
        """Open the applicant page in the working tab and run the gate.

        The page is *waited for* before it is believed. Reading straight after
        ``/json/new`` sees an empty document, and an empty document is exactly
        what a logged-out profile looks like - the gate would answer
        HH_AUTH_REQUIRED for a session that is perfectly fine (measured: the
        first live run of this code did precisely that).
        """
        target = self.ensure_work_tab()
        if target is None:
            return auth_gate({"authenticated": False, "verified": False,
                              "reason": "could not open the applicant page",
                              "evidence": None})
        evidence = settle_hh_page(
            lambda js: self.evaluate_on(target, js), attempts=self.auth_attempts
        )
        if evidence is None:
            return auth_gate({"authenticated": False, "verified": False,
                              "reason": "the applicant page never rendered",
                              "evidence": None})
        return auth_gate(decide_authentication(evidence))

    def stop(self) -> dict[str, Any]:
        """Close our tabs, then our browser, then prove both are gone.

        T7.23: the four verification steps the task asks for are run here, in order,
        and their results returned. A shutdown that only *asks* the browser to close
        and reports ``port_freed`` is a shutdown that cannot tell "closed" from
        "still there but quiet".
        """
        tabs = self.close_all_owned_tabs()
        port = urllib.parse.urlparse(self.cdp_url).port or HEADLESS_CDP_PORT
        quit_browser(self.cdp_url)
        freed = wait_port_free(port)
        state_record_matched: bool | None = None
        if freed:
            # Record the stop instead of deleting the file. Deleting is not
            # needed - profile_in_use() already treats a dead PID as "free" - and
            # a bookkeeping deletion must never be the thing that breaks a
            # shutdown (measured: a sandbox that blocks unlink turned a clean
            # stop into exit code 1 with no report).
            #
            # T7.24: ``runner-state.json`` is ONE file shared by every instance, so
            # a session stopping on its own port must not mark *another* instance
            # stopped. Measured 29.09: the record for the browser started at 11:26
            # (root pid 26152) carried ``pid: null, stopped_at: 12:20:24`` while that
            # browser was still listening - the stop of something else overwrote it.
            # Only write when the record is this session's; a mismatch is reported
            # rather than silently dropped.
            state = read_state()
            recorded_url = str(state.get("cdp_url") or "")
            recorded_profile = str(state.get("profile_dir") or "")
            state_record_matched = (
                (not recorded_url or recorded_url == self.cdp_url)
                and (
                    not recorded_profile
                    or _normalise_path(recorded_profile)
                    == _normalise_path(str(self.profile_dir))
                )
            )
            if state_record_matched:
                state.update({"pid": None, "stopped_at": _now()})
                write_state(state)
        self.work_tab = None

        profile = self.verify_profile_unlocked()
        processes = self.verify_no_owned_processes()
        self.status = "STOPPED" if (freed and profile["unlocked"]) else "STOP_TIMEOUT"
        # T7.24: a browser that is really gone must not leave a lease behind, or the
        # next stale check would report a live owner for a process that no longer
        # exists. Only a lease pointing at *this* port and profile is dropped.
        lease_cleared = False
        if self.status == "STOPPED":
            lease_cleared = _release_lease_after_stop(port, self.profile_dir)
        return {
            "closed_tabs": tabs["closed_count"],
            "closed_tab_ids": tabs["closed"],
            "tabs_that_would_not_close": tabs["failed"],
            "port": port,
            "port_freed": freed,
            "profile_unlocked": profile["unlocked"],
            "profile": profile,
            "owned_processes": processes["count"],
            "owned_processes_detail": processes["processes"],
            "no_owned_processes": processes["count"] == 0,
            "lease_cleared": lease_cleared,
            "state_record_matched": state_record_matched,
            "status": self.status,
        }

    def verify_profile_unlocked(self) -> dict[str, Any]:
        """Is the dedicated profile free? Three independent ways of saying no."""
        port = urllib.parse.urlparse(self.cdp_url).port or HEADLESS_CDP_PORT
        recorded = read_state().get("pid")
        recorded_alive = bool(recorded) and pid_alive(int(recorded))
        port_busy = port_is_listening(port)
        holders = dedicated_profile_processes(self.profile_dir)
        return {
            "profile_dir": str(self.profile_dir),
            "recorded_pid": recorded,
            "recorded_pid_alive": recorded_alive,
            "port": port,
            "port_listening": port_busy,
            "profile_holder_pids": [p["pid"] for p in holders],
            "unlocked": not recorded_alive and not port_busy and not holders,
        }

    def verify_no_owned_processes(self) -> dict[str, Any]:
        """How many processes still hold the dedicated profile."""
        holders = dedicated_profile_processes(self.profile_dir)
        return {"count": len(holders), "processes": holders}

    # -- tabs --------------------------------------------------------------

    def ensure_work_tab(self, url: str | None = None,
                        purpose: str = PURPOSE_WORK) -> str | None:
        """The one tab this session reads through. Created once, reused after.

        One tab, not one per vacancy: the workflow is search -> vacancy -> form ->
        next vacancy, and every step can reuse the same tab. A tab per vacancy is
        how a ten-vacancy scan ends with ten tabs the runner has to remember.
        """
        if self.work_tab is None:
            target = open_tab(self.cdp_url, url or self.open_url)
            if not target:
                return None
            self.work_tab = target.get("id")
            if self.work_tab:
                self.register_tab(self.work_tab, purpose, url or self.open_url)
        elif url:
            self.navigate(self.work_tab, url)
        return self.work_tab

    def open_hh_page(self, url: str | None = None, timeout: float = 25.0) -> str | None:
        """Open an HH page in the work tab and wait for the new document.

        ``ensure_work_tab`` alone is not enough to read from. A fresh tab is
        already ``readyState == "complete"`` on ``about:blank``, so a read taken
        straight after the open sees an empty document and reports "no
        conversations" instead of "not loaded yet". Measured on ``hh.ru/chat``:
        ``about:blank`` with ``body_len 0`` immediately after the open, and
        ``1 ・ Чаты`` eight seconds later on the same tab.

        ``navigate`` is the two-condition wait the runner and the verifier already
        use - complete *and* the URL moved - so this is that idiom, named once,
        for the callers whose whole job is to read a page. Ownership is unchanged:
        it is still the session's own work tab, and ``stop()`` still closes it.
        """
        page = url or self.open_url
        target = self.ensure_work_tab(page)
        if target is None:
            return None
        self.navigate(target, page, timeout=timeout)
        return target

    def repurpose_work_tab(self, purpose: str, url: str = "") -> None:
        """Note what the reused tab is being used for now.

        The registry is a trail, not a ledger: reusing the working tab for a vacancy
        after a search should read as "one tab, three jobs", so the entry is updated
        rather than a second entry invented.
        """
        if not self.work_tab:
            return
        for tab in self.owned:
            if tab.target_id == self.work_tab:
                tab.purpose = purpose
                if url:
                    tab.url = url
                return

    def navigate(self, target_id: str, url: str, timeout: float = 25.0) -> dict[str, Any]:
        """Point a tab at a URL and wait until the *new* document has settled.

        Two conditions, both required. ``readyState == "complete"`` alone is not
        enough: a fresh tab is already complete on ``about:blank``, so the wait
        returns immediately with the old document and the next read answers about
        an empty page - measured, that is exactly what happened on the first live
        run of this runner (a vacancy page that renders perfectly read back as
        all-null). The second condition is that the URL *changed* - not that it
        equals the requested one, because HH redirects (``/applicant/resumes``
        lands on ``/applicant/profile/me``).
        """
        try:
            before = json.loads(self.evaluate_on(
                target_id, "JSON.stringify({url: location.href})"
            )).get("url") or ""
        except Exception:  # noqa: BLE001 - a tab we cannot read yet has no URL
            before = ""

        self.evaluate_on(target_id, f"location.href = {json.dumps(url)}; 'navigating'")

        deadline = time.time() + timeout
        state: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                state = json.loads(self.evaluate_on(
                    target_id,
                    "JSON.stringify({url: location.href, ready: document.readyState})",
                ))
            except Exception:  # noqa: BLE001 - mid-navigation reads fail by design
                continue
            href = state.get("url") or ""
            if (state.get("ready") == "complete" and href
                    and href != before and href != "about:blank"):
                return state
        return {"timeout": True, **state}

    def open_extra_tab(self, url: str, purpose: str = PURPOSE_EXTRA) -> str | None:
        """A deliberately separate tab. Remembered, so stop() closes it."""
        target = open_tab(self.cdp_url, url)
        if not target:
            return None
        target_id = target.get("id")
        if target_id:
            self.register_tab(target_id, purpose, url)
        return target_id

    def close_tab(self, target_id: str) -> bool:
        """Close one of our tabs and forget it, so stop() cannot close it twice."""
        ok = close_tab(self.cdp_url, target_id)
        self.owned = [tab for tab in self.owned if tab.target_id != target_id]
        if target_id == self.work_tab:
            self.work_tab = None
        return ok

    def close_all_owned_tabs(self) -> dict[str, Any]:
        """Close every tab we own, and only those.

        Returns the closed ids and the ones that would not close, because a tab that
        survives its close is the leak this method exists to catch - reporting only
        a count would hide it.
        """
        closed: list[str] = []
        failed: list[str] = []
        for tab in list(self.owned):
            if close_tab(self.cdp_url, tab.target_id):
                closed.append(tab.target_id)
            else:
                failed.append(tab.target_id)
        self.owned = [tab for tab in self.owned if tab.target_id in failed]
        self.work_tab = None
        return {"closed": closed, "failed": failed,
                "closed_count": len(closed), "failed_count": len(failed)}

    # T7.21 called this close_all_tabs(); the name is kept so the old callers and
    # tests keep working. It now delegates, so there is one implementation.
    def close_all_tabs(self) -> int:
        return self.close_all_owned_tabs()["closed_count"]

    def foreign_tabs(self) -> list[dict[str, Any]]:
        """Tabs on this endpoint that we do **not** own.

        Reported, never closed. On a dedicated profile this is usually iframes,
        service workers and browser UI - but the rule does not depend on that being
        true, because the day it is not true is the day we would close someone's
        window.
        """
        mine = {tab.target_id for tab in self.owned}
        return [t for t in list_tabs(self.cdp_url) if t.get("id") not in mine]

    # T7.21's name for the same question; kept for its callers.
    def leftover_tabs(self) -> list[dict[str, Any]]:
        return self.foreign_tabs()

    # -- reads -------------------------------------------------------------

    def evaluate(self, expression: str) -> str:
        """Evaluate in the working tab - the main-frame transport."""
        target_id = self.ensure_work_tab()
        if target_id is None:
            raise RuntimeError("no working tab in the headless session")
        return self.evaluate_on(target_id, expression)

    def evaluate_on(self, target_id: str, expression: str) -> str:
        """Evaluate in one named tab, addressed by target id (see
        ``make_target_evaluate`` for why the URL-substring resolver is not used)."""
        return make_target_evaluate(self.cdp_url, target_id)(expression)

    def capture(self, out_path: str | Path, target_id: str | None = None) -> dict[str, Any]:
        target = target_id or self.ensure_work_tab()
        if target is None:
            return {"ok": False, "error": "no tab to capture"}
        return screenshot(self.cdp_url, target, out_path)


# ---------------------------------------------------------------------------
# the launcher primitive, shaped like ensure_hh_browser()
# ---------------------------------------------------------------------------

def ensure_headless_browser(
    cdp_url: str | None = None,
    profile_dir: str | Path | None = None,
    chrome_exe: str | None = None,
    open_url: str = "https://hh.ru/chat",
    auto_start: bool = True,
    timeout_seconds: float = CDP_STARTUP_TIMEOUT,
    check_session: bool = True,
    owner_type: str | None = None,
) -> dict[str, Any]:
    """Start our headless Chrome if needed and make sure an HH tab is open.

    The return shape is the one ``ensure_hh_browser`` has always returned, so
    every existing caller keeps working unchanged. ``authenticated`` is filled
    in from the same page evidence the gate uses; it is *reported* here and
    *enforced* by ``require_hh_auth``/``HeadlessBrowserSession``, so a caller
    that only wants the browser still gets one.

    T7.24: this is the legacy shape - it hands back a browser and takes no
    responsibility for it. Ownership therefore has to be recorded here too, and
    ``owner_type`` is how a caller says who it is. It is threaded to the lease,
    not used to decide anything, so a caller that says nothing is recorded as
    ``unknown`` rather than guessed at.
    """
    endpoint = cdp_url or headless_cdp_url()
    profile = Path(profile_dir) if profile_dir else headless_profile_dir()

    result: dict[str, Any] = {
        "ok": False,
        "cdp_url": endpoint,
        "reused": False,
        "pid": None,
        "profile_dir": str(profile),
        "version_info": None,
        "target": None,
        "authenticated": True,
        "auth": None,
        "error": None,
    }

    info = cdp_version(endpoint)
    if info and _is_ours(info):
        result.update(reused=True, version_info=info)
        # Reusing a browser is still using it: refresh (or create) the lease so a
        # later stale check can tell "someone is on it" from "nobody is".
        result["lease"] = _register_lease_at_spawn(
            owner_type, profile, urllib.parse.urlparse(endpoint).port or HEADLESS_CDP_PORT,
        )
    elif info and not _is_ours(info):
        result["error"] = (
            f"{endpoint} answers but is not a headless Chrome "
            f"({info.get('Browser')!r}); refusing to drive it"
        )
        return result
    elif not auto_start:
        result["error"] = (
            f"CDP endpoint {endpoint} is unreachable and auto_start is False."
        )
        return result
    else:
        launch = launch_headless(
            cdp_url=endpoint, profile_dir=profile, chrome_exe=chrome_exe,
            open_url=open_url, timeout=timeout_seconds, owner_type=owner_type,
        )
        if not launch.get("ok"):
            result["error"] = launch.get("error")
            result["pid"] = launch.get("pid")
            return result
        result.update(reused=bool(launch.get("reused")), pid=launch.get("pid"),
                      version_info=launch.get("version_info"))

    tabs = [t for t in list_tabs(endpoint)
            if t.get("type") == "page" and "hh.ru" in (t.get("url") or "").lower()]
    if not tabs:
        opened = open_tab(endpoint, open_url)
        if opened:
            result["target"] = opened
            tabs = [opened]
    else:
        from .prefill_execute import select_best_hh_target

        result["target"] = select_best_hh_target(tabs, "hh.ru")

    if check_session:
        target_id = (result.get("target") or {}).get("id")
        try:
            if not target_id:
                raise RuntimeError("no hh.ru tab to read the session from")
            evaluate = make_target_evaluate(endpoint, target_id)
            evidence = settle_hh_page(evaluate)
            if evidence is None:
                auth = decide_authentication(
                    {"error": "the applicant page never rendered"}
                )
            else:
                auth = decide_authentication(evidence)
            result["auth"] = auth
            result["authenticated"] = bool(auth.get("authenticated"))
        except Exception as exc:  # noqa: BLE001 - an unreadable page is not a pass
            result["authenticated"] = False
            result["auth"] = {
                "authenticated": False, "verified": False,
                "reason": f"the page could not be read: {type(exc).__name__}: {exc}",
                "evidence": None,
            }

    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# T7.23: dedicated-profile process enumeration
# ---------------------------------------------------------------------------

# ``chrome.exe`` is the image name of every Chrome on the machine - Mikhail's own,
# BrowserOS, and ours. The *only* thing that distinguishes our headless instance is
# the dedicated profile on its command line, so that is what is matched, exactly.
#
# The path may be quoted, or unquoted *with a space in it* ("...\\JobSearchHeadless\\
# User Data"), which is the shape T7.20 measured. A naive ``\S+`` therefore stops at
# the space and returns a truncated path - and a truncated path matches nothing, so
# the holder probe would answer "no stale instance" while one was running. That is a
# fail-open in the one place that decides whether to terminate a process, so the value
# is taken up to the *next flag* instead of up to the next space.
_PROFILE_ARG_RE = re.compile(
    r"--user-data-dir=(?:\"([^\"]+)\"|'([^']+)'|(.*?))(?=\s+--|$)"
)

# Belt and braces: the BrowserOS profile lives under BrowserClaw, and no amount of
# path arithmetic should ever let a match land there.
FOREIGN_PROFILE_MARKERS = ("browserclaw", "browseros", "chrome-cdp-profile")


def _normalise_path(value: str) -> str:
    text = (value or "").strip().strip('"').strip("'")
    text = text.replace("\\", "/").rstrip("/")
    return text.lower()


def user_data_dir_from_command_line(command_line: str) -> str | None:
    """The ``--user-data-dir`` value of a command line, or None.

    Parsed, not substring-matched: a substring test for the profile path would also
    match a *different* profile whose path merely contains ours, and the whole point
    here is to terminate only processes we can prove are ours.
    """
    match = _PROFILE_ARG_RE.search(command_line or "")
    if not match:
        return None
    value = next((g for g in match.groups() if g), None)
    if value is None:
        return None
    # The unquoted branch swallows the closing quote of the other quoting style,
    # ``"--user-data-dir=C:\path with space"``: the lazy match runs up to the next
    # `` --`` and takes the quote with it. A path never legitimately ends in one,
    # and the value is the evidence for the exact match, so it must read clean.
    return value.strip().strip('"').strip("'")


def chrome_processes_with_command_lines() -> list[dict[str, Any]]:
    """Every ``chrome.exe`` with its command line, read through CIM.

    Windows-only and best-effort: a machine where the probe cannot run returns an
    empty list, which reads as "no holders found". That is the safe direction for a
    *report*, and ``stale_instance_recovery`` refuses to terminate anything when it
    has no evidence, so an empty probe can never authorise a kill.
    """
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Select-Object ProcessId,ParentProcessId,CommandLine,WorkingSetSize,CreationDate | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - a probe that cannot run is not a finding
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [row for row in data if isinstance(row, dict)]


def dedicated_profile_processes(profile_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Chrome processes holding **exactly** the dedicated profile.

    The definition is the command line, because the image name cannot tell our
    instance from Mikhail's own Chrome or from BrowserOS.
    """
    wanted = _normalise_path(str(profile_dir or headless_profile_dir()))
    found: list[dict[str, Any]] = []
    for row in chrome_processes_with_command_lines():
        cmd = row.get("CommandLine") or ""
        actual = user_data_dir_from_command_line(cmd)
        if actual is None or _normalise_path(actual) != wanted:
            continue
        lowered = cmd.lower()
        if any(marker in lowered for marker in FOREIGN_PROFILE_MARKERS):
            continue  # cannot happen with an exact match; refusing anyway
        pid = int(row.get("ProcessId") or 0)
        found.append({
            "pid": pid,
            "parent": int(row.get("ParentProcessId") or 0),
            "alive": pid_alive(pid),
            "ws_mb": round(int(row.get("WorkingSetSize") or 0) / 1024 / 1024, 1),
            "headless_flag": "headless" in lowered,
            "profile_dir": actual,
            "command_line": cmd,
        })
    return found


def terminate_pid(pid: int, timeout: float = 5.0) -> bool:
    """Terminate **one** process, by pid, and wait for it to actually go.

    Deliberately not ``taskkill /IM chrome.exe``: an image-wide kill takes out every
    Chrome on the machine, including the one Mikhail is working in. ``TerminateProcess``
    on a handle we opened for a pid we have already proven is ours cannot reach a
    process we did not name.
    """
    if os.name != "nt" or pid <= 0 or ctypes is None or not hasattr(ctypes, "windll"):
        return False
    PROCESS_TERMINATE = 0x0001
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        ctypes.windll.kernel32.TerminateProcess(handle, 1)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def _lease_view_for_recovery(endpoint: str) -> dict[str, Any] | None:
    """What the lease says about the instance, for the §5 gate.

    Returns ``None`` when there is no lease module or no lease - the caller then
    behaves as it did before T7.24, which is what the T7.23 tests assert.
    """
    try:
        from . import hh_browser_lease as lease

        existing = lease.read_lease()
        if existing is None:
            return None
        info = cdp_version(endpoint)
        return lease.lease_state(existing, instance_is_ours=bool(info) and _is_ours(info))
    except Exception:  # a missing lease must not block a recovery
        logger.debug("could not read the browser lease", exc_info=True)
        return None


def stale_instance_recovery(
    profile_dir: str | Path | None = None,
    cdp_url: str | None = None,
    timeout: float = CDP_SHUTDOWN_TIMEOUT,
) -> dict[str, Any]:
    """Recover from a hung instance of ours, politely first.

    Order, and the order is the safety property:

    1. **Prove** the processes belong to the dedicated profile (exact
       ``--user-data-dir``), and record the evidence.
    2. **Ask** the browser to close through CDP. This is the normal path and it also
       flushes the profile, so it is tried first even when it looks hopeless.
    3. **Only then**, and only for the pids proven in step 1, terminate the process.

    A process that holds our profile but is **not** headless is *not* killed: that
    would be a window with a visible surface, and closing someone's window is worse
    than leaving a stale instance for a human to look at. It is reported instead.
    """
    profile = Path(profile_dir) if profile_dir else headless_profile_dir()
    endpoint = cdp_url or headless_cdp_url()
    port = urllib.parse.urlparse(endpoint).port or HEADLESS_CDP_PORT

    holders = dedicated_profile_processes(profile)
    report: dict[str, Any] = {
        "profile_dir": str(profile),
        "endpoint": endpoint,
        "holders_before": holders,
        "holders_before_count": len(holders),
        "evidence": (
            "matched on the exact --user-data-dir argument, parsed from the command "
            "line read through CIM"
        ),
        "cdp_shutdown_attempted": False,
        "terminated": [],
        "refused": [],
        "status": "",
    }

    if not holders:
        report["status"] = "NO_STALE_INSTANCE"
        report["ok"] = True
        return report

    # T7.24 §5: a dead launcher is NOT evidence of staleness. ``DETACHED_PROCESS``
    # means the launcher is expected to exit while the browser works, so "parent
    # dead" is the normal case. The lease is asked first, and a live or active
    # lease stops the recovery before it can touch anything.
    lease_view = _lease_view_for_recovery(endpoint)
    report["lease"] = lease_view
    if lease_view and lease_view.get("state") in ("LIVE", "ACTIVE"):
        report["status"] = "LEASE_ACTIVE"
        report["ok"] = False
        report["refused"].append({
            "reason": (
                "a lease says this instance is still owned and possibly in use: "
                + "; ".join(lease_view.get("reasons") or [])
            ),
            "lease_state": lease_view.get("state"),
            "owner_type": lease_view.get("owner_type"),
            "owner_pid": lease_view.get("owner_pid"),
            "active_operations": lease_view.get("active_operations"),
        })
        return report

    # Step 2: the polite way.
    if port_is_listening(port):
        report["cdp_shutdown_attempted"] = True
        quit_browser(endpoint)
        report["port_freed"] = wait_port_free(port, timeout=timeout)
    else:
        report["port_freed"] = True

    remaining = dedicated_profile_processes(profile)
    if not remaining:
        report["status"] = "RECOVERED_BY_CDP"
        report["ok"] = True
        report["holders_after"] = []
        return report

    # Step 3: terminate only what we proved, and only if it is headless.
    for holder in remaining:
        if not holder.get("headless_flag"):
            report["refused"].append({
                "pid": holder["pid"],
                "reason": (
                    "process holds the dedicated profile but is not headless; "
                    "refusing to close a process that may own a visible window"
                ),
            })
            continue
        if terminate_pid(holder["pid"]):
            report["terminated"].append(holder["pid"])
        else:
            report["refused"].append({
                "pid": holder["pid"], "reason": "TerminateProcess did not take effect",
            })

    wait_port_free(port, timeout=5.0)
    after = dedicated_profile_processes(profile)
    report["holders_after"] = after
    report["port_freed"] = not port_is_listening(port)

    if not after:
        report["status"] = "RECOVERED_BY_TERMINATION"
        report["ok"] = True
    elif report["refused"]:
        report["status"] = "NEEDS_MANUAL"
        report["ok"] = False
    else:
        report["status"] = "STILL_HELD"
        report["ok"] = False
    return report


# ---------------------------------------------------------------------------
# T7.23: the cleanup guarantee
# ---------------------------------------------------------------------------

def cleanup_report(session: HeadlessBrowserSession) -> dict[str, Any]:
    """The five post-conditions, measured - not asserted.

    ``OWNED_TABS_LEFT = 0`` and friends are claims about the world, so each one is
    read back after the teardown rather than inferred from "stop() returned".
    """
    stop = session.stop()
    profile = session.verify_profile_unlocked()
    processes = session.verify_no_owned_processes()
    return {
        "stop": stop,
        "owned_tabs_left": len(session.owned),
        "owned_tabs": session.ownership_registry(),
        "headless_processes": processes["count"],
        "headless_processes_detail": processes["processes"],
        "port_free": bool(stop.get("port_freed")),
        "profile_lock_free": profile["unlocked"],
        "profile": profile,
        "all_clear": (
            len(session.owned) == 0
            and processes["count"] == 0
            and bool(stop.get("port_freed"))
            and profile["unlocked"]
        ),
    }


@contextlib.contextmanager
def managed_headless_session(
    on_cleanup: Callable[[dict[str, Any]], None] | None = None,
    **session_kwargs: Any,
):
    """Run a whole HH workflow with teardown that cannot be skipped.

    The task's shape, exactly::

        try:
            run_workflow()
        finally:
            close_all_owned_tabs(); stop_headless_browser(); wait_port_free();
            verify_profile_unlocked(); verify_no_owned_processes()

    All five live in ``stop()``/``cleanup_report()``, so a caller cannot implement
    four of them. Because the teardown is in a ``finally``, it runs on the same path
    for PASS, BLOCKED, an auth failure, a parse failure, a timeout, a closed vacancy,
    a geo rejection and an exception thrown one line before submit - which is the
    property the four-checkpoint test exists to prove.

    ``on_cleanup`` receives the measured report, so an interrupted run still records
    what it left behind instead of raising a second exception over the first.
    """
    session = HeadlessBrowserSession(**session_kwargs)
    start: dict[str, Any] = {"ok": False, "status": "NOT_STARTED"}
    try:
        start = session.start()
        yield session, start
    finally:
        try:
            report = cleanup_report(session)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask the cause
            report = {"all_clear": False, "error": f"{type(exc).__name__}: {exc}"}
        session.cleanup = report  # type: ignore[attr-defined]
        if on_cleanup is not None:
            try:
                on_cleanup(report)
            except Exception:
                logger.debug("cleanup reporter raised", exc_info=True)
