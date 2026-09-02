"""Stage 89.2: Hermes Integration Management & Drift Detection Subsystem.

Provides deterministic inspection, drift detection, and synchronization for
external Hermes runtime components (fetcher script and Telegram platform adapter).
"""

from __future__ import annotations

import os
import re
import sys
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional

from integrations.hermes.telegram_adapter_hook import (
    HERMES_CALLBACK_HOOK_MARKER,
    HERMES_CALLBACK_HOOK_CODE,
)

DEFAULT_HERMES_APP_DATA = Path(r"C:\Users\Misha\AppData\Local\hermes")
CANONICAL_ROOT = Path(r"C:\Users\Misha\Documents\job-search")


def get_hermes_paths(base_dir: Optional[Path] = None) -> Dict[str, Path]:
    """Resolve active Hermes filesystem paths."""
    base = base_dir or Path(os.environ.get("HERMES_ROOT", str(DEFAULT_HERMES_APP_DATA)))
    return {
        "base": base,
        "fetcher_deployed": base / "profiles" / "jobs" / "scripts" / "job_search_fetcher.py",
        "fetcher_canonical": CANONICAL_ROOT / "integrations" / "hermes" / "job_search_fetcher.py",
        "adapter_deployed": base / "hermes-agent" / "plugins" / "platforms" / "telegram" / "adapter.py",
    }


def compute_file_sha256(path: Path) -> Optional[str]:
    """Compute SHA256 hex digest of a file if it exists."""
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest().upper()


def get_hermes_integration_status(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Audit external Hermes runtime integration for health, capabilities, and drift."""
    paths = get_hermes_paths(base_dir=base_dir)

    fetcher_canonical_path = paths["fetcher_canonical"]
    fetcher_deployed_path = paths["fetcher_deployed"]
    adapter_deployed_path = paths["adapter_deployed"]

    fetcher_canonical_hash = compute_file_sha256(fetcher_canonical_path)
    fetcher_deployed_hash = compute_file_sha256(fetcher_deployed_path)
    adapter_deployed_hash = compute_file_sha256(adapter_deployed_path)

    # 1. Fetcher Capability & Drift Check
    fetcher_exists = fetcher_deployed_path.exists()
    fetcher_keyboard_capable = False
    fetcher_attempt_locking = False

    if fetcher_exists:
        try:
            content = fetcher_deployed_path.read_text(encoding="utf-8")
            fetcher_keyboard_capable = (
                "reply_markup: dict = None" in content
                and "build_digest_inline_keyboard" in content
            )
            fetcher_attempt_locking = "record_digest_attempt" in content
        except Exception:
            pass

    fetcher_status = "HEALTHY"
    if not fetcher_exists:
        fetcher_status = "MISSING"
    elif not fetcher_keyboard_capable:
        fetcher_status = "DRIFTED"
    elif fetcher_canonical_hash and fetcher_deployed_hash != fetcher_canonical_hash:
        fetcher_status = "DRIFTED"

    # 2. Adapter Callback Routing Capability Check
    adapter_exists = adapter_deployed_path.exists()
    adapter_routing_capable = False

    if adapter_exists:
        try:
            content = adapter_deployed_path.read_text(encoding="utf-8")
            adapter_routing_capable = (
                HERMES_CALLBACK_HOOK_MARKER in content
                and "TelegramFeedbackProcessor" in content
                and 'data.startswith("fb:")' in content
            )
        except Exception:
            pass

    adapter_status = "HEALTHY"
    if not adapter_exists:
        adapter_status = "MISSING"
    elif not adapter_routing_capable:
        adapter_status = "DRIFTED"

    # Overall Integration Status
    if fetcher_status == "HEALTHY" and adapter_status == "HEALTHY":
        overall_status = "HEALTHY"
    elif "MISSING" in (fetcher_status, adapter_status):
        overall_status = "MISSING"
    else:
        overall_status = "DRIFTED"

    return {
        "status": overall_status,
        "fetcher": {
            "status": fetcher_status,
            "exists": fetcher_exists,
            "keyboard_capable": fetcher_keyboard_capable,
            "attempt_locking": fetcher_attempt_locking,
            "canonical_sha256": fetcher_canonical_hash,
            "deployed_sha256": fetcher_deployed_hash,
            "deployed_path": str(fetcher_deployed_path),
        },
        "adapter": {
            "status": adapter_status,
            "exists": adapter_exists,
            "routing_capable": adapter_routing_capable,
            "deployed_sha256": adapter_deployed_hash,
            "deployed_path": str(adapter_deployed_path),
        },
    }


def sync_hermes_integration(base_dir: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Idempotently sync canonical integration files to Hermes runtime locations."""
    paths = get_hermes_paths(base_dir=base_dir)
    status_before = get_hermes_integration_status(base_dir=base_dir)

    actions_taken = []

    # 1. Sync Fetcher
    fetcher_canonical = paths["fetcher_canonical"]
    fetcher_deployed = paths["fetcher_deployed"]

    if fetcher_canonical.exists():
        canon_bytes = fetcher_canonical.read_bytes()
        deployed_bytes = fetcher_deployed.read_bytes() if fetcher_deployed.exists() else b""
        if canon_bytes != deployed_bytes:
            if not dry_run:
                fetcher_deployed.parent.mkdir(parents=True, exist_ok=True)
                fetcher_deployed.write_bytes(canon_bytes)
            actions_taken.append(f"UPDATED_FETCHER: {fetcher_deployed}")
    else:
        actions_taken.append(f"ERROR_CANONICAL_FETCHER_NOT_FOUND: {fetcher_canonical}")

    # 2. Sync Adapter Hook
    adapter_deployed = paths["adapter_deployed"]
    if adapter_deployed.exists():
        adapter_text = adapter_deployed.read_text(encoding="utf-8")
        if HERMES_CALLBACK_HOOK_MARKER not in adapter_text:
            # Inject hook before '# --- Update prompt callbacks ---' or in _handle_callback_query
            target_insertion = "        # --- Update prompt callbacks ---"
            if target_insertion in adapter_text:
                new_text = adapter_text.replace(
                    target_insertion,
                    HERMES_CALLBACK_HOOK_CODE + "\n" + target_insertion,
                    1,
                )
                if not dry_run:
                    adapter_deployed.write_text(new_text, encoding="utf-8")
                actions_taken.append(f"INJECTED_ADAPTER_HOOK: {adapter_deployed}")
            else:
                actions_taken.append(f"ERROR_ADAPTER_INSERTION_POINT_NOT_FOUND: {adapter_deployed}")
    else:
        actions_taken.append(f"ERROR_ADAPTER_NOT_FOUND: {adapter_deployed}")

    status_after = get_hermes_integration_status(base_dir=base_dir)

    return {
        "success": status_after["status"] == "HEALTHY" if not dry_run else True,
        "dry_run": dry_run,
        "actions_taken": actions_taken,
        "status_before": status_before,
        "status_after": status_after,
    }
