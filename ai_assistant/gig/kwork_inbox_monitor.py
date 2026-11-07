"""Kwork Inbox Monitor & Real-Time Buyer Inquiry Tracker.

Monitors active dialogues and new incoming buyer messages on Kwork:
1. Reuses existing authorized browser session on CDP (127.0.0.1:9111).
   - Zero new competing browser instances or profiles.
   - Zero cookie re-extraction or copying.
   - Respects existing browser leases and locks.
2. Identifies new buyer messages:
   - Differentiates incoming buyer inquiries from our own outgoing responses.
   - Tracks multiple active projects/orders and general inbox conversations simultaneously.
3. Reliability & Persistence:
   - Persistent SQLite deduplication (`kwork_inbox_messages` table).
   - Distinct detection (`PENDING`) and delivery (`DELIVERED`) states.
   - Messages are never marked delivered until Telegram confirmation is received.
   - Handles network failures, session expiry, and ambiguous delivery gracefully.
   - Single clear authorization warning with configurable cooldown (no alert spam).
4. Strict Telegram Routing:
   - Alerts routed exclusively through `ai_assistant.telegram_routing` to `@jobsassistbot`.
   - Never falls back to main Hermes or any channel.
   - If configuration is missing or pointing to Hermes, queues event without sending.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import pathlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .. import telegram_routing

logger = logging.getLogger("kwork_inbox_monitor")

DEFAULT_DB_PATH = pathlib.Path(__file__).resolve().parents[2] / "artifacts" / "kwork_inbox.db"
DEFAULT_CDP_URL = "http://127.0.0.1:9111"
AUTH_WARNING_COOLDOWN_SECONDS = 3600  # 1 hour cooldown for auth warning


class KworkMonitorError(RuntimeError):
    """Base error for Kwork inbox monitor."""


class KworkAuthLostError(KworkMonitorError):
    """Kwork session is unauthenticated or cookies expired."""


@dataclass
class KworkMessage:
    """Represents a single message event in Kwork inbox or order chat."""

    message_key: str
    dialogue_id: str
    project_id: str
    project_title: str
    buyer_name: str
    message_id: str
    text: str
    is_outgoing: bool
    chat_url: str
    detected_at: str
    delivery_status: str  # "PENDING", "DELIVERED", "FAILED", "IGNORED", "AMBIGUOUS"
    unread_count: int = 1
    delivered_at: str | None = None
    delivery_error: str | None = None
    telegram_message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KworkScanResult:
    """Outcome of one inbox scan."""

    is_authenticated: bool
    auth_error: str = ""
    messages: list[KworkMessage] = None
    active_dialogues_count: int = 0
    unread_count: int = 0
    auth_status: str = "OK"  # "OK", "AUTH_REQUIRED", "CDP_UNAVAILABLE", "PAGE_NOT_READY", "UNKNOWN"

    def __post_init__(self):
        if self.messages is None:
            self.messages = []
        if not self.is_authenticated and self.auth_status == "OK":
            self.auth_status = "UNKNOWN"


@dataclass
class KworkMonitorCycleReport:
    """Summary of one monitoring cycle."""

    timestamp: str
    scanned_dialogues: int
    new_detected: int
    alerts_sent: int
    alerts_pending: int
    alerts_failed: int
    auth_status: str  # "OK", "AUTH_LOST", "CDP_UNAVAILABLE"
    details: list[str]


class KworkInboxStorage:
    """Thread-safe and process-safe SQLite persistence for Kwork messages."""

    def __init__(self, db_path: pathlib.Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kwork_inbox_messages (
                    message_key TEXT PRIMARY KEY,
                    dialogue_id TEXT NOT NULL,
                    project_id TEXT,
                    project_title TEXT,
                    buyer_name TEXT,
                    message_id TEXT NOT NULL,
                    text TEXT,
                    is_outgoing INTEGER NOT NULL,
                    chat_url TEXT,
                    detected_at TEXT NOT NULL,
                    delivery_status TEXT NOT NULL,
                    unread_count INTEGER DEFAULT 1,
                    delivered_at TEXT,
                    delivery_error TEXT,
                    telegram_message_id INTEGER
                )
            """)
            try:
                conn.execute("ALTER TABLE kwork_inbox_messages ADD COLUMN unread_count INTEGER DEFAULT 1")
            except Exception as exc:
                logger.debug("unread_count column already present: %s", type(exc).__name__)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kwork_monitor_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                )
            """)

    def _row_to_message(self, row: sqlite3.Row) -> KworkMessage:
        return KworkMessage(
            message_key=row["message_key"],
            dialogue_id=row["dialogue_id"],
            project_id=row["project_id"] or "",
            project_title=row["project_title"] or "",
            buyer_name=row["buyer_name"] or "",
            message_id=row["message_id"],
            text=row["text"] or "",
            is_outgoing=bool(row["is_outgoing"]),
            chat_url=row["chat_url"] or "",
            detected_at=row["detected_at"],
            delivery_status=row["delivery_status"],
            # sqlite3.Row has no .get, and `key in row` checks values, not column names.
unread_count=row["unread_count"] if "unread_count" in row.keys() else 1,  # noqa: SIM118
            delivered_at=row["delivered_at"],
            delivery_error=row["delivery_error"],
            telegram_message_id=row["telegram_message_id"],
        )

    def is_known(self, message_key: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM kwork_inbox_messages WHERE message_key = ?", (message_key,))
            return cur.fetchone() is not None

    def get_message(self, message_key: str) -> KworkMessage | None:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM kwork_inbox_messages WHERE message_key = ?", (message_key,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_message(row)

    def save_message(self, msg: KworkMessage) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO kwork_inbox_messages (
                    message_key, dialogue_id, project_id, project_title, buyer_name,
                    message_id, text, is_outgoing, chat_url, detected_at,
                    delivery_status, unread_count, delivered_at, delivery_error, telegram_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.message_key,
                    msg.dialogue_id,
                    msg.project_id,
                    msg.project_title,
                    msg.buyer_name,
                    msg.message_id,
                    msg.text,
                    1 if msg.is_outgoing else 0,
                    msg.chat_url,
                    msg.detected_at,
                    msg.delivery_status,
                    getattr(msg, "unread_count", 1),
                    msg.delivered_at,
                    msg.delivery_error,
                    msg.telegram_message_id,
                ),
            )

    def mark_delivered(self, message_key: str, telegram_message_id: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE kwork_inbox_messages
                SET delivery_status = 'DELIVERED', delivered_at = ?, delivery_error = NULL, telegram_message_id = ?
                WHERE message_key = ?
                """,
                (now, telegram_message_id, message_key),
            )

    def mark_failed(self, message_key: str, error: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE kwork_inbox_messages
                SET delivery_status = 'FAILED', delivery_error = ?
                WHERE message_key = ?
                """,
                (error, message_key),
            )

    def mark_ambiguous(self, message_key: str, error: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE kwork_inbox_messages
                SET delivery_status = 'AMBIGUOUS', delivery_error = ?
                WHERE message_key = ?
                """,
                (error, message_key),
            )

    def get_pending_messages(self) -> list[KworkMessage]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM kwork_inbox_messages
                WHERE delivery_status IN ('PENDING', 'FAILED') AND is_outgoing = 0
                ORDER BY detected_at ASC
                """
            )
            rows = cur.fetchall()
            return [self._row_to_message(r) for r in rows]

    def get_ambiguous_messages(self) -> list[KworkMessage]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM kwork_inbox_messages
                WHERE delivery_status = 'AMBIGUOUS'
                ORDER BY detected_at DESC
                """
            )
            rows = cur.fetchall()
            return [self._row_to_message(r) for r in rows]

    def resolve_ambiguous(self, message_key: str, resolution: str) -> bool:
        """Manually reconcile an AMBIGUOUS delivery event.

        resolution must be one of:
        - "MARK_DELIVERED": Operator verified message reached Telegram chat.
        - "RETRY_DELIVERY": Operator verified message was not received, requeue as PENDING.
        - "DISCARD": Operator chose to ignore.
        """
        res_upper = resolution.upper().strip()
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT delivery_status FROM kwork_inbox_messages WHERE message_key = ?", (message_key,))
            row = cur.fetchone()
            if not row or row["delivery_status"] != "AMBIGUOUS":
                return False

            if res_upper == "MARK_DELIVERED":
                conn.execute(
                    """
                    UPDATE kwork_inbox_messages
                    SET delivery_status = 'DELIVERED', delivered_at = ?, delivery_error = 'manually reconciled: MARK_DELIVERED'
                    WHERE message_key = ?
                    """,
                    (now, message_key),
                )
            elif res_upper == "RETRY_DELIVERY":
                conn.execute(
                    """
                    UPDATE kwork_inbox_messages
                    SET delivery_status = 'PENDING', delivery_error = 'manually reconciled: RETRY_DELIVERY'
                    WHERE message_key = ?
                    """,
                    (message_key,),
                )
            elif res_upper == "DISCARD":
                conn.execute(
                    """
                    UPDATE kwork_inbox_messages
                    SET delivery_status = 'IGNORED', delivery_error = 'manually reconciled: DISCARD'
                    WHERE message_key = ?
                    """,
                    (message_key,),
                )
            else:
                raise ValueError(f"Unknown resolution '{resolution}'. Valid: MARK_DELIVERED, RETRY_DELIVERY, DISCARD")
            return True

    def get_state(self, key: str) -> str | None:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM kwork_monitor_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO kwork_monitor_state (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, now),
            )


