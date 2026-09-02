"""Stage 89 / Stage 91: Telegram Feedback & Application Review Integration.

Provides secure, authenticated, idempotent callback handling for Telegram digest buttons:
- INTERESTED (INT / 👍 Интересно) -> Promotes to ApplicationReview(PENDING_REVIEW), ApplicationStatus.ANALYZED/DISCOVERED.
- NOT_INTERESTED (NOT / 👎 Не подходит) -> Sets ApplicationReview(REJECTED), ApplicationStatus.REJECTED.
- PREPARE_APPLICATION (APP / 📄 Подготовить отклик) -> Sets ApplicationStatus.READY_TO_APPLY, ApplicationReview(APPROVED), application_queue. (NEVER submits externally).
- SKIP (SKP / ⏭ Пропустить) -> Sets ApplicationStatus.WITHDRAWN, ApplicationReview(REJECTED).
- Optional structured reasons for dislikes/skips/likes (Stage 91) without requiring extra steps.
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


class FeedbackReason(str, Enum):
    # Dislike / Skip reasons
    ROLE = "ROLE"
    SALARY = "SALARY"
    COMPANY = "COMPANY"
    LOCATION = "LOCATION"
    TECH_STACK = "TECH_STACK"
    SENIORITY = "SENIORITY"
    LANGUAGE = "LANGUAGE"
    EMPLOYMENT_TYPE = "EMPLOYMENT_TYPE"
    TOO_COMPLEX = "TOO_COMPLEX"
    TOO_JUNIOR = "TOO_JUNIOR"
    TOO_SENIOR = "TOO_SENIOR"
    # Positive / Interest reasons
    REMOTE = "REMOTE"
    CAREER_GROWTH = "CAREER_GROWTH"
    OTHER = "OTHER"


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

REASON_CODE_MAP: Dict[str, FeedbackReason] = {
    "ROL": FeedbackReason.ROLE,
    "SAL": FeedbackReason.SALARY,
    "COM": FeedbackReason.COMPANY,
    "LOC": FeedbackReason.LOCATION,
    "STK": FeedbackReason.TECH_STACK,
    "SEN": FeedbackReason.SENIORITY,
    "LNG": FeedbackReason.LANGUAGE,
    "EMP": FeedbackReason.EMPLOYMENT_TYPE,
    "CPX": FeedbackReason.TOO_COMPLEX,
    "JUN": FeedbackReason.TOO_JUNIOR,
    "SNR": FeedbackReason.TOO_SENIOR,
    "RMT": FeedbackReason.REMOTE,
    "GRW": FeedbackReason.CAREER_GROWTH,
    "OTH": FeedbackReason.OTHER,
}

REVERSE_REASON_CODE_MAP = {v: k for k, v in REASON_CODE_MAP.items()}


def encode_callback_data(
    action: TelegramFeedbackAction,
    vacancy_stable_id: str,
    reason: Optional[FeedbackReason | str] = None,
) -> str:
    """Encode action, stable_id, and optional reason into a compact string respecting Telegram's 64-byte callback limit."""
    code = REVERSE_ACTION_CODE_MAP.get(action, "INT")
    if reason is not None:
        r_enum = FeedbackReason(reason) if isinstance(reason, str) else reason
        r_code = REVERSE_REASON_CODE_MAP.get(r_enum, "OTH")
        direct_str = f"fb:RSN:{code}:{r_code}:{vacancy_stable_id}"
        if len(direct_str.encode("utf-8")) <= 64:
            return direct_str
        h = hashlib.sha256(vacancy_stable_id.encode("utf-8")).hexdigest()[:16]
        return f"fb:RSN:{code}:{r_code}:h:{h}"
    else:
        direct_str = f"fb:{code}:{vacancy_stable_id}"
        if len(direct_str.encode("utf-8")) <= 64:
            return direct_str
        h = hashlib.sha256(vacancy_stable_id.encode("utf-8")).hexdigest()[:16]
        return f"fb:{code}:h:{h}"


class DecodedCallback(tuple):
    """2-tuple (action, stable_id) with optional .reason attribute for seamless 2-tuple unpacking."""
    def __new__(cls, action: Optional[TelegramFeedbackAction], stable_id: Optional[str], reason: Optional[FeedbackReason] = None):
        inst = super().__new__(cls, (action, stable_id))
        inst._reason = reason
        return inst
    
    @property
    def action(self) -> Optional[TelegramFeedbackAction]:
        return self[0]
        
    @property
    def stable_id(self) -> Optional[str]:
        return self[1]

    @property
    def reason(self) -> Optional[FeedbackReason]:
        return getattr(self, "_reason", None)


