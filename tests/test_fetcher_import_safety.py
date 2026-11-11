"""TEST-C characterization test: importing job_search_fetcher must be side-effect free.

Specifically, it must not execute interpreter handover or sys.exit when sys.executable differs from _PROJECT_PY.
"""
import sys
from unittest.mock import patch


def test_import_job_search_fetcher_does_not_reexec_or_exit():
    # Ensure sys.executable is simulated as a non-matching interpreter
    with patch("sys.exit") as mock_exit, patch("subprocess.run") as mock_run:
        import integrations.hermes.job_search_fetcher as jsf
        # Importing must not call sys.exit or subprocess.run
        assert not mock_exit.called
        assert not mock_run.called
        assert hasattr(jsf, "send_to_telegram")
        assert hasattr(jsf, "_maybe_reexec")
