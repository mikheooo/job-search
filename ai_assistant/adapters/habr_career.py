from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

import feedparser

from ..schema import Vacancy
from ..normalizer import normalize_salary_text


class HabrCareerAdapter:
    source = "habrcareer"
    feed_url = "https://career.habr.com/vacancies/rss"

    def fetch_vacancies(self, url: Optional[str] = None) -> List[Vacancy]:
        target_url = url or self.feed_url
        feed = feedparser.parse(target_url)
        results: List[Vacancy] = []

        for entry in feed.entries:
            job_url = entry.get("link") or entry.get("id") or ""
            source_job_id = job_url.rstrip("/").rsplit("/", 1)[-1] if job_url else entry.get("id", "")

            # Company is provided in author or authors list
            company = entry.get("author") or ""
            if not company and entry.get("authors"):
                company = entry.get("authors")[0].get("name", "")

            title = entry.get("title") or ""
            summary = entry.get("summary") or ""

            # Check for salary info in title or summary
            salary_info = normalize_salary_text(summary)
            if not salary_info.get("salary_min") and not salary_info.get("salary_max"):
                salary_info = normalize_salary_text(title)

            published_at = None
            if entry.get("published_parsed"):
                try:
                    published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    published_at = None

            # Tags from RSS
            tags = [t.get("term") for t in (entry.get("tags") or []) if t.get("term")]

            results.append(
                Vacancy(
                    source=self.source,
                    source_job_id=source_job_id,
                    title=title,
                    company=company.strip(),
                    description=summary,
                    job_url=job_url,
                    application_url=job_url,
                    location="Remote / Hybrid / Onsite",
                    country_restrictions=[],
                    timezone_restrictions=[],
                    salary_min=salary_info.get("salary_min"),
                    salary_max=salary_info.get("salary_max"),
                    salary_currency=salary_info.get("salary_currency"),
                    employment_type="Full Time",
                    published_at=published_at,
                    raw_data={
                        "title": title,
                        "company": company,
                        "summary": summary,
                        "published": entry.get("published"),
                        "tags": tags,
                    },
                )
            )

        return results
