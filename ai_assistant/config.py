import os

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        hermes_env = os.path.expanduser(r"~\AppData\Local\hermes\.env")
        if os.path.exists(hermes_env):
            load_dotenv(hermes_env)
except Exception:
    pass

# Canonical project root — DB and vacancies.json resolve against it so that the
# working directory never changes which database is used (cron, CLI, agents).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VACANCIES_FILE = os.getenv("VACANCIES_FILE") or os.path.join(PROJECT_ROOT, "vacancies.json")
DB_FILE = os.getenv("DB_FILE") or os.path.join(PROJECT_ROOT, "state.db")
CANDIDATE_PROFILE_FILE = os.getenv("CANDIDATE_PROFILE_FILE", os.getenv("CANDIDATE_PROFILE", ""))

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-1.5-pro")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TG_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TG_CHAT_ID", os.getenv("TELEGRAM_ALLOWED_USERS", os.getenv("TELEGRAM_HOME_CHANNEL", ""))))
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", os.getenv("TELEGRAM_ADMIN_ID", os.getenv("TELEGRAM_USER_ID", os.getenv("TELEGRAM_ALLOWED_USERS", ""))))
TG_BOT_TOKEN = TELEGRAM_BOT_TOKEN
TG_CHAT_ID = TELEGRAM_CHAT_ID

UNIPILE_API_KEY = os.getenv("UNIPILE_API_KEY", "")
UNIPILE_DSN = os.getenv("UNIPILE_DSN", "")
UNIPILE_BASE_URL = os.getenv("UNIPILE_BASE_URL", "https://api1.unipile.com:13111/api/v1")

try:
    STOP_WORDS = [w.strip().lower() for w in os.getenv("STOP_WORDS", "").split(",") if w.strip()]
    REQUIRED_WORDS = [w.strip().lower() for w in os.getenv("REQUIRED_WORDS", "").split(",") if w.strip()]
    MIN_SALARY = int(os.getenv("MIN_SALARY", "0"))
    BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "10"))
    DIGEST_ATTEMPT_STALE_MINUTES = int(os.getenv("DIGEST_ATTEMPT_STALE_MINUTES", "60"))
    if DIGEST_ATTEMPT_STALE_MINUTES <= 0:
        DIGEST_ATTEMPT_STALE_MINUTES = 60
    PRODUCTION_FAILURE_ALERT_THRESHOLD = int(os.getenv("PRODUCTION_FAILURE_ALERT_THRESHOLD", "3"))
    if PRODUCTION_FAILURE_ALERT_THRESHOLD <= 0:
        PRODUCTION_FAILURE_ALERT_THRESHOLD = 3
    PREFERENCE_CALIBRATION_ENABLED = os.getenv("PREFERENCE_CALIBRATION_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    PREFERENCE_MAX_ADJUSTMENT = float(os.getenv("PREFERENCE_MAX_ADJUSTMENT", "8.0"))
    PREFERENCE_MIN_EVIDENCE_THRESHOLD = int(os.getenv("PREFERENCE_MIN_EVIDENCE_THRESHOLD", "5"))
except Exception:
    STOP_WORDS = []
    REQUIRED_WORDS = []
    MIN_SALARY = 0
    BATCH_LIMIT = 10
    DIGEST_ATTEMPT_STALE_MINUTES = 60
    PRODUCTION_FAILURE_ALERT_THRESHOLD = 3
    PREFERENCE_CALIBRATION_ENABLED = False
    PREFERENCE_MAX_ADJUSTMENT = 8.0
    PREFERENCE_MIN_EVIDENCE_THRESHOLD = 5

LOGS_DIR = os.getenv("LOGS_DIR") or os.path.join(PROJECT_ROOT, "logs", "job_search")
SUBMIT_ALLOWED = os.getenv("SUBMIT_ALLOWED", "false").strip().lower() in ("1", "true", "yes")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()



