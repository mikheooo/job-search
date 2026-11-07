"""Operational alerts for scheduled automation. Not an auth notifier.

HeadHunter application-cycle failures that are *not* a confirmed login loss,
and a Kwork inbox that stays unread, used to show up only in log files. This
module turns those into one owner message on the existing jobs-bot route,
then stays quiet until the cooldown ends or the job recovers.

It does not submit applications, send replies, or emit new-message cards.
``send_fn`` is injected. ``None`` or ``dry_run=True`` builds the text and
does not deliver it. A dry run also does not write the ledger, so a preview
cannot consume a live cooldown.

Secrets (bot tokens, key assignments, home-directory names) are stripped
before the text is stored or returned.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HH_FAILURE_COOLDOWN_SECONDS = 6 * 3600
KWORK_NOT_READY_STREAK = 3
KWORK_ALERT_COOLDOWN_SECONDS = 6 * 3600

_SECRET_PATTERNS = (
    re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(
        r"(?i)\b(token|api[_-]?key|password|passwd|secret|authorization|bearer)"
        r"\b\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    re.compile(r"/home/[^/\s]+"),
    re.compile(r"/Users/[^/\s]+"),
)

_KWORK_AUTH_STATUSES = {
    "AUTH_REQUIRED",
    "LOGIN_REQUIRED",
    "SESSION_EXPIRED",
    "AUTH_EXPIRED",
    "HH_AUTH_REQUIRED",
}
_KWORK_UNHEALTHY_STATUSES = {
    "PAGE_NOT_READY",
    "CDP_UNAVAILABLE",
    "UNKNOWN",
    "EXCEPTION",
    "ERROR",
    "FAILED",
    "UNAVAILABLE",
}
# The inbox monitor already pages AUTH_REQUIRED / AUTH_LOST on its own cooldown.
# The operational ledger must not send a second alert, and must not treat that
# status as proof the page read recovered.
_KWORK_MONITOR_AUTH_STATUSES = _KWORK_AUTH_STATUSES | {"AUTH_LOST"}
_KWORK_HEALTHY_STATUSES = {
    "OK",
    "CHECKED",
    "EMPTY",
    "NO_MESSAGES",
    "IDLE",
    "SUCCESS",
    "READY",
}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def sanitize_alert_text(text: str, *, limit: int = 400) -> str:
    """Collapse a failure into one log-safe line with secrets removed."""
    cleaned = str(text or "")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned or "unknown error"


def _code(text: str) -> str:
    """Literal span. The jobs-bot route parses Markdown, so underscores stay inside code."""
    literal = sanitize_alert_text(text).replace("`", "'")
    return f"`{literal}`"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _plan(
    state: dict[str, Any],
    *,
    unhealthy: bool,
    summary: str,
    now: float,
    cooldown_seconds: float,
    streak_required: int,
    failure_text: Callable[[int], str],
    recovery_text: str,
) -> dict[str, Any]:
    streak = int(state.get("streak") or 0)
    alerted_at = state.get("alerted_at")
    awaiting = bool(state.get("awaiting_recovery"))
    try:
        alerted_value = float(alerted_at) if alerted_at is not None else None
    except (TypeError, ValueError):
        alerted_value = None

    if unhealthy:
        streak += 1
        base = {
            "streak": streak,
            "last_summary": summary,
            "last_seen_at": now,
            "alerted_at": alerted_value,
            "awaiting_recovery": awaiting,
        }
        cooling = (
            awaiting
            and alerted_value is not None
            and (now - alerted_value) < cooldown_seconds
        )
        if streak < max(1, streak_required) or cooling:
            action = "none" if streak < max(1, streak_required) else "suppressed"
            return {"action": action, "text": "", "sent": False, "persist": base, "streak": streak}
        return {
            "action": "alert",
            "text": failure_text(streak),
            "sent": False,
            "streak": streak,
            "persist_if_sent": {
                **base,
                "alerted_at": now,
                "awaiting_recovery": True,
            },
            "persist_if_unsent": base,
        }

    cleared = {
        "streak": 0,
        "last_summary": "",
        "last_seen_at": now,
        "alerted_at": None,
        "awaiting_recovery": False,
    }
    if awaiting:
        return {
            "action": "recovery",
            "text": recovery_text,
            "sent": False,
            "streak": 0,
            "persist_if_sent": cleared,
            "persist_if_unsent": {
                "streak": 0,
                "last_summary": sanitize_alert_text(str(state.get("last_summary") or "")),
                "last_seen_at": now,
                "alerted_at": alerted_value,
                "awaiting_recovery": True,
            },
        }
    # A partial streak that never paged still has to go back to zero.
    # An already-clear ledger is left untouched.
    return {
        "action": "none",
        "text": "",
        "sent": False,
        "streak": 0,
        "persist": None if streak == 0 else cleared,
    }


def _deliver(send_fn: Callable[[str], bool] | None, text: str) -> bool:
    if send_fn is None or not text:
        return False
    try:
        return bool(send_fn(text))
    except Exception as exc:  # noqa: BLE001 - a failed send must not crash the job
        logger.warning(
            "Operational alert delivery failed: %s", type(exc).__name__
        )
        return False


def apply_observation(
    *,
    unhealthy: bool,
    summary: str,
    state_path: Path,
    send_fn: Callable[[str], bool] | None,
    failure_text: Callable[[int], str],
    recovery_text: str,
    dry_run: bool = False,
    cooldown_seconds: float = HH_FAILURE_COOLDOWN_SECONDS,
    streak_required: int = 1,
    now: float | None = None,
) -> dict[str, Any]:
    """Advance one channel ledger and maybe deliver a single message."""
    import time

    moment = time.time() if now is None else float(now)
    safe_summary = sanitize_alert_text(summary) if summary else ""
    plan = _plan(
        _read_state(state_path),
        unhealthy=unhealthy,
        summary=safe_summary,
        now=moment,
        cooldown_seconds=float(cooldown_seconds),
        streak_required=int(streak_required),
        failure_text=failure_text,
        recovery_text=recovery_text,
    )
    action = str(plan["action"])
    if dry_run:
        if action == "alert":
            action = "would_alert"
        elif action == "recovery":
            action = "would_recover"
        plan["action"] = action
        plan["sent"] = False
        plan["text"] = plan.get("text") or ""
        return plan

    if action in {"alert", "recovery"}:
        sent = _deliver(send_fn, str(plan.get("text") or ""))
        persist = plan["persist_if_sent"] if sent else plan["persist_if_unsent"]
        _write_state(state_path, persist)
        plan["sent"] = sent
        plan["action"] = action if sent else "delivery_failed"
        return plan

    if plan.get("persist") is not None:
        _write_state(state_path, plan["persist"])
    plan["sent"] = False
    return plan


def hh_failure_text(summary: str, streak: int, submitted: int | None) -> str:
    lines = [
        "⚠️ HH Autopilot: цикл заявок завершился с ошибкой",
        "",
        "Это не потеря авторизации. Повторный вход не требуется.",
        f"Ошибка: {_code(summary)}",
    ]
    if submitted is not None:
        lines.append(f"Отправлено откликов в этом цикле: {int(submitted)}")
    lines.append(f"Повтор подряд: {int(streak)}")
    lines.append("Лог: `logs/hh_autopilot.log`")
    lines.append("Одинаковые сбои не повторяют это сообщение до конца кулдауна.")
    return "\n".join(lines)


def hh_recovery_text() -> str:
    return "✅ HH Autopilot: цикл заявок снова завершился без этой ошибки."


def notify_hh_non_auth_failure(
    error: str,
    *,
    state_path: Path,
    send_fn: Callable[[str], bool] | None,
    submitted: int | None = None,
    dry_run: bool = False,
    cooldown_seconds: float = HH_FAILURE_COOLDOWN_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """First non-auth cycle failure alerts; repeats inside the cooldown do not."""
    summary = sanitize_alert_text(error)

    def _text(streak: int) -> str:
        return hh_failure_text(summary, streak, submitted)

    return apply_observation(
        unhealthy=True,
        summary=summary,
        state_path=state_path,
        send_fn=send_fn,
        failure_text=_text,
        recovery_text=hh_recovery_text(),
        dry_run=dry_run,
        cooldown_seconds=cooldown_seconds,
        streak_required=1,
        now=now,
    )


def notify_hh_non_auth_recovery(
    *,
    state_path: Path,
    send_fn: Callable[[str], bool] | None,
    dry_run: bool = False,
    cooldown_seconds: float = HH_FAILURE_COOLDOWN_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """One recovery note after an operational alert, then silence."""
    return apply_observation(
        unhealthy=False,
        summary="",
        state_path=state_path,
        send_fn=send_fn,
        failure_text=lambda streak: hh_failure_text("", streak, None),
        recovery_text=hh_recovery_text(),
        dry_run=dry_run,
        cooldown_seconds=cooldown_seconds,
        streak_required=1,
        now=now,
    )


def classify_kwork_inbox(
    status: str | None,
    checked: int | None,
    error: str | None = None,
) -> tuple[bool, str]:
    """Return whether this inbox read is an operational failure, and why.

    A confirmed empty inbox (status CHECKED/OK and zero rows) is healthy.
    An auth-shaped status is ignored here so this alert cannot claim that
    the Kwork session expired.
    """
    if error and not str(status or "").strip():
        status = "EXCEPTION"
    norm = str(status or "").strip().upper().replace("-", "_").replace(" ", "_")
    if norm in _KWORK_MONITOR_AUTH_STATUSES:
        return False, "ignored_auth_status"
    if norm == "PAGE_NOT_READY":
        return True, "page_not_ready"
    if norm in _KWORK_UNHEALTHY_STATUSES:
        return True, "operational_failure"
    if checked == 0 and norm in _KWORK_HEALTHY_STATUSES:
        return False, "empty_but_ready"
    return False, "ready"


def kwork_failure_text(summary: str, streak: int, status: str, checked: int | None) -> str:
    context = _code(summary) if summary else "нет"
    checked_text = "не передано" if checked is None else str(int(checked))
    return "\n".join((
        "⚠️ Kwork inbox: проверка не проходит",
        "",
        (
            "Страница не готова или чтение сорвалось. Это не карточка нового сообщения "
            "и не признак истёкшей авторизации."
        ),
        f"Статус: {_code(status or 'UNKNOWN')}",
        f"Проверено: {checked_text}",
        f"Подряд: {int(streak)}",
        f"Контекст: {context}",
        "Новые сообщения по-прежнему уходят отдельным уведомлением, когда чтение проходит.",
    ))


def kwork_recovery_text() -> str:
    return "✅ Kwork inbox: проверка снова проходит. Служебное предупреждение снято."


def kwork_report_should_update_health(auth_status: str | None) -> bool:
    """False when the monitor owns the alert (login loss), so we leave the ledger alone."""
    norm = str(auth_status or "").strip().upper().replace("-", "_").replace(" ", "_")
    return norm not in _KWORK_MONITOR_AUTH_STATUSES


def observe_kwork_inbox(
    *,
    status: str | None,
    checked: int | None,
    state_path: Path,
    send_fn: Callable[[str], bool] | None,
    error: str | None = None,
    dry_run: bool = False,
    cooldown_seconds: float = KWORK_ALERT_COOLDOWN_SECONDS,
    streak_required: int = KWORK_NOT_READY_STREAK,
    now: float | None = None,
) -> dict[str, Any]:
    """Alert only after ``streak_required`` consecutive unhealthy reads."""
    unhealthy, reason = classify_kwork_inbox(status, checked, error)
    summary = sanitize_alert_text(error or status or reason)
    shown_status = status or ("EXCEPTION" if error else "")

    def _text(streak: int) -> str:
        return kwork_failure_text(summary, streak, shown_status or "UNKNOWN", checked)

    decision = apply_observation(
        unhealthy=unhealthy,
        summary=summary,
        state_path=state_path,
        send_fn=send_fn,
        failure_text=_text,
        recovery_text=kwork_recovery_text(),
        dry_run=dry_run,
        cooldown_seconds=cooldown_seconds,
        streak_required=streak_required,
        now=now,
    )
    decision["reason"] = reason
    decision["unhealthy"] = unhealthy
    return decision


def deliver_owner_alert(text: str) -> bool:
    """Existing HH jobs-bot route. Lazy import: this checkout may lack it.

    ``ai_assistant.telegram_routing`` is not in the repository. Production
    machines that already send auth alerts have that local module, and
    ``send_telegram_alert`` is the supported way to reach it. A missing
    module returns False and does not invent a second transport.
    """
    try:
        from ai_assistant.hh_autopilot import send_telegram_alert
    except Exception as exc:  # noqa: BLE001 - missing local routing must not crash the job
        logger.warning(
            "Owner alert route unavailable: %s", type(exc).__name__
        )
        return False
    try:
        return bool(send_telegram_alert(text))
    except Exception as exc:  # noqa: BLE001 - delivery failures are reported by the ledger
        logger.warning(
            "Owner alert route raised %s", type(exc).__name__
        )
        return False
