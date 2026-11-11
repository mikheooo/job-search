# Environment Variables Inventory (`job-search`)

Complete reference of all environment variables recognized by the `job-search` platform.
Generated and maintained per audit task `CFG-002`. Verified by automated linter tests (`tests/test_env_inventory.py`).

## Summary of Mutation Safety Levels

| Level | Meaning | Policy |
| :--- | :--- | :--- |
| `CRITICAL_KILL_SWITCH` | Outbound submit / auto-reply gate | Must fail closed; `.env` cannot re-arm operator shell overrides |
| `AUTH_SECRET` | Secret keys, passwords, bearer tokens | Kept in local `.env` only; forbidden in git history and CI logs |
| `NETWORK_PERIMETER` | Host bindings, endpoints, CORS | Default to loopback (`127.0.0.1`); external requires explicit auth |
| `RUNTIME_CONTROL` | Rate limits, cooldowns, watchdogs | Safe conservative defaults preventing spam or bans |
| `PATH_CONFIG` | File and directory locations | Canonical paths resolve relative to `PROJECT_ROOT` |
| `OPERATOR_TUNING` | Keywords, thresholds, model settings | Operator-configurable matching and preference weights |
| `DIAGNOSTIC_OFFLINE` | Test harness and mock mode guards | Offline defaults preventing accidental external network egress |

## 1. Critical Outbound Kill-Switches & Safety Latches

Variables controlling outbound submissions, automated replies, and emergency halt mechanisms. All default to fail-closed / safe review mode.

| Variable Name | Default | Safety Level | Source Files | Description |
| :--- | :--- | :--- | :--- | :--- |
| `HH_APPLY_MODE` | `REVIEW` | `CRITICAL_KILL_SWITCH` | `ai_assistant/auto_apply_modes.py` | HH apply workflow mode: REVIEW (safe preview) or AUTO |
| `HH_AUTONOMOUS_REPLY_ENABLED` | `false` | `CRITICAL_KILL_SWITCH` | `ai_assistant/outbound_governor.py`<br>`ai_assistant/hh_autonomous_agent.py` | Kill-switch enabling autonomous employer chat responses |
| `HH_AUTO_REPLY_ENABLED` | `false` | `CRITICAL_KILL_SWITCH` | `ai_assistant/hh_message_reply.py` | Kill-switch enabling auto-reply in HH message reply system |
| `HH_AUTO_SUBMIT` | `0` | `CRITICAL_KILL_SWITCH` | `ai_assistant/hh_application_runner.py` | Flag to arm HH application runner automated submission path |
| `KWORK_AUTO_APPLY` | `false` | `CRITICAL_KILL_SWITCH` | `scripts/run_gig_autopilot_cycle.py` | Kill-switch enabling automated proposals on Kwork |
| `STOP_OUTBOUNDS_DIR` | `None (PROJECT_ROOT)` | `CRITICAL_KILL_SWITCH` | `ai_assistant/outbound_governor.py` | Directory where stop files are checked; isolated in tests via temporary directories |
| `STOP_OUTBOUNDS_FILE` | `None (STOP_ALL_OUTBOUNDS in root)` | `CRITICAL_KILL_SWITCH` | `ai_assistant/hh_submission.py`<br>`ai_assistant/outbound_governor.py` | Custom emergency stop file path; halts all outbound operations across all channels |
| `STOP_SUBMITS_FILE` | `None (STOP_SUBMITS in root)` | `CRITICAL_KILL_SWITCH` | `ai_assistant/hh_submission.py`<br>`ai_assistant/hh_submit_policy.py` | Custom emergency stop file path; if exists, halts all HH submissions |
| `SUBMIT_ALLOWED` | `false` | `CRITICAL_KILL_SWITCH` | `ai_assistant/config.py` | Master submission kill-switch; when false, no form submission or outbound application may fire |

## 2. Authentication Credentials & Tokens

API tokens, secrets, passwords, and user IDs. Must NEVER be committed to version control.

