import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Set
from . import config
from .schema import Vacancy

_DRY_RUN = False


def set_dry_run(enabled: bool) -> None:
    global _DRY_RUN
    _DRY_RUN = bool(enabled)


def is_dry_run() -> bool:
    return _DRY_RUN


def get_connection() -> None:
    return sqlite3.connect(config.DB_FILE)


def init_db() -> None:
    conn = get_connection()
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
    conn.close()


def is_processed(vacancy_id: str, current_hash: str) -> bool:
    """Проверяет, обрабатывалась ли вакансия с таким же хешем."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT content_hash FROM processed_vacancies WHERE id = ?', (str(vacancy_id),))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return False
    return row[0] == current_hash


def save_status(vacancy_id: str, status: str, content_hash: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO processed_vacancies (id, status, content_hash, processed_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET 
            status=excluded.status, 
            content_hash=excluded.content_hash,
            processed_at=CURRENT_TIMESTAMP
    ''', (str(vacancy_id), status, content_hash))
    conn.commit()
    conn.close()


def save_vacancy(vacancy) -> str:
    """Save or update a vacancy deterministically and idempotently (Stage 85).
    
    Returns:
        'INSERTED' - A new vacancy row was inserted.
        'UPDATED'  - Existing vacancy had metadata updated and last_seen_at refreshed.
        'UNCHANGED'- Existing vacancy is identical; only last_seen_at was refreshed.
        'CONFLICT' - Conflicting identity detected.
    """
    if is_dry_run():
        return "UNCHANGED"

    from .vacancy_identity import normalize_url
    
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    
    sid = vacancy.stable_id()
    raw_url = str(vacancy.job_url or "").strip()
    norm_url = normalize_url(raw_url) if raw_url else raw_url
    now_iso = _to_iso(vacancy.last_seen_at) or datetime.utcnow().isoformat()
    first_seen_iso = _to_iso(vacancy.first_seen_at) or now_iso

    # 1. Lookup existing by stable_id or by job_url
    cursor.execute('''
        SELECT stable_id, job_url, title, company, description, location, 
               salary_min, salary_max, salary_currency, employment_type,
               first_seen_at, last_seen_at, state, match_score, match_decision,
               match_reasons, match_strengths, match_gaps, raw_data
        FROM vacancies
        WHERE stable_id = ? OR job_url = ?
    ''', (sid, norm_url))
    rows = cursor.fetchall()
    
    if rows:
        # Match found!
        existing = rows[0]
        ex_sid = existing[0]
        ex_url = existing[1]
        
        # Check if metadata changed
        meta_changed = (
            (vacancy.title and vacancy.title != existing[2]) or
            (vacancy.company and vacancy.company != existing[3]) or
            (vacancy.description and vacancy.description != existing[4]) or
            (vacancy.location and vacancy.location != existing[5]) or
            (vacancy.salary_min is not None and vacancy.salary_min != existing[6]) or
            (vacancy.salary_max is not None and vacancy.salary_max != existing[7]) or
            (vacancy.salary_currency and vacancy.salary_currency != existing[8]) or
            (vacancy.employment_type and vacancy.employment_type != existing[9])
        )
        
        if meta_changed:
            cursor.execute('''
                UPDATE vacancies SET
                    title = COALESCE(NULLIF(?, ''), title),
                    company = COALESCE(NULLIF(?, ''), company),
                    description = COALESCE(NULLIF(?, ''), description),
                    location = COALESCE(NULLIF(?, ''), location),
                    country_restrictions = ?,
                    timezone_restrictions = ?,
                    salary_min = COALESCE(?, salary_min),
                    salary_max = COALESCE(?, salary_max),
                    salary_currency = COALESCE(NULLIF(?, ''), salary_currency),
                    employment_type = COALESCE(NULLIF(?, ''), employment_type),
                    last_seen_at = ?,
                    raw_data = ?
                WHERE stable_id = ?
            ''', (
                vacancy.title,
                vacancy.company,
                vacancy.description,
                vacancy.location,
                ', '.join(vacancy.country_restrictions),
                ', '.join(str(x) for x in vacancy.timezone_restrictions),
                vacancy.salary_min,
                vacancy.salary_max,
                vacancy.salary_currency,
                vacancy.employment_type,
                now_iso,
                str(vacancy.raw_data),
                ex_sid,
            ))
            conn.commit()
            conn.close()
            return "UPDATED"
        else:
            # Metadata is identical -> refresh last_seen_at only
            cursor.execute('''
                UPDATE vacancies SET last_seen_at = ? WHERE stable_id = ?
            ''', (now_iso, ex_sid))
            conn.commit()
            conn.close()
            return "UNCHANGED"
            
    # 2. No existing record found -> Insert fresh vacancy
    try:
        cursor.execute('''
            INSERT INTO vacancies (
                stable_id, source, source_job_id, title, company, description, location,
                country_restrictions, timezone_restrictions, salary_min, salary_max, salary_currency,
                employment_type, job_url, application_url, published_at, first_seen_at, last_seen_at,
                state, raw_data, match_score, match_decision, match_reasons, match_strengths, match_gaps
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', (
            sid,
            vacancy.source,
            vacancy.source_job_id,
            vacancy.title,
            vacancy.company,
            vacancy.description,
            vacancy.location,
            ', '.join(vacancy.country_restrictions),
            ', '.join(str(x) for x in vacancy.timezone_restrictions),
            vacancy.salary_min,
            vacancy.salary_max,
            vacancy.salary_currency,
            vacancy.employment_type,
            norm_url,
            vacancy.application_url,
            _to_iso(vacancy.published_at),
            first_seen_iso,
            now_iso,
            'NEW',
            str(vacancy.raw_data),
            None,
            None,
            None,
            None,
            None,
        ))
        conn.commit()
        conn.close()
        
        # Auto-assess eligibility
        try:
            from .eligibility import assess_vacancy_eligibility
            assessment = assess_vacancy_eligibility(vacancy)
            save_vacancy_eligibility(sid, assessment)
        except Exception:
            pass
            
        return "INSERTED"
    except sqlite3.IntegrityError:
        # Concurrent insertion race condition -> safe no-op
        conn.close()
        return "UNCHANGED"


def save_vacancy_eligibility(vacancy_stable_id: str, assessment: Any, assessed_at: str | None = None) -> None:
    """Save or update structured eligibility assessment for a vacancy."""
    import json
    import datetime as _dt
    if assessed_at is None:
        assessed_at = _dt.datetime.utcnow().isoformat()
    init_db()
    conn = get_connection()
    cur = conn.cursor()

    status_val = assessment.eligibility.value if hasattr(getattr(assessment, 'eligibility', None), 'value') else str(getattr(assessment, 'eligibility', assessment))
    reasons_list = getattr(assessment, 'eligibility_reasons', []) or []
    reasons_str = json.dumps(reasons_list, ensure_ascii=False)
    
    if hasattr(assessment, 'to_dict'):
        assessment_dict = assessment.to_dict()
    elif isinstance(assessment, dict):
        assessment_dict = assessment
    else:
        assessment_dict = {"status": status_val, "reasons": reasons_list}
    assessment_str = json.dumps(assessment_dict, ensure_ascii=False)

    cur.execute('''
        INSERT INTO vacancy_eligibility (vacancy_stable_id, status, reasons_json, assessment_json, assessed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(vacancy_stable_id) DO UPDATE SET
            status=excluded.status,
            reasons_json=excluded.reasons_json,
            assessment_json=excluded.assessment_json,
            assessed_at=excluded.assessed_at
    ''', (vacancy_stable_id, status_val, reasons_str, assessment_str, assessed_at))
    conn.commit()
    conn.close()


def get_vacancy_eligibility(vacancy_stable_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve saved eligibility assessment for a specific vacancy."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT vacancy_stable_id, status, reasons_json, assessment_json, assessed_at FROM vacancy_eligibility WHERE vacancy_stable_id = ?', (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    import json
    try:
        reasons = json.loads(row[2]) if row[2] else []
    except Exception:
        reasons = []
    try:
        assessment = json.loads(row[3]) if row[3] else {}
    except Exception:
        assessment = {}
    return {
        "vacancy_stable_id": row[0],
        "status": row[1],
        "reasons": reasons,
        "assessment": assessment,
        "assessed_at": row[4],
    }


def get_all_vacancy_eligibilities() -> Dict[str, Dict[str, Any]]:
    """Retrieve all saved eligibility records as a mapping from stable_id to dict."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT vacancy_stable_id, status, reasons_json, assessment_json, assessed_at FROM vacancy_eligibility')
    rows = cur.fetchall()
    conn.close()
    import json
    result = {}
    for row in rows:
        try:
            reasons = json.loads(row[2]) if row[2] else []
        except Exception:
            reasons = []
        try:
            assessment = json.loads(row[3]) if row[3] else {}
        except Exception:
            assessment = {}
        result[row[0]] = {
            "vacancy_stable_id": row[0],
            "status": row[1],
            "reasons": reasons,
            "assessment": assessment,
            "assessed_at": row[4],
        }
    return result


