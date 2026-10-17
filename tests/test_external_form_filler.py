# -*- coding: utf-8 -*-
"""Unit tests for external_form_filler.py and its integration with hh_autonomous_agent."""

from unittest.mock import MagicMock, patch
import pytest

from ai_assistant.external_form_filler import (
    FormFillResult,
    _format_chat_confirmation,
    generate_answer_for_question,
    is_external_form_url,
    try_auto_fill_external_form,
)
from ai_assistant.hh_autonomous_agent import (
    AutonomousConfig,
    AutonomousJobAgent,
    NotificationDispatcher,
)
from ai_assistant.hh_message_reply import HHDialog, HHMessage


def test_is_external_form_url():
    # Valid form URLs
    assert is_external_form_url("https://docs.google.com/forms/d/e/1FAIpQLSc_123/viewform")
    assert is_external_form_url("https://forms.gle/xyz890")
    assert is_external_form_url("https://script.google.com/macros/s/AKfycbxyz/exec")
    assert is_external_form_url("https://tinyurl.com/devops-form")
    assert is_external_form_url("https://forms.yandex.ru/u/65b12345/")
    assert is_external_form_url("https://typeform.com/to/abc")

    # Negative / Non-form URLs
    assert not is_external_form_url("https://hh.ru/vacancy/12345678")
    assert not is_external_form_url("https://github.com/mikheooo")
    assert not is_external_form_url("https://t.me/brokeassh")
    assert not is_external_form_url("")
    assert not is_external_form_url(None)


def test_generate_answer_for_contacts():
    # Full name in Russian / English
    ans_name_ru = generate_answer_for_question("Ваше ФИО / имя?")
    assert "Михаил" in ans_name_ru

    ans_name_en = generate_answer_for_question("Your full name / candidate name:")
    assert "Mikhail" in ans_name_en

    # Email & Telegram & Phone
    ans_email = generate_answer_for_question("Укажите ваш email для связи:")
    assert ans_email == "mikhailthaiban@gmail.com"

    ans_tg = generate_answer_for_question("Ваш Telegram @username?")
    assert "@brokeassh" in ans_tg

    ans_phone = generate_answer_for_question("Номер телефона / WhatsApp:")
    assert "+7 (993) 339-76-28" in ans_phone


def test_generate_answer_for_technical_experience():
    # Salary questions
    ans_sal_rub = generate_answer_for_question("Ожидания по заработной плате (руб):")
    assert "200 000" in ans_sal_rub

    ans_sal_usd = generate_answer_for_question("Salary expectations in USD:")
    assert "1300" in ans_sal_usd

    # English options
    options = ["A1 - Beginner", "A2 - Elementary", "B1 - Intermediate", "C1 - Advanced"]
    ans_eng = generate_answer_for_question("English proficiency level", "radio", options)
    assert ans_eng == "B1 - Intermediate"

    # Readiness
    ready_opts = ["Нет", "Да, готов", "Частично"]
    ans_ready = generate_answer_for_question("Готовы ли вы приступить к работе в ближайшее время?", "radio", ready_opts)
    assert ans_ready == "Да, готов"


def test_format_chat_confirmation():
    conf = _format_chat_confirmation("Tech Corp", "Senior Python Developer", 12)
    assert "Анкета успешно заполнена и отправлена (12 полей отвечено)" in conf
    assert "@brokeassh" in conf
    assert "https://mikheooo.github.io/portfolio/" in conf
    assert "Михаил Колесников" in conf


def test_try_auto_fill_unsupported_url():
    res = try_auto_fill_external_form(
        cdp_url="http://127.0.0.1:9111",
        form_url="https://not-a-form.com/page",
    )
    assert not res.ok
    assert "Unsupported form URL" in res.error


@patch("urllib.request.urlopen")
def test_try_auto_fill_captcha_detected(mock_urlopen):
    # Mock opening tab
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"id": "tab123", "webSocketDebuggerUrl": "ws://127.0.0.1:9111/devtools/page/tab123"}'
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    with patch("ai_assistant.external_form_filler._ws_call") as mock_ws:
        # Mock CAPTCHA detection
        mock_ws.return_value = {
            "result": {
                "result": {
                    "value": '{"hasCaptcha": true, "hasRequiredFile": false}'
                }
            }
        }
        res = try_auto_fill_external_form(
            cdp_url="http://127.0.0.1:9111",
            form_url="https://docs.google.com/forms/d/e/test/viewform",
            company="Test Company",
            vacancy="Python Developer",
        )
        assert not res.ok
        assert res.blocked
        assert "CAPTCHA" in res.blocked_reason


@patch("urllib.request.urlopen")
def test_try_auto_fill_required_file_detected(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"id": "tab123", "webSocketDebuggerUrl": "ws://127.0.0.1:9111/devtools/page/tab123"}'
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    with patch("ai_assistant.external_form_filler._ws_call") as mock_ws:
        mock_ws.return_value = {
            "result": {
                "result": {
                    "value": '{"hasCaptcha": false, "hasRequiredFile": true}'
                }
            }
        }
        res = try_auto_fill_external_form(
            cdp_url="http://127.0.0.1:9111",
            form_url="https://docs.google.com/forms/d/e/test/viewform",
            company="Test Company",
            vacancy="Python Developer",
        )
        assert not res.ok
        assert res.blocked
        assert "загрузка файла" in res.blocked_reason


def test_agent_autonomous_external_form_submission_success(monkeypatch, tmp_path):
    """Test that hh_autonomous_agent automatically submits form, updates pending tasks with ✅, and queues confirmation."""
    import ai_assistant.test_task_manager as ttm
    test_artifact = tmp_path / "pending_test_tasks.md"
    monkeypatch.setattr(ttm, "ARTIFACT_PATH", test_artifact)

    form_url = "https://docs.google.com/forms/d/e/1FAIpQLSc_test/viewform"
    dialog = HHDialog(
        conversation_id="conv_form_1",
        vacancy_title="Backend Developer",
        vacancy_stable_id="hh:888777",
        employer="Innovative Solutions",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="10:00"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text=f"Здравствуйте! Пожалуйста, заполните небольшую анкету: {form_url}",
                sent_at="10:05",
            ),
        ],
    )

    mock_res = FormFillResult(
        ok=True,
        fields_count=10,
        submission_proof="Ответ записан",
        chat_confirmation_text=_format_chat_confirmation("Innovative Solutions", "Backend Developer", 10),
    )

    with patch("ai_assistant.external_form_filler.try_auto_fill_external_form", return_value=mock_res):
        agent = AutonomousJobAgent(
            config=AutonomousConfig(evaluate_fn=lambda script: "[]")
        )

        res = agent._process_messages(convs=[dialog])

        # Verify task artifact has been created with ✅ Сдано status
        assert test_artifact.exists()
        art_content = test_artifact.read_text(encoding="utf-8")
        assert "Innovative Solutions" in art_content
        assert "✅" in art_content
        assert "Сдано" in art_content

        # Verify agent logged event with EXTERNAL_QUESTIONNAIRE_SUBMITTED classification
        from ai_assistant import db
        # Check event saved to message events
        cursor = db.get_connection().cursor()
        cursor.execute("SELECT classification, reply_draft FROM hh_message_events WHERE conversation_id = 'conv_form_1'")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "EXTERNAL_QUESTIONNAIRE_SUBMITTED"
        assert "Анкета успешно заполнена и отправлена" in row[1]
