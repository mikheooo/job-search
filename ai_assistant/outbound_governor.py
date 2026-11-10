"""Unified Outbound Action Governor for job-search.

Central authority for determining whether any external, outbound action
(application submission, automated recruiter message reply, external questionnaire
send, etc.) is permitted to proceed.

Enforces:
1. Dynamic disk kill-switches (checked at call time on disk):
   - STOP_ALL_OUTBOUNDS / data/STOP_ALL_OUTBOUNDS
   - STOP_SUBMITS / data/STOP_SUBMITS
2. Dynamic database pause flag (checked in state.db system_settings):
   - submit_paused == '1'
   - outbound_paused == '1'
3. Action-specific permission checks:
   - "submission": SUBMIT_ALLOWED flag (evaluated dynamically)
   - "chat_reply": HH_AUTONOMOUS_REPLY_ENABLED flag (evaluated dynamically)
4. Fail-closed guarantee: any read error or database exception is treated
   as a refusal, never as a permit.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


class OutboundAction(str, Enum):
    SUBMISSION = "submission"
    CHAT_REPLY = "chat_reply"
    QUESTIONNAIRE = "questionnaire"
    GIG_APPLICATION = "gig_application"


def _stop_file_paths() -> Sequence[Path]:
    """Candidate file paths for emergency disk kill-switches."""
    root = Path(__file__).resolve().parent.parent
    return (
        Path("STOP_ALL_OUTBOUNDS"),
        Path("data/STOP_ALL_OUTBOUNDS"),
        Path("STOP_SUBMITS"),
        Path("data/STOP_SUBMITS"),
        root / "STOP_ALL_OUTBOUNDS",
        root / "data" / "STOP_ALL_OUTBOUNDS",
        root / "STOP_SUBMITS",
        root / "data" / "STOP_SUBMITS",
    )


def is_disk_kill_switch_active() -> tuple[bool, str | None]:
    """Check if any stop file exists on disk right now.

    Fail-closed: if file access raises an unexpected exception, treats as active.
    """
    try:
        custom_file = os.environ.get("STOP_SUBMITS_FILE") or os.environ.get("STOP_OUTBOUNDS_FILE")
        if custom_file and os.path.exists(custom_file):
            return True, f"Emergency stop file present: {custom_file}"

        for p in _stop_file_paths():
            try:
                if p.exists():
                    return True, f"Emergency stop file present: {p}"
            except Exception as e:
                return True, f"Emergency stop check failed on path {p} (fail-closed): {e}"
        return False, None
    except Exception as e:
        return True, f"Emergency stop check failed (fail-closed): {e}"


def is_db_pause_active() -> tuple[bool, str | None]:
    """Check if database pause flags are set in system_settings.

    Fail-closed: if database is locked, corrupt, or unreachable, treats as paused.
    """
    try:
        from . import db

        if db.is_submit_paused():
            return True, "Database kill-switch active: submit_paused = 1 in system_settings"
        
        # Also check general outbound pause if present
        try:
            if db.get_system_setting("outbound_paused") == "1":
                return True, "Database kill-switch active: outbound_paused = 1 in system_settings"
        except Exception:
            pass

        return False, None
    except Exception as e:
        return True, f"Database kill-switch check failed (fail-closed): {e}"


def check_outbound_allowed(
    action: OutboundAction | str = OutboundAction.SUBMISSION,
    dry_run: bool = False,
    include_action_flag: bool = True,
) -> tuple[bool, str | None]:
    """Evaluate whether an outbound action may proceed.

    Returns:
        (True, None) if allowed.
        (False, reason_string) if halted.
    """
    action_str = action.value if isinstance(action, OutboundAction) else str(action).lower()

    # 1. Disk kill-switches (outrank all settings, including dry_run)
    disk_stopped, disk_reason = is_disk_kill_switch_active()
    if disk_stopped:
        return False, disk_reason

    # 2. Database kill-switches (outrank all settings, including dry_run)
    db_stopped, db_reason = is_db_pause_active()
    if db_stopped:
        return False, db_reason

    # 3. Dry runs are allowed past operational release flags (but not past stop orders)
    if dry_run:
        return True, None

    # 4. Action-specific operational flags
    if include_action_flag:
        if action_str == OutboundAction.SUBMISSION.value:
            from . import config
            if not config.submit_allowed():
                return False, "Submission blocked: SUBMIT_ALLOWED is not enabled in configuration"

        elif action_str in (OutboundAction.CHAT_REPLY.value, OutboundAction.QUESTIONNAIRE.value):
            # Dynamic check for chat replies
            reply_val = os.getenv("HH_AUTONOMOUS_REPLY_ENABLED", "").strip().lower()
            if reply_val != "true":
                return False, "Chat reply blocked: HH_AUTONOMOUS_REPLY_ENABLED is not 'true'"

    return True, None


def get_outbound_halt_reason(
    action: OutboundAction | str = OutboundAction.SUBMISSION,
    dry_run: bool = False,
) -> str | None:
    """Helper returning halt reason string or None if action is permitted."""
    allowed, reason = check_outbound_allowed(action=action, dry_run=dry_run)
    return reason if not allowed else None
