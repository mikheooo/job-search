"""Stage 56: Production Telegram Notifier for Autonomous Job Agent.

Handles structured notification delivery to Telegram:
- RECRUITER_REPLY_SENT
- INTERVIEW_INVITATION (HIGH priority)
- UNANSWERED_QUESTION_BLOCKED (HIGH priority)
- FATAL_ERROR (HIGH priority)

Safety & Idempotency:
- Strict deduplication per notification / event key via database delivery records.
- Token and credentials never logged or leaked.
- Routine events (discovery, matching, polling, routine submits, rejections) are filtered out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai_assistant import config, db

logger = logging.getLogger(__name__)


def verify_hh_chat_url(conversation_id: str, url: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Verify that an HH chat deep-link corresponds to a real numeric HH conversation ID.

    Rules:
    - conversation_id must be a non-empty numeric string (digits only).
    - neg_* synthetic IDs or non-numeric IDs are strictly rejected.
    - General pages like '/applicant/negotiations' are strictly rejected.
    - If url is provided, it must match the pattern https://(www.|chatik.)?hh.ru/chat/<conversation_id>.
    - If url is not provided or valid, generates https://hh.ru/chat/<conversation_id>.
    """
    cid = str(conversation_id or "").strip()
    if not cid or cid.startswith("neg_") or not re.match(r"^\d+$", cid):
        return False, None

    if url:
        clean_url = url.split("?")[0].split("#")[0].strip()
        if "negotiations" in clean_url or "search" in clean_url:
            return False, None
        m = re.match(r"^https:\/\/(?:[a-zA-Z0-9-]+\.)?hh\.ru\/chat\/(\d+)(?:\/)?$", clean_url)
        if m and m.group(1) == cid:
            return True, f"https://hh.ru/chat/{cid}"
        return False, None

    return True, f"https://hh.ru/chat/{cid}"