def format_kwork_alert(msg: KworkMessage) -> str:
    """Format rich HTML alert card for @jobsassistbot."""
    project_label = f"#{msg.project_id} — {html.escape(msg.project_title)}" if msg.project_id else html.escape(msg.project_title or "Входящий диалог")
    buyer_label = html.escape(msg.buyer_name or "Заказчик Kwork")
    preview_text = html.escape(msg.text.strip()) if msg.text else "<i>[Вложение или пустое сообщение]</i>"
    chat_url = html.escape(msg.chat_url or "https://kwork.ru/inbox")

    msg_header = "💬 <b>Сообщение:</b>"
    if getattr(msg, "unread_count", 1) > 1:
        msg_header = f"💬 <b>Последнее сообщение</b> <i>(всего новых: {msg.unread_count})</i>:"

    lines = [
        "🚨 <b>KWORK: Новое сообщение от заказчика!</b>\n",
        f"💼 <b>Заказ:</b> {project_label}",
        f"👤 <b>Заказчик:</b> {buyer_label}\n",
        msg_header,
        f"<i>«{preview_text}»</i>\n",
        "⚠️ <b>Внимание:</b> Требуется ответ исполнителя!\n",
        f"🔗 <a href=\"{chat_url}\">Открыть переписку на Kwork</a>",
    ]
    return "\n".join(lines)


