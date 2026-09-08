import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from . import config
from .db_schema import apply_schema, schema_fingerprint
from .schema import Vacancy

logger = logging.getLogger(__name__)

_DRY_RUN = False


def set_dry_run(enabled: bool) -> None:
    global _DRY_RUN
    _DRY_RUN = bool(enabled)


def is_dry_run() -> bool:
    return _DRY_RUN


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(config.DB_FILE)


def ensure_schema() -> None:
    """Накатывает схему, если она ещё не актуальна.

    143 вызова init_db() за прогон больше не прогоняют 84 DDL-оператора каждый:
    сверяется fingerprint схемы, и если он совпал — выходим сразу.
    """
    conn = get_connection()
    try:
        _ensure_schema_on(conn)
    finally:
        conn.close()


def _ensure_schema_on(conn: sqlite3.Connection) -> None:
    fingerprint = schema_fingerprint()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_meta (
            id INTEGER PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    row = conn.execute("SELECT fingerprint FROM _schema_meta WHERE id = 1").fetchone()
    if row is not None and row[0] == fingerprint:
        return
    apply_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO _schema_meta (id, fingerprint, applied_at) VALUES (1, ?, ?)",
        (fingerprint, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def init_db() -> None:
    """Обратная совместимость: тонкая обёртка над ensure_schema()."""
    ensure_schema()


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
    import datetime as _dt
    import json
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


def get_vacancy_eligibility(vacancy_stable_id: str) -> dict[str, Any] | None:
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


def get_all_vacancy_eligibilities() -> dict[str, dict[str, Any]]:
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
        import json

        from .submission_verifier import SubmissionVerification
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


def get_hh_message_event(message_fingerprint: str) -> dict[str, Any] | None:
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


def list_hh_message_events(conversation_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
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

def save_hh_questionnaire(data: dict[str, Any]) -> None:
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


def _row_to_questionnaire(row: Any) -> dict[str, Any] | None:
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


def get_hh_questionnaire(questionnaire_id: str) -> dict[str, Any] | None:
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


def get_hh_questionnaire_by_vacancy(vacancy_stable_id: str) -> dict[str, Any] | None:
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


def get_hh_questionnaire_by_conversation(conversation_id: str) -> dict[str, Any] | None:
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


def list_hh_questionnaires(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
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
    answers: dict[str, Any],
    new_status: str | None = None,
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

def save_hh_application(data: dict[str, Any]) -> None:
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


def _row_to_application(row: Any) -> dict[str, Any] | None:
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


def get_hh_application(application_id: str) -> dict[str, Any] | None:
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


def get_hh_application_by_conversation(conversation_id: str) -> dict[str, Any] | None:
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


def get_hh_application_by_vacancy(vacancy_stable_id: str) -> dict[str, Any] | None:
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


def list_hh_applications(state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
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


def save_hh_application_transition(data: dict[str, Any]) -> int:
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


def list_hh_application_transitions(application_id: str, limit: int = 100) -> list[dict[str, Any]]:
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

def save_autonomous_notification(data: dict[str, Any]) -> int:
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


def list_autonomous_notifications(limit: int = 50, unread_only: bool = False) -> list[dict[str, Any]]:
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
    params: list[Any] = []
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


def save_interview_event(data: dict[str, Any]) -> int:
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


def list_interview_events(limit: int = 50) -> list[dict[str, Any]]:
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


def save_autonomous_cycle_run(data: dict[str, Any]) -> int:
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


def list_autonomous_cycle_runs(limit: int = 20) -> list[dict[str, Any]]:
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


def save_conversation_audit(data: dict[str, Any]) -> int:
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
    application_id: str | None = None,
    conversation_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
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
    params: list[Any] = []
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


def get_conversation_audit(audit_id: int) -> dict[str, Any] | None:
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
    payload: dict[str, Any] | None = None,
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


def list_telegram_delivery_records(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """List telegram delivery records from database."""
    import json
    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT id, delivery_key, notification_type, chat_id, delivered_at, status, payload FROM telegram_delivery_records"
    params: list[Any] = []
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


def update_telegram_delivery_status(record_id: int, status: str, payload_update: dict[str, Any] | None = None) -> bool:
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


def is_digest_delivered(vacancy_stable_id: str, canonical_id: str | None = None) -> bool:
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


def compute_digest_batch_key(vacancy_ids: list[str]) -> str:
    """Compute deterministic batch key from sorted vacancy IDs."""
    import hashlib
    clean_ids = sorted([str(vid).strip() for vid in vacancy_ids if vid and str(vid).strip()])
    joined = "|".join(clean_ids)
    h = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    return f"digest_batch:{h}"


def record_digest_attempt(vacancy_ids: list[str], chat_id: str = "-1004399255305") -> str | None:
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


def mark_digest_delivered(vacancy_ids: list[str], batch_key: str | None = None, chat_id: str = "-1004399255305") -> int:
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


def record_digest_failed(vacancy_ids: list[str], batch_key: str | None = None, chat_id: str = "-1004399255305", error: str = "") -> int:
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


def record_digest_ambiguous(vacancy_ids: list[str], batch_key: str | None = None, chat_id: str = "-1004399255305", reason: str = "") -> int:
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


def list_digest_attempts(limit: int = 50, now_dt: Any | None = None) -> list[dict[str, Any]]:
    """List digest delivery attempts from telegram_delivery_records with stale evaluation."""
    import datetime

    from .config import DIGEST_ATTEMPT_STALE_MINUTES
    
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


def list_undigested_vacancies(limit: int = 5000) -> list[Any]:
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


def get_production_health(now_dt: Any | None = None, storage_dir: str | None = None) -> dict[str, Any]:
    """Inspect production state and evaluate overall operational health (Stage 83)."""
    import datetime

    from .config import DB_FILE, PRODUCTION_FAILURE_ALERT_THRESHOLD
    
    if now_dt is None:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now_dt, str):
        now_dt = datetime.datetime.fromisoformat(now_dt.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)

    now_iso = now_dt.isoformat()
    health_result: dict[str, Any] = {
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
            "production_circuit_open": False,
            "production_circuit_threshold": PRODUCTION_FAILURE_ALERT_THRESHOLD,
            "production_circuit_opened_at": None,
            "last_operator_resume_at": None,
            "last_digest_attempt_at": None,
            "last_successful_digest_at": None,
            "pending_undigested_vacancies_count": 0,
        },
        "evaluation_timestamp": now_iso,
    }

    # 1. Check DB accessibility
    try:
        # ensure_schema, не init_db: схема нужна для запросов ниже, а гонять 84
        # DDL-оператора ради проверки доступности незачем. SELECT 1 — настоящая
        # проба: sqlite3.connect ленивый и на отсутствующий файл не ругается.
        ensure_schema()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
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
    health_result["metrics"]["production_circuit_open"] = tracker_status.get("circuit_open", False)
    health_result["metrics"]["production_circuit_opened_at"] = tracker_status.get("circuit_opened_at")
    health_result["metrics"]["last_operator_resume_at"] = tracker_status.get("last_operator_resume_at")

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
    if tracker_status.get("circuit_open", False):
        health_result["health"] = "UNHEALTHY"
        health_result["alerts"].append({
            "severity": "CRITICAL",
            "message": (
                f"Production circuit is OPEN after {consec_fails} consecutive failures "
                f"(configured threshold: {PRODUCTION_FAILURE_ALERT_THRESHOLD}). Live runs are blocked until "
                "operator review and `production-control resume`."
            ),
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
    telegram_user_id: str | None = None,
    telegram_chat_id: str | None = None,
    callback_query_id: str | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
    payload_json: str | None = None,
    created_at: str | None = None,
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


def list_telegram_feedback(limit: int = 50, vacancy_stable_id: str | None = None) -> list[dict[str, Any]]:
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


def get_telegram_feedback_summary() -> dict[str, Any]:
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


def resolve_vacancy_by_hash_prefix(hash_prefix: str) -> str | None:
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


def get_system_setting(key: str, default: str | None = None) -> str | None:
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    return default


def set_system_setting(key: str, value: str) -> None:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )
    conn.commit()
    conn.close()


def is_submit_paused() -> bool:
    return get_system_setting("submit_paused") == "1"


def set_submit_paused(paused: bool) -> None:
    set_system_setting("submit_paused", "1" if paused else "0")


def count_submitted_transitions_since(since_iso: str) -> int:
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM hh_application_transitions WHERE state = 'SUBMITTED' AND created_at >= ?",
        (since_iso,),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


# --- Phase 2.1: Exclusive Submission Claims ---

class SubmissionClaimStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    ATTEMPTING = "ATTEMPTING"
    SUBMITTED = "SUBMITTED"
    FAILED_SAFE = "FAILED_SAFE"
    AMBIGUOUS = "AMBIGUOUS"


def acquire_submission_claim(
    vacancy_stable_id: str,
    application_id: str,
    worker_id: str = "default_worker",
    claim_id: str | None = None,
    details: dict[str, Any] | None = None,
    allow_reclaim_failed_safe: bool = False,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Atomically acquire an exclusive claim to submit a vacancy.

    Guarantees:
    - Strict concurrency exclusion via SQLite transaction and PRIMARY KEY on vacancy_stable_id.
    - Rejection if kill switch (submit_paused) is active.
    - Rejection if vacancy is already submitted or in an ambiguous outcome state.
    - Rejection if another worker is currently in ATTEMPTING state.
    - Ordinary exceptions do not auto-reopen claims.
    """
    import uuid as _uuid
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    cid = claim_id or f"claim_{_uuid.uuid4().hex[:12]}"
    d_json = json.dumps(details or {"worker_id": worker_id, "acquired_at": now})

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")

        # 1. Kill-switch check
        cur.execute("SELECT value FROM system_settings WHERE key = 'submit_paused'")
        row = cur.fetchone()
        if row and str(row[0]).strip() in ("1", "true", "True"):
            conn.rollback()
            return False, "PAUSED_BY_KILL_SWITCH", None

        # 2. Check existing claim
        cur.execute(
            "SELECT vacancy_stable_id, application_id, claim_id, status, worker_id, claimed_at, updated_at, details_json "
            "FROM submission_claims WHERE vacancy_stable_id = ?",
            (vacancy_stable_id,),
        )
        existing = cur.fetchone()
        if existing:
            ex_data = {
                "vacancy_stable_id": existing[0],
                "application_id": existing[1],
                "claim_id": existing[2],
                "status": existing[3],
                "worker_id": existing[4],
                "claimed_at": existing[5],
                "updated_at": existing[6],
                "details": json.loads(existing[7]) if existing[7] else {},
            }
            ex_status = existing[3]
            if ex_status == SubmissionClaimStatus.SUBMITTED.value:
                conn.rollback()
                return False, "ALREADY_SUBMITTED", ex_data
            elif ex_status == SubmissionClaimStatus.ATTEMPTING.value:
                conn.rollback()
                return False, "CONCURRENT_ATTEMPT_IN_PROGRESS", ex_data
            elif ex_status == SubmissionClaimStatus.AMBIGUOUS.value:
                conn.rollback()
                return False, "AMBIGUOUS_OUTCOME_BLOCKED", ex_data
            elif ex_status == SubmissionClaimStatus.FAILED_SAFE.value:
                if not allow_reclaim_failed_safe:
                    conn.rollback()
                    return False, "PREVIOUS_ATTEMPT_FAILED_SAFE", ex_data
                cur.execute(
                    "UPDATE submission_claims SET claim_id = ?, status = ?, worker_id = ?, updated_at = ?, details_json = ? "
                    "WHERE vacancy_stable_id = ?",
                    (cid, SubmissionClaimStatus.ATTEMPTING.value, worker_id, now, d_json, vacancy_stable_id),
                )
                conn.commit()
                ex_data.update({
                    "claim_id": cid,
                    "status": SubmissionClaimStatus.ATTEMPTING.value,
                    "worker_id": worker_id,
                    "updated_at": now,
                })
                return True, "CLAIM_REACQUIRED", ex_data
            else:
                conn.rollback()
                return False, f"CLAIM_BLOCKED_{ex_status}", ex_data

        # 3. Check submission evidence in application_submissions
        cur.execute(
            "SELECT status FROM application_submissions WHERE vacancy_stable_id = ? "
            "AND status IN ('SUBMITTED', 'CONFIRMED', 'AMBIGUOUS', 'AMBIGUOUS_POST_SUBMIT', 'VERIFIED', 'SUCCESS')",
            (vacancy_stable_id,),
        )
        if cur.fetchone():
            conn.rollback()
            return False, "ALREADY_APPLIED_SUBMISSIONS", None

        # 4. Insert new claim
        cur.execute(
            "INSERT INTO submission_claims (vacancy_stable_id, application_id, claim_id, status, worker_id, claimed_at, updated_at, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vacancy_stable_id, application_id, cid, SubmissionClaimStatus.ATTEMPTING.value, worker_id, now, now, d_json),
        )
        conn.commit()
        return True, "CLAIM_ACQUIRED", {
            "vacancy_stable_id": vacancy_stable_id,
            "application_id": application_id,
            "claim_id": cid,
            "status": SubmissionClaimStatus.ATTEMPTING.value,
            "worker_id": worker_id,
            "claimed_at": now,
            "updated_at": now,
        }
    except sqlite3.IntegrityError:
        conn.rollback()
        return False, "CONCURRENT_CLAIM_CONFLICT", None
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to acquire submission claim: {e}")
        return False, f"CLAIM_ERROR: {e}", None
    finally:
        conn.close()


def get_submission_claim(vacancy_stable_id: str) -> dict[str, Any] | None:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT vacancy_stable_id, application_id, claim_id, status, worker_id, claimed_at, updated_at, details_json "
        "FROM submission_claims WHERE vacancy_stable_id = ?",
        (vacancy_stable_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "vacancy_stable_id": row[0],
        "application_id": row[1],
        "claim_id": row[2],
        "status": row[3],
        "worker_id": row[4],
        "claimed_at": row[5],
        "updated_at": row[6],
        "details": json.loads(row[7]) if row[7] else {},
    }


def update_submission_claim(
    vacancy_stable_id: str,
    status: str,
    claim_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        if claim_id:
            cur.execute(
                "SELECT details_json FROM submission_claims WHERE vacancy_stable_id = ? AND claim_id = ?",
                (vacancy_stable_id, claim_id),
            )
        else:
            cur.execute(
                "SELECT details_json FROM submission_claims WHERE vacancy_stable_id = ?",
                (vacancy_stable_id,),
            )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return False

        merged_details = json.loads(row[0]) if row[0] else {}
        if details:
            merged_details.update(details)
        merged_details["last_status_change"] = now
        d_json = json.dumps(merged_details)

        if claim_id:
            cur.execute(
                "UPDATE submission_claims SET status = ?, updated_at = ?, details_json = ? "
                "WHERE vacancy_stable_id = ? AND claim_id = ?",
                (status, now, d_json, vacancy_stable_id, claim_id),
            )
        else:
            cur.execute(
                "UPDATE submission_claims SET status = ?, updated_at = ?, details_json = ? "
                "WHERE vacancy_stable_id = ?",
                (status, now, d_json, vacancy_stable_id),
            )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update submission claim for {vacancy_stable_id}: {e}")
        return False
    finally:
        conn.close()


def list_submission_claims(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    if status:
        cur.execute(
            "SELECT vacancy_stable_id, application_id, claim_id, status, worker_id, claimed_at, updated_at, details_json "
            "FROM submission_claims WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur.execute(
            "SELECT vacancy_stable_id, application_id, claim_id, status, worker_id, claimed_at, updated_at, details_json "
            "FROM submission_claims ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    rows = cur.fetchall()
    conn.close()
    results = []
    for r in rows:
        results.append({
            "vacancy_stable_id": r[0],
            "application_id": r[1],
            "claim_id": r[2],
            "status": r[3],
            "worker_id": r[4],
            "claimed_at": r[5],
            "updated_at": r[6],
            "details": json.loads(r[7]) if r[7] else {},
        })
    return results


def reconcile_submission_claim(
    vacancy_stable_id: str,
    new_status: str,
    reason: str,
    actor: str = "human_admin",
) -> bool:
    """Explicit administrative reconciliation of a submission claim."""
    claim = get_submission_claim(vacancy_stable_id)
    if not claim:
        return False
    details = claim.get("details", {})
    reconcile_entry = {
        "reconciled_by": actor,
        "reason": reason,
        "from_status": claim["status"],
        "to_status": new_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    history = details.get("reconciliation_history", [])
    history.append(reconcile_entry)
    details["reconciliation_history"] = history
    return update_submission_claim(
        vacancy_stable_id=vacancy_stable_id,
        claim_id=claim["claim_id"],
        status=new_status,
        details=details,
    )

