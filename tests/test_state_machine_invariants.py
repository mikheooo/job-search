"""Tests for HH Application State Machine Invariants."""

from __future__ import annotations

import pytest

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_application_orchestrator import (
    HHApplicationState,
    LEGAL_TRANSITIONS,
    transition_application,
)


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_state_machine_invariants.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


def test_no_edge_into_submitted_without_confirm(clean_db):
    """Every edge in LEGAL_TRANSITIONS entering SUBMITTED requires confirm_submit=True."""
    tested_edges = 0
    for from_state, targets in LEGAL_TRANSITIONS.items():
        if HHApplicationState.SUBMITTED.value in targets:
            app_id = f"app_test_edge_{from_state}"
            db.save_hh_application({
                "application_id": app_id,
                "state": from_state,
                "vacancy_stable_id": "hh:12345678",
            })
            res = transition_application(
                application_id=app_id,
                to_state=HHApplicationState.SUBMITTED,
                reason="testing_confirm_gate",
                confirm_submit=False,
            )
            assert res.ok is False
            assert res.error == "MISSING_HUMAN_CONFIRMATION"
            tested_edges += 1
    assert tested_edges > 0


def test_legacy_states_cannot_reach_submitted(clean_db):
    """READY_FOR_AUTONOMOUS_SUBMIT and QUESTIONNAIRE_AUTO_FILLED cannot reach SUBMITTED even with confirm_submit=True."""
    for legacy_state in [
        HHApplicationState.READY_FOR_AUTONOMOUS_SUBMIT,
        HHApplicationState.QUESTIONNAIRE_AUTO_FILLED,
    ]:
        app_id = f"app_test_legacy_{legacy_state.value}"
        db.save_hh_application({
            "application_id": app_id,
            "state": legacy_state.value,
            "vacancy_stable_id": "hh:12345678",
        })
        res = transition_application(
            application_id=app_id,
            to_state=HHApplicationState.SUBMITTED,
            reason="testing_legacy_blocked",
            confirm_submit=True,
        )
        assert res.ok is False
        assert res.error in ("ILLEGAL_TRANSITION", "SUBMIT_FORBIDDEN_FROM_STATE")
