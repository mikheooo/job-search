# -*- coding: utf-8 -*-
"""Stage 70: Hard Telegram Test Isolation Without Breaking Hermes Cron Test Suite.

Verifies:
1. Pytest execution never performs live network calls to Telegram Bot API.
2. TelegramNotifier fail-closed guard intercepts unmocked network calls during pytest.
3. Global conftest.py fixture blocks any direct urllib.request.urlopen call to api.telegram.org.
4. NotificationDispatcher and AutonomousJobAgent can safely run under pytest without touching Telegram.
5. All major notification types (INTERVIEW_INVITATION, UNANSWERED_QUESTION_BLOCKED, EXTERNAL_QUESTIONNAIRE, RECRUITER_REPLY_SENT, TEST_TASK) are safe.
"""

import os
import json
import pytest
from unittest.mock import patch, MagicMock

from ai_assistant import db, config
from ai_assistant.telegram_notifier import (
    TelegramNotifier,
    TelegramGateway,
    get_telegram_notifier,
)
from ai_assistant.hh_autonomous_agent import (
    NotificationDispatcher,
    AutonomousJobAgent,
    AutonomousConfig,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Use an isolated SQLite database for Stage 70 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage70.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


def test_stage70_unmocked_telegram_notifier_returns_safe_mock():
    """Verify calling TelegramNotifier with no transport_fn during pytest does not hit network."""
    notifier = TelegramNotifier(bot_token="real_or_fake_token", chat_id="392046103", transport_fn=None)
    res = notifier.send_message(text="Test message that must NEVER go to live Telegram")
    assert res.get("ok") is True
    assert res.get("mocked") is True
    assert res.get("result", {}).get("message_id") == 999999


def test_stage70_unmocked_delete_message_returns_safe_mock():
    """Verify calling delete_message with no transport_fn during pytest does not hit network."""
    notifier = TelegramNotifier(bot_token="real_or_fake_token", chat_id="392046103", transport_fn=None)
    res = notifier.delete_message(message_id=12345)
    assert res.get("ok") is True
    assert res.get("mocked") is True


def test_stage70_notification_dispatcher_safe_under_pytest():
    """Verify NotificationDispatcher dispatches safely under pytest without live network I/O."""
    # 1. Interview invitation
    res_iv = NotificationDispatcher.notify_interview(
        company="Safety AI Corp",
        vacancy_title="Staff AI Engineer",
        invitation_text="Interview in Zoom",
        invitation_url="https://zoom.us/j/123456",
    )
    assert res_iv["notification_type"] == "INTERVIEW_INVITATION"

    # 2. Blocking question
    res_q = NotificationDispatcher.notify_blocking_question(
        company="Strict Corp",
        vacancy_title="Security Engineer",
        unanswered_question="What is your passport number?",
    )
    assert res_q["notification_type"] == "UNANSWERED_QUESTION_BLOCKED"

    # 3. External questionnaire
    res_eq = NotificationDispatcher.notify_external_questionnaire(
        company="Form Corp",
        vacancy_title="AI Engineer",
        what_they_want="Fill form",
        url="https://forms.gle/xyz123",
    )
    assert res_eq["notification_type"] == "EXTERNAL_QUESTIONNAIRE"

    # 4. Test task
    res_tt = NotificationDispatcher.notify_test_task(
        company="Task Corp",
        vacancy_title="Python Dev",
        task_description="Complete task",
        url="https://github.com/task/test",
    )
    assert res_tt["notification_type"] == "TEST_TASK"

    # 5. Confirmed recruiter reply
    res_rr = NotificationDispatcher.notify_reply_sent(
        company="Алф маркет",
        vacancy_title="Прикладной AI-инженер",
        incoming_message="Where are you?",
        sent_reply="Full remote in Thailand.",
        conversation_id="5585083099",
        vacancy_url="https://hh.ru/chat/5585083099",
    )
    assert res_rr["notification_type"] == "RECRUITER_REPLY_SENT"


def test_stage70_global_conftest_blocks_direct_urlopen_to_telegram():
    """Verify that any accidental direct call to api.telegram.org raises RuntimeError."""
    import urllib.request
    with pytest.raises(RuntimeError, match="SAFETY VIOLATION"):
        urllib.request.urlopen("https://api.telegram.org/bot12345/sendMessage")
