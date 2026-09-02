"""Stage 91: Human Feedback Collection Quality & Calibration Readiness Test Suite.

Verifies:
1. INTERESTED remains explicit human evidence.
2. PREPARE_APPLICATION has higher signal strength (1.5) than INTERESTED (1.0).
3. SKIP (0.5, -0.3) is weaker than NOT_INTERESTED (1.0, -0.8).
4. Optional reason is not required (plain click is valid ground truth).
5. Follow-up reason callback does not create a second independent evidence event.
6. Feedback reason survives persistence and is readable in PreferenceProfile.
7. Newer feedback supersedes effective old state (temporal supersession).
8. Audit history in telegram_feedback_records remains preserved across edits.
9. INTERESTED -> PREPARE progression does not double sample count.
10. NOT_INTERESTED -> INTERESTED reversal resolves deterministically to the latest state.
11. Reason COMPANY stays company-specific and does not penalize role family.
12. Reason SALARY does not penalize role family.
13. Reason TECH_STACK does not modify factual candidate profile skills.
14. No-feedback digest vacancy produces zero preference evidence (no feedback != dislike).
15. Feedback coverage metrics are deterministic and accurate.
16. Dimension readiness is evaluated on independent events per dimension.
17. Company readiness stays RECORD_ONLY even if global feedback count is high.
18. Calibration fixtures (vacancies_json:*) are strictly excluded.
19. Validation callbacks (live_val_*) are strictly excluded.
20. Production feature flag stays disabled (PREFERENCE_CALIBRATION_ENABLED=False).
"""

import json
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import load_candidate_profile
from ai_assistant.telegram_feedback import (
    FeedbackReason,
    TelegramFeedbackAction,
    TelegramFeedbackProcessor,
    encode_callback_data,
    decode_callback_data,
)
from ai_assistant.feedback_analytics import (
    EvidenceProvenance,
    SignalStrength,
    PreferenceEvent,
    PreferenceEvidenceEvent,
    build_preference_profile,
    calculate_preference_adjustment,
    extract_all_preference_evidence,
    get_feedback_coverage_metrics,
)


@pytest.fixture
def prod_vacancy() -> Vacancy:
    return Vacancy(
        source="hh",
        source_job_id="136551280",
        title="AI-разработчик (Python) Junior / Middle",
        company="ООО СП Солюшен",
        description="Python AI разработчик LLM n8n",
        job_url="https://hh.ru/vacancy/136551280",
        location="Remote",
    )


def test_1_interested_remains_explicit_human_evidence(tmp_path):
    """Test 1: Plain INTERESTED button tap creates valid EXPLICIT_HUMAN_FEEDBACK evidence."""
    db_file = tmp_path / "test_int.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 1
        assert evs[0].action == "INTERESTED"
        assert evs[0].signal_strength == SignalStrength.STRONG_POSITIVE
        assert evs[0].provenance == EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK


def test_2_prepare_application_is_stronger_than_interested():
    """Test 2: PREPARE_APPLICATION has weight 1.5 and raw value +1.0 vs INTERESTED (1.0, +0.8)."""
    from ai_assistant.feedback_analytics import SIGNAL_WEIGHT_MAP
    w_prep, val_prep = SIGNAL_WEIGHT_MAP[SignalStrength.VERY_STRONG_POSITIVE]
    w_int, val_int = SIGNAL_WEIGHT_MAP[SignalStrength.STRONG_POSITIVE]
    assert w_prep > w_int
    assert val_prep > val_int


def test_3_skip_weaker_than_not_interested():
    """Test 3: Plain SKIP is contextual/weak (-0.3, weight 0.5) vs NOT_INTERESTED (-0.8, weight 1.0)."""
    from ai_assistant.feedback_analytics import SIGNAL_WEIGHT_MAP
    w_skip, val_skip = SIGNAL_WEIGHT_MAP[SignalStrength.WEAK_NEGATIVE]
    w_not, val_not = SIGNAL_WEIGHT_MAP[SignalStrength.STRONG_NEGATIVE]
    assert abs(val_skip) < abs(val_not)
    assert w_skip < w_not


def test_4_optional_reason_not_required():
    """Test 4: Callback data and decoding function without optional reason."""
    cb = encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, "hh:136551280")
    # Supports seamless 2-tuple unpacking
    act, sid = decode_callback_data(cb)
    assert act == TelegramFeedbackAction.NOT_INTERESTED
    assert sid == "hh:136551280"
    assert decode_callback_data(cb).reason is None


