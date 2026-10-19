# -*- coding: utf-8 -*-
"""The autonomous reply SEND is opt-in and off by default.

Why this file exists
--------------------
`AutonomousJobAgent._process_messages()` contains a send of its own (Stage 51-55):
it types the draft into the chat box and clicks Send. Unlike the canonical pipeline
in `hh_message_reply.process_auto_reply()`, that send carries NONE of the gates —
no `HH_AUTO_REPLY_ENABLED` kill switch, no `is_safe_for_auto_reply()` allowlist, no
`can_auto_send()` re-verification, no `confirm_live_send`.

It fires for `classification in ("NEEDS_REPLY", "RECRUITER_QUESTION")`, which is
exactly the class of technical questions (experience, stack, salary) that the
allowlist in `hh_message_reply._AUTO_FORBIDDEN_RE` exists to keep for a human. And
because `AutonomousConfig.mode` defaults to FULL, a bare `run_autonomous_cycle()`
used to answer employers with no confirmation at all.

Local safety patch (2026-10-10): the send now requires
`HH_AUTONOMOUS_REPLY_ENABLED=true`, a switch deliberately SEPARATE from the guarded
pipeline's `HH_AUTO_REPLY_ENABLED` — arming the guarded path must never silently arm
this ungated one. Off, the draft is still generated and recorded; nothing is typed
and nothing is clicked.

`tests/conftest.py` arms the switch for the suite so the Stage 51-60 tests keep
pinning the mechanism; the tests below pin the default instead.
"""

import json
from unittest.mock import patch

import pytest

from ai_assistant import db
from ai_assistant.hh_autonomous_agent import (
    AutonomousConfig,
    AutonomousJobAgent,
    NotificationDispatcher,
)
from ai_assistant.hh_message_reply import HHDialog, HHMessage

SWITCH = "HH_AUTONOMOUS_REPLY_ENABLED"
CONV_ID = "conv_optin_1"


def _recruiter_question_dialog() -> HHDialog:
    return HHDialog(
        conversation_id=CONV_ID,
        vacancy_title="AI Engineer",
        vacancy_stable_id="hh:999888",
        employer="Axis",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Application response", sent_at="10:00"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Could you tell us about your experience with Python and FastAPI?",
                sent_at="10:05",
            ),
        ],
    )


def _evaluate_recorder(dialog: HHDialog):
    """A fake transport that records every script it is asked to run."""
    seen: list[str] = []

    def _evaluate(script: str) -> str:
        seen.append(script)
        if "chat-input-textarea" in script:
            return json.dumps({"ok": True, "conversation_id": dialog.conversation_id, "verified_in_hh": True})
        return json.dumps({"conversations": [dialog.model_dump()]})

    return _evaluate, seen


def test_send_is_skipped_when_switch_is_off(monkeypatch):
    """Default production behaviour: draft generated, nothing typed, nothing clicked."""
    monkeypatch.delenv(SWITCH, raising=False)

    dialog = _recruiter_question_dialog()
    evaluate_fn, seen = _evaluate_recorder(dialog)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate_fn))

    with patch.object(NotificationDispatcher, "notify_reply_sent") as mock_sent:
        res = agent._process_messages()

    assert res["auto_replies_count"] == 0, "nothing may be sent while the switch is off"
    assert not mock_sent.called, "no 'reply sent' notification may go out"

    send_scripts = [s for s in seen if "chat-input-textarea" in s]
    assert send_scripts == [], f"the send script must never be evaluated, saw: {send_scripts}"

    events = db.list_hh_message_events(conversation_id=CONV_ID)
    assert events, "the incoming message must still be recorded"
    assert events[0]["status"] == "BLOCKED_AUTONOMOUS_REPLY_DISABLED"

    audits = db.list_conversation_audits(conversation_id=CONV_ID)
    assert len(audits) == 1
    assert audits[0]["status"] == "BLOCKED_AUTONOMOUS_REPLY_DISABLED"
    assert audits[0]["sent_reply"] is None, "nothing was sent, so there is no sent reply"
    assert audits[0]["sent_at"] is None
    assert audits[0]["generated_reply"], "the draft must still be there for a human to use"


def test_send_is_skipped_when_switch_holds_a_non_true_value(monkeypatch):
    """Any value other than the exact string 'true' must fail closed."""
    monkeypatch.setenv(SWITCH, "1")

    dialog = _recruiter_question_dialog()
    evaluate_fn, seen = _evaluate_recorder(dialog)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate_fn))

    res = agent._process_messages()

    assert res["auto_replies_count"] == 0
    assert [s for s in seen if "chat-input-textarea" in s] == []


def test_send_still_works_when_explicitly_armed(monkeypatch):
    """With the switch armed the mechanism is unchanged — this is what Stage 51-60 pin."""
    monkeypatch.setenv(SWITCH, "true")

    dialog = _recruiter_question_dialog()
    evaluate_fn, seen = _evaluate_recorder(dialog)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate_fn))

    with patch.object(NotificationDispatcher, "notify_reply_sent", wraps=NotificationDispatcher.notify_reply_sent):
        res = agent._process_messages()

    assert res["auto_replies_count"] == 1
    assert [s for s in seen if "chat-input-textarea" in s], "the send script must run when armed"

    audits = db.list_conversation_audits(conversation_id=CONV_ID)
    assert len(audits) == 1
    assert audits[0]["status"] == "SENT"


