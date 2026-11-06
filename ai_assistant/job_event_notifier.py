"""Idempotent Job Search event notifications for the dedicated jobs bot.

Used by the HH Chat Monitor cron. Unlike the legacy ``hermes send`` route, this
module never touches the main Hermes bot and records every event before the one
Telegram POST. A duplicate event is skipped; an ambiguous outcome is never
retried automatically.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from ai_assistant import db, telegram_routing

EVENT_PREFIX = "job_event:"
DEFAULT_NOTIFICATION_TYPE = "employer_reply"


class JobEventNotificationError(RuntimeError):
    """The event cannot be represented safely (empty key/text)."""


@dataclass(frozen=True)
class JobEventNotificationResult:
    status: str
    detail: str = ""
    message_id: int | None = None
    delivery_key: str = ""
    record_status: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("delivered", "duplicate")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _event_payload(
    *,
    event_key: str,
    source: str,
    company: str,
    role: str,
    chat_url: str,
    text: str,
    extra: dict[str, Any] | None,
    outcome: str,
    message_id: int | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_key": event_key,
        "source": source,
        "company": company,
        "role": role,
        "chat_url": chat_url,
        "message_text": text,
        "outcome": outcome,
    }
    if message_id is not None:
        payload["message_id"] = message_id
    if extra:
        payload.update(extra)
    return payload


def notify_job_event(
    event_key: str,
    text: str,
    *,
    notification_type: str = DEFAULT_NOTIFICATION_TYPE,
    source: str = "",
    company: str = "",
    role: str = "",
    chat_url: str = "",
    payload: dict[str, Any] | None = None,
    retry_failed: bool = False,
    route: telegram_routing.JobRoute | None = None,
    opener: Callable[..., Any] | None = None,
    timeout: int = 15,
    conn: sqlite3.Connection | None = None,
    now: Callable[[], str] = _now_iso,
) -> JobEventNotificationResult:
    """Send one employer-event notification, at most once.

    Lifecycle of ``telegram_delivery_records.job_event:<event_key>``:
    ATTEMPTING -> DELIVERED | FAILED | AMBIGUOUS.

    DELIVERED, ATTEMPTING and AMBIGUOUS are terminal for automatic runs. A
    confirmed FAILED record can be retried only with ``retry_failed=True`` after
    an operator has fixed the cause.
    """
    key = str(event_key or "").strip()
    message = str(text or "").strip()
    if not key:
        raise JobEventNotificationError("event_key is required")
    if any(ch in key for ch in "<>") or any(ch.isspace() for ch in key):
        raise JobEventNotificationError(
            "event_key contains an unresolved placeholder or whitespace"
        )
    if key.lower() in {"gmail:id", "gmail:<id>", "gmail:...", "gmail:"}:
        raise JobEventNotificationError(
            "event_key contains an unresolved Gmail placeholder; refusing to send"
        )
    if not message:
        raise JobEventNotificationError("notification text is empty")

    try:
        target = route or telegram_routing.resolve_job_route()
    except telegram_routing.TelegramRoutingError as exc:
        return JobEventNotificationResult("unconfigured", str(exc))

    delivery_key = EVENT_PREFIX + key
    owns_conn = conn is None
    if conn is None:
        db.init_db()
        conn = db.get_connection()

    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, 'ATTEMPTING', '{}')
            """,
            (delivery_key, notification_type, target.chat_id, now()),
        )
        conn.commit()
        reserved = cur.rowcount == 1

        if not reserved:
            row = conn.execute(
                "SELECT status FROM telegram_delivery_records WHERE delivery_key = ?",
                (delivery_key,),
            ).fetchone()
            previous = str(row[0]) if row else "UNKNOWN"
            if previous == "FAILED" and retry_failed:
                cur = conn.execute(
                    """
                    UPDATE telegram_delivery_records
                       SET status = 'ATTEMPTING', delivered_at = ?
                     WHERE delivery_key = ? AND status = 'FAILED'
                    """,
                    (now(), delivery_key),
                )
                conn.commit()
                reserved = cur.rowcount == 1
            if not reserved:
                detail = (
                    f"event already recorded as {previous}; automatic resend blocked"
                )
                status = "duplicate" if previous in ("DELIVERED", "AMBIGUOUS", "ATTEMPTING") else "skipped"
                return JobEventNotificationResult(
                    status,
                    detail,
                    delivery_key=delivery_key,
                    record_status=previous,
                )

        send_result = telegram_routing.send_job_message_result(
            message,
            parse_mode=None,
            route=target,
            opener=opener,
            timeout=timeout,
        )

        if send_result.status == "delivered":
            record_status = "DELIVERED"
        elif send_result.status == "ambiguous":
            record_status = "AMBIGUOUS"
        else:
            record_status = "FAILED"

        record = _event_payload(
            event_key=key,
            source=source,
            company=company,
            role=role,
            chat_url=chat_url,
            text=message,
            extra=payload,
            outcome=send_result.status,
            message_id=send_result.message_id,
        )
        try:
            conn.execute(
                """
                UPDATE telegram_delivery_records
                   SET status = ?, delivered_at = ?, payload = ?
                 WHERE delivery_key = ? AND status = 'ATTEMPTING'
                """,
                (
                    record_status,
                    now(),
                    json.dumps(record, ensure_ascii=False),
                    delivery_key,
                ),
            )
            conn.commit()
        except sqlite3.Error as exc:
            return JobEventNotificationResult(
                "record_failed",
                f"Telegram outcome={send_result.status}, DB record failed: {exc}",
                message_id=send_result.message_id,
                delivery_key=delivery_key,
                record_status="ATTEMPTING",
            )

        return JobEventNotificationResult(
            send_result.status,
            send_result.detail,
            message_id=send_result.message_id,
            delivery_key=delivery_key,
            record_status=record_status,
        )
    finally:
        if owns_conn:
            conn.close()
