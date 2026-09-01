"""Stage 89: Telegram Feedback & Application Review Integration.

Provides secure, authenticated, idempotent callback handling for Telegram digest buttons:
- INTERESTED (INT / 👍 Интересно) -> Promotes to ApplicationReview(PENDING_REVIEW), ApplicationStatus.ANALYZED/DISCOVERED.
- NOT_INTERESTED (NOT / 👎 Не подходит) -> Sets ApplicationReview(REJECTED), ApplicationStatus.REJECTED.
- PREPARE_APPLICATION (APP / 📄 Подготовить отклик) -> Sets ApplicationStatus.READY_TO_APPLY, ApplicationReview(APPROVED), application_queue. (NEVER submits externally).
- SKIP (SKP / ⏭ Пропустить) -> Sets ApplicationStatus.WITHDRAWN/REJECTED, ApplicationReview(REJECTED).
"""

from __future__ import annotations

import hashlib
import json
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ai_assistant import config, db
from ai_assistant.application_queue import QueueItem, save_queue_item
from ai_assistant.application_review import ApplicationReview, ReviewStatus, save_application_review, get_application_review
from ai_assistant.application_tracking import ApplicationStatus, get_application_status, set_application_status
from ai_assistant.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class TelegramFeedbackAction(str, Enum):
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    PREPARE_APPLICATION = "PREPARE_APPLICATION"
    SKIP = "SKIP"


ACTION_CODE_MAP = {
    "INT": TelegramFeedbackAction.INTERESTED,
    "NOT": TelegramFeedbackAction.NOT_INTERESTED,
    "APP": TelegramFeedbackAction.PREPARE_APPLICATION,
    "SKP": TelegramFeedbackAction.SKIP,
}

REVERSE_ACTION_CODE_MAP = {
    TelegramFeedbackAction.INTERESTED: "INT",
    TelegramFeedbackAction.NOT_INTERESTED: "NOT",
    TelegramFeedbackAction.PREPARE_APPLICATION: "APP",
    TelegramFeedbackAction.SKIP: "SKP",
}


def encode_callback_data(action: TelegramFeedbackAction, vacancy_stable_id: str) -> str:
    """Encode action and stable_id into a compact string respecting Telegram's 64-byte callback limit."""
    code = REVERSE_ACTION_CODE_MAP.get(action, "INT")
    direct_str = f"fb:{code}:{vacancy_stable_id}"
    if len(direct_str.encode("utf-8")) <= 64:
        return direct_str
    
    # Use 16-char SHA256 prefix if stable_id is too long
    h = hashlib.sha256(vacancy_stable_id.encode("utf-8")).hexdigest()[:16]
    return f"fb:{code}:h:{h}"


def decode_callback_data(callback_data: str) -> Tuple[Optional[TelegramFeedbackAction], Optional[str]]:
    """Decode action and resolve canonical vacancy stable_id."""
    if not callback_data or not callback_data.startswith("fb:"):
        return None, None
        
    parts = callback_data.split(":", 2)
    if len(parts) < 3:
        return None, None
        
    code, target = parts[1], parts[2]
    action = ACTION_CODE_MAP.get(code)
    if not action:
        return None, None
        
    if target.startswith("h:"):
        hash_prefix = target[2:]
        sid = db.resolve_vacancy_by_hash_prefix(hash_prefix)
        return action, sid
        
    return action, target


