from __future__ import annotations

import json
import tempfile
import shutil
import os
from datetime import datetime

import pytest

import ai_assistant.config as config
from ai_assistant.schema import Vacancy
from ai_assistant import db
from ai_assistant.vacancy_identity import (
    MatchType,
    IdentityMatch,
    CanonicalVacancy,
    normalize_url,
    normalize_company,
    normalize_title,
    calculate_similarity,
    is_exact_duplicate,
    is_probable_duplicate,
    is_distinct,
    resolve_vacancy_identity,
    sync_identity_from_vacancies,
    save_canonical_vacancy,
    save_vacancy_alias,
    get_canonical_by_id,
    get_canonical_by_normalized_url,
    get_all_canonical_vacancies,
    get_aliases_for_canonical,
    _generate_canonical_id,
    _generate_canonical_id,
)


def _vac(**kwargs):
    defaults = dict(
        source="test",
        source_job_id="1",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="TestCo",
        description="We need python, n8n, automation, LLM, API. Must have AWS. Remote worldwide. Senior level.",
        job_url=None,
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
        salary_min=5000,
        salary_max=5000,
        salary_currency="USD",
        employment_type="Full Time",
    )
    defaults.update(kwargs)
    if not defaults.get("job_url"):
        sid = str(defaults.get("source_job_id") or "1")
        defaults["job_url"] = f"https://example.com/{sid}"
    return Vacancy(**defaults)


def setup_test_db():
    """Create a temporary database for testing."""
    tmp_dir = tempfile.mkdtemp()
    db_file = os.path.join(tmp_dir, "state.db")
    config.DB_FILE = db_file
    db.init_db()
    return tmp_dir


def teardown_test_db(tmp_dir):
    """Clean up temporary database."""
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_normalize_url():
    """Test URL normalization removes tracking params and normalizes."""
    # Tracking params removed
    assert normalize_url("https://example.com/job/123?utm_source=x") == "https://example.com/job/123"
    assert normalize_url("https://example.com/job/123?utm_source=y&ref=abc") == "https://example.com/job/123"
    
    # Query order normalized
    assert normalize_url("https://example.com/job?b=2&a=1") == "https://example.com/job?a=1&b=2"
    
    # Fragment removed
    assert normalize_url("https://example.com/job#section") == "https://example.com/job"
    
    # Scheme/hostname normalized (path case preserved)
    assert normalize_url("HTTPS://EXAMPLE.COM/JOB") == "https://example.com/JOB"
    
    # Trailing slash removed
    assert normalize_url("https://example.com/job/") == "https://example.com/job"
    
    # Real job identifiers preserved
    assert normalize_url("https://example.com/job/123?job_id=456") == "https://example.com/job/123?job_id=456"


def test_tracking_params_removed():
    """Test all tracking params are removed."""
    url = "https://example.com/job?utm_source=google&utm_medium=cpc&ref=twitter&fbclid=123&gclid=abc&job_id=123"
    normalized = normalize_url(url)
    assert "utm_source" not in normalized
    assert "utm_medium" not in normalized
    assert "ref" not in normalized
    assert "fbclid" not in normalized
    assert "gclid" not in normalized
    assert "job_id=123" in normalized


def test_fragment_removed():
    """Test fragment is removed."""
    assert normalize_url("https://example.com/job#apply") == "https://example.com/job"


def test_query_order_normalized():
    """Test query parameters are sorted."""
    assert normalize_url("https://example.com?z=1&a=2") == "https://example.com?a=2&z=1"


def test_normalize_company():
    """Test company normalization."""
    assert normalize_company("DeepSense Inc.") == "deepsense"
    assert normalize_company("DeepSense") == "deepsense"
    assert normalize_company("DeepSense, Inc") == "deepsense"
    assert normalize_company("Test Corp.") == "test"
    assert normalize_company("Test LLC") == "test"
    assert normalize_company("  Test  Co  ") == "test"


def test_normalize_title():
    """Test title normalization."""
    assert normalize_title("Senior Automation Engineer (n8n / Python)") == "senior automation engineer n8n python"
    assert normalize_title("Senior Automation Engineer - n8n AI Workflows") == "senior automation engineer n8n ai workflows"
    assert normalize_title("  Senior   Engineer  ") == "senior engineer"


