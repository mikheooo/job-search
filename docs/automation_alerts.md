# Operational alerts (HH cycle and Kwork inbox)

Scheduled jobs already log failures. Two of those failures now also notify
the owner on the existing jobs-bot route (`send_telegram_alert` →
`telegram_routing.send_job_message`):

- an HH application cycle that exits with an error that is **not** a
  confirmed login loss
- a Kwork inbox that stays `PAGE_NOT_READY` (or otherwise unread) for
  several observations in a row

Neither alert submits an application, sends a reply, or emits a
new-message card. A dry run does not deliver and does not write the ledger.

## HH application cycle

`scripts/run_hh_autopilot_cycle.py` already calls this. No new task.

Confirmed `HH_AUTH_REQUIRED` still uses the auth alert and its 12-hour
marker (`logs/hh_auth_alert.sent`). Anything else — for example
`ImportError: cannot import name hh_structured_preflight`, with zero
submits and exit code 1 — sends one operational message:

- says this is not a login loss and that signing in again is not required
- includes the sanitized error and how many applications this cycle sent
- repeats only after the cooldown, and only while the failure continues
- sends one recovery note the next time the cycle exits 0

| Setting | Default | Meaning |
| --- | --- | --- |
| `HH_AUTOPILOT_FAILURE_ALERT_COOLDOWN_SECONDS` | `21600` (6h) | Minimum gap between operational alerts for the same outage |
| state file | `logs/hh_autopilot_ops_alert.json` | Cooldown, streak, and whether recovery is still owed |

A 4-hour schedule therefore pages once, stays quiet on the next run, and
pages again only if the failure is still there after six hours.

## Kwork inbox

`scripts/run_kwork_inbox_monitor.ps1` and
`scripts/run_kwork_monitor_silent.vbs` launch
`scripts/run_kwork_inbox_monitor_cycle.py`. The default is dry-run. `--live`
is what the scheduled task passes.

After `process_monitor_cycle` returns, the runner reads that report:

- `auth_status` — `PAGE_NOT_READY`, `CDP_UNAVAILABLE`, and `UNKNOWN` are
  operational failures (the monitor itself sends nothing for these)
- `scanned_dialogues` — dialogs actually read; zero on those failures
- `AUTH_REQUIRED` and `AUTH_LOST` are not copied into this ledger. The
  monitor already sends its own login warning, with its own cooldown, and
  only after two confirmed login-page reads. Treating those as "page
  recovered" would clear a real not-ready streak.
- `OK`, including zero dialogues, is a successful read. It clears the streak
  and sends one recovery note if an operational alert is open.

New buyer-message cards stay inside `process_monitor_cycle`. The health
alert is a separate send and does not repeat those cards. Dry-run builds the
decision and does not send or write `logs/kwork_inbox_ops_alert.json`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `KWORK_INBOX_NOT_READY_STREAK` | `3` | Consecutive unhealthy live cycles before the first alert |
| `KWORK_INBOX_ALERT_COOLDOWN_SECONDS` | `21600` (6h) | Repeat gap while the inbox stays unhealthy |
| state file | `logs/kwork_inbox_ops_alert.json` | Streak and cooldown |

`scripts/report_kwork_inbox_health.py` remains a manual preview of the same
ledger. The scheduled cycle does not need it.

## Files added from local feature branches

These were not on `chore-ruff-cleanup`. They were copied from local commits
that were never pushed. They were not decompiled and their control flow was
not redesigned. Six `except: pass` sites now log at debug, because this repo
does not allow a silent handler. `sqlite3.Row` still uses `in row.keys()`:
`in row` checks values, not column names. Broad `except Exception` in those
two files stays, and `ruff.toml` ignores BLE001 there for that reason.

| File | Provenance |
| --- | --- |
| `ai_assistant/gig/kwork_inbox_monitor.py` | `75f1d55` on local `feat/untracked-product-batch` (6 Nov 2026, 22:54 Bangkok) |
| `ai_assistant/telegram_routing.py` | `2a1370e` on local `feat/telegram-notifications`. Required import of the monitor (`from .. import telegram_routing`) and of `hh_autopilot.send_telegram_alert` |
| `scripts/run_kwork_inbox_monitor_cycle.py` | Local untracked Aorus15 runner, plus the health hook above |

Adding the monitor file does not close the whole import graph. The module
also calls `hh_browser_provider.list_tabs`, `open_tab`, `make_target_evaluate`,
and `cdp_version` (already in this repo) and `websockets` when it has to open
a tab over the browser socket. A live scan still needs a reachable CDP on
`127.0.0.1:9111` and a configured jobs-bot route. This checkout was not run
against that browser.

## Still not in this branch

- `ai_assistant/auth_notifications.py` — present on local `feat/telegram-notifications` (`2a1370e`). The HH cycle uses its `AUTH_FAILURE_MESSAGE` when the module is installed, and a short fallback otherwise. It is not required by the Kwork monitor, so it was not copied.
- `ai_assistant/hh_structured_preflight.py` — recovered read-only from local `8be421a` on `feat/hh-shortlist-preflight` (6 Nov 2026, 22:54 Bangkok). It is intentionally not added. The HH operational alert already includes the `ImportError` text when discovery cannot import it. Merging that feature branch would change preflight and submission behavior and is separate work.

Neither source was lost. Both still live on those local branches.
