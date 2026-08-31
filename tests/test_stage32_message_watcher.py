"""Stage 32: Tests for Controlled HH Message Watcher.

Verifies:
1. New incoming employer message is discovered and processed.
2. Outgoing message from candidate is NOT treated as new incoming.
3. Duplicate poll ignores already-processed message in state.db (idempotency).
4. New message properly resolves and links to vacancy context.
5. Direct question generates validated draft and reaches READY_FOR_HUMAN_REVIEW.
6. Sensitive/salary/unverified question reaches NEEDS_HUMAN_REVIEW without draft.
7. System notification or auto-closing message -> NO_REPLY.
8. CDP/Browser failure fails closed (BLOCKED).
9. Safety Invariants: Watcher NEVER autonomously sends messages.
10. Approved message can be sent through existing Stage 30D `hh_message_send(confirm=True)`.
11. CLI `job-search message-watch --once` and `--json` works cleanly.
"""

from __future__ import annotations

import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ai_assistant.schema import Vacancy
from ai_assistant.candidate_profile import CandidateProfile
from ai_assistant import db
import ai_assistant.config as config
from ai_assistant.hh_message_watcher import (
    HHMessageWatcher,
    HHMessageWatcherConfig,
    HHMessageWatcherStatus,
    compute_message_fingerprint,
    run_message_watcher_cycle,
)
from ai_assistant.hh_message_reply import (
    HHDialog,
    HHMessage,
    MessageClassification,
    send_confirmed_hh_reply,
)
from ai_assistant import cli


def _create_test_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_roles=["AI Automation Engineer", "Python Developer", "Backend Developer"],
        skills=["Python", "FastAPI", "n8n", "Docker", "PostgreSQL", "LLM"],
        preferred_seniority=["Senior", "Lead"],
        remote_required=True,
        allowed_locations=["Remote", "Worldwide"],
        allowed_timezones=[],
        languages=["English", "Russian"],
        employment_types=["Full-time"],
        minimum_salary=5000,
        salary_currency="USD",
        years_experience="5",
        excluded_roles=["DevOps", "Frontend React"],
        excluded_companies=["SpammyCorp"],
        excluded_countries=[],
        excluded_industries=[],
    )


