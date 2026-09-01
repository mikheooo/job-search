"""Stage 31: Tests for Controlled Auto-Apply Watcher.

Verifies:
1. New matching vacancy -> READY_FOR_REVIEW.
2. Non-matching vacancy -> filtered / rejected (hard constraints / remote / matcher SKIP).
3. Duplicate vacancy -> idempotency, no duplicate applications or queue entries.
4. Unresolved question -> NEEDS_HUMAN_REVIEW.
5. Watcher NEVER calls final Submit (submit_attempted=False, submit_count=0, click_count=0).
6. Missing/ambiguous CDP target -> fail closed (BLOCKED).
7. Human approval required at review gate.
8. After human approval, existing Stage 30D / 20K submission flow is used.
9. CLI `job-search watch --once` integration.
"""

from __future__ import annotations

import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    list_applications,
)
from ai_assistant.application_queue import get_queue_item, list_queue
from ai_assistant.application_review import get_application_review
from ai_assistant.application_prep import (
    ApplicationPackage,
    ResumeAdaptation,
    ApplicationAnswer,
)
from ai_assistant.hh_extractor import QuestionType, ApplicationForm, ApplicationType
from ai_assistant.job_analyzer import DeepAnalysisResult
from ai_assistant.application_review_gate import (
    HumanReviewGate,
    HumanReviewStore,
    GateStatus,
    build_review_gate,
)
from ai_assistant.hh_human_submission import confirm_human_submission
from ai_assistant.hh_controlled_submit import controlled_real_submit
from ai_assistant.prefill_plan import build_prefill_plan
from ai_assistant.auto_apply_modes import _zero_op_orchestration
from ai_assistant import db
import ai_assistant.config as config
import ai_assistant.watcher
from ai_assistant.watcher import (
    Watcher,
    WatcherConfig,
    WatcherStatus,
    run_watcher_cycle,
)
from ai_assistant import cli


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

class MockAdapter:
    def __init__(self, vacancies: list[dict]):
        self._vacancies = vacancies

    def fetch_vacancies(self) -> list[dict]:
        return self._vacancies


def _create_sample_vacancy_dict(
    sid: str = "vac-1",
    title: str = "Senior AI Automation Engineer",
    company: str = "Acme Corp",
    desc: str = "We need an AI Automation Engineer with Python, n8n, and LLM experience. Fully remote worldwide.",
    location: str = "Remote",
    salary_min: int = 5000,
    salary_max: int = 7000,
    salary_currency: str = "USD",
    source: str = "himalayas",
) -> dict:
    return {
        "source": source,
        "source_job_id": sid,
        "title": title,
        "company": company,
        "description": desc,
        "job_url": f"https://example.com/jobs/{sid}",
        "location": location,
        "country_restrictions": [],
        "timezone_restrictions": [],
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "employment_type": "Full-time",
    }


