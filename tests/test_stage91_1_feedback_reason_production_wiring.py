"""Stage 91.1: Telegram Feedback Reason UX & Coverage Semantics Production Wiring Test Suite.

Verifies:
1. Stage 91 reason callback is routed by deployed Hermes integration (fb: prefix match).
2. Base feedback action remains valid and immediately persisted without selecting a reason.
3. Structured reason enriches the single canonical event rather than creating a new one.
4. Reason callback does not increment the independent human evidence event count.
5. Raw audit history in telegram_feedback_records is preserved append-only.
6. Temporal supersession preserves history but deterministically changes effective preference.
7. Telegram feedback coverage accurately isolates Telegram taps from external applications.
8. Human evidence coverage includes confirmed external applications.
9. Multi-step progression (INTERESTED -> PREPARE -> SUBMITTED) counts as 1 vacancy in coverage.
10. All action + reason callback combinations respect Telegram's 64-byte limit.
11. Long stable IDs in reason callbacks resolve safely via surrogate 16-char hash.
12. Hash collision or zero match fails closed safely.
13. COMPANY reason remains company-specific and does not penalize role family.
14. SALARY reason does not penalize role family or technical skills.
15. TECH_STACK reason does not mutate factual candidate qualifications in candidate_profile.json.
16. Non-genuine/legacy vacancies (vacancies_json:*) cannot receive reason feedback.
17. Unauthorized user ID is strictly rejected (fail-closed).
18. Production calibration flag remains disabled (PREFERENCE_CALIBRATION_ENABLED=False).
19. DecodedCallback provides backward-compatible 2-tuple unpacking and .reason attribute.
20. Structured reason acknowledgment texts are generated accurately.
"""

import json
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import load_candidate_profile
from ai_assistant.telegram_feedback import (
    FeedbackReason,
    TelegramFeedbackAction,
    TelegramFeedbackProcessor,
    DecodedCallback,
    encode_callback_data,
    decode_callback_data,
    build_reason_inline_keyboard,
    ACTION_CODE_MAP,
    REASON_CODE_MAP,
)
from ai_assistant.feedback_analytics import (
    EvidenceProvenance,
    SignalStrength,
    PreferenceEvidenceEvent,
    build_preference_profile,
    extract_all_preference_evidence,
    get_feedback_coverage_metrics,
)
from integrations.hermes.telegram_adapter_hook import HERMES_CALLBACK_HOOK_CODE


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


# ---------------------------------------------------------------------------
# 1. Hermes Adapter Routing
# ---------------------------------------------------------------------------
def test_1_hermes_adapter_routes_reason_callbacks():
    """Test 1: Deployed Hermes adapter hook checks data.startswith('fb:') which captures reason callbacks."""
    assert 'if data.startswith("fb:"):' in HERMES_CALLBACK_HOOK_CODE
    cb_reason = encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, "hh:136551280", FeedbackReason.TECH_STACK)
    assert cb_reason.startswith("fb:")


# ---------------------------------------------------------------------------
# 2. Base Action Valid Immediately
# ---------------------------------------------------------------------------
def test_2_base_action_persisted_without_reason(tmp_path):
    """Test 2: Tapping NOT_INTERESTED immediately mutates review and tracking state without requiring a reason."""
    db_file = tmp_path / "test_base_action.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:136551280', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.commit()
        conn.close()

        processor = TelegramFeedbackProcessor(allowed_user_id="392046103", require_delivered=True)
        cb_query = {
            "id": "1001",
            "from": {"id": 392046103},
            "message": {"chat": {"id": -1004399255305}},
            "data": encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, sid),
        }
        res = processor.process_callback_query(cb_query)
        assert res["success"] is True
        assert res["new_status"] == "REJECTED"
        assert res["feedback_reason"] is None


