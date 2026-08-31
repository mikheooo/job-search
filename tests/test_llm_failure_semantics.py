"""Regression tests for Stage 75 LLM transient-failure semantics.

Covers:
- deep-analysis exception does not crash the watcher run
- failed vacancy becomes ANALYSIS_FAILED, not REJECTED
- ANALYSIS_FAILED is retryable on next watcher run
- successful retry transitions to ANALYZED
- permanent hard-constraint rejection stays REJECTED
- sync_application_tracking preserves ANALYSIS_FAILED
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, r"C:\Users\Misha\Documents\job-search")
os.environ.setdefault("JOB_FETCHER_NO_REEXEC", "1")

from ai_assistant import config
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    MANUAL_STATUSES,
    sync_application_tracking,
)
from ai_assistant.watcher import run_watcher_cycle
from ai_assistant.schema import Vacancy
from ai_assistant.db import (
    init_db,
    save_vacancy,
    get_deep_analysis,
)


def _make_profile():
    return MagicMock(
        remote_required=False,
        batch_limit=5,
        candidate_country="TH",
    )


def _mock_adapters(vac):
    """Return adapter mocks that yield our single vacancy from any source."""
    mocks = {}
    for name in ["himalayas", "remoteok", "weworkremotely", "habrcareer"]:
        m = MagicMock()
        m.fetch_vacancies.return_value = [vac]
        mocks[name] = m
    return mocks


def _isolated_db(tmp_path, monkeypatch):
    """Create an isolated DB and patch both config.DB_FILE and env."""
    db_file = str(tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setenv("DB_FILE", db_file)
    init_db()
    return db_file


def test_deep_analysis_failure_becomes_analysis_failed_not_rejected(tmp_path, monkeypatch):
    """Deep analysis exception must not crash watcher and must set ANALYSIS_FAILED."""
    db_file = _isolated_db(tmp_path, monkeypatch)

    vac = Vacancy(
        source="remoteok",
        source_job_id="llm-fail-1",
        title="Python Automation Engineer",
        company="TestCo",
        description="remote python automation n8n",
        job_url="https://example.com/llm-fail-1",
        application_url="https://example.com/llm-fail-1",
    )
    save_vacancy(vac)

    failing_matcher = MagicMock()
    failing_matcher.match.return_value = MagicMock(decision="APPLY", score=85, reasons=[])

    with patch("ai_assistant.watcher.load_candidate_profile", return_value=_make_profile()), \
         patch("ai_assistant.watcher.assess_vacancy_eligibility", return_value=MagicMock(eligibility="ELIGIBLE", eligibility_reasons=[])), \
         patch("ai_assistant.watcher.is_strictly_remote", return_value=(True, "")), \
         patch("ai_assistant.watcher._hard_constraints", return_value=(False, [])), \
         patch("ai_assistant.watcher.JobMatcher", return_value=failing_matcher), \
         patch("ai_assistant.watcher.ADAPTER_MAP", _mock_adapters(vac)), \
         patch("ai_assistant.cli.SOURCES", _mock_adapters(vac)), \
         patch("ai_assistant.watcher.analyze_job_deep", side_effect=RuntimeError("Simulated LLM 429")):

        result = run_watcher_cycle(dry_run=False)

    assert "Deep analysis failed" in " ".join(result.errors)
    assert result.analyzed_count == 0
    assert result.rejected_count == 0

    track = get_application_status(vac.stable_id())
    assert track is not None
    assert track.status == ApplicationStatus.ANALYSIS_FAILED

    # No partial deep analysis persisted
    assert get_deep_analysis(vac.stable_id()) is None


def test_analysis_failed_is_retryable_on_next_run(tmp_path, monkeypatch):
    """ANALYSIS_FAILED vacancy must be retried on next watcher run."""
    db_file = _isolated_db(tmp_path, monkeypatch)

    vac = Vacancy(
        source="remoteok",
        source_job_id="llm-retry-1",
        title="Python Automation Engineer",
        company="TestCo",
        description="remote python automation n8n",
        job_url="https://example.com/llm-retry-1",
        application_url="https://example.com/llm-retry-1",
    )
    save_vacancy(vac)

    # First run: LLM fails
    failing_matcher = MagicMock()
    failing_matcher.match.return_value = MagicMock(decision="APPLY", score=85, reasons=[])

    with patch("ai_assistant.watcher.load_candidate_profile", return_value=_make_profile()), \
         patch("ai_assistant.watcher.assess_vacancy_eligibility", return_value=MagicMock(eligibility="ELIGIBLE", eligibility_reasons=[])), \
         patch("ai_assistant.watcher.is_strictly_remote", return_value=(True, "")), \
         patch("ai_assistant.watcher._hard_constraints", return_value=(False, [])), \
         patch("ai_assistant.watcher.JobMatcher", return_value=failing_matcher), \
         patch("ai_assistant.watcher.ADAPTER_MAP", _mock_adapters(vac)), \
         patch("ai_assistant.cli.SOURCES", _mock_adapters(vac)), \
         patch("ai_assistant.watcher.analyze_job_deep", side_effect=RuntimeError("Simulated LLM 429")):

        run_watcher_cycle(dry_run=False)

    track = get_application_status(vac.stable_id())
    assert track is not None
    assert track.status == ApplicationStatus.ANALYSIS_FAILED

    # Second run: LLM succeeds
    success_result = MagicMock()
    success_result.fit_score = 90
    success_result.recommendation = "APPLY"
    success_result.why_fit = []
    success_result.application_strategy = "direct"
    success_result.model_dump_json.return_value = "{}"

    succeeding_matcher = MagicMock()
    succeeding_matcher.match.return_value = MagicMock(decision="APPLY", score=85, reasons=[])

    with patch("ai_assistant.watcher.load_candidate_profile", return_value=_make_profile()), \
         patch("ai_assistant.watcher.assess_vacancy_eligibility", return_value=MagicMock(eligibility="ELIGIBLE", eligibility_reasons=[])), \
         patch("ai_assistant.watcher.is_strictly_remote", return_value=(True, "")), \
         patch("ai_assistant.watcher._hard_constraints", return_value=(False, [])), \
         patch("ai_assistant.watcher.JobMatcher", return_value=succeeding_matcher), \
         patch("ai_assistant.watcher.ADAPTER_MAP", _mock_adapters(vac)), \
         patch("ai_assistant.cli.SOURCES", _mock_adapters(vac)), \
         patch("ai_assistant.watcher.analyze_job_deep", return_value=success_result), \
         patch("ai_assistant.watcher.prepare_application", return_value=None), \
         patch("ai_assistant.application_queue.save_queue_item"), \
         patch("ai_assistant.application_review.save_application_review"):

        result2 = run_watcher_cycle(dry_run=False)

    track2 = get_application_status(vac.stable_id())
    assert track2 is not None
    assert track2.status == ApplicationStatus.ANALYZED
    assert result2.analyzed_count == 1
    assert result2.rejected_count == 0


def test_hard_constraint_rejection_stays_rejected(tmp_path, monkeypatch):
    """Hard-constraint rejection must still produce REJECTED, not ANALYSIS_FAILED."""
    db_file = _isolated_db(tmp_path, monkeypatch)

    vac = Vacancy(
        source="remoteok",
        source_job_id="hard-rej-1",
        title="PHP Developer",
        company="LegacyCo",
        description="php developer legacy",
        job_url="https://example.com/hard-rej-1",
        application_url="https://example.com/hard-rej-1",
    )
    save_vacancy(vac)

    matcher = MagicMock()
    matcher.match.return_value = MagicMock(decision="SKIP", score=20, reasons=["Excluded role found: php"])

    with patch("ai_assistant.watcher.load_candidate_profile", return_value=_make_profile()), \
         patch("ai_assistant.watcher.assess_vacancy_eligibility", return_value=MagicMock(eligibility="ELIGIBLE", eligibility_reasons=[])), \
         patch("ai_assistant.watcher.is_strictly_remote", return_value=(True, "")), \
         patch("ai_assistant.watcher._hard_constraints", return_value=(False, [])), \
         patch("ai_assistant.watcher.JobMatcher", return_value=matcher), \
         patch("ai_assistant.watcher.ADAPTER_MAP", _mock_adapters(vac)), \
         patch("ai_assistant.cli.SOURCES", _mock_adapters(vac)):

        result = run_watcher_cycle(dry_run=False)

    track = get_application_status(vac.stable_id())
    assert track is not None
    assert track.status == ApplicationStatus.REJECTED
    assert result.rejected_count == 1


def test_sync_application_tracking_preserves_analysis_failed(tmp_path, monkeypatch):
    """sync_application_tracking must not overwrite ANALYSIS_FAILED with DISCOVERED."""
    db_file = _isolated_db(tmp_path, monkeypatch)

    vac = Vacancy(
        source="remoteok",
        source_job_id="sync-fail-1",
        title="Python Automation Engineer",
        company="TestCo",
        description="remote python automation n8n",
        job_url="https://example.com/sync-fail-1",
        application_url="https://example.com/sync-fail-1",
    )
    save_vacancy(vac)

    from ai_assistant.application_tracking import set_application_status as set_status
    set_status(vac.stable_id(), ApplicationStatus.ANALYSIS_FAILED)

    sync_application_tracking(profile_path=None)

    track = get_application_status(vac.stable_id())
    assert track.status == ApplicationStatus.ANALYSIS_FAILED


def test_analysis_failed_in_manual_statuses():
    """ANALYSIS_FAILED must be in MANUAL_STATUSES to prevent sync overwrite."""
    assert ApplicationStatus.ANALYSIS_FAILED in MANUAL_STATUSES