def test_switch_is_separate_from_the_guarded_pipeline_switch(monkeypatch):
    """Arming the guarded path must not arm this ungated send.

    `HH_AUTO_REPLY_ENABLED` is the kill switch of the guarded
    hh_message_reply.process_auto_reply() pipeline. This send must ignore it.
    """
    monkeypatch.setenv("HH_AUTO_REPLY_ENABLED", "true")
    monkeypatch.delenv(SWITCH, raising=False)

    dialog = _recruiter_question_dialog()
    evaluate_fn, seen = _evaluate_recorder(dialog)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate_fn))

    res = agent._process_messages()

    assert res["auto_replies_count"] == 0
    assert [s for s in seen if "chat-input-textarea" in s] == []


def test_anti_loop_duplicate_reply_suppression(monkeypatch):
    """Anti-loop guard: if identical reply was already sent recently, suppress sending."""
    db.init_db()
    monkeypatch.setenv(SWITCH, "true")
    conv_id = "conv_anti_loop_1"

    # Pre-record an identical sent auto-reply in DB
    db.save_hh_message_event(
        message_fingerprint="fp_prior_1",
        conversation_id=conv_id,
        sender="candidate",
        text="Да, готов. График работы мне полностью подходит.",
        sent_at="2026-10-16T07:30:00Z",
        seen_at="2026-10-16T07:30:00Z",
        processed=1,
        classification="RECRUITER_QUESTION",
        reply_draft="Да, готов. График работы мне полностью подходит.",
        status="MESSAGE_AUTO_REPLIED",
    )

    dialog = HHDialog(
        conversation_id=conv_id,
        vacancy_title="Web Developer",
        vacancy_stable_id="hh:777666",
        employer="Legacy School",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="07:29"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Готовы ли вы работать с понедельника по пятницу, 8 часов в день по времени ОАЭ? 07:34",
                sent_at="07:34",
            ),
        ],
    )

    evaluate_fn, seen = _evaluate_recorder(dialog)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate_fn))

    res = agent._process_messages()

    assert res["auto_replies_count"] == 0, "duplicate reply must be blocked by anti-loop guard"
    assert [s for s in seen if "chat-input-textarea" in s] == [], "no text should be typed into chat"

    events = db.list_hh_message_events(conversation_id=conv_id)
    latest_event = events[0]
    assert latest_event["status"] == "LOOP_PREVENTED"
    assert "identical reply text was already sent" in (latest_event.get("error") or "")


def test_anti_loop_consecutive_ceiling_escalates(monkeypatch):
    """Anti-loop ceiling: 2 consecutive auto-replies must escalate to human review."""
    db.init_db()
    monkeypatch.setenv(SWITCH, "true")
    conv_id = "conv_anti_loop_2"

    # Insert 15 prior auto-replies
    for i in range(1, 16):
        db.save_hh_message_event(
            message_fingerprint=f"fp_streak_{i}",
            conversation_id=conv_id,
            sender="candidate",
            text=f"Auto reply {i}",
            sent_at=f"2026-10-16T07:{i:02d}:00Z",
            seen_at=f"2026-10-16T07:{i:02d}:00Z",
            processed=1,
            classification="RECRUITER_QUESTION",
            reply_draft=f"Different response text {i}",
            status="MESSAGE_AUTO_REPLIED",
        )

    dialog = HHDialog(
        conversation_id=conv_id,
        vacancy_title="Python Lead",
        vacancy_stable_id="hh:555444",
        employer="TechCorp",
        messages=[
            HHMessage(message_id="m1", sender="candidate", text="Отклик", sent_at="07:00"),
            HHMessage(
                message_id="m2",
                sender="employer",
                text="Расскажите еще подробнее о вашем стеке и проектах",
                sent_at="07:35",
            ),
        ],
    )

    evaluate_fn, seen = _evaluate_recorder(dialog)
    agent = AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate_fn))

    with patch.object(NotificationDispatcher, "notify_blocking_question") as mock_notify:
        res = agent._process_messages()

    assert res["auto_replies_count"] == 0, "must not send 3rd consecutive auto-reply"
    assert [s for s in seen if "chat-input-textarea" in s] == []
    assert mock_notify.called, "must dispatch human review escalation notification"

    events = db.list_hh_message_events(conversation_id=conv_id)
    latest_event = events[0]
    assert latest_event["status"] == "HUMAN_REVIEW_REQUIRED"


def test_schedule_confirmation_intent_and_greeting_stripping():
    from ai_assistant.smart_experience_generator import generate_employer_chat_reply

    # 1. Schedule question without prior candidate interaction (includes greeting)
    q = "Готовы ли вы работать с понедельника по пятницу, 8 часов в день, с обязательным началом работы в 8:00 по времени ОАЭ?"
    res1 = generate_employer_chat_reply(q, has_prior_interaction=False)
    expected_stripped = "Да, готов. Полностью готов работать по московскому времени (09:00–18:00 или 10:00–19:00 МСК) — разница в 4 часа мне абсолютно комфортна и проверена годами удаленной работы."
    assert res1["intent"] == "SCHEDULE_CONFIRMATION"
    assert expected_stripped in res1["reply_text"]
    assert res1["reply_text"].startswith("Здравствуйте!")

    # 2. Schedule question with prior candidate interaction (greeting stripped)
    res2 = generate_employer_chat_reply(q, has_prior_interaction=True)
    assert res2["intent"] == "SCHEDULE_CONFIRMATION"
    assert res2["reply_text"] == expected_stripped
    assert not res2["reply_text"].startswith("Здравствуйте")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


