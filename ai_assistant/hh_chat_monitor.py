"""HeadHunter Chat & Negotiations Monitor.

Monitors incoming employer messages and invitation status changes on HH.
When a new employer message or interview invite is detected:
1. Generates a tailored, persuasive reply via interview_auto_responder.
2. Records the event in state.db (deduplicated by fingerprint).
3. Sends an immediate Telegram alert to @jobsassistbot with message text,
   proposed reply draft, and direct negotiation link.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from ai_assistant import config, db
from ai_assistant.hh_browser_provider import HeadlessBrowserSession
from ai_assistant.interview_auto_responder import analyze_employer_inquiry

logger = logging.getLogger("hh_chat_monitor")

# Phase 3 security: credentials come from environment (.env) via
# ai_assistant.config — no hardcoded tokens. Resolves to the same HH bot
# previously hardcoded here, so runtime behavior is unchanged.
TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = config.HH_TELEGRAM_CHAT_ID or config.TELEGRAM_CHAT_ID


def send_telegram_alert(text: str) -> bool:
    """Send alert to the dedicated jobs Telegram bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False


def scan_hh_negotiations(session: HeadlessBrowserSession | None = None) -> list[dict[str, Any]]:
    """Scan https://hh.ru/applicant/negotiations for negotiation cards and status changes."""
    close_session_when_done = False
    if session is None:
        session = HeadlessBrowserSession(require_auth=True)
        info = session.start()
        if not info.get("ok"):
            err = info.get("message") or info.get("error") or info.get("status") or "unknown"
            logger.error(
                "Cannot start browser session for chat monitor: %s | cdp_url=%s profile=%s",
                err, session.cdp_url, session.profile_dir,
            )
            return []
        close_session_when_done = True

    try:
        tab = session.ensure_work_tab()
        session.navigate(tab, "https://hh.ru/applicant/negotiations", timeout=25.0)
        time.sleep(3.0)

        # Extract negotiation items from the page
        cards_data = session.evaluate("""(() => {
            const results = [];
            const cards = Array.from(document.querySelectorAll('div[data-qa*="negotiations-item"], div[class*="negotiations-item"], [data-qa="negotiations-item"]'));
            const candidateElements = cards.length ? cards : Array.from(document.querySelectorAll('div[data-qa*="response"], article'));

            for (const el of candidateElements) {
                const text = el.innerText || '';
                const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                if (lines.length < 2) continue;

                // Find chat or vacancy link
                const links = Array.from(el.querySelectorAll('a')).map(a => ({href: a.href, text: a.innerText.trim()}));
                const chatLink = links.find(l => l.text.toLowerCase().includes('чат') || l.href.includes('chat') || l.href.includes('negotiations'));
                const vacancyLink = links.find(l => l.href.includes('/vacancy/'));

                // Detect unread messages & badges
                const hasUnreadBadge = !!el.querySelector('[class*="badge"], [data-qa*="unread"], [class*="unread"], [data-qa*="badge"], span[class*="dot"]');
                const hasUnreadText = /новое сообщение|непрочитан|новое|написал|сообщение от/i.test(text);
                const hasUnread = hasUnreadBadge || hasUnreadText;

                let status = "Ожидание";
                if (/приглашен/i.test(text)) status = "Приглашение";
                else if (/собеседован/i.test(text)) status = "Собеседование";
                else if (hasUnread) status = "Новое сообщение";
                else if (/отказ/i.test(text)) status = "Отказ";
                else if (/не просмотрен/i.test(text)) status = "Не просмотрен";

                results.push({
                    text: text.slice(0, 1000),
                    lines: lines.slice(0, 8),
                    status: status,
                    has_unread: hasUnread,
                    chat_url: chatLink ? chatLink.href : 'https://hh.ru/applicant/negotiations',
                    vacancy_url: vacancyLink ? vacancyLink.href : '',
                    has_chat_button: !!chatLink
                });
            }
            return {
                title: document.title,
                unread_chats_count: (document.title.match(/^(\\d+)/) || [])[1] || '0',
                cards: results
            };
        })()""")

        if isinstance(cards_data, str):
            try:
                cards_data = json.loads(cards_data)
            except Exception:
                cards_data = {}

        cards = cards_data.get("cards") or []
        unread_chats = cards_data.get("unread_chats_count", "0")
        logger.info(f"Negotiations scanned: {len(cards)} items found, title indicates {unread_chats} unread items")
        return cards
    except Exception as e:
        logger.error(f"Error scanning negotiations: {e}", exc_info=True)
        return []
    finally:
        if close_session_when_done:
            session.stop()