| Variable Name | Default | Safety Level | Source Files | Description |
| :--- | :--- | :--- | :--- | :--- |
| `DASHBOARD_TOKEN` | `None` | `AUTH_SECRET` | `ai_assistant/config.py`<br>`ai_assistant/hub/api.py` | Bearer token required for mutating /api endpoints (and reads when external) |
| `FLN_OAUTH_TOKEN` | `None` | `AUTH_SECRET` | `ai_assistant/gig/submission/freelancer.py` | OAuth bearer token for Freelancer.com API |
| `HH_TELEGRAM_BOT_TOKEN` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Dedicated bot token for HeadHunter notifications, isolating HH traffic from main bot |
| `HH_TELEGRAM_CHAT_ID` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Dedicated chat ID for HeadHunter notifications |
| `LLM_API_KEY` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | API key for LLM provider (OpenAI / Gemini / OpenRouter) |
| `SMTP_PASSWORD` | `None` | `AUTH_SECRET` | `ai_assistant/email_outreach.py`<br>`scripts/run_telegram_outreach.py` | SMTP server password for email outreach delivery |
| `SMTP_USER` | `None` | `AUTH_SECRET` | `ai_assistant/email_outreach.py` | SMTP username / sender email address |
| `TELEGRAM_ADMIN_ID` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Alternative alias for TELEGRAM_OWNER_ID |
| `TELEGRAM_ALLOWED_USERS` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Alternative alias for TELEGRAM_OWNER_ID / allowed chats |
| `TELEGRAM_BOT_TOKEN` | `None (Required)` | `AUTH_SECRET` | `ai_assistant/config.py` | Telegram bot API token for primary Hermes / job-search alerts |
| `TELEGRAM_CHANNEL_ID` | `None (Optional)` | `AUTH_SECRET` | `ai_assistant/config.py` | Telegram public channel for remote-vacancy postings |
| `TELEGRAM_CHAT_ID` | `None (Required)` | `AUTH_SECRET` | `ai_assistant/config.py` | Telegram chat ID for alert delivery |
| `TELEGRAM_HOME_CHANNEL` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Alternative alias for TELEGRAM_CHAT_ID |
| `TELEGRAM_OWNER_ID` | `None (Required)` | `AUTH_SECRET` | `ai_assistant/config.py` | Numeric Telegram ID of owner authorized for interactive callbacks |
| `TELEGRAM_USER_ID` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Alternative alias for TELEGRAM_OWNER_ID |
| `TG_BOT_TOKEN` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Alternative alias for TELEGRAM_BOT_TOKEN |
| `TG_CHAT_ID` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | Alternative alias for TELEGRAM_CHAT_ID |
| `UNIPILE_API_KEY` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | API key for Unipile messaging integration |
| `UNIPILE_DSN` | `None` | `AUTH_SECRET` | `ai_assistant/config.py` | DSN connection string for Unipile account |

## 3. Network Perimeter & Service Endpoints

Host bindings, ports, CORS origins, CDP browser endpoints, and API base URLs. All local services default to 127.0.0.1.

