"""Text command handlers for Telegram control bot."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import threading
import time
import urllib.request
from typing import Any, Callable

from ai_assistant import config, db
from ai_assistant.followup_manager import dispatch_followup_batch
from ai_assistant.gig_pitch_generator import build_gig_deeplink, generate_gig_pitch
from ai_assistant.hh_resume_bumper import bump_hh_resumes
from ai_assistant.interview_auto_responder import analyze_employer_inquiry
from ai_assistant.mock_interviewer import (
    start_mock_interview,
)
from ai_assistant.telegram.cards import build_autopilot_card, build_status_card, get_system_stats
from ai_assistant.telegram.transport import (
    ALLOWED_CHAT_ID,
    MAIN_KEYBOARD,
    PROJECT_ROOT,
    TELEGRAM_BOT_TOKEN,
    send_document as default_send_document,
    send_message as default_send_message,
)

logger = logging.getLogger("telegram_control_bot")


def handle_autopilot_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
    main_keyboard: dict[str, Any] = MAIN_KEYBOARD,
) -> None:
    """Output live Paseo Autopilot control panel report with interactive action buttons."""
    msg, buttons = build_autopilot_card()
    reply_markup = {"inline_keyboard": buttons} if buttons else main_keyboard
    send_message_fn(chat_id, msg, reply_markup=reply_markup)


def handle_status_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
    main_keyboard: dict[str, Any] = MAIN_KEYBOARD,
) -> None:
    """Output comprehensive system status report."""
    stats = get_system_stats()
    text = build_status_card(stats)
    send_message_fn(chat_id, text, reply_markup=main_keyboard)


def handle_funnel_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
    main_keyboard: dict[str, Any] = MAIN_KEYBOARD,
) -> None:
    """Output full recruitment & business conversion funnel report."""
    try:
        from ai_assistant.business_funnel import format_business_funnel_report
        report = format_business_funnel_report()
        send_message_fn(chat_id, report, reply_markup=main_keyboard)
    except Exception as e:
        send_message_fn(chat_id, f"❌ Ошибка генерации воронки: {e}")


def handle_followup_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Dispatch automated follow-up reminders for older applications."""
    def worker():
        send_message_fn(chat_id, "🔄 <b>Проверяю отправленные отклики и формирую фоллоу-апы...</b>")
        try:
            cnt = dispatch_followup_batch(limit=2)
            if cnt == 0:
                send_message_fn(chat_id, "ℹ️ На данный момент нет откликов, требующих повторного фоллоу-апа.")
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка проверки фоллоу-апов: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_bump_resume_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
    main_keyboard: dict[str, Any] = MAIN_KEYBOARD,
) -> None:
    """Trigger HH resume auto-bumping to put candidate at top of search."""
    def worker():
        send_message_fn(chat_id, "🚀 <b>Проверяю и поднимаю резюме на HeadHunter...</b>")
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
            send_message_fn(chat_id, msg, reply_markup=main_keyboard)
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка автоподнятия резюме: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_mock_interview_menu(
    chat_id: int,
    token: str = TELEGRAM_BOT_TOKEN,
) -> None:
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
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    urllib.request.urlopen(
        urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}),
        timeout=10,
    )


def start_track_session(
    chat_id: int,
    track_key: str,
    token: str = TELEGRAM_BOT_TOKEN,
) -> None:
    """Start specific mock track."""
    res = start_mock_interview(chat_id, track_key)
    msg = (
        f"🎯 <b>Начало тренировки: {res['track_title']}</b>\n\n"
        f"<b>Вопрос 1 из {res['total_questions']}:</b>\n\n"
        f"❓ <i>{res['question_text']}</i>\n\n"
        f"💡 <b>Как отвечать:</b> Напишите ваш ответ в чат (или надиктуйте голосом). Опишите реальный опыт и шаги решения!"
    )
    buttons = [[{"text": "🛑 Завершить тренировку", "callback_data": "mock_stop"}]]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    urllib.request.urlopen(
        urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}),
        timeout=10,
    )


