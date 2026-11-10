"""T7.29 §4 — the correspondence check reports an outcome, not a bare zero.

The defect pinned here: ``_process_messages`` returned ``messages_checked = 0``
for four different situations - the mode does not read correspondence at all,
there is no browser transport, the read failed, and the list was read and held
nothing. The operator could not tell them apart, and the worst of them (a failed
read) looked exactly like the best (a confirmed empty list).

The vocabulary is CHECKED / SKIPPED / BLOCKED / ERROR, and every return carries
the reason and the time of the check beside it. What is pinned:

1.  SEARCH_ONLY never reaches a transport at all.
2.  A missing transport is an explicit ERROR, not a zero.
3.  A read that raises is an explicit ERROR.
4.  A transport that answers with an error object is an ERROR, not an empty list.
5.  An empty list on a messages page, with a verified session, is a CONFIRMED zero.
6.  An empty list off a messages page is BLOCKED - nothing was read.
7.  An empty list with an unverifiable session is BLOCKED.
8.  Messages that really are read give the right counters and status.
9.  The status travels into the stored cycle row and into the report.
10. The diagnostic paths send nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from ai_assistant import autonomous_mode, config, db
from ai_assistant.hh_autonomous_agent import (
    MESSAGE_CHECK_STATUSES,
    MSG_CHECK_BLOCKED,
    MSG_CHECK_CHECKED,
    MSG_CHECK_ERROR,
    MSG_CHECK_SKIPPED,
    AutonomousConfig,
    AutonomousJobAgent,
    NotificationDispatcher,
    run_autonomous_cycle,
)

CONV_ID = "conv_t729"
VACANCY_ID = "139900001"
VACANCY_STABLE_ID = f"hh:{VACANCY_ID}"

MESSAGES_URL = "https://hh.ru/applicant/negotiations"
NON_MESSAGES_URL = "https://hh.ru/search/vacancy"

# Markers that identify which script the transport is being asked to run.
AUTH_JS_MARKER = "mainmenu_applicantProfile"   # in check_hh_session_authenticated
LIST_JS_MARKER = "chatik-open-chat-"           # in _CONVERSATIONS_LIST_JS
SEND_JS_MARKER = "chat-input"                  # the composer script


@pytest.fixture
def clean_db(tmp_path):
    original = config.DB_FILE
    config.DB_FILE = str(tmp_path / "test_t729_messages.db")
    db.init_db()
    yield config.DB_FILE
    config.DB_FILE = original


def _employer_dialog() -> dict:
    """One dialog whose last message is an employer question."""
    return {
        "conversation_id": CONV_ID,
        "vacancy_title": "AI Engineer",
        "vacancy_stable_id": VACANCY_STABLE_ID,
        "employer": "T729 Test Co",
        "messages": [
            {
                "message_id": "m1",
                "text": "Здравствуйте! Расскажите, какой у вас стек и опыт автоматизации?",
                "sender": "employer",
                "sent_at": "2026-09-25T16:00:00Z",
            }
        ],
    }


def _transport(*, url=MESSAGES_URL, authenticated=True, verified=True,
               conversations=None, raw_list=None):
    """A fake CDP transport that answers each script by its marker."""
    calls: list[str] = []

    def _evaluate(script: str) -> str:
        calls.append(script)
        if AUTH_JS_MARKER in script:
            return json.dumps({
                "authenticated": authenticated,
                "verified": verified,
                "reason": "test stand-in",
                "url": url,
            })
        if LIST_JS_MARKER in script:
            if raw_list is not None:
                return raw_list
            return json.dumps({"conversations": conversations or []})
        if SEND_JS_MARKER in script:
            return json.dumps({"ok": True, "conversation_id": CONV_ID, "verified_in_hh": True})
        raise AssertionError(f"unexpected script: {script[:120]}")

    return _evaluate, calls


def _agent(evaluate_fn, mode=autonomous_mode.FULL) -> AutonomousJobAgent:
    return AutonomousJobAgent(config=AutonomousConfig(evaluate_fn=evaluate_fn, mode=mode))


# ---------------------------------------------------------------------------
# 1. SEARCH_ONLY never reaches a transport
# ---------------------------------------------------------------------------

def test_search_only_never_reaches_a_transport(clean_db, monkeypatch):
    """SEARCH_ONLY is out of scope for correspondence, and says so without reading."""
    from ai_assistant import prefill_execute as pe

    reached: list[str] = []

    def _factory(cdp_url, url_substring):
        def _evaluate(script: str) -> str:
            reached.append(script)
            return "{}"

        return _evaluate

    monkeypatch.setattr(pe, "make_cdp_evaluate", _factory)

    agent = _agent(None, mode=autonomous_mode.SEARCH_ONLY)
    out = agent._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_SKIPPED
    assert out["messages_checked"] == 0
    assert out["messages_check_reason"]
    assert out["messages_checked_at"]
    # The mode check comes before the transport is even built.
    assert reached == []


# ---------------------------------------------------------------------------
# 2-4. Failure to obtain or perform the read is an ERROR, never a zero
# ---------------------------------------------------------------------------

def test_a_missing_transport_is_an_explicit_error(clean_db, monkeypatch):
    """No tab to attach to is an ERROR with a reason - not 'no messages'."""
    from ai_assistant import prefill_execute as pe

    def _factory(cdp_url, url_substring):
        raise RuntimeError("no open tab matching 'hh.ru' on http://127.0.0.1:9111")

    monkeypatch.setattr(pe, "make_cdp_evaluate", _factory)

    agent = _agent(None)
    out = agent._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_ERROR
    assert out["messages_checked"] == 0
    assert "no browser transport" in out["messages_check_reason"]
    assert "no open tab" in out["messages_check_reason"]


def test_a_read_that_raises_is_an_explicit_error(clean_db):
    """A transport that throws is reported, not swallowed into a zero."""
    def _evaluate(script: str) -> str:
        raise RuntimeError("websocket closed")

    out = _agent(_evaluate)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_ERROR
    assert out["messages_checked"] == 0
    assert "dialog list read failed" in out["messages_check_reason"]
    assert "websocket closed" in out["messages_check_reason"]


def test_a_transport_error_object_is_not_an_empty_list(clean_db):
    """``fetch_hh_conversations_list_readonly`` answers unparseable output with
    an ``error`` key. That is an ERROR, not a confirmed zero."""
    evaluate_fn, _ = _transport(raw_list="<html>login</html>")

    out = _agent(evaluate_fn)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_ERROR
    assert out["messages_checked"] == 0
    assert "dialog list read failed" in out["messages_check_reason"]


# ---------------------------------------------------------------------------
# 5-7. When is a zero a *confirmed* zero?
# ---------------------------------------------------------------------------

def test_an_empty_list_on_a_messages_page_is_a_confirmed_zero(clean_db):
    """Read succeeded, session verified, page is the messages surface: 0 is real."""
    evaluate_fn, calls = _transport(url=MESSAGES_URL, conversations=[])

    out = _agent(evaluate_fn)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_CHECKED
    assert out["messages_checked"] == 0
    assert "0 dialog" in out["messages_check_reason"]
    # The session question really was asked before the zero was accepted.
    assert any(AUTH_JS_MARKER in s for s in calls)


def test_an_empty_list_off_a_messages_page_is_blocked(clean_db):
    """Nothing was read, so nothing may be reported as 'no new messages'."""
    evaluate_fn, _ = _transport(url=NON_MESSAGES_URL, conversations=[])

    out = _agent(evaluate_fn)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_BLOCKED
    assert out["messages_checked"] == 0
    assert "not a messages surface" in out["messages_check_reason"]
    assert NON_MESSAGES_URL in out["messages_check_reason"]


def test_empty_chat_url_on_another_domain_is_blocked(clean_db):
    url = 'https://other.example/chat'
    payload = json.dumps({'url': url, 'conversations': [], 'empty_evidence': 'no_unread_chats'})
    evaluate_fn, _ = _transport(url=url, raw_list=payload)
    out = _agent(evaluate_fn)._process_messages()
    assert out['messages_check_status'] == MSG_CHECK_BLOCKED
    assert 'not on HH' in out['messages_check_reason']


def test_an_empty_list_with_an_unauthenticated_session_is_blocked(clean_db):
    evaluate_fn, _ = _transport(url=MESSAGES_URL, conversations=[],
                                authenticated=False, verified=True)

    out = _agent(evaluate_fn)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_BLOCKED
    assert out["messages_checked"] == 0
    assert "not authenticated" in out["messages_check_reason"]


def test_an_unverifiable_session_blocks_rather_than_passes(clean_db):
    """T7.28 A: 'cannot tell' must not read as 'fine'."""
    evaluate_fn, _ = _transport(url=MESSAGES_URL, conversations=[],
                                authenticated=False, verified=False)

    out = _agent(evaluate_fn)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_BLOCKED
    assert "could not verify" in out["messages_check_reason"]


def test_unread_filter_explicit_empty_state_is_a_confirmed_check(clean_db):
    url = 'https://hh.ru/chat'
    payload = json.dumps({'url': url, 'conversations': [], 'empty_evidence': 'no_unread_chats'})
    evaluate_fn, _ = _transport(url=url, raw_list=payload)
    out = _agent(evaluate_fn)._process_messages()
    assert out['messages_check_status'] == MSG_CHECK_CHECKED
    assert out['messages_checked'] == 0
    assert out['messages_check_reason'] == 'no unread chats; unread-only filter active'


@pytest.mark.parametrize('payload', [
    {'url': 'https://hh.ru/chat', 'conversations': []},
    {'url': 'https://other.example/chat', 'conversations': [], 'empty_evidence': 'no_unread_chats'},
    {'url': 'https://hh.ru/search/vacancy', 'conversations': [], 'empty_evidence': 'no_unread_chats'},
])
def test_empty_chat_without_matching_evidence_is_blocked(clean_db, payload):
    evaluate_fn, _ = _transport(url='https://hh.ru/chat', raw_list=json.dumps(payload))
    out = _agent(evaluate_fn)._process_messages()
    assert out['messages_check_status'] == MSG_CHECK_BLOCKED
    assert 'empty HH chat list could not be verified' in out['messages_check_reason']


@pytest.mark.parametrize('ready,checked,title,expected', [
    ('complete', True, 'Нет непрочитанных чатов', 'no_unread_chats'),
    ('loading', True, 'Нет непрочитанных чатов', None),
    ('complete', False, 'Нет непрочитанных чатов', None),
    ('complete', True, 'Загрузка', None),
])
def test_unread_empty_dom_evidence_requires_all_markers(ready, checked, title, expected):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed to execute the chat-list JavaScript')
    from ai_assistant.hh_message_reply import _conversations_list_js
    harness = '''const input=JSON.parse(process.argv[1]);
global.location={hostname:'hh.ru',pathname:'/chat',href:'https://hh.ru/chat'};
global.document={title:'Chat',readyState:input.ready,
 querySelector:(s)=>s.includes('only-unread')?{checked:input.checked}:s.includes('chatik-layout')?{}:null,
 querySelectorAll:(s)=>s.includes('[data-qa="title"]')?[{innerText:input.title,getClientRects:()=>[{}]}]:[]};
const extract=EXTRACTOR; console.log(extract());'''.replace('EXTRACTOR', _conversations_list_js())
    payload = json.dumps({'ready': ready, 'checked': checked, 'title': title}, ensure_ascii=True)
    result = subprocess.run([node, '-e', harness, payload], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)['empty_evidence'] == expected


# ---------------------------------------------------------------------------
# 8. Messages that are read give the right counters and status
# ---------------------------------------------------------------------------

def test_messages_that_are_read_give_the_right_counters(clean_db):
    """A real read is CHECKED, counted, and still passes through the chat guard."""
    evaluate_fn, _ = _transport(conversations=[_employer_dialog()])

    agent = _agent(evaluate_fn)
    with patch.object(NotificationDispatcher, "notify_reply_sent"), \
         patch('ai_assistant.hh_autonomous_agent._incoming_message_still_current', return_value=True), \
         patch('ai_assistant.hh_llm_reply.generate_contextual_draft', return_value={
             'status': 'DRAFT', 'reply': 'I use Python and automation in my projects.', 'reason': 'test'
         }):
        out = agent._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_CHECKED
    assert out["messages_checked"] == 1
    assert "1 dialog" in out["messages_check_reason"]
    assert out["auto_replies_count"] == 1


def test_the_status_is_always_one_of_the_four_words(clean_db):
    """The vocabulary is closed, so a caller can switch on it."""
    assert set(MESSAGE_CHECK_STATUSES) == {
        MSG_CHECK_CHECKED, MSG_CHECK_SKIPPED, MSG_CHECK_BLOCKED, MSG_CHECK_ERROR
    }

    evaluate_fn, _ = _transport(conversations=[])
    out = _agent(evaluate_fn)._process_messages()
    assert out["messages_check_status"] in MESSAGE_CHECK_STATUSES


# ---------------------------------------------------------------------------
# 9. The status travels into the report and into the stored cycle row
# ---------------------------------------------------------------------------

def _canned_search_report(queries) -> dict:
    return {
        "busy": False,
        "start": {"ok": True, "status": "started", "message": ""},
        "unique_count": 0,
        "read_count": 0,
        "queries": [
            {"query": q, "result_count": 0, "unique_count": 0, "status": "ok"}
            for q in queries
        ],
        "vacancies": [],
    }


def test_the_status_travels_into_the_stored_cycle_row(clean_db, monkeypatch):
    """A stored cycle row must say WHY the correspondence count is what it is."""
    from ai_assistant import hh_readonly_operation as ro

    monkeypatch.setattr(ro, "search_operation_report",
                        lambda queries, **kw: _canned_search_report(queries))

    res = run_autonomous_cycle(config=AutonomousConfig(mode=autonomous_mode.SEARCH_ONLY))

    assert res.messages_check_status == MSG_CHECK_SKIPPED
    assert res.messages_checked == 0
    assert res.messages_checked_at
    # ...in the report...
    assert MSG_CHECK_SKIPPED in res.summary
    assert "Correspondence" in res.summary

    # ...and in the persisted row, both as columns and in the log dump.
    rows = db.list_autonomous_cycle_runs(limit=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["messages_checked"] == 0
    assert row["log"]["messages_check_status"] == MSG_CHECK_SKIPPED
    assert row["log"]["messages_check_reason"]
    assert row["log"]["messages_checked_at"]


# ---------------------------------------------------------------------------
# 10. The diagnostic paths send nothing
# ---------------------------------------------------------------------------

def test_a_blocked_check_writes_nothing_outward(clean_db):
    """BLOCKED means nothing was read, so nothing may be written either."""
    evaluate_fn, _ = _transport(url=NON_MESSAGES_URL, conversations=[])

    out = _agent(evaluate_fn)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_BLOCKED
    assert out["auto_replies_count"] == 0
    assert out["interviews_detected"] == 0
    assert out["notifications_sent"] == []
    assert db.list_hh_message_events(limit=100) == []
    assert db.list_conversation_audits(limit=100) == []


def test_an_error_check_writes_nothing_outward(clean_db):
    def _evaluate(script: str) -> str:
        raise RuntimeError("websocket closed")

    out = _agent(_evaluate)._process_messages()

    assert out["messages_check_status"] == MSG_CHECK_ERROR
    assert out["auto_replies_count"] == 0
    assert out["notifications_sent"] == []
    assert db.list_hh_message_events(limit=100) == []
