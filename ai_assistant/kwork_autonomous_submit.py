"""Autonomous Kwork Proposal Submitter.

Enables the AI autopilot to autonomously discover viable IT orders on Kwork,
generate grounded proposals, fill the offer form via CDP, and submit proposals
without asking the user for confirmation.

When a client replies on Kwork, the continuous Kwork inbox monitor
(JobSearch_Kwork_Inbox_Monitor) detects the reply and alerts Mikhail in Telegram.
"""
from __future__ import annotations

import datetime
import html
import json
import logging
import os
import pathlib
import re
import sys
import time
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config
from ai_assistant.db import get_connection
from ai_assistant.gig_pitch_generator import generate_gig_pitch
from ai_assistant.hh_browser_provider import (
    list_tabs,
    make_target_evaluate,
    open_tab,
)

DEFAULT_CDP_URL = "http://127.0.0.1:9111"

logger = logging.getLogger("kwork_autonomous_submit")

IT_KEYWORDS = (
    "python", "бот", "bot", "парсинг", "парсер", "скрипт", "автоматизац",
    "апи", "api", "crm", "битрикс", "bitrix", "ai", "ии", "chatgpt", "openai",
    "интеграц", "база данных", "backend", "бэкенд", "разработ", "сервер",
    "телеграм", "telegram", "fastapi", "django", "flask", "почт", "сбор данных",
)

EXCLUDE_KEYWORDS = (
    "дизайн", "figma", "баннер", "видеомонтаж", "копирайт", "рерайт",
    "озвучк", "переводчик", "3d", "модель", "фотошоп", "photoshop", "smm",
)


def is_it_relevant_project(title: str, description: str = "") -> bool:
    """Filter projects for IT, Python, automation, bot, and CRM relevance."""
    text = f"{title} {description}".lower()
    if any(ex in text for ex in EXCLUDE_KEYWORDS):
        return False
    return any(kw in text for kw in IT_KEYWORDS)


def is_kwork_already_applied(conn: Any, project_id: str | int) -> bool:
    """Check if a proposal has already been submitted or shortlisted for this project."""
    pid_str = str(project_id)
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM top_gig_proposals WHERE gig_id = ? OR gig_id = ? OR url LIKE ?",
        (pid_str, f"kwork_{pid_str}", f"%projects/{pid_str}%"),
    )
    if cur.fetchone():
        return True

    cur.execute(
        "SELECT id FROM gigs WHERE (id = ? OR source_id = ? OR url LIKE ?) AND status IN ('APPLIED', 'SHORTLISTED', 'IN_PROGRESS')",
        (f"kwork_{pid_str}", pid_str, f"%projects/{pid_str}%"),
    )
    if cur.fetchone():
        return True

    return False


def record_kwork_proposal_submission(
    conn: Any,
    project_id: str | int,
    title: str,
    pitch: str,
    price: int | float,
    duration_days: int,
    url: str,
) -> None:
    """Persist the autonomous proposal submission in state.db."""
    pid_str = str(project_id)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cur = conn.cursor()

    # Record in top_gig_proposals
    cur.execute(
        """
        INSERT OR REPLACE INTO top_gig_proposals
        (gig_id, source, title, budget_text, contact, deeplink, url, proposal_pitch, status, created_at, updated_at)
        VALUES (?, 'kwork', ?, ?, '', '', ?, ?, 'APPLIED', ?, ?)
        """,
        (f"kwork_{pid_str}", title, f"{price:.0f} ₽ ({duration_days} дн.)", url, pitch, now_iso, now_iso),
    )

    # Also update or insert in gigs table
    cur.execute(
        """
        INSERT OR REPLACE INTO gigs
        (id, source, source_id, url, title, description, category, budget_min, budget_max, currency, status, created_at, updated_at)
        VALUES (?, 'kwork', ?, ?, ?, ?, 'Development & IT', ?, ?, 'RUB', 'APPLIED', ?, ?)
        """,
        (f"kwork_{pid_str}", pid_str, url, title, pitch, price, price, now_iso, now_iso),
    )
    conn.commit()


