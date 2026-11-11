"""Email Outreach Engine with Direct SMTP & Attached PDF Resume.

Features:
1. Automated bilingual email drafting (Russian for CIS remote, English for Bangkok/International).
2. Automatic attachment of high-impact PDF resume (Mikhail_Kolesnikov_CV_Bangkok.pdf).
3. Direct SMTP dispatch via Gmail (smtp.gmail.com:465) when SMTP_PASSWORD is set.
4. Generates standard .eml message package for instant 1-click opening in any mail app.
5. Builds mobile mailto: deep links for quick manual review and sending.
"""

from __future__ import annotations

import email.message
import email.mime.application
import email.mime.multipart
import email.mime.text
import email.policy
import json
import logging
import os
import pathlib
import re
import smtplib
import urllib.parse
from email.message import EmailMessage
from typing import Any, Optional

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

from ai_assistant.bangkok_resume_builder import generate_bangkok_cv
from ai_assistant.it_resume_builder import generate_it_cv
from ai_assistant.smart_experience_generator import (
    generate_english_cover_letter,
    generate_tailored_cover_letter,
)

logger = logging.getLogger("email_outreach")

CANDIDATE_NAME = "Михаил Колесников"
CANDIDATE_NAME_EN = "Mikhail Kolesnikov"
DEFAULT_SENDER_EMAIL = "mikhailthaiban@gmail.com"
CANDIDATE_PHONE = "+7 (993) 339-76-28"
CANDIDATE_TELEGRAM = "@brokeassh"
GITHUB_LINK = "https://github.com/mikheooo"

EMAILS_DIR = PROJECT_ROOT / "artifacts" / "emails"
EMAILS_DIR.mkdir(parents=True, exist_ok=True)


def extract_emails(text: str) -> list[str]:
    """Extract valid recruiter email addresses from text, excluding generic/system domains."""
    raw_emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
    cleaned = []
    excluded = ["example.com", "hh.ru", "telegram.org", "sentry.io", "w3.org", "schema.org"]
    for e in raw_emails:
        e_clean = e.lower().strip(".,;:\"'()<>")
        domain = e_clean.split("@")[-1]
        if not any(ex in domain for ex in excluded) and len(e_clean) > 5:
            cleaned.append(e_clean)
    return list(dict.fromkeys(cleaned))


def get_default_cv_path(is_bangkok: bool = False) -> pathlib.Path:
    """Return path to CV PDF based on geography: Russian IT CV for CIS/Remote, English for Bangkok/Intl."""
    if is_bangkok:
        cv_path = PROJECT_ROOT / "artifacts" / "Mikhail_Kolesnikov_CV_Bangkok.pdf"
        if not cv_path.exists() or cv_path.stat().st_size == 0:
            cv_path = generate_bangkok_cv()
        return cv_path
    else:
        cv_path = PROJECT_ROOT / "artifacts" / "Mikhail_Kolesnikov_CV_IT_Engineer.pdf"
        if not cv_path.exists() or cv_path.stat().st_size == 0:
            cv_path = generate_it_cv()
        return cv_path


def generate_email_draft(
    role: str,
    company: str,
    description: str = "",
    is_bangkok: bool = False,
) -> dict[str, str]:
    """Generate tailored subject and body for an email application (RU or EN)."""
    if is_bangkok:
        subject = f"Application for {role} — {CANDIDATE_NAME_EN} (Bangkok-based)"
        body = generate_english_cover_letter(role=role, company=company, description=description)
    else:
        subject = f"Отклик на вакансию «{role}» — {CANDIDATE_NAME}"
        letter = generate_tailored_cover_letter(role=role, company=company, description=description)
        signature = (
            f"\n\n---\n"
            f"С уважением,\n"
            f"{CANDIDATE_NAME}\n"
            f"Телефон / WhatsApp: {CANDIDATE_PHONE}\n"
            f"Telegram: {CANDIDATE_TELEGRAM}\n"
            f"Email: {DEFAULT_SENDER_EMAIL}\n"
            f"GitHub: {GITHUB_LINK}\n"
            f"Формат: 100% удаленная работа (РФ, часовой пояс МСК, оформление ТК РФ / ИП / СЗ)"
        )
        body = letter.strip() + signature

    return {
        "subject": subject,
        "body": body,
    }


def build_mailto_link(to_email: str, subject: str, body: str) -> str:
    """Build a mailto: URL with prefilled recipient, subject and body."""
    params = {
        "subject": subject,
        "body": body,
    }
    return f"mailto:{to_email}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"


def create_eml_package(
    to_email: str,
    subject: str,
    body: str,
    attachment_path: Optional[pathlib.Path] = None,
    slug: str = "outreach",
    is_bangkok: bool = False,
) -> pathlib.Path:
    """Create a standard RFC-822 .eml file containing message and attached CV."""
    msg = EmailMessage(policy=email.policy.SMTP)
    sender = os.environ.get("SMTP_USER", DEFAULT_SENDER_EMAIL)
    msg["From"] = f"{CANDIDATE_NAME_EN} <{sender}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    att = attachment_path or get_default_cv_path(is_bangkok=is_bangkok)
    if att.exists():
        pdf_bytes = att.read_bytes()
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=att.name,
        )

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{slug}_{to_email}")[:50]
    out_path = EMAILS_DIR / f"{safe_name}.eml"
    out_path.write_bytes(msg.as_bytes())
    logger.info(f"Created .eml package at {out_path}")
    return out_path


