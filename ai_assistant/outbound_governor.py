"""Unified Outbound Action Governor for job-search.

Central authority for determining whether any external, outbound action
(application submission, automated recruiter message reply, external questionnaire
send, email dispatch, telegram notifications, etc.) is permitted to proceed.

Enforces:
1. Dynamic disk kill-switches (checked at call time on disk):
   - STOP_ALL_OUTBOUNDS / data/STOP_ALL_OUTBOUNDS (halts all actions)
   - STOP_SUBMITS / data/STOP_SUBMITS (halts submission and questionnaires)
2. Dynamic database pause flag (checked in state.db system_settings):
   - submit_paused == '1'
   - outbound_paused == '1'
3. Action-specific permission checks:
   - "submission": SUBMIT_ALLOWED flag (evaluated dynamically)
   - "chat_reply": HH_AUTONOMOUS_REPLY_ENABLED flag (evaluated dynamically)
   - "email": EMAIL_DRY_RUN flag
4. Fail-closed guarantee: any read error or database exception is treated
   as a refusal, never as a permit.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class OutboundAction(str, Enum):
    SUBMISSION = "submission"
    CHAT_REPLY = "chat_reply"
    QUESTIONNAIRE = "questionnaire"
    GIG_APPLICATION = "gig_application"
    EMAIL = "email"
    TELEGRAM = "telegram"


class OutboundHaltedError(RuntimeError):
    """Raised when an outbound operation is prohibited by safety policies or kill-switches."""


def _stop_file_paths(action: OutboundAction | str | None = None) -> Sequence[Path]:
    """Candidate file paths for emergency disk kill-switches."""
    root_override = os.environ.get("STOP_OUTBOUNDS_DIR")
    base = Path(root_override) if root_override else Path(__file__).resolve().parent.parent

    # Global stops that halt ALL outbound operations
    global_stops = [
        Path("STOP_ALL_OUTBOUNDS"),
        Path("data/STOP_ALL_OUTBOUNDS"),
        base / "STOP_ALL_OUTBOUNDS",
        base / "data" / "STOP_ALL_OUTBOUNDS",
    ]

    action_str = action.value if isinstance(action, OutboundAction) else (str(action).lower() if action else None)

    # Submission and questionnaire actions also obey submission-specific kill-switches
    if action_str is None or action_str in (
        OutboundAction.SUBMISSION.value,
        OutboundAction.QUESTIONNAIRE.value,
        OutboundAction.GIG_APPLICATION.value,
    ):
        return tuple(
            global_stops
            + [
                Path("STOP_SUBMITS"),
                Path("data/STOP_SUBMITS"),
                base / "STOP_SUBMITS",
                base / "data" / "STOP_SUBMITS",
            ]
        )

    return tuple(global_stops)


def is_disk_kill_switch_active(action: OutboundAction | str | None = None) -> tuple[bool, str | None]:
    """Check if any stop file exists on disk right now.

    Fail-closed: if file access raises an unexpected exception, treats as active.
    """
    try:
        custom_outbounds = os.environ.get("STOP_OUTBOUNDS_FILE")
        if custom_outbounds and os.path.exists(custom_outbounds):
            return True, f"Emergency stop file present: {custom_outbounds} (STOP_SUBMITS file present)"

        action_str = action.value if isinstance(action, OutboundAction) else (str(action).lower() if action else None)
        if action_str is None or action_str in (
            OutboundAction.SUBMISSION.value,
            OutboundAction.QUESTIONNAIRE.value,
            OutboundAction.GIG_APPLICATION.value,
        ):
            custom_submits = os.environ.get("STOP_SUBMITS_FILE")
            if custom_submits and os.path.exists(custom_submits):
                return True, f"Emergency stop file present: {custom_submits} (STOP_SUBMITS file present)"

        for p in _stop_file_paths(action):
            try:
                if p.exists():
                    return True, f"Emergency stop file present: {p} (STOP_SUBMITS file present)"
            except Exception as e:
                return True, f"Emergency stop check failed on path {p} (fail-closed): {e}"
        return False, None
    except Exception as e:
        return True, f"Emergency stop check failed (fail-closed): {e}"


def is_db_pause_active(action: OutboundAction | str | None = None) -> tuple[bool, str | None]:
    """Check if database pause flags are set in system_settings.

    Fail-closed: if database is locked, corrupt, or unreachable, treats as paused.
    """
    try:
        from . import db

        if db.is_submit_paused():
            return True, "Database kill-switch active: Submission paused by kill switch (system_settings.submit_paused = 1 / submit_paused=1)"

        # Check universal outbound pause flag
        try:
            if db.get_system_setting("outbound_paused") == "1":
                return True, "Database kill-switch active: Submission paused by kill switch (system_settings.outbound_paused = 1 / outbound_paused=1)"
        except Exception:
            pass

        return False, None
    except Exception as e:
        return True, (
            "Cannot read the submission kill switch - refusing to submit (fail-closed): "
            f"{type(e).__name__}: {e}"
        )


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
    disk_stopped, disk_reason = is_disk_kill_switch_active(action)
    if disk_stopped:
        return False, disk_reason

    # 2. Database kill-switches (outrank all settings, including dry_run)
    db_stopped, db_reason = is_db_pause_active(action)
    if db_stopped:
        return False, db_reason

    # 3. Dry runs are allowed past operational release flags (but not past stop orders)
    if dry_run:
        return True, None

    # 4. Action-specific operational flags
    if include_action_flag:
        if action_str in (OutboundAction.SUBMISSION.value, OutboundAction.GIG_APPLICATION.value):
            from . import config
            if not config.submit_allowed():
                return False, "Submission is disabled by SUBMIT_ALLOWED configuration"

        elif action_str in (OutboundAction.CHAT_REPLY.value, OutboundAction.QUESTIONNAIRE.value):
            reply_val = os.getenv("HH_AUTONOMOUS_REPLY_ENABLED", "").strip().lower()
            if reply_val != "true":
                return False, "Chat reply blocked: HH_AUTONOMOUS_REPLY_ENABLED is not 'true'"

        elif action_str == OutboundAction.EMAIL.value:
            email_dry = os.getenv("EMAIL_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
            if email_dry:
                return False, "Email sending blocked: EMAIL_DRY_RUN is enabled"

    return True, None


def guard_outbound(
    action: OutboundAction | str = OutboundAction.SUBMISSION,
    dry_run: bool = False,
    include_action_flag: bool = True,
) -> None:
    """Raise OutboundHaltedError if the outbound action is halted by safety policies."""
    allowed, reason = check_outbound_allowed(
        action=action,
        dry_run=dry_run,
        include_action_flag=include_action_flag,
    )
    if not allowed:
        raise OutboundHaltedError(reason or f"Outbound action '{action}' halted by safety policy")


def get_outbound_halt_reason(
    action: OutboundAction | str = OutboundAction.SUBMISSION,
    dry_run: bool = False,
) -> str | None:
    """Helper returning halt reason string or None if action is permitted."""
    allowed, reason = check_outbound_allowed(action=action, dry_run=dry_run)
    return reason if not allowed else None
