"""Shim: CDP submit lives in ai_assistant.hh_cdp_submit.

Kept so CLI invocations and older tests that load tools/.t722_send.py still work.
Production (hh_autopilot) imports ai_assistant.hh_cdp_submit directly.
"""
from __future__ import annotations

from ai_assistant.hh_cdp_submit import *  # noqa: F403
from ai_assistant.hh_cdp_submit import main, run_brief, run_submit  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
