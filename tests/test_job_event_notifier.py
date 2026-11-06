"""Idempotent, fail-closed event notifications for the jobs bot."""

from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from ai_assistant import db, telegram_routing
from ai_assistant.job_event_notifier import (
    JobEventNotificationError,
    notify_job_event,
)

JOBS_TOKEN = "8657801093:jobs-test-token"
HERMES_TOKEN = "8217526633:hermes-test-token"
OWNER_CHAT = "392046103"
ROUTE = telegram_routing.JobRoute(token=JOBS_TOKEN, chat_id=OWNER_CHAT)


class _Response:
    status = 200

    def __init__(self, message_id: int = 7):
        self._body = json.dumps(
            {"ok": True, "result": {"message_id": message_id}}
        ).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _success(message_id: int = 7):
    def opener(request, timeout=10):
        _success.url = request.full_url
        _success.body = json.loads(request.data.decode())
        return _Response(message_id)

    return opener


def _row(key: str):
    conn = db.get_connection()
    try:
        return conn.execute(
            "SELECT status, payload FROM telegram_delivery_records WHERE delivery_key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()


def test_delivered_event_is_recorded_and_duplicate_is_not_resent():
    opener = _success()
    first = notify_job_event(
        "gmail:abc",
        "New employer message",
        source="hh_chat_monitor",
        company="Acme Ltd",
        role="Automation Engineer",
        chat_url="https://hh.ru/chat/1",
        route=ROUTE,
        opener=opener,
    )

    assert first.status == "delivered"
    assert first.message_id == 7
    assert JOBS_TOKEN in _success.url
    assert HERMES_TOKEN not in _success.url
    assert _success.body["chat_id"] == OWNER_CHAT

    second_opener = _success()
    second = notify_job_event(
        "gmail:abc",
        "New employer message",
        route=ROUTE,
        opener=second_opener,
    )
    assert second.status == "duplicate"
    assert not hasattr(second_opener, "url")

    row = _row("job_event:gmail:abc")
    assert row[0] == "DELIVERED"
    payload = json.loads(row[1])
    assert payload["company"] == "Acme Ltd"
    assert payload["role"] == "Automation Engineer"
    assert payload["message_text"] == "New employer message"


def test_placeholder_event_key_fails_before_any_record_or_send():
    with pytest.raises(JobEventNotificationError):
        notify_job_event(
            "gmail:<ID>",
            "hello",
            route=ROUTE,
            opener=lambda *a, **k: pytest.fail("must not send a placeholder key"),
        )
    db.init_db()
    assert _row("job_event:gmail:<ID>") is None


def test_missing_route_fails_closed_without_recording(monkeypatch):
    def refuse():
        raise telegram_routing.TelegramRoutingError("main Hermes token")

    monkeypatch.setattr(telegram_routing, "resolve_job_route", refuse)
    result = notify_job_event(
        "gmail:no-route",
        "hello",
        opener=lambda *a, **k: pytest.fail("must not send"),
    )
    assert result.status == "unconfigured"
    db.init_db()
    assert _row("job_event:gmail:no-route") is None


def test_timeout_is_ambiguous_and_never_retried_automatically():
    def timeout_opener(request, timeout=10):
        raise socket.timeout("timed out")

    first = notify_job_event(
        "gmail:timeout",
        "hello",
        route=ROUTE,
        opener=timeout_opener,
    )
    assert first.status == "ambiguous"
    assert _row("job_event:gmail:timeout")[0] == "AMBIGUOUS"

    def must_not_send(request, timeout=10):
        pytest.fail("ambiguous event must not be retried")

    second = notify_job_event(
        "gmail:timeout",
        "hello",
        route=ROUTE,
        opener=must_not_send,
    )
    assert second.status == "duplicate"


def test_auth_401_is_recorded_and_retry_requires_explicit_flag():
    def auth_error(request, timeout=10):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"")
        )

    first = notify_job_event(
        "gmail:401",
        "hello",
        route=ROUTE,
        opener=auth_error,
    )
    assert first.status == "auth_failed"
    assert _row("job_event:gmail:401")[0] == "FAILED"

    skipped = notify_job_event(
        "gmail:401",
        "hello",
        route=ROUTE,
        opener=lambda *a, **k: pytest.fail("must not auto-retry a failed event"),
    )
    assert skipped.status == "skipped"

    recovered = notify_job_event(
        "gmail:401",
        "hello",
        route=ROUTE,
        opener=_success(9),
        retry_failed=True,
    )
    assert recovered.status == "delivered"
    assert recovered.message_id == 9
    assert _row("job_event:gmail:401")[0] == "DELIVERED"


def test_non_200_without_exception_is_a_confirmed_failure():
    class _ErrorResponse(_Response):
        status = 500

    def opener(request, timeout=10):
        return _ErrorResponse()

    result = notify_job_event(
        "gmail:500",
        "hello",
        route=ROUTE,
        opener=opener,
    )
    assert result.status == "ambiguous"
    assert _row("job_event:gmail:500")[0] == "AMBIGUOUS"
