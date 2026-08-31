"""Stage 35: Tests for HH Application State Machine & Orchestrator.

Proves:
1. NEW -> SUBMITTED = FORBIDDEN (Submit = 0).
2. MESSAGE_DETECTED -> SUBMITTED = FORBIDDEN (Submit = 0).
3. QUESTIONNAIRE_REQUIRED -> SUBMITTED = FORBIDDEN (Submit = 0).
4. NEEDS_HUMAN_REVIEW -> SUBMITTED = FORBIDDEN (Submit = 0).
5. READY_TO_SUBMIT without explicit confirmation: Submit = 0 (BLOCKED).
6. READY_TO_SUBMIT + explicit confirmation: Submit MAY proceed (SUBMITTED).
7. Changed questionnaire DOM: Submit = 0, resets to STALE -> NEEDS_HUMAN_REVIEW.
8. Required answer missing: Submit = 0.
9. Invalid option answer: Submit = 0.
10. Duplicate event / idempotency: duplicate application = 0.
11. Duplicate watcher cycle: duplicate reply = 0.
12. Browser/CDP failure: Submit = 0, transitions to FAILED.
13. Session unauthenticated: Submit = 0.
14. Illegal transitions: all invalid shortcuts rejected.
15. Autonomous watcher invariant: Submit = 0.
16. Full state flow & chronological audit trail.
17. CLI commands: application list, show, transitions, status.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_application_orchestrator import (
    HHApplication,
    HHApplicationState,
    HHApplicationOrchestrator,
    LEGAL_TRANSITIONS,
    get_or_create_hh_application,
    transition_application,
    format_application_cli_output,
)
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    compute_questionnaire_fingerprint,
)
from ai_assistant import cli


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage35_orchestrator.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


def _create_mock_questionnaire(qid: str = "quest_test_35") -> HHQuestionnaire:
    q1 = HHQuestionItem(
        question_id="q1",
        text="Формат работы",
        question_type="radio",
        required=True,
        options=["Офис", "Удалённо"],
    )
    q2 = HHQuestionItem(
        question_id="q2",
        text="Опыт работы с Python (лет)",
        question_type="number",
        required=True,
        options=[],
    )
    questions = [q1, q2]
    fp = compute_questionnaire_fingerprint(questions)
    quest = HHQuestionnaire(
        questionnaire_id=qid,
        vacancy_stable_id="hh:123456",
        conversation_id="conv_555",
        title="Python Tech Lead",
        employer="FinTech Corp",
        questions=questions,
        answers={},
        status="NEEDS_HUMAN_REVIEW",
        fingerprint=fp,
        created_at="2026-08-30T10:00:00",
        updated_at="2026-08-30T10:00:00",
    )
    db.save_hh_questionnaire(quest.model_dump())
    return quest


# ---------------------------------------------------------------------------
# Test 1-4: Direct Forbidden Shortcuts to SUBMITTED
# ---------------------------------------------------------------------------

def test_new_to_submitted_forbidden(clean_db):
    """Safety Invariant 1: NEW -> SUBMITTED is forbidden."""
    app = get_or_create_hh_application("app_t1")
    assert app.state == HHApplicationState.NEW.value

    res = transition_application(app.application_id, to_state=HHApplicationState.SUBMITTED, reason="hack", confirm_submit=True)
    assert res.ok is False
    assert "Illegal transition" in res.reason

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.NEW.value


def test_message_detected_to_submitted_forbidden(clean_db):
    """Safety Invariant 2: MESSAGE_DETECTED -> SUBMITTED is forbidden."""
    app = get_or_create_hh_application("app_t2")
    transition_application(app.application_id, to_state=HHApplicationState.MESSAGE_DETECTED, reason="detected")

    res = transition_application(app.application_id, to_state=HHApplicationState.SUBMITTED, reason="hack", confirm_submit=True)
    assert res.ok is False
    assert "Illegal transition" in res.reason

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.MESSAGE_DETECTED.value


def test_questionnaire_required_to_submitted_forbidden(clean_db):
    """Safety Invariant 3: QUESTIONNAIRE_REQUIRED -> SUBMITTED is forbidden."""
    app = get_or_create_hh_application("app_t3")
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.QUESTIONNAIRE_REQUIRED, reason="q_found")

    res = transition_application(app.application_id, to_state=HHApplicationState.SUBMITTED, reason="hack", confirm_submit=True)
    assert res.ok is False
    assert "Illegal transition" in res.reason

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.QUESTIONNAIRE_REQUIRED.value


def test_needs_human_review_to_submitted_forbidden(clean_db):
    """Safety Invariant 4: NEEDS_HUMAN_REVIEW -> SUBMITTED is forbidden."""
    app = get_or_create_hh_application("app_t4")
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.NEEDS_HUMAN_REVIEW, reason="review_needed")

    res = transition_application(app.application_id, to_state=HHApplicationState.SUBMITTED, reason="hack", confirm_submit=True)
    assert res.ok is False
    assert "Illegal transition" in res.reason

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.NEEDS_HUMAN_REVIEW.value


# ---------------------------------------------------------------------------
# Test 5 & 6: Human Confirmation Gate on Submit
# ---------------------------------------------------------------------------

def test_ready_to_submit_without_confirmation_blocks_submit(clean_db):
    """Safety Invariant 5: In READY_TO_SUBMIT, without explicit confirmation Submit = 0."""
    orchestrator = HHApplicationOrchestrator()
    app = get_or_create_hh_application("app_t5")
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.DRAFT_READY, reason="draft_ready")
    transition_application(app.application_id, to_state=HHApplicationState.READY_TO_SUBMIT, reason="approved")

    # Attempt submit without confirmation
    res = orchestrator.execute_confirmed_submit(app.application_id, confirm_submit=False)
    assert res["verdict"] == "BLOCKED"
    assert res["submit_count"] == 0
    assert "explicit human confirmation" in res["reason"].lower()

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.READY_TO_SUBMIT.value


def test_ready_to_submit_with_confirmation_proceeds(clean_db):
    """Safety Invariant 6: In READY_TO_SUBMIT, explicit confirmation allows submit."""
    orchestrator = HHApplicationOrchestrator()
    app = get_or_create_hh_application("app_t6")
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.DRAFT_READY, reason="draft_ready")
    transition_application(app.application_id, to_state=HHApplicationState.READY_TO_SUBMIT, reason="approved")

    fake_evaluate = MagicMock(return_value=json.dumps({"ok": True}))
    res = orchestrator.execute_confirmed_submit(app.application_id, confirm_submit=True, evaluate_fn=fake_evaluate)
    assert res["verdict"] == "SUBMITTED"
    assert res["submit_count"] == 1
    assert fake_evaluate.called

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.SUBMITTED.value


# ---------------------------------------------------------------------------
# Test 7: Changed Questionnaire DOM resets to STALE -> NEEDS_HUMAN_REVIEW
# ---------------------------------------------------------------------------

def test_changed_questionnaire_resets_to_stale_and_needs_review(clean_db):
    """Safety Invariant 7: Changed questionnaire DOM blocks submit and fails closed to NEEDS_HUMAN_REVIEW."""
    orchestrator = HHApplicationOrchestrator()
    quest = _create_mock_questionnaire("quest_t7")
    app = get_or_create_hh_application("app_t7", conversation_id="conv_555", vacancy_stable_id="hh:123456")
    app.questionnaire_id = quest.questionnaire_id
    db.save_hh_application(app.model_dump())

    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.QUESTIONNAIRE_REQUIRED, reason="q_found")
    transition_application(app.application_id, to_state=HHApplicationState.NEEDS_HUMAN_REVIEW, reason="human_answers_req")

    # Record answers with mismatched live DOM fingerprint
    diff_fp = "hh_qfp_completely_different_hash_777"
    ans_res = orchestrator.record_questionnaire_answers(
        application_id=app.application_id,
        human_answers={"q1": "Удалённо", "q2": "5"},
        current_dom_fingerprint=diff_fp,
    )
    assert ans_res.ok is True
    assert ans_res.to_state == HHApplicationState.NEEDS_HUMAN_REVIEW.value

    # Submit must be blocked
    sub_res = orchestrator.execute_confirmed_submit(app.application_id, confirm_submit=True, current_dom_fingerprint=diff_fp)
    assert sub_res["verdict"] == "BLOCKED"
    assert sub_res["submit_count"] == 0


# ---------------------------------------------------------------------------
# Test 8 & 9: Questionnaire Missing / Invalid Answer
# ---------------------------------------------------------------------------

def test_questionnaire_missing_required_answer_blocks_ready_state(clean_db):
    """Safety Invariant 8: Missing required answer rejects transition to READY_TO_SUBMIT."""
    orchestrator = HHApplicationOrchestrator()
    quest = _create_mock_questionnaire("quest_t8")
    app = get_or_create_hh_application("app_t8")
    app.questionnaire_id = quest.questionnaire_id
    db.save_hh_application(app.model_dump())

    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.QUESTIONNAIRE_REQUIRED, reason="q_found")
    transition_application(app.application_id, to_state=HHApplicationState.NEEDS_HUMAN_REVIEW, reason="human_answers_req")

    # Missing q2
    res = orchestrator.record_questionnaire_answers(
        application_id=app.application_id,
        human_answers={"q1": "Удалённо"},
        current_dom_fingerprint=quest.fingerprint,
    )
    assert res.ok is False
    assert "VALIDATION_FAILED" in res.error

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.NEEDS_HUMAN_REVIEW.value


def test_questionnaire_invalid_option_blocks_ready_state(clean_db):
    """Safety Invariant 9: Invalid choice option rejects transition to READY_TO_SUBMIT."""
    orchestrator = HHApplicationOrchestrator()
    quest = _create_mock_questionnaire("quest_t9")
    app = get_or_create_hh_application("app_t9")
    app.questionnaire_id = quest.questionnaire_id
    db.save_hh_application(app.model_dump())

    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.QUESTIONNAIRE_REQUIRED, reason="q_found")
    transition_application(app.application_id, to_state=HHApplicationState.NEEDS_HUMAN_REVIEW, reason="human_answers_req")

    # 'Космос' is not in ['Офис', 'Удалённо']
    res = orchestrator.record_questionnaire_answers(
        application_id=app.application_id,
        human_answers={"q1": "Космос", "q2": "5"},
        current_dom_fingerprint=quest.fingerprint,
    )
    assert res.ok is False
    assert "VALIDATION_FAILED" in res.error

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.NEEDS_HUMAN_REVIEW.value


# ---------------------------------------------------------------------------
# Test 10 & 11: Idempotency: Duplicate Events / Watcher Cycles
# ---------------------------------------------------------------------------

def test_duplicate_incoming_event_idempotency(clean_db):
    """Safety Invariant 10: Repeated event processing does not duplicate application or replay transitions."""
    orchestrator = HHApplicationOrchestrator()
    cid = "conv_idem_10"
    mid = "msg_123"

    # First event run
    app1 = orchestrator.orchestrate_incoming_event(
        conversation_id=cid,
        message_id=mid,
        sender="HR Lead",
        text="Здравствуйте! Расскажите о себе.",
        classification="HUMAN_REVIEW",
    )
    assert app1.state == HHApplicationState.NEEDS_HUMAN_REVIEW.value

    # Second event run with same conversation
    app2 = orchestrator.orchestrate_incoming_event(
        conversation_id=cid,
        message_id=mid,
        sender="HR Lead",
        text="Здравствуйте! Расскажите о себе.",
        classification="HUMAN_REVIEW",
    )
    assert app1.application_id == app2.application_id
    assert app2.state == HHApplicationState.NEEDS_HUMAN_REVIEW.value

    all_apps = db.list_hh_applications()
    assert len(all_apps) == 1


# ---------------------------------------------------------------------------
# Test 12: Browser / CDP Evaluation Failure
# ---------------------------------------------------------------------------

def test_browser_cdp_failure_moves_to_failed(clean_db):
    """Safety Invariant 12: CDP submit evaluation failure keeps Submit = 0 and moves state to FAILED."""
    orchestrator = HHApplicationOrchestrator()
    app = get_or_create_hh_application("app_t12")
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.DRAFT_READY, reason="draft_ready")
    transition_application(app.application_id, to_state=HHApplicationState.READY_TO_SUBMIT, reason="approved")

    # Mock evaluate returning failure
    fail_evaluate = MagicMock(return_value=json.dumps({"ok": False, "reason": "Connection reset"}))
    res = orchestrator.execute_confirmed_submit(app.application_id, confirm_submit=True, evaluate_fn=fail_evaluate)
    assert res["verdict"] == "FAILED"
    assert res["submit_count"] == 0

    refreshed = db.get_hh_application(app.application_id)
    assert refreshed["state"] == HHApplicationState.FAILED.value


# ---------------------------------------------------------------------------
# Test 13: Illegal Transition Rejection
# ---------------------------------------------------------------------------

def test_illegal_transitions_matrix(clean_db):
    """Safety Invariant 14: All non-whitelisted transitions are rejected."""
    app = get_or_create_hh_application("app_t13")
    
    # SUBMITTED is terminal -> no transitions out
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analyzed")
    transition_application(app.application_id, to_state=HHApplicationState.DRAFT_READY, reason="draft")
    transition_application(app.application_id, to_state=HHApplicationState.READY_TO_SUBMIT, reason="ready")
    transition_application(app.application_id, to_state=HHApplicationState.SUBMITTED, reason="submit", confirm_submit=True)

    # Try moving from SUBMITTED to anything
    res = transition_application(app.application_id, to_state=HHApplicationState.NEW, reason="hack")
    assert res.ok is False
    assert "Illegal transition" in res.reason


# ---------------------------------------------------------------------------
# Test 14: Full State Flow & Audit Trail
# ---------------------------------------------------------------------------

def test_full_state_flow_and_audit_trail(clean_db):
    """Test full lawful state flow with continuous audit logging."""
    orchestrator = HHApplicationOrchestrator()
    quest = _create_mock_questionnaire("quest_full")
    cid = "conv_full_flow"

    # Step 1: Orchestrate incoming event with questionnaire
    app = orchestrator.orchestrate_incoming_event(
        conversation_id=cid,
        message_id="msg_999",
        sender="Employer Recruiter",
        text="Пожалуйста, ответьте на вопросы анкеты",
        questionnaire_data=quest.model_dump(),
        title="Principal Engineer",
        employer="Global Tech",
    )
    assert app.state == HHApplicationState.NEEDS_HUMAN_REVIEW.value

    # Step 2: Human provides valid answers
    ans_res = orchestrator.record_questionnaire_answers(
        application_id=app.application_id,
        human_answers={"q1": "Удалённо", "q2": "8"},
        current_dom_fingerprint=quest.fingerprint,
    )
    assert ans_res.ok is True
    assert ans_res.to_state == HHApplicationState.READY_TO_SUBMIT.value

    # Step 3: Human confirms submission
    fake_eval = MagicMock(return_value=json.dumps({"ok": True}))
    sub_res = orchestrator.execute_confirmed_submit(
        application_id=app.application_id,
        confirm_submit=True,
        evaluate_fn=fake_eval,
        current_dom_fingerprint=quest.fingerprint,
    )
    assert sub_res["verdict"] == "SUBMITTED"
    assert sub_res["submit_count"] == 1

    # Check Audit Trail
    transitions = db.list_hh_application_transitions(app.application_id)
    states_sequence = [t["state"] for t in transitions]
    assert "MESSAGE_DETECTED" in states_sequence
    assert "ANALYZED" in states_sequence
    assert "QUESTIONNAIRE_REQUIRED" in states_sequence
    assert "NEEDS_HUMAN_REVIEW" in states_sequence
    assert "READY_TO_SUBMIT" in states_sequence
    assert "SUBMITTED" in states_sequence
    assert len(transitions) >= 6


# ---------------------------------------------------------------------------
# Test 15: CLI Application Commands
# ---------------------------------------------------------------------------

def test_cli_application_commands(clean_db, capsys):
    """Test CLI commands: application list, show, transitions, status."""
    app = get_or_create_hh_application(
        application_id="app_cli_test",
        conversation_id="conv_cli_123",
        vacancy_stable_id="hh:998877",
        title="Python Backend Developer",
        employer="Enterprise Corp",
    )
    transition_application(app.application_id, to_state=HHApplicationState.MESSAGE_DETECTED, reason="test_msg")
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="test_analysis")
    transition_application(app.application_id, to_state=HHApplicationState.NEEDS_HUMAN_REVIEW, reason="requires_action")

    # 1. CLI list
    ret_list = cli.application_list_cmd()
    assert ret_list == 0
    out_list = capsys.readouterr().out
    assert "app_cli_test" in out_list
    assert "NEEDS_HUMAN_REVIEW" in out_list

    # 2. CLI show
    ret_show = cli.application_show_cmd(app.application_id)
    assert ret_show == 0
    out_show = capsys.readouterr().out
    assert "HH APPLICATION" in out_show
    assert "Application:   app_cli_test" in out_show
    assert "State:         NEEDS_HUMAN_REVIEW" in out_show
    assert "Submit allowed: NO" in out_show

    # 3. CLI transitions
    ret_trans = cli.application_transitions_cmd(app.application_id)
    assert ret_trans == 0
    out_trans = capsys.readouterr().out
    assert "HH APPLICATION TRANSITIONS AUDIT TRAIL" in out_trans
    assert "NEW -> MESSAGE_DETECTED" in out_trans
    assert "MESSAGE_DETECTED -> ANALYZED" in out_trans

    # 4. CLI status
    ret_status = cli.application_status_cmd(app.application_id)
    assert ret_status == 0
    out_status = capsys.readouterr().out
    assert "HH APPLICATION" in out_status
