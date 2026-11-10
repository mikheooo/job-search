"""Long-polling main loop and single-instance runner for Telegram control bot."""
from __future__ import annotations

import atexit
import datetime
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from ai_assistant.mock_interviewer import get_current_session, process_user_answer, stop_mock_interview
from ai_assistant.runner import SingleInstanceLock, is_pid_running
from ai_assistant.telegram.cards import get_system_stats
from ai_assistant.telegram.handlers_callbacks import dispatch_callback_query, handle_fb_callback, handle_gigact_callback
from ai_assistant.telegram.handlers_commands import (
    handle_apply_hh_command,
    handle_autopilot_command,
    handle_bump_resume_command,
    handle_chats_command,
    handle_cv_command,
    handle_followup_command,
    handle_funnel_command,
    handle_gigs_command,
    handle_inquiry_reply,
    handle_kwork_command,
    handle_mock_interview_menu,
    handle_more_command,
    handle_scan_command,
    handle_send_email_command,
    handle_status_command,
    handle_tg_apply_command,
    start_track_session,
)
from ai_assistant.telegram.transport import (
    ALLOWED_CHAT_ID,
    BOT_HEARTBEAT_FILE,
    BOT_LOCK_DIR,
    BOT_LOCK_NAME,
    MAIN_KEYBOARD,
    TELEGRAM_BOT_TOKEN,
    answer_callback_query as default_answer_callback_query,
    send_document as default_send_document,
    send_message as default_send_message,
    write_heartbeat,
)

logger = logging.getLogger("telegram_control_bot")