def get_ambiguous_diagnostic_report(storage: KworkInboxStorage) -> dict[str, Any]:
    """Generate diagnostic summary of all AMBIGUOUS events for operator review."""
    ambig_list = storage.get_ambiguous_messages()
    return {
        "ambiguous_count": len(ambig_list),
        "needs_operator_review": len(ambig_list) > 0,
        "items": [
            {
                "message_key": m.message_key,
                "project_id": m.project_id,
                "project_title": m.project_title,
                "buyer_name": m.buyer_name,
                "detected_at": m.detected_at,
                "text_preview": m.text[:120],
                "chat_url": m.chat_url,
                "delivery_error": m.delivery_error,
                "recommended_action": (
                    f"Check Telegram chat. If message was received: "
                    f"reconcile_ambiguous_message(storage, '{m.message_key}', 'MARK_DELIVERED'). "
                    f"If not received: "
                    f"reconcile_ambiguous_message(storage, '{m.message_key}', 'RETRY_DELIVERY')."
                ),
            }
            for m in ambig_list
        ],
    }


def reconcile_ambiguous_message(
    storage: KworkInboxStorage,
    message_key: str,
    resolution: str,
) -> bool:
    """Safe manual reconciliation procedure for an ambiguous event."""
    return storage.resolve_ambiguous(message_key, resolution)


def format_auth_warning(diagnostic_reason: str = "открыта страница входа (/login)") -> str:
    """Format single alert when Kwork session is confirmed unauthenticated."""
    return (
        "⚠️ <b>KWORK: Требуется авторизация!</b>\n\n"
        f"Диагностика: {diagnostic_reason}\n"
        "Действие: выполните вход в аккаунт на https://kwork.ru в основном окне браузера.\n"
        "Монитор входящих сообщений приостановлен до подтверждения входа."
    )


def format_auth_recovered() -> str:
    """Format single recovery notification when Kwork session is confirmed active."""
    return (
        "✅ <b>Kwork: авторизация подтверждена</b>\n\n"
        "Сессия на https://kwork.ru активна, монитор входящих сообщений возобновил работу."
    )


