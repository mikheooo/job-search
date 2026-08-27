"""Stage 27: Email Message Reply MVP (REVIEW-ONLY).

Second message source for the job-search system, architected like HH Message
Reply (Stage 22/24/25): EMAIL -> DISCOVERY -> CONTEXT -> CLASSIFICATION ->
TRUTH-ONLY REPLY -> HUMAN REVIEW. Sending is PHYSICALLY impossible in this
module (EmailSendGate always blocks).

Hard rules:
- NO email send. EmailSendGate.send_email() ALWAYS returns
  EMAIL_REVIEW_ONLY_SEND_BLOCKED; there is no mutation/transport API here.
- NO AUTO mode (future-only; the module never enables it).
- Truth-only replies from the email context + candidate_profile.json. Missing
  facts -> HUMAN_REVIEW, never guessed.
- Dedup uses the real provider message_id when present; otherwise an explicit
  fallback identifier (never silently fabricated).
- Privacy: no full email bodies, no tokens/credentials in audit/state.

Provider access: the audit (Stage 27 §1) found NO email connector/API/IMAP/env
credentials in the project. fetch_incoming_emails_readonly() therefore
requires an injected read-only transport (provider adapter). If no transport
is configured -> EMAIL_ACCESS_BLOCKED with a concrete reason. No new email
stack is built here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

# Reuse the common safe classification/truth primitives from HH Message Reply
# where they are provider-agnostic (sensitive-topic blocking, language).
from .hh_message_reply import (
    MessageClassification,
    _SENSITIVE_RE,
    _NO_REPLY_MARKERS,
    _REPLY_PROBE,
    _load_profile,
    _context_texts,
    detect_language,
    generate_reply as _hh_generate_reply,
    HHDialog,
    HHMessage,
)


DEFAULT_STATE_PATH = os.path.join("artifacts", "email_message_reply_state.json")


class EmailProvider(str, Enum):
    UNKNOWN = "UNKNOWN"
    IMAP = "IMAP"
    GMAIL = "GMAIL"
    API = "API"


class EmailMessage(BaseModel):
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    provider: str = EmailProvider.UNKNOWN.value
    sender_name: str = ""
    sender_email: str = ""
    subject: str = ""
    timestamp: Optional[str] = None
    body_text: str = ""
    reply_to: Optional[str] = None
    # True only when a real provider identifier was present; a False marks a
    # documented fallback (subject+sender fingerprint), never a fake provider ID.
    has_real_message_id: bool = False

    model_config = {"extra": "forbid"}

    def display_key(self) -> str:
        if self.has_real_message_id and self.message_id:
            return f"{self.provider}:{self.message_id}"
        # explicit fallback: no stable provider ID -> derived fingerprint
        return f"{self.provider}:fb:" + _email_fallback_id(self)


class EmailContext(BaseModel):
    message: EmailMessage
    thread_messages: List[EmailMessage] = Field(default_factory=list)
    linked_company: str = ""
    linked_vacancy: str = ""
    linked_application: str = ""

    model_config = {"extra": "forbid"}


class EmailClassification(str, Enum):
    JOB_RELATED = "JOB_RELATED"
    NO_REPLY = "NO_REPLY"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    UNKNOWN = "UNKNOWN"


def _email_fallback_id(msg: EmailMessage) -> str:
    """Deterministic fallback identity when the provider gives no message_id.

    Uses sender + subject + (body head) - explicitly NOT presented as a real
    provider ID (see display_key)."""
    payload = json.dumps(
        {"sender": msg.sender_email, "subject": msg.subject,
         "body_head": (msg.body_text or "")[:120]},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# ---------------- discovery (read-only, provider-agnostic) -------------------

def fetch_incoming_emails_readonly(
    transport: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Read-only discovery of incoming emails via an injected transport.

    transport: callable returning a list of raw email dicts (provider adapter).
    If transport is None -> EMAIL_ACCESS_BLOCKED (no configured email access).
    The transport is READ-ONLY: it must not change read/unread, labels,
    folders, threads, or drafts.
    """
    if transport is None:
        return {
            "verdict": "EMAIL_ACCESS_BLOCKED",
            "reason": "no email provider/transport configured in this project "
                      "(no IMAP/SMTP/Gmail/API/credentials found in audit)",
            "emails": [],
        }
    try:
        raw = transport()
    except Exception as e:
        return {"verdict": "EMAIL_ACCESS_BLOCKED",
                "reason": f"transport error: {e}", "emails": []}
    emails = []
    for r in raw or []:
        emails.append(EmailMessage(
            message_id=r.get("message_id"),
            thread_id=r.get("thread_id"),
            provider=(r.get("provider") or EmailProvider.UNKNOWN.value),
            sender_name=(r.get("sender_name") or ""),
            sender_email=(r.get("sender_email") or ""),
            subject=(r.get("subject") or ""),
            timestamp=r.get("timestamp"),
            body_text=(r.get("body_text") or ""),
            reply_to=r.get("reply_to"),
            has_real_message_id=bool(r.get("message_id")),
        ))
    return {"verdict": "OK", "emails": emails, "email_count": len(emails)}


