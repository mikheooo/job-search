"""Gig Telegram delivery drafts (Phase 4C.1 §5). OFFLINE-PREPARED, NOT ENABLED.

Model: gig found -> gates pass -> saved in gigs -> listed -> optionally sent
to Telegram. Identity is ALWAYS ``gig_id``; ``vacancies.state`` is never
touched for real gigs. Idempotency keys live in the existing
``telegram_delivery_records`` table (``notification_type='gig_digest'``,
``delivery_key='gig:{gig_id}:{batch_key}'``, gig id also inside
``payload_json``) — no schema change.

Policy enforced here (not just documented):
- CLOSED/STALE/UNKNOWN freshness, rejected gigs, and fixture rows are NEVER
  selected for delivery (caller must still filter; ``select_for_delivery``
  does it centrally).
- Transport is injected (``send_fn``). ``None`` means dry-run: payloads are
  built and returned, nothing is sent, nothing is recorded.
- Callback data ``gig:{INT|SKP}:{gig_id}`` + URL button "Открыть заказ".
  ``handle_gig_callback`` is idempotent (same callback twice -> second is a
  no-op ack) and refuses unknown/ambiguous ids without touching anything.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

logger = logging.getLogger("gig_notify")

CALLBACK_PREFIX = "gig:"
ACTION_INTERESTED = "INT"
ACTION_SKIP = "SKP"
NOTIFICATION_TYPE = "gig_digest"

# Freshness values that must never be delivered without explicit policy.
BLOCKED_FRESHNESS = {"CLOSED_CONFIRMED", "STALE_CANDIDATE", "UNKNOWN"}


def delivery_key(gig_id: str, batch_key: str, namespace: str = "") -> str:
    """Per-batch audit key (grouping only — NOT the dedup identity)."""
    key = f"gig:{gig_id}:{batch_key}"
    return f"{namespace}:{key}" if namespace else key


def stable_key(gig_id: str, namespace: str = "") -> str:
    """Stable per-gig delivery identity, constant across batches.

    Phase 4C.2 fix: deduping on the batch key re-sent delivered gigs on
    every new run (batch changes -> key changes). The stable key makes
    "already delivered" permanent regardless of batching.

    Phase 4C.4: ``namespace`` isolates ledgers (e.g. "test" vs prod "").
    Test sends must never mark prod deliveries as done and vice versa.
    Production format (namespace="") is unchanged.
    """
    key = f"gig_digest:{gig_id}"
    return f"{namespace}:{key}" if namespace else key


def batch_key(gig_ids: list[str], namespace: str = "") -> str:
    digest = hashlib.sha256("|".join(sorted(gig_ids)).encode()).hexdigest()[:16]
    key = f"gigbatch:{digest}"
    return f"{namespace}:{key}" if namespace else key


def callback_data(action: str, gig_id: str) -> str:
    if action not in (ACTION_INTERESTED, ACTION_SKIP):
        raise ValueError(f"unknown gig action: {action!r}")
    return f"{CALLBACK_PREFIX}{action}:{gig_id}"


def parse_callback(data: str) -> tuple[str, str] | None:
    """``gig:INT:<gig_id>`` -> (action, gig_id); anything else -> None."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in (ACTION_INTERESTED, ACTION_SKIP):
        return None
    if not parts[2]:
        return None
    return parts[1], parts[2]


def build_card(gig: dict[str, Any]) -> dict[str, Any]:
    """Gig dict (from store row or brief) -> Telegram sendMessage payload."""
    title = str(gig.get("title") or "Untitled gig")
    budget = gig.get("budget_text") or "budget unknown"
    score = gig.get("opportunity_score")
    score_text = f"{score:.0f}" if isinstance(score, (int, float)) else "—"
    url = gig.get("url") or ""
    text = (
        f"💼 <b>{title}</b>\n"
        f"💰 {budget} | ⭐ {score_text} | <code>{gig.get('source')}</code>\n"
    )
    if url:
        text += f"🔗 <a href=\"{url}\">Открыть заказ</a>"
    keyboard = {"inline_keyboard": [[
        {"text": "👍 Интересно", "callback_data": callback_data("INT", gig["id"])},
        {"text": "👎 Не интересно", "callback_data": callback_data("SKP", gig["id"])},
    ]]}
    if url:
        keyboard["inline_keyboard"].append([{"text": "🔗 Открыть заказ", "url": url}])
    return {"text": text, "reply_markup": keyboard, "parse_mode": "HTML"}


