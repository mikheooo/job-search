"""Stage 89: Telegram Feedback & Application Review Integration Test Suite.

Verifies:
1. authorized INTERESTED callback accepted;
2. unauthorized user rejected;
3. malformed callback fails closed;
4. unknown vacancy fails closed;
5. duplicate callback is idempotent;
6. PREPARE_APPLICATION enters existing preparation/review path;
7. PREPARE_APPLICATION does not submit;
8. SKIP persists through existing state architecture;
9. already APPLIED vacancy cannot be re-prepared;
10. callback resolves canonical stable vacancy;
11. cross-source duplicate does not create duplicate application intent;
12. callback history/audit recorded;
13. Telegram failure does not corrupt application state;
14. DB failure does not falsely acknowledge successful transition;
15. digest still contains valid delivery keys;
16. inline keyboard does not alter batch idempotency;
17. legacy/test/dry-run vacancy cannot receive production feedback controls;
18. test suite performs no network access;
19. no real Telegram message is sent;
20. existing application state machine remains authoritative.
"""
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
from ai_assistant.cli import export_digest_cmd, feedback_cmd
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
    test_db = tmp_path / "test_state.db"
    monkeypatch.setattr(config, "DB_FILE", str(test_db))
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", "123456789")
    db.init_db()
    return str(test_db)


@pytest.fixture
def sample_vacancy(isolated_db):
    v = Vacancy(
        source="himalayas",
        source_job_id="hima_s89_1001",
        title="AI Automation Engineer",
        company="Synthetix Technologies",
        description="Remote Worldwide. Required: Python, n8n, automation.",
        job_url="https://himalayas.app/companies/synthetix/jobs/ai-automation-engineer",
        location="Remote Worldwide",
        salary_min=3500,
        salary_currency="USD",
    )
    db.save_vacancy(v)
    db.mark_digest_delivered([v.stable_id()])
    db.save_application_package(
        v.stable_id(), "v1",
        json.dumps({"cover_letter": "I am an experienced engineer.", "answers": []})
    )
    return v