def run_bot_polling(
    lock_dir: str | None = None,
    lock_name: str | None = None,
    heartbeat_file: str | None = None,
    token: str | None = None,
    allowed_chat_id: int | None = None,
    send_message_fn: Callable[..., bool] | None = None,
    answer_callback_fn: Callable[[str, str], bool] | None = None,
    send_document_fn: Callable[..., bool] | None = None,
) -> None:
    """Main long polling loop for Telegram Control Bot."""
    active_lock_dir = lock_dir or BOT_LOCK_DIR
    active_lock_name = lock_name or BOT_LOCK_NAME
    active_hb_file = heartbeat_file or BOT_HEARTBEAT_FILE
    bot_token = token or TELEGRAM_BOT_TOKEN
    chat_id = allowed_chat_id if allowed_chat_id is not None else ALLOWED_CHAT_ID
    send_msg = send_message_fn or default_send_message
    ans_cb = answer_callback_fn or default_answer_callback_query
    send_doc = send_document_fn or default_send_document

    win_path = os.path.join(active_lock_dir, active_lock_name + ".win")
    os.makedirs(active_lock_dir, exist_ok=True)

    def _read_win_owner() -> int:
        try:
            with open(win_path, encoding="utf-8") as f:
                return int((f.read().splitlines() or ["0"])[0].strip() or 0)
        except Exception:
            return 0

    won_race = False
    for _ in range(2):
        try:
            fd = os.open(win_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(
                    f"{os.getpid()}\n"
                    f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}"
                )
            won_race = True
            break
        except FileExistsError:
            owner = _read_win_owner()
            deadline = time.time() + 3.0
            while owner == 0 and time.time() < deadline:
                time.sleep(0.05)
                owner = _read_win_owner()
            if owner and is_pid_running(owner):
                print(
                    f"telegram_control_bot: race lost, live owner {owner} holds {win_path}",
                    flush=True,
                )
                logger.warning("Atomic guard held by live PID %s; exiting.", owner)
                sys.exit(3)
            try:
                os.unlink(win_path)
            except Exception:
                pass

    if not won_race:
        print("telegram_control_bot: cannot arbitrate startup, failing closed.", flush=True)
        logger.warning("Atomic guard arbitration failed; exiting.")
        sys.exit(3)

    def _release_win() -> None:
        try:
            with open(win_path, encoding="utf-8") as f:
                mine = int((f.read().splitlines() or ["0"])[0].strip() or 0)
            if mine == os.getpid():
                os.unlink(win_path)
        except Exception:
            pass

    lock = SingleInstanceLock(
        lock_dir=active_lock_dir,
        lock_name=active_lock_name,
    )
    acquired, reason = lock.acquire()
    if not acquired:
        print(f"telegram_control_bot: another instance is running ({reason})", flush=True)
        logger.warning("Another control-bot instance holds the lock; exiting: %s", reason)
        sys.exit(3)

    logger.info("Starting Telegram Control Bot polling loop...")
    write_heartbeat(heartbeat_file=active_hb_file)

    atexit.register(lock.release)
    atexit.register(_release_win)
    offset = 0

    send_msg(
        chat_id,
        "👋 <b>Пульт управления JobSearch Assistant подключен!</b>\n\n"
        "Добавлены функции:\n"
        "• <b>🚀 Поднять резюме HH:</b> мгновенный подъем резюме в топ выдачи hh.ru\n"
        "• <b>🎤 Тренажер интервью:</b> интерактивная тренировка вопросов DevOps, Linux & Bangkok English\n"
        "• <b>📈 Воронка:</b> детальный аналитический отчет конверсии по всем этапам\n"
        "• <b>🔄 Фоллоу-ап:</b> автоматические напоминания и вежливые повторные сообщения\n"
        "• <b>💬 Отклики в Telegram:</b> интерактивный отклик в 1 тап в чаты рекрутеров\n"
        "• <b>🌏 Бангкок Tech Careers:</b> вакансии Agoda & Lalamove с прямой подачей\n"
        "• <b>💼 Быстрые заказы:</b> фриланс и проектные задачи\n"
        "• <b>📎 Скачать English CV:</b> готовый PDF для Бангкока и LinkedIn\n"
        "• <b>✉️ Отправка Email:</b> команда <code>/send_email hr@company.com Роль</code>\n\n"
        "Используйте кнопки меню внизу:",
        reply_markup=MAIN_KEYBOARD,
    )

    while True:
        write_heartbeat(heartbeat_file=active_hb_file)
        try:
            _allowed = urllib.parse.quote(json.dumps(["message", "callback_query"]))
            url = (f"https://api.telegram.org/bot{bot_token}/getUpdates"
                   f"?offset={offset}&timeout=25&allowed_updates={_allowed}")
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            updates = data.get("result", [])
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)

                # 1. Handle Inline Callbacks
                if "callback_query" in update:
                    cq = update["callback_query"]
                    dispatch_callback_query(
                        cq,
                        allowed_chat_id=chat_id,
                        send_message_fn=send_msg,
                        answer_callback_fn=ans_cb,
                        start_track_fn=start_track_session,
                        stop_mock_fn=stop_mock_interview,
                        main_keyboard=MAIN_KEYBOARD,
                    )
                    continue

                # 2. Handle Text Messages
                msg = update.get("message") or {}
                from_user = msg.get("from") or {}
                user_id = from_user.get("id")

                if user_id != chat_id:
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                logger.info(f"Received user command/message: '{text[:80]}...'")

                # Mock interview session active answer
                active_mock = get_current_session(chat_id)
                if active_mock and not text.startswith("/") and text not in (
                    "📊 Статус", "📈 Воронка", "🚀 Отклики HH", "🚀 Поднять резюме HH",
                    "🎤 Тренажер интервью", "💬 Отклики в Telegram", "🔄 Фоллоу-ап",
                    "💼 Быстрые заказы", "📨 Вакансии в 1 клик", "📎 Скачать English CV",
                    "💬 Диалоги HH", "🔄 Сканировать все источники"
                ):
                    res = process_user_answer(chat_id, text)
                    ev = res.get("evaluation", {})
                    pros_str = "\n".join(f"• {p}" for p in ev.get("pros", []))
                    imp_str = "\n".join(f"• {i}" for i in ev.get("improvements", []))

                    eval_card = (
                        f"📊 <b>Оценка вашего ответа:</b> {ev.get('verdict')}\n\n"
                        f"✅ <b>Сильные стороны:</b>\n{pros_str}\n\n"
                        f"💡 <b>Точки роста:</b>\n{imp_str}\n\n"
                        f"🏆 <b>Эталонный ответ:</b>\n"
                        f"<blockquote>{ev.get('golden_answer')}</blockquote>"
                    )

                    if res.get("has_next"):
                        next_q_msg = (
                            f"\n\n<b>Следующий вопрос ({res['next_q_num']} из {res['total_questions']}):</b>\n\n"
                            f"❓ <i>{res['next_question_text']}</i>"
                        )
                        buttons = [[{"text": "🛑 Завершить тренировку", "callback_data": "mock_stop"}]]
                        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                        payload = {
                            "chat_id": chat_id,
                            "text": eval_card + next_q_msg,
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": buttons},
                        }
                        urllib.request.urlopen(
                            urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}),
                            timeout=10,
                        )
                    else:
                        concl_msg = "\n\n🎉 <b>Поздравляем! Трек полностью пройден.</b> Вы отлично справились со всеми ключевыми вопросами!"
                        send_msg(chat_id, eval_card + concl_msg, reply_markup=MAIN_KEYBOARD)
                    continue

                if text in ("🤖 Автопилот", "/autopilot", "автопилот"):
                    handle_autopilot_command(chat_id, send_message_fn=send_msg, main_keyboard=MAIN_KEYBOARD)
                elif text in ("📊 Статус", "/status"):
                    handle_status_command(chat_id, send_message_fn=send_msg, main_keyboard=MAIN_KEYBOARD)
                elif text in ("📈 Воронка", "/funnel", "/analytics", "воронка"):
                    handle_funnel_command(chat_id, send_message_fn=send_msg, main_keyboard=MAIN_KEYBOARD)
                elif text in ("🚀 Поднять резюме HH", "/bump_resume", "поднять резюме"):
                    handle_bump_resume_command(chat_id, send_message_fn=send_msg, main_keyboard=MAIN_KEYBOARD)
                elif text in ("🎤 Тренажер интервью", "/interview", "/mock", "интервью", "тренажер"):
                    handle_mock_interview_menu(chat_id, token=bot_token)
                elif text in ("🔄 Фоллоу-ап", "/followup", "фоллоу-ап", "фоллоуап"):
                    handle_followup_command(chat_id, send_message_fn=send_msg)
                elif text in ("🚀 Отклики HH", "/apply_hh"):
                    handle_apply_hh_command(chat_id, send_message_fn=send_msg)
                elif text in ("💬 Отклики в Telegram", "/apply_tg", "отклики тг", "тг"):
                    handle_tg_apply_command(chat_id, send_message_fn=send_msg)
                elif text in ("🔄 Сканировать все источники", "/scan"):
                    handle_scan_command(chat_id, send_message_fn=send_msg)
                elif text in ("💬 Диалоги HH", "/chats"):
                    handle_chats_command(chat_id, send_message_fn=send_msg)
                elif text in ("📨 Вакансии в 1 клик", "/more"):
                    handle_more_command(chat_id, send_message_fn=send_msg)
                elif text in ("💼 Быстрые заказы", "/gigs", "заказы", "фриланс"):
                    handle_gigs_command(chat_id, send_message_fn=send_msg)
                elif text.startswith("/kwork"):
                    handle_kwork_command(chat_id, text, send_message_fn=send_msg)
                elif text in ("📎 Скачать English CV", "/cv", "резюме", "cv"):
                    handle_cv_command(chat_id, send_message_fn=send_msg, send_document_fn=send_doc)
                elif text.startswith("/email") or text.startswith("/send_email"):
                    handle_send_email_command(chat_id, text, send_message_fn=send_msg, send_document_fn=send_doc)
                elif text in ("/start", "/help"):
                    send_msg(
                        chat_id,
                        "🤖 <b>JobSearch Assistant Bot</b>\n\n"
                        "Команды управления:\n"
                        "• <b>🤖 Автопилот</b> — Центр управления Paseo (статус, блокеры, кнопки решений)\n"
                        "• <b>📊 Статус</b> — общая статистика откликов и базы\n"
                        "• <b>📈 Воронка</b> — аналитический отчет конверсии по всем источникам\n"
                        "• <b>🚀 Поднять резюме HH</b> — обновить дату резюме в поиске\n"
                        "• <b>🎤 Тренажер интервью</b> — тренировка ответов на собеседованиях\n"
                        "• <b>🔄 Фоллоу-ап</b> — напомнить о себе работодателям через 3–5 дней\n"
                        "• <b>🚀 Отклики HH</b> — запустить внеочередную подачу откликов на HH\n"
                        "• <b>💬 Отклики в Telegram</b> — отклики в 1 тап в чаты рекрутеров\n"
                        "• <b>💼 Быстрые заказы</b> — актуальные заказы на фриланс с готовыми откликами\n"
                        "• <b>📨 Вакансии в 1 клик</b> — свежие вакансии со всех источников\n"
                        "• <b>📎 Скачать English CV</b> — получить готовый PDF резюме для Бангкока\n"
                        "• <b>✉️ /send_email email роль</b> — отправить отклик с прикрепленным CV\n"
                        "• <b>💬 Диалоги HH</b> — проверить статусы и приглашения работодателей\n"
                        "• <b>🔄 Сканировать все источники</b> — обновить базы вакансий\n\n"
                        "💡 <i>Также вы можете переслать в этот чат любое сообщение или вопрос рекрутера — я мгновенно сформирую идеальный ответ!</i>",
                        reply_markup=MAIN_KEYBOARD,
                    )
                else:
                    handle_inquiry_reply(chat_id, text, send_message_fn=send_msg, main_keyboard=MAIN_KEYBOARD)

        except urllib.error.URLError as e:
            logger.debug(f"Polling network glitch: {e}")
            time.sleep(2)
        except Exception as e:
            logger.warning(f"Polling loop exception: {e}")
            time.sleep(3)
