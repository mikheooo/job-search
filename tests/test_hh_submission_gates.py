from __future__ import annotations

import pytest

from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    save_application_review,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.db import init_db, save_submission
from ai_assistant.hh_submission import (
    GateName,
    HHSubmissionGates,
    clear_submitted_reviews,
)


@pytest.fixture(autouse=True)
def reset_gates_state():
    clear_submitted_reviews()
    init_db()


def _make_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["Python Developer"],
        alternative_roles=[],
        skills=["Python", "FastAPI"],
        preferred_seniority=[],
        remote_required=True,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=["Russian", "English"],
        employment_types=["Full-time"],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


def _setup_valid_vacancy(
    vid: str = "123456",
    fp: str = "fp_valid_123",
    status: ReviewStatus = ReviewStatus.APPROVED,
    track_status: ApplicationStatus = ApplicationStatus.READY_TO_APPLY,
):
    sid = f"hh:{vid}"
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=status,
            form_fingerprint=fp,
            review_id=f"rev_{vid}",
        )
    )
    set_application_status(sid, track_status)
    return sid


def test_gate_submit_allowed(monkeypatch):
    sid = _setup_valid_vacancy("111")
    snapshot = {"fingerprint": "fp_valid_123", "cover_letter": "Valid cover letter text here"}
    url = "https://hh.ru/vacancy/111"

    # 1. Blocked when SUBMIT_ALLOWED is false and dry_run is false
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=False, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_SUBMIT_ALLOWED

    # 2. Allowed when SUBMIT_ALLOWED is true
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=False, candidate_profile=_make_profile()
    )
    assert res.passed

    # 3. Allowed in dry_run mode even if SUBMIT_ALLOWED is false
    monkeypatch.setenv("SUBMIT_ALLOWED", "false")
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=True, candidate_profile=_make_profile()
    )
    assert res.passed