def handle_apply_hh_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Trigger an on-demand HeadHunter autopilot cycle in background."""
    def worker():
        send_message_fn(chat_id, "🚀 <b>Запущен внеочередной цикл откликов на HeadHunter...</b>\nПожалуйста, подождите 1–2 минуты.")
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
            send_message_fn(chat_id, msg)
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка цикла автопилота: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_tg_apply_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Deliver targeted Telegram IT vacancies with 1-tap recruiter DM deeplinks."""
    def worker():
        send_message_fn(chat_id, "💬 <b>Подбираю целевые Telegram-вакансии и заказы для быстрого отклика...</b>")
        try:
            from ai_assistant.telegram_auto_apply import dispatch_next_telegram_applications
            cnt = dispatch_next_telegram_applications(limit=3)
            if cnt == 0:
                send_message_fn(chat_id, "ℹ️ Нет новых Telegram-вакансий в очереди. Нажмите <b>«🔄 Сканировать все источники»</b>!")
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка отправки: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_scan_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Trigger full multi-source vacancy discovery across all channels."""
    def worker():
        send_message_fn(chat_id, "🔄 <b>Запущен сбор со всех источников...</b>\n(Agoda Careers, Lalamove, LinkedIn Bangkok, Hirify, Habr, GeekJob, Telegram)")
        try:
            from scripts.run_multi_source_discovery import run_discovery
            res = run_discovery(notify=False)
            new_cnt = res.get("new_saved", 0)
            matched = res.get("target_matched", 0)
            send_message_fn(
                chat_id,
                f"✅ <b>Сбор завершен!</b>\n\n"
                f"• Найдено целевых вакансий/заказов: <b>{matched}</b>\n"
                f"• Добавлено новых в базу: <b>{new_cnt}</b>\n"
                f"• Время выполнения: {res.get('duration_seconds')} сек\n\n"
                f"Нажмите кнопку <b>«💬 Отклики в Telegram»</b> или <b>«💼 Быстрые заказы»</b>!",
            )
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка сбора: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_chats_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Scan and output HH negotiations status."""
    def worker():
        send_message_fn(chat_id, "🔍 <b>Проверяю отклики и чаты на HeadHunter...</b>")
        try:
            from ai_assistant.hh_chat_monitor import process_negotiation_alerts, scan_hh_negotiations
            cards = scan_hh_negotiations()
            if not cards:
                send_message_fn(chat_id, "ℹ️ Нет доступных карточек откликов или страница загружается.")
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
                "\n\n🔗 <a href=\"https://hh.ru/applicant/negotiations\">Открыть переписки на HH</a>\n"
                "💡 <i>Перешлите сюда любое сообщение или вопрос рекрутера для мгновенной генерации ответа!</i>"
            )
            send_message_fn(chat_id, msg)
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка проверки чатов: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_more_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Dispatch next fresh vacancies with 1-click apply links."""
    def worker():
        try:
            from scripts.run_telegram_outreach import dispatch_outreach_batch
            dispatched = dispatch_outreach_batch(limit=3)
            if dispatched == 0:
                send_message_fn(chat_id, "ℹ️ Нет свежих неразобранных вакансий. Запустите <b>«🔄 Сканировать все источники»</b>!")
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка отправки вакансий: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_kwork_command(
    chat_id: int,
    text: str,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Import a Kwork order link sent as ``/kwork <url>``."""
    def worker():
        try:
            from ai_assistant.gig import kwork_import as ki

            if not ki.KWORK_LINK_IMPORT_ENABLED:
                send_message_fn(chat_id, "ℹ️ Импорт ссылок Kwork пока отключён.")
                return
            links = ki.extract_kwork_links(text)
            if not links:
                send_message_fn(
                    chat_id,
                    "ℹ️ Пришлите ссылку вида "
                    "<code>https://kwork.ru/projects/1234567</code> "
                    "командой <code>/kwork &lt;ссылка&gt;</code>.",
                )
                return
            res = ki.import_links(links, persist=True)
            for item in res["imported"]:
                from ai_assistant.gig.notify import build_card

                card = build_card({
                    "id": item["gig_id"], "title": item["title"],
                    "source": "kwork", "source_mode": "live",
                    "url": item["url"], "budget_text": item["budget"],
                    "opportunity_score": item["score"],
                    "rejection_reasons": [],
                })
                card["chat_id"] = chat_id
                send_message_fn(chat_id, card["text"], reply_markup=card["reply_markup"])
            for item in res.get("closed", []):
                send_message_fn(
                    chat_id,
                    f"🔴 <b>Приём предложений завершён.</b>\n"
                    f"«{item['title'][:80]}»\n"
                    f"Заказчик больше не принимает отклики, добавлять нечего.",
                )
            lines = []
            for gid in res["duplicates"]:
                lines.append(f"ℹ️ Уже в базе: <code>{gid}</code>")
            for bad in res["refused"]:
                lines.append(f"⚠️ Отклонено: {bad['reason']}")
            if lines:
                send_message_fn(chat_id, "\n\n".join(lines))
        except Exception as e:
            logger.warning(f"Kwork import failed: {e}")
            try:
                send_message_fn(chat_id, "❌ Ошибка импорта Kwork.")
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()


