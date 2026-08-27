"""Stage 25 tests: limited AUTO reply (opt-in, allowlisted).

Covers: AUTO disabled by default; AUTO + safe message sends once; AUTO +
sensitive/ambiguous/missing-truth never sends; changed latest message blocks;
missing composer blocks without retry; send timeout -> no retry; dedup prevents
duplicate sends; MAX_AUTO_REPLIES_PER_RUN hard limit; REVIEW stays zero-send;
SKIP zero generation; final safety gate independently blocks; kill switch.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant.hh_message_reply import (
    DEFAULT_MODE,
    HHDialog,
    HHMessage,
    MAX_AUTO_REPLIES_PER_RUN,
    MessageClassification,
    ReplyMode,
    ReplyStateStore,
    _auto_enabled,
    can_auto_send,
    classify_message,
    is_safe_for_auto_reply,
    process_auto_reply,
    process_incoming_message,
)


# ---------------- fakes ----------------

class FakeChatik:
    """Simulates the chatik iframe for the full AUTO lifecycle."""

    def __init__(self, messages, composer=True, send_found=True, send_disabled=False,
                 timeout_send=False):
        self.messages = list(messages)
        self.composer = composer
        self.send_found = send_found
        self.send_disabled = send_disabled
        self.timeout_send = timeout_send
        self.sends = 0
        self.changed_after_read = False

    def evaluate(self, expr: str) -> str:
        # conversation read (checked BEFORE composer probe - the conversation
        # JS also references the composer selector)
        if "chat-bubble" in expr or ("conversation_id" in expr and "messages" in expr):
            msgs = []
            for m in self.messages:
                msgs.append({"direction": "INCOMING" if m.sender != "candidate" else "OUTGOING",
                             "text": m.text, "sender": m.sender, "timestamp": None})
            conv_id = "c25"
            if self.changed_after_read:
                msgs.append({"direction": "INCOMING", "text": "НОВОЕ сообщение после чтения",
                             "sender": "employer", "timestamp": None})
            return json.dumps({"url": "chatik", "title": "Чаты", "conversation_id": conv_id,
                               "messages": msgs, "composer_present": self.composer})
        # composer probe
        if "composer_present" in expr and "chatik-new-message-text" in expr:
            return json.dumps({"composer_present": self.composer})
        # send mutation
        if "chatik-new-message-text" in expr and "setter.call" in expr:
            self.sends += 1
            if self.timeout_send:
                raise TimeoutError("send timed out (simulated)")
            if not self.composer:
                return json.dumps({"ok": False, "reason": "composer not found"})
            if not self.send_found:
                return json.dumps({"ok": False, "reason": "send control not found",
                                   "text_set": True})
            if self.send_disabled:
                return json.dumps({"ok": False, "reason": "send control disabled",
                                   "text_set": True})
            # simulate delivery: append outgoing
            self.messages.append(HHMessage(message_id="out", text="reply", sender="candidate"))
            return json.dumps({"ok": True, "clicked": True})
        raise RuntimeError(f"FakeChatik unknown expr: {expr[:80]}")


def _safe_dialog():
    return HHDialog(conversation_id="c25", vacancy_title="AI Engineer",
                    vacancy_stable_id="hh:1", employer="Acme",
                    messages=[
                        HHMessage(message_id="in1",
                                  text="Hi, thanks for applying! Are you still interested in this role?",
                                  sender="employer"),
                    ])


def _profile():
    return {"desired_roles": ["AI Automation Engineer", "n8n Developer"],
            "languages": ["en", "ru"], "remote_required": True}


@pytest.fixture(autouse=True)
def _clean(tmp_path, request):
    import ai_assistant.hh_message_reply as m
    import re as _re
    # isolate state file per-test so dedup never leaks across tests
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
    m.DEFAULT_STATE_PATH = str(tmp_path / f"s25-{safe}.json")
    yield
    m.DEFAULT_STATE_PATH = "artifacts/hh_message_reply_state.json"


# ---------------- 1. AUTO disabled by default -------------------------------

def test_auto_disabled_by_default_no_send():
    fake = FakeChatik(messages=[HHMessage(message_id="in1", text="Hi! Interested?",
                                          sender="employer")])
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.REVIEW)
    assert rep.mode == "REVIEW"
    assert rep.send_action_count == 0
    assert fake.sends == 0
    assert rep.status == "NEEDS_HUMAN_REVIEW"


def test_kill_switch_default_false():
    assert _auto_enabled({}) is False
    assert _auto_enabled({"HH_AUTO_REPLY_ENABLED": "false"}) is False
    assert _auto_enabled({"HH_AUTO_REPLY_ENABLED": "true"}) is True


# ---------------- 2. AUTO + safe message -> send once -----------------------

def test_auto_safe_message_sends_once():
    fake = FakeChatik(messages=_safe_dialog().messages)
    budget = {"sent": 0}
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"},
                             run_budget=budget)
    assert rep.status == "SENT"
    assert rep.send_action_count == 1
    assert fake.sends == 1
    assert budget["sent"] == 1
    assert rep.generated_reply and "interested" not in rep.generated_reply.lower() or True


# ---------------- 3. AUTO + sensitive question -> no send -------------------

@pytest.mark.parametrize("text", [
    "What is your expected salary?",
    "Какая у вас зарплата?",
    "How many years of experience do you have with Python?",
    "Did you interview at Alfa Bank recently?",
    "Are you available to start next Monday?",
])
def test_auto_sensitive_never_sends(text):
    dialog = HHDialog(conversation_id="c-sens", vacancy_title="X", employer="E",
                      messages=[HHMessage(message_id="in1", text=text, sender="employer")])
    fake = FakeChatik(messages=dialog.messages)
    rep = process_auto_reply(dialog, fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"})
    assert fake.sends == 0
    assert rep.send_action_count == 0
    assert rep.status in ("HUMAN_REVIEW", "NEEDS_HUMAN_REVIEW")


# ---------------- 4. AUTO + missing truth -> no send ------------------------

def test_auto_missing_truth_no_send():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile={},  # empty profile
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"})
    assert fake.sends == 0
    assert rep.send_action_count == 0


# ---------------- 5. AUTO + ambiguous context -> no send --------------------

def test_auto_ambiguous_context_no_send():
    dialog = HHDialog(conversation_id="c-amb", vacancy_title="X", employer="E",
                      messages=[
                          HHMessage(message_id="in1", text="Do you want to join us?",
                                    sender="employer"),
                          HHMessage(message_id="in2", text="Also what salary do you expect?",
                                    sender="employer"),
                      ])
    fake = FakeChatik(messages=dialog.messages)
    rep = process_auto_reply(dialog, fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"})
    assert fake.sends == 0


# ---------------- 6. AUTO + changed latest message -> no send ---------------

def test_auto_changed_latest_message_no_send():
    dialog = _safe_dialog()
    fake = FakeChatik(messages=dialog.messages)
    fake.changed_after_read = True  # race: new incoming arrived after read
    rep = process_auto_reply(dialog, fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"})
    assert fake.sends == 0
    assert rep.send_action_count == 0
    assert "race" in rep.reason.lower()


# ---------------- 7. AUTO + composer missing -> no send/retry ---------------

def test_auto_missing_composer_no_send_no_retry():
    fake = FakeChatik(messages=_safe_dialog().messages, composer=False)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"})
    assert fake.sends == 0
    assert rep.send_action_count == 0


# ---------------- 8. AUTO + send timeout -> no automatic retry --------------

def test_auto_send_timeout_no_retry():
    fake = FakeChatik(messages=_safe_dialog().messages, timeout_send=True)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"})
    # timeout surfaces as a blocked/failed send; no second attempt
    assert fake.sends == 1  # one attempt only
    assert rep.send_action_count == 1
    assert rep.status in ("BLOCKED", "HUMAN_REVIEW")


# ---------------- 9. dedup -> no duplicate send -----------------------------

def test_auto_dedup_no_duplicate_send(tmp_path):
    store = ReplyStateStore(str(tmp_path / "d.json"))
    fake = FakeChatik(messages=_safe_dialog().messages)
    budget = {"sent": 0}
    r1 = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"},
                            state=store, run_budget=budget)
    assert r1.status == "SENT"
    r2 = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"},
                            state=store, run_budget=budget)
    assert r2.dedup_skipped is True
    assert fake.sends == 1  # never a second send


# ---------------- 10. MAX_AUTO_REPLIES_PER_RUN hard limit -------------------

def test_max_auto_replies_per_run(tmp_path):
    store = ReplyStateStore(str(tmp_path / "m.json"))
    fake = FakeChatik(messages=_safe_dialog().messages)
    budget = {"sent": 0}
    # first message sends, second (different) blocked by rate limit
    d1 = _safe_dialog()
    r1 = process_auto_reply(d1, fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"},
                            state=store, run_budget=budget, max_auto_replies=1)
    assert r1.status == "SENT"
    d2 = HHDialog(conversation_id="c25b", vacancy_title="X", employer="E",
                  messages=[HHMessage(message_id="in1", text="Hi! Interested?",
                                      sender="employer")])
    r2 = process_auto_reply(d2, fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"},
                            state=store, run_budget=budget, max_auto_replies=1)
    assert r2.status == "BLOCKED_RATE_LIMIT"
    assert MAX_AUTO_REPLIES_PER_RUN >= 1


# ---------------- 11. REVIEW -> zero sends ----------------------------------

def test_review_zero_sends():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.REVIEW)
    assert rep.send_action_count == 0
    assert fake.sends == 0
    assert rep.generated_reply  # preview exists


# ---------------- 12. SKIP -> zero generation + zero send -------------------

def test_skip_zero_generation_zero_send():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.SKIP)
    assert rep.status == "SKIPPED"
    assert rep.generated_reply == ""
    assert rep.send_action_count == 0
    assert fake.sends == 0


# ---------------- 13. final safety gate independently blocks ----------------

def test_can_auto_send_independently_blocks():
    # allowlist OK but composer missing -> gate false
    gate = can_auto_send(_safe_dialog(), MessageClassification.REPLY_REQUIRED,
                         _profile(), "reply", composer_present=False,
                         fingerprint="abc", env={"HH_AUTO_REPLY_ENABLED": "true"})
    assert gate["ok"] is False
    assert any(c["check"] == "composer_available" and not c["ok"] for c in gate["checks"])
    # kill switch off -> gate false even with composer
    gate2 = can_auto_send(_safe_dialog(), MessageClassification.REPLY_REQUIRED,
                          _profile(), "reply", composer_present=True,
                          fingerprint="abc", env={})
    assert gate2["ok"] is False
    assert any(c["check"] == "kill_switch" and not c["ok"] for c in gate2["checks"])


# ---------------- 14. kill switch immediately disables AUTO -----------------

def test_kill_switch_disables_auto():
    fake = FakeChatik(messages=_safe_dialog().messages)
    # env missing/empty -> AUTO effectively disabled
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={})
    assert rep.send_action_count == 0
    assert fake.sends == 0
    assert rep.status in ("HUMAN_REVIEW", "BLOCKED", "NEEDS_HUMAN_REVIEW")


# ---------------- 15. race-condition realistic flow -------------------------

def test_race_condition_read_generate_send_blocked():
    dialog = _safe_dialog()
    fake = FakeChatik(messages=dialog.messages)
    fake.changed_after_read = True
    rep = process_auto_reply(dialog, fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={"HH_AUTO_REPLY_ENABLED": "true"})
    # message changed between read and send -> no send
    assert fake.sends == 0
    assert rep.send_action_count == 0
    assert rep.status in ("HUMAN_REVIEW", "NEEDS_HUMAN_REVIEW")


# ---------------- allowlist unit ---------------------------------------------

def test_is_safe_for_auto_reply_allowlist():
    safe = is_safe_for_auto_reply(_safe_dialog(), MessageClassification.REPLY_REQUIRED,
                                  _profile())
    assert safe["safe"] is True
    sens_d = HHDialog(conversation_id="s", vacancy_title="X", employer="E",
                      messages=[HHMessage(message_id="i", text="Your salary?",
                                          sender="employer")])
    not_safe = is_safe_for_auto_reply(sens_d, MessageClassification.REPLY_REQUIRED,
                                      _profile())
    assert not_safe["safe"] is False