# ---------------- job-related filter -----------------------------------------

# Emails that are clearly NOT recruiter/job correspondence (no-reply senders).
_NO_REPLY_SENDER_RE = re.compile(
    r"no-reply|donotreply|do-not-reply|notifications@|noreply|alert@|"
    r"auto-reply|autoreply|system@", re.IGNORECASE)
# Subject markers that are notifications rather than recruiter correspondence.
_NO_REPLY_SUBJECT_RE = re.compile(
    r"ваш пароль|подтвердите|двухфакторн|счёт|invoice|оплата|receipt|"
    r"newsletter|рассылка|подписка|заказ|order", re.IGNORECASE)


def classify_email(context: EmailContext) -> EmailClassification:
    """Classify an incoming email. Full context (thread) is considered."""
    msg = context.message
    sender = (msg.sender_email or "").lower()
    subject = (msg.subject or "")
    body = (msg.body_text or "")
    full = "\n".join([subject, body] + [m.subject + "\n" + (m.body_text or "")
                                        for m in context.thread_messages])
    if not sender and not subject and not body:
        return EmailClassification.UNKNOWN
    # unknown sender (no name AND no email domain) -> cannot link to a vacancy
    # reliably -> HUMAN_REVIEW (never guess the company/vacancy).
    if not msg.sender_name and not sender:
        return EmailClassification.HUMAN_REVIEW
    # explicit no-reply senders / notification subjects
    if _NO_REPLY_SENDER_RE.search(sender) or _NO_REPLY_SUBJECT_RE.search(subject):
        return EmailClassification.NO_REPLY
    if any(m in (subject + "\n" + body).lower() for m in _NO_REPLY_MARKERS):
        return EmailClassification.NO_REPLY
    # sensitive topics -> human (salary, experience, interviews, dates, offer,
    # relocation, visa/work authorization, contract/legal)
    if _SENSITIVE_RE.search(full) or _EMAIL_SENSITIVE_RE.search(full):
        return EmailClassification.HUMAN_REVIEW
    # recruiter question probe -> JOB_RELATED (reply expected)
    if _REPLY_PROBE.search(subject + "\n" + body) or _RECRUITER_HINT_RE.search(full):
        return EmailClassification.JOB_RELATED
    return EmailClassification.HUMAN_REVIEW


_EMAIL_SENSITIVE_RE = re.compile(
    r"salary|зарплат|опыт|experience|интервью|interview|собеседован|"
    r"статус|status|offer|предложен|relocat|релокац|visa|виз|work authorization|"
    r"контракт|договор|юридич|legal|financial|финанс|технологи|стек|stack|"
    r"available to start|когда.*(мож|смож|доступ)|даты|dates", re.IGNORECASE)

