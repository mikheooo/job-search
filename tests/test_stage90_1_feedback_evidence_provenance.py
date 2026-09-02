"""Stage 90.1: Feedback Evidence Provenance & Label Quality Audit Test Suite.

Verifies:
1. One Telegram PREPARE action does not count as four signals (deduplicated by correlation key).
2. ANALYZED alone is not preference evidence.
3. Auto-created review without human confirmation is not human preference.
4. Explicit human review (with confirmed note) is valid evidence.
5. Confirmed real application is valid positive evidence.
6. Employer rejection is not candidate negative preference.
7. TEST/synthetic callback is excluded (live_val_*, test_*).
8. Processor-level validation callback is excluded from production ground truth.
9. Real Telegram owner callback on genuine production vacancy is included.
10. Calibration vacancy feedback (vacancies_json:*) is excluded from production learning.
11. Dry-run vacancy evidence (/dryrun, dryrun_*) is excluded.
12. Same human action correlation is deterministic.
13. Downstream tracking rows do not double count.
14. Unknown provenance fails closed (is_production_eligible=False).
15. Evidence threshold uses independent human events, not database row count.
16. Role-family confidence recalculates honestly after deduplication.
17. Company signals require genuine production vacancy.
18. Preference profile remains deterministic and reproducible.
19. Analytics and provenance inspection are strictly read-only.
20. Production feature flag stays disabled (PREFERENCE_CALIBRATION_ENABLED=False).
"""

import json
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.feedback_analytics import (
    EvidenceProvenance,
    SignalStrength,
    PreferenceEvidenceEvent,
    build_preference_profile,
    calculate_preference_adjustment,
    extract_all_preference_evidence,
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


def test_1_one_telegram_prepare_does_not_count_as_multiple_signals(tmp_path):
    """Test 1: One Telegram PREPARE action creating rows in tracking, review, and feedback counts as 1 event."""
    db_file = tmp_path / "test_dedup.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        # Telegram feedback row
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'PREPARE_APPLICATION', '392046103', '1683825194773039570', '2026-09-01T12:00:00')", (sid,))
        # Downstream review row
        conn.execute("INSERT INTO application_reviews (vacancy_stable_id, status, note, updated_at) VALUES (?, 'APPROVED', 'Approved via Telegram', '2026-09-01T12:00:01')", (sid,))
        # Downstream tracking row
        conn.execute("INSERT INTO application_tracking (vacancy_stable_id, status, updated_at) VALUES (?, 'READY_TO_APPLY', '2026-09-01T12:00:02')", (sid,))
        conn.commit()
        conn.close()

        eligible_events = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible_events) == 1
        assert eligible_events[0].vacancy_stable_id == sid
        assert eligible_events[0].provenance == EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK


def test_2_analyzed_status_alone_is_not_preference_evidence(tmp_path):
    """Test 2: Status ANALYZED in application_tracking is an operational state, not preference."""
    db_file = tmp_path / "test_analyzed.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO application_tracking (vacancy_stable_id, status, updated_at) VALUES (?, 'ANALYZED', '2026-09-01T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 0


def test_3_auto_created_review_is_not_human_preference(tmp_path):
    """Test 3: An unverified or auto-generated review entry is excluded from ground truth."""
    db_file = tmp_path / "test_auto_rev.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO application_reviews (vacancy_stable_id, status, review_version, note, updated_at) VALUES (?, 'APPROVED', 'v1', NULL, '2026-09-01T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        raw = extract_all_preference_evidence(include_non_production=True)
        assert len(raw) == 1
        assert raw[0].provenance == EvidenceProvenance.AUTOMATED_PIPELINE_STATE
        assert raw[0].is_production_eligible is False

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 0


def test_4_explicit_human_review_is_valid_evidence(tmp_path):
    """Test 4: Review with explicit human confirmation note is valid evidence."""
    db_file = tmp_path / "test_human_rev.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO application_reviews (vacancy_stable_id, status, review_version, note, updated_at) VALUES (?, 'APPROVED', 'v1', 'Approved via Web UI', '2026-09-01T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 1
        assert eligible[0].provenance == EvidenceProvenance.EXPLICIT_HUMAN_APPLICATION_INTENT
        assert eligible[0].is_production_eligible is True


def test_5_confirmed_real_application_is_valid_positive_evidence(tmp_path):
    """Test 5: Confirmed external submission note in hh_applications is valid positive evidence."""
    db_file = tmp_path / "test_confirmed_app.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136704137"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136704137', 'Python Dev', 'Maxima.tech', 'Python', 'https://hh.ru/136704137')", (sid,))
        conn.execute("INSERT INTO hh_applications (application_id, vacancy_stable_id, state, last_transition_reason, created_at, updated_at) VALUES ('app_1', ?, 'SUBMITTED', 'controlled_runner_submit_confirmed', '2026-08-30T08:00:00', '2026-08-30T08:30:00')", (sid,))
        conn.commit()
        conn.close()

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 1
        assert eligible[0].provenance == EvidenceProvenance.CONFIRMED_REAL_APPLICATION
        assert eligible[0].signal_strength == SignalStrength.VERY_STRONG_POSITIVE