class TelegramFeedbackProcessor:
    """Processes incoming Telegram feedback callbacks with strict authorization and idempotency."""

    def __init__(
        self,
        allowed_user_id: Optional[str] = None,
        allowed_chat_id: Optional[str] = None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self.allowed_user_id = str(
            allowed_user_id or getattr(config, "TELEGRAM_OWNER_ID", "") or getattr(config, "TELEGRAM_USER_ID", "") or config.TELEGRAM_CHAT_ID or ""
        ).strip()
        self.allowed_chat_id = str(allowed_chat_id or config.TELEGRAM_CHAT_ID or "").strip()
        self.notifier = notifier or TelegramNotifier()

    def is_authorized(self, user_id: Any, chat_id: Any = None) -> bool:
        """Check whether user or chat is authorized to perform human review actions."""
        uid_str = str(user_id or "").strip()
        cid_str = str(chat_id or "").strip()
        if not self.allowed_user_id and not self.allowed_chat_id:
            return False
        if self.allowed_user_id and uid_str == self.allowed_user_id:
            return True
        if self.allowed_chat_id and (uid_str == self.allowed_chat_id or cid_str == self.allowed_chat_id):
            return True
        return False

    def process_callback_query(self, callback_query: Dict[str, Any]) -> Dict[str, Any]:
        """Process a single Telegram callback query."""
        cb_id = str(callback_query.get("id") or "").strip()
        user_info = callback_query.get("from") or {}
        user_id = user_info.get("id")
        msg = callback_query.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        data = str(callback_query.get("data") or "").strip()

        # 1. Authorization Gate (Fail Closed)
        if not self.is_authorized(user_id=user_id, chat_id=chat_id):
            logger.warning(f"Unauthorized Telegram callback query attempt from user={user_id}, chat={chat_id}")
            if cb_id:
                self.notifier.answer_callback_query(
                    callback_query_id=cb_id,
                    text="⛔ Доступ запрещён. Действие доступно только владельцу.",
                    show_alert=True,
                )
            return {"success": False, "error": "UNAUTHORIZED"}

        # 2. Callback Idempotency Check (Duplicate Telegram update delivery)
        if cb_id:
            existing_records = db.list_telegram_feedback(limit=10)
            if any(r.get("callback_query_id") == cb_id for r in existing_records):
                self.notifier.answer_callback_query(
                    callback_query_id=cb_id,
                    text="ℹ️ Действие уже обработано",
                    show_alert=False,
                )
                return {"success": True, "idempotent": True, "callback_query_id": cb_id}

        # 3. Decode Action and Target Vacancy
        action, stable_id = decode_callback_data(data)
        if action is None or stable_id is None:
            logger.warning(f"Malformed or unresolvable callback data: '{data}'")
            if cb_id:
                self.notifier.answer_callback_query(
                    callback_query_id=cb_id,
                    text="⚠️ Ошибка: некорректные данные кнопки.",
                    show_alert=True,
                )
            return {"success": False, "error": "MALFORMED_CALLBACK"}

        # 4. Target Vacancy Existence Verification
        vac_row = db.get_vacancy_by_id(stable_id)
        if not vac_row:
            logger.warning(f"Callback target vacancy not found in DB: '{stable_id}'")
            if cb_id:
                self.notifier.answer_callback_query(
                    callback_query_id=cb_id,
                    text="⚠️ Вакансия не найдена в базе данных.",
                    show_alert=True,
                )
            return {"success": False, "error": "UNKNOWN_VACANCY", "vacancy_stable_id": stable_id}
        vac = db._row_to_vacancy(vac_row) if isinstance(vac_row, tuple) else vac_row

        # 5. Get Current Application Status
        app_record = get_application_status(stable_id)
        cur_status = app_record.status.value if app_record else "DISCOVERED"
        new_status = cur_status
        ack_text = ""

        # 6. Action Execution State Machine
        if action == TelegramFeedbackAction.INTERESTED:
            if cur_status in ("APPLIED", "SUBMITTED", "VERIFIED", "INTERVIEW", "OFFER"):
                ack_text = f"👍 Вакансия уже находится в статусе {cur_status}"
                new_status = cur_status
            else:
                save_application_review(
                    ApplicationReview(
                        vacancy_stable_id=stable_id,
                        company=getattr(vac, 'company', None),
                        title=getattr(vac, 'title', None),
                        source=getattr(vac, 'source', None),
                        vacancy_url=getattr(vac, 'job_url', None),
                        match_score=getattr(vac, 'match_score', None),
                        status=ReviewStatus.PENDING_REVIEW,
                        note="Marked INTERESTED via Telegram feedback",
                    )
                )
                if not app_record:
                    set_application_status(
                        vacancy_stable_id=stable_id,
                        status=ApplicationStatus.ANALYZED,
                        company=getattr(vac, 'company', None),
                        title=getattr(vac, 'title', None),
                        source=getattr(vac, 'source', None),
                        vacancy_url=getattr(vac, 'job_url', None),
                        match_score=getattr(vac, 'match_score', None),
                        notes="Marked INTERESTED via Telegram feedback",
                    )
                new_status = "ANALYZED"
                ack_text = "👍 Вакансия отмечена как интересная"

        elif action == TelegramFeedbackAction.NOT_INTERESTED:
            if cur_status in ("APPLIED", "OFFER", "INTERVIEW"):
                ack_text = f"⚠️ Нельзя отклонить: вакансия в статусе {cur_status}"
                new_status = cur_status
            else:
                set_application_status(
                    vacancy_stable_id=stable_id,
                    status=ApplicationStatus.REJECTED,
                    company=getattr(vac, 'company', None),
                    title=getattr(vac, 'title', None),
                    source=getattr(vac, 'source', None),
                    vacancy_url=getattr(vac, 'job_url', None),
                    match_score=getattr(vac, 'match_score', None),
                    notes="Marked NOT_INTERESTED via Telegram feedback",
                )
                save_application_review(
                    ApplicationReview(
                        vacancy_stable_id=stable_id,
                        company=getattr(vac, 'company', None),
                        title=getattr(vac, 'title', None),
                        source=getattr(vac, 'source', None),
                        vacancy_url=getattr(vac, 'job_url', None),
                        match_score=getattr(vac, 'match_score', None),
                        status=ReviewStatus.REJECTED,
                        note="Marked NOT_INTERESTED via Telegram feedback",
                    )
                )
                new_status = "REJECTED"
                ack_text = "👎 Вакансия отклонена"

        elif action == TelegramFeedbackAction.PREPARE_APPLICATION:
            if cur_status in ("APPLIED", "SUBMITTED", "VERIFIED", "OFFER"):
                ack_text = f"⚠️ Вакансия уже была отправлена ({cur_status})"
                new_status = cur_status
            else:
                # Progress tracking state to READY_TO_APPLY
                set_application_status(
                    vacancy_stable_id=stable_id,
                    status=ApplicationStatus.READY_TO_APPLY,
                    company=getattr(vac, 'company', None),
                    title=getattr(vac, 'title', None),
                    source=getattr(vac, 'source', None),
                    vacancy_url=getattr(vac, 'job_url', None),
                    match_score=getattr(vac, 'match_score', None),
                    notes="Prepared for application review via Telegram feedback",
                )
                save_application_review(
                    ApplicationReview(
                        vacancy_stable_id=stable_id,
                        company=getattr(vac, 'company', None),
                        title=getattr(vac, 'title', None),
                        source=getattr(vac, 'source', None),
                        vacancy_url=getattr(vac, 'job_url', None),
                        match_score=getattr(vac, 'match_score', None),
                        status=ReviewStatus.APPROVED,
                        note="Prepared for application review via Telegram feedback",
                    )
                )
                # Enqueue into application queue
                canon_id = f"can_{stable_id.replace(':', '_')}"
                save_queue_item(
                    QueueItem(
                        vacancy_stable_id=stable_id,
                        canonical_id=canon_id,
                        representative_vacancy_stable_id=stable_id,
                        company=getattr(vac, 'company', None),
                        title=getattr(vac, 'title', None),
                        source=getattr(vac, 'source', None),
                        vacancy_url=getattr(vac, 'job_url', None),
                        priority_score=int(getattr(vac, 'match_score', None) or 75),
                        rank=1,
                    )
                )
                new_status = "READY_TO_APPLY"
                ack_text = "📄 Добавлено в очередь на подготовку отклика"

        elif action == TelegramFeedbackAction.SKIP:
            if cur_status in ("APPLIED", "OFFER", "INTERVIEW"):
                ack_text = f"⚠️ Вакансия уже в статусе {cur_status}"
                new_status = cur_status
            else:
                set_application_status(
                    vacancy_stable_id=stable_id,
                    status=ApplicationStatus.WITHDRAWN,
                    company=getattr(vac, 'company', None),
                    title=getattr(vac, 'title', None),
                    source=getattr(vac, 'source', None),
                    vacancy_url=getattr(vac, 'job_url', None),
                    match_score=getattr(vac, 'match_score', None),
                    notes="Skipped via Telegram feedback",
                )
                save_application_review(
                    ApplicationReview(
                        vacancy_stable_id=stable_id,
                        company=getattr(vac, 'company', None),
                        title=getattr(vac, 'title', None),
                        source=getattr(vac, 'source', None),
                        vacancy_url=getattr(vac, 'job_url', None),
                        match_score=getattr(vac, 'match_score', None),
                        status=ReviewStatus.REJECTED,
                        note="Skipped via Telegram feedback",
                    )
                )
                new_status = "WITHDRAWN"
                ack_text = "⏭ Вакансия пропущена"

        # 7. Record Feedback Audit Log
        try:
            db.record_telegram_feedback(
                vacancy_stable_id=stable_id,
                action=action.value,
                telegram_user_id=str(user_id) if user_id is not None else None,
                telegram_chat_id=str(chat_id) if chat_id is not None else None,
                callback_query_id=cb_id or None,
                previous_status=cur_status,
                new_status=new_status,
                payload_json=json.dumps(callback_query, ensure_ascii=False),
            )
        except Exception as log_err:
            logger.warning(f"Failed to record feedback audit log: {log_err}")

        # 8. Send Telegram Callback Acknowledgment
        if cb_id:
            self.notifier.answer_callback_query(
                callback_query_id=cb_id,
                text=ack_text,
                show_alert=False,
            )

        return {
            "success": True,
            "action": action.value,
            "vacancy_stable_id": stable_id,
            "previous_status": cur_status,
            "new_status": new_status,
            "message": ack_text,
        }
