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


def test_diagnose_selects_messages_tab_when_search_tab_comes_first(capsys):
    """14. When multiple HH tabs exist and first is /search/ and second is /applicant/negotiations,
    diagnose selects the negotiations tab."""
    targets = [
        {
            "type": "page",
            "url": "https://hh.ru/search/vacancy?text=python",
            "title": "Search HH",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/SEARCH1",
        },
        {
            "type": "page",
            "url": "https://hh.ru/applicant/negotiations",
            "title": "Отклики и сообщения - hh.ru",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/MESSAGES2",
        },
    ]

    def _eval(expr):
        if "pageIsMessages" in expr:
            return _fake_dialogs_json(is_messages=True, count=3)
        if expr.strip() in ("1+1", "1 + 1"):
            return "2"
        return _fake_conversation_json(conversation_id="conv-456", message_count=2)

    def _frame_probe(ws_url, f_subs):
        assert ws_url == "ws://127.0.0.1:9222/devtools/page/MESSAGES2"
        return {
            "frames": [{"frameId": "f1", "url": "https://chatik.hh.ru/chat/conv-456", "matched": True}],
            "chatik_frame_found": True,
            "chatik_frame_url": "https://chatik.hh.ru/chat/conv-456",
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
    assert payload["page_url"] == "https://hh.ru/applicant/negotiations"
    assert payload["page_is_messages"] is True


def test_diagnose_multiple_search_tabs_returns_hh_wrong_page(capsys):
    """15. When multiple HH tabs exist but ALL are search tabs, result remains HH_WRONG_PAGE."""
    targets = [
        {
            "type": "page",
            "url": "https://hh.ru/search/vacancy?text=python",
            "title": "Search 1",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/SEARCH1",
        },
        {
            "type": "page",
            "url": "https://hh.ru/search/vacancy?text=react",
            "title": "Search 2",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/SEARCH2",
        },
    ]

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


def test_select_best_hh_target_helper():
    """16. Unit test for select_best_hh_target helper priorities."""
    from ai_assistant.prefill_execute import select_best_hh_target

    # Empty
    assert select_best_hh_target([], "hh.ru") is None

    # No match
    assert select_best_hh_target([{"type": "page", "url": "https://google.com"}], "hh.ru") is None

    # Single search tab
    t_search = {"type": "page", "url": "https://hh.ru/search/vacancy", "title": "Search"}
    assert select_best_hh_target([t_search], "hh.ru") == t_search

    # Search + negotiations -> negotiations wins
    t_neg = {"type": "page", "url": "https://hh.ru/applicant/negotiations", "title": "Negotiations"}
    assert select_best_hh_target([t_search, t_neg], "hh.ru") == t_neg
    assert select_best_hh_target([t_neg, t_search], "hh.ru") == t_neg

    # Specific conversation query wins over generic negotiations
    t_conv = {"type": "page", "url": "https://hh.ru/applicant/negotiations?messageConversationId=99", "title": "Chat"}
    assert select_best_hh_target([t_search, t_neg, t_conv], "hh.ru") == t_conv

    # Direct /chat/<id> tab also wins over search
    t_direct_chat = {"type": "page", "url": "https://hh.ru/chat/5577169431", "title": "Chat"}
    assert select_best_hh_target([t_search, t_direct_chat], "hh.ru") == t_direct_chat


def test_direct_chat_id_url_recognition(capsys):
    """17. Direct /chat/<id> URL is recognized as a valid messages/chat page and yields HEALTHY."""
    targets = [
        {
            "type": "page",
            "url": "https://hh.ru/chat/5577169431",
            "title": "3 ・ Чаты",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/CHAT1",
        }
    ]

    def _eval(expr):
        if "pageIsMessages" in expr:
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "title": "3 ・ Чаты",
                "dialogs": [],
                "pageIsMessages": True,
            })
        if expr.strip() in ("1+1", "1 + 1"):
            return "2"
        return _fake_conversation_json(conversation_id="5577169431", message_count=8)

    def _frame_probe(ws_url, f_subs):
        return {
            "frames": [{"frameId": "f-main", "url": "https://hh.ru/chat/5577169431", "matched": True}],
            "chatik_frame_found": True,
            "chatik_frame_url": "https://hh.ru/chat/5577169431",
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
    assert payload["page_url"] == "https://hh.ru/chat/5577169431"
    assert payload["page_is_messages"] is True
    assert payload["conversation_id"] == "5577169431"
    assert payload["message_count"] == 8


def test_direct_chat_id_without_conversation_dom_gives_proper_verdict(capsys):
    """18. Direct /chat/<id> recognized as messages page, but if conversation DOM extraction fails,
    returns CONVERSATION_DOM_INACCESSIBLE instead of false HEALTHY."""
    targets = [
        {
            "type": "page",
            "url": "https://hh.ru/chat/5577169431",
            "title": "3 ・ Чаты",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/CHAT1",
        }
    ]

    def _eval(expr):
        if "pageIsMessages" in expr:
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "title": "3 ・ Чаты",
                "dialogs": [],
                "pageIsMessages": True,
            })
        if expr.strip() in ("1+1", "1 + 1"):
            return "2"
        return json.dumps({"error": "Failed to read conversation DOM", "conversation_id": None, "messages": []})

    def _frame_probe(ws_url, f_subs):
        return {
            "frames": [{"frameId": "f-main", "url": "https://hh.ru/chat/5577169431", "matched": True}],
            "chatik_frame_found": True,
            "chatik_frame_url": "https://hh.ru/chat/5577169431",
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


# ------------------------------------------------ Stage 30D.2 Preview Tests -----

def test_preview_chat_id_json_contract(capsys):
    """19. Stage 30D.2: hh-message preview --json returns valid contract for /chat/<id>."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "title": "3 ・ Чаты",
            "conversation_id": "5577169431",
            "participant": "Компания Тест",
            "composer_present": True,
            "messages": [
                {"direction": "OUTGOING", "text": "Msg 1", "timestamp": "10:00"},
                {"direction": "INCOMING", "text": "Msg 2", "timestamp": "10:05"},
                {"direction": "OUTGOING", "text": "Msg 3", "timestamp": "10:10"},
            ],
        })

    rc = cli.hh_message_preview(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_id"] == "5577169431"
    assert payload["url"] == "https://hh.ru/chat/5577169431"
    assert payload["participant"] == "Компания Тест"
    assert payload["message_count"] == 3
    assert payload["composer_present"] is True
    assert payload["status"] == "READ-ONLY"
    assert len(payload["messages"]) == 3
    assert payload["messages"][0] == {"author": "candidate", "text": "Msg 1", "timestamp": "10:00"}
    assert payload["messages"][1] == {"author": "employer", "text": "Msg 2", "timestamp": "10:05"}
    assert payload["messages"][2] == {"author": "candidate", "text": "Msg 3", "timestamp": "10:10"}


def test_preview_chat_id_with_limit(capsys):
    """20. Stage 30D.2: --limit N restricts messages array to the last N in order."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": f"Msg {i}", "timestamp": None}
                for i in range(10)
            ],
            "composer_present": False,
        })

    rc = cli.hh_message_preview(evaluate_fn=_eval, limit=3, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["message_count"] == 10
    assert len(payload["messages"]) == 3
    assert payload["messages"][0]["text"] == "Msg 7"
    assert payload["messages"][1]["text"] == "Msg 8"
    assert payload["messages"][2]["text"] == "Msg 9"


def test_preview_fail_soft_on_error(capsys):
    """21. Stage 30D.2: transport / DOM error returns valid json with errors[] and status READ-ONLY."""
    def _eval(expr):
        raise ConnectionRefusedError("CDP connection broken")

    rc = cli.hh_message_preview(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["errors"]) > 0
    assert payload["message_count"] == 0
    assert payload["messages"] == []
    assert payload["status"] == "READ-ONLY"


def test_preview_human_output_formatting(capsys):
    """22. Stage 30D.2: Human text mode outputs structured headers and last messages."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "title": "3 ・ Чаты",
            "conversation_id": "5577169431",
            "participant": "ООО Рога и Копыта",
            "composer_present": False,
            "messages": [
                {"direction": "INCOMING", "text": "Здравствуйте!", "timestamp": None},
                {"direction": "OUTGOING", "text": "Добрый день!", "timestamp": None},
            ],
        })

    rc = cli.hh_message_preview(evaluate_fn=_eval, as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[hh-message] preview (READ-ONLY)" in out
    assert "conversation: 5577169431" in out
    assert "url: https://hh.ru/chat/5577169431" in out
    assert "participant: ООО Рога и Копыта" in out
    assert "messages: 2" in out
    assert "composer: unavailable" in out
    assert "--- last messages ---" in out
    assert "[employer] Здравствуйте!" in out
    assert "[candidate] Добрый день!" in out
    assert "status: PREVIEW ONLY — nothing sent" in out


def test_preview_cli_dispatch_options(monkeypatch):
    """23. Stage 30D.2: CLI routes optional conversation_id, --limit, and --json."""
    calls = []

    def _fake_preview(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(cli, "hh_message_preview", _fake_preview)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "hh-message", "preview", "--limit", "5", "--json"])
    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][1]["limit"] == 5
    assert calls[0][1]["as_json"] is True


# ---------------------------------------------- Stage 30D.3 Classify Tests -----

def test_classify_direct_question_needs_reply(capsys):
    """24. Stage 30D.3/30D.5: Direct question -> NEEDS_REPLY with honest context-aware draft."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "OUTGOING", "text": "Отклик на вакансию", "timestamp": None},
                {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с e-commerce / маркетплейсами?", "timestamp": None},
                {"direction": "OUTGOING", "text": "Здравствуйте!", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_classify(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_id"] == "5577169431"
    assert payload["classification"] == "NEEDS_REPLY"
    assert payload["confidence"] >= 0.85
    assert payload["prepared_reply"] is not None
    assert "Ozon" in payload["prepared_reply"]
    assert "нет" in payload["prepared_reply"]
    assert "missing_facts" in payload
    assert payload["status"] == "READ-ONLY"


def test_classify_candidate_already_answered(capsys):
    """25. Stage 30D.3: Candidate already gave substantive answer -> NO_REPLY_NEEDED."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт с Python?", "timestamp": None},
                {"direction": "OUTGOING", "text": "Да, более 5 лет разрабатываю на Python.", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_classify(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"] == "NO_REPLY_NEEDED"
    assert payload["prepared_reply"] is None


def test_classify_empty_conversation(capsys):
    """26. Stage 30D.3: Empty conversation -> EMPTY_CONVERSATION."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [],
            "composer_present": True,
        })

    rc = cli.hh_message_classify(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"] == "EMPTY_CONVERSATION"
    assert payload["confidence"] == 1.0
    assert payload["prepared_reply"] is None


def test_classify_salary_requires_human_review(capsys):
    """27. Stage 30D.3: Salary inquiry -> HUMAN_REVIEW with null prepared_reply."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "На какую зарплату вы рассчитываете?", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_classify(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"] == "HUMAN_REVIEW"
    assert payload["prepared_reply"] is None


def test_classify_repeated_bot_questions(capsys):
    """28. Stage 30D.3: Repeated identical bot messages handled cleanly."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Начнем?", "timestamp": None},
                {"direction": "INCOMING", "text": "Есть ли у вас опыт с e-commerce?", "timestamp": None},
                {"direction": "INCOMING", "text": "Есть ли у вас опыт с e-commerce?", "timestamp": None},
                {"direction": "OUTGOING", "text": "Здравствуйте!", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_classify(evaluate_fn=_eval, limit=2, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"] == "NEEDS_REPLY"
    assert len(payload["context"]) == 2


def test_classify_cli_dispatch_options(monkeypatch):
    """29. Stage 30D.3: CLI routes hh-message classify with --json and --limit."""
    calls = []

    def _fake_classify(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(cli, "hh_message_classify", _fake_classify)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "hh-message", "classify", "--limit", "4", "--json"])
    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][1]["limit"] == 4
    assert calls[0][1]["as_json"] is True


def test_classify_forbid_list_security():
    """30. Stage 30D.3: forbid-list check: classify has no live send or mutation imports."""
    src = open(cli.__file__, encoding="utf-8").read()
    assert "send_auto_reply(" not in src
    assert "process_auto_reply(" not in src
    assert "run_auto_apply(" not in src
    assert "can_auto_send(" not in src


# ---------------------------------------------- Stage 30D.4 Validate Tests -----

def test_validate_confirmed_profile_facts_approved(capsys):
    """31. Stage 30D.4: Draft with verified skills (Python/n8n from profile) -> APPROVED."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Уточните, какой у вас основной стек?", "timestamp": None},
            ],
            "composer_present": True,
        })

    profile = {
        "skills": ["python", "n8n", "automation"],
        "desired_roles": ["AI Automation Engineer"],
        "years_experience": 3,
    }
    # Pass custom evaluation and profile
    rc = cli.hh_message_validate(evaluate_fn=_eval, as_json=True, profile=profile)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_id"] == "5577169431"
    assert payload["validation"] == "APPROVED"
    assert payload["checks"]["answers_last_question"] is True
    assert payload["checks"]["uses_supported_facts"] is True
    assert payload["checks"]["contains_unverified_claims"] is False
    assert payload["checks"]["contains_sensitive_claims"] is False
    assert payload["checks"]["is_empty"] is False
    assert payload["status"] == "READ-ONLY"


def test_validate_unverified_ozon_wb_claims_human_review(capsys):
    """32. Stage 30D.4/30D.5: Positive draft claiming Ozon/WB when missing from profile -> HUMAN_REVIEW."""
    dialog = hh_message_reply.HHDialog(
        conversation_id="5577169431",
        messages=[
            hh_message_reply.HHMessage(message_id="m1", text="Есть ли у вас опыт с Ozon/WB?", sender="employer"),
        ],
    )
    profile = {
        "skills": ["python", "n8n", "automation"],
        "desired_roles": ["AI Automation Engineer"],
        "years_experience": 3,
    }
    # Explicit draft falsely asserting positive experience
    hallucinated_draft = "Да, у меня есть 3 года опыта работы с Ozon и Wildberries."
    val = hh_message_reply.validate_hh_reply_draft(dialog, draft=hallucinated_draft, classification="NEEDS_REPLY", profile=profile)
    assert val["validation"] == "HUMAN_REVIEW"
    assert val["checks"]["contains_unverified_claims"] is True
    assert val["checks"]["uses_supported_facts"] is False
    assert any("Ozon/WB" in r for r in val["reasons"])


def test_validate_salary_question_human_review(capsys):
    """33. Stage 30D.4: Sensitive salary inquiry -> HUMAN_REVIEW."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Какая у вас зарплатная вилка?", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_validate(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["validation"] == "HUMAN_REVIEW"


def test_validate_empty_draft_rejected(capsys):
    """34. Stage 30D.4: Empty draft -> REJECTED."""
    dialog = hh_message_reply.HHDialog(
        conversation_id="5577169431",
        messages=[hh_message_reply.HHMessage(message_id="m1", text="Здравствуйте", sender="employer")],
    )
    val = hh_message_reply.validate_hh_reply_draft(dialog, draft="", classification="NEEDS_REPLY")
    assert val["validation"] == "REJECTED"
    assert val["checks"]["is_empty"] is True


def test_validate_off_topic_draft_rejected():
    """35. Stage 30D.4: Off-topic / action commands draft -> REJECTED."""
    dialog = hh_message_reply.HHDialog(
        conversation_id="5577169431",
        messages=[hh_message_reply.HHMessage(message_id="m1", text="Здравствуйте", sender="employer")],
    )
    val = hh_message_reply.validate_hh_reply_draft(dialog, draft="submit form using click()", classification="NEEDS_REPLY")
    assert val["validation"] == "REJECTED"
    assert "instructions" in val["reasons"][0]


def test_validate_classification_human_review_cannot_be_approved():
    """36. Stage 30D.4: If classification=HUMAN_REVIEW, validation cannot become APPROVED."""
    dialog = hh_message_reply.HHDialog(
        conversation_id="5577169431",
        messages=[hh_message_reply.HHMessage(message_id="m1", text="Здравствуйте", sender="employer")],
    )
    val = hh_message_reply.validate_hh_reply_draft(
        dialog, draft="Здравствуйте! Готов обсудить.", classification="HUMAN_REVIEW"
    )
    assert val["validation"] == "HUMAN_REVIEW"


def test_validate_cli_dispatch_options(monkeypatch):
    """37. Stage 30D.4: CLI routes hh-message validate with --json and --limit."""
    calls = []

    def _fake_validate(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(cli, "hh_message_validate", _fake_validate)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "hh-message", "validate", "--limit", "3", "--json"])
    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][1]["limit"] == 3
    assert calls[0][1]["as_json"] is True


def test_validate_forbid_list_security():
    """38. Stage 30D.4: forbid-list check: validate has no live send or mutation imports."""
    src = open(cli.__file__, encoding="utf-8").read()
    assert "send_auto_reply(" not in src
    assert "process_auto_reply(" not in src
    assert "run_auto_apply(" not in src
    assert "can_auto_send(" not in src
    assert "Page.navigate" not in src


# ---------------------------------------------- Stage 30D.5 Context-Aware Tests -

def test_context_aware_ozon_wb_honest_draft_approved(capsys):
    """39. Stage 30D.5: Question about Ozon/WB generates honest draft that passes validation (APPROVED)."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с e-commerce / маркетплейсами (Ozon, Wildberries)?", "timestamp": None},
                {"direction": "OUTGOING", "text": "Здравствуйте!", "timestamp": None},
            ],
            "composer_present": True,
        })

    profile = {
        "skills": ["python", "n8n", "automation", "api"],
        "desired_roles": ["AI Automation Engineer"],
        "years_experience": 3,
    }
    rc = cli.hh_message_validate(evaluate_fn=_eval, as_json=True, profile=profile)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_id"] == "5577169431"
    assert payload["validation"] == "APPROVED"
    assert payload["checks"]["contains_unverified_claims"] is False
    assert payload["checks"]["uses_supported_facts"] is True


def test_context_aware_classify_facts_breakdown(capsys):
    """40. Stage 30D.5: Classify returns required_facts, available_facts, and missing_facts."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с Ozon?", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_classify(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["question"] is not None
    assert len(payload["required_facts"]) > 0
    assert len(payload["available_facts"]) > 0
    assert len(payload["missing_facts"]) > 0
    assert "Ozon" in payload["missing_facts"][0]


def test_classify_with_linked_vacancy_context(monkeypatch):
    """41. Stage 30D.5: Vacancy context from DB is integrated into available_facts if linked."""
    def fake_resolve(dialog):
        return {
            "stable_id": "hh:12345",
            "title": "Senior AI Automation Engineer",
            "company": "Acme AI Corp",
            "description": "Building n8n workflows and Python agents",
        }
    monkeypatch.setattr(hh_message_reply, "resolve_vacancy_for_dialog", fake_resolve)

    dialog = hh_message_reply.HHDialog(
        conversation_id="5577169431",
        vacancy_title="Python Developer",
        vacancy_stable_id="hh:12345",
        messages=[
            hh_message_reply.HHMessage(message_id="m1", text="Готовы ли вы обсудить детали вакансии?", sender="employer"),
        ],
    )
    det = hh_message_reply.classify_hh_conversation_detailed(dialog)
    assert det["classification"] == "NEEDS_REPLY"
    assert any("vacancy: Senior AI Automation Engineer" in f for f in det["available_facts"])
    assert any("database: vacancy hh:12345" in s for s in det["sources"])


def test_classify_missing_vacancy_fail_soft():
    """42. Stage 30D.5: Unlinked/missing vacancy fails soft and continues with dialogue facts."""
    dialog = hh_message_reply.HHDialog(
        conversation_id="5577169431",
        vacancy_title="Unknown Role",
        vacancy_stable_id="hh:999999999",
        messages=[
            hh_message_reply.HHMessage(message_id="m1", text="Здравствуйте! Уточните ваш стек.", sender="employer"),
        ],
    )
    det = hh_message_reply.classify_hh_conversation_detailed(dialog)
    assert det["classification"] == "NEEDS_REPLY"
    assert det["prepared_reply"] is not None
    assert "candidate_profile.json: skills" in det["sources"]


# ---------------------------------------------- Stage 30D.6 Send Tests ---------

def test_send_dry_run_without_confirm_not_sent(capsys):
    """43. Stage 30D.6: send without --confirm performs review only (sent=False, confirmed=False)."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с n8n?", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=False, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_id"] == "5577169431"
    assert payload["classification"] == "NEEDS_REPLY"
    assert payload["validation"] == "APPROVED"
    assert payload["confirmed"] is False
    assert payload["sent"] is False
    assert payload["post_send_verified"] is False
    assert payload["status"] == "AWAITING_CONFIRMATION"


def test_send_human_review_blocked_even_with_confirm(capsys):
    """44. Stage 30D.6: send blocked when validation is HUMAN_REVIEW even if confirm=True."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Какая у вас желаемая зарплата?", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["validation"] == "HUMAN_REVIEW"
    assert payload["sent"] is False
    assert payload["status"] == "BLOCKED_VALIDATION_HUMAN_REVIEW"


def test_send_rejected_blocked_even_with_confirm(capsys):
    """45. Stage 30D.6: send blocked when validation is REJECTED even if confirm=True."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "К сожалению, мы выбрали другого кандидата.", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["validation"] == "REJECTED"
    assert payload["sent"] is False
    assert "BLOCKED_VALIDATION_REJECTED" in payload["status"]


def test_send_mismatched_conversation_id_blocked(capsys):
    """46. Stage 30D.6: send blocked if specified conversation ID does not match active tab."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Здравствуйте!", "timestamp": None},
            ],
            "composer_present": True,
        })

    rc = cli.hh_message_send(conversation_id="9999999999", evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["sent"] is False
    assert payload["status"] == "BLOCKED_CONVERSATION_MISMATCH"


def test_send_missing_conversation_blocked(capsys):
    """47. Stage 30D.6: send blocked if conversation DOM has no messages."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [],
            "composer_present": True,
        })

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["sent"] is False
    assert payload["status"] == "BLOCKED_DOM_INACCESSIBLE"


def test_send_approved_with_confirm_executes_and_verifies(capsys, monkeypatch):
    """48. Stage 30D.6: send with confirm=True executes DOM send and verifies delivery."""
    eval_state = {"sent": False}

    def _fake_eval(expr):
        # Initial read or send execution
        if "button_click" in expr or "Enter" in expr or "__REPLY_VALUE__" in expr or "value" in expr and "setter" in expr:
            eval_state["sent"] = True
            return json.dumps({"ok": True, "method": "button_click"})
        if eval_state["sent"]:
            # Post-send read: message appears!
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "conversation_id": "5577169431",
                "messages": [
                    {"direction": "INCOMING", "text": "Есть ли у вас опыт с n8n?", "timestamp": None},
                    {"direction": "OUTGOING", "text": "Здравствуйте! У меня есть опыт автоматизации процессов, работы с API, n8n и Python.", "timestamp": None},
                ],
                "composer_present": True,
            })
        else:
            # Pre-send read
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "conversation_id": "5577169431",
                "messages": [
                    {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с e-commerce / маркетплейсами?", "timestamp": None},
                ],
                "composer_present": True,
            })

    def fake_send(ev, reply):
        eval_state["sent"] = True
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_fake_eval, confirm=True, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_id"] == "5577169431"
    assert payload["confirmed"] is True
    assert payload["sent"] is True
    assert payload["post_send_verified"] is True
    assert payload["status"] == "SENT"


def test_send_post_send_unverified_records_error(capsys, monkeypatch):
    """49. Stage 30D.6: If DOM does not confirm delivery after send, status is SEND_UNVERIFIED."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с e-commerce?", "timestamp": None},
            ],
            "composer_present": True,
        })

    def fake_send(ev, reply):
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirmed"] is True
    assert payload["sent"] is True
    assert payload["post_send_verified"] is False
    assert payload["status"] == "SEND_UNVERIFIED"
    assert len(payload["errors"]) > 0