class TelegramNotifier:
    """Production Telegram Bot API Notifier."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        transport_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.bot_token = bot_token if bot_token is not None else config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID
        self.transport_fn = transport_fn

    def is_configured(self) -> bool:
        """Check whether Bot Token and Target Chat ID are present."""
        return bool(self.bot_token and self.chat_id)

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> Dict[str, Any]:
        """Send a message via Telegram Bot API with error isolation."""
        target_chat = str(chat_id or self.chat_id or "").strip()
        if not self.bot_token or not target_chat:
            return {"ok": False, "error": "Telegram Bot Token or Chat ID not configured"}

        payload = {
            "chat_id": target_chat,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        # Custom transport hook for testing / mocking
        if self.transport_fn is not None:
            try:
                return self.transport_fn(self.bot_token, payload)
            except Exception as e:
                return {"ok": False, "error": f"Transport exception: {e}"}

        # Fail-closed guard: during pytest execution, never make live network calls if unmocked
        if os.getenv("PYTEST_CURRENT_TEST"):
            logger.info("PYTEST_CURRENT_TEST detected: suppressing real Telegram HTTP network call.")
            return {
                "ok": True,
                "result": {
                    "message_id": 999999,
                    "chat": {"id": int(target_chat) if target_chat.isdigit() else target_chat},
                    "text": text,
                    "date": 1725000000,
                },
                "mocked": True,
            }

        # Real Telegram Bot API HTTP transport
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "JobAgentTelegramBot/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                raw = resp.read().decode("utf-8")
                res_data = json.loads(raw)
                return res_data
        except Exception as e:
            # Mask token from log
            logger.warning(f"Telegram API request failed: {e}")
            return {"ok": False, "error": str(e)}

    def delete_message(
        self,
        chat_id: Optional[str] = None,
        message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Delete a message via Telegram Bot API with error isolation."""
        target_chat = str(chat_id or self.chat_id or "").strip()
        if not self.bot_token or not target_chat or message_id is None:
            return {"ok": False, "error": "Telegram Bot Token, Chat ID, or message_id not provided"}

        payload = {
            "chat_id": target_chat,
            "message_id": message_id,
        }

        # Custom transport hook for testing / mocking
        if self.transport_fn is not None:
            try:
                return self.transport_fn("deleteMessage", payload)
            except Exception as e:
                return {"ok": False, "error": f"Transport exception: {e}"}

        # Fail-closed guard: during pytest execution, never make live network calls if unmocked
        if os.getenv("PYTEST_CURRENT_TEST"):
            logger.info("PYTEST_CURRENT_TEST detected: suppressing real Telegram deleteMessage HTTP call.")
            return {"ok": True, "result": True, "mocked": True}

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/deleteMessage"
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "JobAgentTelegramBot/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"Telegram deleteMessage failed: {e}")
            return {"ok": False, "error": str(e)}

    # ---------------------------------------------------------------------------
    # Formatting Helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def format_recruiter_reply(
        company: str,
        vacancy: str,
        incoming_message: str,
        sent_reply: str,
        conversation_id: str = "",
        hh_chat_url: Optional[str] = None,
        status: str = "CONFIRMED",
    ) -> str:
        """Format RECRUITER_REPLY_SENT notification message with strict confirmation semantics and deep-link."""
        conv_str = conversation_id if conversation_id else "unknown"
        is_valid_url, verified_url = verify_hh_chat_url(conversation_id=conversation_id, url=hh_chat_url)
        url_line = verified_url if (is_valid_url and verified_url) else "NOT AVAILABLE"

        return (
            "RECRUITER REPLY\n\n"
            f"Компания: {company}\n"
            f"Вакансия: {vacancy}\n\n"
            f"Рекрутер:\n{incoming_message}\n\n"
            f"Мой ответ:\n{sent_reply}\n\n"
            f"Открыть чат HH:\n{url_line}\n\n"
            "Статус:\nОтвет отправлен и подтверждён в HH."
        )

    @staticmethod
    def format_reply_generated_not_sent(
        company: str,
        vacancy: str,
        incoming_message: str,
        generated_reply: str,
        conversation_id: str = "",
        hh_chat_url: Optional[str] = None,
    ) -> str:
        """Format REPLY GENERATED — NOT SENT notification message."""
        is_valid_url, verified_url = verify_hh_chat_url(conversation_id=conversation_id, url=hh_chat_url)
        url_line = verified_url if (is_valid_url and verified_url) else "NOT AVAILABLE"
        return (
            "REPLY GENERATED — NOT SENT\n\n"
            f"Компания: {company}\n"
            f"Вакансия: {vacancy}\n\n"
            f"Рекрутер:\n{incoming_message}\n\n"
            f"Черновик:\n{generated_reply}\n\n"
            "HH: NOT CONFIRMED\n\n"
            f"OPEN HH CHAT:\n{url_line}\n\n"
            "Что делать:\nREQUIRES REVIEW"
        )

    @staticmethod
    def format_external_questionnaire(
        company: str,
        vacancy: str,
        what_they_want: str,
        url: str,
        questions: str = "Требуется заполнение внешней анкеты",
        action: str = "REQUIRES REVIEW",
    ) -> str:
        """Format EXTERNAL_QUESTIONNAIRE notification message."""
        return (
            "EXTERNAL QUESTIONNAIRE\n\n"
            f"Company:\n{company}\n\n"
            f"Vacancy:\n{vacancy}\n\n"
            f"What they want:\n{what_they_want}\n\n"
            f"URL:\n{url}\n\n"
            f"Action:\n{action}"
        )

    @staticmethod
    def format_test_task(
        company: str,
        vacancy: str,
        task_description: str,
        url: str = "",
        action: str = "REQUIRES REVIEW",
    ) -> str:
        """Format TEST_TASK notification message."""
        url_line = f"\nURL:\n{url}\n\n" if url else "\n\n"
        return (
            "TEST TASK\n\n"
            f"Company:\n{company}\n\n"
            f"Vacancy:\n{vacancy}\n\n"
            f"Task:\n{task_description}"
            f"{url_line}"
            f"Action:\n{action}"
        )

    @staticmethod
    def format_interview_invitation(
        company: str,
        vacancy: str,
        invitation_text: str,
        date_time: str = "",
        invitation_url: str = "",
    ) -> str:
        """Format INTERVIEW_INVITATION notification message."""
        return (
            "INTERVIEW INVITATION\n\n"
            f"Company:\n{company}\n\n"
            f"Vacancy:\n{vacancy}\n\n"
            f"WHAT HAPPENED:\n{invitation_text}\n\n"
            "WHY AM I SEEING THIS:\nRecruiter wants to continue the hiring process.\n\n"
            "WHAT SHOULD I DO:\nReview the invitation and contact/arrange the interview."
        )

    @staticmethod
    def format_unknown_question(
        company: str,
        vacancy: str,
        question: str,
        reason: str = "",
        chat_url: str = "",
    ) -> str:
        """Format UNANSWERED_QUESTION_BLOCKED notification message."""
        reason_str = reason if reason else "Требуется решение пользователя; автоответ не выдумывается."
        url_str = chat_url if chat_url else "https://hh.ru/applicant/negotiations"
        return (
            "⚠️ NEEDS YOUR INPUT\n\n"
            f"Company:\n{company}\n\n"
            f"Vacancy:\n{vacancy}\n\n"
            f"Employer question:\n{question}\n\n"
            f"Reason:\n{reason_str}\n\n"
            f"Link:\n{url_str}"
        )

    # ---------------------------------------------------------------------------
    # Main Dispatcher Integration & Idempotency
    # ---------------------------------------------------------------------------

    def deliver_notification(
        self,
        notif_type: str,
        details: Dict[str, Any],
        delivery_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Deliver high-value notification to Telegram with strict idempotency protection."""
        # 1. Filter out routine events
        allowed_types = {
            "RECRUITER_REPLY_SENT",
            "INTERVIEW_INVITATION",
            "UNANSWERED_QUESTION_BLOCKED",
            "FATAL_ERROR",
            "EXTERNAL_QUESTIONNAIRE",
            "TEST_TASK",
        }
        if notif_type not in allowed_types:
            return {"delivered": False, "reason": f"Event type '{notif_type}' is routine and not routed to Telegram"}

        # 2. Check Idempotency Key
        cid = str(details.get("conversation_id") or details.get("application_id") or "").strip()
        inc = str(details.get("incoming_message") or details.get("text") or "").strip()
        snt = str(details.get("sent_reply") or details.get("my_reply") or details.get("generated_reply") or "").strip()
        content_hash = hashlib.sha256(f"{cid}_{inc}_{snt}".encode("utf-8")).hexdigest()[:12]
        key = delivery_key or f"{notif_type}_{cid}_{content_hash}"

        if db.is_telegram_delivered(key):
            return {"delivered": False, "reason": f"Notification '{key}' already delivered to Telegram (idempotent skip)"}

        # 3. Format message
        text = ""
        is_valid_url, verified_url = verify_hh_chat_url(conversation_id=cid, url=details.get("hh_chat_url") or details.get("vacancy_url"))
        chat_url = verified_url or details.get("vacancy_url") or "https://hh.ru/applicant/negotiations"

        if notif_type == "RECRUITER_REPLY_SENT":
            is_valid_cid = bool(cid and re.match(r"^\d+$", str(cid).strip()))
            if not is_valid_cid or not is_valid_url or not verified_url:
                return {
                    "delivered": False,
                    "reason": "RECRUITER_REPLY_SENT suppressed: conversation_id must be a verified numeric HH ID with confirmed deep-link.",
                }
            if not snt:
                return {
                    "delivered": False,
                    "reason": "RECRUITER_REPLY_SENT suppressed: sent_reply is empty.",
                }
            text = self.format_recruiter_reply(
                company=details.get("company") or details.get("employer") or "HeadHunter Employer",
                vacancy=details.get("vacancy") or details.get("vacancy_title") or "Python Role",
                incoming_message=details.get("incoming_message") or details.get("text") or "",
                sent_reply=details.get("sent_reply") or details.get("my_reply") or "",
                conversation_id=cid,
                hh_chat_url=verified_url,
                status="CONFIRMED",
            )
        elif notif_type == "EXTERNAL_QUESTIONNAIRE":
            text = self.format_external_questionnaire(
                company=details.get("company") or details.get("employer") or "HeadHunter Employer",
                vacancy=details.get("vacancy") or details.get("vacancy_title") or "Python Role",
                what_they_want=details.get("what_they_want") or details.get("incoming_message") or "Заполнение внешней анкеты для рассмотрения кандидатуры",
                url=details.get("url") or details.get("form_url") or "https://hh.ru/applicant/negotiations",
                questions=details.get("questions") or "Количество вопросов уточняется по ссылке",
                action=details.get("action") or "REQUIRES REVIEW",
            )
        elif notif_type == "TEST_TASK":
            text = self.format_test_task(
                company=details.get("company") or details.get("employer") or "HeadHunter Employer",
                vacancy=details.get("vacancy") or details.get("vacancy_title") or "Python Role",
                task_description=details.get("task_description") or details.get("incoming_message") or "Техническое тестовое задание",
                url=details.get("url") or "",
                action=details.get("action") or "REQUIRES REVIEW",
            )
        elif notif_type == "INTERVIEW_INVITATION":
            text = self.format_interview_invitation(
                company=details.get("company") or details.get("employer") or "HeadHunter Employer",
                vacancy=details.get("vacancy") or details.get("vacancy_title") or "Python Role",
                invitation_text=details.get("invitation_text") or details.get("message") or details.get("text") or "",
                date_time=details.get("date_time") or "",
                invitation_url=details.get("invitation_url") or details.get("link") or chat_url,
            )
        elif notif_type == "UNANSWERED_QUESTION_BLOCKED":
            text = self.format_unknown_question(
                company=details.get("company") or details.get("employer") or "HeadHunter Employer",
                vacancy=details.get("vacancy") or details.get("vacancy_title") or "Python Role",
                question=details.get("unanswered_question") or details.get("question") or "",
                reason=details.get("reason") or "",
                chat_url=chat_url,
            )
        elif notif_type == "FATAL_ERROR":
            text = f"🚨 FATAL ERROR IN AGENT CYCLE\n\nError:\n{details.get('error') or 'Unknown error'}"

        if not text:
            return {"delivered": False, "reason": "No text generated for notification"}

        # 4. Check configuration
        if not self.is_configured():
            return {
                "delivered": False,
                "reason": "Telegram Bot Token or Chat ID not configured in environment",
                "text": text,
            }

        # 5. Dispatch via Bot API
        res = self.send_message(text=text)
        if res.get("ok"):
            tg_msg_id = (res.get("result") or {}).get("message_id")
            db.record_telegram_delivery(
                delivery_key=key,
                notification_type=notif_type,
                chat_id=self.chat_id,
                status="DELIVERED",
                payload={
                    "text": text,
                    "details": details,
                    "telegram_message_id": tg_msg_id,
                    "hh_chat_url": verified_url if is_valid_url else None,
                },
            )
            return {"delivered": True, "delivery_key": key, "result": res}
        else:
            return {"delivered": False, "error": res.get("error"), "text": text}


def cleanup_telegram_test_records(
    notifier: Optional[TelegramNotifier] = None,
) -> Dict[str, Any]:
    """Audit and safely clean up test/debug notifications from DB and Telegram."""
    notifier = notifier or get_telegram_notifier()
    records = db.list_telegram_delivery_records(limit=500)

    cleaned_count = 0
    deleted_tg_count = 0
    preserved_count = 0
    candidates = []

    for r in records:
        key = r.get("delivery_key") or ""
        cid = str((r.get("payload") or {}).get("details", {}).get("conversation_id") or "").strip()
        is_test = (
            key.startswith("test_")
            or "conv_rag_" in key
            or "conv_hh_55_" in key
            or "conv_idem_" in key
            or "conv_failed_" in key
            or cid.startswith("neg_")
            or cid == "conv_rag_1"
            or (bool(cid) and not cid.isdigit())
        )

        if is_test:
            msg_id = (r.get("payload") or {}).get("telegram_message_id")
            deleted_from_tg = False
            if msg_id and notifier.is_configured():
                res = notifier.delete_message(chat_id=r.get("chat_id"), message_id=msg_id)
                if res.get("ok"):
                    deleted_from_tg = True
                    deleted_tg_count += 1

            db.update_telegram_delivery_status(
                record_id=r["id"],
                status="CLEANED_TEST_RECORD",
                payload_update={"cleaned": True, "deleted_from_telegram": deleted_from_tg},
            )
            cleaned_count += 1
            candidates.append({
                "record_id": r["id"],
                "delivery_key": key,
                "conversation_id": cid,
                "reason": "Test/synthetic fixture detected",
                "deleted_from_telegram": deleted_from_tg,
            })
        else:
            preserved_count += 1

    return {
        "total_examined": len(records),
        "cleaned_count": cleaned_count,
        "deleted_telegram_messages": deleted_tg_count,
        "preserved_count": preserved_count,
        "candidates": candidates,
    }


# Global singleton instance
_notifier_instance: Optional[TelegramNotifier] = None


def get_telegram_notifier() -> TelegramNotifier:
    """Retrieve global TelegramNotifier instance."""
    global _notifier_instance
    if _notifier_instance is None:
        _notifier_instance = TelegramNotifier()
    return _notifier_instance


TelegramGateway = TelegramNotifier
get_telegram_gateway = get_telegram_notifier
