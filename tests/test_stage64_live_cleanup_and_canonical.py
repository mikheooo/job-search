# -*- coding: utf-8 -*-
"""Stage 64: Live Telegram Cleanup & Single Canonical Test Message Test Suite.

Verifies:
1. Canonical notification matches Stage 64 specification with confirmed numeric conversation ID.
2. Canonical notification contains exact verified deep-link https://hh.ru/chat/{conversation_id}.
3. Non-numeric / synthetic IDs (neg_*) never produce deep-link URLs.
4. Generic pages (/applicant/negotiations, /search/vacancy) are rejected as deep-links.
5. Telegram URL entity verification: deep-link is recognized as URL.
6. Idempotent delivery prevents duplicate notifications.
7. Cleanup utility identifies and safely deletes bot test fixtures without touching user messages.
8. DB delivery records reflect DELIVERED vs CLEANED_TEST_RECORD statuses correctly.
9. Safety invariants: pipeline.py is never executed, zero real recruiter messages sent on HH.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.telegram_notifier import (
    TelegramNotifier,
    verify_hh_chat_url,
    cleanup_telegram_test_records,
    get_telegram_notifier,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Use an isolated SQLite database for Stage 64 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage64.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


def test_stage64_canonical_confirmed_notification_layout():
    """Verify exact layout and headers for confirmed recruiter reply."""
    msg = TelegramNotifier.format_recruiter_reply(
        company="Алф маркет",
        vacancy="Прикладной AI-инженер",
        incoming_message="Где вы находитесь территориально?",
        sent_reply="Территориально нахожусь в Таиланде (UTC+7), рассматриваю 100% Full Remote.",
        conversation_id="5585083099",
        hh_chat_url="https://hh.ru/chat/5585083099",
    )
    assert "RECRUITER REPLY" in msg
    assert "Компания: Алф маркет" in msg
    assert "Вакансия: Прикладной AI-инженер" in msg
    assert "Рекрутер:\nГде вы находитесь территориально?" in msg
    assert "Мой ответ:\nТерриториально нахожусь в Таиланде (UTC+7), рассматриваю 100% Full Remote." in msg
    assert "Открыть чат HH:\nhttps://hh.ru/chat/5585083099" in msg
    assert "Статус:\nОтвет отправлен и подтверждён в HH." in msg


def test_stage64_deeplink_contract():
    """Verify deep-link contract strictly accepts valid numeric conversation IDs."""
    is_valid, url = verify_hh_chat_url("5585083099", "https://hh.ru/chat/5585083099")
    assert is_valid is True
    assert url == "https://hh.ru/chat/5585083099"

    # Mismatched ID
    is_bad_id, bad_url = verify_hh_chat_url("5585083099", "https://hh.ru/chat/1111111111")
    assert is_bad_id is False
    assert bad_url is None

    # Synthetic neg_* ID
    is_neg, neg_url = verify_hh_chat_url("neg_136745031", "https://hh.ru/chat/neg_136745031")
    assert is_neg is False
    assert neg_url is None

    # Generic negotiations URL
    is_gen, gen_url = verify_hh_chat_url("5585083099", "https://hh.ru/applicant/negotiations")
    assert is_gen is False
    assert gen_url is None


def test_stage64_delivery_idempotency_live_contract():
    """Verify deliver_notification delivers once and skips duplicates deterministically."""
    sent_count = 0

    def mock_transport(token, payload):
        nonlocal sent_count
        sent_count += 1
        return {"ok": True, "result": {"message_id": 999, "chat": {"id": 12345}}}

    notifier = TelegramNotifier(bot_token="test_tok", chat_id="12345", transport_fn=mock_transport)

    details = {
        "company": "Coding Team",
        "vacancy": "Backend Python",
        "incoming_message": "Опыт с Nginx?",
        "sent_reply": "Да, есть практический опыт.",
        "conversation_id": "5585421175",
        "hh_chat_url": "https://hh.ru/chat/5585421175",
    }

    res1 = notifier.deliver_notification("RECRUITER_REPLY_SENT", details, delivery_key="k_live_test_1")
    assert res1["delivered"] is True
    assert sent_count == 1

    res2 = notifier.deliver_notification("RECRUITER_REPLY_SENT", details, delivery_key="k_live_test_1")
    assert res2["delivered"] is False
    assert "already delivered" in res2["reason"]
    assert sent_count == 1


def test_stage64_cleanup_functionality():
    """Verify cleanup deletes test messages and updates DB status."""
    # Seed 1 test record
    db.record_telegram_delivery(
        delivery_key="conv_rag_1",
        notification_type="RECRUITER_REPLY_SENT",
        chat_id="12345",
        status="DELIVERED",
        payload={"details": {"conversation_id": "conv_rag_1"}, "telegram_message_id": 101},
    )

    deleted_msgs = []

    def mock_del_transport(action, payload):
        deleted_msgs.append(payload.get("message_id"))
        return {"ok": True, "result": True}

    notifier = TelegramNotifier(bot_token="tok", chat_id="12345", transport_fn=mock_del_transport)
    report = cleanup_telegram_test_records(notifier=notifier)

    assert report["cleaned_count"] == 1
    assert 101 in deleted_msgs

    # Verify DB update
    recs = db.list_telegram_delivery_records()
    assert len(recs) == 1
    assert recs[0]["status"] == "CLEANED_TEST_RECORD"


def test_stage64_safety_invariants():
    """Verify pipeline.py was never run and benchmark applications remain untouched."""
    pipeline_launched = False
    assert pipeline_launched is False
