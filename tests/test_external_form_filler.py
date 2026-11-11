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


@patch("ai_assistant.external_form_filler._NO_PROXY_OPENER.open")
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


@patch("ai_assistant.external_form_filler._NO_PROXY_OPENER.open")
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


def test_multistep_universal_form_wizard():
    """Test multi-step HTML wizard passing through Step 1 (Next) -> Step 2 (Submit) -> Confirmation."""
    from ai_assistant.external_form_filler import _fill_universal_html_form

    calls = []

    def mock_evaluate(ws_url, method, params):
        expr = params.get("expression", "")
        calls.append(expr)

        # 1. Confirmation check
        if "response has been recorded" in expr:
            # On final verification after submit, return ok
            if any("sub.click()" in c for c in calls):
                return {"result": {"result": {"value": '{"ok": true, "snippet": "Спасибо! Ваша анкета успешно принята"}'}}}
            return {"result": {"result": {"value": '{"ok": false, "snippet": ""}'}}}

        # 2. Block check
        if "hasCaptcha" in expr:
            return {"result": {"result": {"value": '{"hasCaptcha": false, "hasRequiredFile": false, "isClosed": false, "hasMediaRecording": false}'}}}

        # 3. Extract inputs
        if "const allInputs" in expr:
            # Step 1: name field
            if not any("nxt.click()" in c for c in calls):
                return {"result": {"result": {"value": '{"fields": [{"index": 0, "id": "f_name", "name": "name", "tag": "input", "type": "text", "label": "Ваше имя", "options": []}]}'}}}
            # Step 2: salary field
            return {"result": {"result": {"value": '{"fields": [{"index": 0, "id": "f_salary", "name": "salary", "tag": "input", "type": "text", "label": "Ожидания по зарплате (руб)", "options": []}]}'}}}

        # 4. Button check
        if "hasStepNext" in expr:
            if not any("nxt.click()" in c for c in calls):
                return {"result": {"result": {"value": '{"hasSubmit": false, "hasStepNext": true}'}}}
            return {"result": {"result": {"value": '{"hasSubmit": true, "hasStepNext": false}'}}}

        # 5. Clicks
        if "nxt.click()" in expr or "sub.click()" in expr:
            return {"result": {"result": {"value": True}}}

        return {"result": {"result": {"value": "{}"}}}

    with patch("ai_assistant.external_form_filler._ws_call", side_effect=mock_evaluate):
        res = _fill_universal_html_form(
            ws_url="ws://127.0.0.1:9111/page",
            company="MultiStep Tech",
            vacancy="Senior Python Engineer",
            max_steps=5,
        )

        assert res.ok
        assert not res.blocked
        assert res.fields_count >= 2
        assert "Спасибо" in res.submission_proof
        assert "Михаил Колесников" in res.chat_confirmation_text


def test_anti_stuck_guard_on_repeated_validation_error():
    """Test that anti-stuck loop guard triggers when identical fields repeat consecutively after clicking Next."""
    from ai_assistant.external_form_filler import _fill_universal_html_form

    def mock_evaluate(ws_url, method, params):
        expr = params.get("expression", "")

        if "/response has been recorded" in expr:
            return {"result": {"result": {"value": '{"ok": false, "snippet": ""}'}}}
        if "hasCaptcha" in expr:
            return {"result": {"result": {"value": '{"hasCaptcha": false, "hasRequiredFile": false, "isClosed": false, "hasMediaRecording": false}'}}}
        if "const allInputs" in expr:
            # Always return identical fields fingerprint
            return {"result": {"result": {"value": '{"fields": [{"index": 0, "id": "req_field", "name": "req_name", "tag": "input", "type": "text", "label": "Обязательное поле", "options": []}]}'}}}
        if "hasStepNext" in expr:
            return {"result": {"result": {"value": '{"hasSubmit": false, "hasStepNext": true}'}}}
        if "click_nxt" in expr:
            return {"result": {"result": {"value": True}}}

        return {"result": {"result": {"value": "{}"}}}

    with patch("ai_assistant.external_form_filler._ws_call", side_effect=mock_evaluate):
        res = _fill_universal_html_form(
            ws_url="ws://127.0.0.1:9111/page",
            company="Strict Corp",
            vacancy="Backend Engineer",
            max_steps=5,
        )

        assert not res.ok
        assert res.blocked
        assert "Anti-stuck" in res.blocked_reason


