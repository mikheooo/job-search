"""Stage 34: Tests for Human-in-the-Loop HH Questionnaire.

Verifies:
1. Questionnaire detected -> Submit = 0 (stops before submit with NEEDS_HUMAN_REVIEW).
2. Required question without answer -> Submit = 0 (BLOCKED).
3. Partial answers -> Submit = 0 (BLOCKED).
4. Invalid option -> Submit = 0 (BLOCKED).
5. Unknown question_id -> Submit = 0 (BLOCKED).
6. Changed questionnaire DOM -> Submit = 0 (fails closed to NEEDS_HUMAN_REVIEW).
7. Human answers complete + explicit confirmation -> Submit MAY proceed (SUBMITTED).
8. Human answers complete but NO explicit confirmation -> Submit = 0 (READY_TO_SUBMIT).
9. Idempotency: repeated extraction / runs do not create duplicate records or re-submit.
10. CLI commands: questionnaire list, show, answer, submit.
11. Existing zero-autonomous-send invariant remains strictly intact.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock

from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_questionnaire import (
    HHQuestionItem,
    HHQuestionnaire,
    HHQuestionStatus,
    QuestionnaireValidationResult,
    compute_questionnaire_fingerprint,
    discover_hh_questionnaire_from_snapshot,
    format_questionnaire_cli_output,
    validate_human_answers,
    submit_questionnaire_response,
)
from ai_assistant import cli


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage34_questionnaire.db")
    config.DB_FILE = db_file
    db.init_db()

    yield {"db_file": db_file}

    config.DB_FILE = orig_db


def _make_sample_questionnaire(qid: str = "quest_sample_01") -> HHQuestionnaire:
    q1 = HHQuestionItem(
        question_id="q1",
        text="Где располагается ваше фактическое место работы?",
        question_type="radio",
        required=True,
        options=["Москва", "Санкт-Петербург", "Удалённо (РФ)", "Удалённо (Вне РФ)"],
    )
    q2 = HHQuestionItem(
        question_id="q2",
        text="Сколько полных лет опыта работы с Python у вас есть?",
        question_type="number",
        required=True,
        options=[],
    )
    q3 = HHQuestionItem(
        question_id="q3",
        text="Укажите ссылку на портфолио или GitHub (если есть)",
        question_type="text",
        required=False,
        options=[],
    )
    questions = [q1, q2, q3]
    fp = compute_questionnaire_fingerprint(questions)
    return HHQuestionnaire(
        questionnaire_id=qid,
        vacancy_stable_id="hh:135112049",
        conversation_id="conv_9988",
        title="Senior Python Backend Developer",
        employer="FinTech Corp",
        questions=questions,
        answers={},
        status=HHQuestionStatus.NEEDS_HUMAN_REVIEW.value,
        fingerprint=fp,
        created_at="2026-08-30T10:00:00",
        updated_at="2026-08-30T10:00:00",
    )


class FakeSubmitCDP:
    """Mock CDP evaluate function for testing submit actions."""

    def __init__(self, submit_success: bool = True):
        self.submit_success = submit_success
        self.clicked_buttons: list[str] = []

    def evaluate(self, expr: str) -> str:
        if "vacancy-response-submit" in expr or "click" in expr:
            self.clicked_buttons.append(expr)
            if self.submit_success:
                return json.dumps({"ok": True})
            return json.dumps({"ok": False, "reason": "Submit button disabled"})
        return json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# Test 1: Questionnaire detected -> Submit = 0 (NEEDS_HUMAN_REVIEW)
# ---------------------------------------------------------------------------

def test_questionnaire_detected_stops_before_submit(clean_db):
    """When a questionnaire is detected, flow halts before submit with Submit = 0."""
    snapshot = {
        "vacancy_stable_id": "hh:135112049",
        "title": "Python Developer",
        "employer": "TechCorp",
        "questions": [
            {
                "id": "q1",
                "text": "Готовы ли к командировкам?",
                "type": "radio",
                "required": True,
                "options": ["Да", "Нет"],
            },
            {
                "id": "q2",
                "text": "Опыт работы в годах",
                "type": "number",
                "required": True,
                "options": [],
            }
        ]
    }
    quest = discover_hh_questionnaire_from_snapshot(snapshot)
    assert quest is not None
    assert quest.status == HHQuestionStatus.NEEDS_HUMAN_REVIEW.value
    assert len(quest.questions) == 2
    assert quest.fingerprint.startswith("hh_qfp_")

    # Invariant: Saved in DB with NEEDS_HUMAN_REVIEW
    saved = db.get_hh_questionnaire(quest.questionnaire_id)
    assert saved is not None
    assert saved["status"] == "NEEDS_HUMAN_REVIEW"

    # Attempting submit without human answers must fail closed (Submit = 0)
    cdp = FakeSubmitCDP()
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers={},
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 2: Required question without answer -> Submit = 0
# ---------------------------------------------------------------------------

def test_required_question_without_answer_blocks_submit(clean_db):
    """Missing required answers fail validation and block submit (Submit = 0)."""
    quest = _make_sample_questionnaire("quest_req_test")
    db.save_hh_questionnaire(quest.model_dump())

    # Only answer optional q3; omit required q1 and q2
    answers = {"q3": "https://github.com/test"}
    val = validate_human_answers(quest, answers)
    assert val.ok is False
    assert "q1" in val.missing_required
    assert "q2" in val.missing_required

    cdp = FakeSubmitCDP()
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 3: Partial answers -> Submit = 0
# ---------------------------------------------------------------------------

def test_partial_answers_blocks_submit(clean_db):
    """Providing answers to only some required questions blocks submit."""
    quest = _make_sample_questionnaire("quest_partial_test")
    db.save_hh_questionnaire(quest.model_dump())

    # Answer q1 (required), but omit q2 (required)
    answers = {"q1": "Удалённо (Вне РФ)"}
    val = validate_human_answers(quest, answers)
    assert val.ok is False
    assert val.missing_required == ["q2"]

    cdp = FakeSubmitCDP()
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 4: Invalid option -> Submit = 0
# ---------------------------------------------------------------------------

def test_invalid_option_blocks_submit(clean_db):
    """Providing an option outside of the allowed options list blocks submit."""
    quest = _make_sample_questionnaire("quest_opt_test")
    db.save_hh_questionnaire(quest.model_dump())

    # 'Луна' is not in ['Москва', 'Санкт-Петербург', 'Удалённо (РФ)', 'Удалённо (Вне РФ)']
    answers = {
        "q1": "Луна",
        "q2": "5",
    }
    val = validate_human_answers(quest, answers)
    assert val.ok is False
    assert len(val.invalid_options) == 1
    assert "not in options" in val.invalid_options[0]

    cdp = FakeSubmitCDP()
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 5: Unknown question_id -> Submit = 0
# ---------------------------------------------------------------------------

def test_unknown_question_id_blocks_submit(clean_db):
    """Supplying answers for nonexistent question IDs blocks submit."""
    quest = _make_sample_questionnaire("quest_unk_test")
    db.save_hh_questionnaire(quest.model_dump())

    answers = {
        "q1": "Удалённо (Вне РФ)",
        "q2": "5",
        "nonexistent_field_xyz": "value",
    }
    val = validate_human_answers(quest, answers)
    assert val.ok is False
    assert "nonexistent_field_xyz" in val.unknown_questions
    assert "Unknown question_id" in val.reason

    cdp = FakeSubmitCDP()
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 6: Changed questionnaire DOM -> Submit = 0 (NEEDS_HUMAN_REVIEW)
# ---------------------------------------------------------------------------

def test_changed_questionnaire_dom_fails_closed(clean_db):
    """If the live page questionnaire structure changes after extraction, submit fails closed."""
    quest = _make_sample_questionnaire("quest_changed_test")
    db.save_hh_questionnaire(quest.model_dump())

    answers = {
        "q1": "Удалённо (Вне РФ)",
        "q2": "5",
    }
    # Simulate a different DOM fingerprint (e.g. employer added a new question)
    diff_fingerprint = "hh_qfp_different_structure_hash_999"

    val = validate_human_answers(quest, answers, current_dom_fingerprint=diff_fingerprint)
    assert val.ok is False
    assert val.status == HHQuestionStatus.NEEDS_HUMAN_REVIEW.value
    assert "changed" in val.reason.lower()

    cdp = FakeSubmitCDP()
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
        current_dom_fingerprint=diff_fingerprint,
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 7: Human answers complete + explicit confirmation -> Submit proceeds
# ---------------------------------------------------------------------------

def test_complete_answers_with_confirmation_proceeds_to_submit(clean_db):
    """When all required questions have valid answers and explicit confirmation is passed, submit proceeds."""
    quest = _make_sample_questionnaire("quest_ok_test")
    db.save_hh_questionnaire(quest.model_dump())

    answers = {
        "q1": "Удалённо (Вне РФ)",
        "q2": "5",
        "q3": "https://github.com/candidate",
    }
    val = validate_human_answers(quest, answers, current_dom_fingerprint=quest.fingerprint)
    assert val.ok is True
    assert val.status == HHQuestionStatus.READY_TO_SUBMIT.value

    cdp = FakeSubmitCDP(submit_success=True)
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
        current_dom_fingerprint=quest.fingerprint,
    )
    assert res.verdict == "SUBMITTED"
    assert res.status == HHQuestionStatus.SUBMITTED.value
    assert res.submit_count == 1
    assert res.click_count == 1
    assert len(cdp.clicked_buttons) == 1

    # Check state updated in DB
    updated = db.get_hh_questionnaire(quest.questionnaire_id)
    assert updated["status"] == "SUBMITTED"
    assert updated["answers"]["q1"] == "Удалённо (Вне РФ)"


# ---------------------------------------------------------------------------
# Test 8: Complete answers WITHOUT explicit confirmation -> Submit = 0
# ---------------------------------------------------------------------------

def test_complete_answers_without_confirmation_blocks_submit(clean_db):
    """Even if all answers are valid, absence of explicit confirmation keeps Submit = 0."""
    quest = _make_sample_questionnaire("quest_no_conf_test")
    db.save_hh_questionnaire(quest.model_dump())

    answers = {
        "q1": "Удалённо (Вне РФ)",
        "q2": "5",
    }
    cdp = FakeSubmitCDP()
    res = submit_questionnaire_response(
        questionnaire_id=quest.questionnaire_id,
        human_answers=answers,
        evaluate_fn=cdp.evaluate,
        confirm_submit=False,  # NO explicit confirmation!
    )
    assert res.verdict == "BLOCKED"
    assert res.submit_count == 0
    assert res.status == HHQuestionStatus.READY_TO_SUBMIT.value
    assert len(cdp.clicked_buttons) == 0


# ---------------------------------------------------------------------------
# Test 9: Idempotency: repeated extraction does not duplicate questionnaires
# ---------------------------------------------------------------------------

def test_questionnaire_idempotency(clean_db):
    """Extracting the same questionnaire twice upserts the record without duplicating."""
    snapshot = {
        "vacancy_stable_id": "hh:999888",
        "conversation_id": "c999",
        "title": "Python Engineer",
        "questions": [
            {"id": "q1", "text": "Опыт работы?", "type": "number", "required": True}
        ]
    }
    q1 = discover_hh_questionnaire_from_snapshot(snapshot)
    q2 = discover_hh_questionnaire_from_snapshot(snapshot)

    assert q1.questionnaire_id == q2.questionnaire_id
    assert q1.fingerprint == q2.fingerprint

    all_quests = db.list_hh_questionnaires()
    assert len(all_quests) == 1

    # Attempting to submit an already SUBMITTED questionnaire is blocked
    db.update_hh_questionnaire_answers(q1.questionnaire_id, {"q1": "5"}, new_status=HHQuestionStatus.SUBMITTED.value)
    cdp = FakeSubmitCDP()
    res_repeat = submit_questionnaire_response(
        questionnaire_id=q1.questionnaire_id,
        human_answers={"q1": "5"},
        evaluate_fn=cdp.evaluate,
        confirm_submit=True,
    )
    assert res_repeat.verdict == "BLOCKED"
    assert res_repeat.submit_count == 0
    assert "already submitted" in res_repeat.reason


# ---------------------------------------------------------------------------
# Test 10: CLI questionnaire formatting and commands
# ---------------------------------------------------------------------------

def test_cli_questionnaire_formatting_and_lifecycle(clean_db, capsys):
    """Test CLI formatting output, answering, and gated submission."""
    quest = _make_sample_questionnaire("quest_cli_test")
    db.save_hh_questionnaire(quest.model_dump())

    # 1. Format check
    formatted = format_questionnaire_cli_output(quest)
    assert "HH QUESTIONNAIRE — HUMAN INPUT REQUIRED" in formatted
    assert "Conversation: conv_9988" in formatted
    assert "Vacancy:      Senior Python Backend Developer" in formatted
    assert "Q1 [required]" in formatted
    assert "Options:" in formatted
    assert "- Москва" in formatted
    assert "Status: NEEDS_HUMAN_REVIEW" in formatted
    assert "Submit: BLOCKED" in formatted

    # 2. CLI show
    ret_show = cli.questionnaire_show_cmd(quest.questionnaire_id)
    assert ret_show == 0
    out_show = capsys.readouterr().out
    assert "HH QUESTIONNAIRE — HUMAN INPUT REQUIRED" in out_show

    # 3. CLI answer with valid JSON
    ret_ans = cli.questionnaire_answer_cmd(
        quest.questionnaire_id,
        answers_json=json.dumps({"q1": "Удалённо (Вне РФ)", "q2": "6"}),
    )
    assert ret_ans == 0
    out_ans = capsys.readouterr().out
    assert "Answers successfully recorded and validated" in out_ans

    # 4. CLI submit without confirmation -> blocked (code 1)
    ret_sub_no_conf = cli.questionnaire_submit_cmd(quest.questionnaire_id, confirm_submit=False)
    assert ret_sub_no_conf == 1
    out_sub_no_conf = capsys.readouterr().out
    assert "SUBMISSION GATE: EXPLICIT CONFIRMATION REQUIRED" in out_sub_no_conf
    assert "Submit Action:             BLOCKED (Submit = 0)" in out_sub_no_conf

    # 5. CLI submit with confirmation -> success (code 0)
    cdp = FakeSubmitCDP()
    ret_sub_conf = cli.questionnaire_submit_cmd(
        quest.questionnaire_id,
        confirm_submit=True,
        evaluate_fn=cdp.evaluate,
    )
    assert ret_sub_conf == 0
    out_sub_conf = capsys.readouterr().out
    assert "Verdict:                   SUBMITTED" in out_sub_conf
    assert "Submit Count:              1" in out_sub_conf
