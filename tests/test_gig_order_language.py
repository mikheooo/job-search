"""Tests for order-language resolution and language-correct gig replies (Phase 5D)."""
from __future__ import annotations

from ai_assistant.gig.kb_loader import get_confirmed_pitch_focus
from ai_assistant.gig.order_language import (
    LANG_EN,
    LANG_RU,
    detect_text_language,
    resolve_order_language,
    source_default_language,
)
from ai_assistant.gig_pitch_generator import generate_gig_pitch


def test_detect_russian_text():
    assert detect_text_language("Требуется разработать телеграм-бота на Python") == LANG_RU


def test_detect_english_text():
    assert detect_text_language("Need an experienced Python developer for scraping") == LANG_EN


def test_detect_empty_or_digits_returns_none():
    assert detect_text_language("") is None
    assert detect_text_language("2026 $$$ ---") is None


def test_source_defaults():
    assert source_default_language("freelancer") == LANG_EN
    assert source_default_language("contra") == LANG_EN
    assert source_default_language("kwork") == LANG_RU
    assert source_default_language("tg_freelance_orders") == LANG_RU
    assert source_default_language(None) == LANG_RU


def test_resolve_prefers_text_then_source():
    # Clear English text wins even if source says RU.
    assert resolve_order_language(
        "Build a REST API in Python", "We need automation", source="kwork"
    ) == LANG_EN
    # Text with no letters at all gives no signal -> marketplace default.
    assert resolve_order_language("2026", source="freelancer") == LANG_EN
    assert resolve_order_language("2026", source="kwork") == LANG_RU
    # A single Latin word is (correctly) read as English text.
    assert resolve_order_language("Python", source="kwork") == LANG_EN


def test_english_order_gets_english_pitch():
    pitch = generate_gig_pitch(
        title="Build a Telegram bot",
        description="Need a Python Telegram bot with database integration",
        budget="500 USD",
        source="freelancer",
    )
    assert pitch.startswith("Hello!")
    assert "Здравствуйте" not in pitch
    assert "Более 15 лет" not in pitch
    assert "budget guide: 500 USD" in pitch
    # Contact block is language-neutral and always present.
    assert "@brokeassh" in pitch


def test_freelancer_source_forces_english_on_ambiguous_title():
    pitch = generate_gig_pitch(title="Docker setup", description="", source="freelancer")
    assert pitch.startswith("Hello!")


def test_russian_order_still_gets_russian_pitch():
    pitch = generate_gig_pitch(
        title="Настроить VPN-сервер",
        description="Нужен специалист по Linux и WireGuard",
        source="kwork",
    )
    assert pitch.startswith("Здравствуйте!")
    assert "Hello!" not in pitch


def test_explicit_language_overrides_source():
    pitch = generate_gig_pitch(title="Python", description="", source="freelancer", language="ru")
    assert pitch.startswith("Здравствуйте!")


def test_kb_focus_english_vs_russian():
    en = get_confirmed_pitch_focus("Build a Telegram bot", "aiogram", language="en")
    ru = get_confirmed_pitch_focus("Сделать телеграм-бота", "aiogram", language="ru")
    assert en.startswith("Proven track record:")
    assert ru.startswith("В подтвержденном опыте:")
    assert "Telegram" in en or "telegram" in en.lower()


def test_english_pitch_uses_only_confirmed_facts():
    """No invented projects: the draft must not fabricate named clients/products."""
    pitch = generate_gig_pitch(
        title="Data scraping pipeline",
        description="scrape listings to Excel",
        source="freelancer",
    )
    lower = pitch.lower()
    # These would be hallucinated specifics — must never appear.
    for invented in ("we built for google", "our client amazon", "fortune 500", "acme corp"):
        assert invented not in lower
