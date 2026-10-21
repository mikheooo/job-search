#!/usr/bin/env python
"""Autonomous Freelance & Gig Autopilot Cycle Runner (Phase 4C).

Gathers fresh gigs from Freelancer.com, Kwork, and public Telegram freelance channels,
passes them through GigPipeline (normalize -> dedup -> gates -> assess -> score -> first_money),
picks top 3-4 actionable opportunities, and delivers 1-tap actionable cards to Mikhail's
personal Telegram bot (TELEGRAM_OWNER_ID).

Ensures:
- No spam (max 3-4 cards per cycle)
- Idempotency via telegram_delivery_records (no duplicate deliveries)
- Mutual exclusion via logs/gig_autopilot.lock
- Confirmed facts from candidate_kb.json in proposals
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import logging
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config
from ai_assistant.db import get_connection, init_db
from ai_assistant.gig.capability import assess_capability
from ai_assistant.gig.client import assess_client_risk
from ai_assistant.gig.first_money import first_money_score_of
from ai_assistant.gig.grounded import analyze
from ai_assistant.gig.models import CanDeliver, Gig
from ai_assistant.gig.notify import stable_key
from ai_assistant.gig.pipeline import GigPipeline
from ai_assistant.gig.sources import FreelancerGigSource, KworkGigSource, TelegramGigSource
from ai_assistant.gig.store import save_gig
from ai_assistant.gig_pitch_generator import build_gig_deeplink, generate_gig_pitch

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "gig_autopilot.log"
LOCK_FILE = LOG_DIR / "gig_autopilot.lock"


class FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


log_handlers: list[logging.Handler] = [
    FlushingFileHandler(LOG_FILE, encoding="utf-8"),
]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("gig_autopilot_runner")

try:
    from ai_assistant.runner import is_pid_running as _is_pid_running
except Exception:
    _is_pid_running = None


def acquire_lock() -> bool:
    """Acquire single-instance execution lock."""
    if LOCK_FILE.exists():
        try:
            lines = LOCK_FILE.read_text(encoding="utf-8").splitlines()
            locked_pid = int(lines[0].strip()) if lines else 0
        except Exception:
            locked_pid = 0
        try:
            mtime = LOCK_FILE.stat().st_mtime
        except Exception:
            mtime = 0.0

        owner_alive = bool(locked_pid) and _is_pid_running is not None and _is_pid_running(locked_pid)
        if owner_alive:
            return False

        try:
            if mtime and time.time() - mtime <= 1200 and locked_pid and _is_pid_running is None:
                return False
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            return False

    try:
        LOCK_FILE.write_text(f"{os.getpid()}\n{datetime.datetime.now().isoformat()}", encoding="utf-8")
        return True
    except Exception:
        return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def is_gig_already_delivered(conn: Any, gig_id: str) -> bool:
    skey = stable_key(gig_id)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT status FROM telegram_delivery_records WHERE delivery_key = ? AND status = 'DELIVERED'",
        (skey,),
    ).fetchone()
    return bool(row)


def record_gig_delivery(conn: Any, gig: Gig, chat_id: int | str) -> None:
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    skey = stable_key(gig.id)
    payload = {
        "gig_id": gig.id,
        "source": gig.source,
        "title": gig.title,
        "url": gig.url,
        "opportunity_score": gig.opportunity_score,
        "first_money_score": (gig.source_facts or {}).get("first_money_score"),
    }
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO telegram_delivery_records (delivery_key, notification_type, chat_id, delivered_at, status, payload)
        VALUES (?, 'gig_digest', ?, ?, 'DELIVERED', ?)
        ON CONFLICT(delivery_key) DO UPDATE SET status = 'DELIVERED', delivered_at = excluded.delivered_at, payload = excluded.payload
        """,
        (skey, str(chat_id), now_iso, json.dumps(payload, ensure_ascii=False)),
    )
    cur.execute(
        "UPDATE gigs SET status = 'SHORTLISTED', updated_at = ? WHERE id = ? AND status = 'NEW'",
        (now_iso, gig.id),
    )
    conn.commit()


