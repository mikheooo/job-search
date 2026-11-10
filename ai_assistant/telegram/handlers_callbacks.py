"""Callback query handlers for Telegram control bot."""
from __future__ import annotations

import logging
from typing import Any, Callable

from ai_assistant import config, db
from ai_assistant.telegram.transport import (
    ALLOWED_CHAT_ID,
    MAIN_KEYBOARD,
    answer_callback_query as default_answer_callback_query,
    send_message as default_send_message,
)

logger = logging.getLogger("telegram_control_bot")

_gig_acked: set[str] = set()
_gigact_acked: set[str] = set()


def handle_fb_callback(
    cq: dict[str, Any],
    cq_id: str,
    allowed_chat_id: int = ALLOWED_CHAT_ID,
    answer_callback_fn: Callable[[str, str], bool] = default_answer_callback_query,
) -> None:
    """Route fb: digest-feedback callbacks to TelegramFeedbackProcessor."""
    from ai_assistant.telegram_feedback import TelegramFeedbackProcessor

    if not getattr(config, "TELEGRAM_FB_CALLBACKS_ENABLED", False):
        logger.info(
            "fb: callback ignored (TELEGRAM_FB_CALLBACKS_ENABLED off): %s",
            cq.get("data"),
        )
        answer_callback_fn(cq_id, "")
        return

    try:
        processor = TelegramFeedbackProcessor(
            allowed_user_id=allowed_chat_id,
            allowed_chat_id=allowed_chat_id,
        )
        processor.process_callback_query(cq)
    except Exception as e:
        logger.warning(f"Feedback callback failed: {e}")
        answer_callback_fn(cq_id, "")


def handle_gigact_callback(
    data: str,
    callback_query_id: str | None = None,
    allowed_chat_id: int = ALLOWED_CHAT_ID,
    send_message_fn: Callable[..., bool] = default_send_message,
    answer_callback_fn: Callable[[str, str], bool] = default_answer_callback_query,
) -> None:
    """Process a gigact: callback from an action card."""
    from ai_assistant.gig import actions as act
    from ai_assistant.gig.store import get_gig, set_gig_status
    from ai_assistant.gig_pitch_generator import generate_gig_pitch

    def _get(gid: str):
        g = get_gig(gid)
        if g is None:
            return None
        return {
            "id": g.id,
            "title": g.title,
            "status": getattr(g.status, "value", str(g.status)),
            "url": getattr(g, "url", ""),
            "description": getattr(g, "description", ""),
            "budget_text": getattr(g, "budget_text", ""),
            "source": getattr(g, "source", ""),
        }

    def _render_draft(g: dict[str, Any]) -> str:
        return generate_gig_pitch(
            title=g.get("title", ""),
            description=g.get("description", ""),
            budget=g.get("budget_text", ""),
            source=g.get("source"),
        )

    res = act.handle_action(
        data,
        get_gig_fn=_get,
        set_status_fn=lambda gid, st, note=None: set_gig_status(gid, st, note=note),
        render_draft_fn=_render_draft,
        callback_query_id=callback_query_id,
        acked_ids=_gigact_acked,
    )

    outcome = res.get("outcome")
    title = str(res.get("title") or "")
    status = res.get("status")

    if outcome == "already-applied":
        if callback_query_id:
            answer_callback_fn(callback_query_id, "Уже отмечено.")
        return

    if outcome == "refused":
        if callback_query_id:
            answer_callback_fn(callback_query_id, "Не удалось обработать.")
        send_message_fn(allowed_chat_id, act.action_confirmation("refused") or "Не удалось обработать.")
        return

    if outcome == "draft":
        draft = res.get("draft", "")
        if callback_query_id:
            answer_callback_fn(callback_query_id, "Черновик подготовлен.")
        send_message_fn(
            allowed_chat_id,
            f"📝 <b>Черновик отклика для копирования:</b>\n«{title}»\n\n<blockquote>{draft}</blockquote>",
        )
        return

    if outcome == "opened":
        if callback_query_id:
            answer_callback_fn(callback_query_id, "Ссылка открыта.")
        return

    if callback_query_id:
        answer_callback_fn(callback_query_id, "✅ Сохранено!")
    confirm = act.action_confirmation(outcome, title=title, status=status)
    if confirm:
        send_message_fn(allowed_chat_id, confirm)


