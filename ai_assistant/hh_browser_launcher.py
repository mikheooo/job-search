"""Stage 32B: Unified HH Chrome Browser Lifecycle Manager.

Manages finding, checking, and starting the designated HeadHunter (HH)
Chrome instance with remote debugging port and saved user profile.

SAFETY & LIFECYCLE INVARIANTS:
1. Reuses existing running Chrome on CDP port if already reachable.
2. Does NOT launch duplicate browser instances.
3. Uses the dedicated user profile `chrome-cdp-profile`.
4. Uses standard CDP debugging port (default: 9222).
5. Waits for CDP `/json/version` endpoint readiness.
6. Ensures HH tab is available without altering submission flow.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_HH_CDP_PORT = 9222
DEFAULT_HH_CDP_URL = os.getenv("HH_CDP_URL", f"http://127.0.0.1:{DEFAULT_HH_CDP_PORT}")

BROWSEROS_CDP_PORT = 9110
BROWSEROS_CDP_URL = f"http://127.0.0.1:{BROWSEROS_CDP_PORT}"
DEFAULT_HH_PROFILE_DIR = os.getenv(
    "HH_CHROME_PROFILE",
    r"C:\Users\Misha\chrome-cdp-profile",
)
DEFAULT_HH_URL = os.getenv("HH_URL", "https://hh.ru/chat")

# CDP is a localhost protocol. urllib honours http_proxy/HTTP_PROXY, so with a
# proxy configured in the environment a perfectly alive browser answers
# "502 Bad Gateway" and reads as dead - which then trips the BrowserOS swap
# below and silently switches browser profiles. Always talk to the debugging
# port directly.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def resolve_cdp_url(preferred: str | None = None, allow_browseros: bool = True) -> str:
    """Single source of truth for "which browser are we driving".

    Deterministic part (no I/O): explicit argument -> CDP_URL -> HH_CDP_URL ->
    DEFAULT_HH_CDP_URL. An argument or env var that merely repeats the default
    is not treated as a decision (it is usually a default passed through), but
    any *other* value is binding and stops the probing below.

    The BrowserOS probe only runs when nothing was configured at all, and it is
    always logged: swapping browsers means swapping profiles, and a profile
    without an hh.ru session reads a completely different (logged-out) form.
    """
    explicit = preferred if preferred and preferred != DEFAULT_HH_CDP_URL else None
    env_cdp = os.getenv("CDP_URL") or os.getenv("HH_CDP_URL")
    if explicit or env_cdp:
        return explicit or env_cdp
    if not allow_browseros:
        return DEFAULT_HH_CDP_URL
    if not is_cdp_reachable(DEFAULT_HH_CDP_URL, timeout=0.3) and is_cdp_reachable(BROWSEROS_CDP_URL, timeout=0.3):
        logger.warning(
            "CDP %s is unreachable; switching to BrowserOS on %s. This is a "
            "different browser profile - verify it is logged into hh.ru.",
            DEFAULT_HH_CDP_URL,
            BROWSEROS_CDP_URL,
        )
        return BROWSEROS_CDP_URL
    return DEFAULT_HH_CDP_URL


def find_chrome_executable() -> str | None:
    """Locate Google Chrome executable in standard system locations."""
    # 1. Environment override
    env_path = os.getenv("CHROME_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Windows standard paths
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe"))

    prog_files = os.environ.get("PROGRAMFILES")
    if prog_files:
        candidates.append(os.path.join(prog_files, r"Google\Chrome\Application\chrome.exe"))

    prog_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    if prog_files_x86:
        candidates.append(os.path.join(prog_files_x86, r"Google\Chrome\Application\chrome.exe"))

    for c in candidates:
        if os.path.isfile(c):
            return c

    # 3. System PATH lookup
    which_chrome = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    if which_chrome and os.path.isfile(which_chrome):
        return which_chrome

    return None


def is_cdp_reachable(cdp_url: str = DEFAULT_HH_CDP_URL, timeout: float = 1.5) -> bool:
    """Check if CDP `/json/version` endpoint responds with valid JSON."""
    try:
        url = cdp_url.rstrip("/") + "/json/version"
        req = urllib.request.Request(url, headers={"User-Agent": "job-search-watcher"})
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("Browser") or data.get("webSocketDebuggerUrl"))
    except Exception:
        pass
    return False


def get_cdp_version_info(cdp_url: str = DEFAULT_HH_CDP_URL, timeout: float = 2.0) -> dict[str, Any] | None:
    """Retrieve `/json/version` metadata from CDP endpoint."""
    try:
        url = cdp_url.rstrip("/") + "/json/version"
        req = urllib.request.Request(url, headers={"User-Agent": "job-search-watcher"})
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def get_cdp_targets(cdp_url: str = DEFAULT_HH_CDP_URL, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Retrieve `/json/list` targets from CDP endpoint."""
    try:
        url = cdp_url.rstrip("/") + "/json/list"
        req = urllib.request.Request(url, headers={"User-Agent": "job-search-watcher"})
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return []


