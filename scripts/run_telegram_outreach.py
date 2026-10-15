"""Telegram & Email Outreach Delivery Runner.

Safely handles recruiter outreach for discovered multi-source vacancies:
1. Telegram 1-tap card outreach:
   - Filtered strictly for top matches (fit_score / match_score >= 80).
   - Rate-limited to max 2–3 cards per day to eliminate bot spam.
   - Clean UI: exactly 1 action button for quick 1-tap transition to recruiter in Telegram.
2. Email outreach:
   - For vacancies with recruiter email and fit_score >= 75.
   - Auto-dispatches up to 5 emails/day via SMTP when SMTP_PASSWORD is configured,
     attaching the updated IT PDF resume (or Bangkok English CV for international roles).
   - Sends a quiet post-factum confirmation alert to owner in Telegram.
   - If SMTP_PASSWORD is not set, generates .eml package without spamming.
3. Tracks delivery in state.db (telegram_delivery_records & vacancies).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config, db
from ai_assistant.email_outreach import (
    build_mailto_link,
    dispatch_email_outreach,
    extract_emails,
    generate_email_draft,
    get_daily_email_count,
)
from ai_assistant.outreach_generator import build_telegram_deeplink, generate_outreach_pitch
from ai_assistant.smart_experience_generator import generate_english_cover_letter

logger = logging.getLogger("telegram_outreach")

TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = getattr(config, "TELEGRAM_OWNER_ID", None) or config.HH_TELEGRAM_CHAT_ID or config.TELEGRAM_CHAT_ID

MAX_DAILY_TG_CARDS = 3
MAX_DAILY_EMAILS = 5


def get_daily_telegram_cards_count() -> int:
    """Return count of interactive Telegram outreach cards delivered today."""
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT count(*) FROM telegram_delivery_records
            WHERE notification_type = 'outreach_telegram_card'
              AND delivered_at >= date('now', 'start of day')
        """)
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.warning(f"Could not check daily telegram card count: {e}")
        return 0


def record_telegram_card_sent(stable_id: str, tg_username: str, title: str, company: str) -> None:
    """Record outreach telegram card delivery in telegram_delivery_records."""
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, 'outreach_telegram_card', ?, datetime('now'), 'SENT', ?)
        """, (
            f"tg_card:{stable_id}",
            str(TELEGRAM_CHAT_ID),
            json.dumps({"username": tg_username, "title": title, "company": company, "stable_id": stable_id}, ensure_ascii=False),
        ))
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not record telegram card delivery: {e}")


def send_telegram_card(
    message_text: str,
    inline_keyboard: list[list[dict[str, str]]] | None = None,
) -> bool:
    """Send rich vacancy card to Mikhail's Telegram with inline action buttons."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram bot credentials not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Telegram card send failed: {e}")
        return False


