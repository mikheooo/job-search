"""Unified notification gateway (Phase 2).

Sits between *detection* and *Telegram delivery* so every sender shares ONE
dedup identity, ONE rate limiter and ONE priority policy. Before this, each of
the ~6 senders used its own delivery key (``digest:`` / ``gig_digest:`` /
``tg_card:`` / ``outreach_telegram_card``), so the same order could be pushed
several times and rejected items reappeared.

Design goals (from the rework brief):
- Detection and notification are separate: this module never fetches, only gates.
- Normal vacancies/orders -> the daily digest.
- High-relevance gigs / urgent messages -> sent now, rate-limited.
- Employer replies / interview invitations -> priority (IMMEDIATE).
- Global anti-repeat that is independent of the sender.
- Already-seen / rejected / taken-in-work items are not re-notified.
- Unique offers are NEVER deleted here; suppression only stops notifications.
- Rate limiting downgrades to digest, it never drops a unique order.

Everything is pure except the injected ``store`` and ``send_fn`` so it can be
unit-tested with fakes and wired into any sender.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ai_assistant.notification_policy import (
    DEFAULT_DIGEST_FLOOR,
    DEFAULT_HIGH_RELEVANCE_FLOOR,
    DIGEST,
    GIG_HIGH,
    IMMEDIATE,
    SUPPRESS,
    Decision,
    classify,
)

# Global dedup namespace, deliberately independent of the sender path.
CANONICAL_PREFIX = "notify"

# Notification types counted against the per-hour immediate budget.
SEND_NOW_TYPES = (IMMEDIATE, GIG_HIGH)
DEFAULT_SEND_NOW_HOURLY_LIMIT = 6


def canonical_key(kind: str, item_id: str) -> str:
    """Sender-independent dedup identity for one item+kind."""
    return f"{CANONICAL_PREFIX}:{kind}:{item_id}"


@dataclass
class Outbound:
    """One candidate notification (detection output, no I/O)."""

    kind: str
    item_id: str
    chat_id: str | int
    score: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ItemState:
    """Everything the gateway needs to know about the item, from the DB."""

    already_delivered: bool = False
    is_rejected: bool = False
    is_irrelevant: bool = False


@dataclass
class DispatchResult:
    action: str  # "sent" | "digest" | "suppressed" | "rate-limited-to-digest"
    decision: Decision
    key: str
    reason: str = ""


class GatewayStore(Protocol):
    """Minimal persistence contract (bound to telegram_delivery_records)."""

    def reserve(self, key: str, notification_type: str) -> bool: ...
    def status(self, key: str) -> str | None: ...
    def record(self, key: str, notification_type: str, chat: str,
               payload: dict[str, Any], status: str) -> None: ...
    def count_send_now_since(self, iso_since: str) -> int: ...
    def digest_queued(self, item_id: str) -> bool: ...


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class NotificationGateway:
    """Central gate. Inject ``store`` (and optionally ``send_fn``)."""

    def __init__(
        self,
        store: GatewayStore,
        *,
        send_fn: Callable[[Outbound], Any] | None = None,
        digest_floor: float = DEFAULT_DIGEST_FLOOR,
        high_relevance_floor: float = DEFAULT_HIGH_RELEVANCE_FLOOR,
        send_now_hourly_limit: int = DEFAULT_SEND_NOW_HOURLY_LIMIT,
        now_iso: Callable[[], str] = _now_iso,
    ) -> None:
        self._store = store
        self._send_fn = send_fn
        self._digest_floor = digest_floor
        self._high_floor = high_relevance_floor
        self._limit = send_now_hourly_limit
        self._now = now_iso

    def decide(self, outbound: Outbound, state: ItemState | None = None) -> Decision:
        state = state or ItemState()
        if state.already_delivered or self._store.status(
            canonical_key(outbound.kind, outbound.item_id)
        ) in ("DELIVERED", "ATTEMPTING", "AMBIGUOUS"):
            state.already_delivered = True
        return classify(
            outbound.kind,
            score=outbound.score,
            is_duplicate=state.already_delivered,
            is_rejected=state.is_rejected,
            is_irrelevant=state.is_irrelevant,
            high_relevance_floor=self._high_floor,
            digest_floor=self._digest_floor,
        )

    def dispatch(self, outbound: Outbound, state: ItemState | None = None) -> DispatchResult:
        """Gate one notification. Returns what happened; never deletes data."""
        decision = self.decide(outbound, state)
        key = canonical_key(outbound.kind, outbound.item_id)

        if decision.bucket == SUPPRESS:
            return DispatchResult("suppressed", decision, key, decision.reason)

        if decision.bucket == DIGEST:
            return DispatchResult("digest", decision, key, decision.reason)

        # IMMEDIATE / GIG_HIGH: rate-limit into the digest instead of dropping.
        window_start = self._window_start()
        if self._store.count_send_now_since(window_start) >= self._limit:
            return DispatchResult(
                "rate-limited-to-digest", decision, key,
                f"hourly send-now limit ({self._limit}) reached",
            )

        # Atomic reservation: exactly one sender wins the canonical key.
        if not self._store.reserve(key, decision.bucket):
            return DispatchResult("suppressed", decision, key, "already reserved")

        if self._send_fn is not None:
            try:
                self._send_fn(outbound)
            except Exception as exc:  # noqa: BLE001 - per-item failure only
                self._store.record(key, decision.bucket, str(outbound.chat_id),
                                   {**outbound.payload, "error": f"{type(exc).__name__}: {exc}"},
                                   "FAILED")
                return DispatchResult("digest", decision, key,
                                      f"send failed, kept for digest: {exc}")
        self._store.record(key, decision.bucket, str(outbound.chat_id),
                           outbound.payload, "DELIVERED")
        return DispatchResult("sent", decision, key, decision.reason)

    def should_queue_for_digest(self, outbound: Outbound,
                                state: ItemState | None = None) -> bool:
        """True when the item belongs in the daily digest (dedup-safe)."""
        decision = self.decide(outbound, state)
        if decision.bucket in (SUPPRESS,):
            return False
        if decision.bucket in (IMMEDIATE, GIG_HIGH):
            # Already pushed (or will be); do not duplicate in the digest.
            return False
        return not self._store.digest_queued(outbound.item_id)

    def _window_start(self) -> str:
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - datetime.timedelta(hours=1)).isoformat()


def sqlite_gateway_store(conn: Any) -> GatewayStore:
    """Bind the gateway contract to a live SQLite connection.

    Reuses ``telegram_delivery_records`` (delivery_key UNIQUE). No schema change.
    ``reserve`` is a single atomic INSERT OR IGNORE. Suppression never deletes
    rows from ``gigs``/``vacancies``; it only writes delivery bookkeeping.
    """
    import json as _json

    class _Store:
        def reserve(self, key: str, notification_type: str) -> bool:
            cur = conn.execute(
                "INSERT OR IGNORE INTO telegram_delivery_records"
                " (delivery_key, notification_type, chat_id, delivered_at, status, payload)"
                " VALUES (?, ?, '', ?, 'ATTEMPTING', '{}')",
                (key, notification_type, _now_iso()),
            )
            return cur.rowcount == 1

        def status(self, key: str) -> str | None:
            row = conn.execute(
                "SELECT status FROM telegram_delivery_records WHERE delivery_key = ?",
                (key,),
            ).fetchone()
            return str(row[0]) if row else None

        def record(self, key: str, notification_type: str, chat: str,
                   payload: dict[str, Any], status: str) -> None:
            conn.execute(
                "UPDATE telegram_delivery_records SET status = ?, delivered_at = ?,"
                " notification_type = ?, payload = ? WHERE delivery_key = ?"
                " AND status IN ('ATTEMPTING', 'FAILED')",
                (status, _now_iso(), notification_type,
                 _json.dumps(payload, ensure_ascii=False), key),
            )

        def count_send_now_since(self, iso_since: str) -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM telegram_delivery_records"
                " WHERE notification_type IN (?, ?) AND delivered_at >= ?"
                " AND status = 'DELIVERED'",
                (IMMEDIATE, GIG_HIGH, iso_since),
            ).fetchone()
            return int(row[0]) if row else 0

        def digest_queued(self, item_id: str) -> bool:
            row = conn.execute(
                "SELECT 1 FROM telegram_delivery_records WHERE delivery_key = ?"
                " AND status IN ('DELIVERED', 'ATTEMPTING')",
                (f"digest:{item_id}",),
            ).fetchone()
            return bool(row)

    return _Store()


__all__ = [
    "CANONICAL_PREFIX",
    "DEFAULT_SEND_NOW_HOURLY_LIMIT",
    "DispatchResult",
    "GatewayStore",
    "ItemState",
    "NotificationGateway",
    "Outbound",
    "SEND_NOW_TYPES",
    "canonical_key",
    "sqlite_gateway_store",
]
