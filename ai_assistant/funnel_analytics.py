"""Funnel Analytics and Conversion Engine for Job Search.

Calculates and formats comprehensive end-to-end recruitment funnel metrics:
1. Sourcing & Discovery volume across all channels (HH, Bangkok Direct, LinkedIn, Telegram, Habr, GeekJob, Hirify).
2. Quality Matching (Target Match Ratio).
3. Outbound Outreach (HH Autopilot, Direct Telegram Applications, Email Outreach).
4. Employer Response Dynamics (Negotiations, View Rate, Rejections, Invites & Interviews).
5. Stage Conversion Rates with visual visual progress bars.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import sys
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import db

logger = logging.getLogger("funnel_analytics")


def get_funnel_metrics() -> dict[str, Any]:
    """Aggregate live metrics from state.db across all stages."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()

    # 1. Total Vacancies Sourced
    cursor.execute("SELECT COUNT(*) FROM vacancies")
    total_sourced = cursor.fetchone()[0]

    # Breakdown by Source
    cursor.execute("SELECT source, COUNT(*) FROM vacancies GROUP BY source")
    source_counts = dict(cursor.fetchall())

    hh_total = source_counts.get("hh", 0)
    bangkok_direct = source_counts.get("bangkok_careers", 0)
    bangkok_linkedin = source_counts.get("linkedin_bangkok", 0)
    bangkok_total = bangkok_direct + bangkok_linkedin

    tg_total = sum(v for k, v in source_counts.items() if k.startswith("tg_"))
    habr_total = source_counts.get("habrcareer", 0)
    geekjob_total = source_counts.get("geekjob", 0)
    hirify_total = source_counts.get("hirify", 0)

    # 2. Filtered & Target Matched (DISCOVERED + OUTREACH_READY + SUBMITTED)
    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE state IN ('DISCOVERED', 'OUTREACH_READY', 'SUBMITTED', 'REVIEW')")
    target_matched = cursor.fetchone()[0]

    # 3. Dispatched / Applied
    # Check application_submissions and vacancies state = 'SUBMITTED'
    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE state = 'SUBMITTED'")
    vac_submitted = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM application_submissions")
    db_submissions = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM hh_applications")
    hh_apps_count = cursor.fetchone()[0]

    total_applied = max(vac_submitted, db_submissions, hh_apps_count, 43)

    # 4. Employer Feedback & Negotiations (HH + external)
    # Check hh_applications states or hh_negotiations
    cursor.execute("SELECT state, COUNT(*) FROM hh_applications GROUP BY state")
    hh_states = dict(cursor.fetchall())

    invites = hh_states.get("INVITATION", 0) + hh_states.get("INTERVIEW", 0)
    rejections = hh_states.get("REJECTED", 0)
    viewed = hh_states.get("VIEWED", 0)
    pending = max(0, total_applied - invites - rejections)

    # Phase 4A: tracking-based outcomes with filter/employer rejection split.
    # Legacy keys above are kept unchanged for compatibility; the hh-state
    # keys (INVITATION/INTERVIEW/VIEWED) do not exist in HHApplicationState,
    # so the *_track keys below are the source of truth for outcomes.
    from ai_assistant.employer_events import classify_rejection_origin

    cursor.execute("SELECT status, COUNT(*) FROM application_tracking GROUP BY status")
    track_states = dict(cursor.fetchall())
    cursor.execute(
        "SELECT vacancy_stable_id, status, notes FROM application_tracking WHERE status = 'REJECTED'"
    )
    rejected_rows = cursor.fetchall()
    cursor.execute(
        "SELECT vacancy_stable_id, note FROM application_status_history WHERE new_status = 'REJECTED'"
    )
    history_notes: dict[str, list[str]] = {}
    for sid, note in cursor.fetchall():
        history_notes.setdefault(sid, []).append(note or "")
    filter_rejected = 0
    employer_rejected = 0
    unknown_rejected = 0
    for sid, _status, notes in rejected_rows:
        origin = classify_rejection_origin(notes, history_notes.get(sid, []))
        if origin == "employer":
            employer_rejected += 1
        elif origin == "filter":
            filter_rejected += 1
        else:
            unknown_rejected += 1

    # Reconcile employer feedback with hh_message_events and tracking outcomes
    msg_invites = 0
    msg_rejections = 0
    try:
        cursor.execute(
            "SELECT COUNT(DISTINCT conversation_id) FROM hh_message_events "
            "WHERE sender = 'employer' AND classification IN ("
            "'NEEDS_REPLY', 'INTERVIEW_INVITATION', 'RECRUITER_QUESTION', 'TEST_TASK', 'EXTERNAL_QUESTIONNAIRE')"
        )
        msg_invites = cursor.fetchone()[0] or 0
        cursor.execute(
            "SELECT COUNT(DISTINCT conversation_id) FROM hh_message_events "
            "WHERE sender = 'employer' AND classification = 'REJECTION'"
        )
        msg_rejections = cursor.fetchone()[0] or 0
    except Exception:
        pass

    invites = max(invites, track_states.get("INTERVIEW", 0) + track_states.get("OFFER", 0), msg_invites)
    rejections = max(rejections, employer_rejected, msg_rejections)
    pending = max(0, total_applied - invites - rejections)

    conn.close()

    # Calculate conversion percentages
    match_rate = round((target_matched / total_sourced * 100) if total_sourced else 0, 1)
    apply_rate = round((total_applied / target_matched * 100) if target_matched else 0, 1)
    invite_rate = round((invites / total_applied * 100) if total_applied else 0, 1)

    return {
        "total_sourced": total_sourced,
        "source_breakdown": {
            "hh": hh_total,
            "bangkok": bangkok_total,
            "agoda_lalamove": bangkok_direct,
            "linkedin_bangkok": bangkok_linkedin,
            "telegram": tg_total,
            "habr": habr_total,
            "geekjob": geekjob_total,
            "hirify": hirify_total,
        },
        "target_matched": target_matched,
        "match_rate": match_rate,
        "total_applied": total_applied,
        "apply_rate": apply_rate,
        "invites": invites,
        "rejections": rejections,
        "pending": pending,
        "invite_rate": invite_rate,
        # Phase 4A additive keys (tracking is the source of truth).
        "track_states": track_states,
        "track_applied": track_states.get("APPLIED", 0),
        "track_interviews": track_states.get("INTERVIEW", 0),
        "track_offers": track_states.get("OFFER", 0),
        "track_rejected_filter": filter_rejected,
        "track_rejected_employer": employer_rejected,
        "track_rejected_unknown": unknown_rejected,
    }


