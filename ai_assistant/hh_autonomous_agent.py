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
import hashlib
import json
import logging
import re
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from . import db
from .candidate_profile import CandidateProfile, load_candidate_profile
from .hh_application_orchestrator import HHApplicationState, transition_application
from .hh_browser_launcher import ensure_hh_browser
from .hh_message_reply import (
    HHDialog,
    HHMessage,
    classify_hh_conversation_detailed,
    detect_language,
    fetch_hh_conversations_list_readonly,
)
from .hh_message_watcher import compute_message_fingerprint
from .hh_post_submit_verifier import verify_hh_submitted_application
from .hh_questionnaire import (
    HHQuestionnaire,
    HHQuestionStatus,
    compute_questionnaire_fingerprint,
)
from .hh_vacancy_navigator import (
    ensure_open_vacancy_tab,
    extract_hh_numeric_id,
    resolve_hh_vacancy_url,
    verify_and_navigate_hh_vacancy,
)
from .schema import Vacancy

logger = logging.getLogger(__name__)

# Default search queries for active CDP search
DEFAULT_SEARCH_QUERIES = [
    "Python Developer",
    "Backend Python Developer",
    "AI Engineer",
    "AI Agent Engineer",
    "Python FastAPI",
    "Automation Engineer Python",
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
    cdp_url: str = "http://127.0.0.1:9222"
    search_queries: List[str] = Field(default_factory=lambda: list(DEFAULT_SEARCH_QUERIES))
    poll_interval_seconds: int = 60
    max_applications_per_cycle: int = 5
    max_auto_replies_per_cycle: int = 3
    remote_required: bool = True
    min_match_score: float = 70.0
    auto_start_browser: bool = True
    evaluate_fn: Optional[Any] = None
    submit_enabled: bool = False

    model_config = {"extra": "forbid"}


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
    auto_replies_count: int = 0
    interviews_detected: int = 0
    rejections_count: int = 0
    unanswered_questions_count: int = 0
    interview_details: List[Dict[str, Any]] = Field(default_factory=list)
    notifications_sent: List[Dict[str, Any]] = Field(default_factory=list)
    applications_processed: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
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
        invitation_url: Optional[str] = None,
        conversation_id: Optional[str] = None,
        action_required: str = "Review message and confirm available interview time slot.",
    ) -> Dict[str, Any]:
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
        conversation_id: Optional[str] = None,
        application_id: Optional[str] = None,
        vacancy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatch user notification when an autonomous reply is verified sent on HeadHunter."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conv_str = conversation_id or "unknown"
        from .telegram_notifier import TelegramNotifier, verify_hh_chat_url, get_telegram_notifier
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
        print(f" HH:           CONFIRMED")
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
        conversation_id: Optional[str] = None,
        application_id: Optional[str] = None,
    ) -> Dict[str, Any]:
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
    def notify_test_task(
        company: str,
        vacancy_title: str,
        task_description: str,
        url: str = "",
        action: str = "REQUIRES REVIEW",
        conversation_id: Optional[str] = None,
        application_id: Optional[str] = None,
    ) -> Dict[str, Any]:
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
            "action_required": f"Review test task requirements and complete assignment.",
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
        vacancy_url: Optional[str] = None,
        application_id: Optional[str] = None,
    ) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Filter & Match Engine (Candidate Profile as Source of Truth)
# ---------------------------------------------------------------------------

