from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from ..schema import Vacancy
from ..vacancy_identity import normalize_url


class WeWorkRemotelyAdapter:
    source = "weworkremotely"
    feed_url = "https://weworkremotely.com/remote-jobs.rss"

    def fetch_vacancies(self) -> list[Vacancy]:
        req = urllib.request.Request(
            self.feed_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
        except Exception:
            return []

        results: list[Vacancy] = []
        seen_urls = set()

        for item in root.findall(".//item"):
            raw_url = item.findtext("link") or item.findtext("guid") or ""
            job_url = normalize_url(raw_url) if raw_url else ""
            if not job_url or job_url in seen_urls:
                continue
            seen_urls.add(job_url)

            raw_title = item.findtext("title") or ""
            company = ""
            title = raw_title
            if ":" in raw_title:
                parts = raw_title.split(":", 1)
                company = parts[0].strip()
                title = parts[1].strip()

            source_job_id = job_url.rstrip("/").rsplit("/", 1)[-1] if job_url else str(item.findtext("guid") or "")
            summary = item.findtext("description") or ""
            raw_apply = self._extract_apply_link(summary) or job_url or None
            application_url = normalize_url(raw_apply) if raw_apply else None

            region = item.findtext("region")
            country = item.findtext("country")
            location = self._join_nonempty(region, country) or "Remote Worldwide"

            pub_date_str = item.findtext("pubDate")
            published_at = None
            if pub_date_str:
                try:
                    from email.utils import parsedate_to_datetime
                    published_at = parsedate_to_datetime(pub_date_str)
                except Exception:
                    published_at = None

            results.append(
                Vacancy(
                    source=self.source,
                    source_job_id=source_job_id,
                    title=title,
                    company=company,
                    description=summary,
                    job_url=job_url,
                    application_url=application_url,
                    location=location,
                    country_restrictions=[],
                    timezone_restrictions=[],
                    employment_type="full_time",
                    published_at=published_at,
                    raw_data={
                        "title": raw_title,
                        "summary": summary,
                        "pubDate": pub_date_str,
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