def process_negotiation_alerts(cards: list[dict[str, Any]], dry_run: bool = False) -> int:
    """Check scanned negotiation cards, generate replies for invites/messages, and alert user."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hh_negotiation_card_alerts (
            card_key TEXT PRIMARY KEY,
            role TEXT,
            employer TEXT,
            status TEXT,
            chat_url TEXT,
            alerted_at TEXT
        )
    """)
    conn.commit()

    # Alert on any employer interaction worth noticing:
    # invitations, interviews, unread messages, and new chat activity.
    ALERTABLE_STATUSES = ("Приглашение", "Собеседование", "Новое сообщение", "Не просмотрен")
    REJECTION_NEEDLES = ("отказ", "отклонен", "отклонён", "отказано")

    alerts_sent = 0

    for card in cards:
        status = card.get("status", "Ожидание")
        lines = card.get("lines", [])
        if not lines:
            continue

        role = lines[0]
        employer = lines[1] if len(lines) > 1 else "Работодатель"
        if "не просмотрен" in role.lower() and len(lines) > 2:
            role = lines[1]
            employer = lines[2]

        # Skip rejections / archives outright — once an employer said "no" or vacancy is archived,
        # the conversation is dead. If it has an unread badge, record it as read so it stops hanging.
        full_text = " ".join(lines).lower()
        if any(needle in full_text for needle in REJECTION_NEEDLES) or "архив" in full_text:
            if card.get("has_unread"):
                card_key = f"{role}:{employer}:archived_read".strip().lower()
                cursor.execute(
                    "INSERT OR REPLACE INTO hh_negotiation_card_alerts (card_key, role, employer, status, chat_url, alerted_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (card_key, role, employer, "ARCHIVED/REJECTED", card.get("chat_url") or "", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
                )
                conn.commit()
            logger.info(f"Skipping rejected/archived application: {role} at {employer} (marked as read)")
            continue

        card_key = f"{role}:{employer}:{status}".strip().lower()

        cursor.execute("SELECT card_key FROM hh_negotiation_card_alerts WHERE card_key = ?", (card_key,))
        if cursor.fetchone():
            continue

        if status in ALERTABLE_STATUSES or card.get("has_unread"):
            chat_url = card.get("chat_url") or "https://hh.ru/applicant/negotiations"

            if status in ("Приглашение", "Собеседование"):
                logger.info(f"New positive event: {status} for {role} at {employer}")
                # Phase 4A: best-effort bridge employer invitation -> tracking.
                # Strict by design: exactly-one application, legal transition
                # (APPLIED -> INTERVIEW), idempotent via card_key, stale-safe.
                # Never raises, never blocks the Telegram alert below.
                try:
                    from ai_assistant.employer_events import apply_employer_event

                    bridge_res = apply_employer_event(
                        kind="interview",
                        source=f"hh_chat_monitor:{status}",
                        event_key=f"hhcard:{card_key}",
                        employer=employer,
                        title=role,
                    )
                    if bridge_res.get("outcome") not in ("applied", "already-applied"):
                        logger.info(f"Employer bridge deferred: {bridge_res}")
                except Exception as e:
                    logger.warning(f"Employer bridge skipped for {role} at {employer}: {e}")
                inquiry_res = analyze_employer_inquiry(
                    f"Приглашение на собеседование по вакансии {role}",
                    {"role": role, "company": employer}
                )
                draft = inquiry_res.get("reply_text", "")
                alert_msg = (
                    f"🎉 <b>HH: Получено {status.upper()}!</b>\n\n"
                    f"💼 <b>Вакансия:</b> {role}\n"
                    f"🏢 <b>Компания:</b> {employer}\n\n"
                    f"💡 <b>Готовый черновик ответа для отправки:</b>\n"
                    f"<i>«{draft}»</i>\n\n"
                    f"🔗 <a href=\"{chat_url}\">Перейти в переписку на HH</a>\n"
                    f"📱 Контакты для связи: @brokeassh | +7 (993) 339-76-28"
                )
            else:
                # Unread message from employer
                logger.info(f"Unread message: {status} for {role} at {employer}")
                snippet = lines[2] if len(lines) > 2 else "Новое входящее сообщение"
                inquiry_res = analyze_employer_inquiry(
                    f"Вопрос работодателя по вакансии {role}: {snippet}",
                    {"role": role, "company": employer}
                )
                draft = inquiry_res.get("reply_text", "")
                alert_msg = (
                    f"📩 <b>HH: Новое сообщение от работодателя!</b>\n\n"
                    f"💼 <b>Вакансия:</b> {role}\n"
                    f"🏢 <b>Компания:</b> {employer}\n"
                    f"💬 <b>Текст:</b> <i>«{snippet}»</i>\n\n"
                    f"💡 <b>Предложенный ответ ИИ:</b>\n"
                    f"<i>«{draft}»</i>\n\n"
                    f"🔗 <a href=\"{chat_url}\">Открыть переписку на HH</a>\n"
                    f"📱 Контакты: @brokeassh"
                )

            if not dry_run:
                send_telegram_alert(alert_msg)
                cursor.execute("""
                    INSERT INTO hh_negotiation_card_alerts (card_key, role, employer, status, chat_url, alerted_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                """, (card_key, role, employer, status, chat_url))
                conn.commit()

            alerts_sent += 1

    conn.close()
    return alerts_sent