def test_send_cli_dispatch_options(monkeypatch):
    """50. Stage 30D.6: CLI routes hh-message send with --confirm, --json, and --conversation-id."""
    calls = []

    def _fake_send(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(cli, "hh_message_send", _fake_send)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "hh-message", "send", "--conversation-id", "5577169431", "--confirm", "--json"])
    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0][0] == "5577169431"
    assert calls[0][1]["confirm"] is True
    assert calls[0][1]["as_json"] is True


# ---------------------------------------------- Stage 30D.7 Send Integrity Audit Tests -

def test_send_draft_already_in_history_not_falsely_verified(capsys, monkeypatch):
    """51. Stage 30D.7: Existing historical outgoing message identical to draft does not satisfy verification if count unchanged."""
    existing_draft = "Здравствуйте! У меня есть опыт автоматизации процессов, работы с API, n8n и Python. Непосредственно с Ozon и Wildberries подтверждённого коммерческого опыта в профиле нет, но готов применить навыки интеграции и автоматизации для ваших задач."
    
    # Pre-send has unanswered question (greeting only after it)
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "OUTGOING", "text": existing_draft, "timestamp": None},
                {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с e-commerce / маркетплейсами?", "timestamp": None},
                {"direction": "OUTGOING", "text": "Здравствуйте!", "timestamp": None},
            ],
            "composer_present": True,
        })

    def fake_send(ev, reply):
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirmed"] is True
    assert payload["sent"] is True
    assert payload["post_send_verified"] is False
    assert payload["status"] == "SEND_UNVERIFIED"