def open_cdp_tab(cdp_url: str = DEFAULT_HH_CDP_URL, url_to_open: str = DEFAULT_HH_URL, timeout: float = 5.0) -> dict[str, Any] | None:
    """Open a new tab via CDP `/json/new?<url>` endpoint."""
    try:
        url = f"{cdp_url.rstrip('/')}/json/new?{url_to_open}"
        req = urllib.request.Request(url, method="PUT", headers={"User-Agent": "job-search-watcher"})
        try:
            with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            # Fallback to GET for older Chromium endpoints
            req_get = urllib.request.Request(url, method="GET", headers={"User-Agent": "job-search-watcher"})
            with _NO_PROXY_OPENER.open(req_get, timeout=timeout) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"Failed to open tab on {cdp_url}: {e}")
    return None


def check_hh_session_authenticated(evaluate_fn: Callable[[str], str]) -> dict[str, Any]:
    """Check whether the active HH session is authenticated.

    Read-only DOM check: verifies absence of mandatory login redirects/buttons
    and presence of applicant profile elements or cookies.
    """
    check_js = """(() => {
        try {
            const path = location.pathname || '';
            const href = location.href || '';
            const isLoginPage = path.includes('/account/login') || href.includes('account/login');
            const loginBtn = document.querySelector('[data-qa="login"], a[href*="account/login"]');
            const userProfile = document.querySelector(
                '[data-qa="mainmenu_applicantProfile"], [data-qa="mainmenu_profile"], ' +
                '[data-qa="applicant-profile"], [data-qa="mainmenu_messages"], ' +
                'a[href*="/applicant/negotiations"], a[href*="/applicant/resumes"]'
            );
            const cookies = document.cookie || '';
            const hasAuthCookie = cookies.includes('hhtoken') || cookies.includes('hhuid') || cookies.includes('user_uid');

            if (isLoginPage) {
                return JSON.stringify({authenticated: false, reason: "Browser is on HH login page", url: href});
            }
            if (userProfile) {
                return JSON.stringify({authenticated: true, reason: "Applicant profile navigation element found", url: href});
            }
            if (loginBtn && !hasAuthCookie) {
                return JSON.stringify({authenticated: false, reason: "Login button visible and no session cookies", url: href});
            }
            return JSON.stringify({authenticated: true, reason: "Session active (no login prompt)", url: href});
        } catch (e) {
            return JSON.stringify({authenticated: true, reason: 'Check fallback: ' + e.message, url: location.href});
        }
    })()"""
    try:
        raw = evaluate_fn(check_js)
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data
    except Exception as e:
        return {"authenticated": True, "reason": f"DOM evaluation error, assuming session active: {e}"}


