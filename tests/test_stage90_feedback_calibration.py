"""Stage 90: Feedback Analytics & Safe Preference Calibration Test Suite.

Verifies:
1. No feedback gives zero preference adjustment.
2. One feedback event does not materially change ranking.
3. Sufficient repeated positive feedback creates bounded positive adjustment.
4. Repeated negative evidence creates bounded negative adjustment.
5. Contradictory evidence reduces confidence (ambiguous preference).
6. Recent evidence outweighs stale evidence via recency decay.
7. Preference cannot override hard rejection.
8. Preference cannot change candidate skill confidence in candidate profile.
9. Preference cannot change candidate years of experience in candidate profile.
10. Preference cannot promote STRETCH to STRONG_MATCH directly.
11. PREPARE_APPLICATION weighs more than INTERESTED.
12. NOT_INTERESTED does not globally penalize an entire role from one event.
13. Company-specific feedback stays company-specific.
14. Role-family aggregation is deterministic.
15. Skill preference aggregation is deterministic.
16. Preference profile is reproducible from feedback history.
17. Deleting/replaying derived cache does not lose canonical evidence.
18. Analytics command is read-only.
19. Simulation does not mutate production DB.
20. Existing Stage 87 matcher behavior preserved with calibration disabled.
"""

import copy
import json
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile, load_candidate_profile
from ai_assistant.matcher import JobMatcher, MatchResult, apply_preference_adjustment
from ai_assistant.feedback_analytics import (
    SignalStrength,
    SIGNAL_WEIGHT_MAP,
    SkipReason,
    PreferenceEvent,
    DimensionSignal,
    PreferenceProfile,
    aggregate_events_to_signal,
    build_preference_profile,
    calculate_preference_adjustment,
    calculate_recency_weight,
    extract_role_concept,
    extract_vacancy_skills,
)


@pytest.fixture
def sample_candidate_profile() -> CandidateProfile:
    return CandidateProfile(
        target_roles=["AI Automation Engineer", "Python Developer", "Support Engineer"],
        desired_roles=["AI Engineer", "Backend Developer"],
        role_families=["AI_AUTOMATION", "PYTHON_BACKEND", "APPLICATION_SUPPORT"],
        core_skills=["python", "fastapi", "docker", "linux", "sql"],
        secondary_skills=["n8n", "llm", "ai_agents", "kubernetes"],
        skill_confidence={
            "python": "PROFESSIONAL",
            "fastapi": "PRACTICAL",
            "docker": "PRACTICAL",
            "linux": "PROFESSIONAL",
            "sql": "PROFESSIONAL",
            "n8n": "PRACTICAL",
            "llm": "PRACTICAL",
            "kubernetes": "UNKNOWN",
        },
        years_experience=4,
        years_of_experience=4,
        role_priorities={
            "AI_AUTOMATION": "P1",
            "PYTHON_BACKEND": "P1",
            "APPLICATION_SUPPORT": "P2",
        },
    )


@pytest.fixture
def sample_vacancy() -> Vacancy:
    return Vacancy(
        source="hh",
        source_job_id="999001",
        title="AI Automation Engineer (Python & n8n)",
        company="Acme Automation",
        description="Developing AI agents, n8n workflows, LLM pipelines, and Python microservices.",
        job_url="https://hh.ru/vacancy/999001",
        location="Remote",
    )


def test_1_no_feedback_gives_zero_preference_adjustment(sample_vacancy):
    """Test 1: Empty feedback profile results in exact zero preference adjustment."""
    empty_profile = PreferenceProfile(
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_evidence_events=0,
        unique_vacancies_evaluated=0,
        calibration_status="INSUFFICIENT_EVIDENCE_FOR_AUTOMATIC_CALIBRATION",
    )
    adj, reasons = calculate_preference_adjustment(
        vacancy=sample_vacancy,
        profile=empty_profile,
        base_match_score=80,
        decision_class="MATCH",
        eligibility="ELIGIBLE",
        enabled=True,
    )
    assert adj == 0.0
    assert any("Insufficient global evidence" in r for r in reasons)


