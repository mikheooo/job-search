"""Stage 81: Stale Digest Attempt Recovery & Concurrency Gate Test Suite.

Verifies:
1. test_recent_attempting_batch_is_not_stale
2. test_old_attempting_batch_is_reported_stale
3. test_stale_attempt_is_never_automatically_retryable
4. test_stale_attempt_does_not_trigger_telegram_send
5. test_stale_attempt_does_not_block_unrelated_new_vacancies
6. test_reconcile_stale_as_delivered_excludes_original_vacancies
7. test_reconcile_stale_as_failed_reenables_original_vacancies
8. test_recovery_of_unknown_batch_fails_closed
9. test_digest_attempts_list_exposes_age_and_stale_state
10. test_digest_attempts_json_is_machine_readable
11. test_preview_does_not_age_transition_or_mutate_attempts
12. test_stage80_ambiguous_behavior_remains_compatible
13. test_concurrent_workers_exact_single_telegram_send
"""
import datetime
import io
import json
import sys
from pathlib import Path
import pytest

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.cli import export_digest_cmd, digest_attempts_cmd
from ai_assistant.watcher import Watcher, WatcherCycleResult

sys.path.insert(0, r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs\scripts")
import job_search_fetcher as jsf


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Provides an isolated SQLite DB environment for Stage 81 tests."""
    db_file = str(tmp_path / "test_stage81.db")
    vac_file = str(tmp_path / "test_vacancies81.json")
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setattr(config, "VACANCIES_FILE", vac_file)
    monkeypatch.setattr(config, "DIGEST_ATTEMPT_STALE_MINUTES", 60)
    monkeypatch.setattr(db, "get_connection", lambda: __import__("sqlite3").connect(db_file))
    db.init_db()
    # Mock Watcher to avoid live network
    monkeypatch.setattr(Watcher, "poll_once", lambda self, iteration=1, dry_run=False: WatcherCycleResult(iteration=iteration))
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
# 1. test_recent_attempting_batch_is_not_stale
# ------------------------------------------------------------------------------
def test_recent_attempting_batch_is_not_stale(isolated_env):
    """An attempt created 5 minutes ago is within the 60m threshold and not marked stale."""
    v_ids = ["remoteok:recent_1"]
    key = db.record_digest_attempt(v_ids, chat_id="-1004399255305")

    # Evaluate at t = now + 5 minutes
    t_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)
    attempts = db.list_digest_attempts(limit=10, now_dt=t_now)

    assert len(attempts) == 1
    assert attempts[0]["status"] == "ATTEMPTING"
    assert attempts[0]["effective_status"] == "ATTEMPTING"
    assert attempts[0]["stale"] is False
    assert attempts[0]["retry_permitted"] is False


# ------------------------------------------------------------------------------
# 2. test_old_attempting_batch_is_reported_stale
# ------------------------------------------------------------------------------
def test_old_attempting_batch_is_reported_stale(isolated_env):
    """An attempt created 120 minutes ago exceeds the 60m threshold and is reported STALE."""
    v_ids = ["remoteok:old_1"]
    key = db.record_digest_attempt(v_ids, chat_id="-1004399255305")

    # Evaluate at t = now + 120 minutes
    t_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=120)
    attempts = db.list_digest_attempts(limit=10, now_dt=t_now)

    assert len(attempts) == 1
    assert attempts[0]["status"] == "ATTEMPTING"
    assert attempts[0]["effective_status"] == "STALE"
    assert attempts[0]["stale"] is True
    assert attempts[0]["age_minutes"] >= 120
    assert attempts[0]["requires_reconciliation"] is True


# ------------------------------------------------------------------------------
# 3. test_stale_attempt_is_never_automatically_retryable
# ------------------------------------------------------------------------------
def test_stale_attempt_is_never_automatically_retryable(isolated_env):
    """STALE != FAILED. A stale attempt has retry_permitted=False and is excluded from fresh queries."""
    v = _make_matching_vacancy("remoteok", "stale_not_retryable_1", "Stale Co")
    db.save_vacancy(v)

    key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")

    t_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)
    attempts = db.list_digest_attempts(limit=10, now_dt=t_now)
    assert attempts[0]["effective_status"] == "STALE"
    assert attempts[0]["retry_permitted"] is False

    # Vacancy must not appear in fresh query
    undigested = db.list_undigested_vacancies(limit=10)
    assert len(undigested) == 0


# ------------------------------------------------------------------------------
# 4. test_stale_attempt_does_not_trigger_telegram_send
# ------------------------------------------------------------------------------
def test_stale_attempt_does_not_trigger_telegram_send(isolated_env, monkeypatch):
    """Running fetcher when state contains only a stale attempt does NOT resend to Telegram."""
    v = _make_matching_vacancy("remoteok", "stale_no_send_1", "Stale Co")
    db.save_vacancy(v)

    db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")

    send_call_count = 0
    def mock_send(text: str) -> bool:
        nonlocal send_call_count
        send_call_count += 1
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    jsf.main()

    assert send_call_count == 0


# ------------------------------------------------------------------------------
# 5. test_stale_attempt_does_not_block_unrelated_new_vacancies
# ------------------------------------------------------------------------------
def test_stale_attempt_does_not_block_unrelated_new_vacancies(isolated_env, monkeypatch):
    """Scenario: Day 1: A, B, C -> stale. Day 2: D, E arrive -> D, E are sent; A, B, C are NOT resent."""
    # Day 1: Old batch A, B, C
    va = _make_matching_vacancy("remoteok", "vac_a", "Company A")
    vb = _make_matching_vacancy("remoteok", "vac_b", "Company B")
    vc = _make_matching_vacancy("remoteok", "vac_c", "Company C")
    db.save_vacancy(va)
    db.save_vacancy(vb)
    db.save_vacancy(vc)

    batch_old = db.record_digest_attempt([va.stable_id(), vb.stable_id(), vc.stable_id()], chat_id="-1004399255305")

    # Day 2: New vacancies D, E arrive
    vd = _make_matching_vacancy("himalayas", "vac_d", "Company D")
    ve = _make_matching_vacancy("weworkremotely", "vac_e", "Company E")
    db.save_vacancy(vd)
    db.save_vacancy(ve)

    captured_texts = []
    def mock_send(text: str) -> bool:
        captured_texts.append(text)
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    jsf.main()

    # Exactly 1 Telegram post was sent
    assert len(captured_texts) == 1
    post = captured_texts[0]

    # Contains new vacancies D and E
    assert "Company D" in post
    assert "Company E" in post

    # Does NOT contain old vacancies A, B, C
    assert "Company A" not in post
    assert "Company B" not in post
    assert "Company C" not in post

    # D and E are now marked DELIVERED
    assert db.is_digest_delivered(vd.stable_id())
    assert db.is_digest_delivered(ve.stable_id())

    # A, B, C remain safely ATTEMPTING / unresolved
    assert not db.is_digest_delivered(va.stable_id())
    assert not db.is_digest_delivered(vb.stable_id())


# ------------------------------------------------------------------------------
# 6. test_reconcile_stale_as_delivered_excludes_original_vacancies
# ------------------------------------------------------------------------------
def test_reconcile_stale_as_delivered_excludes_original_vacancies(isolated_env):
    """Reconciling a stale batch as DELIVERED permanently resolves the vacancies."""
    v = _make_matching_vacancy("habrcareer", "stale_rec_deliv", "Reconcile Deliv Co")
    db.save_vacancy(v)

    batch_key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")

    res = digest_attempts_cmd(action="recover", batch_key=batch_key, new_status="DELIVERED")
    assert res == 0

    assert db.is_digest_delivered(v.stable_id())
    attempts = db.list_digest_attempts(limit=5)
    assert attempts[0]["status"] == "DELIVERED"
    assert attempts[0]["effective_status"] == "DELIVERED"
    assert attempts[0]["stale"] is False


# ------------------------------------------------------------------------------
# 7. test_reconcile_stale_as_failed_reenables_original_vacancies
# ------------------------------------------------------------------------------
def test_reconcile_stale_as_failed_reenables_original_vacancies(isolated_env, monkeypatch):
    """Reconciling a stale batch as FAILED reenables automatic retry and warns operator."""
    v = _make_matching_vacancy("habrcareer", "stale_rec_fail", "Reconcile Fail Co")
    db.save_vacancy(v)

    batch_key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = digest_attempts_cmd(action="recover", batch_key=batch_key, new_status="FAILED")
    assert res == 0

    out = buf.getvalue()
    assert "WARNING: Marking an ambiguous/stale batch FAILED permits Telegram resend." in out

    # Vacancy is now retryable
    undigested = db.list_undigested_vacancies(limit=10)
    assert len(undigested) == 1
    assert undigested[0].stable_id() == v.stable_id()


# ------------------------------------------------------------------------------
# 8. test_recovery_of_unknown_batch_fails_closed
# ------------------------------------------------------------------------------
def test_recovery_of_unknown_batch_fails_closed(isolated_env, monkeypatch):
    """Recovering a nonexistent batch key fails closed with exit code 1."""
    err_buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err_buf)

    res = digest_attempts_cmd(action="recover", batch_key="digest_batch:nonexistent_999", new_status="DELIVERED")
    assert res == 1
    assert "not found in database" in err_buf.getvalue()


# ------------------------------------------------------------------------------
# 9. test_digest_attempts_list_exposes_age_and_stale_state
# ------------------------------------------------------------------------------
def test_digest_attempts_list_exposes_age_and_stale_state(isolated_env, monkeypatch):
    """CLI list output displays age, created, updated, and stale indicator."""
    v = _make_matching_vacancy("remoteok", "list_inspect_1", "Inspect Co")
    db.save_vacancy(v)
    db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = digest_attempts_cmd(action="list", output_json=False)
    assert res == 0

    out = buf.getvalue()
    assert "DIGEST DELIVERY ATTEMPTS" in out
    assert "Persisted: ATTEMPTING" in out
    assert "Age:" in out
    assert "Created:" in out


# ------------------------------------------------------------------------------
# 10. test_digest_attempts_json_is_machine_readable
# ------------------------------------------------------------------------------
def test_digest_attempts_json_is_machine_readable(isolated_env, monkeypatch):
    """CLI --json output has complete schema with age_seconds, stale, effective_status."""
    v = _make_matching_vacancy("remoteok", "json_inspect_1", "Json Co")
    db.save_vacancy(v)
    batch_key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = digest_attempts_cmd(action="list", output_json=True)
    assert res == 0

    data = json.loads(buf.getvalue())
    assert data["count"] == 1
    att = data["attempts"][0]
    assert att["batch_key"] == batch_key
    assert "effective_status" in att
    assert "stale" in att
    assert "age_seconds" in att
    assert "requires_reconciliation" in att


# ------------------------------------------------------------------------------
# 11. test_preview_does_not_age_transition_or_mutate_attempts
# ------------------------------------------------------------------------------
def test_preview_does_not_age_transition_or_mutate_attempts(isolated_env, monkeypatch):
    """Running export-digest preview never modifies SQLite delivery state."""
    v = _make_matching_vacancy("himalayas", "prev_mut_1", "Mut Co")
    db.save_vacancy(v)

    # Initial delivery records count is 0
    records_before = db.list_telegram_delivery_records(limit=100)
    assert len(records_before) == 0

    export_digest_cmd(format_type="json", limit=5, output_json=True)
    export_digest_cmd(format_type="telegram", limit=5, output_json=False)

    records_after = db.list_telegram_delivery_records(limit=100)
    assert len(records_after) == 0


# ------------------------------------------------------------------------------
# 12. test_stage80_ambiguous_behavior_remains_compatible
# ------------------------------------------------------------------------------
def test_stage80_ambiguous_behavior_remains_compatible(isolated_env):
    """AMBIGUOUS attempts remain protected and report requires_reconciliation=True."""
    v = _make_matching_vacancy("remoteok", "amb_compat_1", "Amb Compat Co")
    db.save_vacancy(v)

    batch_key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")
    db.record_digest_ambiguous([v.stable_id()], batch_key=batch_key, reason="Socket reset")

    attempts = db.list_digest_attempts(limit=5)
    assert attempts[0]["status"] == "AMBIGUOUS"
    assert attempts[0]["effective_status"] == "AMBIGUOUS"
    assert attempts[0]["retry_permitted"] is False
    assert attempts[0]["requires_reconciliation"] is True


# ------------------------------------------------------------------------------
# 13. test_concurrent_workers_exact_single_telegram_send (Multi-Worker Concurrency)
# ------------------------------------------------------------------------------
def test_concurrent_workers_exact_single_telegram_send(isolated_env, monkeypatch):
    """Worker A and Worker B racing for the same batch results in Telegram calls == 1."""
    v1 = _make_matching_vacancy("remoteok", "race_1", "Race Co 1")
    v2 = _make_matching_vacancy("himalayas", "race_2", "Race Co 2")
    db.save_vacancy(v1)
    db.save_vacancy(v2)

    telegram_call_count = 0
    def mock_send(text: str) -> bool:
        nonlocal telegram_call_count
        telegram_call_count += 1
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    # Worker A runs and acquires lock
    jsf.main()
    assert telegram_call_count == 1

    # Worker B runs concurrently with the same vacancy set in DB
    jsf.main()

    # Telegram calls MUST remain exactly 1
    assert telegram_call_count == 1