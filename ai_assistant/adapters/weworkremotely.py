from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

import feedparser

from ..schema import Vacancy
from ..vacancy_identity import normalize_url


class WeWorkRemotelyAdapter:
    source = "weworkremotely"
    feed_url = "https://weworkremotely.com/remote-jobs.rss"

    def fetch_vacancies(self) -> list[Vacancy]:
        feed = feedparser.parse(self.feed_url)
        results: list[Vacancy] = []
        seen_urls = set()
        for entry in feed.entries:
            raw_url = entry.get("link") or entry.get("id") or ""
            job_url = normalize_url(raw_url) if raw_url else ""
            if not job_url or job_url in seen_urls:
                continue
            seen_urls.add(job_url)

            source_job_id = job_url.rstrip("/").rsplit("/", 1)[-1] if job_url else str(entry.get("id", ""))
            summary = entry.get("summary") or ""
            raw_apply = self._extract_apply_link(summary) or job_url or None
            application_url = normalize_url(raw_apply) if raw_apply else None

            location = self._join_nonempty(
                entry.get("region"), entry.get("country"), entry.get("state")
            ) or None

            published_at = None
            if entry.get("published_parsed"):
                try:
                    published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    published_at = None

            results.append(
                Vacancy(
                    source=self.source,
                    source_job_id=source_job_id,
                    title=entry.get("title", "").strip(),
                    company="",
                    description=summary,
                    job_url=job_url,
                    application_url=application_url,
                    location=location,
                    country_restrictions=[],
                    timezone_restrictions=[],
                    employment_type=entry.get("type"),
                    published_at=published_at,
                    raw_data={
                        "title": entry.get("title"),
                        "summary": summary,
                        "published": entry.get("published"),
                        "tags": [t.get("term") for t in (entry.get("tags") or []) if t.get("term")],
                    },
                )
            )
        return results

    @staticmethod
    def _extract_apply_link(text: str) -> str | None:
        match = re.search(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>\s*To apply:", text, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"https?://\S+", text)
        return match.group(0) if match else None

    @staticmethod
    def _join_nonempty(*parts: str | None) -> str | None:
        joined = ", ".join([p.strip() for p in parts if p and p.strip()])
        return joined or None
