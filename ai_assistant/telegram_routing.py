"""Strict Telegram routing for Job Search notifications.

Every Job Search notification belongs to the dedicated jobs bot (@jobsassistbot).
This module is the single place that resolves its credentials. It deliberately
has no fallback to the main Hermes bot or to a public channel: a missing or
ambiguous configuration is an error, not something to work around.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

HERMES_ENV_FILE = pathlib.Path.home() / "AppData" / "Local" / "hermes" / ".env"
TOOLS_ENV_FILE = pathlib.Path.home() / "tools" / ".env"


class TelegramRoutingError(RuntimeError):
    """Job Search Telegram credentials are missing or point at the wrong bot."""


@dataclass(frozen=True)
class JobRoute:
    token: str
    chat_id: str

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/sendMessage"


@dataclass(frozen=True)
class JobSendResult:
    """Outcome of exactly one Telegram POST attempt.

    ``ambiguous`` means the request may have reached Telegram (timeout, reset,
    truncated response). Callers must not retry an ambiguous event
    automatically, otherwise a server-side write can become a duplicate.
    """

    ok: bool
    status: str
    detail: str = ""
    message_id: int | None = None


def _normalize_token(value: str | None) -> str:
    token = (value or "").strip()
    if token.lower().startswith("bot") and ":" in token:
        token = token[3:]
    return token


def _read_env_token(path: pathlib.Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^TELEGRAM_BOT_TOKEN\s*=\s*(.+)$", text, re.MULTILINE)
    return _normalize_token(match.group(1).strip().strip('"').strip("'")) if match else ""


def main_hermes_tokens() -> set[str]:
    """Tokens that belong to the main Hermes bot and must never send job mail."""
    tokens = set()
    for path in (HERMES_ENV_FILE, TOOLS_ENV_FILE):
        token = _read_env_token(path)
        if token:
            tokens.add(token)
    return tokens


def resolve_job_route(
    config_module: Any | None = None,
    *,
    forbidden_tokens: set[str] | None = None,
) -> JobRoute:
    """Resolve the dedicated jobs bot route or raise ``TelegramRoutingError``.

    ``HH_TELEGRAM_BOT_TOKEN`` is the dedicated token. Falling back to the
    project ``TELEGRAM_BOT_TOKEN`` is allowed only when it is not the main
    Hermes token, so a half-migrated environment fails closed instead of
    leaking job notifications into the personal Hermes chat.
    """
    if config_module is None:
        from ai_assistant import config as config_module

    token = _normalize_token(getattr(config_module, "HH_TELEGRAM_BOT_TOKEN", ""))
    if not token:
        token = _normalize_token(getattr(config_module, "TELEGRAM_BOT_TOKEN", ""))
    if not token:
        raise TelegramRoutingError("Job Search Telegram bot token is not configured")

    chat_id = str(getattr(config_module, "HH_TELEGRAM_CHAT_ID", "") or "").strip()
    if not chat_id:
        raise TelegramRoutingError("Job Search Telegram chat id is not configured")

    blocked = main_hermes_tokens() if forbidden_tokens is None else set(forbidden_tokens)
    if token in blocked:
        raise TelegramRoutingError(
            "Job Search is configured with the main Hermes bot token; refusing to send"
        )

    channel_id = str(getattr(config_module, "TELEGRAM_CHANNEL_ID", "") or "").strip()
    if channel_id and chat_id == channel_id:
        raise TelegramRoutingError(
            "Job Search notifications must target the @jobsassistbot chat, not a channel"
        )

    return JobRoute(token=token, chat_id=chat_id)


def job_credentials() -> tuple[str, str]:
    route = resolve_job_route()
    return route.token, route.chat_id


def _is_ambiguous_transport_error(exc: BaseException) -> bool:
    """True when Telegram may have accepted the message despite the exception."""
    if isinstance(exc, urllib.error.HTTPError):
        code = getattr(exc, "code", 0)
        return code >= 500
    try:
        import http.client

        if isinstance(
            exc,
            (
                socket.timeout,
                TimeoutError,
                ConnectionResetError,
                BrokenPipeError,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
                http.client.BadStatusLine,
            ),
        ):
            return True
    except Exception:
        logging.getLogger(__name__).debug("http.client import failed during transport classification")
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        text = str(reason).lower()
        return "timed out" in text or "timeout" in text
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def send_job_message_result(
    text: str,
    *,
    parse_mode: str | None = "HTML",
    disable_notification: bool = False,
    reply_markup: dict[str, Any] | None = None,
    route: JobRoute | None = None,
    opener: Callable[..., Any] | None = None,
    timeout: int = 10,
) -> JobSendResult:
    """Send exactly one message through the jobs bot, with a structured result.

    There is no retry here and no fallback to the main Hermes bot. Confirmed
    failures are ``failed``/``auth_failed``/``rate_limited``; transport
    uncertainty is ``ambiguous`` and must be recorded without a resend.
    """
    try:
        target = route or resolve_job_route()
    except TelegramRoutingError as exc:
        return JobSendResult(False, "unconfigured", str(exc))

    payload: dict[str, Any] = {
        "chat_id": target.chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_notification:
        payload["disable_notification"] = True
    if reply_markup:
        payload["reply_markup"] = reply_markup

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        target.api_url, data=data, headers={"Content-Type": "application/json"}
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                outcome = "ambiguous" if status >= 500 else "failed"
                return JobSendResult(False, outcome, f"HTTP {status}")
            message_id = None
            reader = getattr(response, "read", None)
            if reader:
                try:
                    body = json.loads(reader().decode("utf-8"))
                    message_id = (body.get("result") or {}).get("message_id")
                    if body.get("ok") is False:
                        return JobSendResult(
                            False, "failed", str(body.get("description") or "Telegram ok=false")
                        )
                except Exception:
                    # HTTP 200 already confirms delivery even if the ack body
                    # cannot be parsed. Do not turn success into ambiguity.
                    logging.getLogger(__name__).debug(
                        "Telegram ack body was not parsed; delivery stays confirmed"
                    )
            return JobSendResult(True, "delivered", "HTTP 200", message_id)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return JobSendResult(False, "auth_failed", f"HTTP {exc.code}", None)
        if exc.code == 429:
            return JobSendResult(False, "rate_limited", "HTTP 429", None)
        if _is_ambiguous_transport_error(exc):
            return JobSendResult(False, "ambiguous", f"HTTP {exc.code}", None)
        return JobSendResult(False, "failed", f"HTTP {exc.code}", None)
    except Exception as exc:
        status = "ambiguous" if _is_ambiguous_transport_error(exc) else "failed"
        return JobSendResult(False, status, f"{type(exc).__name__}: {exc}", None)


def send_job_message(
    text: str,
    *,
    parse_mode: str | None = "HTML",
    disable_notification: bool = False,
    reply_markup: dict[str, Any] | None = None,
    route: JobRoute | None = None,
    opener: Callable[..., Any] | None = None,
    timeout: int = 10,
) -> bool:
    """Backward-compatible boolean wrapper around one job-bot POST."""
    return send_job_message_result(
        text,
        parse_mode=parse_mode,
        disable_notification=disable_notification,
        reply_markup=reply_markup,
        route=route,
        opener=opener,
        timeout=timeout,
    ).ok
