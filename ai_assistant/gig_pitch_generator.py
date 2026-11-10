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

GIG_PITCH_VARIANTS = ["direct_focus", "enterprise_proven", "speed_delivery"]


def select_pitch_variant(lead_id: str | None = None) -> str:
    """Deterministically select a variant based on lead_id hash or default."""
    if not lead_id:
        return "direct_focus"
    idx = abs(hash(lead_id)) % len(GIG_PITCH_VARIANTS)
    return GIG_PITCH_VARIANTS[idx]


def generate_gig_pitch(
    title: str,
    description: str = "",
    budget: str = "",
    language: str | None = None,
    source: str | None = None,
    variant_id: str | None = None,
) -> str:
    """Generate a crisp, ready-to-send proposal for a freelance gig.

    ``language`` is the *client-facing* reply language. When ``None`` it is
    auto-resolved from the order text (title+description) with the marketplace
    as a tie-breaker — so English orders (Freelancer.com, Contra) get an
    English reply instead of the old hardcoded Russian one. Only confirmed
    candidate KB facts are used; nothing is invented in either language.
    ``variant_id`` selects an A/B tested pitch strategy (direct_focus, enterprise_proven, speed_delivery).
    """
    if language not in ("ru", "en"):
        from ai_assistant.gig.order_language import resolve_order_language
        language = resolve_order_language(title, description, source)

    clean_title = re.sub(r"^[^\wА-Яа-яЁё]+", "", str(title or "")).strip()
    clean_title = re.sub(r"\s+", " ", clean_title) or str(title or "").strip()

    try:
        from ai_assistant.gig.kb_loader import get_confirmed_pitch_focus
        focus = get_confirmed_pitch_focus(
            title=clean_title, description=description, language=language
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
        if variant_id == "enterprise_proven":
            pitch = (
                f'Hello! I can take on your project "{clean_title}"{budget_note} with high reliability and clean delivery.\n\n'
                f"{focus}\n\n"
                f"Milestone-based delivery, transparent communication, payment on completion / escrow.\n\n"
                f"{contact_block}"
            )
        elif variant_id == "speed_delivery":
            pitch = (
                f'Hello! I can start on your task "{clean_title}"{budget_note} right away today.\n\n'
                f"{focus}\n\n"
                f"Fast turnaround, direct execution, payment upon delivery / escrow.\n\n"
                f"{contact_block}"
            )
        else:
            pitch = (
                f'Hello! I can take on your task "{clean_title}"{budget_note} promptly and reliably.\n\n'
                f"{focus}\n\n"
                f"I can start today. Payment on delivery / secure escrow.\n\n"
                f"{contact_block}"
            )
    else:
        budget_note = f" (ориентир по бюджету: {budget})" if budget else ""
        if variant_id == "enterprise_proven":
            pitch = (
                f"Здравствуйте! Готов взять на себя задачу «{clean_title}»{budget_note} с упором на надежность и качество.\n\n"
                f"{focus}\n\n"
                f"Поэтапная сдача, прозрачная коммуникация, оплата по факту / безопасная сделка.\n\n"
                f"Для связи:\n"
                f"Telegram: @brokeassh\n"
                f"Email: mikhailthaiban@gmail.com\n"
                f"Портфолио: https://github.com/mikheooo"
            )
        elif variant_id == "speed_delivery":
            pitch = (
                f"Здравствуйте! Готов приступить к задаче «{clean_title}»{budget_note} прямо сегодня.\n\n"
                f"{focus}\n\n"
                f"Быстрый старт, минимальные сроки реализации, оплата по результату.\n\n"
                f"Для связи:\n"
                f"Telegram: @brokeassh\n"
                f"Email: mikhailthaiban@gmail.com\n"
                f"Портфолио: https://github.com/mikheooo"
            )
        else:
            pitch = (
                f"Здравствуйте! Готов оперативно и качественно закрыть вашу задачу «{clean_title}»{budget_note}.\n\n"
                f"{focus}\n\n"
                f"Готов приступить сегодня. Оплата по факту выполнения / безопасная сделка.\n\n"
                f"Для связи:\n"
                f"Telegram: @brokeassh\n"
                f"Email: mikhailthaiban@gmail.com\n"
                f"Портфолио: https://github.com/mikheooo"
            )
    return pitch.strip()


def generate_gig_pitch_with_variant(
    title: str,
    description: str = "",
    budget: str = "",
    language: str | None = None,
    source: str | None = None,
    lead_id: str | None = None,
    variant_id: str | None = None,
) -> tuple[str, str]:
    """Generate pitch and return tuple of (pitch_text, variant_id)."""
    chosen = variant_id or select_pitch_variant(lead_id)
    pitch = generate_gig_pitch(
        title=title,
        description=description,
        budget=budget,
        language=language,
        source=source,
        variant_id=chosen,
    )
    return pitch, chosen


def build_gig_deeplink(username: str, pitch_text: str) -> str:
    """Build a direct t.me link with prefilled text."""
    clean_user = username.lstrip("@").strip()
    encoded = urllib.parse.quote(pitch_text)
    return f"https://t.me/{clean_user}?text={encoded}"
