"""Unit tests for Autonomous Kwork Proposal Submitter."""

import sqlite3
from unittest.mock import MagicMock
import pytest

from ai_assistant import kwork_autonomous_submit as kas


def test_is_it_relevant_project():
    assert kas.is_it_relevant_project("Разработка Telegram-бота на Python") is True
    assert kas.is_it_relevant_project("Парсер цен с сайта на requests/playwright") is True
    assert kas.is_it_relevant_project("Настройка интеграции CRM Битрикс24") is True
    assert kas.is_it_relevant_project("Создание AI агента для ответов клиентам") is True

    # Exclusions
    assert kas.is_it_relevant_project("Дизайн карточек товара в Figma") is False
    assert kas.is_it_relevant_project("Монтаж видео для TikTok и Reels") is False
    assert kas.is_it_relevant_project("Рерайт и копирайтинг статей для блога") is False


def test_is_kwork_already_applied():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE top_gig_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gig_id TEXT,
            source TEXT,
            title TEXT,
            budget_text TEXT,
            contact TEXT,
            deeplink TEXT,
            url TEXT,
            proposal_pitch TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE gigs (
            id TEXT PRIMARY KEY,
            source TEXT,
            source_id TEXT,
            url TEXT,
            title TEXT,
            description TEXT,
            category TEXT,
            budget_min REAL,
            budget_max REAL,
            currency TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    assert kas.is_kwork_already_applied(conn, "12345") is False

    kas.record_kwork_proposal_submission(
        conn=conn,
        project_id="12345",
        title="Тестовый проект",
        pitch="Готов выполнить",
        price=1000,
        duration_days=2,
        url="https://kwork.ru/projects/12345/view",
    )

    assert kas.is_kwork_already_applied(conn, "12345") is True
    conn.close()


def test_send_kwork_submission_telegram_alert(monkeypatch):
    sent_msgs = []
    monkeypatch.setattr(
        "ai_assistant.telegram_routing.send_job_message",
        lambda text, **k: sent_msgs.append(text) or True,
    )

    ok = kas.send_kwork_submission_telegram_alert(
        project_id="99901",
        title="Разработка парсера",
        price=1500,
        duration_days=2,
        url="https://kwork.ru/projects/99901/view",
        pitch="Здравствуйте! Готов написать парсер на Python с экспортом в Excel.",
    )

    assert ok is True
    assert len(sent_msgs) == 1
    assert "Kwork: Автопилот отправил предложение на заказ!" in sent_msgs[0]
    assert "1500 ₽" in sent_msgs[0]
    assert "99901" in sent_msgs[0]


def test_submit_kwork_proposal_cdp_no_browser(monkeypatch):
    monkeypatch.setattr(kas, "get_or_open_kwork_tab", lambda cdp_url: None)
    res = kas.submit_kwork_proposal_cdp(
        project_id="111",
        title="Test",
        pitch="Test pitch",
    )
    assert res["ok"] is False
    assert res["status"] == "NO_BROWSER"


def test_submit_kwork_proposal_cdp_closed_project(monkeypatch):
    mock_tab = {"id": "mock_tab_id"}
    monkeypatch.setattr(kas, "get_or_open_kwork_tab", lambda cdp_url: mock_tab)

    def mock_eval(script):
        if "check_status_script" in script or "is_closed" in script:
            return '{"is_logged_in": true, "is_closed": true, "has_form": false}'
        return "{}"

    monkeypatch.setattr(kas, "make_target_evaluate", lambda url, tid: mock_eval)

    res = kas.submit_kwork_proposal_cdp(
        project_id="111",
        title="Test",
        pitch="Test pitch",
    )
    assert res["ok"] is False
    assert res["status"] == "PROJECT_CLOSED"
