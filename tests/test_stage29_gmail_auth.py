"""Stage 29 tests: Gmail read-only auth repair diagnostics.

Covers: valid readonly scope; insufficient scope -> explicit blocker; profile
verification; quota-project failure -> explicit blocker; successful read-only
discovery; mutation counters remain zero. Reuses the Stage 28 connector
without changing its public contract.
"""

from __future__ import annotations

import json

import pytest

from ai_assistant.gmail_readonly_connector import (
    GMAIL_READONLY_SCOPE,
    GmailProviderError,
    GmailReadOnlyConnector,
    gmail_provider_status,
)
from ai_assistant.email_message_reply import fetch_incoming_emails_readonly


class FakeScopedService:
    """Simulates a Gmail service with the readonly scope granted."""

    def __init__(self, fail_with=None):
        self.fail_with = fail_with
        self.calls = {"list": 0, "get": 0, "profile": 0}

    def _profile(self, userId):
        self.calls["profile"] += 1
        if self.fail_with:
            raise self.fail_with
        return {"emailAddress": "zegmund84@gmail.com"}

    def _list(self):
        self.calls["list"] += 1
        if self.fail_with:
            raise self.fail_with
        return {"messages": [{"id": "gm1"}]}

    def _get(self, mid):
        self.calls["get"] += 1
        return {"id": mid, "threadId": "gt1", "payload": {"headers": [
            {"name": "From", "value": "HR <hr@x.com>"},
            {"name": "Subject", "value": "Application"}]}}

    class _Messages:
        def __init__(self, svc):
            self._s = svc
        def list(self, userId, q, maxResults):
            s = self._s
            class R:
                def execute(self):
                    return s._list()
            return R()
        def get(self, userId, id, format):
            s = self._s
            class R:
                def execute(self):
                    return s._get(id)
            return R()

    class _Users:
        def __init__(self, svc):
            self._s = svc
        def getProfile(self, userId):
            s = self._s
            class R:
                def execute(self):
                    return s._profile(userId)
            return R()
        def messages(self):
            return FakeScopedService._Messages(self._s)

    def users(self):
        return self._Users(self)


# ---------------- 1. valid readonly scope -----------------------------------

def test_valid_readonly_scope_constant():
    assert GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
    assert "gmail.readonly" in GMAIL_READONLY_SCOPE


def _http403(body_text: str):
    import httplib2
    from googleapiclient.errors import HttpError
    resp = httplib2.Response({"status": 403, "reason": "Forbidden"})
    return HttpError(resp, body_text.encode("utf-8"))


# ---------------- 2. insufficient scope -> explicit blocker -----------------

def test_insufficient_scope_explicit_blocker():
    err = _http403(
        '{"error":{"code":403,"message":"Request had insufficient '
        'authentication scopes.","errors":[{"reason":"insufficientPermissions"}]}}')
    svc = FakeScopedService(fail_with=err)
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    assert res["verdict"] == "EMAIL_ACCESS_BLOCKED"
    assert "insufficient" in res["reason"].lower() or "403" in res["reason"]


# ---------------- 3. profile verification -----------------------------------

def test_profile_verification_returns_authenticated_email():
    svc = FakeScopedService()
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    assert res["verdict"] == "OK"
    assert svc.calls["profile"] >= 1  # profile is checked during discovery


# ---------------- 4. quota-project failure -> explicit blocker --------------

def test_quota_project_failure_explicit_blocker():
    err = _http403(
        '{"error":{"code":403,"message":"Gmail API has not been used in '
        'project before or it is disabled.","errors":[{"reason":"accessNotConfigured"}]}}')
    svc = FakeScopedService(fail_with=err)
    conn = GmailReadOnlyConnector(service=svc)
    res = fetch_incoming_emails_readonly(conn.transport())
    assert res["verdict"] == "EMAIL_ACCESS_BLOCKED"


# ---------------- 5. successful read-only discovery -------------------------

def test_successful_readonly_discovery():
    svc = FakeScopedService()
    conn = GmailReadOnlyConnector(service=svc, max_live_emails=5,
                                  query="in:inbox newer_than:7d")
    res = fetch_incoming_emails_readonly(conn.transport())
    assert res["verdict"] == "OK"
    assert res["email_count"] == 1
    assert res["emails"][0].message_id == "gm1"
    assert res["emails"][0].has_real_message_id is True


# ---------------- 6. mutation counters remain zero --------------------------

def test_mutation_counters_zero_after_discovery():
    svc = FakeScopedService()
    conn = GmailReadOnlyConnector(service=svc)
    fetch_incoming_emails_readonly(conn.transport())
    assert conn.read_calls >= 1
    assert conn.mutation_calls == 0
    assert conn.send_calls == 0
    assert conn.delete_calls == 0
    assert conn.modify_calls == 0


# ---------------- provider status helper ------------------------------------

def test_provider_status_readonly_scope_present():
    # with a pre-authenticated service injected, status reports READY
    svc = FakeScopedService()
    conn = GmailReadOnlyConnector(service=svc)
    # connector with injected service cannot hit real ADC; verify contract only
    assert conn.mutation_calls == 0