def _create_test_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["AI Automation Engineer", "Python Developer"],
        alternative_roles=["Software Engineer"],
        skills=["Python", "n8n", "LLM", "API Automation", "FastAPI"],
        preferred_seniority=["Senior", "Lead"],
        remote_required=True,
        allowed_locations=["Remote", "Worldwide"],
        allowed_timezones=[],
        languages=["English", "Russian"],
        employment_types=["Full-time"],
        minimum_salary=4000,
        salary_currency="USD",
        years_experience="5",
        excluded_roles=["DevOps", "Frontend React"],
        excluded_companies=["SpammyCorp"],
        excluded_countries=[],
        excluded_industries=[],
    )


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_watcher.db")
    config.DB_FILE = db_file
    db.init_db()

    orig_ja = os.environ.get("JOB_ANALYZER_OFFLINE")
    orig_ap = os.environ.get("APPLICATION_PREP_OFFLINE")
    os.environ["JOB_ANALYZER_OFFLINE"] = "1"
    os.environ["APPLICATION_PREP_OFFLINE"] = "1"

    profile = _create_test_profile()
    profile_path = str(tmp_path / "test_profile.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f)

    yield {"db_file": db_file, "profile_path": profile_path, "profile": profile}

    config.DB_FILE = orig_db
    if orig_ja is not None:
        os.environ["JOB_ANALYZER_OFFLINE"] = orig_ja
    else:
        os.environ.pop("JOB_ANALYZER_OFFLINE", None)
    if orig_ap is not None:
        os.environ["APPLICATION_PREP_OFFLINE"] = orig_ap
    else:
        os.environ.pop("APPLICATION_PREP_OFFLINE", None)


# ---------------------------------------------------------------------------
# Test 1: New matching vacancy -> READY_FOR_REVIEW
# ---------------------------------------------------------------------------

def test_new_matching_vacancy_reaches_ready_for_review(clean_db):
    """A new matching vacancy is collected, normalized, analyzed, prepared, and queued in READY_FOR_REVIEW."""
    raw = _create_sample_vacancy_dict(sid="match-1")
    adapter = MockAdapter([raw])

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    result = run_watcher_cycle(cfg)

    assert result.fetched_count == 1
    assert result.new_vacancies_count == 1
    assert result.matched_count == 1
    assert result.analyzed_count == 1
    assert result.prepared_count == 1
    assert result.ready_for_review_count == 1
    assert result.needs_human_review_count == 0
    assert result.blocked_count == 0
    assert len(result.items) == 1

    item = result.items[0]
    assert item.status == WatcherStatus.READY_FOR_REVIEW.value
    assert item.match_decision in ("APPLY", "REVIEW")
    assert item.submit_attempted is False
    assert item.submit_count == 0
    assert item.click_count == 0

    # Verify DB persistence
    track = get_application_status(item.vacancy_stable_id)
    assert track is not None
    assert track.status == ApplicationStatus.READY_TO_APPLY

    qitem = get_queue_item(item.vacancy_stable_id)
    assert qitem is not None
    assert qitem.priority_score > 0

    rev = get_application_review(item.vacancy_stable_id)
    assert rev is not None
    assert rev.company == "Acme Corp"


# ---------------------------------------------------------------------------
# Test 2: Non-matching vacancy -> filtered / rejected
# ---------------------------------------------------------------------------

def test_non_matching_vacancy_rejected_by_hard_constraints(clean_db):
    """A vacancy excluded by company or role or non-remote is filtered out and marked REJECTED."""
    # Excluded company "SpammyCorp" and excluded role "Frontend React"
    raw_bad_company = _create_sample_vacancy_dict(sid="bad-1", company="SpammyCorp")
    raw_bad_role = _create_sample_vacancy_dict(sid="bad-2", title="Frontend React Developer")
    raw_non_remote = _create_sample_vacancy_dict(sid="bad-3", location="On-site New York ONLY, NO REMOTE")

    adapter = MockAdapter([raw_bad_company, raw_bad_role, raw_non_remote])

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    result = run_watcher_cycle(cfg)

    assert result.fetched_count == 3
    assert result.new_vacancies_count == 3
    assert result.rejected_count == 3
    assert result.matched_count == 0
    assert result.ready_for_review_count == 0

    # Verify tracking status for rejected items
    for sid in ["bad-1", "bad-2", "bad-3"]:
        db_vac = db.get_vacancy_by_id(f"mock:{sid}") or db.get_vacancy_by_id(f"himalayas:{sid}")
        assert db_vac is not None


# ---------------------------------------------------------------------------
# Test 3: Duplicate vacancy -> Idempotency
# ---------------------------------------------------------------------------

def test_duplicate_vacancy_idempotency(clean_db):
    """Running subsequent polling cycles does not create duplicate entries or duplicate applications."""
    raw = _create_sample_vacancy_dict(sid="idemp-1")
    adapter = MockAdapter([raw])

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    # Cycle 1
    res1 = run_watcher_cycle(cfg, iteration=1)
    assert res1.new_vacancies_count == 1
    assert res1.duplicate_count == 0
    assert res1.ready_for_review_count == 1

    # Cycle 2 with identical data
    res2 = run_watcher_cycle(cfg, iteration=2)
    assert res2.new_vacancies_count == 0
    assert res2.duplicate_count == 1
    assert res2.ready_for_review_count == 0  # Already processed!

    # Verify only 1 tracking and 1 queue record exist
    sid = res1.items[0].vacancy_stable_id
    apps = list_applications(limit=50)
    matching_apps = [a for a in apps if a.vacancy_stable_id == sid]
    assert len(matching_apps) == 1

    q_items = list_queue(limit=50)
    matching_q = [q for q in q_items if q.vacancy_stable_id == sid]
    assert len(matching_q) == 1


# ---------------------------------------------------------------------------
# Test 4: Unresolved question -> NEEDS_HUMAN_REVIEW
# ---------------------------------------------------------------------------

def test_unresolved_screening_questions_yields_needs_human_review(clean_db):
    """When an application package contains questions needing review, status is NEEDS_HUMAN_REVIEW."""
    raw = _create_sample_vacancy_dict(sid="unresolved-1")
    adapter = MockAdapter([raw])

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    # Mock prepare_application to inject an unresolved question
    orig_prep = ai_assistant.watcher.prepare_application

    def mock_prepare(vac, deep, prof):
        pkg = orig_prep(vac, deep, prof)
        if pkg:
            pkg.answers.append(
                ApplicationAnswer(
                    question_id="task_123",
                    answer=None,
                    requires_review=True,
                    confidence=0.0,
                )
            )
            pkg.validation_status = "NEEDS_REVIEW"
        return pkg

    with patch("ai_assistant.watcher.prepare_application", side_effect=mock_prepare):
        result = run_watcher_cycle(cfg)

    assert result.ready_for_review_count == 0
    assert result.needs_human_review_count == 1
    assert len(result.items) == 1
    assert result.items[0].status == WatcherStatus.NEEDS_HUMAN_REVIEW.value
    assert "task_123" in result.items[0].unresolved_questions
    assert result.items[0].submit_attempted is False


# ---------------------------------------------------------------------------
# Test 5: Invariant - Watcher NEVER calls final Submit
# ---------------------------------------------------------------------------

def test_watcher_never_executes_submit_click(clean_db):
    """The watcher physically does not attempt submit or click under any circumstances."""
    raw = _create_sample_vacancy_dict(sid="safe-1")
    adapter = MockAdapter([raw])

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    watcher = Watcher(cfg)
    results = watcher.run(stop_callback=lambda: True)

    assert len(results) == 1
    res = results[0]

    for item in res.items:
        assert item.submit_attempted is False
        assert item.submit_count == 0
        assert item.click_count == 0

    # Ensure no submissions in DB
    submissions = db.list_submissions(limit=10)
    assert len(submissions) == 0


# ---------------------------------------------------------------------------
# Test 6: Missing / Ambiguous CDP target -> Fail Closed (BLOCKED)
# ---------------------------------------------------------------------------

def test_cdp_target_failure_fails_closed(clean_db):
    """If CDP evaluate fails or returns empty/invalid tab info, watcher transitions to BLOCKED."""
    raw = _create_sample_vacancy_dict(sid="cdp-fail-1")
    adapter = MockAdapter([raw])

    def broken_evaluate(expr: str) -> str:
        raise RuntimeError("CDP WebSocket connection refused: 127.0.0.1:9222")

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        evaluate_fn=broken_evaluate,
        batch_limit=10,
    )

    result = run_watcher_cycle(cfg)

    assert result.blocked_count == 1
    assert len(result.items) == 1
    item = result.items[0]
    assert item.status == WatcherStatus.BLOCKED.value
    assert "failed closed" in item.stop_reason.lower() or "cdp" in item.stop_reason.lower()
    assert item.submit_attempted is False


