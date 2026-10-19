"""Unit tests for HH screening questionnaire autofill, exam test separation, and fail-closed alerting."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.hh_autopilot import (
    is_vacancy_eligible_for_auto_apply,
    resolve_and_plan_screening,
    send_owner_alert,
    send_owner_questionnaire_alert,
    send_owner_test_alert,
)
from ai_assistant.hh_extractor import QuestionType

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def sample_profile():
    return CandidateProfile(
        name="Mikhail Kolesnikov",
        email="mikhailthaiban@gmail.com",
        phone_ru="+79933397628",
        desired_roles=["AI Engineer", "Python Developer"],
        skills=["python", "docker", "sql", "linux", "bitrix24", "n8n"],
        core_skills=["python", "automation", "n8n"],
        secondary_skills=["docker", "linux", "sql"],
        years_experience=15,
        remote_required=True,
        allowed_locations=["Remote", "Thailand"],
        languages=["ru", "en"],
        employment_types=["Full Time", "Contract"],
    )


@pytest.fixture
def sample_kb():
    return {
        "claims": {
            "lang_en": {"kind": "language_level", "value": "B1 (Intermediate)", "status": "CONFIRMED_BY_USER"},
            "lang_ru": {"kind": "language_level", "value": "Native", "status": "CONFIRMED_BY_USER"},
            "edu": {"kind": "education_level", "value": "Higher Education", "status": "CONFIRMED_BY_USER"},
            "cit": {"kind": "citizenship", "value": "РФ", "status": "CONFIRMED_BY_USER"},
            "loc": {"kind": "contact_location", "value": "Thailand", "status": "CONFIRMED_BY_USER"},
            "mode": {"kind": "work_mode", "value": "Remote", "status": "CONFIRMED_BY_USER"},
        }
    }


def test_t722_send_source_invariants():
    """Verify tools/.t722_send.py retains exactly one _click_at site and expected flags."""
    src = (REPO / "tools" / ".t722_send.py").read_text(encoding="utf-8")
    assert src.count("_click_at(") == 1, "Must maintain exactly one _click_at call site"
    assert "--prefill-json" in src, "Must support --prefill-json argument"
    assert "questions_prefilled" in src, "Must check questions_prefilled in _blockers"


def test_blockers_behavior_on_tests_and_questions():
    """Verify _blockers behavior with tests, questions, and prefilled state."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("t722_send", REPO / "tools" / ".t722_send.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    vacancy_base = {
        "ok": True,
        "archived": False,
        "applicant": {"responseAllowed": True, "alreadyApplied": False},
        "description": "Valid job description",
    }
    form_base = {
        "resume_title": "Специалист по внедрению и сопровождению CRM",
        "submit": {"disabled": False},
        "textareas": [{"visible": True, "value_len": 120}],
    }

    # 1. Normal clean vacancy -> no blockers
    blockers = mod._blockers(vacancy_base, form_base, expect_resume="Специалист по внедрению и сопровождению CRM")
    assert blockers == []

    # 2. Exam test present -> blocked
    vac_with_test = dict(vacancy_base)
    vac_with_test["applicant"] = {"responseAllowed": True, "test": {"hasTests": True}}
    blockers = mod._blockers(vac_with_test, form_base, expect_resume="Специалист по внедрению и сопровождению CRM")
    assert any("carries a test" in b for b in blockers)

    # 3. Questionnaire present but not prefilled -> blocked
    form_with_q = dict(form_base)
    form_with_q["started_with_question"] = True
    blockers = mod._blockers(vacancy_base, form_with_q, expect_resume="Специалист по внедрению и сопровождению CRM")
    assert any("employer questions are required" in b for b in blockers)

    # 4. Questionnaire present and prefilled -> passes!
    form_prefilled = dict(form_with_q)
    form_prefilled["questions_prefilled"] = True
    blockers = mod._blockers(vacancy_base, form_prefilled, expect_resume="Специалист по внедрению и сопровождению CRM")
    assert blockers == []

    # 5. Exam test + questions prefilled -> passes autonomously (agent solves test questions)
    blockers = mod._blockers(vac_with_test, form_prefilled, expect_resume="Специалист по внедрению и сопровождению CRM")
    assert blockers == []