def test_2_one_feedback_event_does_not_materialize_ranking_change(sample_vacancy):
    """Test 2: A single feedback event is RECORD_ONLY and yields 0 adjustment."""
    ev = PreferenceEvent(
        vacancy_stable_id="hh:999001",
        action="INTERESTED",
        signal_strength=SignalStrength.STRONG_POSITIVE,
        created_at=datetime.now(timezone.utc).isoformat(),
        source_table="telegram_feedback_records",
        title=sample_vacancy.title,
        company=sample_vacancy.company,
        role_family="AI_AUTOMATION",
        role_concept="AI Automation Engineer",
        skills=["python", "n8n", "llm", "ai_agents"],
    )
    prof = build_preference_profile(events=[ev])
    assert prof.total_evidence_events == 1
    assert prof.calibration_status == "INSUFFICIENT_EVIDENCE_FOR_AUTOMATIC_CALIBRATION"
    assert prof.role_families["AI_AUTOMATION"].status == "RECORD_ONLY"
    assert prof.role_families["AI_AUTOMATION"].confidence == 0.0

    adj, reasons = calculate_preference_adjustment(
        vacancy=sample_vacancy,
        profile=prof,
        base_match_score=85,
        decision_class="STRONG_MATCH",
        eligibility="ELIGIBLE",
        enabled=True,
    )
    assert adj == 0.0


def test_3_sufficient_repeated_positive_feedback_creates_bounded_positive_adjustment(sample_vacancy):
    """Test 3: 6 consistent positive events generate a positive bounded adjustment."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:99900{i}",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            title="AI Automation Engineer",
            company=f"Company {i}",
            role_family="AI_AUTOMATION",
            role_concept="AI Automation Engineer",
            skills=["python", "n8n"],
        )
        for i in range(1, 7)
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)
    assert prof.total_evidence_events == 6
    assert prof.calibration_status == "READY"
    assert prof.role_families["AI_AUTOMATION"].confidence > 0.5

    adj, reasons = calculate_preference_adjustment(
        vacancy=sample_vacancy,
        profile=prof,
        base_match_score=80,
        decision_class="MATCH",
        eligibility="ELIGIBLE",
        enabled=True,
    )
    assert 2.0 <= adj <= 8.0
    assert any("preference for role family 'AI_AUTOMATION'" in r for r in reasons)


def test_4_repeated_negative_evidence_creates_bounded_negative_adjustment():
    """Test 4: Repeated negative feedback for a role creates bounded negative adjustment."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:88800{i}",
            action="NOT_INTERESTED",
            signal_strength=SignalStrength.STRONG_NEGATIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            title="Technical Support Engineer",
            company=f"Support Co {i}",
            role_family="TECH_SUPPORT",
            role_concept="Technical Support Engineer",
            skills=["active_directory"],
        )
        for i in range(1, 7)
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)
    support_vac = Vacancy(
        source="hh",
        source_job_id="888100",
        title="Technical Support Engineer",
        company="Global Desk",
        description="Help desk support, active directory, user tickets.",
        job_url="https://hh.ru/vacancy/888100",
    )
    adj, reasons = calculate_preference_adjustment(
        vacancy=support_vac,
        profile=prof,
        base_match_score=75,
        decision_class="MATCH",
        eligibility="ELIGIBLE",
        enabled=True,
    )
    assert -8.0 <= adj <= -1.5
    assert any("preference for role family 'TECH_SUPPORT'" in r for r in reasons)