# ---------------------------------------------------------------------------
# Test 7: Human approval required at review gate
# ---------------------------------------------------------------------------

def test_human_approval_required_before_submission(clean_db):
    """A vacancy in READY_FOR_REVIEW cannot proceed without explicit HumanReviewStore approval."""
    raw = _create_sample_vacancy_dict(sid="gate-1")
    adapter = MockAdapter([raw])

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    result = run_watcher_cycle(cfg)
    item = result.items[0]
    sid = item.vacancy_stable_id

    # Retrieve package and plan
    pkg_row = db.get_application_package(sid)
    pkg = ApplicationPackage.model_validate_json(pkg_row[2])
    pkg.validation_status = "VALID"

    form = ApplicationForm(
        source="hh",
        vacancy_stable_id=sid,
        application_type=ApplicationType.resume_only,
        questions=[],
    )
    plan = build_prefill_plan(pkg, form, {})
    orch = _zero_op_orchestration(sid, "test")
    gate = build_review_gate(pkg, plan, orch, {}, form=form)

    store = HumanReviewStore()
    rid = store.save(gate)

    # Without approval, confirm_human_submission fails
    conf_unapproved = confirm_human_submission(store, rid, gate.fingerprint, sid)
    assert conf_unapproved["ok"] is False
    assert "HUMAN_APPROVED" in conf_unapproved["reason"] or "not approved" in conf_unapproved["reason"]

    # Explicit approval
    store.mark_waiting_for_human(rid)
    appr = store.approve_review(rid, gate.fingerprint)
    assert appr["ok"] is True

    # Now confirmation passes
    conf_approved = confirm_human_submission(store, rid, gate.fingerprint, sid)
    assert conf_approved["ok"] is True


