# -*- coding: utf-8 -*-
import os
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
    """Use an isolated SQLite database for Stage 61 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage61.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


def test_stage61_audit_report_artifacts_exist():
    """Verify artifacts for Stage 61 forensic audit exist and are valid JSON and Markdown."""
    json_path = "artifacts/stage61_telegram_reply_audit.json"
    md_path = "artifacts/stage61_telegram_reply_audit.md"

    assert os.path.exists(json_path), f"Missing {json_path}"
    assert os.path.exists(md_path), f"Missing {md_path}"

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert "TOTAL_HISTORICAL_MY_REPLY" in data
        assert "CONFIRMED" in data
        assert "GENERATED_NOT_SENT" in data
        assert "WRONG_CONTEXT" in data
        assert "DEFAULT_REPLY_INSTANCES" in data
        assert len(data["entries"]) >= 10


def test_stage61_wrong_context_suppresses_reply():
    """Verify that negotiation list statuses (e.g. 'Статус: Собеседование') are identified as WRONG_CONTEXT and suppressed."""
    dialog = HHDialog(
        conversation_id="neg_136745031",
        vacancy_title="Senior Python Developer",
        vacancy_stable_id="hh:136745031",
        employer="Лист Ренталс Лимитед",
        messages=[
            HHMessage(message_id="m1", sender="employer", text="Статус: Собеседование", sent_at="19:59"),
        ],
    )

    def mock_eval(script):
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))

    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
        res = agent._process_messages()
        assert not mock_reply.called
        assert res["auto_replies_count"] == 0


def test_stage61_generated_not_sent_blocks_notification():
    """Verify that generated draft answers that fail DOM verification never trigger Telegram notification."""
    dialog = HHDialog(
        conversation_id="5587330524",
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:5587330524",
        employer="Аксис",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик на вакансию", sent_at="10:00"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Расскажите, пожалуйста, реализовывали ли вы RAG с явной привязкой ответов к источникам?",
                sent_at="10:05",
            ),
        ],
    )

    def mock_eval(script):
        if "chat-input" in script or "input.value" in script:
            # Send attempted, but message NOT confirmed in DOM
            return json.dumps({"ok": True, "conversation_id": "5587330524", "verified_in_hh": False})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))

    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
        res = agent._process_messages()
        assert res["auto_replies_count"] == 0
        assert not mock_reply.called

    # Audit in DB must record FAILED status
    audits = db.list_conversation_audits(conversation_id="5587330524")
    assert len(audits) == 1
    assert audits[0]["status"] == "FAILED"


def test_stage61_confirmed_candidate_reply_dispatches_with_dom_proof():
    """Verify confirmed candidate response with DOM proof dispatches RECRUITER_REPLY_SENT."""
    dialog = HHDialog(
        conversation_id="5585083099",
        vacancy_title="Прикладной AI-инженер",
        vacancy_stable_id="hh:5585083099",
        employer="Алф маркет",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="00:05"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="1. Где вы сейчас находитесь территориально?",
                sent_at="00:06",
            ),
        ],
    )

    def mock_eval(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "5585083099", "verified_in_hh": True})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))

    with patch.object(NotificationDispatcher, "notify_reply_sent", wraps=NotificationDispatcher.notify_reply_sent) as mock_reply:
        res = agent._process_messages()
        assert res["auto_replies_count"] == 1
        assert mock_reply.called

    audits = db.list_conversation_audits(conversation_id="5585083099")
    assert len(audits) == 1
    assert audits[0]["status"] == "SENT"
