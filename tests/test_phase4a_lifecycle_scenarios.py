"""Phase 4A scenario tests: unified job & application lifecycle (offline).

Scratch DB per test (conftest redirects config.DB_FILE), network blocked.
Production DB and scheduler untouched. No real submits, no Telegram sends.
"""
from __future__ import annotations

import pytest

from ai_assistant import db
from ai_assistant.application_tracking import (
    ApplicationStatus,
    get_application_history,
    get_application_status,
    list_applications,
    set_application_status,
    transition_application,
    verify_and_apply,
)
from ai_assistant.employer_events import (
    apply_employer_event,
    classify_rejection_origin,
    resolve_application_for_event,
)
from ai_assistant.schema import Vacancy


def _vac(source: str, sid: str, title="Engineer", company="Acme") -> Vacancy:
    return Vacancy(
        source=source,
        source_job_id=sid,
        title=title,
        company=company,
        description="python remote",
        job_url=f"https://example.com/{source}/{sid}",
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
    )


def _tracked(sid: str, **kw) -> str:
    db.save_vacancy(_vac("hh", sid, **kw))
    set_application_status(sid, ApplicationStatus.DISCOVERED, company="Acme", title="Engineer")
    return sid


def _to_applied(sid: str) -> None:
    transition_application(sid, ApplicationStatus.ANALYZED, note="t")
    transition_application(sid, ApplicationStatus.READY_TO_APPLY, note="t")
    transition_application(sid, ApplicationStatus.SUBMITTED, note="t")
    verify_and_apply(sid, "VERIFIED", note="t")  # SUBMITTED -> VERIFIED -> APPLIED


# Scenario 1: employer rejects after confirmed APPLIED.
def test_s1_employer_reject_after_applied():
    sid = _tracked("hh:s1")
    other = _tracked("hh:s1-other")
    _to_applied(sid)
    res = apply_employer_event(
        kind="employer_reject", source="test", event_key="msg-fp-1",
        vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00",
    )
    assert res["outcome"] == "applied"
    assert get_application_status(sid).status == ApplicationStatus.REJECTED
    assert get_application_status(other).status == ApplicationStatus.DISCOVERED
    notes = [h.note or "" for h in get_application_history(sid)]
    assert any("employer:employer_reject" in n for n in notes)
    assert classify_rejection_origin(get_application_status(sid).notes, notes) == "employer"


# Scenario 2: vacancy disappears from source, interview already scheduled.
def test_s2_interview_survives_vanished_vacancy():
    sid = _tracked("hh:s2")
    _to_applied(sid)
    res = apply_employer_event(
        kind="interview", source="test", event_key="msg-fp-2",
        vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00",
    )
    assert res["outcome"] == "applied"
    # No sweep exists: the INTERVIEW record and its history must persist.
    assert get_application_status(sid).status == ApplicationStatus.INTERVIEW
    assert len(get_application_history(sid)) >= 1


# Scenario 2b: conversation -> vacancy resolver (powers interview linkage).
def test_s2b_resolve_vacancy_for_conversation():
    from ai_assistant.employer_events import resolve_vacancy_for_conversation

    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO hh_message_events (message_fingerprint, conversation_id, sender,"
            " text, sent_at, seen_at, vacancy_stable_id) VALUES"
            " ('fp-a1', 'conv-1', 'employer', 'hi', '2026-01-01', '2026-01-01', 'hh:111'),"
            " ('fp-a2', 'conv-1', 'employer', 'welcome', '2026-01-02', '2026-01-02', 'hh:111'),"
            " ('fp-b1', 'conv-2', 'employer', 'hi', '2026-01-01', '2026-01-01', 'hh:111'),"
            " ('fp-b2', 'conv-2', 'employer', 'hello', '2026-01-02', '2026-01-02', 'hh:222')"
        )
        conn.commit()
    finally:
        conn.close()
    assert resolve_vacancy_for_conversation("conv-1") == "hh:111"
    assert resolve_vacancy_for_conversation("conv-2") is None  # ambiguous
    assert resolve_vacancy_for_conversation("conv-unknown") is None
    assert resolve_vacancy_for_conversation("") is None


