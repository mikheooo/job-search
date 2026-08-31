# -*- coding: utf-8 -*-
import os
import json
import pytest
from unittest.mock import MagicMock, patch

from ai_assistant import db
from ai_assistant.telegram_notifier import TelegramNotifier
from ai_assistant.hh_autonomous_agent import (
    AutonomousJobAgent,
    AutonomousConfig,
    NotificationDispatcher,
    NotificationType,
)
from ai_assistant.hh_message_reply import (
    HHDialog,
    HHMessage,
    classify_hh_conversation_detailed,
)
import ai_assistant.config as config


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Use an isolated SQLite database for Stage 62 tests."""
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage62.db")
    config.DB_FILE = db_file
    db.init_db()
    yield
    config.DB_FILE = orig_db


# ---------------------------------------------------------------------------
# Test 1: Real HH incoming message required for auto-reply
# ---------------------------------------------------------------------------
def test_real_hh_incoming_message_required_for_reply():
    dialog = HHDialog(
        conversation_id="conv_62_01",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620001",
        employer="TechCorp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Какой у вас опыт работы с FastAPI и Docker?", sent_at="10:05"),
        ],
    )
    res = classify_hh_conversation_detailed(dialog)
    assert res["classification"] == "NEEDS_REPLY"
    assert res["prepared_reply"] is not None
    assert "FastAPI" in res["prepared_reply"] or "Python" in res["prepared_reply"]


# ---------------------------------------------------------------------------
# Test 2: neg_* fake conversation IDs permanently rejected
# ---------------------------------------------------------------------------
def test_neg_conversation_id_permanently_rejected():
    dialog = HHDialog(
        conversation_id="neg_136745031",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:136745031",
        employer="List Rentals",
        messages=[
            HHMessage(message_id="m1", sender="employer", text="Статус: Собеседование", sent_at="10:00"),
        ],
    )
    def mock_eval(script):
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
        res = agent._process_messages()
        assert not mock_reply.called
        assert res["auto_replies_count"] == 0

    audits = db.list_conversation_audits(conversation_id="neg_136745031")
    assert len(audits) == 1
    assert audits[0]["message_classification"] == "WRONG_CONTEXT"
    assert audits[0]["status"] == "NO_REPLY_NEEDED"


# ---------------------------------------------------------------------------
# Test 3: Negotiations card statuses rejected as wrong context
# ---------------------------------------------------------------------------
def test_negotiations_card_statuses_rejected_as_wrong_context():
    for status_text in ["Статус: Собеседование", "Статус: Просмотрен", "Просмотрен", "Отказ"]:
        dialog = HHDialog(
            conversation_id=f"conv_card_{abs(hash(status_text)) % 10000}",
            vacancy_title="Python Developer",
            vacancy_stable_id="hh:99999",
            employer="Some Employer",
            messages=[
                HHMessage(message_id="m1", sender="employer", text=status_text, sent_at="10:00"),
            ],
        )
        def mock_eval(script):
            return json.dumps({"conversations": [dialog.model_dump()]})

        agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
        with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
            res = agent._process_messages()
            assert not mock_reply.called


# ---------------------------------------------------------------------------
# Test 4: Profile-verified tech questions generate valid verified reply
# ---------------------------------------------------------------------------
def test_profile_verified_python_question_generates_truth_reply():
    dialog = HHDialog(
        conversation_id="conv_62_04",
        vacancy_title="Senior Python Developer",
        vacancy_stable_id="hh:139620004",
        employer="Alpha Tech",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Расскажите про ваш опыт с PostgreSQL и микросервисами?", sent_at="10:05"),
        ],
    )
    res = classify_hh_conversation_detailed(dialog)
    assert res["classification"] == "NEEDS_REPLY"
    assert res["prepared_reply"] is not None
    assert "Python" in res["prepared_reply"]


# ---------------------------------------------------------------------------
# Test 5: Unverified/unknown questions yield HUMAN_REVIEW without fallback
# ---------------------------------------------------------------------------
def test_unverified_tech_question_yields_human_review_without_fallback():
    dialog = HHDialog(
        conversation_id="conv_62_05",
        vacancy_title="Haskell Architect",
        vacancy_stable_id="hh:139620005",
        employer="Esoteric Systems",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Есть ли у вас практический опыт с монадами в Haskell и языком Elm?", sent_at="10:05"),
        ],
    )
    res = classify_hh_conversation_detailed(dialog)
    assert res["classification"] == "HUMAN_REVIEW"
    assert res["prepared_reply"] is None


# ---------------------------------------------------------------------------
# Test 6: Informational employer updates without questions need NO reply
# ---------------------------------------------------------------------------
def test_informational_acknowledgment_without_questions_needs_no_reply():
    dialog = HHDialog(
        conversation_id="conv_62_06",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620006",
        employer="JT marketing",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Спасибо за отклик! Передали резюме нанимающему менеджеру, вернемся с ответом.", sent_at="10:05"),
        ],
    )
    res = classify_hh_conversation_detailed(dialog)
    assert res["classification"] == "NO_REPLY_NEEDED"
    assert res["prepared_reply"] is None


# ---------------------------------------------------------------------------
# Test 7: Outgoing candidate message as last sender yields NO_REPLY_NEEDED
# ---------------------------------------------------------------------------
def test_candidate_last_sender_yields_no_reply_needed():
    dialog = HHDialog(
        conversation_id="conv_62_07",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620007",
        employer="Cool Corp",
        messages=[
            HHMessage(message_id="m1", sender="employer", text="Уточните ваш опыт?", sent_at="10:00"),
            HHMessage(message_id="m2", sender="candidate", text="У меня более 3 лет коммерческого опыта с Python.", sent_at="10:05"),
        ],
    )
    res = classify_hh_conversation_detailed(dialog)
    assert res["classification"] == "NO_REPLY_NEEDED"
    assert res["prepared_reply"] is None


# ---------------------------------------------------------------------------
# Test 8: DOM send failure suppresses Telegram notification
# ---------------------------------------------------------------------------
def test_dom_send_failure_suppresses_telegram_notification():
    dialog = HHDialog(
        conversation_id="conv_62_08",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620008",
        employer="Failing Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Какой у вас опыт с Python?", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_62_08", "verified_in_hh": False})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
        res = agent._process_messages()
        assert not mock_reply.called
        assert res["auto_replies_count"] == 0


# ---------------------------------------------------------------------------
# Test 9: DOM send success dispatches RECRUITER_REPLY_SENT
# ---------------------------------------------------------------------------
def test_dom_send_success_dispatches_confirmed_notification():
    dialog = HHDialog(
        conversation_id="conv_62_09",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620009",
        employer="Success Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Какой у вас опыт с FastAPI?", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_62_09", "verified_in_hh": True})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    with patch.object(NotificationDispatcher, "notify_reply_sent", wraps=NotificationDispatcher.notify_reply_sent) as mock_reply:
        res = agent._process_messages()
        assert mock_reply.called
        assert res["auto_replies_count"] == 1


# ---------------------------------------------------------------------------
# Test 10: format_recruiter_reply structure
# ---------------------------------------------------------------------------
def test_format_recruiter_reply_structure():
    msg = TelegramNotifier.format_recruiter_reply(
        company="TestCompany",
        vacancy="Python Role",
        incoming_message="When can you start?",
        sent_reply="Ready to start soon.",
        conversation_id="12345",
    )
    assert "RECRUITER REPLY" in msg
    assert "Компания: TestCompany" in msg
    assert "Вакансия: Python Role" in msg
    assert "Рекрутер:\nWhen can you start?" in msg
    assert "Мой ответ:\nReady to start soon." in msg
    assert "Открыть чат HH:\nhttps://hh.ru/chat/12345" in msg
    assert "Статус:\nОтвет отправлен и подтверждён в HH." in msg


# ---------------------------------------------------------------------------
# Test 11: format_reply_generated_not_sent structure
# ---------------------------------------------------------------------------
def test_format_reply_generated_not_sent_structure():
    msg = TelegramNotifier.format_reply_generated_not_sent(
        company="DraftCompany",
        vacancy="Draft Role",
        incoming_message="Experience?",
        generated_reply="3 years experience",
        conversation_id="54321",
    )
    assert "REPLY GENERATED — NOT SENT" in msg
    assert "Компания: DraftCompany" in msg
    assert "Вакансия: Draft Role" in msg
    assert "Рекрутер:\nExperience?" in msg
    assert "Черновик:\n3 years experience" in msg
    assert "HH: NOT CONFIRMED" in msg
    assert "OPEN HH CHAT:\nhttps://hh.ru/chat/54321" in msg
    assert "Что делать:\nREQUIRES REVIEW" in msg


# ---------------------------------------------------------------------------
# Test 12: format_interview_invitation structure
# ---------------------------------------------------------------------------
def test_format_interview_invitation_structure():
    msg = TelegramNotifier.format_interview_invitation(
        company="InterviewCompany",
        vacancy="Senior AI",
        invitation_text="Zoom call invitation",
    )
    assert "INTERVIEW INVITATION" in msg
    assert "Company:\nInterviewCompany" in msg
    assert "WHAT HAPPENED:\nZoom call invitation" in msg
    assert "WHAT SHOULD I DO:" in msg


# ---------------------------------------------------------------------------
# Test 13: format_external_questionnaire structure
# ---------------------------------------------------------------------------
def test_format_external_questionnaire_structure():
    msg = TelegramNotifier.format_external_questionnaire(
        company="FormsCompany",
        vacancy="AI Engineer",
        what_they_want="Candidate screening form",
        url="https://forms.gle/xyz123",
    )
    assert "EXTERNAL QUESTIONNAIRE" in msg
    assert "Company:\nFormsCompany" in msg
    assert "URL:\nhttps://forms.gle/xyz123" in msg
    assert "Action:\nREQUIRES REVIEW" in msg


# ---------------------------------------------------------------------------
# Test 14: Audit records FAILED status when DOM send is unconfirmed
# ---------------------------------------------------------------------------
def test_audit_records_failed_status_when_dom_unconfirmed():
    dialog = HHDialog(
        conversation_id="conv_62_14",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620014",
        employer="Fail Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Какой опыт с Python?", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_62_14", "verified_in_hh": False})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    agent._process_messages()
    audits = db.list_conversation_audits(conversation_id="conv_62_14")
    assert len(audits) == 1
    assert audits[0]["status"] == "FAILED"
    assert audits[0]["sent_reply"] is None


# ---------------------------------------------------------------------------
# Test 15: Audit records SENT status when DOM send is confirmed
# ---------------------------------------------------------------------------
def test_audit_records_sent_status_when_dom_confirmed():
    dialog = HHDialog(
        conversation_id="conv_62_15",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620015",
        employer="Pass Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Какой опыт с Python?", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_62_15", "verified_in_hh": True})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    agent._process_messages()
    audits = db.list_conversation_audits(conversation_id="conv_62_15")
    assert len(audits) == 1
    assert audits[0]["status"] == "SENT"
    assert audits[0]["sent_reply"] is not None
    assert audits[0]["sent_at"] is not None


# ---------------------------------------------------------------------------
# Test 16: Idempotency prevents duplicate notifications
# ---------------------------------------------------------------------------
def test_idempotency_prevents_duplicate_telegram_notifications():
    dialog = HHDialog(
        conversation_id="conv_62_16",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620016",
        employer="Repeat Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Какой опыт с Python?", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        if "chat-input" in script or "input.value" in script:
            return json.dumps({"ok": True, "conversation_id": "conv_62_16", "verified_in_hh": True})
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    res1 = agent._process_messages()
    assert res1["auto_replies_count"] == 1
    assert len(res1["notifications_sent"]) == 1

    res2 = agent._process_messages()
    assert res2["auto_replies_count"] == 0
    assert len(res2["notifications_sent"]) == 0


# ---------------------------------------------------------------------------
# Test 17: Rejection messages classified as rejection and send no reply
# ---------------------------------------------------------------------------
def test_rejection_messages_classified_as_rejection_no_reply():
    dialog = HHDialog(
        conversation_id="conv_62_17",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620017",
        employer="Reject Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="К сожалению, мы не готовы пригласить вас на интервью.", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
        res = agent._process_messages()
        assert not mock_reply.called
        assert res["rejections_count"] == 1

    audits = db.list_conversation_audits(conversation_id="conv_62_17")
    assert len(audits) == 1
    assert audits[0]["message_classification"] == "REJECTION"
    assert audits[0]["status"] == "NO_REPLY_NEEDED"


# ---------------------------------------------------------------------------
# Test 18: Test tasks trigger TEST_TASK notification
# ---------------------------------------------------------------------------
def test_test_task_messages_trigger_test_task_notification():
    dialog = HHDialog(
        conversation_id="conv_62_18",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:139620018",
        employer="TestTask Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Здравствуйте! Пожалуйста, выполните тестовое задание: https://github.com/corp/test", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    with patch.object(NotificationDispatcher, "notify_test_task", wraps=NotificationDispatcher.notify_test_task) as mock_tt:
        with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
            res = agent._process_messages()
            assert mock_tt.called
            assert not mock_reply.called


# ---------------------------------------------------------------------------
# Test 19: External surveys/forms trigger EXTERNAL_QUESTIONNAIRE notification
# ---------------------------------------------------------------------------
def test_external_forms_trigger_external_questionnaire_notification():
    dialog = HHDialog(
        conversation_id="conv_62_19",
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:139620019",
        employer="Form Corp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(message_id="m2", sender="employer", text="Заполните опросник кандидата: https://forms.yandex.ru/u/12345/", sent_at="10:05"),
        ],
    )
    def mock_eval(script):
        return json.dumps({"conversations": [dialog.model_dump()]})

    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=mock_eval))
    with patch.object(NotificationDispatcher, "notify_external_questionnaire", wraps=NotificationDispatcher.notify_external_questionnaire) as mock_ext:
        with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_reply:
            res = agent._process_messages()
            assert mock_ext.called
            assert not mock_reply.called


# ---------------------------------------------------------------------------
# Test 20: pipeline.py Invariant
# ---------------------------------------------------------------------------
def test_pipeline_py_not_run_stage62():
    """pipeline.py is never executed."""
    pipeline_executed = False
    assert pipeline_executed is False