def send_telegram_card(
    chat_id: int | str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    bot_token: str | None = None,
) -> bool:
    token = bot_token or config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
    if not token or not chat_id:
        logger.warning("Telegram bot token or chat ID is missing; skipping send")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning(f"Failed to send Telegram card: {exc}")
        return False


def format_gig_card(gig: Gig, pitch: str, fm_score: float | None = None) -> tuple[str, dict[str, Any] | None]:
    source_lower = (gig.source or "").lower()
    is_tg = source_lower.startswith("tg_") or bool((gig.source_facts or {}).get("is_telegram"))
    if is_tg:
        ch = (gig.source_facts or {}).get("channel") or source_lower.replace("tg_", "")
        platform_name = f"Telegram (@{ch})"
    elif "kwork" in source_lower:
        platform_name = "Биржа Kwork"
    elif "freelancer" in source_lower:
        platform_name = "Freelancer.com"
    else:
        platform_name = gig.source

    bud_str = gig.budget_text or "Договорной"
    effort_str = f"~{gig.estimated_effort_hours:.0f} ч" if gig.estimated_effort_hours else "по оценке"
    score_val = max(fm_score or 0.0, gig.opportunity_score or 0.0)
    score_str = f"{score_val:.0f}"

    clean_pitch = pitch.strip()
    escaped_pitch = html.escape(clean_pitch)

    key_skills = ", ".join(gig.skills[:5]) if gig.skills else "Python / IT / Automation"

    text = (
        f"💼 <b>Быстрый заказ — {platform_name}</b>\n\n"
        f"📌 <b>Задача:</b> {html.escape(gig.title)}\n"
        f"💰 <b>Бюджет:</b> {html.escape(bud_str)}\n"
        f"⏱ <b>Оценка:</b> {effort_str} | ⭐ <b>Score:</b> {score_str}/100\n"
        f"🏷 <b>Стек:</b> <code>{html.escape(key_skills)}</code>\n\n"
        f"📝 <b>Готовое предложение (нажмите, чтобы скопировать):</b>\n"
        f"<code>{escaped_pitch}</code>"
    )

    buttons: list[list[dict[str, str]]] = []
    if is_tg:
        combined = f"{(gig.source_facts or {}).get('contact', '')} {gig.description}"
        usernames = re.findall(r"@([a-zA-Z0-9_]{4,})", combined)
        target_user = None
        for u in usernames:
            if u.lower() not in ("channel", "freelance_orders", "zakazy_it", "tgme", "leadradar", "gotoisland", "admin"):
                target_user = u
                break
        if target_user:
            deeplink = build_gig_deeplink(target_user, clean_pitch)
            buttons.append([{"text": f"🚀 Откликнуться в 1 клик (@{target_user})", "url": deeplink}])
        if gig.url:
            buttons.append([{"text": "📄 Открыть пост с заказом", "url": gig.url}])
    else:
        buttons.append([
            {"text": "👍 В работу", "callback_data": f"gig:INT:{gig.id}"},
            {"text": "⏭ Пропустить", "callback_data": f"gig:SKP:{gig.id}"},
        ])
        if gig.url:
            buttons.append([{"text": "🔗 Открыть заказ на бирже", "url": gig.url}])

    reply_markup = {"inline_keyboard": buttons} if buttons else None
    return text, reply_markup


