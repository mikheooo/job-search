"""Console & Telegram Manifest for all pending, ready-to-send outbounds and user tasks.

Aggregates:
1. Priority User Action Tasks (tests, scheduling, questionnaire approvals)
2. Top 10 Freelance Gig Proposals (Kwork, Freelancer, Telegram) with A/B variant tracking
3. Pending Follow-up Messages (3-7 days post-application)
4. 15 Ready-to-Apply Vacancies with Tailored Cover Letters & Direct Links
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def generate_manifest() -> str:
    conn = sqlite3.connect("state.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    lines = []
    lines.append("=" * 70)
    lines.append("🚀 БОЕВОЙ МАНИФЕСТ ИСХОДЯЩИХ ОТКЛИКОВ, ПИТЧЕЙ И ЗАДАЧ (P1.1)")
    lines.append("=" * 70)

    # 1. User Action Tasks
    tasks = c.execute("SELECT * FROM user_action_tasks WHERE status = 'PENDING' ORDER BY priority, id").fetchall()
    lines.append(f"\n📋 1. ГОРЯЧИЕ ЗАДАЧИ ПОЛЬЗОВАТЕЛЯ ({len(tasks)} шт.):")
    for t in tasks:
        prio = t["priority"]
        key = t["task_key"]
        title = t["title"]
        desc = t["description"]
        url = t["action_url"] or ""
        lines.append(f"  • [{prio}] {title}")
        lines.append(f"    Детали: {desc}")
        if url:
            lines.append(f"    Ссылка: {url}")
        lines.append("")

    # 2. Top Gigs
    gigs = c.execute("SELECT * FROM top_gig_proposals WHERE status = 'PROPOSAL_READY' ORDER BY id").fetchall()
    lines.append(f"\n⚡ 2. ТОП ФРИЛАНС-ПИТЧИ К ОТПРАВКЕ ({len(gigs)} шт.):")
    for g in gigs:
        gid = g["gig_id"]
        src = g["source"]
        title = g["title"]
        budget = g["budget_text"]
        url = g["url"] or ""
        pitch = g["proposal_pitch"]
        lines.append(f"  • [{src.upper()}] «{title}» ({budget})")
        lines.append(f"    URL: {url}")
        first_line = pitch.split("\n")[0]
        lines.append(f"    Питч ({len(pitch)} зн): {first_line} ...")
        lines.append("")

    # 3. Follow-ups
    from ai_assistant.followup_manager import find_pending_followups, generate_followup_message
    followups = find_pending_followups()
    lines.append(f"\n⏳ 3. ПИСЬМА ДОЖИМА / FOLLOW-UP 3–7 ДНЕЙ ({len(followups)} шт.):")
    for f in followups:
        sid = f["stable_id"]
        title = f["title"]
        comp = f["company"]
        url = f["job_url"]
        msg = generate_followup_message(title, comp, f.get("recruiter"), f.get("is_bangkok", False))
        lines.append(f"  • {title} @ {comp} ({url})")
        lines.append(f"    Сообщение ({len(msg)} зн): {msg.splitlines()[0]} ...")
        lines.append("")

    # 4. Ready-to-Apply Vacancies
    pkgs = c.execute("""
        SELECT v.stable_id, v.source, v.title, v.company, v.job_url, v.location, a.match_score, p.package_json
        FROM vacancies v
        JOIN application_packages p ON v.stable_id = p.vacancy_stable_id
        LEFT JOIN application_tracking a ON v.stable_id = a.vacancy_stable_id
        WHERE v.state = 'READY_TO_APPLY'
        ORDER BY a.match_score DESC
    """).fetchall()

    lines.append(f"\n💼 4. ГОТОВЫЕ ВАКАНСИИ С СОПРОВОДИТЕЛЬНЫМИ ПИСЬМАМИ ({len(pkgs)} шт.):")
    for p in pkgs:
        sid = p["stable_id"]
        src = p["source"]
        title = p["title"]
        comp = p["company"] or "Компания"
        url = p["job_url"]
        score = p["match_score"]
        pkg_data = json.loads(p["package_json"]) if p["package_json"] else {}
        variant = pkg_data.get("variant_id", "direct_focus")
        cover = pkg_data.get("cover_letter", "")
        lines.append(f"  • [Score: {score}] «{title}» @ {comp} ({src})")
        lines.append(f"    URL: {url}")
        lines.append(f"    Стратегия A/B: {variant}")
        first_p = cover.split("\n\n")[0] if cover else ""
        lines.append(f"    Письмо ({len(cover)} зн): {first_p} ...")
        lines.append("")

    conn.close()
    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_manifest())
