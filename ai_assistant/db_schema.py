"""DDL-схема базы данных.

Вынесено из db.py 2026-09-07: init_db() занимала 486 строк посреди бизнес-логики.
Модуль не знает про config и не открывает соединение сам — принимает готовый conn,
чтобы не было циклического импорта с db.py.
"""
from __future__ import annotations

import hashlib
import inspect
import sqlite3


def apply_schema(conn: sqlite3.Connection) -> None:
    """Идемпотентно накатывает схему. Ничего не создаёт, кроме таблиц и индексов."""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_vacancies (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            content_hash TEXT,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS vacancies (
            stable_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT,
            description TEXT,
            location TEXT,
            country_restrictions TEXT,
            timezone_restrictions TEXT,
            salary_min REAL,
            salary_max REAL,
            salary_currency TEXT,
            employment_type TEXT,
            job_url TEXT NOT NULL,
            application_url TEXT,
            published_at TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            state TEXT NOT NULL DEFAULT 'NEW',
            raw_data TEXT,
            match_score REAL,
            match_decision TEXT,
            match_reasons TEXT,
            match_strengths TEXT,
            match_gaps TEXT
        )
        '''
    )
    cursor.execute(
        '''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vacancies_source_job
        ON vacancies(source, source_job_id)
        '''
    )
    cursor.execute(
        '''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vacancies_job_url
        ON vacancies(job_url)
        '''
    )
    # Deep analysis table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deep_analysis (
            vacancy_stable_id TEXT PRIMARY KEY,
            analyzer_version TEXT NOT NULL,
            fit_score INTEGER,
            recommendation TEXT,
            analysis_json TEXT,
            analyzed_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_deep_analysis_version
        ON deep_analysis(analyzer_version)
    ''')
    # Application packages table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_packages (
            vacancy_stable_id TEXT PRIMARY KEY,
            generator_version TEXT NOT NULL,
            package_json TEXT,
            created_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_app_pkg_version
        ON application_packages(generator_version)
    ''')
    # Application tracking tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_tracking (
            vacancy_stable_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            company TEXT,
            title TEXT,
            source TEXT,
            vacancy_url TEXT,
            match_score REAL,
            deep_score REAL,
            created_at TEXT,
            updated_at TEXT,
            applied_at TEXT,
            verified_at TEXT,
            last_status_change_at TEXT,
            notes TEXT
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_app_tracking_status ON application_tracking(status)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_app_tracking_updated ON application_tracking(updated_at)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_app_tracking_vacancy ON application_tracking(vacancy_stable_id)''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_stable_id TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            note TEXT,
            FOREIGN KEY (vacancy_stable_id) REFERENCES application_tracking(vacancy_stable_id)
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_app_history_vacancy ON application_status_history(vacancy_stable_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_app_history_status ON application_status_history(new_status)''')
    # Application queue table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_queue (
            vacancy_stable_id TEXT PRIMARY KEY,
            priority_score INTEGER,
            rank INTEGER,
            queue_json TEXT,
            generated_at TEXT,
            queue_version TEXT
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_queue_version ON application_queue(queue_version)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_queue_rank ON application_queue(rank)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_queue_priority ON application_queue(priority_score)''')


    # Browser preparations table
    cursor.execute("CREATE TABLE IF NOT EXISTS browser_preparations (vacancy_stable_id TEXT, url TEXT, status TEXT, final_url TEXT, page_title TEXT, site TEXT, form_detected INTEGER, fields_json TEXT, warnings_json TEXT, screenshot_path TEXT, created_at TEXT, updated_at TEXT, executor_version TEXT, PRIMARY KEY (vacancy_stable_id, executor_version))")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_browser_status ON browser_preparations(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_browser_version ON browser_preparations(executor_version)")
    # Application reviews table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS application_reviews (
            vacancy_stable_id TEXT PRIMARY KEY,
            review_json TEXT,
            status TEXT,
            note TEXT,
            created_at TEXT,
            updated_at TEXT,
            review_version TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_review_status ON application_reviews(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_review_version ON application_reviews(review_version)")
    # Application submissions table - supports multiple attempts per vacancy
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_submissions (
            vacancy_stable_id TEXT NOT NULL,
            submission_id TEXT NOT NULL,
            executor_version TEXT NOT NULL,
            submission_json TEXT,
            status TEXT,
            submitted_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (vacancy_stable_id, submission_id, executor_version)
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_submission_status ON application_submissions(status)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_submission_version ON application_submissions(executor_version)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_submission_vacancy ON application_submissions(vacancy_stable_id)''')
    # Submission verifications table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS submission_verifications (
            vacancy_stable_id TEXT NOT NULL,
            submission_id TEXT NOT NULL,
            verification_version TEXT NOT NULL,
            verification_status TEXT,
            verification_json TEXT,
            verified_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (vacancy_stable_id, submission_id, verification_version)
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_verification_status ON submission_verifications(verification_status)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_verification_version ON submission_verifications(verification_version)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_verification_vacancy ON submission_verifications(vacancy_stable_id)''')
    # Canonical vacancies table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS canonical_vacancies (
            canonical_id TEXT PRIMARY KEY,
            normalized_url TEXT UNIQUE NOT NULL,
            normalized_company TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            location TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_canonical_company ON canonical_vacancies(normalized_company)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_canonical_title ON canonical_vacancies(normalized_title)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_canonical_location ON canonical_vacancies(location)''')
    # Vacancy aliases table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vacancy_aliases (
            canonical_id TEXT NOT NULL,
            vacancy_stable_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            match_type TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (canonical_id, vacancy_stable_id)
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_alias_vacancy ON vacancy_aliases(vacancy_stable_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_alias_match_type ON vacancy_aliases(match_type)''')
    # Application queue table (v2 with canonical identity support)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_queue (
            vacancy_stable_id TEXT PRIMARY KEY,
            priority_score INTEGER,
            rank INTEGER,
            queue_json TEXT,
            generated_at TEXT,
            queue_version TEXT,
            canonical_id TEXT,
            representative_vacancy_stable_id TEXT
        )
    ''')
    # Add missing columns if table exists from old version
    try:
        cursor.execute("ALTER TABLE application_queue ADD COLUMN canonical_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE application_queue ADD COLUMN representative_vacancy_stable_id TEXT")
    except sqlite3.OperationalError:
        pass
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_queue_version ON application_queue(queue_version)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_queue_rank ON application_queue(rank)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_queue_priority ON application_queue(priority_score)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_queue_canonical ON application_queue(canonical_id)''')
    # Vacancy eligibility table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vacancy_eligibility (
            vacancy_stable_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            assessment_json TEXT NOT NULL,
            assessed_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_eligibility_status ON vacancy_eligibility(status)''')

    # HH message events table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hh_message_events (
            message_fingerprint TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            text TEXT,
            sent_at TEXT,
            seen_at TEXT NOT NULL,
            processed INTEGER DEFAULT 0,
            classification TEXT,
            validation TEXT,
            reply_draft TEXT,
            status TEXT,
            error TEXT,
            vacancy_stable_id TEXT,
            employer TEXT
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_msg_conv ON hh_message_events(conversation_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_msg_processed ON hh_message_events(processed)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_msg_status ON hh_message_events(status)''')

    # Stage 34 — HH questionnaires table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hh_questionnaires (
            questionnaire_id TEXT PRIMARY KEY,
            vacancy_stable_id TEXT,
            conversation_id TEXT,
            title TEXT,
            employer TEXT,
            fingerprint TEXT NOT NULL,
            questions_json TEXT NOT NULL,
            answers_json TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_quest_vac ON hh_questionnaires(vacancy_stable_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_quest_conv ON hh_questionnaires(conversation_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_quest_status ON hh_questionnaires(status)''')

    # Stage 35 — HH Applications (Single Source of Truth)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hh_applications (
            application_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            vacancy_stable_id TEXT,
            title TEXT,
            employer TEXT,
            state TEXT NOT NULL,
            draft TEXT,
            questionnaire_id TEXT,
            answers_json TEXT,
            error TEXT,
            last_transition_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_app_conv ON hh_applications(conversation_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_app_vac ON hh_applications(vacancy_stable_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_app_state ON hh_applications(state)''')

    # Stage 35 — HH Application Transition Audit Trail
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hh_application_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id TEXT NOT NULL,
            conversation_id TEXT,
            vacancy_stable_id TEXT,
            state TEXT NOT NULL,
            previous_state TEXT,
            reason TEXT NOT NULL,
            evidence_json TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_trans_app ON hh_application_transitions(application_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_trans_state ON hh_application_transitions(state)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_hh_trans_created ON hh_application_transitions(created_at)''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')

    # Phase 2.1 — Exclusive Submission Claims Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS submission_claims (
            vacancy_stable_id TEXT PRIMARY KEY,
            application_id TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            status TEXT NOT NULL,
            worker_id TEXT,
            claimed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            details_json TEXT
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_sub_claim_app ON submission_claims(application_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_sub_claim_status ON submission_claims(status)''')

    # Stage 51 — Autonomous Notifications
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS autonomous_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_type TEXT NOT NULL,
            priority TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            company TEXT,
            vacancy_title TEXT,
            vacancy_url TEXT,
            conversation_id TEXT,
            action_required TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            read INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_notif_type ON autonomous_notifications(notification_type)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_notif_priority ON autonomous_notifications(priority)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_notif_created ON autonomous_notifications(created_at)''')

    # Stage 51 — Interview Events
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS interview_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            vacancy_stable_id TEXT,
            company TEXT NOT NULL,
            vacancy_title TEXT NOT NULL,
            invitation_text TEXT NOT NULL,
            invitation_url TEXT,
            action_required TEXT,
            status TEXT DEFAULT 'NEW',
            detected_at TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_interview_conv ON interview_events(conversation_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_interview_detected ON interview_events(detected_at)''')

    # Stage 51 — Autonomous Cycle Runs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS autonomous_cycle_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            discovered_count INTEGER DEFAULT 0,
            matched_count INTEGER DEFAULT 0,
            applied_count INTEGER DEFAULT 0,
            verified_count INTEGER DEFAULT 0,
            messages_checked INTEGER DEFAULT 0,
            auto_replies_count INTEGER DEFAULT 0,
            interviews_detected INTEGER DEFAULT 0,
            rejections_count INTEGER DEFAULT 0,
            unanswered_questions_count INTEGER DEFAULT 0,
            summary TEXT,
            log_json TEXT
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_cycle_started ON autonomous_cycle_runs(started_at)''')

    # Stage 52 — Autonomous Conversation Audits
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS autonomous_conversation_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            application_id TEXT,
            vacancy_id TEXT,
            vacancy_stable_id TEXT,
            employer TEXT NOT NULL,
            incoming_message TEXT NOT NULL,
            incoming_message_timestamp TEXT,
            message_classification TEXT NOT NULL,
            generated_reply TEXT,
            sent_reply TEXT,
            sent_at TEXT,
            profile_facts_used TEXT,
            decision_reason TEXT,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_conv_audit_conv ON autonomous_conversation_audits(conversation_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_conv_audit_app ON autonomous_conversation_audits(application_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_conv_audit_created ON autonomous_conversation_audits(created_at)''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS telegram_delivery_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_key TEXT NOT NULL UNIQUE,
            notification_type TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tg_delivery_key ON telegram_delivery_records(delivery_key)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tg_delivery_created ON telegram_delivery_records(delivered_at)''')

    # Stage 89 — Telegram Feedback Audit Records
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS telegram_feedback_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_stable_id TEXT NOT NULL,
            action TEXT NOT NULL,
            telegram_user_id TEXT,
            telegram_chat_id TEXT,
            callback_query_id TEXT UNIQUE,
            previous_status TEXT,
            new_status TEXT,
            created_at TEXT NOT NULL,
            payload_json TEXT
        )
    ''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tg_feedback_vac ON telegram_feedback_records(vacancy_stable_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tg_feedback_action ON telegram_feedback_records(action)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tg_feedback_cb ON telegram_feedback_records(callback_query_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tg_feedback_created ON telegram_feedback_records(created_at)''')
    conn.commit()

_FINGERPRINT: str | None = None


def schema_fingerprint() -> str:
    """Хэш исходного текста apply_schema.

    Если кто-то правит DDL, текст функции меняется, хэш меняется,
    и ensure_schema() в db.py переприменяет схему сама. Поднимать номер
    версии руками не нужно.

    Кэшируется: inspect.getsource читает файл с диска, и на 143 вызовах
    за прогон это съедало весь выигрыш.
    """
    global _FINGERPRINT
    if _FINGERPRINT is None:
        _FINGERPRINT = hashlib.sha256(
            inspect.getsource(apply_schema).encode("utf-8")
        ).hexdigest()[:16]
    return _FINGERPRINT