def decode_callback_data(callback_data: str) -> DecodedCallback:
    """Decode action, canonical vacancy stable_id, and optional reason into a DecodedCallback (2-tuple with .reason)."""
    if not callback_data or not callback_data.startswith("fb:"):
        return DecodedCallback(None, None, None)
        
    parts = callback_data.split(":")
    if len(parts) < 3:
        return DecodedCallback(None, None, None)
        
    if parts[1] == "RSN" and len(parts) >= 5:
        # fb:RSN:<code>:<r_code>:<target>
        act_code = parts[2]
        r_code = parts[3]
        target = ":".join(parts[4:])
        action = ACTION_CODE_MAP.get(act_code)
        reason = REASON_CODE_MAP.get(r_code, FeedbackReason.OTHER)
        if target.startswith("h:"):
            hash_prefix = target[2:]
            sid = db.resolve_vacancy_by_hash_prefix(hash_prefix)
            return DecodedCallback(action, sid, reason)
        return DecodedCallback(action, target, reason)

    code, target = parts[1], ":".join(parts[2:])
    action = ACTION_CODE_MAP.get(code)
    if not action:
        return DecodedCallback(None, None, None)
        
    if target.startswith("h:"):
        hash_prefix = target[2:]
        sid = db.resolve_vacancy_by_hash_prefix(hash_prefix)
        return DecodedCallback(action, sid, None)
        
    return DecodedCallback(action, target, None)


def build_reason_inline_keyboard(action: TelegramFeedbackAction, vacancy_stable_id: str) -> List[List[Dict[str, str]]]:
    """Build optional compact 2-level reason keyboard for Telegram."""
    if action in (TelegramFeedbackAction.NOT_INTERESTED, TelegramFeedbackAction.SKIP):
        return [
            [
                {"text": "💼 Роль", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.ROLE)},
                {"text": "💰 Зарплата", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.SALARY)},
            ],
            [
                {"text": "🛠 Стек", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.TECH_STACK)},
                {"text": "🏢 Компания", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.COMPANY)},
            ],
            [
                {"text": "📍 Локация", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.LOCATION)},
                {"text": "❓ Другое", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.OTHER)},
            ],
        ]
    elif action == TelegramFeedbackAction.INTERESTED:
        return [
            [
                {"text": "💼 Роль", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.ROLE)},
                {"text": "🛠 Стек", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.TECH_STACK)},
            ],
            [
                {"text": "🏢 Компания", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.COMPANY)},
                {"text": "🌍 Remote", "callback_data": encode_callback_data(action, vacancy_stable_id, FeedbackReason.REMOTE)},
            ],
        ]
    return []


