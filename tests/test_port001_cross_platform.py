"""Tests for PORT-001 cross-platform support and entrypoints."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from ai_assistant.config import get_hermes_env_candidates


def test_hermes_env_candidates_cross_platform():
    candidates = get_hermes_env_candidates()
    assert len(candidates) >= 2
    # POSIX standard config path must be included
    posix_path_present = any(".config" in c for c in candidates)
    assert posix_path_present, f"Expected .config in candidates: {candidates}"
    # All paths must be strings ending with .env
    for c in candidates:
        assert c.endswith(".env"), c


def test_ai_assistant_module_entrypoint_wired():
    spec = importlib.util.find_spec("ai_assistant.__main__")
    assert spec is not None, "ai_assistant.__main__ spec not found"
    assert spec.origin is not None
    content = Path(spec.origin).read_text(encoding="utf-8")
    assert "from ai_assistant.cli import main" in content
    assert 'if __name__ == "__main__":' in content
    assert "sys.exit(main())" in content
