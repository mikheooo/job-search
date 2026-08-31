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
from ai_assistant.hh_message_reply import (
    HHDialog,
    HHMessage,
    classify_hh_conversation_detailed,
)
import ai_assistant.config as config


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Use an isolated SQLite database for Stage 60 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage60.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


def test_stage60_confirmed_reply_format():
    """Verify exact Telegram format for confirmed recruiter reply per Stage 60 specification."""
    msg = TelegramNotifier.format_recruiter_reply(
        company="Coding Team",
        vacancy="Backend Python разработчик",
        incoming_message="Расскажите, пожалуйста, был ли у вас опыт настройки Nginx в роли обратного прокси?",
        sent_reply="Да, есть практический опыт настройки Nginx в качестве reverse proxy для веб-приложений и API.",
        conversation_id="5585421175",
        status="CONFIRMED",
    )
    assert "RECRUITER REPLY" in msg
    assert "Компания: Coding Team" in msg
    assert "Вакансия: Backend Python разработчик" in msg
    assert "Рекрутер:\nРасскажите, пожалуйста, был ли у вас опыт настройки Nginx в роли обратного прокси?" in msg
    assert "Мой ответ:\nДа, есть практический опыт настройки Nginx в качестве reverse proxy для веб-приложений и API." in msg
    assert "Открыть чат HH:\nhttps://hh.ru/chat/5585421175" in msg
    assert "Статус:\nОтвет отправлен и подтверждён в HH." in msg


def test_stage60_categorization_logic():
    """Verify strict 4-category classification of conversation records."""
    # 1. CONFIRMED
    def eval_confirmed(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_conf_1", "verified_in_hh": True})
        return json.dumps([{"conversation_id": "conv_conf_1", "vacancy_title": "AI Engineer", "employer": "Alf", "messages": [
            {"message_id": "m1", "sender": "employer", "text": "Ваша локация?"}
        ]}])

    agent_conf = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=eval_confirmed))
    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_notif:
        res = agent_conf._process_messages()
        assert res["auto_replies_count"] == 1
        assert mock_notif.called

    # 2. GENERATED_NOT_SENT
    def eval_not_sent(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_not_sent", "verified_in_hh": False})
        return json.dumps([{"conversation_id": "conv_not_sent", "vacancy_title": "Python Dev", "employer": "Axis", "messages": [
            {"message_id": "m1", "sender": "employer", "text": "RAG опыт?"}
        ]}])

    agent_not_sent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=eval_not_sent))
    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_notif:
        res = agent_not_sent._process_messages()
        assert res["auto_replies_count"] == 0
        assert not mock_notif.called


def test_stage60_external_questionnaire_not_mistaken_for_reply():
    """Verify Google Forms links are categorized as EXTERNAL_QUESTIONNAIRE and NOT sent as auto-reply."""
    dialog = HHDialog(
        conversation_id="conv_google_form",
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:5585084622",
        employer="rodinka.recruitment",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="00:07"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Михаил, заполните анкету: https://forms.gle/WAqEAYZMRymwaCNB6",
                sent_at="00:08",
            ),
        ],
    )

    def mock_eval(script):
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))

    with patch.object(NotificationDispatcher, "notify_external_questionnaire") as mock_ext:
        with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_sent:
            res = agent._process_messages()
            assert mock_ext.called
            assert not mock_sent.called
            assert res["auto_replies_count"] == 0


def test_stage60_no_duplicate_telegram_notifications():
    """Ensure idempotency prevents multiple Telegram dispatches for the same confirmed reply."""
    notifier = TelegramNotifier()
    notifier.bot_token = "dummy_token"
    notifier.chat_id = "12345678"

    with patch.object(notifier, "send_message", return_value={"ok": True, "result": {"message_id": 101}}):
        d1 = notifier.deliver_notification(
            notif_type="RECRUITER_REPLY_SENT",
            details={
                "company": "Coding Team",
                "vacancy": "Python Dev",
                "incoming_message": "Question 1",
                "sent_reply": "Reply 1",
                "conversation_id": "5585421175",
                "hh_chat_url": "https://hh.ru/chat/5585421175",
            },
            delivery_key="unique_key_101",
        )
        assert d1["delivered"] is True

        d2 = notifier.deliver_notification(
            notif_type="RECRUITER_REPLY_SENT",
            details={
                "company": "Coding Team",
                "vacancy": "Python Dev",
                "incoming_message": "Question 1",
                "sent_reply": "Reply 1",
                "conversation_id": "5585421175",
                "hh_chat_url": "https://hh.ru/chat/5585421175",
            },
            delivery_key="unique_key_101",
        )
        assert d2["delivered"] is False
        assert "already delivered" in d2["reason"]