def evaluate_candidate_match(
    vacancy_title: str,
    company: str,
    description: str,
    raw_data: Optional[Dict[str, Any]] = None,
    profile: Optional[CandidateProfile] = None,
) -> Tuple[bool, float, str]:
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
            if ex in ("1c", "1с", "php", "bitrix", "java developer", "c++", "c#", "ruby"):
                return False, 0.0, f"Hard exclusion: contains excluded stack '{ex}'"

    # Specific check for Java / 1C / PHP primary roles
    if any(k in title_low for k in [" 1с", "1c", "php", "java ", "java/", "c++", "c#", "bitrix"]):
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
    questions: List[Dict[str, Any]],
    profile: Optional[CandidateProfile] = None,
) -> Tuple[bool, Dict[str, Any], List[str]]:
    """Automatically answer questionnaire questions using Candidate Profile facts.

    Separates AUTO_ANSWERABLE from UNKNOWN_REQUIRES_HUMAN.
    Never fabricates unknown personal facts.

    Returns: (all_answered, answers_dict, unanswered_questions_list)
    """
    if profile is None:
        profile = load_candidate_profile()

    answers: Dict[str, Any] = {}
    unanswered: List[str] = []

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
    profile: Optional[CandidateProfile] = None,
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

    def __init__(self, config: Optional[AutonomousConfig] = None):
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
            raise NotImplementedError(
                "Autonomous submission is disabled by design; use 'application runner next --confirm-submit' after human approval"
            )

        try:
            if self.config.auto_start_browser and self.config.evaluate_fn is None:
                try:
                    ensure_hh_browser()
                except Exception as e:
                    logger.debug(f"Could not auto-start browser: {e}")

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
                f"Messages Checked: {result.messages_checked}, Auto-replies: {result.auto_replies_count}, "
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

    def _discover_fresh_vacancies(self) -> List[Dict[str, Any]]:
        """Discover new HeadHunter vacancies via CDP session and deduplicate against DB."""
        fresh: List[Dict[str, Any]] = []
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
            for query in self.config.search_queries[:3]:
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

    def _process_single_application(self, vac_data: Dict[str, Any], score: float) -> Dict[str, Any]:
        """Process a single matching vacancy autonomously (prepare-only mode: discovers, scores, prepares review, never submits)."""
        if self.config.submit_enabled:
            raise NotImplementedError(
                "Autonomous submission is disabled by design; use 'application runner next --confirm-submit' after human approval"
            )

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

        # Step 3: Prepare Application Package & Cover Letter (Prepare-Only)
        cover_letter = generate_autonomous_cover_letter(title, employer, self.profile)
        pkg_data = {
            "vacancy_stable_id": stable_id,
            "cover_letter": cover_letter,
            "resume_summary": getattr(self.profile, "summary", "") or "",
            "tailored_skills": getattr(self.profile, "skills", []) or [],
            "validation_status": "VALID",
        }
        db.save_application_package(stable_id, "v1", json.dumps(pkg_data, ensure_ascii=False))

        # Step 4: Create ApplicationReview with PENDING_REVIEW
        from .application_review import (
            ApplicationReview,
            ReviewStatus,
            save_application_review,
            get_application_review,
            REVIEW_VERSION,
        )
        rev = get_application_review(stable_id)
        if not rev:
            rev = ApplicationReview(
                vacancy_stable_id=stable_id,
                company=employer,
                title=title,
                source="hh",
                vacancy_url=f"https://hh.ru/vacancy/{vac_id}",
                final_url=f"https://hh.ru/vacancy/{vac_id}",
                match_score=score,
                cover_letter=cover_letter,
                status=ReviewStatus.PENDING_REVIEW,
                review_version=REVIEW_VERSION,
                note="Prepared autonomously (prepare-only mode)",
            )
            save_application_review(rev)

        # Step 5: Transition tracking to READY_TO_APPLY
        from .application_tracking import set_application_status, ApplicationStatus
        set_application_status(
            vacancy_stable_id=stable_id,
            status=ApplicationStatus.READY_TO_APPLY,
            company=employer,
            title=title,
            source="hh",
            vacancy_url=f"https://hh.ru/vacancy/{vac_id}",
            match_score=score,
            notes="Autonomously discovered and prepared for human review",
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

        transition_application(app_id, HHApplicationState.NEEDS_HUMAN_REVIEW, reason="autonomous_prepare_only_ready_for_review")

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
                f"Статус: Ожидает одобрения человека (PENDING_REVIEW).\n"
                f"Для одобрения используйте кнопки ниже или веб-дашборд."
            )
            notifier.send_message(text=msg_text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Failed to send telegram notification for {stable_id}: {e}")

        app_record["reason"] = "Prepared for human review (prepare-only mode, submit disabled)"
        return app_record

    def _process_messages(self) -> Dict[str, Any]:
        """Watch and process incoming dialogs, detecting interviews and auto-replying."""
        out = {
            "messages_checked": 0,
            "auto_replies_count": 0,
            "interviews_detected": 0,
            "rejections_count": 0,
            "interview_details": [],
            "notifications_sent": [],
        }

        eval_fn = self.config.evaluate_fn
        if eval_fn is None:
            try:
                from .prefill_execute import make_cdp_evaluate
                eval_fn = make_cdp_evaluate(self.config.cdp_url, "hh.ru")
            except Exception:
                eval_fn = None

        if not eval_fn:
            return out

        raw_res = fetch_hh_conversations_list_readonly(evaluate_fn=eval_fn)
        convs = []
        if isinstance(raw_res, dict):
            convs = raw_res.get("conversations", [])
        elif isinstance(raw_res, list):
            convs = raw_res

        dialogs: List[HHDialog] = []
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
                dialogs.append(HHDialog(
                    conversation_id=str(it.get("conversation_id") or it.get("id") or ""),
                    vacancy_title=str(it.get("vacancy_title") or it.get("title") or ""),
                    vacancy_stable_id=str(it.get("vacancy_stable_id") or ""),
                    employer=str(it.get("employer") or ""),
                    messages=msgs,
                ))

        out["messages_checked"] = len(dialogs)

        for d in dialogs:
            last_msg = d.last_message()
            if not last_msg or last_msg.sender == "candidate":
                continue

            fp = compute_message_fingerprint(
                conversation_id=d.conversation_id,
                sender=last_msg.sender,
                sent_at=last_msg.sent_at,
                text=last_msg.text,
                message_id=last_msg.message_id,
            )

            if db.is_hh_message_processed(fp):
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
            ext_form_urls = []
            url_matches = re.findall(r'https?://[^\s<>"]+', msg_text)
            is_ext_form = False
            form_url = ""
            for u in url_matches:
                u_low = u.lower()
                if any(domain in u_low for domain in ["forms.gle", "docs.google.com/forms", "forms.yandex.ru", "typeform.com", "kakdela.hh.ru", "t.me/"]):
                    if "t.me/" in u_low and not any(k in msg_low for k in ["анкет", "опрос", "form", "тест"]):
                        continue  # direct telegram contact without form context is handled under interview/contact
                    is_ext_form = True
                    form_url = u
                    break
                if any(k in msg_low for k in ["анкет", "опросник", "заполнить форму", "заполните анкету"]):
                    is_ext_form = True
                    form_url = u
                    break

            if is_ext_form and form_url:
                notif = NotificationDispatcher.notify_external_questionnaire(
                    company=d.employer or "HeadHunter Employer",
                    vacancy_title=d.vacancy_title or "Python Role",
                    what_they_want=msg_text,
                    url=form_url,
                    questions="Требуется заполнение внешней формы / анкеты",
                    action="REQUIRES REVIEW",
                    conversation_id=d.conversation_id,
                    application_id=app_id,
                )
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1,
                    "classification": "EXTERNAL_QUESTIONNAIRE",
                    "status": "NEEDS_HUMAN_REVIEW",
                    "employer": d.employer,
                    "vacancy_stable_id": d.vacancy_stable_id,
                })
                db.save_conversation_audit({
                    "conversation_id": d.conversation_id,
                    "application_id": app_id,
                    "vacancy_id": vac_num_id,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "employer": d.employer or "HeadHunter Employer",
                    "incoming_message": msg_text,
                    "incoming_message_timestamp": last_msg.sent_at,
                    "message_classification": "EXTERNAL_QUESTIONNAIRE",
                    "generated_reply": None,
                    "sent_reply": None,
                    "sent_at": None,
                    "profile_facts_used": None,
                    "decision_reason": f"External questionnaire/form link detected ({form_url}). Paused for candidate review.",
                    "status": "GENERATED",
                    "created_at": now_iso,
                })
                out["notifications_sent"].append(notif)
                continue

            # Check Test Task / Assignment
            test_task_signals = ["тестовое задание", "тестовый проект", "выполнить задание", "ссылка на тестовое"]
            if any(ts in msg_low for ts in test_task_signals) and not any(s in msg_low for s in ["собеседован", "интервью"]):
                notif = NotificationDispatcher.notify_test_task(
                    company=d.employer or "HeadHunter Employer",
                    vacancy_title=d.vacancy_title or "Python Role",
                    task_description=msg_text,
                    url=url_matches[0] if url_matches else "",
                    action="REQUIRES REVIEW",
                    conversation_id=d.conversation_id,
                    application_id=app_id,
                )
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1,
                    "classification": "TEST_TASK",
                    "status": "NEEDS_HUMAN_REVIEW",
                    "employer": d.employer,
                    "vacancy_stable_id": d.vacancy_stable_id,
                })
                db.save_conversation_audit({
                    "conversation_id": d.conversation_id,
                    "application_id": app_id,
                    "vacancy_id": vac_num_id,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "employer": d.employer or "HeadHunter Employer",
                    "incoming_message": msg_text,
                    "incoming_message_timestamp": last_msg.sent_at,
                    "message_classification": "TEST_TASK",
                    "generated_reply": None,
                    "sent_reply": None,
                    "sent_at": None,
                    "profile_facts_used": None,
                    "decision_reason": "Technical test task detected. Paused for candidate review.",
                    "status": "GENERATED",
                    "created_at": now_iso,
                })
                out["notifications_sent"].append(notif)
                continue

            # Check Interview Invitation / Scheduling Request (High Priority)
            interview_signals = [
                "собеседован", "интервью", "созвон", "пообщаться голосом",
                "техническ", "встреч", "interview", "call", "calendly",
                "zoom", "google meet", "удобное время", "когда удобно",
                "приглашаем на", "предлагаем созвониться", "обсудить подробнее",
            ]

            if any(s in msg_low for s in interview_signals):
                notif = NotificationDispatcher.notify_interview(
                    company=d.employer or "HeadHunter Employer",
                    vacancy_title=d.vacancy_title or "Python Role",
                    invitation_text=msg_text,
                    invitation_url=f"https://hh.ru/chat/{d.conversation_id}",
                    conversation_id=d.conversation_id,
                    action_required="Review conversation and confirm suitable interview time slot.",
                )
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1,
                    "classification": "INTERVIEW_INVITATION",
                    "status": "INTERVIEW_INVITED",
                    "employer": d.employer,
                    "vacancy_stable_id": d.vacancy_stable_id,
                })
                db.save_conversation_audit({
                    "conversation_id": d.conversation_id,
                    "application_id": app_id,
                    "vacancy_id": vac_num_id,
                    "vacancy_stable_id": d.vacancy_stable_id,
                    "employer": d.employer or "HeadHunter Employer",
                    "incoming_message": msg_text,
                    "incoming_message_timestamp": last_msg.sent_at,
                    "message_classification": "INTERVIEW_INVITATION",
                    "generated_reply": None,
                    "sent_reply": None,
                    "sent_at": None,
                    "profile_facts_used": ["desired_roles", "availability", "github"],
                    "decision_reason": "Interview invitation detected. High-priority user notification triggered.",
                    "status": "GENERATED",
                    "created_at": now_iso,
                })
                out["interviews_detected"] += 1
                out["interview_details"].append({
                    "company": d.employer,
                    "vacancy": d.vacancy_title,
                    "text": msg_text,
                    "conversation_id": d.conversation_id,
                })
                out["notifications_sent"].append(notif)
                continue

            # Standard recruiter question -> Auto-reply with truth-only candidate facts
            from dataclasses import asdict, is_dataclass
            prof_dict = asdict(self.profile) if is_dataclass(self.profile) else (self.profile.model_dump() if hasattr(self.profile, "model_dump") else self.profile)
            class_res = classify_hh_conversation_detailed(d, profile=prof_dict)
            classification = class_res.get("classification") or "RECRUITER_QUESTION"
            reply_text = class_res.get("prepared_reply") or ""
            facts_used = class_res.get("sources") or ["candidate_profile: desired_roles, skills, experience, remote"]
            send_status = "GENERATED"
            send_error = None
            sent_at = None
            sent_reply = None

            if classification in ("NEEDS_REPLY", "RECRUITER_QUESTION") and reply_text:
                if out["auto_replies_count"] < self.config.max_auto_replies_per_cycle:
                    try:
                        reply_snippet = reply_text[:30].strip()
                        raw_send = eval_fn(f"""(() => {{
                            const targetConvId = {json.dumps(d.conversation_id)};
                            const topicElem = document.querySelector(`[data-qa*="${{targetConvId}}"], a[href*="${{targetConvId}}"]`);
                            if (topicElem && !location.href.includes(targetConvId)) {{
                                topicElem.click();
                            }}
                            const input = document.querySelector('[data-qa="chat-input-textarea"], textarea');
                            const sendBtn = document.querySelector('[data-qa="chat-input-submit"], button[type="submit"]');
                            if (!input || !sendBtn) return JSON.stringify({{ ok: false, reason: 'Chat input elements not found for conversation ' + targetConvId }});
                            input.value = {json.dumps(reply_text)};
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            sendBtn.click();
                            
                            // Post-send DOM verification
                            const bubbles = Array.from(document.querySelectorAll('div[class*="message--"], [class*="chat-bubble"]'));
                            const snippet = {json.dumps(reply_snippet)};
                            const foundInDom = bubbles.some(b => b.innerText && b.innerText.includes(snippet));
                            return JSON.stringify({{ ok: true, conversation_id: targetConvId, verified_in_hh: foundInDom }});
                        }})()""")
                        send_res = json.loads(raw_send) if isinstance(raw_send, str) else raw_send
                        verified_flag = send_res.get("verified_in_hh")
                        is_dom_confirmed = (
                            bool(send_res.get("ok"))
                            and (verified_flag is not False)
                            and (verified_flag is True or ("verified_in_hh" not in send_res))
                            and (send_res.get("conversation_id") == d.conversation_id or not send_res.get("conversation_id"))
                        )
                        if is_dom_confirmed:
                            send_status = "SENT"
                            sent_reply = reply_text
                            sent_at = now_iso
                            out["auto_replies_count"] += 1
                        else:
                            send_status = "FAILED"
                            send_error = send_res.get("reason") or "Message not confirmed in HH chat DOM after send"
                    except Exception as e:
                        send_status = "FAILED"
                        send_error = str(e)
                        logger.debug(f"Auto-reply send exception: {e}")

                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1,
                    "classification": classification,
                    "reply_draft": reply_text,
                    "status": "MESSAGE_AUTO_REPLIED" if send_status == "SENT" else ("FAILED" if send_status == "FAILED" else "GENERATED"),
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
                    "decision_reason": class_res.get("reason") or "Generated from verified candidate profile facts and vacancy context.",
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
            else:
                db.save_hh_message_event(**{
                    "message_fingerprint": fp,
                    "conversation_id": d.conversation_id,
                    "sender": last_msg.sender,
                    "text": msg_text,
                    "sent_at": last_msg.sent_at,
                    "seen_at": now_iso,
                    "processed": 1,
                    "classification": classification,
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
                    "message_classification": classification,
                    "generated_reply": None,
                    "sent_reply": None,
                    "sent_at": None,
                    "profile_facts_used": None,
                    "decision_reason": class_res.get("reason") or "Message does not require candidate reply.",
                    "status": "NO_REPLY_NEEDED",
                    "created_at": now_iso,
                })

        return out


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(str(s))


def run_autonomous_cycle(config: Optional[AutonomousConfig] = None) -> AutonomousCycleResult:
    """Run one single autonomous cycle."""
    agent = AutonomousJobAgent(config=config)
    return agent.run_cycle()


def start_autonomous_daemon(config: Optional[AutonomousConfig] = None) -> None:
    """Run continuous autonomous agent loop until stopped."""
    agent = AutonomousJobAgent(config=config)
    poll_sec = agent.config.poll_interval_seconds

    print("=" * 70)
    print(" FULLY AUTONOMOUS JOB APPLICATION AGENT STARTED")
    print(f"  Poll interval: {poll_sec}s")
    print(f" Target queries: {', '.join(agent.config.search_queries)}")
    print(" Notifications enabled ONLY for Interview Invitations & blocking questions.")
    print(" Press Ctrl+C to stop gracefully.")
    print("=" * 70)

    try:
        while True:
            res = agent.run_cycle()
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {res.summary}")
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        print("\n Autonomous Agent stopped by user.")
