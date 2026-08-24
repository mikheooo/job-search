import sqlite3
from . import config
from .schema import Vacancy


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


def save_vacancy(vacancy):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vacancies (
            stable_id, source, source_job_id, title, company, description, location,
            country_restrictions, timezone_restrictions, salary_min, salary_max, salary_currency,
            employment_type, job_url, application_url, published_at, first_seen_at, last_seen_at,
            state, raw_data, match_score, match_decision, match_reasons, match_strengths, match_gaps
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(stable_id) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            state=excluded.state,
            match_score=excluded.match_score,
            match_decision=excluded.match_decision,
            match_reasons=excluded.match_reasons,
            match_strengths=excluded.match_strengths,
            match_gaps=excluded.match_gaps,
            raw_data=excluded.raw_data
    ''', (
        vacancy.stable_id(),
        vacancy.source,
        vacancy.source_job_id,
        vacancy.title,
        vacancy.company,
        vacancy.description,
        vacancy.location,
        ', '.join(vacancy.country_restrictions),
        ', '.join(vacancy.timezone_restrictions),
        vacancy.salary_min,
        vacancy.salary_max,
        vacancy.salary_currency,
        vacancy.employment_type,
        vacancy.job_url,
        vacancy.application_url,
        _to_iso(vacancy.published_at),
        _to_iso(vacancy.first_seen_at),
        _to_iso(vacancy.last_seen_at),
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
    if verified_at is None:
        verified_at = _dt.datetime.utcnow().isoformat()
    if created_at is None:
        created_at = _dt.datetime.utcnow().isoformat()
    if updated_at is None:
        updated_at = _dt.datetime.utcnow().isoformat()
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
    return row[3] == "VERIFIED"


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