# ---------------------------------------------------------------------------
# 3. Reason Enriches Same Canonical Event
# ---------------------------------------------------------------------------
def test_3_reason_enriches_single_canonical_event(tmp_path):
    """Test 3: Initial tap followed by structured reason click produces 1 enriched event."""
    db_file = tmp_path / "test_enrich.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:136551280', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.commit()
        conn.close()

        processor = TelegramFeedbackProcessor(allowed_user_id="392046103", require_delivered=True)
        # Tap 1: Plain NOT_INTERESTED
        processor.process_callback_query({
            "id": "1001",
            "from": {"id": 392046103},
            "message": {"chat": {"id": -1004399255305}},
            "data": encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, sid),
        })
        # Tap 2: Follow-up reason TECH_STACK
        processor.process_callback_query({
            "id": "1002",
            "from": {"id": 392046103},
            "message": {"chat": {"id": -1004399255305}},
            "data": encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, sid, FeedbackReason.TECH_STACK),
        })

        evs = extract_all_preference_evidence()
        assert len(evs) == 1
        assert evs[0].action == "NOT_INTERESTED"
        assert evs[0].feedback_reason == "TECH_STACK"


# ---------------------------------------------------------------------------
# 4. Reason Does Not Increment Independent Evidence Count
# ---------------------------------------------------------------------------
def test_4_reason_does_not_increment_event_count(tmp_path):
    """Test 4: Follow-up reason does not artificially increment total_evidence_events."""
    db_file = tmp_path / "test_count_no_inc.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '1001', '2026-09-01T12:00:00', '{}')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '1002', '2026-09-01T12:00:05', '{\"feedback_reason\": \"SALARY\"}')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert prof.total_evidence_events == 1


# ---------------------------------------------------------------------------
# 5. Raw Audit History Preserved
# ---------------------------------------------------------------------------
def test_5_raw_audit_history_preserved(tmp_path):
    """Test 5: Both the initial callback and follow-up reason callback exist in telegram_feedback_records."""
    db_file = tmp_path / "test_audit_persist.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:136551280', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.commit()
        conn.close()

        processor = TelegramFeedbackProcessor(allowed_user_id="392046103", require_delivered=True)
        processor.process_callback_query({
            "id": "1001",
            "from": {"id": 392046103},
            "message": {"chat": {"id": -1004399255305}},
            "data": encode_callback_data(TelegramFeedbackAction.SKIP, sid),
        })
        processor.process_callback_query({
            "id": "1002",
            "from": {"id": 392046103},
            "message": {"chat": {"id": -1004399255305}},
            "data": encode_callback_data(TelegramFeedbackAction.SKIP, sid, FeedbackReason.LOCATION),
        })

        records = db.list_telegram_feedback(limit=10)
        assert len(records) == 2
        cb_ids = [r["callback_query_id"] for r in records]
        assert "1001" in cb_ids and "1002" in cb_ids


# ---------------------------------------------------------------------------
# 6. Temporal Supersession
# ---------------------------------------------------------------------------
def test_6_temporal_supersession_reversal(tmp_path):
    """Test 6: User changes mind from SKIP to INTERESTED; effective state becomes INTERESTED."""
    db_file = tmp_path / "test_super_reversal.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'SKIP', '392046103', '1001', '2026-09-01T12:00:00')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES (?, 'INTERESTED', '392046103', '1002', '2026-09-02T12:00:00')", (sid,))
        conn.commit()
        conn.close()

        evs = extract_all_preference_evidence()
        assert len(evs) == 1
        assert evs[0].action == "INTERESTED"
        assert evs[0].signal_strength == SignalStrength.STRONG_POSITIVE


# ---------------------------------------------------------------------------
# 7. Telegram Feedback Coverage
# ---------------------------------------------------------------------------
def test_7_telegram_feedback_coverage_isolates_taps(tmp_path):
    """Test 7: Telegram coverage rate is computed strictly on Telegram feedback events."""
    db_file = tmp_path / "test_cov_tg.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:1', 'hh', '1', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/1')")
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:2', 'hh', '2', 'Python Dev', 'Maxima.tech', 'Python', 'https://hh.ru/2')")
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:1', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:2', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        # 1 Telegram tap on hh:1
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:1', 'INTERESTED', '392046103', '1001', '2026-09-01T12:00:00')")
        # 1 Confirmed application on hh:2 (not Telegram)
        conn.execute("INSERT INTO hh_applications (application_id, vacancy_stable_id, state, last_transition_reason, created_at, updated_at) VALUES (1, 'hh:2', 'SUBMITTED', 'controlled_runner_submit_confirmed', '2026-09-01T12:00:00', '2026-09-01T12:00:00')")
        conn.commit()
        conn.close()

        m = get_feedback_coverage_metrics()
        assert m["delivered_vacancies"] == 2
        assert m["telegram_feedback_vacancies"] == 1
        assert m["telegram_feedback_coverage_rate"] == 0.5
        assert m["confirmed_applications"] == 1