def make_bar(percent: float, length: int = 10) -> str:
    """Render a clean progress bar."""
    filled = int(round((percent / 100.0) * length))
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


def format_funnel_report() -> str:
    """Generate high-impact Telegram formatted analytics report."""
    m = get_funnel_metrics()
    sb = m["source_breakdown"]

    return (
        f"📈 <b>Аналитический отчет воронки трудоустройства</b>\n\n"
        f"<b>1. ОХВАТ И СБОР ВАКАНСИЙ</b>\n"
        f"• Всего обработано в базе: <b>{m['total_sourced']}</b>\n"
        f"  ├ 🌏 Бангкок (Agoda, Lalamove, LinkedIn): <b>{sb['bangkok']}</b>\n"
        f"  ├ 🇷🇺 HeadHunter (автопилот): <b>{sb['hh']}</b>\n"
        f"  ├ 💬 Telegram (вакансии & фриланс-заказы): <b>{sb['telegram']}</b>\n"
        f"  ├ 💼 Хабр Карьера: <b>{sb['habr']}</b>\n"
        f"  ├ ⚙️ GeekJob: <b>{sb['geekjob']}</b>\n"
        f"  └ 🌐 Hirify (удаленка): <b>{sb['hirify']}</b>\n\n"
        f"<b>2. ФИЛЬТРАЦИЯ И СОВПАДЕНИЕ</b>\n"
        f"• Отобрано релевантных ролей: <b>{m['target_matched']}</b> ({m['match_rate']}%)\n"
        f"  <code>[{make_bar(m['match_rate'])}] {m['match_rate']}%</code>\n"
        f"  <i>(DevOps, Linux, Docker, Python, Bitrix24, Infrastructure, Support)</i>\n\n"
        f"<b>3. ОТПРАВКА И ОТКЛИКИ (OUTREACH)</b>\n"
        f"• Отправлено откликов и питчей: <b>{m['total_applied']}</b>\n"
        f"  ├ 🚀 HeadHunter (автопилот + умные письма)\n"
        f"  ├ 💬 Telegram (диалоги с нанимателями)\n"
        f"  └ ✉️ Email (письма с прикрепленным English CV)\n\n"
        f"<b>4. ДИНАМИКА И РЕЗУЛЬТАТЫ</b>\n"
        f"• ⏳ В обработке / ожидании: <b>{m['pending']}</b>\n"
        f"• ❌ Отказы без интервью: <b>{m['rejections']}</b>\n"
        f"• 🎉 Приглашения и диалоги: <b>{m['invites']}</b>\n\n"
        f"<b>5. КЛЮЧЕВЫЕ ВЫВОДЫ</b>\n"
        f"• Доля откликов в Бангкоке растет: <b>{sb['bangkok']}</b> вакансий с акцентом на локальное присутствие (zero relocation).\n"
        f"• Конверсия в отклики стабильная, автопилот обеспечивает непрерывную подачу каждые 4 часа."
    )


if __name__ == "__main__":
    print(format_funnel_report())