def send_kwork_submission_telegram_alert(
    project_id: str | int,
    title: str,
    price: int | float,
    duration_days: int,
    url: str,
    pitch: str,
) -> bool:
    """Send informative Telegram notification for confirmed autonomous proposal."""
    from ai_assistant import telegram_routing

    first_lines = "\n".join(pitch.strip().splitlines()[:5])
    text = (
        f"💼 *Kwork: Автопилот отправил предложение на заказ!*\n\n"
        f"📌 *Заказ:* [{title}]({url})\n"
        f"💰 *Предложение:* {price:.0f} ₽ | ⏱ *Срок:* {duration_days} дн.\n"
        f"🕒 *Время:* {time.strftime('%Y-%m-%d %H:%M:%S MSK')}\n\n"
        f"📝 *Текст предложения (фрагмент):*\n"
        f"_{first_lines}..._\n\n"
        f"✅ _Предложение отправлено заказчику. Когда он ответит в чате, бот сразу пришлет уведомление для взятия в работу._"
    )
    try:
        return bool(telegram_routing.send_job_message(text, parse_mode="Markdown"))
    except Exception as exc:
        logger.warning(f"Failed to send Telegram alert for Kwork submission {project_id}: {exc}")
        return False


def get_or_open_kwork_tab(cdp_url: str = DEFAULT_CDP_URL) -> dict[str, Any] | None:
    """Find an existing Kwork tab or open one."""
    tabs = list_tabs(cdp_url)
    kwork_tab = next((t for t in tabs if "kwork.ru" in t.get("url", "")), None)
    if not kwork_tab:
        try:
            kwork_tab = open_tab(cdp_url, "https://kwork.ru/projects?c=11")
            time.sleep(3.0)
        except Exception as exc:
            logger.error(f"Failed to open Kwork tab: {exc}")
            return None
    return kwork_tab


def scan_live_kwork_projects(cdp_url: str = DEFAULT_CDP_URL, limit: int = 15) -> list[dict[str, Any]]:
    """Scan open projects from Kwork exchange."""
    kwork_tab = get_or_open_kwork_tab(cdp_url)
    if not kwork_tab:
        return []

    eval_fn = make_target_evaluate(cdp_url, kwork_tab["id"])
    eval_fn("window.location.href = 'https://kwork.ru/projects?c=11'")
    time.sleep(3.5)

    script = """
    JSON.stringify((() => {
        const cards = Array.from(document.querySelectorAll('.card, .want-card, .project-card, .b-want-block, tr, [data-project-id]'));
        const results = [];
        const seenIds = new Set();
        for (const c of cards) {
            const a = c.querySelector('a[href*="/projects/"]');
            if (!a) continue;
            const href = a.getAttribute('href') || '';
            const m = href.match(/\\/projects\\/(\\d+)/);
            if (!m) continue;
            const pid = m[1];
            if (seenIds.has(pid)) continue;
            seenIds.add(pid);

            const title = a.innerText.trim();
            if (!title || title.length < 5) continue;

            const text = c.innerText.replace(/\\s+/g, ' ');
            results.push({
                project_id: pid,
                url: `https://kwork.ru/projects/${pid}/view`,
                title: title,
                text: text
            });
        }
        return results;
    })())
    """
    try:
        raw = eval_fn(script)
        items = json.loads(raw) if isinstance(raw, str) else raw
        return items[:limit] if isinstance(items, list) else []
    except Exception as exc:
        logger.error(f"Error scanning Kwork projects: {exc}")
        return []