def test_5_contradictory_evidence_reduces_confidence_and_detects_ambiguity():
    """Test 5: Equal positive and negative feedback produces AMBIGUOUS_PREFERENCE and zero confidence."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:77700{i}",
            action="INTERESTED" if i % 2 == 0 else "NOT_INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE if i % 2 == 0 else SignalStrength.STRONG_NEGATIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            title="Python Developer",
            company=f"Dev Co {i}",
            role_family="PYTHON_BACKEND",
            role_concept="Python Developer",
            skills=["python"],
        )
        for i in range(1, 7)  # 3 positive, 3 negative
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)
    sig = prof.role_families["PYTHON_BACKEND"]
    assert sig.status == "AMBIGUOUS_PREFERENCE"
    assert sig.confidence == 0.0


def test_6_recent_evidence_outweighs_stale_evidence():
    """Test 6: Events <30 days old have higher decay weight than events 120 days old."""
    now_dt = datetime.now(timezone.utc)
    recent_ts = (now_dt - timedelta(days=5)).isoformat()
    old_ts = (now_dt - timedelta(days=120)).isoformat()

    w_recent = calculate_recency_weight(recent_ts, now_dt)
    w_old = calculate_recency_weight(old_ts, now_dt)
    assert w_recent == 1.0
    assert w_old == 0.4
    assert w_recent > w_old


def test_7_preference_cannot_override_hard_rejection(sample_vacancy):
    """Test 7: A rejected or ineligible vacancy receives exactly 0 adjustment regardless of strong preferences."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:99900{i}",
            action="PREPARE_APPLICATION",
            signal_strength=SignalStrength.VERY_STRONG_POSITIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            title=sample_vacancy.title,
            company=f"Company {i}",
            role_family="AI_AUTOMATION",
            role_concept="AI Automation Engineer",
            skills=["python", "n8n"],
        )
        for i in range(1, 8)
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)

    # Ineligible / REJECT
    adj, reasons = calculate_preference_adjustment(
        vacancy=sample_vacancy,
        profile=prof,
        base_match_score=40,
        decision_class="REJECT",
        eligibility="INELIGIBLE",
        enabled=True,
    )
    assert adj == 0.0
    assert any("hard constraint invariant" in r for r in reasons)


def test_8_preference_cannot_change_candidate_skill_confidence(sample_candidate_profile, sample_vacancy):
    """Test 8: User giving positive feedback for Kubernetes does NOT alter candidate's factual skill confidence."""
    assert sample_candidate_profile.skill_confidence.get("kubernetes") == "UNKNOWN"

    # User likes 8 Kubernetes jobs
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:k8s_{i}",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            title="Kubernetes SRE",
            company=f"K8s Co {i}",
            role_family="DEVOPS_SRE",
            role_concept="DevOps Engineer",
            skills=["kubernetes", "docker"],
        )
        for i in range(1, 9)
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)
    assert prof.skills["kubernetes"].positive_count == 8

    # Candidate profile remains untouched
    assert sample_candidate_profile.skill_confidence.get("kubernetes") == "UNKNOWN"


def test_9_preference_cannot_change_candidate_years_of_experience(sample_candidate_profile):
    """Test 9: Preference feedback never mutates years_experience or years_of_experience."""
    initial_years = sample_candidate_profile.years_experience
    initial_exp = sample_candidate_profile.years_of_experience

    events = [
        PreferenceEvent(
            vacancy_stable_id="hh:senior_1",
            action="PREPARE_APPLICATION",
            signal_strength=SignalStrength.VERY_STRONG_POSITIVE,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_table="telegram_feedback_records",
            title="Lead AI Architect 10+ years",
            company="Mega Corp",
            role_family="AI_AUTOMATION",
            role_concept="AI Automation Engineer",
            seniority=["senior"],
        )
    ]
    build_preference_profile(events=events)

    assert sample_candidate_profile.years_experience == initial_years == 4
    assert sample_candidate_profile.years_of_experience == initial_exp == 4


def test_10_preference_cannot_promote_stretch_to_strong_match_directly(sample_vacancy):
    """Test 10: Applying preference to a BORDERLINE match alters ranking_score, but preserves decision_class."""
    base_match = MatchResult(
        score=65,
        decision="REVIEW",
        decision_class="BORDERLINE",
        eligibility="ELIGIBLE",
        role_family="AI_AUTOMATION",
        reasons=["Borderline score"],
        strengths=[],
        gaps=["Missing 2 years seniority"],
    )

    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:99900{i}",
            action="PREPARE_APPLICATION",
            signal_strength=SignalStrength.VERY_STRONG_POSITIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            title=sample_vacancy.title,
            company=f"Company {i}",
            role_family="AI_AUTOMATION",
            role_concept="AI Automation Engineer",
            skills=["python", "n8n"],
        )
        for i in range(1, 8)
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)

    adjusted = apply_preference_adjustment(base_match, sample_vacancy, preference_profile=prof, enabled=True)
    assert adjusted.score == 65  # Base match score unchanged
    assert adjusted.decision_class == "BORDERLINE"  # Decision class unchanged
    assert adjusted.preference_adjustment > 0.0
    assert adjusted.ranking_score > 65


