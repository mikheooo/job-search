"""Stage 24 tests: HH full dialog read-only extraction + reply preview.

Covers: full-conversation extraction, INCOMING/OUTGOING/UNKNOWN direction,
missing message_id handling, classification with full context, truth-only
reply generation, insufficient-info -> HUMAN_REVIEW, read-only extractor,
SendGate blocked, dedup preserved.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ai_assistant.hh_message_reply import (
    HHDialog,
    HHMessage,
    MessageClassification,
    ReplyMode,
    ReplyStateStore,
    SendGate,
    classify_message,
    fetch_hh_conversation_readonly,
    generate_reply,
    process_incoming_message,
)


def _full_dialog(conv="c24", messages=None):
    msgs = messages if messages is not None else [
        HHMessage(message_id="m1", text="Отклик на вакансию", sender="candidate"),
        HHMessage(message_id="m2", text="Здравствуйте! Спасибо за отклик.",
                  sender="employer"),
        HHMessage(message_id="m3", text="Можем ли мы продолжить обсуждение вашей кандидатуры?",
                  sender="employer"),
    ]
    return HHDialog(conversation_id=conv, vacancy_title="AI Engineer",
                    vacancy_stable_id="hh:1", employer="Acme", messages=msgs)


def _profile():
    return {"desired_roles": ["AI Automation Engineer", "n8n Developer"],
            "languages": ["en", "ru"], "remote_required": True}


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    import ai_assistant.hh_message_reply as m
    m.DEFAULT_STATE_PATH = str(tmp_path / "s24.json")
    yield
    m.DEFAULT_STATE_PATH = "artifacts/hh_message_reply_state.json"


# ---------------- 1. full conversation extraction ---------------------------

def test_fetch_conversation_readonly_extracts_messages():
    fake = lambda expr: json.dumps({
        "url": "https://chatik.hh.ru/chat/123",
        "title": "Чаты",
        "conversation_id": "123",
        "messages": [
            {"direction": "INCOMING", "text": "Здравствуйте! Спасибо за отклик.",
             "sender": "Acme", "timestamp": "21:00"},
            {"direction": "OUTGOING", "text": "Здравствуйте!", "sender": None,
             "timestamp": "21:01"},
        ],
        "composer_present": True,
    })
    res = fetch_hh_conversation_readonly(fake)
    assert res["conversation_id"] == "123"
    assert len(res["messages"]) == 2
    assert res["messages"][0]["direction"] == "INCOMING"
    assert res["messages"][1]["direction"] == "OUTGOING"


def test_fetch_conversation_no_message_id_not_invented():
    fake = lambda expr: json.dumps({
        "conversation_id": "456",
        "messages": [{"direction": "INCOMING", "text": "Привет"}],
    })
    res = fetch_hh_conversation_readonly(fake)
    # HH provides no message_id in this DOM -> must not be fabricated
    assert all("message_id" not in m or m.get("message_id") is None
               for m in res["messages"])


def test_fetch_conversation_unknown_direction_preserved():
    fake = lambda expr: json.dumps({
        "conversation_id": "789",
        "messages": [{"direction": "UNKNOWN", "text": "???"}],
    })
    res = fetch_hh_conversation_readonly(fake)
    assert res["messages"][0]["direction"] == "UNKNOWN"


def test_fetch_conversation_readonly_no_send_api():
    src = pathlib.Path("ai_assistant/hh_message_reply.py").read_text(encoding="utf-8")
    assert "def fetch_hh_conversation_readonly" in src
    # read-only extractor itself must contain no mutation primitive
    extractor = src.split("def fetch_hh_conversation_readonly")[-1].split("def _composer_js")[0]
    assert "send.click()" not in extractor and ".click(" not in extractor
    # module-level send gate stays; AUTO send only inside the gated function
    assert "def send_auto_reply" in src


# ---------------- 2. classification uses full context -----------------------

def test_classification_full_context_sensitive_earlier_message():
    # last message looks safe, but earlier context asks about salary
    dialog = HHDialog(conversation_id="c-sens-ctx", vacancy_title="X",
                      employer="E", messages=[
        HHMessage(message_id="a", text="Какая у вас зарплата сейчас?",
                  sender="employer"),
        HHMessage(message_id="b", text="Подскажите, пожалуйста.", sender="employer"),
    ])
    assert classify_message(dialog) == MessageClassification.HUMAN_REVIEW


def test_classification_full_context_plain_reply():
    dialog = _full_dialog()
    # last message "Готовы ли вы к собеседованию?" -> probe hit -> REPLY_REQUIRED
    assert classify_message(dialog) == MessageClassification.REPLY_REQUIRED


def test_classification_earlier_question_not_latest_is_human():
    # employer asked a question earlier, then sent a system/neutral line
    dialog = HHDialog(conversation_id="c-q", vacancy_title="X", employer="E",
                      messages=[
                          HHMessage(message_id="a", text="Уточните, какой у вас опыт?",
                                    sender="employer"),
                          HHMessage(message_id="b", text="Ваш отклик получен.",
                                    sender="system"),
                      ])
    # latest is a system marker -> NO_REPLY wins (no need to answer a system line)
    assert classify_message(dialog) == MessageClassification.NO_REPLY


# ---------------- 3. truth-only reply generation ----------------------------

def test_reply_generation_uses_only_truth_sources():
    dialog = _full_dialog()
    prof = _profile()
    gen = generate_reply(dialog, prof)
    assert gen["status"] == "REPLY_REQUIRED"
    assert gen["sources"] and "candidate_profile.json" in gen["sources"][0]
    low = gen["reply"].lower()
    for forbidden in ("5 лет", "3 года", "1500", "100 000", "python", "llm",
                      "telegram", "гермес", "hermes"):
        assert forbidden not in low


def test_reply_generation_insufficient_info_human_review():
    # employer asks a specific fact not in profile -> must not fabricate
    dialog = HHDialog(conversation_id="c-q2", vacancy_title="X", employer="E",
                      messages=[HHMessage(message_id="a",
                                          text="Расскажите о вашем опыте работы с Java?",
                                          sender="employer")])
    gen = generate_reply(dialog, {})  # empty profile -> no facts
    assert gen["status"] == "HUMAN_REVIEW"
    assert gen["reply"] == ""


# ---------------- 4. SendGate stays blocked ---------------------------------

def test_send_gate_stays_blocked_full_context():
    gate = SendGate(mode=ReplyMode.REVIEW)
    dialog = _full_dialog()
    assert gate.send_reply(dialog, "привет")["blocked"] is True


# ---------------- 5. dedup preserved ----------------------------------------

def test_dedup_preserved_with_full_dialog(tmp_path):
    store = ReplyStateStore(str(tmp_path / "dedup.json"))
    d = _full_dialog()
    r1 = process_incoming_message(d, profile=_profile(), state=store)
    r2 = process_incoming_message(d, profile=_profile(), state=store)
    assert r1.skipped_as_processed is False
    assert r2.skipped_as_processed is True
    assert r2.status == "SKIPPED"
    assert r1.send_action_count == 0 and r2.send_action_count == 0


def test_process_full_dialog_review_reply_generated_no_send(tmp_path):
    store = ReplyStateStore(str(tmp_path / "proc.json"))
    rep = process_incoming_message(_full_dialog(), profile=_profile(), state=store)
    assert rep.classification == MessageClassification.REPLY_REQUIRED.value
    assert rep.generated_reply
    assert rep.send_action_count == 0
    assert rep.status == "NEEDS_HUMAN_REVIEW"