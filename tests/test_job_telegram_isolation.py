"""Job Search notifications must never reach the main Hermes bot."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from ai_assistant import telegram_routing
from ai_assistant.auth_notifications import (
    AUTH_FAILURE_MESSAGE,
    AUTH_RECOVERED_MESSAGE,
    AuthNotifier,
)

JOBS_TOKEN = "8657801093:jobs-test-token"
HERMES_TOKEN = "8217526633:hermes-test-token"
OWNER_CHAT = "392046103"
PUBLIC_CHANNEL = "-1004399255305"


def _config(token: str = JOBS_TOKEN, chat: str = OWNER_CHAT):
    return SimpleNamespace(
        HH_TELEGRAM_BOT_TOKEN=token,
        HH_TELEGRAM_CHAT_ID=chat,
        TELEGRAM_BOT_TOKEN=HERMES_TOKEN,
        TELEGRAM_CHANNEL_ID=PUBLIC_CHANNEL,
    )


def test_jobs_route_uses_dedicated_bot():
    route = telegram_routing.resolve_job_route(
        _config(), forbidden_tokens={HERMES_TOKEN}
    )
    assert route.token == JOBS_TOKEN
    assert route.chat_id == OWNER_CHAT


def test_jobs_route_refuses_main_hermes_token():
    with pytest.raises(telegram_routing.TelegramRoutingError):
        telegram_routing.resolve_job_route(
            _config(token=HERMES_TOKEN), forbidden_tokens={HERMES_TOKEN}
        )


def test_jobs_route_refuses_missing_chat():
    with pytest.raises(telegram_routing.TelegramRoutingError):
        telegram_routing.resolve_job_route(_config(chat=""), forbidden_tokens=set())


def test_jobs_route_refuses_public_channel():
    with pytest.raises(telegram_routing.TelegramRoutingError):
        telegram_routing.resolve_job_route(
            _config(chat=PUBLIC_CHANNEL), forbidden_tokens=set()
        )


def test_send_uses_jobs_chat_and_never_channel(monkeypatch):
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=10):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        return _Response()

    route = telegram_routing.JobRoute(token=JOBS_TOKEN, chat_id=OWNER_CHAT)
    assert telegram_routing.send_job_message("hello", route=route, opener=fake_urlopen)
    assert JOBS_TOKEN in captured["url"]
    assert HERMES_TOKEN not in captured["url"]
    assert f'"chat_id": "{OWNER_CHAT}"' in captured["body"]
    assert PUBLIC_CHANNEL not in captured["body"]


def test_send_fails_closed_on_routing_error(monkeypatch):
    monkeypatch.setattr(
        telegram_routing,
        "resolve_job_route",
        lambda *a, **k: (_ for _ in ()).throw(
            telegram_routing.TelegramRoutingError("wrong bot")
        ),
    )
    assert telegram_routing.send_job_message("hello") is False


class _Clock:
    def __init__(self) -> None:
        self.value = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> dt.datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += dt.timedelta(seconds=seconds)


def test_auth_alert_confirmed_before_marker_and_deduplicated(tmp_path):
    state = tmp_path / "auth.json"
    sent: list[str] = []
    clock = _Clock()
    notifier = AuthNotifier(
        state_path=state,
        send=lambda text: sent.append(text) or True,
        now=clock,
    )

    assert notifier.notify_failure("HH_AUTH_REQUIRED") is True
    assert sent == [AUTH_FAILURE_MESSAGE]
    assert state.exists()
    assert notifier.notify_failure("HH_AUTH_REQUIRED again") is False
    clock.advance(13 * 3600)
    assert notifier.notify_failure("HH_AUTH_REQUIRED still") is True
    assert len(sent) == 2


def test_auth_alert_is_not_marked_when_delivery_fails(tmp_path):
    state = tmp_path / "auth.json"
    calls = {"n": 0}

    def flaky_send(text: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    notifier = AuthNotifier(state_path=state, send=flaky_send, now=_Clock())

    assert notifier.notify_failure("HH_AUTH_REQUIRED") is False
    assert not state.exists()
    assert notifier.notify_failure("HH_AUTH_REQUIRED") is True


def test_auth_recovery_sent_exactly_once(tmp_path):
    sent: list[str] = []
    notifier = AuthNotifier(
        state_path=tmp_path / "auth.json",
        send=lambda text: sent.append(text) or True,
        now=_Clock(),
    )
    assert notifier.notify_success() is False
    assert notifier.notify_failure("HH_AUTH_REQUIRED") is True
    assert notifier.notify_success() is True
    assert notifier.notify_success() is False
    assert sent == [AUTH_FAILURE_MESSAGE, AUTH_RECOVERED_MESSAGE]


def test_digest_sender_uses_jobs_route(monkeypatch):
    import integrations.hermes.job_search_fetcher as jsf

    route = telegram_routing.JobRoute(token=JOBS_TOKEN, chat_id=OWNER_CHAT)
    monkeypatch.setattr(telegram_routing, "resolve_job_route", lambda *a, **k: route)
    seen = {}

    def fake_post(url, payload, headers, timeout=20):
        seen["url"] = url
        seen["payload"] = payload
        return True

    monkeypatch.setattr(jsf, "_post_to_telegram", fake_post)
    assert jsf.send_to_telegram("digest") is True
    assert JOBS_TOKEN in seen["url"]
    assert seen["payload"]["chat_id"] == OWNER_CHAT


def test_digest_sender_fails_closed_without_jobs_route(monkeypatch):
    import integrations.hermes.job_search_fetcher as jsf

    def refuse(*args, **kwargs):
        raise telegram_routing.TelegramRoutingError("main Hermes token")

    monkeypatch.setattr(telegram_routing, "resolve_job_route", refuse)
    monkeypatch.setattr(
        jsf, "_post_to_telegram", lambda *a, **k: pytest.fail("must not send")
    )
    assert jsf.send_to_telegram("digest") is False