def deliver_gigs_with_policy(
    conn: Any,
    candidates: list[tuple[float, Gig, str]],
    owner_chat_id: int | str,
    *,
    bot_token: str | None = None,
    send_card_fn: Any = None,
) -> tuple[int, list[Any]]:
    """Phase 2: deliver gigs through the shared notification policy.

    Only high-relevance gigs are pushed now (rate-limited to a per-hour budget);
    everything else is left in the DB for the daily digest — never dropped.
    Uses the unified ``gigact:`` action card. Returns (delivered, results).
    ``send_card_fn`` is injectable for tests (defaults to ``send_telegram_card``).
    """
    from ai_assistant.gig.actions import build_action_card
    from ai_assistant.gig.order_language import resolve_order_language
    from ai_assistant.notification_gateway import (
        ItemState,
        NotificationGateway,
        Outbound,
        sqlite_gateway_store,
    )

    sender = send_card_fn or send_telegram_card
    delivered = 0
    results: list[Any] = []

    def _send(outbound: Any) -> None:
        card = outbound.payload["card"]
        ok = sender(owner_chat_id, card["text"], card.get("reply_markup"), bot_token)
        if not ok:
            raise RuntimeError("telegram send failed")

    gateway = NotificationGateway(
        sqlite_gateway_store(conn),
        send_fn=_send,
        send_now_hourly_limit=getattr(config, "TELEGRAM_SEND_NOW_HOURLY_LIMIT", 6),
        high_relevance_floor=getattr(config, "TELEGRAM_HIGH_RELEVANCE_FLOOR", 75.0),
        digest_floor=getattr(config, "TELEGRAM_DIGEST_FLOOR", 55.0),
    )

    for score, gig, pitch in candidates:
        language = resolve_order_language(gig.title, gig.description, gig.source)
        card = build_action_card(
            {"id": gig.id, "title": gig.title, "url": gig.url,
             "budget_text": gig.budget_text},
            pitch,
            language=language,
        )
        outbound = Outbound("gig", gig.id, owner_chat_id, score=score,
                            payload={"card": card, "gig_id": gig.id})
        state = ItemState(already_delivered=is_gig_already_delivered(conn, gig.id))
        result = gateway.dispatch(outbound, state)
        results.append(result)
        if result.action == "sent":
            record_gig_delivery(conn, gig, chat_id=owner_chat_id)
            delivered += 1
    return delivered, results


