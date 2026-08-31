"""Stage 75: Regression Test Suite for Unified Job Search Architecture.

Verifies:
- Test A: Single vacancy source (Hermes integration uses canonical state.db, not raw vacancies.json).
- Test B: Canonical database (New vacancies land in state.db tables).
- Test C: Deduplication (Tracking params stripped, no duplicate canonical records).
- Test D: Application gate (Discovery/digest export cannot directly trigger HH submit).
- Test E: Single HH reply owner (Hermes does not run raw hh_reply.py; job-search is sole owner).
- Test F: Telegram separation (Public digest to @remotejobd vs personal alerts to TELEGRAM_CHAT_ID).
- Test G: No legacy auto-apply bypass (auto_apply.py and hh_reply.py fail-closed).
- Test H: Hermes cron contract (Hermes script invokes canonical job-search export).
"""
import io
import json
import os
import sys
from pathlib import Path
import pytest

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.normalizer import normalize_vacancy
from ai_assistant.vacancy_identity import (
    resolve_vacancy_identity,
    get_canonical_by_id,
    normalize_url,
)
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant.matcher import JobMatcher
from ai_assistant.cli import export_digest_cmd


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Provides a fresh isolated SQLite DB and temporary vacancies.json."""
    db_file = str(tmp_path / "test_state.db")
    vac_file = str(tmp_path / "test_vacancies.json")
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setattr(config, "VACANCIES_FILE", vac_file)
    monkeypatch.setattr(db, "get_connection", lambda: __import__("sqlite3").connect(db_file))
    db.init_db()
    return {"db_file": db_file, "vac_file": vac_file, "tmp_path": tmp_path}


# ------------------------------------------------------------------------------
# Test A: Single Vacancy Source
# ------------------------------------------------------------------------------
def test_a_single_vacancy_source(isolated_env, monkeypatch):
    """Hermes integration reads from canonical state.db and does not require vacancies.json."""
    vac = Vacancy(
        source="himalayas",
        source_job_id="test_101",
        title="Senior AI Automation Engineer",
        company="Canonical Corp",
        description="Python and n8n workflow development",
        job_url="https://himalayas.app/jobs/canonical-ai-101",
        salary_min=3000,
        salary_max=4500,
        salary_currency="USD",
        location="Remote",
    )
    db.save_vacancy(vac)
    resolve_vacancy_identity(vac)

    # vacancies.json is absent
    vac_path = Path(isolated_env["vac_file"])
    if vac_path.exists():
        vac_path.unlink()

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = export_digest_cmd(format_type="json", limit=5, min_score=0.0, output_json=True)
    assert res == 0

    out = json.loads(buf.getvalue())
    assert out["count"] >= 1
    assert any("Canonical Corp" in item["company"] for item in out["new_vacancies_data"])
    assert not vac_path.exists(), "export-digest must not write to legacy vacancies.json"


# ------------------------------------------------------------------------------
# Test B: Canonical Database
# ------------------------------------------------------------------------------
def test_b_canonical_database(isolated_env):
    """New discovered vacancies are normalized and stored in SQLite state.db."""
    raw_item = {
        "source": "remoteok",
        "source_job_id": "rok_999",
        "title": "Lead Python & AI Engineer",
        "company": "NextGen AI",
        "job_url": "https://remoteok.com/remote-jobs/rok_999?utm_source=twitter&ref=newsletter",
        "description": "<p>Build LLM agents and <b>n8n</b> automation workflows.</p>",
        "salary_min": 5000,
        "salary_max": 7000,
        "salary_currency": "USD",
        "location": "Remote",
    }
    vac = normalize_vacancy(raw_item)
    assert vac.description == "Build LLM agents and n8n automation workflows."
    db.save_vacancy(vac)

    row = db.get_vacancy_by_id(vac.stable_id())
    assert row is not None
    v_obj = db._row_to_vacancy(row)
    assert v_obj.company == "NextGen AI"
    assert v_obj.title == "Lead Python & AI Engineer"

    c_match = resolve_vacancy_identity(vac)
    assert c_match.canonical_id is not None
    c_row = get_canonical_by_id(c_match.canonical_id)
    assert c_row is not None
    assert "utm_source" not in c_row.normalized_url


# ------------------------------------------------------------------------------
# Test C: Deduplication
# ------------------------------------------------------------------------------
def test_c_deduplication(isolated_env):
    """Duplicate URL with different tracking parameters maps to same canonical vacancy."""
    url1 = "https://weworkremotely.com/remote-jobs/ai-developer?utm_source=feed&utm_campaign=daily"
    url2 = "https://weworkremotely.com/remote-jobs/ai-developer?ref=telegram&fbclid=XYZ123"

    v1 = Vacancy(
        source="weworkremotely",
        source_job_id="wwr_1",
        title="AI Developer",
        company="Remote Automations",
        description="Build workflows with n8n and AI",
        job_url=url1,
    )
    v2 = Vacancy(
        source="weworkremotely",
        source_job_id="wwr_2",
        title="AI Developer",
        company="Remote Automations",
        description="Build workflows with n8n and AI",
        job_url=url2,
    )

    db.save_vacancy(v1)
    m1 = resolve_vacancy_identity(v1)

    db.save_vacancy(v2)
    m2 = resolve_vacancy_identity(v2)

    assert m1.canonical_id == m2.canonical_id, "Both URLs must resolve to the identical canonical ID"


# ------------------------------------------------------------------------------
# Test D: Application Gate
# ------------------------------------------------------------------------------
def test_d_application_gate(isolated_env, monkeypatch):
    """Discovery and digest export are strictly read-only and never trigger HH submission."""
    def _fail_on_submit(*args, **kwargs):
        raise AssertionError("CRITICAL SAFETY VIOLATION: Submit called during discovery/digest export!")

    from ai_assistant import hh_controlled_submit
    monkeypatch.setattr(hh_controlled_submit, "controlled_real_submit", _fail_on_submit)

    vac = Vacancy(
        source="hh",
        source_job_id="hh_123456",
        title="Senior AI Automation Engineer",
        company="HH Employer",
        description="Python and n8n backend automation. 100% remote work.",
        job_url="https://hh.ru/vacancy/123456",
        salary_min=2000,
        salary_max=3500,
        salary_currency="USD",
        location="Remote",
    )
    db.save_vacancy(vac)
    resolve_vacancy_identity(vac)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = export_digest_cmd(format_type="telegram", limit=5, min_score=0.0)
    assert res == 0
    assert "HH Employer" in buf.getvalue()


# ------------------------------------------------------------------------------
# Test E: Single HH Reply Owner
# ------------------------------------------------------------------------------
def test_e_single_hh_reply_owner(monkeypatch):
    """Legacy hh_reply.py is locked out; replies are governed by job-search hh_message_reply."""
    monkeypatch.delenv("LEGACY_HH_REPLY_OVERRIDE", raising=False)
    
    if "hh_reply" in sys.modules:
        del sys.modules["hh_reply"]
    with pytest.raises(SystemExit) as exc:
        import hh_reply
    assert exc.value.code == 1

    from ai_assistant import hh_message_reply
    dialog = hh_message_reply.HHDialog(
        conversation_id="5577169431",
        vacancy_title="Python Developer",
        messages=[
            hh_message_reply.HHMessage(message_id="m1", text="Здравствуйте! Уточните ваш стек.", sender="employer"),
        ],
    )
    det = hh_message_reply.classify_hh_conversation_detailed(dialog)
    assert det["classification"] in ("NEEDS_REPLY", "HUMAN_REVIEW")


# ------------------------------------------------------------------------------
# Test F: Telegram Separation
# ------------------------------------------------------------------------------
def test_f_telegram_separation(monkeypatch):
    """Personal alerts and public digest use strictly separate channels and formats."""
    from ai_assistant.telegram_notifier import TelegramNotifier
    
    sys.path.insert(0, r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs\scripts")
    import job_search_fetcher as jsf
    assert jsf.TARGET_CHANNEL == "-1004399255305", "Public digest target must be channel @remotejobd"

    personal_notifier = TelegramNotifier(bot_token="test_token", chat_id="999888777")
    assert personal_notifier.chat_id == "999888777", "Personal alerts must target individual user chat ID"
    assert personal_notifier.chat_id != jsf.TARGET_CHANNEL


# ------------------------------------------------------------------------------
# Test G: No Legacy Auto-Apply Bypass
# ------------------------------------------------------------------------------
def test_g_no_legacy_auto_apply_bypass(monkeypatch):
    """Legacy auto_apply.py fails closed and cannot bypass Stage 17-51 safety gates."""
    monkeypatch.delenv("LEGACY_AUTO_APPLY_OVERRIDE", raising=False)

    if "auto_apply" in sys.modules:
        del sys.modules["auto_apply"]
    with pytest.raises(SystemExit) as exc:
        import auto_apply
    assert exc.value.code == 1


# ------------------------------------------------------------------------------
# Test H: Hermes Cron Contract
# ------------------------------------------------------------------------------
def test_h_hermes_cron_contract(isolated_env, monkeypatch):
    """Hermes job_search_fetcher successfully executes canonical discovery and export."""
    sys.path.insert(0, r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs\scripts")
    import job_search_fetcher as jsf

    vac = Vacancy(
        source="habrcareer",
        source_job_id="habr_777",
        title="Senior AI Automation Engineer",
        company="Habr Tech",
        description="Python, n8n, LLM agents automation. 100% remote work.",
        job_url="https://career.habr.com/vacancies/777",
        salary_min=2500,
        salary_max=4000,
        salary_currency="USD",
        location="Remote",
    )
    db.save_vacancy(vac)
    resolve_vacancy_identity(vac)

    monkeypatch.setenv("JOB_SEARCH_DRY_RUN", "1")

    from ai_assistant.watcher import Watcher, WatcherCycleResult
    def fake_poll(self, iteration=1):
        return WatcherCycleResult(
            iteration=iteration,
            timestamp="2026-08-31T11:00:00Z",
            fetched_count=1,
            new_vacancies_count=1,
            duplicate_count=0,
            rejected_count=0,
            matched_count=1,
            analyzed_count=1,
            prepared_count=1,
            ready_for_review_count=1,
            needs_human_review_count=0,
            blocked_count=0,
        )
    monkeypatch.setattr(Watcher, "poll_once", fake_poll)

    post_text, vacancies_data = jsf.run_canonical_discovery_and_export(limit=3, min_score=0.0)
    assert "Senior AI Automation Engineer" in post_text
    assert len(vacancies_data) >= 1
    assert any(v["company"] == "Habr Tech" for v in vacancies_data)