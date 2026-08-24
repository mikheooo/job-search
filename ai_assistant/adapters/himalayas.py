from __future__ import annotations

from typing import List
import requests

from ..schema import Vacancy


class HimalayasAdapter:
    source = "himalayas"
    api_url = "https://himalayas.app/jobs/api"

    def fetch_vacancies(self, limit: int = 100, offset: int = 0) -> List[Vacancy]:
        response = requests.get(
            self.api_url,
            params={"limit": limit, "offset": offset},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        results: List[Vacancy] = []
        for item in payload.get("jobs", []):
            source_job_id = str(item.get("guid") or item.get("applicationLink") or item.get("title"))
            job_url = item.get("guid") or item.get("applicationLink") or ""
            application_url = item.get("applicationLink") or job_url or None

            published_at = None
            pub = item.get("pubDate")
            if pub:
                try:
                    published_at = __import__("datetime").datetime.utcfromtimestamp(int(pub))
                except Exception:
                    published_at = None

            salary_currency = item.get("currency")
            salary_min = item.get("minSalary")
            salary_max = item.get("maxSalary")

            results.append(
                Vacancy(
                    source=self.source,
                    source_job_id=source_job_id,
                    title=item.get("title", ""),
                    company=item.get("companyName", ""),
                    description=item.get("description", ""),
                    job_url=job_url,
                    application_url=application_url,
                    location=", ".join(item.get("locationRestrictions") or []) or None,
                    country_restrictions=item.get("locationRestrictions") or [],
                    timezone_restrictions=[str(tz) for tz in (item.get("timezoneRestrictions") or [])],
                    salary_min=float(salary_min) if salary_min is not None else None,
                    salary_max=float(salary_max) if salary_max is not None else None,
                    salary_currency=str(salary_currency) if salary_currency else None,
                    employment_type=item.get("employmentType"),
                    published_at=published_at,
                    raw_data=item,
                )
            )
        return results