def test_gate_review_approved(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    url = "https://hh.ru/vacancy/222"
    snapshot = {"fingerprint": "fp_222", "cover_letter": "A good cover letter for testing"}

    # 1. No review in DB
    res = HHSubmissionGates.check_all_gates(
        "hh:222", url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_REVIEW_APPROVED

    # 2. Review exists but status != APPROVED
    _setup_valid_vacancy("222", fp="fp_222", status=ReviewStatus.PENDING_REVIEW)
    res = HHSubmissionGates.check_all_gates(
        "hh:222", url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_REVIEW_APPROVED


def test_gate_fingerprint_match(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("333", fp="expected_hash")
    url = "https://hh.ru/vacancy/333"

    # Mismatch
    res = HHSubmissionGates.check_all_gates(
        sid, url, {"fingerprint": "different_hash", "cover_letter": "A good cover letter for testing"},
        human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_FINGERPRINT_MATCH

    # Match
    res = HHSubmissionGates.check_all_gates(
        sid, url, {"fingerprint": "expected_hash", "cover_letter": "A good cover letter for testing"},
        human_confirmed=True, candidate_profile=_make_profile()
    )
    assert res.passed


def test_gate_url_domain(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("444", fp="fp_444")
    snapshot = {"fingerprint": "fp_444", "cover_letter": "A good cover letter for testing"}

    # Non-hh domain
    bad_urls = [
        "https://evil-hh.ru/vacancy/444",
        "https://hh.ru.attacker.com/vacancy/444",
        "https://google.com/?hh.ru",
        "not_a_url",
    ]
    for bad_url in bad_urls:
        res = HHSubmissionGates.check_all_gates(
            sid, bad_url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
        )
        assert not res.passed
        assert res.failed_gate == GateName.GATE_URL_DOMAIN

    # Valid domains: hh.ru and subdomains
    good_urls = [
        "https://hh.ru/vacancy/444",
        "https://spb.hh.ru/vacancy/444",
        "https://kakdela.hh.ru/applicant/vacancy_response?vacancyId=444",
    ]
    for good_url in good_urls:
        res = HHSubmissionGates.check_all_gates(
            sid, good_url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
        )
        assert res.passed


def test_gate_vacancy_match(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("555", fp="fp_555")
    snapshot = {"fingerprint": "fp_555", "cover_letter": "A good cover letter for testing"}

    # ID mismatch
    res = HHSubmissionGates.check_all_gates(
        sid, "https://hh.ru/vacancy/999", snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_VACANCY_MATCH

    # Non-numeric / missing source_job_id
    sid_invalid = "hh:invalid_non_numeric"
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid_invalid,
            status=ReviewStatus.APPROVED,
            form_fingerprint="fp_555",
            review_id="rev_invalid",
        )
    )
    res = HHSubmissionGates.check_all_gates(
        sid_invalid, "https://hh.ru/vacancy/555", snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_VACANCY_MATCH

    # Regression: hh:remote_clean_1 must strictly fail Gate 5
    sid_slug = "hh:remote_clean_1"
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid_slug,
            status=ReviewStatus.APPROVED,
            form_fingerprint="fp_slug",
            review_id="rev_slug",
        )
    )
    res_slug = HHSubmissionGates.check_all_gates(
        sid_slug,
        "https://hh.ru/vacancy/remote_clean_1",
        {"fingerprint": "fp_slug", "cover_letter": "A good cover letter for testing"},
        human_confirmed=True,
        candidate_profile=_make_profile(),
    )
    assert not res_slug.passed
    assert res_slug.failed_gate == GateName.GATE_VACANCY_MATCH
    assert "not numeric" in res_slug.reason


def test_gate_profile_loaded(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("666", fp="fp_666")
    snapshot = {"fingerprint": "fp_666", "cover_letter": "A good cover letter for testing"}
    url = "https://hh.ru/vacancy/666"

    # Mock load_candidate_profile returning None
    monkeypatch.setattr("ai_assistant.candidate_profile.load_candidate_profile", lambda *a, **kw: None)
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, candidate_profile=None
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_PROFILE_LOADED


def test_gate_cover_letter_ready(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("777", fp="fp_777")
    url = "https://hh.ru/vacancy/777"

    # Empty / too short (< 10 chars)
    for bad_cl in ["", "short", "123456789"]:
        res = HHSubmissionGates.check_all_gates(
            sid, url, {"fingerprint": "fp_777", "cover_letter": bad_cl},
            human_confirmed=True, candidate_profile=_make_profile()
        )
        assert not res.passed
        assert res.failed_gate == GateName.GATE_COVER_LETTER_READY


def test_gate_no_unknown_questions(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("888", fp="fp_888")
    url = "https://hh.ru/vacancy/888"

    # Question requires manual answer
    snapshot = {
        "fingerprint": "fp_888",
        "cover_letter": "A good cover letter for testing",
        "fields": [{"type": "textarea", "label": "Сколько лет вы управляли космическим кораблём?", "required": True}],
    }
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_NO_UNKNOWN_QUESTIONS


def test_gate_not_already_applied_whitelist(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("999", fp="fp_999")
    url = "https://hh.ru/vacancy/999"
    snapshot = {"fingerprint": "fp_999", "cover_letter": "A good cover letter for testing"}

    # 1. Non-whitelisted tracking status blocks (e.g. APPLIED, SUBMITTED, INTERVIEW, REJECTED, WITHDRAWN)
    for blocked_status in [
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.REJECTED,
        ApplicationStatus.WITHDRAWN,
        ApplicationStatus.APPLIED,
    ]:
        set_application_status(sid, blocked_status)
        # Ensure review remains APPROVED so we isolate and verify GATE_NOT_ALREADY_APPLIED
        save_application_review(
            ApplicationReview(
                vacancy_stable_id=sid,
                status=ReviewStatus.APPROVED,
                form_fingerprint="fp_999",
                review_id="rev_999",
            )
        )
        res = HHSubmissionGates.check_all_gates(
            sid, url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
        )
        assert not res.passed, f"Status {blocked_status} should be blocked"
        assert res.failed_gate == GateName.GATE_NOT_ALREADY_APPLIED

    # 2. Prior completed/ambiguous submission record in DB blocks
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=ReviewStatus.APPROVED,
            form_fingerprint="fp_999",
            review_id="rev_999",
        )
    )
    save_submission(sid, "{}", "AMBIGUOUS_POST_SUBMIT")

    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_NOT_ALREADY_APPLIED

    # 3. Whitelisted non-submitted statuses in DB permit retry (FAILED, GATE_BLOCKED, CANCELLED, DRY_RUN)
    for idx, retry_status in enumerate(["FAILED", "GATE_BLOCKED", "CANCELLED", "DRY_RUN", "BLOCKED", "FAIL_CLOSED"], 1):
        num_id = f"1020{idx}"
        retry_sid = _setup_valid_vacancy(num_id, fp=f"fp_{retry_status}")
        retry_url = f"https://hh.ru/vacancy/{num_id}"
        retry_snapshot = {"fingerprint": f"fp_{retry_status}", "cover_letter": "A good cover letter for testing"}
        save_submission(retry_sid, "{}", retry_status)
        r_res = HHSubmissionGates.check_all_gates(
            retry_sid, retry_url, retry_snapshot, human_confirmed=True, candidate_profile=_make_profile()
        )
        assert r_res.passed, f"Submission with status {retry_status} must permit retry"


def test_gate_no_previous_submission_attempt(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("1001", fp="fp_1001")
    url = "https://hh.ru/vacancy/1001"
    snapshot = {"fingerprint": "fp_1001", "cover_letter": "A good cover letter for testing"}

    # In-progress SUBMITTING status in DB
    save_submission(sid, "{}", "SUBMITTING")

    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT


def test_gate_human_confirmed(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = _setup_valid_vacancy("1002", fp="fp_1002")
    url = "https://hh.ru/vacancy/1002"
    snapshot = {"fingerprint": "fp_1002", "cover_letter": "A good cover letter for testing"}

    # Fails when human_confirmed is False
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=False, dry_run=False, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_HUMAN_CONFIRMED

    # Passes when human_confirmed is True
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, dry_run=False, candidate_profile=_make_profile()
    )
    assert res.passed
