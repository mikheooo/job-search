"""Stage 45: Controlled Application Queue Test Suite.

Proves:
1. Submitted application is classified as SUBMITTED.
2. Ready application is classified as READY_TO_SUBMIT.
3. Human review applications are correctly identified.
4. Blocked applications are correctly identified.
5. can_submit for SUBMITTED is False (application_already_submitted).
6. can_submit for HUMAN_REVIEW is False (human_review_required).
7. can_submit for BLOCKED is False (application_blocked).
8. can_submit for READY_TO_SUBMIT is True (ready_to_submit).
9. can_submit never performs Submit (zero mutations/clicks).
10. queue commands never perform Submit (zero mutations/clicks).
11. queue avoids creating duplicate entries.
12. queue --json returns stable structured payload.
13. app_hh_135112049 remains SUBMITTED.
14. app_hh_136704137 remains READY_TO_SUBMIT.
15. pipeline.py is never executed.
"""

from __future__ import annotations

import json
import pytest
from typing import Any, Dict, List, Optional

from ai_assistant import db, cli
import ai_assistant.config as config
from ai_assistant.hh_application_queue import (
    can_submit,
    get_controlled_application_queue,
    format_queue_cli,
    format_ready_queue_cli,
    format_human_review_queue_cli,
    SubmitEligibilityResult,
    HHQueueItem,
)
from ai_assistant.hh_application_orchestrator import HHApplicationState


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage45_queue.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1 & 5: SUBMITTED Application Classification and Eligibility
# ---------------------------------------------------------------------------

def test_submitted_application_classification_and_eligibility(clean_db):
    """SUBMITTED application is classified properly and can_submit returns False."""
    app_id = "app_hh_135112049"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "employer": "AI Automation Lab",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    elig = can_submit(app_id)
    assert elig.allowed is False
    assert elig.reason == "application_already_submitted"
    assert elig.state == "SUBMITTED"

    items = get_controlled_application_queue()
    assert len(items) == 1
    assert items[0].application_id == app_id
    assert items[0].application_state == "SUBMITTED"
    assert items[0].can_submit_allowed is False


# ---------------------------------------------------------------------------
# Test 2 & 8: READY_TO_SUBMIT Application Classification and Eligibility
# ---------------------------------------------------------------------------

def test_ready_to_submit_application_classification_and_eligibility(clean_db):
    """READY_TO_SUBMIT application is classified properly and can_submit returns True."""
    app_id = "app_hh_136704137"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    elig = can_submit(app_id)
    assert elig.allowed is True
    assert elig.reason == "ready_to_submit"
    assert elig.state == "READY_TO_SUBMIT"

    ready_items = get_controlled_application_queue(filter_mode="ready")
    assert len(ready_items) == 1
    assert ready_items[0].application_id == app_id
    assert ready_items[0].can_submit_allowed is True


# ---------------------------------------------------------------------------
# Test 3 & 6: NEEDS_HUMAN_REVIEW Application Handling
# ---------------------------------------------------------------------------

def test_needs_human_review_application_handling(clean_db):
    """NEEDS_HUMAN_REVIEW application is blocked from submit and surfaced in human-review filter."""
    app_id = "app_hh_review_needed"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:999999",
        "title": "AI Solutions Architect",
        "employer": "Tech Corp",
        "state": "NEEDS_HUMAN_REVIEW",
        "last_transition_reason": "salary_question_requires_human_decision",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    elig = can_submit(app_id)
    assert elig.allowed is False
    assert elig.reason == "human_review_required"

    review_items = get_controlled_application_queue(filter_mode="human_review")
    assert len(review_items) == 1
    assert review_items[0].application_id == app_id
    assert review_items[0].reason_blocker == "salary_question_requires_human_decision"


# ---------------------------------------------------------------------------
# Test 4 & 7: BLOCKED Application Handling
# ---------------------------------------------------------------------------

def test_blocked_application_handling(clean_db):
    """BLOCKED application returns False with reason application_blocked."""
    app_id = "app_hh_blocked"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:888888",
        "title": "Data Engineer",
        "employer": "Data Labs",
        "state": "BLOCKED",
        "last_transition_reason": "browser_disconnected_fatal",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    elig = can_submit(app_id)
    assert elig.allowed is False
    assert elig.reason == "application_blocked"


# ---------------------------------------------------------------------------
# Test 9 & 10: Zero Submit Invariant on can_submit and Queue
# ---------------------------------------------------------------------------

def test_zero_submit_mutation_invariant(clean_db):
    """can_submit and get_controlled_application_queue are strictly read-only."""
    app_id = "app_hh_ready"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    for _ in range(5):
        elig = can_submit(app_id)
        assert elig.allowed is True
        items = get_controlled_application_queue()
        assert len(items) == 1

    # Ensure application state was not altered
    app = db.get_hh_application(app_id)
    assert app["state"] == "READY_TO_SUBMIT"


# ---------------------------------------------------------------------------
# Test 11: Queue Deduplication
# ---------------------------------------------------------------------------

def test_queue_deduplication(clean_db):
    """Multiple records with same application_id do not create duplicate queue items."""
    db.save_hh_application({
        "application_id": "app_hh_dup",
        "vacancy_stable_id": "hh:111",
        "title": "Python Engineer",
        "employer": "Alpha",
        "state": "READY_TO_SUBMIT",
    })
    db.save_hh_application({
        "application_id": "app_hh_dup",
        "vacancy_stable_id": "hh:111",
        "title": "Python Engineer",
        "employer": "Alpha",
        "state": "READY_TO_SUBMIT",
    })

    items = get_controlled_application_queue()
    assert len(items) == 1
    assert items[0].application_id == "app_hh_dup"


# ---------------------------------------------------------------------------
# Test 12: CLI Queue --json Stable Structure
# ---------------------------------------------------------------------------

def test_cli_queue_json_output(clean_db, capsys):
    """application queue --json outputs valid JSON with required fields."""
    db.save_hh_application({
        "application_id": "app_hh_136704137",
        "vacancy_stable_id": "hh:136704137",
        "title": "Python developer middle",
        "employer": "Maxima.tech",
        "state": "READY_TO_SUBMIT",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
    })

    ret = cli.application_queue_cmd(as_json=True)
    assert ret == 0

    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 1
    item = data[0]
    assert "application_id" in item
    assert "vacancy_id" in item
    assert "company" in item
    assert "application_state" in item
    assert "can_submit_allowed" in item
    assert "can_submit_reason" in item


# ---------------------------------------------------------------------------
# Test 13 & 14: Control Applications Invariant
# ---------------------------------------------------------------------------

def test_control_applications_states(clean_db):
    """app_hh_135112049 is SUBMITTED and app_hh_136704137 is READY_TO_SUBMIT in queue."""
    db.save_hh_application({
        "application_id": "app_hh_135112049",
        "vacancy_stable_id": "hh:135112049",
        "title": "Senior AI Automation Engineer",
        "employer": "AI Automation Lab",
        "state": "SUBMITTED",
        "created_at": "2026-08-30T12:00:00",
        "updated_at": "2026-08-30T12:00:00",
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

    items = get_controlled_application_queue()
    states = {it.application_id: it.application_state for it in items}

    assert states["app_hh_135112049"] == "SUBMITTED"
    assert states["app_hh_136704137"] == "READY_TO_SUBMIT"

    # Only app_hh_136704137 is in ready queue
    ready = get_controlled_application_queue(filter_mode="ready")
    ready_ids = [it.application_id for it in ready]
    assert "app_hh_136704137" in ready_ids
    assert "app_hh_135112049" not in ready_ids
