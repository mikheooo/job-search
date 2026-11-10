# -*- coding: utf-8 -*-
"""Test unread message reading and archived vacancy handling."""
import json
from unittest.mock import patch

import pytest

from ai_assistant import db
from ai_assistant.hh_autonomous_agent import AutonomousConfig, AutonomousJobAgent
from ai_assistant.hh_chat_monitor import process_negotiation_alerts
from ai_assistant.hh_message_reply import (
    HHDialog,
    HHMessage,
    classify_hh_conversation_detailed,
    classify_message,
    MessageClassification,
)


def test_archived_vacancy_classification():
    """Vacancies moved to archive require no candidate reply and are classified cleanly."""
    d = HHDialog(
        conversation_id="conv_archived_1",
        vacancy_title="DevOps Engineer",
        employer="Cloud Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Вакансия перенесена в архив", sent_at="10:05"),
        ],
    )
    # Check simple classifier
    basic = classify_message(d)
    assert basic == MessageClassification.NO_REPLY

    # Check detailed classifier
    detailed = classify_hh_conversation_detailed(d)
    assert detailed["classification"] == "NO_REPLY_NEEDED"
    assert detailed["prepared_reply"] is None


def test_unread_dialog_not_skipped_even_if_rejection_snippet(monkeypatch):
    """If a dialog has an unread badge, it must NOT be skipped, even if snippet looks rejected."""
    db.init_db()
    conv_id = "conv_unread_rej_1"

    # Pre-record a prior rejection in DB
    db.save_hh_message_event(
        message_fingerprint="fp_rej_prior",
        conversation_id=conv_id,
        sender="employer",
        text="К сожалению, отказ.",
        sent_at="2026-10-15T10:00:00Z",
        seen_at="2026-10-15T10:00:00Z",
        processed=1,
        classification="NO_REPLY_NEEDED",
        reply_draft=None,
        status="REJECTED",
    )

    # Now an incoming dialog arrives with unread_count=1 and snippet
    dialog = HHDialog(
        conversation_id=conv_id,
        vacancy_title="Linux Admin",
        employer="Hosting Co",
        has_unread=True,
        unread_count=1,
        messages=[
            HHMessage(message_id="m_snippet", sender="employer", text="К сожалению, отказ.", sent_at="12:00")
        ],
    )

    scripts_evaluated = []
    def mock_evaluate(script: str) -> str:
        scripts_evaluated.append(script)
        if "chat-input-textarea" in script:
            return json.dumps({"ok": True, "conversation_id": conv_id})
        return json.dumps({
            "conversation_id": conv_id,
            "messages": [
                {"direction": "INCOMING", "text": "К сожалению, отказ.", "sender": "employer"}
            ]
        })

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_evaluate))
    res = agent._process_messages([dialog], _eval_fn=mock_evaluate)

    # It must have loaded the full chat to clear the unread badge
    assert any("href*" in s and conv_id in s for s in scripts_evaluated), "The unread dialog must be opened/clicked"
    assert res["auto_replies_count"] == 0, "No reply sent on rejection"


def test_negotiation_alerts_marks_archived_unread_as_read():
    """Unread notices on archived/rejected negotiation cards are recorded as read."""
    db.init_db()
    cards = [
        {
            "status": "Отказ",
            "lines": ["Senior DevOps", "Acme Corp", "Вакансия перенесена в архив"],
            "has_unread": True,
            "chat_url": "https://hh.ru/chat/12345",
        }
    ]
    alerts = process_negotiation_alerts(cards)
    assert alerts == 0, "Archived cards should not trigger telegram spam"

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM hh_negotiation_card_alerts WHERE card_key = ?", ("senior devops:acme corp:archived_read",))
    row = cur.fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "ARCHIVED/REJECTED"
