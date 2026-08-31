"""Stage 80: Telegram Digest Crash Consistency & Duplicate-Send Protection Test Suite.

Verifies:
1. test_attempt_is_persisted_before_telegram_send
2. test_confirmed_success_transitions_attempt_to_delivered
3. test_confirmed_failure_remains_retryable
4. test_post_send_db_failure_does_not_allow_blind_resend
5. test_interrupted_attempt_is_reported_as_ambiguous
6. test_ambiguous_attempt_does_not_create_duplicate_send
7. test_delivery_attempt_creation_is_idempotent
8. test_preview_commands_do_not_modify_attempt_state
9. test_successful_recovery_excludes_vacancies_from_future_digest
10. test_existing_stage79_delivery_records_remain_compatible
"""
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
    """Provides an isolated SQLite DB environment for Stage 80 crash-consistency tests."""
    db_file = str(tmp_path / "test_stage80.db")
    vac_file = str(tmp_path / "test_vacancies80.json")
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setattr(config, "VACANCIES_FILE", vac_file)
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
# 1. test_attempt_is_persisted_before_telegram_send
# ------------------------------------------------------------------------------
def test_attempt_is_persisted_before_telegram_send(isolated_env, monkeypatch):
    """Proves attempt state (ATTEMPTING) is durably saved in SQLite before external Telegram call."""
    v = _make_matching_vacancy("remoteok", "pre_send_1", "PreSend Co")
    db.save_vacancy(v)

    attempt_verified_during_send = False

    def spy_send_to_telegram(text: str) -> bool:
        nonlocal attempt_verified_during_send
        # Check DB state while in the middle of sending
        attempts = db.list_digest_attempts(limit=10)
        assert len(attempts) == 1
        assert attempts[0]["status"] == "ATTEMPTING"
        # Check individual vacancy state in DB
        undigested = db.list_undigested_vacancies(limit=10)
        assert len(undigested) == 0  # In-flight attempt excludes vacancy from new queries
        attempt_verified_during_send = True
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", spy_send_to_telegram)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    jsf.main()

    assert attempt_verified_during_send is True


# ------------------------------------------------------------------------------
# 2. test_confirmed_success_transitions_attempt_to_delivered
# ------------------------------------------------------------------------------
def test_confirmed_success_transitions_attempt_to_delivered(isolated_env, monkeypatch):
    """Proves successful send transitions batch and vacancies to DELIVERED."""
    v1 = _make_matching_vacancy("himalayas", "succ_trans_1", "Success Co 1")
    v2 = _make_matching_vacancy("remoteok", "succ_trans_2", "Success Co 2")
    db.save_vacancy(v1)
    db.save_vacancy(v2)

    monkeypatch.setattr(jsf, "send_to_telegram", lambda text: True)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    jsf.main()

    # Verify batch is DELIVERED
    attempts = db.list_digest_attempts(limit=10)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "DELIVERED"
    assert attempts[0]["retry_permitted"] is False

    # Verify individual vacancies are DELIVERED
    assert db.is_digest_delivered(v1.stable_id())
    assert db.is_digest_delivered(v2.stable_id())


# ------------------------------------------------------------------------------
# 3. test_confirmed_failure_remains_retryable
# ------------------------------------------------------------------------------
def test_confirmed_failure_remains_retryable(isolated_env, monkeypatch):
    """Confirmed Telegram failure transitions to FAILED and remains retryable on next run."""
    v = _make_matching_vacancy("habrcareer", "conf_fail_1", "Retryable Co")
    db.save_vacancy(v)

    monkeypatch.setattr(jsf, "send_to_telegram", lambda text: False)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    with pytest.raises(SystemExit) as exc:
        jsf.main()
    assert exc.value.code == 1

    # Check batch status is FAILED
    attempts = db.list_digest_attempts(limit=10)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "FAILED"
    assert attempts[0]["retry_permitted"] is True

    # Vacancy must remain in undigested queue for next cycle
    undigested = db.list_undigested_vacancies(limit=10)
    assert len(undigested) == 1
    assert undigested[0].stable_id() == v.stable_id()


# ------------------------------------------------------------------------------
# 4. test_post_send_db_failure_does_not_allow_blind_resend
# ------------------------------------------------------------------------------
def test_post_send_db_failure_does_not_allow_blind_resend(isolated_env, monkeypatch):
    """If Telegram succeeds but DB finalization fails, next cron run must NOT resend."""
    v = _make_matching_vacancy("remoteok", "post_fail_1", "PostFail Co")
    db.save_vacancy(v)

    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    # 1. Run 1: Telegram succeeds, but mark_digest_delivered throws SQLite write error
    send_call_count = 0
    def mock_send(text: str) -> bool:
        nonlocal send_call_count
        send_call_count += 1
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send)

    original_mark = db.mark_digest_delivered
    def broken_mark(vacancy_ids, batch_key=None, chat_id="-1004399255305"):
        raise RuntimeError("Simulated Disk / SQLite Lock Error during post-send commit")

    monkeypatch.setattr(db, "mark_digest_delivered", broken_mark)

    with pytest.raises(SystemExit) as exc:
        jsf.main()
    assert exc.value.code == 2
    assert send_call_count == 1

    # Invariant: Status must be AMBIGUOUS
    attempts = db.list_digest_attempts(limit=10)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "AMBIGUOUS"

    # 2. Run 2: Next cron run occurs with working DB
    monkeypatch.setattr(db, "mark_digest_delivered", original_mark)
    send_call_count_run2 = 0
    def mock_send2(text: str) -> bool:
        nonlocal send_call_count_run2
        send_call_count_run2 += 1
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send2)

    # Execute next cron cycle
    jsf.main()

    # MUST NOT send duplicate Telegram message
    assert send_call_count_run2 == 0


