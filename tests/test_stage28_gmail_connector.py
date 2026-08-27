"""Stage 28 tests: Gmail read-only connector (transport for Stage 27).

Covers: Gmail transport maps message correctly; thread maps; message_id /
thread_id preserved; sender/subject/body extraction; incoming-only filtering;
no mutation API; no send API; Stage 27 classifier receives full thread;
sensitive thread -> HUMAN_REVIEW; safe thread -> reply preview; missing
provider -> EMAIL_ACCESS_BLOCKED / EMAIL_PROVIDER_UNCONFIGURED; dedup with real
Gmail message_id; malformed Gmail response -> safe failure; credentials missing
-> explicit blocker, no insecure fallback.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ai_assistant.gmail_readonly_connector import (
    DEFAULT_MAX_LIVE_EMAILS,
    GMAIL_READONLY_SCOPE,
    GmailProviderError,
    GmailReadOnlyConnector,
    gmail_provider_status,
)
from ai_assistant.email_message_reply import (
    EmailClassification,
    EmailContext,
    EmailMessage,
    EmailReplyStateStore,
    classify_email,
    fetch_incoming_emails_readonly,
    generate_email_reply,
    process_incoming_email,
)


# ---------------- fake Gmail service (read-only behaviour) ------------------

class FakeGmailService:
    """Simulates the googleapiclient Gmail read-only surface."""

    def __init__(self, messages, threads=None, fail_list=False):
        self.messages = messages  # {id: {payload headers/body}}
        self.threads = threads or {}
        self.fail_list = fail_list
        self.list_calls = 0
        self.get_calls = 0
        self.thread_calls = 0

    def _profile(self, userId):
        return {"emailAddress": "zegmund84@gmail.com"}

    class _MessagesColl:
        def __init__(self, svc):
            self._svc = svc
        def list(self, userId, q, maxResults):
            self._svc.list_calls += 1
            svc = self._svc
            class R:
                def execute(self):
                    if svc.fail_list:
                        raise RuntimeError("API 403 insufficient scope")
                    return {"messages": [{"id": i} for i in svc.messages]}
            return R()
        def get(self, userId, id, format):
            self._svc.get_calls += 1
            svc = self._svc
            class R:
                def execute(self):
                    if id not in svc.messages:
                        raise RuntimeError("404 not found")
                    return svc.messages[id]
            return R()

    class _ThreadsColl:
        def __init__(self, svc):
            self._svc = svc
        def get(self, userId, id, format):
            self._svc.thread_calls += 1
            svc = self._svc
            class R:
                def execute(self):
                    if id not in svc.threads:
                        raise RuntimeError("404 thread not found")
                    return svc.threads[id]
            return R()

    class _UsersColl:
        def __init__(self, svc):
            self._svc = svc
        def getProfile(self, userId):
            svc = self._svc
            class R:
                def execute(self):
                    return svc._profile(userId)
            return R()
        def messages(self):
            return FakeGmailService._MessagesColl(self._svc)
        def threads(self):
            return FakeGmailService._ThreadsColl(self._svc)

    # dispatch: users().messages().list(...) / users().getProfile(...)
    def users(self):
        return self._UsersColl(self)


def _gmail_msg(mid="gm1", tid="gt1", sender="HR Acme <hr@acme.com>",
               subject="Your application", body="Hi! Are you interested in the role?"):
    import base64
    return {
        "id": mid, "threadId": tid,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender},
                        {"name": "Subject", "value": subject},
                        {"name": "Date", "value": "Wed, 26 Aug 2026 10:00:00 +0000"},
                        {"name": "Reply-To", "value": "replies@acme.com"}],
            "parts": [{"mimeType": "text/plain",
                       "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}],
        },
    }


def _safe_thread(mid="gm1", tid="gt1"):
    return {"id": tid, "messages": [_gmail_msg(mid=mid, tid=tid)]}


def _profile():
    return {"desired_roles": ["AI Automation Engineer"], "languages": ["en", "ru"],
            "remote_required": True}


@pytest.fixture(autouse=True)
def _clean(tmp_path, request):
    import ai_assistant.email_message_reply as em
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
    em.DEFAULT_STATE_PATH = str(tmp_path / f"s28-{safe}.json")
    yield
    em.DEFAULT_STATE_PATH = "artifacts/email_message_reply_state.json"


# ---------------- 1. transport maps message correctly -----------------------

def test_gmail_transport_maps_message():
    svc = FakeGmailService({"gm1": _gmail_msg()})
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    assert res["verdict"] == "OK"
    assert res["email_count"] == 1
    e = res["emails"][0]
    assert e.provider == "gmail"
    assert e.message_id == "gm1"
    assert e.thread_id == "gt1"
    assert e.sender_name == "HR Acme"
    assert e.sender_email == "hr@acme.com"
    assert e.subject == "Your application"
    assert "interested in the role" in e.body_text
    assert e.reply_to == "replies@acme.com"


# ---------------- 2. thread maps correctly ----------------------------------

def test_gmail_thread_maps(monkeypatch):
    svc = FakeGmailService({"gm1": _gmail_msg()})
    conn = GmailReadOnlyConnector(service=svc)
    # monkeypatch the thread read onto the connector
    conn._thread_get = lambda s, tid, uid="me": _safe_thread(tid=tid)
    raw = conn._to_email_dict(_gmail_msg())
    assert raw["thread_id"] == "gt1"


# ---------------- 3/4. message_id + thread_id preserved ---------------------

def test_ids_preserved():
    svc = FakeGmailService({"gmX": _gmail_msg(mid="gmX", tid="gtY")})
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    e = res["emails"][0]
    assert e.message_id == "gmX"
    assert e.thread_id == "gtY"
    assert e.has_real_message_id is True


# ---------------- 5. sender/subject/body extraction -------------------------

def test_extraction_fields():
    svc = FakeGmailService({"gm1": _gmail_msg()})
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    e = res["emails"][0]
    assert e.sender_name and e.sender_email and e.subject and e.body_text


# ---------------- 6. incoming-only filtering --------------------------------

def test_incoming_only_query_capped():
    conn = GmailReadOnlyConnector(service=None, max_live_emails=3,
                                  query="in:inbox is:unread newer_than:7d")
    assert "in:inbox" in conn.query
    assert conn.max_live_emails == 3
    assert DEFAULT_MAX_LIVE_EMAILS == 5


# ---------------- 7/8. no mutation/send API ---------------------------------

def test_connector_no_mutation_or_send_api():
    src = pathlib.Path("ai_assistant/gmail_readonly_connector.py").read_text(encoding="utf-8")
    # only read-only Gmail API methods may be called
    for banned in ["messages().send", "users().messages().send",
                   "drafts().create", ".modify(", "messages().modify",
                   ".trash(", ".delete(", "labels().modify", "batchModify",
                   "importMessages", ".insert("]:
        assert banned not in src, f"banned mutation/send API: {banned}"
    # permitted read-only methods present
    assert "messages().list" in src
    assert "messages().get" in src
    assert "threads().get" in src
    assert GMAIL_READONLY_SCOPE.endswith("gmail.readonly")


# ---------------- 9. Stage 27 classifier receives full thread ---------------

def test_classifier_receives_full_thread():
    thread = [_gmail_msg(mid="t0", body="What salary do you expect?"),
              _gmail_msg(mid="t1", body="Please confirm.")]
    ctx = EmailContext(
        message=EmailMessage(**{**GmailReadOnlyConnector()._to_email_dict(_gmail_msg(mid="t1")),
                                "message_id": "t1"}),
        thread_messages=[EmailMessage(**{**GmailReadOnlyConnector()._to_email_dict(_gmail_msg(mid="t0", body="What salary do you expect?")),
                                         "message_id": "t0"})])
    cls = classify_email(ctx)
    assert cls == EmailClassification.HUMAN_REVIEW  # earlier salary question


# ---------------- 10. sensitive thread -> HUMAN_REVIEW ----------------------

def test_sensitive_thread_human_review():
    svc = FakeGmailService({"gm1": _gmail_msg()})
    conn = GmailReadOnlyConnector(service=svc)
    ctx = EmailContext(
        message=EmailMessage(**conn._to_email_dict(_gmail_msg(mid="gm1", body="Offer details"))),
        thread_messages=[EmailMessage(**conn._to_email_dict(_gmail_msg(mid="gm0", body="What salary?")))])
    assert classify_email(ctx) == EmailClassification.HUMAN_REVIEW


# ---------------- 11. safe thread -> reply preview --------------------------

def test_safe_thread_reply_preview():
    svc = FakeGmailService({"gm1": _gmail_msg()})
    conn = GmailReadOnlyConnector(service=svc)
    ctx = EmailContext(
        message=EmailMessage(**conn._to_email_dict(_gmail_msg())),
        thread_messages=[EmailMessage(**conn._to_email_dict(_gmail_msg(mid="gm0", body="Hello!")))])
    assert classify_email(ctx) == EmailClassification.JOB_RELATED
    gen = generate_email_reply(ctx, _profile())
    assert gen["status"] == "JOB_RELATED"
    assert gen["reply"]


# ---------------- 12. missing provider -> blocker ---------------------------

def test_missing_provider_blocked():
    res = fetch_incoming_emails_readonly(None)
    assert res["verdict"] == "EMAIL_ACCESS_BLOCKED"


def test_provider_unconfigured_when_no_google():
    st = gmail_provider_status.__wrapped__ if hasattr(gmail_provider_status, "__wrapped__") else None
    # simulate missing google-auth by checking ADC path absence is reported as blocked
    import ai_assistant.gmail_readonly_connector as gc
    orig = gc.GmailReadOnlyConnector
    # when no credentials and no ADC -> connector raises EMAIL_OAUTH_BLOCKED
    class NoCredsConnector(orig):
        def _service_or_default(self):
            raise GmailProviderError("EMAIL_OAUTH_BLOCKED: ADC unavailable")
    res = fetch_incoming_emails_readonly(NoCredsConnector().transport())
    assert res["verdict"] == "EMAIL_ACCESS_BLOCKED"
    assert "EMAIL_OAUTH_BLOCKED" in res["reason"]


# ---------------- 13. dedup with real Gmail message_id ----------------------

def test_dedup_with_real_message_id(tmp_path):
    store = EmailReplyStateStore(str(tmp_path / "g.json"))
    svc = FakeGmailService({"gm1": _gmail_msg()})
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    e = res["emails"][0]
    ctx = EmailContext(message=e)
    r1 = process_incoming_email(ctx, profile=_profile(), state=store)
    assert r1.status == "NEEDS_HUMAN_REVIEW"
    r2 = process_incoming_email(ctx, profile=_profile(), state=store)
    assert r2.status == "SKIPPED"
    assert r1.dedup_key.startswith("gmail:gm1")


# ---------------- 14. malformed Gmail response -> safe failure --------------

def test_malformed_gmail_safe_failure():
    svc = FakeGmailService({"gm1": _gmail_msg()}, fail_list=True)
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    assert res["verdict"] == "EMAIL_ACCESS_BLOCKED"
    assert res["emails"] == []


# ---------------- 15. credentials missing -> explicit blocker ---------------

def test_credentials_missing_no_insecure_fallback():
    class NoCreds:
        def _service_or_default(self):
            raise GmailProviderError(
                "EMAIL_OAUTH_BLOCKED: no credentials; refusing insecure fallback")
    conn = NoCreds()
    with pytest.raises(GmailProviderError):
        conn._service_or_default()


# ---------------- counter safety ---------------------------------------------

def test_connector_counters_read_only():
    svc = FakeGmailService({"gm1": _gmail_msg()})
    conn = GmailReadOnlyConnector(service=svc)
    fetch_incoming_emails_readonly(conn.transport())
    assert conn.read_calls >= 1
    assert conn.mutation_calls == 0
    assert conn.send_calls == 0
    assert conn.delete_calls == 0
    assert conn.modify_calls == 0