# ---------------------------------------------------------------------------
# 8. Human Evidence Coverage
# ---------------------------------------------------------------------------
def test_8_human_evidence_coverage_includes_confirmed_apps(tmp_path):
    """Test 8: Canonical human evidence coverage includes confirmed applications (total 2/2 = 100%)."""
    db_file = tmp_path / "test_cov_human.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:1', 'hh', '1', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/1')")
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:2', 'hh', '2', 'Python Dev', 'Maxima.tech', 'Python', 'https://hh.ru/2')")
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:1', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:2', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:1', 'INTERESTED', '392046103', '1001', '2026-09-01T12:00:00')")
        conn.execute("INSERT INTO hh_applications (application_id, vacancy_stable_id, state, last_transition_reason, created_at, updated_at) VALUES (1, 'hh:2', 'SUBMITTED', 'controlled_runner_submit_confirmed', '2026-09-01T12:00:00', '2026-09-01T12:00:00')")
        conn.commit()
        conn.close()

        m = get_feedback_coverage_metrics()
        assert m["canonical_human_evidence_vacancies"] == 2
        assert m["human_evidence_coverage_rate"] == 1.0


# ---------------------------------------------------------------------------
# 9. Single Vacancy Counted Once Despite Progression
# ---------------------------------------------------------------------------
def test_9_single_vacancy_counted_once_despite_progression(tmp_path):
    """Test 9: Vacancy with INTERESTED, PREPARE_APPLICATION, and SUBMITTED counts as 1 vacancy in coverage."""
    db_file = tmp_path / "test_prog_cov.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('hh:1', 'hh', '1', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/1')")
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:1', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:1', 'INTERESTED', '392046103', '1001', '2026-09-01T12:00:00')")
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at) VALUES ('hh:1', 'PREPARE_APPLICATION', '392046103', '1002', '2026-09-01T12:05:00')")
        conn.execute("INSERT INTO hh_applications (application_id, vacancy_stable_id, state, last_transition_reason, created_at, updated_at) VALUES (1, 'hh:1', 'SUBMITTED', 'controlled_runner_submit_confirmed', '2026-09-01T12:10:00', '2026-09-01T12:10:00')")
        conn.commit()
        conn.close()

        m = get_feedback_coverage_metrics()
        assert m["delivered_vacancies"] == 1
        assert m["telegram_feedback_vacancies"] == 1
        assert m["canonical_human_evidence_vacancies"] == 1


# ---------------------------------------------------------------------------
# 10. Callback Size Limit Safety
# ---------------------------------------------------------------------------
def test_10_callback_size_safety_all_combinations():
    """Test 10: All action and reason callback encodings fit comfortably within Telegram's 64-byte limit."""
    sid = "hh:136551280"
    for act in TelegramFeedbackAction:
        for rsn in FeedbackReason:
            cb = encode_callback_data(act, sid, rsn)
            assert len(cb.encode("utf-8")) <= 64