_RECRUITER_HINT_RE = re.compile(
    r"ваканс|vacanc|резюме|resume|cv\b|отклик|приглаша|invit|заинтересован|"
    r"интересн|interested|рассмотр|профил|кандидат", re.IGNORECASE)


def link_email_to_vacancy(
    context: EmailContext,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Best-effort linkage to a known vacancy/company. Never guesses.

    Uses sender domain / subject / body keywords and existing project data
    (profile roles). Returns linked_company/vacancy + confidence; ambiguous ->
    HUMAN_REVIEW.
    """
    msg = context.message
    sender_domain = (msg.sender_email or "").split("@")[-1].lower()
    company = msg.sender_name or sender_domain
    vacancy = ""
    # do not guess a specific vacancy; keep company only when a sender name exists
    if not msg.sender_name and not sender_domain:
        return {"linked_company": "", "linked_vacancy": "",
                "confidence": 0.0, "note": "unknown sender - human review"}
    return {"linked_company": company, "linked_vacancy": vacancy,
            "confidence": 0.5 if msg.sender_name else 0.3,
            "note": "company from sender; vacancy not determinable without ambiguity"}


# ---------------- reply generation (truth-only) ------------------------------

def generate_email_reply(
    context: EmailContext,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Short, natural, truth-only email reply. Only for safe JOB_RELATED."""
    cls = classify_email(context)
    if cls != EmailClassification.JOB_RELATED:
        return {"reply": "", "sources": [], "status": cls.value,
                "reason": f"classification is {cls.value}"}
    prof = profile if profile is not None else _load_profile()
    facts = _email_truth_facts(prof)
    if not facts:
        return {"reply": "", "sources": [], "status": "HUMAN_REVIEW",
                "reason": "no provable profile facts available"}
    lang = detect_language(context.message.subject + "\n" + context.message.body_text)
    roles = ", ".join((prof.get("desired_roles") or [])[:3])
    if lang == "ru":
        reply = (f"Здравствуйте! Спасибо за сообщение и интерес к моей кандидатуре. "
                 f"Готов обсудить детали. Рассматриваю роли: {roles}.")
    else:
        reply = (f"Hello! Thank you for reaching out. I would be happy to discuss "
                 f"the details. I am considering roles: {roles}.")
    return {"reply": reply,
            "sources": ["candidate_profile.json: desired_roles"],
            "status": "JOB_RELATED",
            "reason": "safe recruiter message; profile facts available"}


def _email_truth_facts(profile: Dict[str, Any]) -> List[str]:
    facts: List[str] = []
    roles = profile.get("desired_roles") or []
    if roles:
        facts.append("roles: " + ", ".join(roles[:3]))
    return facts


# ---------------- physical send gate -----------------------------------------

class EmailSendGate:
    """Email send is PHYSICALLY blocked in Stage 27 (REVIEW-only)."""

    def send_email(self, context: EmailContext, text: str) -> Dict[str, Any]:
        return {"ok": False, "blocked": True,
                "reason": "EMAIL_REVIEW_ONLY_SEND_BLOCKED",
                "send_action_count": 0}

    # defensive: no transport/mutation primitive ever exposed
    def _never_transport(self) -> None:  # pragma: no cover
        raise RuntimeError("Stage 27 has no email transport/send primitive")


# ---------------- state / dedup ----------------------------------------------

class EmailReplyStateStore:
    """File-backed dedup/state (artifacts/, gitignored). No DB schema change."""

    def __init__(self, path: Optional[str] = None):
        # resolved at call time so tests can override DEFAULT_STATE_PATH after import
        self.path = path if path is not None else DEFAULT_STATE_PATH
        self._records: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._records = data.get("processed", {})
        except Exception:
            self._records = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"processed": self._records}, f, ensure_ascii=False, indent=1)

    def is_processed(self, key: str) -> bool:
        return key in self._records

    def mark_processed(self, key: str, classification: str, reply: str,
                       status: str, provider: str, message_id: str,
                       thread_id: str) -> None:
        self._records[key] = {
            "processed_at": datetime.utcnow().isoformat(),
            "classification": classification, "reply": reply, "status": status,
            "provider": provider, "message_id": message_id,
            "thread_id": thread_id,
        }
        self._save()