def dispatch_kwork_alert(
    msg: KworkMessage,
    storage: KworkInboxStorage,
    sender_fn: Callable[..., Any] | None = None,
) -> bool:
    """Send alert strictly via @jobsassistbot and update storage delivery status."""
    text = format_kwork_alert(msg)
    send_fn = sender_fn or telegram_routing.send_job_message_result

    try:
        res = send_fn(text, parse_mode="HTML")
    except Exception as exc:
        logger.error(f"Telegram transport exception for {msg.message_key}: {exc}")
        storage.mark_failed(msg.message_key, error=str(exc))
        return False

    if getattr(res, "ok", False):
        storage.mark_delivered(msg.message_key, telegram_message_id=getattr(res, "message_id", None))
        logger.info(f"Delivered alert for message {msg.message_key}")
        return True

    status = getattr(res, "status", "failed")
    detail = getattr(res, "detail", "")
    if status == "unconfigured":
        logger.error(f"Job Search bot unconfigured or Hermes token blocked: {detail}")
        storage.mark_failed(msg.message_key, error=f"unconfigured: {detail}")
        return False
    elif status == "ambiguous":
        logger.warning(f"Ambiguous send result for {msg.message_key}: {detail}")
        storage.mark_ambiguous(msg.message_key, error=f"ambiguous: {detail}")
        return False
    else:
        logger.warning(f"Telegram send failed for {msg.message_key}: {status} {detail}")
        storage.mark_failed(msg.message_key, error=f"{status}: {detail}")
        return False


def handle_auth_lost(
    storage: KworkInboxStorage,
    sender_fn: Callable[..., Any] | None = None,
    cooldown_seconds: int = AUTH_WARNING_COOLDOWN_SECONDS,
    reason: str = "открыта страница входа (/login)",
) -> bool:
    """Send a single auth warning with cooldown to avoid spamming."""
    last_raw = storage.get_state("last_auth_warning_at")
    now_ts = time.time()
    if last_raw:
        try:
            last_dt = datetime.fromisoformat(last_raw)
            if (now_ts - last_dt.timestamp()) < cooldown_seconds:
                logger.info(f"Auth warning suppressed due to cooldown ({int(now_ts - last_dt.timestamp())}s < {cooldown_seconds}s)")
                return False
        except Exception as exc:
            logger.debug("Could not parse last auth warning time: %s", type(exc).__name__)

    text = format_auth_warning(reason)
    send_fn = sender_fn or telegram_routing.send_job_message_result
    try:
        res = send_fn(text, parse_mode="HTML")
        if getattr(res, "ok", False):
            storage.set_state("last_auth_warning_at", datetime.now(timezone.utc).isoformat())
            logger.info("Sent Kwork auth lost warning to @jobsassistbot")
            return True
        else:
            logger.warning(f"Failed to send auth warning: {getattr(res, 'detail', '')}")
            return False
    except Exception as exc:
        logger.warning(f"Exception sending auth warning: {exc}")
        return False


def maybe_notify_kwork_recovered(
    storage: KworkInboxStorage,
    sender_fn: Callable[..., Any] | None = None,
) -> bool:
    """Send single all-clear notification when Kwork auth recovers."""
    last_raw = storage.get_state("last_auth_warning_at")
    if not last_raw:
        return False
    send_fn = sender_fn or telegram_routing.send_job_message_result
    try:
        res = send_fn(format_auth_recovered(), parse_mode="HTML")
        if getattr(res, "ok", False):
            storage.set_state("last_auth_warning_at", "")
            logger.info("Sent Kwork auth recovery notification to @jobsassistbot")
            return True
    except Exception as exc:
        logger.warning(f"Failed to send Kwork recovery notification: {exc}")
    return False