# ---------------------------------------------------------------------------
# 11. Long Stable ID Reason Resolves Safely
# ---------------------------------------------------------------------------
def test_11_long_stable_id_reason_resolves_safely(tmp_path):
    """Test 11: Extra-long stable_id uses 16-char SHA256 surrogate and resolves faithfully."""
    long_sid = "weworkremotely:company-name-extremely-long-title-mac-helpdesk-engineer-remote"
    db_file = tmp_path / "test_long_sid.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'weworkremotely', '1', 'Title', 'Company', 'Desc', 'https://wwr.com/1')", (long_sid,))
        conn.commit()
        conn.close()

        cb = encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, long_sid, FeedbackReason.TECH_STACK)
        assert len(cb.encode("utf-8")) <= 64
        assert ":h:" in cb

        decoded = decode_callback_data(cb)
        act, resolved_sid = decoded
        assert act == TelegramFeedbackAction.NOT_INTERESTED
        assert resolved_sid == long_sid
        assert decoded.reason == FeedbackReason.TECH_STACK


# ---------------------------------------------------------------------------
# 12. Hash Collision Fails Closed
# ---------------------------------------------------------------------------
def test_12_hash_collision_fails_closed(tmp_path):
    """Test 12: Zero match or multiple matches for hash surrogate fails closed."""
    db_file = tmp_path / "test_collision.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()

        # Zero match
        decoded_zero = decode_callback_data("fb:RSN:NOT:STK:h:0000000000000000")
        assert decoded_zero.stable_id is None

        # Collision simulation with mock
        with patch("ai_assistant.db.resolve_vacancy_by_hash_prefix", return_value=None):
            decoded_col = decode_callback_data("fb:RSN:NOT:STK:h:1234567812345678")
            assert decoded_col.stable_id is None


# ---------------------------------------------------------------------------
# 13. Reason COMPANY Remains Company-Specific
# ---------------------------------------------------------------------------
def test_13_reason_company_stays_company_specific(tmp_path):
    """Test 13: Negative feedback with reason COMPANY penalizes company but not role family."""
    db_file = tmp_path / "test_co_iso.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'Python Developer', 'ООО СП Солюшен', 'Python code', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '1001', '2026-09-01T12:00:00', '{\"feedback_reason\": \"COMPANY\"}')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert "ООО СП Солюшен" in prof.companies
        assert prof.companies["ООО СП Солюшен"].raw_signal < 0
        assert "PYTHON_BACKEND" not in prof.role_families


# ---------------------------------------------------------------------------
# 14. Reason SALARY Does Not Penalize Role
# ---------------------------------------------------------------------------
def test_14_reason_salary_does_not_penalize_role(tmp_path):
    """Test 14: Negative feedback with reason SALARY does not penalize role family."""
    db_file = tmp_path / "test_sal_iso.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Engineer', 'ООО СП Солюшен', 'AI code', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_feedback_records (vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json) VALUES (?, 'NOT_INTERESTED', '392046103', '1001', '2026-09-01T12:00:00', '{\"feedback_reason\": \"SALARY\"}')", (sid,))
        conn.commit()
        conn.close()

        prof = build_preference_profile()
        assert "AI_AUTOMATION" not in prof.role_families
        assert "PYTHON_BACKEND" not in prof.role_families


# ---------------------------------------------------------------------------
# 15. Reason TECH_STACK Does Not Modify Candidate Factual Skills
# ---------------------------------------------------------------------------
def test_15_reason_tech_stack_does_not_mutate_candidate_profile():
    """Test 15: Feedback with reason TECH_STACK leaves candidate_profile.json completely unaltered."""
    prof_before = load_candidate_profile()
    skills_before = list(prof_before.skills)
    conf_before = dict(prof_before.skill_confidence)

    # Process preference analytics
    build_preference_profile(events=[
        PreferenceEvidenceEvent(
            vacancy_stable_id="hh:136551280",
            action="NOT_INTERESTED",
            signal_strength=SignalStrength.STRONG_NEGATIVE,
            created_at="2026-09-01T12:00:00",
            feedback_reason="TECH_STACK",
            skills=["python", "llm"],
            company="ООО СП Солюшен",
        )
    ])

    prof_after = load_candidate_profile()
    assert prof_after.skills == skills_before
    assert prof_after.skill_confidence == conf_before