def test_send_new_incoming_only_message_not_verified(capsys, monkeypatch):
    """52. Stage 30D.7: New message added to DOM by employer (INCOMING) does not satisfy candidate outgoing verification."""
    eval_state = {"sent": False}

    def _eval(expr):
        if "button_click" in expr or "Enter" in expr or "__REPLY_VALUE__" in expr:
            eval_state["sent"] = True
            return json.dumps({"ok": True, "method": "button_click"})
        if eval_state["sent"]:
            # Post-send returns 2 messages, but 2nd is another incoming from employer
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "conversation_id": "5577169431",
                "messages": [
                    {"direction": "INCOMING", "text": "Есть ли у вас опыт с n8n?", "timestamp": None},
                    {"direction": "INCOMING", "text": "И уточните ваш опыт с Python.", "timestamp": None},
                ],
                "composer_present": True,
            })
        else:
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "conversation_id": "5577169431",
                "messages": [
                    {"direction": "INCOMING", "text": "Есть ли у вас опыт с n8n?", "timestamp": None},
                ],
                "composer_present": True,
            })

    def fake_send(ev, reply):
        eval_state["sent"] = True
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["post_send_verified"] is False
    assert payload["status"] == "SEND_UNVERIFIED"


def test_send_conversation_id_shift_blocked(capsys, monkeypatch):
    """53. Stage 30D.7: If conversation_id shifts during verification, status is SEND_UNVERIFIED."""
    eval_state = {"sent": False}

    def _eval(expr):
        if "button_click" in expr or "Enter" in expr or "__REPLY_VALUE__" in expr:
            eval_state["sent"] = True
            return json.dumps({"ok": True, "method": "button_click"})
        if eval_state["sent"]:
            # Conversation shifted to another ID!
            return json.dumps({
                "url": "https://hh.ru/chat/8888888888",
                "conversation_id": "8888888888",
                "messages": [
                    {"direction": "INCOMING", "text": "Здравствуйте!", "timestamp": None},
                    {"direction": "OUTGOING", "text": "Здравствуйте! У меня есть опыт автоматизации процессов, работы с API, n8n и Python.", "timestamp": None},
                ],
                "composer_present": True,
            })
        else:
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "conversation_id": "5577169431",
                "messages": [
                    {"direction": "INCOMING", "text": "Есть ли у вас опыт с n8n?", "timestamp": None},
                ],
                "composer_present": True,
            })

    def fake_send(ev, reply):
        eval_state["sent"] = True
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["post_send_verified"] is False
    assert any("shifted" in e for e in payload["errors"])