def dispatch_outreach_batch(limit: int = 5) -> dict[str, int]:
    """Dispatch top target vacancies with safe rate limiting for Email and Telegram."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT stable_id, source, title, company, description, salary_min, salary_max, salary_currency, job_url, raw_data, location, match_score
        FROM vacancies
        WHERE state = 'DISCOVERED' AND (source LIKE 'tg_%' OR source IN ('hirify', 'habrcareer', 'geekjob', 'linkedin_bangkok', 'bangkok_careers'))
        ORDER BY (CASE WHEN source IN ('bangkok_careers', 'linkedin_bangkok') THEN 1 ELSE 2 END), match_score DESC, first_seen_at DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()

    stats = {
        "tg_cards_sent": 0,
        "emails_dispatched": 0,
        "processed": 0,
    }

    daily_tg_cards = get_daily_telegram_cards_count()
    daily_emails = get_daily_email_count()

    for row in rows:
        stable_id, source, title, company, description, sal_min, sal_max, sal_curr, job_url, raw_json, loc, match_score = row
        raw = {}
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except Exception:
            pass

        effective_score = float(match_score or raw.get("fit_score") or raw.get("score") or 0.0)
        is_bangkok = source in ("linkedin_bangkok", "bangkok_careers")

        # Format salary string
        sal_parts = []
        if sal_min:
            sal_parts.append(f"от {int(sal_min):,}".replace(",", " "))
        if sal_max:
            sal_parts.append(f"до {int(sal_max):,}".replace(",", " "))
        sal_str = " ".join(sal_parts) + (f" {sal_curr}" if sal_parts else "")

        # Extract contacts
        contact_str = raw.get("contact", "")
        combined_text = f"{contact_str} {description}"

        tg_match = re.search(r"@([a-zA-Z0-9_]{4,})", combined_text)
        tg_username = tg_match.group(1) if tg_match else None
        if tg_username and tg_username.lower() in ("remoteit", "normrabota", "sysadmin_jobs", "forpython", "geekjobs", "backend_jobs"):
            tg_username = None

        recruiter_emails = extract_emails(combined_text)

        # -------------------------------------------------------------
        # 1. Email outreach: fit_score >= 75 and email contact present
        # -------------------------------------------------------------
        if recruiter_emails and effective_score >= 75.0:
            hr_email = recruiter_emails[0]
            if os.environ.get("SMTP_PASSWORD") and daily_emails < MAX_DAILY_EMAILS:
                logger.info(f"Auto-dispatching email to {hr_email} for {title} (score={effective_score})")
                res = dispatch_email_outreach(
                    to_email=hr_email,
                    role=title,
                    company=company,
                    description=description,
                    is_bangkok=is_bangkok,
                    attempt_smtp=True,
                    stable_id=stable_id,
                )
                if res.get("smtp_sent"):
                    stats["emails_dispatched"] += 1
                    daily_emails += 1
                    time.sleep(5.0)  # Safe spacing between emails
            else:
                # Generate .eml package silently without sending
                dispatch_email_outreach(
                    to_email=hr_email,
                    role=title,
                    company=company,
                    description=description,
                    is_bangkok=is_bangkok,
                    attempt_smtp=False,
                    stable_id=stable_id,
                )

        # -------------------------------------------------------------
        # 2. Telegram Recruiter Card: fit_score >= 80, exactly 1 button
        # -------------------------------------------------------------
        card_sent = False
        if tg_username and effective_score >= 80.0:
            if daily_tg_cards < MAX_DAILY_TG_CARDS:
                pitch = generate_outreach_pitch(
                    title=title,
                    company=company,
                    description=description,
                    salary_str=sal_str,
                )
                deeplink = build_telegram_deeplink(tg_username, pitch)

                # Exactly 1 button for 1-click apply in Telegram
                buttons = [[{"text": f"🚀 Отклик в Telegram (@{tg_username})", "url": deeplink}]]

                source_label = source.replace("tg_", "@").capitalize()
                sal_display = f"💰 <b>Зарплата:</b> {sal_str}\n" if sal_str else ""
                comp_display = f"🏢 <b>Компания:</b> {company}\n" if company and company != "Компания" else ""

                message_text = (
                    f"🎯 <b>Целевая вакансия [{source_label}] (Score: {int(effective_score)}%)</b>\n\n"
                    f"💼 <b>Позиция:</b> {title}\n"
                    f"{comp_display}"
                    f"{sal_display}"
                    f"🌍 <b>Формат:</b> 100% удаленно\n\n"
                    f"✉️ <b>Готовое сообщение рекрутеру:</b>\n"
                    f"<i>«{pitch}»</i>"
                )

                ok = send_telegram_card(message_text, inline_keyboard=buttons)
                if ok:
                    record_telegram_card_sent(stable_id, tg_username, title, company)
                    daily_tg_cards += 1
                    stats["tg_cards_sent"] += 1
                    card_sent = True
                    time.sleep(2.0)
            else:
                logger.info(f"Skipping Telegram card for {stable_id}: daily cap ({MAX_DAILY_TG_CARDS}) reached.")

        cursor.execute("UPDATE vacancies SET state = 'OUTREACH_READY' WHERE stable_id = ?", (stable_id,))
        conn.commit()
        stats["processed"] += 1

    conn.close()
    return stats


if __name__ == "__main__":
    res = dispatch_outreach_batch(limit=5)
    print("Outreach batch execution summary:", res)
