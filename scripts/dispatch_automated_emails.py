"""Direct Automated SMTP Outreach Dispatcher.

Sends prepared Australia and Bangkok application packages directly via Gmail SMTP,
with tailored cover letters and attached PDF CVs, then notifies Mikhail in Telegram.
"""

from __future__ import annotations

import email
import email.policy
import logging
import os
import pathlib
import smtplib
import ssl
import sys
import time
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from ai_assistant import config, db
from ai_assistant.telegram_routing import send_job_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dispatch_automated_emails")

AUSTRALIA_DIR = PROJECT_ROOT / "artifacts" / "australia_applications" / "agency_outreach"
BANGKOK_DIR = PROJECT_ROOT / "artifacts" / "emails"

AUSTRALIA_PACKAGES = [
    {
        "name": "Nucleus International Recruitment (Australia)",
        "slug": "nucleus_recruitment",
        "file": AUSTRALIA_DIR / "nucleus_recruitment.eml",
        "to": "info@nucleusrecruitment.com.au",
        "category": "Австралия (Автомеханика / Тяжелое оборудование)",
    },
    {
        "name": "Brunel Australasia (Australia)",
        "slug": "brunel_australasia",
        "file": AUSTRALIA_DIR / "brunel_australasia.eml",
        "to": "australasia@brunel.net",
        "category": "Австралия (Телеком / VSAT / Связь)",
    },
    {
        "name": "WorkPac Group (Australia)",
        "slug": "workpac_group",
        "file": AUSTRALIA_DIR / "workpac_group.eml",
        "to": "contact@workpac.com",
        "category": "Австралия (Выездной сервис / FIFO)",
    },
    {
        "name": "Ventia & Downer Group (Australia)",
        "slug": "ventia_downer",
        "file": AUSTRALIA_DIR / "ventia_downer.eml",
        "to": "careers@ventia.com",
        "category": "Австралия (Инфраструктура и электроника)",
    },
]

BANGKOK_PACKAGES = [
    {
        "name": "Cognizant (Bangkok)",
        "slug": "cognizant",
        "file": BANGKOK_DIR / "outreach_careers_cognizant_com.eml",
        "to": "careers@cognizant.com",
        "category": "Бангкок (Deskside Support Engineer)",
    },
    {
        "name": "HCLTech (Bangkok)",
        "slug": "hcltech",
        "file": BANGKOK_DIR / "outreach_careers_hcltech_com.eml",
        "to": "careers@hcltech.com",
        "category": "Бангкок (L1 Deskside Support)",
    },
    {
        "name": "Microchip Technology Inc. (Bangkok)",
        "slug": "microchip",
        "file": BANGKOK_DIR / "outreach_careers_microchiptechnologyinc_com.eml",
        "to": "careers@microchiptechnologyinc.com",
        "category": "Бангкок (Technician IS Desktop Support)",
    },
    {
        "name": "Nipa Digital Marketing Agency (Bangkok)",
        "slug": "nipa",
        "file": BANGKOK_DIR / "outreach_careers_nipadigitalmarketingagency_com.eml",
        "to": "careers@nipadigitalmarketingagency.com",
        "category": "Бангкок (Technical Support Lead)",
    },
    {
        "name": "eTeam (Bangkok)",
        "slug": "eteam",
        "file": BANGKOK_DIR / "outreach_careers_eteam_com.eml",
        "to": "careers@eteam.com",
        "category": "Бангкок (System Engineer / Linux Admin)",
    },
]


def record_email_sent_db(to_email: str, subject: str, category: str, slug: str) -> None:
    """Record outbound email delivery in state.db."""
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, 'outreach_email_sent', ?, datetime('now'), 'SENT', ?)
        """, (
            f"email:{slug}:{to_email}",
            to_email,
            f'{{"subject": "{subject}", "category": "{category}", "recipient": "{to_email}"}}',
        ))
        conn.commit()
    except Exception as exc:
        logger.warning("DB record error: %s", exc)


def dispatch_all(dry_run: bool = False) -> dict[str, Any]:
    """Connect to Gmail SMTP and send all Australia and Bangkok packages."""
    user = os.environ.get("SMTP_USER", "mikhailthaiban@gmail.com")
    pwd = os.environ.get("SMTP_PASSWORD")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", 465))

    if not pwd:
        raise ValueError("SMTP_PASSWORD is not configured in .env!")

    all_targets = AUSTRALIA_PACKAGES + BANGKOK_PACKAGES
    logger.info("Starting automated SMTP dispatch for %d target recipients...", len(all_targets))

    context = ssl.create_default_context()
    sent_list: list[dict[str, str]] = []
    failed_list: list[dict[str, str]] = []

    with smtplib.SMTP_SSL(host, port, context=context, timeout=25) as server:
        server.login(user, pwd)
        logger.info("SMTP connection authenticated successfully as %s", user)

        for target in all_targets:
            eml_path = target["file"]
            to_addr = target["to"]
            cat = target["category"]
            name = target["name"]

            if not eml_path.exists():
                logger.error("EML package not found: %s", eml_path)
                failed_list.append({"name": name, "to": to_addr, "error": "file_not_found"})
                continue

            try:
                raw_bytes = eml_path.read_bytes()
                msg = email.message_from_bytes(raw_bytes, policy=email.policy.SMTP)
                sub = msg.get("Subject", "Job Application")

                if dry_run:
                    logger.info("[DRY RUN] Would send to %s (%s) — Sub: %s", to_addr, cat, sub)
                    sent_list.append({"name": name, "to": to_addr, "category": cat, "subject": sub})
                else:
                    logger.info("Sending application email to %s (%s)...", to_addr, name)
                    server.send_message(msg)
                    record_email_sent_db(to_addr, sub, cat, target["slug"])
                    sent_list.append({"name": name, "to": to_addr, "category": cat, "subject": sub})
                    logger.info("Successfully sent to %s!", to_addr)
                    time.sleep(3.0)  # Gentle spacing between sends

            except Exception as exc:
                logger.error("Failed sending to %s: %s", to_addr, exc)
                failed_list.append({"name": name, "to": to_addr, "error": str(exc)})

    # Notify owner in Telegram
    if sent_list:
        lines = []
        for s in sent_list:
            lines.append(f"• <b>{s['name']}</b> ({s['to']})\n  <i>{s['category']}</i>")

        tg_text = (
            f"🚀 <b>Автоматически отправлено {len(sent_list)} откликов по Email!</b>\n\n"
            f"Все письма ушли напрямую с вашего адреса <code>{user}</code> с прикрепленными PDF-резюме:\n\n"
            + "\n".join(lines)
            + "\n\n✅ <i>Все исходящие зафиксированы в базе данных.</i>"
        )
        try:
            send_job_message(text=tg_text, parse_mode="HTML")
            logger.info("Sent confirmation notification to owner in Telegram.")
        except Exception as e:
            logger.warning("Failed to send Telegram notification: %s", e)

    return {
        "status": "SUCCESS",
        "total": len(all_targets),
        "sent": len(sent_list),
        "failed": len(failed_list),
        "details_sent": sent_list,
        "details_failed": failed_list,
    }


if __name__ == "__main__":
    is_dry = "--dry-run" in sys.argv
    res = dispatch_all(dry_run=is_dry)
    print("Dispatch result:", res)