def dispatch_callback_query(
    cq: dict[str, Any],
    allowed_chat_id: int = ALLOWED_CHAT_ID,
    send_message_fn: Callable[..., bool] = default_send_message,
    answer_callback_fn: Callable[[str, str], bool] = default_answer_callback_query,
    start_track_fn: Callable[[int, str], None] | None = None,
    stop_mock_fn: Callable[[int], None] | None = None,
    main_keyboard: dict[str, Any] = MAIN_KEYBOARD,
) -> None:
    """Route all callback queries to appropriate domain handlers."""
    cq_id = str(cq.get("id"))
    cq_data = cq.get("data", "")
    cq_from = (cq.get("from") or {}).get("id")

    if cq_from != allowed_chat_id:
        return

    if cq_data.startswith("app:"):
        suffix = cq_data.split(":", 1)[1]
        from ai_assistant.telegram_auto_apply import mark_vacancy_applied
        mark_vacancy_applied(suffix)
        answer_callback_fn(cq_id, "✅ Отклик сохранен как отправленный!")

    elif cq_data.startswith("skp:"):
        suffix = cq_data.split(":", 1)[1]
        from ai_assistant.telegram_auto_apply import mark_vacancy_skipped
        mark_vacancy_skipped(suffix)
        answer_callback_fn(cq_id, "⏭ Вакансия пропущена.")

    elif cq_data.startswith("flw:"):
        suffix = cq_data.split(":", 1)[1]
        from ai_assistant.followup_manager import record_followup_sent
        record_followup_sent(suffix)
        answer_callback_fn(cq_id, "✅ Фоллоу-ап зафиксирован!")

    elif cq_data.startswith("flwmute:"):
        suffix = cq_data.split(":", 1)[1]
        from ai_assistant.followup_manager import mute_followup
        mute_followup(suffix)
        answer_callback_fn(cq_id, "🔕 Напоминания отключены.")

    elif cq_data.startswith("mock_tr:"):
        tr_key = cq_data.split(":", 1)[1]
        if start_track_fn:
            start_track_fn(allowed_chat_id, tr_key)
        answer_callback_fn(cq_id, "🎯 Тренировка запущена!")

    elif cq_data.startswith("gigact:"):
        handle_gigact_callback(
            cq_data,
            callback_query_id=cq_id,
            allowed_chat_id=allowed_chat_id,
            send_message_fn=send_message_fn,
            answer_callback_fn=answer_callback_fn,
        )

    elif cq_data.startswith("task_resolve:"):
        task_id_str = cq_data.split(":", 1)[1]
        try:
            t_id = int(task_id_str)
            with db.db_session() as conn:
                conn.execute(
                    "UPDATE user_action_tasks SET status = 'RESOLVED', updated_at = datetime('now') WHERE id = ?",
                    (t_id,),
                )
            answer_callback_fn(cq_id, "✅ Задача отмечена как решенная!")
            send_message_fn(allowed_chat_id, f"✅ Задача #{t_id} успешно закрыта!", reply_markup=main_keyboard)
        except Exception as e:
            answer_callback_fn(cq_id, f"Ошибка: {e}")

    elif cq_data.startswith("gig:"):
        try:
            parts = cq_data.split(":", 2)
            action = parts[1] if len(parts) > 1 else ""
            gig_id = parts[2] if len(parts) > 2 else ""

            if action == "SENT":
                from ai_assistant.business_funnel import FunnelStage, record_conversion_event
                from ai_assistant.gig.store import set_gig_status
                set_gig_status(gig_id, "OUTREACH_SENT", note="Sent by user via Telegram Bot")
                record_conversion_event(
                    lead_id=gig_id,
                    stream="GIGS_FREELANCE",
                    source="freelance_gig",
                    stage=FunnelStage.OUTBOUND_CONFIRMED,
                    notes="Отправлено пользователем через Telegram Bot",
                )
                try:
                    with db.db_session() as conn:
                        conn.execute("UPDATE top_gig_proposals SET status = 'OUTREACH_SENT' WHERE gig_id = ?", (gig_id,))
                except Exception:
                    pass
                answer_callback_fn(cq_id, "✅ Отклик отмечен как отправленный!")
                send_message_fn(allowed_chat_id, f"✅ Отклик на заказ <code>{gig_id}</code> зафиксирован в воронке исходящих!")

            elif action == "REPLY":
                from ai_assistant.business_funnel import FunnelStage, record_conversion_event
                from ai_assistant.gig.store import set_gig_status
                set_gig_status(gig_id, "REPLY_RECEIVED", note="Client reply received")
                record_conversion_event(
                    lead_id=gig_id,
                    stream="GIGS_FREELANCE",
                    source="freelance_gig",
                    stage=FunnelStage.REPLY_RECEIVED,
                    notes="Получен ответ от заказчика",
                )
                try:
                    with db.db_session() as conn:
                        conn.execute("UPDATE top_gig_proposals SET status = 'REPLY_RECEIVED' WHERE gig_id = ?", (gig_id,))
                except Exception:
                    pass
                answer_callback_fn(cq_id, "💬 Ответ заказчика зафиксирован!")
                send_message_fn(allowed_chat_id, f"💬 Зафиксирован ответ заказчика по заказу <code>{gig_id}</code>! Статус обновлен в бизнес-воронке.")

            else:
                from ai_assistant.gig.notify import followup_text, handle_gig_callback
                from ai_assistant.gig.store import get_gig, set_gig_status

                def _gig_row(gid: str):
                    gig = get_gig(gid)
                    if gig is None:
                        return None
                    return {"id": gig.id, "title": gig.title, "status": gig.status.value}

                res = handle_gig_callback(
                    cq_data,
                    get_gig_fn=_gig_row,
                    set_status_fn=lambda gid, st, note: set_gig_status(gid, st, note=note),
                    callback_query_id=cq_id,
                    acked_ids=_gig_acked,
                )
                outcome = res.get("outcome")

                if outcome == "applied":
                    answer_callback_fn(
                        cq_id,
                        "✅ Сохранено!" if res.get("status") == "SHORTLISTED" else "⏭ Пропущено.",
                    )
                elif outcome == "already-applied":
                    answer_callback_fn(cq_id, "Уже отмечено.")
                else:
                    answer_callback_fn(cq_id, "Не удалось обработать.")

                followup = followup_text(
                    outcome or "",
                    str(res.get("title") or res.get("gig_id") or ""),
                    res.get("status"),
                )
                if followup:
                    send_message_fn(allowed_chat_id, followup)

        except Exception as e:
            logger.warning(f"Gig callback failed: {e}")
            answer_callback_fn(cq_id, "")

    elif cq_data.startswith("fb:"):
        handle_fb_callback(
            cq,
            cq_id,
            allowed_chat_id=allowed_chat_id,
            answer_callback_fn=answer_callback_fn,
        )

    elif cq_data == "mock_stop":
        if stop_mock_fn:
            stop_mock_fn(allowed_chat_id)
        send_message_fn(
            allowed_chat_id,
            "🛑 <b>Тренировка завершена.</b> Вы можете начать заново в любое время через меню <b>«🎤 Тренажер интервью»</b>!",
        )
        answer_callback_fn(cq_id, "Тренировка завершена")

    else:
        answer_callback_fn(cq_id, "")
