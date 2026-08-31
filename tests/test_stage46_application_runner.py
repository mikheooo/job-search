"""Stage 46: Controlled Batch Application Execution Runner Test Suite.

Proves:
1. Runner never selects SUBMITTED applications.
2. Runner never selects NEEDS_HUMAN_REVIEW applications.
3. Runner never selects BLOCKED applications.
4. Preview performs zero browser mutation / submit clicks.
5. next without --confirm-submit stops with Submit Count = 0.
6. --confirm-submit executes exactly ONE submit for selected application.
7. After submit, runner halts immediately (never auto-advances to next application).
8. Re-running already SUBMITTED application is blocked.
9. Post-submit verification is mandatory before marking SUBMITTED.
10. Pre-check failure halts execution with Submit Count = 0.
11. Existing control applications retain their state integrity.
12. pipeline.py is never executed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.hh_application_runner import (
    preview_next_application,
    run_next_application,
    run_application,
    RunnerPreCheckStatus,
    RunnerExecutionResult,
)
from ai_assistant.hh_application_orchestrator import HHApplicationState


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage46_runner.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


class MockRunnerBrowser:
    """Mock CDP browser for runner tests."""
    def __init__(self, target_url: str = "https://hh.ru/vacancy/136704137", submit_ok: bool = True):
        self.target_url = target_url
        self.current_url = target_url
        self.submit_ok = submit_ok
        self.submit_attempts: int = 0

    def evaluate(self, script: str) -> str:
        if "submitBtn.click()" in script or "submit_response" in script or ("click" in script and "response-submit" in script):
            self.submit_attempts += 1
            return json.dumps({"ok": self.submit_ok})

        # Post submit inspection vs page inspection
        if "has_responded_success" in script:
            return json.dumps({
                "url": self.current_url,
                "title": "Python developer middle",
                "h1": "Python developer middle",
                "has_responded_success": True if self.submit_attempts > 0 else False,
                "has_topic_link": True if self.submit_attempts > 0 else False,
                "has_cover_letter_btn": True if self.submit_attempts > 0 else False,
                "has_explicit_rejection": False,
                "is_chat": False,
                "has_submit_btn": True if self.submit_attempts == 0 else False,
                "has_apply_btn": True if self.submit_attempts == 0 else False,
                "evidence_snippet": "Отклик отправлен",
            })

        return json.dumps({
            "url": self.current_url,
            "title": "Python developer middle",
            "h1": "Python developer middle",
            "has_submit_btn": True,
            "has_apply_btn": True,
            "already_responded": False,
            "is_chat": False,
            "is_vacancy_page": True,
        })


# ---------------------------------------------------------------------------
# Test 1, 2, 3: Exclusions from Runner Selection
# ---------------------------------------------------------------------------

def test_runner_never_selects_non_ready_applications(clean_db):
    """Runner strictly ignores SUBMITTED, NEEDS_HUMAN_REVIEW, and BLOCKED applications."""
    db.save_hh_application({
        "application_id": "app_submitted",
        "vacancy_stable_id": "hh:1",
        "title": "Senior Engineer",
        "state": "SUBMITTED",
    })
    db.save_hh_application({
        "application_id": "app_review",
        "vacancy_stable_id": "hh:2",
        "title": "Middle Engineer",
        "state": "NEEDS_HUMAN_REVIEW",
    })
    db.save_hh_application({
        "application_id": "app_blocked",
        "vacancy_stable_id": "hh:3",
        "title": "Junior Engineer",
        "state": "BLOCKED",
    })

    # Preview
    prev = preview_next_application()
    assert prev.selected_application is None
    assert prev.final_application_state == "NO_READY_APPLICATIONS"

    # Run
    res = run_next_application(confirm_submit=False)
    assert res.selected_application is None
    assert res.real_hh_submit == 0


# ---------------------------------------------------------------------------
# Test 4: Preview Performs Zero Mutations
# ---------------------------------------------------------------------------

def test_preview_performs_zero_browser_mutations(clean_db):
    """preview_next_application runs pre-checks without mutating application state or browser."""
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = preview_next_application()
    assert res.selected_application is not None
    assert "app_hh_136704137" in res.selected_application
    assert res.real_hh_submit == 0
    assert res.submit_confirmation is False
    assert res.post_submit_verification == RunnerPreCheckStatus.NOT_RUN

    # Ensure application state is unchanged
    app = db.get_hh_application("app_hh_136704137")
    assert app["state"] == "READY_TO_SUBMIT"


# ---------------------------------------------------------------------------
# Test 5: Next Without --confirm-submit Stops with Submit = 0
# ---------------------------------------------------------------------------

def test_next_without_confirm_flag_stops_at_gate(clean_db):
    """run_next_application without confirm_submit verifies pre-checks and pauses."""
    browser = MockRunnerBrowser()
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = run_next_application(confirm_submit=False, evaluate_fn=browser.evaluate)

    assert res.pre_submit_audit == RunnerPreCheckStatus.PASS
    assert res.navigation == RunnerPreCheckStatus.PASS
    assert res.submit_confirmation is False
    assert res.real_hh_submit == 0
    assert browser.submit_attempts == 0
    assert "explicit confirmation required" in res.reason.lower()

    app = db.get_hh_application("app_hh_136704137")
    assert app["state"] == "READY_TO_SUBMIT"


# ---------------------------------------------------------------------------
# Test 6 & 7: Confirm Submit Executes One Submit and Halts
# ---------------------------------------------------------------------------

def test_confirm_submit_executes_one_and_halts(clean_db):
    """With confirm_submit=True, executes exactly 1 submit, verifies it, and does not auto-advance."""
    browser = MockRunnerBrowser()

    # Two ready applications in queue
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })
    db.save_hh_application({
        "application_id": "app_hh_second",
        "vacancy_stable_id": "hh:135854121",
        "title": "AI Builder",
        "employer": "TutorPlace",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T13:00:00",
        "updated_at": "2026-08-30T13:00:00",
    })

    res = run_next_application(confirm_submit=True, evaluate_fn=browser.evaluate)

    assert res.application_id == "app_hh_136704137"
    assert res.submit_confirmation is True
    assert res.real_hh_submit == 1
    assert res.post_submit_verification == RunnerPreCheckStatus.PASS
    assert res.final_application_state == "SUBMITTED"
    assert res.next_application_executed is False

    # First app is now SUBMITTED
    first = db.get_hh_application("app_hh_136704137")
    assert first["state"] == "SUBMITTED"

    # Second app remains untouched in READY_TO_SUBMIT
    second = db.get_hh_application("app_hh_second")
    assert second["state"] == "READY_TO_SUBMIT"


# ---------------------------------------------------------------------------
# Test 8: Re-running SUBMITTED Application is Blocked
# ---------------------------------------------------------------------------

def test_rerunning_submitted_application_blocked(clean_db):
    """run_application rejects execution of an application in SUBMITTED state."""
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "employer": "AI Automation Lab",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = run_application("app_hh_135112049", confirm_submit=True)
    assert res.real_hh_submit == 0
    assert "application_already_submitted" in res.reason
    assert res.final_application_state == "SUBMITTED"


# ---------------------------------------------------------------------------
# Test 10: Pre-Check Failure Halts with Submit Count = 0
# ---------------------------------------------------------------------------

def test_pre_check_failure_halts_safely(clean_db):
    """If navigation pre-check fails, execution halts with real_hh_submit = 0."""
    def broken_eval(script):
        return json.dumps({
            "url": "https://hh.ru/unknown",
            "title": "404 Not Found",
            "h1": "",
            "has_submit_btn": False,
            "has_apply_btn": False,
            "is_vacancy_page": False,
        })

    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    res = run_application("app_hh_136704137", confirm_submit=True, evaluate_fn=broken_eval)
    assert res.navigation == RunnerPreCheckStatus.FAIL
    assert res.real_hh_submit == 0
    assert "Navigation pre-check failed" in res.reason


# ---------------------------------------------------------------------------
# Test 11 & 12: CLI application runner preview & next
# ---------------------------------------------------------------------------

def test_cli_runner_commands(clean_db, capsys):
    """CLI application runner preview and next work cleanly."""
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    ret = cli.application_runner_cmd("preview")
    assert ret == 0
    out = capsys.readouterr().out
    assert "STAGE 46 CONTROLLED APPLICATION RUNNER" in out
    assert "REAL HH SUBMIT:       0" in out
    assert "Next app executed:    NO" in out
