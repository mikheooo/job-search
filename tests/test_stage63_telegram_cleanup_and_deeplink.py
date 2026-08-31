# -*- coding: utf-8 -*-
"""Stage 63: Telegram Notification Cleanup & HH Chat Deep-Link Integrity Tests.

Verifies:
1. Confirmed reply contains conversation_id.
2. Confirmed reply contains hh_chat_url when valid and verified.
3. URL belongs to the exact same conversation_id.
4. General '/applicant/negotiations' cannot be substituted as a deep-link.
5. Fake/arbitrary URLs are rejected and cannot be used as verified deep-links.
6. If URL is unknown or unverified, notification does not claim deep-link (shows NOT AVAILABLE).
7. REPLY GENERATED — NOT SENT never converts to CONFIRMED.
8. DOM post-send verification remains mandatory.
9. Duplicate Telegram notification is blocked via idempotency.
10. neg_* conversation does not receive deep-link notification.
11. Status-card context does not receive deep-link notification.
12. Cleanup does not delete real non-test notifications.
13. Cleanup is safe and idempotent upon multiple executions.
14. Confirmed notification layout matches Stage 63 contract.
15. Generated-not-sent notification layout matches Stage 63 contract.
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


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Use an isolated SQLite database for Stage 63 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage63.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1: Confirmed reply contains conversation_id
# ---------------------------------------------------------------------------
def test_confirmed_reply_contains_conversation_id():
    msg = TelegramNotifier.format_recruiter_reply(
        company="Alf Market",
        vacancy="AI Engineer",
        incoming_message="Where are you located?",
        sent_reply="Located in Thailand (UTC+7), seeking 100% remote.",
        conversation_id="5585083099",
    )
    assert "Открыть чат HH:\nhttps://hh.ru/chat/5585083099" in msg
    assert "Статус:\nОтвет отправлен и подтверждён в HH." in msg


# ---------------------------------------------------------------------------
# Test 2: Confirmed reply contains hh_chat_url when available
# ---------------------------------------------------------------------------
def test_confirmed_reply_contains_verified_hh_chat_url():
    msg = TelegramNotifier.format_recruiter_reply(
        company="Coding Team",
        vacancy="Backend Python",
        incoming_message="Nginx experience?",
        sent_reply="Yes, practical Nginx reverse proxy experience.",
        conversation_id="5585421175",
        hh_chat_url="https://hh.ru/chat/5585421175",
    )
    assert "Открыть чат HH:\nhttps://hh.ru/chat/5585421175" in msg


# ---------------------------------------------------------------------------
# Test 3: URL belongs to the exact same conversation
# ---------------------------------------------------------------------------
def test_url_must_belong_to_same_conversation():
    is_valid, url = verify_hh_chat_url(
        conversation_id="5585083099",
        url="https://hh.ru/chat/5585083099",
    )
    assert is_valid is True
    assert url == "https://hh.ru/chat/5585083099"

    # Mismatched conversation ID in URL
    is_mismatched, bad_url = verify_hh_chat_url(
        conversation_id="5585083099",
        url="https://hh.ru/chat/9999999999",
    )
    assert is_mismatched is False
    assert bad_url is None


# ---------------------------------------------------------------------------
# Test 4: Cannot substitute /applicant/negotiations as deep-link
# ---------------------------------------------------------------------------
def test_cannot_substitute_applicant_negotiations_as_deeplink():
    is_valid, url = verify_hh_chat_url(
        conversation_id="5585083099",
        url="https://hh.ru/applicant/negotiations",
    )
    assert is_valid is False
    assert url is None


# ---------------------------------------------------------------------------
# Test 5: Cannot generate fake or arbitrary URL
# ---------------------------------------------------------------------------
def test_cannot_generate_fake_url():
    is_valid, url = verify_hh_chat_url(
        conversation_id="5585083099",
        url="https://fake-hh.example.com/chat/5585083099",
    )
    assert is_valid is False
    assert url is None


# ---------------------------------------------------------------------------
# Test 6: Unknown / invalid URL displays NOT AVAILABLE
# ---------------------------------------------------------------------------
def test_unknown_or_invalid_url_displays_not_available():
    msg = TelegramNotifier.format_recruiter_reply(
        company="Secret Corp",
        vacancy="Python Dev",
        incoming_message="Start date?",
        sent_reply="Ready immediately.",
        conversation_id="invalid_id_not_digits",
    )
    assert "Открыть чат HH:\nNOT AVAILABLE" in msg


# ---------------------------------------------------------------------------
# Test 7: REPLY GENERATED — NOT SENT does not become CONFIRMED
# ---------------------------------------------------------------------------
def test_reply_generated_not_sent_never_confirmed():
    msg = TelegramNotifier.format_reply_generated_not_sent(
        company="Axis",
        vacancy="AI Engineer",
        incoming_message="RAG details?",
        generated_reply="Over 3 years Python RAG experience.",
        conversation_id="5587330524",
    )
    assert "REPLY GENERATED — NOT SENT" in msg
    assert "HH: NOT CONFIRMED" in msg
    assert "HH: CONFIRMED" not in msg
    assert "Что делать:\nREQUIRES REVIEW" in msg


# ---------------------------------------------------------------------------
# Test 8: DOM verification remains strictly mandatory
# ---------------------------------------------------------------------------
def test_dom_verification_mandatory_for_reply_notification():
    dialog = HHDialog(
        conversation_id="5585083099",
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:139630001",
        employer="Alf Market",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Где вы находитесь территориально?", sent_at="10:05"),
        ],
    )

    # DOM verification fails
    def mock_eval_fail(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "5585083099", "verified_in_hh": False})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval_fail))
    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_notif:
        res = agent._process_messages()
        assert not mock_notif.called
        assert res["auto_replies_count"] == 0

    # DOM verification succeeds
    dialog_pass = HHDialog(
        conversation_id="5585083099_pass",
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:139630002",
        employer="Alf Market",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Где вы находитесь территориально?", sent_at="10:05"),
        ],
    )
    def mock_eval_pass(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "5585083099_pass", "verified_in_hh": True})
        return json.dumps({"conversations": [dialog_pass.model_dump()]})

    agent_pass = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval_pass))
    with patch.object(NotificationDispatcher, "notify_reply_sent", wraps=NotificationDispatcher.notify_reply_sent) as mock_notif_pass:
        res_pass = agent_pass._process_messages()
        assert mock_notif_pass.called
        assert res_pass["auto_replies_count"] == 1


# ---------------------------------------------------------------------------
# Test 9: Duplicate Telegram notification blocked (Idempotency)
# ---------------------------------------------------------------------------
def test_duplicate_telegram_notification_blocked():
    sent_count = 0

    def mock_transport(token, payload):
        nonlocal sent_count
        sent_count += 1
        return {"ok": True, "result": {"message_id": sent_count}}

    notifier = TelegramNotifier(bot_token="test_tok", chat_id="12345", transport_fn=mock_transport)

    details = {
        "company": "Alf Market",
        "vacancy": "AI Engineer",
        "incoming_message": "Where are you located?",
        "sent_reply": "Thailand (UTC+7), remote only.",
        "conversation_id": "5585083099",
    }

    res1 = notifier.deliver_notification("RECRUITER_REPLY_SENT", details)
    assert res1["delivered"] is True
    assert sent_count == 1

    # Second delivery of same message & conversation
    res2 = notifier.deliver_notification("RECRUITER_REPLY_SENT", details)
    assert res2["delivered"] is False
    assert "already delivered" in res2["reason"]
    assert sent_count == 1


# ---------------------------------------------------------------------------
# Test 10: neg_* conversation does not receive deep-link notification
# ---------------------------------------------------------------------------
def test_neg_conversation_cannot_receive_deeplink():
    is_valid, url = verify_hh_chat_url(
        conversation_id="neg_136745031",
        url="https://hh.ru/chat/neg_136745031",
    )
    assert is_valid is False
    assert url is None


# ---------------------------------------------------------------------------
# Test 11: Status-card context does not receive deep-link notification
# ---------------------------------------------------------------------------
def test_status_card_context_blocked_from_deeplink():
    dialog = HHDialog(
        conversation_id="neg_card_001",
        vacancy_title="Status Card",
        vacancy_stable_id="hh:999001",
        employer="Negotiation Card",
        messages=[
            HHMessage(message_id="m1", sender="employer", text="Статус: Собеседование", sent_at="10:00"),
        ],
    )
    def mock_eval(script):
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_notif:
        res = agent._process_messages()
        assert not mock_notif.called
        assert res["auto_replies_count"] == 0


# ---------------------------------------------------------------------------
# Test 12: Cleanup does not delete real non-test notifications
# ---------------------------------------------------------------------------
def test_cleanup_preserves_real_notifications():
    # Insert a real production delivery record
    db.record_telegram_delivery(
        delivery_key="reply_5585083099_abc123",
        notification_type="RECRUITER_REPLY_SENT",
        chat_id="123456",
        status="DELIVERED",
        payload={
            "details": {"conversation_id": "5585083099", "company": "Alf Market"},
            "telegram_message_id": 777,
        },
    )
    # Insert a test benchmark delivery record
    db.record_telegram_delivery(
        delivery_key="test_conv_rag_1",
        notification_type="RECRUITER_REPLY_SENT",
        chat_id="123456",
        status="DELIVERED",
        payload={
            "details": {"conversation_id": "conv_rag_1", "company": "Axis Test"},
            "telegram_message_id": 888,
        },
    )

    deleted_tg_ids = []

    def mock_transport(action, payload):
        if "delete" in str(action) or "deleteMessage" in str(payload):
            deleted_tg_ids.append(payload.get("message_id"))
            return {"ok": True, "result": True}
        return {"ok": True}

    notifier = TelegramNotifier(bot_token="test_token", chat_id="123456", transport_fn=mock_transport)
    report = cleanup_telegram_test_records(notifier=notifier)

    assert report["cleaned_count"] == 1
    assert report["preserved_count"] == 1
    assert 888 in deleted_tg_ids
    assert 777 not in deleted_tg_ids

    # Check status in DB
    records = db.list_telegram_delivery_records()
    real_rec = [r for r in records if r["delivery_key"] == "reply_5585083099_abc123"][0]
    test_rec = [r for r in records if r["delivery_key"] == "test_conv_rag_1"][0]
    assert real_rec["status"] == "DELIVERED"
    assert test_rec["status"] == "CLEANED_TEST_RECORD"


# ---------------------------------------------------------------------------
# Test 13: Cleanup is safe and idempotent upon multiple runs
# ---------------------------------------------------------------------------
def test_cleanup_is_idempotent():
    db.record_telegram_delivery(
        delivery_key="conv_failed_1",
        notification_type="RECRUITER_REPLY_SENT",
        chat_id="123456",
        status="DELIVERED",
        payload={"details": {"conversation_id": "conv_failed_1"}},
    )

    notifier = TelegramNotifier(bot_token="test_tok", chat_id="123456", transport_fn=lambda a, p: {"ok": True})
    report1 = cleanup_telegram_test_records(notifier=notifier)
    assert report1["cleaned_count"] == 1

    report2 = cleanup_telegram_test_records(notifier=notifier)
    assert report2["cleaned_count"] == 1


# ---------------------------------------------------------------------------
# Test 14: Confirmed notification layout matches Stage 63 contract
# ---------------------------------------------------------------------------
def test_confirmed_notification_layout_matches_stage63():
    msg = TelegramNotifier.format_recruiter_reply(
        company="Alf Market",
        vacancy="AI Engineer",
        incoming_message="What is your location?",
        sent_reply="Located in Thailand (UTC+7).",
        conversation_id="5585083099",
        hh_chat_url="https://hh.ru/chat/5585083099",
    )
    expected_lines = [
        "RECRUITER REPLY",
        "Компания: Alf Market",
        "Вакансия: AI Engineer",
        "Рекрутер:\nWhat is your location?",
        "Мой ответ:\nLocated in Thailand (UTC+7).",
        "Открыть чат HH:\nhttps://hh.ru/chat/5585083099",
        "Статус:\nОтвет отправлен и подтверждён в HH.",
    ]
    for line in expected_lines:
        assert line in msg


# ---------------------------------------------------------------------------
# Test 15: Generated-not-sent notification layout matches Stage 63 contract
# ---------------------------------------------------------------------------
def test_generated_not_sent_notification_layout_matches_stage63():
    msg = TelegramNotifier.format_reply_generated_not_sent(
        company="Draft Corp",
        vacancy="Backend Engineer",
        incoming_message="Any questions?",
        generated_reply="Ready for interview.",
        conversation_id="5585426482",
        hh_chat_url="https://hh.ru/chat/5585426482",
    )
    expected_lines = [
        "REPLY GENERATED — NOT SENT",
        "Компания: Draft Corp",
        "Вакансия: Backend Engineer",
        "Рекрутер:\nAny questions?",
        "Черновик:\nReady for interview.",
        "HH: NOT CONFIRMED",
        "OPEN HH CHAT:\nhttps://hh.ru/chat/5585426482",
        "Что делать:\nREQUIRES REVIEW",
    ]
    for line in expected_lines:
        assert line in msg
