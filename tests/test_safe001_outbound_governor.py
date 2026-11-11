"""Verification tests for SAFE-001: Central Outbound Action Governor wired to all production outbound paths."""

from unittest.mock import patch

import pytest

from ai_assistant import config, db
from ai_assistant.browser_executor import submit_application_in_browser
from ai_assistant.email_outreach import send_smtp_email
from ai_assistant.external_form_filler import try_auto_fill_external_form
from ai_assistant.hh_submission import submission_halt_reason
from ai_assistant.outbound_governor import (
    OutboundAction,
    OutboundHaltedError,
    check_outbound_allowed,
    guard_outbound,
    is_db_pause_active,
)
from ai_assistant.telegram.transport import send_message as transport_send_message
from ai_assistant.telegram_notifier import TelegramNotifier
from integrations.hermes.job_search_fetcher import send_to_telegram


@pytest.fixture
def clean_governor_env(tmp_path, monkeypatch):
    """Isolate stop files and database for governor testing."""
    stop_dir = tmp_path / "stops"
    stop_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STOP_OUTBOUNDS_DIR", str(stop_dir))
    monkeypatch.delenv("STOP_OUTBOUNDS_FILE", raising=False)
    monkeypatch.delenv("STOP_SUBMITS_FILE", raising=False)

    test_db = str(tmp_path / "test_governor.db")
    orig_db = config.DB_FILE
    config.DB_FILE = test_db
    db.init_db()

    yield stop_dir

    config.DB_FILE = orig_db


def test_safe001_all_outbounds_halted_when_stop_all_file_present(clean_governor_env):
    """When STOP_ALL_OUTBOUNDS is present on disk, every single production outbound path refuses to execute."""
    stop_dir = clean_governor_env
    stop_file = stop_dir / "STOP_ALL_OUTBOUNDS"
    stop_file.touch()

    # 1. Governor core API
    allowed, reason = check_outbound_allowed(OutboundAction.SUBMISSION)
    assert allowed is False
    assert "Emergency stop file present" in reason

    with pytest.raises(OutboundHaltedError):
        guard_outbound(OutboundAction.SUBMISSION)

    with pytest.raises(OutboundHaltedError):
        guard_outbound(OutboundAction.EMAIL)

    with pytest.raises(OutboundHaltedError):
        guard_outbound(OutboundAction.TELEGRAM)

    # 2. Production Path 1: Submission authority & browser executor
    halt_reason = submission_halt_reason(include_submit_allowed=True)
    assert halt_reason is not None
    assert "STOP_SUBMITS file present" in halt_reason

    submit_res = submit_application_in_browser("hh:999999", confirm_submit=True, dry_run=False)
    assert submit_res.status == "BLOCKED"
    assert "STOP_SUBMITS file present" in submit_res.error

    # 3. Production Path 2: External Form / Questionnaire
    form_res = try_auto_fill_external_form(
        cdp_url="http://127.0.0.1:9222",
        form_url="https://docs.google.com/forms/d/e/test/viewform",
    )
    assert form_res.ok is False
    assert form_res.blocked is True
    assert "Emergency stop file present" in form_res.blocked_reason

    # 4. Production Path 3: Email Outreach (SMTP)
    with patch("smtplib.SMTP_SSL") as mock_smtp:
        sent, err_msg = send_smtp_email("hr@example.com", "Test Subject", "Test Body")
        assert sent is False
        assert "заблокирована предохранителем" in err_msg
        mock_smtp.assert_not_called()

    # 5. Production Path 4: Telegram (transport, notifier, fetcher)
    with patch("urllib.request.urlopen") as mock_url:
        tg_ok = transport_send_message(12345, "Hello")
        assert tg_ok is False

        notifier = TelegramNotifier("fake_token", "12345")
        notifier_res = notifier.send_message("Test")
        assert notifier_res.get("ok") is False
        assert "Halted by outbound governor" in notifier_res.get("error", "")

        fetcher_ok = send_to_telegram("Digest")
        assert fetcher_ok is False

        mock_url.assert_not_called()


def test_safe001_stop_submits_halts_only_submission_and_questionnaire(clean_governor_env):
    """When only STOP_SUBMITS is present, submissions and questionnaires are halted, while alerts are allowed."""
    stop_dir = clean_governor_env
    stop_file = stop_dir / "STOP_SUBMITS"
    stop_file.touch()

    # Submission halted
    assert check_outbound_allowed(OutboundAction.SUBMISSION)[0] is False
    assert check_outbound_allowed(OutboundAction.QUESTIONNAIRE)[0] is False

    # Email and Telegram not halted by STOP_SUBMITS (only STOP_ALL_OUTBOUNDS halts them)
    assert check_outbound_allowed(OutboundAction.EMAIL)[0] is True
    assert check_outbound_allowed(OutboundAction.TELEGRAM)[0] is True


def test_safe001_db_pause_halts_production_paths(clean_governor_env):
    """When system_settings submit_paused=1 in DB, submissions and questionnaires halt fail-closed."""
    db.set_system_setting("submit_paused", "1")

    allowed, reason = check_outbound_allowed(OutboundAction.SUBMISSION)
    assert allowed is False
    assert "submit_paused = 1" in reason

    halt_reason = submission_halt_reason()
    assert halt_reason is not None
    assert "submit_paused = 1" in halt_reason

    form_res = try_auto_fill_external_form(
        cdp_url="http://127.0.0.1:9222",
        form_url="https://docs.google.com/forms/d/e/test/viewform",
    )
    assert form_res.ok is False
    assert form_res.blocked is True
    assert "submit_paused = 1" in form_res.blocked_reason


def test_safe001_fail_closed_on_db_exception():
    """Governor must fail closed when DB reads fail unexpectedly."""
    with patch("ai_assistant.db.is_submit_paused", side_effect=RuntimeError("disk I/O error")):
        paused, reason = is_db_pause_active(OutboundAction.SUBMISSION)
        assert paused is True
        assert "fail-closed" in reason
