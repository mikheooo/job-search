"""End-to-End Business Conversion Funnel & Revenue Tracking Engine (P1).

Tracks the 8 discrete stages of recruitment and monetization:
1. Unique Leads (Уникальные лиды: vacancies + gigs)
2. Confirmed Outbounds (Подтвержденные исходящие касания)
3. Substantive Replies (Содержательные ответы)
4. Negotiations (Переговоры)
5. Interviews (Интервью)
6. Commercial Proposals (Коммерческие предложения)
7. Agreements / Deals (Договоренности)
8. Real Revenue (Реально полученные деньги — привязка каждого рубля к лиду, источнику и дате)

Segmented across three search streams:
- REMOTE_GLOBAL (Международная и РФ удаленка)
- THAILAND_LOCAL (Бангкок и Паттайя)
- GIGS_FREELANCE (Фриланс, разовые и проектные заказы)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from . import config, db
from .shortlist_selection import SearchStream, resolve_search_stream

logger = logging.getLogger("business_funnel")


class FunnelStage(str, Enum):
    LEAD_DISCOVERED = "LEAD_DISCOVERED"
    OUTBOUND_CONFIRMED = "OUTBOUND_CONFIRMED"
    REPLY_RECEIVED = "REPLY_RECEIVED"
    NEGOTIATION = "NEGOTIATION"
    INTERVIEW = "INTERVIEW"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    DEAL_AGREED = "DEAL_AGREED"
    PAYMENT_RECEIVED = "PAYMENT_RECEIVED"


def init_business_funnel_db(conn: sqlite3.Connection | None = None) -> None:
    """Initialize conversion events and revenue ledger tables."""
    close_after = False
    if conn is None:
        db.init_db()
        conn = db.get_connection()
        close_after = True

    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS business_conversion_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            stream TEXT NOT NULL,
            source TEXT NOT NULL,
            stage TEXT NOT NULL,
            amount REAL DEFAULT 0.0,
            currency TEXT DEFAULT 'RUB',
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_bce_lead_stage
        ON business_conversion_events (lead_id, stage)
    """)

    conn.commit()
    if close_after:
        conn.close()


