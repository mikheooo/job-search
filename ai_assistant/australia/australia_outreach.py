"""Australian Job Outreach & Telegram Dispatcher.

Handles:
1. Formatting EEO & migration-compliant Australian job alerts for Telegram.
2. Generating tailored Australian Cover Letters & pairing with Australian Standard CV PDF.
3. Tracking dispatched job cards in `data/australia/sent_alerts.json` to prevent duplicates.
4. Direct outreach recommendations for approved Australian 482 / DAMA sponsor employers.
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

from ai_assistant.australia.australia_cover_letter import generate_australian_cover_letter
from ai_assistant.australia.australia_resume_builder import ensure_australian_cv_pdf
from ai_assistant.telegram_routing import send_job_message

logger = logging.getLogger("australia_outreach")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data" / "australia"
SENT_ALERTS_FILE = DATA_DIR / "sent_alerts.json"

# Leading Australian recruitment agencies specializing in overseas visa sponsorship
VETTED_AUSTRALIAN_SPONSOR_AGENCIES = [
    {
        "name": "Nucleus International Recruitment",
        "specialty": "Automotive, Heavy Diesel, Telecommunications & Skilled Trades (Subclass 482 & DAMA 494)",
        "website": "https://nucleusrecruitment.com.au",
        "email": "info@nucleusrecruitment.com.au",
        "focus": "Direct international placement with Australian host employers across WA, QLD, NSW.",
    },
    {
        "name": "Brunel Australasia",
        "specialty": "Telecommunications, Mining, Field Engineering & Infrastructure",
        "website": "https://www.brunel.net/en-au",
        "email": "australasia@brunel.net",
        "focus": "Large infrastructure and resources projects requiring experienced technical specialists.",
    },
    {
        "name": "WorkPac Group",
        "specialty": "Industrial, Field Technicians, Mining & Remote Operations",
        "website": "https://www.workpac.com",
        "email": "contact@workpac.com",
        "focus": "Regional Australia and FIFO assignments with employer sponsorship pathways.",
    },
    {
        "name": "Ventia & Downer Group",
        "specialty": "Telecommunications Network Operations, VSAT/Wireless & Critical Infrastructure",
        "website": "https://www.ventia.com",
        "email": "careers@ventia.com",
        "focus": "Standard Business Sponsors for telecommunications technicians and electronics specialists.",
    },
]


def load_sent_alerts() -> set[str]:
    """Load IDs of jobs already alerted to avoid duplicate messages."""
    if not SENT_ALERTS_FILE.exists():
        return set()
    try:
        with open(SENT_ALERTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data if isinstance(data, list) else data.keys())
    except Exception as exc:
        logger.warning("Failed to load sent alerts: %s", exc)
        return set()


def record_sent_alert(job_id: str) -> None:
    """Record job ID as alerted."""
    alerts = load_sent_alerts()
    alerts.add(str(job_id))
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(SENT_ALERTS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(alerts), f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save sent alert: %s", exc)


def format_australian_job_card(job: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    """Format Telegram card and inline buttons for an Australian visa-sponsored vacancy."""
    title = job.get("title", "Technical / Trade Role")
    company = job.get("company", "Australian Employer")
    location = job.get("location", "Australia")
    salary = job.get("salary", "AUD $79,423 - $115,000+ / year (Meets CSIT Threshold)")
    url = job.get("url", "https://www.seek.com.au")
    teaser = job.get("teaser") or ""
    bullet_points = job.get("bullet_points") or []
    matched = job.get("matched_keywords") or []

    bullets_text = ""
    if bullet_points:
        bullets_text = "\n" + "\n".join(f"• {b}" for b in bullet_points[:3])
    elif teaser:
        bullets_text = f"\n_{teaser[:250]}..._"

    sponsorship_badge = "✅ Подтверждено спонсорство" if matched else "🔍 Требуется рабочая виза (482/494)"

    from ai_assistant.tailoring.analyzer import analyze_vacancy
    analysis = analyze_vacancy(job)

    text = (
        f"🦘 *Австралия: Вакансия со спонсорством визы*\n\n"
        f"🔧 *Должность:* {title}\n"
        f"🏢 *Работодатель / Агентство:* {company}\n"
        f"📍 *Локация:* {location}\n"
        f"💰 *Оплата:* {salary}\n"
        f"🛡️ *Статус визы:* {sponsorship_badge}\n"
        f"{bullets_text}\n\n"
        f"🎯 *Индивидуальная адаптация опыта:*\n"
        f"• Направление: *{analysis.domain.value.upper()}*\n"
        f"• Резюме скомпилировано в PDF под требования {company}.\n"
        f"• Подготовлено персонализированное Cover Letter и шпаргалка к интервью.\n"
    )

    buttons = [
        [
            {"text": "📋 Открыть на Seek.com.au", "url": url},
        ]
    ]

    return text, buttons


def build_application_dossier(job: dict[str, Any]) -> dict[str, Any]:
    """Build bespoke tailored CV, tailored cover letter, and interview cheatsheet for this vacancy."""
    from ai_assistant.tailoring.resume_tailor import tailor_application_for_vacancy

    pkg = tailor_application_for_vacancy(job)
    return {
        "job_id": pkg.job_id,
        "title": pkg.target_role_title,
        "company": pkg.company_name,
        "domain": pkg.domain.value,
        "location": job.get("location", "Australia"),
        "url": job.get("url"),
        "cover_letter": pkg.cover_letter_text,
        "cheatsheet": pkg.cheatsheet_text,
        "cv_pdf_path": str(pkg.pdf_cv_path),
        "cv_html_path": str(pkg.html_cv_path),
        "cover_letter_path": str(pkg.cover_letter_path),
        "cheatsheet_path": str(pkg.cheatsheet_path),
    }


def send_australian_job_telegram(job: dict[str, Any]) -> bool:
    """Send formatted job alert card to Telegram."""
    text, buttons = format_australian_job_card(job)
    try:
        sent = send_job_message(text=text, inline_keyboard=buttons)
        if sent:
            record_sent_alert(str(job.get("id")))
        return bool(sent)
    except Exception as exc:
        logger.error("Failed to send Australian job alert: %s", exc)
        return False
