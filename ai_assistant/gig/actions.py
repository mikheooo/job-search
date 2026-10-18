"""Unified, working gig action buttons (Phase 5B).

Replaces the inconsistent gig keyboards (three different producers shipped
three different button sets: ``gig:INT``/``gig:SKP``, a "👍 В работу" label that
only shortlisted, and URL-only cards) with ONE card whose every button has a
real handler.

Actions (all offline-testable, no network):
- ``WORK``  — "В работу": move the order into the active queue
             (status SHORTLISTED). This is a *queue* action, never a send.
- ``OPEN``  — "Открыть": return the original listing URL. Rendered as a plain
             URL button when a URL exists, so it always works; the callback is
             only a fallback for URL-less rows.
- ``DRAFT`` — "Черновик отклика": return the proposal draft in the ORDER's
             language for the owner to copy. Never sends anything.
- ``SKIP``  — "Пропустить": drop from active offers (status SKIPPED).

The card never renders a button whose handler does not exist, and taking an
order into work is deliberately NOT the same as sending a proposal to a client.
"""
from __future__ import annotations

from typing import Any, Callable

CALLBACK_PREFIX = "gigact:"

ACTION_WORK = "WORK"
ACTION_OPEN = "OPEN"
ACTION_DRAFT = "DRAFT"
ACTION_SKIP = "SKP"

_ACTIONS = (ACTION_WORK, ACTION_OPEN, ACTION_DRAFT, ACTION_SKIP)

# Status written for each mutating action (existing GigStatus values only).
_STATUS_FOR_ACTION: dict[str, str] = {
    ACTION_WORK: "SHORTLISTED",
    ACTION_SKIP: "SKIPPED",
}


def callback_data(action: str, gig_id: str) -> str:
    if action not in _ACTIONS:
        raise ValueError(f"unknown gig action: {action!r}")
    return f"{CALLBACK_PREFIX}{action}:{gig_id}"


def parse_action(data: str) -> tuple[str, str] | None:
    """``gigact:WORK:<id>`` -> (action, gig_id); anything else -> None."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ACTIONS or not parts[2]:
        return None
    return parts[1], parts[2]


def build_action_card(
    gig: dict[str, Any],
    pitch: str,
    *,
    language: str = "ru",
) -> dict[str, Any]:
    """Build a Telegram sendMessage payload with only working buttons.

    ``gig`` needs at least ``id`` and ``title``; ``url`` enables the OPEN
    button. ``pitch`` is the pre-generated draft (already in ``language``).
    """
    gig_id = gig["id"]
    title = str(gig.get("title") or "Заказ")
    url = gig.get("url") or ""

    header = "💼 <b>Фриланс-заказ</b>" if language != "en" else "💼 <b>Freelance order</b>"
    draft_label = "✍️ Черновик отклика" if language != "en" else "✍️ Reply draft"
    work_label = "▶️ В работу"
    skip_label = "⏭ Пропустить"
    open_label = "🔗 Открыть заказ"

    text = (
        f"{header}\n"
        f"📌 <b>{title}</b>\n"
    )
    if gig.get("budget_text"):
        text += f"💰 {gig['budget_text']}\n"
    text += (
        f"\n📝 <b>{draft_label}:</b>\n"
        f"<code>{pitch}</code>"
    )

    rows: list[list[dict[str, str]]] = [
        [
            {"text": work_label, "callback_data": callback_data(ACTION_WORK, gig_id)},
            {"text": draft_label, "callback_data": callback_data(ACTION_DRAFT, gig_id)},
        ],
        [
            {"text": skip_label, "callback_data": callback_data(ACTION_SKIP, gig_id)},
        ],
    ]
    if url:
        rows.append([{"text": open_label, "url": url}])
    else:
        rows[-1].append({"text": open_label, "callback_data": callback_data(ACTION_OPEN, gig_id)})

    return {"text": text, "reply_markup": {"inline_keyboard": rows}, "parse_mode": "HTML"}


def handle_action(
    data: str,
    *,
    get_gig_fn: Callable[[str], dict[str, Any] | None],
    set_status_fn: Callable[[str, str, str | None], Any] | None = None,
    render_draft_fn: Callable[[dict[str, Any]], str] | None = None,
    callback_query_id: str | None = None,
    acked_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Process one ``gigact:`` callback. Idempotent, single-gig, no sends.

    Returns an outcome dict:
    - ``{"outcome": "queued", "status": "SHORTLISTED", ...}`` for WORK
    - ``{"outcome": "skipped", "status": "SKIPPED", ...}`` for SKIP
    - ``{"outcome": "opened", "url": ...}`` for OPEN
    - ``{"outcome": "draft", "draft": ...}`` for DRAFT
    - ``{"outcome": "already-applied"|"refused"|"ignored", ...}`` otherwise
    """
    parsed = parse_action(data)
    if parsed is None:
        return {"outcome": "ignored", "reason": "not a gig action callback"}

    if callback_query_id and acked_ids is not None:
        if callback_query_id in acked_ids:
            return {"outcome": "already-applied", "reason": "duplicate callback"}
        acked_ids.add(callback_query_id)

    action, gig_id = parsed
    gig = get_gig_fn(gig_id)
    if gig is None:
        return {"outcome": "refused", "reason": "unknown gig_id", "gig_id": gig_id}

    title = str(gig.get("title") or gig_id)

    if action == ACTION_OPEN:
        url = gig.get("url")
        if not url:
            return {"outcome": "refused", "reason": "no url", "gig_id": gig_id}
        return {"outcome": "opened", "gig_id": gig_id, "url": url}

    if action == ACTION_DRAFT:
        draft = ""
        if render_draft_fn is not None:
            draft = render_draft_fn(gig) or ""
        return {"outcome": "draft", "gig_id": gig_id, "title": title, "draft": draft}

    # WORK / SKIP mutate status.
    target = _STATUS_FOR_ACTION[action]
    if str(gig.get("status") or "").upper() == target:
        return {"outcome": "already-applied", "gig_id": gig_id, "status": target,
                "title": title}
    if set_status_fn is not None:
        set_status_fn(gig_id, target, f"gig action {action}")
    outcome = "queued" if action == ACTION_WORK else "skipped"
    return {"outcome": outcome, "gig_id": gig_id, "status": target, "title": title}


def action_confirmation(outcome: str, title: str = "", status: str | None = None) -> str | None:
    """User-facing Russian confirmation line, or None for silent outcomes."""
    if outcome == "queued":
        return f"✅ <b>Взято в работу.</b>\n«{title}»\nСтатус: <code>{status}</code>."
    if outcome == "skipped":
        return f"⏭ <b>Пропущено.</b>\n«{title}»\nСтатус: <code>{status}</code>."
    if outcome == "refused":
        return "⚠️ <b>Не удалось обработать.</b>\nЗаказ не найден — возможно, сообщение устарело."
    return None


__all__ = [
    "ACTION_DRAFT",
    "ACTION_OPEN",
    "ACTION_SKIP",
    "ACTION_WORK",
    "CALLBACK_PREFIX",
    "action_confirmation",
    "build_action_card",
    "callback_data",
    "handle_action",
    "parse_action",
]
