"""T7.8: which autonomous modes may emit anything, and the one place that decides.

WHY THIS LIVES IN ITS OWN MODULE
--------------------------------
The cycle and its configuration live in ``hh_autonomous_agent.py``, and
``docs/ble001_triage.md`` cites that file by line number (443, 934, 983, 994,
1002). Keeping the mode vocabulary here means the agent file needs no new lines
above those, and the predicate gets one importable home for the tests instead of
being spelled out again at each call site.

THE TWO MODES
-------------
``FULL`` is the historical behaviour and is left exactly as it was. ``SEARCH_ONLY``
searches and reads, and nothing else: no application, no answer to a recruiter,
no new message, no approval written and no application status moved.

WHY THE PREDICATE IS FAIL-CLOSED
--------------------------------
A missing, empty, misspelled or non-string mode is **not** FULL, so it cannot
authorise an outbound action. This is the same shape as the geographic fuse in
``eligibility``: an assessment that is absent or wrong must never read as
permission. ``autonomous_outbound_allowed(None)`` is False on purpose - the safe
answer to "I do not know which mode this is" is "emit nothing".
"""

from __future__ import annotations

from typing import Any

FULL = "FULL"
SEARCH_ONLY = "SEARCH_ONLY"
MODES: tuple[str, ...] = (FULL, SEARCH_ONLY)


def normalise(mode: Any) -> str:
    """Accept a plain string or an enum-like object. Anything else is not a mode."""
    value = getattr(mode, "value", mode)
    if not isinstance(value, str):
        return ""
    return value.strip().upper()


def outbound_allowed(mode: Any) -> bool:
    """True only for an explicit FULL. ``None``, ``""`` and typos are all False."""
    return normalise(mode) == FULL


def refusal(mode: Any, action: str) -> str:
    """The reason string for a refused action, for a log line or an audit row."""
    return f"{normalise(mode) or '(unset)'} mode forbids {action}"