def delete_queue_item(vacancy_stable_id: str) -> None:
    """Safely remove a specific vacancy from application_queue only (zero physical vacancy delete)."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM application_queue WHERE vacancy_stable_id = ?', (vacancy_stable_id,))
    conn.commit()
    conn.close()


def _to_iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def get_vacancy_by_id(stable_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vacancies WHERE stable_id = ?', (stable_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def list_vacancies(limit: int = 50, state: str | None = None):
    conn = get_connection()
    cursor = conn.cursor()
    if state:
        cursor.execute('SELECT * FROM vacancies WHERE state = ? ORDER BY first_seen_at DESC LIMIT ?', (state, limit))
    else:
        cursor.execute('SELECT * FROM vacancies ORDER BY first_seen_at DESC LIMIT ?', (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# --- Deep analysis persistence ---
def save_deep_analysis(vacancy_stable_id: str, analyzer_version: str, fit_score: int, recommendation: str, analysis_json: str, analyzed_at: str | None = None) -> None:
    import datetime as _dt
    if analyzed_at is None:
        analyzed_at = _dt.datetime.utcnow().isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO deep_analysis (vacancy_stable_id, analyzer_version, fit_score, recommendation, analysis_json, analyzed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(vacancy_stable_id) DO UPDATE SET
            analyzer_version=excluded.analyzer_version,
            fit_score=excluded.fit_score,
            recommendation=excluded.recommendation,
            analysis_json=excluded.analysis_json,
            analyzed_at=excluded.analyzed_at
    ''', (vacancy_stable_id, analyzer_version, fit_score, recommendation, analysis_json, analyzed_at))
    conn.commit()
    conn.close()


def get_deep_analysis(vacancy_stable_id: str, analyzer_version: str | None = None):
    conn = get_connection()
    cur = conn.cursor()
    if analyzer_version is not None:
        cur.execute('SELECT vacancy_stable_id, analyzer_version, fit_score, recommendation, analysis_json, analyzed_at FROM deep_analysis WHERE vacancy_stable_id=? AND analyzer_version=?', (vacancy_stable_id, analyzer_version))
    else:
        cur.execute('SELECT vacancy_stable_id, analyzer_version, fit_score, recommendation, analysis_json, analyzed_at FROM deep_analysis WHERE vacancy_stable_id=?', (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    return row


def is_deep_analyzed(vacancy_stable_id: str, analyzer_version: str) -> bool:
    return get_deep_analysis(vacancy_stable_id, analyzer_version) is not None


def list_deep_analyses(limit: int = 50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT vacancy_stable_id, analyzer_version, fit_score, recommendation, analysis_json, analyzed_at FROM deep_analysis ORDER BY analyzed_at DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# --- Application packages persistence ---
def save_application_package(vacancy_stable_id: str, generator_version: str, package_json: str, created_at: str | None = None) -> None:
    import datetime as _dt
    if created_at is None:
        created_at = _dt.datetime.utcnow().isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO application_packages (vacancy_stable_id, generator_version, package_json, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(vacancy_stable_id) DO UPDATE SET
            generator_version=excluded.generator_version,
            package_json=excluded.package_json,
            created_at=excluded.created_at
    ''', (vacancy_stable_id, generator_version, package_json, created_at))
    conn.commit()
    conn.close()


def get_application_package(vacancy_stable_id: str, generator_version: str | None = None):
    conn = get_connection()
    cur = conn.cursor()
    if generator_version is not None:
        cur.execute('SELECT vacancy_stable_id, generator_version, package_json, created_at FROM application_packages WHERE vacancy_stable_id=? AND generator_version=?', (vacancy_stable_id, generator_version))
    else:
        cur.execute('SELECT vacancy_stable_id, generator_version, package_json, created_at FROM application_packages WHERE vacancy_stable_id=?', (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    return row


def is_application_prepared(vacancy_stable_id: str, generator_version: str) -> bool:
    return get_application_package(vacancy_stable_id, generator_version) is not None


def list_application_packages(limit: int = 50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT vacancy_stable_id, generator_version, package_json, created_at FROM application_packages ORDER BY created_at DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# --- Application submissions persistence ---
def save_submission(vacancy_stable_id: str, submission_json: str, status: str, submitted_at: str | None = None, created_at: str | None = None, updated_at: str | None = None, executor_version: str = "v1", submission_id: str | None = None) -> str:
    """
    Save a submission attempt. Returns the submission_id used.
    If submission_id is not provided, generates one from the JSON or creates a new one.
    """
    import datetime as _dt
    import json as _json

    if submitted_at is None:
        submitted_at = _dt.datetime.utcnow().isoformat()
    if created_at is None:
        created_at = _dt.datetime.utcnow().isoformat()
    if updated_at is None:
        updated_at = _dt.datetime.utcnow().isoformat()

    # Extract submission_id from JSON if not provided
    if submission_id is None:
        try:
            sub_data = _json.loads(submission_json) if submission_json else {}
            submission_id = sub_data.get("submission_id")
        except Exception:
            submission_id = None

    # Generate submission_id if still not available
    if submission_id is None:
        submission_id = f"{vacancy_stable_id}_{_dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO application_submissions (vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vacancy_stable_id, submission_id, executor_version) DO UPDATE SET
            submission_json=excluded.submission_json,
            status=excluded.status,
            submitted_at=excluded.submitted_at,
            updated_at=excluded.updated_at
    ''', (vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at))
    conn.commit()
    conn.close()
    return submission_id


def get_submission(vacancy_stable_id: str, submission_id: str | None = None, executor_version: str | None = None):
    """
    Get a specific submission by submission_id, or the latest if not specified.
    """
    conn = get_connection()
    cur = conn.cursor()
    if submission_id is not None and executor_version is not None:
        cur.execute('SELECT vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at FROM application_submissions WHERE vacancy_stable_id=? AND submission_id=? AND executor_version=?', (vacancy_stable_id, submission_id, executor_version))
    elif submission_id is not None:
        cur.execute('SELECT vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at FROM application_submissions WHERE vacancy_stable_id=? AND submission_id=?', (vacancy_stable_id, submission_id))
    elif executor_version is not None:
        cur.execute('SELECT vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at FROM application_submissions WHERE vacancy_stable_id=? AND executor_version=? ORDER BY submitted_at DESC LIMIT 1', (vacancy_stable_id, executor_version))
    else:
        cur.execute('SELECT vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at FROM application_submissions WHERE vacancy_stable_id=? ORDER BY submitted_at DESC LIMIT 1', (vacancy_stable_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_all_submissions(vacancy_stable_id: str, executor_version: str | None = None):
    """
    Get all submission attempts for a vacancy (for audit).
    Returns list ordered by submitted_at ASC (chronological).
    """
    conn = get_connection()
    cur = conn.cursor()
    if executor_version is not None:
        cur.execute('SELECT vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at FROM application_submissions WHERE vacancy_stable_id=? AND executor_version=? ORDER BY submitted_at ASC', (vacancy_stable_id, executor_version))
    else:
        cur.execute('SELECT vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at FROM application_submissions WHERE vacancy_stable_id=? ORDER BY submitted_at ASC', (vacancy_stable_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def is_submitted(vacancy_stable_id: str, executor_version: str | None = None) -> bool:
    return get_submission(vacancy_stable_id, executor_version=executor_version) is not None


def list_submissions(limit: int = 50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT vacancy_stable_id, submission_id, executor_version, submission_json, status, submitted_at, created_at, updated_at FROM application_submissions ORDER BY submitted_at DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# --- Submission verifications persistence ---
def save_verification(vacancy_stable_id: str, submission_id: str, verification_version: str, verification_status: str, verification_json: str, verified_at: str | None = None, created_at: str | None = None, updated_at: str | None = None) -> None:
    import datetime as _dt
    import json as _json
    if verified_at is None:
        verified_at = _dt.datetime.utcnow().isoformat()
    if created_at is None:
        created_at = _dt.datetime.utcnow().isoformat()
    if updated_at is None:
        updated_at = _dt.datetime.utcnow().isoformat()

    known_statuses = {"VERIFIED", "FAILED", "AMBIGUOUS", "BLOCKED"}
    # Compatibility with legacy callers that passed status before version.
    if verification_version in known_statuses and verification_status not in known_statuses:
        verification_version, verification_status = verification_status, verification_version
    verification_version = str(verification_version or "v1")
    verification_status = str(verification_status or "").upper()
    if verification_status not in known_statuses:
        raise ValueError(f"Invalid verification status: {verification_status}")

    try:
        payload = _json.loads(verification_json) if verification_json else {}
    except Exception:
        payload = {"legacy_message": str(verification_json or "")}
    required = {"vacancy_stable_id", "submission_id", "verification_status", "verified_at"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        evidence = payload if isinstance(payload, dict) else {"legacy_payload": payload}
        payload = {
            "vacancy_stable_id": vacancy_stable_id,
            "submission_id": submission_id,
            "verification_status": verification_status,
            "evidence": evidence,
            "final_url": None,
            "page_title": None,
            "success_signal": None,
            "screenshot_path": None,
            "verified_at": verified_at,
            "warnings": ["Normalized legacy verification payload"],
            "flow_type": None,
            "source_url": None,
            "application_url": None,
            "application_domain": None,
            "redirect_chain": [],
            "is_external_application": False,
            "verification_strategy": None,
            "verification_version": verification_version,
        }
    else:
        payload["vacancy_stable_id"] = vacancy_stable_id
        payload["submission_id"] = submission_id
        payload["verification_status"] = verification_status
        payload["verification_version"] = verification_version
    verification_json = _json.dumps(payload, ensure_ascii=False)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO submission_verifications (vacancy_stable_id, submission_id, verification_version, verification_status, verification_json, verified_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vacancy_stable_id, submission_id, verification_version) DO UPDATE SET
            verification_status=excluded.verification_status,
            verification_json=excluded.verification_json,
            verified_at=excluded.verified_at,
            updated_at=excluded.updated_at
    ''', (vacancy_stable_id, submission_id, verification_version, verification_status, verification_json, verified_at, created_at, updated_at))
    conn.commit()
    conn.close()


def get_verification(vacancy_stable_id: str, submission_id: str, verification_version: str | None = None):
    conn = get_connection()
    cur = conn.cursor()
    if verification_version is not None:
        cur.execute('SELECT vacancy_stable_id, submission_id, verification_version, verification_status, verification_json, verified_at, created_at, updated_at FROM submission_verifications WHERE vacancy_stable_id=? AND submission_id=? AND verification_version=?', (vacancy_stable_id, submission_id, verification_version))
    else:
        cur.execute('SELECT vacancy_stable_id, submission_id, verification_version, verification_status, verification_json, verified_at, created_at, updated_at FROM submission_verifications WHERE vacancy_stable_id=? AND submission_id=? ORDER BY verified_at DESC LIMIT 1', (vacancy_stable_id, submission_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        # Import at module level to avoid circular import issues
        from .submission_verifier import SubmissionVerification
        import json
        data = json.loads(row[4]) if row[4] else {}
        return SubmissionVerification(**data)
    except Exception as e:
        # Log the error for debugging
        import logging
        logging.getLogger(__name__).warning(f"get_verification failed: {e}")
        return None


def is_verified(vacancy_stable_id: str, submission_id: str, verification_version: str | None = None) -> bool:
    row = get_verification(vacancy_stable_id, submission_id, verification_version)
    if not row:
        return False
    status = row.verification_status.value if hasattr(row.verification_status, "value") else str(row.verification_status)
    return status == "VERIFIED"


def list_verifications(limit: int = 50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT vacancy_stable_id, submission_id, verification_version, verification_status, verification_json, verified_at, created_at, updated_at FROM submission_verifications ORDER BY verified_at DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def _row_to_vacancy(row) -> Vacancy:
    from .schema import Vacancy
    return Vacancy(
        source=row[1],
        source_job_id=row[2],
        title=row[3],
        company=row[4] or "",
        description=row[5] or "",
        job_url=row[13],
        application_url=row[14],
        location=row[6],
        country_restrictions=[x.strip() for x in (row[7] or "").split(",") if x.strip()],
        timezone_restrictions=[x.strip() for x in (row[8] or "").split(",") if x.strip()],
        salary_min=row[9],
        salary_max=row[10],
        salary_currency=row[11],
        employment_type=row[12],
        published_at=row[15],
        first_seen_at=row[16],
        last_seen_at=row[17],
        raw_data=row[19] or {},
    )


def save_hh_message_event(
    message_fingerprint: str,
    conversation_id: str,
    sender: str,
    text: str,
    sent_at: str | None = None,
    seen_at: str | None = None,
    processed: int = 0,
    classification: str | None = None,
    validation: str | None = None,
    reply_draft: str | None = None,
    status: str | None = None,
    error: str | None = None,
    vacancy_stable_id: str | None = None,
    employer: str | None = None,
) -> None:
    seen_at = seen_at or datetime.utcnow().isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO hh_message_events (
            message_fingerprint, conversation_id, sender, text, sent_at,
            seen_at, processed, classification, validation, reply_draft,
            status, error, vacancy_stable_id, employer
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_fingerprint) DO UPDATE SET
            processed = excluded.processed,
            classification = COALESCE(excluded.classification, hh_message_events.classification),
            validation = COALESCE(excluded.validation, hh_message_events.validation),
            reply_draft = COALESCE(excluded.reply_draft, hh_message_events.reply_draft),
            status = COALESCE(excluded.status, hh_message_events.status),
            error = COALESCE(excluded.error, hh_message_events.error),
            vacancy_stable_id = COALESCE(excluded.vacancy_stable_id, hh_message_events.vacancy_stable_id),
            employer = COALESCE(excluded.employer, hh_message_events.employer)
    ''', (
        message_fingerprint, conversation_id, sender, text, sent_at,
        seen_at, processed, classification, validation, reply_draft,
        status, error, vacancy_stable_id, employer
    ))
    conn.commit()
    conn.close()


def is_hh_message_processed(message_fingerprint: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT processed FROM hh_message_events WHERE message_fingerprint = ?', (message_fingerprint,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    return bool(row[0])


def get_hh_message_event(message_fingerprint: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT message_fingerprint, conversation_id, sender, text, sent_at,
               seen_at, processed, classification, validation, reply_draft,
               status, error, vacancy_stable_id, employer
        FROM hh_message_events WHERE message_fingerprint = ?
    ''', (message_fingerprint,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "message_fingerprint": row[0],
        "conversation_id": row[1],
        "sender": row[2],
        "text": row[3],
        "sent_at": row[4],
        "seen_at": row[5],
        "processed": bool(row[6]),
        "classification": row[7],
        "validation": row[8],
        "reply_draft": row[9],
        "status": row[10],
        "error": row[11],
        "vacancy_stable_id": row[12],
        "employer": row[13],
    }


def list_hh_message_events(conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()
    if conversation_id:
        cur.execute('''
            SELECT message_fingerprint, conversation_id, sender, text, sent_at,
                   seen_at, processed, classification, validation, reply_draft,
                   status, error, vacancy_stable_id, employer
            FROM hh_message_events WHERE conversation_id = ? ORDER BY seen_at DESC LIMIT ?
        ''', (conversation_id, limit))
    else:
        cur.execute('''
            SELECT message_fingerprint, conversation_id, sender, text, sent_at,
                   seen_at, processed, classification, validation, reply_draft,
                   status, error, vacancy_stable_id, employer
            FROM hh_message_events ORDER BY seen_at DESC LIMIT ?
        ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "message_fingerprint": r[0],
            "conversation_id": r[1],
            "sender": r[2],
            "text": r[3],
            "sent_at": r[4],
            "seen_at": r[5],
            "processed": bool(r[6]),
            "classification": r[7],
            "validation": r[8],
            "reply_draft": r[9],
            "status": r[10],
            "error": r[11],
            "vacancy_stable_id": r[12],
            "employer": r[13],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Stage 34: HH Questionnaire DB Helpers
# ---------------------------------------------------------------------------

def save_hh_questionnaire(data: Dict[str, Any]) -> None:
    """Save or update an HH questionnaire in state.db."""
    conn = get_connection()
    cur = conn.cursor()
    import json
    q_id = str(data["questionnaire_id"]).strip()
    questions_json = data.get("questions_json")
    if not isinstance(questions_json, str):
        questions_json = json.dumps(data.get("questions") or [], ensure_ascii=False)
    answers_json = data.get("answers_json")
    if answers_json is not None and not isinstance(answers_json, str):
        answers_json = json.dumps(data.get("answers") or {}, ensure_ascii=False)

    now = datetime.utcnow().isoformat()
    created_at = data.get("created_at") or now
    updated_at = data.get("updated_at") or now

    cur.execute('''
        INSERT INTO hh_questionnaires (
            questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
            fingerprint, questions_json, answers_json, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(questionnaire_id) DO UPDATE SET
            vacancy_stable_id = COALESCE(excluded.vacancy_stable_id, hh_questionnaires.vacancy_stable_id),
            conversation_id = COALESCE(excluded.conversation_id, hh_questionnaires.conversation_id),
            title = COALESCE(excluded.title, hh_questionnaires.title),
            employer = COALESCE(excluded.employer, hh_questionnaires.employer),
            fingerprint = excluded.fingerprint,
            questions_json = excluded.questions_json,
            answers_json = COALESCE(excluded.answers_json, hh_questionnaires.answers_json),
            status = excluded.status,
            updated_at = excluded.updated_at
    ''', (
        q_id,
        data.get("vacancy_stable_id"),
        data.get("conversation_id"),
        data.get("title"),
        data.get("employer"),
        data.get("fingerprint", ""),
        questions_json,
        answers_json,
        data.get("status", "NEEDS_HUMAN_REVIEW"),
        created_at,
        updated_at,
    ))
    conn.commit()
    conn.close()


def _row_to_questionnaire(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    import json
    questions = []
    if row[6]:
        try:
            questions = json.loads(row[6])
        except Exception:
            questions = []
    answers = {}
    if row[7]:
        try:
            answers = json.loads(row[7])
        except Exception:
            answers = {}
    return {
        "questionnaire_id": row[0],
        "vacancy_stable_id": row[1],
        "conversation_id": row[2],
        "title": row[3],
        "employer": row[4],
        "fingerprint": row[5],
        "questions": questions,
        "answers": answers,
        "status": row[8],
        "created_at": row[9],
        "updated_at": row[10],
    }


def get_hh_questionnaire(questionnaire_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve an HH questionnaire by its ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
               fingerprint, questions_json, answers_json, status, created_at, updated_at
        FROM hh_questionnaires WHERE questionnaire_id = ?
    ''', (str(questionnaire_id).strip(),))
    row = cur.fetchone()
    conn.close()
    return _row_to_questionnaire(row)


def get_hh_questionnaire_by_vacancy(vacancy_stable_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the latest HH questionnaire for a given vacancy."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
               fingerprint, questions_json, answers_json, status, created_at, updated_at
        FROM hh_questionnaires WHERE vacancy_stable_id = ? ORDER BY updated_at DESC LIMIT 1
    ''', (str(vacancy_stable_id).strip(),))
    row = cur.fetchone()
    conn.close()
    return _row_to_questionnaire(row)


def get_hh_questionnaire_by_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the latest HH questionnaire for a given conversation."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
               fingerprint, questions_json, answers_json, status, created_at, updated_at
        FROM hh_questionnaires WHERE conversation_id = ? ORDER BY updated_at DESC LIMIT 1
    ''', (str(conversation_id).strip(),))
    row = cur.fetchone()
    conn.close()
    return _row_to_questionnaire(row)


def list_hh_questionnaires(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """List stored HH questionnaires optionally filtered by status."""
    conn = get_connection()
    cur = conn.cursor()
    if status:
        cur.execute('''
            SELECT questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
                   fingerprint, questions_json, answers_json, status, created_at, updated_at
            FROM hh_questionnaires WHERE status = ? ORDER BY updated_at DESC LIMIT ?
        ''', (status, limit))
    else:
        cur.execute('''
            SELECT questionnaire_id, vacancy_stable_id, conversation_id, title, employer,
                   fingerprint, questions_json, answers_json, status, created_at, updated_at
            FROM hh_questionnaires ORDER BY updated_at DESC LIMIT ?
        ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return [_row_to_questionnaire(r) for r in rows if r is not None]


def update_hh_questionnaire_answers(
    questionnaire_id: str,
    answers: Dict[str, Any],
    new_status: Optional[str] = None,
) -> bool:
    """Update answers and status for a questionnaire."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    answers_json = json.dumps(answers, ensure_ascii=False)
    now = datetime.utcnow().isoformat()
    if new_status:
        cur.execute('''
            UPDATE hh_questionnaires
            SET answers_json = ?, status = ?, updated_at = ?
            WHERE questionnaire_id = ?
        ''', (answers_json, new_status, now, str(questionnaire_id).strip()))
    else:
        cur.execute('''
            UPDATE hh_questionnaires
            SET answers_json = ?, updated_at = ?
            WHERE questionnaire_id = ?
        ''', (answers_json, now, str(questionnaire_id).strip()))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# ---------------------------------------------------------------------------