def test_resolve_screening_questionnaire_success(sample_profile, sample_kb):
    """Test resolution of standard screening questions from confirmed KB and profile."""
    form_data = {
        "started_with_question": True,
        "controls": [
            {
                "tag": "INPUT",
                "type": "radio",
                "name": "task_q_english",
                "id": "q1_opt_b1",
                "label": "B1 (Средний)",
                "options": ["A1 (Начальный)", "A2 (Элементарный)", "B1 (Средний)", "B2 (Выше среднего)"],
            },
            {
                "tag": "INPUT",
                "type": "radio",
                "name": "task_q_cit",
                "id": "q2_opt_rf",
                "label": "Российская Федерация",
                "options": ["Российская Федерация", "Беларусь", "Казахстан"],
            },
            {
                "tag": "INPUT",
                "type": "radio",
                "name": "task_q_remote",
                "id": "q3_opt_rem",
                "label": "Удаленно",
                "options": ["В офисе", "Гибрид", "Удаленно"],
            },
            {
                "tag": "INPUT",
                "type": "radio",
                "name": "task_q_py",
                "id": "q4_opt_yes",
                "label": "Да, есть опыт",
                "options": ["Да, есть опыт", "Нет опыта"],
            },
        ],
        "question_groups": [
            {"name": "task_q_english", "stem": "Укажите ваш уровень знания английского языка"},
            {"name": "task_q_cit", "stem": "Ваше гражданство?"},
            {"name": "task_q_remote", "stem": "Предпочтительный формат работы?"},
            {"name": "task_q_py", "stem": "Есть ли у вас практический опыт работы с Python?"},
        ],
    }

    ok, verdict, plan, reasons = resolve_and_plan_screening(
        vid="138500100",
        form_data=form_data,
        profile=sample_profile,
        kb_data=sample_kb,
    )

    assert ok is True
    assert verdict == "PLAN_READY"
    assert plan is not None
    assert plan.status == "VALID"
    assert len(plan.operations) == 4
    assert len(reasons) == 0

    # Verify operations target the intended options
    op_values = [op.value for op in plan.operations]
    assert "B1 (Средний)" in op_values
    assert "Российская Федерация" in op_values
    assert "Удаленно" in op_values
    assert "Да, есть опыт" in op_values


def test_resolve_screening_questionnaire_fails_closed_on_unknown(sample_profile, sample_kb):
    """Test questionnaire fail-closed behavior when an unconfirmed skill or question is asked."""
    form_data = {
        "started_with_question": True,
        "controls": [
            {
                "tag": "INPUT",
                "type": "radio",
                "name": "task_q_haskell",
                "id": "q1_opt_yes",
                "label": "Да, более 5 лет",
                "options": ["Да, более 5 лет", "1-3 года", "Нет опыта"],
            },
        ],
        "question_groups": [
            {"name": "task_q_haskell", "stem": "Укажите опыт коммерческой разработки на Haskell"},
        ],
    }

    ok, verdict, plan, reasons = resolve_and_plan_screening(
        vid="138500200",
        form_data=form_data,
        profile=sample_profile,
        kb_data=sample_kb,
    )

    assert ok is False
    assert verdict == "REQUIRES_REVIEW"
    assert plan is None
    assert len(reasons) >= 1
    assert "Haskell" in reasons[0] or "task_q_haskell" in reasons[0]


def test_alerts_formatting():
    """Verify Telegram alert dispatch formatting for exam tests and review questions."""
    with patch("ai_assistant.hh_autopilot.send_owner_alert") as mock_alert:
        mock_alert.return_value = True

        # 1. Exam test alert
        res_test = send_owner_test_alert(
            vid="138500300",
            role="Senior Python Engineer",
            company="Tech Corp",
            test_info={"hasTests": True, "testId": 99},
        )
        assert res_test is True
        mock_alert.assert_called_once()
        sent_text = mock_alert.call_args[0][0]
        assert "138500300" in sent_text
        assert "Senior Python Engineer" in sent_text
        assert "Tech Corp" in sent_text
        assert "квалификационный тест" in sent_text

        mock_alert.reset_mock()

        # 2. Questionnaire review alert
        res_q = send_owner_questionnaire_alert(
            vid="138500400",
            role="DevOps Engineer",
            company="Cloud Systems",
            reasons=["Вопрос 'Опыт работы с Rust': нет подтвержденного факта в базе"],
        )
        assert res_q is True
        mock_alert.assert_called_once()
        sent_text = mock_alert.call_args[0][0]
        assert "138500400" in sent_text
        assert "DevOps Engineer" in sent_text
        assert "Cloud Systems" in sent_text
        assert "Rust" in sent_text


def test_is_vacancy_eligible_gate_with_resolved_screening():
    """Verify is_vacancy_eligible_for_auto_apply respects has_resolved_screening flag."""
    brief = {
        "vacancy": {
            "ok": True,
            "archived": False,
            "closedForApplicants": False,
            "applicant": {"responseAllowed": True, "alreadyApplied": False, "test": {"hasTests": False}},
        },
        "form": {
            "started_with_question": True,
            "resume_title": "Специалист по внедрению и сопровождению CRM",
            "submit": {"disabled": False},
        },
    }

    # Without resolved screening -> ineligible (requires initial employer questionnaire)
    ok_before, reason_before = is_vacancy_eligible_for_auto_apply(brief, has_resolved_screening=False)
    assert ok_before is False
    assert "questionnaire" in reason_before

    # With resolved screening -> eligible!
    ok_after, reason_after = is_vacancy_eligible_for_auto_apply(brief, has_resolved_screening=True)
    assert ok_after is True
    assert reason_after == "ELIGIBLE"
