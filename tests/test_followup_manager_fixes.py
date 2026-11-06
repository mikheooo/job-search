"""P0 regressions for followup_manager (audit 2026-10-19, findings F1 + F2).

F1 (false follow-ups): ``find_pending_followups()`` used to select *any* hh
vacancy (the ``OR v.source = 'hh'`` clause cancelled the state filter) and had
no age window at all, so the card claimed "Отклик отправлен ранее, ответа пока
нет" for vacancies that were never applied to, including ``state='NEW'``.

F2 (PK bug): ``record_followup_sent()``/``mute_followup()`` inserted
``f"%{suffix}"`` as the PRIMARY KEY, never touched the real row, and always
returned ``True`` -- so the Telegram toasts lied, reminders repeated and mute
did not work.

Every test runs against the per-test scratch SQLite that ``tests/conftest.py``
installs through ``config.DB_FILE``; the production ``state.db`` is never
opened for writing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_assistant import db
from ai_assistant import followup_manager as fm


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_db():
    """Full schema + the follow-up table on the per-test scratch DB."""
    db.init_db()
    fm.init_followup_table()
    return db


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(days: float) -> str:
    return _iso(_utcnow() - timedelta(days=days))


def _add_vacancy(stable_id: str, *, state: str = "OUTREACH_READY", source: str = "hh",
                 title: str = "DevOps Engineer", company: str = "Acme") -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO vacancies (stable_id, source, source_job_id, title, company,"
            " description, job_url, first_seen_at, state)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id,
                source,
                stable_id,
                title,
                company,
                "python docker linux",
                f"https://example.test/vacancy/{stable_id}",
                _iso(_utcnow()),
                state,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _add_tracking(stable_id: str, status: str, *, applied_at: str | None = None,
                  last_status_change_at: str | None = None) -> None:
    """Insert one application_tracking row (one row per vacancy, as in schema)."""
    changed = last_status_change_at or _days_ago(5)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO application_tracking"
            " (vacancy_stable_id, status, company, title, created_at, updated_at,"
            "  applied_at, last_status_change_at)"
            " VALUES (?, ?, 'Acme', 'DevOps Engineer', ?, ?, ?, ?)",
            (stable_id, status, changed, changed, applied_at, changed),
        )
        conn.commit()
    finally:
        conn.close()


def _add_submission(stable_id: str, *, status: str = "SUBMITTED",
                    submitted_at: str | None = None) -> None:
    at = submitted_at or _days_ago(5)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO application_submissions"
            " (vacancy_stable_id, submission_id, executor_version, status,"
            "  submitted_at, created_at, updated_at)"
            " VALUES (?, 'sub-1', 'v1', ?, ?, ?, ?)",
            (stable_id, status, at, at, at),
        )
        conn.commit()
    finally:
        conn.close()


def _followup_rows() -> list[tuple[str, int, str]]:
    conn = db.get_connection()
    try:
        return [
            (row[0], row[1], row[2])
            for row in conn.execute(
                "SELECT stable_id, followup_count, status FROM application_followups"
            ).fetchall()
        ]
    finally:
        conn.close()


def _pending_ids() -> list[str]:
    return [item["stable_id"] for item in fm.find_pending_followups(limit=50)]


# A stable_id longer than the Telegram callback_data budget, used to prove the
# "last 20 chars" callback token is resolved back to the real row exactly once.
LONG_ID = (
    "himalayas:https://himalayas.app/companies/acme/jobs/"
    "senior-devops-engineer-remote-1234567890"
)


# ---------------------------------------------------------------------------
# F1: only applications that were really sent and are 3-7 days old
# ---------------------------------------------------------------------------


def test_new_vacancy_without_application_never_selected(seeded_db):
    """F1 repro: a discovery-stage hh vacancy must never get a card."""
    _add_vacancy("hh:new-never-applied", state="NEW")

    _add_vacancy("hh:ok")
    _add_tracking("hh:ok", "SUBMITTED", last_status_change_at=_days_ago(5))

    assert _pending_ids() == ["hh:ok"]


def test_rejected_and_withdrawn_applications_never_selected(seeded_db):
    """REJECTED/WITHDRAWN applications and REJECTED vacancy states stay out."""
    _add_vacancy("hh:rejected-tracked")
    _add_tracking("hh:rejected-tracked", "REJECTED", last_status_change_at=_days_ago(5))

    _add_vacancy("hh:withdrawn-tracked")
    _add_tracking("hh:withdrawn-tracked", "WITHDRAWN", last_status_change_at=_days_ago(5))

    _add_vacancy("hh:rejected-vacancy-state", state="REJECTED")
    _add_tracking("hh:rejected-vacancy-state", "SUBMITTED",
                  last_status_change_at=_days_ago(5))

    _add_vacancy("hh:interview")
    _add_tracking("hh:interview", "INTERVIEW", last_status_change_at=_days_ago(5))

    assert _pending_ids() == ["hh:interview"]


def test_hh_vacancy_still_in_new_state_after_application_is_selected(seeded_db):
    """Measured 2026-10-19: hh writes tracking SUBMITTED but leaves state NEW.

    Deliberate policy (confirmed with the fix owner): eligibility is gated by
    the send evidence, not by the vacancy state, so a future audit must NOT
    "fix" this back by blanket-excluding ``state='NEW'`` -- that would hide
    real applications like hh:137448071 in production.
    """
    _add_vacancy("hh:applied-still-new", state="NEW")
    _add_tracking("hh:applied-still-new", "SUBMITTED",
                  last_status_change_at=_days_ago(5))

    assert _pending_ids() == ["hh:applied-still-new"]


def test_vacancy_without_a_sent_application_excluded(seeded_db):
    """The old ``OR v.source = 'hh'`` clause returned never-applied vacancies."""
    _add_vacancy("hh:discovered", state="DISCOVERED")
    _add_vacancy("hh:only-analyzed")
    _add_tracking("hh:only-analyzed", "ANALYZED", last_status_change_at=_days_ago(5))
    _add_vacancy("hh:ready-to-apply")
    _add_tracking("hh:ready-to-apply", "READY_TO_APPLY", last_status_change_at=_days_ago(5))

    _add_vacancy("hh:applied")
    _add_tracking("hh:applied", "SUBMITTED", last_status_change_at=_days_ago(5))

    assert _pending_ids() == ["hh:applied"]


def test_age_window_is_3_to_7_days(seeded_db):
    """Too recent (2d) and too old (10d, 30d) applications stay out."""
    for stable_id, days in (
        ("hh:too-recent", 2),
        ("hh:in-window", 5),
        ("hh:too-old", 10),
        ("hh:ancient", 30),
    ):
        _add_vacancy(stable_id)
        _add_tracking(stable_id, "APPLIED", applied_at=_days_ago(days),
                      last_status_change_at=_days_ago(days))

    assert _pending_ids() == ["hh:in-window"]


def test_window_parameters_are_honoured(seeded_db):
    """Callers can widen/narrow the window; the default stays 3-7 days."""
    for stable_id, days in (("hh:two-days", 2), ("hh:five-days", 5)):
        _add_vacancy(stable_id)
        _add_tracking(stable_id, "SUBMITTED", last_status_change_at=_days_ago(days))

    narrow = [
        item["stable_id"]
        for item in fm.find_pending_followups(limit=10, min_days=1, max_days=3)
    ]
    assert narrow == ["hh:two-days"]


def test_submission_row_counts_as_sent_application(seeded_db):
    """A successful application_submissions row is evidence of a real response."""
    _add_vacancy("habrcareer:1000167966", source="habrcareer")
    _add_submission("habrcareer:1000167966", status="SUBMITTED",
                    submitted_at=_days_ago(5))

    assert _pending_ids() == ["habrcareer:1000167966"]


def test_muted_row_is_excluded_later(seeded_db):
    """A real MUTED row (written by mute_followup) removes the card."""
    _add_vacancy("hh:mute-me")
    _add_tracking("hh:mute-me", "SUBMITTED", last_status_change_at=_days_ago(5))
    _add_vacancy("hh:keep-me")
    _add_tracking("hh:keep-me", "SUBMITTED", last_status_change_at=_days_ago(5))

    assert sorted(_pending_ids()) == ["hh:keep-me", "hh:mute-me"]
    assert fm.mute_followup("hh:mute-me") is True
    assert _pending_ids() == ["hh:keep-me"]


# ---------------------------------------------------------------------------
# F2: writes must hit the real stable_id row and report the truth
# ---------------------------------------------------------------------------


def test_record_followup_sent_twice_writes_real_row(seeded_db):
    _add_vacancy(LONG_ID)
    _add_tracking(LONG_ID, "SUBMITTED", last_status_change_at=_days_ago(5))

    assert fm.record_followup_sent(LONG_ID) is True
    assert fm.record_followup_sent(LONG_ID) is True

    rows = _followup_rows()
    assert rows == [(LONG_ID, 2, "SENT")]
    # the old bug created a second "row" whose PK was the LIKE template
    assert not any(row[0].startswith("%") for row in rows)
    # followup_count reached the cap, so the card is no longer offered
    assert _pending_ids() == []


def test_callback_token_for_long_id_resolves_to_real_row(seeded_db):
    """Telegram gets a short token; the handler must resolve it once, not store it."""
    _add_vacancy(LONG_ID)
    _add_tracking(LONG_ID, "SUBMITTED", last_status_change_at=_days_ago(5))

    token = fm._followup_callback_id(LONG_ID)
    assert token != LONG_ID
    assert len(f"flwmute:{token}".encode()) <= 64

    assert fm.record_followup_sent(token) is True
    assert _followup_rows() == [(LONG_ID, 1, "SENT")]


def test_callback_token_for_short_id_is_the_full_id(seeded_db):
    _add_vacancy("hh:136097888")
    _add_tracking("hh:136097888", "SUBMITTED", last_status_change_at=_days_ago(5))

    token = fm._followup_callback_id("hh:136097888")
    assert token == "hh:136097888"
    assert fm.record_followup_sent(token) is True
    assert _followup_rows() == [("hh:136097888", 1, "SENT")]


def test_mute_followup_writes_muted_status_on_real_row(seeded_db):
    _add_vacancy(LONG_ID)
    _add_tracking(LONG_ID, "SUBMITTED", last_status_change_at=_days_ago(5))

    assert fm.mute_followup(LONG_ID) is True
    assert _followup_rows() == [(LONG_ID, 0, "MUTED")]
    assert _pending_ids() == []


def test_unknown_identifier_returns_false_and_writes_nothing(seeded_db):
    _add_vacancy("hh:136097888")
    _add_tracking("hh:136097888", "SUBMITTED", last_status_change_at=_days_ago(5))

    assert fm.record_followup_sent("hh:999999999") is False
    assert fm.mute_followup("hh:999999999") is False
    assert _followup_rows() == []


def test_ambiguous_suffix_is_refused(seeded_db):
    """A suffix matching two vacancies must not silently pick one of them."""
    tail = "shared-tail-0123456789"
    # Both ids are long enough that the callback token is the last 20 chars.
    first = "himalayas:https://example.test/jobs/" + tail
    second = "linkedin:https://example.test/jobs/" + tail
    _add_vacancy(first, source="himalayas")
    _add_vacancy(second, source="linkedin_bangkok")

    ambiguous = fm._followup_callback_id(first)
    assert len(f"flwmute:{ambiguous}".encode()) <= 64
    assert ambiguous == fm._followup_callback_id(second)

    assert fm.record_followup_sent(ambiguous) is False
    assert fm.mute_followup(ambiguous) is False
    assert _followup_rows() == []