def select_for_delivery(
    gigs: list[dict[str, Any]],
    freshness: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Central delivery filter. Fixture/CLOSED/STALE/UNKNOWN/rejected out."""
    freshness = freshness or {}
    selected = []
    for gig in gigs:
        if gig.get("source_mode") == "fixture" or gig.get("source") == "fixture":
            continue
        if gig.get("rejection_reasons"):
            continue
        if freshness.get(gig["id"], "ACTIVE_SEEN") in BLOCKED_FRESHNESS:
            continue
        selected.append(gig)
    return selected


def prepare_batch(
    gigs: list[dict[str, Any]],
    *,
    chat_id: str | int,
    freshness: dict[str, str] | None = None,
    send_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    record_fn: Callable[..., None] | None = None,
    delivery_status_fn: Callable[[str], str | None] | None = None,
    reserve_fn: Callable[[str], bool] | None = None,
    namespace: str = "",
) -> dict[str, Any]:
    """Build (and optionally send+record) one delivery batch.

    Idempotency is anchored on the STABLE per-gig key, not the batch key:
    a gig delivered once is never re-sent by a later batch. Per-gig status
    is honored: DELIVERED -> skip; ATTEMPTING/AMBIGUOUS -> defer (never
    auto-retried, reported in ``deferred``); FAILED/missing -> send.
    On success BOTH the stable key (DELIVERED) and the batch key (audit
    grouping) are recorded. With ``send_fn=None`` this is a pure dry-run.

    ``reserve_fn(key) -> bool`` (optional) closes the check-then-send race
    between concurrent processes: it must atomically claim the key
    (INSERT OR IGNORE semantics) and return False when already claimed.
    A False claim counts as skipped-duplicate, never as a send.
    ``record_fn(key, ntype, chat, payload, status)``; ``delivery_status_fn``
    returns a status string or None when unknown.
    """
    selected = select_for_delivery(gigs, freshness)
    bkey = batch_key([g["id"] for g in selected], namespace)
    sent: list[str] = []
    skipped: list[str] = []
    deferred: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    cards: list[dict[str, Any]] = []
    for gig in selected:
        skey = stable_key(gig["id"], namespace)
        status = delivery_status_fn(skey) if delivery_status_fn is not None else None
        if status == "DELIVERED":
            skipped.append(gig["id"])
            continue
        if status in ("ATTEMPTING", "AMBIGUOUS"):
            deferred.append({"gig_id": gig["id"], "status": status})
            continue
        card = build_card(gig)
        card["chat_id"] = chat_id
        cards.append(card)
        if send_fn is None:
            continue
        if reserve_fn is not None and not reserve_fn(skey):
            skipped.append(gig["id"])
            continue
        try:
            resp = send_fn(card) or {}
            if record_fn is not None:
                payload = {"gig_id": gig["id"], "batch": bkey,
                           "telegram_message_id": resp.get("message_id")}
                record_fn(skey, NOTIFICATION_TYPE, str(chat_id), payload, "DELIVERED")
                record_fn(delivery_key(gig["id"], bkey, namespace), NOTIFICATION_TYPE,
                          str(chat_id), payload, "DELIVERED")
            sent.append(gig["id"])
        except Exception as exc:  # noqa: BLE001 - per-card failure, batch continues
            logger.warning("gig delivery failed for %s: %s", gig["id"], exc)
            if record_fn is not None:
                try:
                    record_fn(skey, NOTIFICATION_TYPE, str(chat_id),
                              {"gig_id": gig["id"], "batch": bkey,
                               "error": f"{type(exc).__name__}: {exc}"}, "FAILED")
                except Exception:
                    pass
            failed.append({"gig_id": gig["id"], "error": f"{type(exc).__name__}: {exc}"})
    return {"batch_key": bkey, "selected": [g["id"] for g in selected],
            "sent": sent, "skipped": skipped, "deferred": deferred,
            "failed": failed, "cards": cards, "dry_run": send_fn is None}


def sqlite_delivery_store(conn):
    """Bind (delivery_status_fn, record_fn, reserve_fn) to a SQLite connection.

    Uses the existing ``telegram_delivery_records`` table (delivery_key
    UNIQUE) — no schema change. Reservation is a single atomic
    ``INSERT OR IGNORE``: exactly one concurrent process wins per key.
    Recording never downgrades DELIVERED (only ATTEMPTING/FAILED rows move).
    Caller owns commit/rollback and the connection lifetime.
    """
    import datetime
    import json as _json

    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def delivery_status_fn(key: str) -> str | None:
        row = conn.execute(
            "SELECT status FROM telegram_delivery_records WHERE delivery_key = ?",
            (key,),
        ).fetchone()
        return str(row[0]) if row else None

    def reserve_fn(key: str) -> bool:
        cur = conn.execute(
            "INSERT OR IGNORE INTO telegram_delivery_records"
            " (delivery_key, notification_type, chat_id, delivered_at, status, payload)"
            " VALUES (?, ?, '', ?, 'ATTEMPTING', '{}')",
            (key, NOTIFICATION_TYPE, _now()),
        )
        return cur.rowcount == 1

    def record_fn(key: str, ntype: str, chat: str, payload: dict,
                  status: str = "DELIVERED") -> None:
        conn.execute(
            "UPDATE telegram_delivery_records SET status = ?, delivered_at = ?,"
            " payload = ? WHERE delivery_key = ? AND status IN ('ATTEMPTING', 'FAILED')",
            (status, _now(), _json.dumps(payload, ensure_ascii=False), key),
        )

    return delivery_status_fn, record_fn, reserve_fn


def followup_text(outcome: str, title: str, status: str | None = None) -> str | None:
    """Explicit follow-up message for a gig callback outcome.

    Toasts alone proved too easy to miss (Gate 2). Returns the message
    text, or None when silence is correct: repeated presses
    (already-applied) stay toast-only to avoid spamming on every re-tap.
    """
    if outcome == "applied":
        if status == "SHORTLISTED":
            return (f"✅ <b>Заказ сохранён.</b>\n«{title}»\n"
                    f"Статус: <code>SHORTLISTED</code>.")
        return (f"⏭ <b>Заказ пропущен.</b>\n«{title}»\n"
                f"Статус: <code>SKIPPED</code>.")
    if outcome == "refused":
        return ("⚠️ <b>Не удалось обработать.</b>\n"
                "Заказ не найден — возможно, сообщение устарело.")
    return None  # already-applied / ignored: toast is enough


def handle_gig_callback(
    data: str,
    *,
    get_gig_fn: Callable[[str], dict[str, Any] | None],
    set_status_fn: Callable[[str, str, str | None], Any],
    callback_query_id: str | None = None,
    acked_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Process one ``gig:`` callback offline-testably.

    Idempotent on two levels: a repeated ``callback_query_id`` collapses
    before any read (caller passes the set of acked ids, mutated in place),
    and an already-targeted gig status short-circuits without writes.
    Unknown actions/ids refuse without writes. Single-gig scope only:
    one callback can never change several gigs. Returns an outcome dict.
    """
    parsed = parse_callback(data)
    if parsed is None:
        return {"outcome": "ignored", "reason": "not a gig callback"}
    if callback_query_id and acked_ids is not None:
        if callback_query_id in acked_ids:
            return {"outcome": "already-applied", "reason": "duplicate callback"}
        acked_ids.add(callback_query_id)
    action, gig_id = parsed
    gig = get_gig_fn(gig_id)
    if gig is None:
        return {"outcome": "refused", "reason": "unknown gig_id"}
    target = "SHORTLISTED" if action == ACTION_INTERESTED else "SKIPPED"
    title = str(gig.get("title") or gig_id)
    if str(gig.get("status") or "").upper() == target:
        return {"outcome": "already-applied", "gig_id": gig_id,
                "status": target, "title": title}
    set_status_fn(gig_id, target, f"gig telegram callback {action}")
    return {"outcome": "applied", "gig_id": gig_id, "status": target,
            "title": title}


__all__ = [
    "ACTION_INTERESTED", "ACTION_SKIP", "BLOCKED_FRESHNESS", "CALLBACK_PREFIX",
    "NOTIFICATION_TYPE", "batch_key", "build_card", "callback_data",
    "delivery_key", "handle_gig_callback", "parse_callback",
    "prepare_batch", "select_for_delivery", "sqlite_delivery_store",
    "stable_key",
]
