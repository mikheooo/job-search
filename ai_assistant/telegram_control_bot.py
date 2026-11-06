"""Live Telegram long-poller for @jobsassistbot.

This is the recommended poller entry point:

    python -m ai_assistant.telegram_control_bot

``ai_assistant.bot`` is a locked legacy callback script.
``ai_assistant.telegram_bot.TelegramBot`` is a read-only command helper and
does not poll. Do not run a second poller against the same bot token.

Interactive Telegram Control Bot for Job Search Assistant.

Provides an interactive mobile control room via @jobsassistbot:
- 📊 Статус: real-time application metrics, DB counts, queue health.
- 📈 Воронка: end-to-end analytics report (sourcing, filtering, outreach, responses, conversion rates).
- 🚀 Отклики HH: triggers on-demand autonomous HH application cycle.
- 🚀 Поднять резюме HH: automated resume bumping on HeadHunter (keeps resume at top of recruiter search).
- 🎤 Тренажер интервью: AI mock interview trainer (DevOps, Linux, Python, Bangkok English, System Design).
- 💬 Отклики в Telegram: delivers targeted Telegram IT vacancies with 1-tap recruiter DM deeplinks & copyable pitches.
- 🔄 Фоллоу-ап: automated follow-up reminders & sequences after 3-5 days of silence.
- 💼 Быстрые заказы: delivers freelance & quick IT tasks with ready-to-send proposal pitches.
- 📨 Вакансии в 1 клик: delivers fresh target vacancies with 1-click apply deeplinks across all sources.
- 📎 Скачать English CV: generates and uploads high-impact Bangkok English CV (PDF).
- ✉️ Отправка Email (/send_email): автоматическая отправка письма рекрутеру с вложенным PDF резюме.
- 💬 Диалоги HH: checks negotiation statuses, chats, and prepares response drafts.
- 🔄 Сканировать все источники: triggers multi-source discovery (Agoda, TG, Hirify, Habr, LinkedIn, GeekJob).
- 🤖 Умный ассистент переписки: перешлите любой вопрос работодателя или рекрутера в бот — получите безупречный готовый ответ!
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config, db
from ai_assistant.followup_manager import dispatch_followup_batch, mute_followup, record_followup_sent
from ai_assistant.funnel_analytics import format_funnel_report
from ai_assistant.gig_pitch_generator import build_gig_deeplink, generate_gig_pitch
from ai_assistant.hh_resume_bumper import bump_hh_resumes
from ai_assistant.interview_auto_responder import analyze_employer_inquiry
from ai_assistant.mock_interviewer import (
    TRACKS,
    get_current_session,
    process_user_answer,
    start_mock_interview,
    stop_mock_interview,
)

logger = logging.getLogger("telegram_control_bot")

# Phase 5 windowless: this process is started by pythonw.exe (no console at all),
# so stderr is NOT attached to anything. Log to the file directly instead of
# relying on the launcher's `>> log 2>&1` redirection — that redirection was
# what forced a visible cmd.exe window on every scheduled start.
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
    # Never let logging setup kill the bot; fall back to stderr-only.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

# Phase 3 single-instance + health: lock prevents duplicate polling loops
# (scheduler hourly trigger uses IgnoreNew, but manual starts need a guard);
# heartbeat file lets read-only diagnostics prove the bot is alive.
BOT_LOCK_NAME = "telegram_control_bot.lock"
BOT_LOCK_DIR = os.path.join(str(PROJECT_ROOT), "logs")
BOT_HEARTBEAT_FILE = os.path.join(PROJECT_ROOT, "logs", "telegram_control_bot.heartbeat")

try:
    _CODE_REV = str(int(os.path.getmtime(__file__)))
except Exception:
    _CODE_REV = "unknown"


def _write_heartbeat() -> None:
    try:
        os.makedirs(os.path.dirname(BOT_HEARTBEAT_FILE), exist_ok=True)
        with open(BOT_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "timestamp": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        # Phase 4C.4: code revision of the RUNNING image, so
                        # diagnostics can tell stale-code pollers apart from
                        # current ones (a long-lived poller pins old code).
                        "code_rev": _CODE_REV,
                    }
                )
            )
    except Exception:
        pass

# Phase 3 security: credentials come from environment (.env) via
# ai_assistant.config — no hardcoded tokens. Resolves to the same HH bot
# previously hardcoded here, so runtime behavior is unchanged.
TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
try:
    ALLOWED_CHAT_ID = int(str(config.TELEGRAM_OWNER_ID or config.TELEGRAM_CHAT_ID or 0))
except (TypeError, ValueError):
    ALLOWED_CHAT_ID = 0

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Статус"}, {"text": "📈 Воронка"}],
        [{"text": "🚀 Отклики HH"}, {"text": "🚀 Поднять резюме HH"}],
        [{"text": "🎤 Тренажер интервью"}, {"text": "💬 Отклики в Telegram"}],
        [{"text": "🔄 Фоллоу-ап"}, {"text": "💼 Быстрые заказы"}],
        [{"text": "📨 Вакансии в 1 клик"}, {"text": "📎 Скачать English CV"}],
        [{"text": "💬 Диалоги HH"}, {"text": "🔄 Сканировать все источники"}],
    ],
    "resize_keyboard": True,
}


def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str = "HTML",
) -> bool:
    """Send text message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
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
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Failed to send message: {e}")
        return False


def answer_callback_query(callback_query_id: str, text: str = "") -> bool:
    """Acknowledge an incoming inline callback query."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "text": text}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception as e:
        logger.debug(f"Failed to answer callback query: {e}")
        return False


def send_document(
    chat_id: int,
    file_path: pathlib.Path | str,
    caption: str = "",
    filename: str | None = None,
) -> bool:
    """Upload and send a document/file to Telegram."""
    path = pathlib.Path(file_path)
    if not path.exists():
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
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


def get_system_stats() -> dict[str, Any]:
    """Query state.db and return live dashboard metrics."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM vacancies")
    total_vacancies = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE state = 'DISCOVERED'")
    discovered_cnt = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE state = 'SUBMITTED'")
    submitted_cnt = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE source = 'hh'")
    hh_cnt = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE source IN ('linkedin_bangkok', 'bangkok_careers')")
    bangkok_cnt = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE source LIKE 'tg_%' OR source IN ('hirify', 'habrcareer', 'geekjob')")
    external_cnt = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM vacancies WHERE source IN ('tg_freelance_orders', 'tg_zakazy_it') OR raw_data LIKE '%\"is_gig\": true%'")
    gigs_cnt = cursor.fetchone()[0]

    conn.close()

    return {
        "total": total_vacancies,
        "discovered": discovered_cnt,
        "submitted": submitted_cnt,
        "hh": hh_cnt,
        "bangkok": bangkok_cnt,
        "external": external_cnt,
        "gigs": gigs_cnt,
    }


def handle_status_command(chat_id: int):
    """Output comprehensive system status report."""
    stats = get_system_stats()
    text = (
        f"📊 <b>Живой статус системы JobSearch:</b>\n\n"
        f"📂 <b>Всего вакансий в базе:</b> {stats['total']}\n"
        f"⏳ <b>Готовы к отклику (DISCOVERED):</b> {stats['discovered']}\n"
        f"✅ <b>Успешно отправлено откликов:</b> {stats['submitted']}\n\n"
        f"🌐 <b>Источники в базе:</b>\n"
        f"• HeadHunter (автопилот): <b>{stats['hh']}</b>\n"
        f"• 🌏 Бангкок (Agoda, Lalamove, LinkedIn): <b>{stats['bangkok']}</b>\n"
        f"• Внешние IT-каналы, Habr, GeekJob, Hirify: <b>{stats['external']}</b>\n"
        f"• Быстрые заказы / Фриланс: <b>{stats['gigs']}</b>\n\n"
        f"🕒 <i>Фоновые демоны активны (HH Autopilot каждые 4ч, Multi-Discovery каждые 2ч).</i>"
    )
    send_message(chat_id, text, reply_markup=MAIN_KEYBOARD)


def handle_funnel_command(chat_id: int):
    """Output full recruitment funnel analytics report."""
    try:
        report = format_funnel_report()
        send_message(chat_id, report, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        send_message(chat_id, f"❌ Ошибка генерации воронки: {e}")


def handle_followup_command(chat_id: int):
    """Dispatch automated follow-up reminders for older applications."""
    def worker():
        send_message(chat_id, "🔄 <b>Проверяю отправленные отклики и формирую фоллоу-апы...</b>")
        try:
            cnt = dispatch_followup_batch(limit=2)
            if cnt == 0:
                send_message(chat_id, "ℹ️ На данный момент нет откликов, требующих повторного фоллоу-апа.")
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка проверки фоллоу-апов: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_bump_resume_command(chat_id: int):
    """Trigger HH resume auto-bumping to put candidate at top of search."""
    def worker():
        send_message(chat_id, "🚀 <b>Проверяю и поднимаю резюме на HeadHunter...</b>")
        try:
            res = bump_hh_resumes()
            bumped = res.get("bumped", 0)
            resumes = res.get("resumes", [])
            lines = []
            for r in resumes:
                act = "✅" if r.get("action") == "BUMPED" else "⏳"
                lines.append(f"{act} <b>{r.get('title', 'Резюме')}</b>: {r.get('status')}")

            details = "\n".join(lines) if lines else "Кнопка обновления проверена."
            msg = (
                f"🚀 <b>Результат обновления резюме на HH:</b>\n\n"
                f"• Поднято резюме: <b>{bumped}</b>\n\n"
                f"{details}\n\n"
                f"💡 <i>Резюме в топе выдачи привлекает в 3-5 раз больше прямых просмотров работодателей!</i>"
            )
            send_message(chat_id, msg, reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка автоподнятия резюме: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_mock_interview_menu(chat_id: int):
    """Offer track selection for mock interview."""
    buttons = [
        [{"text": "🛠 DevOps, Docker & CI/CD", "callback_data": "mock_tr:devops"}],
        [{"text": "🐧 Linux, Сети & Траблшутинг", "callback_data": "mock_tr:linux_net"}],
        [{"text": "🌏 Bangkok English (Expat Interview)", "callback_data": "mock_tr:bangkok_en"}],
        [{"text": "🏗 System Design & Высокие нагрузки", "callback_data": "mock_tr:sysdesign"}],
    ]
    msg = (
        "🎤 <b>AI-Тренажер технических и международных собеседований</b>\n\n"
        "Выберите направление для тренировки:\n\n"
        "• <b>DevOps & Docker:</b> OOMKilled, мониторинг SRE, zero-downtime CI/CD\n"
        "• <b>Linux & Сети:</b> Траблшутинг по уровням OSI, Load Average, iptables\n"
        "• <b>Bangkok English:</b> Самопрезентация, опыт в Таиланде, zero-relocation\n"
        "• <b>System Design:</b> Отказоустойчивость, спасение production баз данных\n\n"
        "<i>Бот задаст вопрос, вы ответите текстом или голосом — ИИ оценит глубину и даст эталонную формулировку!</i>"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}), timeout=10)


def start_track_session(chat_id: int, track_key: str):
    """Start specific mock track."""
    res = start_mock_interview(chat_id, track_key)
    msg = (
        f"🎯 <b>Начало тренировки: {res['track_title']}</b>\n\n"
        f"<b>Вопрос 1 из {res['total_questions']}:</b>\n\n"
        f"❓ <i>{res['question_text']}</i>\n\n"
        f"💡 <b>Как отвечать:</b> Напишите ваш ответ в чат (или надиктуйте голосом). Опишите реальный опыт и шаги решения!"
    )
    buttons = [[{"text": "🛑 Завершить тренировку", "callback_data": "mock_stop"}]]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}), timeout=10)


def handle_apply_hh_command(chat_id: int):
    """Trigger an on-demand HeadHunter autopilot cycle in background."""
    def worker():
        send_message(chat_id, "🚀 <b>Запущен внеочередной цикл откликов на HeadHunter...</b>\nПожалуйста, подождите 1–2 минуты.")
        try:
            from scripts.run_hh_autopilot_cycle import run_hh_autopilot_cycle
            res = run_hh_autopilot_cycle()
            submitted = res.get("submitted", 0)
            msg = (
                f"✅ <b>Цикл HH завершен!</b>\n\n"
                f"• Отправлено новых откликов: <b>{submitted}</b>\n"
                f"• Статус сессии: <b>{res.get('status')}</b>\n"
                f"• Время: {res.get('duration_seconds')} сек\n\n"
                f"<i>Все отклики снабжены умными сопроводительными письмами.</i>"
            )
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка цикла автопилота: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_tg_apply_command(chat_id: int):
    """Deliver targeted Telegram IT vacancies with 1-tap recruiter DM deeplinks."""
    def worker():
        send_message(chat_id, "💬 <b>Подбираю целевые Telegram-вакансии и заказы для быстрого отклика...</b>")
        try:
            from ai_assistant.telegram_auto_apply import dispatch_next_telegram_applications
            cnt = dispatch_next_telegram_applications(limit=3)
            if cnt == 0:
                send_message(chat_id, "ℹ️ Нет новых Telegram-вакансий в очереди. Нажмите <b>«🔄 Сканировать все источники»</b>!")
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка отправки: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_scan_command(chat_id: int):
    """Trigger full multi-source vacancy discovery across all channels."""
    def worker():
        send_message(chat_id, "🔄 <b>Запущен сбор со всех источников...</b>\n(Agoda Careers, Lalamove, LinkedIn Bangkok, Hirify, Habr, GeekJob, Telegram)")
        try:
            from scripts.run_multi_source_discovery import run_discovery
            res = run_discovery(notify=False)
            new_cnt = res.get("new_saved", 0)
            matched = res.get("target_matched", 0)
            send_message(
                chat_id,
                f"✅ <b>Сбор завершен!</b>\n\n"
                f"• Найдено целевых вакансий/заказов: <b>{matched}</b>\n"
                f"• Добавлено новых в базу: <b>{new_cnt}</b>\n"
                f"• Время выполнения: {res.get('duration_seconds')} сек\n\n"
                f"Нажмите кнопку <b>«💬 Отклики в Telegram»</b> или <b>«💼 Быстрые заказы»</b>!"
            )
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка сбора: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_chats_command(chat_id: int):
    """Scan and output HH negotiations status."""
    def worker():
        send_message(chat_id, "🔍 <b>Проверяю отклики и чаты на HeadHunter...</b>")
        try:
            from ai_assistant.hh_chat_monitor import process_negotiation_alerts, scan_hh_negotiations
            cards = scan_hh_negotiations()
            if not cards:
                send_message(chat_id, "ℹ️ Нет доступных карточек откликов или страница загружается.")
                return

            process_negotiation_alerts(cards)

            invites = [c for c in cards if c.get("status") in ("Приглашение", "Собеседование")]
            lines = []
            for c in cards[:6]:
                st = c.get("status", "Ожидание")
                r = c.get("lines", ["Вакансия"])[0]
                emp = c.get("lines", ["", "Работодатель"])[1] if len(c.get("lines", [])) > 1 else ""
                icon = "🎉" if st in ("Приглашение", "Собеседование") else ("❌" if st == "Отказ" else "⏳")
                lines.append(f"{icon} <b>{r}</b> ({emp})\n   Статус: <i>{st}</i>")

            inv_text = f"🎉 <b>Активных приглашений: {len(invites)}</b>\n\n" if invites else ""
            msg = (
                f"📋 <b>Последние отклики на HH:</b>\n\n"
                f"{inv_text}" + "\n\n".join(lines) +
                f"\n\n🔗 <a href=\"https://hh.ru/applicant/negotiations\">Открыть переписки на HH</a>\n"
                f"💡 <i>Перешлите сюда любое сообщение или вопрос рекрутера для мгновенной генерации ответа!</i>"
            )
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка проверки чатов: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_more_command(chat_id: int):
    """Dispatch next fresh vacancies with 1-click apply links."""
    def worker():
        try:
            from scripts.run_telegram_outreach import dispatch_outreach_batch
            dispatched = dispatch_outreach_batch(limit=3)
            if dispatched == 0:
                send_message(chat_id, "ℹ️ Нет свежих неразобранных вакансий. Запустите <b>«🔄 Сканировать все источники»</b>!")
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка отправки вакансий: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_kwork_command(chat_id: int, text: str):
    """Import a Kwork order link sent as ``/kwork <url>``.

    Phase 4C.2: inert unless ``KWORK_LINK_IMPORT_ENABLED`` is flipped with
    owner approval (bot restart required). Owner-only caller (router gates).
    """
    def worker():
        try:
            from ai_assistant.gig import kwork_import as ki

            if not ki.KWORK_LINK_IMPORT_ENABLED:
                send_message(chat_id, "ℹ️ Импорт ссылок Kwork пока отключён.")
                return
            links = ki.extract_kwork_links(text)
            if not links:
                send_message(chat_id, "ℹ️ Пришлите ссылку вида "
                                     "<code>https://kwork.ru/projects/1234567</code> "
                                     "командой <code>/kwork &lt;ссылка&gt;</code>.")
                return
            res = ki.import_links(links, persist=True)
            for item in res["imported"]:
                # Full card with actions for suitable active orders.
                from ai_assistant.gig.notify import build_card

                card = build_card({
                    "id": item["gig_id"], "title": item["title"],
                    "source": "kwork", "source_mode": "live",
                    "url": item["url"], "budget_text": item["budget"],
                    "opportunity_score": item["score"],
                    "rejection_reasons": []})
                card["chat_id"] = chat_id
                send_message(chat_id, card["text"],
                             reply_markup=card["reply_markup"])
            for item in res.get("closed", []):
                send_message(
                    chat_id,
                    f"🔴 <b>Приём предложений завершён.</b>\n"
                    f"«{item['title'][:80]}»\n"
                    f"Заказчик больше не принимает отклики, добавлять нечего.")
            lines = []
            for gid in res["duplicates"]:
                lines.append(f"ℹ️ Уже в базе: <code>{gid}</code>")
            for bad in res["refused"]:
                lines.append(f"⚠️ Отклонено: {bad['reason']}")
            if lines:
                send_message(chat_id, "\n\n".join(lines))
        except Exception as e:
            logger.warning(f"Kwork import failed: {e}")
            try:
                send_message(chat_id, "❌ Ошибка импорта Kwork.")
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()


