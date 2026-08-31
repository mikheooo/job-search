# -*- coding: utf-8 -*-
import json
import pytest
from unittest.mock import MagicMock, patch

from ai_assistant import db
from ai_assistant.telegram_notifier import TelegramNotifier
from ai_assistant.hh_autonomous_agent import (
    AutonomousJobAgent,
    AutonomousConfig,
    NotificationDispatcher,
    NotificationType,
)
from ai_assistant.hh_message_reply import HHDialog, HHMessage
import ai_assistant.config as config


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Use an isolated SQLite database for Stage 59 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage59.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


def test_telegram_format_recruiter_reply_confirmed():
    """Verify exact formatting of confirmed recruiter reply."""
    msg = TelegramNotifier.format_recruiter_reply(
        company="TechCorp",
        vacancy="Senior Python Developer",
        incoming_message="Tell us about your experience with FastAPI?",
        sent_reply="Hello! Over 3 years of commercial experience with FastAPI.",
        conversation_id="5585083099",
        status="CONFIRMED",
    )
    assert "RECRUITER REPLY" in msg
    assert "Компания: TechCorp" in msg
    assert "Вакансия: Senior Python Developer" in msg
    assert "Рекрутер:\nTell us about your experience with FastAPI?" in msg
    assert "Мой ответ:\nHello! Over 3 years of commercial experience with FastAPI." in msg
    assert "Открыть чат HH:\nhttps://hh.ru/chat/5585083099" in msg
    assert "Статус:\nОтвет отправлен и подтверждён в HH." in msg


def test_telegram_format_external_questionnaire():
    """Verify exact formatting of external questionnaire."""
    msg = TelegramNotifier.format_external_questionnaire(
        company="rodinka.recruitment",
        vacancy="AI Engineer",
        what_they_want="Please complete our screening form",
        url="https://forms.gle/WAqEAYZMRymwaCNB6",
        questions="Google Form questionnaire",
        action="REQUIRES REVIEW",
    )
    assert "EXTERNAL QUESTIONNAIRE" in msg
    assert "Company:\nrodinka.recruitment" in msg
    assert "Vacancy:\nAI Engineer" in msg
    assert "URL:\nhttps://forms.gle/WAqEAYZMRymwaCNB6" in msg
    assert "Action:\nREQUIRES REVIEW" in msg


def test_telegram_format_test_task():
    """Verify exact formatting of test task."""
    msg = TelegramNotifier.format_test_task(
        company="DevStudio",
        vacancy="Backend Engineer",
        task_description="Complete technical assignment in repository",
        url="https://github.com/example/test-task",
        action="REQUIRES REVIEW",
    )
    assert "TEST TASK" in msg
    assert "Company:\nDevStudio" in msg
    assert "Vacancy:\nBackend Engineer" in msg
    assert "Task:\nComplete technical assignment in repository" in msg
    assert "Action:\nREQUIRES REVIEW" in msg


def test_external_questionnaire_detection_in_dialog():
    """Verify external form URLs trigger EXTERNAL_QUESTIONNAIRE and skip auto-replying standard text."""
    dialog = HHDialog(
        conversation_id="conv_ext_1",
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:123456",
        employer="rodinka.recruitment",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Application response", sent_at="10:00"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Please complete our candidate questionnaire at https://forms.gle/WAqEAYZMRymwaCNB6",
                sent_at="10:05",
            ),
        ],
    )

    def mock_eval(script):
        if "chat-input-textarea" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_ext_1", "verified_in_hh": True})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))

    with patch.object(NotificationDispatcher, "notify_external_questionnaire", wraps=NotificationDispatcher.notify_external_questionnaire) as mock_ext_notif:
        with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_sent_notif:
            res = agent._process_messages()
            assert mock_ext_notif.called
            assert not mock_sent_notif.called
            assert res["auto_replies_count"] == 0

    # Verify audit in DB
    audits = db.list_conversation_audits(conversation_id="conv_ext_1")
    assert len(audits) == 1
    assert audits[0]["message_classification"] == "EXTERNAL_QUESTIONNAIRE"
    assert audits[0]["status"] == "GENERATED"
    assert "forms.gle" in audits[0]["decision_reason"]


def test_auto_reply_sent_confirmed_in_hh():
    """Verify candidate reply is marked SENT and notified ONLY when post-send HH DOM verification succeeds."""
    dialog = HHDialog(
        conversation_id="conv_rag_1",
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:999888",
        employer="Axis",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Application response", sent_at="10:00"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Could you tell us about your experience with Python and FastAPI?",
                sent_at="10:05",
            ),
        ],
    )

    def mock_eval_fn(script):
        if "chat-input-textarea" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_rag_1", "verified_in_hh": True})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval_fn))

    with patch.object(NotificationDispatcher, "notify_reply_sent", wraps=NotificationDispatcher.notify_reply_sent) as mock_reply_notif:
        res = agent._process_messages()
        assert res["auto_replies_count"] == 1
        assert mock_reply_notif.called

    # Verify audit in DB
    audits = db.list_conversation_audits(conversation_id="conv_rag_1")
    assert len(audits) == 1
    assert audits[0]["status"] == "SENT"
    assert audits[0]["sent_reply"] is not None
    assert audits[0]["sent_at"] is not None


def test_auto_reply_missing_in_hh_blocks_notification():
    """Verify that if post-send DOM verification fails (message not found in HH), status is FAILED and NO notification is sent."""
    dialog = HHDialog(
        conversation_id="conv_failed_1",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:777666",
        employer="JT marketing",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Application response", sent_at="10:00"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Could you tell us about your experience with Python?",
                sent_at="10:05",
            ),
        ],
    )

    def mock_eval_fn(script):
        if "chat-input-textarea" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_failed_1", "verified_in_hh": False})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval_fn))

    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply_notif:
        res = agent._process_messages()
        assert res["auto_replies_count"] == 0
        assert not mock_reply_notif.called

    # Verify audit in DB
    audits = db.list_conversation_audits(conversation_id="conv_failed_1")
    assert len(audits) == 1
    assert audits[0]["status"] == "FAILED"
    assert audits[0]["sent_reply"] is None


def test_telegram_notification_idempotency():
    """Verify duplicate notifications are suppressed via idempotency key."""
    notifier = TelegramNotifier()
    notifier.bot_token = "dummy_token"
    notifier.chat_id = "12345678"

    with patch.object(notifier, "send_message", return_value={"ok": True, "result": {"message_id": 999}}):
        # First delivery: should succeed
        res1 = notifier.deliver_notification(
            notif_type="RECRUITER_REPLY_SENT",
            details={
                "company": "Company A",
                "vacancy": "Python Dev",
                "incoming_message": "Hello",
                "sent_reply": "Hi there",
                "conversation_id": "5585083099",
                "hh_chat_url": "https://hh.ru/chat/5585083099",
            },
            delivery_key="reply_5585083099_idem",
        )
        assert res1["delivered"] is True

        # Second delivery with same key: must be skipped
        res2 = notifier.deliver_notification(
            notif_type="RECRUITER_REPLY_SENT",
            details={
                "company": "Company A",
                "vacancy": "Python Dev",
                "incoming_message": "Hello",
                "sent_reply": "Hi there",
                "conversation_id": "5585083099",
                "hh_chat_url": "https://hh.ru/chat/5585083099",
            },
            delivery_key="reply_5585083099_idem",
        )
        assert res2["delivered"] is False
        assert "already delivered" in res2["reason"]