def parse_inbox_payload(payload: dict[str, Any]) -> KworkScanResult:
    """Parse JSON payload extracted from Kwork DOM or API response into KworkScanResult."""
    if not payload.get("is_authenticated", True):
        return KworkScanResult(
            is_authenticated=False,
            auth_status=payload.get("auth_status") or "UNKNOWN",
            auth_error=payload.get("auth_error") or "Kwork session is not authenticated",
            active_dialogues_count=0,
            unread_count=0,
            messages=[],
        )

    dialogues = payload.get("dialogues") or []
    detected_messages: list[KworkMessage] = []
    unread_total = 0

    for diag in dialogues:
        diag_id = str(diag.get("dialogue_id") or diag.get("username") or diag.get("track_id") or "").strip()
        if not diag_id:
            continue

        project_id = str(diag.get("project_id") or "").strip()
        project_title = str(diag.get("project_title") or diag.get("title") or "").strip()
        buyer_name = str(diag.get("buyer_name") or diag.get("username") or "Заказчик").strip()
        chat_url = str(diag.get("chat_url") or f"https://kwork.ru/inbox/{diag_id}").strip()
        has_unread = bool(diag.get("has_unread") or diag.get("unread_count", 0) > 0)
        if has_unread:
            unread_total += int(diag.get("unread_count", 1) or 1)

        raw_messages = diag.get("messages") or []
        if not raw_messages and (diag.get("last_message_text") or has_unread):
            # Synthesize single last message from dialogue summary
            raw_messages = [
                {
                    "message_id": diag.get("last_message_id") or f"last_{int(time.time())}",
                    "text": diag.get("last_message_text") or "",
                    "is_outgoing": bool(diag.get("is_last_outgoing", False)),
                }
            ]

        for m in raw_messages:
            msg_id = str(m.get("message_id") or f"msg_{hash(m.get('text', ''))}").strip()
            text = str(m.get("text") or "").strip()
            is_outgoing = bool(m.get("is_outgoing", False))

            key = f"kwork:{diag_id}:{msg_id}"
            now_iso = datetime.now(timezone.utc).isoformat()
            status = "IGNORED" if is_outgoing else "PENDING"

            diag_unread_count = int(diag.get("unread_count", 1) or 1)
            detected_messages.append(
                KworkMessage(
                    message_key=key,
                    dialogue_id=diag_id,
                    project_id=project_id,
                    project_title=project_title,
                    buyer_name=buyer_name,
                    message_id=msg_id,
                    text=text,
                    is_outgoing=is_outgoing,
                    chat_url=chat_url,
                    detected_at=now_iso,
                    delivery_status=status,
                    unread_count=diag_unread_count,
                )
            )

    return KworkScanResult(
        is_authenticated=True,
        messages=detected_messages,
        active_dialogues_count=len(dialogues),
        unread_count=unread_total,
    )


