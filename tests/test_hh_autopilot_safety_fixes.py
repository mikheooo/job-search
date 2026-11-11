"""Regression tests for the 2026-10-19 audit P0 submission-safety fixes.

Covers:
- H-01: autopilot does not re-arm SUBMIT_ALLOWED; the operator kill-switch wins.
- H-02/H-03: live submit path refuses URLs that are not the requested hh.ru form
  (source invariant), and the autopilot has a role-fit gate.
- H-04: a click is not reported as a confirmed submission unless the live page
  confirms it.
- H-11/H-12: vacancy is persisted and reconciliation is idempotent + truthful.
- Daily submission cap.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys

import pytest

from ai_assistant import config, db
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant import hh_autopilot as ha

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_tool(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def profile() -> CandidateProfile:
    return CandidateProfile(
        target_roles=[
            "AI Automation Engineer",
            "Technical Support Engineer",
            "System Administrator",
        ],
        alternative_roles=["AI Engineer", "Python Developer"],
        role_priorities={
            "AI_AUTOMATION": "P1",
            "TECH_SUPPORT": "P1",
            "APPLICATION_SUPPORT": "P1",
            "PYTHON_BACKEND": "P2",
            "SYSTEM_ADMIN": "P2",
            "DEVOPS_SRE": "P3",
            "DATA_ENGINEERING": "P3",
        },
    )


@pytest.fixture(autouse=True)
def _clean_db():
    db.init_db()
    yield


# ---------------------------------------------------------------- role gate


@pytest.mark.parametrize(
    "title",
    [
        "3D-разработчик / Технический партнер (Vibe-coder)",
        "3D-дженералист с AI-пайплайном (моделинг, моушен, интерактив)",
        "ML Engineer / Инженер машинного обучения",
        "Инженер по внедрению SIEM (KUMA / Kaspersky)",
        "Frontend разработчик",
        "Менеджер по продажам",
        "Администратор медицинского центра (телефон, CRM)",
    ],
)
def test_role_gate_blocks_off_target_titles(title, profile):
    allowed, reason = ha.role_gate(title, "", profile)
    assert allowed is False, f"off-target title passed the gate: {title} ({reason})"


@pytest.mark.parametrize(
    "title",
    [
        "AI Engineer",
        "AI Engineer (Fullstack)",
        "Разработчик AI-автоматизации и аналитики",
        "Системный администратор",
        "Специалист по внедрению CRM",
        "Администратор Битрикс24",
        "Разработчик Битрикс24",
        "Инженер технической поддержки",
        "Инженер сопровождения",
        "Специалист сопровождения",
        "Инженер по эксплуатации веб-инфраструктуры/DevOps",
        "Python Developer",
        "Сетевой инженер NOC",
        "AI Architect / Архитектор ИИ-решений",
        "Системный аналитик AI/Data / Forward Deployed Engineer (FDE)",
    ],
)
def test_role_gate_allows_target_titles(title, profile):
    allowed, reason = ha.role_gate(title, "", profile)
    assert allowed is True, f"target title was rejected: {title} ({reason})"


def test_salary_floor_allows_open_upper_bound_from_70k():
    assert ha.check_salary_floor("от 70 000 ₽ на руки")[0] is True
    assert ha.check_salary_floor({"from": 70000, "to": None, "currency": "RUR"})[0] is True
    assert ha.check_salary_floor("до 70 000 ₽ на руки")[0] is False
    assert ha.check_salary_floor("50 000 – 80 000 ₽")[0] is False


def test_role_gate_fails_closed_without_profile_priorities():
    empty = CandidateProfile()
    allowed, _reason = ha.role_gate("AI Engineer", "", empty)
    assert allowed is False


# ------------------------------------------------------- kill switch / env


def _offline_submit(monkeypatch, tmp_path, profile):
    monkeypatch.setattr(ha, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ha, "generate_tailored_letter", lambda **kw: "letter")
    monkeypatch.setattr(ha, "load_candidate_profile", lambda: profile)
    monkeypatch.setattr(ha, "brief_vacancy", lambda vid: None)

    def _boom():
        raise RuntimeError("CDP must not be touched")

    monkeypatch.setattr(ha, "_get_t722_send_module", _boom)


def test_submit_never_re_arms_submit_allowed_env(monkeypatch, tmp_path, profile):
    _offline_submit(monkeypatch, tmp_path, profile)
    monkeypatch.setattr(ha.config, "submit_allowed", lambda: True)
    monkeypatch.delenv("SUBMIT_ALLOWED", raising=False)

    ok = ha.submit_vacancy(vid="424245", role="AI Engineer", company="Acme")

    assert ok is False, "dead CDP must fail closed"
    assert "SUBMIT_ALLOWED" not in os.environ, (
        "the autopilot must not mutate the operator kill-switch"
    )


def test_submit_refuses_when_kill_switch_is_off(monkeypatch, tmp_path, profile):
    _offline_submit(monkeypatch, tmp_path, profile)
    monkeypatch.setattr(ha.config, "submit_allowed", lambda: False)

    outcome: dict = {}
    ok = ha.submit_vacancy(
        vid="424246", role="AI Engineer", company="Acme", outcome=outcome
    )

    assert ok is False
    assert outcome.get("status") == "REFUSED_KILL_SWITCH"


def test_t722_submit_refuses_before_browser_when_kill_switch_is_off(monkeypatch, tmp_path):
    from ai_assistant import hh_cdp_submit as module
    monkeypatch.setattr(config, "submit_allowed", lambda: False)

    def _no_browser(*args, **kwargs):
        raise AssertionError("browser session must not be created when the switch is off")

    monkeypatch.setattr(module, "HeadlessBrowserSession", _no_browser)
    letter = tmp_path / "letter.txt"
    letter.write_text("letter", encoding="utf-8")
    args = argparse.Namespace(
        command="submit", vid="424247", expect_resume="Ai engineer",
        letter=str(letter), prefill_plan=None,
    )

    report = module.run_submit(args)

    assert report["verdict"] == "REFUSED"
    assert report["clicked"] == 0
    assert "SUBMIT_ALLOWED" in str(report["blockers"])


def test_t722_submit_checks_url_host_and_vacancy_id(monkeypatch):
    src = (REPO / "ai_assistant" / "hh_cdp_submit.py").read_text(encoding="utf-8")
    assert "form_url" in src
    assert "not hh.ru" in src
    assert "does not belong to vacancy" in src


# ---------------------------------------------------- post-click confirmation


def test_click_without_live_confirmation_is_not_success():
    payload = {
        "clicked": 1,
        "vacancy_after": {
            "dom_already_responded": False,
            "applicant": {
                "alreadyApplied": False,
                "usedResumeIds": [],
                "negotiations_total": 0,
            },
        },
        "form_after_send": {"avrs": {"negotiations": {"total": 0}}},
    }
    confirmed, reason = ha._post_submit_confirmed(payload)
    assert confirmed is False
    assert "does not confirm" in reason


def test_t722_post_confirmed_field_wins_over_page_snapshot():
    """The live tool re-reads the page once; its verdict must be respected."""
    confirmed, reason = ha._post_submit_confirmed(
        {"post_confirmed": True, "post_confirm_reason": "live page shows the response"}
    )
    assert confirmed is True
    assert reason == "live page shows the response"

    confirmed, reason = ha._post_submit_confirmed(
        {"post_confirmed": False, "post_confirm_reason": "re-read still empty"}
    )
    assert confirmed is False
    assert reason == "re-read still empty"


@pytest.mark.parametrize(
    "after",
    [
        {"dom_already_responded": True, "applicant": {}},
        {"applicant": {"alreadyApplied": True}},
        {"applicant": {"usedResumeIds": ["287960707"]}},
        {"applicant": {"negotiations_total": 1}},
    ],
)
def test_live_confirmation_markers(after):
    confirmed, _reason = ha._post_submit_confirmed({"vacancy_after": after})
    assert confirmed is True


# ------------------------------------------------ prior submission / daily cap


def test_prior_submission_guard_finds_recorded_submission():
    assert ha._prior_submission_exists("777001") is None
    db.save_submission(
        "hh:777001",
        json.dumps({"submission_id": "hh-assisted-777001-test"}),
        "SUBMITTED",
        submission_id="hh-assisted-777001-test",
    )
    assert ha._prior_submission_exists("777001") is not None


def test_daily_submission_cap_counts_today_only():
    before = ha.submissions_today()
    db.save_submission(
        "hh:777002",
        json.dumps({"submission_id": "hh-assisted-777002-test"}),
        "SUBMITTED",
        submitted_at="2026-10-19T10:00:00Z",
        submission_id="hh-assisted-777002-test",
    )
    db.save_submission(
        "hh:777003",
        json.dumps({"submission_id": "hh-assisted-777003-test"}),
        "SUBMITTED",
        submitted_at="2020-01-01T10:00:00Z",
        submission_id="hh-assisted-777003-test",
    )
    today = __import__("time").strftime("%Y-%m-%d")
    delta = 1 if today == "2026-10-19" else 0
    assert ha.submissions_today() == before + delta


# ------------------------------------------------- vacancy persistence / reconcile


def test_persist_vacancy_snapshot_creates_parent_row():
    ok = ha._persist_vacancy_snapshot(
        "777004", "AI Engineer", "Acme",
        "remote automation role", None,
        brief={"vacancy": {"name": "AI Engineer", "area": {"name": "Remote"}}},
    )
    assert ok is True
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT source, source_job_id, title FROM vacancies WHERE stable_id=?",
            ("hh:777004",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[1] == "777004"


def test_reconcile_is_idempotent_and_truthful(monkeypatch, tmp_path):
    module = _load_tool("reconcile_sub_guard", "tools/reconcile_submission.py")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    app_dir = tmp_path / "artifacts" / "applications" / "hh-777005"
    app_dir.mkdir(parents=True)
    (app_dir / "cover_letter.ru.txt").write_text("hello letter", encoding="utf-8")

    first = module.reconcile(
        "777005", resume_title="Ai engineer", confirmed=False,
        post_reason="page does not show the response",
    )
    assert first["ok"] is True
    assert first["status"] == "SUBMITTED_UNVERIFIED"

    second = module.reconcile("777005", resume_title="Ai engineer", confirmed=True)
    assert second["ok"] is True
    assert second["status"] == "ALREADY_RECORDED"

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT status, submission_json FROM application_submissions "
            "WHERE vacancy_stable_id='hh:777005'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "reconcile must not create a duplicate submission row"
    assert rows[0][0] == "AMBIGUOUS"
    payload = json.loads(rows[0][1])
    assert payload["resume_title"] == "Ai engineer"
    assert payload["post_confirmed"] is False
    assert payload["record_status"] == "AMBIGUOUS"
    assert ha._prior_submission_exists("777005") is not None
    conn = db.get_connection()
    try:
        tracking = conn.execute(
            "SELECT status FROM application_tracking WHERE vacancy_stable_id='hh:777005'"
        ).fetchone()
    finally:
        conn.close()
    assert tracking[0] == "AMBIGUOUS"
    assert "%" not in payload["submission_id"]


def test_ambiguous_click_counts_toward_daily_safety_cap():
    before = ha.submissions_today()
    today = __import__("time").strftime("%Y-%m-%d")
    db.save_submission(
        "hh:777007", json.dumps({"submission_id": "ambiguous-777007"}),
        "AMBIGUOUS", submitted_at=f"{today}T10:00:00Z",
        submission_id="ambiguous-777007",
    )
    assert ha.submissions_today() == before + 1
    assert ha._prior_submission_exists("777007") is not None


def test_confirmed_reconcile_keeps_submitted_status(monkeypatch, tmp_path):
    module = _load_tool("reconcile_sub_confirmed", "tools/reconcile_submission.py")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    app_dir = tmp_path / "artifacts" / "applications" / "hh-777008"
    app_dir.mkdir(parents=True)
    (app_dir / "cover_letter.ru.txt").write_text("hello letter", encoding="utf-8")
    assert module.reconcile("777008", confirmed=True)["status"] == "SUBMITTED"
    conn = db.get_connection()
    try:
        submission = conn.execute("SELECT status FROM application_submissions WHERE vacancy_stable_id='hh:777008'").fetchone()
        tracking = conn.execute("SELECT status FROM application_tracking WHERE vacancy_stable_id='hh:777008'").fetchone()
    finally:
        conn.close()
    assert submission[0] == tracking[0] == "SUBMITTED"


def test_cycle_counts_ambiguous_click_against_limit_but_not_as_submitted(monkeypatch, tmp_path):
    from ai_assistant import hh_readonly_operation, hh_chat_monitor, remote_filter

    vacancies = [
        {"vacancy_id": str(i), "card": {"title": "DevOps", "employer": f"Company {i}"}}
        for i in (1, 2)
    ]
    monkeypatch.setattr(ha, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ha, "get_known_vacancy_ids", lambda: set())
    monkeypatch.setattr(ha, "load_candidate_profile", lambda: None)
    monkeypatch.setattr(hh_readonly_operation, "search_operation_report", lambda *a, **k: {"vacancies": vacancies})
    monkeypatch.setattr(ha.config, "submit_allowed", lambda: True)
    monkeypatch.setattr(ha, "submissions_today", lambda: 0)
    monkeypatch.setattr(ha, "_prior_submission_exists", lambda *a, **k: None)
    monkeypatch.setattr(ha, "role_gate", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(remote_filter, "classify_work_format", lambda **k: (True, "ok"))
    monkeypatch.setattr(ha, "brief_vacancy", lambda vid: {"vacancy": {}, "form": {}})
    monkeypatch.setattr(ha, "is_vacancy_eligible_for_auto_apply", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(ha, "select_best_resume_for_vacancy", lambda *a: "resume")
    monkeypatch.setattr(ha.time, "sleep", lambda *a: None)
    monkeypatch.setattr(hh_chat_monitor, "scan_hh_negotiations", lambda: [])
    calls = []
    def submit(**kwargs):
        calls.append(kwargs["vid"])
        kwargs["outcome"].update(confirmed=False, status="SUBMITTED_UNVERIFIED")
        return True
    monkeypatch.setattr(ha, "submit_vacancy", submit)
    result = ha.run_autopilot_cycle(max_submits=1)
    assert result["submitted"] == 0
    assert result["attempted"] == 1
    assert calls == ["1"]


def test_reconcile_refuses_missing_letter(monkeypatch, tmp_path):
    module = _load_tool("reconcile_sub_guard2", "tools/reconcile_submission.py")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    result = module.reconcile("777006")
    assert result["ok"] is False
    assert "cover letter missing" in result["error"]
