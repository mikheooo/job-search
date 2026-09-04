"""Remediation Step 2.4 Tests: Gate defects, check_readonly_gates, and fail-closed audit."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import ai_assistant.config as cfg
from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    save_application_review,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.db import init_db, save_hh_application, save_submission
from ai_assistant.hh_application_runner import (
    RunnerPreCheckStatus,
    run_application,
)
from ai_assistant.hh_submission import (
    GateName,
    HHSubmissionGates,
    clear_submitted_reviews,
)


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    clear_submitted_reviews()
    db_file = str(tmp_path / "test_step24.db")
    monkeypatch.setattr(cfg, "DB_FILE", db_file)
    init_db()


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


def test_readonly_gates_pass_without_cover_letter_or_confirmation(monkeypatch):
    """check_readonly_gates must pass for valid review/vacancy even if no cover letter or confirmation is present."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")  # Gate 1 is NOT part of readonly gates
    sid = "hh:12345678"
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid,
        status=ReviewStatus.APPROVED,
        form_fingerprint="fp_step24_ok",
        review_id="rev_12345678",
    ))
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)

    url = "https://hh.ru/vacancy/12345678"
    res = HHSubmissionGates.check_readonly_gates(
        vacancy_stable_id=sid,
        current_url=url,
        fingerprint="fp_step24_ok",
    )
    assert res.passed is True
    assert "All readonly gates passed" in res.reason


def test_readonly_gates_detect_stale_fingerprint():
    sid = "hh:12345678"
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid,
        status=ReviewStatus.APPROVED,
        form_fingerprint="fp_original",
        review_id="rev_12345678",
    ))
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)

    res = HHSubmissionGates.check_readonly_gates(
        vacancy_stable_id=sid,
        current_url="https://hh.ru/vacancy/12345678",
        fingerprint="fp_modified_tampered",
    )
    assert res.passed is False
    assert res.failed_gate == GateName.GATE_FINGERPRINT_MATCH


def test_readonly_gates_detect_already_applied():
    sid = "hh:12345678"
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid,
        status=ReviewStatus.APPROVED,
        form_fingerprint="fp_step24_ok",
        review_id="rev_12345678",
    ))
    # Record previous submission in DB
    save_submission(sid, json.dumps({"status": "SUBMITTED"}), "SUBMITTED")

    res = HHSubmissionGates.check_readonly_gates(
        vacancy_stable_id=sid,
        current_url="https://hh.ru/vacancy/12345678",
        fingerprint="fp_step24_ok",
    )
    assert res.passed is False
    assert res.failed_gate == GateName.GATE_NOT_ALREADY_APPLIED


def test_check_all_gates_enforces_gate7_and_gate11(monkeypatch):
    """check_all_gates strictly enforces cover letter (Gate 7) and human confirmation (Gate 11)."""
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = "hh:12345678"
    save_application_review(ApplicationReview(
        vacancy_stable_id=sid,
        status=ReviewStatus.APPROVED,
        form_fingerprint="fp_step24_ok",
        review_id="rev_12345678",
    ))
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    url = "https://hh.ru/vacancy/12345678"

    # 1. Missing cover letter fails Gate 7
    res_no_letter = HHSubmissionGates.check_all_gates(
        vacancy_stable_id=sid,
        current_url=url,
        form_snapshot={"fingerprint": "fp_step24_ok", "cover_letter": ""},
        human_confirmed=True,
        candidate_profile=_make_profile(),
    )
    assert res_no_letter.passed is False
    assert res_no_letter.failed_gate == GateName.GATE_COVER_LETTER_READY

    # 2. Missing human confirmation fails Gate 11 (when dry_run=False)
    res_no_confirm = HHSubmissionGates.check_all_gates(
        vacancy_stable_id=sid,
        current_url=url,
        form_snapshot={"fingerprint": "fp_step24_ok", "cover_letter": "A long and valid cover letter here"},
        human_confirmed=False,
        dry_run=False,
        candidate_profile=_make_profile(),
    )
    assert res_no_confirm.passed is False
    assert res_no_confirm.failed_gate == GateName.GATE_HUMAN_CONFIRMED


def test_runner_audit_exception_fails_closed(monkeypatch):
    """An exception in pre-submit questionnaire audit must result in FAIL, halting submission."""
    from ai_assistant import db
    sid = "hh:99999"
    app_id = "app_99999"
    save_hh_application({
        "application_id": app_id,
        "vacancy_stable_id": sid,
        "vacancy_id": "99999",
        "title": "Backend Engineer",
        "state": "READY_TO_SUBMIT",
        "questionnaire_id": "q_error_trigger",
    })
    db.save_hh_questionnaire({
        "questionnaire_id": "q_error_trigger",
        "vacancy_stable_id": sid,
        "questions": [],
    })

    with patch("ai_assistant.hh_application_runner.audit_questionnaire", side_effect=RuntimeError("Audit engine crash")):
        res = run_application(app_id, confirm_submit=True)
        assert res.pre_submit_audit == RunnerPreCheckStatus.FAIL
        assert res.questionnaire == RunnerPreCheckStatus.FAIL
        assert "Pre-submit questionnaire audit failed with exception" in (res.reason or "")