def scan_kwork_inbox_via_cdp(
    cdp_url: str = DEFAULT_CDP_URL,
    timeout: float = 15.0,
) -> KworkScanResult:
    """Scan https://kwork.ru/inbox through active CDP browser session without re-auth."""
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(cdp_url.rstrip("/") + "/json/version", headers={"User-Agent": "kwork-monitor"})
        with opener.open(req, timeout=3.0) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Unexpected status {resp.status}")
    except Exception as exc:
        logger.error(f"CDP endpoint {cdp_url} is unreachable: {exc}")
        return KworkScanResult(
            is_authenticated=False,
            auth_error=f"CDP unavailable: {exc}",
            auth_status="CDP_UNAVAILABLE",
        )

    from ..hh_browser_provider import list_tabs, make_target_evaluate, open_tab

    try:
        tabs = list_tabs(cdp_url, timeout=5.0)
    except Exception as exc:
        logger.error(f"Cannot list tabs on CDP {cdp_url}: {exc}")
        return KworkScanResult(
            is_authenticated=False,
            auth_status="CDP_UNAVAILABLE",
            auth_error=f"CDP unavailable: {exc}",
        )

    # 1. Prefer tab already on /inbox (refresh so SSR/Vue reflects latest incoming messages)
    kwork_tab = next((t for t in tabs if "kwork.ru/inbox" in (t.get("url") or "")), None)
    if kwork_tab and kwork_tab.get("id"):
        try:
            eval_nav = make_target_evaluate(cdp_url, kwork_tab["id"])
            eval_nav("window.location.href = 'https://kwork.ru/inbox'")
            time.sleep(2.5)
        except Exception as exc:
            logger.debug("Could not refresh the open Kwork inbox tab: %s", type(exc).__name__)
    else:
        any_kw = next((t for t in tabs if "kwork.ru" in (t.get("url") or "")), None)
        if any_kw and any_kw.get("id"):
            kwork_tab = any_kw
            try:
                eval_nav = make_target_evaluate(cdp_url, kwork_tab["id"])
                eval_nav("window.location.href = 'https://kwork.ru/inbox'")
                time.sleep(2.5)
            except Exception as exc:
                logger.debug("Could not navigate an existing Kwork tab: %s", type(exc).__name__)
        else:
            try:
                kwork_tab = open_tab(cdp_url, "https://kwork.ru/inbox", timeout=timeout)
                if not kwork_tab or not kwork_tab.get("id"):
                    # Chrome 138+ disables HTTP /json/new; fall back to browser WS Target.createTarget
                    from ..hh_browser_provider import cdp_version
                    ver_info = cdp_version(cdp_url, timeout=3.0) or {}
                    ws_url = ver_info.get("webSocketDebuggerUrl")
                    if ws_url:
                        import asyncio

                        import websockets

                        async def _create_bg_tab(b_ws: str) -> str | None:
                            async with websockets.connect(b_ws, max_size=16 * 1024 * 1024) as ws:
                                await ws.send(
                                    json.dumps(
                                        {
                                            "id": 1,
                                            "method": "Target.createTarget",
                                            "params": {"url": "https://kwork.ru/inbox", "background": True},
                                        }
                                    )
                                )
                                while True:
                                    msg = json.loads(await ws.recv())
                                    if msg.get("id") == 1:
                                        return (msg.get("result") or {}).get("targetId")

                        new_tid = asyncio.run(_create_bg_tab(ws_url))
                        if new_tid:
                            kwork_tab = {"id": new_tid, "url": "https://kwork.ru/inbox"}
                if kwork_tab and kwork_tab.get("id"):
                    time.sleep(3.5)
            except Exception as exc:
                return KworkScanResult(
                    is_authenticated=False,
                    auth_status="PAGE_NOT_READY",
                    auth_error=f"Cannot open Kwork tab: {exc}",
                )

    if not kwork_tab or not kwork_tab.get("id"):
        return KworkScanResult(
            is_authenticated=False,
            auth_status="PAGE_NOT_READY",
            auth_error="Failed to acquire Kwork tab",
        )

    eval_fn = make_target_evaluate(cdp_url, kwork_tab["id"])

    # Extract DOM / Vue state
    extract_js = """
    (() => {
        const path = window.location.pathname;
        const isLoginPage = path.startsWith('/login') || path.startsWith('/user/login');
        const hasLoginForm = !!document.querySelector('.auth-form__button, form[action*="login"]');
        if (isLoginPage && hasLoginForm) {
            return JSON.stringify({ is_authenticated: false, auth_status: "AUTH_REQUIRED", auth_error: "Confirmed login page rendered (/login)" });
        }

        // Check if page is blank or not ready
        if (document.readyState !== 'complete' && document.readyState !== 'interactive') {
            return JSON.stringify({ is_authenticated: false, auth_status: "PAGE_NOT_READY", auth_error: "DOM readyState is " + document.readyState });
        }

        // Check user session indicators (avatar, username, profile link)
        const hasUserAvatar = !!document.querySelector('.user-avatar, .header-user, [class*="avatar"]');
        const hasUserMenu = !!document.querySelector('.user-menu, .header-nav__user');
        const hasLoginButton = !!document.querySelector('.auth-form__button, .js-login-btn');

        if (hasLoginButton && !hasUserAvatar && !hasUserMenu) {
            return JSON.stringify({ is_authenticated: false, auth_status: "AUTH_REQUIRED", auth_error: "Login button rendered without user avatar" });
        }

        const dialogues = [];

        // Layer 1: Check Vue reactive model from chat-list component if present
        const chatListEl = Array.from(document.querySelectorAll('*')).find(el => el.__vue__?.$options.name === 'chat-list');
        const chatListVue = chatListEl?.__vue__;
        const actorId = chatListVue?.actorId || 0;

        if (chatListVue && Array.isArray(chatListVue.chatList) && chatListVue.chatList.length > 0) {
            for (const item of chatListVue.chatList) {
                const uName = item.username || item.name || 'Заказчик';
                const dId = String(item.userId || item.user_id || uName);
                const hasUnread = (item.unread_count || item.unreadCount || 0) > 0 || !!item.is_unread;
                const isOut = item.from_id ? (item.from_id === actorId) : (item.is_out || false);
                dialogues.push({
                    dialogue_id: dId,
                    username: uName,
                    buyer_name: uName,
                    has_unread: hasUnread,
                    unread_count: item.unread_count || (hasUnread ? 1 : 0),
                    last_message_id: String(item.last_message_id || item.message_id || ''),
                    last_message_text: item.last_message || item.text || '',
                    is_last_outgoing: isOut,
                    chat_url: `https://kwork.ru/inbox/${uName}`,
                    project_id: String(item.order_id || item.project_id || ''),
                    project_title: item.order_title || item.project_title || ''
                });
            }
        }

        // Layer 2: DOM fallback: query items rendered in DOM tree
        if (dialogues.length === 0) {
            const items = document.querySelectorAll('.chat__item, .inbox-list__item, .inbox-item, [class*="dialog-item"]');
            for (const el of items) {
                const hasUnread = !!el.querySelector('[class*="unread"], [class*="badge"], .dot') ||
                                  el.classList.contains('chat__item--unread') ||
                                  el.classList.contains('unread');
                const unreadBadge = el.querySelector('.chat__badge, .badge, [class*="badge"]');
                const unreadCount = unreadBadge ? (parseInt(unreadBadge.textContent.trim(), 10) || 1) : (hasUnread ? 1 : 0);

                const nameEl = el.querySelector('.chat__name, .inbox-item__username, .user-name, [class*="name"]');
                const previewEl = el.querySelector('.chat__preview, .inbox-item__text, .last-message, [class*="text"]');
                const isOut = !!el.querySelector('.chat__message--out, [class*="out"]') ||
                              (previewEl && previewEl.textContent.trim().startsWith('Вы:'));

                const linkEl = el.querySelector('a[href*="/inbox/"], a[href*="/track"]') || el.closest('a');
                const href = linkEl ? linkEl.href : '';
                const diagId = href ? href.split('/').filter(Boolean).pop() : (nameEl ? nameEl.textContent.trim() : '');

                dialogues.push({
                    dialogue_id: diagId,
                    username: nameEl ? nameEl.textContent.trim() : 'Заказчик',
                    buyer_name: nameEl ? nameEl.textContent.trim() : 'Заказчик',
                    has_unread: hasUnread,
                    unread_count: unreadCount,
                    last_message_text: previewEl ? previewEl.textContent.trim() : '',
                    is_last_outgoing: isOut,
                    chat_url: href || 'https://kwork.ru/inbox',
                    project_id: '',
                    project_title: ''
                });
            }
        }

        return JSON.stringify({
            is_authenticated: true,
            dialogues: dialogues
        });
    })()
    """

    try:
        raw_res = eval_fn(extract_js)
        parsed = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
        return parse_inbox_payload(parsed)
    except Exception as exc:
        logger.error(f"Error evaluating Kwork inbox DOM: {exc}")
        return KworkScanResult(
            is_authenticated=False,
            auth_status="PAGE_NOT_READY",
            auth_error=f"DOM extraction error: {exc}",
        )


