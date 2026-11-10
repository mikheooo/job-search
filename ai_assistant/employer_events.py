"""Employer events bridge (Phase 4A).

Connects employer-side signals (interview invitations, rejections, offers
detected by HH watchers) to the application_tracking state machine WITHOUT
mixing them with filter rejections or HH-automation technical states.

Design constraints (no prod schema changes in this phase):
- No new ApplicationStatus values: employer refusal reuses REJECTED, but the
  origin is recorded in tracking.notes + history.note as ``employer:<kind>``.
  Filter rejections carry their existing filter notes. Analytics splits by
  these markers (see classify_rejection_origin).
- All transitions go through transition_application(): legality enforced,
  tracking UPDATE + history INSERT committed atomically, no-op on same state.
- Idempotency: the caller passes a stable event_key (message fingerprint,
  card_key, ...). A history row already carrying that key means "processed".
- Staleness: an event older than the row's last_status_change_at never moves
  the status backwards; it is recorded as observed-only.
- Ambiguous classification must NOT auto-transition: callers only invoke the
  bridge for high-confidence kinds (interview / employer_reject / offer).
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from ai_assistant import db
from ai_assistant.application_tracking import (
    ApplicationRecord,
    ApplicationStatus,
    get_application_history,
    get_application_status,
    is_valid_transition,
    transition_application,
)

logger = logging.getLogger("employer_events")

EmployerKind = Literal["interview", "employer_reject", "offer"]

# Target tracking status per employer event kind.
KIND_TARGETS: dict[str, ApplicationStatus] = {
    "interview": ApplicationStatus.INTERVIEW,
    "employer_reject": ApplicationStatus.REJECTED,
    "offer": ApplicationStatus.OFFER,
}

# Note markers written by filter rejections (watcher.py + manual NOT_INTERESTED).
FILTER_REJECT_MARKERS = (
    "Ineligible for candidate country",
    "Remote constraint violated",
    "Hard constraints rejected",
    "Matcher SKIP",
    "Marked NOT_INTERESTED via Telegram",
)

# Note markers written by this bridge for employer decisions.
EMPLOYER_REJECT_MARKERS = ("employer:employer_reject", "employer declined", "DISCARD", "negotiation")


def classify_rejection_origin(
    track_notes: str | None, history_notes: list[str] | None = None
) -> Literal["filter", "employer", "unknown"]:
    """Split filter rejections from employer refusals using recorded notes.

    Historical rows without any marker classify as "filter": automatic
    employer transitions did not exist before this bridge, so an unmarked
    REJECTED could only have come from filters, manual candidate skips, or
    one-off reconciles documented as filter-side.
    """
    notes = [track_notes or ""] + list(history_notes or [])
    blob = "\n".join(notes)
    if any(m in blob for m in EMPLOYER_REJECT_MARKERS):
        return "employer"
    if any(m in blob for m in FILTER_REJECT_MARKERS):
        return "filter"
    return "filter" if blob.strip() == "" else "unknown"


def resolve_application_for_event(
    vacancy_stable_id: str | None = None,
    employer: str | None = None,
    title: str | None = None,
) -> ApplicationRecord | None:
    """Resolve EXACTLY ONE tracking row for an employer event.

    Exact stable_id wins. Employer+title matching requires a single candidate;
    zero or ambiguous matches return None (never touch a чужой row).
    """
    from ai_assistant.application_tracking import list_applications

    if vacancy_stable_id:
        rec = get_application_status(vacancy_stable_id)
        return rec
    if not employer or not title:
        return None
    norm = lambda s: " ".join((s or "").lower().split())
    want_e, want_t = norm(employer), norm(title)
    candidates = [
        r
        for r in list_applications(limit=10000)
        if norm(r.company) == want_e and norm(r.title) == want_t
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        logger.warning(
            "Ambiguous employer event for %r @ %r: %d tracking rows; refusing.",
            title, employer, len(candidates),
        )
    return None


def resolve_vacancy_for_conversation(
    conversation_id: str, conn=None
) -> str | None:
    """Map an HH conversation to its vacancy via recorded message events.

    hh_message_events rows carry vacancy_stable_id (written by the autonomous
    agent). Requires a single distinct vacancy; else None. Pass an existing
    connection to stay on it (read-only callers); otherwise opens one.
    """
    if not conversation_id:
        return None
    own = conn is None
    if own:
        db.init_db()
        conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT vacancy_stable_id FROM hh_message_events"
            " WHERE conversation_id = ? AND vacancy_stable_id IS NOT NULL",
            (conversation_id,),
        )
        rows = [r[0] for r in cur.fetchall() if r[0]]
        return rows[0] if len(rows) == 1 else None
    finally:
        if own:
            conn.close()


def _event_already_applied(vacancy_stable_id: str, event_key: str) -> bool:
    for h in get_application_history(vacancy_stable_id):
        if event_key and event_key in (h.note or ""):
            return True
    return False


def apply_employer_event(
    *,
    kind: EmployerKind,
    source: str,
    event_key: str,
    vacancy_stable_id: str | None = None,
    employer: str | None = None,
    title: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Apply one employer decision to application_tracking, safely.

    Returns a result dict with outcome in:
    applied | already-applied | not-found | ambiguous | illegal-transition |
    stale-event | invalid-kind. Never raises for business cases (only for
    programming errors like unknown kind). Never touches vacancies.state.
    """
    if kind not in KIND_TARGETS:
        raise ValueError(f"Unknown employer event kind: {kind!r}")
    if not event_key:
        return {"outcome": "ambiguous", "reason": "missing event_key"}
    target = KIND_TARGETS[kind]

    rec = resolve_application_for_event(vacancy_stable_id, employer, title)
    if rec is None:
        if vacancy_stable_id and get_application_status(vacancy_stable_id) is None:
            return {"outcome": "not-found", "reason": "no tracking row"}
        return {"outcome": "ambiguous", "reason": "cannot resolve unique application"}

    sid = rec.vacancy_stable_id
    if _event_already_applied(sid, event_key):
        return {"outcome": "already-applied", "stable_id": sid, "status": rec.status.value}

    if occurred_at and rec.last_status_change_at and occurred_at < rec.last_status_change_at:
        return {
            "outcome": "stale-event",
            "stable_id": sid,
            "status": rec.status.value,
            "reason": f"event at {occurred_at} predates status change at {rec.last_status_change_at}",
        }

    if rec.status == target:
        return {"outcome": "already-applied", "stable_id": sid, "status": rec.status.value}

    if not is_valid_transition(rec.status, target):
        return {
            "outcome": "illegal-transition",
            "stable_id": sid,
            "status": rec.status.value,
            "reason": f"{rec.status.value} -> {target.value} not allowed",
        }

    note = f"employer:{kind} via {source} [{event_key}]"
    updated = transition_application(sid, target, note=note)
    return {"outcome": "applied", "stable_id": sid, "status": updated.status.value}
