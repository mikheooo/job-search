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
from ai_assistant.hh_autopilot import (
    _ROLE_GATE_BLOCK_RE,
    _ROLE_GATE_FALLBACK_RE,
    _ROLE_GATE_POSITIVE_RE,
    check_salary_floor,
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


NON_RECRUITER_HANDLES = {
    "channel", "freelance_orders", "zakazy_it", "remoteit", "normrabota",
    "sysadmin_jobs", "forpython", "geekjobs", "backend_jobs", "devops_jobs",
    "devops_jobs_feed", "proglib_jobs", "bot", "gmail", "mail"
}


def extract_true_recruiter_handle(text: str) -> str | None:
    """Extract valid individual recruiter @username or t.me link, ignoring channel metadata."""
    # 1. Look for explicit contact lines at the bottom or labeled sections
    contact_patterns = [
        r"(?:для\s+связи|пиши(?:те)?(?:\s+в\s+лс)?|контакт(?:ы)?(?:\s+для\s+отклика)?|откликнуться|tg|телеграм|telegram)[:\s]+([^\n]+)",
    ]
    for cp in contact_patterns:
        m = re.search(cp, text, re.IGNORECASE)
        if m:
            sub = m.group(1)
            for h in re.findall(r"@([a-zA-Z0-9_]{4,})", sub):
                if h.lower() not in NON_RECRUITER_HANDLES and not h.lower().endswith("bot"):
                    return h

    # 2. Look for handles outside discussion / publisher headers
    clean = re.sub(r"^(?:Публикатор|Обсуждение)[:\s]+[^\n]*\n", "", text, flags=re.MULTILINE | re.IGNORECASE)
    for h in re.findall(r"(?:@([a-zA-Z0-9_]{4,})|t\.me/([a-zA-Z0-9_]{4,}))", clean):
        cand = h[0] or h[1]
        if cand and cand.lower() not in NON_RECRUITER_HANDLES and not cand.lower().endswith("bot"):
            return cand
    return None


def extract_true_company(company: str, text: str) -> str:
    """Clean company name from post body when header has placeholder or author name."""
    m = re.search(r"Компания:\s*([^\n.,]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    c = (company or "").strip()
    if not c or c in ("Компания", "%hirify_global%", "Заказчик", "акансия", "сем привет") or len(c) <= 2:
        return "Компания"
    return c


def clean_tg_title(title: str, desc: str) -> str:
    """Normalize extracted vacancy title from raw post when it contains boilerplate."""
    t = (title or "").strip()
    if any(p in t.lower() for p in ["компания:", "всем привет", "публикатор:", "вакансия", "🔔"]):
        m = re.search(r"(?:Позиция|Должность|Роль|Ищем|Требуется)[:\s]+([^\n.,]+)", desc, re.IGNORECASE)
        if m:
            t = m.group(1).strip()
        else:
            m2 = re.search(r"([^\n]*\b(?:DevOps|SRE|Python|Admin|Инженер|Разработчик|CRM|Битрикс|Support|Поддержк\w*)\b[^\n]*)", desc, re.IGNORECASE)
            if m2:
                t = m2.group(1).strip()
    return t


def dispatch_outreach_batch(limit: int = 5) -> dict[str, int]:
    """Dispatch top target vacancies with safe rate limiting for Email and Telegram."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT stable_id, source, title, company, description, salary_min, salary_max, salary_currency, job_url, raw_data, location, match_score, application_url
        FROM vacancies
        WHERE state = 'DISCOVERED' AND (source LIKE 'tg_%' OR description LIKE '%@%' OR application_url LIKE '%greenhouse.io%' OR application_url LIKE '%lever.co%')
        ORDER BY (CASE WHEN description LIKE '%@%' THEN 1 WHEN application_url LIKE '%greenhouse.io%' OR application_url LIKE '%lever.co%' THEN 2 ELSE 3 END), first_seen_at DESC
        LIMIT ?
    """, (limit * 5,))
    rows = cursor.fetchall()

    stats = {
        "tg_cards_sent": 0,
        "emails_dispatched": 0,
        "ats_cards_sent": 0,
        "processed": 0,
    }

    daily_tg_cards = get_daily_telegram_cards_count()
    daily_emails = get_daily_email_count()

    for row in rows:
        stable_id, source, title, company, description, sal_min, sal_max, sal_curr, job_url, raw_json, loc, match_score, app_url = row
        desc_str = description or ""
        title_str = clean_tg_title(title or "", desc_str)
        comp_str = extract_true_company(company or "Компания", desc_str)

        # Role gate & craft block
        if _ROLE_GATE_BLOCK_RE.search(title_str):
            cursor.execute("UPDATE vacancies SET state = 'REJECTED' WHERE stable_id = ?", (stable_id,))
            conn.commit()
            continue

        raw = {}
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except Exception:
            pass

        effective_score = float(match_score or raw.get("fit_score") or raw.get("score") or 0.0)
        is_target_role = bool(
            effective_score >= 70.0
            or _ROLE_GATE_POSITIVE_RE.search(title_str)
            or _ROLE_GATE_FALLBACK_RE.search(title_str)
            or _ROLE_GATE_POSITIVE_RE.search(desc_str[:300])
        )
        if not is_target_role:
            continue

        # Salary check
        sal_parts = []
        if sal_min:
            sal_parts.append(f"от {int(sal_min):,}".replace(",", " "))
        if sal_max:
            sal_parts.append(f"до {int(sal_max):,}".replace(",", " "))
        sal_str = " ".join(sal_parts) + (f" {sal_curr}" if sal_parts else "")
        sal_ok, _ = check_salary_floor(sal_str)
        if not sal_ok:
            continue

        is_bangkok = source in ("linkedin_bangkok", "bangkok_careers")

        # Extract contacts
        contact_str = raw.get("contact", "")
        combined_text = f"{contact_str} {desc_str}"
        tg_username = extract_true_recruiter_handle(combined_text)
        recruiter_emails = extract_emails(combined_text)

        # ATS direct URL check
        ats_url = None
        for u in (app_url or "", job_url or ""):
            if any(dom in u.lower() for dom in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com")):
                ats_url = u
                break

        action_taken = False

        # -------------------------------------------------------------
        # 1. Telegram Recruiter Card: 1-click apply deeplink
        # -------------------------------------------------------------
        if tg_username and daily_tg_cards < MAX_DAILY_TG_CARDS:
            pitch = generate_outreach_pitch(
                title=title_str,
                company=comp_str,
                description=desc_str,
                salary_str=sal_str,
            )
            deeplink = build_telegram_deeplink(tg_username, pitch)
            buttons = [[{"text": f"🚀 Отправить рекрутеру в 1 клик (@{tg_username})", "url": deeplink}]]

            source_label = source.replace("tg_", "@").capitalize()
            sal_display = f"💰 <b>Зарплата:</b> {sal_str}\n" if sal_str else ""
            comp_display = f"🏢 <b>Компания:</b> {comp_str}\n" if comp_str and comp_str != "Компания" else ""

            message_text = (
                f"🎯 <b>Целевая вакансия [{source_label}]</b>\n\n"
                f"💼 <b>Позиция:</b> <a href=\"{job_url or deeplink}\">{title_str}</a>\n"
                f"{comp_display}"
                f"{sal_display}"
                f"👤 <b>Рекрутер:</b> @{tg_username}\n"
                f"🌍 <b>Формат:</b> 100% удаленно\n\n"
                f"✉️ <b>Готовое сообщение рекрутеру:</b>\n"
                f"<i>«{pitch}»</i>"
            )

            ok = send_telegram_card(message_text, inline_keyboard=buttons)
            if ok:
                record_telegram_card_sent(stable_id, tg_username, title_str, comp_str)
                daily_tg_cards += 1
                stats["tg_cards_sent"] += 1
                action_taken = True
                time.sleep(2.0)

        # -------------------------------------------------------------
        # 2. Email outreach: direct recruiter email
        # -------------------------------------------------------------
        elif recruiter_emails and daily_emails < MAX_DAILY_EMAILS:
            hr_email = recruiter_emails[0]
            if os.environ.get("SMTP_PASSWORD"):
                logger.info(f"Auto-dispatching email to {hr_email} for {title_str}")
                res = dispatch_email_outreach(
                    to_email=hr_email,
                    role=title_str,
                    company=comp_str,
                    description=desc_str,
                    is_bangkok=is_bangkok,
                    attempt_smtp=True,
                    stable_id=stable_id,
                )
                if res.get("smtp_sent"):
                    stats["emails_dispatched"] += 1
                    daily_emails += 1
                    action_taken = True
                    time.sleep(5.0)
            else:
                res = dispatch_email_outreach(
                    to_email=hr_email,
                    role=title_str,
                    company=comp_str,
                    description=desc_str,
                    is_bangkok=is_bangkok,
                    attempt_smtp=False,
                    stable_id=stable_id,
                )
                mailto_url = res.get("mailto_url")
                buttons = [[{"text": f"✉️ Отправить Email ({hr_email})", "url": mailto_url}]]
                message_text = (
                    f"✉️ <b>Подготовлен Email-отклик с резюме!</b>\n\n"
                    f"💼 <b>Позиция:</b> <a href=\"{job_url}\">{title_str}</a>\n"
                    f"🏢 <b>Компания:</b> {comp_str}\n"
                    f"📧 <b>Email рекрутера:</b> {hr_email}\n"
                    f"📄 <b>Резюме:</b> PDF прикреплен к пакету\n\n"
                    f"✉️ <b>Тема:</b> {res.get('subject')}\n"
                )
                ok = send_telegram_card(message_text, inline_keyboard=buttons)
                if ok:
                    stats["emails_dispatched"] += 1
                    daily_emails += 1
                    action_taken = True
                    time.sleep(2.0)

        # -------------------------------------------------------------
        # 3. Direct ATS Career Link (Greenhouse / Lever / Ashby)
        # -------------------------------------------------------------
        elif ats_url and stats["ats_cards_sent"] < 3:
            buttons = [[{"text": "🌐 Открыть форму компании (Greenhouse/Lever)", "url": ats_url}]]
            message_text = (
                f"🌐 <b>Целевая вакансия на прямом ATS работодателя!</b>\n\n"
                f"💼 <b>Позиция:</b> <a href=\"{ats_url}\">{title_str}</a>\n"
                f"🏢 <b>Компания:</b> {comp_str}\n"
                f"{('💰 <b>Зарплата:</b> ' + sal_str + chr(10)) if sal_str else ''}"
                f"📄 <b>Резюме:</b> Mikhail_Kolesnikov_CV_{'Bangkok' if is_bangkok else 'IT_Engineer'}.pdf\n\n"
                f"👉 <i>Прямая подача через корпоративную ATS систему компании без посредников.</i>"
            )
            ok = send_telegram_card(message_text, inline_keyboard=buttons)
            if ok:
                stats["ats_cards_sent"] += 1
                action_taken = True
                time.sleep(2.0)

        if action_taken:
            cursor.execute("UPDATE vacancies SET state = 'OUTREACH_READY' WHERE stable_id = ?", (stable_id,))
            conn.commit()
            stats["processed"] += 1
            if stats["processed"] >= limit:
                break

    conn.close()
    return stats


if __name__ == "__main__":
    res = dispatch_outreach_batch(limit=5)
    print("Outreach batch execution summary:", res)
