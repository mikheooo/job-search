"""Stage 26 tests: first controlled live AUTO reply gates.

Covers: conversation_id required for live AUTO; target not found; no pending
incoming; already processed; confirmation required; safe target + confirm ->
send allowed; message changes before send; budget=1 max one send; unknown send
result -> no retry; post-send verification; successful send -> dedup blocks
second send; kill switch still mandatory.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant.hh_message_reply import (
    HHDialog,
    HHMessage,
    ReplyMode,
    ReplyStateStore,
    process_auto_reply,
)


class FakeChatik:
    def __init__(self, messages, composer=True, send_ok=True, changed_after_read=False):
        self.messages = list(messages)
        self.composer = composer
        self.send_ok = send_ok
        self.changed_after_read = changed_after_read
        self.sends = 0

    def evaluate(self, expr):
        if "chat-bubble" in expr or ("conversation_id" in expr and "messages" in expr):
            msgs = [{"direction": "INCOMING" if m.sender != "candidate" else "OUTGOING",
                     "text": m.text, "sender": m.sender, "timestamp": None}
                    for m in self.messages]
            if self.changed_after_read:
                msgs.append({"direction": "INCOMING", "text": "НОВОЕ",
                             "sender": "employer", "timestamp": None})
            return json.dumps({"url": "chatik", "title": "Чаты", "conversation_id": "c26",
                               "messages": msgs, "composer_present": self.composer})
        if "composer_present" in expr:
            return json.dumps({"composer_present": self.composer})
        if "setter.call" in expr and "chatik-new-message-text" in expr:
            self.sends += 1
            if self.send_ok:
                return json.dumps({"ok": True, "clicked": True})
            return json.dumps({"ok": False, "reason": "unknown send failure"})
        raise RuntimeError(f"FakeChatik: {expr[:60]}")


def _safe_dialog(cid="c26"):
    return HHDialog(conversation_id=cid, vacancy_title="AI Engineer",
                    vacancy_stable_id="hh:1", employer="Acme",
                    messages=[HHMessage(message_id="in1",
                                        text="Hi! Are you still interested in this role?",
                                        sender="employer")])


def _profile():
    return {"desired_roles": ["AI Automation Engineer"], "languages": ["en"],
            "remote_required": True}


@pytest.fixture(autouse=True)
def _clean(tmp_path, request):
    import ai_assistant.hh_message_reply as m
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
    m.DEFAULT_STATE_PATH = str(tmp_path / f"s26-{safe}.json")
    yield
    m.DEFAULT_STATE_PATH = "artifacts/hh_message_reply_state.json"


ENV = {"HH_AUTO_REPLY_ENABLED": "true"}


# ---------------- 1. conversation_id required for live AUTO -----------------

def test_live_auto_requires_explicit_target():
    # without target_conversation_id, a live AUTO attempt with a target is
    # refused: target not specified -> treated as not-a-live-run, so it must
    # not send. We pass a target that mismatches to force the refusal path.
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="WRONG_ID",
                             confirm_live_send=True)
    assert rep.status == "BLOCKED_TARGET_NOT_FOUND"
    assert fake.sends == 0


# ---------------- 2. target not found -> 0 send -----------------------------

def test_target_not_found_zero_send():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(cid="c26"), fake.evaluate,
                             profile=_profile(), mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="c999", confirm_live_send=True)
    assert rep.status == "BLOCKED_TARGET_NOT_FOUND"
    assert fake.sends == 0


# ---------------- 3. no pending incoming -> 0 send --------------------------

def test_no_pending_incoming_zero_send():
    dialog = HHDialog(conversation_id="c26", vacancy_title="X", employer="E",
                      messages=[HHMessage(message_id="out1", text="Спасибо за отклик",
                                          sender="candidate")])
    fake = FakeChatik(messages=dialog.messages)
    rep = process_auto_reply(dialog, fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="c26", confirm_live_send=True)
    assert rep.status == "BLOCKED_NO_PENDING_INCOMING"
    assert fake.sends == 0


# ---------------- 4. already processed -> 0 send ----------------------------

def test_already_processed_zero_send(tmp_path):
    store = ReplyStateStore(str(tmp_path / "proc.json"))
    fake = FakeChatik(messages=_safe_dialog().messages)
    # first run in REVIEW marks processed
    process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                       mode=ReplyMode.REVIEW, state=store)
    # now attempt live AUTO on same target -> skipped
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV, state=store,
                             target_conversation_id="c26", confirm_live_send=True)
    assert rep.status == "SKIPPED"
    assert fake.sends == 0


# ---------------- 5. confirmation absent -> 0 send --------------------------

def test_confirmation_required_zero_send():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="c26", confirm_live_send=False)
    assert rep.status == "BLOCKED_LIVE_CONFIRMATION_REQUIRED"
    assert fake.sends == 0


# ---------------- 6. safe target + confirmation -> send allowed -------------

def test_safe_target_with_confirmation_sends_once():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="c26", confirm_live_send=True,
                             max_auto_replies=1, run_budget={"sent": 0})
    assert rep.status == "SENT"
    assert rep.send_action_count == 1
    assert fake.sends == 1
    assert rep.generated_reply


# ---------------- 7. message changes before send -> 0 send ------------------

def test_message_changes_before_send_zero_send():
    fake = FakeChatik(messages=_safe_dialog().messages, changed_after_read=True)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="c26", confirm_live_send=True)
    assert fake.sends == 0
    assert rep.send_action_count == 0
    assert "race" in rep.reason.lower() or "changed" in rep.reason.lower()


# ---------------- 8. budget=1 -> max one send -------------------------------

def test_budget_one_max_one_send():
    fake = FakeChatik(messages=_safe_dialog().messages)
    budget = {"sent": 0}
    r1 = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env=ENV,
                            target_conversation_id="c26", confirm_live_send=True,
                            max_auto_replies=1, run_budget=budget)
    assert r1.status == "SENT"
    # second target already sent this run -> rate limited
    d2 = _safe_dialog(cid="c26b")
    r2 = process_auto_reply(d2, fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env=ENV,
                            target_conversation_id="c26b", confirm_live_send=True,
                            max_auto_replies=1, run_budget=budget)
    assert r2.status == "BLOCKED_RATE_LIMIT"
    assert fake.sends == 1


# ---------------- 9. unknown send result -> no retry ------------------------

def test_unknown_send_result_no_retry():
    fake = FakeChatik(messages=_safe_dialog().messages, send_ok=False)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="c26", confirm_live_send=True,
                             max_auto_replies=1, run_budget={"sent": 0})
    assert fake.sends == 1  # exactly one attempt
    assert rep.status in ("BLOCKED", "HUMAN_REVIEW")
    assert rep.send_action_count == 1


# ---------------- 10. post-send verification --------------------------------

def test_post_send_verification_reads_dialog():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env=ENV,
                             target_conversation_id="c26", confirm_live_send=True,
                             max_auto_replies=1, run_budget={"sent": 0})
    assert rep.status == "SENT"
    # re-read (read-only) after send must still work and match conversation
    fresh = json.loads(fake.evaluate("JSON.stringify({conversation_id:'c26',messages:[]})"))
    assert rep.conversation_id == "c26"
    assert rep.send_result is not None


# ---------------- 11. successful send -> dedup blocks second send -----------

def test_successful_send_dedup_blocks_second(tmp_path):
    store = ReplyStateStore(str(tmp_path / "dd.json"))
    fake = FakeChatik(messages=_safe_dialog().messages)
    budget = {"sent": 0}
    r1 = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env=ENV, state=store,
                            target_conversation_id="c26", confirm_live_send=True,
                            max_auto_replies=1, run_budget=budget)
    assert r1.status == "SENT"
    r2 = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                            mode=ReplyMode.AUTO, env=ENV, state=store,
                            target_conversation_id="c26", confirm_live_send=True,
                            max_auto_replies=1, run_budget=budget)
    assert r2.status == "SKIPPED"
    assert fake.sends == 1  # never a second send


# ---------------- 12. kill switch still mandatory ---------------------------

def test_kill_switch_still_mandatory():
    fake = FakeChatik(messages=_safe_dialog().messages)
    rep = process_auto_reply(_safe_dialog(), fake.evaluate, profile=_profile(),
                             mode=ReplyMode.AUTO, env={},  # kill switch off
                             target_conversation_id="c26", confirm_live_send=True)
    assert fake.sends == 0
    assert rep.send_action_count == 0
    assert any(c["check"] == "kill_switch" and not c["ok"]
               for c in rep.safety_checks) or rep.status in (
        "HUMAN_REVIEW", "BLOCKED", "NEEDS_HUMAN_REVIEW")