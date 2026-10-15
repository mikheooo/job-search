"""Phase 3 regression tests: HH autopilot schema + submit limits.

Pins the 2026-10-10 production failure (`no such column: vacancy_id` in
get_known_vacancy_ids) and the owner-mandated submit limits. No browser, no
network: CDP entry points are stubbed to raise, letter generation is stubbed,
PROJECT_ROOT is redirected to tmp_path. Production DB untouched (conftest
redirects config.DB_FILE to a scratch DB per test).
"""
from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

import ai_assistant.hh_autopilot as ha
from ai_assistant import config, db
from ai_assistant.schema import Vacancy


def _hh_vacancy(sid: str) -> Vacancy:
    return Vacancy(
        source="hh",
        source_job_id=sid,
        title="Python Developer",
        company="Acme",
        description="python remote",
        job_url=f"https://hh.ru/vacancy/{sid}",
        location="Remote",
        country_restrictions=[],
        timezone_restrictions=[],
    )


def test_get_known_vacancy_ids_matches_real_schema():
    """Regression: must query vacancy_stable_id (not vacancy_id)."""
    db.init_db()
    assert db.save_vacancy(_hh_vacancy("424242")) in ("INSERTED", "UPDATED", "UNCHANGED", "CONFLICT")
    now = "2026-10-11T00:00:00+00:00"
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO application_tracking"
            " (vacancy_stable_id, status, company, title) VALUES (?, 'ANALYZED', 'Acme', 'Python Developer')",
            ("hh:424243",),
        )
        # hh_applications row WITH a vacancy link + one WITHOUT (NULL must be skipped).
        conn.execute(
            "INSERT OR IGNORE INTO hh_applications"
            " (application_id, vacancy_stable_id, state, created_at, updated_at)"
            " VALUES ('app_test_1', 'hh:424244', 'ANALYZED', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO hh_applications"
            " (application_id, vacancy_stable_id, state, created_at, updated_at)"
            " VALUES ('app_test_null', NULL, 'ANALYZED', ?, ?)",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    known = ha.get_known_vacancy_ids()  # must not raise OperationalError
    assert "424242" in known, "vacancies table (hh source) not picked up"
    assert "424243" in known, "application_tracking not picked up"
    assert "424244" in known, "hh_applications.vacancy_stable_id not picked up"
    assert "None" not in known, "NULL vacancy_stable_id leaked as 'None'"
    assert "app_test_1" not in known, "application_id must never appear in vacancy ids"


def test_autopilot_submit_limit_defaults_to_eight():
    """Owner mandate: max_submits=8 per cycle."""
    sig = inspect.signature(ha.run_autopilot_cycle)
    assert sig.parameters["max_submits"].default == 8


def test_preflight_records_auto_authorization_not_human_confirmation(monkeypatch, tmp_path):
    """Audit honesty: autopilot preflight must NOT claim human_confirmed=True."""
    monkeypatch.setattr(ha, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ha, "generate_tailored_letter", lambda **kw: "test cover letter")

    def _boom():
        raise RuntimeError("CDP must not be touched in this test")

    monkeypatch.setattr(ha, "_get_t722_send_module", _boom)

    ok = ha.submit_vacancy(vid="424245", role="Python Developer", company="Acme")
    assert ok is False, "with dead CDP the submit must fail closed"

    preflight = json.loads((tmp_path / "artifacts" / "applications" / "hh-424245" / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("human_confirmed") is False, "auto-submit must not pose as manual confirmation"
    assert preflight.get("auto_submit_authorized") is True


def test_is_vacancy_eligible_gates_unchanged():
    """Spot-check the safety gates the owner ordered to keep."""
    eligible, _ = ha.is_vacancy_eligible_for_auto_apply(
        {
            "vacancy": {"ok": True, "applicant": {"responseAllowed": True}},
            "form": {"submit": {"disabled": False}},
        }
    )
    assert eligible is True
    for bad in [
        {"vacancy": {"ok": True, "archived": True}, "form": {}},
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": False}}, "form": {}},
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": True, "hasTests": True}}, "form": {}},
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": True}}, "form": {"started_with_question": True}},
        {"vacancy": {"ok": True, "applicant": {"responseAllowed": True}}, "form": {"submit": {"disabled": True}}},
    ]:
        ok, _reason = ha.is_vacancy_eligible_for_auto_apply(bad)
        assert ok is False, f"gate bypassed for {bad}"
