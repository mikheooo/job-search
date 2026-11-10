"""Tests for the live Telegram poller wiring of gig action buttons (Phase 2)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai_assistant.gig.actions import callback_data, ACTION_DRAFT, ACTION_OPEN, ACTION_SKIP, ACTION_WORK


class _Gig:
    def __init__(self, status="NEW"):
        self.id = "g1"
        self.title = "Build a Telegram bot"
        self.status = MagicMock(value=status)
        self.url = "https://t.me/x/1"
        self.source = "freelancer"
        self.description = "Need a Python Telegram bot"
        self.budget_text = "500 USD"


@pytest.fixture()
def bot():
    import ai_assistant.telegram_control_bot as m
    return m


def test_work_queues_and_confirms(bot):
    status_calls = []
    msgs = []
    with patch("ai_assistant.gig.store.get_gig", return_value=_Gig()), \
         patch("ai_assistant.gig.store.set_gig_status",
               side_effect=lambda gid, st, note=None: status_calls.append((gid, st))), \
         patch.object(bot, "send_message", side_effect=lambda cid, text, **kw: msgs.append(text)), \
         patch.object(bot, "answer_callback_query", return_value=True):
        bot.handle_gigact_callback(callback_data(ACTION_WORK, "g1"), "cq-work-1")
    assert status_calls == [("g1", "SHORTLISTED")]
    assert any("Взято в работу" in m for m in msgs)


def test_draft_sends_text_and_does_not_mutate(bot):
    status_calls = []
    msgs = []
    with patch("ai_assistant.gig.store.get_gig", return_value=_Gig()), \
         patch("ai_assistant.gig.store.set_gig_status",
               side_effect=lambda *a, **k: status_calls.append(a)), \
         patch.object(bot, "send_message", side_effect=lambda cid, text, **kw: msgs.append(text)), \
         patch.object(bot, "answer_callback_query", return_value=True):
        bot.handle_gigact_callback(callback_data(ACTION_DRAFT, "g1"), "cq-draft-1")
    assert status_calls == []
    joined = "\n".join(msgs)
    assert "Черновик" in joined
    # English order -> English draft (no auto-send to any client).
    assert "Hello!" in joined


def test_skip(bot):
    status_calls = []
    with patch("ai_assistant.gig.store.get_gig", return_value=_Gig()), \
         patch("ai_assistant.gig.store.set_gig_status",
               side_effect=lambda gid, st, note=None: status_calls.append(st)), \
         patch.object(bot, "send_message", return_value=True), \
         patch.object(bot, "answer_callback_query", return_value=True):
        bot.handle_gigact_callback(callback_data(ACTION_SKIP, "g1"), "cq-skip-1")
    assert status_calls == ["SKIPPED"]


def test_repeat_press_is_idempotent(bot):
    status_calls = []
    with patch("ai_assistant.gig.store.get_gig", return_value=_Gig()), \
         patch("ai_assistant.gig.store.set_gig_status",
               side_effect=lambda gid, st, note=None: status_calls.append(st)), \
         patch.object(bot, "send_message", return_value=True), \
         patch.object(bot, "answer_callback_query", return_value=True) as ack:
        bot.handle_gigact_callback(callback_data(ACTION_WORK, "g1"), "cq-dup-1")
        bot.handle_gigact_callback(callback_data(ACTION_WORK, "g1"), "cq-dup-1")
    assert status_calls == ["SHORTLISTED"]  # only once
    ack_texts = [str(c.args) + str(c.kwargs) for c in ack.call_args_list]
    assert any("Уже отмечено" in t for t in ack_texts)


def test_unknown_gig_refused(bot):
    with patch("ai_assistant.gig.store.get_gig", return_value=None), \
         patch.object(bot, "send_message", return_value=True), \
         patch.object(bot, "answer_callback_query", return_value=True):
        bot.handle_gigact_callback(callback_data(ACTION_WORK, "missing"), "cq-miss-1")


def test_open_with_url(bot):
    with patch("ai_assistant.gig.store.get_gig", return_value=_Gig()), \
         patch.object(bot, "send_message", return_value=True), \
         patch.object(bot, "answer_callback_query", return_value=True) as ack:
        bot.handle_gigact_callback(callback_data(ACTION_OPEN, "g1"), "cq-open-1")
    assert ack.called
