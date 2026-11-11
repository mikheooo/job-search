from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ..normalizer import normalize_salary_text
from ..schema import Vacancy


class HabrCareerAdapter:
    source = "habrcareer"
    feed_url = "https://career.habr.com/vacancies/rss"

    def fetch_vacancies(self, url: str | None = None) -> list[Vacancy]:
        target_url = url or self.feed_url
        req = urllib.request.Request(
            target_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
        except Exception:
            return []

        results: list[Vacancy] = []

        for item in root.findall(".//item"):
            job_url = item.findtext("link") or item.findtext("guid") or ""
            source_job_id = job_url.rstrip("/").rsplit("/", 1)[-1] if job_url else str(item.findtext("guid") or "")

            company = item.findtext("author") or ""
            if not company:
                # check dc:creator or category
                for child in item:
                    if child.tag.endswith("creator"):
                        company = child.text or ""
                        break

            title = item.findtext("title") or ""
            summary = item.findtext("description") or ""

            salary_info = normalize_salary_text(summary)
            if not salary_info.get("salary_min") and not salary_info.get("salary_max"):
                salary_info = normalize_salary_text(title)

            pub_date_str = item.findtext("pubDate")
            published_at = None
            if pub_date_str:
                try:
                    published_at = parsedate_to_datetime(pub_date_str)
                except Exception:
                    published_at = None

            tags = [cat.text.strip() for cat in item.findall("category") if cat.text]

            location = None
            m_loc = re.search(r"\(([^)]+)\)$", title.strip())
            if m_loc:
                loc_cand = m_loc.group(1).strip()
                if not re.search(r"\d", loc_cand):
                    location = loc_cand
            if not location and ("удаленн" in summary.lower() or "удалённ" in summary.lower()):
                location = "Удаленно"

            results.append(
                Vacancy(
                    source=self.source,
                    source_job_id=source_job_id,
                    title=title,
                    company=company.strip(),
                    description=summary,
                    job_url=job_url,
                    application_url=job_url,
                    location=location,
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
                        "published": pub_date_str,
                        "tags": tags,
                    },
                )
            )

        return results
