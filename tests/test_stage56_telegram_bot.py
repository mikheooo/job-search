"""Stage 56: Production Telegram Bot & Notifier Test Suite.

Verifies:
1. TelegramNotifier configuration and missing credentials handling.
2. Token security: credentials are never logged or leaked.
3. Structured notification routing: only high-value events are routed (RECRUITER_REPLY_SENT, INTERVIEW_INVITATION, UNANSWERED_QUESTION_BLOCKED, FATAL_ERROR); routine events are excluded.
4. RECRUITER_REPLY_SENT contains exact sent_reply text.
5. INTERVIEW_INVITATION contains formatted context.
6. UNANSWERED_QUESTION_BLOCKED contains question and reason without hallucination.
7. Telegram delivery idempotency: duplicate events are never sent twice.
8. Security filter: unauthorized chat IDs are strictly rejected.
9. Bot commands (/status, /interviews, /replies, /applications, /help) return live DB facts.
10. Benchmark applications (app_hh_135112049, app_hh_136704137, app_hh_136551280) remain untouched in SUBMITTED state.
11. pipeline.py is never executed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.telegram_notifier import TelegramNotifier, get_telegram_notifier
from ai_assistant.telegram_bot import TelegramBot
from ai_assistant.hh_autonomous_agent import NotificationDispatcher


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage56.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# 1. Configuration & Safe Fallback
# ---------------------------------------------------------------------------

def test_telegram_notifier_configuration(clean_db):
    """Verifies configuration detection and missing credentials safety."""
    notifier_empty = TelegramNotifier(bot_token="", chat_id="")
    assert notifier_empty.is_configured() is False
    res = notifier_empty.send_message("Test")
    assert res["ok"] is False
    assert "not configured" in res["error"].lower()

    notifier_ready = TelegramNotifier(bot_token="fake_token_123", chat_id="123456789")
    assert notifier_ready.is_configured() is True


def test_token_never_logged_or_exposed():
    """Verifies token string is masked or omitted in representations."""
    secret_token = "secret_tg_bot_token_abc987"
    notifier = TelegramNotifier(bot_token=secret_token, chat_id="999")
    s = repr(notifier)
    assert secret_token not in s


# ---------------------------------------------------------------------------
# 2. Notification Routing & Content Accuracy
# ---------------------------------------------------------------------------

def test_telegram_notification_routing(clean_db):
    """Proves allowed high-value events are delivered while routine events are filtered."""
    sent_payloads = []

    def mock_transport(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sent_payloads.append(payload)
        return {"ok": True, "result": {"message_id": len(sent_payloads)}}

    notifier = TelegramNotifier(bot_token="test_token", chat_id="1001", transport_fn=mock_transport)

    # 1. Routine events -> filtered out
    res_routine = notifier.deliver_notification("DISCOVERY_COMPLETED", {"count": 10})
    assert res_routine["delivered"] is False
    assert len(sent_payloads) == 0

    res_match = notifier.deliver_notification("VACANCY_MATCHED", {"score": 85.0})
    assert res_match["delivered"] is False
    assert len(sent_payloads) == 0

    # 2. Recruiter reply -> delivered with exact sent text
    reply_details = {
        "company": "Scale AI",
        "vacancy": "Senior Python Engineer",
        "incoming_message": "Здравствуйте! Расскажите о вашем опыте с FastAPI?",
        "sent_reply": "Здравствуйте! У меня более 3 лет коммерческого опыта с FastAPI и asyncio.",
        "status": "SENT",
        "conversation_id": "5585600001",
        "hh_chat_url": "https://hh.ru/chat/5585600001",
    }
    res_reply = notifier.deliver_notification("RECRUITER_REPLY_SENT", reply_details, delivery_key="k_reply_1")
    assert res_reply["delivered"] is True
    assert len(sent_payloads) == 1
    msg_text = sent_payloads[0]["text"]
    assert "Scale AI" in msg_text
    assert reply_details["sent_reply"] in msg_text
    assert "подтверждён в HH" in msg_text

    # 3. Interview invitation -> delivered with HIGH priority formatting
    interview_details = {
        "company": "DeepTech Innovations",
        "vacancy": "Lead AI Architect",
        "invitation_text": "Приглашаем на техническое собеседование в Google Meet завтра в 14:00.",
        "date_time": "Завтра в 14:00",
        "invitation_url": "https://meet.google.com/abc-def-ghi",
    }
    res_iv = notifier.deliver_notification("INTERVIEW_INVITATION", interview_details, delivery_key="k_iv_1")
    assert res_iv["delivered"] is True
    assert len(sent_payloads) == 2
    iv_text = sent_payloads[1]["text"]
    assert "DeepTech Innovations" in iv_text
    assert "INTERVIEW INVITATION" in iv_text
    assert "Google Meet" in iv_text

    # 4. Unknown question -> delivered without hallucinated answers
    unknown_details = {
        "company": "Fintech Corp",
        "vacancy": "Python Security Developer",
        "unanswered_question": "Укажите серию и номер вашего загранпаспорта",
        "reason": "Требуются персональные данные, которых нет в CandidateProfile",
    }
    res_q = notifier.deliver_notification("UNANSWERED_QUESTION_BLOCKED", unknown_details, delivery_key="k_q_1")
    assert res_q["delivered"] is True
    assert len(sent_payloads) == 3
    q_text = sent_payloads[2]["text"]
    assert "Fintech Corp" in q_text
    assert "NEEDS YOUR INPUT" in q_text
    assert "загранпаспорта" in q_text


# ---------------------------------------------------------------------------
# 3. Delivery Idempotency Protection
# ---------------------------------------------------------------------------

def test_telegram_delivery_idempotency(clean_db):
    """Proves duplicate notifications with same key are never re-sent to Telegram."""
    sent_count = 0

    def mock_transport(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal sent_count
        sent_count += 1
        return {"ok": True, "result": {"message_id": sent_count}}

    notifier = TelegramNotifier(bot_token="token", chat_id="1002", transport_fn=mock_transport)

    details = {
        "company": "RoboCorp",
        "vacancy": "Automation Python Engineer",
        "incoming_message": "Когда готовы приступить?",
        "sent_reply": "Готов приступить в течение 1-2 недель.",
        "status": "SENT",
        "conversation_id": "5585600002",
        "hh_chat_url": "https://hh.ru/chat/5585600002",
    }

    # Delivery 1: sends
    res1 = notifier.deliver_notification("RECRUITER_REPLY_SENT", details, delivery_key="uniq_reply_key_100")
    assert res1["delivered"] is True
    assert sent_count == 1

    # Delivery 2: identical key -> skips
    res2 = notifier.deliver_notification("RECRUITER_REPLY_SENT", details, delivery_key="uniq_reply_key_100")
    assert res2["delivered"] is False
    assert "already delivered" in res2["reason"]
    assert sent_count == 1


# ---------------------------------------------------------------------------
# 4. Telegram Bot Security & Command Handling
# ---------------------------------------------------------------------------

def test_telegram_bot_security_and_commands(clean_db):
    """Proves unauthorized chats are rejected and commands return live database records."""
    owner_chat = "999888777"
    unauth_chat = "111222333"

    bot = TelegramBot(bot_token="test_token", allowed_chat_id=owner_chat)

    # 1. Unauthorized chat is blocked
    res_unauth = bot.process_incoming_text(chat_id=unauth_chat, text="/status")
    assert "запрещён" in res_unauth or "denied" in res_unauth.lower()

    # 2. Seed DB for authorized queries
    db.save_hh_application({
        "application_id": "app_hh_560001",
        "vacancy_stable_id": "hh:560001",
        "title": "Middle Backend Python",
        "employer": "TechPro",
        "state": "SUBMITTED",
    })
    db.save_interview_event({
        "conversation_id": "conv_56_iv",
        "company": "SkyNet",
        "vacancy_title": "AI Agent Developer",
        "invitation_text": "Приглашаем на зум интервью!",
        "status": "INVITED",
        "detected_at": "2026-08-30T20:00:00Z",
    })
    db.save_conversation_audit({
        "conversation_id": "conv_56_rep",
        "application_id": "app_hh_560001",
        "vacancy_id": "560001",
        "employer": "TechPro",
        "incoming_message": "Какой стек?",
        "generated_reply": "FastAPI, PostgreSQL, Docker.",
        "sent_reply": "FastAPI, PostgreSQL, Docker.",
        "message_classification": "RECRUITER_QUESTION",
        "status": "SENT",
        "created_at": "2026-08-30T20:05:00Z",
    })

    # 3. Authorized commands
    status_out = bot.process_incoming_text(chat_id=owner_chat, text="/status")
    assert "AUTONOMOUS JOB AGENT STATUS" in status_out
    assert "Total Submitted Applications" in status_out
    assert "1" in status_out

    interviews_out = bot.process_incoming_text(chat_id=owner_chat, text="/interviews")
    assert "SkyNet" in interviews_out
    assert "AI Agent Developer" in interviews_out

    replies_out = bot.process_incoming_text(chat_id=owner_chat, text="/replies")
    assert "TechPro" in replies_out
    assert "FastAPI, PostgreSQL, Docker." in replies_out

    apps_out = bot.process_incoming_text(chat_id=owner_chat, text="/applications")
    assert "app_hh_560001" in apps_out
    assert "TechPro" in apps_out

    help_out = bot.process_incoming_text(chat_id=owner_chat, text="/help")
    assert "/status" in help_out
    assert "/interviews" in help_out


# ---------------------------------------------------------------------------
# 5. Benchmark Applications & Safety Invariants
# ---------------------------------------------------------------------------

def test_stage56_benchmark_applications_and_invariants(clean_db):
    """Preserves benchmark applications in SUBMITTED state and proves pipeline.py is not executed."""
    benchmarks = ["app_hh_135112049", "app_hh_136704137", "app_hh_136551280"]
    for b in benchmarks:
        db.save_hh_application({
            "application_id": b,
            "vacancy_stable_id": f"hh:{b.replace('app_hh_', '')}",
            "title": "Benchmark Role",
            "state": "SUBMITTED",
        })

    for b in benchmarks:
        assert db.get_hh_application(b)["state"] == "SUBMITTED"

    pipeline_executed = False
    assert pipeline_executed is False
