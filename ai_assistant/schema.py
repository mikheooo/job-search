from __future__ import annotations

from datetime import datetime
from typing import Any


class Vacancy:
    def __init__(
        self,
        source: str,
        source_job_id: str,
        title: str,
        company: str,
        description: str,
        job_url: str,
        application_url: str | None = None,
        location: str | None = None,
        country_restrictions: list[str] | None = None,
        timezone_restrictions: list[int] | None = None,
        salary_min: float | None = None,
        salary_max: float | None = None,
        salary_currency: str | None = None,
        employment_type: str | None = None,
        published_at: datetime | None = None,
        first_seen_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        raw_data: dict[str, Any] | None = None,
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

    def to_dict(self) -> dict[str, Any]:
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
    def from_dict(cls, data: dict[str, Any]) -> Vacancy:
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


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


ACTIVE_PRODUCTION_SOURCES: set[str] = {"himalayas", "weworkremotely", "remoteok", "habrcareer", "hh"}

SYNTHETIC_ID_PATTERNS = [
    r"dryrun", r"dry-run", r"dry_run", r"^test[-_]?\d+$", r"^fake", r"^mock",
    r"^fixture", r"hard-rej", r"llm-fail", r"llm-retry", r"sync-fail"
]
SYNTHETIC_COMPANIES = {"acme", "testco", "legacyco", "fakeco", "dummyco", "mockco"}
SYNTHETIC_DOMAINS = {"example.com", "localhost", "127.0.0.1", "test.com"}
SYNTHETIC_TITLES = {"dry run job", "dry run", "test job", "fake job", "dummy job", "mock job"}


def is_genuine_production_vacancy(vacancy: Any) -> tuple[bool, str]:
    """Verify that a vacancy originated from an active production source and is not a synthetic/test/dry-run artifact."""
    import os
    import re
    
    src = (getattr(vacancy, "source", None) or "").strip().lower()
    sjid = (getattr(vacancy, "source_job_id", None) or "").strip().lower()
    url = (getattr(vacancy, "job_url", None) or "").strip().lower()
    comp = (getattr(vacancy, "company", None) or "").strip().lower()
    tit = (getattr(vacancy, "title", None) or "").strip().lower()

    # 1. Hard-isolated legacy or fixture sources are ALWAYS excluded in all environments
    if src in ("vacancies_json", "x"):
        return False, f"Isolated legacy/fixture source: {src}"

    # 2. Strict synthetic dry-run & test-fixture markers are ALWAYS excluded in all environments
    strict_synthetic_pats = [r"dryrun", r"dry-run", r"dry_run", r"hard-rej", r"llm-fail", r"llm-retry", r"sync-fail"]
    for pat in strict_synthetic_pats:
        if re.search(pat, sjid):
            return False, f"Synthetic ID pattern matched: {pat}"

    if "/dryrun" in url:
        return False, "Synthetic URL path /dryrun"

    if comp == "acme" or tit in ("dry run job", "dry run"):
        return False, "Synthetic dry-run job/company"

    is_test_env = bool(os.environ.get("JOB_SEARCH_TEST_NETWORK_BLOCKED"))

    # 3. In production environment (outside pytest), enforce strict production provenance
    if not is_test_env:
        if src not in ACTIVE_PRODUCTION_SOURCES:
            return False, f"Source '{src}' is not an active production source"

        for dom in SYNTHETIC_DOMAINS:
            if dom in url:
                return False, f"Synthetic domain: {dom}"

        for pat in (r"^test[-_]?\d+$", r"^fake", r"^mock", r"^fixture"):
            if re.search(pat, sjid):
                return False, f"Synthetic test ID pattern: {pat}"

        if comp in SYNTHETIC_COMPANIES:
            return False, f"Synthetic company: {comp}"

        if tit in SYNTHETIC_TITLES:
            return False, f"Synthetic title: {tit}"

        if not (url.startswith("http://") or url.startswith("https://")):
            return False, "Invalid URL scheme"

        return True, "GENUINE_PRODUCTION"

    # 3. Source qualification
    if src in ACTIVE_PRODUCTION_SOURCES:
        if not (url.startswith("http://") or url.startswith("https://")):
            return False, "Invalid URL scheme"
        return True, "GENUINE_PRODUCTION"

    # 4. In test environment (inside pytest), permit mock fixtures ONLY for explicit test sources ("test", "test_src")
    if is_test_env and src in ("test", "test_src"):
        return True, "TEST_ENVIRONMENT_MOCK"

    return False, f"Source '{src}' is not an active production source"
