"""Unit test suite for Telegram routing fix and autonomous pipeline unlock."""

import re
import pytest

from ai_assistant import config
from ai_assistant.schema import Vacancy, is_genuine_production_vacancy, is_active_production_source
from ai_assistant.hh_autopilot import check_salary_floor
from ai_assistant.hh_message_reply import (
    HHDialog,
    HHMessage,
    MessageClassification,
    _auto_enabled,
    _AUTO_FORBIDDEN_RE,
    _AUTO_ALLOWED_PROBE,
    is_safe_for_auto_reply,
)
from ai_assistant.hh_autonomous_agent import _autonomous_reply_enabled


class TestCheckSalaryFloor:
    def test_salary_floor_with_unicode_narrow_nbsp(self):
        # HeadHunter uses \u202f as a thousands separator
        sal_str = "до 400\u202f000 руб."
        ok, reason = check_salary_floor(sal_str)
        assert ok is True
        assert "400000" in reason

    def test_salary_floor_with_dict_salary_to_only(self):
        sal_dict = {"from": None, "to": 400000, "currency": "RUR"}
        ok, reason = check_salary_floor(sal_dict)
        assert ok is True
        assert "400000" in reason

    def test_salary_floor_with_dict_zero_from(self):
        sal_dict = {"from": 0, "to": 250000, "currencyCode": "RUB"}
        ok, reason = check_salary_floor(sal_dict)
        assert ok is True
        assert "250000" in reason

    def test_salary_floor_below_minimum(self):
        sal_str = "от 40 000 до 70 000 руб."
        ok, reason = check_salary_floor(sal_str, min_rub=100000)
        assert ok is False
        assert "below floor" in reason

    def test_salary_floor_foreign_currency(self):
        ok, reason = check_salary_floor("1 500 $")
        assert ok is True
        assert "foreign currency" in reason

    def test_salary_floor_unspecified(self):
        ok, reason = check_salary_floor(None)
        assert ok is True
        assert "acceptable" in reason


class TestProductionSources:
    def test_geekjob_is_active_source(self):
        assert is_active_production_source("geekjob") is True

    def test_hirify_is_active_source(self):
        assert is_active_production_source("hirify") is True

    def test_telegram_channel_prefix_is_active_source(self):
        assert is_active_production_source("tg_remoteit") is True
        assert is_active_production_source("tg_zakazy_it") is True

    def test_genuine_production_vacancy_validation(self):
        v_geek = Vacancy(
            source="geekjob",
            source_job_id="gj_12345",
            title="Python Developer",
            company="Tech Corp",
            description="Remote Python Developer",
            job_url="https://geekjob.ru/vacancy/12345",
            location="Remote",
        )
        is_gen, reason = is_genuine_production_vacancy(v_geek)
        assert is_gen is True
        assert reason in ("GENUINE_PRODUCTION", "TEST_ENVIRONMENT_MOCK")

        v_tg = Vacancy(
            source="tg_remoteit",
            source_job_id="tg_9999",
            title="DevOps Engineer",
            company="Cloud Team",
            description="Remote DevOps",
            job_url="https://t.me/remoteit/9999",
            location="Remote",
        )
        is_gen_tg, reason_tg = is_genuine_production_vacancy(v_tg)
        assert is_gen_tg is True


