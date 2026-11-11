import sys
import os
import socket
from pathlib import Path
import pytest

ai_assistant_dir = Path(__file__).resolve().parent.parent / "ai_assistant"
if str(ai_assistant_dir) not in sys.path:
    sys.path.insert(0, str(ai_assistant_dir))


# Audit 2026-10-19: SUBMIT_ALLOWED is read from the process environment at call
# time, so a single test that writes it directly (or rebuilds config state)
# changes the kill-switch for every later test in the session. Measured in the
# 2026-10-19 audit: test_submission_verifier/test_hh_submit_policy pass in
# isolation and fail in the full run with "Submission is disabled by
# SUBMIT_ALLOWED configuration". Restore the operator baseline after every test.
_SUBMIT_KILL_SWITCH_BASELINE: tuple | None = None


@pytest.fixture(autouse=True)
def restore_submit_kill_switch(monkeypatch):
    from ai_assistant import config as _config

    global _SUBMIT_KILL_SWITCH_BASELINE
    if _SUBMIT_KILL_SWITCH_BASELINE is None:
        _SUBMIT_KILL_SWITCH_BASELINE = (
            os.environ.get("SUBMIT_ALLOWED"),
            getattr(_config, "_OPERATOR_SUBMIT_ALLOWED", None),
            getattr(_config, "SUBMIT_ALLOWED", None),
        )
    # Default SUBMIT_ALLOWED is false in every test; clear _OPERATOR_SUBMIT_ALLOWED
    # so per-test monkeypatch.setenv("SUBMIT_ALLOWED", "true") can take effect.
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")
    monkeypatch.setattr(_config, "_OPERATOR_SUBMIT_ALLOWED", None)
    monkeypatch.setattr(_config, "SUBMIT_ALLOWED", False)

    yield
    saved_env, saved_operator, saved_flag = _SUBMIT_KILL_SWITCH_BASELINE
    if saved_env is None:
        os.environ.pop("SUBMIT_ALLOWED", None)
    else:
        os.environ["SUBMIT_ALLOWED"] = saved_env
    _config._OPERATOR_SUBMIT_ALLOWED = saved_operator
    _config.SUBMIT_ALLOWED = saved_flag


@pytest.fixture
def submit_allowed(monkeypatch):
    """Explicitly enable submit_allowed for tests requesting this fixture."""
    from ai_assistant import config as _config
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    monkeypatch.setattr(_config, "_OPERATOR_SUBMIT_ALLOWED", None)
    monkeypatch.setattr(_config, "SUBMIT_ALLOWED", True)




@pytest.fixture(autouse=True)
def global_test_isolation(monkeypatch, tmp_path):
    """Fail closed for real network and production runtime state.

    Tests that need transport semantics must inject a fake adapter/response. We
    allow ephemeral loopback for in-process ASGI clients, but block known CDP
    ports. Every test also starts with isolated SQLite, vacancies, and logs
    paths; per-test fixtures may override these paths further.
    """
    import urllib.request
    from ai_assistant import config

    state_dir = tmp_path / "global_runtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_file = state_dir / "state.db"
    vacancies_file = state_dir / "vacancies.json"
    logs_dir = state_dir / "logs"

    monkeypatch.setattr(config, "DB_FILE", str(db_file))
    monkeypatch.setattr(config, "VACANCIES_FILE", str(vacancies_file))
    monkeypatch.setattr(config, "LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("DB_FILE", str(db_file))
    monkeypatch.setenv("VACANCIES_FILE", str(vacancies_file))
    monkeypatch.setenv("LOGS_DIR", str(logs_dir))

    # The headless runner keeps a record and a lease, and both are repository files
    # (artifacts/headless-poc/). They are shared with whatever is really running, so
    # a test that merely calls stop() - or reaches the launch path - would stamp the
    # record of the live browser. Measured 29.09 (T7.24): runner-state.json carried
    # `pid: null, stopped_at: ...` from six separate pytest runs while root pid 26152
    # was still listening on 9111, and a lease appeared naming the pytest process
    # itself as the owner of that browser.
    from ai_assistant import hh_browser_lease as _lease
    from ai_assistant import hh_browser_provider as _bp

    headless_dir = state_dir / "headless"
    headless_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_bp, "STATE_DIR", headless_dir)
    monkeypatch.setattr(_bp, "STATE_FILE", headless_dir / "runner-state.json")
    monkeypatch.setattr(_bp, "CHROME_LOG", headless_dir / "chrome-headless.log")
    # Computed at import time from STATE_DIR, so it needs its own redirect.
    monkeypatch.setattr(_lease, "LEASE_FILE", headless_dir / "browser-lease.json")

    monkeypatch.setenv("HH_CDP_URL", "http://127.0.0.1:9222")

    def blocked_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        raise RuntimeError(
            f"SAFETY VIOLATION: unmocked network request attempted during pytest: {url}"
        )

    original_create_connection = socket.create_connection
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_safe_loopback(address):
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        host, port = address[0], address[1]
        # 9111 is the headless runner's own CDP port (T7.21) and carries a *real*
        # browser with a *real* HH session, so it belongs in this list. It was
        # missing, and the consequence was measured on 29.09 (T7.24): a pytest
        # process reached the live browser over 9111 and registered itself as its
        # owner. Keep this set in step with hh_browser_provider.HEADLESS_CDP_PORT.
        blocked_ports = {9110, 9111, 9010, 9210, 9222, 9223, 9999}
        return str(host).lower() in {"127.0.0.1", "::1", "localhost"} and port not in blocked_ports

    def blocked_connection(*args, **kwargs):
        target = args[0] if args else kwargs.get("address", "unknown")
        if is_safe_loopback(target):
            return original_create_connection(*args, **kwargs)
        raise RuntimeError(
            f"SAFETY VIOLATION: unmocked socket connection attempted during pytest: {target}"
        )

    def blocked_connect(sock, address):
        if is_safe_loopback(address):
            return original_connect(sock, address)
        raise RuntimeError(
            f"SAFETY VIOLATION: unmocked socket connection attempted during pytest: {address}"
        )

    def blocked_connect_ex(sock, address):
        if is_safe_loopback(address):
            return original_connect_ex(sock, address)
        blocked_connect(sock, address)

    monkeypatch.setattr(urllib.request, "urlopen", blocked_urlopen)
    monkeypatch.setattr(socket, "create_connection", blocked_connection)
    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect_ex)
    monkeypatch.setenv("JOB_SEARCH_TEST_NETWORK_BLOCKED", "1")


@pytest.fixture(autouse=True)
def autonomous_reply_switch_on(monkeypatch):
    """Arm the Stage 51-55 autonomous reply send for the test suite.

    That send is opt-in in production (HH_AUTONOMOUS_REPLY_ENABLED, off by
    default — see ai_assistant/hh_autonomous_agent.py). The Stage 51-55 tests
    (test_stage51..60) pin the *mechanism*, so they need the switch armed
    explicitly rather than relying on a production default.

    Tests that assert the OFF behaviour must monkeypatch.delenv this variable
    themselves; see tests/test_hh_autonomous_reply_optin.py.
    """
    monkeypatch.setenv("HH_AUTONOMOUS_REPLY_ENABLED", "true")
