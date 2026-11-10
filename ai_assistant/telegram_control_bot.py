"""Live Telegram long-poller facade for @jobsassistbot.

This is the recommended poller entry point:

    python -m ai_assistant.telegram_control_bot

``ai_assistant.bot`` is a locked legacy callback script.
``ai_assistant.telegram_bot.TelegramBot`` is a read-only command helper and
does not poll. Do not run a second poller against the same bot token.

Interactive Telegram Control Bot for Job Search Assistant.
Decomposed into modular domain components under `ai_assistant.telegram`:
- `transport`: low-level HTTP transport and Bot API bindings
- `cards`: message card builders and live system metrics
- `handlers_callbacks`: inline callback query handlers (`fb:*`, `gigact:*`, etc.)
- `handlers_commands`: text command handlers (`/status`, `/gigs`, `/funnel`, etc.)
- `bot_runner`: long-polling main loop and single-instance coordination
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Add DLL directories for Windows CPython distributions (e.g. Astral uv)
if sys.platform == "win32":
    for p in [
        pathlib.Path(sys.executable).parent / "DLLs",
        pathlib.Path(r"C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\DLLs"),
    ]:
        if p.exists():
            try:
                os.add_dll_directory(str(p))
            except Exception:
                pass

from ai_assistant import config, db
from ai_assistant.telegram import bot_runner, cards, handlers_callbacks, handlers_commands, transport
from ai_assistant.telegram.cards import get_system_stats
from ai_assistant.telegram.transport import (
    ALLOWED_CHAT_ID,
    BOT_HEARTBEAT_FILE,
    BOT_LOCK_DIR,
    BOT_LOCK_NAME,
    MAIN_KEYBOARD,
    TELEGRAM_BOT_TOKEN,
    _CODE_REV,
    answer_callback_query as _transport_answer_callback_query,
    send_document as _transport_send_document,
    send_message as _transport_send_message,
    write_heartbeat,
)

logger = logging.getLogger("telegram_control_bot")

_LOG_PATH = os.path.join(str(PROJECT_ROOT), "logs", "telegram_control_bot.log")
try:
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    _file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _write_heartbeat() -> None:
    mod = sys.modules[__name__]
    hb_file = getattr(mod, "BOT_HEARTBEAT_FILE", BOT_HEARTBEAT_FILE)
    write_heartbeat(heartbeat_file=hb_file, code_rev=_CODE_REV)


def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str = "HTML",
) -> bool:
    mod = sys.modules[__name__]
    token = getattr(mod, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    return _transport_send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        token=token,
    )


def answer_callback_query(callback_query_id: str, text: str = "") -> bool:
    mod = sys.modules[__name__]
    token = getattr(mod, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    return _transport_answer_callback_query(
        callback_query_id=callback_query_id,
        text=text,
        token=token,
    )


def send_document(
    chat_id: int,
    file_path: pathlib.Path | str,
    caption: str = "",
    filename: str | None = None,
) -> bool:
    mod = sys.modules[__name__]
    token = getattr(mod, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    return _transport_send_document(
        chat_id=chat_id,
        file_path=file_path,
        caption=caption,
        filename=filename,
        token=token,
    )


def handle_autopilot_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_autopilot_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
        main_keyboard=getattr(mod, "MAIN_KEYBOARD", MAIN_KEYBOARD),
    )


def handle_status_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_status_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
        main_keyboard=getattr(mod, "MAIN_KEYBOARD", MAIN_KEYBOARD),
    )


def handle_funnel_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_funnel_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
        main_keyboard=getattr(mod, "MAIN_KEYBOARD", MAIN_KEYBOARD),
    )


def handle_followup_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_followup_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_bump_resume_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_bump_resume_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
        main_keyboard=getattr(mod, "MAIN_KEYBOARD", MAIN_KEYBOARD),
    )


def handle_mock_interview_menu(chat_id: int):
    mod = sys.modules[__name__]
    token = getattr(mod, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    return handlers_commands.handle_mock_interview_menu(chat_id=chat_id, token=token)


def start_track_session(chat_id: int, track_key: str):
    mod = sys.modules[__name__]
    token = getattr(mod, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    return handlers_commands.start_track_session(chat_id=chat_id, track_key=track_key, token=token)


def handle_apply_hh_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_apply_hh_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_tg_apply_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_tg_apply_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_scan_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_scan_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_chats_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_chats_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_more_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_more_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_kwork_command(chat_id: int, text: str):
    mod = sys.modules[__name__]
    return handlers_commands.handle_kwork_command(
        chat_id=chat_id,
        text=text,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_gigact_callback(data: str, callback_query_id: str | None = None) -> None:
    mod = sys.modules[__name__]
    return handlers_callbacks.handle_gigact_callback(
        data=data,
        callback_query_id=callback_query_id,
        allowed_chat_id=getattr(mod, "ALLOWED_CHAT_ID", ALLOWED_CHAT_ID),
        send_message_fn=getattr(mod, "send_message", send_message),
        answer_callback_fn=getattr(mod, "answer_callback_query", answer_callback_query),
    )


def handle_fb_callback(cq: dict[str, Any], cq_id: str) -> None:
    mod = sys.modules[__name__]
    return handlers_callbacks.handle_fb_callback(
        cq=cq,
        cq_id=cq_id,
        allowed_chat_id=getattr(mod, "ALLOWED_CHAT_ID", ALLOWED_CHAT_ID),
        answer_callback_fn=getattr(mod, "answer_callback_query", answer_callback_query),
    )


def handle_gigs_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_gigs_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
    )


def handle_cv_command(chat_id: int):
    mod = sys.modules[__name__]
    return handlers_commands.handle_cv_command(
        chat_id=chat_id,
        send_message_fn=getattr(mod, "send_message", send_message),
        send_document_fn=getattr(mod, "send_document", send_document),
    )


def handle_send_email_command(chat_id: int, text: str):
    mod = sys.modules[__name__]
    return handlers_commands.handle_send_email_command(
        chat_id=chat_id,
        text=text,
        send_message_fn=getattr(mod, "send_message", send_message),
        send_document_fn=getattr(mod, "send_document", send_document),
    )


def handle_inquiry_reply(chat_id: int, user_text: str):
    mod = sys.modules[__name__]
    return handlers_commands.handle_inquiry_reply(
        chat_id=chat_id,
        user_text=user_text,
        send_message_fn=getattr(mod, "send_message", send_message),
        main_keyboard=getattr(mod, "MAIN_KEYBOARD", MAIN_KEYBOARD),
    )


def run_bot_polling():
    mod = sys.modules[__name__]
    return bot_runner.run_bot_polling(
        lock_dir=getattr(mod, "BOT_LOCK_DIR", BOT_LOCK_DIR),
        lock_name=getattr(mod, "BOT_LOCK_NAME", BOT_LOCK_NAME),
        heartbeat_file=getattr(mod, "BOT_HEARTBEAT_FILE", BOT_HEARTBEAT_FILE),
        token=getattr(mod, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        allowed_chat_id=getattr(mod, "ALLOWED_CHAT_ID", ALLOWED_CHAT_ID),
        send_message_fn=getattr(mod, "send_message", send_message),
        answer_callback_fn=getattr(mod, "answer_callback_query", answer_callback_query),
        send_document_fn=getattr(mod, "send_document", send_document),
    )


if __name__ == "__main__":
    run_bot_polling()
