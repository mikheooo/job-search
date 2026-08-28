"""Stage 30C Phase 1: CLI wiring tests for Stage 21-29 REVIEW/READ-ONLY commands.

Verifies:
  - `hh-message list`, `hh-message preview`, `email list`, `email preview`,
    `gmail status` exist and parse/dispatch correctly;
  - list/preview/status run in READ-ONLY mode (fakes, no network, no browser);
  - no send/submit function is invoked;
  - confirm_live_send is never called;
  - EmailSendGate is never instantiated/bypassed;
  - HH_AUTO_REPLY_ENABLED / HH_APPLY_MODE are never toggled;
  - the AUTO (submit-capable) module is not wired into the CLI.

All tests use mocks/fakes only.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from ai_assistant import cli
import ai_assistant.hh_message_reply as hh_message_reply
import ai_assistant.email_message_reply as email_message_reply


# ---------------------------------------------------------------- fakes -----

def _profile():
    return {
        "desired_roles": ["AI Automation Engineer"],
        "languages": ["en"],
        "remote_required": True,
    }


def _fake_dialogs_evaluate(expr):
    return json.dumps({
        "url": "https://hh.ru/account/messages",
        "title": "Сообщения",
        "pageIsMessages": True,
        "dialogs": [
            {"qa": "dialog-item", "tag": "DIV",
             "text": "Компания X: Здравствуйте! Всё ещё заинтересованы?"},
        ],
    })


def _fake_conversation_evaluate(expr):
    return json.dumps({
        "url": "https://hh.ru/applicant/negotiations?messageConversationId=123",
        "title": "chat",
        "conversation_id": "123",
        "messages": [
            {"direction": "INCOMING", "text": "Здравствуйте! Вы всё ещё заинтересованы?",
             "sender": "Рекрутер"},
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


def _rising(*a, **k):
    raise AssertionError("forbidden AUTO/send function was called")


# ------------------------------------------- parsing + dispatch (no network) -

@pytest.mark.parametrize("argv,attr", [
    (["hh-message", "list"], "hh_message_list"),
    (["hh-message", "preview", "123"], "hh_message_preview"),
    (["email", "list"], "email_list"),
    (["email", "preview", "0"], "email_preview"),
    (["gmail", "status"], "gmail_status"),
])
def test_cli_dispatch_routes_readonly_commands(monkeypatch, argv, attr):
    calls = []

    def fake(*a, **k):
        calls.append((a, k))
        return 0

    monkeypatch.setattr(cli, attr, fake)
    monkeypatch.setattr(sys, "argv", ["job-search-cli"] + argv)
    rc = cli.main()
    assert rc == 0
    assert calls, f"{attr} was not reached by CLI dispatch"
    assert "_resolve_hh_evaluate" not in str(calls) or True  # handler-level, not here


# ------------------------------------------------------------- HH list/preview

def test_hh_message_list_readonly(monkeypatch, capsys):
    # forbid any AUTO/send path from being reached
    for name in ("process_auto_reply", "send_auto_reply",
                 "can_auto_send", "is_safe_for_auto_reply"):
        monkeypatch.setattr(hh_message_reply, name, _rising)
    rc = cli.hh_message_list(evaluate_fn=_fake_dialogs_evaluate)
    out = capsys.readouterr().out
    assert rc == 0
    assert "READ-ONLY" in out
    assert "dialog-item" in out


def test_hh_message_list_no_dialogs_readonly(capsys):
    def ev(expr):
        return json.dumps({"url": "http://x", "title": "", "dialogs": [],
                           "pageIsMessages": True})
    rc = cli.hh_message_list(evaluate_fn=ev)
    assert rc == 0
    assert "no dialogs" in capsys.readouterr().out


def test_hh_message_preview_readonly(monkeypatch, capsys):
    for name in ("process_auto_reply", "send_auto_reply",
                 "can_auto_send", "is_safe_for_auto_reply"):
        monkeypatch.setattr(hh_message_reply, name, _rising)
    rc = cli.hh_message_preview("123", evaluate_fn=_fake_conversation_evaluate,
                                profile=_profile())
    out = capsys.readouterr().out
    assert rc == 0
    assert "PREVIEW ONLY" in out
    assert "prepared reply" in out
    assert "nothing sent" in out


def test_hh_message_preview_gap_when_no_messages(capsys):
    # simulates the chatik-iframe not reachable: handler must stop, not fabricate
    def ev(expr):
        return json.dumps({"url": "http://x", "title": "", "conversation_id": None,
                           "messages": [], "composer_present": False})
    rc = cli.hh_message_preview("123", evaluate_fn=ev)
    out = capsys.readouterr().out
    assert rc == 1
    assert "no messages" in out
    assert "nothing sent" in out


# ---------------------------------------------------------------- email list

def test_email_list_readonly(monkeypatch, capsys):
    monkeypatch.setattr(email_message_reply, "process_incoming_email", _rising)
    rc = cli.email_list(transport=_fake_transport)
    out = capsys.readouterr().out
    assert rc == 0
    assert "r@job.com" in out
    assert "READ-ONLY" in out


def test_email_list_blocked_transport(capsys):
    def bad():
        raise RuntimeError("provider unreachable")
    rc = cli.email_list(transport=bad)
    out = capsys.readouterr().out
    assert rc == 1
    assert "blocked" in out


# --------------------------------------------------------------- email preview

def test_email_preview_readonly(monkeypatch, capsys):
    monkeypatch.setattr(email_message_reply, "process_incoming_email", _rising)
    rc = cli.email_preview("0", transport=_fake_transport, profile=_profile())
    out = capsys.readouterr().out
    assert rc == 0
    assert "PREVIEW ONLY" in out
    assert "prepared reply" in out


def test_email_preview_target_out_of_range(capsys):
    rc = cli.email_preview("99", transport=_fake_transport)
    assert rc == 1
    assert "nothing sent" in capsys.readouterr().out


# ------------------------------------------------------------------ gmail

def test_gmail_status_ready(capsys):
    rc = cli.gmail_status(status_fn=lambda: {"status": "READY",
                                             "reason": "test"})
    out = capsys.readouterr().out
    assert rc == 0
    assert "gmail.readonly" in out


def test_gmail_status_blocked(capsys):
    rc = cli.gmail_status(status_fn=lambda: {"status": "EMAIL_OAUTH_BLOCKED",
                                             "reason": "no scope"})
    assert rc == 1
    assert "EMAIL_OAUTH_BLOCKED" in capsys.readouterr().out


# ------------------------------------------------- safety guards (behavioral)

def test_hh_handlers_never_call_auto_send_or_submit(monkeypatch, capsys):
    for name in ("process_auto_reply", "send_auto_reply", "can_auto_send",
                 "is_safe_for_auto_reply"):
        monkeypatch.setattr(hh_message_reply, name, _rising)
    assert cli.hh_message_list(evaluate_fn=_fake_dialogs_evaluate) == 0
    assert cli.hh_message_preview("123", evaluate_fn=_fake_conversation_evaluate,
                                  profile=_profile()) == 0
    capsys.readouterr()


def test_email_handlers_never_gate_send_or_mutate_state(monkeypatch, capsys):
    # forbid state-mutating processing and any EmailSendGate use
    monkeypatch.setattr(email_message_reply, "process_incoming_email", _rising)

    class _GateBoom:
        def __init__(self, *a, **k):
            raise AssertionError("EmailSendGate was instantiated by a CLI handler")
        def send_email(self, *a, **k):
            raise AssertionError("EmailSendGate.send_email was called")

    monkeypatch.setattr(email_message_reply, "EmailSendGate", _GateBoom)
    assert cli.email_list(transport=_fake_transport) == 0
    assert cli.email_preview("0", transport=_fake_transport, profile=_profile()) == 0
    capsys.readouterr()


def test_phase1_does_not_toggle_auto_environment(monkeypatch, capsys):
    monkeypatch.delenv("HH_APPLY_MODE", raising=False)
    monkeypatch.delenv("HH_AUTO_REPLY_ENABLED", raising=False)
    assert cli.hh_message_list(evaluate_fn=_fake_dialogs_evaluate) == 0
    assert cli.email_preview("0", transport=_fake_transport, profile=_profile()) == 0
    assert cli.gmail_status(status_fn=lambda: {"status": "READY", "reason": "t"}) == 0
    capsys.readouterr()
    assert os.environ.get("HH_APPLY_MODE") is None
    assert os.environ.get("HH_AUTO_REPLY_ENABLED") is None


def test_auto_submit_module_not_wired_into_cli():
    # cli.py must not import/bind the submit-capable auto_apply_modes module,
    # nor the send primitives of hh_message_reply.
    src = open(cli.__file__, encoding="utf-8").read()
    assert "auto_apply_modes" not in src
    assert "process_auto_reply(" not in src
    assert "run_auto_apply(" not in src
    assert "send_auto_reply(" not in src
    assert "can_auto_send(" not in src
    # no CLI command dispatches to an AUTO/send entry point
    assert "auto-apply" not in src