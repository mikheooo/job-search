"""Tests for the unified notification gateway (Phase 2 anti-spam)."""
from __future__ import annotations

import sqlite3
from typing import Any

from ai_assistant.notification_gateway import (
    IMMEDIATE,
    ItemState,
    NotificationGateway,
    Outbound,
    canonical_key,
    sqlite_gateway_store,
)


class FakeStore:
    """In-memory stand-in: mirrors the telegram_delivery_records contract."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str]] = {}  # key -> (type, status)
        self.count_calls = 0
        self._forced = 0

    def reserve(self, key: str, notification_type: str) -> bool:
        if key in self.rows:
            return False
        self.rows[key] = (notification_type, "ATTEMPTING")
        return True

    def status(self, key: str) -> str | None:
        row = self.rows.get(key)
        return row[1] if row else None

    def record(self, key: str, notification_type: str, chat: str,
               payload: dict[str, Any], status: str) -> None:
        self.rows[key] = (notification_type, status)

    def count_send_now_since(self, iso_since: str) -> int:
        self.count_calls += 1
        delivered = sum(
            1 for t, s in self.rows.values()
            if s == "DELIVERED" and t in ("IMMEDIATE", "GIG_HIGH")
        )
        return delivered + self._forced

    def digest_queued(self, item_id: str) -> bool:
        return any(k == f"digest:{item_id}" for k in self.rows)


def _gw(store: FakeStore, sent: list) -> NotificationGateway:
    def _send(outbound: Outbound) -> None:
        sent.append(outbound)

    return NotificationGateway(store, send_fn=_send)


def test_immediate_sent_and_recorded():
    store, sent = FakeStore(), []
    gw = _gw(store, sent)
    res = gw.dispatch(Outbound("recruiter_reply", "v1", "42"))
    assert res.action == "sent"
    assert sent[0].item_id == "v1"
    assert store.status(canonical_key("recruiter_reply", "v1")) == "DELIVERED"


def test_global_dedup_across_senders():
    """Second sender with a different path must not re-send the same item."""
    store, sent = FakeStore(), []
    gw = _gw(store, sent)
    gw.dispatch(Outbound("gig", "g1", "42", score=90))
    # Same item, another sender (same canonical key) -> suppressed.
    res2 = gw.dispatch(Outbound("gig", "g1", "42", score=90))
    assert res2.action == "suppressed"
    assert len(sent) == 1


def test_rate_limit_downgrades_to_digest_not_drop():
    store, sent = FakeStore(), []
    store._forced = 6  # hourly budget already spent
    gw = _gw(store, sent)
    res = gw.dispatch(Outbound("gig", "g9", "42", score=95))
    assert res.action == "rate-limited-to-digest"
    assert sent == []  # nothing sent now -> no loss (kept for digest)


def test_normal_vacancy_goes_to_digest_not_sent():
    store, sent = FakeStore(), []
    gw = _gw(store, sent)
    res = gw.dispatch(Outbound("vacancy", "vac1", "42", score=60))
    assert res.action == "digest"
    assert sent == []


def test_low_score_suppressed():
    store, sent = FakeStore(), []
    gw = _gw(store, sent)
    res = gw.dispatch(Outbound("vacancy", "vac2", "42", score=10))
    assert res.action == "suppressed"


def test_rejected_and_irrelevant_not_sent():
    store, sent = FakeStore(), []
    gw = _gw(store, sent)
    assert gw.dispatch(Outbound("gig", "g2", "42", score=99),
                       ItemState(is_rejected=True)).action == "suppressed"
    assert gw.dispatch(Outbound("vacancy", "v9", "42", score=99),
                       ItemState(is_irrelevant=True)).action == "suppressed"
    assert sent == []


def test_send_failure_kept_for_digest_not_lost():
    store = FakeStore()

    def _boom(_o: Outbound) -> None:
        raise RuntimeError("network down")

    gw = NotificationGateway(store, send_fn=_boom)
    res = gw.dispatch(Outbound("recruiter_reply", "v5", "42"))
    assert res.action == "digest"
    assert store.status(canonical_key("recruiter_reply", "v5")) == "FAILED"


def test_should_queue_for_digest_rules():
    store, sent = FakeStore(), []
    gw = _gw(store, sent)
    assert gw.should_queue_for_digest(Outbound("vacancy", "v1", "42", score=60)) is True
    assert gw.should_queue_for_digest(Outbound("gig", "g1", "42", score=90)) is False
    assert gw.should_queue_for_digest(Outbound("vacancy", "v2", "42", score=10)) is False


def test_sqlite_store_roundtrip_uses_real_table():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE telegram_delivery_records ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_key TEXT NOT NULL UNIQUE,"
        " notification_type TEXT NOT NULL, chat_id TEXT NOT NULL,"
        " delivered_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT)"
    )
    store = sqlite_gateway_store(conn)
    sent: list = []
    gw = NotificationGateway(store, send_fn=lambda o: sent.append(o))
    assert gw.dispatch(Outbound("gig", "g1", "42", score=90)).action == "sent"
    # Duplicate across a second gateway instance (independent sender).
    gw2 = NotificationGateway(sqlite_gateway_store(conn), send_fn=lambda o: sent.append(o))
    assert gw2.dispatch(Outbound("gig", "g1", "42", score=90)).action == "suppressed"
    assert len(sent) == 1
    # Rate-limit counter sees the delivered send-now record.
    assert store.count_send_now_since("0000") == 1