# Stage 35: HH Applications & Transitions DB Helpers
# ---------------------------------------------------------------------------

def save_hh_application(data: Dict[str, Any]) -> None:
    """Save or update an HH application record in state.db."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    app_id = str(data["application_id"]).strip()
    answers_json = data.get("answers_json")
    if answers_json is not None and not isinstance(answers_json, str):
        answers_json = json.dumps(data.get("answers") or {}, ensure_ascii=False)
    now = datetime.utcnow().isoformat()
    created_at = data.get("created_at") or now
    updated_at = data.get("updated_at") or now

    cur.execute('''
        INSERT INTO hh_applications (
            application_id, conversation_id, vacancy_stable_id, title, employer,
            state, draft, questionnaire_id, answers_json, error,
            last_transition_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(application_id) DO UPDATE SET
            conversation_id = COALESCE(excluded.conversation_id, hh_applications.conversation_id),
            vacancy_stable_id = COALESCE(excluded.vacancy_stable_id, hh_applications.vacancy_stable_id),
            title = COALESCE(excluded.title, hh_applications.title),
            employer = COALESCE(excluded.employer, hh_applications.employer),
            state = excluded.state,
            draft = COALESCE(excluded.draft, hh_applications.draft),
            questionnaire_id = COALESCE(excluded.questionnaire_id, hh_applications.questionnaire_id),
            answers_json = COALESCE(excluded.answers_json, hh_applications.answers_json),
            error = excluded.error,
            last_transition_reason = excluded.last_transition_reason,
            updated_at = excluded.updated_at
    ''', (
        app_id,
        data.get("conversation_id"),
        data.get("vacancy_stable_id"),
        data.get("title"),
        data.get("employer"),
        data.get("state", "NEW"),
        data.get("draft"),
        data.get("questionnaire_id"),
        answers_json,
        data.get("error"),
        data.get("last_transition_reason"),
        created_at,
        updated_at,
    ))
    conn.commit()
    conn.close()


def _row_to_application(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    import json
    answers = {}
    if row[8]:
        try:
            answers = json.loads(row[8])
        except Exception:
            answers = {}
    return {
        "application_id": row[0],
        "conversation_id": row[1],
        "vacancy_stable_id": row[2],
        "title": row[3],
        "employer": row[4],
        "state": row[5],
        "draft": row[6],
        "questionnaire_id": row[7],
        "answers": answers,
        "error": row[9],
        "last_transition_reason": row[10],
        "created_at": row[11],
        "updated_at": row[12],
    }


def get_hh_application(application_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve an HH application by its application_id."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT application_id, conversation_id, vacancy_stable_id, title, employer,
               state, draft, questionnaire_id, answers_json, error,
               last_transition_reason, created_at, updated_at
        FROM hh_applications WHERE application_id = ?
    ''', (str(application_id).strip(),))
    row = cur.fetchone()
    conn.close()
    return _row_to_application(row)


def get_hh_application_by_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the latest HH application for a conversation."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT application_id, conversation_id, vacancy_stable_id, title, employer,
               state, draft, questionnaire_id, answers_json, error,
               last_transition_reason, created_at, updated_at
        FROM hh_applications WHERE conversation_id = ? ORDER BY updated_at DESC LIMIT 1
    ''', (str(conversation_id).strip(),))
    row = cur.fetchone()
    conn.close()
    return _row_to_application(row)


def get_hh_application_by_vacancy(vacancy_stable_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the latest HH application for a vacancy."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT application_id, conversation_id, vacancy_stable_id, title, employer,
               state, draft, questionnaire_id, answers_json, error,
               last_transition_reason, created_at, updated_at
        FROM hh_applications WHERE vacancy_stable_id = ? ORDER BY updated_at DESC LIMIT 1
    ''', (str(vacancy_stable_id).strip(),))
    row = cur.fetchone()
    conn.close()
    return _row_to_application(row)


def list_hh_applications(state: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """List stored HH applications, optionally filtered by state."""
    conn = get_connection()
    cur = conn.cursor()
    if state:
        cur.execute('''
            SELECT application_id, conversation_id, vacancy_stable_id, title, employer,
                   state, draft, questionnaire_id, answers_json, error,
                   last_transition_reason, created_at, updated_at
            FROM hh_applications WHERE state = ? ORDER BY updated_at DESC LIMIT ?
        ''', (state, limit))
    else:
        cur.execute('''
            SELECT application_id, conversation_id, vacancy_stable_id, title, employer,
                   state, draft, questionnaire_id, answers_json, error,
                   last_transition_reason, created_at, updated_at
            FROM hh_applications ORDER BY updated_at DESC LIMIT ?
        ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return [_row_to_application(r) for r in rows if r is not None]


def save_hh_application_transition(data: Dict[str, Any]) -> int:
    """Save an audit record for an application state transition."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    evidence_json = data.get("evidence_json")
    if evidence_json is None and data.get("evidence") is not None:
        evidence_json = json.dumps(data["evidence"], ensure_ascii=False)
    now = datetime.utcnow().isoformat()
    created_at = data.get("created_at") or now

    cur.execute('''
        INSERT INTO hh_application_transitions (
            application_id, conversation_id, vacancy_stable_id,
            state, previous_state, reason, evidence_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        str(data["application_id"]).strip(),
        data.get("conversation_id"),
        data.get("vacancy_stable_id"),
        str(data["state"]).strip(),
        data.get("previous_state"),
        str(data["reason"]).strip(),
        evidence_json,
        created_at,
    ))
    trans_id = cur.lastrowid or 0
    conn.commit()
    conn.close()
    return trans_id


def list_hh_application_transitions(application_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """List transition history for an application."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, application_id, conversation_id, vacancy_stable_id,
               state, previous_state, reason, evidence_json, created_at
        FROM hh_application_transitions
        WHERE application_id = ? ORDER BY id ASC LIMIT ?
    ''', (str(application_id).strip(), limit))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        evidence = {}
        if r[7]:
            try:
                evidence = json.loads(r[7])
            except Exception:
                evidence = {"raw": r[7]}
        result.append({
            "id": r[0],
            "application_id": r[1],
            "conversation_id": r[2],
            "vacancy_stable_id": r[3],
            "state": r[4],
            "previous_state": r[5],
            "reason": r[6],
            "evidence": evidence,
            "created_at": r[8],
        })
    return result