class TelegramFeedbackProcessor:
    """Processes incoming Telegram feedback callbacks with strict authorization and idempotency."""

    def __init__(
        self,
        allowed_user_id: Optional[str] = None,
        allowed_chat_id: Optional[str] = None,
        notifier: Optional[TelegramNotifier] = None,
        require_delivered: bool = True,
    ):
        self.allowed_user_id = str(
            allowed_user_id if allowed_user_id is not None else (
                getattr(config, "TELEGRAM_OWNER_ID", "") or getattr(config, "TELEGRAM_USER_ID", "")
            )
        ).strip()
        self.allowed_chat_id = str(
            allowed_chat_id if allowed_chat_id is not None else getattr(config, "TELEGRAM_CHAT_ID", "")
        ).strip()
        self.notifier = notifier or TelegramNotifier()
        self.require_delivered = require_delivered

    def is_authorized(self, user_id: Any, chat_id: Any = None) -> bool:
        """Check whether user is authorized to perform human review actions.
        
        Strict safety:
        - Owner authentication is bound to user identity (callback_query.from.id == TELEGRAM_OWNER_ID).
        - Chat ID represents destination channel/chat and cannot authorize mutation actions on its own.
        - Fail closed if TELEGRAM_OWNER_ID is missing or mismatched.
        """
        uid_str = str(user_id or "").strip()
        if not self.allowed_user_id or not uid_str:
            return False
        return uid_str == self.allowed_user_id

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

        # 3. Decode Action, Target Vacancy, and Optional Reason
        decoded = decode_callback_data(data)
        action, stable_id = decoded
        feedback_reason = decoded.reason
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

        # 4.1 Provenance Verification (Fail Closed on Test / Synthetic / Legacy Artifacts)
        from ai_assistant.schema import is_genuine_production_vacancy
        is_gen, gen_reason = is_genuine_production_vacancy(vac)
        if not is_gen:
            logger.warning(f"Rejected feedback for non-genuine vacancy '{stable_id}': {gen_reason}")
            if cb_id:
                self.notifier.answer_callback_query(
                    callback_query_id=cb_id,
                    text="⚠️ Действие недоступно для тестовых/legacy вакансий.",
                    show_alert=True,
                )
            return {"success": False, "error": "INVALID_PROVENANCE", "reason": gen_reason, "vacancy_stable_id": stable_id}

        # 4.2 Delivery Relationship Verification (Digest-delivered invariant)
        if self.require_delivered and not db.is_vacancy_delivered_in_digest(stable_id):
            logger.warning(f"Rejected feedback for undelivered vacancy '{stable_id}'")
            if cb_id:
                self.notifier.answer_callback_query(
                    callback_query_id=cb_id,
                    text="⚠️ Вакансия ещё не была доставлена в Telegram дайджесте.",
                    show_alert=True,
                )
            return {"success": False, "error": "NOT_DELIVERED", "vacancy_stable_id": stable_id}

        # 5. Get Current Application Status
        app_record = get_application_status(stable_id)
        cur_status = app_record.status.value if app_record else "DISCOVERED"
        new_status = cur_status
        ack_text = ""

        # 6. Action Execution State Machine
        reason_str = feedback_reason.value if feedback_reason else None

        if action == TelegramFeedbackAction.INTERESTED:
            if cur_status in ("APPLIED", "SUBMITTED", "VERIFIED", "INTERVIEW", "OFFER"):
                ack_text = f"👍 Вакансия уже находится в статусе {cur_status}"
                new_status = cur_status
            else:
                note = "Marked INTERESTED via Telegram feedback"
                if reason_str:
                    note += f" (reason: {reason_str})"
                save_application_review(
                    ApplicationReview(
                        vacancy_stable_id=stable_id,
                        company=getattr(vac, 'company', None),
                        title=getattr(vac, 'title', None),
                        source=getattr(vac, 'source', None),
                        vacancy_url=getattr(vac, 'job_url', None),
                        match_score=getattr(vac, 'match_score', None),
                        status=ReviewStatus.PENDING_REVIEW,
                        note=note,
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
                        notes=note,
                    )
                new_status = "ANALYZED"
                ack_text = "👍 Вакансия отмечена как интересная" if not reason_str else f"👍 Интересно (причина: {reason_str})"

        elif action == TelegramFeedbackAction.NOT_INTERESTED:
            if cur_status in ("APPLIED", "OFFER", "INTERVIEW"):
                ack_text = f"⚠️ Нельзя отклонить: вакансия в статусе {cur_status}"
                new_status = cur_status
            else:
                note = "Marked NOT_INTERESTED via Telegram feedback"
                if reason_str:
                    note += f" (reason: {reason_str})"
                set_application_status(
                    vacancy_stable_id=stable_id,
                    status=ApplicationStatus.REJECTED,
                    company=getattr(vac, 'company', None),
                    title=getattr(vac, 'title', None),
                    source=getattr(vac, 'source', None),
                    vacancy_url=getattr(vac, 'job_url', None),
                    match_score=getattr(vac, 'match_score', None),
                    notes=note,
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
                        note=note,
                    )
                )
                new_status = "REJECTED"
                ack_text = "👎 Вакансия отклонена" if not reason_str else f"👎 Отклонено (причина: {reason_str})"

        elif action == TelegramFeedbackAction.PREPARE_APPLICATION:
            if cur_status in ("APPLIED", "SUBMITTED", "VERIFIED", "OFFER"):
                ack_text = f"⚠️ Вакансия уже была отправлена ({cur_status})"
                new_status = cur_status
            else:
                note = "Prepared for application review via Telegram feedback"
                if reason_str:
                    note += f" (reason: {reason_str})"
                set_application_status(
                    vacancy_stable_id=stable_id,
                    status=ApplicationStatus.READY_TO_APPLY,
                    company=getattr(vac, 'company', None),
                    title=getattr(vac, 'title', None),
                    source=getattr(vac, 'source', None),
                    vacancy_url=getattr(vac, 'job_url', None),
                    match_score=getattr(vac, 'match_score', None),
                    notes=note,
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
                        note=note,
                    )
                )
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
                note = "Skipped via Telegram feedback"
                if reason_str:
                    note += f" (reason: {reason_str})"
                set_application_status(
                    vacancy_stable_id=stable_id,
                    status=ApplicationStatus.WITHDRAWN,
                    company=getattr(vac, 'company', None),
                    title=getattr(vac, 'title', None),
                    source=getattr(vac, 'source', None),
                    vacancy_url=getattr(vac, 'job_url', None),
                    match_score=getattr(vac, 'match_score', None),
                    notes=note,
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
                        note=note,
                    )
                )
                new_status = "WITHDRAWN"
                ack_text = "⏭ Вакансия пропущена" if not reason_str else f"⏭ Пропущено (причина: {reason_str})"

        # 7. Record Feedback Audit Log with Structured Payload
        audit_payload = dict(callback_query)
        if reason_str:
            audit_payload["feedback_reason"] = reason_str
            audit_payload["skip_reason"] = reason_str

        try:
            db.record_telegram_feedback(
                vacancy_stable_id=stable_id,
                action=action.value,
                telegram_user_id=str(user_id) if user_id is not None else None,
                telegram_chat_id=str(chat_id) if chat_id is not None else None,
                callback_query_id=cb_id or None,
                previous_status=cur_status,
                new_status=new_status,
                payload_json=json.dumps(audit_payload, ensure_ascii=False),
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
            "feedback_reason": reason_str,
            "previous_status": cur_status,
            "new_status": new_status,
            "message": ack_text,
        }
