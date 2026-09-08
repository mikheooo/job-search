from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _norm_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        # comma-separated?
        if "," in values:
            return [v.strip() for v in values.split(",") if v.strip()]
        return [values.strip()] if values.strip() else []
    result: list[str] = []
    for v in values:
        s = str(v).strip()
        if s:
            result.append(s)
    return result


def _norm_lower_list(values: Any) -> list[str]:
    return [v.lower() for v in _norm_list(values)]


@dataclass
class CandidateProfile:
    # --- Role & Targeting ---
    target_roles: list[str] = field(default_factory=list)
    desired_roles: list[str] = field(default_factory=list)
    alternative_roles: list[str] = field(default_factory=list)
    role_families: list[str] = field(default_factory=list)

    # --- Skills: Confirmed vs Transferable ---
    core_skills: list[str] = field(default_factory=list)          # Confirmed / Direct demonstrated skills
    secondary_skills: list[str] = field(default_factory=list)     # Related / Transferable skills
    transferable_skills: list[str] = field(default_factory=list)  # Alias for secondary_skills
    skills: list[str] = field(default_factory=list)               # Combined / Legacy skills

    # --- Seniority & Experience ---
    seniority_range: list[str] = field(default_factory=list)
    preferred_seniority: list[str] = field(default_factory=list)
    years_experience: int | None = None
    years_of_experience: int | None = None

    # --- Location & Remote ---
    remote_required: bool = False
    allowed_locations: list[str] = field(default_factory=list)
    allowed_timezones: list[str] = field(default_factory=list)
    location_constraints: list[str] = field(default_factory=list)

    # --- Stage 87: Profile Provenance, Skill Confidence & Domain Calibration ---
    provenance: dict[str, str] = field(default_factory=dict)
    skill_confidence: dict[str, str] = field(default_factory=dict)
    role_priorities: dict[str, str] = field(default_factory=dict)
    role_family_seniority: dict[str, list[str]] = field(default_factory=dict)
    domain_years: dict[str, float] = field(default_factory=dict)
    role_specific_skills: dict[str, Any] = field(default_factory=dict)

    # --- Other Preferences ---
    languages: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    employment_type: str | None = None
    minimum_salary: float | None = None
    salary_currency: str | None = None
    salary_preferences: dict[str, Any] = field(default_factory=dict)
    industries: list[str] = field(default_factory=list)

    # --- Contact Info ---
    name: str | None = None
    email: str | None = None
    phone_ru: str | None = None
    phone_th: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None

    # --- Exclusions & Hard Gates ---
    excluded_roles: list[str] = field(default_factory=list)
    excluded_companies: list[str] = field(default_factory=list)
    excluded_countries: list[str] = field(default_factory=list)
    excluded_industries: list[str] = field(default_factory=list)
    must_avoid_conditions: list[str] = field(default_factory=list)

    # normalized lower-case caches (filled in __post_init__)
    _target_roles_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _desired_roles_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _alternative_roles_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _role_families_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _core_skills_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _secondary_skills_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _transferable_skills_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _skills_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _seniority_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _seniority_range_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _allowed_locations_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _allowed_timezones_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _languages_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _employment_types_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _industries_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _excluded_roles_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _excluded_companies_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _excluded_countries_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _excluded_industries_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _must_avoid_conditions_lc: list[str] = field(init=False, repr=False, default_factory=list)
    _skill_confidence_lc: dict[str, str] = field(init=False, repr=False, default_factory=dict)
    _role_priorities_lc: dict[str, str] = field(init=False, repr=False, default_factory=dict)
    _role_family_seniority_lc: dict[str, list[str]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        # Cross-populate target_roles / desired_roles
        norm_desired = _norm_list(self.desired_roles)
        norm_target = _norm_list(self.target_roles)
        if not norm_target and norm_desired:
            norm_target = list(norm_desired)
        elif not norm_desired and norm_target:
            norm_desired = list(norm_target)
        self.desired_roles = norm_desired
        self.target_roles = norm_target
        self.alternative_roles = _norm_list(self.alternative_roles)
        self.role_families = _norm_list(self.role_families)

        # Cross-populate core_skills / secondary_skills / skills
        norm_skills = _norm_list(self.skills)
        norm_core = _norm_list(self.core_skills)
        norm_sec = _norm_list(self.secondary_skills or self.transferable_skills)
        if not norm_core and norm_skills:
            norm_core = list(norm_skills)
        if not norm_skills and norm_core:
            norm_skills = list(norm_core)
            for s in norm_sec:
                if s not in norm_skills:
                    norm_skills.append(s)
        self.core_skills = norm_core
        self.secondary_skills = norm_sec
        self.transferable_skills = norm_sec
        self.skills = norm_skills

        # Cross-populate seniority
        norm_sen = _norm_list(self.preferred_seniority)
        norm_srange = _norm_list(self.seniority_range)
        if not norm_srange and norm_sen:
            norm_srange = list(norm_sen)
        elif not norm_sen and norm_srange:
            norm_sen = list(norm_srange)
        self.preferred_seniority = norm_sen
        self.seniority_range = norm_srange

        # Cross-populate years of experience
        yrs = self.years_experience if self.years_experience is not None else self.years_of_experience
        if yrs is not None:
            try:
                yrs = int(yrs)
            except Exception:
                yrs = None
        self.years_experience = yrs
        self.years_of_experience = yrs

        # Locations & Timezones
        self.allowed_locations = _norm_list(self.allowed_locations or self.location_constraints)
        self.location_constraints = list(self.allowed_locations)
        self.allowed_timezones = _norm_list(self.allowed_timezones)
        self.languages = _norm_list(self.languages)
        self.employment_types = _norm_list(self.employment_types or ([self.employment_type] if self.employment_type else []))
        self.industries = _norm_list(self.industries)

        # Exclusions
        self.excluded_roles = _norm_list(self.excluded_roles)
        self.excluded_companies = _norm_list(self.excluded_companies)
        self.excluded_countries = _norm_list(self.excluded_countries)
        self.excluded_industries = _norm_list(self.excluded_industries)
        self.must_avoid_conditions = _norm_list(self.must_avoid_conditions or self.excluded_roles)

        if self.salary_currency:
            self.salary_currency = str(self.salary_currency).strip().upper() or None
        if self.name:
            self.name = str(self.name).strip() or None
        if self.email:
            self.email = str(self.email).strip() or None
        if self.phone_ru:
            self.phone_ru = str(self.phone_ru).strip() or None
        if self.phone_th:
            self.phone_th = str(self.phone_th).strip() or None
        if self.phone:
            self.phone = str(self.phone).strip() or None
        if self.linkedin:
            self.linkedin = str(self.linkedin).strip() or None
        if self.github:
            self.github = str(self.github).strip() or None
        if self.portfolio:
            self.portfolio = str(self.portfolio).strip() or None
        if self.minimum_salary is not None:
            try:
                self.minimum_salary = float(self.minimum_salary)
            except Exception:
                self.minimum_salary = None

        self.remote_required = bool(self.remote_required)

        # lower caches
        self._target_roles_lc = [x.lower() for x in self.target_roles]
        self._desired_roles_lc = [x.lower() for x in self.desired_roles]
        self._alternative_roles_lc = [x.lower() for x in self.alternative_roles]
        self._role_families_lc = [x.lower() for x in self.role_families]
        self._core_skills_lc = [x.lower() for x in self.core_skills]
        self._secondary_skills_lc = [x.lower() for x in self.secondary_skills]
        self._transferable_skills_lc = [x.lower() for x in self.transferable_skills]
        self._skills_lc = [x.lower() for x in self.skills]
        self._seniority_lc = [x.lower() for x in self.preferred_seniority]
        self._seniority_range_lc = [x.lower() for x in self.seniority_range]
        self._allowed_locations_lc = [x.lower() for x in self.allowed_locations]
        self._allowed_timezones_lc = [x.lower() for x in self.allowed_timezones]
        self._languages_lc = [x.lower() for x in self.languages]
        self._employment_types_lc = [x.lower() for x in self.employment_types]
        self._industries_lc = [x.lower() for x in self.industries]
        self._excluded_roles_lc = [x.lower() for x in self.excluded_roles]
        self._excluded_companies_lc = [x.lower() for x in self.excluded_companies]
        self._excluded_countries_lc = [x.lower() for x in self.excluded_countries]
        self._excluded_industries_lc = [x.lower() for x in self.excluded_industries]
        self._must_avoid_conditions_lc = [x.lower() for x in self.must_avoid_conditions]
        
        # Lower-case / Upper-case normalized lookup maps
        self._skill_confidence_lc = {str(k).lower().strip(): str(v).upper().strip() for k, v in self.skill_confidence.items() if str(k).strip()}
        self._role_priorities_lc = {str(k).upper().strip(): str(v).upper().strip() for k, v in self.role_priorities.items() if str(k).strip()}
        self._role_family_seniority_lc = {
            str(k).upper().strip(): [str(s).lower().strip() for s in v if str(s).strip()]
            for k, v in self.role_family_seniority.items() if str(k).strip() and isinstance(v, (list, tuple, set))
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateProfile":
        desired = data.get("desired_roles") or data.get("desiredRoles") or data.get("target_roles") or data.get("targetRoles") or []
        target = data.get("target_roles") or data.get("targetRoles") or desired
        alternative = data.get("alternative_roles") or data.get("alternativeRoles") or []
        role_fams = data.get("role_families") or data.get("roleFamilies") or []
        
        # Skills
        core_sk = data.get("core_skills") or data.get("coreSkills") or []
        sec_sk = data.get("secondary_skills") or data.get("secondarySkills") or data.get("transferable_skills") or data.get("transferableSkills") or []
        all_sk = data.get("skills") or []
        if not core_sk and all_sk:
            core_sk = all_sk

        seniority = data.get("preferred_seniority") or data.get("seniority") or data.get("experience") or data.get("seniority_range") or data.get("seniorityRange") or []
        yrs = data.get("years_experience") or data.get("yearsExperience") or data.get("years_of_experience") or data.get("yearsOfExperience")
        
        # Stage 87 calibrations
        provenance = data.get("provenance") or {}
        skill_conf = data.get("skill_confidence") or data.get("skillConfidence") or {}
        role_prio = data.get("role_priorities") or data.get("rolePriorities") or {}
        role_sen = data.get("role_family_seniority") or data.get("roleFamilySeniority") or {}
        dom_years = data.get("domain_years") or data.get("domainYears") or {}
        role_spec_skills = data.get("role_specific_skills") or data.get("roleSpecificSkills") or {}

        remote = data.get("remote_required")
        if remote is None:
            remote = data.get("remoteRequired")
        if remote is None:
            remote = False
        allowed_loc = data.get("allowed_locations") or data.get("allowedLocations") or data.get("countries") or data.get("location_constraints") or []
        allowed_tz = data.get("allowed_timezones") or data.get("allowedTimezones") or data.get("timezones") or []
        langs = data.get("languages") or []
        emp = data.get("employment_types") or data.get("employmentTypes") or data.get("employment_type") or []
        inds = data.get("industries") or []
        must_avoid = data.get("must_avoid_conditions") or data.get("mustAvoidConditions") or []

        # salary aliases
        min_salary = data.get("minimum_salary")
        if min_salary is None:
            min_salary = data.get("minimumSalary")
        if min_salary is None:
            min_salary = data.get("salary_min")
        if min_salary is None:
            min_salary = data.get("min_salary")
        curr = data.get("salary_currency") or data.get("salaryCurrency") or data.get("currency")
        
        # contact fields
        name = data.get("name") or data.get("full_name") or data.get("fullName") or data.get("candidate_name")
        email = data.get("email") or data.get("mail")
        phone_ru = data.get("phone_ru") or data.get("phoneRu") or data.get("phone_russia")
        phone_th = data.get("phone_th") or data.get("phoneTh") or data.get("phone_thailand")
        phone = data.get("phone") or data.get("telephone") or data.get("mobile")
        linkedin = data.get("linkedin") or data.get("linkedin_url") or data.get("linkedIn")
        github = data.get("github") or data.get("github_url") or data.get("gitHub")
        portfolio = data.get("portfolio") or data.get("portfolio_url") or data.get("website")

        return cls(
            target_roles=_norm_list(target),
            desired_roles=_norm_list(desired),
            alternative_roles=_norm_list(alternative),
            role_families=_norm_list(role_fams),
            core_skills=_norm_list(core_sk),
            secondary_skills=_norm_list(sec_sk),
            transferable_skills=_norm_list(sec_sk),
            skills=_norm_list(all_sk or core_sk),
            seniority_range=_norm_list(seniority),
            preferred_seniority=_norm_list(seniority),
            years_experience=yrs,
            years_of_experience=yrs,
            provenance=dict(provenance) if isinstance(provenance, dict) else {},
            skill_confidence=dict(skill_conf) if isinstance(skill_conf, dict) else {},
            role_priorities=dict(role_prio) if isinstance(role_prio, dict) else {},
            role_family_seniority=dict(role_sen) if isinstance(role_sen, dict) else {},
            domain_years=dict(dom_years) if isinstance(dom_years, dict) else {},
            role_specific_skills=dict(role_spec_skills) if isinstance(role_spec_skills, dict) else {},
            remote_required=bool(remote) if isinstance(remote, bool) else str(remote).lower() in ("1", "true", "yes") if remote not in (None, "") else False,
            allowed_locations=_norm_list(allowed_loc),
            location_constraints=_norm_list(allowed_loc),
            allowed_timezones=_norm_list(allowed_tz),
            languages=_norm_list(langs),
            employment_types=_norm_list(emp),
            industries=_norm_list(inds),
            minimum_salary=min_salary,
            salary_currency=curr,
            name=str(name).strip() if name else None,
            email=str(email).strip() if email else None,
            phone_ru=str(phone_ru).strip() if phone_ru else None,
            phone_th=str(phone_th).strip() if phone_th else None,
            phone=str(phone).strip() if phone else None,
            linkedin=str(linkedin).strip() if linkedin else None,
            github=str(github).strip() if github else None,
            portfolio=str(portfolio).strip() if portfolio else None,
            excluded_roles=_norm_list(data.get("excluded_roles") or data.get("excludedRoles") or []),
            excluded_companies=_norm_list(data.get("excluded_companies") or data.get("excludedCompanies") or []),
            excluded_countries=_norm_list(data.get("excluded_countries") or data.get("excludedCountries") or []),
            excluded_industries=_norm_list(data.get("excluded_industries") or data.get("excludedIndustries") or []),
            must_avoid_conditions=_norm_list(must_avoid),
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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "target_roles": self.target_roles,
            "desired_roles": self.desired_roles,
            "alternative_roles": self.alternative_roles,
            "role_families": self.role_families,
            "core_skills": self.core_skills,
            "secondary_skills": self.secondary_skills,
            "transferable_skills": self.transferable_skills,
            "skills": self.skills,
            "seniority_range": self.seniority_range,
            "preferred_seniority": self.preferred_seniority,
            "years_of_experience": self.years_of_experience,
            "years_experience": self.years_experience,
            "provenance": self.provenance,
            "skill_confidence": self.skill_confidence,
            "role_priorities": self.role_priorities,
            "role_family_seniority": self.role_family_seniority,
            "domain_years": self.domain_years,
            "role_specific_skills": self.role_specific_skills,
            "remote_required": self.remote_required,
            "allowed_locations": self.allowed_locations,
            "location_constraints": self.location_constraints,
            "allowed_timezones": self.allowed_timezones,
            "languages": self.languages,
            "employment_types": self.employment_types,
            "industries": self.industries,
            "minimum_salary": self.minimum_salary,
            "salary_currency": self.salary_currency,
            "excluded_roles": self.excluded_roles,
            "excluded_companies": self.excluded_companies,
            "excluded_countries": self.excluded_countries,
            "excluded_industries": self.excluded_industries,
            "must_avoid_conditions": self.must_avoid_conditions,
        }
        if self.name is not None:
            d["name"] = self.name
        if self.email is not None:
            d["email"] = self.email
        if self.phone_ru is not None:
            d["phone_ru"] = self.phone_ru
        if self.phone_th is not None:
            d["phone_th"] = self.phone_th
        if self.phone is not None:
            d["phone"] = self.phone
        if self.linkedin is not None:
            d["linkedin"] = self.linkedin
        if self.github is not None:
            d["github"] = self.github
        if self.portfolio is not None:
            d["portfolio"] = self.portfolio
        return d


# Default search locations (project root and ai_assistant folder)
DEFAULT_PROFILE_PATHS = [
    Path("candidate_profile.json"),
    Path("ai_assistant/candidate_profile.json"),
    Path(__file__).parent / "candidate_profile.json",
]


def load_candidate_profile(path: str | os.PathLike | None = None) -> CandidateProfile:
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
    # fallback: return default profile
    return CandidateProfile(
        target_roles=["AI Automation Engineer", "n8n Developer", "Automation Engineer", "AI Agent Developer", "Application Support Engineer"],
        desired_roles=["AI Automation Engineer", "n8n Developer", "Automation Engineer", "AI Agent Developer", "Application Support Engineer"],
        alternative_roles=["Python Developer", "AI Engineer", "Technical Support Engineer", "Integration Engineer"],
        role_families=["AI_AUTOMATION", "APPLICATION_SUPPORT", "TECH_SUPPORT", "PYTHON_BACKEND"],
        role_priorities={
            "AI_AUTOMATION": "P1",
            "APPLICATION_SUPPORT": "P1",
            "TECH_SUPPORT": "P1",
            "PYTHON_BACKEND": "P2",
            "DATA_ENGINEERING": "P3",
            "DEVOPS_SRE": "P3",
            "SYSTEM_ADMIN": "P2",
        },
        role_family_seniority={
            "AI_AUTOMATION": ["mid", "middle", "senior"],
            "APPLICATION_SUPPORT": ["mid", "middle", "senior"],
            "TECH_SUPPORT": ["mid", "middle", "senior"],
            "SYSTEM_ADMIN": ["mid", "middle", "senior"],
            "PYTHON_BACKEND": ["junior", "mid", "middle"],
            "DATA_ENGINEERING": ["junior", "mid", "middle"],
            "DEVOPS_SRE": ["junior", "mid", "middle"],
        },
        domain_years={
            "it_support": 11.0,
            "system_admin": 9.0,
            "application_support": 5.0,
            "automation": 3.5,
            "python": 3.5,
            "ai_llm": 2.0,
        },
        core_skills=["python", "n8n", "automation", "ai agents", "rest api", "webhooks", "telegram", "active directory", "sql"],
        secondary_skills=["fastapi", "docker", "make", "linux", "bash", "powershell", "whisper", "llm", "postgresql"],
        transferable_skills=["troubleshooting", "git", "ci/cd", "itsm", "networking", "vpn", "pandas", "asyncio"],
        skills=["python", "n8n", "automation", "ai agents", "rest api", "webhooks", "telegram", "active directory", "sql"],
        provenance={
            "name": "CONFIRMED",
            "total_it_experience": "CONFIRMED",
            "python_automation_experience": "CONFIRMED",
            "application_support_experience": "CONFIRMED",
            "russian_language": "CONFIRMED",
            "english_language": "CONFIRMED",
            "location_thailand": "CONFIRMED",
            "remote_required": "CONFIRMED",
            "salary_floor": "DEFAULT",
        },
        skill_confidence={
            "python": "PROFESSIONAL",
            "n8n": "PROFESSIONAL",
            "automation": "PROFESSIONAL",
            "telegram": "PROFESSIONAL",
            "rest api": "PROFESSIONAL",
            "webhooks": "PROFESSIONAL",
            "linux": "PROFESSIONAL",
            "sql": "PROFESSIONAL",
            "active directory": "PROFESSIONAL",
            "whisper": "PROFESSIONAL",
            "make": "PROFESSIONAL",
            "docker": "PROFESSIONAL",
            "bash": "PROFESSIONAL",
            "powershell": "PROFESSIONAL",
            "fastapi": "PROJECT",
            "ai agents": "PROJECT",
            "llm": "PROJECT",
            "postgresql": "PROJECT",
            "telethon": "PROJECT",
            "aiogram": "PROJECT",
            "asyncio": "PROJECT",
            "git": "BASIC",
            "ci/cd": "BASIC",
            "pandas": "BASIC",
            "itsm": "TRANSFERABLE",
            "troubleshooting": "TRANSFERABLE",
            "vpn": "TRANSFERABLE",
            "networking": "TRANSFERABLE",
        },
        role_specific_skills={
            "AI_AUTOMATION": {
                "core": ["n8n", "python", "automation"],
                "secondary": ["ai agents", "llm", "make", "webhooks", "telegram", "whisper", "rest api", "docker", "fastapi", "sql", "git"],
            },
            "APPLICATION_SUPPORT": {
                "core": ["technical support", "troubleshooting", "sql", "active directory"],
                "secondary": ["linux", "api", "incident management", "it infrastructure", "bash", "powershell", "networking", "python", "vpn", "monitoring"],
            },
            "TECH_SUPPORT": {
                "core": ["technical support", "troubleshooting", "windows server", "active directory"],
                "secondary": ["exchange", "networking", "itsm", "linux", "bash", "powershell", "sql", "telephony", "hardware"],
            },
            "PYTHON_BACKEND": {
                "core": ["python", "fastapi", "rest api", "sql"],
                "secondary": ["docker", "asyncio", "postgresql", "git", "ci/cd", "redis", "linux"],
            },
            "SYSTEM_ADMIN": {
                "core": ["linux", "windows server", "active directory"],
                "secondary": ["bash", "powershell", "networking", "vpn", "docker", "sql", "hyper-v", "monitoring", "exchange", "backups"],
            },
            "DATA_ENGINEERING": {
                "core": ["python", "sql", "etl"],
                "secondary": ["postgresql", "docker", "rest api", "git", "linux", "bash", "pandas", "asyncio"],
            },
            "DEVOPS_SRE": {
                "core": ["linux", "docker", "bash"],
                "secondary": ["networking", "vpn", "ci/cd", "powershell", "python", "monitoring", "git"],
            },
        },
        seniority_range=["mid", "middle", "senior"],
        preferred_seniority=["mid", "middle", "senior"],
        years_experience=3,
        years_of_experience=3,
        remote_required=True,
        allowed_locations=["Remote", "Worldwide", "EU", "USA", "anywhere"],
        allowed_timezones=[],
        languages=["en", "ru", "english", "russian"],
        employment_types=["Full Time", "Contract", "Freelance", "Part Time"],
        minimum_salary=1500,
        salary_currency="USD",
        excluded_roles=["1c", "php", "bitrix", "ruby", "java developer", "c++", "c#", "senior lead", "1с"],
        excluded_companies=[],
        excluded_countries=["china"],
        excluded_industries=["gambling", "casino", "adult", "crypto scam"],
        must_avoid_conditions=["1c", "php", "bitrix", "ruby", "java developer", "c++", "c#"],
    )
