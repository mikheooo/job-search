"""Tests for LOG-001 structured logging and secret masking."""

from __future__ import annotations

from ai_assistant.runner import mask_secrets


def test_mask_secrets_empty_and_normal_text():
    assert mask_secrets("") == ""
    assert mask_secrets(None) == ""
    normal = "INFO: Application cycle completed successfully for vacancy 12345."
    assert mask_secrets(normal) == normal


def test_mask_secrets_telegram_tokens():
    mock_token_suffix = "ABCdefGHIjklMNOpqrSTUvwxYZ_" + "123456"
    text1 = f"Connecting via bot1234567890:{mock_token_suffix} to Telegram API"
    masked1 = mask_secrets(text1)
    assert mock_token_suffix not in masked1
    assert "bot<MASKED_TELEGRAM_TOKEN>" in masked1

    raw_token_suffix = "mock_secret_suffix_" + "A" * 20
    text2 = f"Raw token in error 8217526633:{raw_token_suffix} received"
    masked2 = mask_secrets(text2)
    assert raw_token_suffix not in masked2
    assert "<MASKED_TELEGRAM_TOKEN>" in masked2


def test_mask_secrets_bearer_and_api_keys():
    bearer_token = "secret_access_token_value_" + "longer_than_20_chars"
    bearer_text = f"Authorization: Bearer {bearer_token}"
    masked_b = mask_secrets(bearer_text)
    assert bearer_token not in masked_b
    assert "Bearer <MASKED_TOKEN>" in masked_b

    openai_key = "sk-" + "abcdef1234567890" + "abcdef1234567890"
    openai_text = f"Using OpenAI key {openai_key} for chat completions"
    masked_o = mask_secrets(openai_text)
    assert openai_key not in masked_o
    assert "sk-<MASKED_API_KEY>" in masked_o

    google_key = "AIzaSy" + "A1B2C3D4E5F6G7H8" + "I9J0K1L2M3N4O5P6Q"
    google_text = f"Using Google key {google_key}"
    masked_g = mask_secrets(google_text)
    assert google_key not in masked_g
    assert "AIzaSy<MASKED_GOOGLE_KEY>" in masked_g


def test_mask_secrets_env_assignments():
    env_dump = "TELEGRAM_BOT_TOKEN=secret_bot_val DASHBOARD_TOKEN=secret_pass_123 LLM_API_KEY=key_val_456"
    masked = mask_secrets(env_dump)
    assert "secret_bot_val" not in masked
    assert "secret_pass_123" not in masked
    assert "key_val_456" not in masked
    assert "TELEGRAM_BOT_TOKEN=<MASKED>" in masked
    assert "DASHBOARD_TOKEN=<MASKED>" in masked
    assert "LLM_API_KEY=<MASKED>" in masked