def handle_gigs_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
) -> None:
    """Deliver fresh freelance and project gigs with interactive action buttons."""
    def worker():
        send_message_fn(chat_id, "💼 <b>Подбираю актуальные быстрые заказы...</b>")
        db.init_db()

        # Check top_gig_proposals first
        try:
            with db.db_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT gig_id, source, title, budget_text, contact, deeplink, url, proposal_pitch, status
                    FROM top_gig_proposals
                    WHERE status != 'SKIPPED'
                    ORDER BY id ASC
                    LIMIT 4
                """)
                top_rows = cursor.fetchall()
            if top_rows:
                for r in top_rows:
                    gid, src, title, bud_text, contact, deeplink, url, pitch, status = r
                    buttons: list[list[dict[str, str]]] = []

                    # Row 1: Direct link
                    if deeplink and "t.me/" in deeplink and contact and contact != "Платформа":
                        buttons.append([{"text": f"🚀 Открыть диалог в TG ({contact})", "url": deeplink}])
                    elif url:
                        buttons.append([{"text": "📄 Открыть страницу заказа", "url": url}])

                    # Row 2: Confirmed outbound
                    buttons.append([{"text": "✅ Отправлено", "callback_data": f"gig:SENT:{gid}"}])

                    # Row 3: Reply received and skip
                    buttons.append([
                        {"text": "💬 Ответ получен", "callback_data": f"gig:REPLY:{gid}"},
                        {"text": "❌ Пропустить", "callback_data": f"gig:SKP:{gid}"},
                    ])

                    bud_line = f"💰 <b>Бюджет:</b> {bud_text}\n" if bud_text else "💰 <b>Бюджет:</b> Договорной\n"
                    contact_line = f"👤 <b>Контакт:</b> {contact}\n" if contact and contact != "Платформа" else ""

                    msg = (
                        f"💼 <b>Быстрый заказ (Фриланс / Проект)</b>\n\n"
                        f"📌 <b>Задача:</b> {title}\n"
                        f"{bud_line}"
                        f"🌐 <b>Источник:</b> {src}\n"
                        f"{contact_line}\n"
                        f"📝 <b>Готовое предложение заказчику:</b>\n"
                        f"<blockquote>{pitch}</blockquote>\n\n"
                        f"💡 <i>Переход в диалог не является отправкой. После отправки нажмите «✅ Отправлено».</i>"
                    )
                    send_message_fn(chat_id, msg, reply_markup={"inline_keyboard": buttons})
                    time.sleep(1.0)
                return
        except Exception as exc:
            logger.warning(f"Error querying top_gig_proposals: {exc}")

        with db.db_cursor(commit=False) as cursor:
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
                from ai_assistant.adapters.telegram_channels import GIG_CHANNELS, TelegramChannelsAdapter
                from scripts.run_multi_source_discovery import save_vacancies_to_db
                adapter = TelegramChannelsAdapter(channels=GIG_CHANNELS)
                vacs = adapter.fetch_all()
                save_vacancies_to_db(vacs)
                with db.db_cursor(commit=False) as cursor:
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

        if not rows:
            send_message_fn(chat_id, "ℹ️ На данный момент нет новых быстрых заказов. Запустите <b>«🔄 Сканировать все источники»</b>!")
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
            send_message_fn(chat_id, msg, reply_markup={"inline_keyboard": buttons} if buttons else None)
            time.sleep(1.0)

    threading.Thread(target=worker, daemon=True).start()


def handle_cv_command(
    chat_id: int,
    send_message_fn: Callable[..., bool] = default_send_message,
    send_document_fn: Callable[..., bool] = default_send_document,
) -> None:
    """Generate and deliver polished Bangkok English CV PDF."""
    def worker():
        send_message_fn(chat_id, "📄 <b>Формирую актуальный English CV (PDF) для Бангкока...</b>")
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
            ok = send_document_fn(chat_id, pdf_path, caption=caption, filename="Mikhail_Kolesnikov_CV_Bangkok.pdf")
            if not ok:
                send_message_fn(chat_id, "❌ Не удалось отправить PDF файл в Telegram.")
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка генерации CV: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_send_email_command(
    chat_id: int,
    text: str,
    send_message_fn: Callable[..., bool] = default_send_message,
    send_document_fn: Callable[..., bool] = default_send_document,
) -> None:
    """Handle /email or /send_email command."""
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        send_message_fn(
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
        send_message_fn(chat_id, f"✉️ <b>Подготавливаю письмо и прикрепляю PDF резюме для {to_email}...</b>")
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
                send_message_fn(
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
                    send_document_fn(chat_id, eml_path, caption=caption, filename=f"Отклик_{to_email}.eml")
                else:
                    send_message_fn(chat_id, caption)
        except Exception as e:
            send_message_fn(chat_id, f"❌ Ошибка отправки: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_inquiry_reply(
    chat_id: int,
    user_text: str,
    send_message_fn: Callable[..., bool] = default_send_message,
    main_keyboard: dict[str, Any] = MAIN_KEYBOARD,
) -> None:
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
        send_message_fn(chat_id, msg, reply_markup=main_keyboard)
    except Exception as e:
        logger.warning(f"Inquiry generation error: {e}")
        send_message_fn(chat_id, f"❌ Ошибка генерации ответа: {e}", reply_markup=main_keyboard)
