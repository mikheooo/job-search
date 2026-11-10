"""Tests for Kwork Inbox Monitor & Auth Guard."""

import os
import pathlib
import tempfile
import pytest

from ai_assistant.gig.kwork_inbox_monitor import (
    KworkInboxStorage,
    KworkScanResult,
    KworkMessage,
    process_monitor_cycle,
    format_auth_warning,
    format_auth_recovered,
)


class MockSender:
    def __init__(self, should_succeed=True):
        self.should_succeed = should_succeed
        self.sent_messages = []

    def __call__(self, text, parse_mode="HTML"):
        self.sent_messages.append({"text": text, "parse_mode": parse_mode})
        class Res:
            ok = self.should_succeed
            status = "delivered" if self.should_succeed else "failed"
            detail = ""
            message_id = 999
        return Res()


def test_dry_run_suppresses_auth_alert():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = pathlib.Path(tmpdir) / "kwork_inbox.db"
        storage = KworkInboxStorage(db_path)
        sender = MockSender()

        scan_fn = lambda: KworkScanResult(
            is_authenticated=False,
            auth_status="AUTH_REQUIRED",
            auth_error="Confirmed login page rendered (/login)",
        )

        # In dry-run, live alert MUST NOT be sent
        report = process_monitor_cycle(storage, scan_fn, sender_fn=sender, dry_run=True)
        assert len(sender.sent_messages) == 0
        assert report.auth_status == "AUTH_REQUIRED"
        assert any("strike 1/2" in d for d in report.details)


def test_transient_page_not_ready_does_not_trigger_auth_alert():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = pathlib.Path(tmpdir) / "kwork_inbox.db"
        storage = KworkInboxStorage(db_path)
        sender = MockSender()

        scan_fn = lambda: KworkScanResult(
            is_authenticated=False,
            auth_status="PAGE_NOT_READY",
            auth_error="DOM readyState is loading",
        )

        # Even with dry_run=False, transient PAGE_NOT_READY MUST NOT trigger auth alert
        report = process_monitor_cycle(storage, scan_fn, sender_fn=sender, dry_run=False)
        assert len(sender.sent_messages) == 0
        assert report.auth_status == "PAGE_NOT_READY"
        assert any("Page not ready" in d for d in report.details)


def test_live_auth_required_sends_alert_and_observes_cooldown():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = pathlib.Path(tmpdir) / "kwork_inbox.db"
        storage = KworkInboxStorage(db_path)
        sender = MockSender()

        scan_fn = lambda: KworkScanResult(
            is_authenticated=False,
            auth_status="AUTH_REQUIRED",
            auth_error="Confirmed login page rendered (/login)",
        )

        # Cycle 1: first login-page observation is only a strike, no alert
        report1 = process_monitor_cycle(storage, scan_fn, sender_fn=sender, dry_run=False)
        assert report1.auth_status == "AUTH_REQUIRED"
        assert len(sender.sent_messages) == 0

        # Cycle 2: second consecutive confirmation sends exactly one alert
        report2 = process_monitor_cycle(storage, scan_fn, sender_fn=sender, dry_run=False)
        assert report2.auth_status == "AUTH_LOST"
        assert len(sender.sent_messages) == 1
        assert "Требуется авторизация" in sender.sent_messages[0]["text"]
        assert "Confirmed login page rendered (/login)" in sender.sent_messages[0]["text"]

        # Cycle 3 immediately after: cooldown should suppress
        report3 = process_monitor_cycle(storage, scan_fn, sender_fn=sender, dry_run=False)
        assert report3.auth_status == "AUTH_LOST"
        assert len(sender.sent_messages) == 1  # No duplicate alert


def test_kwork_recovery_notification_sent():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = pathlib.Path(tmpdir) / "kwork_inbox.db"
        storage = KworkInboxStorage(db_path)
        sender = MockSender()

        fail_scan = lambda: KworkScanResult(
            is_authenticated=False,
            auth_status="AUTH_REQUIRED",
            auth_error="Confirmed login page rendered (/login)",
        )
        ok_scan = lambda: KworkScanResult(
            is_authenticated=True,
            auth_status="OK",
            messages=[],
        )

        # 1. Two consecutive failures send exactly one warning
        process_monitor_cycle(storage, fail_scan, sender_fn=sender, dry_run=False)
        process_monitor_cycle(storage, fail_scan, sender_fn=sender, dry_run=False)
        assert len(sender.sent_messages) == 1

        # 2. Recovery sends recovery notification
        process_monitor_cycle(storage, ok_scan, sender_fn=sender, dry_run=False)
        assert len(sender.sent_messages) == 2
        assert "авторизация подтверждена" in sender.sent_messages[1]["text"]

        # 3. Subsequent ok cycles do not re-send recovery
        process_monitor_cycle(storage, ok_scan, sender_fn=sender, dry_run=False)
        assert len(sender.sent_messages) == 2
