from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Dict, Any


class Vacancy:
    def __init__(
        self,
        source: str,
        source_job_id: str,
        title: str,
        company: str,
        description: str,
        job_url: str,
        application_url: Optional[str] = None,
        location: Optional[str] = None,
        country_restrictions: Optional[List[str]] = None,
        timezone_restrictions: Optional[List[int]] = None,
        salary_min: Optional[float] = None,
        salary_max: Optional[float] = None,
        salary_currency: Optional[str] = None,
        employment_type: Optional[str] = None,
        published_at: Optional[datetime] = None,
        first_seen_at: Optional[datetime] = None,
        last_seen_at: Optional[datetime] = None,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.source = source
        self.source_job_id = source_job_id
        self.title = title
        self.company = company
        self.description = description
        self.job_url = job_url
        self.application_url = application_url
        self.location = location
        self.country_restrictions = country_restrictions or []
        self.timezone_restrictions = timezone_restrictions or []
        self.salary_min = salary_min
        self.salary_max = salary_max
        self.salary_currency = salary_currency
        self.employment_type = employment_type
        self.published_at = published_at
        self.first_seen_at = first_seen_at or datetime.utcnow()
        self.last_seen_at = last_seen_at or datetime.utcnow()
        self.raw_data = raw_data or {}

    def stable_id(self) -> str:
        return f"{self.source}:{self.source_job_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.stable_id(),
            "source": self.source,
            "source_job_id": self.source_job_id,
            "title": self.title,
            "company": self.company,
            "description": self.description,
            "location": self.location,
            "country_restrictions": self.country_restrictions,
            "timezone_restrictions": self.timezone_restrictions,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "employment_type": self.employment_type,
            "job_url": self.job_url,
            "application_url": self.application_url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "raw_data": self.raw_data,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Vacancy:
        return cls(
            source=data["source"],
            source_job_id=data["source_job_id"],
            title=data["title"],
            company=data["company"],
            description=data["description"],
            job_url=data["job_url"],
            application_url=data.get("application_url"),
            location=data.get("location"),
            country_restrictions=data.get("country_restrictions") or [],
            timezone_restrictions=data.get("timezone_restrictions") or [],
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            salary_currency=data.get("salary_currency"),
            employment_type=data.get("employment_type"),
            published_at=_parse_dt(data.get("published_at")),
            first_seen_at=_parse_dt(data.get("first_seen_at")),
            last_seen_at=_parse_dt(data.get("last_seen_at")),
            raw_data=data.get("raw_data") or {},
        )


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
