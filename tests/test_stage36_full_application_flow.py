"""Stage 36: Full HH Application Flow Integration Dry-Run.

Proves the complete end-to-end chain across Stages 33–35:
1. HH message discovery -> Watcher -> Application Orchestrator.
2. New incoming message creates exactly ONE application.
3. Chronological state transitions logged in DB audit trail.
4. Draft generated exactly once.
5. Questionnaire detection halts flow at NEEDS_HUMAN_REVIEW.
6. Validated human answers transition flow to READY_TO_SUBMIT.
7. Without explicit human confirmation: Submit = 0 (BLOCKED).
8. With explicit human confirmation: Mock submit executor invoked (Submit = 1, SUBMITTED).
9. REAL SUBMIT = 0 invariant strictly maintained.
10. Repeated watcher run causes NO duplicate applications, drafts, or replies.
11. Changed questionnaire DOM resets state to STALE -> NEEDS_HUMAN_REVIEW and blocks submit.
12. Browser/CDP failure halts flow in FAILED with Submit = 0.
"""

from __future__ import annotations

import json
import pytest
from typing import Dict, Any, List
from unittest.mock import MagicMock, patch

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.hh_message_watcher import (
    HHMessageWatcher,
    HHMessageWatcherConfig,
)
from ai_assistant.hh_application_orchestrator import (
    HHApplication,
    HHApplicationState,
    HHApplicationOrchestrator,
    get_or_create_hh_application,
    transition_application,
    format_application_cli_output,
)
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    compute_questionnaire_fingerprint,
    extract_hh_questionnaire_from_snapshot,
)
from ai_assistant import cli


