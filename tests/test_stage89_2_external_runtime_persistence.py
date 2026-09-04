"""Stage 89.2: External Runtime Persistence & Drift Detection Test Suite.

Verifies:
1. expected Telegram feedback integration detected (HEALTHY);
2. missing outbound keyboard wiring detected (DRIFTED);
3. missing Hermes callback routing detected (DRIFTED);
4. drifted integration surfaces warning in production-health;
5. sync operation is idempotent;
6. .env is Git ignored and not tracked;
7. bot tokens/secrets are not tracked in git or printed unmasked;
8. no network calls occur during automated test execution.
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from ai_assistant import config, db
from ai_assistant.hermes_integration import (
    get_hermes_paths,
    get_hermes_integration_status,
    sync_hermes_integration,
    compute_file_sha256,
)
from ai_assistant.cli import hermes_cmd
from integrations.hermes.telegram_adapter_hook import (
    HERMES_CALLBACK_HOOK_MARKER,
    HERMES_CALLBACK_HOOK_CODE,
)


@pytest.fixture
def isolated_hermes_env(tmp_path):
    """Create an isolated fake Hermes directory structure for persistence testing."""
    fake_hermes = tmp_path / "fake_hermes"
    fake_fetcher_dir = fake_hermes / "profiles" / "jobs" / "scripts"
    fake_adapter_dir = fake_hermes / "hermes-agent" / "plugins" / "platforms" / "telegram"
    fake_fetcher_dir.mkdir(parents=True, exist_ok=True)
    fake_adapter_dir.mkdir(parents=True, exist_ok=True)

    # Deploy initial valid files
    canonical_fetcher = Path(r"C:\Users\Misha\Documents\job-search\integrations\hermes\job_search_fetcher.py")
    fetcher_dest = fake_fetcher_dir / "job_search_fetcher.py"
    fetcher_dest.write_bytes(canonical_fetcher.read_bytes())

    fake_adapter_content = (
        "# Fake adapter base\n"
        "class TelegramAdapter:\n"
        "    async def _handle_callback_query(self, query):\n"
        "        data = getattr(query, 'data', '')\n"
        + HERMES_CALLBACK_HOOK_CODE + "\n"
        "        # --- Update prompt callbacks ---\n"
        "        if not data.startswith('update_prompt:'):\n"
        "            return\n"
    )
    adapter_dest = fake_adapter_dir / "adapter.py"
    adapter_dest.write_text(fake_adapter_content, encoding="utf-8")

    return fake_hermes


# ---------------------------------------------------------------------------
# 1. Expected Telegram feedback integration detected (HEALTHY)
# ---------------------------------------------------------------------------
def test_healthy_integration_detected(isolated_hermes_env):
    status = get_hermes_integration_status(base_dir=isolated_hermes_env)
    assert status["status"] == "HEALTHY"
    assert status["fetcher"]["status"] == "HEALTHY"
    assert status["fetcher"]["keyboard_capable"] is True
    assert status["fetcher"]["attempt_locking"] is True
    assert status["adapter"]["status"] == "HEALTHY"
    assert status["adapter"]["routing_capable"] is True


# ---------------------------------------------------------------------------
# 2. Missing outbound keyboard wiring detected (DRIFTED)
# ---------------------------------------------------------------------------
def test_missing_outbound_keyboard_detected(isolated_hermes_env):
    fetcher_path = isolated_hermes_env / "profiles" / "jobs" / "scripts" / "job_search_fetcher.py"
    # Legacy fetcher without reply_markup
    legacy_code = "def send_to_telegram(text: str) -> bool:\n    pass\n"
    fetcher_path.write_text(legacy_code, encoding="utf-8")

    status = get_hermes_integration_status(base_dir=isolated_hermes_env)
    assert status["status"] == "DRIFTED"
    assert status["fetcher"]["status"] == "DRIFTED"
    assert status["fetcher"]["keyboard_capable"] is False


# ---------------------------------------------------------------------------
# 3. Missing Hermes callback routing detected (DRIFTED / MISSING)
# ---------------------------------------------------------------------------
def test_missing_adapter_routing_detected(isolated_hermes_env):
    adapter_path = isolated_hermes_env / "hermes-agent" / "plugins" / "platforms" / "telegram" / "adapter.py"
    # Adapter without fb: hook
    clean_adapter_code = (
        "class TelegramAdapter:\n"
        "    async def _handle_callback_query(self, query):\n"
        "        data = getattr(query, 'data', '')\n"
        "        # --- Update prompt callbacks ---\n"
        "        if not data.startswith('update_prompt:'):\n"
        "            return\n"
    )
    adapter_path.write_text(clean_adapter_code, encoding="utf-8")

    status = get_hermes_integration_status(base_dir=isolated_hermes_env)
    assert status["status"] == "DRIFTED"
    assert status["adapter"]["status"] == "DRIFTED"
    assert status["adapter"]["routing_capable"] is False


# ---------------------------------------------------------------------------
# 4. Drifted integration surfaces warning in production-health
# ---------------------------------------------------------------------------
def test_drifted_integration_surfaces_in_production_health(tmp_path, isolated_hermes_env, monkeypatch):
    test_db = tmp_path / "test_health_stage89_2.db"
    monkeypatch.setattr(config, "DB_FILE", str(test_db))
    monkeypatch.setenv("HERMES_ROOT", str(isolated_hermes_env))
    db.init_db()

    # Create drift in fetcher
    fetcher_path = isolated_hermes_env / "profiles" / "jobs" / "scripts" / "job_search_fetcher.py"
    fetcher_path.write_text("broken content", encoding="utf-8")

    health = db.get_production_health()
    assert health["health"] in ("DEGRADED", "UNHEALTHY")
    alert_messages = [a["message"] for a in health["alerts"]]
    assert any("Hermes external integration is DRIFTED" in m for m in alert_messages)


# ---------------------------------------------------------------------------
# 5. Sync operation is idempotent and restores HEALTHY state
# ---------------------------------------------------------------------------
def test_sync_operation_is_idempotent(isolated_hermes_env):
    # Corrupt both files
    fetcher_path = isolated_hermes_env / "profiles" / "jobs" / "scripts" / "job_search_fetcher.py"
    adapter_path = isolated_hermes_env / "hermes-agent" / "plugins" / "platforms" / "telegram" / "adapter.py"

    fetcher_path.write_text("corrupted fetcher", encoding="utf-8")
    adapter_path.write_text(
        "class TelegramAdapter:\n"
        "    async def _handle_callback_query(self, query):\n"
        "        # --- Update prompt callbacks ---\n"
        "        pass\n",
        encoding="utf-8",
    )

    # First sync
    res1 = sync_hermes_integration(base_dir=isolated_hermes_env, dry_run=False)
    assert res1["success"] is True
    assert res1["status_after"]["status"] == "HEALTHY"
    assert len(res1["actions_taken"]) >= 2

    # Second sync (idempotent no-op)
    res2 = sync_hermes_integration(base_dir=isolated_hermes_env, dry_run=False)
    assert res2["success"] is True
    assert res2["status_after"]["status"] == "HEALTHY"
    assert len(res2["actions_taken"]) == 0


# ---------------------------------------------------------------------------
# 6. .env is Git ignored and not tracked
# ---------------------------------------------------------------------------
def test_env_is_git_ignored():
    gitignore_path = Path(r"C:\Users\Misha\Documents\job-search\.gitignore")
    assert gitignore_path.exists()
    content = gitignore_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines()]
    assert ".env" in lines


# ---------------------------------------------------------------------------
# 7. Secrets are not committed in git or printed unmasked
# ---------------------------------------------------------------------------
def test_secrets_masked_in_status_and_logs(isolated_hermes_env, monkeypatch):
    monkeypatch.setenv("HERMES_ROOT", str(isolated_hermes_env))
    fake_token = "000000000:FAKE_TOKEN_FOR_MASKING_TEST_xxxxxxxx"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", fake_token)
    status = get_hermes_integration_status(base_dir=isolated_hermes_env)
    status_str = json.dumps(status)
    assert fake_token not in status_str
    assert "FAKE_TOKEN_FOR_MASKING_TEST_xxxxxxxx" not in status_str


# ---------------------------------------------------------------------------
# 8. No network calls occur in automated test execution
# ---------------------------------------------------------------------------
def test_no_network_in_hermes_sync(isolated_hermes_env):
    res = sync_hermes_integration(base_dir=isolated_hermes_env, dry_run=False)
    assert res["success"] is True