# ---------------------------------------------------------------------------
# Test 8: After approval, existing Stage 30D / 20K submit flow works
# ---------------------------------------------------------------------------

def test_approved_vacancy_can_submit_via_existing_stage20k(clean_db):
    """An approved review is successfully submitted using existing controlled_real_submit."""
    raw = _create_sample_vacancy_dict(sid="submit-flow-1")
    adapter = MockAdapter([raw])

    cfg = WatcherConfig(
        custom_adapters={"mock": adapter},
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    result = run_watcher_cycle(cfg)
    item = result.items[0]
    sid = item.vacancy_stable_id

    pkg_row = db.get_application_package(sid)
    pkg = ApplicationPackage.model_validate_json(pkg_row[2])
    pkg.validation_status = "VALID"

    form = ApplicationForm(
        source="hh",
        vacancy_stable_id=sid,
        application_type=ApplicationType.resume_only,
        questions=[],
    )
    plan = build_prefill_plan(pkg, form, {})
    orch = _zero_op_orchestration(sid, "test")
    gate = build_review_gate(pkg, plan, orch, {}, form=form)

    store = HumanReviewStore()
    rid = store.save(gate)
    store.mark_waiting_for_human(rid)
    store.approve_review(rid, gate.fingerprint)

    class TestCDP:
        def __init__(self):
            self.url = "https://hh.ru/applicant/vacancy_response?vacancyId=submit-flow-1"
            self.clicked = False

        def evaluate(self, expression: str) -> str:
            if expression == 'JSON.stringify({url: location.href})':
                return json.dumps({"url": self.url})
            if "markers" in expression and "body" in expression:
                return json.dumps({"found": ["Вы откликнулись"] if self.clicked else [], "url": self.url})
            if '"vacancy-response-submit-popup"' in expression and "el.click()" not in expression:
                return json.dumps({
                    "found": True,
                    "tag": "BUTTON",
                    "type": "submit",
                    "text": "Откликнуться",
                    "dataQa": "vacancy-response-submit-popup",
                    "disabled": False,
                    "visible": True,
                    "cls": "magritte-button",
                })
            if "el.click()" in expression:
                self.clicked = True
                return json.dumps({"ok": True})
            return json.dumps({"ok": True})

        def evaluate_fn(self, expr: str) -> str:
            return self.evaluate(expr)

    cdp = TestCDP()

    submit_rep = controlled_real_submit(
        store,
        rid,
        gate.fingerprint,
        pkg,
        plan,
        orch,
        cdp.evaluate,
        expected_url_markers=("hh.ru", "applicant/vacancy_response"),
    )

    assert submit_rep.click_count == 1
    assert submit_rep.submit_count == 1
    assert submit_rep.verdict == "SUBMITTED"


# ---------------------------------------------------------------------------
# Test 9: CLI `job-search watch --once` integration
# ---------------------------------------------------------------------------

def test_cli_watch_once_json_output(clean_db, capsys):
    """CLI watch command with --once and --json produces valid JSON report and returns 0."""
    raw = _create_sample_vacancy_dict(sid="cli-watch-1")
    adapter = MockAdapter([raw])

    with patch.dict(cli.SOURCES, {"mock": adapter}, clear=True):
        ret = cli.watch_cmd(
            sources=["mock"],
            once=True,
            limit=5,
            profile_path=clean_db["profile_path"],
            output_json=True,
        )

    assert ret == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["fetched_count"] == 1
    assert data["new_vacancies_count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["submit_attempted"] is False


def test_dry_run_never_calls_real_vacancy_adapters(clean_db):
    """Dry-run previews local state and never invokes configured live sources."""
    forbidden = MagicMock(side_effect=AssertionError("live adapter called during dry-run"))
    adapters = {
        name: MagicMock(fetch_vacancies=forbidden)
        for name in ("himalayas", "remoteok", "weworkremotely", "habrcareer")
    }
    cfg = WatcherConfig(
        sources=list(adapters),
        profile_path=clean_db["profile_path"],
        batch_limit=5,
    )

    with patch.dict(cli.SOURCES, adapters, clear=True):
        result = run_watcher_cycle(cfg, dry_run=True)

    assert result.errors == []
    assert forbidden.call_count == 0
