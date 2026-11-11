import os
from unittest.mock import patch
import pytest

from ai_assistant.runner import is_pid_running


def test_is_pid_running_self_pid_fast_path():
    """Verify is_pid_running returns True for current process PID without subprocess or ctypes calls."""
    self_pid = os.getpid()

    with patch("subprocess.run", side_effect=AssertionError("subprocess.run should not be called for self PID")):
        with patch("sys.platform", "win32"):
            # Ensure fast-path returns True immediately
            assert is_pid_running(self_pid) is True


def test_is_pid_running_invalid_pid():
    """Verify non-positive PIDs return False immediately without OS checks."""
    with patch("subprocess.run", side_effect=AssertionError("subprocess.run should not be called for invalid PID")):
        assert is_pid_running(0) is False
        assert is_pid_running(-100) is False