# ---------------------------------------------------------------------------
# Stage 51 — Autonomous Operations DB Helpers
# ---------------------------------------------------------------------------

def save_autonomous_notification(data: Dict[str, Any]) -> int:
    """Save an autonomous notification."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    meta_json = json.dumps(data.get("metadata") or {}, ensure_ascii=False)
    now = datetime.utcnow().isoformat()
    created_at = data.get("created_at") or now

    cur.execute('''
        INSERT INTO autonomous_notifications (
            notification_type, priority, title, message, company,
            vacancy_title, vacancy_url, conversation_id, action_required,
            metadata_json, created_at, read
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        str(data.get("notification_type", "GENERAL")).strip(),
        str(data.get("priority", "NORMAL")).strip(),
        str(data.get("title", "")).strip(),
        str(data.get("message", "")).strip(),
        data.get("company"),
        data.get("vacancy_title"),
        data.get("vacancy_url"),
        data.get("conversation_id"),
        data.get("action_required"),
        meta_json,
        created_at,
        1 if data.get("read") else 0,
    ))
    notif_id = cur.lastrowid or 0
    conn.commit()
    conn.close()
    return notif_id


def list_autonomous_notifications(limit: int = 50, unread_only: bool = False) -> List[Dict[str, Any]]:
    """List recent autonomous notifications."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    query = '''
        SELECT id, notification_type, priority, title, message, company,
               vacancy_title, vacancy_url, conversation_id, action_required,
               metadata_json, created_at, read
        FROM autonomous_notifications
    '''
    params: List[Any] = []
    if unread_only:
        query += " WHERE read = 0"
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        meta = {}
        if r[10]:
            try:
                meta = json.loads(r[10])
            except Exception:
                meta = {"raw": r[10]}
        result.append({
            "id": r[0],
            "notification_type": r[1],
            "priority": r[2],
            "title": r[3],
            "message": r[4],
            "company": r[5],
            "vacancy_title": r[6],
            "vacancy_url": r[7],
            "conversation_id": r[8],
            "action_required": r[9],
            "metadata": meta,
            "created_at": r[11],
            "read": bool(r[12]),
        })
    return result


def save_interview_event(data: Dict[str, Any]) -> int:
    """Save a detected interview event."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    detected_at = data.get("detected_at") or now

    cur.execute('''
        INSERT INTO interview_events (
            conversation_id, vacancy_stable_id, company, vacancy_title,
            invitation_text, invitation_url, action_required, status,
            detected_at, notified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        str(data.get("conversation_id", "")).strip(),
        data.get("vacancy_stable_id"),
        str(data.get("company", "Unknown Employer")).strip(),
        str(data.get("vacancy_title", "Unknown Role")).strip(),
        str(data.get("invitation_text", "")).strip(),
        data.get("invitation_url"),
        data.get("action_required"),
        data.get("status", "NEW"),
        detected_at,
        1 if data.get("notified") else 0,
    ))
    event_id = cur.lastrowid or 0
    conn.commit()
    conn.close()
    return event_id