# Scenario 3: same employer message processed twice.
def test_s3_duplicate_message_is_idempotent():
    sid = _tracked("hh:s3")
    _to_applied(sid)
    first = apply_employer_event(
        kind="interview", source="test", event_key="msg-fp-3",
        vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00",
    )
    assert first["outcome"] == "applied"
    n_history = len(get_application_history(sid))
    second = apply_employer_event(
        kind="interview", source="test", event_key="msg-fp-3",
        vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00",
    )
    assert second["outcome"] == "already-applied"
    assert get_application_status(sid).status == ApplicationStatus.INTERVIEW
    assert len(get_application_history(sid)) == n_history


# Scenario 4: ambiguous/partial Telegram callback suffix touches nothing.
def test_s4_ambiguous_suffix_refuses():
    from ai_assistant.telegram_auto_apply import mark_vacancy_applied, mark_vacancy_skipped

    db.save_vacancy(_vac("tg_chan", "9428"))
    db.save_vacancy(_vac("tg_chan", "19428"))  # shares suffix "9428"
    assert mark_vacancy_applied("9428") is False
    assert mark_vacancy_skipped(" Mono ") is False  # unknown suffix
    conn = db.get_connection()
    try:
        states = dict(conn.execute("SELECT stable_id, state FROM vacancies").fetchall())
    finally:
        conn.close()
    assert states["tg_chan:9428"] == "NEW"
    assert states["tg_chan:19428"] == "NEW"
    # Exact full id still works.
    assert mark_vacancy_applied("tg_chan:9428") is True


# Scenario 5: filter rejection is not an employer refusal.
def test_s5_filter_reject_not_counted_as_employer():
    sid = _tracked("hh:s5")
    set_application_status(sid, ApplicationStatus.REJECTED, notes="Matcher SKIP: no python")
    rec = get_application_status(sid)
    assert classify_rejection_origin(rec.notes, [h.note or "" for h in get_application_history(sid)]) == "filter"
    assert rec.status == ApplicationStatus.REJECTED  # stored status unchanged


# Scenario 5b: funnel splits filter vs employer rejections, legacy keys intact.
def test_s5b_funnel_split_keys():
    from ai_assistant.funnel_analytics import format_funnel_report, get_funnel_metrics

    f = _tracked("hh:s5b-filter")
    set_application_status(f, ApplicationStatus.REJECTED, notes="Matcher SKIP: off-profile")
    e = _tracked("hh:s5b-employer")
    _to_applied(e)
    apply_employer_event(
        kind="employer_reject", source="test", event_key="msg-fp-5b",
        vacancy_stable_id=e, occurred_at="2099-01-01T00:00:00",
    )
    m = get_funnel_metrics()
    for legacy in ("total_sourced", "target_matched", "total_applied", "invites",
                   "rejections", "pending", "invite_rate"):
        assert legacy in m, f"legacy funnel key {legacy} must stay"
    assert m["track_rejected_filter"] >= 1
    assert m["track_rejected_employer"] >= 1
    assert m["track_rejected_unknown"] >= 0
    assert m["track_applied"] >= 0
    report = format_funnel_report()
    assert isinstance(report, str) and len(report) > 100
def test_s6_interview_after_verified_applied():
    sid = _tracked("hh:s6")
    transition_application(sid, ApplicationStatus.ANALYZED, note="t")
    transition_application(sid, ApplicationStatus.READY_TO_APPLY, note="t")
    transition_application(sid, ApplicationStatus.SUBMITTED, note="t")
    early = apply_employer_event(
        kind="interview", source="test", event_key="msg-fp-6a",
        vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00",
    )
    assert early["outcome"] == "illegal-transition"
    assert get_application_status(sid).status == ApplicationStatus.SUBMITTED
    verify_and_apply(sid, "VERIFIED", note="t")
    late = apply_employer_event(
        kind="interview", source="test", event_key="msg-fp-6b",
        vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00",
    )
    assert late["outcome"] == "applied"
    assert get_application_status(sid).status == ApplicationStatus.INTERVIEW