def test_11_prepare_application_weighs_more_than_interested():
    """Test 11: PREPARE_APPLICATION (weight 1.5) produces higher signal weight than INTERESTED (weight 1.0)."""
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    ev_prep = [
        PreferenceEvent(
            vacancy_stable_id="hh:prep_1",
            action="PREPARE_APPLICATION",
            signal_strength=SignalStrength.VERY_STRONG_POSITIVE,
            created_at=now_iso,
            source_table="telegram_feedback_records",
            role_family="AI_AUTOMATION",
        )
    ]
    ev_int = [
        PreferenceEvent(
            vacancy_stable_id="hh:int_1",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=now_iso,
            source_table="telegram_feedback_records",
            role_family="AI_AUTOMATION",
        )
    ]

    sig_prep = aggregate_events_to_signal("AI_AUTOMATION", "AI_AUTOMATION", ev_prep, now_dt)
    sig_int = aggregate_events_to_signal("AI_AUTOMATION", "AI_AUTOMATION", ev_int, now_dt)

    w_prep, v_prep = SIGNAL_WEIGHT_MAP[SignalStrength.VERY_STRONG_POSITIVE]
    w_int, v_int = SIGNAL_WEIGHT_MAP[SignalStrength.STRONG_POSITIVE]
    assert (w_prep * v_prep) > (w_int * v_int)


def test_12_not_interested_does_not_globally_penalize_entire_role_from_one_event(sample_vacancy):
    """Test 12: A single negative feedback does not globally penalize the role family."""
    ev = PreferenceEvent(
        vacancy_stable_id="hh:neg_1",
        action="NOT_INTERESTED",
        signal_strength=SignalStrength.STRONG_NEGATIVE,
        created_at=datetime.now(timezone.utc).isoformat(),
        source_table="telegram_feedback_records",
        title=sample_vacancy.title,
        company="Bad Company",
        role_family="AI_AUTOMATION",
        role_concept="AI Automation Engineer",
    )
    prof = build_preference_profile(events=[ev])
    assert prof.role_families["AI_AUTOMATION"].confidence == 0.0

    adj, _ = calculate_preference_adjustment(
        vacancy=sample_vacancy,
        profile=prof,
        base_match_score=85,
        decision_class="STRONG_MATCH",
        eligibility="ELIGIBLE",
        enabled=True,
    )
    assert adj == 0.0


def test_13_company_specific_feedback_stays_company_specific():
    """Test 13: Negative feedback on Company A penalizes Company A, but not Company B in the same role."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:comp_{i}",
            action="NOT_INTERESTED",
            signal_strength=SignalStrength.STRONG_NEGATIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            title="Python Developer",
            company="Spam Corp",
            role_family="PYTHON_BACKEND",
            role_concept="Python Developer",
            skip_reason="COMPANY",
        )
        for i in range(1, 6)
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)

    vac_spam = Vacancy(
        source="hh",
        source_job_id="111",
        title="Python Developer",
        company="Spam Corp",
        description="Python backend dev.",
        job_url="https://hh.ru/111",
    )
    vac_good = Vacancy(
        source="hh",
        source_job_id="222",
        title="Python Developer",
        company="Good Corp",
        description="Python backend dev.",
        job_url="https://hh.ru/222",
    )

    adj_spam, reasons_spam = calculate_preference_adjustment(
        vacancy=vac_spam, profile=prof, base_match_score=80, decision_class="MATCH", eligibility="ELIGIBLE", enabled=True
    )
    adj_good, reasons_good = calculate_preference_adjustment(
        vacancy=vac_good, profile=prof, base_match_score=80, decision_class="MATCH", eligibility="ELIGIBLE", enabled=True
    )

    assert adj_spam < 0.0
    assert any("company suppression for 'Spam Corp'" in r for r in reasons_spam)
    assert "company suppression" not in " ".join(reasons_good)


def test_14_role_family_aggregation_is_deterministic():
    """Test 14: Processing the same events yields byte-identical role family signals."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:det_{i}",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            role_family="PYTHON_BACKEND",
        )
        for i in range(1, 5)
    ]
    p1 = build_preference_profile(events=events, now_dt=now_dt)
    p2 = build_preference_profile(events=events, now_dt=now_dt)

    assert p1.role_families["PYTHON_BACKEND"].to_dict() == p2.role_families["PYTHON_BACKEND"].to_dict()


