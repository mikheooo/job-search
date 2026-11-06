"""Notification priority & frequency policy (Phase 5C).

Today the job-search system has several independent delivery paths that share
the database but not the dedup key, relevance gate, or schedule, so the same
item reaches Telegram through more than one route and rejected items come back.
The only batched, filtered path is the daily digest.

This module is the *single decision point* for "may this item be pushed to
Telegram right now, and in which bucket?". It is pure (no I/O, no network, no
DB) so it can be unit-tested with mocks and wired into any sender later.

Buckets
-------
``IMMEDIATE``  – urgent employer messages (recruiter reply / interview invite):
                 always sent at once.
``GIG_HIGH``   – high-relevance freelance orders: sent as their own message,
                 but only above a relevance floor.
``DIGEST``     – everything else worth keeping: queued for the ONE daily digest.
``SUPPRESS``   – duplicates, rejected, and clearly irrelevant items:
                 never pushed again (records are still kept in the DB by the
                 callers — suppression here only stops *notifications*).

Nothing in this module sends anything. It only classifies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Buckets
IMMEDIATE = "IMMEDIATE"
GIG_HIGH = "GIG_HIGH"
DIGEST = "DIGEST"
SUPPRESS = "SUPPRESS"

# Notification kinds that are always time-critical.
URGENT_KINDS = frozenset({
    "recruiter_reply",
    "interview_invitation",
    "employer_message",
    "offer",
})

# Relevance floors (documented defaults, overridable per call).
# NOTE: the digest floor is deliberately kept at the gig autopilot's existing
# 48 so that switching to the policy NEVER drops an order that used to be
# notified (the brief forbids silently raising the floor). 75 is only the
# "send now" gate; everything at/above 48 and below it is batched into the
# daily digest instead of being sent immediately.
DEFAULT_HIGH_RELEVANCE_FLOOR = 75.0
DEFAULT_DIGEST_FLOOR = 48.0


@dataclass(frozen=True)
class Decision:
    """Outcome of :func:`classify`."""

    bucket: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def send_now(self) -> bool:
        return self.bucket in (IMMEDIATE, GIG_HIGH)


def classify(
    kind: str,
    *,
    score: float | None = None,
    is_duplicate: bool = False,
    is_rejected: bool = False,
    is_irrelevant: bool = False,
    high_relevance_floor: float = DEFAULT_HIGH_RELEVANCE_FLOOR,
    digest_floor: float = DEFAULT_DIGEST_FLOOR,
) -> Decision:
    """Decide the delivery bucket for one item.

    Precedence is deliberate: suppression (never resend / never spam) beats
    urgency, because re-notifying a rejected or duplicate item is exactly the
    behaviour being fixed.
    """
    kind_key = (kind or "").strip().lower()

    if is_duplicate:
        return Decision(SUPPRESS, "duplicate: already delivered", {"kind": kind_key})
    if is_rejected:
        return Decision(SUPPRESS, "rejected by user: do not resend", {"kind": kind_key})
    if is_irrelevant:
        return Decision(SUPPRESS, "clearly irrelevant: filtered before send",
                        {"kind": kind_key})

    if kind_key in URGENT_KINDS:
        return Decision(IMMEDIATE, "urgent employer message",
                        {"kind": kind_key, "score": score})

    if kind_key == "gig":
        if score is not None and score >= high_relevance_floor:
            return Decision(GIG_HIGH, "high-relevance freelance order",
                            {"kind": kind_key, "score": score})
        if score is not None and score < digest_floor:
            return Decision(SUPPRESS, "gig below relevance floor",
                            {"kind": kind_key, "score": score,
                             "digest_floor": digest_floor})
        return Decision(DIGEST, "gig kept for the daily digest",
                        {"kind": kind_key, "score": score})

    # Vacancies and anything else non-urgent.
    if score is not None and score < digest_floor:
        return Decision(SUPPRESS, "below relevance floor",
                        {"kind": kind_key, "score": score, "digest_floor": digest_floor})
    return Decision(DIGEST, "kept for the daily digest",
                    {"kind": kind_key, "score": score})


def classify_vacancy_row(
    row: dict[str, Any],
    *,
    already_delivered: bool = False,
    high_relevance_floor: float = DEFAULT_HIGH_RELEVANCE_FLOOR,
    digest_floor: float = DEFAULT_DIGEST_FLOOR,
) -> Decision:
    """Convenience wrapper for a DB-style vacancy/gig row (dict).

    Recognises the states set elsewhere in the codebase: ``REJECTED`` (skip
    button / feedback 👎) and ``SUBMITTED`` (owner already applied) count as
    "do not notify again".
    """
    state = str(row.get("state") or "").upper()
    is_rejected = state in ("REJECTED", "WITHDRAWN", "SKIPPED")
    kind = "gig" if row.get("is_gig") else "vacancy"
    score = row.get("score", row.get("opportunity_score"))
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return classify(
        kind,
        score=score,
        is_duplicate=already_delivered,
        is_rejected=is_rejected,
        is_irrelevant=bool(row.get("is_irrelevant")),
        high_relevance_floor=high_relevance_floor,
        digest_floor=digest_floor,
    )


# Documented policy, exposed for the report / tests to assert against.
POLICY = {
    URGENT_KINDS: "sent immediately",
    "gig >= high_relevance_floor": "sent as its own message",
    "everything else >= digest_floor": "one daily digest",
    "duplicates / rejected / irrelevant": "never pushed again (still stored in DB)",
}
POLICY_SUMMARY = (
    "Urgent employer messages go out immediately; high-relevance freelance "
    "orders (>={high}) get their own message; everything else at/above the "
    "digest floor waits for one daily digest; duplicates, rejected and "
    "irrelevant items are never pushed again but remain in the database."
)

__all__ = [
    "DEFAULT_DIGEST_FLOOR",
    "DEFAULT_HIGH_RELEVANCE_FLOOR",
    "DIGEST",
    "Decision",
    "GIG_HIGH",
    "IMMEDIATE",
    "POLICY",
    "POLICY_SUMMARY",
    "SUPPRESS",
    "URGENT_KINDS",
    "classify",
    "classify_vacancy_row",
]
