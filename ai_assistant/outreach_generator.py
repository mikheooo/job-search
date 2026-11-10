"""Telegram & Direct Outreach Message Generator.

Crafts punchy, persuasive 3-4 sentence first-contact messages tailored
to the specific job requirements, positioning Mikhail as an experienced
senior practitioner in the exact stack required.

Includes deep links to open Telegram directly with prefilled text:
https://t.me/<username>?text=<encoded_pitch>
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

from .schema import Vacancy
from .smart_experience_generator import extract_key_technologies

logger = logging.getLogger("outreach_generator")

CANDIDATE_NAME = "Михаил"
TELEGRAM_HANDLE = "brokeassh"
GITHUB_LINK = "https://github.com/mikheooo"
PHONE = "+7 (993) 339-76-28"


def generate_outreach_pitch(
    title: str,
    company: str,
    description: str = "",
    salary_str: str | None = None
) -> str:
    """Generate a crisp, compelling outreach message for Telegram or Email."""
    techs = extract_key_technologies(f"{title}\n{description}")
    role_clean = re.sub(r"#\S+", "", title).strip(" |:-—–")
    if len(role_clean) > 50:
        role_clean = role_clean[:47] + "..."

    # Determine core specialty
    combined = (title + " " + description).lower()

    if any(k in combined for k in ["devops", "девопс", "kubernetes", "k8s", "ansible", "ci/cd"]):
        focus = "DevOps/SRE (Docker, K8s, CI/CD pipelines, Linux/Bash, IaC)"
        achieve = "развертывание отказоустойчивой инфраструктуры, автоматизацию деплоя и мониторинг"
    elif any(k in combined for k in ["crm", "битрикс", "bitrix", "amocrm"]):
        focus = "внедрении и системном администрировании CRM (Битрикс24, amoCRM, API-интеграции, вебхуки)"
        achieve = "настройку бизнес-процессов, кастомные интеграции через REST API и поддержку пользователей"
    elif any(k in combined for k in ["ai", "mlops", "python", "нейро"]):
        focus = "Python-разработке и внедрении AI/LLM решений (FastAPI, асинхронные сервисы, парсинг, интеграция моделей)"
        achieve = "разработку надежных бэкенд-сервисов и автоматизацию прикладных процессов"
    elif any(k in combined for k in ["admin", "админ", "linux", "линукс", "сетев", "сисадмин"]):
        focus = "системном администрировании Linux/Windows (серверная инфраструктура, Docker, сети, безопасность)"
        achieve = "поддержание high availability сервисов, автоматизацию рутины и решение инцидентов"
    else:
        focus = "технической поддержке L2/L3 и системном сопровождении сложных IT-систем"
        achieve = "быструю локализацию сложных инцидентов и автоматизацию сопровождения"

    comp_str = f" в {company}" if company and company not in ("Компания", "%hirify_global%") else ""

    text = (
        f"Здравствуйте! Увидел вашу вакансию на позицию «{role_clean}»{comp_str}.\n\n"
        f"Имею уверенный практический опыт в {focus}. "
        f"Стек отлично знаком — специализируюсь на задачах под ключ: {achieve}.\n\n"
        f"Формат: удаленная работа, часовой пояс UTC+7 (МСК+4), оперативное подключение.\n"
        f"Мой GitHub: {GITHUB_LINK}\n\n"
        f"Буду рад обсудить подробности задач и ответить на вопросы!"
    )
    return text


def build_telegram_deeplink(username: str, pitch_text: str) -> str:
    """Build a direct t.me link that opens the chat with the recruiter and pre-fills the pitch."""
    clean_user = username.lstrip("@").strip()
    encoded_text = urllib.parse.quote(pitch_text)
    return f"https://t.me/{clean_user}?text={encoded_text}"
