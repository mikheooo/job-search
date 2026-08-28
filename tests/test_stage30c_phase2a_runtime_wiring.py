"""Stage 30C Phase 2A: REVIEW-only runtime wiring for new safe helpers.

Wires REVIEW/READ-ONLY helpers that were NOT exposed in Phase 1:
  hh_message_reply.classify_message via hh-message classify,
  email_message_reply.classify_email via email classify,
  email_message_reply.link_email_to_vacancy via email link.

Gaps (intentionally NOT wired — no safe REVIEW-only entry):
  - Stage 21 dual-mode apply (requires HH submit + gates, AUTO path) — no read-only entry.
  - HH AUTO reply path (send/composer mutation + kill-switch) — live send, not REVIEW.
  - Email/HH send primitives and Gmail send/modify/delete — blocked, gmail.readonly only.
  - process_incoming_* dedup stores (artifacts file writes) — stateful; pure
    classify/generate/link helpers are wired instead (stateless, no DB).
  - Phase 1 known gap preserved: chatik iframe isolated-world helper needed for
    hh-message preview/classify (main-frame evaluate returns no messages).

Covers: (a) CLI parsing dispatch, (b) handler dispatch, (c) REVIEW-only with
fakes, (d) absence of AUTO, (e) absence of send/submit, (f) absence of DB
changes, (g) absence of env-flag changes. Mirrors Phase 1 mock/fake style.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from ai_assistant import cli
import ai_assistant.hh_message_reply as hh_message_reply
import ai_assistant.email_message_reply as email_message_reply
import ai_assistant.db as db_module


# ---------------------------------------------------------------- fakes -----

def _profile():
    return {"desired_roles": ["AI Automation Engineer"], "languages": ["en"], "remote_required": True}


def _fake_conversation_evaluate(expr):
    return json.dumps({
        "url": "https://hh.ru/applicant/negotiations?messageConversationId=123",
        "title": "chat",
        "conversation_id": "123",
        "messages": [
            {"direction": "INCOMING", "text": "Здравствуйте! Вы всё ещё заинтересованы?", "sender": "Рекрутер"},
        ],
        "composer_present": True,
    })


def _fake_transport():
    return [{
        "provider": "gmail", "message_id": "m1", "thread_id": "t1",
        "sender_name": "Recruiter", "sender_email": "r@job.com",
        "subject": "Interested?",
        "body_text": "Are you still interested?",
        "reply_to": None,
    }]


def _fake_transport_link():
    # sender_name present so link has company
    return [{
        "provider": "gmail", "message_id": "m2", "thread_id": "t2",
        "sender_name": "Acme Corp", "sender_email": "hr@acme.com",
        "subject": "Vacancy: AI Automation Engineer",
        "body_text": "Are you interested in Acme role?",
        "reply_to": None,
    }]


def _rising(*a, **k):
    raise AssertionError("forbidden AUTO/send function was called")


# ------------------------------------------- (a)+(b) parsing + dispatch -----

@pytest.mark.parametrize("argv,attr", [
    (["hh-message", "classify", "123"], "hh_message_classify"),
    (["email", "classify", "0"], "email_classify"),
    (["email", "link", "0"], "email_link"),
])
def test_phase2a_cli_dispatch_routes_new_readonly_commands(monkeypatch, argv, attr):
    calls = []

    def fake(*a, **k):
        calls.append((a, k))
        return 0

    monkeypatch.setattr(cli, attr, fake)
    monkeypatch.setattr(sys, "argv", ["job-search-cli"] + argv)
    rc = cli.main()
    assert rc == 0
    assert calls, f"{attr} was not reached by CLI dispatch"


# ------------------------------------------------ (c) REVIEW-only with fakes -

def test_hh_message_classify_readonly(monkeypatch, capsys):
    for name in ("process_auto_reply", "send_auto_reply", "can_auto_send", "is_safe_for_auto_reply"):
        monkeypatch.setattr(hh_message_reply, name, _rising)
    rc = cli.hh_message_classify("123", evaluate_fn=_fake_conversation_evaluate)
    out = capsys.readouterr().out
    assert rc == 0
    assert "classification" in out
    assert "PREVIEW ONLY" in out
    assert "nothing sent" in out


def test_hh_message_classify_gap_no_messages(capsys):
    def ev(expr):
        return json.dumps({"url": "http://x", "title": "", "conversation_id": None, "messages": [], "composer_present": False})
    rc = cli.hh_message_classify("123", evaluate_fn=ev)
    out = capsys.readouterr().out
    assert rc == 1
    assert "no messages" in out
    assert "PREVIEW ONLY" in out
    assert "nothing sent" in out


def test_email_classify_readonly(monkeypatch, capsys):
    monkeypatch.setattr(email_message_reply, "process_incoming_email", _rising)

    class _GateBoom:
        def __init__(self, *a, **k):
            raise AssertionError("EmailSendGate was instantiated by a CLI handler")
        def send_email(self, *a, **k):
            raise AssertionError("EmailSendGate.send_email was called")
    monkeypatch.setattr(email_message_reply, "EmailSendGate", _GateBoom)

    rc = cli.email_classify("0", transport=_fake_transport)
    out = capsys.readouterr().out
    assert rc == 0
    assert "classification" in out
    assert "READ-ONLY" in out
    assert "nothing sent" in out


def test_email_classify_out_of_range(capsys):
    rc = cli.email_classify("99", transport=_fake_transport)
    out = capsys.readouterr().out
    assert rc == 1
    assert "out of range" in out
    assert "nothing sent" in out


def test_email_link_readonly(monkeypatch, capsys):
    monkeypatch.setattr(email_message_reply, "process_incoming_email", _rising)

    class _GateBoom:
        def __init__(self, *a, **k):
            raise AssertionError("EmailSendGate was instantiated by a CLI handler")
    monkeypatch.setattr(email_message_reply, "EmailSendGate", _GateBoom)

    rc = cli.email_link("0", transport=_fake_transport_link)
    out = capsys.readouterr().out
    assert rc == 0
    assert "linked_company" in out
    assert "READ-ONLY" in out
    assert "nothing sent" in out


def test_email_link_out_of_range(capsys):
    rc = cli.email_link("99", transport=_fake_transport_link)
    assert rc == 1
    assert "out of range" in capsys.readouterr().out


# ------------------------------------------------ (d) absence of AUTO --------

def test_phase2a_handlers_never_call_auto_paths(monkeypatch, capsys):
    # forbid every AUTO entry point; handlers must still succeed via read-only path
    for name in ("process_auto_reply", "send_auto_reply", "can_auto_send", "is_safe_for_auto_reply"):
        monkeypatch.setattr(hh_message_reply, name, _rising)
    # auto_apply_modes is not imported in cli, but ensure the module's run path would explode if called
    import ai_assistant.auto_apply_modes as aam
    monkeypatch.setattr(aam, "run_auto_apply", _rising)
    # also guard classify_form / resolve_mode if someone tried to wire Stage 21
    monkeypatch.setattr(aam, "classify_form", _rising)
    monkeypatch.setattr(aam, "resolve_mode", _rising)

    assert cli.hh_message_classify("123", evaluate_fn=_fake_conversation_evaluate) == 0
    assert cli.email_classify("0", transport=_fake_transport) == 0
    assert cli.email_link("0", transport=_fake_transport_link) == 0
    capsys.readouterr()


# ------------------------------------------------ (e) absence of send/submit --

def test_phase2a_handlers_never_instantiate_send_or_submit(monkeypatch, capsys):
    # EmailSendGate must never be instantiated; process_incoming_* never called
    monkeypatch.setattr(email_message_reply, "process_incoming_email", _rising)

    class _GateBoom:
        def __init__(self, *a, **k):
            raise AssertionError("EmailSendGate was instantiated by a CLI handler")
        def send_email(self, *a, **k):
            raise AssertionError("EmailSendGate.send_email was called")
    monkeypatch.setattr(email_message_reply, "EmailSendGate", _GateBoom)
    # HH send gate must not be triggered
    for name in ("process_auto_reply", "send_auto_reply"):
        monkeypatch.setattr(hh_message_reply, name, _rising)

    assert cli.hh_message_classify("123", evaluate_fn=_fake_conversation_evaluate) == 0
    assert cli.email_classify("0", transport=_fake_transport) == 0
    assert cli.email_link("0", transport=_fake_transport_link) == 0
    capsys.readouterr()


# ------------------------------------------------ (f) absence of DB changes ----

def test_phase2a_handlers_do_not_touch_db(monkeypatch, capsys):
    # Handlers are stateless read-only; they must not init_db or write vacancies
    monkeypatch.setattr(cli, "init_db", _rising)
    monkeypatch.setattr(db_module, "init_db", _rising)
    # save helpers would be db writes — must not be called
    for name in ("save_vacancy", "save_deep_analysis", "save_application_package"):
        if hasattr(db_module, name):
            monkeypatch.setattr(db_module, name, _rising)

    assert cli.hh_message_classify("123", evaluate_fn=_fake_conversation_evaluate) == 0
    assert cli.email_classify("0", transport=_fake_transport) == 0
    assert cli.email_link("0", transport=_fake_transport_link) == 0
    capsys.readouterr()


# ------------------------------------------------ (g) absence of env-flag changes

def test_phase2a_does_not_toggle_auto_environment(monkeypatch, capsys):
    monkeypatch.delenv("HH_APPLY_MODE", raising=False)
    monkeypatch.delenv("HH_AUTO_REPLY_ENABLED", raising=False)

    assert cli.hh_message_classify("123", evaluate_fn=_fake_conversation_evaluate) == 0
    assert cli.email_classify("0", transport=_fake_transport) == 0
    assert cli.email_link("0", transport=_fake_transport_link) == 0
    assert cli.hh_message_classify("123", evaluate_fn=_fake_conversation_evaluate) == 0
    capsys.readouterr()

    assert os.environ.get("HH_APPLY_MODE") is None
    assert os.environ.get("HH_AUTO_REPLY_ENABLED") is None


# ------------------------------------------------ forbid check mirror ----------

def test_phase2a_cli_does_not_wire_auto_or_send():
    src = open(cli.__file__, encoding="utf-8").read()
    assert "auto_apply_modes" not in src
    assert "process_auto_reply(" not in src
    assert "run_auto_apply(" not in src
    assert "send_auto_reply(" not in src
    assert "can_auto_send(" not in src
    assert "confirm_live_send(" not in src
    assert "EmailSendGate(" not in src
    assert "hh_controlled_submit" not in src
    assert "hh_submission" not in src
    assert "hh_human_submission" not in src
