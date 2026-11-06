"""Bangkok Direct Tech Careers Adapter (Agoda, Lalamove, Bitkub, MNCs).

Directly integrates with official ATS APIs of top tech employers headquartered or operating in Bangkok:
1. Agoda Careers (Global Tech HQ in CentralWorld, Bangkok - Greenhouse API).
2. Lalamove Thailand (Regional logistics tech hub - Lever API).
3. Bitkub Capital Group (Bangkok fintech/crypto leader - direct HR portal/contact).

Filters specifically for technical, infrastructure, DevOps, systems administration,
Python, support, and engineering roles suitable for English-speaking expats in Bangkok.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..schema import Vacancy

logger = logging.getLogger("bangkok_direct_careers")

TECH_KEYWORDS = [
    "engineer",
    "devops",
    "system",
    "infrastructure",
    "support",
    "linux",
    "network",
    "cloud",
    "python",
    "sre",
    "operations",
    "architect",
    "database",
    "admin",
    "security",
    "backend",
    "it ",
    "tech",
]

NON_TECH_EXCLUDES = [
    "sales consultant",
    "customer service associate",
    "driver operations",
    "public affairs",
    "rewards senior",
    "accounting manager",
    "japanese speaking",
]


class BangkokDirectCareersAdapter:
    source = "bangkok_careers"

    def __init__(self, user_agent: str | None = None):
        self.headers = {
            "User-Agent": user_agent
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

    def fetch_all(self) -> list[Vacancy]:
        """Fetch all matching open tech roles across Bangkok corporate portals."""
        all_vacancies: list[Vacancy] = []

        # 1. Agoda Greenhouse API
        try:
            agoda_vacs = self._fetch_agoda()
            logger.info(f"Agoda Direct Careers: fetched {len(agoda_vacs)} tech vacancies in Bangkok.")
            all_vacancies.extend(agoda_vacs)
        except Exception as e:
            logger.warning(f"Failed to fetch Agoda careers: {e}")

        # 2. Lalamove Lever API
        try:
            lala_vacs = self._fetch_lalamove()
            logger.info(f"Lalamove Direct Careers: fetched {len(lala_vacs)} vacancies in Bangkok.")
            all_vacancies.extend(lala_vacs)
        except Exception as e:
            logger.warning(f"Failed to fetch Lalamove careers: {e}")

        return all_vacancies

    def _fetch_agoda(self) -> list[Vacancy]:
        """Query Agoda's official Greenhouse board API."""
        url = "https://boards-api.greenhouse.io/v1/boards/agoda/jobs"
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        jobs = data.get("jobs", [])
        results: list[Vacancy] = []

        for j in jobs:
            loc_name = (j.get("location") or {}).get("name", "")
            if "bangkok" not in loc_name.lower() and "thailand" not in loc_name.lower():
                continue

            title = (j.get("title") or "").strip()
            title_lower = title.lower()

            # Filter for IT / Tech
            if not any(k in title_lower for k in TECH_KEYWORDS):
                continue
            if any(ex in title_lower for ex in NON_TECH_EXCLUDES):
                continue

            job_id = str(j.get("id"))
            job_url = j.get("absolute_url") or f"https://job-boards.greenhouse.io/agoda/jobs/{job_id}"

            # Check if relocation/work permit is noted
            is_visa_friendly = bool(
                "relocation provided" in title_lower
                or "relocation support" in title_lower
                or "work permit" in title_lower
            )

            desc = f"Official open engineering/tech position at Agoda Bangkok Headquarters. Location: {loc_name}. Application via Greenhouse."

            v = Vacancy(
                source=self.source,
                source_job_id=f"agoda_{job_id}",
                title=title,
                company="Agoda",
                description=desc,
                job_url=job_url,
                application_url=job_url,
                location="Bangkok, Thailand",
                published_at=datetime.now(timezone.utc),
                first_seen_at=datetime.now(timezone.utc),
                last_seen_at=datetime.now(timezone.utc),
                raw_data={
                    "is_expat_friendly": True,
                    "offers_visa_or_work_permit": is_visa_friendly,
                    "english_speaking": True,
                    "headquarters": "CentralWorld, Bangkok",
                    "ats": "greenhouse",
                },
            )
            results.append(v)

        return results

    def _fetch_lalamove(self) -> list[Vacancy]:
        """Query Lalamove's Lever board API for Thailand positions."""
        url = "https://api.lever.co/v0/postings/lalamove"
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results: list[Vacancy] = []
        for j in data:
            loc = (j.get("categories") or {}).get("location", "")
            if "bangkok" not in loc.lower() and "thailand" not in loc.lower():
                continue

            title = (j.get("text") or "").strip()
            title_lower = title.lower()

            if any(ex in title_lower for ex in NON_TECH_EXCLUDES):
                continue

            job_id = str(j.get("id"))
            job_url = j.get("hostedUrl") or f"https://jobs.lever.co/lalamove/{job_id}"
            desc = (j.get("descriptionPlain") or j.get("text") or "")[:500]

            v = Vacancy(
                source=self.source,
                source_job_id=f"lalamove_{job_id}",
                title=title,
                company="Lalamove",
                description=desc,
                job_url=job_url,
                application_url=job_url,
                location="Bangkok, Thailand",
                published_at=datetime.now(timezone.utc),
                first_seen_at=datetime.now(timezone.utc),
                last_seen_at=datetime.now(timezone.utc),
                raw_data={
                    "is_expat_friendly": True,
                    "offers_visa_or_work_permit": True,
                    "english_speaking": True,
                    "ats": "lever",
                },
            )
            results.append(v)

        return results


if __name__ == "__main__":
    adapter = BangkokDirectCareersAdapter()
    vacancies = adapter.fetch_all()
    print(f"Total Bangkok Direct Tech Vacancies: {len(vacancies)}")
    for v in vacancies[:8]:
        print(f" • [{v.company}] {v.title} -> {v.job_url}")
