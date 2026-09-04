"""Remediation Step 2.5 Tests: Unified Submission Routing and Dry-Run."""
from __future__ import annotations

import json

import pytest

import ai_assistant.config as cfg
from ai_assistant import db
from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    save_application_review,
)
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
)
from ai_assistant.browser_executor import (
    MockBrowserAdapter,
    submit_application_in_browser,
)
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.hh_application_runner import (
    run_application,
)
from ai_assistant.hh_submission import (
    clear_submitted_reviews,
    execute_hh_submission,
)


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    clear_submitted_reviews()
    db_file = str(tmp_path / "test_step25.db")
    monkeypatch.setattr(cfg, "DB_FILE", db_file)
    db.init_db()


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


from ai_assistant.schema import Vacancy


def _setup_vacancy(vid: str = "777888", fp: str = "fp_test_25"):
    sid = f"hh:{vid}"
    vac = Vacancy(
        source="hh",
        source_job_id=vid,
        title="Senior Python Backend Developer",
        company="Tech Corp",
        description="Python, SQL, FastAPI, Docker",
        job_url=f"https://hh.ru/vacancy/{vid}",
        location="Remote",
    )
    db.save_vacancy(vac)
    db.save_application_package(
        sid,
        "v1",
        json.dumps({
            "vacancy_stable_id": sid,
            "cover_letter": "A comprehensive and tailored cover letter for this role.",
            "title": "Senior Python Backend Developer",
            "validation_status": "VALID",
        }),
    )
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid,
        status=ReviewStatus.APPROVED,
        form_fingerprint=fp,
        review_id=f"rev_{vid}",
    ))
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    return sid


def test_execute_hh_submission_dry_run(monkeypatch):
    """execute_hh_submission with dry_run=True passes all gates and performs ZERO mutations."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")  # dry_run does not require SUBMIT_ALLOWED
    sid = _setup_vacancy("111222", "fp_dry")
    url = "https://hh.ru/vacancy/111222"

    eval_fn = lambda js: json.dumps({
        "ok": True,
        "url": url,
        "title": "Senior Python Backend Developer",
        "has_submit_btn": True,
        "submit_btn_disabled": False,
        "has_apply_btn": True,
    })

    res = execute_hh_submission(
        vacancy_stable_id=sid,
        evaluate_fn=eval_fn,
        human_confirmed=True,
        dry_run=True,
        candidate_profile=_make_profile(),
    )
    assert res.ok is True
    assert res.status == "DRY_RUN_OK"
    assert res.submit_count == 0
    assert "dry-run mode" in res.reason


def test_execute_hh_submission_blocked_without_submit_allowed(monkeypatch):
    """execute_hh_submission with dry_run=False and SUBMIT_ALLOWED=false must be BLOCKED."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")
    sid = _setup_vacancy("333444", "fp_block")
    url = "https://hh.ru/vacancy/333444"

    eval_fn = lambda js: json.dumps({
        "ok": True,
        "url": url,
        "title": "Senior Python Backend Developer",
        "has_submit_btn": True,
        "submit_btn_disabled": False,
        "has_apply_btn": True,
    })

    res = execute_hh_submission(
        vacancy_stable_id=sid,
        evaluate_fn=eval_fn,
        human_confirmed=True,
        dry_run=False,
        candidate_profile=_make_profile(),
    )
    assert res.ok is False
    assert res.status == "BLOCKED"
    assert "SUBMIT_ALLOWED" in res.reason
    assert res.submit_count == 0


def test_execute_hh_submission_success_updates_all_dbs(monkeypatch):
    """execute_hh_submission when fully confirmed and allowed submits and updates state across all databases."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_vacancy("555666", "fp_submit")
    url = "https://hh.ru/vacancy/555666"

    # Setup hh_applications record
    db.save_hh_application({
        "application_id": "app_555666",
        "vacancy_stable_id": sid,
        "vacancy_id": "555666",
        "title": "Senior Python Backend Developer",
        "state": "READY_TO_SUBMIT",
    })

    submitted = False
    def eval_fn(js: str) -> str:
        nonlocal submitted
        if "submitBtn.click()" in js:
            submitted = True
            return json.dumps({"ok": True})
        if submitted:
            return json.dumps({
                "ok": True,
                "text": "отклик отправлен",
                "url": "https://hh.ru/applicant/negotiations",
                "has_banner": True,
            })
        return json.dumps({
            "ok": True,
            "url": url,
            "title": "Senior Python Backend Developer",
            "has_submit_btn": True,
            "submit_btn_disabled": False,
            "has_apply_btn": True,
            "already_responded": False,
        })

    res = execute_hh_submission(
        vacancy_stable_id=sid,
        evaluate_fn=eval_fn,
        human_confirmed=True,
        dry_run=False,
        candidate_profile=_make_profile(),
    )
    assert res.ok is True
    assert res.status == "SUBMITTED"
    assert res.submit_count == 1

    # Check database 1: application_submissions
    sub = db.get_submission(sid)
    assert sub is not None
    assert sub[4] == "SUBMITTED"

    # Check database 2: application_tracking
    track = get_application_status(sid)
    assert track.status == ApplicationStatus.SUBMITTED

    # Check database 3: hh_applications
    hh_app = db.get_hh_application("app_555666")
    assert hh_app["state"] == "SUBMITTED"


def test_path_a_browser_executor_dry_run():
    """Path A: submit_application_in_browser with dry_run=True returns DRY_RUN_OK with submit_count=0."""
    sid = _setup_vacancy("777111", "fp_path_a")

    class FakeAdapter(MockBrowserAdapter):
        def evaluate(self, js: str) -> str:
            return json.dumps({
                "ok": True,
                "url": "https://hh.ru/vacancy/777111",
                "title": "Senior Python Backend Developer",
                "has_submit_btn": True,
                "submit_btn_disabled": False,
                "has_apply_btn": True,
            })

    res = submit_application_in_browser(
        vacancy_stable_id=sid,
        confirm_submit=True,
        dry_run=True,
        adapter=FakeAdapter(),
    )
    assert res.status == "DRY_RUN_OK"
    assert res.submit_count == 0


def test_path_b_runner_dry_run():
    """Path B: run_application with dry_run=True executes pre-checks and returns without submitting."""
    sid = _setup_vacancy("888222", "fp_path_b")
    app_id = "app_888222"
    db.save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": sid,
        "vacancy_id": "888222",
        "title": "Senior Python Backend Developer",
        "state": "READY_TO_SUBMIT",
    })

    def eval_fn(js: str) -> str:
        return json.dumps({
            "ok": True,
            "url": "https://hh.ru/vacancy/888222",
            "title": "Senior Python Backend Developer",
            "has_submit_btn": True,
            "submit_btn_disabled": False,
            "has_apply_btn": True,
            "already_responded": False,
        })

    res = run_application(
        application_id=app_id,
        confirm_submit=True,
        evaluate_fn=eval_fn,
        dry_run=True,
    )
    assert res.real_hh_submit == 0
    assert "dry-run mode" in (res.reason or "")
