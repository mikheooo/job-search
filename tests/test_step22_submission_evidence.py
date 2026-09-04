import pytest

from ai_assistant.application_review import (
    ApplicationReview,
    ReviewStatus,
    save_application_review,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.db import (
    get_connection,
    init_db,
    save_hh_application,
    save_submission,
)
from ai_assistant.hh_submission import (
    GateName,
    HHSubmissionGates,
    clear_submitted_reviews,
)
from ai_assistant.submission_state import (
    get_submission_evidence,
)


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    clear_submitted_reviews()
    db_file = str(tmp_path / "test_evidence.db")
    import ai_assistant.config as cfg
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


def _setup_approved_review(sid: str, fp: str = "valid_fp_hash"):
    save_application_review(
        ApplicationReview(
            vacancy_stable_id=sid,
            status=ReviewStatus.APPROVED,
            form_fingerprint=fp,
            review_id=f"rev_{sid}",
        )
    )


def test_submission_evidence_clean_vacancy():
    sid = "hh:10001"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    ev = get_submission_evidence(sid)
    assert not ev.is_already_applied
    assert not ev.has_active_submitting_attempt
    can_sub, reason = ev.can_submit()
    assert can_sub is True
    assert reason is None


def test_submission_evidence_detects_ambiguous_post_submit():
    sid = "hh:10002"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    save_submission(vacancy_stable_id=sid, submission_json="{}", status="AMBIGUOUS_POST_SUBMIT")

    ev = get_submission_evidence(sid)
    assert ev.is_already_applied is True
    can_sub, reason = ev.can_submit()
    assert can_sub is False
    assert "AMBIGUOUS_POST_SUBMIT" in reason


def test_submission_evidence_detects_verification_status():
    sid = "hh:10003"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO submission_verifications
           (vacancy_stable_id, submission_id, verification_version, verification_status, verified_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, "sub_v", "v1", "VERIFIED", "2026-09-04T12:00:00", "2026-09-04T12:00:00", "2026-09-04T12:00:00"),
    )
    conn.commit()
    conn.close()

    ev = get_submission_evidence(sid)
    assert ev.is_already_applied is True
    can_sub, reason = ev.can_submit()
    assert can_sub is False
    assert "VERIFIED" in reason


def test_submission_evidence_detects_hh_application_state():
    sid = "hh:10004"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    save_hh_application({
        "application_id": "app_10004",
        "vacancy_stable_id": sid,
        "title": "Python Dev",
        "employer": "Tech Corp",
        "state": "SUBMITTED",
        "created_at": "2026-09-04T12:00:00",
        "updated_at": "2026-09-04T12:00:00",
    })

    ev = get_submission_evidence(sid)
    assert ev.is_already_applied is True
    can_sub, reason = ev.can_submit()
    assert can_sub is False
    assert "SUBMITTED" in reason


def test_submission_evidence_allows_retry_after_failed_submission():
    sid = "hh:10005"
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    # A prior failure should NOT block retrying
    save_submission(vacancy_stable_id=sid, submission_json="{}", status="FAILED")

    ev = get_submission_evidence(sid)
    assert ev.is_already_applied is False
    can_sub, reason = ev.can_submit()
    assert can_sub is True
    assert reason is None


def test_gate9_blocks_even_when_review_obj_passed(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = "hh:20001"
    url = f"https://hh.ru/vacancy/{sid.split(':')[1]}"
    _setup_approved_review(sid, "fp_test")
    set_application_status(sid, ApplicationStatus.APPLIED)  # Not in whitelist!

    rev_obj = {"status": "APPROVED", "fingerprint": "fp_test", "review_id": "rev_20001"}
    snapshot = {"fingerprint": "fp_test", "cover_letter": "A valid cover letter here"}

    # In old code, passing review_obj skipped Gate 9 check!
    # In new code, Gate 9 runs ALWAYS.
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, candidate_profile=_make_profile(), review_obj=rev_obj
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_NOT_ALREADY_APPLIED
    assert "not in allowed whitelist" in res.reason


def test_gate9_blocks_on_live_dom_already_applied(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = "hh:20002"
    url = f"https://hh.ru/vacancy/{sid.split(':')[1]}"
    _setup_approved_review(sid, "fp_test")
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)

    # DOM banner detected: already applied
    snapshot = {"fingerprint": "fp_test", "cover_letter": "A valid cover letter here", "already_applied": True}
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_NOT_ALREADY_APPLIED
    assert "already responded" in res.reason


def test_gate10_blocks_active_submitting(monkeypatch):
    monkeypatch.setenv("SUBMIT_ALLOWED", "true")
    sid = "hh:20003"
    url = f"https://hh.ru/vacancy/{sid.split(':')[1]}"
    _setup_approved_review(sid, "fp_test")
    set_application_status(sid, ApplicationStatus.READY_TO_APPLY)
    save_submission(vacancy_stable_id=sid, submission_json="{}", status="SUBMITTING")

    snapshot = {"fingerprint": "fp_test", "cover_letter": "A valid cover letter here"}
    res = HHSubmissionGates.check_all_gates(
        sid, url, snapshot, human_confirmed=True, candidate_profile=_make_profile()
    )
    assert not res.passed
    assert res.failed_gate == GateName.GATE_NO_PREVIOUS_SUBMISSION_ATTEMPT
    assert "SUBMITTING" in res.reason
