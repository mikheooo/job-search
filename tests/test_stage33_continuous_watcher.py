"""Stage 33: Tests for Continuous HH Message Watcher.

Verifies:
1. Continuous loop calls polling for multiple iterations.
2. Polling interval is respected.
3. Overlapping / concurrent cycles are prevented.
4. New incoming message is detected, drafted, and emits NEW HH MESSAGE event.
5. Repeated message is not processed a second time (idempotency).
6. Outgoing candidate message is never treated as incoming.
7. Browser disconnect/reconnect resilience (loop survives temporary CDP drops).
8. Graceful shutdown via stop() and KeyboardInterrupt (Ctrl+C).
9. ZERO AUTONOMOUS SEND invariant strictly maintained across continuous runs.
10. Existing CLI --once and --json behaviour remains fully functional.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch, MagicMock
import pytest

from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_message_watcher import (
    HHMessageWatcher,
    HHMessageWatcherConfig,
    HHMessageWatcherStatus,
    run_message_watcher_cycle,
)
from ai_assistant import cli


def _create_test_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["Python Developer", "AI Automation Engineer", "Backend Developer"],
        skills=["Python", "FastAPI", "Docker", "PostgreSQL", "LLM"],
        preferred_seniority=["Senior"],
        remote_required=True,
        allowed_locations=["Remote", "Worldwide"],
        allowed_timezones=[],
        languages=["English", "Russian"],
        employment_types=["Full-time"],
        minimum_salary=5000,
        salary_currency="USD",
        years_experience="5",
        excluded_roles=[],
        excluded_companies=[],
        excluded_countries=[],
        excluded_industries=[],
    )


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_stage33_watcher.db")
    config.DB_FILE = db_file
    db.init_db()

    profile = _create_test_profile()
    profile_path = str(tmp_path / "test_profile_stage33.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f)

    yield {"db_file": db_file, "profile_path": profile_path, "profile": profile}

    config.DB_FILE = orig_db


class FakeChatikCDP:
    """Simulates read-only HH chatik DOM responses with state transitions."""

    def __init__(
        self,
        conversations: list | None = None,
        active_messages: list | None = None,
        active_title: str = "Senior Python Developer",
        active_employer: str = "InnovateTech",
        fail_connection: bool = False,
    ):
        self.conversations = conversations or []
        self.active_messages = active_messages or []
        self.active_title = active_title
        self.active_employer = active_employer
        self.fail_connection = fail_connection
        self.call_count = 0
        self.sent_messages: list[str] = []

    def evaluate(self, expr: str) -> str:
        self.call_count += 1
        if self.fail_connection:
            raise RuntimeError("CDP connection failed: target disconnected")

        # 1. Fetch conversations list
        if "conversations-list" in expr or "chatik-conversation" in expr or "conversations" in expr:
            return json.dumps({
                "ok": True,
                "conversations": self.conversations,
            })

        # 2. Fetch single conversation message history
        if "messages" in expr or "chatik-messages" in expr or "conversation-detail" in expr:
            return json.dumps({
                "ok": True,
                "title": self.active_title,
                "employer": self.active_employer,
                "messages": self.active_messages,
            })

        # 3. Guard against send calls
        if "chatik-send" in expr or "composer" in expr or "button" in expr:
            self.sent_messages.append(expr)
            return json.dumps({"ok": True, "sent": True})

        return json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# Test 1: Continuous loop calls polling for multiple iterations
# ---------------------------------------------------------------------------

def test_continuous_loop_executes_multiple_iterations(clean_db):
    """HHMessageWatcher.run() executes multiple iterations up to max_iterations."""
    convs = [{
        "conversation_id": "c201",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Когда готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Когда готовы приступить к работе?", "sent_at": "10:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=3,
    )
    watcher = HHMessageWatcher(cfg)

    with patch("time.sleep", return_value=None):
        results = watcher.run()

    assert len(results) == 3
    assert results[0].iteration == 1
    assert results[0].new_messages == 1
    assert results[1].iteration == 2
    assert results[1].new_messages == 0  # Idempotent on 2nd cycle
    assert results[1].already_processed == 1
    assert results[2].iteration == 3
    assert results[2].new_messages == 0


# ---------------------------------------------------------------------------
# Test 2: Polling interval is respected via sleep slicing
# ---------------------------------------------------------------------------

def test_continuous_loop_respects_interval(clean_db):
    """Watcher sleeps for the configured interval between polling cycles."""
    cdp = FakeChatikCDP(conversations=[], active_messages=[])

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=10,
        max_iterations=2,
    )
    watcher = HHMessageWatcher(cfg)

    slept_intervals = []

    def mock_sleep(seconds):
        slept_intervals.append(seconds)

    with patch("time.sleep", side_effect=mock_sleep):
        watcher.run()

    total_slept = sum(slept_intervals)
    assert total_slept >= 10.0


# ---------------------------------------------------------------------------
# Test 3: New incoming message is detected, drafted, and emits explicit event
# ---------------------------------------------------------------------------

def test_continuous_loop_discovers_new_incoming_message(clean_db, capsys):
    """New incoming employer message triggers explicit NEW HH MESSAGE console output."""
    convs = [{
        "conversation_id": "c203",
        "title": "Senior Python Developer",
        "employer": "FinTech LLC",
        "snippet": "Здравствуйте! Когда готовы приступить к работе?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "OUTGOING", "text": "Здравствуйте, отклик на вакансию."},
        {"message_id": "msg_002", "direction": "INCOMING", "text": "Здравствуйте! Когда готовы приступить к работе?", "sent_at": "11:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs, active_title="Senior Python Developer", active_employer="FinTech LLC")

    events_received = []

    def on_msg_cb(item):
        events_received.append(item)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=1,
        on_new_message=on_msg_cb,
    )
    watcher = HHMessageWatcher(cfg)

    with patch("time.sleep", return_value=None):
        watcher.run()

    assert len(events_received) == 1
    item = events_received[0]
    assert item.conversation_id == "c203"
    assert item.employer == "FinTech LLC"
    assert item.status == HHMessageWatcherStatus.READY_FOR_HUMAN_REVIEW.value
    assert item.reply_draft is not None

    captured = capsys.readouterr()
    assert "NEW HH MESSAGE" in captured.out
    assert "Conversation: c203" in captured.out
    assert "Employer:     FinTech LLC" in captured.out
    assert "Status:       READY_FOR_HUMAN_REVIEW" in captured.out


# ---------------------------------------------------------------------------
# Test 4: Idempotency in continuous loop prevents duplicate reviews
# ---------------------------------------------------------------------------

def test_continuous_loop_idempotency_prevents_duplicate_processing(clean_db):
    """Subsequent cycles do not re-classify or re-draft already-seen messages."""
    convs = [{
        "conversation_id": "c204",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Добрый день! Подскажите, когда вы готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Добрый день! Подскажите, когда вы готовы приступить?", "sent_at": "12:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=2,
    )
    watcher = HHMessageWatcher(cfg)

    with patch("time.sleep", return_value=None):
        results = watcher.run()

    assert len(results) == 2
    assert results[0].new_messages == 1
    assert results[0].ready_for_human_review == 1
    assert results[1].new_messages == 0
    assert results[1].already_processed == 1
    assert results[1].ready_for_human_review == 0


# ---------------------------------------------------------------------------
# Test 5: Outgoing candidate messages are never treated as incoming
# ---------------------------------------------------------------------------

def test_continuous_loop_ignores_outgoing_candidate_messages(clean_db):
    """When latest message is from candidate, watcher does not create drafts or reviews."""
    convs = [{
        "conversation_id": "c205",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Спасибо за отклик",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Здравствуйте!"},
        {"message_id": "msg_002", "direction": "OUTGOING", "text": "Здравствуйте, спасибо за ответ!"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=1,
    )
    watcher = HHMessageWatcher(cfg)

    with patch("time.sleep", return_value=None):
        results = watcher.run()

    assert results[0].new_messages == 0
    assert results[0].replies_prepared == 0
    assert len(results[0].items) == 0


# ---------------------------------------------------------------------------
# Test 6: Browser disconnect & reconnect resilience
# ---------------------------------------------------------------------------

def test_continuous_loop_resilience_to_browser_disconnection(clean_db):
    """Watcher survives temporary CDP disconnection on iteration 1 and recovers on iteration 2."""
    convs = [{
        "conversation_id": "c206",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Когда готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Когда готовы приступить к работе?", "sent_at": "13:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs, fail_connection=True)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=2,
    )
    watcher = HHMessageWatcher(cfg)

    iteration_count = 0

    def mock_sleep(seconds):
        nonlocal iteration_count
        iteration_count += 1
        # Recover connection on second iteration
        if iteration_count == 1:
            cdp.fail_connection = False

    with patch("time.sleep", side_effect=mock_sleep):
        results = watcher.run()

    assert len(results) == 2
    # Iteration 1 failed closed (blocked)
    assert results[0].blocked >= 1
    assert results[0].new_messages == 0
    # Iteration 2 recovered and processed incoming message
    assert results[1].blocked == 0
    assert results[1].new_messages == 1
    assert results[1].ready_for_human_review == 1


# ---------------------------------------------------------------------------
# Test 7: Graceful shutdown via stop() and KeyboardInterrupt
# ---------------------------------------------------------------------------

def test_continuous_loop_graceful_shutdown(clean_db):
    """Watcher gracefully stops when stop() or stop_callback is invoked."""
    cdp = FakeChatikCDP(conversations=[], active_messages=[])

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=60,
    )
    watcher = HHMessageWatcher(cfg)

    # Test 1: stop_callback cleanly halts after 2 iterations
    cycles_completed = 0

    def on_msg_cycle(item):
        pass

    def stop_cb():
        return cycles_completed >= 2

    def mock_poll_once(iteration=1):
        nonlocal cycles_completed
        cycles_completed += 1
        return run_message_watcher_cycle(cfg, iteration=iteration)

    with patch.object(watcher, "poll_once", side_effect=mock_poll_once), patch("time.sleep", return_value=None):
        results = watcher.run(stop_callback=stop_cb)

    assert len(results) == 2
    assert watcher._running is False

    # Test 2: watcher.stop() cleanly halts the loop
    watcher2 = HHMessageWatcher(cfg)
    with patch("time.sleep", side_effect=lambda _: watcher2.stop()):
        results2 = watcher2.run()

    assert len(results2) == 1
    assert watcher2._running is False


def test_cli_message_watch_handles_keyboard_interrupt(clean_db, capsys):
    """cli.message_watch_cmd catches KeyboardInterrupt and exits cleanly with 0."""
    with patch("ai_assistant.hh_message_watcher.HHMessageWatcher.run", side_effect=KeyboardInterrupt):
        code = cli.message_watch_cmd(
            once=False,
            profile_path=clean_db["profile_path"],
        )
    assert code == 0
    captured = capsys.readouterr()
    assert "Message watcher stopped by user." in captured.out


# ---------------------------------------------------------------------------
# Test 8: Prevention of concurrent / parallel polling cycles
# ---------------------------------------------------------------------------

def test_prevent_concurrent_polling(clean_db):
    """If a polling cycle is already running, poll_once returns immediately with warning."""
    cdp = FakeChatikCDP(conversations=[], active_messages=[])
    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )
    watcher = HHMessageWatcher(cfg)

    watcher._is_polling = True
    res = watcher.poll_once(iteration=99)
    assert res.conversations_checked == 0
    assert res.new_messages == 0
    watcher._is_polling = False


# ---------------------------------------------------------------------------
# Test 9: ZERO AUTONOMOUS SEND Invariant strictly maintained
# ---------------------------------------------------------------------------

def test_continuous_loop_zero_autonomous_send_invariant(clean_db):
    """Throughout multi-iteration runs, replies_sent is strictly 0 and no browser send is invoked."""
    convs = [
        {
            "conversation_id": "c209_1",
            "title": "Python Developer",
            "employer": "TechCorp",
            "snippet": "Когда готовы приступить?",
            "is_selected": True,
        },
        {
            "conversation_id": "c209_2",
            "title": "Python Developer",
            "employer": "FinanceCorp",
            "snippet": "Какие зарплатные ожидания?",
            "is_selected": False,
        },
    ]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Когда готовы приступить к работе?", "sent_at": "14:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        poll_interval_seconds=1,
        max_iterations=3,
    )
    watcher = HHMessageWatcher(cfg)

    with patch("time.sleep", return_value=None):
        results = watcher.run()

    for res in results:
        assert res.replies_sent == 0
        assert res.reply_sent_count == 0
        assert res.duplicate_reply_count == 0
        assert res.human_approval_bypass is False
        for it in res.items:
            assert it.reply_attempted is False
            assert it.reply_sent is False

    assert len(cdp.sent_messages) == 0


# ---------------------------------------------------------------------------
# Test 10: CLI --continuous, --once, and --json functionality
# ---------------------------------------------------------------------------

def test_cli_message_watch_continuous_and_once(clean_db, capsys):
    """cli.message_watch_cmd works with continuous=True, once=True, and json=True."""
    convs = [{
        "conversation_id": "c210",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Когда готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Когда готовы приступить к работе?", "sent_at": "15:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    # 1. Test --once --json
    ret_json = cli.message_watch_cmd(
        once=True,
        profile_path=clean_db["profile_path"],
        output_json=True,
        evaluate_fn=cdp.evaluate,
    )
    assert ret_json == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["conversations_checked"] == 1
    assert data["new_messages"] == 1
    assert data["replies_sent"] == 0

    # 2. Test --continuous with stop callback
    ret_cont = cli.message_watch_cmd(
        continuous=True,
        profile_path=clean_db["profile_path"],
        evaluate_fn=cdp.evaluate,
        stop_callback=lambda: True,
    )
    assert ret_cont == 0