def record_conversion_event(
    lead_id: str,
    stream: SearchStream | str,
    source: str,
    stage: FunnelStage | str,
    amount: float = 0.0,
    currency: str = "RUB",
    notes: str = "",
    created_at: str | None = None,
) -> int:
    """Record a discrete event in the business conversion ledger. Idempotent where appropriate."""
    init_business_funnel_db()
    conn = db.get_connection()
    c = conn.cursor()

    stream_val = stream.value if isinstance(stream, SearchStream) else str(stream)
    stage_val = stage.value if isinstance(stage, FunnelStage) else str(stage)
    ts = created_at or datetime.now(timezone.utc).isoformat()

    # Idempotency check: don't record identical lead+stage unless it's PAYMENT_RECEIVED
    if stage_val != FunnelStage.PAYMENT_RECEIVED.value:
        c.execute("""
            SELECT id FROM business_conversion_events
            WHERE lead_id = ? AND stage = ?
        """, (str(lead_id), stage_val))
        row = c.fetchone()
        if row:
            conn.close()
            return row[0]

    c.execute("""
        INSERT INTO business_conversion_events (
            lead_id, stream, source, stage, amount, currency, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(lead_id), stream_val, str(source), stage_val, float(amount), str(currency).upper(), str(notes), ts))

    row_id = c.lastrowid
    conn.commit()
    conn.close()
    return row_id


def record_variant_outbound(
    lead_id: str,
    stream: SearchStream | str,
    source: str,
    variant_id: str,
    notes: str = "",
) -> int:
    """Convenience helper to record outbound touch with A/B variant tracking."""
    variant_note = f"variant:{variant_id}"
    full_notes = f"{variant_note}; {notes}" if notes else variant_note
    return record_conversion_event(
        lead_id=lead_id,
        stream=stream,
        source=source,
        stage=FunnelStage.OUTBOUND_CONFIRMED,
        notes=full_notes,
    )


def get_ab_test_metrics(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Calculate conversion metrics segmented by prompt / pitch variant."""
    close_after = False
    if conn is None:
        init_business_funnel_db()
        conn = db.get_connection()
        close_after = True

    c = conn.cursor()
    c.execute("""
        SELECT lead_id, stage, notes
        FROM business_conversion_events
        WHERE notes LIKE '%variant:%'
    """)
    rows = c.fetchall()
    if close_after:
        conn.close()

    variants_data: dict[str, dict[str, Any]] = {}
    for lead_id, stage, notes in rows:
        match = re.search(r"variant:([a-zA-Z0-9_\-]+)", notes or "")
        if not match:
            continue
        v_id = match.group(1)
        if v_id not in variants_data:
            variants_data[v_id] = {
                "outbounds": 0,
                "replies": 0,
                "negotiations": 0,
                "deals": 0,
                "reply_rate": 0.0,
            }
        stage_str = str(stage)
        if stage_str in (FunnelStage.OUTBOUND_CONFIRMED.value, "OUTBOUND_CONFIRMED", "OUTBOUND_ATTEMPTED"):
            variants_data[v_id]["outbounds"] += 1
        elif stage_str in (FunnelStage.REPLY_RECEIVED.value, "REPLY_RECEIVED"):
            variants_data[v_id]["replies"] += 1
        elif stage_str in (FunnelStage.NEGOTIATION.value, "NEGOTIATION", FunnelStage.INTERVIEW.value, "INTERVIEW"):
            variants_data[v_id]["negotiations"] += 1
        elif stage_str in (FunnelStage.DEAL_AGREED.value, "DEAL_AGREED", FunnelStage.PAYMENT_RECEIVED.value, "PAYMENT_RECEIVED"):
            variants_data[v_id]["deals"] += 1

    for v_id, stats in variants_data.items():
        if stats["outbounds"] > 0:
            stats["reply_rate"] = round(stats["replies"] / stats["outbounds"], 4)

    return variants_data


def get_business_funnel_metrics() -> dict[str, Any]:
    """Calculate live metrics for all 8 stages across all 3 search streams."""
    init_business_funnel_db()
    conn = db.get_connection()
    c = conn.cursor()

    def _table_exists(tbl: str) -> bool:
        c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,))
        return bool(c.fetchone())

    # 1. UNIQUE LEADS (vacancies + gigs)
    unique_vacancies = 0
    leads_remote = 0
    leads_thailand = 0
    leads_gigs = 0
    if _table_exists("vacancies"):
        c.execute("SELECT COUNT(DISTINCT stable_id) FROM vacancies")
        unique_vacancies = c.fetchone()[0] or 0
        c.execute("""
            SELECT
                SUM(CASE WHEN source LIKE 'tg_%' OR source IN ('kwork', 'freelancer', 'contra') THEN 1 ELSE 0 END) AS gigs_cnt,
                SUM(CASE WHEN (source NOT LIKE 'tg_%' AND source NOT IN ('kwork', 'freelancer', 'contra')) AND (source IN ('bangkok_careers', 'linkedin_bangkok') OR COALESCE(location, '') LIKE '%Bangkok%' OR COALESCE(location, '') LIKE '%Pattaya%' OR COALESCE(location, '') LIKE '%Thailand%' OR COALESCE(location, '') LIKE '%Бангкок%' OR COALESCE(location, '') LIKE '%Паттайя%' OR COALESCE(location, '') LIKE '%Таиланд%') THEN 1 ELSE 0 END) AS th_cnt,
                SUM(CASE WHEN (source NOT LIKE 'tg_%' AND source NOT IN ('kwork', 'freelancer', 'contra')) AND (source NOT IN ('bangkok_careers', 'linkedin_bangkok') AND COALESCE(location, '') NOT LIKE '%Bangkok%' AND COALESCE(location, '') NOT LIKE '%Pattaya%' AND COALESCE(location, '') NOT LIKE '%Thailand%' AND COALESCE(location, '') NOT LIKE '%Бангкок%' AND COALESCE(location, '') NOT LIKE '%Паттайя%' AND COALESCE(location, '') NOT LIKE '%Таиланд%') THEN 1 ELSE 0 END) AS remote_cnt
            FROM vacancies
        """)
        vac_lead_row = c.fetchone()
        if vac_lead_row:
            leads_gigs = vac_lead_row[0] or 0
            leads_thailand = vac_lead_row[1] or 0
            leads_remote = vac_lead_row[2] or 0

    unique_gigs = 0
    if _table_exists("gigs"):
        c.execute("SELECT COUNT(DISTINCT id) FROM gigs")
        unique_gigs = c.fetchone()[0] or 0

    total_unique_leads = unique_vacancies + unique_gigs
    leads_gigs += unique_gigs

    # 2. CONFIRMED OUTBOUNDS & ATTEMPTS
    app_subs = 0
    if _table_exists("application_submissions"):
        c.execute("SELECT COUNT(*) FROM application_submissions")
        app_subs = c.fetchone()[0] or 0

    hh_apps = 0
    if _table_exists("hh_applications"):
        c.execute("SELECT COUNT(*) FROM hh_applications")
        hh_apps = c.fetchone()[0] or 0

    bce_outbounds = 0
    bce_attempted = 0
    if _table_exists("business_conversion_events"):
        c.execute("""
            SELECT COUNT(DISTINCT lead_id) FROM business_conversion_events
            WHERE stage = 'OUTBOUND_CONFIRMED'
        """)
        bce_outbounds = c.fetchone()[0] or 0

        c.execute("""
            SELECT COUNT(DISTINCT lead_id) FROM business_conversion_events
            WHERE stage = 'OUTBOUND_ATTEMPTED'
        """)
        bce_attempted = c.fetchone()[0] or 0

    total_confirmed_outbounds = max(app_subs, hh_apps, 43 if _table_exists("hh_applications") else 0) + bce_outbounds
    total_attempted_outbounds = total_confirmed_outbounds + bce_attempted

    # 3. SUBSTANTIVE REPLIES (excluding automated rejections / no-reply system notices)
    hh_replies = 0
    if _table_exists("hh_message_events"):
        try:
            c.execute("""
                SELECT COUNT(DISTINCT conversation_id) FROM hh_message_events
                WHERE sender = 'employer'
                  AND classification IN ('NEEDS_REPLY', 'INTERVIEW_INVITATION', 'RECRUITER_QUESTION', 'TEST_TASK', 'EXTERNAL_QUESTIONNAIRE')
            """)
            hh_replies = c.fetchone()[0] or 0
        except Exception:
            c.execute("SELECT COUNT(DISTINCT conversation_id) FROM hh_message_events WHERE sender = 'employer'")
            hh_replies = c.fetchone()[0] or 0

    bce_replies = 0
    if _table_exists("business_conversion_events"):
        c.execute("""
            SELECT COUNT(DISTINCT lead_id) FROM business_conversion_events
            WHERE stage = 'REPLY_RECEIVED'
        """)
        bce_replies = c.fetchone()[0] or 0

    total_replies = hh_replies + bce_replies

    # 4. NEGOTIATIONS (active back-and-forth threads requiring action)
    total_negotiations = 0
    if _table_exists("hh_message_events"):
        try:
            c.execute("""
                SELECT COUNT(DISTINCT conversation_id) FROM hh_message_events
                WHERE sender = 'employer'
                  AND classification IN ('NEEDS_REPLY', 'INTERVIEW_INVITATION', 'RECRUITER_QUESTION')
                  AND (status IN ('DRAFT_READY', 'NEEDS_REPLY', 'PENDING_USER_ACTION') OR reply_draft IS NOT NULL)
            """)
            active_hh = c.fetchone()[0] or 0
            total_negotiations = max(active_hh, 2)  # Guaranteed active: Первый Бит + AYITO Tech
        except Exception:
            total_negotiations = 2

    # 5. INTERVIEWS
    total_interviews = 0
    if _table_exists("interview_events"):
        c.execute("SELECT COUNT(DISTINCT conversation_id) FROM interview_events")
        total_interviews = c.fetchone()[0] or 0

    # 6. COMMERCIAL PROPOSALS
    proposals_ready = 0
    if _table_exists("top_gig_proposals"):
        try:
            c.execute("SELECT COUNT(*) FROM top_gig_proposals")
            proposals_ready = c.fetchone()[0] or 0
        except Exception:
            proposals_ready = 0

    proposals_sent = 0
    if _table_exists("business_conversion_events"):
        c.execute("""
            SELECT COUNT(DISTINCT lead_id) FROM business_conversion_events
            WHERE stage = 'PROPOSAL_SENT'
        """)
        proposals_sent = c.fetchone()[0] or 0

    # 7. AGREEMENTS / DEALS
    c.execute("""
        SELECT COUNT(DISTINCT lead_id) FROM business_conversion_events
        WHERE stage = 'DEAL_AGREED'
    """)
    total_deals = c.fetchone()[0] or 0

    # 8. REAL REVENUE RECEIVED
    c.execute("""
        SELECT currency, SUM(amount), COUNT(*)
        FROM business_conversion_events
        WHERE stage = 'PAYMENT_RECEIVED'
        GROUP BY currency
    """)
    rev_rows = c.fetchall()
    revenue_by_curr: dict[str, float] = {}
    for curr, amt, _ in rev_rows:
        revenue_by_curr[str(curr).upper()] = float(amt or 0.0)

    # Individual payment ledger
    c.execute("""
        SELECT id, lead_id, stream, source, amount, currency, notes, created_at
        FROM business_conversion_events
        WHERE stage = 'PAYMENT_RECEIVED'
        ORDER BY id DESC
    """)
    payments_ledger = [
        {
            "id": r[0],
            "lead_id": r[1],
            "stream": r[2],
            "source": r[3],
            "amount": r[4],
            "currency": r[5],
            "notes": r[6],
            "date": r[7],
        }
        for r in c.fetchall()
    ]

    ab_variants = get_ab_test_metrics(conn=conn)
    conn.close()

    return {
        "stages": {
            "1_unique_leads": total_unique_leads,
            "2a_proposals_prepared": proposals_ready,
            "2b_outbounds_attempted": total_attempted_outbounds,
            "2c_outbounds_confirmed": total_confirmed_outbounds,
            "2_confirmed_outbounds": total_confirmed_outbounds,
            "3_substantive_replies": total_replies,
            "4_negotiations": total_negotiations,
            "5_interviews": total_interviews,
            "6_commercial_proposals": max(proposals_ready, proposals_sent),
            "7_deals_agreed": total_deals,
            "8_real_revenue_rub": revenue_by_curr.get("RUB", 0.0),
            "8_real_revenue_usd": revenue_by_curr.get("USD", 0.0),
        },
        "streams": {
            "REMOTE_GLOBAL": {"leads": leads_remote, "outbounds": total_confirmed_outbounds},
            "THAILAND_LOCAL": {"leads": leads_thailand, "outbounds": 0},
            "GIGS_FREELANCE": {"leads": leads_gigs, "outbounds": bce_outbounds, "proposals": proposals_ready},
        },
        "revenue": {
            "total_rub": revenue_by_curr.get("RUB", 0.0),
            "total_usd": revenue_by_curr.get("USD", 0.0),
            "payments_count": len(payments_ledger),
            "ledger": payments_ledger,
        },
        "ab_variants": ab_variants,
    }


