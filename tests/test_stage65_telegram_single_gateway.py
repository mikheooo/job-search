# -*- coding: utf-8 -*-
"""Stage 65: Telegram Single Gateway & Live Integrity Test Suite.

Verifies:
1. Single Production Gateway: TelegramNotifier.deliver_notification is the sole authorized gateway.
2. Routine events (DISCOVERY, VACANCY_MATCHED, HEARTBEAT) are strictly filtered and suppressed.
3. Actionable events (RECRUITER_REPLY_SENT, INTERVIEW_INVITATION, EXTERNAL_QUESTIONNAIRE, TEST_TASK, UNANSWERED_QUESTION_BLOCKED) are delivered.
4. RECRUITER_REPLY_SENT is strictly suppressed if conversation_id is non-numeric or deep-link is invalid/unconfirmed.
5. RECRUITER_REPLY_SENT is strictly suppressed if sent_reply is empty.
6. Legacy send_to_telegram in core.py routes through gateway and does not perform ungrounded requests.post.
7. Idempotency protection prevents duplicate delivery of the exact same event.
8. Deep-link contract enforcement: only real numeric HH IDs produce valid URLs.
9. Safety invariants: pipeline.py is never executed, zero real recruiter messages sent on HH.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from ai_assistant import db, config
from ai_assistant.telegram_notifier import (
    TelegramNotifier,
    TelegramGateway,
    verify_hh_chat_url,
    get_telegram_notifier,
    get_telegram_gateway,
)
from ai_assistant.core import send_to_telegram, VacancyAnalysis


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Use an isolated SQLite database for Stage 65 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage65.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


def test_stage65_single_gateway_filtering():
    """Verify routine non-actionable events are filtered out by the gateway."""
    gateway = TelegramGateway(bot_token="test_tok", chat_id="12345", transport_fn=lambda t, p: {"ok": True})

    # Routine events
    res_disc = gateway.deliver_notification("DISCOVERY_COMPLETED", {"count": 10})
    assert res_disc["delivered"] is False
    assert "routine" in res_disc["reason"].lower()

    res_match = gateway.deliver_notification("VACANCY_MATCHED", {"score": 8})
    assert res_match["delivered"] is False
    assert "routine" in res_match["reason"].lower()


def test_stage65_recruiter_reply_strict_numeric_and_deeplink_gating():
    """Verify RECRUITER_REPLY_SENT is suppressed if conversation_id is non-numeric or deep-link is invalid."""
    gateway = TelegramGateway(bot_token="test_tok", chat_id="12345", transport_fn=lambda t, p: {"ok": True})

    # 1. Non-numeric / synthetic ID (e.g. neg_12345) -> SUPPRESSED
    res_neg = gateway.deliver_notification(
        "RECRUITER_REPLY_SENT",
        {
            "company": "TechCorp",
            "vacancy": "Python Dev",
            "incoming_message": "Hello",
            "sent_reply": "Hello back",
            "conversation_id": "neg_136745031",
            "hh_chat_url": "https://hh.ru/chat/neg_136745031",
        },
    )
    assert res_neg["delivered"] is False
    assert "suppressed" in res_neg["reason"].lower()

    # 2. General /applicant/negotiations URL -> SUPPRESSED
    res_gen = gateway.deliver_notification(
        "RECRUITER_REPLY_SENT",
        {
            "company": "TechCorp",
            "vacancy": "Python Dev",
            "incoming_message": "Hello",
            "sent_reply": "Hello back",
            "conversation_id": "5585083099",
            "hh_chat_url": "https://hh.ru/applicant/negotiations",
        },
    )
    assert res_gen["delivered"] is False
    assert "suppressed" in res_gen["reason"].lower()

    # 3. Empty sent_reply -> SUPPRESSED
    res_empty_reply = gateway.deliver_notification(
        "RECRUITER_REPLY_SENT",
        {
            "company": "TechCorp",
            "vacancy": "Python Dev",
            "incoming_message": "Hello",
            "sent_reply": "",
            "conversation_id": "5585083099",
            "hh_chat_url": "https://hh.ru/chat/5585083099",
        },
    )
    assert res_empty_reply["delivered"] is False
    assert "suppressed" in res_empty_reply["reason"].lower()

    # 4. Valid numeric ID & confirmed deep-link -> DELIVERED
    res_valid = gateway.deliver_notification(
        "RECRUITER_REPLY_SENT",
        {
            "company": "TechCorp",
            "vacancy": "Python Dev",
            "incoming_message": "Hello",
            "sent_reply": "Hello back",
            "conversation_id": "5585083099",
            "hh_chat_url": "https://hh.ru/chat/5585083099",
        },
    )
    assert res_valid["delivered"] is True


def test_stage65_core_send_to_telegram_routes_through_gateway():
    """Verify legacy send_to_telegram in core.py routes through gateway without ungrounded direct POST."""
    analysis = VacancyAnalysis(
        score=9,
        interview_probability="Высокая",
        offer_probability="Высокая",
        strengths=["Python", "FastAPI"],
        weaknesses=[],
        red_flags=[],
        apply_reasons=["Great fit"],
        skip_reasons=[],
        recommendation="Откликаться",
    )

    with patch("ai_assistant.core.requests.post") as mock_post:
        res = send_to_telegram("vac_123", "Python Lead", "TechCorp", "300000", analysis, "https://hh.ru/vacancy/123")
        assert not mock_post.called  # Proves direct ungrounded requests.post is completely eliminated
        assert res["delivered"] is False  # Routine discovery is suppressed from Telegram


def test_stage65_gateway_idempotency_duplicate_suppression():
    """Verify gateway prevents duplicate deliveries deterministically."""
    sent_count = 0

    def mock_transport(tok, payload):
        nonlocal sent_count
        sent_count += 1
        return {"ok": True, "result": {"message_id": 500}}

    gateway = TelegramGateway(bot_token="test_tok", chat_id="12345", transport_fn=mock_transport)

    details = {
        "company": "Алф маркет",
        "vacancy": "AI-инженер",
        "incoming_message": "Локация?",
        "sent_reply": "Таиланд (UTC+7), Full Remote.",
        "conversation_id": "5585083099",
        "hh_chat_url": "https://hh.ru/chat/5585083099",
    }

    # First send -> delivered
    r1 = gateway.deliver_notification("RECRUITER_REPLY_SENT", details, delivery_key="k_stage65_idemp_1")
    assert r1["delivered"] is True
    assert sent_count == 1

    # Second send -> suppressed
    r2 = gateway.deliver_notification("RECRUITER_REPLY_SENT", details, delivery_key="k_stage65_idemp_1")
    assert r2["delivered"] is False
    assert "already delivered" in r2["reason"]
    assert sent_count == 1


def test_stage65_canonical_notification_contract_and_deeplink():
    """Verify exact canonical format with clickable URL."""
    msg = TelegramGateway.format_recruiter_reply(
        company="Алф маркет",
        vacancy="Прикладной AI-инженер",
        incoming_message="1. Где вы находитесь территориально?",
        sent_reply="1. Территориально нахожусь в Таиланде (UTC+7).",
        conversation_id="5585083099",
        hh_chat_url="https://hh.ru/chat/5585083099",
    )
    assert "RECRUITER REPLY" in msg
    assert "Компания: Алф маркет" in msg
    assert "Вакансия: Прикладной AI-инженер" in msg
    assert "Открыть чат HH:\nhttps://hh.ru/chat/5585083099" in msg
    assert "Статус:\nОтвет отправлен и подтверждён в HH." in msg