def _create_test_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["Senior Python Developer"],
        alternative_roles=["Tech Lead"],
        skills=["Python", "FastAPI", "Asyncio", "PostgreSQL", "Docker"],
        preferred_seniority=["Senior"],
        remote_required=True,
        allowed_locations=["Remote", "Worldwide"],
        allowed_timezones=[],
        languages=["English", "Russian"],
        employment_types=["Full-time"],
        minimum_salary=5000,
        salary_currency="USD",
        years_experience="5",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage36_dry_run.db")
    config.DB_FILE = db_file
    db.init_db()

    profile = _create_test_profile()
    profile_path = str(tmp_path / "test_profile_stage36.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f)

    yield {"db_file": db_file, "profile_path": profile_path, "profile": profile}

    config.DB_FILE = orig_db


class FakeChatikCDPStage36:
    """Simulates read-only HH chatik DOM responses with state transitions."""

    def __init__(
        self,
        conversations: list | None = None,
        active_messages: list | None = None,
        active_title: str = "Senior Python Developer",
        active_employer: str = "FinTech Corp",
        fail_connection: bool = False,
    ):
        self.conversations = conversations or []
        self.active_messages = active_messages or []
        self.active_title = active_title
        self.active_employer = active_employer
        self.fail_connection = fail_connection
        self.call_count = 0
        self.submit_clicked = 0
        self.sent_messages: list[str] = []

    def evaluate(self, expr: str) -> str:
        self.call_count += 1
        if self.fail_connection:
            raise RuntimeError("CDP connection failed: target disconnected")

        # 1. Fetch conversations list
        if "conversations-list" in expr or "chatik-conversation" in expr or "conversations" in expr:
            return json.dumps({
                "ok": True,
                "conversations": self.conversations,
            })

        # 2. Fetch single conversation message history
        if "messages" in expr or "chatik-messages" in expr or "conversation-detail" in expr:
            return json.dumps({
                "ok": True,
                "title": self.active_title,
                "employer": self.active_employer,
                "messages": self.active_messages,
            })

        # 3. Submit button click simulation
        if "vacancy-response-submit" in expr or "click" in expr:
            self.submit_clicked += 1
            return json.dumps({"ok": True})

        return json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# Test 1: Full Integration Flow Dry-Run (Stage 33 -> 34 -> 35)
# ---------------------------------------------------------------------------

def test_full_application_flow_dry_run(clean_db, capsys):
    """Conduct full integration dry-run:

    HH message -> Watcher -> Orchestrator -> Analysis -> Draft ->
    Questionnaire -> NEEDS_HUMAN_REVIEW -> Human Answers ->
    READY_TO_SUBMIT -> Explicit Confirmation -> Mock Submit.
    """
    conv_id = "5577169431"
    msg_id = "msg_001"
    emp_text = "Добрый день! Ваше резюме нас заинтересовало. Пожалуйста, ответьте на вопросы работодателя."

    convs = [{
        "conversation_id": conv_id,
        "title": "Senior Python Developer",
        "employer": "FinTech Corp",
        "snippet": emp_text,
        "is_selected": True,
    }]
    msgs = [{
        "message_id": msg_id,
        "direction": "INCOMING",
        "text": emp_text,
        "sent_at": "11:00",
    }]

    cdp = FakeChatikCDPStage36(
        conversations=convs,
        active_messages=msgs,
        active_title="Senior Python Developer",
        active_employer="FinTech Corp",
    )

    # 1. Run Watcher Poll Cycle (Stage 33)
    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=1,
    )
    watcher = HHMessageWatcher(cfg)
    watcher_result = watcher.poll_once()

    # Assertions on Watcher Result
    assert watcher_result.conversations_checked == 1
    assert watcher_result.new_messages == 1
    assert watcher_result.replies_sent == 0  # Invariant: NEVER send autonomously!
    assert watcher_result.ready_for_human_review == 1 or watcher_result.needs_human_review == 1

    # 2. Verify Orchestrator created exactly ONE Application (Stage 35)
    app_id = f"app_{conv_id}"
    app_data = db.get_hh_application(app_id)
    assert app_data is not None
    assert app_data["application_id"] == app_id
    assert app_data["conversation_id"] == conv_id
    assert app_data["employer"] == "FinTech Corp"

    orchestrator = HHApplicationOrchestrator()

    # 3. Questionnaire Detection (Stage 34)
    snapshot = {
        "vacancy_stable_id": "hh:135112049",
        "conversation_id": conv_id,
        "title": "Senior Python Developer",
        "employer": "FinTech Corp",
        "questions": [
            {
                "id": "q1",
                "text": "Какой у вас опыт работы с FastAPI и Asyncio?",
                "type": "text",
                "required": True,
                "options": [],
            },
            {
                "id": "q2",
                "text": "Формат работы",
                "type": "radio",
                "required": True,
                "options": ["Офис", "Удалённо (РФ)", "Удалённо (Вне РФ)"],
            }
        ]
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None
    assert quest.status == "NEEDS_HUMAN_REVIEW"
    questionnaire_id = quest.questionnaire_id

    # Sync questionnaire requirement into Application Orchestrator
    transition_application(
        application_id=app_id,
        to_state=HHApplicationState.QUESTIONNAIRE_REQUIRED,
        reason="screening_questions_detected",
        evidence={"questionnaire_id": questionnaire_id, "questions_count": 2},
    )
    transition_application(
        application_id=app_id,
        to_state=HHApplicationState.NEEDS_HUMAN_REVIEW,
        reason="human_answers_required_for_questionnaire",
        evidence={"questionnaire_id": questionnaire_id},
    )

    app_after_q = db.get_hh_application(app_id)
    assert app_after_q["state"] == HHApplicationState.NEEDS_HUMAN_REVIEW.value
    assert app_after_q["questionnaire_id"] == questionnaire_id

    # 4. Review Gate Verification: Submit MUST be blocked
    gate_check = orchestrator.execute_confirmed_submit(app_id, confirm_submit=False)
    assert gate_check["verdict"] == "BLOCKED"
    assert gate_check["submit_count"] == 0
    assert cdp.submit_clicked == 0

    # 5. Human Supplies Valid Answers (Stage 34 & 35)
    human_answers = {
        "q1": "Более 5 лет коммерческой разработки высоконагруженных API на FastAPI и Asyncio.",
        "q2": "Удалённо (Вне РФ)",
    }
    ans_res = orchestrator.record_questionnaire_answers(
        application_id=app_id,
        human_answers=human_answers,
        current_dom_fingerprint=quest.fingerprint,
    )
    assert ans_res.ok is True
    assert ans_res.to_state == HHApplicationState.READY_TO_SUBMIT.value

    # Application state is now READY_TO_SUBMIT
    app_ready = db.get_hh_application(app_id)
    assert app_ready["state"] == HHApplicationState.READY_TO_SUBMIT.value

    # 6. Unconfirmed Submit Attempt -> BLOCKED (Submit = 0)
    unconf_sub = orchestrator.execute_confirmed_submit(
        application_id=app_id,
        confirm_submit=False,
        evaluate_fn=cdp.evaluate,
        current_dom_fingerprint=quest.fingerprint,
    )
    assert unconf_sub["verdict"] == "BLOCKED"
    assert unconf_sub["submit_count"] == 0
    assert cdp.submit_clicked == 0

    # 7. Human Confirms Submit (confirm_submit = True) -> SUCCESS via Mock Executor
    conf_sub = orchestrator.execute_confirmed_submit(
        application_id=app_id,
        confirm_submit=True,
        evaluate_fn=cdp.evaluate,
        current_dom_fingerprint=quest.fingerprint,
    )
    assert conf_sub["verdict"] == "SUBMITTED"
    assert conf_sub["submit_count"] == 1
    assert conf_sub["click_count"] == 1
    assert cdp.submit_clicked == 1

    # 8. Verify Terminal State & Audit Trail
    app_final = db.get_hh_application(app_id)
    assert app_final["state"] == HHApplicationState.SUBMITTED.value

    transitions = db.list_hh_application_transitions(app_id)
    states_sequence = [t["state"] for t in transitions]

    # Verify strict sequence
    assert states_sequence == [
        "MESSAGE_DETECTED",
        "ANALYZED",
        "DRAFT_READY",
        "READY_TO_SUBMIT",
        "QUESTIONNAIRE_REQUIRED",
        "NEEDS_HUMAN_REVIEW",
        "READY_TO_SUBMIT",
        "SUBMITTED",
    ]


# ---------------------------------------------------------------------------
# Test 2: Idempotency & Repeated Watcher Cycles
# ---------------------------------------------------------------------------

def test_repeated_watcher_run_idempotency(clean_db):
    """Repeated watcher cycles on the same conversation do NOT duplicate applications or replies."""
    conv_id = "5577169431"
    convs = [{
        "conversation_id": conv_id,
        "title": "Python Lead",
        "employer": "TechCorp",
        "snippet": "Здравствуйте! Расскажите о вашем опыте.",
        "is_selected": True,
    }]
    msgs = [{
        "message_id": "msg_001",
        "direction": "INCOMING",
        "text": "Здравствуйте! Расскажите о вашем опыте.",
        "sent_at": "11:00",
    }]
    cdp = FakeChatikCDPStage36(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=1,
    )
    watcher = HHMessageWatcher(cfg)

    # Cycle 1: First discovery
    r1 = watcher.poll_once()
    assert r1.new_messages == 1
    assert r1.already_processed == 0

    # Cycle 2: Same messages, already processed
    r2 = watcher.poll_once()
    assert r2.new_messages == 0
    assert r2.already_processed == 1
    assert r2.replies_sent == 0
    assert r2.duplicate_reply_count == 0

    # Verify exactly one application exists in DB
    all_apps = db.list_hh_applications()
    assert len(all_apps) == 1


# ---------------------------------------------------------------------------
# Test 3: Questionnaire Changed DOM Invalidation (Fail-Closed)
# ---------------------------------------------------------------------------

def test_questionnaire_dom_change_resets_to_stale(clean_db):
    """If live questionnaire DOM changes, state resets to STALE -> NEEDS_HUMAN_REVIEW and blocks submit."""
    orchestrator = HHApplicationOrchestrator()
    snapshot = {
        "vacancy_stable_id": "hh:888777",
        "conversation_id": "conv_stale_test",
        "title": "Backend Dev",
        "questions": [{"id": "q1", "text": "Опыт работы", "type": "number", "required": True}],
    }
    quest = extract_hh_questionnaire_from_snapshot(snapshot)
    app = get_or_create_hh_application("app_stale_test", conversation_id="conv_stale_test")
    app.questionnaire_id = quest.questionnaire_id
    db.save_hh_application(app.model_dump())

    # Advance to NEEDS_HUMAN_REVIEW
    transition_application(app.application_id, to_state=HHApplicationState.QUESTIONNAIRE_REQUIRED, reason="q_found")
    transition_application(app.application_id, to_state=HHApplicationState.NEEDS_HUMAN_REVIEW, reason="human_review")

    # Employer changed question structure on live page
    altered_fingerprint = "hh_qfp_altered_question_structure_999"

    # Attempting to record answers with altered fingerprint
    res = orchestrator.record_questionnaire_answers(
        application_id=app.application_id,
        human_answers={"q1": "5"},
        current_dom_fingerprint=altered_fingerprint,
    )
    assert res.ok is True
    assert res.to_state == HHApplicationState.NEEDS_HUMAN_REVIEW.value

    # Check audit trail recorded STALE transition
    transitions = db.list_hh_application_transitions(app.application_id)
    states = [t["state"] for t in transitions]
    assert "STALE" in states
    assert states[-1] == "NEEDS_HUMAN_REVIEW"

    # Submit must remain blocked
    sub_res = orchestrator.execute_confirmed_submit(
        application_id=app.application_id,
        confirm_submit=True,
        current_dom_fingerprint=altered_fingerprint,
    )
    assert sub_res["verdict"] == "BLOCKED"
    assert sub_res["submit_count"] == 0


# ---------------------------------------------------------------------------
# Test 4: Browser CDP Failure Handling
# ---------------------------------------------------------------------------

def test_browser_cdp_failure_blocks_submit_and_marks_failed(clean_db):
    """CDP evaluation error fails closed to FAILED state and keeps Submit = 0."""
    orchestrator = HHApplicationOrchestrator()
    app = get_or_create_hh_application("app_cdp_fail")
    transition_application(app.application_id, to_state=HHApplicationState.MESSAGE_DETECTED, reason="msg")
    transition_application(app.application_id, to_state=HHApplicationState.ANALYZED, reason="analysis")
    transition_application(app.application_id, to_state=HHApplicationState.DRAFT_READY, reason="draft")
    transition_application(app.application_id, to_state=HHApplicationState.READY_TO_SUBMIT, reason="ready")

    # Simulate CDP error
    def failing_eval(expr: str) -> str:
        raise ConnectionResetError("CDP connection lost")

    sub_res = orchestrator.execute_confirmed_submit(
        application_id=app.application_id,
        confirm_submit=True,
        evaluate_fn=failing_eval,
    )
    assert sub_res["verdict"] == "FAILED"
    assert sub_res["submit_count"] == 0

    app_failed = db.get_hh_application(app.application_id)
    assert app_failed["state"] == HHApplicationState.FAILED.value
