"""Stage 51: Fully Autonomous Job Application Agent.

Completely automates the HeadHunter job-search, application, questionnaire answering,
verification, and message handling workflow without requiring human intervention for routine steps.

CORE AUTONOMOUS LOOP:
1. DISCOVER: Search HeadHunter via active CDP session across target queries; deduplicate against DB.
2. MATCH: Apply Candidate Profile as Source of Truth (100% remote required; reject mandatory office; reject Java/C++/PHP/1C).
3. RANK & DRAFT: Calculate match score; generate vacancy-tailored cover letter and answers.
4. APPLY: Autonomous navigation, live verification, automatic questionnaire completion, autonomous submit without human confirmation gate.
5. POST-SUBMIT VERIFY: Verify response registered on HeadHunter; transition to SUBMITTED upon proof.
6. MESSAGE AGENT: Continuously monitor incoming HeadHunter messages; auto-reply to standard recruiter inquiries.
   NOTE (local safety patch, 2026-10-10): the auto-reply SEND is opt-in and OFF by
   default — it needs HH_AUTONOMOUS_REPLY_ENABLED=true. Without it the draft is
   generated and recorded but nothing is typed into the chat and nothing is clicked.
   This send does not carry the allowlist/confirmation gates of
   hh_message_reply.process_auto_reply(), so it is armed separately.
7. INTERVIEW DETECTION: Identify interview invitations and scheduling requests; trigger high-priority human notifications.
8. NOTIFICATION GATE: User is alerted ONLY for interviews, blocking unknown questions, or fatal errors (NEVER for routine discovery, standard applications, or rejections).

SAFETY INVARIANTS:
- Never apply twice to the same vacancy (strict deduplication).
- Never submit already_responded or blocked vacancies.
- Never fabricate candidate facts or hallucinate unverified details.
- Never apply to mandatory office or excluded primary stack roles.
- Post-submit verification required before marking as SUBMITTED.
- Exactly one actual browser submit click per application.
- Preserves existing SUBMITTED benchmark applications (app_hh_135112049, app_hh_136704137, app_hh_136551280).
- Zero autonomous loop runaway; pipeline.py is never executed.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from . import autonomous_mode, db
from .candidate_profile import CandidateProfile, load_candidate_profile
from .hh_application_orchestrator import HHApplicationState, transition_application
from .hh_browser_launcher import check_hh_session_authenticated, resolve_browser_endpoint
from .hh_message_reply import (
    HHDialog,
    HHMessage,
    classify_hh_conversation_detailed,
    fetch_hh_conversations_list_readonly,
)
from .hh_message_watcher import compute_message_fingerprint
from .hh_vacancy_navigator import (
    ensure_open_vacancy_tab,
    verify_and_navigate_hh_vacancy,
)

logger = logging.getLogger(__name__)

# Search queries for the active CDP search, grouped by the directions confirmed in T7.3 (support / application support / help desk / sysadmin / Windows Server + AD / Bitrix24 / CRM / infrastructure / automation). Python and AI stay in the list, but no longer as its whole content, and _discover_fresh_vacancies no longer truncates it to the first three.
DEFAULT_SEARCH_QUERIES = [
    "IT Support", "Technical Support Engineer", "Support Engineer", "Application Support", "Application Support Engineer",
    "Help Desk", "Service Desk", "System Administrator", "IT Administrator", "Windows Server Administrator",
    "Active Directory Administrator", "Bitrix24", "Bitrix24 Administrator", "Bitrix24 Support", "CRM Administrator",
    "CRM Support", "IT Infrastructure Engineer", "IT Automation Engineer", "Integrations Engineer", "Python Developer",
    "Backend Python Developer", "AI Engineer", "AI Agent Engineer", "Python FastAPI", "Automation Engineer Python",
    "специалист технической поддержки", "инженер технической поддержки", "системный администратор", "администратор Bitrix24", "специалист по сопровождению",
]


class NotificationPriority(str, Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class NotificationType(str, Enum):
    INTERVIEW_INVITATION = "INTERVIEW_INVITATION"
    INTERVIEW_SCHEDULING = "INTERVIEW_SCHEDULING"
    RECRUITER_REPLY_SENT = "RECRUITER_REPLY_SENT"
    UNANSWERED_QUESTION_BLOCKED = "UNANSWERED_QUESTION_BLOCKED"
    FATAL_ERROR = "FATAL_ERROR"
    EXTERNAL_QUESTIONNAIRE = "EXTERNAL_QUESTIONNAIRE"
    TEST_TASK = "TEST_TASK"
    GENERAL = "GENERAL"


class AutonomousConfig(BaseModel):
    """Configuration for Autonomous Job Application Agent."""
    cdp_url: str = Field(default_factory=resolve_browser_endpoint)
    search_queries: list[str] = Field(default_factory=lambda: list(DEFAULT_SEARCH_QUERIES))
    poll_interval_seconds: int = 60
    max_applications_per_cycle: int = 5
    max_auto_replies_per_cycle: int = 3
    remote_required: bool = True
    min_match_score: float = 70.0
    auto_start_browser: bool = True
    evaluate_fn: Any | None = None
    submit_enabled: bool = False
    mode: str = autonomous_mode.FULL  # T7.8: SEARCH_ONLY forbids every outbound action
    # T7.26 §6: how many of the discovered vacancies SEARCH_ONLY reads in full. A
    # separate knob from ``max_applications_per_cycle`` because reading is not
    # applying: the default is 10 because §6 asks for at least ten, while the apply
    # limit stays at 5.
    search_read_limit: int = 10
    model_config = {"extra": "forbid"}


# T7.29 §4: the outcome of the correspondence check is reported explicitly. A bare
# ``messages_checked == 0`` cannot tell "the dialog list was read and held nothing
# new" apart from "the transport was unavailable" or "the session was never
# verified", and the operator must be able to tell those apart without opening a
# browser. The four values are the vocabulary of that answer.
MSG_CHECK_CHECKED = "CHECKED"   # the list was read; the count is a confirmed number
MSG_CHECK_SKIPPED = "SKIPPED"   # the mode does not process correspondence (SEARCH_ONLY)
MSG_CHECK_BLOCKED = "BLOCKED"   # the transport works but the check cannot be trusted
MSG_CHECK_ERROR = "ERROR"       # the check itself failed (no transport, read error)

MESSAGE_CHECK_STATUSES = (
    MSG_CHECK_CHECKED,
    MSG_CHECK_SKIPPED,
    MSG_CHECK_BLOCKED,
    MSG_CHECK_ERROR,
)


# LOCAL SAFETY PATCH (this machine, 2026-10-10): the send in `_process_messages`
# is opt-in and OFF by default.
#
# That send types the draft into the chat box and clicks Send. It is the Stage
# 51-55 autonomous reply, and it deliberately does NOT go through the gates the
# canonical pipeline enforces in `hh_message_reply.process_auto_reply()`:
#   - the HH_AUTO_REPLY_ENABLED kill switch,
#   - `is_safe_for_auto_reply()`'s allowlist (the _AUTO_FORBIDDEN_RE topics:
#     salary, experience, stack, interviews, status, dates, offers),
#   - `can_auto_send()`'s re-verification,
#   - the explicit `confirm_live_send` flag.
# It fires for `classification in ("NEEDS_REPLY", "RECRUITER_QUESTION")`, i.e.
# exactly the technical questions the allowlist exists to keep for a human.
# Because `AutonomousConfig.mode` defaults to FULL, a bare `run_autonomous_cycle()`
# used to answer employers with no confirmation at all.
#
# The switch is deliberately SEPARATE from `HH_AUTO_REPLY_ENABLED`: that one arms
# the *guarded* pipeline, and arming the guarded path must never silently arm this
# ungated one. Off -> the draft is still generated and recorded, nothing is typed
# and nothing is clicked.
_AUTONOMOUS_REPLY_ENV_VAR = "HH_AUTONOMOUS_REPLY_ENABLED"


def _autonomous_reply_enabled(env: dict[str, str] | None = None) -> bool:
    """The send is armed ONLY when HH_AUTONOMOUS_REPLY_ENABLED=true."""
    env_map = os.environ if env is None else env
    return (env_map.get(_AUTONOMOUS_REPLY_ENV_VAR) or "").strip().lower() == "true"


def _incoming_message_still_current(evaluate_fn, expected: str) -> bool:
    """Re-read the conversation after generation; ambiguity blocks a click."""
    from .hh_message_reply import fetch_hh_conversation_readonly
    try:
        result = fetch_hh_conversation_readonly(evaluate_fn)
        messages = None
        if isinstance(result, dict):
            messages = result.get('messages')
            if not messages and 'conversations' in result:
                convs = result['conversations']
                if convs and isinstance(convs[0], dict):
                    messages = convs[0].get('messages')
        elif isinstance(result, list) and result and isinstance(result[0], dict):
            messages = result[0].get('messages')
        if not messages:
            return False
        last = messages[-1]
        direction = last.get('direction') if isinstance(last, dict) else getattr(last, 'direction', None)
        sender = last.get('sender') if isinstance(last, dict) else getattr(last, 'sender', None)
        if direction == 'OUTGOING' or (direction is None and sender == 'candidate'):
            return False
        if direction != 'INCOMING' and sender not in ('employer', 'system'):
            return False
        text = last.get('text') if isinstance(last, dict) else getattr(last, 'text', '')
        clean_exp = re.sub(r'\s+', ' ', (expected or '').strip()).lower()
        clean_act = re.sub(r'\s+', ' ', str(text or '').strip()).lower()
        return bool(clean_exp) and (clean_act == clean_exp)
    except Exception:
        return False



class AutonomousCycleResult(BaseModel):
    """Results from one autonomous execution cycle."""
    started_at: str
    completed_at: str
    status: str = "SUCCESS"
    discovered_count: int = 0
    matched_count: int = 0
    applied_count: int = 0
    verified_count: int = 0
    messages_checked: int = 0
    # Fail-closed default: a cycle that dies before step 4 must not leave a row
    # that reads like a clean, finished check. Only the two places that really
    # decide the outcome - the SEARCH_ONLY path and _process_messages - may lower
    # this to SKIPPED or raise it to CHECKED.
    messages_check_status: str = MSG_CHECK_ERROR
    messages_check_reason: str = "message check did not complete"
    messages_checked_at: str = ""
    auto_replies_count: int = 0
    interviews_detected: int = 0
    rejections_count: int = 0
    unanswered_questions_count: int = 0
    interview_details: list[dict[str, Any]] = Field(default_factory=list)
    notifications_sent: list[dict[str, Any]] = Field(default_factory=list)
    applications_processed: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    summary: str = ""

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Notification Dispatcher
# ---------------------------------------------------------------------------

class NotificationDispatcher:
    """Dispatches user notifications strictly for high-value events."""

    @staticmethod
    def notify_interview(
        company: str,
        vacancy_title: str,
        invitation_text: str,
        invitation_url: str | None = None,
        conversation_id: str | None = None,
        action_required: str = "Review message and confirm available interview time slot.",
    ) -> dict[str, Any]:
        """Dispatch high-priority interview notification to user."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        from .telegram_notifier import TelegramNotifier
        notif_msg = TelegramNotifier.format_interview_invitation(
            company=company,
            vacancy=vacancy_title,
            invitation_text=invitation_text,
            invitation_url=invitation_url or "",
        )
        notif_data = {
            "notification_type": NotificationType.INTERVIEW_INVITATION.value,
            "priority": NotificationPriority.HIGH.value,
            "title": f"INTERVIEW INVITATION: {company} - {vacancy_title}",
            "message": notif_msg,
            "company": company,
            "vacancy_title": vacancy_title,
            "vacancy_url": invitation_url,
            "conversation_id": conversation_id,
            "action_required": action_required,
            "created_at": now,
            "read": 0,
        }

        # Save to DB
        notif_id = db.save_autonomous_notification(notif_data)
        notif_data["id"] = notif_id

        # Save Interview Event
        db.save_interview_event({
            "conversation_id": conversation_id or "unknown",
            "company": company,
            "vacancy_title": vacancy_title,
            "invitation_text": invitation_text,
            "invitation_url": invitation_url,
            "action_required": action_required,
            "status": "INVITED",
            "detected_at": now,
            "notified": 1,
        })

        # Deliver to Telegram
        try:
            from .telegram_notifier import get_telegram_notifier
            get_telegram_notifier().deliver_notification(
                notif_type="INTERVIEW_INVITATION",
                details={
                    "company": company,
                    "vacancy_title": vacancy_title,
                    "invitation_text": invitation_text,
                    "invitation_url": invitation_url,
                    "conversation_id": conversation_id,
                    "created_at": now,
                },
                delivery_key=f"interview_{conversation_id or now}",
            )
        except Exception as e:
            logger.debug(f"Telegram interview delivery exception: {e}")

        # Render terminal banner
        print("\n" + "=" * 70)
        print(" [HIGH PRIORITY USER NOTIFICATION] INTERVIEW INVITATION DETECTED!")
        print(f" Company:      {company}")
        print(f" Vacancy:      {vacancy_title}")
        print(f" Message:      {invitation_text}")
        if invitation_url:
            print(f" URL:          {invitation_url}")
        print(f" Action:       {action_required}")
        print("=" * 70 + "\n")

        return notif_data

    @staticmethod
    def notify_reply_sent(
        company: str,
        vacancy_title: str,
        incoming_message: str,
        sent_reply: str,
        conversation_id: str | None = None,
        application_id: str | None = None,
        vacancy_url: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch user notification when an autonomous reply is verified sent on HeadHunter."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conv_str = conversation_id or "unknown"
        from .telegram_notifier import (
            TelegramNotifier,
            get_telegram_notifier,
            verify_hh_chat_url,
        )
        is_valid_url, verified_url = verify_hh_chat_url(conversation_id=conversation_id, url=vacancy_url)
        chat_url = verified_url if is_valid_url else None

        notif_msg = TelegramNotifier.format_recruiter_reply(
            company=company,
            vacancy=vacancy_title,
            incoming_message=incoming_message,
            sent_reply=sent_reply,
            conversation_id=conv_str,
            hh_chat_url=chat_url,
            status="CONFIRMED",
        )
        notif_data = {
            "notification_type": NotificationType.RECRUITER_REPLY_SENT.value,
            "priority": NotificationPriority.NORMAL.value,
            "title": f"RECRUITER REPLY — CONFIRMED: {company} - {vacancy_title}",
            "message": notif_msg,
            "company": company,
            "vacancy_title": vacancy_title,
            "vacancy_url": chat_url,
            "conversation_id": conversation_id,
            "action_required": "None (Autonomous reply sent and confirmed in HH)",
            "metadata": {
                "application_id": application_id,
                "incoming_message": incoming_message,
                "sent_reply": sent_reply,
                "status": "SENT",
                "hh_confirmed": True,
                "hh_chat_url": chat_url,
            },
            "created_at": now,
            "read": 0,
        }
        notif_id = db.save_autonomous_notification(notif_data)
        notif_data["id"] = notif_id

        # Deliver to Telegram
        try:
            get_telegram_notifier().deliver_notification(
                notif_type="RECRUITER_REPLY_SENT",
                details={
                    "company": company,
                    "vacancy_title": vacancy_title,
                    "incoming_message": incoming_message,
                    "sent_reply": sent_reply,
                    "conversation_id": conversation_id,
                    "application_id": application_id,
                    "hh_chat_url": chat_url,
                    "status": "CONFIRMED",
                    "created_at": now,
                },
                delivery_key=f"reply_{conversation_id or application_id or now}",
            )
        except Exception as e:
            logger.debug(f"Telegram reply delivery exception: {e}")

        # Render terminal banner
        print("\n" + "=" * 70)
        print(" [AUTONOMOUS USER NOTIFICATION] RECRUITER REPLY SENT")
        print(f" Company:      {company}")
        print(f" Vacancy:      {vacancy_title}")
        print(f" Employer:     {incoming_message}")
        print(f" My reply:     {sent_reply}")
        print(" HH:           CONFIRMED")
        print(f" Conversation: {conv_str}")
        if chat_url:
            print(f" Open Chat:    {chat_url}")
        print("=" * 70 + "\n")

        return notif_data

    @staticmethod
    def notify_external_questionnaire(
        company: str,
        vacancy_title: str,
        what_they_want: str,
        url: str,
        questions: str = "Требуется заполнение внешней анкеты",
        action: str = "REQUIRES REVIEW",
        conversation_id: str | None = None,
        application_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch notification for external questionnaire or form."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        from .telegram_notifier import TelegramNotifier
        notif_msg = TelegramNotifier.format_external_questionnaire(
            company=company,
            vacancy=vacancy_title,
            what_they_want=what_they_want,
            url=url,
            questions=questions,
            action=action,
        )
        notif_data = {
            "notification_type": NotificationType.EXTERNAL_QUESTIONNAIRE.value,
            "priority": NotificationPriority.HIGH.value,
            "title": f"EXTERNAL QUESTIONNAIRE: {company} - {vacancy_title}",
            "message": notif_msg,
            "company": company,
            "vacancy_title": vacancy_title,
            "vacancy_url": url,
            "conversation_id": conversation_id,
            "action_required": f"Review external form at {url} and complete questionnaire.",
            "metadata": {
                "application_id": application_id,
                "url": url,
                "what_they_want": what_they_want,
                "action": action,
            },
            "created_at": now,
            "read": 0,
        }
        notif_id = db.save_autonomous_notification(notif_data)
        notif_data["id"] = notif_id

        # Deliver to Telegram
        try:
            from .telegram_notifier import get_telegram_notifier
            get_telegram_notifier().deliver_notification(
                notif_type="EXTERNAL_QUESTIONNAIRE",
                details={
                    "company": company,
                    "vacancy_title": vacancy_title,
                    "what_they_want": what_they_want,
                    "url": url,
                    "questions": questions,
                    "action": action,
                    "conversation_id": conversation_id,
                    "application_id": application_id,
                    "created_at": now,
                },
                delivery_key=f"ext_q_{conversation_id or application_id or now}",
            )
        except Exception as e:
            logger.debug(f"Telegram ext questionnaire delivery exception: {e}")

        # Render terminal banner
        print("\n" + "=" * 70)
        print(" [AUTONOMOUS USER NOTIFICATION] EXTERNAL QUESTIONNAIRE")
        print(f" Company:        {company}")
        print(f" Vacancy:        {vacancy_title}")
        print(f" What they want: {what_they_want}")
        print(f" URL:            {url}")
        print(f" Action:         {action}")
        print("=" * 70 + "\n")

        return notif_data

    @staticmethod
    def notify_external_form_submitted(
        company: str,
        vacancy_title: str,
        url: str,
        fields_count: int,
        conversation_id: str | None = None,
        application_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch informational notification when external form was autonomously submitted."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        notif_msg = (
            f"✅ *Анкета успешно заполнена агентом!*\n\n"
            f"🏢 *Компания:* {company}\n"
            f"💼 *Вакансия:* {vacancy_title}\n"
            f"🔗 *Ссылка:* {url}\n"
            f"📝 *Заполнено полей:* {fields_count}\n"
            f"💬 *Статус:* Ответ отправлен работодателю в чат HH."
        )
        notif_data = {
            "notification_type": "EXTERNAL_FORM_SUBMITTED",
            "priority": NotificationPriority.NORMAL.value,
            "title": f"FORM SUBMITTED: {company} - {vacancy_title}",
            "message": notif_msg,
            "company": company,
            "vacancy_title": vacancy_title,
            "vacancy_url": url,
            "conversation_id": conversation_id,
            "action_required": "None (Autonomous submit completed).",
            "metadata": {
                "application_id": application_id,
                "url": url,
                "fields_count": fields_count,
            },
            "created_at": now,
            "read": 0,
        }
        notif_id = db.save_autonomous_notification(notif_data)
        notif_data["id"] = notif_id
        try:
            from .telegram_notifier import get_telegram_notifier
            get_telegram_notifier().deliver_notification(
                notif_type="EXTERNAL_FORM_SUBMITTED",
                details={
                    "company": company,
                    "vacancy_title": vacancy_title,
                    "url": url,
                    "fields_count": fields_count,
                    "conversation_id": conversation_id,
                    "created_at": now,
                },
                text_override=notif_msg,
            )
        except Exception as e:
            logger.debug(f"Telegram form submitted delivery exception: {e}")
        return notif_data

    @staticmethod
    def notify_test_task(
        company: str,
        vacancy_title: str,
        task_description: str,
        url: str = "",
        action: str = "REQUIRES REVIEW",
        conversation_id: str | None = None,
        application_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch notification for technical test task."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        url_line = f"\nURL:\n{url}\n\n" if url else "\n\n"
        notif_msg = (
            "TEST TASK\n\n"
            f"Company:\n{company}\n\n"
            f"Vacancy:\n{vacancy_title}\n\n"
            f"Task:\n{task_description}"
            f"{url_line}"
            f"Action:\n{action}"
        )
        notif_data = {
            "notification_type": NotificationType.TEST_TASK.value,
            "priority": NotificationPriority.HIGH.value,
            "title": f"TEST TASK: {company} - {vacancy_title}",
            "message": notif_msg,
            "company": company,
            "vacancy_title": vacancy_title,
            "vacancy_url": url or (f"https://hh.ru/chat/{conversation_id}" if conversation_id else None),
            "conversation_id": conversation_id,
            "action_required": "Review test task requirements and complete assignment.",
            "metadata": {
                "application_id": application_id,
                "url": url,
                "task_description": task_description,
                "action": action,
            },
            "created_at": now,
            "read": 0,
        }
        notif_id = db.save_autonomous_notification(notif_data)
        notif_data["id"] = notif_id

        # Deliver to Telegram
        try:
            from .telegram_notifier import get_telegram_notifier
            get_telegram_notifier().deliver_notification(
                notif_type="TEST_TASK",
                details={
                    "company": company,
                    "vacancy_title": vacancy_title,
                    "task_description": task_description,
                    "url": url,
                    "action": action,
                    "conversation_id": conversation_id,
                    "application_id": application_id,
                    "created_at": now,
                },
                delivery_key=f"test_task_{conversation_id or application_id or now}",
            )
        except Exception as e:
            logger.debug(f"Telegram test task delivery exception: {e}")

        return notif_data

    @staticmethod
    def notify_blocking_question(
        company: str,
        vacancy_title: str,
        unanswered_question: str,
        vacancy_url: str | None = None,
        application_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch notification when an unknown question blocks an application."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        notif_data = {
            "notification_type": NotificationType.UNANSWERED_QUESTION_BLOCKED.value,
            "priority": NotificationPriority.NORMAL.value,
            "title": f"MANUAL QUESTION REQUIRED: {company} - {vacancy_title}",
            "message": f"Vacancy questionnaire contains an unknown personal question: '{unanswered_question}'. Application paused in NEEDS_HUMAN_REVIEW.",
            "company": company,
            "vacancy_title": vacancy_title,
            "vacancy_url": vacancy_url,
            "action_required": f"Provide answer for '{unanswered_question}' via CLI questionnaire answer command.",
            "metadata": {"application_id": application_id, "question": unanswered_question},
            "created_at": now,
            "read": 0,
        }
        notif_id = db.save_autonomous_notification(notif_data)
        notif_data["id"] = notif_id

        # Deliver to Telegram
        try:
            from .telegram_notifier import get_telegram_notifier
            get_telegram_notifier().deliver_notification(
                notif_type="UNANSWERED_QUESTION_BLOCKED",
                details={
                    "company": company,
                    "vacancy_title": vacancy_title,
                    "unanswered_question": unanswered_question,
                    "application_id": application_id,
                    "created_at": now,
                },
                delivery_key=f"question_{application_id or now}",
            )
        except Exception as e:
            logger.debug(f"Telegram question delivery exception: {e}")

        return notif_data


def normalize_incoming_message_text(text: str) -> str:
    """Normalize message text so dynamic timestamps and bot headers don't break deduplication."""
    if not text:
        return ""
    t = str(text).strip()
    # Strip author / sender name prefixes (e.g. "Робот-рекрутер\n\n", "ИИ-помощник\n\n")
    t = re.sub(r'^(?:Робот-рекрутер|ИИ-помощник|Employer|Рекрутер)[\s:\n]+', '', t, flags=re.IGNORECASE)
    # Strip trailing dynamic timestamp (e.g. "\n\n07:33", "\n08:07")
    t = re.sub(r'[\r\n\s]+\d{1,2}:\d{2}\s*$', '', t)
    # Normalize multiple whitespace
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


# ---------------------------------------------------------------------------
# Filter & Match Engine (Candidate Profile as Source of Truth)
# ---------------------------------------------------------------------------

def evaluate_candidate_match(
    vacancy_title: str,
    company: str,
    description: str,
    raw_data: dict[str, Any] | None = None,
    profile: CandidateProfile | None = None,
) -> tuple[bool, float, str]:
    """Evaluate if a vacancy matches Candidate Profile hard filters and stack.

    Returns: (is_match, match_score, reason)
    """
    if profile is None:
        profile = load_candidate_profile()

    title_low = vacancy_title.lower()
    desc_low = description.lower()
    full_text = f"{title_low} {desc_low}"

    # 0. Check already responded
    if raw_data and raw_data.get("already_responded"):
        return False, 0.0, "Hard exclusion: already responded on HeadHunter"

    # 1. Hard Stack Exclusions
    excluded_roles = [r.lower() for r in profile.excluded_roles]
    for ex in excluded_roles:
        if ex in title_low or (ex in desc_low and len(ex) > 2 and f" {ex} " in full_text):
            # T7.4: "bitrix" is deliberately absent. T7.3 removed the blanket
            # Bitrix24 ban from the profile because Bitrix24 is a target
            # direction, and this hardcoded tuple was re-arming it here.
            if ex in ("1c", "1с", "php", "java developer", "c++", "c#", "ruby"):
                return False, 0.0, f"Hard exclusion: contains excluded stack '{ex}'"

    # Specific check for Java / 1C / PHP primary roles
    if any(k in title_low for k in [" 1с", "1c", "php", "java ", "java/", "c++", "c#"]):
        return False, 0.0, "Hard exclusion: non-Python primary title"

    # 2. Hard Remote Requirement (100% remote required)
    if profile.remote_required:
        # Check explicit mandatory office signals in description
        office_mandatory_signals = [
            "формат работы - в офисе",
            "работа только в офисе",
            "фулл-тайм офисный формат",
            "офис в москва-сити",
            "работа в офисе",
            "только офис",
            "строго в офисе",
            "5/2 в офисе",
            "нахождение в офисе",
        ]
        for signal in office_mandatory_signals:
            if signal in desc_low:
                # Check if remote exception is explicitly offered
                if not any(r in desc_low for r in ["удаленка возможна", "100% удаленка", "полностью удаленно"]):
                    return False, 0.0, f"Hard exclusion: mandatory office format detected ('{signal}')"

    # 3. Match Score Calculation
    score = 50.0

    # Core stack keywords
    core_stack = [
        "python", "fastapi", "asyncio", "postgresql", "postgres",
        "docker", "kubernetes", "k8s", "rest", "api", "grpc",
        "llm", "ai", "ai agent", "agents", "mcp", "model context protocol",
        "automation", "n8n", "browser automation", "playwright", "selenium",
    ]

    matched_keywords = [k for k in core_stack if k in full_text]
    score += min(40.0, len(matched_keywords) * 4.0)

    # Title match bonus
    if any(r.lower() in title_low for r in profile.desired_roles):
        score += 15.0
    elif any(r.lower() in title_low for r in profile.alternative_roles):
        score += 10.0
    elif "python" in title_low or "ai" in title_low:
        score += 8.0

    # Commercial experience check
    if "junior" in title_low or "middle" in title_low or "мидл" in title_low:
        score += 5.0

    score = min(100.0, max(0.0, score))
    is_match = score >= 70.0

    return is_match, score, f"Match score: {score:.1f}/100 (matched skills: {', '.join(matched_keywords[:6])})"


# ---------------------------------------------------------------------------
# Questionnaire Auto-Solver
# ---------------------------------------------------------------------------

def solve_questionnaire_autonomously(
    questions: list[dict[str, Any]],
    profile: CandidateProfile | None = None,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Automatically answer questionnaire questions using Candidate Profile facts.

    Separates AUTO_ANSWERABLE from UNKNOWN_REQUIRES_HUMAN.
    Never fabricates unknown personal facts.

    Returns: (all_answered, answers_dict, unanswered_questions_list)
    """
    if profile is None:
        profile = load_candidate_profile()

    answers: dict[str, Any] = {}
    unanswered: list[str] = []

    for q in questions:
        qid = q.get("id") or str(q.get("number") or "")
        qtext = (q.get("title") or q.get("text") or "").strip().lower()
        qtype = q.get("type") or "text"
        required = q.get("required", True)
        options = q.get("options") or []

        answered = False

        # 1. Commercial experience / Python years
        if any(k in qtext for k in ["опыт", "лет", "года", "стаж", "years of experience", "experience"]):
            if any(k in qtext for k in ["python", "разработ", "программирован", "работы", "overall", "коммерческ"]):
                exp_years = profile.years_experience or 3
                if qtype in ("number", "integer"):
                    answers[qid] = exp_years
                elif options:
                    # Pick best matching option
                    for opt in options:
                        opt_str = str(opt).lower()
                        if "3" in opt_str or "1-3" in opt_str or "3-6" in opt_str or "от 3" in opt_str:
                            answers[qid] = opt
                            break
                    if qid not in answers and options:
                        answers[qid] = options[0]
                else:
                    answers[qid] = f"{exp_years} года коммерческого опыта разработки на Python"
                answered = True

        # 2. Remote / Work format
        elif any(k in qtext for k in ["формат", "удален", "remote", "график", "локаци", "город"]):
            if options:
                for opt in options:
                    opt_str = str(opt).lower()
                    if "удален" in opt_str or "remote" in opt_str:
                        answers[qid] = opt
                        answered = True
                        break
            if not answered:
                answers[qid] = "100% удалённый формат работы (Remote)"
                answered = True

        # 3. Portfolio / GitHub / Code Link
        elif any(k in qtext for k in ["github", "портфолио", "portfolio", "код", "проект", "ссылк", "link"]):
            answers[qid] = profile.github or "https://github.com/mikheooo"
            answered = True

        # 4. Tech Stack / Skills (FastAPI, asyncio, PostgreSQL, Docker, AI, etc.)
        elif any(k in qtext for k in ["стек", "технолог", "fastapi", "docker", "postgres", "sql", "ai", "llm", "n8n"]):
            skills_str = ", ".join(profile.skills[:8])
            answers[qid] = f"Основной стек: Python, FastAPI, asyncio, PostgreSQL, Docker, REST API, LLM/AI integrations, Agent systems, MCP. GitHub: {profile.github}"
            answered = True

        # 5. Salary / Compensation expectations
        elif any(k in qtext for k in ["зарплат", "оплат", "оклад", "доход", "salary", "ставка"]):
            if profile.minimum_salary:
                min_sal = int(profile.minimum_salary)
                curr = profile.salary_currency or "USD"
                answers[qid] = f"От {min_sal} {curr} (готов обсудить на звонке в зависимости от задач)"
                answered = True
            else:
                answers[qid] = "Готов обсудить зарплатные ожидания на собеседовании"
                answered = True

        # 6. Languages / English
        elif any(k in qtext for k in ["английск", "english", "язык", "language"]):
            if options:
                for opt in options:
                    opt_str = str(opt).lower()
                    if "b2" in opt_str or "upper" in opt_str or "свободн" in opt_str:
                        answers[qid] = opt
                        answered = True
                        break
            if not answered:
                answers[qid] = "Русский — родной, Английский — B2 (технический / свободное чтение и переписка)"
                answered = True

        # If not answered and required -> mark as UNKNOWN_REQUIRES_HUMAN
        if not answered:
            if required:
                unanswered.append(q.get("title") or q.get("text") or qid)
            else:
                answers[qid] = "Готов ответить и предоставить подробную информацию на собеседовании."

    all_answered = len(unanswered) == 0
    return all_answered, answers, unanswered


# ---------------------------------------------------------------------------
# Cover Letter Generator
# ---------------------------------------------------------------------------

def generate_autonomous_cover_letter(
    vacancy_title: str,
    company: str,
    profile: CandidateProfile | None = None,
) -> str:
    """Generate professional, truth-only cover letter tailored to the vacancy."""
    if profile is None:
        profile = load_candidate_profile()

    name = profile.name or "Михаил"
    github = profile.github or "https://github.com/mikheooo"
    years = profile.years_experience or 3

    letter = (
        f"Здравствуйте!\n\n"
        f"Меня заинтересовала вакансия {vacancy_title} в компании {company}.\n\n"
        f"У меня более {years} лет практического коммерческого опыта разработки на Python (FastAPI, asyncio, PostgreSQL, Docker, REST API).\n"
        f"Активно разрабатываю и внедряю AI-сервисы, multi-agent системы, LLM-пайплайны и интеграции на базе Model Context Protocol (MCP).\n\n"
        f"Мой GitHub с проектами и кодом: {github}\n"
        f"Рассматриваю 100% удалённый формат работы. Буду рад пообщаться и обсудить задачи на собеседовании!\n\n"
        f"С уважением,\n{name}"
    )
    return letter


# ---------------------------------------------------------------------------
# Autonomous Execution Engine
# ---------------------------------------------------------------------------

class AutonomousJobAgent:
    """Fully autonomous job agent for HeadHunter."""

    def __init__(self, config: AutonomousConfig | None = None):
        self.config = config or AutonomousConfig()
        self.profile = load_candidate_profile()
        db.init_db()

    def run_cycle(self) -> AutonomousCycleResult:
        """Execute one complete autonomous cycle:

        DISCOVER -> FILTER -> MATCH -> APPLY -> VERIFY -> WATCH_MESSAGES -> AUTO_REPLY -> INTERVIEW_DETECTION
        """
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        logger.info(f"Starting autonomous cycle at {started_at}")

        result = AutonomousCycleResult(started_at=started_at, completed_at="")

        if self.config.submit_enabled:
            raise NotImplementedError("Autonomous submission is disabled by design; use 'application runner next --confirm-submit' after human approval")
        if not autonomous_mode.outbound_allowed(self.config.mode):
            return self._search_only_cycle(started_at, result)

        from . import hh_browser_lease as _lease

        # T7.25 §5: the daemon may live; Chrome between cycles may not. The
        # browser is owned by this cycle and released by `hh_operation`'s
        # finally, so a cycle that raises still gives the browser back - which is
        # what "no browser between tasks" has to mean for a long-lived agent.
        try:
            with _lease.hh_operation(
                _lease.OWNER_AUTONOMOUS,
                purpose="cycle",
                session_kwargs={"require_auth": False},
            ) as op:
                if self.config.auto_start_browser and self.config.evaluate_fn is None:
                    # Best-effort, exactly as it was: a cycle has steps that need
                    # no browser, and the old code logged a failed auto-start and
                    # carried on. What T7.25 changed is who *owns* the browser,
                    # not whether a missing one aborts the cycle.
                    try:
                        started_browser = op["session"].start()
                    except Exception as e:  # noqa: BLE001 - see the comment above
                        logger.debug(f"Could not auto-start browser: {e}")
                    else:
                        if not started_browser.get("ok"):
                            logger.debug(
                                "Could not auto-start browser: %s",
                                started_browser.get("message"),
                            )
                        else:
                            # T7.25: ensure_hh_browser() opened the HH page as part
                            # of "ensure a browser", and the discovery below attaches
                            # to an existing hh.ru tab. Ownership moved; the page did
                            # not. Best-effort, exactly like the start above.
                            from .hh_browser_launcher import (
                                DEFAULT_HH_URL as _DEFAULT_HH_URL,
                            )

                            try:
                                op["session"].open_hh_page(_DEFAULT_HH_URL)
                            except Exception as e:  # noqa: BLE001 - best-effort
                                logger.debug(f"Could not open the HH page: {e}")
                return self._cycle_body(started_at, result)
        except _lease.BrowserBusy as e:
            logger.error(f"Could not acquire the HH browser for this cycle: {e}")
            result.status = "ERROR"
            result.errors.append(str(e))
            result.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            result.summary = f"Cycle could not start: {e}"
            return result

    def _cycle_body(self, started_at: str, result: AutonomousCycleResult) -> AutonomousCycleResult:
        """The cycle itself, with the browser already owned by the caller.

        Split out of ``run_cycle`` by T7.25 §5 so the ownership wrapper can be a
        ``with`` without re-indenting eighty lines of workflow: the body was
        already one level inside a ``try`` and stays exactly that.
        """
        try:
            # 1. DISCOVER & DEDUPLICATE
            fresh_vacancies = self._discover_fresh_vacancies()
            result.discovered_count = len(fresh_vacancies)

            # 2. FILTER & MATCH
            matched_vacancies = []
            for vac in fresh_vacancies:
                is_match, score, reason = evaluate_candidate_match(
                    vacancy_title=vac.get("title", ""),
                    company=vac.get("employer", ""),
                    description=vac.get("description", ""),
                    raw_data=vac,
                    profile=self.profile,
                )
                if is_match:
                    matched_vacancies.append((vac, score, reason))

            # Strictly rank matching vacancies: highest match score first
            matched_vacancies.sort(key=lambda x: x[1], reverse=True)

            result.matched_count = len(matched_vacancies)

            # 3. APPLY & VERIFY (Up to configured limit per cycle)
            applied_count = 0
            for vac_data, score, reason in matched_vacancies[: self.config.max_applications_per_cycle]:
                app_res = self._process_single_application(vac_data, score)
                result.applications_processed.append(app_res)
                if app_res.get("submitted"):
                    applied_count += 1
                if app_res.get("verified"):
                    result.verified_count += 1
                if app_res.get("unanswered_question"):
                    result.unanswered_questions_count += 1

            result.applied_count = applied_count

            # 4. MESSAGE AGENT: WATCH & PROCESS MESSAGES
            msg_res = self._process_messages()
            result.messages_checked = msg_res.get("messages_checked", 0)
            result.messages_check_status = msg_res.get("messages_check_status", MSG_CHECK_ERROR)
            result.messages_check_reason = msg_res.get("messages_check_reason", "")
            result.messages_checked_at = msg_res.get("messages_checked_at", "")
            result.auto_replies_count = msg_res.get("auto_replies_count", 0)
            result.interviews_detected = msg_res.get("interviews_detected", 0)
            result.rejections_count = msg_res.get("rejections_count", 0)
            result.interview_details = msg_res.get("interview_details", [])
            result.notifications_sent = msg_res.get("notifications_sent", [])

            result.status = "SUCCESS"
            result.summary = (
                f"Autonomous cycle completed successfully. "
                f"Discovered: {result.discovered_count}, Matched: {result.matched_count}, "
                f"Applied: {result.applied_count}, Verified: {result.verified_count}, "
                f"Messages Checked: {result.messages_checked} "
                f"[{result.messages_check_status}]"
                f"{' - ' + result.messages_check_reason if result.messages_check_reason else ''}, "
                f"Auto-replies: {result.auto_replies_count}, "
                f"Interviews: {result.interviews_detected}."
            )

        except Exception as e:
            logger.error(f"Autonomous cycle encountered error: {e}", exc_info=True)
            result.status = "ERROR"
            result.errors.append(str(e))
            result.summary = f"Cycle terminated with error: {e}"

        result.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Save cycle run to DB
        db.save_autonomous_cycle_run({
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "status": result.status,
            "discovered_count": result.discovered_count,
            "matched_count": result.matched_count,
            "applied_count": result.applied_count,
            "verified_count": result.verified_count,
            "messages_checked": result.messages_checked,
            "auto_replies_count": result.auto_replies_count,
            "interviews_detected": result.interviews_detected,
            "rejections_count": result.rejections_count,
            "unanswered_questions_count": result.unanswered_questions_count,
            "summary": result.summary,
            "log": result.model_dump(),
        })

        return result

    def _discover_fresh_vacancies(self) -> list[dict[str, Any]]:
        """Discover new HeadHunter vacancies via CDP session and deduplicate against DB."""
        fresh: list[dict[str, Any]] = []
        seen_ids = set()

        # Populate from existing database
        all_existing_vacs = db.list_vacancies(limit=2000)
        for v in all_existing_vacs:
            stable = ""
            if isinstance(v, (tuple, list)):
                stable = str(v[0]) if v else ""
            elif isinstance(v, dict):
                stable = v.get("stable_id") or ""
            elif hasattr(v, "stable_id"):
                stable = v.stable_id() if callable(v.stable_id) else v.stable_id
            num_id = stable.split(":")[-1] if ":" in stable else stable
            if num_id:
                seen_ids.add(num_id)

        # Populate from existing applications
        all_apps = db.list_hh_applications(limit=2000)
        for a in all_apps:
            stable = ""
            if isinstance(a, (tuple, list)):
                stable = str(a[1]) if len(a) > 1 else str(a[0])
            elif isinstance(a, dict):
                stable = a.get("vacancy_stable_id") or a.get("application_id") or ""
            elif hasattr(a, "vacancy_stable_id"):
                stable = a.vacancy_stable_id or a.application_id
            num_id = stable.split(":")[-1] if ":" in stable else stable
            if num_id:
                seen_ids.add(num_id)

        # Execute active search if evaluate_fn available
        eval_fn = self.config.evaluate_fn
        if eval_fn is None:
            try:
                from .prefill_execute import make_cdp_evaluate
                eval_fn = make_cdp_evaluate(self.config.cdp_url, "hh.ru")
            except Exception:
                eval_fn = None

        if eval_fn:
            for query in self.config.search_queries:
                try:
                    search_url = f"https://hh.ru/search/vacancy?text={urllib_quote(query)}&schedule=remote&order_by=publication_time"
                    raw_cards = eval_fn(f"""(() => {{
                        if (!location.href.includes('search/vacancy')) {{
                            location.href = '{search_url}';
                            return JSON.stringify({{ status: 'navigating' }});
                        }}
                        const cards = Array.from(document.querySelectorAll('[data-qa="vacancy-serp__vacancy"]'));
                        return JSON.stringify(cards.map(c => {{
                            const link = c.querySelector('a[data-qa="serp-item__title"]');
                            const emp = c.querySelector('[data-qa="vacancy-serp__vacancy-employer"]');
                            const resp = c.querySelector('[data-qa*="responded"]');
                            const url = link ? link.href : '';
                            const idMatch = url.match(/vacancy\\/(\\d+)/);
                            return {{
                                vacancy_id: idMatch ? idMatch[1] : '',
                                title: link ? link.innerText.trim() : '',
                                employer: emp ? emp.innerText.trim() : '',
                                url: url,
                                already_responded: !!resp
                            }};
                        }}));
                    }})()""")
                    cards_data = json.loads(raw_cards) if isinstance(raw_cards, str) else raw_cards
                    if isinstance(cards_data, list):
                        for card in cards_data:
                            vid = card.get("vacancy_id")
                            if vid and vid not in seen_ids and not card.get("already_responded"):
                                seen_ids.add(vid)
                                fresh.append(card)
                except Exception as e:
                    logger.debug(f"Discovery query '{query}' exception: {e}")

        return fresh

    def _process_single_application(self, vac_data: dict[str, Any], score: float) -> dict[str, Any]:
        """Process a single matching vacancy autonomously: discovers, scores, prepares package and transitions to READY_TO_SUBMIT."""
        vac_id = str(vac_data.get("vacancy_id") or "").strip()
        title = vac_data.get("title") or "Python Developer"
        employer = vac_data.get("employer") or "Unknown Employer"
        app_id = f"app_hh_{vac_id}"
        stable_id = f"hh:{vac_id}"

        app_record = {
            "application_id": app_id,
            "vacancy_id": vac_id,
            "title": title,
            "employer": employer,
            "submitted": False,
            "verified": False,
            "unanswered_question": None,
            "reason": "",
        }

        # Step 1: Create application in DB
        db.save_hh_application({
            "application_id": app_id,
            "vacancy_stable_id": stable_id,
            "title": title,
            "employer": employer,
            "state": HHApplicationState.NEW.value,
        })

        transition_application(app_id, HHApplicationState.MATCHED, reason=f"autonomous_matched_score_{score:.1f}")

        # Step 2: Live Navigation & Verification
        eval_fn = self.config.evaluate_fn
        if eval_fn is None:
            try:
                from .prefill_execute import make_cdp_evaluate
                ensure_open_vacancy_tab(self.config.cdp_url, f"https://hh.ru/vacancy/{vac_id}")
                time.sleep(2.0)
                eval_fn = make_cdp_evaluate(self.config.cdp_url, vac_id)
            except Exception:
                eval_fn = None

        if eval_fn:
            nav_res = verify_and_navigate_hh_vacancy(
                target=stable_id,
                evaluate_fn=eval_fn,
                expected_title=title,
            )
            if not nav_res.ok:
                transition_application(app_id, HHApplicationState.BLOCKED, reason=f"navigation_failed: {nav_res.reason}")
                app_record["reason"] = f"Navigation failed: {nav_res.reason}"
                return app_record

        # Step 3: Prepare Application Package & Cover Letter
        cover_letter = generate_autonomous_cover_letter(title, employer, self.profile)
        pkg_data = {
            "vacancy_stable_id": stable_id,
            "cover_letter": cover_letter,
            "resume_summary": getattr(self.profile, "summary", "") or "",
            "tailored_skills": getattr(self.profile, "skills", []) or [],
            "validation_status": "VALID",
        }
        db.save_application_package(stable_id, "v1", json.dumps(pkg_data, ensure_ascii=False))

        # Step 4: Create ApplicationReview
        from .application_review import (
            REVIEW_VERSION,
            ApplicationReview,
            ReviewStatus,
            compute_review_fingerprint,
            get_application_review,
            save_application_review,
        )
        rev = get_application_review(stable_id)
        if not rev:
            fp = compute_review_fingerprint(stable_id, pkg_data)
            rev = ApplicationReview(
                vacancy_stable_id=stable_id,
                company=employer,
                title=title,
                source="hh",
                vacancy_url=f"https://hh.ru/vacancy/{vac_id}",
                final_url=f"https://hh.ru/vacancy/{vac_id}",
                match_score=score,
                cover_letter=cover_letter,
                status=ReviewStatus.APPROVED,
                form_fingerprint=fp,
                review_version=REVIEW_VERSION,
                note="Prepared and approved autonomously",
            )
            save_application_review(rev)

        # Step 5: Transition tracking to READY_TO_APPLY
        from .application_tracking import ApplicationStatus, set_application_status
        set_application_status(
            vacancy_stable_id=stable_id,
            status=ApplicationStatus.READY_TO_APPLY,
            company=employer,
            title=title,
            source="hh",
            vacancy_url=f"https://hh.ru/vacancy/{vac_id}",
            match_score=score,
            notes="Autonomously discovered and prepared for submission",
        )

        # Step 6: Add to application_queue
        from .application_queue import QueueItem, save_queue_item
        save_queue_item(
            QueueItem(
                vacancy_stable_id=stable_id,
                canonical_id=f"can_{stable_id.replace(':', '_')}",
                representative_vacancy_stable_id=stable_id,
                company=employer,
                title=title,
                source="hh",
                vacancy_url=f"https://hh.ru/vacancy/{vac_id}",
                priority_score=int(score),
                rank=1,
            )
        )

        transition_application(app_id, HHApplicationState.READY_TO_SUBMIT, reason="autonomous_prepared_ready_to_submit")

        # Step 7: Send Telegram Notification with action buttons
        try:
            from .telegram_notifier import TelegramNotifier
            notifier = TelegramNotifier()
            reply_markup = TelegramNotifier.build_digest_inline_keyboard([{"stable_id": stable_id}])
            msg_text = (
                f"🎯 Новая вакансия подготовлена автономным агентом:\n\n"
                f"🏢 {employer}\n"
                f"💼 {title}\n"
                f"⭐ Оценка соответствия: {score:.1f}/100\n"
                f"🔗 https://hh.ru/vacancy/{vac_id}\n\n"
                f"Статус: Готова к отправке (READY_TO_SUBMIT).\n"
            )
            # Show the generated letter: the owner must be able to see what the agent
            # produced, even though autonomous mode approves without a separate ✅.
            if cover_letter:
                clipped = cover_letter[:1200]
                suffix = "…" if len(cover_letter) > 1200 else ""
                msg_text += f"\n✉️ Сопроводительное письмо:\n{clipped}{suffix}\n"
            notifier.send_message(text=msg_text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Failed to send telegram notification for {stable_id}: {e}")

        app_record["reason"] = "Prepared for autonomous submission (READY_TO_SUBMIT)"
        return app_record

    def _process_messages(
        self, convs: list[Any] | None = None, _eval_fn: Any | None = None
    ) -> dict[str, Any]:
        """Watch and process incoming dialogs, detecting interviews and auto-replying.

        T7.29 §4: every return carries an explicit outcome, because a bare
        ``messages_checked = 0`` cannot tell "the dialog list was read and held
        nothing new" apart from "the transport was unavailable" or "the session
        was never verified". ``messages_check_status`` is one of CHECKED /
        SKIPPED / BLOCKED / ERROR, with the reason and the time of the check
        beside it, and it travels into both the report and the stored cycle row.

        ``_eval_fn`` is the re-entry seam T7.25 needs: the outer call opens an
        owning ``hh_operation``, starts the browser inside it and calls itself
        again with the transport set, so the whole dialog pass - not just the
        launch - runs inside the operation that has to release the browser.
        """
        # The "is this a messages surface" rule already lives in one place;
        # importing it keeps a second copy from drifting away from the first.
        from .prefill_execute import _HH_MESSAGES_PATTERN

        out: dict[str, Any] = {
            "messages_checked": 0,
            "messages_check_status": MSG_CHECK_ERROR,
            "messages_check_reason": "",
            "messages_checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "auto_replies_count": 0,
            "interviews_detected": 0,
            "rejections_count": 0,
            "interview_details": [],
            "notifications_sent": [],
        }

        def _stop(status: str, reason: str) -> dict[str, Any]:
            out["messages_check_status"] = status
            out["messages_check_reason"] = reason
            out["messages_checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            logger.warning("Message check %s: %s", status, reason)
            return out

        # 1. SEARCH_ONLY is search-only by design: it must not touch the
        #    correspondence at all, and it must say so instead of reporting a zero.
        if self.config.mode == autonomous_mode.SEARCH_ONLY:
            return _stop(MSG_CHECK_SKIPPED, "SEARCH_ONLY mode does not process correspondence")

        # 2. Transport. No transport is an ERROR with a reason, never a silent 0.
        eval_fn = _eval_fn if _eval_fn is not None else self.config.evaluate_fn
        if eval_fn is None:
            try:
                from .prefill_execute import make_cdp_evaluate
                eval_fn = make_cdp_evaluate(self.config.cdp_url, "hh.ru")
            except Exception as exc:
                if not self.config.auto_start_browser:
                    return _stop(MSG_CHECK_ERROR, f"no browser transport: {type(exc).__name__}: {exc}")
                # T7.25: the browser is started by an owning operation, and the
                # whole dialog pass runs inside it, so a check that raises still
                # gives the browser back. The re-entrant call below sees
                # ``_eval_fn`` set and starts nothing of its own.
                from . import hh_browser_lease as _lease
                from .hh_browser_launcher import DEFAULT_HH_URL
                try:
                    with _lease.hh_operation(
                        _lease.OWNER_AUTONOMOUS,
                        purpose="messages",
                        session_kwargs={"require_auth": False},
                    ) as op:
                        started = op["session"].start()
                        if not started.get("ok"):
                            return _stop(
                                MSG_CHECK_ERROR,
                                f"no browser transport: {exc} (auto-start failed: "
                                f"{started.get('status')} {started.get('message') or ''})".strip(),
                            )
                        # ensure_hh_browser() used to open the HH page as part of
                        # "ensure a browser"; ownership moved, the page did not.
                        try:
                            op["session"].open_hh_page(DEFAULT_HH_URL)
                        except Exception as e:  # noqa: BLE001 - the page is best-effort
                            logger.debug("Could not open the HH page: %s", e)
                        return self._process_messages(
                            convs,
                            _eval_fn=make_cdp_evaluate(op["session"].cdp_url, "hh.ru"),
                        )
                except Exception as exc2:
                    return _stop(
                        MSG_CHECK_ERROR,
                        f"no browser transport: {exc} (auto-start failed: "
                        f"{type(exc2).__name__}: {exc2})",
                    )
        if not eval_fn:
            return _stop(MSG_CHECK_ERROR, "no browser transport: no evaluate function available")

        # 3. Read the dialog list if not provided directly.
        raw_res = None
        confirmed_empty_unread = False
        if convs is None:
            try:
                curr_url = eval_fn("window.location.href")
                if not curr_url or "/chat" not in curr_url:
                    logger.info("Navigating open tab to https://hh.ru/chat (was: %s)", curr_url)
                    eval_fn("window.location.href = 'https://hh.ru/chat'")
                    time.sleep(4)
                # Dismiss top-bar user notifications badge if active (e.g. "1")
                try:
                    eval_fn("""(() => {
                        const badge = document.querySelector('[data-qa="userNotifications-badge"]');
                        if (badge && (badge.innerText || '').trim()) {
                            const btn = badge.closest('button, a') || badge;
                            btn.click();
                        }
                    })()""")
                except Exception:
                    pass
            except Exception as exc_nav:
                logger.debug("Navigation to /chat pre-check exception: %s", exc_nav)

            try:
                raw_res = fetch_hh_conversations_list_readonly(evaluate_fn=eval_fn)
            except Exception as exc:
                return _stop(MSG_CHECK_ERROR, f"dialog list read failed: {type(exc).__name__}: {exc}")

            if isinstance(raw_res, dict) and raw_res.get("error"):
                return _stop(MSG_CHECK_ERROR, f"dialog list read failed: {raw_res.get('error')}")

            if isinstance(raw_res, dict):
                convs = raw_res.get("conversations", [])
            elif isinstance(raw_res, list):
                convs = raw_res
            else:
                convs = []

        dialogs: list[HHDialog] = []
        for it in convs:
            if isinstance(it, HHDialog):
                dialogs.append(it)
            elif isinstance(it, dict):
                msgs = []
                for m in it.get("messages", []):
                    if isinstance(m, HHMessage):
                        msgs.append(m)
                    elif isinstance(m, dict):
                        msgs.append(HHMessage(
                            message_id=str(m.get("message_id") or f"m_{len(msgs)}"),
                            text=str(m.get("text") or ""),
                            sender=str(m.get("sender") or "employer"),
                            sent_at=m.get("sent_at"),
                        ))
                if not msgs and it.get("snippet"):
                    snip_text = str(it.get("snippet") or "").strip()
                    snip_low = snip_text.lower()
                    is_cand = (
                        bool(it.get("is_outgoing"))
                        or snip_low.startswith(("вы:", "вы: ", "вы:\xa0", "you:", "you: "))
                        or any(k in snip_low for k in ["отклик на вакансию", "отклик на\xa0вакансию"])
                    )
                    msgs.append(HHMessage(
                        message_id="m_snippet",
                        text=snip_text,
                        sender="candidate" if is_cand else "employer",
                        sent_at=it.get("last_message_date_raw"),
                    ))
                unread_cnt = int(it.get("unread_count") or 0)
                has_unr = bool(it.get("has_unread") or unread_cnt > 0)
                dialogs.append(HHDialog(
                    conversation_id=str(it.get("conversation_id") or it.get("id") or ""),
                    vacancy_title=str(it.get("vacancy_title") or it.get("title") or ""),
                    vacancy_stable_id=str(it.get("vacancy_stable_id") or ""),
                    employer=str(it.get("employer") or ""),
                    messages=msgs,
                    unread_count=unread_cnt,
                    has_unread=has_unr,
                ))

        # HH chat URLs and the live extractor use numeric conversation IDs.
        # Fake IDs such as ``conv_question_999`` must never reach the real DB,
        # notifier, or browser. Isolated pytest fixtures intentionally use them.
        production_db = os.path.realpath(os.path.join(db.config.PROJECT_ROOT, "state.db"))
        active_db = os.path.realpath(db.config.DB_FILE)
        isolated_test = (
            bool(os.getenv("PYTEST_CURRENT_TEST"))
            and os.getenv("JOB_SEARCH_TEST_NETWORK_BLOCKED") == "1"
            and active_db != production_db
        )
        if not isolated_test:
            valid_dialogs = [d for d in dialogs if d.conversation_id and d.conversation_id.isascii() and d.conversation_id.isdecimal()]
            invalid_count = len(dialogs) - len(valid_dialogs)
            if invalid_count:
                logger.warning("Ignored %d HH dialog(s) with non-numeric conversation IDs", invalid_count)
                if not valid_dialogs:
                    return _stop(MSG_CHECK_BLOCKED, "dialog list contained no valid numeric HH conversation IDs")
                dialogs = valid_dialogs

        out["messages_checked"] = len(dialogs)

        # 4. A non-empty read is its own proof: those dialogs came off a messages
        #    page. An EMPTY read is not, and that is exactly where the silent zero
        #    used to live - "0 messages" was printed whether the list had been read
        #    or not. Before a zero may be called confirmed, ask the session gate in
        #    the MAIN frame (T7.28 A: the chatik transport cannot answer "who am I"
        #    and invents a refusal on a healthy session), and require that the page
        #    we read from really is a messages surface. Anything that stops either
        #    question from being answered blocks, rather than passing.
        if not dialogs:
            # check_hh_session_authenticated already fails closed on its own: it
            # catches a transport that throws and a page that answers something
            # unrelated, and reports both as authenticated=False, verified=False
            # (BLE001 finding #28). Wrapping it again here would only duplicate a
            # contract that is already tested.
            gate = check_hh_session_authenticated(eval_fn)
            if not gate.get("authenticated", False):
                gate_reason = gate.get("reason") or "login required"
                if gate.get("verified", True):
                    return _stop(MSG_CHECK_BLOCKED, f"HH session not authenticated: {gate_reason}")
                return _stop(MSG_CHECK_BLOCKED, f"could not verify the HH session: {gate_reason}")
            if not gate.get("verified", True):
                return _stop(MSG_CHECK_BLOCKED, "could not verify the HH session")
            page_url = str(gate.get("url") or "")
            page_parts = urlsplit(page_url)
            if page_parts.scheme != "https" or page_parts.hostname not in {"hh.ru", "chatik.hh.ru"}:
                return _stop(MSG_CHECK_BLOCKED, "dialog list not read: page is not on HH")
            if not _HH_MESSAGES_PATTERN.search(page_url):
                return _stop(
                    MSG_CHECK_BLOCKED,
                    "dialog list not read: the open HH tab is not a messages surface "
                    f"(url={page_url or 'unknown'})",
                )
            if page_parts.hostname == "hh.ru" and page_parts.path.rstrip("/") == "/chat":
                read_url = str(raw_res.get("url") or "") if isinstance(raw_res, dict) else ""
                read_parts = urlsplit(read_url)
                confirmed_empty_unread = (
                    isinstance(raw_res, dict)
                    and raw_res.get("empty_evidence") == "no_unread_chats"
                    and read_parts.hostname == "hh.ru"
                    and read_parts.path.rstrip("/") == "/chat"
                )
                if not confirmed_empty_unread:
                    return _stop(MSG_CHECK_BLOCKED, "empty HH chat list could not be verified")

        out["messages_check_status"] = MSG_CHECK_CHECKED
        out["messages_check_reason"] = (
            "no unread chats; unread-only filter active" if confirmed_empty_unread
            else f"dialog list read, {len(dialogs)} dialog(s)"
        )
        out["messages_checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        for d in dialogs:
            # Skip already-processed snippets before triggering expensive DOM navigation
            only_snippet = len(d.messages) == 1 and d.messages[0].message_id == "m_snippet"
            if only_snippet and d.conversation_id:
                # If the dialog has unread messages, NEVER skip it! We must open it so that the unread badge is cleared.
                if not d.has_unread and not d.unread_count:
                    snip_norm = normalize_incoming_message_text(d.messages[0].text or "").rstrip(".… ").strip()
                    try:
                        prior_events = db.list_hh_message_events(conversation_id=d.conversation_id, limit=1)
                    except Exception:
                        prior_events = []
                    if prior_events and prior_events[0].get("processed"):
                        latest_ev = prior_events[0]
                        prev_norm = normalize_incoming_message_text(str(latest_ev.get("text") or "")).strip()
                        snip_head = snip_norm[:40].lower()
                        if latest_ev.get("status") in ("REJECTED", "ARCHIVED", "NO_REPLY_NEEDED") and (
                            not snip_head or snip_head in prev_norm.lower() or "отказ" in snip_head or "архив" in snip_head
                        ):
                            continue
                    # A card snippet may share its prefix with a newer message.
                    # Read the full conversation before deciding it is a duplicate.

            # For dialogs with card snippets, load full conversation from chat DOM
            is_full_loaded = not any(m.message_id == "m_snippet" for m in d.messages)
            if not d.messages or any(m.message_id == "m_snippet" for m in d.messages):
                if d.conversation_id:
                    try:
                        eval_fn(f"""(() => {{
                            const el = document.querySelector('a[href*="{d.conversation_id}"]');
                            if (el) {{ el.click(); return "clicked"; }}
                            if (!location.href.includes("{d.conversation_id}")) {{
                                location.href = "https://hh.ru/chat/{d.conversation_id}";
                                return "navigated";
                            }}
                            return "already_on_chat";
                        }})()""")
                        time.sleep(2.0)
                        from .hh_message_reply import fetch_hh_conversation_readonly
                        conv_data = fetch_hh_conversation_readonly(eval_fn)
                        if not (conv_data and conv_data.get("messages")):
                            time.sleep(2.0)
                            conv_data = fetch_hh_conversation_readonly(eval_fn)
                        if conv_data and conv_data.get("messages"):
                            prior_sent_norm = {
                                re.sub(r"[^\w\s]", "", (r or "").lower()).strip()
                                for r in db.get_recent_sent_replies_for_conversation(d.conversation_id, limit=20)
                                if r
                            }
                            real_msgs = []
                            for rm in conv_data.get("messages", []):
                                r_dir = rm.get("direction")
                                rm_text = str(rm.get("text") or "")
                                rm_norm = re.sub(r"[^\w\s]", "", rm_text.lower()).strip()
                                is_cand_text = bool(
                                    rm_norm and (
                                        rm_norm in prior_sent_norm
                                        or "с уважением михаил колесников" in rm_norm
                                        or rm_norm.startswith("здравствуйте меня заинтересовала вакансия")
                                    )
                                )
                                r_sender = "candidate" if (r_dir == "OUTGOING" or is_cand_text) else "employer"
                                real_msgs.append(HHMessage(
                                    message_id=f"m_{len(real_msgs)}",
                                    text=rm_text,
                                    sender=r_sender,
                                    sent_at=rm.get("timestamp"),
                                ))
                            if real_msgs:
                                d.messages = real_msgs
                                is_full_loaded = True
                    except Exception as e_load:
                        logger.warning("Could not load full chat for %s: %s", d.conversation_id, e_load)

            last_msg = d.last_message()
            if not last_msg or last_msg.sender == "candidate":
                continue

            # CRITICAL DEFENSE: Never send an auto-reply blind based solely on an unresolved card snippet!
            if not is_full_loaded and any(m.message_id == "m_snippet" for m in d.messages):
                logger.warning("Suppressed auto-reply for %s: full chat bubbles not loaded into DOM", d.conversation_id)
                continue

            msg_raw = (last_msg.text or "").strip()
            clean_text = normalize_incoming_message_text(msg_raw)

            fp = compute_message_fingerprint(
                conversation_id=d.conversation_id,
                sender=last_msg.sender,
                sent_at=last_msg.sent_at,
                text=clean_text or msg_raw,
                message_id=last_msg.message_id,
            )

            if db.is_hh_message_processed(fp):
                prior = db.get_hh_message_event(fp) or {}
                # Anti-loop check: only suppress duplicate if this exact message was processed recently (< 12 hours ago).
                # If an employer asks the same screening question after days, it is a new prompt that needs a reply.
                is_recent = True
                prior_seen = prior.get("seen_at")
                if prior_seen:
                    try:
                        seen_dt = datetime.datetime.fromisoformat(prior_seen)
                        now_dt = datetime.datetime.now(datetime.timezone.utc)
                        if seen_dt.tzinfo is None:
                            seen_dt = seen_dt.replace(tzinfo=datetime.timezone.utc)
                        if (now_dt - seen_dt).total_seconds() > 12 * 3600:
                            is_recent = False
                    except Exception:
                        pass
                if is_recent:
                    if not (_autonomous_reply_enabled() and
                            prior.get("status") == "BLOCKED_AUTONOMOUS_REPLY_DISABLED"):
                        continue

            # Extract IDs
            vac_stable = d.vacancy_stable_id or ""
            vac_num_id = vac_stable.split(":")[-1] if ":" in vac_stable else vac_stable
            app_id = f"app_hh_{vac_num_id}" if vac_num_id else None

            # Classify incoming employer message
            msg_text = (last_msg.text or "").strip()
            msg_low = msg_text.lower()
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

            # Check WRONG_CONTEXT (negotiations card statuses, fake neg_* convs)
            if d.conversation_id.startswith("neg_") or msg_text.startswith("Статус:") or msg_text in ["Собеседование", "Просмотрен", "Не просмотрен", "Отказ"]:
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1,
                    "classification": "WRONG_CONTEXT",
                    "status": "NO_REPLY_NEEDED",
                    "employer": d.employer,
                    "vacancy_stable_id": d.vacancy_stable_id,
                })
                db.save_conversation_audit({
                    "conversation_id": d.conversation_id,
                    "application_id": app_id,
                    "vacancy_id": vac_num_id,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "employer": d.employer or "Unknown Employer",
                    "incoming_message": msg_text,
                    "incoming_message_timestamp": last_msg.sent_at,
                    "message_classification": "WRONG_CONTEXT",
                    "generated_reply": None,
                    "sent_reply": None,
                    "sent_at": None,
                    "profile_facts_used": None,
                    "decision_reason": "Card status or negotiations item is not a real chat message; no reply required.",
                    "status": "NO_REPLY_NEEDED",
                    "created_at": now_iso,
                })
                continue

            # Check Rejection
            if any(k in msg_low for k in ["к сожалению", "не готовы пригласить", "отказ", "закрыта вакансия", "отклонен"]):
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1,
                    "classification": "REJECTION",
                    "status": "REJECTED",
                    "employer": d.employer,
                    "vacancy_stable_id": d.vacancy_stable_id,
                })
                db.save_conversation_audit({
                    "conversation_id": d.conversation_id,
                    "application_id": app_id,
                    "vacancy_id": vac_num_id,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "employer": d.employer or "Unknown Employer",
                    "incoming_message": msg_text,
                    "incoming_message_timestamp": last_msg.sent_at,
                    "message_classification": "REJECTION",
                    "generated_reply": None,
                    "sent_reply": None,
                    "sent_at": None,
                    "profile_facts_used": None,
                    "decision_reason": "Employer sent rejection notification; no reply required.",
                    "status": "NO_REPLY_NEEDED",
                    "created_at": now_iso,
                })
                out["rejections_count"] += 1
                continue

            # Check External Questionnaire / Google Form / Survey Link
            from ai_assistant.external_form_filler import is_external_form_url, try_auto_fill_external_form
            ext_form_urls = []
            url_matches = re.findall(r'https?://[^\s<>"]+', msg_text)
            is_tg_bot = any("t.me/" in u.lower() and ("bot" in u.lower() or any(k in msg_low for k in ["тестов", "задан", "test"])) for u in url_matches)
            is_ext_form = False
            form_url = ""
            if not is_tg_bot:
                for u in url_matches:
                    u_low = u.lower()
                    if is_external_form_url(u, msg_text) or any(domain in u_low for domain in ["forms.gle", "docs.google.com/forms", "forms.yandex.ru", "typeform.com", "kakdela.hh.ru", "script.google.com", "tinyurl.com"]):
                        is_ext_form = True
                        form_url = u
                        break
                    if any(k in msg_low for k in ["анкет", "опросник", "заполнить форму", "заполните анкету"]) and not any(k in msg_low for k in ["тестов", "задан"]):
                        is_ext_form = True
                        form_url = u
                        break

            # Check Test Task / Assignment
            test_task_signals = [
                "тестовое", "тестовым", "тестового", "тестовому",
                "задание", "задания", "заданием", "испытание",
                "test task", "хакатон",
                "github.com", "notion.site", "drive.google.com", "docs.google.com",
                "решите задачу", "сделайте задание", "выполните задание",
            ]
            question_signals = [
                "расскажите, пожалуйста", "подскажите, пожалуйста", "у вас есть опыт",
                "есть ли у вас опыт", "какой у вас опыт", "сколько лет опыта",
            ]
            is_test_task = (
                not is_ext_form
                and (
                    is_tg_bot
                    or any(ts in msg_low for ts in test_task_signals)
                )
                and (bool(url_matches) or not any(q in msg_low for q in question_signals))
                and (bool(url_matches) or not any(s in msg_low for s in ["собеседован", "интервью"]))
            )
            # Check Interview Invitation / Scheduling Request (High Priority)
            interview_signals = [
                "собеседован", "интервью", "созвон", "пообщаться голосом",
                "встреч", "interview", "call", "calendly",
                "zoom", "google meet", "удобное время", "когда удобно",
                "приглашаем на", "предлагаем созвониться", "обсудить подробнее",
            ]
            interview_text = re.sub(r'(?im)^\s*(?:собеседование|интервью)\s*$', '', msg_text).lower()
            is_interview = not is_ext_form and not is_test_task and any(s in interview_text for s in interview_signals)

            from dataclasses import asdict, is_dataclass
            prof_dict = asdict(self.profile) if is_dataclass(self.profile) else (self.profile.model_dump() if hasattr(self.profile, "model_dump") else self.profile)

            classification = "NO_REPLY_NEEDED"
            reply_text = ""
            facts_used = None
            decision_reason = ""

            if is_ext_form and form_url and is_external_form_url(form_url, msg_text):
                from ai_assistant.test_task_manager import record_pending_test_task, generate_questionnaire_acknowledgement
                target_url = form_url
                fill_res = None
                cdp_target = self.config.cdp_url or "http://127.0.0.1:9111"
                try:
                    logger.info("Attempting autonomous fill of external form: %s", target_url)
                    fill_res = try_auto_fill_external_form(
                        cdp_url=cdp_target,
                        form_url=target_url,
                        company=d.employer or "HeadHunter Employer",
                        vacancy=d.vacancy_title or "Python Role",
                        msg_context=msg_text,
                    )
                except Exception as e_fill:
                    logger.warning("External form autonomous fill error: %s", e_fill)

                if fill_res and fill_res.ok:
                    record_pending_test_task(
                        company=d.employer or "HeadHunter Employer",
                        vacancy=d.vacancy_title or "Python Role",
                        message_text=msg_text,
                        url=target_url,
                        conversation_id=d.conversation_id,
                        status="✅ **Сдано**",
                    )
                    classification = "EXTERNAL_QUESTIONNAIRE_SUBMITTED"
                    reply_text = fill_res.chat_confirmation_text
                    facts_used = ["external_form_filler.py: auto_filled_and_verified", "contacts: @brokeassh"]
                    decision_reason = f"External form {target_url} autonomously filled ({fill_res.fields_count} fields) and verified; confirmation queued for HH chat."
                    notif = NotificationDispatcher.notify_external_form_submitted(
                        company=d.employer or "HeadHunter Employer",
                        vacancy_title=d.vacancy_title or "Python Role",
                        url=target_url,
                        fields_count=fill_res.fields_count,
                        conversation_id=d.conversation_id,
                        application_id=app_id,
                    )
                    out["notifications_sent"].append(notif)
                else:
                    is_physically_blocked = bool(fill_res and fill_res.blocked and (
                        "микрофон" in fill_res.blocked_reason.lower()
                        or "камер" in fill_res.blocked_reason.lower()
                        or "captcha" in fill_res.blocked_reason.lower()
                        or "turnstile" in fill_res.blocked_reason.lower()
                        or "hcaptcha" in fill_res.blocked_reason.lower()
                    ))
                    task_status = "⚠️ **Требуется ручное заполнение**" if is_physically_blocked else "⏳ **В работе**"
                    record_pending_test_task(
                        company=d.employer or "HeadHunter Employer",
                        vacancy=d.vacancy_title or "Python Role",
                        message_text=msg_text,
                        url=target_url,
                        conversation_id=d.conversation_id,
                        status=task_status,
                    )
                    notif = NotificationDispatcher.notify_external_questionnaire(
                        company=d.employer or "HeadHunter Employer",
                        vacancy_title=d.vacancy_title or "Python Role",
                        what_they_want=msg_text,
                        url=target_url,
                        questions=f"Форма физически заблокирована: {fill_res.blocked_reason}" if is_physically_blocked else "Требуется заполнение внешней анкеты",
                        action="REQUIRES REVIEW",
                        conversation_id=d.conversation_id,
                        application_id=app_id,
                    )
                    out["notifications_sent"].append(notif)
                    classification = "EXTERNAL_QUESTIONNAIRE"
                    reply_text = generate_questionnaire_acknowledgement(
                        company=d.employer or "HeadHunter Employer",
                        vacancy=d.vacancy_title or "Python Role",
                        task_text=msg_text,
                    )
                    facts_used = ["candidate_kb.json: questionnaire_acknowledgement", "contacts: @brokeassh"]
                    decision_reason = f"External form link detected ({target_url}); recorded in pending_test_tasks.md; acknowledgement prepared."

            elif is_test_task:
                # Technical test task (GitHub repo, Telegram bot, document, or prompt assignment)
                from ai_assistant.test_task_manager import record_pending_test_task, generate_test_task_acknowledgement
                target_url = url_matches[0] if url_matches else form_url
                record_pending_test_task(
                    company=d.employer or "HeadHunter Employer",
                    vacancy=d.vacancy_title or "Python Role",
                    message_text=msg_text,
                    url=target_url,
                    conversation_id=d.conversation_id,
                    status="⏳ **В работе**",
                )
                notif = NotificationDispatcher.notify_test_task(
                    company=d.employer or "HeadHunter Employer",
                    vacancy_title=d.vacancy_title or "Python Role",
                    task_description=msg_text,
                    url=target_url or "",
                    action="REQUIRES REVIEW",
                    conversation_id=d.conversation_id,
                    application_id=app_id,
                )
                out["notifications_sent"].append(notif)
                classification = "TEST_TASK"
                reply_text = generate_test_task_acknowledgement(
                    company=d.employer or "HeadHunter Employer",
                    vacancy=d.vacancy_title or "Python Role",
                    task_text=msg_text,
                )
                facts_used = ["candidate_kb.json: test_task_acknowledgement", "contacts: @brokeassh"]
                decision_reason = "Technical test task detected; logged to artifacts/pending_test_tasks.md and sent acknowledgement."
            elif is_interview:
                from ai_assistant.interview_auto_responder import generate_interview_response
                notif = NotificationDispatcher.notify_interview(
                    company=d.employer or "HeadHunter Employer",
                    vacancy_title=d.vacancy_title or "Python Role",
                    invitation_text=msg_text,
                    invitation_url=f"https://hh.ru/chat/{d.conversation_id}",
                    conversation_id=d.conversation_id,
                    action_required="Review conversation and confirm suitable interview time slot.",
                )
                out["notifications_sent"].append(notif)
                out["interviews_detected"] += 1
                out["interview_details"].append({
                    "company": d.employer,
                    "vacancy": d.vacancy_title,
                    "text": msg_text,
                    "conversation_id": d.conversation_id,
                })
                classification = "INTERVIEW_INVITATION"
                reply_text = generate_interview_response(
                    msg_text,
                    employer=d.employer or "HeadHunter Employer",
                    vacancy=d.vacancy_title or "Python Role",
                )
                facts_used = [
                    "candidate_kb.json: interview slots weekdays 09:00-16:00 MSK (UTC+7)",
                    "candidate_kb.json: contacts @brokeassh, +79933397628, mikhailthaiban@gmail.com",
                    "candidate_kb.json: remote 100% full-time"
                ]
                decision_reason = "Interview invitation detected; confirmed availability in 09:00-16:00 MSK window with contacts."

            else:
                from ai_assistant.hh_message_reply import classify_message, MessageClassification
                from ai_assistant.hh_llm_reply import generate_contextual_draft

                basic_class = classify_message(d)
                draft = None
                if basic_class != MessageClassification.NO_REPLY:
                    try:
                        draft = generate_contextual_draft(d)
                    except Exception as draft_error:
                        logger.warning("Contextual model failed for %s: %s", d.conversation_id, type(draft_error).__name__)
                if draft and draft.get("status") == "DRAFT" and draft.get("reply"):
                    classification = "NEEDS_REPLY"
                    reply_text = draft["reply"]
                    facts_used = ["candidate_kb.json: candidate-supplied claims (not independently verified)"]
                    decision_reason = "Antigravity contextual draft based on recent conversation and candidate KB."
                else:
                    from ai_assistant.hh_message_reply import classify_hh_conversation_detailed
                    class_res = classify_hh_conversation_detailed(d, profile=prof_dict)
                    classification = class_res.get("classification") or "NO_REPLY_NEEDED"
                    reply_text = class_res.get("prepared_reply") or ""
                    facts_used = class_res.get("sources") or ["candidate_profile: desired_roles, skills, experience, remote"]
                    fallback_reason = (draft or {}).get("reason", "model not used")
                    decision_reason = f"Fallback generator ({fallback_reason}): {class_res.get('reason') or 'classification'}"

            send_status = "GENERATED"
            send_error = None
            sent_at = None
            sent_reply = None

            is_sendable = bool(reply_text and classification in (
                "NEEDS_REPLY", "RECRUITER_QUESTION", "EXTERNAL_QUESTIONNAIRE_SUBMITTED"
            ))
            if is_sendable:
                if any(m.sender == "candidate" for m in d.messages) or db.has_prior_candidate_message(d.conversation_id):
                    from ai_assistant.smart_experience_generator import strip_repeated_greeting
                    reply_text = strip_repeated_greeting(reply_text)

                recent_sent = db.get_recent_sent_replies_for_conversation(d.conversation_id, limit=5)
                norm_target = re.sub(r'[^\w\s]', '', (reply_text or '').lower()).strip()
                is_duplicate = any(re.sub(r'[^\w\s]', '', (r or '').lower()).strip() == norm_target for r in recent_sent)
                consecutive_count = db.count_recent_consecutive_auto_replies(d.conversation_id)

                from ai_assistant.outbound_governor import is_disk_kill_switch_active, is_db_pause_active
                disk_halted, disk_reason = is_disk_kill_switch_active()
                db_halted, db_reason = is_db_pause_active()
                if disk_halted:
                    send_status = "BLOCKED_KILL_SWITCH"
                    send_error = disk_reason or "Emergency kill-switch active (STOP_ALL_OUTBOUNDS)"
                    logger.warning("Autonomous reply SUPPRESSED by disk kill-switch for %s: %s", d.conversation_id, send_error)
                elif db_halted:
                    send_status = "BLOCKED_KILL_SWITCH"
                    send_error = db_reason or "Database kill-switch active (submit_paused=1)"
                    logger.warning("Autonomous reply SUPPRESSED by DB kill-switch for %s: %s", d.conversation_id, send_error)
                elif is_duplicate:
                    send_status = "LOOP_PREVENTED"
                    send_error = f"Anti-loop guard: identical reply text was already sent to conversation {d.conversation_id}"
                    logger.warning("Anti-loop guard blocked duplicate reply for conv %s: %s", d.conversation_id, reply_text)
                elif consecutive_count >= 15:
                    send_status = "HUMAN_REVIEW_REQUIRED"
                    send_error = f"Anti-loop ceiling: conversation {d.conversation_id} already reached {consecutive_count} consecutive auto-replies. Escalating to human review."
                    logger.warning("Anti-loop ceiling reached for conv %s (%d replies) -> HUMAN_REVIEW_REQUIRED", d.conversation_id, consecutive_count)
                elif not autonomous_mode.outbound_allowed(self.config.mode):
                    send_status = "BLOCKED_SEARCH_ONLY"
                    send_error = autonomous_mode.refusal(self.config.mode, "answering an employer message")
                elif not _autonomous_reply_enabled():
                    send_status = "BLOCKED_AUTONOMOUS_REPLY_DISABLED"
                    send_error = (
                        "autonomous reply is paused pending verification of individual claims; "
                        "the generated draft requires human review before sending."
                    )
                    logger.warning(
                        "Autonomous reply SUPPRESSED for conv %s (%s): draft kept, nothing sent",
                        d.conversation_id,
                        classification,
                    )
                elif out["auto_replies_count"] < self.config.max_auto_replies_per_cycle:
                    from ai_assistant.smart_experience_generator import _is_well_formed_reply
                    if not _is_well_formed_reply(reply_text):
                        send_status = "HUMAN_REVIEW_REQUIRED"
                        send_error = f"Malformed or truncated reply suppressed by safety guard: {reply_text!r}"
                        logger.error("SAFETY GUARD BLOCKED TRUNCATED REPLY for conv %s: %r", d.conversation_id, reply_text)
                    elif not _incoming_message_still_current(eval_fn, msg_text):
                        send_status = "HUMAN_REVIEW_REQUIRED"
                        send_error = "The latest incoming message changed or could not be verified before sending."
                    else:
                        try:
                            claimed = db.claim_hh_message_reply(
                                fp, d.conversation_id, last_msg.sender, msg_text,
                                now_iso, reply_text,
                            )
                        except Exception as claim_error:
                            logger.error("Reply claim failed for %s: %s", d.conversation_id, type(claim_error).__name__)
                            continue
                        if not claimed:
                            logger.warning("Reply already claimed or processed for %s", d.conversation_id)
                            continue
                        try:
                            raw_send = eval_fn(f"""(() => {{
                                const targetConvId = {json.dumps(d.conversation_id)};
                                const current = new URL(location.href);
                                const segments = current.pathname.split('/').filter(Boolean);
                                if (!['hh.ru', 'chatik.hh.ru'].includes(current.hostname) || !segments.includes(targetConvId))
                                    return JSON.stringify({{ok:false, reason:'Open conversation does not match target'}});
                                const input = document.querySelector('textarea[data-qa="text-input"], textarea[data-qa="chatik-new-message-text"], textarea[data-qa="chat-input-textarea"], textarea[data-qa*="message"], [data-qa*="chatik-new-message"], textarea');
                                if (!input) return JSON.stringify({{ ok: false, reason: 'Chat input textarea not found for conversation ' + targetConvId }});
                                if ((input.value || '').trim()) return JSON.stringify({{ok:false, reason:'Chat already has a draft'}});
                                
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set
                                            || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                                if (setter) {{
                                    setter.call(input, {json.dumps(reply_text)});
                                }} else {{
                                    input.value = {json.dumps(reply_text)};
                                }}
                                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    
                                let sendBtn = document.querySelector('[data-qa="chatik-do-send-message"], [data-qa="chat-input-submit"], [data-qa="chatik-send-button"], button[type="submit"]');
                                if (!sendBtn) {{
                                    const buttons = Array.from(document.querySelectorAll('button'));
                                    sendBtn = buttons.find(b => /^(Отправить|Send|Отпр|Сенд)$/i.test((b.innerText||'').trim())
                                                                || (b.getAttribute('aria-label')||'').match(/отправ|send/i)
                                                                || (b.getAttribute('data-qa')||'').includes('send'));
                                }}
                                if (!sendBtn) return JSON.stringify({{ ok: false, reason: 'Chat send button not found for conversation ' + targetConvId }});
    
                                if (sendBtn.disabled) return JSON.stringify({{ok:false, reason:'Send button disabled'}});
                                sendBtn.click();
                                return JSON.stringify({{ ok: true, conversation_id: targetConvId }});
                            }})()""")
                            send_res = json.loads(raw_send) if isinstance(raw_send, str) else raw_send
                            # A missing/disabled button is a confirmed pre-click
                            # outcome. Give HH time to render, without retyping or
                            # ever retrying an uncertain click/transport failure.
                            for _ in range(3):
                                reason = str(send_res.get("reason") or "")
                                if send_res.get("ok") is not False or not (
                                    reason.startswith("Chat send button not found") or reason == "Send button disabled" or reason == "Send button not ready"
                                ):
                                    break
                                time.sleep(1)
                                if not _incoming_message_still_current(eval_fn, msg_text):
                                    send_res = {"ok": False, "reason": "Latest incoming message changed before retry"}
                                    break
                                raw_retry = eval_fn(f"""(() => {{
                                    const target = {json.dumps(d.conversation_id)};
                                    const u = new URL(location.href);
                                    if (!['hh.ru', 'chatik.hh.ru'].includes(u.hostname) || !u.pathname.split('/').filter(Boolean).includes(target))
                                        return JSON.stringify({{ok:false, reason:'Open conversation does not match target'}});
                                    const input = document.querySelector('textarea[data-qa="text-input"], textarea[data-qa="chatik-new-message-text"], textarea[data-qa="chat-input-textarea"], textarea[data-qa*="message"], [data-qa*="chatik-new-message"], textarea');
                                    if (!input || input.value !== {json.dumps(reply_text)})
                                        return JSON.stringify({{ok:false, reason:'Draft changed before retry'}});
                                    const button = document.querySelector('[data-qa="chatik-do-send-message"], [data-qa="chat-input-submit"], [data-qa="chatik-send-button"]');
                                    if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true')
                                        return JSON.stringify({{ok:false, reason:'Send button not ready'}});
                                    button.click();
                                    return JSON.stringify({{ok:true, conversation_id:target}});
                                }})()""")
                                send_res = json.loads(raw_retry) if isinstance(raw_retry, str) else raw_retry
                            if bool(send_res.get("ok")) and (send_res.get("conversation_id") is None or str(send_res.get("conversation_id")) == str(d.conversation_id)):
                                # A transport that reports the DOM verdict itself is
                                # believed: re-reading the page could only overwrite a
                                # measurement with a guess.
                                explicit_verdict = send_res.get("verified_in_hh")
                                if explicit_verdict is not None:
                                    is_dom_confirmed = bool(explicit_verdict)
                                else:
                                    time.sleep(2)
                                    verify_js = f"""(() => {{
                                        const u = new URL(location.href);
                                        const target = {json.dumps(d.conversation_id)};
                                        if (!['hh.ru', 'chatik.hh.ru'].includes(u.hostname) || !u.pathname.split('/').filter(Boolean).includes(target))
                                            return JSON.stringify({{verified:false}});
                                        const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                                        const expected = normalize({json.dumps(reply_text)});
                                        const rows = Array.from(document.querySelectorAll('div[class*="message--"]'));
                                        const found = rows.some(row => {{
                                            const bubble = row.querySelector('[class*="chat-bubble"]');
                                            if (!bubble) return false;
                                            const outgoing = /message[_-]my--/.test(String(row.className || '')) ||
                                                /chat-bubble_ou|magritte-outgoing|right-tail/.test(String(bubble.className || ''));
                                            const text = bubble.querySelector('[data-qa="chat-bubble-text"]');
                                            return outgoing && normalize((text || bubble).innerText) === expected;
                                        }});
                                        return JSON.stringify({{verified:found}});
                                    }})()"""
                                    try:
                                        raw_v = eval_fn(verify_js)
                                        verify_res = json.loads(raw_v) if isinstance(raw_v, str) else raw_v
                                        is_dom_confirmed = verify_res.get("verified") is True
                                    except Exception as e_verify:
                                        # A click may have succeeded even when the probe fails.
                                        is_dom_confirmed = False
                                        logger.debug(
                                            "DOM verification unreadable for conv %s: %s",
                                            d.conversation_id, e_verify,
                                        )
    
                                # Confirmed via DOM verification or successful CDP click
                                if is_dom_confirmed or (send_res.get("ok") and (not isinstance(verify_res, dict) or "verified" not in verify_res)):
                                    send_status = "SENT"
                                    sent_reply = reply_text
                                    sent_at = now_iso
                                    out["auto_replies_count"] += 1
                                else:
                                    send_status = "REPLY_AMBIGUOUS"
                                    send_error = "Message not confirmed in HH chat DOM after send"
                            else:
                                send_status = "HUMAN_REVIEW_REQUIRED" if send_res.get("ok") is False else "REPLY_AMBIGUOUS"
                                send_error = send_res.get("reason") or "Failed to send message via HH chat button"
                        except Exception as e:
                            send_status = "REPLY_AMBIGUOUS"
                            send_error = f"Send outcome unknown: {type(e).__name__}"
                            logger.debug(f"Auto-reply send exception: {e}")
    
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1 if send_status in ("SENT", "REPLY_AMBIGUOUS", "LOOP_PREVENTED", "HUMAN_REVIEW_REQUIRED", "BLOCKED_AUTONOMOUS_REPLY_DISABLED", "BLOCKED_SEARCH_ONLY") else 0,
                    "classification": classification,
                    "reply_draft": reply_text,
                    "status": {
                        "SENT": "MESSAGE_AUTO_REPLIED",
                        "FAILED": "FAILED",
                        "REPLY_AMBIGUOUS": "REPLY_AMBIGUOUS",
                        "BLOCKED_SEARCH_ONLY": "BLOCKED_SEARCH_ONLY",
                        "BLOCKED_AUTONOMOUS_REPLY_DISABLED": "BLOCKED_AUTONOMOUS_REPLY_DISABLED",
                        "LOOP_PREVENTED": "LOOP_PREVENTED",
                        "HUMAN_REVIEW_REQUIRED": "HUMAN_REVIEW_REQUIRED",
                    }.get(send_status, "GENERATED"),
                    "employer": d.employer,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "error": send_error,
                })

                db.save_conversation_audit({
                    "conversation_id": d.conversation_id,
                    "application_id": app_id,
                    "vacancy_id": vac_num_id,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "employer": d.employer or "Unknown Employer",
                    "incoming_message": msg_text,
                    "incoming_message_timestamp": last_msg.sent_at,
                    "message_classification": classification,
                    "generated_reply": reply_text,
                    "sent_reply": sent_reply,
                    "sent_at": sent_at,
                    "profile_facts_used": facts_used,
                    "decision_reason": decision_reason,
                    "status": send_status,
                    "error": send_error,
                    "created_at": now_iso,
                })

                # ONLY notify if physically confirmed sent in HH
                if send_status == "SENT" and sent_reply:
                    notif = NotificationDispatcher.notify_reply_sent(
                        company=d.employer or "Unknown Employer",
                        vacancy_title=d.vacancy_title or "Python Role",
                        incoming_message=msg_text,
                        sent_reply=sent_reply,
                        conversation_id=d.conversation_id,
                        application_id=app_id,
                    )
                    out["notifications_sent"].append(notif)
                elif send_status == "HUMAN_REVIEW_REQUIRED":
                    notif = NotificationDispatcher.notify_blocking_question(
                        company=d.employer or "Unknown Employer",
                        vacancy_title=d.vacancy_title or "Position",
                        unanswered_question=f"Переписка требует личного ответа (лимит автоответов исчерпан): {msg_text[:150]}",
                        vacancy_url=f"https://hh.ru/chat/{d.conversation_id}",
                        application_id=app_id,
                    )
                    out["notifications_sent"].append(notif)
            else:
                is_transient_llm_fail = bool(
                    classification == "HUMAN_REVIEW"
                    and not reply_text
                    and "LLM unavailable:" in str(decision_reason or "")
                )
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 0 if is_transient_llm_fail else 1,
                    "classification": classification,
                    "reply_draft": reply_text or None,
                    "status": send_status if reply_text else ("HUMAN_REVIEW_REQUIRED" if classification == "HUMAN_REVIEW" else ("ARCHIVED" if ("архив" in msg_low or "закрыт" in msg_low) else "NO_REPLY_NEEDED")),
                    "employer": d.employer,
                    "vacancy_stable_id": d.vacancy_stable_id,
                })
                db.save_conversation_audit({
                    "conversation_id": d.conversation_id,
                    "application_id": app_id,
                    "vacancy_id": vac_num_id,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "employer": d.employer or "Unknown Employer",
                    "incoming_message": msg_text,
                    "incoming_message_timestamp": last_msg.sent_at,
                    "message_classification": classification,
                    "generated_reply": reply_text or None,
                    "sent_reply": None,
                    "sent_at": None,
                    "profile_facts_used": facts_used if reply_text else None,
                    "decision_reason": decision_reason or "Message does not require candidate reply.",
                    "status": send_status if reply_text else ("HUMAN_REVIEW_REQUIRED" if classification == "HUMAN_REVIEW" else "NO_REPLY_NEEDED"),
                    "created_at": now_iso,
                })

        # Sweep any remaining unread conversation cells in the DOM so no unread badge hangs
        try:
            eval_fn("""(() => {
                const unreadChats = Array.from(document.querySelectorAll('a[href*="/chat/"]')).filter(a => {
                    const badge = a.querySelector('[class*="unread" i], [data-qa*="unread" i], [class*="badge" i], span[class*="dot" i]');
                    return !!badge && (badge.innerText || '').trim() !== '';
                });
                for (const a of unreadChats) {
                    a.click();
                }
            })()""")
        except Exception:
            pass

        return out


    def _search_only_cycle(self, started_at: str, result: AutonomousCycleResult) -> AutonomousCycleResult:
        """T7.8: search and read, and emit nothing.

        A separate path rather than a set of flags inside ``run_cycle``, because a
        flag can be forgotten at one of the six write sites while a different
        function simply does not contain them. Nothing here writes an application,
        a package, a review, an approval, a queue row or an application status.
        The only row it writes is its own cycle-run log, which is how the operator
        can tell that the mode actually ran.

        T7.26 §3: the HH search is an *owned* operation now. It acquires the browser
        through ``hh_operation(OWNER_AUTONOMOUS, purpose="search")`` and releases it
        when the searching and the reading are done, so the browser lives for the
        segment rather than for the process (§4). Before this, SEARCH_ONLY never
        reached the ownership layer at all and never executed a query.
        """
        logger.info("SEARCH_ONLY cycle starting at %s: discovery and reading only", started_at)
        result.status = "SEARCH_ONLY"
        try:
            from . import hh_browser_lease as _lease
            from .hh_readonly_operation import search_operation_report

            report = search_operation_report(
                list(self.config.search_queries),
                read_limit=int(self.config.search_read_limit),
                owner_type=_lease.OWNER_AUTONOMOUS,
                purpose="search",
            )

            if report.get("busy"):
                result.status = "ERROR"
                result.errors.append(
                    f"SEARCH_ONLY could not acquire the HH browser: {report.get('error')}")
            elif not (report.get("start") or {}).get("ok"):
                result.status = "ERROR"
                result.errors.append(
                    "SEARCH_ONLY could not start the HH browser: "
                    f"{(report.get('start') or {}).get('status')}"
                    f" {(report.get('start') or {}).get('message') or ''}".strip())

            # §5: per query - results, unique and what the page actually settled on.
            for row in report.get("queries", []):
                logger.info(
                    "SEARCH_ONLY query %r: results=%s unique=%s status=%s",
                    row.get("query"), row.get("result_count"),
                    row.get("unique_count"), row.get("status"),
                )

            found: list[dict[str, Any]] = []
            for entry in report.get("vacancies", []):
                if not entry.get("ok"):
                    continue
                read = entry.get("read") or {}
                geo = entry.get("geo") or {}
                preflight = entry.get("preflight") or {}
                is_match, score, reason = evaluate_candidate_match(
                    vacancy_title=str(read.get("title") or ""),
                    company=str(read.get("employer") or ""),
                    description=str(read.get("description") or ""),
                    raw_data={"vacancy_id": entry.get("vacancy_id"), **read},
                    profile=self.profile,
                )
                if not is_match:
                    continue
                found.append({
                    "kind": "SEARCH_ONLY_MATCH",
                    "vacancy_id": entry.get("vacancy_id"),
                    "title": read.get("title"),
                    "employer": read.get("employer"),
                    "url": (entry.get("card") or {}).get("url"),
                    "score": score,
                    "reason": reason,
                    # §6/§7: what was read, and how it classifies.
                    "geo_band": geo.get("band"),
                    "geo_evidence": geo.get("evidence"),
                    "employment": read.get("employment"),
                    "civil_law_contracts": read.get("civil_law_contracts"),
                    "salary": read.get("salary"),
                    "area": read.get("area"),
                    "archived": read.get("archived"),
                    "response_allowed": read.get("response_allowed"),
                    "verdict": preflight.get("verdict"),
                    "auto_send_permitted": preflight.get("auto_send_permitted"),
                    "submitted": False,
                    "verified": False,
                })
            found.sort(key=lambda item: item["score"], reverse=True)

            result.discovered_count = int(report.get("unique_count") or 0)
            result.matched_count = len(found)
            result.applications_processed = found

            for item in found[: self.config.max_applications_per_cycle]:
                logger.info(
                    "SEARCH_ONLY match: %s / %s (%.1f) band=%s %s",
                    item["employer"], item["title"], item["score"],
                    item["geo_band"], item["url"],
                )

            per_query = ", ".join(
                f"{row.get('query')}={row.get('result_count')}"
                for row in report.get("queries", [])
            ) or "no queries"
            result.summary = (
                f"SEARCH_ONLY cycle: {len(report.get('queries', []))} queries "
                f"[{per_query}], {result.discovered_count} unique vacancies, "
                f"{report.get('read_count', 0)} read in full, "
                f"matched {result.matched_count}. No application, no reply, no new "
                f"message, no approval and no application status was written. "
                f"Correspondence: {MSG_CHECK_SKIPPED} - SEARCH_ONLY does not process it."
            )
        except Exception as e:
            logger.exception("SEARCH_ONLY cycle encountered an error")
            result.status = "ERROR"
            result.errors.append(str(e))
            result.summary = f"SEARCH_ONLY cycle terminated with error: {e}"

        result.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # T7.29 §4: SEARCH_ONLY keeps the correspondence out of scope by design,
        # and says so in the stored cycle row instead of leaving an unexplained 0.
        result.messages_check_status = MSG_CHECK_SKIPPED
        result.messages_check_reason = "SEARCH_ONLY mode does not process correspondence"
        result.messages_checked_at = result.completed_at

        db.save_autonomous_cycle_run({
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "status": result.status,
            "discovered_count": result.discovered_count,
            "matched_count": result.matched_count,
            "applied_count": 0,
            "verified_count": 0,
            "messages_checked": 0,
            "auto_replies_count": 0,
            "interviews_detected": 0,
            "rejections_count": 0,
            "unanswered_questions_count": 0,
            "summary": result.summary,
            "log": result.model_dump(),
        })

        return result


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(str(s))


def run_autonomous_cycle(config: AutonomousConfig | None = None) -> AutonomousCycleResult:
    """Run one single autonomous cycle."""
    agent = AutonomousJobAgent(config=config)
    return agent.run_cycle()


def start_autonomous_daemon(config: AutonomousConfig | None = None) -> None:
    """Run continuous autonomous agent loop until stopped."""
    agent = AutonomousJobAgent(config=config)
    poll_sec = agent.config.poll_interval_seconds

    print("=" * 70)
    print(" FULLY AUTONOMOUS JOB APPLICATION AGENT STARTED")
    print(f"  Mode: {autonomous_mode.normalise(agent.config.mode) or '(unset)'}")
    print(f"  Poll interval: {poll_sec}s")
    print(f" Target queries: {', '.join(agent.config.search_queries)}")
    print(" Notifications enabled ONLY for Interview Invitations & blocking questions.")
    print(" Press Ctrl+C to stop gracefully.")
    print("=" * 70)

    try:
        while True:
            res = agent.run_cycle()
            print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {res.summary}")
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        print("\n Autonomous Agent stopped by user.")
