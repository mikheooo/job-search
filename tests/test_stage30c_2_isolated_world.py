"""Stage 30C-2: HH message preview/classify via the chatik isolated world.

Covers:
  - the isolated-world frame selector (unit, no browser);
  - the isolated-world evaluate_fn seam + frame-not-found degrade path;
  - CLI preview and classify actually route to the isolated-world helper by
    default (NOT the main-frame make_cdp_evaluate) and still classify;
  - error path when the chatik iframe/world is absent (messages=[] -> preview
    returns 1 with 'no messages, nothing sent');
  - no send/submit primitives are present in, or reached by, the new code.

All browser I/O is faked via the injectable seams; nothing sends.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant import cli
from ai_assistant.prefill_execute import (
    ChatikFrameNotFound,
    make_isolated_world_evaluate,
    select_frame_id_by_url,
)


def _profile():
    return {
        "desired_roles": ["AI Automation Engineer"],
        "languages": ["en", "ru"],
        "remote_required": True,
    }


def _chatik_world_result(expr):
    """Simulate running _CONVERSATION_JS inside the chatik iframe world."""
    return json.dumps({
        "url": "https://chatik.hh.ru/chat/123",
        "title": "Чаты",
        "conversation_id": "123",
        "messages": [
            {"direction": "INCOMING", "text": "Здравствуйте! Всё ещё "
             "заинтересованы в обсуждении?", "sender": "Рекрутер"},
            {"direction": "OUTGOING", "text": "Да, конечно.", "sender": None},
        ],
        "composer_present": True,
    })


def _boom(*a, **k):
    raise AssertionError("forbidden send/submit OR main-frame path was reached")


# ----------------------------------------------- frame selector (pure, unit) --

def test_select_frame_id_prefers_chat_iframe():
    tree = {
        "frame": {"id": "root", "url": "https://hh.ru/account/messages"},
        "childFrames": [
            {"frame": {"id": "ad", "url": "https://tracker.example/px"},
             "childFrames": []},
            {"frame": {"id": "chat", "url": "https://chatik.hh.ru/chat/123"},
             "childFrames": []},
        ],
    }
    fid = select_frame_id_by_url(tree, ("chatik.hh.ru", "/chat/"))
    assert fid == "chat"


def test_select_frame_id_defaults_to_first_match_without_chat():
    tree = {
        "frame": {"id": "root", "url": "https://hh.ru/messaging"},
        "childFrames": [
            {"frame": {"id": "a", "url": "https://chatik.hh.ru/"},
             "childFrames": []},
        ],
    }
    assert select_frame_id_by_url(tree, ("chatik.hh.ru",)) == "a"


def test_select_frame_id_none_when_no_match():
    tree = {"frame": {"id": "root", "url": "https://hh.ru/messaging"},
            "childFrames": []}
    assert select_frame_id_by_url(tree, ("chatik.hh.ru", "/chat/")) is None


def test_select_frame_id_nested_deep_found():
    tree = {
        "frame": {"id": "root", "url": "https://hh.ru"},
        "childFrames": [
            {"frame": {"id": "mid", "url": "https://app.hh.ru"},
             "childFrames": [
                 {"frame": {"id": "deep", "url": "https://chatik.hh.ru/chat/9"},
                  "childFrames": []}]},
        ],
    }
    assert select_frame_id_by_url(tree, ("chatik.hh.ru", "/chat/")) == "deep"


# --------------------------------------- isolated-world transport (unit, seam) --

def test_make_isolated_world_evaluate_routes_through_seam(capsys):
    calls = []
    ev = make_isolated_world_evaluate(
        "http://127.0.0.1:9222", "hh.ru", ("chatik.hh.ru", "/chat/"),
        _run_in_frame=lambda expr: (calls.append(expr), _chatik_world_result(expr))[1])
    raw = ev("_CONVERSATION_JS")
    assert calls == ["_CONVERSATION_JS"]
    parsed = json.loads(raw)
    assert parsed["conversation_id"] == "123"
    assert parsed["messages"][0]["direction"] == "INCOMING"


def test_make_isolated_world_evaluate_frame_not_found_returns_empty():
    def _no_frame(expr):
        raise ChatikFrameNotFound("no chatik frame")
    ev = make_isolated_world_evaluate(
        "http://127.0.0.1:9222", "hh.ru", ("chatik.hh.ru",),
        _run_in_frame=_no_frame)
    raw = ev("_CONVERSATION_JS")
    parsed = json.loads(raw)
    assert parsed["messages"] == []
    assert parsed["conversation_id"] is None  # degrades, never raises


# ---------------- preview/classify route to isolated world, not main frame ---

def test_preview_uses_isolated_world_and_classifies(monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_cdp_evaluate", _boom)  # main frame must NOT be used
    isolated_calls = {}

    def fake_isolated(cdp_url, page_substring, frame_substrings, _run_in_frame=None):
        isolated_calls["frame_substrings"] = tuple(frame_substrings)
        return lambda expr: _chatik_world_result(expr)

    monkeypatch.setattr(cli, "make_isolated_world_evaluate", fake_isolated)
    rc = cli.hh_message_preview("123", profile=_profile())
    out = capsys.readouterr().out
    assert rc == 0
    assert "chatik.hh.ru" in isolated_calls.get("frame_substrings", ())
    assert "/chat/" in isolated_calls.get("frame_substrings", ())
    assert "classification:" in out
    assert "PREVIEW ONLY" in out and "nothing sent" in out


def test_classify_uses_isolated_world_and_classifies(monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_cdp_evaluate", _boom)
    monkeypatch.setattr(cli, "make_isolated_world_evaluate",
                        lambda *a, **k: lambda expr: _chatik_world_result(expr))
    rc = cli.hh_message_classify("123", profile=_profile())
    out = capsys.readouterr().out
    assert rc == 0
    assert "classification:" in out
    assert "REPLY_REQUIRED" in out or "classification:" in out
    assert "nothing sent" in out


def test_handlers_accept_injected_evaluate_fn(monkeypatch, capsys):
    # legacy contract: an explicitly injected evaluate_fn must still win and
    # must never be routed through the browser transport.
    monkeypatch.setattr(cli, "make_cdp_evaluate", _boom)
    monkeypatch.setattr(cli, "make_isolated_world_evaluate", _boom)

    def _injected(expr):
        return json.dumps({
            "conversation_id": "9", "messages": [
                {"direction": "INCOMING", "text": "Готовы обсудить?", "sender": "X"},
            ], "composer_present": True})
    rc = cli.hh_message_preview("9", evaluate_fn=_injected, profile=_profile())
    assert rc == 0
    rc2 = cli.hh_message_classify("9", evaluate_fn=_injected, profile=_profile())
    assert rc2 == 0
    assert "nothing sent" in capsys.readouterr().out


# ------------------------------------------------ error path: no iframe/world --

def test_preview_error_path_when_world_absent(monkeypatch, capsys):
    # chatik iframe not present -> isolated-world evaluate returns empty messages
    def _empty_world(cdp_url, page_substring, frame_substrings, _run_in_frame=None):
        return lambda expr: json.dumps({"conversation_id": None, "messages": []})

    monkeypatch.setattr(cli, "make_cdp_evaluate", _boom)
    monkeypatch.setattr(cli, "make_isolated_world_evaluate", _empty_world)
    rc = cli.hh_message_preview("123", profile=_profile())
    out = capsys.readouterr().out
    assert rc == 1
    assert "no messages" in out
    assert "nothing sent" in out


def test_classify_error_path_when_world_absent(monkeypatch, capsys):
    def _empty_world(cdp_url, page_substring, frame_substrings, _run_in_frame=None):
        return lambda expr: json.dumps({"conversation_id": None, "messages": []})

    monkeypatch.setattr(cli, "make_cdp_evaluate", _boom)
    monkeypatch.setattr(cli, "make_isolated_world_evaluate", _empty_world)
    rc = cli.hh_message_classify("123", profile=_profile())
    out = capsys.readouterr().out
    assert rc == 1
    assert "no messages" in out
    assert "nothing sent" in out


# ------------------------------------------------------- static no-send gates --

def test_isolated_world_helper_contains_no_mutation_primitives():
    src = open("ai_assistant/prefill_execute.py", encoding="utf-8").read()
    # Only the new isolated-world section, from the marker comment to EOF.
    marker = "# ---------- isolated-world transport (read-only, cross-origin iframe)"
    assert marker in src
    tail = src.split(marker, 1)[1]
    # Transport is allowed to open a websocket and call Runtime.evaluate, but
    # DOM-mutation and send primitives (clicks, value/checked writes, form
    # submits, Gmail/HH senders) must be absent from this read-only code.
    for forbidden in (".click(", "dispatchEvent", "value =", "checked =",
                      "innerText =", "submit", "send_auto_reply(",
                      "process_auto_reply(", "can_auto_send(",
                      "hh_submission", "gmail.send"):
        assert forbidden not in tail, f"{forbidden!r} present in isolated-world transport"


def test_preview_classify_do_not_reach_submit_or_auto_send_modules(monkeypatch):
    import ai_assistant.hh_message_reply as hmr
    monkeypatch.setattr(hmr, "send_auto_reply", _boom)
    monkeypatch.setattr(hmr, "process_auto_reply", _boom)
    monkeypatch.setattr(hmr, "can_auto_send", _boom)
    monkeypatch.setattr(cli, "make_isolated_world_evaluate",
                        lambda *a, **k: lambda expr: _chatik_world_result(expr))
    assert cli.hh_message_preview("123", evaluate_fn=lambda e: _chatik_world_result(e),
                                  profile=_profile()) == 0
    assert cli.hh_message_classify("123", evaluate_fn=lambda e: _chatik_world_result(e),
                                   profile=_profile()) == 0
    # The AUTO/send entry points were never invoked:
    # reaching them requires HH_AUTO_REPLY_ENABLED + live evaluate; the
    # monkeypatched _boom above can only fire if the handler calls them.
    # Explicitly assert the handler source never calls them:
    p_src = open(cli.__file__, encoding="utf-8").read()
    assert "send_auto_reply(" not in p_src.split("def hh_message_preview")[1].split("def email_list")[0]