def test_6_employer_rejection_is_not_candidate_negative_preference(tmp_path):
    """Test 6: Inbound employer rejection in chat is not candidate negative preference."""
    db_file = tmp_path / "test_recruiter_msg.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136611193"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136611193', 'AI Pipeline', 'Волчкова Анастасия Александровна', 'Python', 'https://hh.ru/136611193')", (sid,))
        conn.execute("INSERT INTO hh_applications (application_id, vacancy_stable_id, state, last_transition_reason, created_at, updated_at) VALUES ('app_2', ?, 'ANALYZED', 'message_classified_as_NO_REPLY_NEEDED', '2026-08-30T04:00:00', '2026-08-30T04:30:00')", (sid,))
        conn.commit()
        conn.close()

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 0


def test_7_test_callback_excluded(tmp_path):
    """Test 7: Synthetic callback IDs like live_val_* or test_* are strictly excluded."""
    db_file = tmp_path / "test_synth_cb.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "weworkremotely:ignition-inc"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'weworkremotely', 'ignition-inc', 'Help Desk', 'Ignition Inc', 'Support', 'https://weworkremotely.com/jobs/1')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', 'live_val_stage89_1_001', '2026-09-01T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        raw = extract_all_preference_evidence(include_non_production=True)
        assert len(raw) == 1
        assert raw[0].provenance == EvidenceProvenance.TEST_OR_DRY_RUN
        assert raw[0].is_production_eligible is False

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 0


def test_8_processor_level_validation_callback_excluded(tmp_path):
    """Test 8: Processor-level test callback payload does not enter production preference profile."""
    db_file = tmp_path / "test_proc_cb.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:1', 'hh', '1', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/1')")
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:1', 'INTERESTED', '392046103', 'mock_callback_123', '2026-09-01T12:00:00')")
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert prof.production_eligible_events_count == 0
        assert prof.calibration_readiness == "NO_EVIDENCE"


def test_9_real_telegram_owner_callback_included(tmp_path):
    """Test 9: Real Telegram owner callback query from Telegram API (numeric ID) is included."""
    db_file = tmp_path / "test_real_tg.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI-разработчик (Python)', 'ООО СП Солюшен', 'Python AI', 'https://hh.ru/vacancy/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        conn.commit()
        conn.close()

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 1
        assert eligible[0].provenance == EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK
        assert eligible[0].is_production_eligible is True


def test_10_calibration_vacancy_feedback_excluded_from_production_learning(tmp_path):
    """Test 10: Feedback or reviews on vacancies_json:* calibration fixtures are strictly excluded."""
    db_file = tmp_path / "test_calib_excl.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('vacancies_json:24', 'vacancies_json', '24', 'Automation Eng', 'DeepSense', 'n8n', 'https://example.com/24')")
        conn.execute("INSERT INTO application_reviews (vacancy_stable_id, status, note, updated_at) VALUES ('vacancies_json:24', 'APPROVED', 'Approved via Web UI', '2026-08-22T15:12:26')")
        conn.commit()
        conn.close()

        raw = extract_all_preference_evidence(include_non_production=True)
        assert len(raw) == 1
        assert raw[0].provenance == EvidenceProvenance.TEST_OR_DRY_RUN

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 0


def test_11_dry_run_vacancy_evidence_excluded(tmp_path):
    """Test 11: Dry-run vacancies (e.g. /dryrun URL, dryrun source job ID) are excluded."""
    db_file = tmp_path / "test_dryrun_excl.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:dryrun_123', 'hh', 'dryrun_123', 'Dry Run Job', 'ООО СП Солюшен', 'Python', 'https://hh.ru/dryrun/123')")
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:dryrun_123', 'INTERESTED', '392046103', '999888777', '2026-09-01T12:00:00')")
        conn.commit()
        conn.close()

        raw = extract_all_preference_evidence(include_non_production=True)
        assert raw[0].is_production_eligible is False

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 0


def test_12_same_human_action_correlation_is_deterministic(tmp_path):
    """Test 12: Calling extract_all_preference_evidence multiple times produces identical deduplicated event keys."""
    db_file = tmp_path / "test_det_corr.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI-разработчик', 'ООО СП Солюшен', 'Python AI', 'https://hh.ru/vacancy/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        conn.commit()
        conn.close()

        ev1 = extract_all_preference_evidence()
        ev2 = extract_all_preference_evidence()
        assert [e.to_dict() for e in ev1] == [e.to_dict() for e in ev2]


