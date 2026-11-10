#!/usr/bin/env python3
"""HH.ru Chat Monitor - Deterministic Script Runner for Hermes Cron.

Replaces the wasteful LLM-first agent polling with a zero-cost deterministic checker:
1. Searches Gmail for 'from:hh.ru is:unread' using Google Workspace API.
2. If no unread messages -> exits immediately ([SILENT], 0 tokens, 0 API keys used).
3. If unread messages exist:
   - Fetches message details.
   - Filters out simple rejections and template notifications (marks them as read silently).
   - If a complex recruiter message / interview invite is found:
     - Extracts company, role, text snippet, and chat URL.
     - Prepares a professional draft reply.
     - Delivers the notification to Mikhail via Telegram (@jobsassistbot) through notify_job_owner.py.
     - On successful delivery (status: delivered/duplicate), marks the email as read.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Configuration & Paths ---
HERMES_HOME = Path(r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs")
HERMES_PYTHON = Path(r"C:\Users\Misha\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe")
GOOGLE_API_SCRIPT = Path(r"C:\Users\Misha\AppData\Local\hermes\hermes-agent\skills\productivity\google-workspace\scripts\google_api.py")

JOB_SEARCH_ROOT = Path(r"C:\Users\Misha\Documents\job-search")
JOB_SEARCH_PYTHON = JOB_SEARCH_ROOT / ".venv" / "Scripts" / "python.exe"
NOTIFY_SCRIPT = JOB_SEARCH_ROOT / "scripts" / "notify_job_owner.py"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("hh_chat_monitor")


def _run_google_api(args: List[str]) -> tuple[int, str]:
    """Execute google_api.py under hermes python with required environment."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [str(HERMES_PYTHON), str(GOOGLE_API_SCRIPT)] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        logger.error(f"Failed to run google_api command {args}: {exc}")
        return 1, ""


def search_unread_hh_emails() -> List[Dict[str, Any]]:
    """Search for unread emails from hh.ru."""
    code, stdout = _run_google_api(["gmail", "search", "from:hh.ru is:unread", "--max", "5"])
    if code != 0 or not stdout:
        return []

    json_start = stdout.find("[")
    if json_start == -1:
        return []
    
    try:
        return json.loads(stdout[json_start:])
    except Exception as exc:
        logger.error(f"Failed to parse search JSON: {exc}, raw: {stdout[:200]}")
        return []


def get_email_details(message_id: str) -> Optional[Dict[str, Any]]:
    """Fetch full details for an email."""
    code, stdout = _run_google_api(["gmail", "get", message_id])
    if code != 0 or not stdout:
        return None

    json_start = stdout.find("{")
    if json_start == -1:
        return None
    
    try:
        return json.loads(stdout[json_start:])
    except Exception as exc:
        logger.error(f"Failed to parse message JSON for {message_id}: {exc}")
        return None


def mark_email_as_read(message_id: str) -> bool:
    """Remove UNREAD label from an email."""
    code, stdout = _run_google_api(["gmail", "modify", message_id, "--remove-label", "UNREAD"])
    if code == 0:
        logger.info(f"Marked email {message_id} as read.")
        return True
    logger.warning(f"Failed to mark email {message_id} as read: {stdout}")
    return False


def is_simple_rejection_or_notice(subject: str, snippet: str, body: str) -> bool:
    """Check if message is a standard rejection or automated delivery receipt."""
    text_to_check = f"{subject} {snippet}".lower()
    
    rejection_keywords = [
        "не готов пригласить",
        "отказ",
        "к сожалению",
        "мы получили ваше резюме",
        "ваше резюме доставлено",
        "просмотр резюме",
        "резюме просмотрено",
        "передали руководителю",
        "передали ваше резюме",
        "автоматическое сообщение",
        "статус отклика",
        "подходящие вакансии",
        "новые вакансии",
        "еженедельная рассылка"
    ]
    for kw in rejection_keywords:
        if kw in text_to_check:
            return True
    return False


