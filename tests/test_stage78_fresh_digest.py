"""Stage 78: Fresh Job Digest & Delivery Tracking Test Suite.

Verifies:
- Test 1: Fresh vacancies preferred over high-scoring historical vacancies_json.
- Test 2: Delivered vacancies are excluded from subsequent digests.
- Test 3: Preview / export --json does not mutate delivery state.
- Test 4: Telegram delivery failure preserves undelivered state.
- Test 5: Newly ingested vacancies appear in subsequent digest cycles.
- Test 6: Historical vacancies_json records never appear in fresh daily digest.
- Test 7: --limit 3 selects at most 3 fresh eligible vacancies.
- Test 8: Candidate matcher scoring algorithm remains unchanged.
"""
import io
import json
import sys
from pathlib import Path
import pytest

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile, load_candidate_profile
from ai_assistant.matcher import JobMatcher
from ai_assistant.vacancy_identity import resolve_vacancy_identity
from ai_assistant.cli import export_digest_cmd


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Provides a fresh isolated SQLite DB."""
    db_file = str(tmp_path / "test_state78.db")
    vac_file = str(tmp_path / "test_vacancies.json")
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setattr(config, "VACANCIES_FILE", vac_file)
    monkeypatch.setattr(db, "get_connection", lambda: __import__("sqlite3").connect(db_file))
    db.init_db()
    return {"db_file": db_file, "tmp_path": tmp_path}


def _make_matching_vacancy(source: str, source_job_id: str, company: str, salary_min: float = 4000, salary_max: float = 6000) -> Vacancy:
    return Vacancy(
        source=source,
        source_job_id=source_job_id,
        title="Senior AI Automation Engineer (n8n / Python)",
        company=company,
        description="Python, n8n, automation, AI agents, LLM, API. English. 100% remote work.",
        job_url=f"https://example.com/jobs/{source}-{source_job_id}",
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency="USD",
        location="Remote (Worldwide)",
        employment_type="Full Time",
    )


# ------------------------------------------------------------------------------
# TEST 1: Fresh vacancies preferred over high-scoring historical vacancies_json
# ------------------------------------------------------------------------------
def test_1_fresh_vacancies_preferred_over_historical(isolated_env, monkeypatch):
    """Digest returns fresh vacancies even if historical vacancies_json have higher scores."""
    # 1. Insert 10 historical vacancies_json with score 90+
    for i in range(10):
        v_hist = _make_matching_vacancy(
            source="vacancies_json",
            source_job_id=f"hist_{i}",
            company=f"Historical Corp {i}",
            salary_min=5000,
            salary_max=7000,
        )
        db.save_vacancy(v_hist)

    # 2. Insert 3 new fresh vacancies from live source (e.g. RemoteOK) with score 70+
    fresh_ids = []
    for j in range(3):
        v_fresh = _make_matching_vacancy(
            source="remoteok",
            source_job_id=f"fresh_{j}",
            company=f"Fresh Remote Co {j}",
            salary_min=3000,
            salary_max=4500,
        )
        db.save_vacancy(v_fresh)
        fresh_ids.append(v_fresh.stable_id())

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    assert res == 0

    data = json.loads(buf.getvalue())
    returned_ids = [item["id"] for item in data["new_vacancies_data"]]
    assert len(returned_ids) == 3
    assert all(id in fresh_ids for id in returned_ids)
    assert not any(id.startswith("vacancies_json:") for id in returned_ids)


# ------------------------------------------------------------------------------
# TEST 2: Delivered vacancies are excluded from subsequent digests
# ------------------------------------------------------------------------------
def test_2_delivered_vacancies_not_repeated(isolated_env, monkeypatch):
    """Once vacancies are marked delivered, subsequent export does not return them."""
    v1 = _make_matching_vacancy("himalayas", "him_1", "Himalayas Co 1")
    v2 = _make_matching_vacancy("himalayas", "him_2", "Himalayas Co 2")
    db.save_vacancy(v1)
    db.save_vacancy(v2)

    # 1. Run export with mark_delivered=True
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res1 = export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True, mark_delivered=True)
    assert res1 == 0
    data1 = json.loads(buf.getvalue())
    assert len(data1["new_vacancies_data"]) == 2

    # 2. Run export again (should now be empty)
    buf2 = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf2)
    res2 = export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    assert res2 == 0
    data2 = json.loads(buf2.getvalue())
    assert len(data2["new_vacancies_data"]) == 0
    assert "Сегодня новых подходящих вакансий не найдено" in data2["telegram_post"]


# ------------------------------------------------------------------------------
# TEST 3: Preview/export --json does NOT mutate delivery state
# ------------------------------------------------------------------------------
def test_3_preview_does_not_mutate_delivery_state(isolated_env, monkeypatch):
    """Running export-digest without --mark-delivered is strictly read-only."""
    v = _make_matching_vacancy("weworkremotely", "wwr_preview_1", "WWR Co")
    db.save_vacancy(v)

    # Preview 1
    buf1 = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf1)
    export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    assert not db.is_digest_delivered(v.stable_id())

    # Preview 2 (still present)
    buf2 = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf2)
    export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    data2 = json.loads(buf2.getvalue())
    assert len(data2["new_vacancies_data"]) == 1
    assert data2["new_vacancies_data"][0]["id"] == v.stable_id()


# ------------------------------------------------------------------------------
# TEST 4: Telegram delivery failure preserves undelivered state
# ------------------------------------------------------------------------------
def test_4_delivery_failure_preserves_undelivered_state(isolated_env, monkeypatch):
    """If Telegram sending fails, vacancies are not marked delivered."""
    sys.path.insert(0, r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs\scripts")
    import job_search_fetcher as jsf

    v = _make_matching_vacancy("habrcareer", "habr_fail_1", "Habr Fail Co")
    db.save_vacancy(v)

    # Mock Telegram delivery to fail
    monkeypatch.setattr(jsf, "send_to_telegram", lambda text: False)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    # Mock Watcher.poll_once to avoid live network
    from ai_assistant.watcher import Watcher, WatcherCycleResult
    monkeypatch.setattr(Watcher, "poll_once", lambda self, iteration=1: WatcherCycleResult(iteration=iteration))

    with pytest.raises(SystemExit) as exc:
        jsf.main()
    assert exc.value.code == 1

    # Invariant: vacancy must NOT be marked delivered
    assert not db.is_digest_delivered(v.stable_id())


# ------------------------------------------------------------------------------
# TEST 5: Newly ingested vacancies appear in subsequent digest
# ------------------------------------------------------------------------------
def test_5_new_vacancies_appear_in_subsequent_digest(isolated_env, monkeypatch):
    """Vacancies added after a digest delivery appear in the next cycle."""
    # Batch 1
    v1 = _make_matching_vacancy("remoteok", "rok_b1", "Batch 1 Co")
    db.save_vacancy(v1)

    # Deliver Batch 1
    export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True, mark_delivered=True)
    assert db.is_digest_delivered(v1.stable_id())

    # Ingest Batch 2
    v2 = _make_matching_vacancy("remoteok", "rok_b2", "Batch 2 Co")
    db.save_vacancy(v2)

    # Next digest must only contain Batch 2
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    data = json.loads(buf.getvalue())
    assert len(data["new_vacancies_data"]) == 1
    assert data["new_vacancies_data"][0]["id"] == v2.stable_id()


# ------------------------------------------------------------------------------
# TEST 6: Historical vacancies_json records never appear in fresh digest
# ------------------------------------------------------------------------------
def test_6_historical_records_never_appear_in_fresh_digest(isolated_env, monkeypatch):
    """Legacy vacancies_json entries are ignored by fresh digest selection."""
    for i in range(5):
        v = _make_matching_vacancy("vacancies_json", f"legacy_{i}", "Legacy Co")
        db.save_vacancy(v)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    data = json.loads(buf.getvalue())
    assert len(data["new_vacancies_data"]) == 0


# ------------------------------------------------------------------------------
# TEST 7: --limit 3 selects at most 3 fresh eligible vacancies
# ------------------------------------------------------------------------------
def test_7_limit_3_selects_max_3_fresh_eligible(isolated_env, monkeypatch):
    """Limit 3 returns at most 3 highest scoring fresh vacancies."""
    for i in range(7):
        v = _make_matching_vacancy("himalayas", f"lim_{i}", f"Limit Co {i}", salary_min=2000 + i * 500, salary_max=3500 + i * 500)
        db.save_vacancy(v)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    export_digest_cmd(format_type="json", limit=3, min_score=60.0, output_json=True)
    data = json.loads(buf.getvalue())
    assert len(data["new_vacancies_data"]) == 3
    assert data["count"] == 3


# ------------------------------------------------------------------------------
# TEST 8: Candidate matcher scoring algorithm remains unchanged
# ------------------------------------------------------------------------------
def test_8_matcher_scoring_algorithm_unchanged(isolated_env):
    """JobMatcher produces identical scores and reasons."""
    prof = load_candidate_profile()
    matcher = JobMatcher(prof)

    v = Vacancy(
        source="remoteok",
        source_job_id="matcher_test_1",
        title="Senior AI Automation Engineer (n8n / Python)",
        company="Synthetix Automations",
        description="Senior AI Automation Engineer (n8n / Python) English required. Automation, AI Agents, LLM API.",
        job_url="https://wellfound.com/jobs/4412093-senior-ai-automation-engineer-n8n-python",
        salary_min=4000.0,
        salary_max=6000.0,
        salary_currency="USD",
        location="Remote (Worldwide)",
        employment_type="Full Time",
    )

    res = matcher.match(v)
    assert res.score == 100
    assert res.decision == "APPLY"
    assert "role:25/25 exact" in "".join(res.reasons)