import logging
import os

logger = logging.getLogger(__name__)

# Canonical project root — DB and vacancies.json resolve against it so that the
# working directory never changes which database is used (cron, CLI, agents).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OPERATOR_SAFETY_VARS = (
    "SUBMIT_ALLOWED",
    "HH_AUTO_SUBMIT",
    "HH_AUTONOMOUS_REPLY_ENABLED",
    "KWORK_AUTO_APPLY",
    "BROWSER_REAL",
    "HH_DRY_RUN",
    "EMAIL_DRY_RUN",
    "STOP_SUBMITS_FILE",
    "STOP_OUTBOUNDS_FILE",
)

# Kill-switch & safety flag snapshot, taken BEFORE load_dotenv below.
# Dotenv must not overwrite real environment variables set by an operator in their shell
# (BLE001 finding #8 & CFG-001 audit remediation).
_OPERATOR_FLAGS = {k: os.getenv(k) for k in OPERATOR_SAFETY_VARS}
_OPERATOR_SUBMIT_ALLOWED = _OPERATOR_FLAGS.get("SUBMIT_ALLOWED")

def get_hermes_env_candidates() -> list[str]:
    """Return potential cross-platform file paths for hermes agent's .env."""
    candidates = []
    localappdata = os.getenv("LOCALAPPDATA")
    if localappdata:
        candidates.append(os.path.join(localappdata, "hermes", ".env"))
    candidates.extend([
        os.path.expanduser(os.path.join("~", "AppData", "Local", "hermes", ".env")),
        os.path.expanduser(os.path.join("~", ".config", "hermes", ".env")),
        os.path.expanduser(os.path.join("~", ".local", "share", "hermes", ".env")),
    ])
    seen = set()
    result = []
    for c in candidates:
        norm = os.path.normpath(c)
        if norm not in seen:
            seen.add(norm)
            result.append(c)
    return result


try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        for hermes_env in get_hermes_env_candidates():
            if os.path.exists(hermes_env):
                load_dotenv(hermes_env, override=False)
                break
except (ImportError, OSError, ValueError) as exc:
    logger.warning("Could not load .env (%s: %s)", type(exc).__name__, exc)

# Re-apply operator safety flags in case any external loader mutated them.
for _var, _val in _OPERATOR_FLAGS.items():
    if _val is not None:
        os.environ[_var] = _val

