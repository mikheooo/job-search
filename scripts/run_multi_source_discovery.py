"""Multi-Source Vacancy Discovery Runner.

Fetches tech vacancies and project gigs across diverse channels:
- HeadHunter (handled via dedicated autopilot cycle)
- Bangkok Direct Careers (Agoda, Lalamove, MNC tech hubs via Greenhouse/Lever ATS)
- LinkedIn Bangkok (expat-friendly offline/hybrid positions with visa sponsorship)
- Habr Career (telecom, sysadmin, devops, python)
- Hirify API (Russian remote IT aggregator)
- GeekJob (senior/middle developer & infrastructure roles)
- Telegram job & freelance channels (@freelance_orders, @zakazy_it, @remoteit, @normrabota, etc.)

Filters against candidate profile, writes to state.db as 'DISCOVERED',
and optionally sends rich digest to Telegram (@jobsassistbot).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import time
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_assistant import config, db
from ai_assistant.adapters.bangkok_direct_careers import BangkokDirectCareersAdapter
from ai_assistant.adapters.geekjob import GeekJobAdapter
from ai_assistant.adapters.habr_career import HabrCareerAdapter
from ai_assistant.adapters.hirify import HirifyAdapter
from ai_assistant.adapters.linkedin_bangkok import LinkedInBangkokAdapter
from ai_assistant.adapters.remoteok import RemoteOkAdapter
from ai_assistant.adapters.telegram_channels import TelegramChannelsAdapter
from ai_assistant.adapters.weworkremotely import WeWorkRemotelyAdapter
from ai_assistant.schema import Vacancy

# Phase 5 windowless: started by pythonw.exe (no console), so stderr goes
# nowhere. Write the log file from inside python instead of depending on the
# launcher's `>> log 2>&1`, which forced a visible cmd.exe window each run.
_LOG_PATH = os.path.join(str(PROJECT_ROOT), "logs", "multi_source_discovery.log")
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
logger = logging.getLogger("multi_source_discovery")

# Phase 3 security: credentials come from environment (.env) via
# ai_assistant.config — no hardcoded tokens. Resolves to the same HH bot
# previously hardcoded here, so runtime behavior is unchanged.
TELEGRAM_BOT_TOKEN = config.HH_TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = config.HH_TELEGRAM_CHAT_ID or config.TELEGRAM_CHAT_ID
def configured_channel_id() -> str:
    """Channel from the environment. Empty when unset — never a hardcoded id."""
    env = (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
    if env:
        return env
    return (getattr(config, "TELEGRAM_CHANNEL_ID", "") or "").strip()


# Snapshot at import. send_telegram_alert reads configured_channel_id() live.
TELEGRAM_CHANNEL_ID = configured_channel_id()
MIN_SALARY_RUB = getattr(config, "MIN_SALARY", 0)

TARGET_KEYWORDS = [
    "admin", "админ", "devops", "девопс", "linux", "линукс", "support", "поддержк",
    "сопровожд", "crm", "битрикс", "bitrix", "amocrm", "python", "ai", "mlops", "инженер",
    "инфраструктур", "сетев", "sre", "backend", "бэкенд", "network", "engineer",
    "deskside", "technician", "helpdesk", "cloud", "system", "парс", "бот", "скрипт",
    "vps", "сервер", "vpn", "docker"
]


def send_telegram_alert(text: str, chat_id: str | int | None = None) -> bool:
    """Send message to Telegram channel or chat. Fail closed without a channel."""
    import urllib.request
    target = chat_id or configured_channel_id()
    if not str(target or "").strip():
        logger.error(
            "TELEGRAM_CHANNEL_ID is not set; refusing to send a discovery alert"
        )
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False


def is_target_role(title: str, description: str = "") -> bool:
    """Check if vacancy matches Mikhail's target roles and technical skills."""
    import re
    combined = (title + " " + description).lower()
    for kw in TARGET_KEYWORDS:
        if len(kw) <= 2:
            if re.search(r"\b" + re.escape(kw) + r"\b", combined):
                return True
        elif kw in combined:
            return True
    return False


def is_acceptable_salary(vac: Vacancy) -> bool:
    """Check if salary meets floor (project gigs and foreign currencies are accepted)."""
    min_sal = getattr(config, "MIN_SALARY", 0)
    if min_sal <= 0:
        return True
    if vac.employment_type == "project" or (vac.raw_data and vac.raw_data.get("is_gig")):
        return True
    if vac.salary_currency not in ("RUB", "RUR"):
        return True
    if vac.salary_max is not None and vac.salary_max < min_sal:
        return False
    if vac.salary_min is not None and vac.salary_min < min_sal and vac.salary_max is None:
        return False
    return True


def save_vacancies_to_db(vacancies: list[Vacancy]) -> int:
    """Save new vacancies to state.db. Returns count of newly added rows."""
    db.init_db()
    conn = db.get_connection()
    cursor = conn.cursor()

    new_count = 0
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    for v in vacancies:
        stable_id = f"{v.source}:{v.source_job_id}"
        cursor.execute("SELECT stable_id FROM vacancies WHERE stable_id = ?", (stable_id,))
        if cursor.fetchone():
            continue

        raw_json = json.dumps(v.raw_data or {}, ensure_ascii=False)
        pub_iso = v.published_at.isoformat() if v.published_at else now_iso

        cursor.execute("""
            INSERT INTO vacancies (
                stable_id, source, source_job_id, title, company, description,
                location, salary_min, salary_max, salary_currency,
                employment_type, job_url, application_url, published_at,
                first_seen_at, last_seen_at, state, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stable_id, v.source, v.source_job_id, v.title, v.company, v.description,
            v.location or "Удаленно", v.salary_min, v.salary_max, v.salary_currency or "RUB",
            v.employment_type or "full", v.job_url, v.application_url or v.job_url, pub_iso,
            now_iso, now_iso, "DISCOVERED", raw_json
        ))
        new_count += 1

    conn.commit()
    conn.close()
    return new_count


def run_discovery(notify: bool = True) -> dict[str, Any]:
    """Run full discovery across Habr, Hirify, GeekJob, Bangkok Careers, LinkedIn, and Telegram."""
    logger.info("Starting multi-source vacancy discovery...")
    start_time = time.time()
    collected: list[Vacancy] = []

    # 1. Habr Career
    try:
        habr = HabrCareerAdapter()
        habr_vacs = habr.fetch_vacancies()
        logger.info(f"Habr Career: fetched {len(habr_vacs)} vacancies")
        collected.extend(habr_vacs)
    except Exception as e:
        logger.error(f"Habr Career adapter failed: {e}")

    # 2. Hirify API
    try:
        hirify = HirifyAdapter()
        hirify_vacs = hirify.fetch_vacancies(max_pages=2)
        logger.info(f"Hirify: fetched {len(hirify_vacs)} vacancies")
        collected.extend(hirify_vacs)
    except Exception as e:
        logger.error(f"Hirify adapter failed: {e}")

    # 3. GeekJob
    try:
        geekjob = GeekJobAdapter()
        geek_vacs = geekjob.fetch_all(max_pages=2)
        logger.info(f"GeekJob: fetched {len(geek_vacs)} vacancies")
        collected.extend(geek_vacs)
    except Exception as e:
        logger.error(f"GeekJob adapter failed: {e}")

    # 4. Bangkok Direct Corporate Tech Careers (Agoda, Lalamove via ATS APIs)
    try:
        bkk_direct = BangkokDirectCareersAdapter()
        bkk_vacs = bkk_direct.fetch_all()
        logger.info(f"Bangkok Direct Careers: fetched {len(bkk_vacs)} vacancies")
        collected.extend(bkk_vacs)
    except Exception as e:
        logger.error(f"Bangkok Direct Careers adapter failed: {e}")

    # 5. LinkedIn Bangkok (Offline / Hybrid for Expats)
    try:
        linkedin = LinkedInBangkokAdapter()
        li_vacs = linkedin.fetch_all()
        logger.info(f"LinkedIn Bangkok: fetched {len(li_vacs)} offline/hybrid vacancies")
        collected.extend(li_vacs)
    except Exception as e:
        logger.error(f"LinkedIn Bangkok adapter failed: {e}")

    # 6. Telegram Channels (IT Jobs & Freelance Gigs)
    try:
        tg = TelegramChannelsAdapter()
        tg_vacs = tg.fetch_all()
        logger.info(f"Telegram channels: fetched {len(tg_vacs)} vacancies/gigs")
        collected.extend(tg_vacs)
    except Exception as e:
        logger.error(f"Telegram channels adapter failed: {e}")

    # 7. RemoteOK (Global Remote Tech Jobs)
    try:
        remoteok = RemoteOkAdapter()
        ro_vacs = remoteok.fetch_vacancies()
        logger.info(f"RemoteOK: fetched {len(ro_vacs)} global remote vacancies")
        collected.extend(ro_vacs)
    except Exception as e:
        logger.error(f"RemoteOK adapter failed: {e}")

    # 8. WeWorkRemotely (Top International Remote Tech Feed)
    try:
        wwr = WeWorkRemotelyAdapter()
        wwr_vacs = wwr.fetch_vacancies()
        logger.info(f"WeWorkRemotely: fetched {len(wwr_vacs)} remote tech vacancies")
        collected.extend(wwr_vacs)
    except Exception as e:
        logger.error(f"WeWorkRemotely adapter failed: {e}")

    # Filtering
    filtered = []
    for v in collected:
        if is_target_role(v.title, v.description) and is_acceptable_salary(v):
            filtered.append(v)

    logger.info(f"Filtered {len(filtered)} target vacancies/gigs (out of {len(collected)} total)")

    # Save to state.db
    new_saved = save_vacancies_to_db(filtered)
    logger.info(f"Ingested {new_saved} brand-new vacancies/gigs into state.db")

    duration = round(time.time() - start_time, 1)

    # Summary notification to Telegram channel (@remotejobd)
    if notify and new_saved > 0 and not getattr(config, "TELEGRAM_FLASH_SUPPRESS_ENABLED", False):
        sample_lines = []
        for v in filtered[:4]:
            sal = f" ({v.salary_min or ''}..{v.salary_max or ''} {v.salary_currency})" if v.salary_min or v.salary_max else ""
            src = v.source.replace("tg_", "@")
            sample_lines.append(f"• <b>{v.title}</b> ({src}){sal}\n  <a href=\"{v.job_url}\">Ссылка на вакансию</a>")

        samples_block = "\n".join(sample_lines)
        msg = (
            f"📡 <b>Мульти-сбор вакансий завершен!</b>\n\n"
            f"Источники: <i>Agoda Careers, LinkedIn Bangkok, Telegram, Hirify, GeekJob, Habr Career</i>\n"
            f"Собрано всего: <b>{len(collected)}</b>\n"
            f"Соответствует фильтрам: <b>{len(filtered)}</b>\n"
            f"Новых сохранено в базу: <b>{new_saved}</b>\n\n"
            f"<b>Примеры свежих вакансий:</b>\n{samples_block}"
        )
        send_telegram_alert(msg, chat_id=TELEGRAM_CHANNEL_ID)

    return {
        "status": "SUCCESS",
        "total_collected": len(collected),
        "target_matched": len(filtered),
        "new_saved": new_saved,
        "duration_seconds": duration,
    }


if __name__ == "__main__":
    result = run_discovery(notify=True)
    print(json.dumps(result, indent=2, ensure_ascii=False))
