"""Characterization tests for the global socket-level network safety guard."""
import socket
import urllib.request

import pytest
import requests


def test_socket_connect_external_is_blocked():
    """Raw socket.connect to external IP must raise RuntimeError SAFETY VIOLATION."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="SAFETY VIOLATION: unmocked socket connection attempted"):
            s.connect(("8.8.8.8", 53))
    finally:
        s.close()


def test_socket_create_connection_external_is_blocked():
    """socket.create_connection to external domain must raise RuntimeError SAFETY VIOLATION."""
    with pytest.raises(RuntimeError, match="SAFETY VIOLATION: unmocked socket connection attempted"):
        socket.create_connection(("api.telegram.org", 443))


def test_urllib_request_urlopen_external_is_blocked():
    """urllib.request.urlopen to external URL must raise RuntimeError SAFETY VIOLATION."""
    with pytest.raises(RuntimeError, match="SAFETY VIOLATION: unmocked network request attempted"):
        urllib.request.urlopen("https://hh.ru/vacancy/12345")


def test_requests_external_is_blocked_at_socket_level():
    """requests.get to external domain must be intercepted by socket guard."""
    with pytest.raises(Exception) as excinfo:
        requests.get("https://api.telegram.org", timeout=5)
    # The underlying cause must be the socket SAFETY VIOLATION
    assert "SAFETY VIOLATION" in str(excinfo.value) or (
        excinfo.value.__cause__ and "SAFETY VIOLATION" in str(excinfo.value.__cause__)
    )


def test_network_blocked_even_when_submit_allowed_enabled(submit_allowed):
    """Even when SUBMIT_ALLOWED=true is explicitly patched, external network remains physically severed."""
    from ai_assistant import config
    assert config.submit_allowed() is True

    # Any real attempt to contact HH or Telegram is physically aborted
    with pytest.raises(RuntimeError, match="SAFETY VIOLATION"):
        urllib.request.urlopen("https://api.telegram.org/bot123456/sendMessage")
