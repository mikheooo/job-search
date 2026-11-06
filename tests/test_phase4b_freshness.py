"""Phase 4B offline tests: freshness, protection, dry-run safety (15 scenarios).

Scratch DB per test (conftest redirects config.DB_FILE), no network, no
Telegram sends, no prod writes. db.get_connection() resolves config.DB_FILE
dynamically, so all engine calls below hit the scratch DB.
"""
from __future__ import annotations

import datetime

import pytest

from ai_assistant import db
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_status,
    set_application_status,
    transition_application,
)
from ai_assistant.schema import Vacancy
from ai_assistant.vacancy_lifecycle import (
    classify_vacancy,
    dry_run_report,
    is_vacancy_protected,
)

NOW = datetime.datetime(2026, 10, 11, tzinfo=datetime.timezone.utc)


def _old_vac(source: str, sid: str, days_ago: int = 40) -> Vacancy:
    return Vacancy(
        source=source,
        source_job_id=sid,
        title="Old Role",
        company="Acme",
        description="python remote",
        job_url=f"https://example.com/{source}/{sid}",
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
    )


def _insert_aged(source: str, sid: str, days_ago: int) -> str:
    stable = f"{source}:{sid}"
    db.save_vacancy(_old_vac(source, sid))
    seen = (NOW - datetime.timedelta(days=days_ago)).isoformat()
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE vacancies SET first_seen_at=?, last_seen_at=? WHERE stable_id=?",
            (seen, seen, stable),
        )
        conn.commit()
    finally:
        conn.close()
    return stable


def _row(sid: str) -> dict:
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT stable_id, source, title, company, first_seen_at,"
            " last_seen_at, state FROM vacancies WHERE stable_id=?",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    return dict(zip(
        ["stable_id", "source", "title", "company",
         "first_seen_at", "last_seen_at", "state"], r))


def _conn():
    return db.get_connection()


# 1. Old vacancy, no application.
def test_1_old_no_application_is_stale_or_archive():
    sid = _insert_aged("himalayas", "t1", 40)
    conn = _conn()
    try:
        item = classify_vacancy(_row(sid), conn, now=NOW, source_healthy=True)
        assert item["freshness"] in ("STALE_CANDIDATE", "ARCHIVE_CANDIDATE")
        assert item["protected"] is False
    finally:
        conn.close()


# 2. Old vacancy with active application.
def test_2_old_with_active_application_protected():
    sid = _insert_aged("himalayas", "t2", 100)
    set_application_status(sid, ApplicationStatus.DISCOVERED, company="Acme", title="Old Role")
    transition_application(sid, ApplicationStatus.ANALYZED, note="t")
    transition_application(sid, ApplicationStatus.READY_TO_APPLY, note="t")
    transition_application(sid, ApplicationStatus.SUBMITTED, note="t")
    conn = _conn()
    try:
        item = classify_vacancy(_row(sid), conn, now=NOW, source_healthy=True,
                                archive_days=30)
        assert item["freshness"] == "STALE_CANDIDATE"
        assert item["protected"] is True
        assert get_application_status(sid).status == ApplicationStatus.SUBMITTED
    finally:
        conn.close()


# 3. Closed vacancy with upcoming interview stays protected.
def test_3_closed_with_interview_protected():
    sid = _insert_aged("hh", "t3", 50)
    set_application_status(sid, ApplicationStatus.DISCOVERED, company="Acme", title="Old Role")
    for st in (ApplicationStatus.ANALYZED, ApplicationStatus.READY_TO_APPLY,
               ApplicationStatus.SUBMITTED):
        transition_application(sid, st, note="t")
    from ai_assistant.application_tracking import verify_and_apply

    verify_and_apply(sid, "VERIFIED", note="t")
    from ai_assistant.employer_events import apply_employer_event

    res = apply_employer_event(kind="interview", source="test", event_key="fp-t3",
                               vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00")
    assert res["outcome"] == "applied"
    conn = _conn()
    try:
        item = classify_vacancy(_row(sid), conn, now=NOW, source_healthy=True,
                                closed_evidence={sid: "archived"},
                                archive_days=30)
        assert item["freshness"] == "CLOSED_CONFIRMED"
        assert item["protected"] is True
    finally:
        conn.close()


# 4. Unhealthy source -> UNKNOWN, never closed on age.
def test_4_unhealthy_source_is_unknown():
    sid = _insert_aged("remoteok", "t4", 200)
    conn = _conn()
    try:
        item = classify_vacancy(_row(sid), conn, now=NOW, source_healthy=False,
                                archive_days=30)
        assert item["freshness"] == "UNKNOWN"
    finally:
        conn.close()


# 5. Partial listing: old row, healthy source -> STALE, not CLOSED.
def test_5_partial_listing_never_implies_closed():
    sid = _insert_aged("himalayas", "t5", 45)
    conn = _conn()
    try:
        item = classify_vacancy(_row(sid), conn, now=NOW, source_healthy=True)
        assert item["freshness"] != "CLOSED_CONFIRMED"
    finally:
        conn.close()


# 6. Reappearing vacancy is ACTIVE_SEEN.
def test_6_reappeared_vacancy_active():
    sid = _insert_aged("habrcareer", "t6", 60)
    fresh = NOW.isoformat()
    conn = db.get_connection()
    try:
        conn.execute("UPDATE vacancies SET last_seen_at=? WHERE stable_id=?", (fresh, sid))
        conn.commit()
    finally:
        conn.close()
    conn = _conn()
    try:
        item = classify_vacancy(_row(sid), conn, now=NOW, source_healthy=True)
        assert item["freshness"] == "ACTIVE_SEEN"
    finally:
        conn.close()


# 7. Duplicates from several sources classified independently.
def test_7_cross_source_duplicates_independent():
    a = _insert_aged("himalayas", "t7", 40)
    b = _insert_aged("remoteok", "t7", 1)
    conn = _conn()
    try:
        ia = classify_vacancy(_row(a), conn, now=NOW, source_healthy=True)
        ib = classify_vacancy(_row(b), conn, now=NOW, source_healthy=True)
        assert ia["freshness"] != "ACTIVE_SEEN"
        assert ib["freshness"] == "ACTIVE_SEEN"
    finally:
        conn.close()


# 8. Unknown tracking status protects (fail-closed).
def test_8_unknown_tracking_status_protects():
    sid = _insert_aged("hh", "t8", 100)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO application_tracking (vacancy_stable_id, status) VALUES (?, ?)",
            (sid, "SOMETHING_NEW"),
        )
        conn.commit()
    finally:
        conn.close()
    conn = _conn()
    try:
        protected, reasons = is_vacancy_protected(sid, conn)
        assert protected is True
    finally:
        conn.close()


