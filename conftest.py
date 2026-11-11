"""Root conftest.py providing repository-wide test isolation and network guards."""
import os
import socket
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent
ai_assistant_dir = REPO_ROOT / "ai_assistant"
if str(ai_assistant_dir) not in sys.path:
    sys.path.insert(0, str(ai_assistant_dir))
hermes_dir = REPO_ROOT / "integrations" / "hermes"
if str(hermes_dir) not in sys.path:
    sys.path.insert(0, str(hermes_dir))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure required fixture documents are populated in isolated CI environments
_fixture_resume = REPO_ROOT / "tests" / "fixtures" / "resume.md"
_target_resume = REPO_ROOT / "resume.md"
if _fixture_resume.exists() and not _target_resume.exists():
    _target_resume.write_text(_fixture_resume.read_text(encoding="utf-8"), encoding="utf-8")

_fixture_profile = REPO_ROOT / "tests" / "fixtures" / "candidate_profile.json"
if not _fixture_profile.exists():
    _fixture_profile = REPO_ROOT / "candidate_profile.example.json"
_target_profile = REPO_ROOT / "candidate_profile.json"
if _fixture_profile.exists() and not _target_profile.exists():
    _target_profile.write_text(_fixture_profile.read_text(encoding="utf-8"), encoding="utf-8")

_fixture_kb = REPO_ROOT / "tests" / "fixtures" / "candidate_kb.json"
_target_kb = REPO_ROOT / "candidate_kb.json"
if _fixture_kb.exists() and not _target_kb.exists():
    _target_kb.write_text(_fixture_kb.read_text(encoding="utf-8"), encoding="utf-8")

_target_db = REPO_ROOT / "state.db"
if not _target_db.exists():
    try:
        from ai_assistant import db
        db.init_db()
    except Exception:
        pass

# Ensure CANDIDATE_PROJECTS_ROOT allows projects.py to find Documents/job-search
_doc_root = REPO_ROOT / ".doc_root"
_doc_link = _doc_root / "Documents" / "job-search"
if not (_doc_link / "README.md").exists():
    _doc_link.mkdir(parents=True, exist_ok=True)
    _orig_readme = REPO_ROOT / "README.md"
    if _orig_readme.exists():
        (_doc_link / "README.md").write_text(_orig_readme.read_text(encoding="utf-8"), encoding="utf-8")
os.environ.setdefault("CANDIDATE_PROJECTS_ROOT", str(_doc_root))

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

    from ai_assistant import hh_browser_lease as _lease
    from ai_assistant import hh_browser_provider as _bp

    headless_dir = state_dir / "headless"
    headless_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_bp, "STATE_DIR", headless_dir)
    monkeypatch.setattr(_bp, "STATE_FILE", headless_dir / "runner-state.json")
    monkeypatch.setattr(_bp, "CHROME_LOG", headless_dir / "chrome-headless.log")
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
    """Arm the Stage 51-55 autonomous reply send for the test suite."""
    monkeypatch.setenv("HH_AUTONOMOUS_REPLY_ENABLED", "true")


def pytest_addoption(parser):
    parser.addoption(
        "--deselect-known-failures",
        action="store_true",
        default=False,
        help="Deselect known failures tracked in docs/audit/known_failures.txt",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--deselect-known-failures", default=False):
        return
    kf_path = REPO_ROOT / "docs" / "audit" / "known_failures.txt"
    if not kf_path.exists():
        return
    known = set(
        line.strip()
        for line in kf_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    selected = []
    deselected = []
    for item in items:
        norm_nodeid = item.nodeid.replace("\\", "/")
        if any(norm_nodeid == k or norm_nodeid.startswith(k + "[") for k in known):
            deselected.append(item)
        else:
            selected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected

