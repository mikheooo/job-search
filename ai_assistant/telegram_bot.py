"""Stage 56: Production Telegram Bot Interface for Autonomous Job Agent.

Provides commands reading directly from database:
- /status        -> Agent state, recent cycles, active stats.
- /interviews    -> Detected interview invitations.
- /replies       -> Last sent recruiter replies with full exact text.
- /applications  -> Last submitted and tracked applications.
- /help          -> Command guide.

Security:
- Accepts commands ONLY from TELEGRAM_CHAT_ID.
- Ignores/rejects unauthorized chat requests.
- Read-only data queries from existing SQLite DB, zero shell command execution.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ai_assistant import config, db
from ai_assistant.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class TelegramBot:
    """Production Telegram Bot Command Server and Poller."""

    def __init__(
        self,
        bot_token: str | None = None,
        allowed_chat_id: str | None = None,
        notifier: TelegramNotifier | None = None,
        get_updates_fn: Callable[[int], list[dict[str, Any]]] | None = None,
    ):
        self.bot_token = bot_token if bot_token is not None else config.TELEGRAM_BOT_TOKEN
        self.allowed_chat_id = str(allowed_chat_id or config.TELEGRAM_CHAT_ID or "").strip()
        self.notifier = notifier or TelegramNotifier(bot_token=self.bot_token, chat_id=self.allowed_chat_id)
        self.get_updates_fn = get_updates_fn
        self.last_update_id = 0

    def is_authorized(self, chat_id: Any) -> bool:
        """Check whether chat ID matches the designated owner."""
        if not self.allowed_chat_id:
            return False
        return str(chat_id).strip() == self.allowed_chat_id

    # ---------------------------------------------------------------------------
    # Command Handlers (Read-Only DB Queries)
    # ---------------------------------------------------------------------------

    def handle_status(self) -> str:
        """Handle /status command."""
        runs = db.list_autonomous_cycle_runs(limit=3)
        ivs = db.list_interview_events(limit=5)
        notifs = db.list_autonomous_notifications(limit=5, unread_only=True)
        apps = db.list_hh_applications(limit=100)

        submitted_apps = [a for a in apps if a.get("state") == "SUBMITTED"]

        last_run_str = "No runs recorded yet."
        if runs:
            r = runs[0]
            last_run_str = (
                f"Status: {r['status']}\n"
                f"Time: {r['started_at']}\n"
                f"Discovered: {r['discovered_count']} | Applied: {r['applied_count']} | Verified: {r['verified_count']}"
            )

        return (
            "🤖 *AUTONOMOUS JOB AGENT STATUS*\n\n"
            f"*Total Submitted Applications:* {len(submitted_apps)}\n"
            f"*Interview Invitations:* {len(ivs)}\n"
            f"*Pending Notifications:* {len(notifs)}\n\n"
            f"*Last Autonomous Run:*\n{last_run_str}"
        )

    def handle_interviews(self) -> str:
        """Handle /interviews command."""
        ivs = db.list_interview_events(limit=10)
        if not ivs:
            return "🎉 *INTERVIEW INVITATIONS*\n\nПока нет активных приглашений на собеседования."

        lines = ["🎉 *INTERVIEW INVITATIONS*\n"]
        for i, iv in enumerate(ivs, 1):
            detected = iv.get("detected_at") or ""
            company = iv.get("company") or "HeadHunter Employer"
            title = iv.get("vacancy_title") or "Python Role"
            msg = iv.get("invitation_text") or ""
            short_msg = f"{msg[:150]}..." if len(msg) > 150 else msg
            lines.append(
                f"{i}. *{company}* — {title}\n"
                f"   📅 _{detected}_\n"
                f"   💬 \"{short_msg}\""
            )
        return "\n\n".join(lines)

    def handle_replies(self) -> str:
        """Handle /replies command."""
        audits = db.list_conversation_audits(limit=5, status="SENT")
        if not audits:
            return "📩 *SENT RECRUITER REPLIES*\n\nПока нет отправленных автоответов."

        lines = ["📩 *LAST SENT RECRUITER REPLIES*\n"]
        for i, a in enumerate(audits, 1):
            comp = a.get("employer") or "Company"
            inc = a.get("incoming_message") or ""
            sent = a.get("sent_reply") or ""
            sent_at = a.get("sent_at") or a.get("created_at") or ""
            lines.append(
                f"*{i}. {comp}* ({sent_at})\n"
                f"*Employer:* \"{inc[:80]}...\"\n"
                f"*My reply:* \"{sent}\""
            )
        return "\n\n".join(lines)

    def handle_applications(self) -> str:
        """Handle /applications command."""
        apps = db.list_hh_applications(limit=10)
        if not apps:
            return "📋 *APPLICATIONS*\n\nЗаявок пока нет в базе."

        lines = ["📋 *RECENT HH APPLICATIONS*\n"]
        for i, a in enumerate(apps, 1):
            app_id = a.get("application_id")
            title = a.get("title") or "Python Developer"
            comp = a.get("employer") or "Company"
            state = a.get("state")
            lines.append(f"{i}. `{app_id}` | *{state}*\n   {comp} — {title}")
        return "\n".join(lines)

    def handle_stop(self) -> str:
        """Handle /stop command (kill switch)."""
        db.set_submit_paused(True)
        return "🛑 Автоматическая отправка откликов ПРИОСТАНОВЛЕНА (submit_paused=1)."

    def handle_resume(self) -> str:
        """Handle /resume command (kill switch resume)."""
        db.set_submit_paused(False)
        return "▶️ Автоматическая отправка откликов ВОЗОБНОВЛЕНА (submit_paused=0)."

    def handle_digest(self) -> str:
        """Handle /digest command."""
        from .telegram_notifier import format_daily_digest
        return format_daily_digest()

    def handle_help(self) -> str:
        """Handle /help command."""
        return (
            "🤖 *AUTONOMOUS JOB AGENT COMMANDS*\n\n"
            "/status — Текущее состояние агента и статистика\n"
            "/interviews — Приглашения на собеседования\n"
            "/replies — Последние отправленные автоответы работодателям\n"
            "/applications — Последние обработанные заявки\n"
            "/digest — Ежедневный дайджест откликов\n"
            "/stop — Приостановить автоматическую отправку откликов (kill switch)\n"
            "/resume — Возобновить автоматическую отправку откликов\n"
            "/help — Список доступных команд"
        )

    # ---------------------------------------------------------------------------
    # Message Dispatcher
    # ---------------------------------------------------------------------------

    def process_incoming_text(self, chat_id: Any, text: str) -> str:
        """Process incoming command and enforce security filter."""
        if not self.is_authorized(chat_id):
            return "⛔ Доступ запрещён. Этот бот настроен только для авторизованного владельца."

        cmd = (text or "").strip().split()[0].lower()
        if cmd in ("/status", "status"):
            return self.handle_status()
        elif cmd in ("/interviews", "interviews"):
            return self.handle_interviews()
        elif cmd in ("/replies", "replies"):
            return self.handle_replies()
        elif cmd in ("/applications", "applications"):
            return self.handle_applications()
        elif cmd in ("/stop", "stop"):
            return self.handle_stop()
        elif cmd in ("/resume", "resume"):
            return self.handle_resume()
        elif cmd in ("/digest", "digest"):
            return self.handle_digest()
        elif cmd in ("/help", "help", "/start", "start"):
            return self.handle_help()
        else:
            return f"Неизвестная команда: `{cmd}`. Введите /help для просмотра доступных команд."

    def process_update(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single Telegram update dict (messages and callback queries)."""
        update_id = update.get("update_id")
        if update_id:
            self.last_update_id = max(self.last_update_id, update_id)

        # 1. Handle Callback Query (Stage 89 Human Feedback)
        cb = update.get("callback_query")
        if cb:
            from .telegram_feedback import TelegramFeedbackProcessor
            processor = TelegramFeedbackProcessor(
                allowed_user_id=self.allowed_chat_id,
                allowed_chat_id=self.allowed_chat_id,
                notifier=self.notifier,
            )
            return processor.process_callback_query(cb)

        # 2. Handle Text Messages / Commands
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return None

        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = msg.get("text") or ""

        response_text = self.process_incoming_text(chat_id=chat_id, text=text)
        if response_text:
            return self.notifier.send_message(text=response_text, chat_id=chat_id)
        return None
