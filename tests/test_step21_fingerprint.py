import json

import pytest

from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    approve_review,
    compute_review_fingerprint,
    get_application_review,
    save_application_review,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.db import init_db, save_application_package
from ai_assistant.hh_submission import (
    GateName,
    HHSubmissionGates,
    clear_submitted_reviews,
)


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    clear_submitted_reviews()
    db_file = str(tmp_path / "test_fingerprint.db")
    import ai_assistant.config as cfg
    monkeypatch.setattr(cfg, "DB_FILE", db_file)
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


def test_compute_review_fingerprint_canonical():
    sid = "hh:12345"
    p1 = {
        "cover_letter": "Здравствуйте, я опытный разработчик.",
        "answers": [
            {"question_id": "q2", "answer": "Да"},
            {"question_id": "q1", "answer": "5 лет"},
        ],
    }
    p2 = {
        "cover_letter": "Здравствуйте, я опытный разработчик.",
        "answers": [
            {"question_id": "q1", "answer": "5 лет"},
            {"question_id": "q2", "answer": "Да"},
        ],
    }

    fp1 = compute_review_fingerprint(sid, p1)
    fp2 = compute_review_fingerprint(sid, p2)
    assert fp1 == fp2
    assert len(fp1) == 64

    # Change in cover letter produces different fingerprint
    p3 = dict(p1, cover_letter="Другое сопроводительное письмо.")
    assert compute_review_fingerprint(sid, p3) != fp1

    # Change in answer produces different fingerprint
    p4 = {
        "cover_letter": p1["cover_letter"],
        "answers": [
            {"question_id": "q1", "answer": "3 года"},
            {"question_id": "q2", "answer": "Да"},
        ],
    }
    assert compute_review_fingerprint(sid, p4) != fp1


def test_compute_review_fingerprint_formats():
    sid = "hh:12345"
    pkg_dict = {"cover_letter": "Letter A", "answers": []}
    fp_expected = compute_review_fingerprint(sid, pkg_dict)

    # JSON string
    assert compute_review_fingerprint(sid, json.dumps(pkg_dict)) == fp_expected

    # DB tuple: (sid, version, json, created_at)
    db_tuple = (sid, "v1", json.dumps(pkg_dict), "2026-09-04T00:00:00")
    assert compute_review_fingerprint(sid, db_tuple) == fp_expected


def test_application_review_fingerprint_property_and_migration():
    # 1. Normal creation with form_fingerprint
    rev = ApplicationReview(vacancy_stable_id="hh:1", form_fingerprint="fp123")
    assert rev.form_fingerprint == "fp123"

    # 2. Accessing deprecated fingerprint property emits warning
    with pytest.deprecated_call():
        val = rev.fingerprint
    assert val == "fp123"

    # 3. Setting deprecated fingerprint property emits warning and updates form_fingerprint
    with pytest.deprecated_call():
        rev.fingerprint = "fp456"
    assert rev.form_fingerprint == "fp456"

    # 4. Deserialization of legacy dict with 'fingerprint' migrates to form_fingerprint
    legacy_dict = {"vacancy_stable_id": "hh:2", "fingerprint": "legacy_fp"}
    migrated = ApplicationReview.model_validate(legacy_dict)
    assert migrated.form_fingerprint == "legacy_fp"


def test_approve_review_without_package_fails_closed():
    sid = "hh:11111"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    rev = ApplicationReview(vacancy_stable_id=sid, status=ReviewStatus.PENDING_REVIEW)
    save_application_review(rev)

    # Approve without package in DB must fail closed
    with pytest.raises(ValueError, match=f"Application package not found for {sid}: сначала подготовьте пакет"):
        approve_review(sid)


def test_approve_review_with_package_records_fingerprint():
    sid = "hh:22222"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    pkg_data = {"cover_letter": "Detailed cover letter text", "answers": [{"question_id": "q1", "answer": "yes"}]}
    save_application_package(sid, "v1", json.dumps(pkg_data))

    rev = ApplicationReview(vacancy_stable_id=sid, status=ReviewStatus.PENDING_REVIEW)
    save_application_review(rev)

    expected_fp = compute_review_fingerprint(sid, pkg_data)

    appr = approve_review(sid, note="Human OK", force=True)
    assert appr.status == ReviewStatus.APPROVED
    assert appr.form_fingerprint == expected_fp

    # Verify persisted in DB
    reloaded = get_application_review(sid)
    assert reloaded is not None
    assert reloaded.form_fingerprint == expected_fp


def test_reapprove_updates_missing_fingerprint():
    sid = "hh:33333"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    pkg_data = {"cover_letter": "Old letter", "answers": []}
    save_application_package(sid, "v1", json.dumps(pkg_data))

    # Existing review was APPROVED but has no form_fingerprint (pre-2.1 state)
    rev = ApplicationReview(vacancy_stable_id=sid, status=ReviewStatus.APPROVED, form_fingerprint=None)
    save_application_review(rev)

    expected_fp = compute_review_fingerprint(sid, pkg_data)

    reappr = approve_review(sid, force=True)
    assert reappr.status == ReviewStatus.APPROVED
    assert reappr.form_fingerprint == expected_fp


def test_gate3_detects_mutation_post_approval(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = "hh:44444"
    url = "https://hh.ru/vacancy/44444"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)

    original_pkg = {"cover_letter": "Approved cover letter for vacancy 44444", "answers": []}
    save_application_package(sid, "v1", json.dumps(original_pkg))

    rev = ApplicationReview(vacancy_stable_id=sid, status=ReviewStatus.PENDING_REVIEW)
    save_application_review(rev)
    approved_rev = approve_review(sid, force=True)
    assert approved_rev.form_fingerprint is not None

    # Case 1: form_snapshot with same cover_letter passes Gate 3
    res_pass = HHSubmissionGates.check_all_gates(
        sid, url,
        form_snapshot={"cover_letter": "Approved cover letter for vacancy 44444"},
        human_confirmed=True, candidate_profile=_make_profile(),
    )
    assert res_pass.passed

    # Case 2: form_snapshot with altered cover_letter fails Gate 3
    res_fail = HHSubmissionGates.check_all_gates(
        sid, url,
        form_snapshot={"cover_letter": "MUTATED cover letter! Attacker or LLM re-run."},
        human_confirmed=True, candidate_profile=_make_profile(),
    )
    assert not res_fail.passed
    assert res_fail.failed_gate == GateName.GATE_FINGERPRINT_MATCH
    assert "Fingerprint mismatch" in res_fail.reason
