"""Tests for autonomous freelance & gig autopilot cycle runner."""
from __future__ import annotations

import sqlite3
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from ai_assistant.gig.models import BudgetType, CanDeliver, Gig, GigStatus
from scripts.run_gig_autopilot_cycle import (
    acquire_lock,
    format_gig_card,
    is_gig_already_delivered,
    record_gig_delivery,
    release_lock,
    run_gig_cycle,
)


def _make_sample_gig(
    gig_id: str = "gig_sample1",
    source: str = "tg_freelance_orders",
    title: str = "Разработать Telegram-бота на Python aiogram",
    desc: str = "Требуется создать Telegram-бота на Python (aiogram) для обработки заявок. Контакт: @client_tg",
    can_deliver: CanDeliver = CanDeliver.HIGH,
    status: GigStatus = GigStatus.NEW,
    budget_min: float = 10000.0,
    budget_max: float = 20000.0,
    url: str = "https://t.me/freelance_orders/100",
) -> Gig:
    return Gig(
        id=gig_id,
        source=source,
        source_id="100",
        url=url,
        title=title,
        description=desc,
        skills=["python", "telegram", "aiogram"],
        budget_min=budget_min,
        budget_max=budget_max,
        currency="RUB",
        budget_type=BudgetType.FIXED,
        can_deliver=can_deliver,
        status=status,
        opportunity_score=75.0,
        source_facts={"is_telegram": source.startswith("tg_"), "contact": "@client_tg"},
    )


def test_format_gig_card_telegram_deeplink():
    gig = _make_sample_gig()
    pitch = "Здравствуйте! Готов выполнить разработку бота. Опыт aiogram 3."
    text, reply_markup = format_gig_card(gig, pitch=pitch, fm_score=68.0)

    assert "💼 <b>Быстрый заказ — Telegram" in text
    assert html_safe(gig.title) in text
    assert "<code>" in text
    assert "Опыт aiogram 3." in text

    buttons = reply_markup.get("inline_keyboard", [])
    assert len(buttons) >= 1
    # Check 1-tap deeplink button
    deeplink_btn = buttons[0][0]
    assert "🚀 Откликнуться в 1 клик (@client_tg)" in deeplink_btn["text"]
    assert "https://t.me/client_tg?text=" in deeplink_btn["url"]
    assert urllib.parse.quote(pitch) in deeplink_btn["url"]

    # Check post URL button
    assert any(b["text"] == "📄 Открыть пост с заказом" for row in buttons for b in row)


def test_format_gig_card_kwork_buttons():
    gig = _make_sample_gig(
        gig_id="gig_kwork_123",
        source="kwork",
        title="Скрипт парсинга интернет-магазина",
        desc="Нужно написать парсер товаров на Python.",
        url="https://kwork.ru/projects/3333333",
    )
    gig.source_facts = {}
    pitch = "Здравствуйте! Имею подтвержденный опыт в парсинге данных."
    text, reply_markup = format_gig_card(gig, pitch=pitch, fm_score=72.0)

    assert "💼 <b>Быстрый заказ — Биржа Kwork</b>" in text
    buttons = reply_markup.get("inline_keyboard", [])
    assert len(buttons) == 2

    # Row 1: Actionable buttons
    assert buttons[0][0]["text"] == "👍 В работу"
    assert buttons[0][0]["callback_data"] == f"gig:INT:{gig.id}"
    assert buttons[0][1]["text"] == "⏭ Пропустить"
    assert buttons[0][1]["callback_data"] == f"gig:SKP:{gig.id}"

    # Row 2: Exchange link
    assert buttons[1][0]["text"] == "🔗 Открыть заказ на бирже"
    assert buttons[1][0]["url"] == gig.url


