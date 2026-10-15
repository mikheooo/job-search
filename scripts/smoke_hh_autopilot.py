"""Smoke-test for the HH autopilot cycle — proves it no longer dies at step 1.

Why this exists
---------------
On 2026-10-09 19:05 the scheduled autopilot started failing with
`sqlite3.OperationalError: no such column: vacancy_id` inside
`get_known_vacancy_ids()` (hh_autopilot.py:111). That killed the cycle before
it did anything at all, every 4 hours.

This script exercises the real code path with two hard safety rails:

  1. `max_submits=0` — the submission loop in `run_autopilot_cycle` breaks on
     its first line (`if submitted_count >= max_submits: break`), so no
     application can be sent. `SUBMIT_ALLOWED=false` is set by the cycle too.
  2. `process_negotiation_alerts` is replaced with a no-op, so the cycle cannot
     push Telegram alerts while we are only testing.

What it DOES do: discovery (a read-only HH search over the headless browser),
which is exactly the part that failed with HH_AUTH_REQUIRED on 2026-10-09.

Run:  .venv/Scripts/python.exe scripts/smoke_hh_autopilot.py
"""

from __future__ import annotations

import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def main() -> int:
    # Rail 2: no Telegram alerts during the smoke test.
    from ai_assistant import hh_chat_monitor

    def _no_alerts(cards, dry_run=False):  # noqa: ANN001
        print(f"[smoke] process_negotiation_alerts suppressed ({len(cards)} cards)")
        return 0

    hh_chat_monitor.process_negotiation_alerts = _no_alerts

    from ai_assistant.hh_autopilot import get_known_vacancy_ids, run_autopilot_cycle

    known = get_known_vacancy_ids()
    print(f"[smoke] get_known_vacancy_ids() -> {len(known)} ids  (was raising before the fix)")

    # Rail 1: max_submits=0 => the submit loop breaks before touching HH.
    result = run_autopilot_cycle(max_submits=0, read_limit=5)

    print(f"[smoke] cycle result: {result}")
    print(f"[smoke] submitted = {result.get('submitted')} (must be 0)")
    print(f"[smoke] status = {result.get('status')}")

    if result.get("submitted") != 0:
        print("[smoke] FAIL: something was submitted during a smoke test!")
        return 2
    if result.get("status") != "SUCCESS":
        print("[smoke] cycle did not reach SUCCESS — see the error above.")
        return 1
    print("[smoke] PASS: cycle runs past the old crash point.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