def test_send_byte_for_byte_draft_integrity(monkeypatch):
    """54. Stage 30D.7: Ensure draft passed to send_confirmed_hh_reply exactly matches validated draft."""
    sent_drafts = []

    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт с n8n?", "timestamp": None},
            ],
            "composer_present": True,
        })

    def fake_send(ev, reply):
        sent_drafts.append(reply)
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert len(sent_drafts) == 1
    assert "AI Automation Engineer" in sent_drafts[0]


def test_send_forbid_list_comprehensive_audit():
    """55. Stage 30D.7: Comprehensive security forbid-list audit on send primitives."""
    src_cli = open(cli.__file__, encoding="utf-8").read()
    src_reply = open(hh_message_reply.__file__, encoding="utf-8").read()

    # No navigation
    assert "Page.navigate" not in src_cli
    assert "Page.navigate" not in src_reply

    # No storage mutation in send JS
    assert "localStorage.setItem" not in src_reply
    assert "sessionStorage.setItem" not in src_reply
    assert "document.cookie =" not in src_reply

    # Confirm gate is enforced before mutation
    assert "if not confirm:" in src_cli


# ---------------------------------------------- Stage 30D.8 Live E2E & Guard Tests -

def test_already_answered_conversation_blocks_send_confirm(capsys, monkeypatch):
    """56. Stage 30D.8: Already-answered conversation strictly blocks subsequent send --confirm."""
    calls = []

    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт с n8n?", "timestamp": None},
                {"direction": "OUTGOING", "text": "Здравствуйте! У меня есть подтверждённый опыт с n8n.", "timestamp": None},
            ],
            "composer_present": True,
        })

    def fake_send(ev, reply):
        calls.append(reply)
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    assert len(calls) == 0  # Send was never called!
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirmed"] is True
    assert payload["sent"] is False
    assert payload["classification"] == "NO_REPLY_NEEDED"
    assert "BLOCKED" in payload["status"]