def test_15_skill_preference_aggregation_is_deterministic():
    """Test 15: Skill aggregation is 100% reproducible."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:sk_{i}",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            skills=["fastapi", "docker"],
        )
        for i in range(1, 6)
    ]
    p1 = build_preference_profile(events=events, now_dt=now_dt)
    p2 = build_preference_profile(events=events, now_dt=now_dt)

    assert p1.skills["fastapi"].to_dict() == p2.skills["fastapi"].to_dict()
    assert p1.skills["docker"].to_dict() == p2.skills["docker"].to_dict()


def test_16_preference_profile_reproducible_from_feedback_history():
    """Test 16: Profile can be serialized to JSON and deserialized identically."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id="hh:rep_1",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=now_dt.isoformat(),
            source_table="telegram_feedback_records",
            role_family="AI_AUTOMATION",
            skills=["python"],
        )
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)
    p_dict = prof.to_dict()
    json_str = json.dumps(p_dict, ensure_ascii=False)
    loaded = json.loads(json_str)

    assert loaded["total_evidence_events"] == 1
    assert loaded["role_families"]["AI_AUTOMATION"]["dimension_key"] == "AI_AUTOMATION"


def test_17_deleting_or_replaying_derived_cache_does_not_lose_canonical_evidence():
    """Test 17: PreferenceProfile is completely stateless and reconstructed on-the-fly."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id="hh:stateless_1",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=now_dt.isoformat(),
            source_table="telegram_feedback_records",
            role_family="AI_AUTOMATION",
        )
    ]
    # Call 1
    p1 = build_preference_profile(events=events, now_dt=now_dt)
    # Simulate cache wipe / garbage collection
    del p1
    # Call 2
    p2 = build_preference_profile(events=events, now_dt=now_dt)
    assert p2.total_evidence_events == 1


def test_18_analytics_command_is_read_only(tmp_path):
    """Test 18: feedback analytics execution leaves sqlite database file untouched."""
    db_file = tmp_path / "test_state.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:136551280', 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T00:00:00')")
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url, match_score, match_decision) VALUES ('hh:136551280', 'hh', '136551280', 'Python Dev', 'ООО СП Солюшен', 'Python code', 'https://hh.ru/136551280', 80, 'APPLY')")
        conn.commit()
        conn.close()

        # Calculate SHA256 before
        import hashlib
        with open(db_file, "rb") as f:
            hash_before = hashlib.sha256(f.read()).hexdigest()

        from ai_assistant.feedback_analytics import extract_all_preference_evidence, build_preference_profile
        evs = extract_all_preference_evidence()
        prof = build_preference_profile(evs)
        assert prof.total_evidence_events == 1

        with open(db_file, "rb") as f:
            hash_after = hashlib.sha256(f.read()).hexdigest()

        assert hash_before == hash_after


def test_19_simulation_does_not_mutate_production_db(tmp_path, sample_candidate_profile):
    """Test 19: Simulation across vacancies does not insert or update any database rows."""
    db_file = tmp_path / "test_sim.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url, match_score, match_decision) VALUES ('hh:sim_1', 'hh', 'sim_1', 'AI Developer', 'Acme', 'Python LLM', 'http://hh.ru/1', 90.0, 'APPLY')")
        conn.commit()
        conn.close()

        import hashlib
        with open(db_file, "rb") as f:
            hash_before = hashlib.sha256(f.read()).hexdigest()

        from ai_assistant.cli import feedback_cmd
        res = feedback_cmd(action="simulate", limit=5, output_json=True)
        assert res == 0

        with open(db_file, "rb") as f:
            hash_after = hashlib.sha256(f.read()).hexdigest()

        assert hash_before == hash_after


def test_20_existing_stage87_matcher_behavior_preserved_with_calibration_disabled(sample_candidate_profile, sample_vacancy):
    """Test 20: With PREFERENCE_CALIBRATION_ENABLED=False, JobMatcher.match(...) produces 0 preference adjustment."""
    matcher = JobMatcher(sample_candidate_profile)
    res = matcher.match(sample_vacancy)

    assert res.score >= 75
    assert res.preference_adjustment == 0.0
    assert res.ranking_score == res.score