# 9. Repeated dry-runs are identical.
def test_9_dry_run_repeatable():
    _insert_aged("himalayas", "t9", 40)
    c1 = _conn()
    try:
        r1 = dry_run_report(c1, limit=100, now=NOW)
    finally:
        c1.close()
    c2 = _conn()
    try:
        r2 = dry_run_report(c2, limit=100, now=NOW)
    finally:
        c2.close()
    assert r1["buckets"] == r2["buckets"]
    assert r1["items"].keys() == r2["items"].keys()


# 10. Dry-run performs no UPDATE/DELETE.
def test_10_dry_run_writes_nothing():
    _insert_aged("himalayas", "t10", 40)

    def _counts():
        conn = db.get_connection()
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            return {t: conn.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]
                    for t in tables}
        finally:
            conn.close()

    before = _counts()
    conn = _conn()
    try:
        dry_run_report(conn, limit=100, now=NOW)
    finally:
        conn.close()
    assert _counts() == before


# 11. Telegram dedup data untouched + suffix resolution exact.
def test_11_telegram_dedup_preserved():
    from ai_assistant.telegram_auto_apply import resolve_vacancy_by_suffix

    db.save_vacancy(_old_vac("tg_chan", "9428"))
    db.save_vacancy(_old_vac("tg_chan", "19428"))
    conn = db.get_connection()
    try:
        before = conn.execute("SELECT COUNT(*) FROM telegram_delivery_records").fetchone()
        conn2 = _conn()
        try:
            dry_run_report(conn2, limit=100, now=NOW)
        finally:
            conn2.close()
        after = conn.execute("SELECT COUNT(*) FROM telegram_delivery_records").fetchone()
    finally:
        conn.close()
    assert before == after
    assert resolve_vacancy_by_suffix("9428") is None  # ambiguous
    assert resolve_vacancy_by_suffix("tg_chan:9428") == "tg_chan:9428"


# 12. HH submit gates and limits intact.
def test_12_hh_gates_and_limits_intact():
    import inspect

    import ai_assistant.hh_autopilot as ha

    assert inspect.signature(ha.run_autopilot_cycle).parameters["max_submits"].default == 8
    ok, _ = ha.is_vacancy_eligible_for_auto_apply(
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": True}},
         "form": {"submit": {"disabled": False}}})
    assert ok is True
    bad, _ = ha.is_vacancy_eligible_for_auto_apply(
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": False}}, "form": {}})
    assert bad is False


# 13. Ambiguous application linkage refuses.
def test_13_ambiguous_linkage_refuses():
    from ai_assistant.employer_events import resolve_application_for_event

    db.save_vacancy(_old_vac("hh", "t13a", ))
    db.save_vacancy(_old_vac("hh", "t13b"))
    set_application_status("hh:t13a", ApplicationStatus.APPLIED, company="Acme", title="Old Role")
    set_application_status("hh:t13b", ApplicationStatus.APPLIED, company="Acme", title="Old Role")
    assert resolve_application_for_event(employer="Acme", title="Old Role") is None
    rec = resolve_application_for_event(vacancy_stable_id="hh:t13a")
    assert rec is not None and rec.vacancy_stable_id == "hh:t13a"


# 14. DB read error -> protected (fail-closed).
def test_14_db_error_fail_closed():
    conn = db.get_connection()
    conn.close()
    protected, reasons = is_vacancy_protected("hh:whatever", conn)
    assert protected is True
    assert any("check-failed" in r for r in reasons)


# 15. Closure never ends the hiring process itself.
def test_15_closed_keeps_hiring_state():
    sid = _insert_aged("hh", "t15", 60)
    set_application_status(sid, ApplicationStatus.DISCOVERED, company="Acme", title="Old Role")
    transition_application(sid, ApplicationStatus.ANALYZED, note="t")
    transition_application(sid, ApplicationStatus.READY_TO_APPLY, note="t")
    transition_application(sid, ApplicationStatus.SUBMITTED, note="t")
    conn = _conn()
    try:
        item = classify_vacancy(_row(sid), conn, now=NOW, source_healthy=True,
                                closed_evidence={sid: "closed"},
                                archive_days=30)
        assert item["freshness"] == "CLOSED_CONFIRMED"
        assert get_application_status(sid).status == ApplicationStatus.SUBMITTED
    finally:
        conn.close()
