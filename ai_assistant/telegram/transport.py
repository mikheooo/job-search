"""Low-level HTTP transport and Telegram Bot API bindings."""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import sys
import urllib.parse
import urllib.request
from typing import Any

from ai_assistant import config

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

logger = logging.getLogger("telegram_control_bot")

BOT_LOCK_NAME = "telegram_control_bot.lock"
BOT_LOCK_DIR = os.path.join(str(PROJECT_ROOT), "logs")
BOT_HEARTBEAT_FILE = os.path.join(str(PROJECT_ROOT), "logs", "telegram_control_bot.heartbeat")

try:
    _CODE_REV = str(int(os.path.getmtime(__file__)))
except Exception:
    _CODE_REV = "unknown"

TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
try:
    ALLOWED_CHAT_ID = int(str(config.TELEGRAM_OWNER_ID or config.TELEGRAM_CHAT_ID or 0))
except (TypeError, ValueError):
    ALLOWED_CHAT_ID = 0

MAIN_KEYBOARD: dict[str, Any] = {
    "keyboard": [
        [{"text": "🤖 Автопилот"}, {"text": "📊 Статус"}],
        [{"text": "📈 Воронка"}, {"text": "💼 Быстрые заказы"}],
        [{"text": "🚀 Отклики HH"}, {"text": "🚀 Поднять резюме HH"}],
        [{"text": "🎤 Тренажер интервью"}, {"text": "💬 Отклики в Telegram"}],
        [{"text": "🔄 Фоллоу-ап"}, {"text": "📨 Вакансии в 1 клик"}],
        [{"text": "📎 Скачать English CV"}, {"text": "💬 Диалоги HH"}],
        [{"text": "🔄 Сканировать все источники"}],
    ],
    "resize_keyboard": True,
}


def write_heartbeat(heartbeat_file: str | None = None, code_rev: str | None = None) -> None:
    hb_file = heartbeat_file or BOT_HEARTBEAT_FILE
    rev = code_rev or _CODE_REV
    try:
        os.makedirs(os.path.dirname(hb_file), exist_ok=True)
        with open(hb_file, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "code_rev": rev,
                    }
                )
            )
    except Exception:
        pass


def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str = "HTML",
    token: str | None = None,
) -> bool:
    """Send text message to Telegram."""
    bot_token = token or TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            status = getattr(resp, "status", getattr(resp, "code", 200))
            return status == 200
    except Exception as e:
        logger.warning(f"Failed to send message: {e}")
        return False


def answer_callback_query(
    callback_query_id: str,
    text: str = "",
    token: str | None = None,
) -> bool:
    """Acknowledge an incoming inline callback query."""
    bot_token = token or TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "text": text}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = getattr(resp, "status", getattr(resp, "code", 200))
            return status == 200
    except Exception as e:
        logger.debug(f"Failed to answer callback query: {e}")
        return False


def send_document(
    chat_id: int,
    file_path: pathlib.Path | str,
    caption: str = "",
    filename: str | None = None,
    token: str | None = None,
) -> bool:
    """Upload and send a document/file to Telegram."""
    path = pathlib.Path(file_path)
    if not path.exists():
        return False
    bot_token = token or TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    name = filename or path.name
    try:
        import requests
        with open(path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"document": (name, f, "application/octet-stream")},
                timeout=35,
            )
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to send document: {e}")
        return False
