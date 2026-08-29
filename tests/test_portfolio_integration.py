from __future__ import annotations

from ai_assistant.application_prep import _build_cover_letter_system_prompt


def test_cover_letter_prompt_mentions_portfolio():
    prompt = _build_cover_letter_system_prompt()
    assert "portfolio URL" in prompt
    assert "proof of real systems" in prompt
    assert "Do not mention it mechanically in every letter" in prompt