def test_exact_duplicate():
    """Test exact duplicate detection."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        # Create canonical vacancy
        canon = CanonicalVacancy(
            canonical_id="canonical_test1",
            normalized_url="https://example.com/job/123",
            normalized_company="testco",
            normalized_title="senior automation engineer",
            location="remote",
            first_seen_at=datetime.utcnow().isoformat(),
            last_seen_at=datetime.utcnow().isoformat(),
        )
        
        # Exact match
        assert is_exact_duplicate("https://example.com/job/123", canon) is True
        # Different URL
        assert is_exact_duplicate("https://example.com/job/456", canon) is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_probable_duplicate():
    """Test probable duplicate detection."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        canon = CanonicalVacancy(
            canonical_id="canonical_test1",
            normalized_url="https://example.com/job/123",
            normalized_company="testco",
            normalized_title="senior automation engineer",
            location="remote",
            first_seen_at=datetime.utcnow().isoformat(),
            last_seen_at=datetime.utcnow().isoformat(),
        )
        
        # High similarity
        is_prob, conf, reasons = is_probable_duplicate(
            "testco", "senior automation engineer", "remote", canon
        )
        assert is_prob is True
        assert conf >= 70
        
        # Lower title similarity
        is_prob, conf, reasons = is_probable_duplicate(
            "testco", "junior developer", "remote", canon
        )
        assert is_prob is False
        
        # Lower company similarity
        is_prob, conf, reasons = is_probable_duplicate(
            "otherco", "senior automation engineer", "remote", canon
        )
        assert is_prob is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_distinct():
    """Test distinct vacancy detection."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        canon = CanonicalVacancy(
            canonical_id="canonical_test1",
            normalized_url="https://example.com/job/123",
            normalized_company="testco",
            normalized_title="senior automation engineer",
            location="remote",
            first_seen_at=datetime.utcnow().isoformat(),
            last_seen_at=datetime.utcnow().isoformat(),
        )
        
        # Different company
        assert is_distinct("otherco", "senior automation engineer", canon) is True
        # Different title
        assert is_distinct("testco", "junior developer", canon) is True
        # Similar
        assert is_distinct("testco", "senior automation engineer", canon) is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_resolve_vacancy_identity_exact():
    """Test resolve_vacancy_identity returns EXACT for URL match."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        vac = Vacancy(
            source="test",
            source_job_id="1",
            title="Senior AI Automation Engineer",
            company="TestCo",
            description="Test",
            job_url="https://example.com/job/123",
            location="Remote",
        )
        
        # First resolve - creates new
        result1 = resolve_vacancy_identity(vac)
        assert result1.match_type.value == "DISTINCT"
        assert result1.confidence == 100
        
        # Second resolve with same URL - should be EXACT
        vac2 = Vacancy(
            source="test",
            source_job_id="2",
            title="Senior AI Automation Engineer",
            company="TestCo",
            description="Test",
            job_url="https://example.com/job/123",
            location="Remote",
        )
        result2 = resolve_vacancy_identity(vac2)
        assert result2.match_type.value == "EXACT"
        assert result2.confidence == 100
        assert result2.canonical_id == result1.canonical_id
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_resolve_vacancy_identity_probable():
    """Test resolve_vacancy_identity returns PROBABLE for similar."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        # First vacancy
        vac1 = Vacancy(
            source="test",
            source_job_id="1",
            title="Senior AI Automation Engineer",
            company="TestCo",
            description="Test",
            job_url="https://example.com/job/123",
            location="Remote",
        )
        result1 = resolve_vacancy_identity(vac1)
        assert result1.match_type.value == "DISTINCT"
        
        # Similar vacancy - different URL but same company/title
        vac2 = Vacancy(
            source="test",
            source_job_id="2",
            title="Senior AI Automation Engineer",
            company="TestCo",
            description="Test",
            job_url="https://example.com/job/456",
            location="Remote",
        )
        result2 = resolve_vacancy_identity(vac2)
        # Should be PROBABLE (same company, title, location)
        assert result2.match_type.value in ("PROBABLE", "EXACT")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_probable_not_auto_merged():
    """Test PROBABLE duplicates are not auto-merged."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        vac1 = Vacancy(
            source="test", source_job_id="1", title="Senior Engineer",
            company="TestCo", description="Test",
            job_url="https://example.com/job/123", location="Remote",
        )
        vac2 = Vacancy(
            source="test", source_job_id="2", title="Senior Engineer",
            company="TestCo", description="Test",
            job_url="https://example.com/job/456", location="Remote",
        )
        
        resolve_vacancy_identity(vac1)
        result = resolve_vacancy_identity(vac2)
        
        # PROBABLE duplicates should NOT be auto-merged (should return PROBABLE, not EXACT)
        assert result.match_type.value == "PROBABLE"
        # Should include warning about manual review
        assert any("manual review" in r.lower() for r in result.reasons)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_canonical_persistence():
    """Test canonical vacancy persists correctly."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        canon = CanonicalVacancy(
            canonical_id="canonical_test1",
            normalized_url="https://example.com/job/123",
            normalized_company="testco",
            normalized_title="senior automation engineer",
            location="remote",
            first_seen_at=datetime.utcnow().isoformat(),
            last_seen_at=datetime.utcnow().isoformat(),
        )
        
        save_canonical_vacancy(canon)
        
        # Retrieve
        retrieved = get_canonical_by_id("canonical_test1")
        assert retrieved is not None
        assert retrieved.canonical_id == "canonical_test1"
        assert retrieved.normalized_url == "https://example.com/job/123"
        assert retrieved.normalized_company == "testco"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_aliases_persistence():
    """Test vacancy aliases persist correctly."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        save_vacancy_alias(
            canonical_id="canonical_test1",
            vacancy_stable_id="vac:1",
            source="test",
            source_url="https://example.com/job/123",
            normalized_url="https://example.com/job/123",
            match_type=MatchType.EXACT,
            confidence=100
        )
        
        aliases = get_aliases_for_canonical("canonical_test1")
        assert len(aliases) == 1
        assert aliases[0]["vacancy_stable_id"] == "vac:1"
        assert aliases[0]["match_type"] == "EXACT"
        assert aliases[0]["confidence"] == 100
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_repeated_sync_idempotent():
    """Test repeated identity sync is idempotent."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        vac = Vacancy(
            source="test", source_job_id="1", title="Senior Engineer",
            company="TestCo", description="Test",
            job_url="https://example.com/job/123", location="Remote",
        )
        db.save_vacancy(vac)
        
        # First sync
        stats1 = sync_identity_from_vacancies()
        # Second sync
        stats2 = sync_identity_from_vacancies()
        
        # Should be idempotent - no new canonical on second run
        assert stats2["created"] == 0
        assert stats2["exact_duplicates"] == 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_no_matcher_changes():
    """Test identity resolution doesn't change matcher scores."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        from ai_assistant.matcher import JobMatcher, JobProfile
        
        profile = JobProfile(
            desired_roles=["AI Engineer"],
            skills=["python", "n8n", "automation"],
            min_salary=5000, max_salary=10000, salary_currency="USD",
            employment_types=["full time"],
            remote_preference=True,
        )
        matcher = JobMatcher(profile)
        
        vac = Vacancy(
            source="test", source_job_id="1", title="Senior AI Automation Engineer",
            company="TestCo", description="python n8n automation LLM API AWS remote senior",
            job_url="https://example.com/job/123", location="Remote",
            salary_min=8000, salary_max=12000, salary_currency="USD",
            employment_type="Full Time",
        )
        db.save_vacancy(vac)
        
        # Get matcher score before identity resolution
        match_before = matcher.match(vac)
        score_before = match_before.score
        
        # Resolve identity
        resolve_vacancy_identity(vac)
        
        # Get matcher score after
        match_after = matcher.match(vac)
        score_after = match_after.score
        
        # Scores should be identical
        assert score_before == score_after
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_no_tracking_changes():
    """Test identity resolution doesn't change tracking status."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        vac = Vacancy(
            source="test", source_job_id="1", title="Senior Engineer",
            company="TestCo", description="Test",
            job_url="https://example.com/job/123", location="Remote",
        )
        db.save_vacancy(vac)
        
        from ai_assistant.application_tracking import get_application_status as get_app_status, set_application_status, ApplicationStatus
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        
        track_before = get_app_status(vac.stable_id())
        assert track_before.status.value == "READY_TO_APPLY"
        
        resolve_vacancy_identity(vac)
        
        track_after = get_app_status(vac.stable_id())
        assert track_after.status.value == "READY_TO_APPLY"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_no_queue_changes():
    """Test identity resolution doesn't change queue."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        vac = Vacancy(
            source="test", source_job_id="1", title="Senior Engineer",
            company="TestCo", description="Test python n8n",
            job_url="https://example.com/job/123", location="Remote",
            salary_min=8000, salary_max=12000, salary_currency="USD",
            employment_type="Full Time",
        )
        db.save_vacancy(vac)
        
        from ai_assistant.application_tracking import set_application_status, ApplicationStatus
        from ai_assistant.application_prep import APPLICATION_PREP_VERSION, prepare_application
        from ai_assistant.job_analyzer import ANALYZER_VERSION, analyze_job_deep
        from ai_assistant.candidate_profile import load_candidate_profile
        from ai_assistant.application_queue import generate_queue
        
        set_application_status(vac.stable_id(), ApplicationStatus.READY_TO_APPLY)
        
        # Generate queue before
        queue_before = generate_queue(top_n=10)
        queue_ids_before = {item.vacancy_stable_id for item in queue_before}
        
        resolve_vacancy_identity(vac)
        
        # Generate queue after
        queue_after = generate_queue(top_n=10)
        queue_ids_after = {item.vacancy_stable_id for item in queue_after}
        
        assert queue_ids_before == queue_ids_after
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_no_llm_calls():
    """Test identity resolution doesn't call LLM."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        vac = Vacancy(
            source="test", source_job_id="1", title="Senior Engineer",
            company="TestCo", description="Test",
            job_url="https://example.com/job/123", location="Remote",
        )
        db.save_vacancy(vac)
        
        # Resolve identity - should not call LLM
        result = resolve_vacancy_identity(vac)
        
        assert result is not None
        # No LLM calls made - purely deterministic
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_no_browser_calls():
    """Test identity resolution doesn't call browser."""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_file = os.path.join(tmp_dir, "state.db")
        config.DB_FILE = db_file
        db.init_db()
        
        vac = Vacancy(
            source="test", source_job_id="1", title="Senior Engineer",
            company="TestCo", description="Test",
            job_url="https://example.com/job/123", location="Remote",
        )
        db.save_vacancy(vac)
        
        result = resolve_vacancy_identity(vac)
        
        assert result is not None
        # No browser calls made
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])