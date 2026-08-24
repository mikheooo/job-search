from __future__ import annotations

from typing import List, Optional
from datetime import datetime, timezone

from ..schema import Vacancy


class RemoteOkAdapter:
    source = "remoteok"
    api_url = "https://remoteok.com/api"

    def fetch_vacancies(self) -> List[Vacancy]:
        import requests

        response = requests.get(self.api_url, timeout=20)
        response.raise_for_status()
        payload = response.json()
        items = payload[1:] if payload and isinstance(payload[0], dict) and "legal" in payload[0] else payload

        results: List[Vacancy] = []
        for item in items:
            source_job_id = str(item.get("id") or item.get("slug") or item.get("url"))
            job_url = item.get("url") or item.get("apply_url") or f"https://remoteok.com/remote-jobs/{source_job_id}"
            application_url = item.get("apply_url") or job_url or None

            salary_min = item.get("salary_min")
            salary_max = item.get("salary_max")
            if isinstance(salary_min, str) and salary_min.isdigit():
                salary_min = int(salary_min)
            if isinstance(salary_max, str) and salary_max.isdigit():
                salary_max = int(salary_max)

            published_at = self._parse_date(item.get("date") or item.get("last_updated"))

            results.append(
                Vacancy(
                    source=self.source,
                    source_job_id=source_job_id,
                    title=item.get("position") or item.get("title") or "",
                    company=item.get("company") or "",
                    description=item.get("description") or "",
                    job_url=job_url,
                    application_url=application_url,
                    location=item.get("location") or None,
                    country_restrictions=[],
                    timezone_restrictions=[],
                    salary_min=float(salary_min) if salary_min is not None else None,
                    salary_max=float(salary_max) if salary_max is not None else None,
                    salary_currency="USD",
                    employment_type=self._infer_employment(item.get("tags") or []),
                    published_at=published_at,
                    raw_data=item,
                )
            )
        return results

    @staticmethod
    def _parse_date(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except Exception:
                return None
        text = str(value)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    @staticmethod
    def _infer_employment(tags: List[str]) -> Optional[str]:
        joined = ", ".join(tags).lower()
        if "full-time" in joined or "full time" in joined:
            return "Full Time"
        if "contract" in joined:
            return "Contract"
        if "part-time" in joined or "part time" in joined:
            return "Part Time"
        return None
