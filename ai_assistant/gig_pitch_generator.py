"""Freelance & Quick Gig Pitch Generator.

Generates concise, highly persuasive, conversion-focused proposals
for freelance and project tasks (Telegram, Kwork, FL, Habr, Contra)
matching Mikhail's core tech proficiencies:
- DevOps, Linux, Docker, WireGuard/VPN, VPS setup
- Python scripts, data scrapers, Telegram bots, API integrations
- Bitrix24, amoCRM customization, webhook automations
- System administration, network configuration, troubleshooting
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any


def generate_gig_pitch(
    title: str,
    description: str = "",
    budget: str = "",
    language: str | None = None,
    source: str | None = None,
) -> str:
    """Generate a crisp, ready-to-send proposal for a freelance gig.

    ``language`` is the *client-facing* reply language. When ``None`` it is
    auto-resolved from the order text (title+description) with the marketplace
    as a tie-breaker — so English orders (Freelancer.com, Contra) get an
    English reply instead of the old hardcoded Russian one. Only confirmed
    candidate KB facts are used; nothing is invented in either language.
    """
    if language not in ("ru", "en"):
        from ai_assistant.gig.order_language import resolve_order_language
        language = resolve_order_language(title, description, source)

    try:
        from ai_assistant.gig.kb_loader import get_confirmed_pitch_focus
        focus = get_confirmed_pitch_focus(
            title=title, description=description, language=language
        )
    except Exception:
        if language == "en":
            focus = (
                "15+ years of enterprise experience in IT infrastructure, systems "
                "administration, automation and Python. I ramp up fast and deliver in "
                "stages with testing and documentation."
            )
        else:
            focus = (
                "Более 15 лет enterprise-опыта в IT-инфраструктуре, системном администрировании, автоматизации и Python. "
                "Быстро вникаю в требования, сдаю результат поэтапно с тестированием и документацией."
            )

    contact_block = (
        "Contact:\n"
        "Telegram: @brokeassh\n"
        "Email: mikhailthaiban@gmail.com\n"
        "Portfolio: https://github.com/mikheooo"
    )

    if language == "en":
        budget_note = f" (budget guide: {budget})" if budget else ""
        pitch = (
            f'Hello! I can take on your task "{title}"{budget_note} promptly and reliably.\n\n'
            f"{focus}\n\n"
            f"I can start today. Payment on delivery / secure escrow.\n\n"
            f"{contact_block}"
        )
    else:
        budget_note = f" (ориентир по бюджету: {budget})" if budget else ""
        pitch = (
            f"Здравствуйте! Готов оперативно и качественно закрыть вашу задачу «{title}»{budget_note}.\n\n"
            f"{focus}\n\n"
            f"Готов приступить сегодня. Оплата по факту выполнения / безопасная сделка.\n\n"
            f"Для связи:\n"
            f"Telegram: @brokeassh\n"
            f"Email: mikhailthaiban@gmail.com\n"
            f"Портфолио: https://github.com/mikheooo"
        )
    return pitch.strip()


def build_gig_deeplink(username: str, pitch_text: str) -> str:
    """Build a direct t.me link with prefilled text."""
    clean_user = username.lstrip("@").strip()
    encoded = urllib.parse.quote(pitch_text)
    return f"https://t.me/{clean_user}?text={encoded}"
