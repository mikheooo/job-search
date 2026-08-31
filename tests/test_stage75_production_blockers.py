"""Targeted regression tests for Stage 75 production blockers.

Covers:
- Canonical DB path (cwd-independent).
- Dry-run produces no DB mutations.
- Rejection reasons normalization (str/list/tuple).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"C:\Users\Misha\Documents\job-search")
os.environ.setdefault("JOB_FETCHER_NO_REEXEC", "1")

from ai_assistant.config import DB_FILE, PROJECT_ROOT  # noqa: E402
from ai_assistant.db import (  # noqa: E402
    save_vacancy,
    save_vacancy_eligibility,
    save_deep_analysis,
    save_application_package,
    set_dry_run,
    is_dry_run,
)
from ai_assistant.application_tracking import set_application_status  # noqa: E402
from ai_assistant.application_queue import save_queue_item  # noqa: E402
from ai_assistant.watcher import _normalize_rejection_reasons, run_watcher_cycle  # noqa: E402
from ai_assistant.schema import Vacancy  # noqa: E402
from ai_assistant.candidate_profile import load_candidate_profile  # noqa: E402

def test_canonical_db_path_is_absolute_and_cwd_independent():
    expected = str(Path(r"C:\Users\Misha\Documents\job-search\state.db").resolve())
    assert str(DB_FILE) == expected, f"DB_FILE={DB_FILE}"
    assert str(PROJECT_ROOT) == str(Path(r"C:\Users\Misha\Documents\job-search").resolve())


def test_dry_run_does_not_mutate_db():
    from unittest.mock import patch, MagicMock

    dry_run_calls = []

    def record_call(name):
        def wrapper(*args, **kwargs):
            dry_run_calls.append(name)
            return None
        return wrapper

    with patch("ai_assistant.db.save_vacancy", side_effect=record_call("save_vacancy")), \
         patch("ai_assistant.db.save_vacancy_eligibility", side_effect=record_call("save_vacancy_eligibility")), \
         patch("ai_assistant.db.save_deep_analysis", side_effect=record_call("save_deep_analysis")), \
         patch("ai_assistant.db.save_application_package", side_effect=record_call("save_application_package")), \
         patch("ai_assistant.application_queue.save_queue_item", side_effect=record_call("save_queue_item")), \
         patch("ai_assistant.application_tracking.set_application_status", side_effect=record_call("set_application_status")), \
         patch("ai_assistant.application_tracking.transition_application", side_effect=record_call("transition_application")), \
         patch("ai_assistant.db.init_db"), \
         patch("ai_assistant.candidate_profile.load_candidate_profile", return_value=MagicMock(remote_required=False, batch_limit=5)), \
         patch("ai_assistant.watcher.assess_vacancy_eligibility", return_value=MagicMock(eligibility="ELIGIBLE", eligibility_reasons=[])), \
         patch("ai_assistant.watcher.is_strictly_remote", return_value=(True, "")), \
         patch("ai_assistant.watcher._hard_constraints", return_value=(False, [])), \
         patch("ai_assistant.matcher.JobMatcher") as mock_matcher_cls, \
         patch("ai_assistant.watcher.ADAPTER_MAP", {"test": MagicMock(fetch_vacancies=lambda: [Vacancy(
             source="test",
             source_job_id="dryrun-1",
             title="Dry Run",
             company="ACME",
             description="remote python job",
             job_url="https://example.com/dryrun-1",
             application_url="https://example.com/dryrun-1",
         )])}):

        mock_matcher = MagicMock()
        mock_matcher.match.return_value = MagicMock(decision="APPLY", score=80, reasons=[])
        mock_matcher_cls.return_value = mock_matcher
        set_dry_run(True)
        try:
            result = run_watcher_cycle(dry_run=True)
        finally:
            set_dry_run(False)

    assert not dry_run_calls, f"dry_run DB mutation calls: {dry_run_calls}"
    assert not result.errors, f"dry_run errors: {result.errors}"


def test_rejection_reason_string_normalization():
    reasons_str = "Excluded role found: php"
    reasons_list = ["Low match", "Remote constraint violated"]
    reasons_tuple = ("Hard constraints rejected",)
    assert _normalize_rejection_reasons(reasons_str) == [reasons_str]
    assert _normalize_rejection_reasons(reasons_list) == reasons_list
    assert _normalize_rejection_reasons(reasons_tuple) == list(reasons_tuple)
    assert _normalize_rejection_reasons("") == []
    assert _normalize_rejection_reasons(None) == []


if __name__ == "__main__":
    test_canonical_db_path_is_absolute_and_cwd_independent()
    test_dry_run_does_not_mutate_db()
    test_rejection_reason_string_normalization()
    print("TARGETED TESTS PASS")
