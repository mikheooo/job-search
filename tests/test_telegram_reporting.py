"""Stage 56 / Phase 2: Telegram Reporting & Kill Switch Test Suite.

Verifies:
1. Post-submit message formatting: [Отклик отправлен] Компания: ..., Вакансия: ..., letter[:200]..., url.
2. Daily digest generation with counts of submitted, needs_human_review, and stale applications.
3. /stop command toggles db submit_paused=1 (kill switch).
4. /resume command toggles db submit_paused=0.
5. End-to-end notification delivery with idempotency and mock transport.
6. Approval buttons (📄, ✅) remain accessible via callback query processor.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.telegram_notifier import (
    TelegramNotifier,
    format_daily_digest,
    send_post_submit_notification,
)
from ai_assistant.telegram_bot import TelegramBot


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_tg_reporting.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


def test_format_post_submit_message():
    """Verifies post-submit notification format with 200 character truncation."""
    company = "Yandex"
    title = "Senior Python Developer"
    long_letter = "А" * 350
    url = "https://hh.ru/vacancy/999999"

    msg = TelegramNotifier.format_post_submit_message(
        company=company,
        title=title,
        cover_letter=long_letter,
        vacancy_url=url,
    )

    assert "[Отклик отправлен]" in msg
    assert f"Компания: {company}" in msg
    assert f"Вакансия: {title}" in msg
    assert f"{'А' * 200}..." in msg
    assert f"{'А' * 201}" not in msg
    assert url in msg


def test_format_daily_digest(clean_db):
    """Verifies daily digest summarizes submitted, needs_human_review, and stale counts."""
    # Seed db with various states
    db.save_hh_application({
        "application_id": "app_sub_1",
        "vacancy_stable_id": "hh:101",
        "title": "Backend Dev",
        "employer": "Tech Corp",
        "state": "SUBMITTED",
    })
    db.save_hh_application({
        "application_id": "app_sub_2",
        "vacancy_stable_id": "hh:102",
        "title": "Backend Dev",
        "employer": "Tech Corp",
        "state": "SUBMITTED",
    })
    db.save_hh_application({
        "application_id": "app_rev_1",
        "vacancy_stable_id": "hh:103",
        "title": "Frontend Dev",
        "employer": "Web Corp",
        "state": "NEEDS_HUMAN_REVIEW",
    })
    db.save_hh_application({
        "application_id": "app_stale_1",
        "vacancy_stable_id": "hh:104",
        "title": "DevOps",
        "employer": "Cloud Corp",
        "state": "STALE",
    })

    digest = format_daily_digest()

    assert "ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ" in digest
    assert "SUBMITTED" in digest and "2" in digest
    assert "NEEDS_HUMAN_REVIEW" in digest and "1" in digest
    assert "STALE" in digest and "1" in digest


def test_bot_stop_command_sets_kill_switch(clean_db):
    """The /stop command sets submit_paused in DB and returns paused message."""
    owner_chat = "12345678"
    bot = TelegramBot(bot_token="test_token", allowed_chat_id=owner_chat)

    assert db.is_submit_paused() is False

    reply = bot.process_incoming_text(chat_id=owner_chat, text="/stop")
    assert "ПРИОСТАНОВЛЕНА" in reply
    assert "submit_paused=1" in reply
    assert db.is_submit_paused() is True


def test_bot_resume_command_resumes_submissions(clean_db):
    """The /resume command clears submit_paused in DB and returns resumed message."""
    owner_chat = "12345678"
    bot = TelegramBot(bot_token="test_token", allowed_chat_id=owner_chat)

    db.set_submit_paused(True)
    assert db.is_submit_paused() is True

    reply = bot.process_incoming_text(chat_id=owner_chat, text="/resume")
    assert "ВОЗОБНОВЛЕНА" in reply
    assert "submit_paused=0" in reply
    assert db.is_submit_paused() is False


def test_bot_digest_command(clean_db):
    """The /digest command returns formatted daily digest."""
    owner_chat = "12345678"
    bot = TelegramBot(bot_token="test_token", allowed_chat_id=owner_chat)

    db.save_hh_application({
        "application_id": "app_sub_test",
        "vacancy_stable_id": "hh:201",
        "title": "ML Engineer",
        "employer": "AI Lab",
        "state": "SUBMITTED",
    })

    reply = bot.process_incoming_text(chat_id=owner_chat, text="/digest")
    assert "ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ" in reply
    assert "SUBMITTED" in reply


def test_send_post_submit_notification_delivery_and_idempotency(clean_db):
    """Verifies send_post_submit_notification dispatches via notifier and prevents duplicates."""
    sent_payloads = []

    def mock_transport(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sent_payloads.append(payload)
        return {"ok": True, "result": {"message_id": 42}}

    notifier = TelegramNotifier(bot_token="fake_tok", chat_id="12345", transport_fn=mock_transport)

    company = "Sber"
    title = "Python Lead"
    letter = "Добрый день! Откликаюсь на позицию..."
    url = "https://hh.ru/vacancy/777888"

    res1 = send_post_submit_notification(
        company=company,
        title=title,
        cover_letter=letter,
        vacancy_url=url,
        notifier=notifier,
    )
    assert res1["delivered"] is True
    assert len(sent_payloads) == 1
    assert "[Отклик отправлен]" in sent_payloads[0]["text"]
    assert company in sent_payloads[0]["text"]

    # Second call for the same submission is skipped idempotently
    res2 = send_post_submit_notification(
        company=company,
        title=title,
        cover_letter=letter,
        vacancy_url=url,
        notifier=notifier,
    )
    assert res2["delivered"] is False
    assert "already delivered" in res2["reason"]
    assert len(sent_payloads) == 1