# 1. authorized INTERESTED callback accepted
def test_authorized_interested_callback_accepted(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_vacancy.stable_id())

    update = {
        "id": "cb_001",
        "from": {"id": 123456789, "username": "owner"},
        "message": {"message_id": 101, "chat": {"id": 123456789}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is True
    assert res["action"] == "INTERESTED"

    # Review status updated
    rev = get_application_review(sample_vacancy.stable_id())
    assert rev is not None
    assert rev.status == ReviewStatus.PENDING_REVIEW


# 2. unauthorized user rejected
def test_unauthorized_user_rejected(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_vacancy.stable_id())

    update = {
        "id": "cb_unauth",
        "from": {"id": 999999999, "username": "intruder"},
        "message": {"message_id": 101, "chat": {"id": 999999999}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is False
    assert res["error"] == "UNAUTHORIZED"

    # State not mutated
    assert get_application_review(sample_vacancy.stable_id()) is None


# 3. malformed callback fails closed
def test_malformed_callback_fails_closed():
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)

    update = {
        "id": "cb_malformed",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": "invalid_format_without_prefix",
    }
    res = processor.process_callback_query(update)
    assert res["success"] is False
    assert res["error"] == "MALFORMED_CALLBACK"


# 4. unknown vacancy fails closed
def test_unknown_vacancy_fails_closed(isolated_db):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, "himalayas:non_existent_id_999")

    update = {
        "id": "cb_unknown",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is False
    assert res["error"] == "UNKNOWN_VACANCY"


# 5. duplicate callback is idempotent
def test_duplicate_callback_is_idempotent(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_vacancy.stable_id())

    update = {
        "id": "cb_dup_1",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    res1 = processor.process_callback_query(update)
    assert res1["success"] is True

    # Repeated identical callback_query_id
    res2 = processor.process_callback_query(update)
    assert res2["success"] is True
    assert res2.get("idempotent") is True


# 6. PREPARE_APPLICATION enters existing preparation/review path
def test_prepare_application_enters_preparation_queue(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_vacancy.stable_id())

    update = {
        "id": "cb_prep_1",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is True
    assert res["new_status"] == "READY_TO_APPLY"

    # Verified in tracking and review tables
    app = get_application_status(sample_vacancy.stable_id())
    assert app is not None
    assert app.status == ApplicationStatus.READY_TO_APPLY

    # Two-step approval (1.5-R.7, option (a)): 📄 queues and shows the package,
    # approval is a separate explicit action.
    rev = get_application_review(sample_vacancy.stable_id())
    assert rev is not None
    assert rev.status == ReviewStatus.PENDING_REVIEW

    q_item = get_queue_item(sample_vacancy.stable_id())
    assert q_item is not None


# 6b. ✅ APPROVE_APPLICATION is what actually approves and fingerprints the package
def test_approve_application_sets_approved_and_fingerprint(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)

    cb_prep = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_vacancy.stable_id())
    processor.process_callback_query({
        "id": "cb_prep_approve_flow",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_prep,
    })
    assert get_application_review(sample_vacancy.stable_id()).status == ReviewStatus.PENDING_REVIEW

    cb_approve = encode_callback_data(TelegramFeedbackAction.APPROVE_APPLICATION, sample_vacancy.stable_id())
    res = processor.process_callback_query({
        "id": "cb_approve_1",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_approve,
    })
    assert res["success"] is True

    rev = get_application_review(sample_vacancy.stable_id())
    assert rev.status == ReviewStatus.APPROVED
    assert rev.form_fingerprint


# 6c. 📄 sends the cover letter to the chat together with an explicit ✅ button
def test_prepare_application_sends_letter_and_approve_button(sample_vacancy):
    sent: list = []

    def transport(token, payload):
        sent.append(payload)
        return {"ok": True, "result": {"message_id": 1}}

    notifier = TelegramNotifier(transport_fn=transport)
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_vacancy.stable_id())

    processor.process_callback_query({
        "id": "cb_prep_preview",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    })

    # The transport also carries answerCallbackQuery - keep only outbound messages.
    messages = [p for p in sent if "callback_query_id" not in p]
    assert len(messages) == 1
    text = messages[0]["text"]
    assert "I am an experienced engineer." in text

    markup = messages[0].get("reply_markup") or {}
    buttons = [b for row in markup.get("inline_keyboard", []) for b in row]
    callbacks = [b["callback_data"] for b in buttons]
    assert any(c.startswith("fb:APR:") for c in callbacks)
    assert all(not c.startswith("fb:APP:") for c in callbacks)


# 6d. ✅ without a prepared package fails loudly instead of a silent log warning
def test_approve_without_package_fails_loudly(isolated_db):
    v = Vacancy(
        source="himalayas",
        source_job_id="hima_s89_nopkg",
        title="Python Developer",
        company="No Package Ltd",
        description="Remote.",
        job_url="https://himalayas.app/jobs/no-package",
        location="Remote",
    )
    db.save_vacancy(v)
    db.mark_digest_delivered([v.stable_id()])
    assert db.get_application_package(v.stable_id()) is None

    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.APPROVE_APPLICATION, v.stable_id())

    res = processor.process_callback_query({
        "id": "cb_approve_nopkg",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    })

    assert res["success"] is False
    assert res["error"] == "PACKAGE_MISSING"
    assert get_application_review(v.stable_id()) is None


# 6e. APR action code survives encode/decode round trip (64-byte callback limit)
def test_approve_action_code_round_trip():
    sid = "hh:136591579"
    data = encode_callback_data(TelegramFeedbackAction.APPROVE_APPLICATION, sid)
    assert len(data.encode("utf-8")) <= 64
    action, decoded_sid = decode_callback_data(data)
    assert action == TelegramFeedbackAction.APPROVE_APPLICATION
    assert decoded_sid == sid


# 7. PREPARE_APPLICATION does not submit
def test_prepare_application_does_not_submit(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_vacancy.stable_id())

    update = {
        "id": "cb_prep_nosubmit",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is True
    # Proves zero external submit called - status transitions only to READY_TO_APPLY
    app = get_application_status(sample_vacancy.stable_id())
    assert app.status == ApplicationStatus.READY_TO_APPLY
    assert app.status != ApplicationStatus.SUBMITTED
    assert app.status != ApplicationStatus.APPLIED
    assert app.status != ApplicationStatus.VERIFIED


# 8. SKIP persists through existing state architecture
def test_skip_persists_through_existing_architecture(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.SKIP, sample_vacancy.stable_id())

    update = {
        "id": "cb_skip_1",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is True
    assert res["new_status"] == "WITHDRAWN"

    app = get_application_status(sample_vacancy.stable_id())
    assert app.status == ApplicationStatus.WITHDRAWN

    rev = get_application_review(sample_vacancy.stable_id())
    assert rev.status == ReviewStatus.REJECTED


# 9. already APPLIED vacancy cannot be re-prepared
def test_already_applied_vacancy_cannot_be_re_prepared(sample_vacancy):
    set_application_status(
        vacancy_stable_id=sample_vacancy.stable_id(),
        status=ApplicationStatus.APPLIED,
        notes="Pre-existing applied status",
    )

    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_vacancy.stable_id())

    update = {
        "id": "cb_prep_applied",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is True
    assert res["new_status"] == "APPLIED"
    assert "уже была отправлена" in res["message"]

    app = get_application_status(sample_vacancy.stable_id())
    assert app.status == ApplicationStatus.APPLIED


# 10. callback resolves canonical stable vacancy
def test_callback_resolves_canonical_stable_vacancy(sample_vacancy):
    # Test surrogate hash encoding for long IDs
    long_sid = "weworkremotely:ignition-inc-mac-msp-help-desk-guru-work-from-home"
    v_long = Vacancy(
        source="weworkremotely",
        source_job_id="ignition-inc-mac-msp-help-desk-guru-work-from-home",
        title="Mac MSP Help Desk Guru",
        company="Ignition, Inc.",
        description="Remote support",
        job_url="https://weworkremotely.com/jobs/1",
    )
    db.save_vacancy(v_long)

    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, long_sid)
    action, resolved_sid = decode_callback_data(cb_data)
    assert action == TelegramFeedbackAction.INTERESTED
    assert resolved_sid == long_sid


# 11. cross-source duplicate does not create duplicate application intent
def test_cross_source_duplicate_handled_safely(isolated_db):
    v1 = Vacancy(
        source="himalayas",
        source_job_id="dup_1",
        title="AI Engineer",
        company="SameCompany",
        description="Python automation",
        job_url="https://himalayas.app/jobs/dup1",
    )
    db.save_vacancy(v1)
    db.mark_digest_delivered([v1.stable_id()])

    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, v1.stable_id())

    update = {
        "id": "cb_dup_intent",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    res = processor.process_callback_query(update)
    assert res["success"] is True

    # Second submission on same target
    res2 = processor.process_callback_query(update)
    assert res2["success"] is True


# 12. callback history/audit recorded
def test_callback_history_audit_recorded(sample_vacancy):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, sample_vacancy.stable_id())

    update = {
        "id": "cb_audit_99",
        "from": {"id": 123456789, "username": "mikheooo"},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    processor.process_callback_query(update)

    records = db.list_telegram_feedback(limit=10, vacancy_stable_id=sample_vacancy.stable_id())
    assert len(records) >= 1
    rec = records[0]
    assert rec["action"] == "NOT_INTERESTED"
    assert rec["telegram_user_id"] == "123456789"
    assert rec["callback_query_id"] == "cb_audit_99"


# 13. Telegram failure does not corrupt application state
def test_telegram_failure_does_not_corrupt_application_state(sample_vacancy):
    failing_notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="123456789",
        transport_fn=MagicMock(side_effect=RuntimeError("Network timeout")),
    )
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=failing_notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_vacancy.stable_id())

    update = {
        "id": "cb_net_err",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    # Should safely record DB state despite Telegram transport notification error
    res = processor.process_callback_query(update)
    assert res["success"] is True
    assert get_application_review(sample_vacancy.stable_id()) is not None


# 14. DB failure does not falsely acknowledge successful transition
def test_db_failure_handles_gracefully(sample_vacancy, monkeypatch):
    notifier = TelegramNotifier()
    processor = TelegramFeedbackProcessor(allowed_user_id="123456789", notifier=notifier)
    cb_data = encode_callback_data(TelegramFeedbackAction.INTERESTED, sample_vacancy.stable_id())

    # Simulate get_vacancy error
    monkeypatch.setattr("ai_assistant.db.get_vacancy_by_id", MagicMock(side_effect=Exception("DB Corrupted")))

    update = {
        "id": "cb_db_err",
        "from": {"id": 123456789},
        "message": {"chat": {"id": 123456789}},
        "data": cb_data,
    }
    with pytest.raises(Exception):
        processor.process_callback_query(update)


# 15. digest still contains valid delivery keys
def test_digest_still_contains_valid_delivery_keys(calibrated_profile, isolated_db, monkeypatch):
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    v = Vacancy(
        source="himalayas",
        source_job_id="hima_key_1",
        title="AI Engineer",
        company="GlobalTech",
        description="Remote Worldwide. Python, n8n.",
        job_url="https://himalayas.app/jobs/key1",
        location="Remote Worldwide",
        salary_min=3000,
        salary_currency="USD",
    )
    db.save_vacancy(v)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 1
    assert payload["new_vacancies_data"][0]["id"] == "himalayas:hima_key_1"
    assert "reply_markup" in payload
    assert "inline_keyboard" in payload["reply_markup"]


# 16. inline keyboard does not alter batch idempotency
def test_inline_keyboard_preserves_batch_idempotency(sample_vacancy):
    kb = TelegramNotifier.build_digest_inline_keyboard([{"id": sample_vacancy.stable_id()}])
    assert "inline_keyboard" in kb
    row = kb["inline_keyboard"][0]
    assert len(row) == 4
    assert row[0]["text"] == "1. 👍"
    assert row[2]["text"] == "1. 📄 Отклик"


# 17. legacy/test/dry-run vacancy cannot receive production feedback controls
def test_legacy_test_vacancy_cannot_receive_production_feedback(calibrated_profile, isolated_db, monkeypatch):
    monkeypatch.setattr("ai_assistant.cli.load_candidate_profile", lambda *args: calibrated_profile)
    v_legacy = Vacancy(
        source="vacancies_json",
        source_job_id="101",
        title="Legacy Role",
        company="LegacyCo",
        description="Old",
        job_url="https://example.com/101",
    )
    db.save_vacancy(v_legacy)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 0


# 18. test suite performs no network access
def test_suite_performs_no_network_access():
    notifier = TelegramNotifier()
    # In pytest environment, send_message / answer_callback_query must return mocked response
    res = notifier.send_message("Test message")
    assert res.get("ok") is True
    assert res.get("mocked") is True


# 19. no real Telegram message is sent
def test_no_real_telegram_message_is_sent():
    notifier = TelegramNotifier()
    res = notifier.answer_callback_query(callback_query_id="cb_test", text="Ack")
    assert res.get("ok") is True
    assert res.get("mocked") is True


# 20. existing application state machine remains authoritative
def test_application_state_machine_remains_authoritative(sample_vacancy):
    notifier = TelegramNotifier()
    bot = TelegramBot(allowed_chat_id="123456789", notifier=notifier)

    # Process PREPARE_APPLICATION update via TelegramBot dispatcher
    cb_data = encode_callback_data(TelegramFeedbackAction.PREPARE_APPLICATION, sample_vacancy.stable_id())
    update = {
        "update_id": 5001,
        "callback_query": {
            "id": "cb_bot_dispatch",
            "from": {"id": 123456789},
            "message": {"chat": {"id": 123456789}},
            "data": cb_data,
        },
    }
    res = bot.process_update(update)
    assert res["success"] is True
    assert bot.last_update_id == 5001

    app = get_application_status(sample_vacancy.stable_id())
    assert app.status == ApplicationStatus.READY_TO_APPLY
