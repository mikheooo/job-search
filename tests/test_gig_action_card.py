"""Tests for the unified gig action card & handlers (Phase 5B)."""
from __future__ import annotations

from ai_assistant.gig.actions import (
    ACTION_DRAFT,
    ACTION_OPEN,
    ACTION_SKIP,
    ACTION_WORK,
    CALLBACK_PREFIX,
    action_confirmation,
    build_action_card,
    callback_data,
    handle_action,
    parse_action,
)


def _gig(**over):
    base = {"id": "g1", "title": "Telegram bot", "status": "NEW",
            "url": "https://t.me/x/1", "budget_text": "500 USD"}
    base.update(over)
    return base


def test_parse_action_roundtrip():
    data = callback_data(ACTION_WORK, "g1")
    assert data == "gigact:WORK:g1"
    assert parse_action(data) == ("WORK", "g1")


def test_parse_action_rejects_foreign_and_bad():
    assert parse_action("gig:INT:g1") is None
    assert parse_action("gigact:BOGUS:g1") is None
    assert parse_action("gigact:WORK:") is None


def test_card_renders_only_actions_with_handlers():
    card = build_action_card(_gig(), pitch="Hello!", language="en")
    rows = card["reply_markup"]["inline_keyboard"]
    callbacks = [
        b["callback_data"] for row in rows for b in row if "callback_data" in b
    ]
    for cb in callbacks:
        assert parse_action(cb) is not None
    actions = {parse_action(cb)[0] for cb in callbacks}
    assert actions == {ACTION_WORK, ACTION_DRAFT, ACTION_SKIP}


def test_card_uses_url_button_when_url_present():
    card = build_action_card(_gig(), pitch="Hi")
    urls = [b["url"] for row in card["reply_markup"]["inline_keyboard"]
            for b in row if "url" in b]
    assert "https://t.me/x/1" in urls


def test_card_open_is_callback_when_no_url():
    card = build_action_card(_gig(url=""), pitch="Hi")
    callbacks = [b.get("callback_data") for row in card["reply_markup"]["inline_keyboard"]
                 for b in row]
    assert callback_data(ACTION_OPEN, "g1") in callbacks


def test_work_queues_without_sending():
    calls = []
    out = handle_action(
        callback_data(ACTION_WORK, "g1"),
        get_gig_fn=lambda gid: _gig(),
        set_status_fn=lambda gid, st, note: calls.append((gid, st, note)),
    )
    assert out["outcome"] == "queued"
    assert out["status"] == "SHORTLISTED"
    assert calls == [("g1", "SHORTLISTED", "gig action WORK")]
    # Taking into work must NOT involve any send/render action.
    assert out.get("draft") is None


def test_skip_sets_skipped():
    calls = []
    out = handle_action(
        callback_data(ACTION_SKIP, "g1"),
        get_gig_fn=lambda gid: _gig(),
        set_status_fn=lambda gid, st, note: calls.append(st),
    )
    assert out["outcome"] == "skipped"
    assert out["status"] == "SKIPPED"
    assert calls == ["SKIPPED"]


def test_draft_returns_text_without_mutation():
    calls = []
    out = handle_action(
        callback_data(ACTION_DRAFT, "g1"),
        get_gig_fn=lambda gid: _gig(),
        set_status_fn=lambda *a: calls.append(a),
        render_draft_fn=lambda g: "Hello! I can do it.",
    )
    assert out["outcome"] == "draft"
    assert out["draft"] == "Hello! I can do it."
    assert calls == []


def test_open_returns_url():
    out = handle_action(callback_data(ACTION_OPEN, "g1"), get_gig_fn=lambda gid: _gig())
    assert out["outcome"] == "opened"
    assert out["url"] == "https://t.me/x/1"


def test_unknown_gig_refused_without_writes():
    calls = []
    out = handle_action(
        callback_data(ACTION_WORK, "missing"),
        get_gig_fn=lambda gid: None,
        set_status_fn=lambda *a: calls.append(a),
    )
    assert out["outcome"] == "refused"
    assert calls == []


def test_callback_id_idempotent():
    acked: set[str] = set()
    first = handle_action(
        callback_data(ACTION_WORK, "g1"),
        get_gig_fn=lambda gid: _gig(),
        set_status_fn=lambda *a: None,
        callback_query_id="q1",
        acked_ids=acked,
    )
    second = handle_action(
        callback_data(ACTION_WORK, "g1"),
        get_gig_fn=lambda gid: _gig(),
        set_status_fn=lambda *a: None,
        callback_query_id="q1",
        acked_ids=acked,
    )
    assert first["outcome"] == "queued"
    assert second["outcome"] == "already-applied"


def test_already_in_state_is_noop():
    out = handle_action(
        callback_data(ACTION_WORK, "g1"),
        get_gig_fn=lambda gid: _gig(status="SHORTLISTED"),
        set_status_fn=lambda *a: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    assert out["outcome"] == "already-applied"


def test_confirmation_lines():
    assert "Взято в работу" in action_confirmation("queued", "T", "SHORTLISTED")
    assert "Пропущено" in action_confirmation("skipped", "T", "SKIPPED")
    assert action_confirmation("draft") is None
    assert CALLBACK_PREFIX == "gigact:"
