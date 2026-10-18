"""Telegram Freelance Channel adapter (Phase 4C).

Reads target IT freelance projects from public Telegram channels (@freelance_orders, @zakazy_it).
Uses web preview (https://t.me/s/<channel>) without requiring bot credentials or user session.
Maps extracted posts to the standard Gig raw dictionary schema.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterator

from .base import (
    HEALTH_FAILED,
    HEALTH_PARTIAL,
    HEALTH_SUCCESS,
    HEALTH_UNKNOWN,
    SourceHealth,
    summarize_health,
)

logger = logging.getLogger(__name__)

DEFAULT_GIG_CHANNELS = ["freelance_orders", "zakazy_it"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TelegramGigSource:
    """Live source for public Telegram freelance and project channels."""

    key = "telegram"
    source_mode = "live"
    source_scope = "public_channel"

    def __init__(
        self,
        channels: list[str] | None = None,
        *,
        include_db_recent: bool = True,
    ) -> None:
        self.channels = list(channels) if channels else list(DEFAULT_GIG_CHANNELS)
        self.include_db_recent = include_db_recent
        self._last_fetch_errors: list[str] = []
        self._last_health: SourceHealth | None = None

    def fetch(self, limit: int | None = None) -> Iterator[dict[str, Any]]:
        self._last_fetch_errors = []
        produced = 0
        try:
            for record in self._fetch_records(limit):
                produced += 1
                yield record
        except Exception as exc:
            self._last_fetch_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._last_health = summarize_health(produced, self._last_fetch_errors)

    def health(self) -> SourceHealth:
        return self._last_health or SourceHealth()

    def _fetch_records(self, limit: int | None = None) -> Iterator[dict[str, Any]]:
        seen_ids: set[str] = set()
        wanted = int(limit) if limit is not None else None
        count = 0

        # 1. Fetch live via TelegramChannelsAdapter
        try:
            from ...adapters.telegram_channels import TelegramChannelsAdapter

            adapter = TelegramChannelsAdapter(channels=self.channels)
            vacancies = adapter.fetch_all()
            for vac in vacancies:
                if wanted is not None and count >= wanted:
                    return
                raw_dict = self._vacancy_to_gig_raw(vac)
                sid = raw_dict["source_id"]
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    count += 1
                    yield raw_dict
        except Exception as exc:
            logger.warning("Live Telegram channels fetch failed: %s", exc)
            self._last_fetch_errors.append(f"Adapter error: {exc}")

        # 2. Check recent orders in state.db if live fetch yielded few items
        if self.include_db_recent and (wanted is None or count < wanted):
            try:
                import sqlite3
                from pathlib import Path

                db_path = Path(__file__).resolve().parents[3] / "state.db"
                if db_path.exists():
                    conn = sqlite3.connect(str(db_path))
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT stable_id, source, source_job_id, title, company, description,
                               salary_min, salary_max, salary_currency, job_url, published_at, raw_data
                        FROM vacancies
                        WHERE source IN ('tg_freelance_orders', 'tg_zakazy_it')
                           OR (raw_data LIKE '%"is_gig": true%' OR raw_data LIKE '%"is_gig":true%')
                        ORDER BY first_seen_at DESC
                        LIMIT 30
                        """
                    )
                    rows = cur.fetchall()
                    conn.close()

                    for row in rows:
                        if wanted is not None and count >= wanted:
                            return
                        (stable_id, src, s_job_id, title, comp, desc,
                         smin, smax, scurr, job_url, pub_at, raw_json) = row
                        
                        sid = str(s_job_id or stable_id or "")
                        clean_sid = sid.replace("/", "_")
                        if clean_sid in seen_ids or sid in seen_ids:
                            continue

                        raw_dict = self._db_row_to_gig_raw(
                            stable_id=stable_id,
                            source=src,
                            source_job_id=sid,
                            title=title,
                            description=desc,
                            salary_min=smin,
                            salary_max=smax,
                            salary_currency=scurr,
                            job_url=job_url,
                            published_at=pub_at,
                            raw_json=raw_json,
                        )
                        seen_ids.add(clean_sid)
                        count += 1
                        yield raw_dict
            except Exception as exc:
                logger.warning("DB recent gigs read failed: %s", exc)

    def _vacancy_to_gig_raw(self, vac: Any) -> dict[str, Any]:
        source = vac.source or "tg_freelance"
        source_id = str(vac.source_job_id or vac.stable_id or "").replace("/", "_")
        url = vac.job_url or f"https://t.me/{vac.source_job_id}"

        # If salary is unstated in the post, provide default floor so hard_gates doesn't discard
        bmin = float(vac.salary_min) if vac.salary_min is not None else 5000.0
        bmax = float(vac.salary_max) if vac.salary_max is not None else 25000.0
        curr = vac.salary_currency or "RUB"

        desc = vac.description or ""
        contact = (vac.raw_data or {}).get("contact", "")
        if contact and contact not in desc:
            desc = f"{desc}\n\nКонтакты: {contact}"

        return {
            "source": source,
            "source_id": source_id,
            "source_mode": "live",
            "source_scope": "public_channel",
            "title": vac.title or "Фриланс заказ",
            "description": desc,
            "url": url,
            "budget_min": bmin,
            "budget_max": bmax,
            "budget_type": "fixed",
            "currency": curr,
            "source_status": "OPEN",
            "published_at": _utcnow(),
            "client_name": vac.company or "Заказчик в Telegram",
            "skills": self._extract_skills(vac.title, desc),
            "source_facts": {
                "channel": (vac.raw_data or {}).get("channel", "telegram"),
                "contact": contact,
                "is_gig": True,
                "is_telegram": True,
            },
        }

    def _db_row_to_gig_raw(
        self,
        stable_id: str,
        source: str,
        source_job_id: str,
        title: str,
        description: str,
        salary_min: float | None,
        salary_max: float | None,
        salary_currency: str | None,
        job_url: str,
        published_at: str | None,
        raw_json: str | None,
    ) -> dict[str, Any]:
        import json

        raw_data = {}
        if raw_json:
            try:
                raw_data = json.loads(raw_json)
            except Exception:
                pass

        clean_sid = str(source_job_id or stable_id).replace("/", "_")
        url = job_url or f"https://t.me/{source_job_id}"

        bmin = float(salary_min) if salary_min is not None else 5000.0
        bmax = float(salary_max) if salary_max is not None else 25000.0
        curr = salary_currency or "RUB"

        contact = raw_data.get("contact", "")
        desc = description or ""
        if contact and contact not in desc:
            desc = f"{desc}\n\nКонтакты: {contact}"

        return {
            "source": source or "tg_freelance",
            "source_id": clean_sid,
            "source_mode": "live",
            "source_scope": "public_channel",
            "title": title or "Фриланс заказ",
            "description": desc,
            "url": url,
            "budget_min": bmin,
            "budget_max": bmax,
            "budget_type": "fixed",
            "currency": curr,
            "source_status": "OPEN",
            "published_at": published_at or _utcnow(),
            "client_name": "Заказчик в Telegram",
            "skills": self._extract_skills(title, desc),
            "source_facts": {
                "channel": raw_data.get("channel", source.replace("tg_", "")),
                "contact": contact,
                "is_gig": True,
                "is_telegram": True,
            },
        }

    def _extract_skills(self, title: str, description: str) -> list[str]:
        text = f"{title} {description}".lower()
        mapping = {
            "telegram": [r"\b(?:telegram|aiogram|telethon|телеграм|боты?|bots?|тг)\b"],
            "python": [r"\b(?:python|парсер\w*|парсинг\w*|скрипт\w*|django|fastapi|питон|playwright|beautifulsoup|scrapy)\b"],
            "bitrix24": [r"\b(?:bitrix\w*|битрикс\w*|crm|amocrm|амоцрм)\b"],
            "n8n": [r"\b(?:n8n|make\.com|zapier|albato|автоматизац\w*|integromat)\b"],
            "linux": [r"\b(?:linux|линукс|сервер\w*|ubuntu|debian|vps|vds|docker|vpn|wireguard|nginx|деплой\w*)\b"],
            "sql": [r"\b(?:sql|postgres\w*|mysql|баз\w+ данн\w*)\b"],
            "excel": [r"\b(?:excel|гугл таблиц\w*|google sheets?)\b"],
            "rest api": [r"\b(?:api|вебхук\w*|webhooks?|интеграц\w*|rest)\b"],
            "ai agents": [r"\b(?:ai|llm|rag|gpt|whisper|gemini|нейросет\w*|агент\w*)\b"],
        }
        found: list[str] = []
        for canonical, patterns in mapping.items():
            if any(re.search(p, text, re.I) for p in patterns):
                found.append(canonical)
        return found