def test_is_gig_already_delivered_and_record():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE telegram_delivery_records (
            delivery_key TEXT PRIMARY KEY,
            notification_type TEXT,
            chat_id TEXT,
            delivered_at TEXT,
            status TEXT,
            payload TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE gigs (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            status TEXT,
            updated_at TEXT
        )
    """)
    gig = _make_sample_gig(gig_id="gig_test_42")
    conn.execute("INSERT INTO gigs (id, source, title, status) VALUES (?, ?, ?, 'NEW')",
                 (gig.id, gig.source, gig.title))
    conn.commit()

    # Before delivery
    assert is_gig_already_delivered(conn, gig.id) is False

    # Record delivery
    record_gig_delivery(conn, gig, chat_id="392046103")

    # After delivery
    assert is_gig_already_delivered(conn, gig.id) is True

    # Gig status moved to SHORTLISTED
    row = conn.execute("SELECT status FROM gigs WHERE id = ?", (gig.id,)).fetchone()
    assert row[0] == "SHORTLISTED"


def test_lock_acquire_and_release(tmp_path, monkeypatch):
    test_lock = tmp_path / "test_gig_autopilot.lock"
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.LOCK_FILE", test_lock)

    # First acquisition
    assert acquire_lock() is True
    assert test_lock.exists()

    # Re-acquisition while current process holds it
    assert acquire_lock() is False

    # Release
    release_lock()
    assert not test_lock.exists()


def test_run_gig_cycle_dry_run_and_filtering(monkeypatch):
    """Test full cycle execution in dry-run mode with mocked sources."""
    gigs_to_return = [
        # Viable high-score gig
        {
            "source": "kwork",
            "source_id": "111",
            "title": "Telegram-бот на aiogram с базой данных",
            "description": "Нужен Telegram бот на Python aiogram 3 и PostgreSQL. Развернуть на сервере Linux в Docker.",
            "budget_min": 15000,
            "budget_max": 25000,
            "currency": "RUB",
            "url": "https://kwork.ru/projects/111",
            "source_mode": "live",
            "source_status": "OPEN",
        },
        # Low deliverability gig (should be filtered out)
        {
            "source": "kwork",
            "source_id": "222",
            "title": "Дизайн логотипа в Figma",
            "description": "Нарисовать логотип и баннеры для соцсетей в Figma.",
            "budget_min": 3000,
            "budget_max": 5000,
            "currency": "RUB",
            "url": "https://kwork.ru/projects/222",
            "source_mode": "live",
            "source_status": "OPEN",
        },
    ]

    mock_source = MagicMock()
    mock_source.key = "mock_source"
    mock_source.fetch.return_value = iter(gigs_to_return)

    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.TelegramGigSource", lambda **kw: mock_source)
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.KworkGigSource", lambda **kw: mock_source)
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.FreelancerGigSource", lambda **kw: mock_source)

    summary = run_gig_cycle(max_deliveries=3, min_score=40.0, dry_run=True)
    assert summary["candidates"] >= 1
    assert summary["delivered"] >= 1


def html_safe(s: str) -> str:
    import html
    return html.escape(s)


def test_pitch_incorporates_confirmed_kb_facts():
    from ai_assistant.gig_pitch_generator import generate_gig_pitch

    # Test Telegram / aiogram task
    tg_pitch = generate_gig_pitch("Создать Telegram-бота", "Нужен бот на Python aiogram с очередями задач")
    assert any(w in tg_pitch.lower() for w in ("aiogram", "telegram", "fsm", "бот", "развертывание"))

    # Test Scraping / parser task
    scr_pitch = generate_gig_pitch("Парсер каталога", "Собрать данные каталога в CSV")
    assert any(w in scr_pitch.lower() for w in ("пайплайн", "парсинг", "playwright", "выгрузка", "etl"))

    # Test Bitrix / CRM task
    crm_pitch = generate_gig_pitch("Интеграция Битрикс24", "Настроить роботов и вебхуки в CRM")
    assert any(w in crm_pitch.lower() for w in ("битрикс", "бизнес-процесс", "робот", "crm", "вебхук"))

    # Test Linux / DevOps task
    srv_pitch = generate_gig_pitch("Настройка VPS Linux", "Поднять WireGuard VPN и Docker на Ubuntu")
    assert any(w in srv_pitch.lower() for w in ("linux", "docker", "vpn", "nginx", "инфраструктур"))


def test_telegram_routing_owner_only(monkeypatch):
    """Verify delivery goes strictly to TELEGRAM_OWNER_ID and never to the public channel."""
    from ai_assistant import config

    sent_chat_ids = []

    def mock_send_telegram_card(chat_id, text, reply_markup=None, bot_token=None):
        sent_chat_ids.append(str(chat_id))
        return True

    mock_gig = {
        "source": "kwork",
        "source_id": "test_routing_999",
        "title": "Telegram-бот на Python aiogram",
        "description": "Разработка бота под ключ.",
        "budget_min": 15000,
        "budget_max": 20000,
        "currency": "RUB",
        "url": "https://kwork.ru/projects/test_routing_999",
        "source_mode": "live",
        "source_status": "OPEN",
    }

    mock_source = MagicMock()
    mock_source.key = "mock"
    mock_source.fetch.return_value = iter([mock_gig])

    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.TelegramGigSource", lambda **kw: mock_source)
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.KworkGigSource", lambda **kw: mock_source)
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.FreelancerGigSource", lambda **kw: mock_source)
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.send_telegram_card", mock_send_telegram_card)
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.is_gig_already_delivered", lambda conn, gid: False)
    monkeypatch.setattr("scripts.run_gig_autopilot_cycle.record_gig_delivery", lambda conn, gig, chat_id: None)

    summary = run_gig_cycle(max_deliveries=1, min_score=40.0, dry_run=False)
    assert summary["delivered"] == 1
    assert len(sent_chat_ids) == 1

    owner_id = str(config.TELEGRAM_OWNER_ID or "392046103")
    channel_id = str(config.TELEGRAM_CHANNEL_ID or "")

    # MUST be owner chat ID
    assert sent_chat_ids[0] == owner_id
    # MUST NOT be the public channel, including the removed hardcoded id.
    if channel_id:
        assert sent_chat_ids[0] != channel_id
    assert sent_chat_ids[0] != "-1004399255305"



def test_kwork_stopped_and_oversubscribed_filtering():
    """Verify that stopped projects and projects with >50 offers are marked CLOSED and rejected."""
    from ai_assistant.gig.sources.kwork import KworkGigSource

    # 1. Stopped project
    html_stop = ('<html><body>{"wantData":{"id":12345,"status":"user_stop",'
                 '"name":"Scrape data Python","description":"Python scraping task.",'
                 '"kwork_count":10,"isWantActive":false}}</body></html>')
    rec_stop = KworkGigSource.record_from_html(html_stop, page_url="https://kwork.ru/projects/12345", fetched_at="now")
    assert rec_stop is not None
    assert rec_stop["source_status"] == "CLOSED"
    assert "user_stop" in rec_stop["source_facts"]["kwork_closed_reason"]

    # 2. Oversubscribed project (>50 offers)
    html_over = ('<html><body>{"wantData":{"id":54321,"status":"active",'
                 '"name":"Build Telegram bot","description":"Python bot task.",'
                 '"kwork_count":65,"isWantActive":true}}</body></html>')
    rec_over = KworkGigSource.record_from_html(html_over, page_url="https://kwork.ru/projects/54321", fetched_at="now")
    assert rec_over is not None
    assert rec_over["source_status"] == "CLOSED"
    assert ">50" in rec_over["source_facts"]["kwork_closed_reason"]

    # 3. Active open project (<50 offers)
    html_open = ('<html><body>{"wantData":{"id":77777,"status":"active",'
                 '"name":"Bitrix24 integration","description":"Python bitrix task.",'
                 '"kwork_count":15,"isWantActive":true}}</body></html>')
    rec_open = KworkGigSource.record_from_html(html_open, page_url="https://kwork.ru/projects/77777", fetched_at="now")
    assert rec_open is not None
    assert rec_open["source_status"] == "OPEN"
    assert rec_open["source_facts"]["kwork_closed_reason"] is None
