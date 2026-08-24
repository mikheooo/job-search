from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _norm_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        # comma-separated?
        if "," in values:
            return [v.strip() for v in values.split(",") if v.strip()]
        return [values.strip()] if values.strip() else []
    result: List[str] = []
    for v in values:
        s = str(v).strip()
        if s:
            result.append(s)
    return result


def _norm_lower_list(values: Any) -> List[str]:
    return [v.lower() for v in _norm_list(values)]


@dataclass
class CandidateProfile:
    # role
    desired_roles: List[str] = field(default_factory=list)
    alternative_roles: List[str] = field(default_factory=list)
    # skills
    skills: List[str] = field(default_factory=list)
    # seniority
    preferred_seniority: List[str] = field(default_factory=list)
    years_experience: Optional[int] = None
    # location
    remote_required: bool = False
    allowed_locations: List[str] = field(default_factory=list)
    allowed_timezones: List[str] = field(default_factory=list)
    # other
    languages: List[str] = field(default_factory=list)
    employment_types: List[str] = field(default_factory=list)
    minimum_salary: Optional[float] = None
    salary_currency: Optional[str] = None
    # exclusions (hard)
    excluded_roles: List[str] = field(default_factory=list)
    excluded_companies: List[str] = field(default_factory=list)
    excluded_countries: List[str] = field(default_factory=list)
    excluded_industries: List[str] = field(default_factory=list)

    # normalized lower-case caches (filled in __post_init__)
    _desired_roles_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _alternative_roles_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _skills_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _seniority_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _allowed_locations_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _allowed_timezones_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _languages_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _employment_types_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _excluded_roles_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _excluded_companies_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _excluded_countries_lc: List[str] = field(init=False, repr=False, default_factory=list)
    _excluded_industries_lc: List[str] = field(init=False, repr=False, default_factory=list)

    def __post_init__(self) -> None:
        self.desired_roles = _norm_list(self.desired_roles)
        self.alternative_roles = _norm_list(self.alternative_roles)
        self.skills = _norm_list(self.skills)
        self.preferred_seniority = _norm_list(self.preferred_seniority)
        self.allowed_locations = _norm_list(self.allowed_locations)
        self.allowed_timezones = _norm_list(self.allowed_timezones)
        self.languages = _norm_list(self.languages)
        self.employment_types = _norm_list(self.employment_types)
        self.excluded_roles = _norm_list(self.excluded_roles)
        self.excluded_companies = _norm_list(self.excluded_companies)
        self.excluded_countries = _norm_list(self.excluded_countries)
        self.excluded_industries = _norm_list(self.excluded_industries)

        if self.salary_currency:
            self.salary_currency = str(self.salary_currency).strip().upper() or None
        if self.minimum_salary is not None:
            try:
                self.minimum_salary = float(self.minimum_salary)
            except Exception:
                self.minimum_salary = None

        if self.years_experience is not None:
            try:
                self.years_experience = int(self.years_experience)
            except Exception:
                self.years_experience = None

        self.remote_required = bool(self.remote_required)

        # lower caches
        self._desired_roles_lc = [x.lower() for x in self.desired_roles]
        self._alternative_roles_lc = [x.lower() for x in self.alternative_roles]
        self._skills_lc = [x.lower() for x in self.skills]
        self._seniority_lc = [x.lower() for x in self.preferred_seniority]
        self._allowed_locations_lc = [x.lower() for x in self.allowed_locations]
        self._allowed_timezones_lc = [x.lower() for x in self.allowed_timezones]
        self._languages_lc = [x.lower() for x in self.languages]
        self._employment_types_lc = [x.lower() for x in self.employment_types]
        self._excluded_roles_lc = [x.lower() for x in self.excluded_roles]
        self._excluded_companies_lc = [x.lower() for x in self.excluded_companies]
        self._excluded_countries_lc = [x.lower() for x in self.excluded_countries]
        self._excluded_industries_lc = [x.lower() for x in self.excluded_industries]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CandidateProfile":
        # support alias fields for backward compat / env style
        desired = data.get("desired_roles") or data.get("desiredRoles") or []
        alternative = data.get("alternative_roles") or data.get("alternativeRoles") or []
        skills = data.get("skills") or []
        seniority = data.get("preferred_seniority") or data.get("seniority") or data.get("experience") or []
        yrs = data.get("years_experience") or data.get("yearsExperience")
        remote = data.get("remote_required")
        if remote is None:
            remote = data.get("remoteRequired")
        if remote is None:
            remote = False
        allowed_loc = data.get("allowed_locations") or data.get("allowedLocations") or data.get("countries") or []
        allowed_tz = data.get("allowed_timezones") or data.get("allowedTimezones") or data.get("timezones") or []
        langs = data.get("languages") or []
        emp = data.get("employment_types") or data.get("employmentTypes") or []
        # salary aliases
        min_salary = data.get("minimum_salary")
        if min_salary is None:
            min_salary = data.get("minimumSalary")
        if min_salary is None:
            min_salary = data.get("salary_min")
        if min_salary is None:
            min_salary = data.get("min_salary")
        curr = data.get("salary_currency") or data.get("salaryCurrency") or data.get("currency")

        return cls(
            desired_roles=_norm_list(desired),
            alternative_roles=_norm_list(alternative),
            skills=_norm_list(skills),
            preferred_seniority=_norm_list(seniority),
            years_experience=yrs,
            remote_required=bool(remote) if isinstance(remote, bool) else str(remote).lower() in ("1", "true", "yes") if remote not in (None, "") else False,
            allowed_locations=_norm_list(allowed_loc),
            allowed_timezones=_norm_list(allowed_tz),
            languages=_norm_list(langs),
            employment_types=_norm_list(emp),
            minimum_salary=min_salary,
            salary_currency=curr,
            excluded_roles=_norm_list(data.get("excluded_roles") or data.get("excludedRoles") or []),
            excluded_companies=_norm_list(data.get("excluded_companies") or data.get("excludedCompanies") or []),
            excluded_countries=_norm_list(data.get("excluded_countries") or data.get("excludedCountries") or []),
            excluded_industries=_norm_list(data.get("excluded_industries") or data.get("excludedIndustries") or []),
        )

    @classmethod
    def from_json_file(cls, path: str | os.PathLike) -> "CandidateProfile":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Candidate profile file not found: {p}")
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"Profile file must contain JSON object, got {type(data)}")
        return cls.from_dict(data)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "desired_roles": self.desired_roles,
            "alternative_roles": self.alternative_roles,
            "skills": self.skills,
            "preferred_seniority": self.preferred_seniority,
            "years_experience": self.years_experience,
            "remote_required": self.remote_required,
            "allowed_locations": self.allowed_locations,
            "allowed_timezones": self.allowed_timezones,
            "languages": self.languages,
            "employment_types": self.employment_types,
            "minimum_salary": self.minimum_salary,
            "salary_currency": self.salary_currency,
            "excluded_roles": self.excluded_roles,
            "excluded_companies": self.excluded_companies,
            "excluded_countries": self.excluded_countries,
            "excluded_industries": self.excluded_industries,
        }