def list_interview_events(limit: int = 50) -> List[Dict[str, Any]]:
    """List recent interview events."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, conversation_id, vacancy_stable_id, company, vacancy_title,
               invitation_text, invitation_url, action_required, status,
               detected_at, notified
        FROM interview_events
        ORDER BY id DESC LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "conversation_id": r[1],
            "vacancy_stable_id": r[2],
            "company": r[3],
            "vacancy_title": r[4],
            "invitation_text": r[5],
            "invitation_url": r[6],
            "action_required": r[7],
            "status": r[8],
            "detected_at": r[9],
            "notified": bool(r[10]),
        }
        for r in rows
    ]


def save_autonomous_cycle_run(data: Dict[str, Any]) -> int:
    """Save an autonomous cycle execution summary."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    log_json = json.dumps(data.get("log") or {}, ensure_ascii=False)
    now = datetime.utcnow().isoformat()
    started_at = data.get("started_at") or now
    completed_at = data.get("completed_at") or now

    cur.execute('''
        INSERT INTO autonomous_cycle_runs (
            started_at, completed_at, status, discovered_count, matched_count,
            applied_count, verified_count, messages_checked, auto_replies_count,
            interviews_detected, rejections_count, unanswered_questions_count,
            summary, log_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        started_at,
        completed_at,
        str(data.get("status", "SUCCESS")).strip(),
        int(data.get("discovered_count", 0)),
        int(data.get("matched_count", 0)),
        int(data.get("applied_count", 0)),
        int(data.get("verified_count", 0)),
        int(data.get("messages_checked", 0)),
        int(data.get("auto_replies_count", 0)),
        int(data.get("interviews_detected", 0)),
        int(data.get("rejections_count", 0)),
        int(data.get("unanswered_questions_count", 0)),
        data.get("summary", ""),
        log_json,
    ))
    run_id = cur.lastrowid or 0
    conn.commit()
    conn.close()
    return run_id


def list_autonomous_cycle_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """List recent autonomous cycle runs."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, started_at, completed_at, status, discovered_count,
               matched_count, applied_count, verified_count, messages_checked,
               auto_replies_count, interviews_detected, rejections_count,
               unanswered_questions_count, summary, log_json
        FROM autonomous_cycle_runs
        ORDER BY id DESC LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        log_data = {}
        if r[14]:
            try:
                log_data = json.loads(r[14])
            except Exception:
                log_data = {"raw": r[14]}
        result.append({
            "id": r[0],
            "started_at": r[1],
            "completed_at": r[2],
            "status": r[3],
            "discovered_count": r[4],
            "matched_count": r[5],
            "applied_count": r[6],
            "verified_count": r[7],
            "messages_checked": r[8],
            "auto_replies_count": r[9],
            "interviews_detected": r[10],
            "rejections_count": r[11],
            "unanswered_questions_count": r[12],
            "summary": r[13],
            "log": log_data,
        })
    return result


def save_conversation_audit(data: Dict[str, Any]) -> int:
    """Save an autonomous conversation audit record."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    facts = data.get("profile_facts_used")
    if isinstance(facts, (list, dict)):
        facts_str = json.dumps(facts, ensure_ascii=False)
    else:
        facts_str = str(facts or "")

    now = data.get("created_at") or datetime.utcnow().isoformat()
    cur.execute('''
        INSERT INTO autonomous_conversation_audits (
            conversation_id, application_id, vacancy_id, vacancy_stable_id,
            employer, incoming_message, incoming_message_timestamp,
            message_classification, generated_reply, sent_reply,
            sent_at, profile_facts_used, decision_reason, status,
            error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get("conversation_id", ""),
        data.get("application_id"),
        data.get("vacancy_id"),
        data.get("vacancy_stable_id"),
        data.get("employer", ""),
        data.get("incoming_message", ""),
        data.get("incoming_message_timestamp"),
        data.get("message_classification", "UNKNOWN"),
        data.get("generated_reply"),
        data.get("sent_reply"),
        data.get("sent_at"),
        facts_str,
        data.get("decision_reason"),
        data.get("status", "GENERATED"),
        data.get("error"),
        now,
    ))
    audit_id = cur.lastrowid or 0
    conn.commit()
    conn.close()
    return audit_id


