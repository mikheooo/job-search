"""Hirify Adapter.

Fetches remote IT vacancies from Hirify API (https://api.hirify.me/api/vacancies).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..normalizer import normalize_salary_text
from ..schema import Vacancy

logger = logging.getLogger("hirify_adapter")


class HirifyAdapter:
    source = "hirify"
    base_api_url = "https://api.hirify.me/api/vacancies"

    def __init__(self, user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"):
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}

    def fetch_vacancies(
        self,
        search_query: str = "devops",
        remote_only: bool = True,
        max_pages: int = 2
    ) -> list[Vacancy]:
        """Fetch vacancies matching query from Hirify REST API."""
        results: list[Vacancy] = []

        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"page": page}
            if search_query:
                params["search"] = search_query
            if remote_only:
                params["work_format[]"] = "remote"

            url = f"{self.base_api_url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    items = payload.get("data") or []
                    if not items:
                        break

                    for item in items:
                        vid = str(item.get("id") or "")
                        title = item.get("title") or ""
                        company = item.get("company_title") or "Компания"
                        if company.startswith("%") and company.endswith("%"):
                            company = "Компания"

                        work_format = item.get("work_format") or []
                        is_remote = "remote" in [str(w).lower() for w in work_format]
                        if remote_only and not is_remote:
                            continue

                        # Extract salary
                        sal_obj = item.get("salary") or {}
                        salary_min = sal_obj.get("min")
                        salary_max = sal_obj.get("max")
                        salary_curr = sal_obj.get("currency") or "RUB"
                        if sal_obj.get("salary_period") == "hour":
                            # Hourly rate in foreign currency or ruble
                            if salary_min:
                                salary_min = int(salary_min * 160)
                            if salary_max:
                                salary_max = int(salary_max * 160)

                        job_url = item.get("apply_url") or f"https://hirify.me/vacancies/{vid}"

                        tags = [t.get("name") for t in (item.get("tags") or []) if t.get("name")]
                        specs = [s.get("name") for s in (item.get("specializations") or []) if s.get("name")]
                        desc_parts = [title, f"Компания: {company}"]
                        if tags:
                            desc_parts.append(f"Теги: {', '.join(tags)}")
                        if specs:
                            desc_parts.append(f"Направления: {', '.join(specs)}")
                        description = "\n".join(desc_parts)

                        published_at = None
                        created_str = item.get("created_at") or item.get("updated_at")
                        if created_str:
                            try:
                                dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                                published_at = dt.astimezone(timezone.utc)
                            except Exception:
                                published_at = None

                        vac = Vacancy(
                            source=self.source,
                            source_job_id=vid,
                            title=title,
                            company=company,
                            description=description,
                            location="Удаленно" if is_remote else (item.get("remote_type") or "РФ"),
                            salary_min=salary_min,
                            salary_max=salary_max,
                            salary_currency=salary_curr,
                            employment_type="full" if "fulltime" in str(item.get("work_type", "")).lower() else "any",
                            job_url=job_url,
                            published_at=published_at,
                            raw_data=item,
                        )
                        results.append(vac)

            except Exception as e:
                logger.warning(f"Hirify API fetch failed for page {page}: {e}")
                break

        return results
