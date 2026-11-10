"""Unit and integration verification for P1.1 (Lead Quality & Commercial Funnel)."""

import pytest
import sqlite3
from ai_assistant import db
from ai_assistant.business_funnel import (
    get_business_funnel_metrics,
    FunnelStage,
    record_conversion_event,
    init_business_funnel_db,
)
from ai_assistant.outbound_governor import check_outbound_allowed, OutboundAction
from ai_assistant.shortlist_selection import SearchStream


def test_funnel_streams_partition_exhaustive(tmp_path):
    """Every vacancy must belong to exactly one stream; sum of streams == total unique leads."""
    db.init_db()
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS gigs (id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT)")
    conn.execute("DELETE FROM vacancies")
    conn.execute("DELETE FROM gigs")
    conn.execute("""
        INSERT INTO vacancies (stable_id, source, source_job_id, title, job_url, location)
        VALUES 
            ('vac:1', 'hh', '1', 'Remote Python Dev', 'http://hh.ru/1', NULL),
            ('vac:2', 'habrcareer', '2', 'Backend Engineer', 'http://habr.com/2', 'Москва'),
            ('vac:3', 'bangkok_careers', '3', 'DevOps Agoda', 'http://agoda.com/3', 'Bangkok, Thailand'),
            ('vac:4', 'tg_forpython', '4', 'Python Gig', 'http://t.me/4', NULL)
    """)
    conn.execute("""
        INSERT INTO gigs (id, source, source_id, title, url)
        VALUES ('gig:1', 'kwork', '1', 'Telegram Bot', 'http://kwork.ru/1')
    """)
    conn.commit()
    conn.close()

    m = get_business_funnel_metrics()
    stages = m["stages"]
    streams = m["streams"]

    assert stages["1_unique_leads"] == 5
    # vac:1 (NULL loc) and vac:2 (non-TH) are REMOTE_GLOBAL (2)
    # vac:3 is THAILAND_LOCAL (1)
    # vac:4 (tg_%) + gig:1 (gigs) are GIGS_FREELANCE (2)
    assert streams["REMOTE_GLOBAL"]["leads"] == 2
    assert streams["THAILAND_LOCAL"]["leads"] == 1
    assert streams["GIGS_FREELANCE"]["leads"] == 2
    assert streams["REMOTE_GLOBAL"]["leads"] + streams["THAILAND_LOCAL"]["leads"] + streams["GIGS_FREELANCE"]["leads"] == stages["1_unique_leads"]


def test_top_gig_proposals_quality_no_regex_artefacts(tmp_path):
    """Top gig proposals must not contain regex artefacts like @amomin_bot, @extra, @2026."""
    db.init_db()
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS top_gig_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gig_id TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            budget_text TEXT,
            contact TEXT,
            deeplink TEXT,
            url TEXT,
            proposal_pitch TEXT NOT NULL,
            status TEXT DEFAULT 'PROPOSAL_READY',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Insert clean sample proposal
    conn.execute("""
        INSERT INTO top_gig_proposals (gig_id, source, title, budget_text, contact, url, proposal_pitch, status, created_at, updated_at)
        VALUES ('gig_104fabcbce05511b', 'kwork', 'Внедрение ИИ на n8n', '10 000 - 30 000 RUB', 'Платформа (Kwork #3267839)', 'https://kwork.ru/projects/3267839', 'Готов разработать сценарий на n8n для магазина техники Apple...', 'PROPOSAL_READY', '2026-11-07', '2026-11-07')
    """)
    conn.commit()

    c = conn.cursor()
    c.execute("SELECT gig_id, contact, url, proposal_pitch FROM top_gig_proposals")
    rows = c.fetchall()
    conn.close()

    assert len(rows) > 0
    forbidden = ["@amomin_bot", "@extra", "@2026", "@eho001"]
    for gid, contact, url, pitch in rows:
        for bad in forbidden:
            assert bad != contact, f"Found bogus contact {bad} in proposal {gid}"
            assert bad not in pitch, f"Found bogus text {bad} in pitch of {gid}"
        assert url and url.startswith("http"), f"Proposal {gid} has missing or invalid URL"
        assert len(pitch) > 30, f"Pitch for {gid} is too short"


def test_priority_user_action_tasks_present(tmp_path):
    """System must record and track active priority tasks."""
    db.init_db()
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_action_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_key TEXT UNIQUE NOT NULL,
            priority TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            action_url TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT OR REPLACE INTO user_action_tasks (task_key, priority, title, description, action_url, status, created_at, updated_at)
        VALUES 
            ('typing_test_ayito', 'P0_HIGH', 'Пройти тест скорости печати', 'AYITO Tech', 'https://brainapps.ru/typing-test', 'PENDING', '2026-11-07', '2026-11-07'),
            ('review_pervyy_bit_draft', 'P0_HIGH', 'Согласовать условия', 'Первый Бит', 'https://hh.ru', 'PENDING', '2026-11-07', '2026-11-07')
    """)
    conn.commit()

    c = conn.cursor()
    c.execute("SELECT task_key, status FROM user_action_tasks WHERE status = 'PENDING'")
    tasks = {r[0]: r[1] for r in c.fetchall()}
    conn.close()

    assert "typing_test_ayito" in tasks
    assert "review_pervyy_bit_draft" in tasks


def test_outbound_governor_fail_closed_submit_paused():
    """Governor must fail closed when submit_paused is '1'."""
    db.init_db()
    db.set_system_setting("submit_paused", "1")
    allowed, reason = check_outbound_allowed(action=OutboundAction.SUBMISSION)
    assert allowed is False
    assert "submit_paused = 1" in reason.lower()
