"""Stage 27 tests: Email Message Reply MVP (REVIEW-only).

Covers: recruiter email -> JOB_RELATED/reply; system notification -> NO_REPLY;
sensitive salary -> HUMAN_REVIEW; missing truth -> HUMAN_REVIEW; ambiguous
vacancy -> HUMAN_REVIEW; thread context; provider message_id dedup; duplicate ->
SKIPPED; missing message_id -> safe fallback; truth-only reply; EmailSendGate
blocks; send_action_count == 0; unknown sender -> HUMAN_REVIEW; malformed
transport -> safe failure; no email access -> EMAIL_ACCESS_BLOCKED.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ai_assistant.email_message_reply import (
    EmailClassification,
    EmailContext,
    EmailMessage,
    EmailProvider,
    EmailReplyStateStore,
    EmailSendGate,
    classify_email,
    fetch_incoming_emails_readonly,
    generate_email_reply,
    process_incoming_email,
)


def _msg(mid="m1", tid="t1", sender="hr@acme.com", name="Acme HR",
         subject="Your application", body="Hi! Are you interested in the role?",
         provider=EmailProvider.API.value):
    return EmailMessage(message_id=mid, thread_id=tid, provider=provider,
                        sender_name=name, sender_email=sender, subject=subject,
                        body_text=body, has_real_message_id=bool(mid))


def _ctx(msg=None, thread=None):
    m = msg if msg is not None else _msg()
    return EmailContext(message=m, thread_messages=thread or [])


def _profile():
    return {"desired_roles": ["AI Automation Engineer"], "languages": ["en", "ru"],
            "remote_required": True}


@pytest.fixture(autouse=True)
def _clean(tmp_path, request):
    import ai_assistant.email_message_reply as em
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
    em.DEFAULT_STATE_PATH = str(tmp_path / f"s27-{safe}.json")
    yield
    em.DEFAULT_STATE_PATH = "artifacts/email_message_reply_state.json"


# ---------------- 1. recruiter email -> JOB_RELATED + reply -----------------

def test_recruiter_email_job_related_and_reply():
    rep = process_incoming_email(_ctx(), profile=_profile())
    assert rep.classification == EmailClassification.JOB_RELATED.value
    assert rep.generated_reply
    assert rep.status == "NEEDS_HUMAN_REVIEW"
    assert rep.send_action_count == 0
    assert rep.linked_company == "Acme HR"


# ---------------- 2. system notification -> NO_REPLY ------------------------

@pytest.mark.parametrize("sender,subject", [
    ("no-reply@hh.ru", "Ваш отклик получен"),
    ("notifications@job.ru", "Подтверждение подписки"),
])
def test_system_notification_no_reply(sender, subject):
    rep = process_incoming_email(_ctx(msg=_msg(mid="s", sender=sender, subject=subject)),
                                 profile=_profile())
    assert rep.classification == EmailClassification.NO_REPLY.value
    assert rep.generated_reply == ""


# ---------------- 3. sensitive salary -> HUMAN_REVIEW ------------------------

def test_sensitive_salary_human_review():
    rep = process_incoming_email(
        _ctx(msg=_msg(subject="Question", body="What is your expected salary?")),
        profile=_profile())
    assert rep.classification == EmailClassification.HUMAN_REVIEW.value
    assert rep.generated_reply == ""


# ---------------- 4. missing truth -> HUMAN_REVIEW ---------------------------

def test_missing_truth_human_review():
    rep = process_incoming_email(_ctx(), profile={})  # empty profile
    assert rep.classification == EmailClassification.HUMAN_REVIEW.value
    assert rep.generated_reply == ""


# ---------------- 5. ambiguous vacancy -> HUMAN_REVIEW -----------------------

def test_ambiguous_vacancy_human_review():
    # unknown sender (no name/domain) -> ambiguous linkage
    rep = process_incoming_email(_ctx(msg=_msg(mid="x", sender="", name="")),
                                 profile=_profile())
    assert rep.linked_company == ""
    assert rep.classification in (EmailClassification.HUMAN_REVIEW.value,
                                  EmailClassification.UNKNOWN.value)


# ---------------- 6. thread context used -------------------------------------

def test_thread_context_used():
    # earlier thread message asks a sensitive fact -> full context -> HUMAN_REVIEW
    thread = [_msg(mid="t0", subject="Re: salary?", body="What salary do you want?")]
    rep = process_incoming_email(
        _ctx(msg=_msg(mid="t1", subject="Re: salary?", body="Please confirm."),
             thread=thread), profile=_profile())
    assert rep.classification == EmailClassification.HUMAN_REVIEW.value


# ---------------- 7. provider message_id for dedup ---------------------------

def test_provider_message_id_dedup(tmp_path):
    store = EmailReplyStateStore(str(tmp_path / "d.json"))
    r1 = process_incoming_email(_ctx(), profile=_profile(), state=store)
    assert r1.status == "NEEDS_HUMAN_REVIEW"
    r2 = process_incoming_email(_ctx(), profile=_profile(), state=store)
    assert r2.status == "SKIPPED"
    assert r2.classification == "ALREADY_PROCESSED"


# ---------------- 8. duplicate message -> SKIPPED ----------------------------

def test_duplicate_message_skipped(tmp_path):
    store = EmailReplyStateStore(str(tmp_path / "dd.json"))
    process_incoming_email(_ctx(), profile=_profile(), state=store)
    rep2 = process_incoming_email(_ctx(), profile=_profile(), state=store)
    assert rep2.status == "SKIPPED"
    assert rep2.send_action_count == 0


# ---------------- 9. missing message_id -> safe fallback ---------------------

def test_missing_message_id_safe_fallback(tmp_path):
    msg = _msg(mid=None)  # no provider id
    assert msg.has_real_message_id is False
    assert msg.display_key().startswith(f"{EmailProvider.API.value}:fb:")
    store = EmailReplyStateStore(str(tmp_path / "fb.json"))
    r1 = process_incoming_email(_ctx(msg=msg), profile=_profile(), state=store)
    r2 = process_incoming_email(_ctx(msg=msg), profile=_profile(), state=store)
    assert r2.status == "SKIPPED"  # fallback key still dedups consistently


# ---------------- 10. truth-only reply ---------------------------------------

def test_truth_only_reply_no_invented_facts():
    rep = process_incoming_email(_ctx(), profile=_profile())
    low = rep.generated_reply.lower()
    for forbidden in ("5 лет", "3 года", "1500", "100 000", "python", "llm",
                      "telegram", "гермес", "hermes", "интервью"):
        assert forbidden not in low


# ---------------- 11. EmailSendGate blocks send ------------------------------

def test_email_send_gate_blocks():
    gate = EmailSendGate()
    res = gate.send_email(_ctx(), "reply")
    assert res == {"ok": False, "blocked": True,
                   "reason": "EMAIL_REVIEW_ONLY_SEND_BLOCKED",
                   "send_action_count": 0}


# ---------------- 12. send_action_count == 0 everywhere ----------------------

def test_send_action_count_zero_all_paths(tmp_path):
    store = EmailReplyStateStore(str(tmp_path / "z.json"))
    for ctx in [_ctx(), _ctx(msg=_msg(subject="Spam", body="offer")),
                _ctx(msg=_msg(mid="z2", sender="noreply@x.ru", subject="Ваш пароль"))]:
        rep = process_incoming_email(ctx, profile=_profile(), state=store)
        assert rep.send_action_count == 0


# ---------------- 13. unknown sender -> HUMAN_REVIEW -------------------------

def test_unknown_sender_human_review():
    rep = process_incoming_email(_ctx(msg=_msg(mid="u", sender="", name="",
                                               subject="", body="???")),
                                 profile=_profile())
    assert rep.classification in (EmailClassification.HUMAN_REVIEW.value,
                                  EmailClassification.UNKNOWN.value)


# ---------------- 14. malformed/provider error -> safe failure ---------------

def test_malformed_transport_safe_failure():
    def broken():
        raise RuntimeError("imap auth failed")
    res = fetch_incoming_emails_readonly(broken)
    assert res["verdict"] == "EMAIL_ACCESS_BLOCKED"
    assert res["emails"] == []


# ---------------- 15. no email access -> EMAIL_ACCESS_BLOCKED ----------------

def test_no_email_access_blocked():
    res = fetch_incoming_emails_readonly(None)
    assert res["verdict"] == "EMAIL_ACCESS_BLOCKED"
    assert "no email provider/transport configured" in res["reason"]


# ---------------- module safety: no send/transport primitives ---------------

def test_module_no_email_send_or_transport_primitive():
    src = pathlib.Path("ai_assistant/email_message_reply.py").read_text(encoding="utf-8")
    # no real email transport/send primitives (IMAP/SMTP client, network POST)
    for banned in ["smtplib", "imapclient", "imaplib", "requests.post",
                   "SMTP_SSL", ".sendmail(", "urllib.request"]:
        assert banned not in src, f"banned: {banned}"
    # the ONLY send path is the blocked EmailSendGate
    assert "EMAIL_REVIEW_ONLY_SEND_BLOCKED" in src
    assert "def send_email" in src
    # discovery transport must be injected, never self-connecting
    assert "def fetch_incoming_emails_readonly" in src
    assert "transport is None" in src


def test_discovery_readonly_transport_returns_emails():
    def fake_transport():
        return [{"message_id": "abc", "thread_id": "t", "provider": "GMAIL",
                 "sender_name": "HR", "sender_email": "hr@x.com",
                 "subject": "Application", "body_text": "Hi! Interested?"}]
    res = fetch_incoming_emails_readonly(fake_transport)
    assert res["verdict"] == "OK"
    assert res["email_count"] == 1
    assert res["emails"][0].message_id == "abc"