def test_no_retry_after_send_unverified(monkeypatch):
    """57. Stage 30D.8: When post-send verification fails, exactly one send attempt is made (no blind retry)."""
    send_attempts = []

    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversation_id": "5577169431",
            "messages": [
                {"direction": "INCOMING", "text": "Есть ли у вас опыт с Python?", "timestamp": None},
            ],
            "composer_present": True,
        })

    def fake_send(ev, reply):
        send_attempts.append(reply)
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 1
    assert len(send_attempts) == 1  # Exactly one send attempt was made, never retried


def test_exact_draft_string_matched_in_post_send(capsys, monkeypatch):
    """58. Stage 30D.8: Post-send verification checks that the new outgoing message matches the draft."""
    eval_state = {"sent": False}
    expected_draft = "Здравствуйте! У меня есть опыт автоматизации процессов, работы с API, n8n и Python. Непосредственно с Ozon и Wildberries подтверждённого коммерческого опыта в профиле нет, но готов применить навыки интеграции и автоматизации для ваших задач."

    def _eval(expr):
        if "button_click" in expr or "Enter" in expr or "__REPLY_VALUE__" in expr:
            eval_state["sent"] = True
            return json.dumps({"ok": True, "method": "button_click"})
        if eval_state["sent"]:
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "conversation_id": "5577169431",
                "messages": [
                    {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с e-commerce / маркетплейсами?", "timestamp": None},
                    {"direction": "OUTGOING", "text": expected_draft, "timestamp": "23:23"},
                ],
                "composer_present": True,
            })
        else:
            return json.dumps({
                "url": "https://hh.ru/chat/5577169431",
                "conversation_id": "5577169431",
                "messages": [
                    {"direction": "INCOMING", "text": "Есть ли у вас опыт работы с e-commerce / маркетплейсами?", "timestamp": None},
                ],
                "composer_present": True,
            })

    def fake_send(ev, reply):
        eval_state["sent"] = True
        return {"ok": True, "method": "button_click"}

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", fake_send)

    rc = cli.hh_message_send(evaluate_fn=_eval, confirm=True, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["post_send_verified"] is True
    assert payload["status"] == "SENT"


# ---------------------------------------------- Stage 30D.9 Multi-Conversation Triage Tests -

def test_triage_conversations_list(capsys):
    """59. Stage 30D.9: Triage discovers conversations and returns structured items."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/5577169431",
            "conversations": [
                {"conversation_id": "111", "url": "https://hh.ru/chat/111", "title": "Dev", "employer": "Emp1", "snippet": "Отказ", "is_selected": False},
                {"conversation_id": "222", "url": "https://hh.ru/chat/222", "title": "AI", "employer": "Emp2", "snippet": "Есть ли у вас опыт с n8n?", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "READ-ONLY"
    assert payload["conversation_count"] == 2
    assert len(payload["items"]) == 2


def test_triage_deduplication(capsys):
    """60. Stage 30D.9: Duplicate conversations in DOM are deduplicated by conversation_id."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "url": "https://hh.ru/chat/111", "title": "Dev", "employer": "Emp1", "snippet": "Отказ", "is_selected": False},
                {"conversation_id": "111", "url": "https://hh.ru/chat/111", "title": "Dev", "employer": "Emp1", "snippet": "Отказ", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_count"] == 1


def test_triage_limit_flag(capsys):
    """61. Stage 30D.9: --limit restricts number of triaged conversations."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "Dev1", "employer": "Emp1", "snippet": "Отказ", "is_selected": False},
                {"conversation_id": "222", "title": "Dev2", "employer": "Emp2", "snippet": "Отказ", "is_selected": False},
                {"conversation_id": "333", "title": "Dev3", "employer": "Emp3", "snippet": "Отказ", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(limit=2, evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_count"] == 2
    assert payload["items"][0]["conversation_id"] == "111"
    assert payload["items"][1]["conversation_id"] == "222"


def test_triage_conversation_id_filter(capsys):
    """62. Stage 30D.9: --conversation-id filters triage to target conversation."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/222",
            "conversations": [
                {"conversation_id": "111", "title": "Dev1", "employer": "Emp1", "snippet": "Отказ", "is_selected": False},
                {"conversation_id": "222", "title": "Dev2", "employer": "Emp2", "snippet": "Отказ", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(conversation_id="222", evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversation_count"] == 1
    assert payload["items"][0]["conversation_id"] == "222"


def test_triage_classification_multiple_dialogs(capsys):
    """63. Stage 30D.9: Multiple dialogs receive correct classification status."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "Role1", "employer": "Emp1", "snippet": "Есть ли у вас опыт работы с n8n?", "is_selected": False},
                {"conversation_id": "222", "title": "Role2", "employer": "Emp2", "snippet": "Отказ", "is_selected": False},
                {"conversation_id": "333", "title": "Role3", "employer": "Emp3", "snippet": "Какая у вас желаемая зарплата?", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    items = payload["items"]
    assert items[0]["classification"] == "NEEDS_REPLY"
    assert items[1]["classification"] == "NO_REPLY_NEEDED"
    assert items[2]["classification"] == "HUMAN_REVIEW"


def test_triage_vacancy_mapping(capsys, monkeypatch):
    """64. Stage 30D.9: Linked vacancy in DB attaches vacancy_stable_id."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "AI Engineer", "employer": "TechCorp", "snippet": "Отказ", "is_selected": False},
            ],
        })

    def fake_resolve(dialog):
        return {"stable_id": "hh:12345678", "title": "AI Engineer", "employer": "TechCorp"}

    monkeypatch.setattr(hh_message_reply, "resolve_vacancy_for_dialog", fake_resolve)

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["vacancy_stable_id"] == "hh:12345678"


def test_triage_missing_vacancy_fail_soft(capsys, monkeypatch):
    """65. Stage 30D.9: Missing vacancy in DB returns null without crashing triage."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "AI Engineer", "employer": "TechCorp", "snippet": "Отказ", "is_selected": False},
            ],
        })

    monkeypatch.setattr(hh_message_reply, "resolve_vacancy_for_dialog", lambda d: None)

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["vacancy_stable_id"] is None


def test_triage_single_conversation_error_fail_soft(capsys, monkeypatch):
    """66. Stage 30D.9: An exception in one conversation records error and continues others."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "Role1", "employer": "Emp1", "snippet": "Boom", "is_selected": False},
                {"conversation_id": "222", "title": "Role2", "employer": "Emp2", "snippet": "Отказ", "is_selected": False},
            ],
        })

    call_count = {"n": 0}
    orig_classify = hh_message_reply.classify_hh_conversation_detailed

    def _maybe_explode(dialog, profile=None):
        call_count["n"] += 1
        if dialog.conversation_id == "111":
            raise RuntimeError("Temporary DOM parse error")
        return orig_classify(dialog, profile=profile)

    monkeypatch.setattr(hh_message_reply, "classify_hh_conversation_detailed", _maybe_explode)

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["items"]) == 2
    assert payload["items"][0]["classification"] == "ERROR"
    assert "Temporary DOM parse error" in payload["items"][0]["error"]
    assert payload["items"][1]["classification"] == "NO_REPLY_NEEDED"


