import json

import pytest

from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    save_application_review,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.db import init_db
from ai_assistant.hh_live_page_checks import check_live_page
from ai_assistant.hh_submission import (
    GateName,
    HHSubmissionGates,
    clear_submitted_reviews,
)


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    clear_submitted_reviews()
    db_file = str(tmp_path / "test_live_checks.db")
    import ai_assistant.config as cfg
    monkeypatch.setattr(cfg, "DB_FILE", db_file)
    init_db()


def _make_eval(payload: dict):
    return lambda js: json.dumps(payload)


def _make_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["Python Developer"],
        alternative_roles=[],
        skills=["Python"],
        preferred_seniority=[],
        remote_required=True,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=["Russian"],
        employment_types=["Full-time"],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


def test_live_page_valid_ready():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "Senior Python Developer",
        "has_submit_btn": True,
        "submit_btn_disabled": False,
        "has_apply_btn": True,
    })
    res = check_live_page(eval_fn, "hh:12345678", "Senior Python Developer")
    assert res.ok is True
    assert res.is_ok is True
    assert res.page_title == "Senior Python Developer"
    assert res.numeric_id_match is True
    assert res.already_applied is False
    assert res.error_reason is None
    assert res.status == "READY"
    assert res.url_matched is True
    assert res.title_matched is True
    assert res.has_submit_btn is True


def test_live_page_host_fail_closed():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://evil-hh.ru/vacancy/12345678",
        "title": "Senior Python Developer",
        "has_submit_btn": True,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.status == "FAIL_CLOSED"
    assert "evil-hh.ru" in res.reason


def test_live_page_numeric_id_mismatch():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/88888888",
        "title": "Senior Python Developer",
        "has_submit_btn": True,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.status == "MISMATCH"
    assert res.numeric_id == "88888888"
    assert "Vacancy ID mismatch" in res.reason


def test_live_page_404_blocked():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "404 Страница не найдена",
        "is_404": True,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.is_ok is False
    assert res.error_reason == "VACANCY_NOT_FOUND"
    assert res.status == "BLOCKED"
    assert res.is_404 is True
    assert "404" in res.reason


def test_live_page_captcha_blocked():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "Проверка безопасности",
        "is_captcha": True,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.is_ok is False
    assert res.error_reason == "CAPTCHA"
    assert res.status == "BLOCKED"
    assert res.is_captcha is True
    assert "CAPTCHA" in res.reason


def test_live_page_access_denied_blocked():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "Access Denied",
        "is_access_denied": True,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.is_ok is False
    assert res.error_reason == "ACCESS_DENIED"
    assert res.status == "BLOCKED"
    assert res.is_access_denied is True


def test_live_page_login_required_blocked():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "Вход в личный кабинет",
        "is_login_required": True,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.is_ok is False
    assert res.error_reason == "AUTH_REQUIRED"
    assert res.status == "BLOCKED"
    assert res.is_login_required is True


def test_live_page_already_responded_blocked():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "Python Dev",
        "already_responded": True,
        "has_submit_btn": False,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.is_ok is False
    assert res.already_applied is True
    assert res.error_reason == "ALREADY_APPLIED"
    assert res.status == "ALREADY_RESPONDED"
    assert res.already_responded is True


def test_live_page_no_button_blocked():
    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "Python Dev",
        "has_submit_btn": False,
        "has_apply_btn": False,
        "has_response_modal": False,
    })
    res = check_live_page(eval_fn, "hh:12345678")
    assert res.ok is False
    assert res.status == "BLOCKED"
    assert "Neither submit button nor apply button" in res.reason


def test_live_page_integration_with_gates(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = "hh:12345678"
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid, status=ReviewStatus.APPROVED, form_fingerprint="fp_ok"
    ))
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)

    eval_fn = _make_eval({
        "ok": True,
        "url": "https://hh.ru/vacancy/12345678",
        "title": "Senior Python Developer",
        "has_submit_btn": True,
        "already_responded": True,
    })
    live_res = check_live_page(eval_fn, sid)
    assert live_res.already_responded is True

    # Gate 9 catches already_responded from live_res
    gate_res = HHSubmissionGates.check_all_gates(
        vacancy_stable_id=sid,
        current_url=live_res.current_url,
        form_snapshot={
            "fingerprint": "fp_ok",
            "cover_letter": "A valid cover letter of good length",
            "already_responded": live_res.already_responded,
        },
        human_confirmed=True,
        candidate_profile=_make_profile(),
    )
    assert not gate_res.passed
    assert gate_res.failed_gate == GateName.GATE_NOT_ALREADY_APPLIED