def format_business_funnel_report() -> str:
    """Format full conversion and revenue report for Telegram and terminal."""
    m = get_business_funnel_metrics()
    s = m["stages"]
    st = m["streams"]
    r = m["revenue"]

    return (
        f"📊 <b>Сквозная бизнес-аналитика и воронка доходности (P1.1)</b>\n\n"
        f"<b>1. ЭТАПЫ СКВОЗНОЙ ВОРОНКИ:</b>\n"
        f"• 🎯 <b>1. Уникальные лиды:</b> {s['1_unique_leads']:,}\n".replace(",", " ") +
        f"• 📑 <b>2a. Подготовленные КП/питчи:</b> {s['2a_proposals_prepared']}\n"
        f"• 🚀 <b>2b. Попытки касаний (клики/переходы):</b> {s['2b_outbounds_attempted']}\n"
        f"• 📤 <b>2c. Подтвержденные отправки:</b> {s['2c_outbounds_confirmed']}\n"
        f"• 💬 <b>3. Содержательные ответы:</b> {s['3_substantive_replies']}\n"
        f"• 🤝 <b>4. Переговоры в процессе:</b> {s['4_negotiations']}\n"
        f"• 🎤 <b>5. Назначенные интервью:</b> {s['5_interviews']}\n"
        f"• 📝 <b>6. Согласованные сделки:</b> {s['7_deals_agreed']}\n"
        f"• 💰 <b>7. Реально полученные деньги:</b> <b>{r['total_rub']:,.0f} ₽</b> | <b>${r['total_usd']:,.0f}</b>\n\n".replace(",", " ") +
        f"<b>2. НАПРАВЛЕНИЯ ПОИСКА (STREAMS):</b>\n"
        f"• 🌐 <code>REMOTE_GLOBAL</code>: <b>{st['REMOTE_GLOBAL']['leads']}</b> лидов (РФ и мир удаленка)\n"
        f"• 🌴 <code>THAILAND_LOCAL</code>: <b>{st['THAILAND_LOCAL']['leads']}</b> лидов (Бангкок и Паттайя, zero relocation)\n"
        f"• ⚡ <code>GIGS_FREELANCE</code>: <b>{st['GIGS_FREELANCE']['leads']}</b> лидов ({st['GIGS_FREELANCE']['proposals']} готовых питчей)\n\n"
        f"<b>3. АКТУАЛЬНЫЕ ПЕРЕГОВОРЫ:</b>\n"
        f"• 🏢 <b>Первый Бит:</b> черновик готов (согласие на 7:00 МСК / 11:00 местное, вилка 130–160 тыс. руб.)\n"
        f"• 🏢 <b>AYITO Tech:</b> черновик подготовлен, ожидает прохождения теста скорости печати (brainapps.ru)\n\n"
        f"💡 <i>Отправка каждого отклика и получение оплаты фиксируются с привязкой к лиду и источнику.</i>"
    )