def ensure_hh_browser(
    cdp_url: str = DEFAULT_HH_CDP_URL,
    profile_dir: str | None = None,
    open_url: str = DEFAULT_HH_URL,
    auto_start: bool = True,
    timeout_seconds: float = 15.0,
    check_session: bool = True,
) -> dict[str, Any]:
    """Ensure that the designated HH Chrome instance is running and accessible over CDP.

    1. Checks if CDP endpoint is already reachable.
    2. If not reachable and auto_start is True: launches Chrome with correct profile and remote-debugging-port.
    3. Waits up to timeout_seconds for CDP readiness.
    4. Ensures an HH page target exists.
    """
    resolved_profile = profile_dir or DEFAULT_HH_PROFILE_DIR
    result: dict[str, Any] = {
        "ok": False,
        "cdp_url": cdp_url,
        "reused": False,
        "pid": None,
        "profile_dir": resolved_profile,
        "version_info": None,
        "target": None,
        "authenticated": True,
        "error": None,
    }

    # Step 1: Check if already reachable
    if is_cdp_reachable(cdp_url):
        result["reused"] = True
        result["version_info"] = get_cdp_version_info(cdp_url)
    elif not auto_start:
        result["error"] = f"CDP endpoint {cdp_url} is unreachable and auto_start is False."
        return result
    else:
        # Step 2: Find Chrome executable
        chrome_exe = find_chrome_executable()
        if not chrome_exe:
            result["error"] = (
                "Google Chrome executable was not found in standard system locations. "
                "Set CHROME_PATH environment variable to chrome.exe location."
            )
            return result

        # Ensure profile directory exists
        profile_path = Path(resolved_profile)
        profile_path.mkdir(parents=True, exist_ok=True)

        # Extract port from CDP URL
        import urllib.parse
        parsed = urllib.parse.urlparse(cdp_url)
        port = parsed.port or DEFAULT_HH_CDP_PORT

        cmd = [
            chrome_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={resolved_profile}",
            "--no-first-run",
            "--no-default-browser-check",
            open_url,
        ]

        logger.info(f"Starting HH Chrome: {' '.join(cmd)}")

        # Launch detached subprocess
        try:
            creationflags = 0
            if os.name == "nt":
                # CREATE_NEW_PROCESS_GROUP and DETACHED_PROCESS on Windows
                creationflags = 0x00000200 | 0x00000008

            proc = subprocess.Popen(
                cmd,
                creationflags=creationflags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True if os.name != "nt" else False,
            )
            result["pid"] = proc.pid
            result["reused"] = False
        except Exception as e:
            result["error"] = f"Failed to spawn Chrome process: {e}"
            return result

        # Step 3: Wait for CDP endpoint readiness
        start_time = time.time()
        ready = False
        while time.time() - start_time < timeout_seconds:
            if is_cdp_reachable(cdp_url, timeout=0.5):
                ready = True
                break
            time.sleep(0.4)

        if not ready:
            result["error"] = (
                f"Chrome process started (PID {result['pid']}), but CDP endpoint {cdp_url} "
                f"did not become reachable within {timeout_seconds}s."
            )
            return result

        result["version_info"] = get_cdp_version_info(cdp_url)

    # Step 4: Ensure HH target is open
    targets = get_cdp_targets(cdp_url)
    hh_targets = [
        t for t in targets
        if t.get("type") == "page" and "hh.ru" in (t.get("url") or "").lower()
    ]

    if not hh_targets:
        # Open HH tab
        opened = open_cdp_tab(cdp_url, open_url)
        if opened:
            result["target"] = opened
        else:
            time.sleep(1.0)
            targets = get_cdp_targets(cdp_url)
            hh_targets = [
                t for t in targets
                if t.get("type") == "page" and "hh.ru" in (t.get("url") or "").lower()
            ]
            result["target"] = hh_targets[0] if hh_targets else None
    else:
        from .prefill_execute import select_best_hh_target
        result["target"] = select_best_hh_target(targets, "hh.ru")

    # Step 5: Wait for HH tab to finish loading past about:blank
    try:
        from .prefill_execute import make_cdp_evaluate
        ev = make_cdp_evaluate(cdp_url, "hh.ru")
        for _ in range(25):
            time.sleep(0.3)
            try:
                state_raw = ev("JSON.stringify({url: location.href, readyState: document.readyState})")
                state = json.loads(state_raw)
                if "about:blank" not in state.get("url", "") and state.get("readyState") in ("interactive", "complete"):
                    break
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Navigation wait notice: {e}")

    result["ok"] = True
    return result
