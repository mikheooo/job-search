from __future__ import annotations
import re
from datetime import datetime, timezone

import feedparser

from ..schema import Vacancy


class WeWorkRemotelyAdapter:
    source = "weworkremotely"
    feed_url = "https://weworkremotely.com/remote-jobs.rss"

    def fetch_vacancies(self) -> List[Vacancy]:
        feed = feedparser.parse(self.feed_url)
        results: List[Vacancy] = []
        for entry in feed.entries:
            job_url = entry.get("link") or entry.get("id") or ""
            source_job_id = job_url.rsplit("/", 1)[-1] if job_url else entry.get("id", "")
            summary = entry.get("summary") or ""
            application_url = self._extract_apply_link(summary) or job_url or None

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
                    title=entry.get("title", ""),
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
