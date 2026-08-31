"""Stage 79: Production-like E2E Verification of Digest Delivery State.

Verifies:
1. test_successful_delivery_marks_digest_items_delivered
2. test_failed_delivery_does_not_mark_anything_delivered
3. test_telegram_exception_does_not_mark_delivered
4. test_retry_after_failure_returns_same_vacancies
5. test_successful_delivery_is_idempotent
6. test_partial_batch_failure_does_not_create_false_delivery_state
7. test_preview_never_marks_delivered
8. test_legacy_vacancies_never_enter_delivery_batch
"""
import io
import json
import sys
from pathlib import Path
import pytest

from ai_assistant import config, db
from ai_assistant.schema import Vacancy
from ai_assistant.cli import export_digest_cmd
from ai_assistant.watcher import Watcher, WatcherCycleResult

sys.path.insert(0, r"C:\Users\Misha\AppData\Local\hermes\profiles\jobs\scripts")
import job_search_fetcher as jsf


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Provides an isolated SQLite DB environment for delivery tests."""
    db_file = str(tmp_path / "test_stage79.db")
    vac_file = str(tmp_path / "test_vacancies79.json")
    monkeypatch.setattr(config, "DB_FILE", db_file)
    monkeypatch.setattr(config, "VACANCIES_FILE", vac_file)
    monkeypatch.setattr(db, "get_connection", lambda: __import__("sqlite3").connect(db_file))
    db.init_db()
    # Mock Watcher to avoid external network requests
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
# 1. test_successful_delivery_marks_digest_items_delivered
# ------------------------------------------------------------------------------
def test_successful_delivery_marks_digest_items_delivered(isolated_env, monkeypatch):
    """Successful Telegram delivery marks all delivered vacancies as DELIVERED."""
    # 1. Insert 3 fresh eligible vacancies
    v1 = _make_matching_vacancy("remoteok", "rok_succ_1", "Success Co 1")
    v2 = _make_matching_vacancy("himalayas", "him_succ_2", "Success Co 2")
    v3 = _make_matching_vacancy("weworkremotely", "wwr_succ_3", "Success Co 3")
    db.save_vacancy(v1)
    db.save_vacancy(v2)
    db.save_vacancy(v3)

    send_call_count = 0
    captured_payload = []

    def mock_send_to_telegram(text: str) -> bool:
        nonlocal send_call_count
        send_call_count += 1
        captured_payload.append(text)
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send_to_telegram)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    # Execute fetcher entrypoint
    jsf.main()

    assert send_call_count == 1
    assert len(captured_payload) == 1
    assert "Success Co 1" in captured_payload[0]

    # Verify DB delivery state
    assert db.is_digest_delivered(v1.stable_id())
    assert db.is_digest_delivered(v2.stable_id())
    assert db.is_digest_delivered(v3.stable_id())

    # Check delivery records detail
    records = db.list_telegram_delivery_records(status="DELIVERED", limit=10)
    vac_records = [r for r in records if r["notification_type"] == "job_digest"]
    assert len(vac_records) == 3
    keys = {r["delivery_key"] for r in vac_records}
    assert keys == {f"digest:{v1.stable_id()}", f"digest:{v2.stable_id()}", f"digest:{v3.stable_id()}"}


# ------------------------------------------------------------------------------
# 2. test_failed_delivery_does_not_mark_anything_delivered
# ------------------------------------------------------------------------------
def test_failed_delivery_does_not_mark_anything_delivered(isolated_env, monkeypatch):
    """Failed Telegram delivery leaves vacancies undelivered and returns failure."""
    v1 = _make_matching_vacancy("remoteok", "rok_fail_1", "Fail Co 1")
    v2 = _make_matching_vacancy("himalayas", "him_fail_2", "Fail Co 2")
    v3 = _make_matching_vacancy("weworkremotely", "wwr_fail_3", "Fail Co 3")
    db.save_vacancy(v1)
    db.save_vacancy(v2)
    db.save_vacancy(v3)

    monkeypatch.setattr(jsf, "send_to_telegram", lambda text: False)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        jsf.main()
    assert exc_info.value.code == 1

    # Verify zero delivery records
    assert not db.is_digest_delivered(v1.stable_id())
    assert not db.is_digest_delivered(v2.stable_id())
    assert not db.is_digest_delivered(v3.stable_id())

    records = db.list_telegram_delivery_records(status="DELIVERED", limit=10)
    assert len(records) == 0

    # Vacancies must remain in fresh queue
    undigested = db.list_undigested_vacancies(limit=10)
    assert len(undigested) == 3


# ------------------------------------------------------------------------------
# 3. test_telegram_exception_does_not_mark_delivered
# ------------------------------------------------------------------------------
def test_telegram_exception_does_not_mark_delivered(isolated_env, monkeypatch):
    """Exceptions raised during Telegram delivery do not create false DELIVERED state."""
    v = _make_matching_vacancy("remoteok", "rok_exc_1", "Exception Co")
    db.save_vacancy(v)

    def mock_throw(text: str):
        raise RuntimeError("Simulated Telegram Network Drop / TLS Error")

    monkeypatch.setattr(jsf, "send_to_telegram", mock_throw)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    with pytest.raises((RuntimeError, SystemExit)):
        jsf.main()

    assert not db.is_digest_delivered(v.stable_id())
    records = db.list_telegram_delivery_records(status="DELIVERED", limit=10)
    assert len(records) == 0


# ------------------------------------------------------------------------------
# 4. test_retry_after_failure_returns_same_vacancies
# ------------------------------------------------------------------------------
def test_retry_after_failure_returns_same_vacancies(isolated_env, monkeypatch):
    """Failed delivery retry recovers and delivers the exact same vacancies."""
    v1 = _make_matching_vacancy("remoteok", "rok_retry_1", "Retry Co 1")
    v2 = _make_matching_vacancy("himalayas", "him_retry_2", "Retry Co 2")
    db.save_vacancy(v1)
    db.save_vacancy(v2)

    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    # 1. Run 1: Failure
    monkeypatch.setattr(jsf, "send_to_telegram", lambda text: False)
    with pytest.raises(SystemExit):
        jsf.main()
    assert not db.is_digest_delivered(v1.stable_id())
    assert not db.is_digest_delivered(v2.stable_id())

    # 2. Run 2: Retry succeeds
    delivered_batches = []
    def mock_success(text: str) -> bool:
        delivered_batches.append(text)
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_success)
    jsf.main()

    assert len(delivered_batches) == 1
    assert "Retry Co 1" in delivered_batches[0]
    assert "Retry Co 2" in delivered_batches[0]
    assert db.is_digest_delivered(v1.stable_id())
    assert db.is_digest_delivered(v2.stable_id())

    # 3. Run 3: Preview returns empty (already delivered)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    res = export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    assert res == 0
    data = json.loads(buf.getvalue())
    assert len(data["new_vacancies_data"]) == 0
    assert "Сегодня новых подходящих вакансий не найдено" in data["telegram_post"]


# ------------------------------------------------------------------------------
# 5. test_successful_delivery_is_idempotent
# ------------------------------------------------------------------------------
def test_successful_delivery_is_idempotent(isolated_env):
    """Marking delivery repeatedly is idempotent and does not violate DB constraints."""
    v_ids = ["remoteok:idemp_1", "himalayas:idemp_2"]

    count1 = db.mark_digest_delivered(v_ids, chat_id="-1004399255305")
    assert count1 == 2
    records1 = [r for r in db.list_telegram_delivery_records(status="DELIVERED", limit=10) if r["notification_type"] == "job_digest"]
    assert len(records1) == 2

    # Second call with same IDs
    count2 = db.mark_digest_delivered(v_ids, chat_id="-1004399255305")
    assert count2 == 2
    records2 = [r for r in db.list_telegram_delivery_records(status="DELIVERED", limit=10) if r["notification_type"] == "job_digest"]
    assert len(records2) == 2

    # Keys remain unique
    keys = [r["delivery_key"] for r in records2]
    assert len(keys) == len(set(keys))


# ------------------------------------------------------------------------------
# 6. test_partial_batch_failure_does_not_create_false_delivery_state
# ------------------------------------------------------------------------------
def test_partial_batch_failure_does_not_create_false_delivery_state(isolated_env, monkeypatch):
    """Since production digest is delivered as a single batch message, failure marks 0 items."""
    v1 = _make_matching_vacancy("remoteok", "rok_batch_1", "Batch Co 1")
    v2 = _make_matching_vacancy("remoteok", "rok_batch_2", "Batch Co 2")
    db.save_vacancy(v1)
    db.save_vacancy(v2)

    monkeypatch.setattr(jsf, "send_to_telegram", lambda text: False)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    with pytest.raises(SystemExit):
        jsf.main()

    # Neither vacancy is marked DELIVERED
    assert not db.is_digest_delivered(v1.stable_id())
    assert not db.is_digest_delivered(v2.stable_id())


# ------------------------------------------------------------------------------
# 7. test_preview_never_marks_delivered
# ------------------------------------------------------------------------------
def test_preview_never_marks_delivered(isolated_env, monkeypatch):
    """export-digest --json and --format telegram remain strictly read-only."""
    v = _make_matching_vacancy("habrcareer", "habr_prev_1", "Preview Co")
    db.save_vacancy(v)

    # 1. JSON Preview
    buf1 = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf1)
    export_digest_cmd(format_type="json", limit=5, min_score=60.0, output_json=True)
    assert not db.is_digest_delivered(v.stable_id())

    # 2. Telegram Markdown Preview
    buf2 = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf2)
    export_digest_cmd(format_type="telegram", limit=5, min_score=60.0, output_json=False)
    assert not db.is_digest_delivered(v.stable_id())

    # Records in DB must be exactly 0
    records = db.list_telegram_delivery_records(limit=10)
    assert len(records) == 0


# ------------------------------------------------------------------------------
# 8. test_legacy_vacancies_never_enter_delivery_batch
# ------------------------------------------------------------------------------
def test_legacy_vacancies_never_enter_delivery_batch(isolated_env, monkeypatch):
    """Legacy vacancies_json entries are neither selected for delivery nor marked."""
    v_legacy = _make_matching_vacancy("vacancies_json", "legacy_e2e_1", "Legacy Corp", salary_min=6000, salary_max=8000)
    db.save_vacancy(v_legacy)

    sent_texts = []
    def mock_send(text: str) -> bool:
        sent_texts.append(text)
        return True

    monkeypatch.setattr(jsf, "send_to_telegram", mock_send)
    monkeypatch.delenv("JOB_SEARCH_DRY_RUN", raising=False)

    jsf.main()

    # No message sent because no fresh vacancies exist
    assert len(sent_texts) == 0
    assert not db.is_digest_delivered(v_legacy.stable_id())