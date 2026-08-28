"""Stage 30D: Tests for `hh-message diagnose` (READ-ONLY HH probe).

Verifies:
  1. CDP_UNAVAILABLE when CDP endpoint is unreachable.
  2. HH_NOT_OPEN when no tab matches URL substring.
  3. HH_WRONG_PAGE when tab is open but not on messages section.
  4. CHATIK_FRAME_ABSENT when chatik iframe is missing.
  5. ISOLATED_WORLD_UNAVAILABLE when isolated world creation or 1+1 test fails.
  6. CONVERSATION_DOM_INACCESSIBLE when conversation DOM cannot be parsed.
  7. NO_MESSAGES when conversation is open but has 0 messages.
  8. HEALTHY on full happy path.
  9. --json output payload contract.
  10. Security / forbid-list verification (strictly READ-ONLY, no sends/mutations).
  11. Fail-soft error capture into errors[].
  12. Human text output format with READ-ONLY status line.
  13. CLI dispatch routing.

All tests use mocks/fakes only — no live Chrome or network calls.
"""

from __future__ import annotations

import json
import os
import sys
import pytest

from ai_assistant import cli
from ai_assistant.prefill_execute import ChatikFrameNotFound
import ai_assistant.hh_message_reply as hh_message_reply


# ------------------------------------------------------------------ Fakes -----

def _fake_targets(page_url="https://hh.ru/messages/123", page_title="Messages | hh.ru"):
    return [
        {
            "type": "page",
            "url": page_url,
            "title": page_title,
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/FAKE123",
        },
        {
            "type": "background_page",
            "url": "chrome-extension://abc/bg.html",
            "title": "Extension",
        },
    ]


def _fake_dialogs_json(is_messages=True, count=3):
    dialogs = [{"qa": "dialog-item", "text": f"Msg {i}"} for i in range(count)]
    return json.dumps({
        "url": "https://hh.ru/messages/123",
        "title": "Messages | hh.ru",
        "dialogs": dialogs,
        "pageIsMessages": is_messages,
    })


def _fake_conversation_json(conversation_id="conv-123", message_count=2, error=None):
    if error:
        return json.dumps({"error": error, "conversation_id": None, "messages": []})
    messages = [
        {"direction": "INCOMING", "text": f"Hello {i}", "sender": "employer"}
        for i in range(message_count)
    ]
    return json.dumps({
        "url": f"https://chatik.hh.ru/chat/{conversation_id}",
        "title": "Chat",
        "conversation_id": conversation_id,
        "composer_present": True,
        "messages": messages,
    })


# ----------------------------------------------------------- Verdict Tests -----

