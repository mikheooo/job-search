"""Tests for Smart Resume Mapping, IT PDF CV, and Safe Outreach Delivery."""

from __future__ import annotations

import pathlib
import pytest

from ai_assistant.hh_autopilot import select_best_resume_for_vacancy
from ai_assistant.email_outreach import get_default_cv_path, dispatch_email_outreach
from scripts.run_telegram_outreach import MAX_DAILY_TG_CARDS, MAX_DAILY_EMAILS


def test_select_best_resume_crm():
    roles = [
        "Интегратор Битрикс24",
        "Главный эксперт (Отдел внедрения и сопровождения информационных систем)",
        "Специалист по внедрению CRM",
        "Администратор Bitrix24",
        "Интегратор CRM / Битрикс24 (стажер + обучение)",
        "Ведущий специалист внедрения 1С и CRM",
    ]
    for r in roles:
        chosen = select_best_resume_for_vacancy(r)
        assert chosen == "Специалист по внедрению CRM (Битрикс24 / amoCRM) / Интегратор API", f"Failed for role: {r} (got {chosen})"


def test_select_best_resume_ai_python():
    roles = [
        "AI Engineer",
        "Python Developer",
        "Инженер AI / ML",
        "Разработчик Python (FastAPI / asyncio)",
        "Специалист по автоматизации процессов (n8n / Python)",
        "Prompt Engineer / LLM Integrator",
    ]
    for r in roles:
        chosen = select_best_resume_for_vacancy(r)
        assert chosen == "AI Engineer / Python Developer / Инженер автоматизации", f"Failed for role: {r} (got {chosen})"


def test_select_best_resume_support_sysadmin():
    roles = [
        "Системный администратор",
        "Инженер технической поддержки",
        "DevOps инженер",
        "Инженер сопровождения",
        "Сетевой инженер (Cisco / Mikrotik)",
        "L2 Support Engineer",
    ]
    for r in roles:
        chosen = select_best_resume_for_vacancy(r)
        assert chosen == "Инженер технической поддержки L2/L3 / Системный администратор", f"Failed for role: {r} (got {chosen})"


def test_get_default_cv_paths():
    russian_cv = get_default_cv_path(is_bangkok=False)
    assert russian_cv.exists()
    assert russian_cv.name == "Mikhail_Kolesnikov_CV_IT_Engineer.pdf"
    assert russian_cv.stat().st_size > 50000

    bangkok_cv = get_default_cv_path(is_bangkok=True)
    assert bangkok_cv.exists()
    assert bangkok_cv.name == "Mikhail_Kolesnikov_CV_Bangkok.pdf"
    assert bangkok_cv.stat().st_size > 50000


def test_outreach_rate_limits_constants():
    assert MAX_DAILY_TG_CARDS <= 3
    assert MAX_DAILY_EMAILS <= 5


def test_dispatch_email_outreach_safe_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    res = dispatch_email_outreach(
        to_email="test_hr@example.org",
        role="Python Developer",
        company="Acme Corp",
        description="Looking for Python and n8n",
        is_bangkok=False,
        attempt_smtp=True,
    )
    assert res["smtp_sent"] is False
    assert "SMTP_PASSWORD не настроен" in res["status_message"]
    assert pathlib.Path(res["eml_path"]).exists()
    assert pathlib.Path(res["cv_path"]).name == "Mikhail_Kolesnikov_CV_IT_Engineer.pdf"