def clean_html_text(raw_html: str) -> str:
    """Strip tags and unescape html entities."""
    clean = re.sub(r"<style.*?</style>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<script.*?</script>", "", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    return "\n".join(lines)


def extract_chat_link(body: str) -> str:
    """Find link to negotiations or vacancy chat."""
    match = re.search(r'https?://[^\s"\'<>]*hh\.ru/applicant/negotiations[^\s"\'<>]*', body)
    if match:
        return match.group(0)
    match_any = re.search(r'https?://[^\s"\'<>]*hh\.ru/[^\s"\'<>]+', body)
    if match_any:
        return match_any.group(0)
    return "https://hh.ru/applicant/negotiations"


def extract_company_and_role(subject: str, snippet: str, body_text: str) -> tuple[str, str]:
    """Attempt to extract company name and vacancy title."""
    company = "HeadHunter"
    role = "Вакансия"

    m_subj = re.search(r'[«"]([^»"]+)[»"]', subject)
    if m_subj:
        role = m_subj.group(1)

    m_comp = re.search(r'от\s+(?:компании\s+)?([А-ЯA-Z][A-Za-zА-Яа-я0-9\s\.\-]{2,30})', subject + " " + snippet)
    if m_comp:
        company = m_comp.group(1).strip()

    return company, role


def send_telegram_notification(
    event_key: str,
    company: str,
    role: str,
    chat_url: str,
    text: str
) -> bool:
    """Send notification via job-search notify_job_owner.py."""
    if not NOTIFY_SCRIPT.exists() or not JOB_SEARCH_PYTHON.exists():
        logger.error(f"Notify script or python missing: {NOTIFY_SCRIPT}, {JOB_SEARCH_PYTHON}")
        return False

    env = dict(os.environ)
    for leaked in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(leaked, None)

    cmd = [
        str(JOB_SEARCH_PYTHON),
        str(NOTIFY_SCRIPT),
        "--event-key", event_key,
        "--source", "hh_chat_monitor",
        "--company", company,
        "--role", role,
        "--chat-url", chat_url
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=20
        )
        if proc.returncode == 0:
            logger.info(f"Delivered notification for {event_key}: {proc.stdout.strip()}")
            return True
        logger.error(f"notify_job_owner failed ({proc.returncode}): {proc.stderr or proc.stdout}")
        return False
    except Exception as exc:
        logger.error(f"Exception sending notification: {exc}")
        return False


def main() -> int:
    logger.info("HH.ru Chat Monitor starting check...")
    unread_emails = search_unread_hh_emails()

    if not unread_emails:
        print("[SILENT] No unread messages from hh.ru.")
        return 0

    logger.info(f"Found {len(unread_emails)} unread emails from hh.ru.")
    complex_count = 0

    for item in unread_emails:
        msg_id = item.get("id")
        if not msg_id:
            continue

        subject = item.get("subject", "")
        snippet = item.get("snippet", "")

        # Quick check on subject/snippet
        if is_simple_rejection_or_notice(subject, snippet, ""):
            logger.info(f"Email {msg_id} is a simple notice/rejection ('{subject}'). Marking read.")
            mark_email_as_read(msg_id)
            continue

        # Fetch full message
        details = get_email_details(msg_id)
        if not details:
            continue

        raw_body = details.get("body", "")
        body_text = clean_html_text(raw_body)

        if is_simple_rejection_or_notice(subject, snippet, body_text):
            logger.info(f"Email {msg_id} full text classified as simple notice. Marking read.")
            mark_email_as_read(msg_id)
            continue

        # Complex message: Recruiter question, interview invitation, test task
        complex_count += 1
        company, role = extract_company_and_role(subject, snippet, body_text)
        chat_url = extract_chat_link(raw_body)

        recruiter_message = snippet if snippet else body_text[:200]

        draft_reply = (
            "Здравствуйте! Спасибо за отклик и приглашение. Я готов обсудить подробности "
            "и ответить на все интересующие вопросы. Подскажите, пожалуйста, удобное время для звонка."
        )

        notification_text = (
            f"🏢 **Новое сообщение от {company}**\n"
            f"💼 Вакансия: {role}\n"
            f"💬 *{recruiter_message}*\n"
            f"🔗 {chat_url}\n\n"
            f"🤖 **Предлагаю ответить:** {draft_reply}"
        )

        event_key = f"gmail:{msg_id}"
        success = send_telegram_notification(
            event_key=event_key,
            company=company,
            role=role,
            chat_url=chat_url,
            text=notification_text
        )

        if success:
            mark_email_as_read(msg_id)
        else:
            logger.warning(f"Notification not confirmed for {msg_id}; leaving email unread for manual review.")

    print(f"HH.ru Chat Monitor completed: {len(unread_emails)} checked, {complex_count} notifications dispatched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