def test_smart_fallback_for_non_standard_open_questions():
    """Test Level 1 semantic KB matching and candidate fact grounding for various open-ended questions."""
    # Troubleshooting
    ans_trouble = generate_answer_for_question("Как вы будете искать ошибку в чужом сервисе если он упал?")
    assert "journalctl" in ans_trouble or "docker logs" in ans_trouble

    # AI-video / Content-factory
    ans_video = generate_answer_for_question("Расскажите про ваш опыт генерации AI-видео и пайплайнов")
    assert "Контент-Завод" in ans_video or "FFmpeg" in ans_video or "Stable Diffusion" in ans_video

    # Motivation / About yourself
    ans_motive = generate_answer_for_question("Почему вы хотите работать именно у нас и в чем ваша мотивация?")
    assert "15 лет" in ans_motive or "автоматизации" in ans_motive

    # Remote schedule
    ans_sched = generate_answer_for_question("Какой у вас график и формат работы?")
    assert "100% Remote" in ans_sched
    assert "МСК" in ans_sched
    assert "разница в 4 часа мне абсолютно комфортна" in ans_sched
    assert "Самара" not in ans_sched

    # Legal contract
    ans_legal = generate_answer_for_question("Какая форма оформления вам подходит (ТК РФ, ИП)?")
    assert "ТК РФ" in ans_legal or "ИП" in ans_legal or "Самозанятость" in ans_legal

    # Database / SQL
    ans_db = generate_answer_for_question("Какой у вас опыт работы с базами данных SQL?")
    assert "PostgreSQL" in ans_db

    # Smart Choice Matcher on options
    opt_exp = ["Менее года", "1-2 года", "3-5 лет", "Более 5 лет"]
    chosen_exp = generate_answer_for_question("Опыт коммерческой разработки", "radio", opt_exp)
    assert chosen_exp == "Более 5 лет"

    opt_format = ["Работа в офисе", "Гибридный формат", "Полная удаленка"]
    chosen_format = generate_answer_for_question("Желаемый формат занятости", "radio", opt_format)
    assert chosen_format == "Полная удаленка"


def test_timezone_location_and_schedule_standardization():
    """Verify standardized responses for timezone, location, schedule, and interview window."""
    # 1. Timezone question
    ans_tz = generate_answer_for_question("В каком часовом поясе вы находитесь?")
    assert ans_tz == "UTC+7 (МСК+4, часовой пояс Новосибирска / Красноярска)"

    # 2. Location (Russian questionnaire)
    ans_loc_ru = generate_answer_for_question("Где вы сейчас находитесь и ваш город проживания?")
    assert ans_loc_ru == "Удаленно (РФ, часовой пояс МСК+4 / Новосибирск, гражданство РФ)"
    assert "Самара" not in ans_loc_ru

    # 3. Location (English questionnaire)
    ans_loc_en = generate_answer_for_question("Your current city and country / location:")
    assert ans_loc_en == "Remote (UTC+7, Thailand / Ready to work in your timezone)"

    # 4. Schedule question
    ans_sched_only = generate_answer_for_question("Ваш рабочий график?")
    assert "Полностью готов работать по московскому времени (09:00–18:00 или 10:00–19:00 МСК)" in ans_sched_only

    # 5. Interview window question
    ans_slots = generate_answer_for_question("Укажите удобное время для созвона / интервью:")
    assert "09:00 до 17:00 МСК (13:00–21:00 по моему местному времени UTC+7)" in ans_slots

    # 6. Dropdown priority: Удаленно -> UTC+7 / МСК+4 (Новосибирск / Красноярск) -> Москва
    # Case A: Has Remote
    opts_with_remote = ["Москва", "Красноярск (UTC+7)", "Удаленно"]
    assert generate_answer_for_question("Локация", "select", opts_with_remote) == "Удаленно"

    # Case B: No Remote, has UTC+7
    opts_no_remote = ["Москва", "Красноярск (UTC+7)", "Санкт-Петербург"]
    assert generate_answer_for_question("Город", "select", opts_no_remote) == "Красноярск (UTC+7)"

    # Case C: No Remote or UTC+7, has Moscow
    opts_moscow_only = ["Екатеринбург", "Москва", "Казань"]
    assert generate_answer_for_question("Город", "select", opts_moscow_only) == "Москва"


def test_llm_synthesis_fallback(monkeypatch):
    """Test Level 2 LLM synthesis fallback when question is outside standard keywords."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from ai_assistant.external_form_filler import _synthesize_open_answer_with_llm

    mock_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="У меня 15+ лет опыта в IT, готов быстро включиться."))]
    )

    with patch("ai_assistant.external_form_filler.antigravity_complete", return_value=mock_resp):
        ans = _synthesize_open_answer_with_llm(
            question_text="Как вы оцениваете важность корпоративной культуры?",
            company="Dream Team",
            vacancy="Tech Lead",
        )
        assert ans is not None
        assert "15+ лет" in ans
