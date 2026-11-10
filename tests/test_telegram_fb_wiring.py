"""Tests for the Phase 2 wiring of digest ``fb:`` feedback callbacks in the live poller."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def bot():
    import ai_assistant.telegram_control_bot as m
    return m


_CQ = {
    "id": "cb-fb-1",
    "from": {"id": 392046103},
    "message": {"chat": {"id": -1004399255305}},
    "data": "fb:INT:hh:123",
}


def test_flag_off_is_inert_and_never_calls_processor(bot, monkeypatch):
    monkeypatch.setattr(bot.config, "TELEGRAM_FB_CALLBACKS_ENABLED", False, raising=False)
    proc_cls = MagicMock()
    with patch("ai_assistant.telegram_feedback.TelegramFeedbackProcessor", proc_cls), \
         patch.object(bot, "answer_callback_query", return_value=True) as ack:
        bot.handle_fb_callback(dict(_CQ), "cb-fb-1")
    proc_cls.assert_not_called()
    assert ack.called


def test_flag_on_routes_to_feedback_processor(bot, monkeypatch):
    monkeypatch.setattr(bot.config, "TELEGRAM_FB_CALLBACKS_ENABLED", True, raising=False)
    proc = MagicMock()
    proc_cls = MagicMock(return_value=proc)
    with patch("ai_assistant.telegram_feedback.TelegramFeedbackProcessor", proc_cls), \
         patch.object(bot, "answer_callback_query", return_value=True):
        bot.handle_fb_callback(dict(_CQ), "cb-fb-1")
    proc_cls.assert_called_once()
    proc.process_callback_query.assert_called_once()
    assert proc.process_callback_query.call_args.args[0]["data"] == "fb:INT:hh:123"


def test_processor_exception_falls_back_to_ack(bot, monkeypatch):
    monkeypatch.setattr(bot.config, "TELEGRAM_FB_CALLBACKS_ENABLED", True, raising=False)
    proc = MagicMock()
    proc.process_callback_query.side_effect = RuntimeError("boom")
    with patch("ai_assistant.telegram_feedback.TelegramFeedbackProcessor", MagicMock(return_value=proc)), \
         patch.object(bot, "answer_callback_query", return_value=True) as ack:
        bot.handle_fb_callback(dict(_CQ), "cb-fb-1")
    assert ack.called
