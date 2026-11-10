"""Unit tests for Kwork Inbox Monitor & Buyer Inquiry Tracker.

Covers all 10 mandatory cases:
1. New incoming message from buyer.
2. Duplicate of already processed message (idempotency/dedup).
3. Multiple new messages from the same buyer.
4. Own outgoing message (saved as IGNORED, no alert sent).
5. Authorization loss with cooldown (no spamming).
6. Telegram unavailability (network timeout / retry later from queue).
7. Recovery after restart (persistence in SQLite across instances).
8. Multiple orders/conversations simultaneously (no data bleed).
9. Missing job-bot configuration (stored in queue, no fallback).
10. Main Hermes token rejection (guaranteed isolation from main Hermes).
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from ai_assistant import telegram_routing
from ai_assistant.gig.kwork_inbox_monitor import (
    KworkInboxStorage,
    KworkMessage,
    KworkScanResult,
    dispatch_kwork_alert,
    handle_auth_lost,
    parse_inbox_payload,
    process_monitor_cycle,
)


@pytest.fixture
def temp_storage(tmp_path: pathlib.Path) -> KworkInboxStorage:
    db_file = tmp_path / "test_kwork_inbox.db"
    return KworkInboxStorage(db_file)


# ---------------------------------------------------------------------------
# 1. New incoming message from buyer
# ---------------------------------------------------------------------------
def test_1_new_incoming_message(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    mock_sender.return_value = telegram_routing.JobSendResult(
        ok=True, status="delivered", message_id=8801
    )

    msg = KworkMessage(
        message_key="kwork:diag_101:msg_1",
        dialogue_id="diag_101",
        project_id="3268401",
        project_title="Виджеты в сделку Битрикс24",
        buyer_name="Алексей",
        message_id="msg_1",
        text="Здравствуйте! Когда сможете приступить?",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/alexey",
        detected_at="2026-10-21T18:00:00Z",
        delivery_status="PENDING",
    )

    def mock_scan():
        return KworkScanResult(
            is_authenticated=True,
            messages=[msg],
            active_dialogues_count=1,
            unread_count=1,
        )

    report = process_monitor_cycle(temp_storage, mock_scan, sender_fn=mock_sender)

    assert report.new_detected == 1
    assert report.alerts_sent == 1
    assert report.alerts_failed == 0

    # Storage verification
    stored = temp_storage.get_message("kwork:diag_101:msg_1")
    assert stored is not None
    assert stored.delivery_status == "DELIVERED"
    assert stored.telegram_message_id == 8801

    # Telegram payload verification
    mock_sender.assert_called_once()
    sent_text = mock_sender.call_args[0][0]
    assert "KWORK: Новое сообщение от заказчика!" in sent_text
    assert "#3268401" in sent_text
    assert "Виджеты в сделку Битрикс24" in sent_text
    assert "Алексей" in sent_text
    assert "Когда сможете приступить?" in sent_text
    assert "Требуется ответ исполнителя!" in sent_text
    assert "https://kwork.ru/inbox/alexey" in sent_text


# ---------------------------------------------------------------------------
# 2. Duplicate of already processed message (Persistent Dedup)
# ---------------------------------------------------------------------------
def test_2_duplicate_message_dedup(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    mock_sender.return_value = telegram_routing.JobSendResult(ok=True, status="delivered", message_id=8802)

    msg = KworkMessage(
        message_key="kwork:diag_101:msg_1",
        dialogue_id="diag_101",
        project_id="3268401",
        project_title="Виджеты в сделку",
        buyer_name="Алексей",
        message_id="msg_1",
        text="Повторное обнаружение сообщения",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/alexey",
        detected_at="2026-10-21T18:00:00Z",
        delivery_status="PENDING",
    )

    # Cycle 1: Delivered
    process_monitor_cycle(temp_storage, lambda: KworkScanResult(True, "", [msg], 1, 1), sender_fn=mock_sender)
    assert mock_sender.call_count == 1

    # Cycle 2: Same message arrives
    report2 = process_monitor_cycle(temp_storage, lambda: KworkScanResult(True, "", [msg], 1, 1), sender_fn=mock_sender)
    assert report2.new_detected == 0
    assert report2.alerts_sent == 0
    # Must NOT call Telegram sender again
    assert mock_sender.call_count == 1


# ---------------------------------------------------------------------------
# 3. Multiple new messages from the same buyer
# ---------------------------------------------------------------------------
def test_3_multiple_messages_same_buyer(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    mock_sender.return_value = telegram_routing.JobSendResult(ok=True, status="delivered", message_id=8803)

    messages = [
        KworkMessage(
            message_key=f"kwork:ivan_chat:msg_{i}",
            dialogue_id="ivan_chat",
            project_id="3268367",
            project_title="AI-бот барбершоп",
            buyer_name="Иван",
            message_id=f"msg_{i}",
            text=f"Фраза номер {i}",
            is_outgoing=False,
            chat_url="https://kwork.ru/inbox/ivan",
            detected_at="2026-10-21T18:05:00Z",
            delivery_status="PENDING",
        )
        for i in range(1, 4)
    ]

    report = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(True, "", messages, 1, 3),
        sender_fn=mock_sender,
    )

    assert report.new_detected == 3
    assert report.alerts_sent == 3
    assert mock_sender.call_count == 3
    for m in messages:
        stored = temp_storage.get_message(m.message_key)
        assert stored.delivery_status == "DELIVERED"


# ---------------------------------------------------------------------------
# 4. Own outgoing message (saved as IGNORED, no alert sent)
# ---------------------------------------------------------------------------
def test_4_own_outgoing_message_no_alert(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()

    msg_out = KworkMessage(
        message_key="kwork:diag_202:msg_my_1",
        dialogue_id="diag_202",
        project_id="3268401",
        project_title="Битрикс24",
        buyer_name="Клиент",
        message_id="msg_my_1",
        text="Вы: Добрый день, сейчас протестирую и пришлю отчёт.",
        is_outgoing=True,  # Sent by us!
        chat_url="https://kwork.ru/inbox/client",
        detected_at="2026-10-21T18:10:00Z",
        delivery_status="IGNORED",
    )

    report = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(True, "", [msg_out], 1, 0),
        sender_fn=mock_sender,
    )

    assert report.new_detected == 1
    assert report.alerts_sent == 0
    mock_sender.assert_not_called()

    # Stored as IGNORED
    stored = temp_storage.get_message("kwork:diag_202:msg_my_1")
    assert stored is not None
    assert stored.delivery_status == "IGNORED"


# ---------------------------------------------------------------------------
# 5. Authorization loss with 2-strike confirmation and cooldown (no spamming)
# ---------------------------------------------------------------------------
def test_5_authorization_loss_with_cooldown(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    mock_sender.return_value = telegram_routing.JobSendResult(ok=True, status="delivered", message_id=8805)

    def unauth_scan():
        return KworkScanResult(is_authenticated=False, auth_status="AUTH_REQUIRED", auth_error="Session expired")

    # Cycle 1: First check -> strike 1/2, no alert yet
    report1 = process_monitor_cycle(temp_storage, unauth_scan, sender_fn=mock_sender)
    assert report1.auth_status == "AUTH_REQUIRED"
    assert mock_sender.call_count == 0

    # Cycle 2: Second check -> confirmed loss (strike 2/2), sends warning
    report2 = process_monitor_cycle(temp_storage, unauth_scan, sender_fn=mock_sender)
    assert report2.auth_status == "AUTH_LOST"
    assert mock_sender.call_count == 1
    assert "KWORK: Требуется авторизация!" in mock_sender.call_args[0][0]

    # Cycle 3: Immediate next cycle -> auth warning suppressed due to cooldown!
    report3 = process_monitor_cycle(temp_storage, unauth_scan, sender_fn=mock_sender)
    assert report3.auth_status == "AUTH_LOST"
    # No duplicate spam!
    assert mock_sender.call_count == 1


# ---------------------------------------------------------------------------
# 6. Telegram unavailability (network timeout / retry later)
# ---------------------------------------------------------------------------
def test_6_telegram_unavailable_and_retry(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    # First attempt fails (e.g. timeout)
    mock_sender.return_value = telegram_routing.JobSendResult(
        ok=False, status="failed", detail="Connection timed out"
    )

    msg = KworkMessage(
        message_key="kwork:diag_303:msg_fail",
        dialogue_id="diag_303",
        project_id="3268401",
        project_title="Тест",
        buyer_name="Заказчик",
        message_id="msg_fail",
        text="Важное сообщение",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/buyer",
        detected_at="2026-10-21T18:15:00Z",
        delivery_status="PENDING",
    )

    # Cycle 1: Telegram down
    report1 = process_monitor_cycle(
        temp_storage, lambda: KworkScanResult(True, "", [msg], 1, 1), sender_fn=mock_sender
    )
    assert report1.alerts_sent == 0
    assert report1.alerts_failed == 1

    # Message must NOT be marked DELIVERED, but saved in backlog
    stored = temp_storage.get_message("kwork:diag_303:msg_fail")
    assert stored.delivery_status == "FAILED"
    pending = temp_storage.get_pending_messages()
    assert len(pending) == 1
    assert pending[0].message_key == "kwork:diag_303:msg_fail"

    # Cycle 2: Telegram connection restored!
    mock_sender.return_value = telegram_routing.JobSendResult(
        ok=True, status="delivered", message_id=8806
    )
    report2 = process_monitor_cycle(
        temp_storage, lambda: KworkScanResult(True, "", [], 1, 0), sender_fn=mock_sender
    )
    assert report2.alerts_sent == 1

    stored_after = temp_storage.get_message("kwork:diag_303:msg_fail")
    assert stored_after.delivery_status == "DELIVERED"
    assert len(temp_storage.get_pending_messages()) == 0


# ---------------------------------------------------------------------------
# 7. Recovery after restart (persistence across storage instances)
# ---------------------------------------------------------------------------
def test_7_recovery_after_restart(tmp_path: pathlib.Path):
    db_file = tmp_path / "persistent_kwork.db"

    # Instance 1: Saves a pending message and process halts
    storage1 = KworkInboxStorage(db_file)
    msg = KworkMessage(
        message_key="kwork:restart_diag:msg_1",
        dialogue_id="restart_diag",
        project_id="3268367",
        project_title="Голосовой бот",
        buyer_name="Дмитрий",
        message_id="msg_1",
        text="Ждём демо",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/dmitry",
        detected_at="2026-10-21T18:20:00Z",
        delivery_status="PENDING",
    )
    storage1.save_message(msg)
    del storage1

    # Instance 2: Brand new instance pointing to same file
    storage2 = KworkInboxStorage(db_file)
    pending = storage2.get_pending_messages()
    assert len(pending) == 1
    assert pending[0].message_key == "kwork:restart_diag:msg_1"

    # Deliver via cycle
    mock_sender = MagicMock(return_value=telegram_routing.JobSendResult(True, "delivered", message_id=8807))
    report = process_monitor_cycle(storage2, lambda: KworkScanResult(True, "", [], 0, 0), sender_fn=mock_sender)
    assert report.alerts_sent == 1

    assert storage2.get_message("kwork:restart_diag:msg_1").delivery_status == "DELIVERED"


# ---------------------------------------------------------------------------
# 8. Multiple orders/conversations simultaneously
# ---------------------------------------------------------------------------
def test_8_multiple_orders_simultaneously(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock(return_value=telegram_routing.JobSendResult(True, "delivered", message_id=8808))

    msg_order1 = KworkMessage(
        message_key="kwork:user_bitrix:m1",
        dialogue_id="user_bitrix",
        project_id="3268401",
        project_title="Виджеты Битрикс24",
        buyer_name="Сергей (Битрикс)",
        message_id="m1",
        text="Готовы протестировать на нашем портале.",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/user_bitrix",
        detected_at="2026-10-21T18:25:00Z",
        delivery_status="PENDING",
    )
    msg_order2 = KworkMessage(
        message_key="kwork:user_barber:m2",
        dialogue_id="user_barber",
        project_id="3268367",
        project_title="AI-бот барбершоп",
        buyer_name="Михаил (Барбершоп)",
        message_id="m2",
        text="Пришлите команду для /test_call.",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/user_barber",
        detected_at="2026-10-21T18:25:01Z",
        delivery_status="PENDING",
    )

    report = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(True, "", [msg_order1, msg_order2], 2, 2),
        sender_fn=mock_sender,
    )

    assert report.new_detected == 2
    assert report.alerts_sent == 2
    assert mock_sender.call_count == 2

    # Check alert contents are isolated and accurate
    calls = [call[0][0] for call in mock_sender.call_args_list]
    call_bitrix = next(c for c in calls if "3268401" in c)
    call_barber = next(c for c in calls if "3268367" in c)

    assert "Сергей (Битрикс)" in call_bitrix
    assert "user_bitrix" in call_bitrix
    assert "Михаил (Барбершоп)" in call_barber
    assert "user_barber" in call_barber


# ---------------------------------------------------------------------------
# 9. Missing job-bot configuration (stored in queue, no fallback)
# ---------------------------------------------------------------------------
def test_9_unconfigured_job_bot_no_fallback(temp_storage: KworkInboxStorage):
    # Sender indicates unconfigured (e.g. HH_TELEGRAM_BOT_TOKEN missing)
    mock_sender = MagicMock(
        return_value=telegram_routing.JobSendResult(
            ok=False,
            status="unconfigured",
            detail="Job Search Telegram bot token is not configured",
        )
    )

    msg = KworkMessage(
        message_key="kwork:unconf_diag:m1",
        dialogue_id="unconf_diag",
        project_id="3268401",
        project_title="Проект",
        buyer_name="Покупатель",
        message_id="m1",
        text="Срочный вопрос",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/buyer",
        detected_at="2026-10-21T18:30:00Z",
        delivery_status="PENDING",
    )

    report = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(True, "", [msg], 1, 1),
        sender_fn=mock_sender,
    )

    assert report.alerts_sent == 0
    assert report.alerts_failed == 1

    stored = temp_storage.get_message("kwork:unconf_diag:m1")
    assert stored.delivery_status == "FAILED"
    assert "unconfigured" in stored.delivery_error

    # Message remains safely in pending backlog for when config is provided!
    pending = temp_storage.get_pending_messages()
    assert len(pending) == 1
    assert pending[0].message_key == "kwork:unconf_diag:m1"


# ---------------------------------------------------------------------------
# 10. Rejection of Main Hermes token (strict isolation)
# ---------------------------------------------------------------------------
def test_10_no_send_to_main_hermes(temp_storage: KworkInboxStorage, monkeypatch):
    hermes_token = "123456:FAKE_HERMES_TOKEN"
    monkeypatch.setattr(telegram_routing, "main_hermes_tokens", lambda: {hermes_token})

    # Test route pointing to forbidden Hermes token
    bad_route = telegram_routing.JobRoute(token=hermes_token, chat_id="112233")

    msg = KworkMessage(
        message_key="kwork:hermes_block:m1",
        dialogue_id="hermes_block",
        project_id="3268401",
        project_title="Проект",
        buyer_name="Покупатель",
        message_id="m1",
        text="Сообщение не должно попасть в Hermes",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/buyer",
        detected_at="2026-10-21T18:35:00Z",
        delivery_status="PENDING",
    )

    temp_storage.save_message(msg)

    # Call dispatch_kwork_alert with real telegram_routing.send_job_message_result
    # passing forbidden token in route -> must fail closed with unconfigured status!
    def custom_send(text, parse_mode="HTML"):
        return telegram_routing.send_job_message_result(text, parse_mode=parse_mode, route=bad_route)

    ok = dispatch_kwork_alert(msg, temp_storage, sender_fn=custom_send)
    assert ok is False

    stored = temp_storage.get_message("kwork:hermes_block:m1")
    assert stored.delivery_status == "FAILED"
    assert stored.delivered_at is None
    # Still safely preserved in backlog!
    assert len(temp_storage.get_pending_messages()) == 1


# ---------------------------------------------------------------------------
# 11. HTML escaping in alerts (security & parse mode safety)
# ---------------------------------------------------------------------------
def test_11_html_escaping(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock(return_value=telegram_routing.JobSendResult(True, "delivered", message_id=8811))

    msg = KworkMessage(
        message_key="kwork:html_test:m1",
        dialogue_id="html_test",
        project_id="3268401",
        project_title="Разработка <виджетов> & 'ботов'",
        buyer_name='ООО "Рога & Копыта"',
        message_id="m1",
        text='Привет <script>alert("hack")</script> & 5 > 3 & 2 < 4',
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/test?a=1&b=2",
        detected_at="2026-10-21T18:40:00Z",
        delivery_status="PENDING",
    )

    process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(True, "", [msg], 1, 1),
        sender_fn=mock_sender,
    )

    sent_text = mock_sender.call_args[0][0]
    # Check that dangerous HTML characters are fully escaped
    assert "<script>" not in sent_text
    assert "&lt;script&gt;" in sent_text
    assert "&lt;виджетов&gt;" in sent_text
    assert "&amp;" in sent_text
    assert 'ООО &quot;Рога &amp; Копыта&quot;' in sent_text
    assert "https://kwork.ru/inbox/test?a=1&amp;b=2" in sent_text


# ---------------------------------------------------------------------------
# 12. AMBIGUOUS state is recorded and NOT automatically retried
# ---------------------------------------------------------------------------
def test_12_ambiguous_state_not_retried_automatically(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock(
        return_value=telegram_routing.JobSendResult(
            ok=False,
            status="ambiguous",
            detail="HTTP 504 Gateway Timeout: telegram may have received request",
        )
    )

    msg = KworkMessage(
        message_key="kwork:ambig_test:m1",
        dialogue_id="ambig_test",
        project_id="3268401",
        project_title="Проект",
        buyer_name="Клиент",
        message_id="m1",
        text="Тестовое сообщение с неоднозначной доставкой",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/client",
        detected_at="2026-10-21T18:45:00Z",
        delivery_status="PENDING",
    )

    # Cycle 1: Ambiguous failure
    report1 = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(True, "", [msg], 1, 1),
        sender_fn=mock_sender,
    )
    assert report1.alerts_sent == 0
    assert report1.alerts_failed == 1

    stored = temp_storage.get_message("kwork:ambig_test:m1")
    assert stored.delivery_status == "AMBIGUOUS"
    assert "ambiguous" in stored.delivery_error

    # Cycle 2: Ambiguous message MUST NOT be in get_pending_messages()
    assert len(temp_storage.get_pending_messages()) == 0

    report2 = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(True, "", [], 0, 0),
        sender_fn=mock_sender,
    )
    # Must NOT retry ambiguous message (prevents duplicate spam on server-side write)
    assert report2.alerts_sent == 0
    assert report2.alerts_failed == 0
    assert mock_sender.call_count == 1  # Still 1, no second call!


# ---------------------------------------------------------------------------
# 13. AMBIGUOUS diagnostic report and safe manual reconciliation
# ---------------------------------------------------------------------------
def test_13_ambiguous_diagnostic_report_and_manual_reconciliation(temp_storage: KworkInboxStorage):
    from ai_assistant.gig.kwork_inbox_monitor import (
        get_ambiguous_diagnostic_report,
        reconcile_ambiguous_message,
    )

    msg = KworkMessage(
        message_key="kwork:reconcile_test:m1",
        dialogue_id="reconcile_test",
        project_id="3268401",
        project_title="Битрикс24",
        buyer_name="Клиент",
        message_id="m1",
        text="Срочное сообщение",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/client",
        detected_at="2026-10-21T18:50:00Z",
        delivery_status="AMBIGUOUS",
        delivery_error="HTTP 504 Timeout",
    )
    temp_storage.save_message(msg)

    # 1. Diagnostic report generates structured summary
    report = get_ambiguous_diagnostic_report(temp_storage)
    assert report["ambiguous_count"] == 1
    assert report["needs_operator_review"] is True
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["message_key"] == "kwork:reconcile_test:m1"
    assert "recommended_action" in item

    # 2. Operator reconciles: MARK_DELIVERED
    ok = reconcile_ambiguous_message(temp_storage, "kwork:reconcile_test:m1", "MARK_DELIVERED")
    assert ok is True

    stored = temp_storage.get_message("kwork:reconcile_test:m1")
    assert stored.delivery_status == "DELIVERED"
    assert "MARK_DELIVERED" in stored.delivery_error

    # 3. Test RETRY_DELIVERY resolution
    # Reset back to AMBIGUOUS
    temp_storage.mark_ambiguous("kwork:reconcile_test:m1", "Ambiguous retry test")
    ok2 = reconcile_ambiguous_message(temp_storage, "kwork:reconcile_test:m1", "RETRY_DELIVERY")
    assert ok2 is True
    stored2 = temp_storage.get_message("kwork:reconcile_test:m1")
    assert stored2.delivery_status == "PENDING"
    assert len(temp_storage.get_pending_messages()) == 1

    # 4. Invalid resolution raises ValueError
    temp_storage.mark_ambiguous("kwork:reconcile_test:m1", "Ambiguous error")
    with pytest.raises(ValueError):
        reconcile_ambiguous_message(temp_storage, "kwork:reconcile_test:m1", "INVALID_ACTION")


# ---------------------------------------------------------------------------
# 14. Multiple messages chain indicator
# ---------------------------------------------------------------------------
def test_14_multiple_messages_chain_indicator():
    from ai_assistant.gig.kwork_inbox_monitor import format_kwork_alert

    msg_multi = KworkMessage(
        message_key="kwork:multi_chain:m3",
        dialogue_id="multi_chain",
        project_id="3268367",
        project_title="Барбершоп бот",
        buyer_name="Александр",
        message_id="m3",
        text="Посмотрите прикрепленные файлы в ТЗ",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/alex",
        detected_at="2026-10-21T18:55:00Z",
        delivery_status="PENDING",
        unread_count=4,  # 4 unread messages!
    )

    alert_text = format_kwork_alert(msg_multi)
    assert "(всего новых: 4)" in alert_text
    assert "Посмотрите прикрепленные файлы в ТЗ" in alert_text


# ---------------------------------------------------------------------------
# 15. Dry-run + CDP unavailable -> 0 Telegram POST, returns CDP_UNAVAILABLE
# ---------------------------------------------------------------------------
def test_15_dry_run_cdp_unavailable_zero_telegram_post(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()

    def cdp_down_scan():
        return KworkScanResult(
            is_authenticated=False,
            auth_status="CDP_UNAVAILABLE",
            auth_error="Connection refused (127.0.0.1:9111)",
        )

    report = process_monitor_cycle(temp_storage, cdp_down_scan, sender_fn=mock_sender, dry_run=True)
    assert report.auth_status == "CDP_UNAVAILABLE"
    assert report.alerts_sent == 0
    mock_sender.assert_not_called()


# ---------------------------------------------------------------------------
# 16. Dry-run + confirmed auth loss -> 0 Telegram POST
# ---------------------------------------------------------------------------
def test_16_dry_run_auth_lost_zero_telegram_post(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()

    def login_page_scan():
        return KworkScanResult(
            is_authenticated=False,
            auth_status="AUTH_REQUIRED",
            auth_error="Confirmed login page rendered (/login)",
        )

    # 3 cycles in dry-run mode
    for _ in range(3):
        report = process_monitor_cycle(temp_storage, login_page_scan, sender_fn=mock_sender, dry_run=True)
        assert report.alerts_sent == 0

    # Zero Telegram calls across all cycles
    mock_sender.assert_not_called()


# ---------------------------------------------------------------------------
# 17. Single page glitch -> 0 auth-warning
# ---------------------------------------------------------------------------
def test_17_single_page_glitch_zero_auth_warning(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()

    # Part A: PAGE_NOT_READY (transient DOM loading / blank page)
    def page_glitch_scan():
        return KworkScanResult(
            is_authenticated=False,
            auth_status="PAGE_NOT_READY",
            auth_error="DOM readyState interactive",
        )

    report_glitch = process_monitor_cycle(temp_storage, page_glitch_scan, sender_fn=mock_sender, dry_run=False)
    assert report_glitch.auth_status == "PAGE_NOT_READY"
    mock_sender.assert_not_called()

    # Part B: Single login page flash (strike 1)
    def single_flash_scan():
        return KworkScanResult(
            is_authenticated=False,
            auth_status="AUTH_REQUIRED",
            auth_error="Single login page flash",
        )

    report_single = process_monitor_cycle(temp_storage, single_flash_scan, sender_fn=mock_sender, dry_run=False)
    assert report_single.auth_status == "AUTH_REQUIRED"
    mock_sender.assert_not_called()


# ---------------------------------------------------------------------------
# 18. Two confirmed login checks -> exactly 1 warning in live mode
# ---------------------------------------------------------------------------
def test_18_two_confirmed_login_checks_sends_one_live_warning(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    mock_sender.return_value = telegram_routing.JobSendResult(ok=True, status="delivered", message_id=8818)

    def login_scan():
        return KworkScanResult(
            is_authenticated=False,
            auth_status="AUTH_REQUIRED",
            auth_error="Confirmed login page rendered (/login)",
        )

    # Check 1: Strike 1 -> 0 warning
    rep1 = process_monitor_cycle(temp_storage, login_scan, sender_fn=mock_sender, dry_run=False)
    assert rep1.auth_status == "AUTH_REQUIRED"
    assert mock_sender.call_count == 0

    # Check 2: Strike 2 -> exactly 1 warning sent!
    rep2 = process_monitor_cycle(temp_storage, login_scan, sender_fn=mock_sender, dry_run=False)
    assert rep2.auth_status == "AUTH_LOST"
    assert mock_sender.call_count == 1
    assert "KWORK: Требуется авторизация!" in mock_sender.call_args[0][0]


# ---------------------------------------------------------------------------
# 19. Auth recovery -> error state reset (and live notification)
# ---------------------------------------------------------------------------
def test_19_auth_recovery_resets_error_state(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    mock_sender.return_value = telegram_routing.JobSendResult(ok=True, status="delivered", message_id=8819)

    # Pre-condition: Put storage in warned state
    temp_storage.set_state("consecutive_login_page_count", "2")
    temp_storage.set_state("last_auth_warning_at", "2026-10-21T18:00:00Z")

    def ok_scan():
        return KworkScanResult(is_authenticated=True, auth_status="OK", messages=[])

    # Live recovery
    rep = process_monitor_cycle(temp_storage, ok_scan, sender_fn=mock_sender, dry_run=False)
    assert rep.auth_status == "OK"
    assert temp_storage.get_state("consecutive_login_page_count") == "0"
    assert temp_storage.get_state("last_auth_warning_at") == ""
    assert mock_sender.call_count == 1
    assert "авторизация подтверждена" in mock_sender.call_args[0][0]

    # Pre-condition for dry-run recovery
    temp_storage.set_state("consecutive_login_page_count", "1")
    temp_storage.set_state("last_auth_warning_at", "2026-10-21T18:10:00Z")

    # Dry-run recovery: clears state, 0 Telegram POST
    rep_dry = process_monitor_cycle(temp_storage, ok_scan, sender_fn=mock_sender, dry_run=True)
    assert rep_dry.auth_status == "OK"
    assert temp_storage.get_state("consecutive_login_page_count") == "0"
    assert temp_storage.get_state("last_auth_warning_at") == ""
    # Call count still 1 (no new POST in dry run)
    assert mock_sender.call_count == 1


# ---------------------------------------------------------------------------
# 20. Dry-run saves incoming messages as PENDING, never DELIVERED
# ---------------------------------------------------------------------------
def test_20_dry_run_saves_messages_as_pending_never_delivered(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()

    msg = KworkMessage(
        message_key="kwork:buyer_99:m1",
        dialogue_id="buyer_99",
        project_id="3268401",
        project_title="Битрикс24",
        buyer_name="Сергей",
        message_id="m1",
        text="Добрый вечер, предложение актуально?",
        is_outgoing=False,
        chat_url="https://kwork.ru/inbox/sergey",
        detected_at="2026-10-21T19:00:00Z",
        delivery_status="PENDING",
    )

    rep = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(is_authenticated=True, messages=[msg], active_dialogues_count=1, unread_count=1),
        sender_fn=mock_sender,
        dry_run=True,
    )

    assert rep.new_detected == 1
    assert rep.alerts_sent == 0
    assert rep.alerts_pending == 1
    mock_sender.assert_not_called()

    stored = temp_storage.get_message("kwork:buyer_99:m1")
    assert stored is not None
    assert stored.delivery_status == "PENDING"
    assert stored.delivered_at is None
    assert stored.telegram_message_id is None


# ---------------------------------------------------------------------------
# 21. Send-one limit enforced in live mode
# ---------------------------------------------------------------------------
def test_21_send_one_limit_enforced(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()
    mock_sender.return_value = telegram_routing.JobSendResult(ok=True, status="delivered", message_id=8821)

    msgs = [
        KworkMessage(
            message_key=f"kwork:limit_test:m{i}",
            dialogue_id="limit_test",
            project_id="3268367",
            project_title="AI-бот",
            buyer_name="Клиент",
            message_id=f"m{i}",
            text=f"Сообщение {i}",
            is_outgoing=False,
            chat_url="https://kwork.ru/inbox/limit",
            detected_at="2026-10-21T19:05:00Z",
            delivery_status="PENDING",
        )
        for i in (1, 2)
    ]

    rep = process_monitor_cycle(
        temp_storage,
        lambda: KworkScanResult(is_authenticated=True, messages=msgs, active_dialogues_count=1, unread_count=2),
        sender_fn=mock_sender,
        dry_run=False,
        send_one=True,
    )

    assert rep.new_detected == 2
    assert rep.alerts_sent == 1
    assert rep.alerts_pending == 1
    assert mock_sender.call_count == 1

    stored1 = temp_storage.get_message("kwork:limit_test:m1")
    stored2 = temp_storage.get_message("kwork:limit_test:m2")
    assert stored1.delivery_status == "DELIVERED"
    assert stored2.delivery_status == "PENDING"


# ---------------------------------------------------------------------------
# 22. Bare unauthenticated failure without login evidence -> UNKNOWN, no alert
# ---------------------------------------------------------------------------
def test_22_unknown_auth_state_zero_alert(temp_storage: KworkInboxStorage):
    mock_sender = MagicMock()

    def bare_failure_scan():
        return KworkScanResult(is_authenticated=False, auth_error="evaluate failed")

    assert bare_failure_scan().auth_status == "UNKNOWN"

    for _ in range(3):
        report = process_monitor_cycle(
            temp_storage,
            bare_failure_scan,
            sender_fn=mock_sender,
            dry_run=False,
        )
        assert report.auth_status == "UNKNOWN"
        assert report.alerts_sent == 0

    assert temp_storage.get_state("consecutive_login_page_count") is None
    assert temp_storage.get_state("last_auth_warning_at") is None
    mock_sender.assert_not_called()


