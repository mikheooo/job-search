"""
Weekly Operational Metrics Module (OP-MET-001)

Computes 7-day performance metrics for job application operations:
- Applications submitted in window (total and by source)
- Employer responses in window
- Interview invitations in window
- Funnel conversion: applications -> responses -> interviews
- Top-5 rejection reasons parsed from employer message texts
- Formats weekly digest for Telegram channel (@remotejobd)
- Dispatches weekly digest with idempotency guard
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
from typing import Any

from . import config, db, outbound_governor
from .outbound_governor import OutboundAction
from .telegram_routing import JobRoute, send_job_message_result

logger = logging.getLogger(__name__)

# Rejection category classification rules
REJECTION_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "Вакансия закрыта / на паузе",
        [
            r"закрыли позицию",
            r"позиция закрыта",
            r"вакансия закрыта",
            r"вакансия уже закрыта",
            r"поиск приостановлен",
            r"приостановили поиск",
            r"набор закрыт",
            r"снята с публикации",
        ],
    ),
    (
        "Выбран другой кандидат",
        [
            r"другими кандидатами",
            r"другого кандидата",
            r"в пользу другого",
            r"в пользу другого кандидата",
            r"другие кандидаты",
            r"сделали выбор в пользу",
            r"остановили выбор",
        ],
    ),
    (
        "Несоответствие стека / опыта",
        [
            r"недостаточно опыта",
            r"релевантного опыта",
            r"требуется опыт",
            r"требованиям вакансии",
            r"по стеку",
            r"квалификаци",
            r"навык",
        ],
    ),
    (
        "Локация / формат работы / гражданство",
        [
            r"локаци",
            r"гражданств",
            r"релокац",
            r"только в офис",
            r"не подходит формат",
            r"удаленный формат не",
        ],
    ),
    (
        "Стандартный вежливый отказ",
        [
            r"не готовы пригласить",
            r"не готовы предложить",
            r"не можем предложить",
            r"отклонен",
            r"к сожалению, отказ",
            r"не готовы продолжить",
            r"решили не продолжать",
        ],
    ),
]


def classify_rejection_reason(text: str | None) -> str:
    """Classify employer rejection text into a categorized bucket."""
    if not text:
        return "Без текста / системный отказ"
    clean = text.lower().strip()
    for category, patterns in REJECTION_PATTERNS:
        for pat in patterns:
            if re.search(pat, clean, re.IGNORECASE):
                return category
    return "Прочее / без детализации"


def compute_weekly_metrics(
    days: int = 7,
    ref_date: datetime | None = None,
) -> dict[str, Any]:
    """Compute application and conversion metrics for the specified window."""
    now = ref_date or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    db.init_db()
    conn = db.get_connection()
    try:
        # 1. Outbound submissions
        # Query application_submissions
        sub_rows = conn.execute(
            """
            SELECT vacancy_stable_id, submitted_at, status
            FROM application_submissions
            WHERE status IN ('SUBMITTED', 'VERIFIED', 'APPLIED', 'CONFIRMED')
            """
        ).fetchall()

        submitted_ids: set[str] = set()
        by_source: dict[str, int] = {}

        for sid, sat, st in sub_rows:
            if not sat or sat.startswith("{"):
                continue
            try:
                dt = datetime.fromisoformat(sat.replace("Z", "+00:00"))
                if dt >= cutoff:
                    submitted_ids.add(sid)
            except Exception:
                continue

        # Also check application_tracking for submissions recorded directly or via applied_at
        track_rows = conn.execute(
            """
            SELECT vacancy_stable_id, source, applied_at, last_status_change_at, status
            FROM application_tracking
            WHERE status IN ('SUBMITTED', 'APPLIED')
            """
        ).fetchall()

        for sid, src, applied_at, last_ch, st in track_rows:
            target_ts = applied_at or last_ch
            if not target_ts:
                continue
            try:
                dt = datetime.fromisoformat(target_ts.replace("Z", "+00:00"))
                if dt >= cutoff:
                    submitted_ids.add(sid)
            except Exception:
                continue

        # Determine sources for all submitted_ids
        for sid in submitted_ids:
            row = conn.execute(
                "SELECT source FROM application_tracking WHERE vacancy_stable_id = ?",
                (sid,),
            ).fetchone()
            src = row[0] if row and row[0] else None
            if not src:
                src = sid.split(":", 1)[0] if ":" in sid else "other"
            src_key = src.lower()
            by_source[src_key] = by_source.get(src_key, 0) + 1

        total_submitted = len(submitted_ids)

        # 2. Employer responses
        resp_rows = conn.execute(
            """
            SELECT conversation_id, text, classification, seen_at, employer
            FROM hh_message_events
            WHERE sender = 'employer' AND seen_at >= ?
            """,
            (cutoff_iso,),
        ).fetchall()

        total_responses = len(resp_rows)
        response_classifications: dict[str, int] = {}
        for row in resp_rows:
            cls_name = row[2] or "UNCLASSIFIED"
            response_classifications[cls_name] = response_classifications.get(cls_name, 0) + 1

        # 3. Interviews
        int_rows = conn.execute(
            """
            SELECT id, vacancy_stable_id, conversation_id, detected_at
            FROM interview_events
            WHERE detected_at >= ?
            """,
            (cutoff_iso,),
        ).fetchall()

        interview_keys: set[str] = set()
        for r in int_rows:
            key = r[1] or r[2] or str(r[0])
            interview_keys.add(key)

        for r in resp_rows:
            if r[2] == "INTERVIEW_INVITATION":
                key = r[0] or "msg_interview"
                interview_keys.add(key)

        total_interviews = len(interview_keys)

        # 4. Conversion rates
        rate_submit_to_response = (
            round((total_responses / total_submitted) * 100, 1)
            if total_submitted > 0
            else 0.0
        )
        rate_response_to_interview = (
            round((total_interviews / total_responses) * 100, 1)
            if total_responses > 0
            else 0.0
        )
        rate_overall = (
            round((total_interviews / total_submitted) * 100, 1)
            if total_submitted > 0
            else 0.0
        )

        # 5. Top 5 rejection reasons
        rejection_texts = [r[1] for r in resp_rows if r[2] == "REJECTION"]
        rejection_counts: dict[str, int] = {}
        for txt in rejection_texts:
            cat = classify_rejection_reason(txt)
            rejection_counts[cat] = rejection_counts.get(cat, 0) + 1

        sorted_rejections = sorted(
            rejection_counts.items(), key=lambda x: x[1], reverse=True
        )
        top5_rejections = [
            {
                "reason": r,
                "count": c,
                "percent": round((c / len(rejection_texts)) * 100, 1) if rejection_texts else 0.0,
            }
            for r, c in sorted_rejections[:5]
        ]

        return {
            "period_days": days,
            "window_start": cutoff.strftime("%Y-%m-%d"),
            "window_end": now.strftime("%Y-%m-%d"),
            "computed_at": now.isoformat(),
            "applications_submitted": {
                "total": total_submitted,
                "by_source": by_source,
            },
            "employer_responses": {
                "total": total_responses,
                "by_classification": response_classifications,
            },
            "interviews_scheduled": {
                "total": total_interviews,
            },
            "conversion": {
                "submit_to_response_pct": rate_submit_to_response,
                "response_to_interview_pct": rate_response_to_interview,
                "overall_conversion_pct": rate_overall,
            },
            "rejections": {
                "total_in_period": len(rejection_texts),
                "top_reasons": top5_rejections,
            },
        }
    finally:
        conn.close()


def format_weekly_metrics_digest(metrics: dict[str, Any]) -> str:
    """Format structured metrics dictionary into human-readable Markdown for Telegram."""
    w_start = metrics.get("window_start", "")
    w_end = metrics.get("window_end", "")
    days = metrics.get("period_days", 7)

    app_data = metrics.get("applications_submitted", {})
    total_apps = app_data.get("total", 0)
    by_src = app_data.get("by_source", {})

    resp_data = metrics.get("employer_responses", {})
    total_resp = resp_data.get("total", 0)

    int_data = metrics.get("interviews_scheduled", {})
    total_int = int_data.get("total", 0)

    conv = metrics.get("conversion", {})
    s_to_r = conv.get("submit_to_response_pct", 0.0)
    r_to_i = conv.get("response_to_interview_pct", 0.0)
    overall = conv.get("overall_conversion_pct", 0.0)

    rej = metrics.get("rejections", {})
    top_rej = rej.get("top_reasons", [])

    lines = [
        "📊 *Еженедельный дайджест поиска работы*",
        f"Период: {w_start} — {w_end} ({days} дн.)",
        "",
        "📤 *Отклики:*",
        f"• Всего отправлено: *{total_apps}*",
    ]
    if by_src:
        lines.append("• По источникам:")
        source_labels = {
            "hh": "HeadHunter (hh.ru)",
            "habrcareer": "Хабр Карьера",
            "linkedin": "LinkedIn",
            "remoteok": "RemoteOK",
            "weworkremotely": "WeWorkRemotely",
        }
        for s_key, cnt in sorted(by_src.items(), key=lambda x: x[1], reverse=True):
            lbl = source_labels.get(s_key, s_key.upper())
            lines.append(f"  — {lbl}: *{cnt}*")
    else:
        lines.append("• По источникам: откликов не было")

    lines.extend([
        "",
        "📥 *Обратная связь:*",
        f"• Ответов работодателей: *{total_resp}*",
        f"• Назначено интервью: *{total_int}*",
        "",
        "📈 *Конверсия:*",
        f"• Отклик → Ответ: *{s_to_r}%*",
        f"• Ответ → Интервью: *{r_to_i}%*",
        f"• Общая конверсия (отклик → интервью): *{overall}%*",
        "",
        "🚫 *Топ причин отказов:*",
    ])

    if top_rej:
        for idx, item in enumerate(top_rej, start=1):
            r_name = item["reason"]
            cnt = item["count"]
            pct = item["percent"]
            lines.append(f"{idx}. {r_name}: *{cnt}* ({pct}%)")
    else:
        lines.append("Отказов за указанный период не зафиксировано.")

    return "\n".join(lines)


def send_weekly_metrics_digest(
    channel_id: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Send weekly metrics digest to Telegram channel (@remotejobd).
    Enforces outbound governor, checks idempotency via telegram_delivery_records.
    """
    allowed, reason = outbound_governor.check_outbound_allowed(OutboundAction.TELEGRAM)
    if not allowed:
        logger.warning(f"Telegram send blocked by outbound governor: {reason}")
        return {"delivered": False, "reason": f"Blocked by governor: {reason}"}

    data = metrics or compute_weekly_metrics(days=7)
    text = format_weekly_metrics_digest(data)

    target_channel = (
        channel_id
        or os.getenv("TELEGRAM_CHANNEL_ID")
        or getattr(config, "TELEGRAM_CHANNEL_ID", "")
        or "-1004399255305"
    ).strip()
    delivery_key = f"weekly_metrics_{data['window_start']}_{data['window_end']}"

    if dry_run:
        logger.info(f"[DRY RUN] Would send weekly digest to {target_channel}:\n{text}")
        return {
            "delivered": True,
            "dry_run": True,
            "delivery_key": delivery_key,
            "channel": target_channel,
            "text": text,
        }

    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("HH_TELEGRAM_BOT_TOKEN")
        or getattr(config, "TELEGRAM_BOT_TOKEN", "")
        or getattr(config, "HH_TELEGRAM_BOT_TOKEN", "")
    ).strip()

    if not token:
        logger.error("No Telegram bot token found for weekly digest delivery.")
        return {"delivered": False, "reason": "No Telegram bot token configured"}

    db.init_db()
    if not force:
        with db.db_session(commit=False) as conn:
            existing = conn.execute(
                """
                SELECT status, delivered_at FROM telegram_delivery_records
                WHERE delivery_key = ? AND status = 'DELIVERED'
                """,
                (delivery_key,),
            ).fetchone()
            if existing:
                logger.info(f"Weekly digest {delivery_key} already delivered at {existing[1]}. Skipping.")
                return {
                    "delivered": False,
                    "reason": f"Already delivered at {existing[1]} (use --force to resend)",
                    "delivery_key": delivery_key,
                }

    route = JobRoute(token=token, chat_id=target_channel)
    result = send_job_message_result(text, route=route, parse_mode="Markdown")

    now_iso = datetime.now(timezone.utc).isoformat()
    status = "DELIVERED" if result.ok else "FAILED"
    payload_str = json.dumps({"metrics": data, "error": result.error}, ensure_ascii=False)

    with db.db_session(commit=True) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO telegram_delivery_records
            (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (delivery_key, "WEEKLY_METRICS_DIGEST", target_channel, now_iso, status, payload_str),
        )

    if result.ok:
        logger.info(f"Weekly digest successfully delivered to {target_channel} (key={delivery_key})")
        return {"delivered": True, "delivery_key": delivery_key, "channel": target_channel}
    else:
        logger.error(f"Weekly digest delivery failed: {result.error}")
        return {"delivered": False, "delivery_key": delivery_key, "error": result.error}