| Variable Name | Default | Safety Level | Source Files | Description |
| :--- | :--- | :--- | :--- | :--- |
| `CDP_URL` | `None` | `NETWORK_PERIMETER` | `ai_assistant/hh_browser_launcher.py` | Fallback generic CDP endpoint URL |
| `CONTRA_CDP_URL` | `None` | `NETWORK_PERIMETER` | `ai_assistant/gig/cli.py` | CDP endpoint for Contra automation |
| `DASHBOARD_CORS_ORIGINS` | `localhost:8000, 127.0.0.1:8000` | `NETWORK_PERIMETER` | `ai_assistant/ui/app.py` | Allowed CORS origins for dashboard API |
| `DASHBOARD_HOST` | `127.0.0.1` | `NETWORK_PERIMETER` | `ai_assistant/ui/app.py`<br>`scripts/launch_dashboard.py` | Host binding for web dashboard; defaults to loopback 127.0.0.1 |
| `DASHBOARD_PORT` | `8001` | `NETWORK_PERIMETER` | `scripts/launch_dashboard.py` | Port for web dashboard |
| `DASHBOARD_REQUIRE_AUTH_FOR_READS` | `false (forced true if non-loopback)` | `NETWORK_PERIMETER` | `ai_assistant/ui/app.py` | Whether GET /api/* calls require DASHBOARD_TOKEN |
| `HH_CDP_URL` | `http://127.0.0.1:9110` | `NETWORK_PERIMETER` | `ai_assistant/hh_browser_launcher.py` | CDP endpoint for BrowserOS profile |
| `HH_HEADLESS_CDP_URL` | `http://127.0.0.1:9111` | `NETWORK_PERIMETER` | `scripts/run_hh_autopilot_cycle.py`<br>`ai_assistant/hh_browser_provider.py` | CDP endpoint for headless Chrome instance |
| `HH_URL` | `https://hh.ru/chat` | `NETWORK_PERIMETER` | `ai_assistant/hh_browser_launcher.py` | Initial URL navigated to by HH browser launcher |
| `JOB_SEARCH_TEST_NETWORK_BLOCKED` | `None` | `NETWORK_PERIMETER` | `ai_assistant/hh_autonomous_agent.py` | Internal test guard blocking real outbound network calls |
| `LLM_BASE_URL` | `None` | `NETWORK_PERIMETER` | `ai_assistant/config.py` | Base URL for OpenAI-compatible LLM endpoint |
| `SMTP_HOST` | `smtp.gmail.com` | `NETWORK_PERIMETER` | `ai_assistant/email_outreach.py` | SMTP mail server hostname |
| `SMTP_PORT` | `465` | `NETWORK_PERIMETER` | `ai_assistant/email_outreach.py` | SMTP mail server SSL port |
| `UNIPILE_BASE_URL` | `https://api1.unipile.com:13111/api/v1` | `NETWORK_PERIMETER` | `ai_assistant/config.py` | Base URL for Unipile REST API |

## 4. Runtime Automation, Rate Limits & Watchdogs

Operational rate limits, daily caps, heartbeat watchdogs, alert cooldowns, and browser engine selectors.

| Variable Name | Default | Safety Level | Source Files | Description |
| :--- | :--- | :--- | :--- | :--- |
| `BROWSER_REAL` | `0` | `RUNTIME_CONTROL` | `ai_assistant/browser_executor.py`<br>`ai_assistant/submission_verifier.py` | Flag to use live Playwright browser instead of mock |
| `BROWSER_USE_PLAYWRIGHT` | `None` | `RUNTIME_CONTROL` | `ai_assistant/browser_executor.py`<br>`ai_assistant/cli.py` | Alternative flag to force Playwright driver |
| `DIGEST_ATTEMPT_STALE_MINUTES` | `60` | `RUNTIME_CONTROL` | `ai_assistant/config.py` | Age threshold in minutes before unconfirmed attempt is retried |
| `GIG_ENABLE_CONTRA` | `false` | `RUNTIME_CONTROL` | `scripts/run_gig_autopilot_cycle.py` | Enable Contra stream in gig autopilot cycle |
| `GIG_ENABLE_FREELANCER` | `false` | `RUNTIME_CONTROL` | `scripts/run_gig_autopilot_cycle.py` | Enable Freelancer stream in gig autopilot cycle |
| `HH_AUTOPILOT_DAILY_CAP` | `12` | `RUNTIME_CONTROL` | `ai_assistant/hh_autopilot.py` | Maximum autonomous HH applications per calendar day |
| `HH_AUTOPILOT_FAILURE_ALERT_COOLDOWN_SECONDS` | `21600 (6h)` | `RUNTIME_CONTROL` | `scripts/run_hh_autopilot_cycle.py` | Cooldown between autopilot failure alert notifications |
| `HH_BROWSER_MODE` | `headless` | `RUNTIME_CONTROL` | `ai_assistant/hh_autopilot.py`<br>`ai_assistant/hub/api.py` | Browser driver: 'headless' (isolated instance) or 'browseros' (shared) |
| `HH_CDP_ALLOW_FALLBACK` | `0` | `RUNTIME_CONTROL` | `ai_assistant/hh_browser_launcher.py` | Allow fallback port hopping in non-prod environments |
| `HH_CDP_NONPROD` | `0` | `RUNTIME_CONTROL` | `ai_assistant/hh_browser_launcher.py` | Explicitly declare browser session as non-production |
| `HH_MAX_SUBMITS_PER_DAY` | `30` | `RUNTIME_CONTROL` | `ai_assistant/hh_submit_policy.py` | Submission policy hard ceiling for daily applications |
| `HH_MAX_SUBMITS_PER_HOUR` | `10` | `RUNTIME_CONTROL` | `ai_assistant/hh_submit_policy.py` | Submission policy rate limit for applications per hour |
| `HH_REALTIME_AUTH_RETRY_SECONDS` | `300` | `RUNTIME_CONTROL` | `scripts/run_hh_realtime_daemon.py` | Retry backoff after authentication outage in daemon |
| `HH_REALTIME_BROWSER_RECOVERY_BASE_SECONDS` | `5` | `RUNTIME_CONTROL` | `scripts/run_hh_realtime_daemon.py` | Base backoff for browser connection recovery |
| `HH_REALTIME_BROWSER_RECOVERY_MAX_SECONDS` | `300` | `RUNTIME_CONTROL` | `scripts/run_hh_realtime_daemon.py` | Max backoff for browser connection recovery |
| `HH_REALTIME_HEARTBEAT_INTERVAL` | `60` | `RUNTIME_CONTROL` | `scripts/run_hh_realtime_daemon.py` | Interval in seconds between realtime daemon heartbeat writes |
| `HH_REALTIME_HEARTBEAT_STALE_SECONDS` | `600` | `RUNTIME_CONTROL` | `scripts/check_runtime_health.py` | Seconds before daemon heartbeat file is considered dead |
| `HH_REALTIME_PAUSE_ALERT_COOLDOWN_SECONDS` | `3600` | `RUNTIME_CONTROL` | `scripts/run_hh_realtime_daemon.py` | Cooldown between realtime pause alert notifications |
| `HH_REALTIME_PAUSE_ALERT_SECONDS` | `1800` | `RUNTIME_CONTROL` | `scripts/check_runtime_health.py` | Seconds of daemon pause before owner alert is sent |
| `HH_REALTIME_POLL_INTERVAL` | `30` | `RUNTIME_CONTROL` | `scripts/run_hh_realtime_daemon.py` | Interval in seconds between realtime chat polls |
| `KWORK_INBOX_ALERT_COOLDOWN_SECONDS` | `14400 (4h)` | `RUNTIME_CONTROL` | `scripts/report_kwork_inbox_health.py` | Cooldown between alerts for Kwork inbox health issues |
| `KWORK_INBOX_NOT_READY_STREAK` | `3` | `RUNTIME_CONTROL` | `scripts/report_kwork_inbox_health.py` | Consecutive failing checks before raising Kwork alert |
| `KWORK_MONITOR_ENABLED` | `true` | `RUNTIME_CONTROL` | `scripts/run_kwork_inbox_monitor_cycle.py` | Enable background monitoring of Kwork inbox |
| `PRODUCTION_FAILURE_ALERT_THRESHOLD` | `3` | `RUNTIME_CONTROL` | `ai_assistant/config.py` | Consecutive production failures before dispatching incident alert |
| `TELEGRAM_FB_CALLBACKS_ENABLED` | `false` | `RUNTIME_CONTROL` | `.env.example` | Enable Telegram feedback inline button callbacks |
| `TELEGRAM_FLASH_SUPPRESS_ENABLED` | `false` | `RUNTIME_CONTROL` | `scripts/run_multi_source_discovery.py` | Suppress burst telegram messages during bulk discovery |
| `TELEGRAM_NOTIFY_POLICY_ENABLED` | `false` | `RUNTIME_CONTROL` | `scripts/run_gig_autopilot_cycle.py` | Enforce Telegram frequency and deduplication notification policy |
| `USE_PLAYWRIGHT` | `None` | `RUNTIME_CONTROL` | `ai_assistant/browser_executor.py`<br>`ai_assistant/submission_verifier.py` | Alternative alias for BROWSER_USE_PLAYWRIGHT |

## 5. Storage & File System Paths

Locations of state databases, cache files, candidate dossiers, browser profiles, and logs.

| Variable Name | Default | Safety Level | Source Files | Description |
| :--- | :--- | :--- | :--- | :--- |
| `CANDIDATE_INTERVIEW_STATE` | `candidate_interview_state.json` | `PATH_CONFIG` | `ai_assistant/candidate/interview.py` | Path to candidate interview state store |
| `CANDIDATE_KB_FILE` | `candidate_kb.json` | `PATH_CONFIG` | `ai_assistant/candidate/kb.py` | Path to candidate knowledge base JSON file |
| `CANDIDATE_PROFILE` | `None` | `PATH_CONFIG` | `ai_assistant/config.py`<br>`ai_assistant/candidate_profile.py` | Alternative alias for CANDIDATE_PROFILE_FILE |
| `CANDIDATE_PROFILE_FILE` | `candidate_profile.json` | `PATH_CONFIG` | `ai_assistant/config.py`<br>`ai_assistant/candidate_profile.py` | Path to candidate profile JSON file |
| `CANDIDATE_PROJECTS_ROOT` | `candidate_projects` | `PATH_CONFIG` | `ai_assistant/candidate/projects.py` | Directory containing candidate project repositories and evidence |
| `CHROME_PATH` | `auto-detected` | `PATH_CONFIG` | `ai_assistant/hh_browser_launcher.py`<br>`ai_assistant/hh_browser_provider.py` | Custom path to Google Chrome binary |
| `DB_FILE` | `PROJECT_ROOT/state.db` | `PATH_CONFIG` | `ai_assistant/config.py` | Path to SQLite database file |
| `GIG_FIXTURE_FILE` | `None` | `PATH_CONFIG` | `ai_assistant/gig/sources/fixture.py` | Path to gig test fixture JSON |
| `HERMES_ENV_FILE` | `HERMES_ROOT/.env` | `PATH_CONFIG` | `ai_assistant/interview/assistant.py` | Explicit path to Hermes environment file |
| `HERMES_HOME` | `None` | `PATH_CONFIG` | `scripts/hermes-vertex-adc-fix.py` | Alternative alias for HERMES_ROOT |
| `HERMES_ROOT` | `%LOCALAPPDATA%/hermes` | `PATH_CONFIG` | `ai_assistant/hermes_integration.py`<br>`ai_assistant/interview/assistant.py` | Filesystem root of external Hermes installation |
| `HH_CHROME_PROFILE` | `None` | `PATH_CONFIG` | `ai_assistant/hh_browser_launcher.py` | Chrome profile name/directory for BrowserOS launcher |
| `HH_HEADLESS_PROFILE` | `%LOCALAPPDATA%/JobSearchHeadless/User Data` | `PATH_CONFIG` | `ai_assistant/hh_browser_provider.py` | Chrome user profile directory for headless automation |
| `HH_STORAGE_STATE` | `hh_storage_state.json` | `PATH_CONFIG` | `ai_assistant/browser_executor.py` | Path to Playwright storage state file with session cookies |
| `JOB_SEARCH_ROOT` | `repo root` | `PATH_CONFIG` | `ai_assistant/hermes_integration.py` | Alternative alias for PROJECT_ROOT |
| `LOGS_DIR` | `PROJECT_ROOT/logs/job_search` | `PATH_CONFIG` | `ai_assistant/config.py` | Directory for application and runner logs |
| `PROJECT_ROOT` | `repo root` | `PATH_CONFIG` | `ai_assistant/config.py`<br>`ai_assistant/hermes_integration.py` | Root directory for job-search project |
| `STT_MODEL_DIR` | `None` | `PATH_CONFIG` | `ai_assistant/interview/stt.py` | Directory for Vosk STT speech-to-text models |
| `VACANCIES_FILE` | `PROJECT_ROOT/vacancies.json` | `PATH_CONFIG` | `ai_assistant/config.py` | Path to legacy/fallback vacancies JSON cache |
| `VOSK_MODEL_DIR` | `None` | `PATH_CONFIG` | `ai_assistant/interview/stt.py` | Alternative alias for STT_MODEL_DIR |

## 6. Operator Matching & Scoring Tuning

Prefilter keywords, salary minimums, batch sizes, LLM model choice, and feedback calibration.

| Variable Name | Default | Safety Level | Source Files | Description |
| :--- | :--- | :--- | :--- | :--- |
| `BATCH_LIMIT` | `10` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Number of vacancies processed in a single batch |
| `CANDIDATE_INTERVIEW_LLM` | `offline` | `OPERATOR_TUNING` | `ai_assistant/candidate/interview_extract.py` | LLM mode for candidate interview extraction: 'offline' or 'online' |
| `EMAIL_DRY_RUN` | `true` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Default dry-run behavior for email outreach operations |
| `HH_DRY_RUN` | `true` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Default dry-run behavior for HeadHunter operations |
| `LLM_MODEL` | `google/gemini-1.5-pro` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Model identifier for LLM calls |
| `MIN_SALARY` | `0` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Minimum acceptable salary threshold |
| `PREFERENCE_CALIBRATION_ENABLED` | `false` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Feature flag enabling dynamic weight adjustments from user feedback |
| `PREFERENCE_MAX_ADJUSTMENT` | `8.0` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Maximum point adjustment applied by preference calibration |
| `PREFERENCE_MIN_EVIDENCE_THRESHOLD` | `5` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Minimum feedback samples required before calibration shifts weights |
| `REQUIRED_WORDS` | `[]` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Comma-separated list of mandatory words for vacancy matching |
| `STOP_WORDS` | `[]` | `OPERATOR_TUNING` | `ai_assistant/config.py` | Comma-separated list of words causing immediate vacancy rejection |
| `STT_DEVICE` | `auto` | `OPERATOR_TUNING` | `ai_assistant/interview/stt.py` | Audio input device identifier for speech recognition |

## 7. Diagnostics, Environment & Offline Mocks

Offline test guards, non-production environment flags, and migration overrides.

| Variable Name | Default | Safety Level | Source Files | Description |
| :--- | :--- | :--- | :--- | :--- |
| `APPLICATION_PREP_OFFLINE` | `None` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/application_prep.py` | Run application preparation in offline mode without external models |
| `APP_ENV` | `prod` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/hh_browser_launcher.py` | Alias for JOB_SEARCH_ENV |
| `ENV` | `prod` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/hh_browser_launcher.py` | Alias for JOB_SEARCH_ENV |
| `EXTERNAL_FORM_FILLER_OFFLINE` | `None` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/external_form_filler.py` | Run external form filler in offline mock mode |
| `GOOGLE_APPLICATION_CREDENTIALS` | `None` | `DIAGNOSTIC_OFFLINE` | `scripts/hermes-vertex-adc-fix.py` | GCP Service Account credentials path (for Vertex AI troubleshooting script) |
| `GOOGLE_CLOUD_PROJECT` | `None` | `DIAGNOSTIC_OFFLINE` | `scripts/hermes-vertex-adc-fix.py` | GCP Project ID (for Vertex AI troubleshooting script) |
| `JOB_ANALYZER_OFFLINE` | `None` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/application_prep.py`<br>`ai_assistant/job_analyzer.py` | Run job analyzer in offline mode using heuristic fallbacks |
| `JOB_SEARCH_ENV` | `prod` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/hh_browser_launcher.py` | Environment identifier: 'prod', 'dev', 'test', 'local', 'ci' |
| `LEGACY_BOT_OVERRIDE` | `None` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/bot.py` | Allow loading deprecated legacy bot entrypoint |
| `LEGACY_PIPELINE_OVERRIDE` | `None` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/pipeline.py` | Allow loading deprecated legacy pipeline entrypoint |
| `TEST_ENVIRONMENT_MOCK` | `None` | `DIAGNOSTIC_OFFLINE` | `ai_assistant/schema.py` | Test environment flag enabling mock serialization behavior |
| `VERTEX_CREDENTIALS_PATH` | `None` | `DIAGNOSTIC_OFFLINE` | `scripts/hermes-vertex-adc-fix.py` | Vertex credentials path override in helper script |
| `VERTEX_PROJECT` | `None` | `DIAGNOSTIC_OFFLINE` | `scripts/hermes-vertex-adc-fix.py` | Vertex project override in helper script |
| `VERTEX_PROJECT_ID` | `None` | `DIAGNOSTIC_OFFLINE` | `scripts/hermes-vertex-adc-fix.py` | Vertex project id override in helper script |

