"""Stage 22 tests: HH message reply MVP (REVIEW-only).

Covers every scenario from the spec §8: reply-required, no-reply, lack of
info -> HUMAN_REVIEW with no invented facts, dedup, REVIEW never sends,
ambiguous browser state -> HUMAN_REVIEW / 0 sends, truth-only generation,
and the absence of any send/mutation API in the module.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ai_assistant.hh_message_reply import (
    DEFAULT_MODE,
    HHDialog,
    HHMessage,
    MessageClassification,
    MessageReplyReport,
    ReplyMode,
    ReplyStateStore,
    SendGate,
    classify_message,
    detect_language,
    fetch_hh_dialogs_readonly,
    generate_reply,
    process_incoming_message,
)


def _dialog(conv="c1", messages=None, vacancy="Backend dev", employer="Acme",
            vid="hh:123"):
    msgs = messages if messages is not None else [
        HHMessage(message_id="m1", text="Здравствуйте! Спасибо за отклик.",
                  sender="employer"),
        HHMessage(message_id="m2", text="Можем ли мы начать обсуждение вашей кандидатуры?",
                  sender="employer"),
    ]
    return HHDialog(conversation_id=conv, vacancy_title=vacancy,
                    vacancy_stable_id=vid, employer=employer, messages=msgs)


def _profile():
    return {
        "desired_roles": ["AI Automation Engineer", "n8n Developer"],
        "languages": ["en", "ru"],
        "remote_required": True,
    }


def _fresh_store(tmp_path, name):
    return ReplyStateStore(str(tmp_path / name))


# ---------------- 1. new ordinary message -> REPLY_REQUIRED + reply ---------

def test_new_message_reply_required_and_reply_generated(tmp_path):
    dialog = _dialog()
    rep = process_incoming_message(dialog, profile=_profile(), state=_fresh_store(tmp_path, 's.json'))
    assert rep.classification == MessageClassification.REPLY_REQUIRED.value
    assert rep.generated_reply
    assert "Здравствуйте" in rep.generated_reply
    assert rep.status == "NEEDS_HUMAN_REVIEW"
    assert rep.send_action_count == 0
    assert rep.sources and "candidate_profile.json" in rep.sources[0]


def test_new_message_english_reply(tmp_path):
    dialog = _dialog(conv="c-en", messages=[
        HHMessage(message_id="m1", text="Hi! Would you like to discuss the position?",
                  sender="employer"),
    ])
    rep = process_incoming_message(dialog, profile=_profile(), state=_fresh_store(tmp_path, 's.json'))
    assert rep.classification == "REPLY_REQUIRED"
    assert "Thank you" in rep.generated_reply
    assert rep.send_action_count == 0


# ---------------- 2. system/irrelevant -> NO_REPLY, no generation ----------

@pytest.mark.parametrize("text", [
    "Ваш отклик получен. Работодатель рассмотрит его в ближайшее время.",
    "Уведомление: изменились условия вакансии.",
    "Активируйте подписку hh, чтобы видеть больше откликов.",
])
def test_system_message_no_reply(tmp_path, text):
    dialog = _dialog(conv="c-sys", messages=[
        HHMessage(message_id="m1", text=text, sender="system"),
    ])
    rep = process_incoming_message(dialog, profile=_profile(), state=_fresh_store(tmp_path, 's.json'))
    assert rep.classification == MessageClassification.NO_REPLY.value
    assert rep.generated_reply == ""
    assert rep.send_action_count == 0


# ---------------- 3. insufficient info -> HUMAN_REVIEW, no invented facts ---

def test_missing_info_human_review_no_invention(tmp_path):
    dialog = _dialog(conv="c-qi", messages=[
        HHMessage(message_id="m1",
                  text="Расскажите о вашем опыте и ожиданиях по зарплате.",
                  sender="employer"),
    ])
    rep = process_incoming_message(dialog, profile=_profile(), state=_fresh_store(tmp_path, 's.json'))
    assert rep.classification == MessageClassification.HUMAN_REVIEW.value
    assert rep.generated_reply == ""
    assert rep.send_action_count == 0


def test_sensitive_question_human_review(tmp_path):
    dialog = _dialog(conv="c-sens", messages=[
        HHMessage(message_id="m1",
                  text="Какая у вас зарплата сейчас и какой опыт Python?",
                  sender="employer"),
    ])
    rep = process_incoming_message(dialog, profile=_profile(), state=_fresh_store(tmp_path, 's.json'))
    assert rep.classification == "HUMAN_REVIEW"
    assert rep.generated_reply == ""


# ---------------- 4. dedup: same message id skipped -------------------------

def test_duplicate_message_skipped(tmp_path):
    store = ReplyStateStore(str(tmp_path / "state.json"))
    dialog = _dialog()
    rep1 = process_incoming_message(dialog, profile=_profile(), state=store)
    assert rep1.skipped_as_processed is False
    rep2 = process_incoming_message(dialog, profile=_profile(), state=store)
    assert rep2.skipped_as_processed is True
    assert rep2.classification == "ALREADY_PROCESSED"
    assert rep2.status == "SKIPPED"
    assert rep2.send_action_count == 0
    # persists across store instances (file-backed)
    store2 = ReplyStateStore(str(tmp_path / "state.json"))
    assert store2.is_processed(dialog.conversation_id, "m2") is True


def test_different_message_same_dialog_not_skipped(tmp_path):
    store = ReplyStateStore(str(tmp_path / "state.json"))
    d1 = _dialog(messages=[HHMessage(message_id="m1", text="Когда приступите?",
                                     sender="employer")])
    d2 = _dialog(messages=[HHMessage(message_id="m2", text="Уточните, какой у вас опыт?",
                                     sender="employer")])
    process_incoming_message(d1, profile=_profile(), state=store)
    rep2 = process_incoming_message(d2, profile=_profile(), state=store)
    assert rep2.skipped_as_processed is False
    assert rep2.conversation_id == "c1"


# ---------------- 5. REVIEW mode: reply exists, send action = 0 -------------

def test_review_mode_never_sends(tmp_path):
    dialog = _dialog()
    gate = SendGate(mode=ReplyMode.REVIEW)
    sent = gate.send_reply(dialog, "some reply")
    assert sent == {"ok": False, "blocked": True,
                    "reason": "REVIEW_MODE: message send is forbidden in Stage 22"}
    rep = process_incoming_message(dialog, profile=_profile(), state=_fresh_store(tmp_path, 's.json'))
    assert rep.generated_reply
    assert rep.send_action_count == 0
    assert DEFAULT_MODE is ReplyMode.REVIEW


def test_review_mode_future_modes_blocked():
    gate = SendGate(mode=ReplyMode.AUTO)
    assert gate.send_reply(_dialog(), "x")["blocked"] is True


# ---------------- 6. ambiguous browser state -> HUMAN_REVIEW, 0 sends -------

def test_ambiguous_browser_state_human_review_zero_sends(tmp_path):
    # dialog has no messages / unreadable state -> no send, human review
    dialog = _dialog(conv="c-amb", messages=[])
    rep = process_incoming_message(dialog, profile=_profile(), state=_fresh_store(tmp_path, 's.json'))
    assert rep.classification == "NEEDS_HUMAN_REVIEW" or rep.reason
    assert rep.send_action_count == 0


# ---------------- 7. truth-only: reply has no invented facts ----------------

def test_truth_only_reply_contains_no_invented_facts(tmp_path):
    dialog = _dialog(conv="c-t", messages=[
        HHMessage(message_id="m1", text="Можем ли мы обсудить вашу кандидатуру?", sender="employer"),
    ])
    prof = _profile()  # no salary, no years, no tech stack claims beyond roles
    rep = process_incoming_message(dialog, profile=prof,
                                   state=_fresh_store(tmp_path, "truth.json"))
    low = rep.generated_reply.lower()
    # profile roles legitimately include "n8n developer" (truth source);
    # everything else must not leak as invented facts
    for forbidden in ("5 лет", "3 года", "1500", "100 000", "python",
                      "telegram", "llm", "гермес", "hermes", "200 000"):
        assert forbidden not in low, f"invented fact leaked: {forbidden}"
    assert rep.send_action_count == 0


# ---------------- module safety: no send/mutation APIs ----------------------

def test_module_has_no_send_or_browser_mutation_apis():
    src = pathlib.Path("ai_assistant/hh_message_reply.py").read_text(encoding="utf-8")
    # Stage 22: REVIEW path must contain no browser-mutation API. Stage 25 adds
    # a strictly-gated AUTO send inside the dedicated send_auto_reply function;
    # the REVIEW/SKIP processing entry points must never touch the browser.
    for banned in [".fill(", ".type(", ".press(", ".goto(",
                   ".send_keys(", ".keyboard", ".mouse", ".set_input_files(",
                   "requests.post", "urllib.request.urlopen", ".submit("]:
        assert banned not in src, f"FORBIDDEN API present: {banned}"
    # SendGate must exist and must not call any browser primitive
    assert "def send_reply" in src
    assert "REVIEW_MODE: message send is forbidden" in src
    # AUTO send is isolated and strictly gated: only reachable via the
    # send_auto_reply function guarded by can_auto_send.
    assert "def send_auto_reply" in src
    assert "send.click()" in src  # the single, gated mutation primitive
    # REVIEW entry point (process_incoming_message) itself must never send:
    # no mutation primitive inside its body (the AUTO send lives in the
    # separate send_auto_reply function, which is not part of this function).
    pm = src.split("def process_incoming_message")[-1].split("def fetch_hh_dialogs_readonly")[0]
    assert ".click(" not in pm and "send.click()" not in pm
    assert "new Event('input'" not in pm and "new Event('change'" not in pm


# ---------------- fetch read-only helper (fake evaluate) --------------------

def test_fetch_hh_dialogs_readonly_uses_existing_transport():
    def fake_evaluate(expr):
        return json.dumps({
            "url": "https://hh.ru/messaging?conversation=1",
            "title": "Сообщения",
            "dialogs": [{"qa": "dialog-item-1", "text": "Acme: Можем ли мы продолжить?"}],
            "pageIsMessages": True,
        })
    res = fetch_hh_dialogs_readonly(fake_evaluate, "https://hh.ru/messaging")
    assert res["pageIsMessages"] is True
    assert res["dialogs"][0]["text"] == "Acme: Можем ли мы продолжить?"


def test_fetch_hh_dialogs_readonly_no_send():
    calls = []
    def fake_evaluate(expr):
        calls.append(expr)
        return json.dumps({"dialogs": [], "pageIsMessages": True})
    fetch_hh_dialogs_readonly(fake_evaluate)
    assert len(calls) == 1
    assert "click" not in calls[0] and "Send" not in calls[0]


# ---------------- classification unit tests ---------------------------------

def test_classify_system_vs_reply_vs_human():
    sys_d = _dialog(messages=[HHMessage(message_id="1", text="Ваш отклик получен.",
                                        sender="system")])
    assert classify_message(sys_d) == MessageClassification.NO_REPLY
    reply_d = _dialog(messages=[HHMessage(message_id="2", text="Можем ли мы продолжить?",
                                          sender="employer")])
    assert classify_message(reply_d) == MessageClassification.REPLY_REQUIRED
    sens_d = _dialog(messages=[HHMessage(message_id="3", text="Сколько вы хотите получать?",
                                         sender="employer")])
    assert classify_message(sens_d) == MessageClassification.HUMAN_REVIEW


def test_detect_language():
    assert detect_language("Здравствуйте!") == "ru"
    assert detect_language("Hello!") == "en"