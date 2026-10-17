"""Test Task Manager.

Extracts, structures, and manages technical test tasks from HeadHunter employer chats into
artifacts/pending_test_tasks.md and formulates polite acknowledgement messages.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("test_task_manager")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = PROJECT_ROOT / "artifacts" / "pending_test_tasks.md"


def extract_test_task_links(text: str) -> list[str]:
    """Find external links (GitHub, Google Docs/Sheets/Drive, Yandex Disk, Notion) in message."""
    matches = re.findall(r'https?://[^\s<>"]+', text)
    valid_links = []
    for m in matches:
        clean = m.rstrip(".,;:!?)")
        if any(d in clean.lower() for d in [
            "github.com", "gitlab.com", "notion.site", "notion.so",
            "drive.google.com", "docs.google.com", "forms.gle", "forms.yandex.ru",
            "disk.yandex.ru", "yadi.sk", "t.me", "figma.com", "jira", "confluence"
        ]):
            valid_links.append(clean)
        elif "hh.ru" not in clean:
            valid_links.append(clean)
    return valid_links


def record_pending_test_task(
    company: str,
    vacancy_title: str = "",
    task_text: str = "",
    url: str = "",
    conversation_id: str = "",
    deadline: str = "",
    status: str = "⏳ **В работе**",
    **kwargs: Any,
) -> None:
    """Save or update structured test task in artifacts/pending_test_tasks.md."""
    vacancy_title = vacancy_title or kwargs.get("vacancy") or "Python Role"
    task_text = task_text or kwargs.get("message_text") or kwargs.get("task_description") or ""
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    links = extract_test_task_links(task_text)
    if url and url not in links:
        links.insert(0, url)

    links_md = ", ".join(f"[Ссылка]({l})" for l in links) if links else "В тексте сообщения"

    status_md = status or "⏳ **В работе**"
    if "**" not in status_md:
        status_md = f"**{status_md}**"

    # Read existing content if present
    content = ""
    if ARTIFACT_PATH.exists():
        try:
            content = ARTIFACT_PATH.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read existing test tasks file: {e}")

    # Check if this company & vacancy is already recorded
    identifier = f"{company} — {vacancy_title}".strip()
    if identifier.lower() in content.lower():
        logger.info(f"Test task for '{identifier}' already in {ARTIFACT_PATH}")
        if "✅" in status_md and "⏳" in content:
            updated_content = re.sub(
                rf"(\|\s*\*\*{re.escape(company)}\*\*\s*\|\s*{re.escape(vacancy_title)}.*?\|\s*)⏳\s*\*\*В работе\*\*",
                rf"\g<1>{status_md}",
                content,
                flags=re.IGNORECASE,
            )
            if updated_content != content:
                try:
                    ARTIFACT_PATH.write_text(updated_content, encoding="utf-8")
                    logger.info(f"Updated status to {status_md} for '{identifier}' in {ARTIFACT_PATH}")
                except Exception as e:
                    logger.error(f"Failed to update test task artifact: {e}")
        return

    # If new file, initialize header
    if not content.strip():
        header = (
            "# 📋 Актуальные тестовые задания (HeadHunter)\n\n"
            f"*Сформировано автоматически системой мониторинга откликов.*\n"
            f"*Последнее обновление: {now_str}*\n\n"
            "## Сводная таблица\n\n"
            "| Компания | Вакансия | Дата получения | Ссылки / Материалы | Статус |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
        )
        content = header

    # Append to table
    chat_link_md = f"[Чат HH](https://hh.ru/chat/{conversation_id})" if conversation_id else "HH"
    table_row = f"| **{company}** | {vacancy_title} | {now_str} | {links_md} | {status_md} |\n"

    # Insert row before detailed section if exists
    if "## Детальные описания заданий" in content:
        parts = content.split("## Детальные описания заданий", 1)
        content = parts[0] + table_row + "\n## Детальные описания заданий" + parts[1]
    else:
        content += table_row + "\n---\n\n## Детальные описания заданий\n\n"

    # Append detailed section
    clean_task_text = task_text.replace("\n", "\n> ")
    deadline_info = f"- **Дедлайн:** {deadline}\n" if deadline else ""
    chat_info = f"- **Ссылка на чат:** [Перейти в переписку на HH](https://hh.ru/chat/{conversation_id})\n" if conversation_id else ""
    status_info = f"- **Статус:** {status_md}\n"

    detail_block = (
        f"### {company} — {vacancy_title}\n"
        f"- **Дата фиксации:** {now_str}\n"
        f"{status_info}"
        f"{chat_info}"
        f"- **Материалы / Ссылки:** {links_md}\n"
        f"{deadline_info}"
        f"- **Требования и текст сообщения:**\n"
        f"> {clean_task_text}\n\n"
        f"- **Стек и план реализации:**\n"
        f"  * Анализ стека и подготовка чистой архитектуры (FastAPI / n8n / Python / Docker / Bitrix24).\n"
        f"  * Реализация в изолированном репозитории с тестами, docstrings и README.\n"
        f"  * Отправка ссылки на GitHub репозиторий работодателю.\n\n"
        f"---\n\n"
    )

    content += detail_block

    try:
        ARTIFACT_PATH.write_text(content, encoding="utf-8")
        logger.info(f"Recorded test task for '{identifier}' into {ARTIFACT_PATH}")
    except Exception as e:
        logger.error(f"Failed to write test task artifact: {e}")


def generate_test_task_acknowledgement(
    company: str = "",
    vacancy: str = "",
    task_text: str = "",
) -> str:
    """Generate a polite acknowledgement for an employer who sent a test task."""
    sample = f"{company} {vacancy} {task_text}".lower()
    is_english = any(w in sample for w in ["thank you", "dear", "questionnaire", "test", "task", "form", "assessment", "screening"]) or (sum(1 for c in sample if 'a' <= c <= 'z') > sum(1 for c in sample if 'а' <= c <= 'я'))
    if is_english:
        return (
            "Hello! Thank you for the update. I have received the assignment and will review it and get back to you shortly.\n\n"
            "Best regards,\nMikhail\nTelegram: @brokeassh"
        )
    return (
        "Здравствуйте! Большое спасибо за обратную связь и тестовое задание. Принял в работу, ознакомлюсь с деталями и вернусь с решением в ближайшее время.\n\n"
        "С уважением,\nМихаил\nTelegram: @brokeassh"
    )


def generate_questionnaire_acknowledgement(
    company: str = "",
    vacancy: str = "",
    task_text: str = "",
) -> str:
    """Generate a polite acknowledgement for an employer who sent a screening questionnaire."""
    sample = f"{company} {vacancy} {task_text}".lower()
    is_english = any(w in sample for w in ["thank you", "dear", "questionnaire", "test", "task", "form", "screening"]) or (sum(1 for c in sample if 'a' <= c <= 'z') > sum(1 for c in sample if 'а' <= c <= 'я'))
    if is_english:
        return (
            "Hello! Thank you for the update. I have received the screening questionnaire and will complete it shortly.\n\n"
            "Best regards,\nMikhail\nTelegram: @brokeassh"
        )
    return (
        "Здравствуйте! Спасибо за предоставленную анкету. Принял ссылку, заполню все вопросы в ближайшее время.\n\n"
        "С уважением,\nМихаил\nTelegram: @brokeassh"
    )

