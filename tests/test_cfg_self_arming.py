"""Tests for CFG-001: Prevent .env from self-arming operator-disabled flags.

Verifies that operator-defined safety variables cannot be clobbered or re-armed
by values in .env files when loading configuration.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ai_assistant import config


def test_operator_safety_vars_inventory():
    """All required safety flags are tracked in OPERATOR_SAFETY_VARS."""
    expected = {
        "SUBMIT_ALLOWED",
        "HH_AUTO_SUBMIT",
        "HH_AUTONOMOUS_REPLY_ENABLED",
        "KWORK_AUTO_APPLY",
        "BROWSER_REAL",
        "HH_DRY_RUN",
        "EMAIL_DRY_RUN",
        "STOP_SUBMITS_FILE",
        "STOP_OUTBOUNDS_FILE",
    }
    assert expected.issubset(set(config.OPERATOR_SAFETY_VARS))


@pytest.mark.parametrize(
    ("var_name", "operator_val", "dotenv_val"),
    [
        ("SUBMIT_ALLOWED", "false", "true"),
        ("HH_AUTO_SUBMIT", "0", "1"),
        ("HH_AUTONOMOUS_REPLY_ENABLED", "false", "true"),
        ("KWORK_AUTO_APPLY", "false", "true"),
        ("BROWSER_REAL", "0", "1"),
        ("HH_DRY_RUN", "true", "false"),
        ("EMAIL_DRY_RUN", "true", "false"),
        ("STOP_SUBMITS_FILE", "/operator/stop_submits", "/env/stop_submits"),
        ("STOP_OUTBOUNDS_FILE", "/operator/stop_outbounds", "/env/stop_outbounds"),
    ],
)
def test_operator_flag_not_overwritten_by_dotenv_reload(monkeypatch, var_name, operator_val, dotenv_val):
    """When an operator sets a variable in the environment, reloading config with a .env

    file containing an armed/different value must NOT clobber the operator's value.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        fake_env_file = Path(tmp_dir) / ".env"
        fake_env_file.write_text(f"{var_name}={dotenv_val}\n", encoding="utf-8")

        monkeypatch.setenv(var_name, operator_val)
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_dir)

        # Reload config module under monkeypatched conditions
        reloaded = importlib.reload(config)

        assert os.getenv(var_name) == operator_val, (
            f"Environment variable {var_name} was overwritten by .env! "
            f"Expected {operator_val}, got {os.getenv(var_name)}"
        )
        assert reloaded._OPERATOR_FLAGS.get(var_name) == operator_val


def test_submit_allowed_remains_false_when_operator_disables(monkeypatch):
    """submit_allowed() stays False if operator set SUBMIT_ALLOWED=false even if .env has true."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        fake_env = Path(tmp_dir) / ".env"
        fake_env.write_text("SUBMIT_ALLOWED=true\n", encoding="utf-8")

        monkeypatch.setenv("SUBMIT_ALLOWED", "false")
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_dir)

        reloaded = importlib.reload(config)
        assert reloaded.submit_allowed() is False


def test_subprocess_isolation_operator_env_wins():
    """Verify in a fresh child Python process that an operator env variable survives."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        env_file = Path(tmp_dir) / ".env"
        env_file.write_text("SUBMIT_ALLOWED=true\nHH_AUTO_SUBMIT=1\n", encoding="utf-8")

        code = (
            "import os, ai_assistant.config as cfg\n"
            "assert os.getenv('SUBMIT_ALLOWED') == 'false'\n"
            "assert not cfg.submit_allowed()\n"
            "assert os.getenv('HH_AUTO_SUBMIT') == '0'\n"
            "print('OK')\n"
        )

        child_env = dict(os.environ)
        child_env["SUBMIT_ALLOWED"] = "false"
        child_env["HH_AUTO_SUBMIT"] = "0"
        # Point PROJECT_ROOT simulation or standard PYTHONPATH
        project_root = str(Path(__file__).resolve().parents[1])
        child_env["PYTHONPATH"] = f"{project_root};{project_root}/.venv/Lib/site-packages"

        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            cwd=project_root,
        )
        assert proc.returncode == 0, f"Child process failed: {proc.stderr}\n{proc.stdout}"
        assert "OK" in proc.stdout