@pytest.fixture
def clean_db(tmp_path):
    orig_db = config.DB_FILE
    db_file = str(tmp_path / "test_msg_watcher.db")
    config.DB_FILE = db_file
    db.init_db()

    profile = _create_test_profile()
    profile_path = str(tmp_path / "test_profile.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f)

    yield {"db_file": db_file, "profile_path": profile_path, "profile": profile}

    config.DB_FILE = orig_db


class FakeChatikCDP:
    """Simulates read-only HH chatik DOM responses."""

    def __init__(
        self,
        conversations: list | None = None,
        active_messages: list | None = None,
        active_title: str = "Senior AI Engineer",
        active_employer: str = "TechCorp",
        fail_connection: bool = False,
    ):
        self.conversations = conversations or []
        self.active_messages = active_messages or []
        self.active_title = active_title
        self.active_employer = active_employer
        self.fail_connection = fail_connection
        self.sent_messages: list[str] = []

    def evaluate(self, expr: str) -> str:
        if self.fail_connection:
            raise RuntimeError("CDP connection failed: target crashed")

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

        # 3. Send confirmed reply (Stage 30D)
        if "chatik-send" in expr or "composer" in expr or "button" in expr:
            self.sent_messages.append(expr)
            return json.dumps({"ok": True, "sent": True})

        return json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# Test 1: New incoming employer message is discovered and processed
# ---------------------------------------------------------------------------

def test_new_incoming_message_discovered(clean_db):
    """A new incoming employer message is parsed, classified, validated, and recorded."""
    convs = [{
        "conversation_id": "c101",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Добрый день! Подскажите, когда вы готовы приступить к работе?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "OUTGOING", "text": "Здравствуйте, отклик на вакансию."},
        {"message_id": "msg_002", "direction": "INCOMING", "text": "Добрый день! Подскажите, когда вы готовы приступить к работе?", "sent_at": "12:30"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs, active_title="Python Developer", active_employer="TechCorp")

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
        batch_limit=10,
    )

    result = run_message_watcher_cycle(cfg)

    assert result.conversations_checked == 1
    assert result.messages_seen == 2
    assert result.new_messages == 1
    assert result.already_processed == 0
    assert result.replies_prepared == 1
    assert result.ready_for_human_review == 1
    assert result.needs_human_review == 0
    assert result.replies_sent == 0
    assert len(result.items) == 1

    item = result.items[0]
    assert item.conversation_id == "c101"
    assert item.sender == "employer"
    assert item.status == HHMessageWatcherStatus.READY_FOR_HUMAN_REVIEW.value
    assert item.classification == "NEEDS_REPLY"
    assert item.validation == "APPROVED"
    assert item.reply_draft is not None
    assert item.reply_attempted is False
    assert item.reply_sent is False

    # Check state.db persistence
    ev = db.get_hh_message_event(item.message_fingerprint)
    assert ev is not None
    assert ev["conversation_id"] == "c101"
    assert ev["processed"] is True
    assert ev["status"] == "READY_FOR_HUMAN_REVIEW"


# ---------------------------------------------------------------------------
# Test 2: Outgoing message from candidate is NOT processed as incoming
# ---------------------------------------------------------------------------

def test_outgoing_candidate_message_not_treated_as_incoming(clean_db):
    """When the latest message in a conversation is from candidate, watcher ignores it."""
    convs = [{
        "conversation_id": "c102",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Отправил резюме",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Здравствуйте!"},
        {"message_id": "msg_002", "direction": "OUTGOING", "text": "Здравствуйте, отправил резюме."},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)

    assert result.conversations_checked == 1
    assert result.new_messages == 0
    assert result.replies_prepared == 0
    assert len(result.items) == 0


# ---------------------------------------------------------------------------
# Test 3: Idempotency (subsequent poll ignores already-processed message)
# ---------------------------------------------------------------------------

def test_message_watcher_idempotency(clean_db):
    """Running subsequent polling cycles does not duplicate message processing or replies."""
    convs = [{
        "conversation_id": "c103",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Когда готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Когда готовы приступить?", "sent_at": "14:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    # Cycle 1
    res1 = run_message_watcher_cycle(cfg, iteration=1)
    assert res1.new_messages == 1
    assert res1.already_processed == 0
    assert res1.replies_prepared == 1

    # Cycle 2
    res2 = run_message_watcher_cycle(cfg, iteration=2)
    assert res2.new_messages == 0
    assert res2.already_processed == 1
    assert res2.replies_prepared == 0
    assert len(res2.items) == 0


# ---------------------------------------------------------------------------
# Test 4: New message resolves and links to vacancy context
# ---------------------------------------------------------------------------

def test_message_links_to_tracked_vacancy(clean_db):
    """Message matches tracked vacancy by employer or title."""
    # Pre-save vacancy in DB
    vac = Vacancy(
        source="hh",
        source_job_id="998877",
        title="Senior Python Backend Engineer",
        company="FinTech Innovations",
        description="We need python backend",
        job_url="https://hh.ru/vacancy/998877",
        location="Remote",
    )
    db.save_vacancy(vac)

    convs = [{
        "conversation_id": "c104",
        "title": "Senior Python Backend Engineer",
        "employer": "FinTech Innovations",
        "snippet": "Здравствуйте! Готовы ли выполнить тестовое задание?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Здравствуйте! Готовы ли выполнить тестовое задание?", "sent_at": "15:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs, active_title="Senior Python Backend Engineer", active_employer="FinTech Innovations")

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.new_messages == 1
    item = result.items[0]
    assert item.employer == "FinTech Innovations"
    assert item.vacancy_stable_id == "hh:998877"


# ---------------------------------------------------------------------------
# Test 5: Sensitive/Salary question -> NEEDS_HUMAN_REVIEW
# ---------------------------------------------------------------------------

def test_salary_question_yields_needs_human_review(clean_db):
    """A salary question requires human judgment and stops at NEEDS_HUMAN_REVIEW without auto-draft."""
    convs = [{
        "conversation_id": "c105",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Какие у вас зарплатные ожидания?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Какие у вас зарплатные ожидания?", "sent_at": "16:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.new_messages == 1
    assert result.ready_for_human_review == 0
    assert result.needs_human_review == 1
    assert result.replies_prepared == 0

    item = result.items[0]
    assert item.status == HHMessageWatcherStatus.NEEDS_HUMAN_REVIEW.value
    assert item.reply_draft is None


# ---------------------------------------------------------------------------
# Test 6: System notification / auto-closing -> NO_REPLY
# ---------------------------------------------------------------------------

def test_system_notification_yields_no_reply(clean_db):
    """A system notification (auto-reject or closing) is classified as NO_REPLY."""
    convs = [{
        "conversation_id": "c106",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "К сожалению, мы выбрали другого кандидата. Спасибо за отклик!",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "К сожалению, мы выбрали другого кандидата. Спасибо за отклик!", "sent_at": "17:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.new_messages == 1
    assert result.ready_for_human_review == 0
    assert result.needs_human_review == 0
    assert result.replies_prepared == 0

    item = result.items[0]
    assert item.status == HHMessageWatcherStatus.NO_REPLY.value
    assert item.reply_draft is None


# ---------------------------------------------------------------------------
# Test 7: CDP connection failure fails closed (BLOCKED)
# ---------------------------------------------------------------------------

def test_cdp_connection_failure_fails_closed(clean_db):
    """When browser target is unreachable, watcher fails closed with status BLOCKED."""
    cdp = FakeChatikCDP(fail_connection=True)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.blocked >= 1
    assert result.new_messages == 0
    assert result.replies_sent == 0
    assert len(result.errors) >= 1


# ---------------------------------------------------------------------------
# Test 8: Safety Invariants: Watcher NEVER autonomously sends messages
# ---------------------------------------------------------------------------

def test_watcher_never_autonomously_sends_messages(clean_db):
    """Watcher invariants: reply_sent_count == 0, duplicate_reply == 0, human_approval_bypass == False."""
    convs = [{
        "conversation_id": "c108",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Готовы приступить к работе?", "sent_at": "18:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.replies_sent == 0
    assert result.reply_sent_count == 0
    assert result.duplicate_reply_count == 0
    assert result.human_approval_bypass is False

    for it in result.items:
        assert it.reply_attempted is False
        assert it.reply_sent is False

    assert len(cdp.sent_messages) == 0


# ---------------------------------------------------------------------------
# Test 9: Approved message can be sent through existing Stage 30D send primitive
# ---------------------------------------------------------------------------

def test_approved_reply_sends_via_existing_stage30d(clean_db):
    """A prepared and approved reply draft is sent using existing send_confirmed_hh_reply."""
    convs = [{
        "conversation_id": "c109",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Когда готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Когда вы готовы приступить к работе?", "sent_at": "19:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.replies_prepared == 1
    item = result.items[0]
    draft = item.reply_draft
    assert draft is not None

    # Use existing Stage 30D / 22 primitive with human confirmation
    send_res = send_confirmed_hh_reply(cdp.evaluate, draft)
    assert send_res.get("ok") is True
    assert len(cdp.sent_messages) == 1


# ---------------------------------------------------------------------------
# Test 10: CLI `message-watch --once` and `--json` execution
# ---------------------------------------------------------------------------

def test_cli_message_watch_once_json(clean_db, capsys):
    """CLI message-watch with --once and --json produces valid JSON report and returns 0."""
    convs = [{
        "conversation_id": "c110",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Когда готовы приступить?",
        "is_selected": True,
    }]
    msgs = [
        {"message_id": "msg_001", "direction": "INCOMING", "text": "Когда готовы приступить?", "sent_at": "20:00"},
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    ret = cli.message_watch_cmd(
        once=True,
        limit=5,
        profile_path=clean_db["profile_path"],
        output_json=True,
        evaluate_fn=cdp.evaluate,
    )

    assert ret == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["conversations_checked"] == 1
    assert data["new_messages"] == 1
    assert data["replies_prepared"] == 1
    assert data["replies_sent"] == 0
    assert data["human_approval_bypass"] is False


# ---------------------------------------------------------------------------
# Test 11: Stage 32E Informational employer message generates validated draft
# ---------------------------------------------------------------------------

def test_stage32e_informational_employer_message_creates_draft(clean_db):
    """Informational message ('Спасибо за отклик... Изучу резюме') yields NO_REPLY without draft."""
    convs = [{
        "conversation_id": "c111",
        "title": "Python Developer",
        "employer": "JT marketing",
        "snippet": "Михаил Кириллович, здравствуйте! Спасибо за отклик на нашу вакансию. Изучу ваше резюме, покажу его нанимающему менеджеру для согласования, и чуть позже вернусь с ответом к вам!",
        "is_selected": True,
    }]
    msgs = [
        {
            "message_id": "msg_001",
            "direction": "INCOMING",
            "text": "Михаил Кириллович, здравствуйте! Спасибо за отклик на нашу вакансию. Изучу ваше резюме, покажу его нанимающему менеджеру для согласования, и чуть позже вернусь с ответом к вам!",
            "sent_at": "04:47",
        }
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.conversations_checked == 1
    assert result.new_messages == 1
    assert result.replies_prepared == 0
    assert result.ready_for_human_review == 0
    assert result.replies_sent == 0

    item = result.items[0]
    assert item.status == HHMessageWatcherStatus.NO_REPLY.value
    assert item.reply_draft is None


def test_stage32e_salary_question_requires_human_decision(clean_db):
    """Salary question strictly stops at NEEDS_HUMAN_REVIEW without automated draft."""
    convs = [{
        "conversation_id": "c112",
        "title": "Python Developer",
        "employer": "TechCorp",
        "snippet": "Какой уровень оклада вы ожидаете?",
        "is_selected": True,
    }]
    msgs = [
        {
            "message_id": "msg_002",
            "direction": "INCOMING",
            "text": "Какой уровень оклада вы ожидаете на руки в месяц?",
            "sent_at": "12:00",
        }
    ]
    cdp = FakeChatikCDP(conversations=convs, active_messages=msgs)

    cfg = HHMessageWatcherConfig(
        custom_evaluate_fn=cdp.evaluate,
        profile_path=clean_db["profile_path"],
    )

    result = run_message_watcher_cycle(cfg)
    assert result.conversations_checked == 1
    assert result.new_messages == 1
    assert result.replies_prepared == 0
    assert result.needs_human_review == 1
    assert result.ready_for_human_review == 0
    assert result.replies_sent == 0

    item = result.items[0]
    assert item.status == HHMessageWatcherStatus.NEEDS_HUMAN_REVIEW.value
    assert item.reply_draft is None

