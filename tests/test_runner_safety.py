"""Tests for runner.py process verification safety (RUN-001)."""
import ctypes
import os
from unittest.mock import patch

if not hasattr(ctypes, "windll"):
    ctypes.windll = type("_W", (), {"kernel32": type("_K", (), {"OpenProcess": lambda *a: None})()})()

from ai_assistant.runner import is_pid_running


def test_is_pid_running_current_process():
    assert is_pid_running(os.getpid()) is True


def test_is_pid_running_non_existent_process():
    assert is_pid_running(9999999) is False


def test_is_pid_running_prefix_collision_prevention():
    """Verify that PID 123 does not match output containing PID 1234 (no substring false positives)."""
    with (
        patch("ctypes.windll.kernel32.OpenProcess", side_effect=Exception("force fallback"), create=True),
        patch("subprocess.check_output") as mock_check_output,
    ):
        # Tasklist returns process with PID 1234
        mock_check_output.return_value = '"app.exe","1234","Console","1","5,000 K"\n'
        # Asking for PID 123 must NOT match
        assert is_pid_running(123) is False
        # Asking for exact PID 1234 MUST match
        assert is_pid_running(1234) is True


def test_is_pid_running_image_name_contains_pid_substring():
    """Verify that PID 123 does not match an image name like '123proc.exe' when PID is different."""
    with (
        patch("ctypes.windll.kernel32.OpenProcess", side_effect=Exception("force fallback"), create=True),
        patch("subprocess.check_output") as mock_check_output,
    ):
        mock_check_output.return_value = '"123proc.exe","9999","Console","1","5,000 K"\n'
        assert is_pid_running(123) is False
        assert is_pid_running(9999) is True