def test_5_reason_does_not_create_second_independent_event(tmp_path):
    """Test 5: A tap followed by a reason click produces 1 deduplicated PreferenceEvidenceEvent."""
    db_file = tmp_path / "test_rsn_dedup.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        # Tap 1: plain click
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '10001', '2026-09-01T12:00:00', '{}')", (sid,))
        # Tap 2: follow-up reason
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '10002', '2026-09-01T12:00:05', '{\"feedback_reason\": \"TECH_STACK\"}')", (sid,))
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 1
        assert evs[0].feedback_reason == "TECH_STACK"


def test_6_feedback_reason_survives_persistence(tmp_path):
    """Test 6: Structured reason is aggregated into PreferenceProfile.feedback_reasons."""
    db_file = tmp_path / "test_rsn_persist.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '10001', '2026-09-01T12:00:00', '{\"feedback_reason\": \"SALARY\"}')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert prof.feedback_reasons.get("SALARY") == 1


def test_7_newer_feedback_supersedes_effective_old_state(tmp_path):
    """Test 7: Changing mind from INTERESTED to NOT_INTERESTED sets effective state to NOT_INTERESTED."""
    db_file = tmp_path / "test_supersede.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        # Day 1: Interested
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '10001', '2026-09-01T12:00:00')", (sid,))
        # Day 3: Changed mind to NOT_INTERESTED
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'NOT_INTERESTED', '392046103', '10002', '2026-09-03T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 1
        assert evs[0].action == "NOT_INTERESTED"
        assert evs[0].signal_strength == SignalStrength.STRONG_NEGATIVE


def test_8_audit_history_remains_preserved(tmp_path):
    """Test 8: Database table telegram_feedback_records preserves all historical rows."""
    db_file = tmp_path / "test_audit_hist.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '10001', '2026-09-01T12:00:00')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'NOT_INTERESTED', '392046103', '10002', '2026-09-03T12:00:00')", (sid,))
        conn.commit()

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM telegram_feedback_records WHERE vacancy_stable_id = ?", (sid,))
        count = cur.fetchone()[0]
        conn.close()

        assert count == 2  # Audit history fully preserved in DB


def test_9_interested_to_prepare_does_not_double_sample_count(tmp_path):
    """Test 9: Progression from INTERESTED to PREPARE_APPLICATION counts as 1 opportunity with upgraded strength."""
    db_file = tmp_path / "test_progression.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '10001', '2026-09-01T12:00:00')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'PREPARE_APPLICATION', '392046103', '10002', '2026-09-01T12:05:00')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert prof.total_evidence_events == 1
        assert prof.evidence_events[0].signal_strength == SignalStrength.VERY_STRONG_POSITIVE


def test_10_not_interested_to_interested_resolves_deterministically(tmp_path):
    """Test 10: Reversal from NOT_INTERESTED to INTERESTED cleanly updates effective state."""
    db_file = tmp_path / "test_reversal.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'NOT_INTERESTED', '392046103', '10001', '2026-09-01T12:00:00')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '10002', '2026-09-01T12:10:00')", (sid,))
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 1
        assert evs[0].action == "INTERESTED"


def test_11_reason_company_stays_company_specific(tmp_path):
    """Test 11: Dislike with reason COMPANY penalizes only company, not role family or skills."""
    db_file = tmp_path / "test_co_only.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'Python Developer', 'ООО СП Солюшен', 'Python code', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '10001', '2026-09-01T12:00:00', '{\"feedback_reason\": \"COMPANY\"}')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert "ООО СП Солюшен" in prof.companies
        assert prof.companies["ООО СП Солюшен"].raw_signal < 0
        # Role family is NOT penalized
        assert "PYTHON_BACKEND" not in prof.role_families


def test_12_reason_salary_does_not_penalize_role_family(tmp_path):
    """Test 12: Dislike with reason SALARY does not penalize role family."""
    db_file = tmp_path / "test_sal_only.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI-разработчик', 'ООО СП Солюшен', 'Python AI', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '10001', '2026-09-01T12:00:00', '{\"feedback_reason\": \"SALARY\"}')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert "AI_AUTOMATION" not in prof.role_families
        assert "PYTHON_BACKEND" not in prof.role_families