# ------------------------------------------------------------------------------
# 5. test_interrupted_attempt_is_reported_as_ambiguous
# ------------------------------------------------------------------------------
def test_interrupted_attempt_is_reported_as_ambiguous(isolated_env, monkeypatch):
    """An attempt that was interrupted / killed remains ATTEMPTING or AMBIGUOUS and is inspectable."""
    v = _make_matching_vacancy("weworkremotely", "interr_1", "Interrupted Co")
    db.save_vacancy(v)

    # Record attempt as would happen before a hard kill
    batch_key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")

    # Inspect attempts via CLI
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = digest_attempts_cmd(action="list", output_json=True)
    assert res == 0

    data = json.loads(buf.getvalue())
    assert data["count"] == 1
    assert data["attempts"][0]["batch_key"] == batch_key
    assert data["attempts"][0]["status"] == "ATTEMPTING"
    assert data["attempts"][0]["retry_permitted"] is False


# ------------------------------------------------------------------------------
# 6. test_ambiguous_attempt_does_not_create_duplicate_send
# ------------------------------------------------------------------------------
def test_ambiguous_attempt_does_not_create_duplicate_send(isolated_env, monkeypatch):
    """Unresolved/ambiguous attempts prevent automatic blind re-sending."""
    v = _make_matching_vacancy("remoteok", "amb_dup_1", "Ambiguous Dup Co")
    db.save_vacancy(v)

    batch_key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")
    db.record_digest_ambiguous([v.stable_id()], batch_key=batch_key, reason="Timeout during handshake")

    send_called = False
    def mock_send(text: str) -> bool:
        nonlocal send_called
        send_called = True
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    jsf.main()

    # Telegram sender must not be invoked
    assert send_called is False


# ------------------------------------------------------------------------------
# 7. test_delivery_attempt_creation_is_idempotent
# ------------------------------------------------------------------------------
def test_delivery_attempt_creation_is_idempotent(isolated_env):
    """Recreating the exact same batch attempt locks out duplicate attempts and preserves uniqueness."""
    v_ids = ["remoteok:idem_80_1", "himalayas:idem_80_2"]

    key1 = db.record_digest_attempt(v_ids, chat_id="-1004399255305")
    assert key1.startswith("digest_batch:")

    # Second call for the same active batch is locked out (returns None)
    key2 = db.record_digest_attempt(v_ids, chat_id="-1004399255305")
    assert key2 is None

    attempts = db.list_digest_attempts(limit=10)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "ATTEMPTING"


# ------------------------------------------------------------------------------
# 8. test_preview_commands_do_not_modify_attempt_state
# ------------------------------------------------------------------------------
def test_preview_commands_do_not_modify_attempt_state(isolated_env, monkeypatch):
    """export-digest preview never creates attempts or modifies state."""
    v = _make_matching_vacancy("himalayas", "prev_80_1", "Prev Co")
    db.save_vacancy(v)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    export_digest_cmd(format_type="json", limit=5, output_json=True)
    export_digest_cmd(format_type="telegram", limit=5, output_json=False)

    attempts = db.list_digest_attempts(limit=10)
    assert len(attempts) == 0


# ------------------------------------------------------------------------------
# 9. test_successful_recovery_excludes_vacancies_from_future_digest
# ------------------------------------------------------------------------------
def test_successful_recovery_excludes_vacancies_from_future_digest(isolated_env, monkeypatch):
    """Reconciling an ambiguous batch to DELIVERED permanently resolves the vacancies."""
    v = _make_matching_vacancy("remoteok", "recov_1", "Reconcile Co")
    db.save_vacancy(v)

    batch_key = db.record_digest_attempt([v.stable_id()], chat_id="-1004399255305")
    db.record_digest_ambiguous([v.stable_id()], batch_key=batch_key, reason="Manual inspection needed")

    # Operator reconciles via CLI
    res = digest_attempts_cmd(action="recover", batch_key=batch_key, new_status="DELIVERED")
    assert res == 0

    # Verify status is now DELIVERED
    attempts = db.list_digest_attempts(limit=10)
    assert attempts[0]["status"] == "DELIVERED"
    assert db.is_digest_delivered(v.stable_id())

    # Undigested queue must now be empty
    undigested = db.list_undigested_vacancies(limit=10)
    assert len(undigested) == 0


# ------------------------------------------------------------------------------
# 10. test_existing_stage79_delivery_records_remain_compatible
# ------------------------------------------------------------------------------
def test_existing_stage79_delivery_records_remain_compatible(isolated_env):
    """Pre-existing DELIVERED records from Stage 79 without batch records remain respected."""
    # Insert legacy vacancy and mark delivered using Stage 79 signature
    v = _make_matching_vacancy("remoteok", "legacy_79_compat", "Legacy 79 Co")
    db.save_vacancy(v)

    db.mark_digest_delivered([v.stable_id()], chat_id="-1004399255305")

    assert db.is_digest_delivered(v.stable_id())
    undigested = db.list_undigested_vacancies(limit=10)
    assert len(undigested) == 0