def send_owner_email_alert(to_email: str, role: str, company: str) -> bool:
    """Send a quiet post-factum notification to owner when an email is sent."""
    from ai_assistant import config
    bot_token = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
    owner_id = getattr(config, "TELEGRAM_OWNER_ID", None) or config.HH_TELEGRAM_CHAT_ID or config.TELEGRAM_CHAT_ID
    if not bot_token or not owner_id:
        return False
    text = f"✉️ Отправлено письмо на email {to_email} по вакансии «{role}» ({company})"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": owner_id,
        "text": text,
        "disable_notification": True,
    }
    try:
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Failed to send email alert to owner: {e}")
        return False


def get_daily_email_count() -> int:
    """Return count of emails sent today from database tracking."""
    try:
        from ai_assistant import db
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT count(*) FROM telegram_delivery_records
            WHERE notification_type = 'outreach_email_sent'
              AND delivered_at >= date('now', 'start of day')
        """)
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.warning(f"Could not check daily email count: {e}")
        return 0


def record_email_sent(to_email: str, role: str, company: str, stable_id: str = "") -> None:
    """Record successfully sent email in telegram_delivery_records."""
    try:
        from ai_assistant import db
        import datetime
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, 'outreach_email_sent', ?, datetime('now'), 'SENT', ?)
        """, (
            f"email:{to_email}:{datetime.date.today().isoformat()}",
            to_email,
            json.dumps({"role": role, "company": company, "stable_id": stable_id}, ensure_ascii=False),
        ))
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not record email sent in DB: {e}")


def send_smtp_email(
    to_email: str,
    subject: str,
    body: str,
    attachment_path: Optional[pathlib.Path] = None,
    is_bangkok: bool = False,
) -> tuple[bool, str]:
    """Send email directly via SMTP (e.g. Gmail SSL port 465) with attached CV PDF."""
    from ai_assistant.outbound_governor import OutboundAction, check_outbound_allowed

    allowed, reason = check_outbound_allowed(OutboundAction.EMAIL)
    if not allowed:
        logger.warning("SMTP email send refused by outbound governor: %s", reason)
        return False, f"Отправка email заблокирована предохранителем (kill-switch): {reason}"

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", DEFAULT_SENDER_EMAIL)
    password = os.environ.get("SMTP_PASSWORD")

    if not password:
        return False, "SMTP_PASSWORD не задан в .env. Для отправки через Gmail укажите пароль приложения Google."

    msg = email.mime.multipart.MIMEMultipart()
    msg["From"] = f"{CANDIDATE_NAME_EN} <{user}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

    att = attachment_path or get_default_cv_path(is_bangkok=is_bangkok)
    if att.exists():
        part = email.mime.application.MIMEApplication(att.read_bytes(), Name=att.name)
        part["Content-Disposition"] = f'attachment; filename="{att.name}"'
        msg.attach(part)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)
        logger.info(f"Successfully sent email to {to_email} via SMTP ({host}:{port})")
        return True, f"Письмо успешно отправлено на {to_email} с прикрепленным резюме!"
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False, f"Ошибка отправки через SMTP: {e}"


def dispatch_email_outreach(
    to_email: str,
    role: str,
    company: str,
    description: str = "",
    is_bangkok: bool = False,
    attempt_smtp: bool = True,
    stable_id: str = "",
) -> dict[str, Any]:
    """Orchestrate complete email outreach: draft + CV attachment + SMTP or EML."""
    draft = generate_email_draft(role=role, company=company, description=description, is_bangkok=is_bangkok)
    subject = draft["subject"]
    body = draft["body"]

    cv_path = get_default_cv_path(is_bangkok=is_bangkok)
    eml_path = create_eml_package(to_email=to_email, subject=subject, body=body, attachment_path=cv_path, is_bangkok=is_bangkok)
    mailto_url = build_mailto_link(to_email=to_email, subject=subject, body=body)

    smtp_sent = False
    smtp_message = ""
    if attempt_smtp and os.environ.get("SMTP_PASSWORD"):
        daily_count = get_daily_email_count()
        if daily_count >= 5:
            smtp_message = f"Дневной лимит отправки писем ({daily_count}/5) достигнут. Сформирован .eml файл."
            logger.info(smtp_message)
        else:
            smtp_sent, smtp_message = send_smtp_email(to_email, subject, body, attachment_path=cv_path, is_bangkok=is_bangkok)
            if smtp_sent:
                record_email_sent(to_email, role, company, stable_id=stable_id)
                send_owner_email_alert(to_email, role, company)
    else:
        smtp_message = "SMTP_PASSWORD не настроен в .env — сформирован .eml файл и ссылка mailto:"

    return {
        "to_email": to_email,
        "subject": subject,
        "body": body,
        "cv_path": str(cv_path),
        "eml_path": str(eml_path),
        "mailto_url": mailto_url,
        "smtp_sent": smtp_sent,
        "status_message": smtp_message,
    }


if __name__ == "__main__":
    res = dispatch_email_outreach(
        to_email="hr@example.com",
        role="Senior DevOps Engineer",
        company="Agoda",
        is_bangkok=True,
        attempt_smtp=False,
    )
    print("Draft generated successfully:")
    print("Subject:", res["subject"])
    print("EML Path:", res["eml_path"])
    print("CV Path:", res["cv_path"])
