from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.application_queue import (
    QueueItem,
    QUEUE_VERSION,
    compute_priority,
    build_queue_items,
    generate_queue,
    save_queue_item,
    get_queue_item,
    list_queue,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.job_analyzer import DeepAnalysisResult, ANALYZER_VERSION
from ai_assistant.matcher import JobMatcher

def _vac(sid="1", title="Test Engineer", desc="python", company="Acme", source="test", job_url=None, salary_min=None, salary_max=None, salary_currency=None, published_at=None, first_seen_at=None):
    if job_url is None:
        job_url = f"https://example.com/{sid}_{source}"
    vac = Vacancy(
        source=source,
        source_job_id=str(sid),
        title=title,
        company=company,
        description=desc,
        job_url=job_url,
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        employment_type="Full Time",
        published_at=published_at,
        first_seen_at=first_seen_at,
    )
    # override published if provided as datetime
    if published_at:
        vac.published_at = published_at
    if first_seen_at:
        vac.first_seen_at = first_seen_at
    return vac

def _profile():
    return CandidateProfile(
        desired_roles=["Test Engineer"],
        alternative_roles=[],
        skills=["python", "n8n"],
        preferred_seniority=[],
        remote_required=True,
        allowed_locations=["Remote"],
        allowed_timezones=[],
        languages=[],
        employment_types=[],
        minimum_salary=3000,
        salary_currency="USD",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )

def _deep(fit=80, rec="APPLY", missing=None, resume_needed=False, must=None):
    return DeepAnalysisResult(
        fit_score=fit,
        recommendation=rec,
        why_fit=["fit"],
        gaps=[],
        must_have_requirements=must or ["python"],
        nice_to_have_requirements=[],
        matched_skills=["python"],
        missing_skills=missing or [],
        seniority_assessment="",
        remote_assessment="",
        salary_assessment="",
        resume_adaptation_needed=resume_needed,
        resume_adaptation_reasons=[],
        application_strategy="apply",
    )

def test_deterministic_ranking():
    profile = _profile()
    vac1 = _vac(sid="a", title="Test Engineer", desc="python")
    vac2 = _vac(sid="b", title="Test Engineer", desc="python")
    # same scores
    m1 = type("obj", (), {"score": 90})()
    m2 = type("obj", (), {"score": 90})()
    d1 = _deep(fit=90)
    d2 = _deep(fit=90)
    # compute twice
    items1 = build_queue_items([vac1, vac2], profile, {vac1.stable_id(): m1, vac2.stable_id(): m2}, {vac1.stable_id(): d1, vac2.stable_id(): d2})
    items2 = build_queue_items([vac2, vac1], profile, {vac1.stable_id(): m1, vac2.stable_id(): m2}, {vac1.stable_id(): d1, vac2.stable_id(): d2})
    # deterministic: same order regardless of input order? Should be sorted by stable_id tie-break
    assert items1[0].vacancy_stable_id == items2[0].vacancy_stable_id
    assert items1[0].priority_score == items2[0].priority_score

def test_high_deep_score_ranks_higher():
    profile = _profile()
    vac1 = _vac(sid="highdeep", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD")
    vac2 = _vac(sid="lowdeep", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD")
    m = type("obj", (), {"score": 80})()
    d_high = _deep(fit=95)
    d_low = _deep(fit=60)
    items = build_queue_items([vac1, vac2], profile, {vac1.stable_id(): m, vac2.stable_id(): m}, {vac1.stable_id(): d_high, vac2.stable_id(): d_low})
    # vac1 should rank 1
    assert items[0].vacancy_stable_id == vac1.stable_id()
    assert items[0].priority_score > items[1].priority_score

def test_high_match_ranks_higher():
    profile = _profile()
    vac1 = _vac(sid="highmatch", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD")
    vac2 = _vac(sid="lowmatch", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD")
    m_high = type("obj", (), {"score": 95})()
    m_low = type("obj", (), {"score": 60})()
    d = _deep(fit=80)
    items = build_queue_items([vac1, vac2], profile, {vac1.stable_id(): m_high, vac2.stable_id(): m_low}, {vac1.stable_id(): d, vac2.stable_id(): d})
    assert items[0].vacancy_stable_id == vac1.stable_id()

def test_missing_must_have_lowers_priority():
    profile = _profile()
    vac1 = _vac(sid="nomissing", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD")
    vac2 = _vac(sid="missing", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD")
    m = type("obj", (), {"score": 80})()
    d_ok = _deep(fit=80, missing=[])
    d_missing = _deep(fit=80, missing=["AWS", "GCP"], must=["AWS", "GCP"])
    items = build_queue_items([vac1, vac2], profile, {vac1.stable_id(): m, vac2.stable_id(): m}, {vac1.stable_id(): d_ok, vac2.stable_id(): d_missing})
    # vac1 should rank higher
    assert items[0].vacancy_stable_id == vac1.stable_id()
    assert items[0].priority_score > items[1].priority_score

def test_salary_fit_affects_score():
    profile = _profile()
    # salary fit 100 vs 0
    vac_ok = _vac(sid="salary_ok", title="Test Engineer", desc="python", salary_min=4000, salary_max=5000, salary_currency="USD", published_at=datetime.utcnow())
    vac_bad = _vac(sid="salary_bad", title="Test Engineer", desc="python", salary_min=1000, salary_max=1000, salary_currency="USD", published_at=datetime.utcnow())
    m = type("obj", (), {"score": 80})()
    d = _deep(fit=80)
    # compute separately
    p_ok, _, _, _ = compute_priority(vac_ok, profile, 80, 80, d)
    p_bad, _, _, _ = compute_priority(vac_bad, profile, 80, 80, d)
    assert p_ok > p_bad

def test_unknown_salary_doesnt_invent_value():
    profile = _profile()
    vac = _vac(sid="unknown_sal", title="Test Engineer", desc="python", salary_min=None, salary_max=None, salary_currency=None, published_at=datetime.utcnow())
    m = type("obj", (), {"score": 80})()
    d = _deep(fit=80)
    priority, comps, reasons, warnings = compute_priority(vac, profile, 80, 80, d)
    # salary_fit should be 0, not 50 invented
    assert comps["salary_fit"] == 0
    assert any("unknown" in w.lower() for w in warnings)

def test_freshness_works_when_date_exists():
    profile = _profile()
    recent = _vac(sid="recent", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD", published_at=datetime.utcnow())
    stale = _vac(sid="stale", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD", published_at=datetime.utcnow() - timedelta(days=90))
    m = type("obj", (), {"score": 80})()
    d = _deep(fit=80)
    p_recent, comps_r, _, _ = compute_priority(recent, profile, 80, 80, d)
    p_stale, comps_s, _, _ = compute_priority(stale, profile, 80, 80, d)
    assert comps_r["freshness"] > comps_s["freshness"]
    assert p_recent > p_stale

def test_missing_date_is_neutral():
    profile = _profile()
    vac = _vac(sid="nodate", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD")
    # remove dates
    vac.published_at = None
    vac.first_seen_at = None
    vac.last_seen_at = None
    from ai_assistant.application_queue import _freshness_score
    score, reason = _freshness_score(vac)
    assert score == 50
    assert "neutral" in reason.lower()

def test_only_ready_to_apply_enters_queue():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "q.db")
        config.DB_FILE = db_file
        db.init_db()
        # create 3 vacancies with different tracking statuses
        prof = _profile()
        # Create profile file for sync
        prof_path = Path(tmp) / "profile.json"
        prof_path.write_text(json.dumps(prof.to_dict()), encoding="utf-8")
        from ai_assistant.application_tracking import set_application_status, ApplicationStatus
        # create vacancies
        vac_ready = _vac(sid="ready", title="Test Engineer", desc="python")
        vac_disc = _vac(sid="disc", title="Test Engineer", desc="python")
        vac_applied = _vac(sid="applied", title="Test Engineer", desc="python")
        for v in [vac_ready, vac_disc, vac_applied]:
            db.save_vacancy(v)
        # set tracking statuses
        set_application_status(vac_ready.stable_id(), ApplicationStatus.READY_TO_APPLY, company="C", title="T", source="test", vacancy_url="http://a")
        set_application_status(vac_disc.stable_id(), ApplicationStatus.DISCOVERED, company="C", title="T", source="test", vacancy_url="http://a")
        set_application_status(vac_applied.stable_id(), ApplicationStatus.APPLIED, company="C", title="T", source="test", vacancy_url="http://a")
        # Need matcher APPLY for ready to be considered, but we already have tracking; queue generation will use tracking list which only includes READY
        # For this test, directly call list_applications and then generate
        # Create deep and match for ready vacancy to have scores
        # Instead test generate_queue filtering: it should only pick READY
        # Mock deep and match via build not via sync; easier test via generate_queue internals
        from ai_assistant.application_queue import generate_queue
        # Ensure ready vacancy has deep and match via sync? Simplify: we test that generate_queue only outputs READY
        # We can directly test that list_queue filtering works, but we want to test generate_queue logic
        # For simplicity, create deep for ready vacancy
        db.save_deep_analysis(vac_ready.stable_id(), ANALYZER_VERSION, 90, "APPLY", json.dumps({"fit_score":90,"recommendation":"APPLY","why_fit":[],"gaps":[],"must_have_requirements":[],"nice_to_have_requirements":[],"matched_skills":[],"missing_skills":[],"seniority_assessment":"","remote_assessment":"","salary_assessment":"","resume_adaptation_needed":False,"resume_adaptation_reasons":[],"application_strategy":""}))
        # Now generate queue with top 10, should only include ready
        items = generate_queue(top_n=10, profile_path=str(prof_path))
        ids = [i.vacancy_stable_id for i in items]
        assert vac_ready.stable_id() in ids
        assert vac_disc.stable_id() not in ids
        assert vac_applied.stable_id() not in ids
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_queue_persistence():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "q.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="persist", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD", published_at=datetime.utcnow())
        item = QueueItem(
            vacancy_stable_id=vac.stable_id(),
            canonical_id="canonical_test",
            representative_vacancy_stable_id=vac.stable_id(),
            priority_score=90,
            match_score=90,
            deep_score=85,
            company=vac.company,
            title=vac.title,
            source=vac.source,
            vacancy_url=vac.job_url,
            reasons=["high deep"],
            warnings=[],
            rank=1,
            components={"match_score":90},
            application_strategy="apply",
        )
        save_queue_item(item)
        fetched = get_queue_item(vac.stable_id(), QUEUE_VERSION)
        assert fetched is not None
        assert fetched.priority_score == 90
        assert fetched.rank == 1
        # without version also fetches
        fetched2 = get_queue_item(vac.stable_id())
        assert fetched2 is not None
        all_items = list_queue(limit=10)
        assert len(all_items) == 1
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_queue_version_invalidation():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "q.db")
        config.DB_FILE = db_file
        db.init_db()
        vac = _vac(sid="ver", title="Test Engineer", desc="python")
        item = QueueItem(
            vacancy_stable_id=vac.stable_id(),
            canonical_id="canonical_test",
            representative_vacancy_stable_id=vac.stable_id(),
            priority_score=80,
            match_score=80,
            deep_score=80,
            company=vac.company,
            title=vac.title,
            source=vac.source,
            vacancy_url=vac.job_url,
            reasons=[],
            warnings=[],
            rank=1,
        )
        save_queue_item(item)
        # fetch with v1 should succeed
        assert get_queue_item(vac.stable_id(), QUEUE_VERSION) is not None
        # fetch with different version should be None
        assert get_queue_item(vac.stable_id(), "v999") is None
        # also test that saving with new version overwrites but old version invalidated
        import ai_assistant.application_queue as aq
        orig_ver = aq.QUEUE_VERSION
        try:
            aq.QUEUE_VERSION = "v2"
            item2 = QueueItem(
                vacancy_stable_id=vac.stable_id(),
                canonical_id="canonical_test",
                representative_vacancy_stable_id=vac.stable_id(),
                priority_score=70,
                match_score=70,
                deep_score=70,
                company=vac.company,
                title=vac.title,
                source=vac.source,
                vacancy_url=vac.job_url,
                reasons=[],
                warnings=[],
                rank=1,
                queue_version="v2",
            )
            save_queue_item(item2)
            assert get_queue_item(vac.stable_id(), "v2") is not None
            assert get_queue_item(vac.stable_id(), "v1") is None  # overwritten, old not found with v1 because PK same
            # Actually with PK same, old is overwritten, so v1 fetch after overwrite would be None, which demonstrates invalidation
        finally:
            aq.QUEUE_VERSION = orig_ver
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)

def test_repeated_generation_is_idempotent():
    tmp = tempfile.mkdtemp()
    orig = config.DB_FILE
    try:
        db_file = str(Path(tmp) / "q.db")
        config.DB_FILE = db_file
        db.init_db()
        prof = _profile()
        prof_path = Path(tmp) / "profile.json"
        prof_path.write_text(json.dumps(prof.to_dict()), encoding="utf-8")
        # Create 2 ready vacancies
        vac1 = _vac(sid="idem1", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD", published_at=datetime.utcnow())
        vac2 = _vac(sid="idem2", title="Test Engineer", desc="python", salary_min=4000, salary_currency="USD", published_at=datetime.utcnow() - timedelta(days=30))
        for v in [vac1, vac2]:
            db.save_vacancy(v)
            set_application_status(v.stable_id(), ApplicationStatus.READY_TO_APPLY, company=v.company, title=v.title, source=v.source, vacancy_url=v.job_url, match_score=90, deep_score=90)
            # add deep
            deep = _deep(fit=90)
            db.save_deep_analysis(v.stable_id(), ANALYZER_VERSION, deep.fit_score, deep.recommendation, deep.model_dump_json())
        # first generate
        items1 = generate_queue(top_n=2, profile_path=str(prof_path))
        # second generate should produce same ranking
        items2 = generate_queue(top_n=2, profile_path=str(prof_path))
        assert len(items1) == len(items2) == 2
        assert items1[0].vacancy_stable_id == items2[0].vacancy_stable_id
        assert items1[0].priority_score == items2[0].priority_score
        # Also check persistence not duplicated
        all_items = list_queue(limit=10)
        assert len(all_items) == 2
    finally:
        config.DB_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)