def submit_kwork_proposal_cdp(
    project_id: str | int,
    title: str,
    pitch: str,
    budget: float | int | None = None,
    duration_days: int = 2,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    """Fill and submit proposal for a Kwork project via CDP."""
    pid = str(project_id)
    kwork_tab = get_or_open_kwork_tab(cdp_url)
    if not kwork_tab:
        return {"ok": False, "status": "NO_BROWSER", "reason": "No Kwork tab available on CDP"}

    eval_fn = make_target_evaluate(cdp_url, kwork_tab["id"])
    target_url = f"https://kwork.ru/new_offer?project={pid}"
    logger.info("Navigating to Kwork offer page: %s", target_url)
    eval_fn(f"window.location.href = '{target_url}'")
    time.sleep(3.5)

    # Check page status (e.g. if project is closed or redirected)
    check_status_script = """
    JSON.stringify((() => {
        const body = document.body ? document.body.innerText : '';
        const isClosed = body.includes('проект уже закрыт') || body.includes('Набор предложений завершен');
        const loginBtn = document.querySelector('a[href*="/login"], .header-login-btn');
        const hasForm = !!document.querySelector('button.kw-button--green, #offer-custom-price');
        return {
            url: window.location.href,
            title: document.title,
            is_closed: isClosed,
            is_logged_in: !loginBtn,
            has_form: hasForm
        };
    })())
    """
    status_raw = eval_fn(check_status_script)
    status_info = json.loads(status_raw) if isinstance(status_raw, str) else status_raw

    if not status_info.get("is_logged_in"):
        return {"ok": False, "status": "AUTH_REQUIRED", "reason": "Not logged in on Kwork"}
    if status_info.get("is_closed"):
        return {"ok": False, "status": "PROJECT_CLOSED", "reason": "Project is already closed on Kwork"}
    if not status_info.get("has_form"):
        return {"ok": False, "status": "NO_OFFER_FORM", "reason": f"No offer form on {status_info.get('url')}"}

    # Prepare safe title and pitch strings
    safe_title = title.strip()[:100]
    safe_pitch = pitch.strip()

    # Fill form via Vue component and Trumbowyg
    fill_script = f"""
    JSON.stringify((() => {{
        const btn = document.querySelector('button.kw-button--green');
        let cur = btn;
        while (cur && !cur.__vue__) cur = cur.parentElement;
        const vueRoot = cur ? cur.__vue__ : null;

        let p = document.querySelector('#offer-custom-price');
        while (p && !p.__vue__) p = p.parentElement;
        const vueOfferCustom = p ? p.__vue__ : null;

        if (!vueRoot) return {{ ok: false, error: 'vueRoot not found' }};

        const minPrice = vueOfferCustom ? (vueOfferCustom.minPrice || 500) : (vueRoot.minPrice || 500);
        const maxPrice = vueOfferCustom ? (vueOfferCustom.maxPrice || 1500) : (vueRoot.maxPrice || 1500);
        
        let targetPrice = {budget if budget else 'null'};
        if (!targetPrice || targetPrice < minPrice) targetPrice = minPrice;
        if (targetPrice > maxPrice) targetPrice = maxPrice;

        const offerTitle = {json.dumps(safe_title, ensure_ascii=False)};
        const offerPitch = {json.dumps(safe_pitch, ensure_ascii=False)};
        const duration = {int(duration_days)};

        // 1. Fill Description (Trumbowyg & check-text)
        const descTextarea = document.querySelector('textarea[name="description"]');
        if (window.$ && descTextarea) {{
            $(descTextarea).trumbowyg('html', offerPitch);
        }}
        const descEditor = document.querySelector('.trumbowyg-editor');
        if (descEditor) {{
            descEditor.innerHTML = offerPitch;
            descEditor.dispatchEvent(new Event('input', {{ bubbles: true }}));
            descEditor.dispatchEvent(new Event('blur', {{ bubbles: true }}));
        }}

        // 2. Fill Name/Title
        const nameTextarea = document.querySelector('textarea[name="name"]');
        if (window.$ && nameTextarea) {{
            $(nameTextarea).trumbowyg('html', offerTitle);
        }}
        const editors = Array.from(document.querySelectorAll('.trumbowyg-editor'));
        if (editors.length > 1) {{
            editors[1].innerHTML = offerTitle;
            editors[1].dispatchEvent(new Event('input', {{ bubbles: true }}));
            editors[1].dispatchEvent(new Event('blur', {{ bubbles: true }}));
        }}

        // 3. Fill Price
        const priceInput = document.querySelector('#offer-custom-price');
        if (priceInput) {{
            priceInput.value = String(targetPrice);
            priceInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            priceInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
            priceInput.dispatchEvent(new Event('blur', {{ bubbles: true }}));
        }}
        if (vueOfferCustom) {{
            try {{ vueOfferCustom.inputPrice({{ target: {{ value: String(targetPrice) }} }}); }} catch (e) {{}}
            try {{ vueOfferCustom.updatePrice(String(targetPrice)); }} catch (e) {{}}
            try {{ vueOfferCustom.inputOfferName({{ target: {{ value: offerTitle }} }}); }} catch (e) {{}}
            try {{ vueOfferCustom.updateDuration(duration); }} catch (e) {{}}
        }}

        // 4. Validate
        let isValid = false;
        try {{
            isValid = vueRoot.validateAll ? vueRoot.validateAll() : true;
        }} catch (e) {{
            isValid = false;
        }}

        return {{
            ok: true,
            is_valid: isValid,
            target_price: targetPrice,
            duration: duration,
            errors: vueRoot.errors || []
        }};
    }})())
    """
    fill_raw = eval_fn(fill_script)
    fill_res = json.loads(fill_raw) if isinstance(fill_raw, str) else fill_raw

    if not fill_res.get("ok"):
        return {"ok": False, "status": "FILL_FAILED", "reason": fill_res.get("error")}

    if not fill_res.get("is_valid") and fill_res.get("errors"):
        return {
            "ok": False,
            "status": "VALIDATION_FAILED",
            "reason": f"Form validation failed with errors: {fill_res.get('errors')}",
        }

    # Form is valid! Guard outbound submission immediately before physical submit
    from ai_assistant.outbound_governor import OutboundAction, check_outbound_allowed
    allowed, halt = check_outbound_allowed(action=OutboundAction.GIG_APPLICATION, dry_run=False)
    if not allowed:
        return {"ok": False, "status": "REFUSED_KILL_SWITCH", "reason": halt or "Outbound gig application halted by safety policy"}

    # Now submit the offer.
    submit_script = """
    JSON.stringify((() => {
        const btn = document.querySelector('button.kw-button--green');
        let cur = btn;
        while (cur && !cur.__vue__) cur = cur.parentElement;
        const vueRoot = cur ? cur.__vue__ : null;

        if (vueRoot && typeof vueRoot.submitOffer === 'function') {
            vueRoot.submitOffer();
            return { ok: true, method: 'vueRoot.submitOffer' };
        }
        if (btn) {
            btn.click();
            return { ok: true, method: 'btn.click' };
        }
        return { ok: false, error: 'no submit button or method found' };
    })())
    """
    eval_fn(submit_script)
    time.sleep(3.5)

    # Verify submission result (redirected to inbox/chat or success state)
    verify_script = """
    JSON.stringify((() => {
        return {
            url: window.location.href,
            pathname: window.location.pathname,
            body: document.body ? document.body.innerText.slice(0, 500) : ''
        };
    })())
    """
    verify_raw = eval_fn(verify_script)
    verify_res = json.loads(verify_raw) if isinstance(verify_raw, str) else verify_raw

    submitted = "/inbox" in verify_res.get("url", "") or "/track" in verify_res.get("url", "") or "/projects" in verify_res.get("pathname", "")
    final_price = fill_res.get("target_price", 1000)

    return {
        "ok": True,
        "submitted": submitted,
        "project_id": pid,
        "price": final_price,
        "duration": duration_days,
        "target_url": target_url,
        "final_url": verify_res.get("url"),
    }


def run_kwork_autonomous_cycle(limit: int = 1, min_score: float = 40.0) -> dict[str, Any]:
    """Autonomous Kwork cycle: scan open projects, pick top IT project, submit proposal."""
    logger.info("Starting Kwork autonomous submission cycle...")
    conn = get_connection()
    try:
        projects = scan_live_kwork_projects(limit=15)
        logger.info("Found %d projects on Kwork board", len(projects))

        viable_candidates = []
        for p in projects:
            pid = p["project_id"]
            title = p["title"]
            text = p.get("text", "")
            if not is_it_relevant_project(title, text):
                continue
            if is_kwork_already_applied(conn, pid):
                continue
            viable_candidates.append(p)

        logger.info("Viable Kwork candidates: %d", len(viable_candidates))
        if not viable_candidates:
            return {"status": "NO_NEW_PROJECTS", "submitted": 0}

        submitted_count = 0
        for cand in viable_candidates[:limit]:
            pid = cand["project_id"]
            title = cand["title"]
            text = cand.get("text", "")
            url = f"https://kwork.ru/projects/{pid}/view"

            # Generate tailored proposal pitch
            pitch = generate_gig_pitch(
                title=title,
                description=text,
                budget="",
                source="kwork",
            )

            # Submit autonomously via CDP
            sub_res = submit_kwork_proposal_cdp(
                project_id=pid,
                title=title,
                pitch=pitch,
                duration_days=2,
            )

            if sub_res.get("ok"):
                price = sub_res.get("price", 1000)
                record_kwork_proposal_submission(
                    conn=conn,
                    project_id=pid,
                    title=title,
                    pitch=pitch,
                    price=price,
                    duration_days=2,
                    url=url,
                )
                send_kwork_submission_telegram_alert(
                    project_id=pid,
                    title=title,
                    price=price,
                    duration_days=2,
                    url=url,
                    pitch=pitch,
                )
                submitted_count += 1
                logger.info("Autonomous Kwork submission successful for #%s: %s", pid, title)
            else:
                logger.warning("Autonomous Kwork submission failed for #%s: %s", pid, sub_res)

        return {"status": "COMPLETED", "submitted": submitted_count}
    finally:
        conn.close()
