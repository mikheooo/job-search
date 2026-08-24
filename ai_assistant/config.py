import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

VACANCIES_FILE = os.getenv("VACANCIES_FILE", "../vacancies.json")
DB_FILE = os.getenv("DB_FILE", "state.db")
CANDIDATE_PROFILE_FILE = os.getenv("CANDIDATE_PROFILE_FILE", os.getenv("CANDIDATE_PROFILE", ""))

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-1.5-pro")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

UNIPILE_API_KEY = os.getenv("UNIPILE_API_KEY", "")
UNIPILE_DSN = os.getenv("UNIPILE_DSN", "")
UNIPILE_BASE_URL = os.getenv("UNIPILE_BASE_URL", "https://api1.unipile.com:13111/api/v1")

try:
    STOP_WORDS = [w.strip().lower() for w in os.getenv("STOP_WORDS", "").split(",") if w.strip()]
    REQUIRED_WORDS = [w.strip().lower() for w in os.getenv("REQUIRED_WORDS", "").split(",") if w.strip()]
    MIN_SALARY = int(os.getenv("MIN_SALARY", "0"))
    BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "10"))
except Exception:
    STOP_WORDS = []
    REQUIRED_WORDS = []
    MIN_SALARY = 0
    BATCH_LIMIT = 10