def test_cdp_unreachable_verdict(capsys):
    """1. CDP unreachable -> CDP_UNAVAILABLE, error in errors[], exit 0."""
    def _failing_targets(cdp_url):
        raise ConnectionRefusedError("Connection refused to 9222")

    rc = cli.hh_message_diagnose(
        cdp_url="http://127.0.0.1:9999",
        evaluate_fn=None,
        frame_probe_fn=None,
        targets=None,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "CDP_UNAVAILABLE"
    assert payload["cdp_reachable"] is False
    assert any("unreachable" in e.lower() for e in payload["errors"])


def test_hh_not_open_verdict(capsys):
    """2. Targets exist but none match url_substring -> HH_NOT_OPEN."""
    targets = [
        {"type": "page", "url": "https://google.com", "title": "Google"},
        {"type": "page", "url": "https://github.com", "title": "GitHub"},
    ]
    rc = cli.hh_message_diagnose(
        url_substring="hh.ru",
        targets=targets,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "HH_NOT_OPEN"
    assert payload["hh_page_present"] is False


def test_hh_wrong_page_verdict(capsys):
    """3. Matching tab exists but pageIsMessages is False -> HH_WRONG_PAGE."""
    targets = _fake_targets(page_url="https://hh.ru/search/vacancy", page_title="Job Search")

    def _eval(expr):
        return _fake_dialogs_json(is_messages=False, count=0)

    rc = cli.hh_message_diagnose(
        targets=targets,
        evaluate_fn=_eval,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "HH_WRONG_PAGE"
    assert payload["page_is_messages"] is False


def test_chatik_frame_absent_verdict(capsys):
    """4. Messages page OK but chatik frame is missing -> CHATIK_FRAME_ABSENT."""
    targets = _fake_targets()

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=2)
        raise ChatikFrameNotFound("Chatik frame not found")

    def _frame_probe(ws_url, f_subs):
        return {
            "frames": [{"frameId": "main", "url": "https://hh.ru/messages", "matched": False}],
            "chatik_frame_found": False,
            "chatik_frame_url": None,
            "isolated_world_ok": False,
        }

    rc = cli.hh_message_diagnose(
        targets=targets,
        evaluate_fn=_eval,
        frame_probe_fn=_frame_probe,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "CHATIK_FRAME_ABSENT"
    assert payload["chatik_frame_found"] is False


def test_isolated_world_unavailable_verdict(capsys):
    """5. Chatik frame found but isolated world creation or 1+1 fails -> ISOLATED_WORLD_UNAVAILABLE."""
    targets = _fake_targets()

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=2)
        if expr.strip() in ("1+1", "1 + 1"):
            return "undefined"  # 1+1 test failed
        return _fake_conversation_json()

    def _frame_probe(ws_url, f_subs):
        return {
            "frames": [{"frameId": "f1", "url": "https://chatik.hh.ru/chat/123", "matched": True}],
            "chatik_frame_found": True,
            "chatik_frame_url": "https://chatik.hh.ru/chat/123",
            "isolated_world_ok": False,
        }

    rc = cli.hh_message_diagnose(
        targets=targets,
        evaluate_fn=_eval,
        frame_probe_fn=_frame_probe,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "ISOLATED_WORLD_UNAVAILABLE"
    assert payload["isolated_world_ok"] is False


def test_conversation_dom_inaccessible_verdict(capsys):
    """6. Isolated world OK but conversation JS extraction errors -> CONVERSATION_DOM_INACCESSIBLE."""
    targets = _fake_targets()

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=2)
        if expr.strip() in ("1+1", "1 + 1"):
            return "2"
        return _fake_conversation_json(error="Cannot read properties of null")

    def _frame_probe(ws_url, f_subs):
        return {
            "frames": [{"frameId": "f1", "url": "https://chatik.hh.ru/chat/123", "matched": True}],
            "chatik_frame_found": True,
            "chatik_frame_url": "https://chatik.hh.ru/chat/123",
            "isolated_world_ok": True,
        }

    rc = cli.hh_message_diagnose(
        targets=targets,
        evaluate_fn=_eval,
        frame_probe_fn=_frame_probe,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "CONVERSATION_DOM_INACCESSIBLE"
    assert payload["conversation_dom_ok"] is False


def test_no_messages_verdict(capsys):
    """7. Conversation DOM accessible but 0 messages -> NO_MESSAGES."""
    targets = _fake_targets()

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=2)
        if expr.strip() in ("1+1", "1 + 1"):
            return "2"
        return _fake_conversation_json(message_count=0)

    def _frame_probe(ws_url, f_subs):
        return {
            "frames": [{"frameId": "f1", "url": "https://chatik.hh.ru/chat/123", "matched": True}],
            "chatik_frame_found": True,
            "chatik_frame_url": "https://chatik.hh.ru/chat/123",
            "isolated_world_ok": True,
        }

    rc = cli.hh_message_diagnose(
        targets=targets,
        evaluate_fn=_eval,
        frame_probe_fn=_frame_probe,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "NO_MESSAGES"
    assert payload["message_count"] == 0
    assert payload["conversation_dom_ok"] is True


def test_healthy_verdict(capsys):
    """8. Full happy path -> HEALTHY."""
    targets = _fake_targets()

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=5)
        if expr.strip() in ("1+1", "1 + 1"):
            return "2"
        return _fake_conversation_json(conversation_id="c-777", message_count=4)

    def _frame_probe(ws_url, f_subs):
        return {
            "frames": [{"frameId": "f1", "url": "https://chatik.hh.ru/chat/c-777", "matched": True}],
            "chatik_frame_found": True,
            "chatik_frame_url": "https://chatik.hh.ru/chat/c-777",
            "isolated_world_ok": True,
        }

    rc = cli.hh_message_diagnose(
        targets=targets,
        evaluate_fn=_eval,
        frame_probe_fn=_frame_probe,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "HEALTHY"
    assert payload["message_count"] == 4
    assert payload["conversation_id"] == "c-777"
    assert payload["dialogs_visible"] == 5


# ----------------------------------------------- Contract & Safety Tests -----

def test_json_flag_payload_contract(capsys):
    """9. --json output payload contract: all required keys present and valid."""
    targets = _fake_targets()

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=1)
        return "2"

    cli.hh_message_diagnose(targets=targets, evaluate_fn=_eval, as_json=True)
    raw = capsys.readouterr().out
    payload = json.loads(raw)

    expected_keys = {
        "cdp_reachable", "matching_tabs", "hh_page_present", "page_url",
        "page_title", "page_is_messages", "frames", "chatik_frame_found",
        "chatik_frame_url", "isolated_world_ok", "conversation_dom_ok",
        "conversation_id", "composer_present", "message_count",
        "dialogs_visible", "errors", "verdict", "checked_at",
    }
    assert expected_keys.issubset(payload.keys())
    assert isinstance(payload["errors"], list)
    assert isinstance(payload["checked_at"], str)


def test_diagnose_never_sends_or_mutates():
    """10. Forbid-list security scan: no AUTO / send / navigation / submit imports."""
    src = open(cli.__file__, encoding="utf-8").read()
    assert "auto_apply_modes" not in src
    assert "process_auto_reply(" not in src
    assert "run_auto_apply(" not in src
    assert "send_auto_reply(" not in src
    assert "can_auto_send(" not in src
    assert "Page.navigate" not in src


def test_fail_soft_records_errors(capsys):
    """11. Fail-soft: mid-probe unexpected exception is recorded in errors[], no traceback."""
    targets = _fake_targets()

    def _broken_eval(expr):
        raise ValueError("Unexpected network glitch during evaluate")

    rc = cli.hh_message_diagnose(
        targets=targets,
        evaluate_fn=_broken_eval,
        as_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["errors"]) > 0
    assert any("glitch" in e for e in payload["errors"])
    assert payload["verdict"] in [
        "CDP_UNAVAILABLE", "HH_NOT_OPEN", "HH_WRONG_PAGE",
        "CHATIK_FRAME_ABSENT", "ISOLATED_WORLD_UNAVAILABLE",
        "CONVERSATION_DOM_INACCESSIBLE", "NO_MESSAGES", "HEALTHY"
    ]


def test_diagnose_human_output_lines(capsys):
    """12. Human output contains required section labels and READ-ONLY trailer."""
    targets = _fake_targets()

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=3)
        if expr.strip() in ("1+1", "1 + 1"):
            return "2"
        return _fake_conversation_json(conversation_id="12345", message_count=7)

    rc = cli.hh_message_diagnose(targets=targets, evaluate_fn=_eval, as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[hh-message] diagnose (READ-ONLY probe)" in out
    assert "cdp:" in out
    assert "hh tab:" in out
    assert "page is messages:" in out
    assert "frames:" in out
    assert "isolated world:" in out
    assert "conversation DOM:" in out
    assert "dialogs visible:" in out
    assert "errors:" in out
    assert "verdict: HEALTHY" in out
    assert "status: READ-ONLY — nothing sent." in out


def test_cli_dispatch_hh_message_diagnose(monkeypatch):
    """13. CLI dispatch routes `hh-message diagnose` subcommand."""
    called = []

    def _fake_diag(*args, **kwargs):
        called.append((args, kwargs))
        return 0

    monkeypatch.setattr(cli, "hh_message_diagnose", _fake_diag)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "hh-message", "diagnose", "--json"])
    rc = cli.main()
    assert rc == 0
    assert len(called) == 1
    assert called[0][1]["as_json"] is True
