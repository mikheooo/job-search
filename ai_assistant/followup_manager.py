"""Automated Follow-up Reminders & Sequences for Job Applications.

Identifies submitted applications that are 3 to 7 days old with no response,
synthesizes professional, non-intrusive follow-up messages (Russian / English),
and delivers interactive action cards to Telegram (@jobsassistbot).
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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config, db, telegram_routing
from ai_assistant.gig_pitch_generator import build_gig_deeplink
from ai_assistant.outreach_generator import build_telegram_deeplink

logger = logging.getLogger("followup_manager")

# Phase 3 security: credentials come from environment (.env) via
# ai_assistant.config — no hardcoded tokens. Resolves to the same HH bot
# previously hardcoded here, so runtime behavior is unchanged.
TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN
try:
    ALLOWED_CHAT_ID = int(str(config.HH_TELEGRAM_CHAT_ID or 0))
except (TypeError, ValueError):
    ALLOWED_CHAT_ID = 0

# Age window (days): a follow-up card is only offered when the application was
# really sent at least FOLLOWUP_MIN_AGE_DAYS and at most FOLLOWUP_MAX_AGE_DAYS
# ago. This is what the module docstring and the Telegram menu have always
# promised; before the F1 fix the query had no age filter at all.
FOLLOWUP_MIN_AGE_DAYS = 3
FOLLOWUP_MAX_AGE_DAYS = 7

# application_tracking statuses that prove a response was actually sent.
# VERIFIED is included: the lifecycle reaches it only after the send was
# verified (see application_tracking._ALLOWED). Pre-send stages (DISCOVERED,
# ANALYZED, READY_TO_APPLY, ...) are never evidence of an application.
_SENT_TRACKING_STATUSES = ("SUBMITTED", "VERIFIED", "APPLIED", "INTERVIEW")
# A rejected or withdrawn application must not receive a follow-up, even if a
# stale submission row still exists for it.
_DEAD_TRACKING_STATUSES = ("REJECTED", "WITHDRAWN")
# Vacancy states that can never be follow-up candidates.
#
# DO NOT add 'NEW' to this tuple (deliberate policy, confirmed by the fix
# owner 2026-10-19). Audit F1 saw hh rows in state='NEW' in the follow-up list,
# but the defect was the OLD query having no sent-evidence filter at all
# (`OR v.source = 'hh'`), not the NEW state itself: the hh autopilot writes
# application_tracking.SUBMITTED while vacancies.state stays 'NEW'/'REVIEW'
# (measured in production: hh:137448071 verified submitted 2026-10-07 but
# state='NEW'; hh:137882190, hh:137697411 state='REVIEW'). A blanket NEW
# exclusion would therefore suppress real follow-ups. Never-applied vacancies
# are kept out by the `sent` JOIN in find_pending_followups -- see
# tests/test_followup_manager_fixes.py::
# test_hh_vacancy_still_in_new_state_after_application_is_selected.
# Audit F1's own fix recommendation also excluded only REJECTED/WITHDRAWN.
_EXCLUDED_VACANCY_STATES = ("REJECTED", "WITHDRAWN")

# Telegram caps callback_data at 64 bytes. The longest prefix we use is
# "flwmute:" (8 bytes), so the id token gets 56 bytes.
_CALLBACK_DATA_LIMIT = 64
_CALLBACK_ID_BUDGET = _CALLBACK_DATA_LIMIT - len("flwmute:")


def init_followup_table():
    """Ensure follow-up tracking table exists in state.db."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS application_followups (
            stable_id TEXT PRIMARY KEY,
            company TEXT,
            role TEXT,
            contact TEXT,
            followup_count INTEGER DEFAULT 0,
            last_followup_at TEXT,
            status TEXT DEFAULT 'PENDING'
        )
    """)
    conn.commit()
    conn.close()


def generate_followup_message(
    role: str,
    company: str,
    is_bangkok: bool = False,
    recipient_name: Optional[str] = None,
) -> str:
    """Generate tailored, polite follow-up message."""
    if is_bangkok:
        greeting = f"Hello {company} Hiring Team," if not recipient_name else f"Hello {recipient_name},"
        return (
            f"{greeting}\n\n"
            f"I am following up on my application for the {role} role at {company}.\n\n"
            f"As I am already based locally here in Bangkok, I am readily available for an in-person meeting at your office or a quick video call at your convenience. Zero relocation expenses or timeline delays are required.\n\n"
            f"Looking forward to hearing from you!\n\n"
            f"Best regards,\n"
            f"Mikhail Kolesnikov\n"
            f"Telegram: @brokeassh | Phone/WhatsApp: +7 (993) 339-76-28"
        )
    else:
        greeting = f"Здравствуйте, {company}!" if not recipient_name else f"Здравствуйте, {recipient_name}!"
        return (
            f"{greeting}\n\n"
            f"Несколько дней назад направлял вам отклик на вакансию «{role}».\n\n"
            f"Хотел вежливо уточнить, удалось ли ознакомиться с резюме? Готов оперативно выйти на короткий звонок, ответить на технические вопросы по стеку (Linux, Docker, Python, Bitrix24, сети) или выполнить тестовое задание.\n\n"
            f"Всегда на связи в Telegram: @brokeassh или по телефону: +7 (993) 339-76-28.\n"
            f"Спасибо и хорошего дня!"
        )


def _sql_placeholders(values: tuple[str, ...]) -> str:
    """Comma-separated ``?`` placeholders for a fixed-length IN (...) list."""
    return ", ".join("?" for _ in values)


def _utc_iso(value: datetime) -> str:
    """UTC timestamp in the same shape the DB writers use (no tz suffix)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def find_pending_followups(
    limit: int = 5,
    min_days: float = FOLLOWUP_MIN_AGE_DAYS,
    max_days: float = FOLLOWUP_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Query applications eligible for a follow-up reminder.

    Only applications that were really sent are eligible: an
    ``application_tracking`` row in a post-send status (SUBMITTED, VERIFIED,
    APPLIED, INTERVIEW), or an ``application_submissions`` row with
    ``status='SUBMITTED'``. The send timestamp must fall into the
    ``min_days``..``max_days`` age window (3..7 days by default).

    Excluded: never-applied vacancies (a ``state='NEW'`` row without a real
    send is filtered out by the JOIN itself -- 'NEW' is NOT excluded
    unconditionally, see the ``_EXCLUDED_VACANCY_STATES`` comment), vacancies
    marked REJECTED or WITHDRAWN, applications whose tracking status is
    REJECTED/WITHDRAWN, rows muted by the user and applications followed up
    twice already. ``now`` is injectable so the window can be pinned in tests.
    """
    if min_days < 0 or max_days < min_days:
        raise ValueError(f"invalid follow-up window: {min_days}..{max_days} days")
    if now is None:
        now = datetime.now(timezone.utc)
    # "sent no later than now - min_days" and "sent no earlier than now - max_days"
    newest_iso = _utc_iso(now - timedelta(days=min_days))
    oldest_iso = _utc_iso(now - timedelta(days=max_days))

    init_followup_table()
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            WITH sent AS (
                SELECT t.vacancy_stable_id AS stable_id,
                       COALESCE(NULLIF(t.applied_at, ''),
                                NULLIF(t.last_status_change_at, ''),
                                NULLIF(t.updated_at, ''),
                                NULLIF(t.created_at, '')) AS sent_at
                FROM application_tracking t
                WHERE t.status IN ({_sql_placeholders(_SENT_TRACKING_STATUSES)})
                UNION ALL
                SELECT s.vacancy_stable_id,
                       COALESCE(NULLIF(s.submitted_at, ''),
                                NULLIF(s.created_at, ''),
                                NULLIF(s.updated_at, ''))
                FROM application_submissions s
                WHERE s.status = 'SUBMITTED'
            ),
            last_sent AS (
                -- MAX: follow up 3..7 days after the LAST real send, so a
                -- re-submission resets the window instead of adding cards.
                SELECT stable_id, MAX(sent_at) AS sent_at
                FROM sent
                WHERE sent_at IS NOT NULL AND sent_at != ''
                GROUP BY stable_id
            )
            SELECT v.stable_id, v.source, v.title, v.company, v.description,
                   v.first_seen_at, v.job_url, v.raw_data,
                   f.followup_count, f.last_followup_at
            FROM vacancies v
            JOIN last_sent ls ON ls.stable_id = v.stable_id
            LEFT JOIN application_followups f ON v.stable_id = f.stable_id
            WHERE v.state NOT IN ({_sql_placeholders(_EXCLUDED_VACANCY_STATES)})
              AND v.stable_id NOT IN (
                  SELECT vacancy_stable_id FROM application_tracking
                  WHERE status IN ({_sql_placeholders(_DEAD_TRACKING_STATUSES)}))
              AND julianday(ls.sent_at) IS NOT NULL
              AND julianday(ls.sent_at) >= julianday(?)
              AND julianday(ls.sent_at) <= julianday(?)
              AND (f.followup_count IS NULL OR f.followup_count < 2)
              AND (f.status IS NULL OR f.status != 'MUTED')
            ORDER BY v.first_seen_at ASC
            LIMIT ?
            """,
            (
                *_SENT_TRACKING_STATUSES,
                *_EXCLUDED_VACANCY_STATES,
                *_DEAD_TRACKING_STATUSES,
                oldest_iso,
                newest_iso,
                limit,
            ),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    results = []
    for r in rows:
        stable_id, source, title, company, desc, first_seen, job_url, raw_json, f_cnt, f_last = r
        raw = {}
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except Exception:
            pass

        is_bangkok = bool(source in ("linkedin_bangkok", "bangkok_careers"))

        # Extract recruiter username if available
        combined = f"{raw.get('contact', '')} {desc}"
        tg_match = re.search(r"@([a-zA-Z0-9_]{4,})", combined)
        recruiter = tg_match.group(1) if tg_match else None

        results.append({
            "stable_id": stable_id,
            "source": source,
            "title": title,
            "company": company or "Компания",
            "job_url": job_url,
            "recruiter": recruiter,
            "is_bangkok": is_bangkok,
            "followup_count": f_cnt or 0,
        })
    return results


def _followup_callback_id(stable_id: str) -> str:
    """Callback token for a follow-up card.

    Telegram limits ``callback_data`` to 64 bytes. The full stable_id is used
    when it fits together with the longest prefix ("flwmute:"); long ids are
    sent as their last 20 characters and resolved back to the real row exactly
    once by ``_resolve_stable_id`` (never written as a PRIMARY KEY).
    """
    if len(stable_id.encode("utf-8")) <= _CALLBACK_ID_BUDGET:
        return stable_id
    token = stable_id[-20:]
    while len(token.encode("utf-8")) > _CALLBACK_ID_BUDGET:
        token = token[1:]
    return token


def send_followup_card(chat_id: int, item: dict[str, Any]) -> bool:
    """Send an interactive follow-up card to Telegram."""
    stable_id = item["stable_id"]
    title = item["title"]
    company = item["company"]
    recruiter = item["recruiter"]
    is_bangkok = item["is_bangkok"]
    job_url = item.get("job_url", "")

    msg_text = generate_followup_message(role=title, company=company, is_bangkok=is_bangkok)

    buttons: list[list[dict[str, str]]] = []
    if recruiter:
        deeplink = build_telegram_deeplink(recruiter, msg_text)
        buttons.append([{"text": f"🚀 Написать фоллоу-ап @{recruiter}", "url": deeplink}])
    elif job_url:
        buttons.append([{"text": "📄 Открыть вакансию", "url": job_url}])

    callback_id = _followup_callback_id(stable_id)
    buttons.append([
        {"text": "✅ Фоллоу-ап отправлен", "callback_data": f"flw:{callback_id}"},
        {"text": "🔕 Больше не напоминать", "callback_data": f"flwmute:{callback_id}"},
    ])

    lang_badge = "🌏 Бангкок (English)" if is_bangkok else "🇷🇺 РФ (Удаленка)"
    card = (
        f"🔄 <b>Напоминание о фоллоу-апе ({lang_badge})</b>\n\n"
        f"💼 <b>Позиция:</b> {title}\n"
        f"🏢 <b>Компания:</b> {company}\n"
        f"⏳ <b>Статус:</b> Отклик отправлен ранее, ответа пока нет.\n\n"
        f"📝 <b>Готовое сообщение для повторного контакта:</b>\n"
        f"<blockquote>{msg_text}</blockquote>\n\n"
        f"💡 <i>Нажмите кнопку для открытия диалога или нажмите на цитату, чтобы скопировать текст!</i>"
    )

    return telegram_routing.send_job_message(
        card,
        parse_mode="HTML",
        reply_markup={"inline_keyboard": buttons},
        timeout=12,
    )


def _resolve_stable_id(identifier: str) -> Optional[str]:
    """Resolve a callback token (or full stable_id) to a real vacancies.stable_id.

    Long stable_ids are shortened in the Telegram callback, so the token may be
    either the full id or its suffix. The lookup happens exactly once, before
    any write: exact match first, then a unique suffix match. Missing or
    ambiguous identifiers resolve to ``None`` -- the caller then reports the
    truth instead of faking success.
    """
    if not identifier:
        return None
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT stable_id FROM vacancies WHERE stable_id = ? LIMIT 1",
            (identifier,),
        ).fetchone()
        if row is not None:
            return row[0]
        # LIKE treats % and _ as wildcards; a suffix token may legitimately
        # contain them (e.g. percent-encoded URLs).
        escaped = (
            identifier.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        matches = conn.execute(
            "SELECT stable_id FROM vacancies WHERE stable_id LIKE ? ESCAPE '\\' LIMIT 2",
            (f"%{escaped}",),
        ).fetchall()
    finally:
        conn.close()

    if len(matches) == 1:
        return matches[0][0]
    if not matches:
        logger.warning("follow-up callback id %r matched no vacancy", identifier)
    else:
        logger.warning(
            "follow-up callback id %r is ambiguous: %s",
            identifier,
            [m[0] for m in matches],
        )
    return None


def record_followup_sent(stable_id_or_suffix: str) -> bool:
    """Record that a follow-up was dispatched for this application.

    Writes to the real ``application_followups`` row (the callback token is
    resolved once, it is never stored as a key). Returns ``True`` only when the
    row was actually inserted/updated; ``False`` means the identifier could not
    be resolved and the caller must not claim success.
    """
    resolved = _resolve_stable_id(stable_id_or_suffix)
    if resolved is None:
        return False
    init_followup_table()
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor.execute("""
            INSERT INTO application_followups (stable_id, followup_count, last_followup_at, status)
            VALUES (?, 1, ?, 'SENT')
            ON CONFLICT(stable_id) DO UPDATE SET
                followup_count = COALESCE(followup_count, 0) + 1,
                last_followup_at = excluded.last_followup_at,
                status = 'SENT'
        """, (resolved, now_iso))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def mute_followup(stable_id_or_suffix: str) -> bool:
    """Mute future follow-ups for this application.

    Same resolution contract as ``record_followup_sent``: the MUTED status is
    written to the real row and ``False`` means nothing was persisted.
    """
    resolved = _resolve_stable_id(stable_id_or_suffix)
    if resolved is None:
        return False
    init_followup_table()
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO application_followups (stable_id, followup_count, status)
            VALUES (?, 0, 'MUTED')
            ON CONFLICT(stable_id) DO UPDATE SET status = 'MUTED'
        """, (resolved,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def dispatch_followup_batch(limit: int = 2) -> int:
    """Fetch and send top pending follow-ups to Mikhail."""
    items = find_pending_followups(limit=limit)
    dispatched = 0
    for it in items:
        if send_followup_card(ALLOWED_CHAT_ID, it):
            dispatched += 1
            time.sleep(1.0)
    return dispatched


if __name__ == "__main__":
    cnt = dispatch_followup_batch(limit=1)
    print(f"Dispatched {cnt} follow-up reminders.")
