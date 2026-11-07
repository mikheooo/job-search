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
`scripts/run_kwork_inbox_monitor_cycle.py`. **That cycle script is not in
this repository** (local-only). This change does not add or replace it, so
a machine that already has the file keeps it.

What is in the repo is the decision helper and a small reporter:

```text
python scripts/report_kwork_inbox_health.py --status PAGE_NOT_READY --checked 0 --dry-run
python scripts/report_kwork_inbox_health.py --status CHECKED --checked 4 --live
```

`--dry-run` prints JSON and does not send. `--live` delivers only after the
streak threshold. One not-ready read is not an incident. A healthy read with
`checked=0` (empty inbox) is not an incident. A status that already says
the session expired is ignored here, so this alert cannot claim that auth
expired.

| Setting | Default | Meaning |
| --- | --- | --- |
| `KWORK_INBOX_NOT_READY_STREAK` | `3` | Consecutive unhealthy observations before the first alert |
| `KWORK_INBOX_ALERT_COOLDOWN_SECONDS` | `21600` (6h) | Repeat gap while the inbox stays unhealthy |
| state file | `logs/kwork_inbox_ops_alert.json` | Streak and cooldown |

Unhealthy means `PAGE_NOT_READY`, or a failed read (`ERROR`, `FAILED`,
`EXCEPTION`, `UNAVAILABLE`). New Kwork message cards stay on the existing
cycle path; this reporter never sends them.

### Wiring the local cycle

Inside the local script, after the read and without removing its message
alerts:

```python
from ai_assistant.automation_alerts import deliver_owner_alert, observe_kwork_inbox

observe_kwork_inbox(
    status=result_status,          # "PAGE_NOT_READY", "CHECKED", ...
    checked=checked_count,         # dialogs actually read
    error=error_text or None,      # optional; tokens are stripped
    state_path=pathlib.Path("logs/kwork_inbox_ops_alert.json"),
    send_fn=None if dry_run else deliver_owner_alert,
    dry_run=dry_run,
)
```

Or schedule the reporter next to the existing task, passing the status that
cycle already computed. Do not point either command at a bot token; the
route reads the environment the jobs bot already uses.

## What this checkout does not contain

These names are imported by code that already ships, and they are absent
here. They were not recreated, because a local copy would be overwritten:

- `ai_assistant/telegram_routing.py` — jobs-bot send used by `send_telegram_alert`
- `ai_assistant/auth_notifications.py` — auth failure / recovery wording
- `ai_assistant/hh_structured_preflight.py` — imported by `hh_readonly_operation`
  (matches the logged `cannot import name hh_structured_preflight`)
- `scripts/run_kwork_inbox_monitor_cycle.py` — inbox monitor the launchers call

On a machine where auth alerts already arrive, the first two are present
and the new alerts use that same route. Where `telegram_routing` is missing,
delivery returns false and the ledger does not mark the alert as sent, so a
later run can retry. Fixing the missing preflight module is a separate
change; the alert only reports the failure.