# ---------------------------------------------------------------------------
# 16. Legacy/Test Vacancy Excluded
# ---------------------------------------------------------------------------
def test_16_legacy_test_vacancy_cannot_create_reason_evidence(tmp_path):
    """Test 16: Feedback processor rejects reason callbacks for non-genuine vacancies."""
    db_file = tmp_path / "test_legacy_rej.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES ('vacancies_json:24', 'vacancies_json', '24', 'AI Dev', 'DeepSense', 'Python', 'http://example.com/24')")
        conn.commit()
        conn.close()

        processor = TelegramFeedbackProcessor(allowed_user_id="392046103", require_delivered=False)
        cb_query = {
            "id": "1001",
            "from": {"id": 392046103},
            "message": {"chat": {"id": -1004399255305}},
            "data": encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, "vacancies_json:24", FeedbackReason.ROLE),
        }
        res = processor.process_callback_query(cb_query)
        assert res["success"] is False
        assert res["error"] == "INVALID_PROVENANCE"


# ---------------------------------------------------------------------------
# 17. Unauthorized User Rejected
# ---------------------------------------------------------------------------
def test_17_unauthorized_user_rejected():
    """Test 17: Callback from non-owner user ID is rejected fail-closed."""
    processor = TelegramFeedbackProcessor(allowed_user_id="392046103", require_delivered=False)
    cb_query = {
        "id": "1001",
        "from": {"id": 999999999},
        "message": {"chat": {"id": -1004399255305}},
        "data": encode_callback_data(TelegramFeedbackAction.INTERESTED, "hh:136551280"),
    }
    res = processor.process_callback_query(cb_query)
    assert res["success"] is False
    assert res["error"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 18. Production Calibration Flag Disabled
# ---------------------------------------------------------------------------
def test_18_production_calibration_flag_stays_disabled():
    """Test 18: PREFERENCE_CALIBRATION_ENABLED remains False."""
    assert config.PREFERENCE_CALIBRATION_ENABLED is False


# ---------------------------------------------------------------------------
# 19. DecodedCallback 2-Tuple Unpacking Compatibility
# ---------------------------------------------------------------------------
def test_19_decoded_callback_tuple_compatibility():
    """Test 19: DecodedCallback supports 2-tuple unpacking, len()==2, and .reason property."""
    dec = DecodedCallback(TelegramFeedbackAction.NOT_INTERESTED, "hh:136551280", FeedbackReason.SALARY)
    assert len(dec) == 2
    act, sid = dec
    assert act == TelegramFeedbackAction.NOT_INTERESTED
    assert sid == "hh:136551280"
    assert dec.reason == FeedbackReason.SALARY
    assert dec.action == TelegramFeedbackAction.NOT_INTERESTED
    assert dec.stable_id == "hh:136551280"


# ---------------------------------------------------------------------------
# 20. Structured Reason Acknowledgment Text
# ---------------------------------------------------------------------------
def test_20_reason_acknowledgment_text_accurate(tmp_path):
    """Test 20: Feedback processor generates accurate human acknowledgment with reason tag."""
    db_file = tmp_path / "test_ack_text.db"
    with patch("ai_assistant.config.DB_FILE", str(db_file)):
        db.init_db()
        conn = db.get_connection()
        sid = "hh:136551280"
        conn.execute("INSERT INTO vacancies (stable_id, source, source_job_id, title, company, description, job_url) VALUES (?, 'hh', '136551280', 'AI Dev', 'ООО СП Солюшен', 'Python', 'https://hh.ru/136551280')", (sid,))
        conn.execute("INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status) VALUES ('digest:hh:136551280', 'JOB_DIGEST', '-1004399255305', '2026-09-01T10:00:00', 'DELIVERED')")
        conn.commit()
        conn.close()

        processor = TelegramFeedbackProcessor(allowed_user_id="392046103", require_delivered=True)
        res = processor.process_callback_query({
            "id": "1001",
            "from": {"id": 392046103},
            "message": {"chat": {"id": -1004399255305}},
            "data": encode_callback_data(TelegramFeedbackAction.NOT_INTERESTED, sid, FeedbackReason.TECH_STACK),
        })
        assert "TECH_STACK" in res["message"]
        assert "👎" in res["message"]
