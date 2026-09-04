"""Tests for HH Application State Machine Invariants (Phase 2)."""

from __future__ import annotations

import pytest

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_application_orchestrator import (
    HHApplicationState,
    LEGAL_TRANSITIONS,
    SubmitApproval,
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


def test_no_edge_into_submitted_without_approval(clean_db):
    """Every edge in LEGAL_TRANSITIONS entering SUBMITTED requires SubmitApproval."""
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
                reason="testing_approval_gate",
                approval=None,
            )
            assert res.ok is False
            assert res.error == "MISSING_SUBMIT_APPROVAL"
            tested_edges += 1
    assert tested_edges > 0


def test_legacy_states_cannot_reach_submitted(clean_db):
    """READY_FOR_AUTONOMOUS_SUBMIT and QUESTIONNAIRE_AUTO_FILLED cannot reach SUBMITTED even with approval."""
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
            approval=SubmitApproval(source="policy"),
            evidence={"fingerprint": "valid_fingerprint_legacy"},
        )
        assert res.ok is False
        assert res.error in ("ILLEGAL_TRANSITION", "SUBMIT_FORBIDDEN_FROM_STATE")


def test_submitted_requires_fingerprint_evidence(clean_db):
    """Transition READY_TO_SUBMIT -> SUBMITTED with approval but without fingerprint is rejected."""
    app_id = "app_test_no_fp"
    db.save_hh_application({
        "application_id": app_id,
        "state": HHApplicationState.READY_TO_SUBMIT.value,
        "vacancy_stable_id": "hh:12345678",
    })
    res = transition_application(
        application_id=app_id,
        to_state=HHApplicationState.SUBMITTED,
        reason="test_no_fingerprint",
        approval=SubmitApproval(source="policy"),
        evidence={"note": "missing fingerprint"},
    )
    assert res.ok is False
    assert res.error == "MISSING_FINGERPRINT_EVIDENCE"


def test_submitted_with_human_approval_succeeds(clean_db):
    """Transition READY_TO_SUBMIT -> SUBMITTED with human SubmitApproval succeeds and writes approval to evidence."""
    app_id = "app_test_human_appr"
    db.save_hh_application({
        "application_id": app_id,
        "state": HHApplicationState.READY_TO_SUBMIT.value,
        "vacancy_stable_id": "hh:12345678",
    })
    approval = SubmitApproval(source="human", policy_version="manual_test", checks_passed=["human_review_ok"])
    res = transition_application(
        application_id=app_id,
        to_state=HHApplicationState.SUBMITTED,
        reason="test_human_approval",
        approval=approval,
        evidence={"fingerprint": "valid_sha256_package_fingerprint_human"},
    )
    assert res.ok is True
    assert res.to_state == HHApplicationState.SUBMITTED.value
    stored = db.get_hh_application(app_id)
    assert stored["state"] == HHApplicationState.SUBMITTED.value
    transitions = db.list_hh_application_transitions(app_id)
    assert len(transitions) == 1
    ev = transitions[0]["evidence"]
    assert "approval" in ev
    assert ev["approval"]["source"] == "human"
    assert ev["approval"]["policy_version"] == "manual_test"


def test_submitted_with_policy_approval_succeeds(clean_db):
    """Transition READY_TO_SUBMIT -> SUBMITTED with policy SubmitApproval succeeds and writes approval to evidence."""
    app_id = "app_test_policy_appr"
    db.save_hh_application({
        "application_id": app_id,
        "state": HHApplicationState.READY_TO_SUBMIT.value,
        "vacancy_stable_id": "hh:12345678",
    })
    approval = SubmitApproval(
        source="policy",
        policy_version="v2.0_autonomous",
        checks_passed=["fingerprint_match", "letter_quality", "limits_ok"],
    )
    res = transition_application(
        application_id=app_id,
        to_state=HHApplicationState.SUBMITTED,
        reason="test_policy_approval",
        approval=approval,
        evidence={"fingerprint": "valid_sha256_package_fingerprint_policy"},
    )
    assert res.ok is True
    assert res.to_state == HHApplicationState.SUBMITTED.value
    stored = db.get_hh_application(app_id)
    assert stored["state"] == HHApplicationState.SUBMITTED.value
    transitions = db.list_hh_application_transitions(app_id)
    assert len(transitions) == 1
    ev = transitions[0]["evidence"]
    assert "approval" in ev
    assert ev["approval"]["source"] == "policy"
    assert ev["approval"]["policy_version"] == "v2.0_autonomous"
    assert "letter_quality" in ev["approval"]["checks_passed"]