def handle_gigs_command(chat_id: int):
    """Deliver fresh freelance and project gigs with 1-click apply proposals."""
    def worker():
        send_message(chat_id, "💼 <b>Подбираю актуальные быстрые заказы...</b>")
        db.init_db()
        conn = db.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT stable_id, source, title, company, description, salary_min, salary_max, salary_currency, job_url, raw_data
            FROM vacancies
            WHERE (source IN ('tg_freelance_orders', 'tg_zakazy_it') OR raw_data LIKE '%\"is_gig\": true%')
            ORDER BY first_seen_at DESC
            LIMIT 4
        """)
        rows = cursor.fetchall()

        if len(rows) < 3:
            try:
                from ai_assistant.adapters.telegram_channels import TelegramChannelsAdapter, GIG_CHANNELS
                from scripts.run_multi_source_discovery import save_vacancies_to_db
                adapter = TelegramChannelsAdapter(channels=GIG_CHANNELS)
                vacs = adapter.fetch_all()
                save_vacancies_to_db(vacs)
                cursor.execute("""
                    SELECT stable_id, source, title, company, description, salary_min, salary_max, salary_currency, job_url, raw_data
                    FROM vacancies
                    WHERE (source IN ('tg_freelance_orders', 'tg_zakazy_it') OR raw_data LIKE '%\"is_gig\": true%')
                    ORDER BY first_seen_at DESC
                    LIMIT 4
                """)
                rows = cursor.fetchall()
            except Exception as e:
                logger.warning(f"Gig live refresh failed: {e}")

        conn.close()

        if not rows:
            send_message(chat_id, "ℹ️ На данный момент нет новых быстрых заказов. Запустите <b>«🔄 Сканировать все источники»</b>!")
            return

        for row in rows[:3]:
            stable_id, source, title, company, description, sal_min, sal_max, sal_curr, job_url, raw_json = row
            raw = {}
            try:
                raw = json.loads(raw_json) if raw_json else {}
            except Exception:
                pass

            bud_parts = []
            if sal_min:
                bud_parts.append(f"от {sal_min:,}".replace(",", " "))
            if sal_max:
                bud_parts.append(f"до {sal_max:,}".replace(",", " "))
            bud_str = " ".join(bud_parts) + (f" {sal_curr}" if bud_parts else "")

            pitch = generate_gig_pitch(title=title, description=description, budget=bud_str)

            combined = f"{raw.get('contact', '')} {description}"
            tg_match = re.search(r"@([a-zA-Z0-9_]{4,})", combined)
            tg_user = tg_match.group(1) if tg_match else None

            buttons: list[list[dict[str, str]]] = []
            if tg_user and tg_user.lower() not in ("channel", "freelance_orders", "zakazy_it"):
                deeplink = build_gig_deeplink(tg_user, pitch)
                buttons.append([{"text": f"🚀 Откликнуться заказчику (@{tg_user})", "url": deeplink}])

            if job_url:
                buttons.append([{"text": "📄 Открыть пост с заказом", "url": job_url}])

            bud_line = f"💰 <b>Бюджет:</b> {bud_str}\n" if bud_str else "💰 <b>Бюджет:</b> Договорной\n"

            msg = (
                f"💼 <b>Быстрый заказ (Фриланс / Проект)</b>\n\n"
                f"📌 <b>Задача:</b> {title}\n"
                f"{bud_line}"
                f"🌐 <b>Источник:</b> {source.replace('tg_', '@')}\n\n"
                f"📝 <b>Готовое предложение заказчику:</b>\n"
                f"<blockquote>{pitch}</blockquote>\n\n"
                f"💡 <i>Нажмите кнопку ниже, чтобы перейти в диалог с предзаполненным предложением!</i>"
            )
            send_message(chat_id, msg, reply_markup={"inline_keyboard": buttons} if buttons else None)
            time.sleep(1.0)

    threading.Thread(target=worker, daemon=True).start()


def handle_cv_command(chat_id: int):
    """Generate and deliver polished Bangkok English CV PDF."""
    def worker():
        send_message(chat_id, "📄 <b>Формирую актуальный English CV (PDF) для Бангкока...</b>")
        try:
            from ai_assistant.bangkok_resume_builder import generate_bangkok_cv
            pdf_path = generate_bangkok_cv()
            caption = (
                "📄 <b>Mikhail Kolesnikov — English CV (PDF)</b>\n\n"
                "🎯 <b>Позиционирование:</b>\n"
                "• Senior Systems Administrator / DevOps & Infrastructure Engineer\n"
                "• 15+ лет enterprise-опыта (Linux, Windows Server, Docker, Networks, Python, AI)\n"
                "• Опыт работы в Таиланде (Sayama Travel, 2022–2026)\n"
                "• <b>Локация:</b> Бангкок, Таиланд (Уже на месте, 0 расходов на релокацию для работодателя, готов к очным собеседованиям)\n\n"
                "💡 <i>Используйте этот файл для прикрепления к откликам на LinkedIn и отправки рекрутерам!</i>"
            )
            ok = send_document(chat_id, pdf_path, caption=caption, filename="Mikhail_Kolesnikov_CV_Bangkok.pdf")
            if not ok:
                send_message(chat_id, "❌ Не удалось отправить PDF файл в Telegram.")
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка генерации CV: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_send_email_command(chat_id: int, text: str):
    """Handle /email or /send_email command."""
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        send_message(
            chat_id,
            "ℹ️ <b>Формат команды:</b>\n"
            "<code>/send_email hr@company.com Название роли</code>\n\n"
            "Пример: <code>/send_email hr@fintech.ru DevOps Инженер</code>\n\n"
            "Бот сформирует сопроводительное письмо, прикрепит актуальный PDF CV и отправит через SMTP или создаст готовый .eml пакет.",
        )
        return

    to_email = parts[1].strip()
    role = parts[2].strip() if len(parts) > 2 else "Senior Systems / DevOps Engineer"

    def worker():
        send_message(chat_id, f"✉️ <b>Подготавливаю письмо и прикрепляю PDF резюме для {to_email}...</b>")
        try:
            from ai_assistant.email_outreach import dispatch_email_outreach
            res = dispatch_email_outreach(
                to_email=to_email,
                role=role,
                company="Компания",
                is_bangkok=("bangkok" in role.lower() or "international" in role.lower()),
                attempt_smtp=True,
            )
            if res.get("smtp_sent"):
                send_message(
                    chat_id,
                    f"🎉 <b>Письмо успешно отправлено!</b>\n\n"
                    f"• <b>Кому:</b> {to_email}\n"
                    f"• <b>Тема:</b> {res['subject']}\n"
                    f"• <b>Вложение:</b> Mikhail_Kolesnikov_CV_Bangkok.pdf (прикреплено)\n"
                    f"• <b>Канал:</b> Прямой SMTP (Gmail)",
                )
            else:
                eml_path = res.get("eml_path")
                caption = (
                    f"✉️ <b>Пакет отклика для {to_email} готов!</b>\n\n"
                    f"• <b>Тема:</b> {res['subject']}\n"
                    f"• <b>Вложение:</b> Mikhail_Kolesnikov_CV_Bangkok.pdf\n\n"
                    f"💡 <i>{res.get('status_message')}</i>\n\n"
                    f"Файл .eml содержит готовое письмо с прикрепленным PDF. Откройте его на телефоне или ПК для отправки в 1 клик!"
                )
                if eml_path and pathlib.Path(eml_path).exists():
                    send_document(chat_id, eml_path, caption=caption, filename=f"Отклик_{to_email}.eml")
                else:
                    send_message(chat_id, caption)
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка отправки: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_inquiry_reply(chat_id: int, user_text: str):
    """Analyze incoming employer question/message and return immediate ready draft."""
    try:
        res = analyze_employer_inquiry(user_text)
        cat = res.get("category", "ОТВЕТ")
        reply_text = res.get("reply_text", "")
        lang = res.get("language", "RU")

        msg = (
            f"🤖 <b>Умный ассистент по собеседованиям & диалогам</b>\n\n"
            f"📌 <b>Интент:</b> <code>{cat}</code> ({lang})\n\n"
            f"📝 <b>Готовый ответ для отправки работодателю:</b>\n\n"
            f"<blockquote>{reply_text}</blockquote>\n\n"
            f"💡 <i>Нажмите на блок цитаты выше, чтобы скопировать готовый ответ в буфер!</i>"
        )
        send_message(chat_id, msg, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.warning(f"Inquiry generation error: {e}")
        send_message(chat_id, f"❌ Ошибка генерации ответа: {e}", reply_markup=MAIN_KEYBOARD)


def run_bot_polling():
    """Main long polling loop for @jobsassistbot."""
    # Phase 3: single-instance guard. A second concurrent poller would split
    # getUpdates streams and double-process callbacks, so exit(3) instead.
    # The scheduler (hourly trigger + IgnoreNew) restarts only dead bots.
    # Phase 4A: atomic first-wins file (O_EXCL create) closes the TOCTOU race
    # in check-then-write locking — two same-second starters (observed live)
    # both believed they owned the lock. Exactly one O_EXCL create succeeds.
    from ai_assistant.runner import SingleInstanceLock, is_pid_running

    win_path = os.path.join(BOT_LOCK_DIR, BOT_LOCK_NAME + ".win")
    os.makedirs(BOT_LOCK_DIR, exist_ok=True)

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
            # Phase 4C.4: the winner creates-then-writes in two steps, so a
            # freshly created file may still be EMPTY. Treat empty as
            # "winner not done yet": wait briefly instead of assuming stale.
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
        lock_dir=BOT_LOCK_DIR,
        lock_name=BOT_LOCK_NAME,
    )
    acquired, reason = lock.acquire()
    if not acquired:
        print(f"telegram_control_bot: another instance is running ({reason})", flush=True)
        logger.warning("Another control-bot instance holds the lock; exiting: %s", reason)
        sys.exit(3)
    logger.info("Starting Telegram Control Bot polling loop...")
    _write_heartbeat()
    import atexit

    # Released on clean exit; on kill -9 the next start recovers via
    # dead-PID detection in SingleInstanceLock.acquire() and the .win guard.
    atexit.register(lock.release)
    atexit.register(_release_win)
    offset = 0

    send_message(
        ALLOWED_CHAT_ID,
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
        _write_heartbeat()
        try:
            # Phase 4C.5: explicit update-type filter. The account carries a
            # legacy server-side allow-list of ["callback_query"] only, which
            # silently drops every text message (commands like /start and
            # /kwork never reach the poller). Per-request allowed_updates
            # overrides it; keep the surface minimal (message+callback only).
            _allowed = urllib.parse.quote(json.dumps(["message", "callback_query"]))
            url = (f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
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
                    cq_id = str(cq.get("id"))
                    cq_data = cq.get("data", "")
                    cq_from = (cq.get("from") or {}).get("id")

                    if cq_from == ALLOWED_CHAT_ID:
                        if cq_data.startswith("app:"):
                            suffix = cq_data.split(":", 1)[1]
                            from ai_assistant.telegram_auto_apply import mark_vacancy_applied
                            mark_vacancy_applied(suffix)
                            answer_callback_query(cq_id, text="✅ Отклик сохранен как отправленный!")
                        elif cq_data.startswith("skp:"):
                            suffix = cq_data.split(":", 1)[1]
                            from ai_assistant.telegram_auto_apply import mark_vacancy_skipped
                            mark_vacancy_skipped(suffix)
                            answer_callback_query(cq_id, text="⏭ Вакансия пропущена.")
                        elif cq_data.startswith("flw:"):
                            suffix = cq_data.split(":", 1)[1]
                            record_followup_sent(suffix)
                            answer_callback_query(cq_id, text="✅ Фоллоу-ап зафиксирован!")
                        elif cq_data.startswith("flwmute:"):
                            suffix = cq_data.split(":", 1)[1]
                            mute_followup(suffix)
                            answer_callback_query(cq_id, text="🔕 Напоминания отключены.")
                        elif cq_data.startswith("mock_tr:"):
                            tr_key = cq_data.split(":", 1)[1]
                            start_track_session(ALLOWED_CHAT_ID, tr_key)
                            answer_callback_query(cq_id, text="🎯 Тренировка запущена!")
                        elif cq_data.startswith("gig:"):
                            # Phase 4C.1: gig callbacks (gig:INT/SKP:<gig_id>).
                            # Never touches vacancies; idempotent; refuses unknown.
                            # Phase 4C.4: explicit follow-up message (a toast alone
                            # proved too easy to miss) + callback_query_id dedup.
                            try:
                                from ai_assistant.gig.notify import handle_gig_callback
                                from ai_assistant.gig.store import get_gig, set_gig_status

                                def _gig_row(gig_id: str):
                                    gig = get_gig(gig_id)
                                    if gig is None:
                                        return None
                                    return {"id": gig.id, "title": gig.title,
                                            "status": gig.status.value}

                                if "_gig_acked" not in globals():
                                    globals()["_gig_acked"] = set()
                                res = handle_gig_callback(
                                    cq_data, get_gig_fn=_gig_row,
                                    set_status_fn=lambda gid, st, note: set_gig_status(gid, st, note=note),
                                    callback_query_id=cq_id,
                                    acked_ids=globals()["_gig_acked"],
                                )
                                outcome = res.get("outcome")
                                # Phase 4C.5: shared ack policy — toast always,
                                # follow-up message only when informative
                                # (applied/refused), never on repeats.
                                from ai_assistant.gig.notify import followup_text

                                if outcome == "applied":
                                    answer_callback_query(
                                        cq_id,
                                        text="✅ Сохранено!" if res.get("status") == "SHORTLISTED"
                                        else "⏭ Пропущено.")
                                elif outcome == "already-applied":
                                    answer_callback_query(cq_id, text="Уже отмечено.")
                                else:
                                    answer_callback_query(cq_id, text="Не удалось обработать.")
                                followup = followup_text(
                                    outcome or "",
                                    str(res.get("title") or res.get("gig_id") or ""),
                                    res.get("status"))
                                if followup:
                                    send_message(ALLOWED_CHAT_ID, followup)
                            except Exception as e:
                                logger.warning(f"Gig callback failed: {e}")
                                answer_callback_query(cq_id)
                        elif cq_data == "mock_stop":
                            stop_mock_interview(ALLOWED_CHAT_ID)
                            send_message(ALLOWED_CHAT_ID, "🛑 <b>Тренировка завершена.</b> Вы можете начать заново в любое время через меню <b>«🎤 Тренажер интервью»</b>!")
                            answer_callback_query(cq_id, text="Тренировка завершена")
                        else:
                            answer_callback_query(cq_id)
                    continue

                # 2. Handle Text Messages
                msg = update.get("message") or {}
                from_user = msg.get("from") or {}
                user_id = from_user.get("id")

                if user_id != ALLOWED_CHAT_ID:
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                logger.info(f"Received user command/message: '{text[:80]}...'")

                # If user is in an active Mock Interview Session, grade their response!
                active_mock = get_current_session(ALLOWED_CHAT_ID)
                if active_mock and not text.startswith("/") and text not in (
                    "📊 Статус", "📈 Воронка", "🚀 Отклики HH", "🚀 Поднять резюме HH",
                    "🎤 Тренажер интервью", "💬 Отклики в Telegram", "🔄 Фоллоу-ап",
                    "💼 Быстрые заказы", "📨 Вакансии в 1 клик", "📎 Скачать English CV",
                    "💬 Диалоги HH", "🔄 Сканировать все источники"
                ):
                    res = process_user_answer(ALLOWED_CHAT_ID, text)
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
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        payload = {
                            "chat_id": ALLOWED_CHAT_ID,
                            "text": eval_card + next_q_msg,
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": buttons},
                        }
                        urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}), timeout=10)
                    else:
                        concl_msg = "\n\n🎉 <b>Поздравляем! Трек полностью пройден.</b> Вы отлично справились со всеми ключевыми вопросами!"
                        send_message(ALLOWED_CHAT_ID, eval_card + concl_msg, reply_markup=MAIN_KEYBOARD)
                    continue

                if text in ("📊 Статус", "/status"):
                    handle_status_command(ALLOWED_CHAT_ID)
                elif text in ("📈 Воронка", "/funnel", "/analytics", "воронка"):
                    handle_funnel_command(ALLOWED_CHAT_ID)
                elif text in ("🚀 Поднять резюме HH", "/bump_resume", "поднять резюме"):
                    handle_bump_resume_command(ALLOWED_CHAT_ID)
                elif text in ("🎤 Тренажер интервью", "/interview", "/mock", "интервью", "тренажер"):
                    handle_mock_interview_menu(ALLOWED_CHAT_ID)
                elif text in ("🔄 Фоллоу-ап", "/followup", "фоллоу-ап", "фоллоуап"):
                    handle_followup_command(ALLOWED_CHAT_ID)
                elif text in ("🚀 Отклики HH", "/apply_hh"):
                    handle_apply_hh_command(ALLOWED_CHAT_ID)
                elif text in ("💬 Отклики в Telegram", "/apply_tg", "отклики тг", "тг"):
                    handle_tg_apply_command(ALLOWED_CHAT_ID)
                elif text in ("🔄 Сканировать все источники", "/scan"):
                    handle_scan_command(ALLOWED_CHAT_ID)
                elif text in ("💬 Диалоги HH", "/chats"):
                    handle_chats_command(ALLOWED_CHAT_ID)
                elif text in ("📨 Вакансии в 1 клик", "/more"):
                    handle_more_command(ALLOWED_CHAT_ID)
                elif text in ("💼 Быстрые заказы", "/gigs", "заказы", "фриланс"):
                    handle_gigs_command(ALLOWED_CHAT_ID)
                elif text.startswith("/kwork"):
                    handle_kwork_command(ALLOWED_CHAT_ID, text)
                elif text in ("📎 Скачать English CV", "/cv", "резюме", "cv"):
                    handle_cv_command(ALLOWED_CHAT_ID)
                elif text.startswith("/email") or text.startswith("/send_email"):
                    handle_send_email_command(ALLOWED_CHAT_ID, text)
                elif text in ("/start", "/help"):
                    send_message(
                        ALLOWED_CHAT_ID,
                        "🤖 <b>JobSearch Assistant Bot</b>\n\n"
                        "Команды управления:\n"
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
                    handle_inquiry_reply(ALLOWED_CHAT_ID, text)

        except urllib.error.URLError as e:
            logger.debug(f"Polling network glitch: {e}")
            time.sleep(2)
        except Exception as e:
            logger.warning(f"Polling loop exception: {e}")
            time.sleep(3)


if __name__ == "__main__":
    run_bot_polling()
