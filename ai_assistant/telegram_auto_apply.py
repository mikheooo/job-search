"""Telegram Auto-Apply and Recruiter Outreach Engine.

Orchestrates interactive 1-tap applications to Telegram channels and direct recruiter contacts:
1. Extracts @username, deep links, and contact channels from vacancies and freelance gigs.
2. Synthesizes tailored Russian/English pitch specifically adapted to the role.
3. Delivers high-priority application cards with:
   - Deep-link to recruiter DM with pre-filled pitch.
   - 1-touch copyable pitch block in Telegram (<blockquote>).
   - Interactive callbacks: [✅ Я откликнулся] (marks in DB as CONTACTED/SUBMITTED), [⏭ Пропустить].
4. Tracks application history in state.db so the candidate never reapplies or spams the same recruiter.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config, db
from ai_assistant.gig_pitch_generator import build_gig_deeplink, generate_gig_pitch
from ai_assistant.outreach_generator import build_telegram_deeplink, generate_outreach_pitch

logger = logging.getLogger("telegram_auto_apply")

# Phase 3 security: credentials come from environment (.env) via
# ai_assistant.config — no hardcoded tokens. Resolves to the same HH bot
# previously hardcoded here, so runtime behavior is unchanged.
TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
try:
    ALLOWED_CHAT_ID = int(str(config.TELEGRAM_OWNER_ID or config.TELEGRAM_CHAT_ID or 0))
except (TypeError, ValueError):
    ALLOWED_CHAT_ID = 0

NON_RECRUITER_HANDLES = {
    "channel",
    "freelance_orders",
    "zakazy_it",
    "remoteit",
    "normrabota",
    "sysadmin_jobs",
    "forpython",
    "geekjobs",
    "backend_jobs",
    "devops_jobs_feed",
    "proglib_jobs",
    "bot",
}


def extract_recruiter_contact(text: str) -> dict[str, Any]:
    """Extract valid recruiter @username, t.me link or contact bot from text."""
    # Look for t.me links
    tme_matches = re.findall(r"t\.me/([a-zA-Z0-9_]{4,})", text)
    usernames = re.findall(r"@([a-zA-Z0-9_]{4,})", text)

    all_handles = tme_matches + usernames
    valid_handles = [
        h.strip()
        for h in all_handles
        if h.lower() not in NON_RECRUITER_HANDLES and not h.lower().endswith("bot")
    ]

    contact_bots = [h.strip() for h in all_handles if h.lower().endswith("bot")]

    return {
        "recruiter_username": valid_handles[0] if valid_handles else None,
        "contact_bot": contact_bots[0] if contact_bots else None,
        "all_handles": list(dict.fromkeys(valid_handles)),
    }


def send_telegram_apply_card(
    chat_id: int,
    vacancy: dict[str, Any],
) -> bool:
    """Send an interactive 1-tap apply card to candidate with callback actions."""
    stable_id = vacancy["stable_id"]
    title = vacancy["title"]
    company = vacancy.get("company", "Компания")
    source = vacancy.get("source", "tg")
    desc = vacancy.get("description", "")
    sal_min = vacancy.get("salary_min")
    sal_max = vacancy.get("salary_max")
    sal_curr = vacancy.get("salary_currency", "RUB")
    is_gig = vacancy.get("is_gig", False)

    # Format salary
    sal_parts = []
    if sal_min:
        sal_parts.append(f"от {sal_min:,}".replace(",", " "))
    if sal_max:
        sal_parts.append(f"до {sal_max:,}".replace(",", " "))
    sal_str = " ".join(sal_parts) + (f" {sal_curr}" if sal_parts else "")

    contact_info = extract_recruiter_contact(desc)
    recruiter = contact_info.get("recruiter_username")
    contact_bot = contact_info.get("contact_bot")

    if is_gig:
        pitch = generate_gig_pitch(title=title, description=desc, budget=sal_str)
        badge = "💼 <b>Быстрый заказ (Фриланс / Проект)</b>"
    else:
        pitch = generate_outreach_pitch(title=title, company=company, description=desc, salary_str=sal_str)
        badge = "🎯 <b>Целевая вакансия (Telegram IT)</b>"

    buttons: list[list[dict[str, str]]] = []

    if recruiter:
        deeplink = build_gig_deeplink(recruiter, pitch) if is_gig else build_telegram_deeplink(recruiter, pitch)
        buttons.append([{"text": f"🚀 Открыть диалог с @{recruiter}", "url": deeplink}])
    elif contact_bot:
        buttons.append([{"text": f"🤖 Отклик через бота @{contact_bot}", "url": f"https://t.me/{contact_bot}"}])

    # Add Action callbacks
    # Store minimal callback data: "app:<stable_id_suffix>"
    suffix = stable_id[-20:] if len(stable_id) > 20 else stable_id
    buttons.append([
        {"text": "✅ Я откликнулся", "callback_data": f"app:{suffix}"},
        {"text": "⏭ Пропустить", "callback_data": f"skp:{suffix}"},
    ])

    sal_line = f"💰 <b>Оплата:</b> {sal_str}\n" if sal_str else ""
    comp_line = f"🏢 <b>Компания / Заказчик:</b> {company}\n" if company and company != "Компания" else ""
    contact_line = f"👤 <b>Контакт:</b> @{recruiter}\n" if recruiter else (f"🤖 <b>Бот:</b> @{contact_bot}\n" if contact_bot else "")

    msg = (
        f"{badge}\n\n"
        f"📌 <b>Позиция / Задача:</b> {title}\n"
        f"{comp_line}"
        f"{sal_line}"
        f"{contact_line}"
        f"🌐 <b>Источник:</b> {source.replace('tg_', '@')}\n\n"
        f"📝 <b>Готовый персонализированный отклик:</b>\n"
        f"<blockquote>{pitch}</blockquote>\n\n"
        f"💡 <i>Нажмите кнопку <b>«🚀 Открыть диалог»</b> (текст подставится сам) или нажмите на цитату, чтобы скопировать отклик!</i>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": buttons},
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Apply card send failed: {e}")
        return False


def dispatch_next_telegram_applications(limit: int = 3) -> int:
    """Fetch unapplied Telegram vacancies and dispatch cards to Mikhail."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT stable_id, source, title, company, description, salary_min, salary_max, salary_currency, raw_data
        FROM vacancies
        WHERE state = 'DISCOVERED' AND source LIKE 'tg_%'
        ORDER BY first_seen_at DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()

    dispatched = 0
    for row in rows:
        stable_id, source, title, company, description, sal_min, sal_max, sal_curr, raw_json = row
        raw = {}
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except Exception:
            pass

        is_gig = bool(source in ("tg_freelance_orders", "tg_zakazy_it") or raw.get("is_gig"))

        vac_data = {
            "stable_id": stable_id,
            "title": title,
            "company": company,
            "source": source,
            "description": description,
            "salary_min": sal_min,
            "salary_max": sal_max,
            "salary_currency": sal_curr,
            "is_gig": is_gig,
        }

        ok = send_telegram_apply_card(ALLOWED_CHAT_ID, vac_data)
        if ok:
            cursor.execute("UPDATE vacancies SET state = 'OUTREACH_READY' WHERE stable_id = ?", (stable_id,))
            conn.commit()
            dispatched += 1
            time.sleep(1.0)

    conn.close()
    return dispatched


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so a suffix matches literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve_vacancy_by_suffix(suffix: str) -> str | None:
    """Resolve a callback suffix to exactly one vacancy stable_id.

    Phase 4A safety: the old ``LIKE %suffix`` UPDATE could rewrite several
    rows at once (truncated 20-char suffixes, unescaped ``_``/``%``). Now:
    exact full-stable_id match wins; otherwise the suffix must match EXACTLY
    ONE row; zero or ambiguous matches refuse (return None, no writes).
    """
    suffix = (suffix or "").strip()
    if not suffix:
        return None
    db.init_db()
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT stable_id FROM vacancies WHERE stable_id = ?", (suffix,))
        row = cursor.fetchone()
        if row:
            return row[0]
        pattern = f"%{_escape_like(suffix)}"
        cursor.execute(
            "SELECT stable_id FROM vacancies WHERE stable_id LIKE ? ESCAPE '\\'",
            (pattern,),
        )
        matches = [r[0] for r in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            logger.warning(
                "Ambiguous callback suffix %r matches %d vacancies; refusing to update.",
                suffix, len(matches),
            )
        return None
    finally:
        conn.close()


def mark_vacancy_applied(suffix: str) -> bool:
    """Mark vacancy as applied/contacted in DB (exactly one row, never more)."""
    stable_id = resolve_vacancy_by_suffix(suffix)
    if not stable_id:
        return False
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE vacancies SET state = 'SUBMITTED' WHERE stable_id = ?", (stable_id,))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def mark_vacancy_skipped(suffix: str) -> bool:
    """Mark vacancy as skipped/rejected in DB (exactly one row, never more)."""
    stable_id = resolve_vacancy_by_suffix(suffix)
    if not stable_id:
        return False
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE vacancies SET state = 'REJECTED' WHERE stable_id = ?", (stable_id,))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


if __name__ == "__main__":
    cnt = dispatch_next_telegram_applications(limit=2)
    print(f"Dispatched {cnt} Telegram auto-apply cards.")