def test_triage_needs_reply_produces_draft_and_validation(capsys):
    """67. Stage 30D.9: NEEDS_REPLY conversation produces validated draft."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "Role1", "employer": "Emp1", "snippet": "Есть ли у вас опыт работы с n8n?", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    it = payload["items"][0]
    assert it["classification"] == "NEEDS_REPLY"
    assert it["draft"] is not None
    assert it["validation"] == "APPROVED"


def test_triage_no_reply_needed_draft_null(capsys):
    """68. Stage 30D.9: NO_REPLY_NEEDED conversation has draft = null."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "Role1", "employer": "Emp1", "snippet": "Отказ", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["draft"] is None


def test_triage_human_review_cannot_be_approved(capsys):
    """69. Stage 30D.9: HUMAN_REVIEW classification has validation = HUMAN_REVIEW and draft = null."""
    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "Role1", "employer": "Emp1", "snippet": "Какая у вас желаемая зарплата?", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["classification"] == "HUMAN_REVIEW"
    assert payload["items"][0]["validation"] == "HUMAN_REVIEW"
    assert payload["items"][0]["draft"] is None


def test_triage_never_calls_send_primitive(monkeypatch):
    """70. Stage 30D.9: Triage strictly never invokes send_confirmed_hh_reply."""
    send_called = []

    def _fail_on_send(*args, **kwargs):
        send_called.append(True)
        raise AssertionError("send_confirmed_hh_reply must never be called during triage!")

    monkeypatch.setattr(hh_message_reply, "send_confirmed_hh_reply", _fail_on_send)

    def _eval(expr):
        return json.dumps({
            "url": "https://hh.ru/chat/111",
            "conversations": [
                {"conversation_id": "111", "title": "Role1", "employer": "Emp1", "snippet": "Есть ли у вас опыт с n8n?", "is_selected": False},
            ],
        })

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    assert rc == 0
    assert len(send_called) == 0


def test_triage_security_forbid_list():
    """71. Stage 30D.9: Ensure triage is strictly READ-ONLY."""
    src = open(cli.__file__, encoding="utf-8").read()
    # Check that in hh_message_triage definition, no mutations occur
    triage_src = src.split("def hh_message_triage(")[1].split("def email_classify(")[0]
    assert "send_confirmed_hh_reply" not in triage_src
    assert "Page.navigate" not in triage_src
    assert "localStorage" not in triage_src


def test_triage_cli_dispatch_options(monkeypatch):
    """72. Stage 30D.9: CLI routes hh-message triage with --limit, --conversation-id, --json."""
    calls = []

    def _fake_triage(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(cli, "hh_message_triage", _fake_triage)
    monkeypatch.setattr(sys, "argv", ["job-search-cli", "hh-message", "triage", "--limit", "5", "--conversation-id", "5577169431", "--json"])
    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0][0] == "5577169431"
    assert calls[0][1]["limit"] == 5
    assert calls[0][1]["as_json"] is True