def test_13_downstream_tracking_rows_do_not_double_count(tmp_path):
    """Test 13: Multiple downstream status updates do not inflate preference vote counts."""
    db_file = tmp_path / "test_no_double_count.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        conn.execute("INSERT INTO application_reviews (vacancy_stable_id, status, updated_at) VALUES (?, 'APPROVED', '2026-09-01T16:27:53')", (sid,))
        conn.execute("INSERT INTO application_tracking (vacancy_stable_id, status, updated_at) VALUES (?, 'ANALYZED', '2026-09-01T16:27:54')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert prof.production_eligible_events_count == 1
        assert prof.raw_evidence_count == 2


def test_14_unknown_provenance_fails_closed(tmp_path):
    """Test 14: Events with unknown provenance are marked is_production_eligible=False."""
    db_file = tmp_path / "test_unauth.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        # User 99999999 is unauthorized
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '99999999', '1234567890', '2026-09-01T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        raw = extract_all_preference_evidence(include_non_production=True)
        assert raw[0].provenance == EvidenceProvenance.UNKNOWN
        assert raw[0].is_production_eligible is False

        eligible = extract_all_preference_evidence(include_non_production=False)
        assert len(eligible) == 0


def test_15_evidence_threshold_uses_independent_human_events(tmp_path):
    """Test 15: Minimum evidence threshold evaluates independent events, not raw row count."""
    db_file = tmp_path / "test_threshold.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        # 1 real event
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        # 4 fake/synthetic callback rows
        for i in range(1, 5):
            conn.execute(f"INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('weworkremotely:fake_{i}', 'INTERESTED', '392046103', 'live_val_{i}', '2026-09-01T12:00:00')")
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert prof.raw_evidence_count == 5
        assert prof.production_eligible_events_count == 1
        assert prof.calibration_readiness == "COLLECTING"
        assert prof.calibration_status == "INSUFFICIENT_EVIDENCE_FOR_AUTOMATIC_CALIBRATION"


def test_16_role_family_confidence_recalculates_after_deduplication(tmp_path):
    """Test 16: Role family confidence is based strictly on validated production ground truth."""
    db_file = tmp_path / "test_rf_conf.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI-разработчик (Python)', 'ООО СП Солюшен', 'Python LLM', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        # 1 event yields RECORD_ONLY / 0 confidence
        assert prof.role_families["PYTHON_BACKEND"].status == "RECORD_ONLY"
        assert prof.role_families["PYTHON_BACKEND"].confidence == 0.0


def test_17_company_signals_require_genuine_production_vacancy(tmp_path):
    """Test 17: Company signals ignore synthetic/fixture companies (e.g. WebLife from vacancies_json)."""
    db_file = tmp_path / "test_comp_signal.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        # Fixture company
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('vacancies_json:42', 'vacancies_json', '42', 'AI Dev', 'WebLife', 'Python', 'https://example.com/42')")
        conn.execute("INSERT INTO application_reviews (vacancy_stable_id, status, note, updated_at) VALUES ('vacancies_json:42', 'REJECTED', 'Not a fit', '2026-08-22T15:14:37')")
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert "WebLife" not in prof.companies


def test_18_preference_profile_remains_deterministic(tmp_path):
    """Test 18: PreferenceProfile serialization and structure are deterministic."""
    db_file = tmp_path / "test_prof_det.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        conn.commit()
        conn.close()

        now_dt = datetime.now(timezone.utc)
        p1 = build_preference_profile(now_dt=now_dt)
        p2 = build_preference_profile(now_dt=now_dt)
        assert p1.to_dict() == p2.to_dict()


def test_19_analytics_and_provenance_are_strictly_read_only(tmp_path):
    """Test 19: Provenance audit CLI command does not mutate sqlite database file."""
    db_file = tmp_path / "test_read_only.db"

    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1683825194773039570', '2026-09-01T16:27:52')", (sid,))
        conn.commit()
        conn.close()

        import hashlib
        with open(db_file, "rb") as f:
            hash_before = hashlib.sha256(f.read()).hexdigest()

        from ai_assistant.cli import feedback_cmd
        res = feedback_cmd(action="provenance", output_json=True)
        assert res == 0

        with open(db_file, "rb") as f:
            hash_after = hashlib.sha256(f.read()).hexdigest()

        assert hash_before == hash_after


def test_20_production_feature_flag_stays_disabled():
    """Test 20: PREFERENCE_CALIBRATION_ENABLED default is False."""
    assert config.PREFERENCE_CALIBRATION_ENABLED is False