# Default search locations (project root and ai_assistant folder)
DEFAULT_PROFILE_PATHS = [
    Path("candidate_profile.json"),
    Path("ai_assistant/candidate_profile.json"),
    Path(__file__).parent / "candidate_profile.json",
]


def load_candidate_profile(path: Optional[str | os.PathLike] = None) -> CandidateProfile:
    """Load profile from explicit path, env var, or default locations.
    Falls back to a sensible default profile if nothing found.
    """
    # explicit
    if path:
        return CandidateProfile.from_json_file(path)
    # env
    env_path = os.getenv("CANDIDATE_PROFILE") or os.getenv("CANDIDATE_PROFILE_FILE")
    if env_path:
        return CandidateProfile.from_json_file(env_path)
    # default locations
    for p in DEFAULT_PROFILE_PATHS:
        if p.exists():
            return CandidateProfile.from_json_file(p)
    # fallback: try to locate from repo root (two levels)
    # if still not found, return default hard-coded profile that mirrors cli.py legacy
    return CandidateProfile(
        desired_roles=["AI Automation Engineer", "n8n", "automation", "python"],
        alternative_roles=["Python Developer", "AI Engineer", "Automation Engineer"],
        skills=["python", "n8n", "automation", "ai agents", "llm", "api", "telegram"],
        preferred_seniority=["mid", "middle", "senior"],
        years_experience=3,
        remote_required=True,
        allowed_locations=["Remote", "Worldwide", "EU", "USA", "anywhere"],
        allowed_timezones=[],
        languages=["en", "ru", "english", "russian"],
        employment_types=["Full Time", "Contract", "Freelance", "Part Time"],
        minimum_salary=1500,
        salary_currency="USD",
        excluded_roles=["1c", "php", "bitrix", "ruby", "java", "1с"],
        excluded_companies=[],
        excluded_countries=["china"],
        excluded_industries=["gambling", "casino", "adult"],
    )
