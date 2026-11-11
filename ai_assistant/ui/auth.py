"""Shared authentication and loopback verification helpers for Job Search web interfaces."""
from __future__ import annotations

import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import config

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_dashboard_token() -> str:
    """Токен дашборда. env приоритетнее config: config читает env только на импорте."""
    return os.getenv("DASHBOARD_TOKEN", "").strip() or getattr(config, "DASHBOARD_TOKEN", "").strip()


def request_dashboard_token(request: Request) -> str:
    """Токен из заголовка: Authorization: Bearer <token> либо X-API-Key: <token>."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def dashboard_token_ok(request: Request) -> bool:
    configured_token = get_dashboard_token()
    if not configured_token:
        return False
    token = request_dashboard_token(request)
    return bool(token) and secrets.compare_digest(token, configured_token)


def is_loopback_host(host: str | None) -> bool:
    """Return True if host binds strictly to local loopback interface."""
    if not host:
        return True
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


def dashboard_require_auth_for_reads() -> bool:
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    if not is_loopback_host(host):
        return True
    raw = os.getenv("DASHBOARD_REQUIRE_AUTH_FOR_READS", "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes")
    return bool(getattr(config, "DASHBOARD_REQUIRE_AUTH_FOR_READS", False))


async def require_dashboard_token_middleware(request: Request, call_next):
    """Токен-авторизация для /api/.

    Мутации (POST/PUT/PATCH/DELETE) — всегда.
    Читающие запросы — при не-loopback хосте или DASHBOARD_REQUIRE_AUTH_FOR_READS=1.
    OPTIONS (CORS preflight) — пропускаются.
    Не-/api/ пути — пропускаются.
    """
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    if request.method == "OPTIONS":
        return await call_next(request)

    if request.method not in MUTATING_METHODS and not dashboard_require_auth_for_reads():
        return await call_next(request)

    if not get_dashboard_token():
        return JSONResponse(
            status_code=503,
            content={"detail": "DASHBOARD_TOKEN not configured"},
        )

    if not dashboard_token_ok(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized: invalid or missing dashboard token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await call_next(request)