def test_13_reason_tech_stack_does_not_modify_candidate_skill_truth(tmp_path):
    """Test 13: Feedback on technology stacks never modifies candidate_profile.json."""
    prof_before = load_candidate_profile()
    skills_before = list(prof_before.skills)
    conf_before = dict(prof_before.skill_confidence)

    db_file = tmp_path / "test_tech_immut.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'Python Dev', 'ООО СП Солюшен', 'Python code', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '10001', '2026-09-01T12:00:00', '{\"feedback_reason\": \"TECH_STACK\"}')", (sid,))
        conn.commit()
        conn.close()

        build_preference_profile()

    prof_after = load_candidate_profile()
    assert prof_before.skills == prof_after.skills
    assert prof_before.skill_confidence == prof_after.skill_confidence


def test_14_no_feedback_vacancy_produces_no_preference_evidence(tmp_path):
    """Test 14: Digest-delivered vacancy without human feedback yields no preference evidence."""
    db_file = tmp_path / "test_no_fb.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:136551280', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 0


def test_15_feedback_coverage_metrics_deterministic(tmp_path):
    """Test 15: get_feedback_coverage_metrics computes accurate rates without error."""
    db_file = tmp_path / "test_cov.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        # 2 delivered vacancies
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:1', 'hh', '1', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/1')")
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:2', 'hh', '2', 'Python Dev', 'Maxima.tech', 'Python', 'https://hh.ru/2')")
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:1', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:2', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        # 1 feedback
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:1', 'INTERESTED', '392046103', '10001', '2026-09-01T12:00:00')")
        conn.commit()
        conn.close()

        m = get_feedback_coverage_metrics()
        assert m["delivered_vacancies"] == 2
        assert m["feedback_received"] == 1
        assert m["coverage_rate"] == 0.5
        assert m["explicit_positive"] == 1
        assert m["no_feedback"] == 1


def test_16_dimension_readiness_uses_independent_events(tmp_path):
    """Test 16: Dimension readiness reports COLLECTING when dimension events are below threshold."""
    db_file = tmp_path / "test_dim_readiness.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI-разработчик (Python)', 'ООО СП Солюшен', 'Python AI', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '10001', '2026-09-01T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert prof.dimension_readiness.get("role_family:PYTHON_BACKEND") == "NO_EVIDENCE" or prof.dimension_readiness.get("role_family:PYTHON_BACKEND") == "COLLECTING"


def test_17_company_readiness_does_not_inherit_global_event_count():
    """Test 17: A single company event remains RECORD_ONLY and not CALIBRATION_ELIGIBLE."""
    now_dt = datetime.now(timezone.utc)
    events = [
        PreferenceEvent(
            vacancy_stable_id=f"hh:co_{i}",
            action="INTERESTED",
            signal_strength=SignalStrength.STRONG_POSITIVE,
            created_at=(now_dt - timedelta(days=i)).isoformat(),
            source_table="telegram_feedback_records",
            company="RareCo" if i == 1 else f"OtherCo_{i}",
            role_family="AI_AUTOMATION",
        )
        for i in range(1, 8)
    ]
    prof = build_preference_profile(events=events, now_dt=now_dt)
    assert prof.companies["RareCo"].status == "RECORD_ONLY"
    assert prof.companies["RareCo"].readiness == "NO_EVIDENCE"


def test_18_calibration_fixtures_excluded(tmp_path):
    """Test 18: Calibration fixtures from vacancies_json:* are excluded from coverage and preference."""
    db_file = tmp_path / "test_fixture_excl.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('vacancies_json:24', 'vacancies_json', '24', 'AI Dev', 'DeepSense', 'n8n', 'http://example.com/24')")
        conn.execute("INSERT INTO application_reviews (vacancy_stable_id, status, updated_at) VALUES ('vacancies_json:24', 'APPROVED', '2026-08-22T15:00:00')")
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 0


def test_19_validation_callbacks_excluded(tmp_path):
    """Test 19: Synthetic validation callbacks like live_val_* are excluded."""
    db_file = tmp_path / "test_live_val_excl.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:1', 'hh', '1', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/1')")
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:1', 'INTERESTED', '392046103', 'live_val_123', '2026-09-01T12:00:00')")
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 0


def test_20_production_feature_flag_stays_disabled():
    """Test 20: PREFERENCE_CALIBRATION_ENABLED remains False."""
    assert config.PREFERENCE_CALIBRATION_ENABLED is False
