"""LinkedIn Bangkok Expat & Foreigner-Friendly Vacancy Adapter.

Queries LinkedIn guest job search API for tech roles in Bangkok, Thailand
that are open to foreigners, offer Visa Sponsorship, Work Permit, or English-speaking
international environments (Agoda, MNCs, BOI companies, international tech hubs).
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..schema import Vacancy

logger = logging.getLogger("linkedin_bangkok")

EXPAT_QUERIES = [
    '"relocation provided" IT',
    '"relocation support" IT',
    '"visa sponsorship" IT',
    '"work permit" IT',
    "Agoda DevOps",
    "Agoda Systems",
    "Agoda IT Support",
    "Agoda Infrastructure",
    "English speaking IT Support",
    "System Administrator English",
    "Network Engineer English",
    "Linux Administrator English",
]

LOCATION_BANGKOK = "Bangkok, Bangkok City, Thailand"


class LinkedInBangkokAdapter:
    source = "linkedin_bangkok"

    def __init__(self, user_agent: str | None = None):
        self.headers = {
            "User-Agent": user_agent
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def fetch_query(self, query: str, start: int = 0) -> list[Vacancy]:
        """Fetch Bangkok expat job postings for a given query."""
        enc_query = urllib.parse.quote(query)
        enc_loc = urllib.parse.quote(LOCATION_BANGKOK)
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={enc_query}&location={enc_loc}&start={start}"

        req = urllib.request.Request(url, headers=self.headers)
        vacancies: list[Vacancy] = []

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(f"LinkedIn Bangkok search '{query}' failed: {e}")
            return []

        titles = re.findall(r'<h3 class="base-search-card__title">\s*([^<]+)\s*</h3>', content)
        companies = re.findall(r'<h4 class="base-search-card__subtitle">\s*<a[^>]*>\s*([^<]+)\s*</a>', content)
        links = re.findall(r'<a class="base-card__full-link[^"]*" href="([^"?]+)', content)
        locs = re.findall(r'<span class="job-search-card__location">\s*([^<]+)\s*</span>', content)

        for i in range(min(len(titles), len(companies), len(links))):
            raw_title = html_lib.unescape(titles[i].strip())
            raw_company = html_lib.unescape(companies[i].strip())
            job_url = links[i].strip()
            loc_str = html_lib.unescape(locs[i].strip()) if i < len(locs) else "Bangkok, Thailand"

            # Extract job ID
            id_m = re.search(r"-(\d{8,12})$", job_url)
            job_id = id_m.group(1) if id_m else str(hash(job_url))

            # Expat indicator
            title_low = raw_title.lower()
            is_expat = any(
                w in title_low
                for w in [
                    "relocation", "relocation provided", "visa", "english", "expat", "agoda"
                ]
            ) or "agoda" in raw_company.lower()

            vac = Vacancy(
                source="linkedin_bangkok",
                source_job_id=job_id,
                title=raw_title,
                company=raw_company,
                description=f"Expat/Foreigner vacancy in Bangkok: {raw_title} at {raw_company}. Location: {loc_str}.",
                location=f"{loc_str} (Bangkok / Expat-friendly)",
                salary_currency="THB",
                employment_type="full",
                job_url=job_url,
                published_at=datetime.now(timezone.utc),
                raw_data={
                    "query": query,
                    "location_raw": loc_str,
                    "is_expat_friendly": True,
                    "offers_visa_or_work_permit": is_expat,
                },
            )
            vacancies.append(vac)

        return vacancies

    def fetch_all(self, queries: list[str] | None = None) -> list[Vacancy]:
        """Fetch all Bangkok expat positions across target queries."""
        target_queries = queries or EXPAT_QUERIES
        all_vacs: list[Vacancy] = []
        seen_ids = set()

        for q in target_queries:
            vacs = self.fetch_query(q)
            for v in vacs:
                if v.source_job_id not in seen_ids:
                    seen_ids.add(v.source_job_id)
                    all_vacs.append(v)

        logger.info(f"LinkedIn Bangkok (Expat/Foreigner): found {len(all_vacs)} unique jobs.")
        return all_vacs