def process_monitor_cycle(
    storage: KworkInboxStorage,
    scan_fn: Callable[[], KworkScanResult],
    sender_fn: Callable[..., Any] | None = None,
    dry_run: bool = False,
    send_one: bool = False,
) -> KworkMonitorCycleReport:
    """Execute one complete monitoring cycle.

    1. Process backlog of pending / previously failed messages.
    2. Perform inbox scan.
    3. Handle auth status (CDP unavailable, temporary glitch, or 2-strike login confirmation).
    4. Save new incoming messages with persistent deduplication.
    5. Dispatch alerts to @jobsassistbot strictly (never Hermes, never channel).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    alerts_sent = 0
    alerts_pending = 0
    alerts_failed = 0
    new_detected = 0
    details = []

    # Step 1: Retry backlog of pending messages from previous cycles
    backlog = storage.get_pending_messages()
    if backlog:
        logger.info(f"Processing backlog of {len(backlog)} pending Kwork messages")
        for p_msg in backlog:
            if dry_run:
                alerts_pending += 1
                details.append(f"DRY_RUN: would retry pending {p_msg.message_key}")
                continue
            if send_one and alerts_sent >= 1:
                alerts_pending += 1
                details.append(f"send_one limit reached: backlog {p_msg.message_key} kept in queue")
                continue
            ok = dispatch_kwork_alert(p_msg, storage, sender_fn=sender_fn)
            if ok:
                alerts_sent += 1
                details.append(f"Backlog message delivered: {p_msg.message_key}")
            else:
                alerts_failed += 1
                details.append(f"Backlog message delivery failed: {p_msg.message_key}")

    # Step 2: Run fresh scan
    scan_result = scan_fn()
    if not scan_result.is_authenticated:
        raw_status = getattr(scan_result, "auth_status", "UNKNOWN") or "UNKNOWN"
        if raw_status == "OK":
            raw_status = "UNKNOWN"

        # 1. CDP unavailable -> strictly return CDP_UNAVAILABLE, zero Telegram POST
        if raw_status == "CDP_UNAVAILABLE":
            details.append(f"CDP unavailable: {scan_result.auth_error} - zero alert sent")
            return KworkMonitorCycleReport(
                timestamp=now_iso,
                scanned_dialogues=0,
                new_detected=0,
                alerts_sent=alerts_sent,
                alerts_pending=alerts_pending,
                alerts_failed=alerts_failed,
                auth_status="CDP_UNAVAILABLE",
                details=details,
            )

        # 2. Temporary page error / DOM not ready -> zero alert sent
        if raw_status == "PAGE_NOT_READY":
            details.append(f"Page not ready: {scan_result.auth_error} - temporary glitch, zero alert sent")
            return KworkMonitorCycleReport(
                timestamp=now_iso,
                scanned_dialogues=0,
                new_detected=0,
                alerts_sent=alerts_sent,
                alerts_pending=alerts_pending,
                alerts_failed=alerts_failed,
                auth_status="PAGE_NOT_READY",
                details=details,
            )

        # 3. No conclusive signal -> never guess auth loss from a bare failure
        if raw_status != "AUTH_REQUIRED":
            details.append(
                f"Auth state unknown ({raw_status}): {scan_result.auth_error} - zero alert sent"
            )
            return KworkMonitorCycleReport(
                timestamp=now_iso,
                scanned_dialogues=0,
                new_detected=0,
                alerts_sent=alerts_sent,
                alerts_pending=alerts_pending,
                alerts_failed=alerts_failed,
                auth_status="UNKNOWN",
                details=details,
            )

        # 4. True login page detection (AUTH_REQUIRED) - requires 2 confirmed checks before alert
        current_strikes = int(storage.get_state("consecutive_login_page_count") or "0") + 1
        storage.set_state("consecutive_login_page_count", str(current_strikes))

        if current_strikes < 2:
            details.append(
                f"Login page detected once (strike 1/2): {scan_result.auth_error} - awaiting 2nd confirmation before alert"
            )
            return KworkMonitorCycleReport(
                timestamp=now_iso,
                scanned_dialogues=0,
                new_detected=0,
                alerts_sent=alerts_sent,
                alerts_pending=alerts_pending,
                alerts_failed=alerts_failed,
                auth_status="AUTH_REQUIRED",
                details=details,
            )

        # 2 or more confirmed checks of login page
        if dry_run:
            details.append("DRY_RUN: confirmed login page (2 strikes), live alert suppressed")
            return KworkMonitorCycleReport(
                timestamp=now_iso,
                scanned_dialogues=0,
                new_detected=0,
                alerts_sent=alerts_sent,
                alerts_pending=alerts_pending,
                alerts_failed=alerts_failed,
                auth_status="AUTH_LOST",
                details=details,
            )

        auth_warned = handle_auth_lost(storage, sender_fn=sender_fn, reason=scan_result.auth_error)
        details.append(f"Confirmed login page (strike {current_strikes}) - auth warning dispatched (warned={auth_warned})")
        return KworkMonitorCycleReport(
            timestamp=now_iso,
            scanned_dialogues=0,
            new_detected=0,
            alerts_sent=alerts_sent,
            alerts_pending=alerts_pending,
            alerts_failed=alerts_failed,
            auth_status="AUTH_LOST",
            details=details,
        )

    # Step 2b: Recovery check if previously warned or strike counter was set
    storage.set_state("consecutive_login_page_count", "0")
    if storage.get_state("last_auth_warning_at"):
        if not dry_run:
            recovered = maybe_notify_kwork_recovered(storage, sender_fn=sender_fn)
            if recovered:
                details.append("Auth recovered: sent recovery notification to @jobsassistbot and cleared warning state")
        else:
            storage.set_state("last_auth_warning_at", "")
            details.append("DRY_RUN: auth recovered, cleared previous warning state without sending alert")

    # Step 3: Process detected messages
    for msg in scan_result.messages:
        if storage.is_known(msg.message_key):
            # Dedup guard: already seen and handled
            continue

        new_detected += 1
        storage.save_message(msg)

        if msg.is_outgoing:
            # Own outgoing message: record as IGNORED, do not alert
            details.append(f"Own outgoing message recorded (no alert): {msg.message_key}")
            continue

        # New incoming message from buyer: dispatch alert
        if dry_run:
            alerts_pending += 1
            details.append(f"DRY_RUN: would alert incoming {msg.message_key}")
            continue

        if send_one and alerts_sent >= 1:
            alerts_pending += 1
            details.append(f"send_one limit reached: queued {msg.message_key} as PENDING without sending")
            continue

        ok = dispatch_kwork_alert(msg, storage, sender_fn=sender_fn)
        if ok:
            alerts_sent += 1
            details.append(f"New buyer message alerted: {msg.message_key}")
        else:
            alerts_failed += 1
            details.append(f"Alert delivery failed for {msg.message_key} (saved as PENDING/FAILED)")

    return KworkMonitorCycleReport(
        timestamp=now_iso,
        scanned_dialogues=scan_result.active_dialogues_count,
        new_detected=new_detected,
        alerts_sent=alerts_sent,
        alerts_pending=alerts_pending,
        alerts_failed=alerts_failed,
        auth_status="OK",
        details=details,
    )