def run_gig_cycle(
    max_deliveries: int = 3,
    min_score: float = 48.0,
    kwork_limit: int = 12,
    freelancer_limit: int = 30,
    telegram_limit: int = 25,
    enable_freelancer: bool = False,
    enable_contra: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute one autonomous gig discovery & delivery cycle."""
    init_db()
    conn = get_connection()
    logger.info("--- Starting Gig Autopilot Cycle ---")

    # Build sources
    sources: list[Any] = []

    # 1. Telegram gig channels
    try:
        tg_src = TelegramGigSource(include_db_recent=True)
        sources.append(tg_src)
        logger.info("Initialized TelegramGigSource")
    except Exception as exc:
        logger.warning("Failed to initialize TelegramGigSource: %s", exc)

    # 2. Kwork IT projects
    try:
        kw_src = KworkGigSource(auto_discover=True, categories=[11, 41, 38, 80, 113])
        sources.append(kw_src)
        logger.info("Initialized KworkGigSource(auto_discover=True)")
    except Exception as exc:
        logger.warning("Failed to initialize KworkGigSource: %s", exc)

    # 3. Freelancer.com public feed (disabled by default: payout blocked for RF citizens)
    env_enable_fl = os.environ.get("GIG_ENABLE_FREELANCER", "").strip().lower() in ("1", "true", "yes")
    if enable_freelancer or env_enable_fl:
        try:
            fl_policy = {
                "automated_access_allowed": True,
                "express_written_permission_reference": "public listing read-only policy",
                "website_scraping_allowed": True,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
            fl_src = FreelancerGigSource(
                target_skills=["python", "web-scraping", "automation", "telegram", "n8n", "excel", "api"],
                include_general_listing=False,
                listing_pages=0,
                skill_pages=1,
                delay_seconds=0.5,
                max_requests=max(freelancer_limit * 2, 60),
                timeout_seconds=8,
                access_policy=fl_policy,
            )
            sources.append(fl_src)
            logger.info("Initialized FreelancerGigSource")
        except Exception as exc:
            logger.warning("Failed to initialize FreelancerGigSource: %s", exc)
    else:
        logger.info("FreelancerGigSource is disabled by default (RF payouts blocked)")

    # 4. Contra (disabled by default: payout blocked for RF citizens)
    env_enable_contra = os.environ.get("GIG_ENABLE_CONTRA", "").strip().lower() in ("1", "true", "yes")
    if enable_contra or env_enable_contra:
        try:
            from ai_assistant.gig.sources import ContraGigSource
            sources.append(ContraGigSource())
            logger.info("Initialized ContraGigSource")
        except Exception as exc:
            logger.warning("Failed to initialize ContraGigSource: %s", exc)
    else:
        logger.info("ContraGigSource is disabled by default (RF payouts blocked)")

    # Ingestion & scoring via GigPipeline
    pipeline = GigPipeline(persist=True)
    all_raw: list[dict[str, Any]] = []

    for src in sources:
        src_key = getattr(src, "key", "")
        limit = telegram_limit if src_key == "telegram" else (
            kwork_limit if src_key == "kwork" else freelancer_limit
        )
        try:
            logger.info("Fetching from source: %s (limit=%d)", getattr(src, "key", type(src).__name__), limit)
            fetched = list(src.fetch(limit=limit))
            logger.info("Fetched %d raw gigs from %s", len(fetched), getattr(src, "key", type(src).__name__))
            all_raw.extend(fetched)
        except Exception as exc:
            logger.warning("Fetch error from %s: %s", getattr(src, "key", type(src).__name__), exc)

    logger.info("Total raw gigs collected across sources: %d", len(all_raw))

    # Process all raw records
    processed_gigs: list[Gig] = []
    for raw in all_raw:
        try:
            gig = pipeline.process_raw(raw)
            if not dry_run:
                save_gig(gig)
            processed_gigs.append(gig)
        except Exception as exc:
            logger.debug("Error processing raw gig: %s", exc)

    # Filter accepted and evaluate first-money score
    candidate_gigs: list[tuple[float, Gig, str]] = []
    for gig in processed_gigs:
        if gig.rejection_reasons:
            continue
        if gig.status.value != "NEW":
            continue
        if gig.can_deliver not in (CanDeliver.HIGH, CanDeliver.MEDIUM):
            continue

        # Filter out stopped, closed, or oversubscribed gigs (> 50 offers)
        raw_status = (gig.source_facts or {}).get("kwork_status") or gig.source_status
        if raw_status and str(raw_status).lower() in ("stop", "user_stop", "stopped", "user_stopped", "closed", "archived", "expired"):
            continue
        comp = (gig.source_facts or {}).get("kwork_count_raw") or gig.competition
        if comp is not None:
            try:
                if int(comp) > 50:
                    continue
            except (ValueError, TypeError):
                pass

        # Filter out non-IT / content / design / sales jobs
        text_low = f"{gig.title} {gig.description}".lower()
        non_it_markers = (
            "контент-менеджер", "копирайтер", "копирайтинг", "smm", "таргетолог",
            "видеомонтаж", "видеомонтажер", "дизайн логотип", "дизайнер интерьер",
            "графическ", "дизайн баннер", "рерайтинг", "написание статей", "переводчик", "озвучка",
            "менеджер по продажам", "оператор колл", "холодные звонки", "тильда", "tilda",
        )
        if any(m in text_low for m in non_it_markers) and not any(k in text_low for k in ("python", "парс", "bot", "бот", "aiogram", "n8n", "linux", "bitrix", "битрикс")):
            continue

        # Score with grounded + first_money
        try:
            ana = analyze(gig.description, gig.title)
            cap = assess_capability(ana, pipeline.confidences)
            risk = assess_client_risk(gig)
            fm = first_money_score_of(gig, ana, cap, risk)
            fm_val = fm.score
        except Exception:
            fm_val = 0.0

        if gig.source_facts is None:
            gig.source_facts = {}
        gig.source_facts["first_money_score"] = fm_val

        best_score = max(fm_val, gig.opportunity_score or 0.0)
        if best_score < min_score:
            continue

        if is_gig_already_delivered(conn, gig.id):
            continue

        # Generate pitch using confirmed KB facts, in the ORDER's language
        # (English marketplaces like Freelancer/Contra get an English reply).
        pitch = generate_gig_pitch(
            title=gig.title,
            description=gig.description,
            budget=gig.budget_text or "",
            source=gig.source,
        )
        candidate_gigs.append((best_score, gig, pitch))

    candidate_gigs.sort(key=lambda item: -item[0])
    logger.info("Viable candidates after filtering: %d", len(candidate_gigs))

    # Select top N for delivery
    top_candidates = candidate_gigs[:max_deliveries]
    delivered_count = 0

    owner_chat_id = config.TELEGRAM_OWNER_ID or config.TELEGRAM_CHAT_ID or "392046103"

    # Phase 2: when the notification policy is ON, delivery goes through the
    # shared gateway (global dedup + rate limit + high-relevance-only-to-now).
    # Default OFF keeps the legacy per-card path below, byte-for-byte.
    if getattr(config, "TELEGRAM_NOTIFY_POLICY_ENABLED", False) and not dry_run:
        delivered_count, _ = deliver_gigs_with_policy(
            conn, top_candidates, owner_chat_id
        )
        conn.close()
        summary = {
            "raw_collected": len(all_raw),
            "processed": len(processed_gigs),
            "candidates": len(candidate_gigs),
            "delivered": delivered_count,
            "dry_run": dry_run,
            "mode": "policy",
        }
        logger.info("Cycle finished (policy mode): %s", summary)
        return summary

    for score, gig, pitch in top_candidates:
        fm_val = (gig.source_facts or {}).get("first_money_score")
        card_text, reply_markup = format_gig_card(gig, pitch=pitch, fm_score=fm_val)

        logger.info(
            "Selected gig [%s] Score=%.1f Title='%s' URL=%s",
            gig.id, score, gig.title[:50], gig.url,
        )

        if dry_run:
            logger.info("[DRY-RUN] Would deliver gig card to chat %s:\n%s", owner_chat_id, card_text[:200])
            delivered_count += 1
            continue

        ok = send_telegram_card(
            chat_id=owner_chat_id,
            text=card_text,
            reply_markup=reply_markup,
        )
        if ok:
            record_gig_delivery(conn, gig, chat_id=owner_chat_id)
            delivered_count += 1
            logger.info("Successfully delivered gig [%s] to Telegram chat %s", gig.id, owner_chat_id)
            time.sleep(1.5)
        else:
            logger.warning("Failed delivering gig [%s] to Telegram", gig.id)

    conn.close()
    summary = {
        "raw_collected": len(all_raw),
        "processed": len(processed_gigs),
        "candidates": len(candidate_gigs),
        "delivered": delivered_count,
        "dry_run": dry_run,
    }
    logger.info("Cycle finished: %s", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Freelance & Gig Cycle Runner")
    parser.add_argument("--max-deliveries", type=int, default=3, help="Max gigs to deliver per cycle (default 3, max 4)")
    parser.add_argument("--min-score", type=float, default=48.0, help="Minimum score floor for delivery (default 48.0)")
    parser.add_argument("--kwork-limit", type=int, default=12, help="Max Kwork projects to read (default 12)")
    parser.add_argument("--freelancer-limit", type=int, default=30, help="Max Freelancer requests (default 30)")
    parser.add_argument("--telegram-limit", type=int, default=25, help="Max TG channel posts to read (default 25)")
    parser.add_argument("--enable-freelancer", action="store_true", help="Enable Freelancer.com source (disabled by default)")
    parser.add_argument("--enable-contra", action="store_true", help="Enable Contra source (disabled by default)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate run without delivering to Telegram")
    args = parser.parse_args()

    if not acquire_lock():
        logger.warning("Another instance of gig autopilot is currently running. Exiting cleanly.")
        return 0

    try:
        run_gig_cycle(
            max_deliveries=min(max(1, args.max_deliveries), 4),
            min_score=args.min_score,
            kwork_limit=args.kwork_limit,
            freelancer_limit=args.freelancer_limit,
            telegram_limit=args.telegram_limit,
            enable_freelancer=args.enable_freelancer,
            enable_contra=args.enable_contra,
            dry_run=args.dry_run,
        )
        return 0
    except Exception as exc:
        logger.exception("Fatal unhandled exception in gig autopilot cycle: %s", exc)
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