class TestTelegramRoutingConfig:
    def test_channel_id_comes_from_env_only(self):
        import os
        expected = (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
        assert config.TELEGRAM_CHANNEL_ID == expected
        # The historical public-channel id is not a default anymore.
        if not expected:
            assert config.TELEGRAM_CHANNEL_ID == ""

    def test_fetcher_targets_channel(self):
        import os
        import integrations.hermes.job_search_fetcher as jsf
        assert jsf.target_channel() == (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()

    def test_discovery_targets_channel(self):
        import os
        import scripts.run_multi_source_discovery as msd
        assert msd.configured_channel_id() == (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()


class TestAutoReplyFlagsAndGates:
    def test_auto_enabled_with_both_var_names(self):
        assert _auto_enabled({"HH_AUTO_REPLY_ENABLED": "true"}) is True
        assert _auto_enabled({"HH_AUTONOMOUS_REPLY_ENABLED": "true"}) is True
        assert _auto_enabled({"HH_AUTO_REPLY_ENABLED": "false"}) is False
        assert _auto_enabled({}) is False

    def test_autonomous_reply_enabled_with_both_var_names(self):
        # HH_AUTO_REPLY_ENABLED arms only the guarded reply pipeline.
        # The ungated send stays off unless HH_AUTONOMOUS_REPLY_ENABLED is true.
        assert _autonomous_reply_enabled({"HH_AUTO_REPLY_ENABLED": "true"}) is False
        assert _autonomous_reply_enabled({"HH_AUTONOMOUS_REPLY_ENABLED": "true"}) is True
        assert _autonomous_reply_enabled({"HH_AUTONOMOUS_REPLY_ENABLED": "false"}) is False
        assert _autonomous_reply_enabled({}) is False
        assert _autonomous_reply_enabled({
            "HH_AUTO_REPLY_ENABLED": "true",
            "HH_AUTONOMOUS_REPLY_ENABLED": "false",
        }) is False

    def test_auto_forbidden_re_strictly_matches_critical_topics(self):
        forbidden_samples = [
            "Вот вам тестовое задание, сделайте за 3 дня",
            "Заполните, пожалуйста, анкету по ссылке",
            "Пройдите опросник: https://forms.gle/xyz",
            "Пожалуйста, заполните форму docs.google.com/forms",
            "Давайте согласуем время для звонка в Zoom",
            "Укажите удобное время для звонка",
            "Мы готовы выставить оффер, согласны ли принять оффер?",
            "Необходимо подписать договор на оказание услуг",
        ]
        for s in forbidden_samples:
            assert _AUTO_FORBIDDEN_RE.search(s) is not None, f"Expected forbidden match for: {s}"

    def test_auto_forbidden_re_allows_standard_clarifications(self):
        allowed_samples = [
            "Уточните, пожалуйста, ваш стек технологий?",
            "Какой у вас опыт работы с Python и Docker?",
            "Рассматриваете ли вы удаленный формат работы?",
            "Какие у вас зарплатные ожидания?",
            "Подскажите, актуален ли для вас поиск работы?",
            "Готовы ли вы обсудить задачи по автоматизации?",
        ]
        for s in allowed_samples:
            assert _AUTO_FORBIDDEN_RE.search(s) is None, f"Expected no forbidden match for: {s}"
            assert _AUTO_ALLOWED_PROBE.search(s) is not None, f"Expected allowed probe match for: {s}"

    def test_is_safe_for_auto_reply_evaluation(self):
        profile_with_salary = {
            "desired_roles": ["AI Automation Engineer", "Python Developer"],
            "minimum_salary": 1000,
            "salary_currency": "USD",
            "remote_required": True,
        }
        dialog_stack = HHDialog(
            conversation_id="conv_1",
            vacancy_title="Python Developer",
            employer="Acme",
            messages=[
                HHMessage(message_id="m1", text="Какой у вас опыт с Python и n8n?", sender="employer"),
            ],
        )
        safe_stack = is_safe_for_auto_reply(dialog_stack, MessageClassification.REPLY_REQUIRED, profile_with_salary)
        assert safe_stack["safe"] is True

        dialog_forbidden = HHDialog(
            conversation_id="conv_2",
            vacancy_title="Python Developer",
            employer="Acme",
            messages=[
                HHMessage(message_id="m1", text="Заполните тестовое задание по ссылке", sender="employer"),
            ],
        )
        safe_forb = is_safe_for_auto_reply(dialog_forbidden, MessageClassification.REPLY_REQUIRED, profile_with_salary)
        assert safe_forb["safe"] is False
        assert any("sensitive/forbidden" in r for r in safe_forb["reasons"])
