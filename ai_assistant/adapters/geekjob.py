"""GeekJob Vacancy Adapter.

Fetches remote IT vacancies from GeekJob (https://geekjob.ru).
Parses vacancy listings across remote pages, normalizes salary and metadata.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..normalizer import normalize_salary_text
from ..schema import Vacancy

logger = logging.getLogger("geekjob_adapter")


class GeekJobAdapter:
    source = "geekjob"

    def __init__(self, user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"):
        self.headers = {"User-Agent": user_agent}

    def fetch_page(self, page: int = 1) -> list[Vacancy]:
        """Fetch remote vacancies from a specific page."""
        url = f"https://geekjob.ru/vacancies?format=remote&page={page}"
        req = urllib.request.Request(url, headers=self.headers)
        vacancies: list[Vacancy] = []

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                page_html = resp.read().decode("utf-8")
        except Exception as e:
            logger.warning(f"GeekJob page {page} fetch failed: {e}")
            return []

        cards = re.findall(r'<li class="collection-item avatar[^"]*"[^>]*>(.*?)</li>', page_html, re.DOTALL)

        for card in cards:
            # Match title and link
            title_m = re.search(r'href="(/vacancy/([a-zA-Z0-9]+))"[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</a>', card)
            if not title_m:
                title_m = re.search(r'class="[^"]*title[^"]*"[^>]*href="(/vacancy/([a-zA-Z0-9]+))"[^>]*>([^<]+)</a>', card)

            if not title_m:
                continue

            rel_url = title_m.group(1)
            job_id = title_m.group(2)
            title = html_lib.unescape(title_m.group(3).strip())

            # Company name
            comp_m = re.search(r'class="[^"]*company-name[^"]*"[^>]*><a[^>]*>([^<]+)</a>', card)
            company = html_lib.unescape(comp_m.group(1).strip()) if comp_m else "Компания"

            # Salary
            sal_m = re.search(r'class="salary">([^<]*)</span>', card)
            sal_text = html_lib.unescape(sal_m.group(1).strip()) if sal_m else ""
            sal_info = normalize_salary_text(sal_text) if sal_text else {}

            # Location
            loc_m = re.search(r'<div class="info"><a[^>]*>(.*?)<br>', card, re.DOTALL)
            loc_text = html_lib.unescape(re.sub(r'<[^>]+>', '', loc_m.group(1)).strip()) if loc_m else "Удаленно"

            job_url = f"https://geekjob.ru{rel_url}"

            vac = Vacancy(
                source="geekjob",
                source_job_id=job_id,
                title=title,
                company=company,
                description=f"Вакансия на GeekJob: {title} в {company}. Формат: Удаленно ({loc_text}). Зарплата: {sal_text or 'По договоренности'}.",
                location="Удаленно (РФ/СНГ)",
                salary_min=sal_info.get("salary_min"),
                salary_max=sal_info.get("salary_max"),
                salary_currency=sal_info.get("salary_currency", "RUB"),
                employment_type="full",
                job_url=job_url,
                published_at=datetime.now(timezone.utc),
                raw_data={"raw_salary": sal_text, "location": loc_text},
            )
            vacancies.append(vac)

        return vacancies

    def fetch_all(self, max_pages: int = 2) -> list[Vacancy]:
        """Fetch remote vacancies across pages."""
        all_vacs: list[Vacancy] = []
        seen_ids = set()

        for page in range(1, max_pages + 1):
            vacs = self.fetch_page(page)
            for v in vacs:
                if v.source_job_id not in seen_ids:
                    seen_ids.add(v.source_job_id)
                    all_vacs.append(v)

        logger.info(f"GeekJob: fetched {len(all_vacs)} unique remote vacancies across {max_pages} pages.")
        return all_vacs