VACANCIES_FILE = os.getenv("VACANCIES_FILE") or os.path.join(PROJECT_ROOT, "vacancies.json")
DB_FILE = os.getenv("DB_FILE") or os.path.join(PROJECT_ROOT, "state.db")
CANDIDATE_PROFILE_FILE = os.getenv("CANDIDATE_PROFILE_FILE", os.getenv("CANDIDATE_PROFILE", ""))

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-1.5-pro")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TG_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TG_CHAT_ID", os.getenv("TELEGRAM_ALLOWED_USERS", os.getenv("TELEGRAM_HOME_CHANNEL", ""))))
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", os.getenv("TELEGRAM_ADMIN_ID", os.getenv("TELEGRAM_USER_ID", os.getenv("TELEGRAM_ALLOWED_USERS", ""))))
# No baked-in channel. An empty value means "not configured"; send paths must
# call require_telegram_channel_id() and refuse rather than guess a chat.
TELEGRAM_CHANNEL_ID = (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
TG_BOT_TOKEN = TELEGRAM_BOT_TOKEN
TG_CHAT_ID = TELEGRAM_CHAT_ID

# Dedicated HeadHunter bot configuration (isolates HH chat / employer alerts from Hermes bot)
HH_TELEGRAM_BOT_TOKEN = os.getenv("HH_TELEGRAM_BOT_TOKEN", "")
HH_TELEGRAM_CHAT_ID = os.getenv("HH_TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))

UNIPILE_API_KEY = os.getenv("UNIPILE_API_KEY", "")
UNIPILE_DSN = os.getenv("UNIPILE_DSN", "")
UNIPILE_BASE_URL = os.getenv("UNIPILE_BASE_URL", "https://api1.unipile.com:13111/api/v1")

def _parse_word_list(key: str) -> list[str]:
    """Comma-separated word list. Empty or unset stays empty for this key only."""
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return []
    return [w.strip().lower() for w in str(raw).split(",") if w.strip()]


def _parse_int_env(key: str, default: int, *, minimum: int | None = None) -> int:
    """Parse one integer env var. Failure keeps `default` and does not touch other keys."""
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        logger.error(
            "Failed to parse %s=%r (%s: %s); keeping default %s",
            key,
            raw,
            type(exc).__name__,
            exc,
            default,
        )
        return default
    if minimum is not None and value < minimum:
        logger.error(
            "%s=%r is below minimum %s; keeping default %s",
            key,
            raw,
            minimum,
            default,
        )
        return default
    return value


def _parse_float_env(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        logger.error(
            "Failed to parse %s=%r (%s: %s); keeping default %s",
            key,
            raw,
            type(exc).__name__,
            exc,
            default,
        )
        return default


def _parse_bool_env(raw: object) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes")


# Each key is parsed on its own. A bad MIN_SALARY must not wipe STOP_WORDS.
STOP_WORDS = _parse_word_list("STOP_WORDS")
REQUIRED_WORDS = _parse_word_list("REQUIRED_WORDS")
MIN_SALARY = _parse_int_env("MIN_SALARY", 0)
BATCH_LIMIT = _parse_int_env("BATCH_LIMIT", 10)
DIGEST_ATTEMPT_STALE_MINUTES = _parse_int_env("DIGEST_ATTEMPT_STALE_MINUTES", 60, minimum=1)
PRODUCTION_FAILURE_ALERT_THRESHOLD = _parse_int_env(
    "PRODUCTION_FAILURE_ALERT_THRESHOLD", 3, minimum=1
)
PREFERENCE_CALIBRATION_ENABLED = _parse_bool_env(os.getenv("PREFERENCE_CALIBRATION_ENABLED", "false"))
PREFERENCE_MAX_ADJUSTMENT = _parse_float_env("PREFERENCE_MAX_ADJUSTMENT", 8.0)
PREFERENCE_MIN_EVIDENCE_THRESHOLD = _parse_int_env("PREFERENCE_MIN_EVIDENCE_THRESHOLD", 5)


class TelegramChannelRequired(RuntimeError):
    """A channel send was requested but TELEGRAM_CHANNEL_ID is unset."""


def require_telegram_channel_id() -> str:
    """Channel id from the environment. Fail closed when it is missing.

    Reads the live environment so a late export is honored. Never substitutes
    a hardcoded chat id.
    """
    channel = (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
    if not channel:
        logger.error(
            "TELEGRAM_CHANNEL_ID is not set; refusing to target a channel"
        )
        raise TelegramChannelRequired(
            "TELEGRAM_CHANNEL_ID is required to target a channel and is not set"
        )
    return channel

LOGS_DIR = os.getenv("LOGS_DIR") or os.path.join(PROJECT_ROOT, "logs", "job_search")

SUBMIT_ALLOWED = _parse_bool_env(
    _OPERATOR_SUBMIT_ALLOWED
    if _OPERATOR_SUBMIT_ALLOWED is not None and str(_OPERATOR_SUBMIT_ALLOWED).strip()
    else os.getenv("SUBMIT_ALLOWED", "false")
)


def submit_allowed() -> bool:
    """Effective kill-switch state, re-evaluated at call time.

    Precedence, most specific first:
      1. the real environment as it was before .env could clobber it;
      2. the current environment (tests and late `export`s land here);
      3. the value resolved from .env at import time.

    Any explicitly-set source that says "off" disables submission. The
    kill-switch must never be silently re-armed by a stale value in .env.
    """
    explicit = [
        v for v in (_OPERATOR_SUBMIT_ALLOWED, os.getenv("SUBMIT_ALLOWED"))
        if v is not None and str(v).strip()
    ]
    if explicit:
        return all(_parse_bool_env(v) for v in explicit)
    return bool(SUBMIT_ALLOWED)


DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
