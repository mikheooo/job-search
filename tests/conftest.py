import sys
import os
import socket
from pathlib import Path
import pytest

ai_assistant_dir = Path(__file__).resolve().parent.parent / "ai_assistant"
if str(ai_assistant_dir) not in sys.path:
    sys.path.insert(0, str(ai_assistant_dir))


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
        return str(host).lower() in {"127.0.0.1", "::1", "localhost"} and port not in {9222, 9223, 9999}

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
