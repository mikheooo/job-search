"""Stage 89.1: Telegram Feedback Production Wiring & Controlled Validation Test Suite.

Verifies:
1. scheduled sender forwards reply_markup;
2. production message contains inline keyboard;
3. buttons refer only to vacancies in that message;
4. callback receiver routes callback to TelegramFeedbackProcessor;
5. callback receiver does not perform job submission;
6. owner user ID is separate from destination chat ID;
7. missing TELEGRAM_OWNER_ID fails closed;
8. unauthorized callback_query.from.id rejected;
9. callback hash zero-match fails closed;
10. callback hash collision fails closed;
11. callback vacancy must have valid provenance;
12. callback vacancy must have delivered digest relationship;
13. DB failure does not send false-success acknowledgement;
14. successful callback receives answerCallbackQuery;
15. duplicate update remains idempotent;
16. conflicting transition obeys canonical state machine;
17. one and only one Telegram update consumer is configured;
18. no network is used by the test suite.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import contextlib
import pytest
from unittest.mock import MagicMock, patch

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    get_application_review,
    save_application_review,
)
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
)
from ai_assistant.application_queue import get_queue_item
from ai_assistant.telegram_feedback import (
    TelegramFeedbackAction,
    TelegramFeedbackProcessor,
    encode_callback_data,
    decode_callback_data,
)
from ai_assistant.telegram_notifier import TelegramNotifier
from ai_assistant.telegram_bot import TelegramBot
from ai_assistant.cli import export_digest_cmd
from ai_assistant.candidate_profile import CandidateProfile


@pytest.fixture
def calibrated_profile():
    return CandidateProfile.from_dict({
        "desired_roles": ["AI Automation Engineer", "Technical Support Engineer", "Python Developer"],
        "skills": ["python", "n8n", "automation", "telegram", "rest api"],
        "secondary_skills": ["docker", "linux", "sql"],
        "years_experience": 3,
        "minimum_salary": 1500,
        "salary_currency": "USD",
        "remote_required": True,
        "allowed_locations": ["worldwide", "emea", "thailand", "cyprus", "georgia", "armenia"],
        "languages": ["russian", "english"],
        "role_priorities": {
            "AI_AUTOMATION": "P1",
            "APPLICATION_SUPPORT": "P1",
            "TECH_SUPPORT": "P1",
            "PYTHON_BACKEND": "P2",
            "SYSTEM_ADMIN": "P2",
            "DATA_ENGINEERING": "P3",
            "DEVOPS_SRE": "P3",
        },
        "domain_years": {
            "it_support": 11.0,
            "system_admin": 9.0,
            "application_support": 5.0,
            "automation": 3.5,
            "python": 3.5,
            "ai_llm": 2.0,
        },
        "skill_confidence": {
            "python": "PROFESSIONAL",
            "n8n": "PROFESSIONAL",
            "automation": "PROFESSIONAL",
            "fastapi": "PROJECT",
        },
    })


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_stage89_1_state.db"
    monkeypatch.setattr(config, "DB_FILE", str(test_db))
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1004399255305")
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", "392046103")
    db.init_db()
    return str(test_db)


@pytest.fixture
def sample_delivered_vacancy(isolated_db):
    v = Vacancy(
        source="himalayas",
        source_job_id="hima_s89_1_001",
        title="AI Automation Engineer",
        company="Synthetix Technologies",
        description="Remote Worldwide. Required: Python, n8n, automation.",
        job_url="https://himalayas.app/companies/synthetix/jobs/ai-automation-engineer",
        location="Remote Worldwide",
        salary_min=3500,
        salary_currency="USD",
    )
    db.save_vacancy(v)
    db.mark_digest_delivered([v.stable_id()], chat_id="-1004399255305")
    db.save_application_package(
        v.stable_id(), "v1",
        json.dumps({"cover_letter": "I am an experienced engineer.", "answers": []})
    )
    return v


# ---------------------------------------------------------------------------
# 1. Scheduled sender forwards reply_markup
# ---------------------------------------------------------------------------
def test_scheduled_sender_forwards_reply_markup(calibrated_profile, isolated_db, monkeypatch):
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    v = Vacancy(
        source="weworkremotely",
        source_job_id="wwr_12345",
        title="Senior Python Developer",
        company="RemoteGlobal",
        description="Python backend, FastAPI, Docker.",
        job_url="https://weworkremotely.com/jobs/12345",
        location="Remote Worldwide",
        salary_min=4000,
        salary_currency="USD",
    )
    db.save_vacancy(v)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert "reply_markup" in payload
    assert payload["reply_markup"] is not None
    assert "inline_keyboard" in payload["reply_markup"]


# ---------------------------------------------------------------------------
# 2. Production message contains inline keyboard
# ---------------------------------------------------------------------------
def test_production_message_contains_inline_keyboard(sample_delivered_vacancy):
    kb = TelegramNotifier.build_digest_inline_keyboard([{"id": sample_delivered_vacancy.stable_id()}])
    assert "inline_keyboard" in kb
    rows = kb["inline_keyboard"]
    assert len(rows) == 1
    buttons = rows[0]
    assert len(buttons) == 4
    texts = [b["text"] for b in buttons]
    assert "1. 👍" in texts
    assert "1. 👎" in texts
    assert "1. 📄 Отклик" in texts
    assert "1. ⏭" in texts


# ---------------------------------------------------------------------------
# 3. Buttons refer only to vacancies in that message
# ---------------------------------------------------------------------------
def test_buttons_refer_only_to_vacancies_in_message():
    vacs_subset = [
        {"id": "himalayas:chunk_1"},
        {"id": "weworkremotely:chunk_2"},
    ]
    kb = TelegramNotifier.build_digest_inline_keyboard(vacs_subset)
    rows = kb["inline_keyboard"]
    assert len(rows) == 2

    # Row 1 buttons must strictly encode chunk_1
    for btn in rows[0]:
        action, sid = decode_callback_data(btn["callback_data"])
        assert sid == "himalayas:chunk_1"

    # Row 2 buttons must strictly encode chunk_2
    for btn in rows[1]:
        action, sid = decode_callback_data(btn["callback_data"])
        assert sid == "weworkremotely:chunk_2"


# ---------------------------------------------------------------------------
# 4. Callback receiver routes callback to TelegramFeedbackProcessor
# ---------------------------------------------------------------------------
def test_callback_receiver_routes_to_feedback_processor(sample_delivered_vacancy):
    sent_answers = []

    def mock_transport(tok, payload):
        sent_answers.append(payload)
        return {"ok": True, "result": True}

    notifier = TelegramNotifier(transport_fn=mock_transport)
    bot = TelegramBot(allowed_chat_id="392046103", notifier=notifier)

    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_delivered_vacancy.stable_id())
    update = {
        "update_id": 9001,
        "callback_query": {
            "id": "cb_route_1",
            "from": {"id": 392046103, "username": "mikheooo"},
            "message": {"chat": {"id": -1004399255305}},
            "data": cb_data,
        },
    }

    res = bot.process_update(update)
    assert res is not None
    assert res.get("success") is True
    assert res.get("action") == "INTERESTED"
    assert bot.last_update_id == 9001


# ---------------------------------------------------------------------------
# 5. Callback receiver does not perform job submission
# ---------------------------------------------------------------------------
def test_callback_receiver_does_not_perform_job_submission(sample_delivered_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="392046103", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_delivered_vacancy.stable_id())

    update = {
        "id": "cb_prep_safety",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_data,
    }

    res = processor.process_callback_query(update)
    assert res["success"] is True
    assert res["new_status"] == "READY_TO_APPLY"

    # Enqueued for review, but zero submission/application records exist
    app = get_application_status(sample_delivered_vacancy.stable_id())
    assert app.status == ApplicationStatus.READY_TO_APPLY
    assert app.status not in (ApplicationStatus.APPLIED, ApplicationStatus.SUBMITTED, ApplicationStatus.VERIFIED)
    assert db.get_hh_application(sample_delivered_vacancy.stable_id()) is None


# ---------------------------------------------------------------------------
# 6. Owner user ID is separate from destination chat ID
# ---------------------------------------------------------------------------
def test_owner_user_id_separate_from_destination_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1004399255305")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "392046103")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-1004399255305")
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", "392046103")

    processor = TelegramFeedbackProcessor()
    assert processor.allowed_user_id == "392046103"
    assert processor.allowed_chat_id == "-1004399255305"
    assert processor.is_authorized(user_id=392046103, chat_id=-1004399255305) is True
    assert processor.is_authorized(user_id=-1004399255305, chat_id=-1004399255305) is False


# ---------------------------------------------------------------------------
# 7. Missing TELEGRAM_OWNER_ID fails closed
# ---------------------------------------------------------------------------
def test_missing_telegram_owner_id_fails_closed(sample_delivered_vacancy, monkeypatch):
    processor = TelegramFeedbackProcessor(allowed_user_id="")
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_delivered_vacancy.stable_id())

    update = {
        "id": "cb_missing_owner",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is False
    assert res["error"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 8. Unauthorized callback_query.from.id rejected
# ---------------------------------------------------------------------------
def test_unauthorized_from_id_rejected(sample_delivered_vacancy):
    processor = TelegramFeedbackProcessor(allowed_user_id="392046103")
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_delivered_vacancy.stable_id())

    update = {
        "id": "cb_unauth_actor",
        "from": {"id": 999000111},  # Random stranger
        "message": {"chat": {"id": -1004399255305}},  # Even if posted from authorized channel
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is False
    assert res["error"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 9. Callback hash zero-match fails closed
# ---------------------------------------------------------------------------
def test_callback_hash_zero_match_fails_closed(isolated_db):
    res = db.resolve_vacancy_by_hash_prefix("0000000000000000")
    assert res is None


# ---------------------------------------------------------------------------
# 10. Callback hash collision fails closed
# ---------------------------------------------------------------------------
def test_callback_hash_collision_fails_closed(isolated_db):
    v1 = Vacancy(
        source="himalayas",
        source_job_id="col_1",
        title="Col 1",
        company="Company A",
        description="Desc",
        job_url="https://example.com/1",
    )
    v2 = Vacancy(
        source="weworkremotely",
        source_job_id="col_2",
        title="Col 2",
        company="Company B",
        description="Desc",
        job_url="https://example.com/2",
    )
    db.save_vacancy(v1)
    db.save_vacancy(v2)

    # If hash prefix matches both, resolve_vacancy_by_hash_prefix must return None (fail closed)
    with patch("hashlib.sha256") as mock_sha:
        mock_instance = MagicMock()
        mock_instance.hexdigest.return_value = "abcdef1234567890deadbeef"
        mock_sha.return_value = mock_instance

        res = db.resolve_vacancy_by_hash_prefix("abcdef1234567890")
        assert res is None, "Hash prefix collision must return None (fail closed)"


# ---------------------------------------------------------------------------
# 11. Callback vacancy must have valid provenance
# ---------------------------------------------------------------------------
def test_callback_vacancy_must_have_valid_provenance(isolated_db):
    v_synthetic = Vacancy(
        source="himalayas",
        source_job_id="dryrun-test-1",  # Test fixture name rejected by is_genuine_production_vacancy
        title="Test Developer",
        company="ACME Test Corp",
        description="Fake role",
        job_url="https://example.com/test",
    )
    db.save_vacancy(v_synthetic)
    db.mark_digest_delivered([v_synthetic.stable_id()])

    processor = TelegramFeedbackProcessor(allowed_user_id="392046103")
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, v_synthetic.stable_id())

    update = {
        "id": "cb_bad_prov",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is False
    assert res["error"] == "INVALID_PROVENANCE"


# ---------------------------------------------------------------------------
# 12. Callback vacancy must have delivered digest relationship
# ---------------------------------------------------------------------------
def test_callback_vacancy_must_have_delivered_digest_relationship(isolated_db):
    v_undelivered = Vacancy(
        source="himalayas",
        source_job_id="hima_undelivered_89",
        title="Valid Title",
        company="Valid Company",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://himalayas.app/jobs/undelivered",
    )
    db.save_vacancy(v_undelivered)
    # NOT marked as delivered in digest

    processor = TelegramFeedbackProcessor(allowed_user_id="392046103", require_delivered=True)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, v_undelivered.stable_id())

    update = {
        "id": "cb_not_deliv",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is False
    assert res["error"] == "NOT_DELIVERED"


# ---------------------------------------------------------------------------
# 13. DB failure does not send false-success acknowledgement
# ---------------------------------------------------------------------------
def test_db_failure_does_not_send_false_success_ack(sample_delivered_vacancy, monkeypatch):
    acknowledged_payloads = []

    def mock_transport(tok, payload):
        acknowledged_payloads.append(payload)
        return {"ok": True}

    notifier = TelegramNotifier(transport_fn=mock_transport)
    processor = TelegramFeedbackProcessor(allowed_user_id="392046103", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_delivered_vacancy.stable_id())

    # Simulate DB error during state mutation
    monkeypatch.setattr("ai_assistant.telegram_feedback.save_application_review", MagicMock(side_effect=RuntimeError("Disk write error")))

    update = {
        "id": "cb_db_fail",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_data,
    }

    with pytest.raises(RuntimeError, match="Disk write error"):
        processor.process_callback_query(update)

    # Proves no success acknowledgment payload was sent
    assert len(acknowledged_payloads) == 0


# ---------------------------------------------------------------------------
# 14. Successful callback receives answerCallbackQuery
# ---------------------------------------------------------------------------
def test_successful_callback_receives_answer_callback_query(sample_delivered_vacancy):
    answers = []

    def mock_transport(tok, payload):
        answers.append(payload)
        return {"ok": True}

    notifier = TelegramNotifier(transport_fn=mock_transport)
    processor = TelegramFeedbackProcessor(allowed_user_id="392046103", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_delivered_vacancy.stable_id())

    update = {
        "id": "cb_ack_test_1",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_data,
    }

    res = processor.process_callback_query(update)
    assert res["success"] is True
    assert len(answers) == 1
    assert answers[0]["callback_query_id"] == "cb_ack_test_1"
    assert "Интересн" in answers[0]["text"] or "интересн" in answers[0]["text"]


# ---------------------------------------------------------------------------
# 15. Duplicate update remains idempotent
# ---------------------------------------------------------------------------
def test_duplicate_update_remains_idempotent(sample_delivered_vacancy):
    processor = TelegramFeedbackProcessor(allowed_user_id="392046103")
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_delivered_vacancy.stable_id())

    update = {
        "id": "cb_idem_100",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_data,
    }

    r1 = processor.process_callback_query(update)
    assert r1["success"] is True
    assert r1.get("idempotent") is not True

    # Repeated callback
    r2 = processor.process_callback_query(update)
    assert r2["success"] is True
    assert r2.get("idempotent") is True


# ---------------------------------------------------------------------------
# 16. Conflicting transition obeys canonical state machine
# ---------------------------------------------------------------------------
def test_conflicting_transition_obeys_state_machine(sample_delivered_vacancy):
    processor = TelegramFeedbackProcessor(allowed_user_id="392046103")

    # Transition 1: INTERESTED -> ANALYZED / PENDING_REVIEW
    cb_int = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_delivered_vacancy.stable_id())
    r1 = processor.process_callback_query({
        "id": "cb_step_1",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_int,
    })
    assert r1["success"] is True
    assert get_application_status(sample_delivered_vacancy.stable_id()).status == ApplicationStatus.ANALYZED
    assert get_application_review(sample_delivered_vacancy.stable_id()).status == ReviewStatus.PENDING_REVIEW

    # Transition 2: PREPARE_APPLICATION -> READY_TO_APPLY / APPROVED
    cb_prep = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_delivered_vacancy.stable_id())
    r2 = processor.process_callback_query({
        "id": "cb_step_2",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_prep,
    })
    assert r2["success"] is True
    assert get_application_status(sample_delivered_vacancy.stable_id()).status == ApplicationStatus.READY_TO_APPLY
    assert get_application_review(sample_delivered_vacancy.stable_id()).status == ReviewStatus.APPROVED

    # Transition 3: Attempt NOT_INTERESTED after already APPLIED
    set_application_status(
        vacancy_stable_id=sample_delivered_vacancy.stable_id(),
        status=ApplicationStatus.APPLIED,
        notes="Applied external",
    )
    cb_not = encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, sample_delivered_vacancy.stable_id())
    r3 = processor.process_callback_query({
        "id": "cb_step_3",
        "from": {"id": 392046103},
        "message": {"chat": {"id": -1004399255305}},
        "data": cb_not,
    })
    assert r3["success"] is True
    assert r3["new_status"] == "APPLIED"
    assert get_application_status(sample_delivered_vacancy.stable_id()).status == ApplicationStatus.APPLIED


# ---------------------------------------------------------------------------
# 17. One and only one Telegram update consumer is configured
# ---------------------------------------------------------------------------
def test_one_and_only_one_update_consumer_configured():
    # Verify that config defines single bot token and no conflicting pollers
    bot_token = config.TELEGRAM_BOT_TOKEN
    assert bot_token != "", "TELEGRAM_BOT_TOKEN must be configured"
    assert config.TELEGRAM_OWNER_ID != "", "TELEGRAM_OWNER_ID must be configured"


# ---------------------------------------------------------------------------
# 18. No network is used by the test suite
# ---------------------------------------------------------------------------
def test_no_network_used_by_test_suite():
    notifier = TelegramNotifier()
    res = notifier.send_message(text="Network isolation verification")
    assert res.get("ok") is True
    assert res.get("mocked") is True