# ---------------- review report ----------------------------------------------

class EmailReplyReport(BaseModel):
    provider: str = EmailProvider.UNKNOWN.value
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    sender_name: str = ""
    sender_email: str = ""
    subject: str = ""
    classification: str = ""
    linked_company: str = ""
    linked_vacancy: str = ""
    generated_reply: str = ""
    sources: List[str] = Field(default_factory=list)
    status: str = ""  # NEEDS_HUMAN_REVIEW | SKIPPED | EMAIL_ACCESS_BLOCKED | ...
    reason: str = ""
    dedup_key: str = ""
    send_action_count: int = 0
    processed_at: str = ""

    model_config = {"extra": "forbid"}


def process_incoming_email(
    context: EmailContext,
    profile: Optional[Dict[str, Any]] = None,
    state: Optional[EmailReplyStateStore] = None,
) -> EmailReplyReport:
    """REVIEW-only processing of one incoming email. Never sends."""
    msg = context.message
    dedup_key = msg.display_key()
    store = state if state is not None else EmailReplyStateStore()
    report = EmailReplyReport(
        provider=msg.provider, message_id=msg.message_id, thread_id=msg.thread_id,
        sender_name=msg.sender_name, sender_email=msg.sender_email,
        subject=msg.subject, dedup_key=dedup_key,
        processed_at=datetime.utcnow().isoformat())

    if store.is_processed(dedup_key):
        report.status = "SKIPPED"
        report.reason = "already processed (dedup)"
        report.classification = "ALREADY_PROCESSED"
        return report

    classification = classify_email(context)
    report.classification = classification.value

    link = link_email_to_vacancy(context, profile)
    report.linked_company = link.get("linked_company", "")
    report.linked_vacancy = link.get("linked_vacancy", "")

    reply = ""
    sources: List[str] = []
    if classification == EmailClassification.JOB_RELATED:
        gen = generate_email_reply(context, profile)
        reply = gen.get("reply", "")
        sources = list(gen.get("sources", []) or [])
        if not reply:
            classification = EmailClassification.HUMAN_REVIEW
            report.classification = classification.value
            report.reason = gen.get("reason", "")
    report.generated_reply = reply
    report.sources = sources

    # physical send gate (always blocked)
    gate = EmailSendGate()
    gate.send_email(context, reply)  # always blocked

    if classification == EmailClassification.NO_REPLY:
        report.status = "NEEDS_HUMAN_REVIEW"
        report.reason = "no-reply notification - no candidate response needed"
    elif classification == EmailClassification.HUMAN_REVIEW:
        report.status = "NEEDS_HUMAN_REVIEW"
        report.reason = "ambiguous/sensitive - human review required, no facts guessed"
    elif classification == EmailClassification.UNKNOWN:
        report.status = "NEEDS_HUMAN_REVIEW"
        report.reason = "unrecognized email - safe stop"
    else:
        report.status = "NEEDS_HUMAN_REVIEW"
        report.reason = "REVIEW-only: reply preview generated, send blocked"

    store.mark_processed(dedup_key, report.classification, reply,
                         report.status, msg.provider,
                         msg.message_id or "", msg.thread_id or "")
    return report


__all__ = [
    "DEFAULT_STATE_PATH",
    "EmailClassification",
    "EmailContext",
    "EmailMessage",
    "EmailProvider",
    "EmailReplyReport",
    "EmailReplyStateStore",
    "EmailSendGate",
    "classify_email",
    "fetch_incoming_emails_readonly",
    "generate_email_reply",
    "link_email_to_vacancy",
    "process_incoming_email",
]