def list_conversation_audits(
    application_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List stored autonomous conversation audits with optional filtering."""
    import json
    conn = get_connection()
    cur = conn.cursor()

    query = '''
        SELECT id, conversation_id, application_id, vacancy_id, vacancy_stable_id,
               employer, incoming_message, incoming_message_timestamp,
               message_classification, generated_reply, sent_reply, sent_at,
               profile_facts_used, decision_reason, status, error, created_at
        FROM autonomous_conversation_audits
    '''
    params: List[Any] = []
    clauses = []
    if application_id:
        clauses.append("application_id = ?")
        params.append(str(application_id).strip())
    if conversation_id:
        clauses.append("conversation_id = ?")
        params.append(str(conversation_id).strip())
    if status:
        clauses.append("status = ?")
        params.append(str(status).strip())

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        facts_val = r[12]
        try:
            facts_parsed = json.loads(facts_val) if facts_val and facts_val.startswith(("[", "{")) else facts_val
        except Exception:
            facts_parsed = facts_val

        result.append({
            "id": r[0],
            "conversation_id": r[1],
            "application_id": r[2],
            "vacancy_id": r[3],
            "vacancy_stable_id": r[4],
            "employer": r[5],
            "incoming_message": r[6],
            "incoming_message_timestamp": r[7],
            "message_classification": r[8],
            "generated_reply": r[9],
            "sent_reply": r[10],
            "sent_at": r[11],
            "profile_facts_used": facts_parsed,
            "decision_reason": r[13],
            "status": r[14],
            "error": r[15],
            "created_at": r[16],
        })
    return result


def get_conversation_audit(audit_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve a single conversation audit by ID."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, conversation_id, application_id, vacancy_id, vacancy_stable_id,
               employer, incoming_message, incoming_message_timestamp,
               message_classification, generated_reply, sent_reply, sent_at,
               profile_facts_used, decision_reason, status, error, created_at
        FROM autonomous_conversation_audits
        WHERE id = ?
    ''', (audit_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    facts_val = row[12]
    try:
        facts_parsed = json.loads(facts_val) if facts_val and facts_val.startswith(("[", "{")) else facts_val
    except Exception:
        facts_parsed = facts_val

    return {
        "id": row[0],
        "conversation_id": row[1],
        "application_id": row[2],
        "vacancy_id": row[3],
        "vacancy_stable_id": row[4],
        "employer": row[5],
        "incoming_message": row[6],
        "incoming_message_timestamp": row[7],
        "message_classification": row[8],
        "generated_reply": row[9],
        "sent_reply": row[10],
        "sent_at": row[11],
        "profile_facts_used": facts_parsed,
        "decision_reason": row[13],
        "status": row[14],
        "error": row[15],
        "created_at": row[16],
    }


def is_telegram_delivered(delivery_key: str) -> bool:
    """Check if a notification or message event has already been delivered to Telegram."""
    if not delivery_key:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM telegram_delivery_records WHERE delivery_key = ?", (str(delivery_key),))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def record_telegram_delivery(
    delivery_key: str,
    notification_type: str,
    chat_id: str,
    status: str = "DELIVERED",
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """Record a delivery event to Telegram to ensure strict idempotency."""
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload_str = json.dumps(payload, ensure_ascii=False) if payload else None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO telegram_delivery_records
            (delivery_key, notification_type, chat_id, delivered_at, status, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(delivery_key) DO UPDATE SET
            delivered_at = excluded.delivered_at,
            status = excluded.status,
            payload = excluded.payload
    ''', (str(delivery_key), str(notification_type), str(chat_id), now_iso, str(status), payload_str))
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return record_id


def list_telegram_delivery_records(status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """List telegram delivery records from database."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT id, delivery_key, notification_type, chat_id, delivered_at, status, payload FROM telegram_delivery_records"
    params: List[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(str(status).strip())
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        payload_data = {}
        if r[6]:
            try:
                payload_data = json.loads(r[6])
            except Exception:
                payload_data = {"raw": r[6]}
        result.append({
            "id": r[0],
            "delivery_key": r[1],
            "notification_type": r[2],
            "chat_id": r[3],
            "delivered_at": r[4],
            "status": r[5],
            "payload": payload_data,
        })
    return result


def update_telegram_delivery_status(record_id: int, status: str, payload_update: Optional[Dict[str, Any]] = None) -> bool:
    """Update status and payload for a telegram delivery record."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    if payload_update is not None:
        cur.execute("SELECT payload FROM telegram_delivery_records WHERE id = ?", (record_id,))
        row = cur.fetchone()
        existing_payload = {}
        if row and row[0]:
            try:
                existing_payload = json.loads(row[0])
            except Exception:
                existing_payload = {}
        existing_payload.update(payload_update)
        new_payload_str = json.dumps(existing_payload, ensure_ascii=False)
        cur.execute("UPDATE telegram_delivery_records SET status = ?, payload = ? WHERE id = ?", (str(status).strip(), new_payload_str, record_id))
    else:
        cur.execute("UPDATE telegram_delivery_records SET status = ? WHERE id = ?", (str(status).strip(), record_id))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def is_digest_delivered(vacancy_stable_id: str, canonical_id: Optional[str] = None) -> bool:
    """Check if a vacancy has already been included in a successfully delivered Telegram digest."""
    if not vacancy_stable_id:
        return False
    keys = [f"digest:{vacancy_stable_id}"]
    if canonical_id:
        keys.append(f"digest:{canonical_id}")
    
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in keys)
    cur.execute(f"SELECT 1 FROM telegram_delivery_records WHERE delivery_key IN ({placeholders}) AND status = 'DELIVERED'", tuple(keys))
    row = cur.fetchone()
    conn.close()
    return bool(row)

is_vacancy_delivered_in_digest = is_digest_delivered


def compute_digest_batch_key(vacancy_ids: List[str]) -> str:
    """Compute deterministic batch key from sorted vacancy IDs."""
    import hashlib
    clean_ids = sorted([str(vid).strip() for vid in vacancy_ids if vid and str(vid).strip()])
    joined = "|".join(clean_ids)
    h = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    return f"digest_batch:{h}"


def record_digest_attempt(vacancy_ids: List[str], chat_id: str = "-1004399255305") -> Optional[str]:
    """DURABLY record/acquire in-flight delivery attempt BEFORE external Telegram API side-effect.
    
    Returns batch_key if this process successfully claimed exclusive attempt permission.
    Returns None if the attempt is already ATTEMPTING, DELIVERED, or AMBIGUOUS (concurrency protection).
    """
    if not vacancy_ids:
        return None
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    batch_key = compute_digest_batch_key(vacancy_ids)
    
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    
    # Check if batch already exists in an active/non-failed state
    cur.execute("SELECT status, payload FROM telegram_delivery_records WHERE delivery_key = ?", (batch_key,))
    existing = cur.fetchone()
    if existing:
        curr_status = existing[0]
        if curr_status in ("ATTEMPTING", "DELIVERED", "AMBIGUOUS"):
            # Already in-flight or completed by another process -> DO NOT acquire duplicate permission
            conn.close()
            return None
        # If FAILED, we are attempting a retry: atomically update
        batch_payload = json.dumps({
            "vacancies": vacancy_ids,
            "chat_id": chat_id,
            "attempted_at": now_iso,
            "last_updated_at": now_iso,
        })
        cur.execute('''
            UPDATE telegram_delivery_records
            SET status = 'ATTEMPTING', delivered_at = ?, payload = ?
            WHERE delivery_key = ? AND status = 'FAILED'
        ''', (now_iso, batch_payload, batch_key))
        if cur.rowcount == 0:
            # Lost race with another worker
            conn.close()
            return None
    else:
        # First-time attempt: try to insert
        batch_payload = json.dumps({
            "vacancies": vacancy_ids,
            "chat_id": chat_id,
            "attempted_at": now_iso,
            "last_updated_at": now_iso,
        })
        try:
            cur.execute('''
                INSERT INTO telegram_delivery_records
                    (delivery_key, notification_type, chat_id, delivered_at, status, payload)
                VALUES (?, 'digest_batch', ?, ?, 'ATTEMPTING', ?)
            ''', (batch_key, str(chat_id), now_iso, batch_payload))
        except Exception:
            # Unique constraint race lost
            conn.close()
            return None

    # Record vacancy-level in-flight attempts
    for vid in vacancy_ids:
        if not vid:
            continue
        deliv_key = f"digest:{vid}"
        vac_payload = json.dumps({"batch_key": batch_key, "attempted_at": now_iso, "last_updated_at": now_iso})
        cur.execute('''
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                delivered_at = excluded.delivered_at,
                status = 'ATTEMPTING',
                payload = excluded.payload
        ''', (deliv_key, "job_digest", str(chat_id), now_iso, "ATTEMPTING", vac_payload))
        
    conn.commit()
    conn.close()
    return batch_key


def mark_digest_delivered(vacancy_ids: List[str], batch_key: Optional[str] = None, chat_id: str = "-1004399255305") -> int:
    """Atomically record successful digest delivery for vacancy stable_ids and associated batch."""
    if not vacancy_ids:
        return 0
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not batch_key:
        batch_key = compute_digest_batch_key(vacancy_ids)

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    count = 0
    for vid in vacancy_ids:
        if not vid:
            continue
        deliv_key = f"digest:{vid}"
        vac_payload = json.dumps({"batch_key": batch_key, "delivered_at": now_iso, "last_updated_at": now_iso})
        cur.execute('''
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                delivered_at = excluded.delivered_at,
                status = 'DELIVERED',
                payload = excluded.payload
        ''', (deliv_key, "job_digest", str(chat_id), now_iso, "DELIVERED", vac_payload))
        count += 1
        
    # Mark batch DELIVERED
    if batch_key:
        batch_payload = json.dumps({"vacancies": vacancy_ids, "delivered_at": now_iso, "last_updated_at": now_iso})
        cur.execute('''
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                delivered_at = excluded.delivered_at,
                status = 'DELIVERED',
                payload = excluded.payload
        ''', (batch_key, "digest_batch", str(chat_id), now_iso, "DELIVERED", batch_payload))

    conn.commit()
    conn.close()
    return count


def record_digest_failed(vacancy_ids: List[str], batch_key: Optional[str] = None, chat_id: str = "-1004399255305", error: str = "") -> int:
    """Record confirmed Telegram delivery failure so vacancies remain retryable."""
    if not vacancy_ids:
        return 0
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not batch_key:
        batch_key = compute_digest_batch_key(vacancy_ids)

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    count = 0
    err_payload = json.dumps({"batch_key": batch_key, "error": error, "failed_at": now_iso, "last_updated_at": now_iso})
    for vid in vacancy_ids:
        if not vid:
            continue
        deliv_key = f"digest:{vid}"
        cur.execute('''
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                delivered_at = excluded.delivered_at,
                status = 'FAILED',
                payload = excluded.payload
        ''', (deliv_key, "job_digest", str(chat_id), now_iso, "FAILED", err_payload))
        count += 1

    if batch_key:
        batch_err_payload = json.dumps({"vacancies": vacancy_ids, "error": error, "failed_at": now_iso, "last_updated_at": now_iso})
        cur.execute('''
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                delivered_at = excluded.delivered_at,
                status = 'FAILED',
                payload = excluded.payload
        ''', (batch_key, "digest_batch", str(chat_id), now_iso, "FAILED", batch_err_payload))

    conn.commit()
    conn.close()
    return count


def record_digest_ambiguous(vacancy_ids: List[str], batch_key: Optional[str] = None, chat_id: str = "-1004399255305", reason: str = "") -> int:
    """Record ambiguous / interrupted attempt so vacancies are protected against duplicate send."""
    if not vacancy_ids:
        return 0
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not batch_key:
        batch_key = compute_digest_batch_key(vacancy_ids)

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    count = 0
    amb_payload = json.dumps({"batch_key": batch_key, "reason": reason, "ambiguous_at": now_iso, "last_updated_at": now_iso})
    for vid in vacancy_ids:
        if not vid:
            continue
        deliv_key = f"digest:{vid}"
        cur.execute('''
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                delivered_at = excluded.delivered_at,
                status = 'AMBIGUOUS',
                payload = excluded.payload
        ''', (deliv_key, "job_digest", str(chat_id), now_iso, "AMBIGUOUS", amb_payload))
        count += 1

    if batch_key:
        batch_amb_payload = json.dumps({"vacancies": vacancy_ids, "reason": reason, "ambiguous_at": now_iso, "last_updated_at": now_iso})
        cur.execute('''
            INSERT INTO telegram_delivery_records
                (delivery_key, notification_type, chat_id, delivered_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                delivered_at = excluded.delivered_at,
                status = 'AMBIGUOUS',
                payload = excluded.payload
        ''', (batch_key, "digest_batch", str(chat_id), now_iso, "AMBIGUOUS", batch_amb_payload))

    conn.commit()
    conn.close()
    return count


def list_digest_attempts(limit: int = 50, now_dt: Optional[Any] = None) -> List[Dict[str, Any]]:
    """List digest delivery attempts from telegram_delivery_records with stale evaluation."""
    from .config import DIGEST_ATTEMPT_STALE_MINUTES
    import datetime
    
    if now_dt is None:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now_dt, str):
        now_dt = datetime.datetime.fromisoformat(now_dt.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, delivery_key, chat_id, delivered_at, status, payload
        FROM telegram_delivery_records
        WHERE notification_type = 'digest_batch'
        ORDER BY id DESC
        LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    
    stale_threshold_seconds = max(1, DIGEST_ATTEMPT_STALE_MINUTES) * 60
    results = []
    for r in rows:
        payload = {}
        if r[5]:
            try:
                payload = json.loads(r[5])
            except Exception:
                payload = {"raw": r[5]}
        vac_list = payload.get("vacancies", [])
        
        # Calculate age
        # delivered_at is the authoritative start time for the current
        # persisted attempt. It is refreshed atomically when a failed batch is
        # retried; payload timestamps are diagnostic and may be stale.
        attempted_at_str = r[3] or payload.get("attempted_at")
        age_seconds = 0
        try:
            att_dt = datetime.datetime.fromisoformat(attempted_at_str.replace("Z", "+00:00"))
            if att_dt.tzinfo is None:
                att_dt = att_dt.replace(tzinfo=datetime.timezone.utc)
            age_seconds = max(0, int((now_dt - att_dt).total_seconds()))
        except Exception:
            pass

        persisted_status = r[4]
        is_stale = (persisted_status == "ATTEMPTING" and age_seconds >= stale_threshold_seconds)
        
        effective_status = persisted_status
        if is_stale:
            effective_status = "STALE"
            
        retry_permitted = (persisted_status == "FAILED")
        requires_reconciliation = is_stale or (persisted_status in ("AMBIGUOUS", "ATTEMPTING"))

        results.append({
            "id": r[0],
            "batch_key": r[1],
            "chat_id": r[2],
            "timestamp": r[3],
            "created_at": attempted_at_str,
            "last_updated_at": payload.get("last_updated_at", r[3]),
            "status": persisted_status,
            "effective_status": effective_status,
            "age_seconds": age_seconds,
            "age_minutes": round(age_seconds / 60.0, 1),
            "stale": is_stale,
            "vacancy_count": len(vac_list),
            "vacancies": vac_list,
            "retry_permitted": retry_permitted,
            "requires_reconciliation": requires_reconciliation,
            "payload": payload,
        })
    return results


def reconcile_digest_attempt(batch_key: str, new_status: str, chat_id: str = "-1004399255305") -> bool:
    """Manually reconcile an unresolved digest batch and its associated vacancies. Fails closed if batch does not exist."""
    if new_status not in ("DELIVERED", "FAILED", "AMBIGUOUS"):
        raise ValueError(f"Invalid status: {new_status}. Must be DELIVERED, FAILED, or AMBIGUOUS.")
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT payload FROM telegram_delivery_records WHERE delivery_key = ? AND notification_type = 'digest_batch'", (batch_key,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise KeyError(f"Batch key '{batch_key}' not found in database.")

    vac_list = []
    if row and row[0]:
        try:
            p = json.loads(row[0])
            vac_list = p.get("vacancies", [])
        except Exception:
            pass

    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # Update batch
    updated_payload = json.dumps({"vacancies": vac_list, "reconciled_to": new_status, "reconciled_at": now_iso, "last_updated_at": now_iso})
    cur.execute('''
        UPDATE telegram_delivery_records
        SET status = ?, delivered_at = ?, payload = ?
        WHERE delivery_key = ?
    ''', (new_status, now_iso, updated_payload, batch_key))
    
    # Update individual vacancies
    for vid in vac_list:
        deliv_key = f"digest:{vid}"
        vac_payload = json.dumps({"batch_key": batch_key, "reconciled_to": new_status, "reconciled_at": now_iso, "last_updated_at": now_iso})
        cur.execute('''
            UPDATE telegram_delivery_records
            SET status = ?, delivered_at = ?, payload = ?
            WHERE delivery_key = ?
        ''', (new_status, now_iso, vac_payload, deliv_key))
        
    conn.commit()
    conn.close()
    return True


def list_undigested_vacancies(limit: int = 5000) -> List[Any]:
    """List fresh vacancies from state.db excluding legacy baseline and synthetic/test artifacts."""
    from .schema import is_genuine_production_vacancy
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT v.* FROM vacancies v
        LEFT JOIN telegram_delivery_records t
            ON t.delivery_key = 'digest:' || v.stable_id
        WHERE v.source != 'vacancies_json'
          AND (t.id IS NULL OR t.status = 'FAILED')
        ORDER BY v.first_seen_at DESC
        LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    vacancies = [_row_to_vacancy(r) for r in rows if r]
    return [v for v in vacancies if is_genuine_production_vacancy(v)[0]]


def get_production_health(now_dt: Optional[Any] = None, storage_dir: Optional[str] = None) -> Dict[str, Any]:
    """Inspect production state and evaluate overall operational health (Stage 83)."""
    from .config import DB_FILE, PRODUCTION_FAILURE_ALERT_THRESHOLD
    import datetime
    
    if now_dt is None:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now_dt, str):
        now_dt = datetime.datetime.fromisoformat(now_dt.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)

    now_iso = now_dt.isoformat()
    health_result: Dict[str, Any] = {
        "db_path": DB_FILE,
        "db_accessible": False,
        "health": "HEALTHY",
        "alerts": [],
        "metrics": {
            "total_delivery_records": 0,
            "job_digest_records": 0,
            "digest_batch_records": 0,
            "attempting_count": 0,
            "stale_count": 0,
            "ambiguous_count": 0,
            "failed_count": 0,
            "delivered_count": 0,
            "duplicate_delivery_keys_count": 0,
            "consecutive_failures": 0,
            "last_production_error": None,
            "last_digest_attempt_at": None,
            "last_successful_digest_at": None,
            "pending_undigested_vacancies_count": 0,
        },
        "evaluation_timestamp": now_iso,
    }

    # 1. Check DB accessibility
    try:
        init_db()
        conn = get_connection()
        cur = conn.cursor()
        health_result["db_accessible"] = True
    except Exception as db_err:
        health_result["health"] = "UNHEALTHY"
        health_result["alerts"].append({
            "severity": "CRITICAL",
            "message": f"Database inaccessible: {db_err}",
            "timestamp": now_iso,
        })
        return health_result

    # 2. Query duplicate delivery keys
    try:
        cur.execute("SELECT delivery_key, COUNT(*) FROM telegram_delivery_records GROUP BY delivery_key HAVING COUNT(*) > 1")
        dups = cur.fetchall()
        health_result["metrics"]["duplicate_delivery_keys_count"] = len(dups)
        if dups:
            health_result["health"] = "UNHEALTHY"
            health_result["alerts"].append({
                "severity": "CRITICAL",
                "message": f"Duplicate delivery keys detected ({len(dups)} duplicates).",
                "timestamp": now_iso,
                "diagnostics": [d[0] for d in dups[:5]],
            })
    except Exception:
        pass

    # 3. Query attempt states
    attempts = list_digest_attempts(limit=50, now_dt=now_dt)
    attempting_cnt = 0
    stale_cnt = 0
    ambiguous_cnt = 0
    failed_cnt = 0
    delivered_cnt = 0
    last_attempt_ts = None
    last_delivered_ts = None

    for att in attempts:
        eff = att.get("effective_status")
        pers = att.get("status")
        if not last_attempt_ts and att.get("created_at"):
            last_attempt_ts = att.get("created_at")

        if eff == "STALE":
            stale_cnt += 1
        elif pers == "ATTEMPTING":
            attempting_cnt += 1
        elif pers == "AMBIGUOUS":
            ambiguous_cnt += 1
        elif pers == "FAILED":
            failed_cnt += 1
        elif pers == "DELIVERED":
            delivered_cnt += 1
            if not last_delivered_ts and att.get("timestamp"):
                last_delivered_ts = att.get("timestamp")

    health_result["metrics"]["attempting_count"] = attempting_cnt
    health_result["metrics"]["stale_count"] = stale_cnt
    health_result["metrics"]["ambiguous_count"] = ambiguous_cnt
    health_result["metrics"]["failed_count"] = failed_cnt
    health_result["metrics"]["delivered_count"] = delivered_cnt
    health_result["metrics"]["last_digest_attempt_at"] = last_attempt_ts
    health_result["metrics"]["last_successful_digest_at"] = last_delivered_ts

    # 4. Total record counts
    try:
        cur.execute("SELECT COUNT(*) FROM telegram_delivery_records")
        health_result["metrics"]["total_delivery_records"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM telegram_delivery_records WHERE notification_type = 'job_digest'")
        health_result["metrics"]["job_digest_records"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM telegram_delivery_records WHERE notification_type = 'digest_batch'")
        health_result["metrics"]["digest_batch_records"] = cur.fetchone()[0]
    except Exception:
        pass

    # 5. Pending undigested vacancies count
    try:
        undigested = list_undigested_vacancies(limit=1000)
        health_result["metrics"]["pending_undigested_vacancies_count"] = len(undigested)
    except Exception:
        pass

    conn.close()

    # 6. Consecutive failures tracker
    from .runner import ConsecutiveFailureTracker
    tracker = ConsecutiveFailureTracker(storage_dir=storage_dir)
    consec_fails = tracker.get_consecutive_failures()
    tracker_status = tracker.get_status()
    health_result["metrics"]["consecutive_failures"] = consec_fails
    health_result["metrics"]["last_production_error"] = tracker_status.get("last_error")

    # 7. Evaluate Health & Alert Rules
    # Rule A: Critical / Unhealthy if AMBIGUOUS, STALE, or Duplicate keys exist
    if ambiguous_cnt > 0:
        health_result["health"] = "UNHEALTHY"
        health_result["alerts"].append({
            "severity": "CRITICAL",
            "message": f"Ambiguous digest delivery attempts detected ({ambiguous_cnt}). Manual operator reconciliation required.",
            "timestamp": now_iso,
        })
    if stale_cnt > 0:
        health_result["health"] = "UNHEALTHY"
        health_result["alerts"].append({
            "severity": "CRITICAL",
            "message": f"Stale unresolved digest attempts detected ({stale_cnt}). Manual operator reconciliation required.",
            "timestamp": now_iso,
        })

    # Rule B: Repeated failures threshold
    if consec_fails >= PRODUCTION_FAILURE_ALERT_THRESHOLD:
        health_result["health"] = "UNHEALTHY"
        health_result["alerts"].append({
            "severity": "CRITICAL",
            "message": f"Consecutive production run failures ({consec_fails}) reached alert threshold ({PRODUCTION_FAILURE_ALERT_THRESHOLD}).",
            "timestamp": now_iso,
        })
    elif consec_fails > 0 or failed_cnt > 0:
        # Isolated failure awaiting retry -> DEGRADED
        if health_result["health"] != "UNHEALTHY":
            health_result["health"] = "DEGRADED"
            health_result["alerts"].append({
                "severity": "WARNING",
                "message": (
                    f"Production run failure recorded: {tracker_status.get('last_error')}"
                    if consec_fails > 0 and tracker_status.get("last_error")
                    else "Isolated digest delivery failure recorded. Automatic retry will occur on next scheduled run."
                ),
                "timestamp": now_iso,
            })

    # 8. Check Hermes Integration Health (Stage 89.2)
    try:
        from .hermes_integration import get_hermes_integration_status
        h_status = get_hermes_integration_status()
        health_result["hermes_integration"] = h_status
        if h_status["status"] == "DRIFTED":
            if health_result["health"] == "HEALTHY":
                health_result["health"] = "DEGRADED"
            health_result["alerts"].append({
                "severity": "WARNING",
                "message": "Hermes external integration is DRIFTED. Run 'python -m ai_assistant.cli hermes sync' to reconcile.",
                "timestamp": now_iso,
            })
        elif h_status["status"] == "MISSING":
            if health_result["health"] == "HEALTHY":
                health_result["health"] = "DEGRADED"
            health_result["alerts"].append({
                "severity": "WARNING",
                "message": "Hermes external runtime files are MISSING. Run 'python -m ai_assistant.cli hermes sync' to deploy.",
                "timestamp": now_iso,
            })
    except Exception:
        pass

    return health_result






# ---------------------------------------------------------------------------
# Stage 89: Telegram Feedback Records & Helpers
# ---------------------------------------------------------------------------

def record_telegram_feedback(
    vacancy_stable_id: str,
    action: str,
    telegram_user_id: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    callback_query_id: Optional[str] = None,
    previous_status: Optional[str] = None,
    new_status: Optional[str] = None,
    payload_json: Optional[str] = None,
    created_at: Optional[str] = None,
) -> int:
    """Record a Telegram feedback callback event into the audit trail."""
    if is_dry_run():
        return 0
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    now_iso = created_at or datetime.utcnow().isoformat()
    cur.execute('''
        INSERT INTO telegram_feedback_records (
            vacancy_stable_id, action, telegram_user_id, telegram_chat_id,
            callback_query_id, previous_status, new_status, created_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        vacancy_stable_id,
        action,
        str(telegram_user_id) if telegram_user_id is not None else None,
        str(telegram_chat_id) if telegram_chat_id is not None else None,
        str(callback_query_id) if callback_query_id is not None else None,
        previous_status,
        new_status,
        now_iso,
        payload_json,
    ))
    rec_id = cur.lastrowid
    conn.commit()
    conn.close()
    return rec_id


def list_telegram_feedback(limit: int = 50, vacancy_stable_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List recent Telegram feedback records."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    if vacancy_stable_id:
        cur.execute('''
            SELECT id, vacancy_stable_id, action, telegram_user_id, telegram_chat_id,
                   callback_query_id, previous_status, new_status, created_at, payload_json
            FROM telegram_feedback_records
            WHERE vacancy_stable_id = ?
            ORDER BY id DESC LIMIT ?
        ''', (vacancy_stable_id, limit))
    else:
        cur.execute('''
            SELECT id, vacancy_stable_id, action, telegram_user_id, telegram_chat_id,
                   callback_query_id, previous_status, new_status, created_at, payload_json
            FROM telegram_feedback_records
            ORDER BY id DESC LIMIT ?
        ''', (limit,))
    rows = cur.fetchall()
    conn.close()
    results = []
    for r in rows:
        results.append({
            "id": r[0],
            "vacancy_stable_id": r[1],
            "action": r[2],
            "telegram_user_id": r[3],
            "telegram_chat_id": r[4],
            "callback_query_id": r[5],
            "previous_status": r[6],
            "new_status": r[7],
            "created_at": r[8],
            "payload_json": r[9],
        })
    return results


def get_telegram_feedback_summary() -> Dict[str, Any]:
    """Produce read-only aggregation of recorded Telegram feedback (Stage 89)."""
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT action, COUNT(*) FROM telegram_feedback_records GROUP BY action
    ''')
    action_counts = {r[0]: r[1] for r in cur.fetchall()}

    # Group by role_family / company / source by joining with vacancies
    cur.execute('''
        SELECT f.action, v.source, v.company, v.match_decision, COUNT(*)
        FROM telegram_feedback_records f
        LEFT JOIN vacancies v ON v.stable_id = f.vacancy_stable_id
        GROUP BY f.action, v.source, v.company, v.match_decision
    ''')
    breakdown_rows = cur.fetchall()
    conn.close()

    return {
        "total_feedbacks": sum(action_counts.values()),
        "action_counts": {
            "INTERESTED": action_counts.get("INTERESTED", 0),
            "NOT_INTERESTED": action_counts.get("NOT_INTERESTED", 0),
            "PREPARE_APPLICATION": action_counts.get("PREPARE_APPLICATION", 0),
            "SKIP": action_counts.get("SKIP", 0),
        },
        "breakdown": [
            {
                "action": r[0],
                "source": r[1] or "UNKNOWN",
                "company": r[2] or "UNKNOWN",
                "decision": r[3] or "UNKNOWN",
                "count": r[4],
            }
            for r in breakdown_rows
        ],
    }


def resolve_vacancy_by_hash_prefix(hash_prefix: str) -> Optional[str]:
    """Resolve a vacancy stable_id by its SHA256 hash prefix for compact Telegram callbacks.
    
    Fail-closed collision safety:
    - Rejects empty or short prefixes (< 8 hex characters).
    - Returns stable_id if and only if exactly ONE matching vacancy is found.
    - If 0 matches or >1 matches (collision) are found, returns None.
    """
    if not hash_prefix or len(hash_prefix) < 8:
        return None
    import hashlib
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT stable_id FROM vacancies")
    rows = cur.fetchall()
    conn.close()
    
    matches = [
        sid for (sid,) in rows
        if sid and hashlib.sha256(sid.encode("utf-8")).hexdigest().startswith(hash_prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    return None

get_vacancy = get_vacancy_by_id