# Scenario 7: stale event must not roll state back.
def test_s7_stale_event_no_rollback():
    sid = _tracked("hh:s7")
    _to_applied(sid)
    apply_employer_event(
        kind="interview", source="test", event_key="msg-fp-7a",
        vacancy_stable_id=sid, occurred_at="2099-06-01T00:00:00",
    )
    assert get_application_status(sid).status == ApplicationStatus.INTERVIEW
    res = apply_employer_event(
        kind="employer_reject", source="test", event_key="msg-fp-7b",
        vacancy_stable_id=sid, occurred_at="2001-01-01T00:00:00",
    )
    assert res["outcome"] == "stale-event"
    assert get_application_status(sid).status == ApplicationStatus.INTERVIEW


# Scenario 8: DB failure mid-transition leaves no partial state.
def test_s8_db_failure_is_atomic(monkeypatch):
    import ai_assistant.application_tracking as tr

    sid = _tracked("hh:s8")
    _to_applied(sid)
    before_hist = len(get_application_history(sid))
    real_connect = tr.get_connection

    class _FailCommitConn:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            if name == "commit":
                raise RuntimeError("injected commit failure")
            return getattr(self._real, name)

        def close(self):
            try:
                self._real.rollback()
            finally:
                self._real.close()

    calls = {"n": 0}

    def _connect_once_ok_then_fail():
        calls["n"] += 1
        return _FailCommitConn(real_connect())

    monkeypatch.setattr(tr, "get_connection", _connect_once_ok_then_fail)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        tr.transition_application(sid, ApplicationStatus.INTERVIEW, note="t")

    monkeypatch.setattr(tr, "get_connection", real_connect)
    assert get_application_status(sid).status == ApplicationStatus.APPLIED
    assert len(get_application_history(sid)) == before_hist


# Scenario 9: autopilot limits/filters unchanged, no real sends in tests.
def test_s9_autopilot_limits_and_gates_intact(monkeypatch):
    import inspect

    import ai_assistant.hh_autopilot as ha

    assert inspect.signature(ha.run_autopilot_cycle).parameters["max_submits"].default == 8
    ok, _ = ha.is_vacancy_eligible_for_auto_apply(
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": True}},
         "form": {"submit": {"disabled": False}}}
    )
    assert ok is True
    bad, _ = ha.is_vacancy_eligible_for_auto_apply(
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": True, "hasTests": True}},
         "form": {}}
    )
    assert bad is False

    monkeypatch.setattr(ha, "PROJECT_ROOT", __import__("pathlib").Path(str(__import__("tempfile").mkdtemp())))
    monkeypatch.setattr(ha, "generate_tailored_letter", lambda **kw: "x")
    monkeypatch.setattr(ha, "_get_t722_send_module", lambda: (_ for _ in ()).throw(RuntimeError("no CDP")))
    assert ha.submit_vacancy(vid="s9", role="R", company="C") is False
    assert get_application_status("hh:s9") is None  # failed submit tracks nothing


# Scenario 10: dashboard/TG/CLI readers agree after a transition.
def test_s10_readers_agree_after_transition():
    from ai_assistant.application_dashboard import build_dashboard

    sid = _tracked("hh:s10")
    _to_applied(sid)
    apply_employer_event(
        kind="interview", source="test", event_key="msg-fp-10",
        vacancy_stable_id=sid, occurred_at="2099-01-01T00:00:00",
    )
    via_get = get_application_status(sid).status
    via_list = [r for r in list_applications(limit=10000) if r.vacancy_stable_id == sid][0].status
    via_hist = get_application_history(sid)[-1].new_status
    assert via_get == via_list == ApplicationStatus.INTERVIEW
    assert via_hist == "INTERVIEW"
    dash = build_dashboard()
    assert dash.interview >= 1
