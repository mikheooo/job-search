from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .schema import Vacancy
from .remote_filter import is_strictly_remote, classify_work_format

try:
    from .candidate_profile import CandidateProfile
except Exception:
    CandidateProfile = None  # type: ignore


# =============================================================================
# ENUMS & DATA STRUCTURES
# =============================================================================

class RoleFamily(str, Enum):
    AI_AUTOMATION = "AI_AUTOMATION"
    PYTHON_BACKEND = "PYTHON_BACKEND"
    DEVOPS_SRE = "DEVOPS_SRE"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"
    TECH_SUPPORT = "TECH_SUPPORT"
    APPLICATION_SUPPORT = "APPLICATION_SUPPORT"
    QA_AUTOMATION = "QA_AUTOMATION"
    QA_MANUAL = "QA_MANUAL"
    DATA_ENGINEERING = "DATA_ENGINEERING"
    BUSINESS_ANALYST = "BUSINESS_ANALYST"
    FRONTEND_DEVELOPER = "FRONTEND_DEVELOPER"
    FULLSTACK_DEVELOPER = "FULLSTACK_DEVELOPER"
    MOBILE_DEVELOPER = "MOBILE_DEVELOPER"
    SECURITY = "SECURITY"
    OTHER = "OTHER"


class MatchDecisionClass(str, Enum):
    STRONG_MATCH = "STRONG_MATCH"
    MATCH = "MATCH"
    STRETCH = "STRETCH"
    BORDERLINE = "BORDERLINE"
    REJECT = "REJECT"


class RolePriority(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    NOT_TARGET = "NOT_TARGET"


class SkillConfidenceLevel(str, Enum):
    PROFESSIONAL = "PROFESSIONAL"
    PROJECT = "PROJECT"
    BASIC = "BASIC"
    TRANSFERABLE = "TRANSFERABLE"
    UNKNOWN = "UNKNOWN"


class HardRequirementStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    BORDERLINE = "BORDERLINE"
    INELIGIBLE = "INELIGIBLE"


@dataclass
class DimensionResult:
    score: float
    max_score: float
    evidence: List[str] = field(default_factory=list)
    confidence: str = "HIGH"  # HIGH, MEDIUM, LOW
    gaps: List[str] = field(default_factory=list)


class JobProfile:
    """Legacy compatibility JobProfile wrapper."""
    def __init__(
        self,
        desired_roles: Sequence[str] = (),
        skills: Sequence[str] = (),
        experience: Sequence[str] = (),
        seniority: Sequence[str] = (),
        salary_min: Optional[float] = None,
        salary_max: Optional[float] = None,
        salary_currency: Optional[str] = None,
        employment_types: Sequence[str] = (),
        countries: Sequence[str] = (),
        timezones: Sequence[str] = (),
        excluded_roles: Sequence[str] = (),
        excluded_countries: Sequence[str] = (),
        excluded_companies: Sequence[str] = (),
        required_countries: Sequence[str] = (),
        hard_gates: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        self.desired_roles = [str(x).strip().lower() for x in desired_roles if str(x).strip()]
        self.skills = [str(x).strip().lower() for x in skills if str(x).strip()]
        self.experience = [str(x).strip().lower() for x in experience if str(x).strip()]
        self.seniority = [str(x).strip().lower() for x in seniority if str(x).strip()]
        self.salary_min = float(salary_min) if salary_min is not None else None
        self.salary_max = float(salary_max) if salary_max is not None else None
        self.salary_currency = str(salary_currency).strip().upper() if salary_currency else None
        self.employment_types = [str(x).strip().lower() for x in employment_types if str(x).strip()]
        self.countries = [str(x).strip().lower() for x in countries if str(x).strip()]
        self.timezones = [str(x).strip().lower() for x in timezones if str(x).strip()]
        self.excluded_roles = [str(x).strip().lower() for x in excluded_roles if str(x).strip()]
        self.excluded_countries = [str(x).strip().lower() for x in excluded_countries if str(x).strip()]
        self.excluded_companies = [str(x).strip().lower() for x in excluded_companies if str(x).strip()]
        self.required_countries = [str(x).strip().lower() for x in required_countries if str(x).strip()]
        self.hard_gates = [str(x).strip().lower() for x in hard_gates if str(x).strip()]
        self.extra = kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)

    def missing_profile_fields(self) -> List[str]:
        missing = []
        if not self.desired_roles:
            missing.append("desired_roles")
        if not self.skills:
            missing.append("skills")
        if self.salary_min is None and self.salary_max is None:
            missing.append("salary_expectations")
        return missing


class MatchResult:
    def __init__(
        self,
        score: int,
        decision: str,
        reasons: List[str],
        strengths: List[str],
        gaps: List[str],
        decision_class: Optional[str] = None,
        eligibility: str = "ELIGIBLE",
        role_family: str = "OTHER",
        role_priority: str = "P1",
        dimensions: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.score = int(score)
        self.decision = str(decision)
        self.decision_class = decision_class or ("STRONG_MATCH" if self.score >= 85 else "MATCH" if self.score >= 75 else "BORDERLINE" if self.score >= 60 else "REJECT")
        self.eligibility = eligibility
        self.role_family = role_family
        self.role_priority = role_priority
        self.reasons = reasons
        self.strengths = strengths
        self.gaps = gaps
        self.dimensions = dimensions or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "decision": self.decision,
            "decision_class": self.decision_class,
            "eligibility": self.eligibility,
            "role_family": self.role_family,
            "role_priority": self.role_priority,
            "reasons": self.reasons,
            "strengths": self.strengths,
            "gaps": self.gaps,
            "dimensions": self.dimensions,
        }


# =============================================================================
# PROFILE COERCION LAYER
# =============================================================================

def _coerce_profile(profile: Any) -> Dict[str, Any]:
    """Normalize any profile (CandidateProfile, JobProfile, or Dict) to unified dict."""
    if CandidateProfile is not None and isinstance(profile, CandidateProfile):
        return {
            "target_roles_lc": getattr(profile, "_target_roles_lc", []),
            "desired_roles_lc": getattr(profile, "_desired_roles_lc", []),
            "alternative_roles_lc": getattr(profile, "_alternative_roles_lc", []),
            "role_families_lc": getattr(profile, "_role_families_lc", []),
            "core_skills_lc": getattr(profile, "_core_skills_lc", []),
            "secondary_skills_lc": getattr(profile, "_secondary_skills_lc", []),
            "transferable_skills_lc": getattr(profile, "_transferable_skills_lc", []),
            "skills_lc": getattr(profile, "_skills_lc", []),
            "seniority_lc": getattr(profile, "_seniority_lc", []),
            "seniority_range_lc": getattr(profile, "_seniority_range_lc", []),
            "years_experience": getattr(profile, "years_experience", None),
            "years_of_experience": getattr(profile, "years_of_experience", None),
            "provenance": getattr(profile, "provenance", {}),
            "skill_confidence": getattr(profile, "_skill_confidence_lc", {}) or getattr(profile, "skill_confidence", {}),
            "role_priorities": getattr(profile, "_role_priorities_lc", {}) or getattr(profile, "role_priorities", {}),
            "role_family_seniority": getattr(profile, "_role_family_seniority_lc", {}) or getattr(profile, "role_family_seniority", {}),
            "domain_years": getattr(profile, "domain_years", {}),
            "role_specific_skills": getattr(profile, "role_specific_skills", {}),
            "remote_required": bool(getattr(profile, "remote_required", False)),
            "allowed_locations_lc": getattr(profile, "_allowed_locations_lc", []),
            "allowed_timezones_lc": getattr(profile, "_allowed_timezones_lc", []),
            "languages_lc": getattr(profile, "_languages_lc", []),
            "employment_types_lc": getattr(profile, "_employment_types_lc", []),
            "minimum_salary": getattr(profile, "minimum_salary", None),
            "salary_currency": (getattr(profile, "salary_currency", None) or "").upper() or None,
            "industries_lc": getattr(profile, "_industries_lc", []),
            "excluded_roles_lc": getattr(profile, "_excluded_roles_lc", []),
            "excluded_companies_lc": getattr(profile, "_excluded_companies_lc", []),
            "excluded_countries_lc": getattr(profile, "_excluded_countries_lc", []),
            "excluded_industries_lc": getattr(profile, "_excluded_industries_lc", []),
            "must_avoid_conditions_lc": getattr(profile, "_must_avoid_conditions_lc", []),
            "candidate_country": getattr(profile, "candidate_country", "TH") if hasattr(profile, "candidate_country") else "TH",
        }

    # JobProfile or dict-like
    desired = [str(x).lower() for x in getattr(profile, "desired_roles", []) or []]
    alt = []
    if hasattr(profile, "alternative_roles"):
        alt = [str(x).lower() for x in getattr(profile, "alternative_roles") or []]
    elif hasattr(profile, "extra") and isinstance(getattr(profile, "extra"), dict):
        alt = [str(x).lower() for x in profile.extra.get("alternative_roles", []) or profile.extra.get("alternativeRoles", [])]

    skills = [str(x).lower() for x in getattr(profile, "skills", []) or []]
    core_sk = []
    if hasattr(profile, "core_skills"):
        core_sk = [str(x).lower() for x in getattr(profile, "core_skills") or []]
    elif hasattr(profile, "extra") and isinstance(getattr(profile, "extra"), dict):
        core_sk = [str(x).lower() for x in profile.extra.get("core_skills", []) or []]
    if not core_sk:
        core_sk = list(skills)

    sec_sk = []
    if hasattr(profile, "secondary_skills"):
        sec_sk = [str(x).lower() for x in getattr(profile, "secondary_skills") or []]
    elif hasattr(profile, "transferable_skills"):
        sec_sk = [str(x).lower() for x in getattr(profile, "transferable_skills") or []]
    elif hasattr(profile, "extra") and isinstance(getattr(profile, "extra"), dict):
        sec_sk = [str(x).lower() for x in profile.extra.get("secondary_skills", []) or profile.extra.get("transferable_skills", []) or []]

    seniority: List[str] = []
    if hasattr(profile, "preferred_seniority"):
        seniority = [str(x).lower() for x in getattr(profile, "preferred_seniority") or []]
    elif hasattr(profile, "seniority") and getattr(profile, "seniority"):
        seniority = [str(x).lower() for x in getattr(profile, "seniority") or []]
    elif hasattr(profile, "experience") and getattr(profile, "experience"):
        seniority = [str(x).lower() for x in getattr(profile, "experience") or []]

    remote_required = False
    if hasattr(profile, "remote_required"):
        remote_required = bool(getattr(profile, "remote_required"))
    elif hasattr(profile, "extra") and isinstance(getattr(profile, "extra"), dict):
        remote_required = bool(profile.extra.get("remote_required") or profile.extra.get("remoteRequired"))

    allowed_locs: List[str] = []
    if hasattr(profile, "allowed_locations"):
        allowed_locs = [str(x).lower() for x in getattr(profile, "allowed_locations") or []]
    elif hasattr(profile, "countries"):
        allowed_locs = [str(x).lower() for x in getattr(profile, "countries") or []]

    allowed_tz: List[str] = []
    if hasattr(profile, "allowed_timezones"):
        allowed_tz = [str(x).lower() for x in getattr(profile, "allowed_timezones") or []]
    elif hasattr(profile, "timezones"):
        allowed_tz = [str(x).lower() for x in getattr(profile, "timezones") or []]

    langs: List[str] = []
    if hasattr(profile, "languages"):
        langs = [str(x).lower() for x in getattr(profile, "languages") or []]

    emp: List[str] = []
    if hasattr(profile, "employment_types"):
        emp = [str(x).lower() for x in getattr(profile, "employment_types") or []]

    min_sal = getattr(profile, "minimum_salary", None)
    if min_sal is None:
        min_sal = getattr(profile, "salary_min", None)
    if min_sal is not None:
        try:
            min_sal = float(min_sal)
        except Exception:
            min_sal = None

    curr = getattr(profile, "salary_currency", None)
    if curr:
        curr = str(curr).strip().upper() or None

    excl_roles = [str(x).lower() for x in getattr(profile, "excluded_roles", []) or []]
    excl_comp = [str(x).lower() for x in getattr(profile, "excluded_companies", []) or []]
    excl_countries = [str(x).lower() for x in getattr(profile, "excluded_countries", []) or []]
    excl_industries = [str(x).lower() for x in getattr(profile, "excluded_industries", []) or []]

    yrs = getattr(profile, "years_experience", None) or getattr(profile, "years_of_experience", None)
    if yrs is not None:
        try:
            yrs = int(yrs)
        except Exception:
            yrs = None

    role_fams: List[str] = []
    if hasattr(profile, "role_families"):
        role_fams = [str(x).lower() for x in getattr(profile, "role_families") or []]

    extra_dict = getattr(profile, "extra", {}) if isinstance(getattr(profile, "extra", {}), dict) else {}
    prov = getattr(profile, "provenance", {}) or extra_dict.get("provenance", {})
    sk_conf = getattr(profile, "skill_confidence", {}) or extra_dict.get("skill_confidence", {}) or extra_dict.get("skillConfidence", {})
    r_prios = getattr(profile, "role_priorities", {}) or extra_dict.get("role_priorities", {}) or extra_dict.get("rolePriorities", {})
    r_sens = getattr(profile, "role_family_seniority", {}) or extra_dict.get("role_family_seniority", {}) or extra_dict.get("roleFamilySeniority", {})
    d_yrs = getattr(profile, "domain_years", {}) or extra_dict.get("domain_years", {}) or extra_dict.get("domainYears", {})
    r_spec_skills = getattr(profile, "role_specific_skills", {}) or extra_dict.get("role_specific_skills", {}) or extra_dict.get("roleSpecificSkills", {})

    return {
        "target_roles_lc": desired,
        "desired_roles_lc": desired,
        "alternative_roles_lc": alt,
        "role_families_lc": role_fams,
        "core_skills_lc": core_sk,
        "secondary_skills_lc": sec_sk,
        "transferable_skills_lc": sec_sk,
        "skills_lc": skills,
        "seniority_lc": seniority,
        "seniority_range_lc": seniority,
        "years_experience": yrs,
        "years_of_experience": yrs,
        "provenance": prov,
        "skill_confidence": {str(k).lower().strip(): str(v).upper().strip() for k, v in sk_conf.items() if str(k).strip()} if isinstance(sk_conf, dict) else {},
        "role_priorities": {str(k).upper().strip(): str(v).upper().strip() for k, v in r_prios.items() if str(k).strip()} if isinstance(r_prios, dict) else {},
        "role_family_seniority": {
            str(k).upper().strip(): [str(s).lower().strip() for s in v if str(s).strip()]
            for k, v in r_sens.items() if str(k).strip() and isinstance(v, (list, tuple, set))
        } if isinstance(r_sens, dict) else {},
        "domain_years": {str(k).lower().strip(): float(v) for k, v in d_yrs.items() if str(k).strip()} if isinstance(d_yrs, dict) else {},
        "role_specific_skills": r_spec_skills if isinstance(r_spec_skills, dict) else {},
        "remote_required": bool(remote_required),
        "allowed_locations_lc": allowed_locs,
        "allowed_timezones_lc": allowed_tz,
        "languages_lc": langs,
        "employment_types_lc": emp,
        "minimum_salary": min_sal,
        "salary_currency": curr,
        "industries_lc": [],
        "excluded_roles_lc": excl_roles,
        "excluded_companies_lc": excl_comp,
        "excluded_countries_lc": excl_countries,
        "excluded_industries_lc": excl_industries,
        "must_avoid_conditions_lc": excl_roles,
        "candidate_country": getattr(profile, "candidate_country", "TH") if hasattr(profile, "candidate_country") else "TH",
    }


# =============================================================================
# ROLE FAMILIES & CLASSIFIER
# =============================================================================

_ROLE_FAMILY_PATTERNS: List[Tuple[RoleFamily, List[str]]] = [
    (
        RoleFamily.AI_AUTOMATION,
        [
            r"\bai[- ]automation\b", r"\bn8n\b", r"\bai[- ]agent\b", r"\bworkflow\s+automation\b",
            r"\bmake\.com\b", r"\bzapier\b", r"\bllm\s+engineer\b", r"\bprompt\s+engineer\b",
            r"\bai[- ]engineer\b", r"\bai\s+workflow\b", r"\blangchain\b", r"\bllamaindex\b",
            r"\bавтоматизац\w*", r"\bai-инженер\b", r"\bai\s+automation\b"
        ],
    ),
    (
        RoleFamily.PYTHON_BACKEND,
        [
            r"\bpython\s+(?:developer|engineer|разработчик|бэкенд|backend)\b",
            r"\bbackend\s+(?:developer|engineer|разработчик)\s+\(python\b",
            r"\bразработчик\s+бэкенда\s+\(python\b",
            r"\bpython\b", r"\bdjango\b", r"\bfastapi\b", r"\bflask\b",
        ],
    ),
    (
        RoleFamily.QA_AUTOMATION,
        [
            r"\bqa\s+auto\w*\b", r"\bautomation\s+qa\b", r"\btest\s+automation\b",
            r"\bsdet\b", r"\bqa\s+fullstack\b", r"\bавтотест\w*", r"\bqa\s+automation\b",
            r"\bplaywright\b", r"\bselenium\b"
        ],
    ),
    (
        RoleFamily.QA_MANUAL,
        [
            r"\bqa\s+manual\b", r"\bmanual\s+qa\b", r"\bручное\s+тестирован\w*",
            r"\bqa\s+engineer\b", r"\bqa\s+инженер\b", r"\bинженер\s+по\s+тестированию\b",
            r"\bтестировщик\b"
        ],
    ),
    (
        RoleFamily.DEVOPS_SRE,
        [
            r"\bdevops\b", r"\bsre\b", r"\bcloud\s+engineer\b", r"\binfrastructure\s+engineer\b",
            r"\bkubernetes\b", r"\bplatform\s+engineer\b", r"\bинженер\s+devops\b", r"\bterraform\b"
        ],
    ),
    (
        RoleFamily.SYSTEM_ADMIN,
        [
            r"\bsystem\s+administrator\b", r"\bsysadmin\b", r"\bсистемный\s+администратор\b",
            r"\blinux\s+admin\w*\b", r"\bwindows\s+admin\w*\b", r"\bnetwork\s+engineer\b",
            r"\bсетевой\s+инженер\b", r"\bсисадмин\b", r"\bactive\s+directory\b", r"\bexchange\b"
        ],
    ),
    (
        RoleFamily.TECH_SUPPORT,
        [
            r"\btechnical\s+support\b", r"\btech\s+support\b", r"\bспециалист\s+технической\s+поддержки\b",
            r"\bтехподдержк\w*", r"\bhelpdesk\b", r"\bservice\s+desk\b", r"\bl1\s+support\b",
            r"\bl2\s+support\b", r"\bl3\s+support\b", r"\bit\s+support\b", r"\bтехническая\s+поддержка\b"
        ],
    ),
    (
        RoleFamily.APPLICATION_SUPPORT,
        [
            r"\bbitrix\w*\b", r"\b1c\b", r"\b1с\b", r"\bcrm\b", r"\berp\b",
            r"\bapplication\s+support\b", r"\bсопровождение\s+по\b", r"\bинженер\s+внедрения\b",
            r"\bадминистратор\s+bitrix\b"
        ],
    ),
    (
        RoleFamily.DATA_ENGINEERING,
        [
            r"\bdata\s+engineer\b", r"\bдата\s+инженер\b", r"\bинженер\s+данных\b",
            r"\betl\b", r"\bbi\s+developer\b", r"\bdwh\b", r"\bdata\s+pipeline\b"
        ],
    ),
    (
        RoleFamily.BUSINESS_ANALYST,
        [
            r"\bbusiness\s+analyst\b", r"\bсистемный\s+аналитик\b", r"\bsystem\s+analyst\b",
            r"\bproduct\s+analyst\b", r"\bбизнес-аналитик\b", r"\bproduct\s+manager\b",
            r"\bproject\s+manager\b"
        ],
    ),
    (
        RoleFamily.FRONTEND_DEVELOPER,
        [
            r"\bfrontend\b", r"\bfront-end\b", r"\breact\b", r"\bvue\b", r"\bangular\b",
            r"\bjavascript\s+developer\b", r"\bfrontend-разработчик\b"
        ],
    ),
    (
        RoleFamily.FULLSTACK_DEVELOPER,
        [
            r"\bfullstack\b", r"\bfull-stack\b", r"\bfull\s+stack\b", r"\bфулстек\b"
        ],
    ),
    (
        RoleFamily.MOBILE_DEVELOPER,
        [
            r"\bios\b", r"\bandroid\b", r"\bflutter\b", r"\breact\s+native\b",
            r"\bмобильный\s+разработчик\b"
        ],
    ),
    (
        RoleFamily.SECURITY,
        [
            r"\bsecurity\b", r"\bинформационная\s+безопасность\b", r"\bиб\b",
            r"\bsoc\b", r"\bpentest\w*", r"\bcybersecurity\b"
        ],
    ),
]


def classify_role_family(title: str, description: str = "") -> RoleFamily:
    """Classify vacancy into canonical role family based on title and description."""
    t_lower = (title or "").lower()
    d_lower = (description or "").lower()

    # 1. First priority: search title with precise patterns
    for family, patterns in _ROLE_FAMILY_PATTERNS:
        for p in patterns:
            if re.search(p, t_lower):
                return family

    # 2. Second priority: description check
    for family, patterns in _ROLE_FAMILY_PATTERNS:
        for p in patterns[:3]:
            if re.search(p, d_lower):
                return family

    return RoleFamily.OTHER


def calculate_role_compatibility(
    candidate_target_roles: List[str],
    candidate_alt_roles: List[str],
    candidate_role_families: List[str],
    vacancy_title: str,
    vacancy_family: RoleFamily,
) -> Tuple[float, str]:
    """Calculates compatibility multiplier (0.0 to 1.0) and evidence string."""
    title_lc = (vacancy_title or "").lower()
    
    # 1. Exact title match against desired / target roles
    for r in candidate_target_roles:
        if r and r.lower() in title_lc:
            return 1.0, f"Exact target role match: '{r}'"

    # 2. Match against alternative roles
    for r in candidate_alt_roles:
        if r and r.lower() in title_lc:
            return 0.85, f"Alternative role match: '{r}'"

    # 3. Dynamic role family compatibility matrix
    cand_fams: Set[RoleFamily] = set()
    for fam_str in candidate_role_families:
        try:
            cand_fams.add(RoleFamily(fam_str.upper()))
        except Exception:
            pass

    if not cand_fams:
        for r in candidate_target_roles:
            cand_fams.add(classify_role_family(r))

    # Exact role family match if vacancy_family is in candidate's specified families
    if vacancy_family in cand_fams:
        return 1.0, f"Role family match: {vacancy_family.value}"

    if RoleFamily.AI_AUTOMATION in cand_fams:
        matrix = {
            RoleFamily.AI_AUTOMATION: 1.0,
            RoleFamily.PYTHON_BACKEND: 0.80,
            RoleFamily.APPLICATION_SUPPORT: 0.65,
            RoleFamily.TECH_SUPPORT: 0.60,
            RoleFamily.DATA_ENGINEERING: 0.50,
            RoleFamily.DEVOPS_SRE: 0.45,
            RoleFamily.SYSTEM_ADMIN: 0.40,
            RoleFamily.QA_AUTOMATION: 0.30,
            RoleFamily.QA_MANUAL: 0.25,
            RoleFamily.BUSINESS_ANALYST: 0.30,
            RoleFamily.FULLSTACK_DEVELOPER: 0.35,
            RoleFamily.FRONTEND_DEVELOPER: 0.15,
            RoleFamily.SECURITY: 0.15,
            RoleFamily.MOBILE_DEVELOPER: 0.10,
            RoleFamily.OTHER: 0.20,
        }
        score = matrix.get(vacancy_family, 0.2)
        return score, f"Role family compatibility: {RoleFamily.AI_AUTOMATION.value} ↔ {vacancy_family.value} ({score*100:.0f}%)"

    if RoleFamily.SYSTEM_ADMIN in cand_fams or RoleFamily.TECH_SUPPORT in cand_fams:
        matrix = {
            RoleFamily.SYSTEM_ADMIN: 1.0,
            RoleFamily.TECH_SUPPORT: 0.90,
            RoleFamily.APPLICATION_SUPPORT: 0.80,
            RoleFamily.DEVOPS_SRE: 0.65,
            RoleFamily.SECURITY: 0.50,
            RoleFamily.AI_AUTOMATION: 0.40,
            RoleFamily.PYTHON_BACKEND: 0.35,
            RoleFamily.QA_AUTOMATION: 0.25,
            RoleFamily.DATA_ENGINEERING: 0.25,
            RoleFamily.QA_MANUAL: 0.20,
            RoleFamily.BUSINESS_ANALYST: 0.20,
            RoleFamily.FRONTEND_DEVELOPER: 0.10,
            RoleFamily.MOBILE_DEVELOPER: 0.10,
            RoleFamily.OTHER: 0.20,
        }
        score = matrix.get(vacancy_family, 0.2)
        return score, f"Role family compatibility: SYSTEM_ADMIN/TECH_SUPPORT ↔ {vacancy_family.value} ({score*100:.0f}%)"
    
    return 0.35, f"Unrelated role family: {vacancy_family.value}"


# =============================================================================
# SENIORITY & EXPERIENCE-YEARS PARSER
# =============================================================================

_SENIORITY_PATTERNS = [
    ("intern", [r"\bintern\b", r"\bстажер\b", r"\bстажёр\b", r"\bученик\b", r"\btrainee\b"]),
    ("junior", [r"\bjunior\b", r"\bджуниор\b", r"\bмладший\b", r"\bjr\b", r"\bjr\.\b"]),
    ("mid", [r"\bmid\b", r"\bmiddle\b", r"\bмидл\b", r"\bмиддл\b", r"\bmid-level\b"]),
    ("senior", [r"\bsenior\b", r"\bсеньор\b", r"\bстарший\b", r"\bsr\b", r"\bsr\.\b"]),
    ("lead", [r"\blead\b", r"\bлид\b", r"\bтимлид\b", r"\bteam\s*lead\b", r"\btech\s*lead\b", r"\bруководитель\b"]),
    ("principal", [r"\bprincipal\b", r"\bstaff\b", r"\bглавный\b"]),
    ("architect", [r"\barchitect\b", r"\bархитектор\b"]),
    ("head", [r"\bhead\s+of\b", r"\bdirector\b", r"\bcto\b", r"\bдиректор\b"]),
]


def extract_seniority(title: str, description: str = "") -> List[str]:
    """Extract seniority levels found in title and description."""
    text = f"{title or ''} {description or ''}".lower()
    found: List[str] = []
    for level, patterns in _SENIORITY_PATTERNS:
        for p in patterns:
            if re.search(p, text):
                if level not in found:
                    found.append(level)
                break
    return found


def extract_required_years(title: str, description: str = "") -> Optional[int]:
    """Extract required minimum years of experience from vacancy text."""
    text = f"{title or ''} {description or ''}".lower()
    
    patterns = [
        r"(?:at least|minimum of|минимум|от|более)?\s*(\d+)\+?\s*(?:years?|years of|лет|года|год)\s*(?:of\s+experience|опыта|коммерческого опыта)?",
        r"experience\s*(?:of)?\s*(\d+)\+?\s*years",
        r"опыт\s*(?:работы)?\s*(?:от)?\s*(\d+)\+?\s*(?:лет|года|год)",
        r"(\d+)\+\s*(?:years|лет)",
    ]
    years_found: List[int] = []
    for p in patterns:
        for m in re.finditer(p, text):
            try:
                val = int(m.group(1))
                if 1 <= val <= 25:
                    years_found.append(val)
            except Exception:
                pass

    if years_found:
        return max(years_found)
    return None


# =============================================================================
# HARD REQUIREMENTS EVALUATOR
# =============================================================================

_CLEARANCE_PATTERNS = [
    r"\bactive\s+security\s+clearance\b",
    r"\bsecret\s+clearance\b",
    r"\btop\s+secret\b",
    r"\bus\s+citizenship\s+required\b",
    r"\bsecurity\s+clearance\s+required\b",
    r"\bts/sci\b",
]

_DISQUALIFYING_TECH_PATTERNS = [
    ("c++", [r"\b(?:senior|lead|principal)\s+c\+\+\s+(?:developer|engineer)\b", r"\b7\+\s+years\s+(?:of\s+)?c\+\+\b", r"\bc\+\+\s+game\s+engine\b"]),
    ("java", [r"\b(?:senior|lead)\s+java\s+(?:developer|engineer|backend)\b", r"\b5\+\s+years\s+(?:of\s+)?java\b", r"\bdeep\s+java\s+internals\b"]),
    ("rust", [r"\b(?:senior|lead)\s+rust\s+(?:developer|engineer)\b", r"\b5\+\s+years\s+(?:of\s+)?rust\b"]),
    ("golang", [r"\b(?:senior|lead)\s+golang\s+(?:developer|engineer)\b", r"\b5\+\s+years\s+(?:of\s+)?golang\b"]),
    ("solidity", [r"\bsolidity\s+smart\s+contracts\b", r"\bsenior\s+solidity\b"]),
]


def evaluate_hard_requirements(
    profile_dict: Dict[str, Any],
    vacancy: Vacancy,
    candidate_years: Optional[int] = None,
    candidate_seniority: Optional[List[str]] = None,
) -> Tuple[HardRequirementStatus, List[str]]:
    """Evaluates strict mandatory gates and returns (status, reasons)."""
    text = f"{vacancy.title or ''} {vacancy.description or ''}".lower()
    company = (vacancy.company or "").lower()
    loc = (vacancy.location or "").lower()
    country_text = ", ".join(vacancy.country_restrictions or []).lower()
    reasons: List[str] = []

    # 1. Excluded roles (hard avoid)
    for role in profile_dict.get("excluded_roles_lc", []):
        if role and role in text:
            return HardRequirementStatus.INELIGIBLE, [f"Excluded role keyword matched: '{role}'"]

    # 2. Excluded companies
    for comp in profile_dict.get("excluded_companies_lc", []):
        if comp and comp in company:
            return HardRequirementStatus.INELIGIBLE, [f"Excluded company matched: '{comp}'"]

    # 3. Excluded countries
    for country in profile_dict.get("excluded_countries_lc", []):
        if country and (country in country_text or country in loc):
            return HardRequirementStatus.INELIGIBLE, [f"Excluded country matched: '{country}'"]

    # 4. Excluded industries
    for ind in profile_dict.get("excluded_industries_lc", []):
        if ind and (ind in text or ind in company):
            return HardRequirementStatus.INELIGIBLE, [f"Excluded industry matched: '{ind}'"]

    # 5. Security Clearance requirement
    for cp in _CLEARANCE_PATTERNS:
        if re.search(cp, text):
            return HardRequirementStatus.INELIGIBLE, ["Mandatory security clearance / US citizenship required"]

    # 6. Remote requirement constraint
    if profile_dict.get("remote_required", False):
        is_rem, rem_reason = is_strictly_remote(vacancy)
        if not is_rem:
            return HardRequirementStatus.INELIGIBLE, [f"Remote required but vacancy is not remote: {rem_reason}"]

        # Multi-dimensional Remote Eligibility Gate
        from .eligibility import assess_vacancy_eligibility, EligibilityStatus
        cand_country = profile_dict.get("candidate_country") or "TH"
        assessment = assess_vacancy_eligibility(vacancy, candidate_country=cand_country)
        if assessment.eligibility == EligibilityStatus.INELIGIBLE:
            return HardRequirementStatus.INELIGIBLE, [f"Ineligible remote vacancy: {'; '.join(assessment.eligibility_reasons)}"]

    # 7. Mandatory foreign language mismatch (e.g., Native Japanese / Fluent German)
    cand_langs = profile_dict.get("languages_lc", [])
    if cand_langs:
        foreign_checks = [
            ("japanese", r"\b(?:native|fluent|business)\s+japanese\b|\bfluent\s+in\s+japanese\b|\bтребуется\s+японский\b"),
            ("german", r"\b(?:native|fluent|c1|c2)\s+german\b|\bfluent\s+in\s+german\b|\bdeutsch\s+c1\b|\bнемецкий\s+(?:c1|свободный)\b"),
            ("french", r"\b(?:native|fluent|c1|c2)\s+french\b|\bfluent\s+in\s+french\b|\bфранцузский\s+c1\b"),
            ("chinese", r"\b(?:native|fluent)\s+chinese\b|\bfluent\s+in\s+mandarin\b"),
        ]
        for lang_name, pattern in foreign_checks:
            if not any(lang_name in l for l in cand_langs):
                if re.search(pattern, text):
                    return HardRequirementStatus.INELIGIBLE, [f"Mandatory language requirement mismatch: {lang_name.title()} required"]

    # 8. Deep Technical Mismatch for unconfirmed stacks
    cand_skills = profile_dict.get("core_skills_lc", []) + profile_dict.get("skills_lc", [])
    cand_skills_str = " ".join(cand_skills).lower()
    for tech_name, patterns in _DISQUALIFYING_TECH_PATTERNS:
        if tech_name not in cand_skills_str:
            for p in patterns:
                if re.search(p, text):
                    return HardRequirementStatus.INELIGIBLE, [f"Mandatory tech stack mismatch: role requires deep {tech_name.upper()}"]

    # 9. Severe experience / seniority gap
    req_years = extract_required_years(vacancy.title, vacancy.description)
    if candidate_years is not None and req_years is not None:
        if req_years >= candidate_years + 5 and req_years >= 8:
            reasons.append(f"Severe experience gap: requires {req_years}+ years (candidate has {candidate_years} yrs)")
            return HardRequirementStatus.BORDERLINE, reasons

    return HardRequirementStatus.ELIGIBLE, reasons


def _hard_constraints(profile_dict: Dict[str, Any], vacancy: Vacancy) -> Tuple[bool, str]:
    """Legacy helper for backward compatibility."""
    status, reasons = evaluate_hard_requirements(profile_dict, vacancy)
    if status == HardRequirementStatus.INELIGIBLE:
        return True, reasons[0] if reasons else "Hard constraint violated"
    return False, ""


# =============================================================================
# MULTI-DIMENSIONAL MATCHING ENGINE
# =============================================================================

class JobMatcher:
    def __init__(self, profile: Any) -> None:
        self.profile = profile
        self._p = _coerce_profile(profile)

    def assess_eligibility(self, vacancy: Vacancy):
        from .eligibility import assess_vacancy_eligibility
        cand_country = self._p.get("candidate_country") or "TH"
        return assess_vacancy_eligibility(vacancy, candidate_country=cand_country)

    def _hard_constraints(self, vacancy: Vacancy) -> Tuple[bool, str]:
        return _hard_constraints(self._p, vacancy)

    def match(self, vacancy: Vacancy) -> MatchResult:
        strengths: List[str] = []
        gaps: List[str] = []
        dimensions: Dict[str, Any] = {}

        title = vacancy.title or ""
        desc = vacancy.description or ""
        title_lc = title.lower()
        desc_lc = desc.lower()
        full_text_lc = f"{title_lc} {desc_lc}"

        cand_years = self._p.get("years_experience")
        cand_sen = self._p.get("seniority_lc", [])

        # ---------------------------------------------------------------------
        # 1. EVALUATE HARD REQUIREMENTS & GATES
        # ---------------------------------------------------------------------
        hard_status, hard_reasons = evaluate_hard_requirements(
            self._p,
            vacancy,
            candidate_years=cand_years,
            candidate_seniority=cand_sen,
        )

        if hard_status == HardRequirementStatus.INELIGIBLE:
            reasons = list(hard_reasons)
            return MatchResult(
                score=0,
                decision="SKIP",
                decision_class="REJECT",
                eligibility="INELIGIBLE",
                role_family=classify_role_family(title, desc).value,
                reasons=reasons,
                strengths=[],
                gaps=hard_reasons,
                dimensions={"hard_requirements": {"status": "INELIGIBLE", "reasons": hard_reasons}},
            )

        # Record warnings for eligible with warning
        try:
            from .eligibility import assess_vacancy_eligibility, EligibilityStatus
            cand_country = self._p.get("candidate_country") or "TH"
            assessment = assess_vacancy_eligibility(vacancy, candidate_country=cand_country)
            if assessment.eligibility == EligibilityStatus.ELIGIBLE_WITH_WARNING:
                for w in assessment.eligibility_reasons:
                    gaps.append(w)
        except Exception:
            pass

        # ---------------------------------------------------------------------
        # 2. ROLE FAMILY & TITLE RELEVANCE (max 25 pts)
        # ---------------------------------------------------------------------
        vacancy_family = classify_role_family(title, desc)
        desired_roles = self._p.get("desired_roles_lc", []) or self._p.get("target_roles_lc", [])
        alt_roles = self._p.get("alternative_roles_lc", [])
        cand_fams = self._p.get("role_families_lc", [])
        role_prios = self._p.get("role_priorities", {})
        
        # Determine role priority (P1, P2, P3, NOT_TARGET)
        role_prio = role_prios.get(vacancy_family.name) or role_prios.get(vacancy_family.value)
        if not role_prio:
            if cand_fams and vacancy_family.value in [f.upper() for f in cand_fams]:
                role_prio = RolePriority.P1.value
            elif vacancy_family in (RoleFamily.DATA_ENGINEERING, RoleFamily.DEVOPS_SRE, RoleFamily.FULLSTACK_DEVELOPER):
                role_prio = RolePriority.P3.value
            elif vacancy_family in (RoleFamily.SYSTEM_ADMIN, RoleFamily.APPLICATION_SUPPORT, RoleFamily.TECH_SUPPORT):
                role_prio = RolePriority.P1.value
            else:
                role_prio = RolePriority.P2.value

        role_score = 0
        role_breakdown = ""
        if desired_roles or alt_roles:
            matched_desired = next((r for r in desired_roles if r and r in title_lc), None)
            matched_alt = next((r for r in alt_roles if r and r in title_lc), None) if alt_roles else None
            matched_desc = next((r for r in desired_roles if r and r in desc_lc), None) if desired_roles else None
            
            comp_factor, comp_evidence = calculate_role_compatibility(
                desired_roles, alt_roles, cand_fams, title, vacancy_family
            )

            if matched_desired:
                role_score = 25
                strengths.append(f"Exact role match: {matched_desired}")
                role_breakdown = "role:25/25 exact"
            elif matched_alt:
                role_score = 15
                strengths.append(f"Alternative role match: {matched_alt}")
                gaps.append("No exact desired role, alternative matched")
                role_breakdown = "role:15/25 alternative"
            elif matched_desc:
                role_score = 10
                strengths.append(f"Role mentioned in description: {matched_desc}")
                gaps.append("Role not in title but in description")
                role_breakdown = "role:10/25 desc"
            elif comp_factor >= 0.8:
                role_score = int(round(25 * comp_factor))
                strengths.append(f"Role match: {comp_evidence}")
                role_breakdown = f"role:{role_score}/25 family"
            elif comp_factor >= 0.5:
                role_score = int(round(25 * comp_factor))
                strengths.append(f"Related role fit: {comp_evidence}")
                role_breakdown = f"role:{role_score}/25 related"
            else:
                role_score = 0
                gaps.append(f"Role does not match desired/alternative roles ({comp_evidence})")
                role_breakdown = "role:0/25"
        elif cand_fams:
            comp_factor, comp_evidence = calculate_role_compatibility(
                [], [], cand_fams, title, vacancy_family
            )
            role_score = int(round(25 * comp_factor))
            if comp_factor >= 0.5:
                strengths.append(f"Role match: {comp_evidence}")
            else:
                gaps.append(f"Role mismatch: {comp_evidence}")
            role_breakdown = f"role:{role_score}/25"
        else:
            role_score = 25
            role_breakdown = "role:25/25 neutral"

        dimensions["role_relevance"] = {
            "score": role_score,
            "max": 25,
            "role_family": vacancy_family.value,
            "role_priority": role_prio,
        }

        # ---------------------------------------------------------------------
        # 3. SKILLS MATCH & EVIDENCE CONFIDENCE (max 25 pts)
        # ---------------------------------------------------------------------
        role_skills_map = self._p.get("role_specific_skills", {})
        fam_skills = role_skills_map.get(vacancy_family.name) or role_skills_map.get(vacancy_family.value)
        if fam_skills and isinstance(fam_skills, dict) and ("core" in fam_skills or "core_skills" in fam_skills):
            core_skills = [str(s).lower() for s in (fam_skills.get("core") or fam_skills.get("core_skills") or [])]
            sec_skills = [str(s).lower() for s in (fam_skills.get("secondary") or fam_skills.get("secondary_skills") or [])]
        else:
            core_skills = self._p.get("core_skills_lc", [])
            sec_skills = list(dict.fromkeys(self._p.get("secondary_skills_lc", []) + self._p.get("transferable_skills_lc", [])))
        flat_skills = self._p.get("skills_lc", [])

        # Skill confidence weighting function
        skill_conf = self._p.get("skill_confidence", {})
        def _get_conf_wt(skill_name: str) -> float:
            lvl = skill_conf.get(skill_name.lower().strip())
            if lvl == SkillConfidenceLevel.PROFESSIONAL.value:
                return 1.0
            elif lvl == SkillConfidenceLevel.PROJECT.value:
                return 0.85
            elif lvl == SkillConfidenceLevel.BASIC.value:
                return 0.60
            elif lvl == SkillConfidenceLevel.TRANSFERABLE.value:
                return 0.40
            elif lvl == SkillConfidenceLevel.UNKNOWN.value:
                return 0.0
            return 1.0

        skills_score = 0
        skills_breakdown = ""
        matched_core = []
        matched_sec = []
        if not core_skills and not flat_skills:
            skills_score = 25
            skills_breakdown = "skills:25/25 neutral"
        elif core_skills:
            matched_core = [s for s in core_skills if s and s in full_text_lc]
            matched_sec = [s for s in sec_skills if s and s in full_text_lc]
            
            total_core_wt = sum(_get_conf_wt(s) for s in core_skills)
            matched_core_wt = sum(_get_conf_wt(s) for s in matched_core)
            core_ratio = (matched_core_wt / total_core_wt) if total_core_wt > 0 else 0.0

            total_sec_wt = sum(_get_conf_wt(s) for s in sec_skills) if sec_skills else 1.0
            matched_sec_wt = sum(_get_conf_wt(s) for s in matched_sec) if matched_sec else 0.0
            sec_ratio = (matched_sec_wt / total_sec_wt) if total_sec_wt > 0 else 0.0
            
            # Core skills form the base (up to 25 pts), secondary/transferable add bonus capped at 25
            if total_core_wt > 0:
                skills_score = min(25, int(round(25.0 * core_ratio + (5.0 * sec_ratio if core_ratio < 1.0 else 0.0))))
            else:
                skills_score = 0
                
            if matched_core:
                strengths.append(f"Skills match {len(matched_core)}/{len(core_skills)}: {', '.join(matched_core[:5])}")
            if matched_sec:
                strengths.append(f"Transferable skills matched ({len(matched_sec)}/{len(sec_skills)}): {', '.join(matched_sec[:3])}")
            missing_core = [s for s in core_skills if s and s not in full_text_lc]
            if missing_core:
                gaps.append(f"Missing skills: {', '.join(missing_core[:5])}")
            skills_breakdown = f"skills:{skills_score}/25 core:{len(matched_core)}/{len(core_skills)}"
        else:
            matched_all = [s for s in flat_skills if s and s in full_text_lc]
            total_wt = sum(_get_conf_wt(s) for s in flat_skills)
            matched_wt = sum(_get_conf_wt(s) for s in matched_all)
            ratio = (matched_wt / total_wt) if total_wt > 0 else 0
            skills_score = int(round(25 * ratio)) if total_wt > 0 else 0
            
            if matched_all:
                strengths.append(f"Skills match {len(matched_all)}/{len(flat_skills)}: {', '.join(matched_all[:5])}")
            missing_all = [s for s in flat_skills if s and s not in full_text_lc]
            if missing_all:
                gaps.append(f"Missing skills: {', '.join(missing_all[:5])}")
            if ratio >= 1:
                skills_breakdown = f"skills:25/25 {len(matched_all)}/{len(flat_skills)}"
            elif ratio == 0:
                skills_breakdown = "skills:0/25 none"
            else:
                skills_breakdown = f"skills:{skills_score}/25 {len(matched_all)}/{len(flat_skills)}"

        dimensions["skills"] = {
            "score": skills_score,
            "max": 25,
        }

        # ---------------------------------------------------------------------
        # 4. SENIORITY FIT (max 15 pts) - Role Family Specific
        # ---------------------------------------------------------------------
        role_fam_sen = self._p.get("role_family_seniority", {})
        fam_sen = role_fam_sen.get(vacancy_family.name) or role_fam_sen.get(vacancy_family.value)
        if fam_sen and isinstance(fam_sen, (list, tuple, set)):
            seniority = [str(s).lower().strip() for s in fam_sen if str(s).strip()]
        else:
            seniority = self._p.get("seniority_lc", [])

        seniority_score = 0
        seniority_breakdown = ""
        vac_sen_list = extract_seniority(title, desc)
        if not seniority:
            seniority_score = 15
            seniority_breakdown = "seniority:15/15 neutral"
        else:
            hit = next((s for s in seniority if s and (s in title_lc or s in desc_lc)), None)
            if hit:
                seniority_score = 15
                strengths.append(f"Seniority match: {hit}")
                seniority_breakdown = f"seniority:15/15 {hit}"
            elif not vac_sen_list and any(m in seniority for m in ("mid", "middle", "junior", "any", "all", "unspecified")):
                # Vacancy has no restrictive seniority qualifier -> open/mid compatible for candidates accepting mid
                seniority_score = 10
                strengths.append("Seniority open / unspecified level")
                seniority_breakdown = "seniority:10/15 open"
            else:
                seniority_score = 0
                expected_str = ', '.join(vac_sen_list) if vac_sen_list else ', '.join(seniority)
                gaps.append(f"Seniority mismatch: expected {expected_str}")
                seniority_breakdown = f"seniority:0/15"

        dimensions["seniority"] = {
            "score": seniority_score,
            "max": 15,
        }

        # ---------------------------------------------------------------------
        # 5. REMOTE & LOCATION FIT (max 15 pts)
        # ---------------------------------------------------------------------
        loc_score = 0
        loc_breakdown = ""
        allowed_locs = self._p.get("allowed_locations_lc", [])
        allowed_tzs = self._p.get("allowed_timezones_lc", [])
        remote_req = self._p.get("remote_required", False)
        loc_text = f"{(vacancy.location or '').lower()} {', '.join(str(x) for x in (vacancy.country_restrictions or [])).lower()}".strip()
        tz_text = ", ".join(str(x) for x in (vacancy.timezone_restrictions or [])).lower()
        is_remote, _ = is_strictly_remote(vacancy)

        if not remote_req and not allowed_locs and not allowed_tzs:
            loc_score = 15
            loc_breakdown = "location:15/15 neutral"
        else:
            loc_ok = True
            tz_ok = True
            if remote_req:
                if is_remote:
                    strengths.append("Remote location matches requirement")
                else:
                    loc_ok = False
                    gaps.append("Remote required but vacancy not remote")

            if allowed_locs:
                if any(loc in loc_text for loc in allowed_locs):
                    strengths.append(f"Location allowed: {loc_text[:40]}")
                elif is_remote and "remote" in allowed_locs:
                    strengths.append("Remote location allowed")
                elif not remote_req and not loc_text:
                    gaps.append(f"Location not in allowed: {', '.join(allowed_locs[:3])}")
                    loc_ok = False
                else:
                    if is_remote:
                        strengths.append("Remote vacancy considered location-flexible")
                    else:
                        loc_ok = False
                        gaps.append(f"Location not in allowed: {', '.join(allowed_locs[:3])}")

            if allowed_tzs:
                if any(tz in tz_text for tz in allowed_tzs):
                    strengths.append(f"Timezone allowed: {tz_text[:20]}")
                else:
                    if tz_text.strip():
                        tz_ok = False
                        gaps.append(f"Timezone not in allowed: {', '.join(allowed_tzs[:3])}")

            if loc_ok and tz_ok:
                loc_score = 15
                loc_breakdown = "location:15/15"
            else:
                loc_score = 0
                loc_breakdown = "location:0/15"

        dimensions["remote_location"] = {
            "score": loc_score,
            "max": 15,
        }

        # ---------------------------------------------------------------------
        # 6. SALARY FIT (max 10 pts)
        # ---------------------------------------------------------------------
        min_sal = self._p.get("minimum_salary")
        prof_curr = self._p.get("salary_currency")
        salary_score = 0
        salary_breakdown = ""
        if min_sal is None:
            salary_score = 10
            salary_breakdown = "salary:10/10 neutral"
        else:
            vac_min = vacancy.salary_min
            vac_max = vacancy.salary_max
            vac_curr = (vacancy.salary_currency or "").upper() if vacancy.salary_currency else None
            effective = vac_max if vac_max is not None else vac_min
            effective_min = vac_min if vac_min is not None else vac_max

            if effective is None and effective_min is None:
                salary_score = 5
                gaps.append("Salary not specified")
                salary_breakdown = "salary:5/10 unspecified"
            elif prof_curr and vac_curr and prof_curr != vac_curr:
                salary_score = 0
                gaps.append(f"Currency mismatch: {vac_curr} vs {prof_curr}")
                salary_breakdown = "salary:0/10 currency"
            else:
                top = effective if effective is not None else effective_min
                if top is not None and top >= min_sal:
                    salary_score = 10
                    strengths.append(f"Salary meets minimum: {top} {vac_curr or prof_curr or ''}")
                    salary_breakdown = f"salary:10/10 {top}>={min_sal}"
                else:
                    salary_score = 0
                    gaps.append(f"Salary below minimum: {top} < {min_sal}")
                    salary_breakdown = f"salary:0/10 {top}<{min_sal}"

        dimensions["salary"] = {
            "score": salary_score,
            "max": 10,
        }

        # ---------------------------------------------------------------------
        # 7. EMPLOYMENT TYPE (max 5 pts)
        # ---------------------------------------------------------------------
        emp_types = self._p.get("employment_types_lc", [])
        emp_score = 0
        emp_breakdown = ""
        if not emp_types:
            emp_score = 5
            emp_breakdown = "employment:5/5 neutral"
        else:
            vac_emp = (vacancy.employment_type or "").lower().strip()
            if vac_emp and any(e in vac_emp or vac_emp in e for e in emp_types):
                emp_score = 5
                strengths.append(f"Employment type matches: {vac_emp}")
                emp_breakdown = "employment:5/5"
            elif not vac_emp:
                emp_score = 2
                gaps.append("Employment type not specified")
                emp_breakdown = "employment:2/5 unspecified"
            else:
                emp_score = 0
                gaps.append(f"Employment type mismatch: {vac_emp or 'n/a'} not in {', '.join(emp_types[:3])}")
                emp_breakdown = "employment:0/5"

        dimensions["employment"] = {
            "score": emp_score,
            "max": 5,
        }

        # ---------------------------------------------------------------------
        # 8. LANGUAGE (max 5 pts)
        # ---------------------------------------------------------------------
        cand_langs = self._p.get("languages_lc", [])
        lang_score = 0
        lang_breakdown = ""
        if not cand_langs:
            lang_score = 5
            lang_breakdown = "language:5/5 neutral"
        else:
            found_lang = None
            for lang in cand_langs:
                candidates = [lang]
                if lang in ("en", "english"):
                    candidates = ["en", "english", "английский"]
                elif lang in ("ru", "russian"):
                    candidates = ["ru", "russian", "русский"]
                for cand in candidates:
                    if len(cand) <= 2:
                        if re.search(rf"\b{re.escape(cand)}\b", full_text_lc):
                            found_lang = lang
                            break
                    else:
                        if cand in full_text_lc:
                            found_lang = lang
                            break
                if found_lang:
                    break

            if found_lang:
                lang_score = 5
                strengths.append(f"Language match: {found_lang}")
                lang_breakdown = "language:5/5"
            else:
                lang_score = 0
                gaps.append(f"Language mismatch: expected {', '.join(cand_langs[:3])}")
                lang_breakdown = "language:0/5"

        dimensions["language"] = {
            "score": lang_score,
            "max": 5,
        }

        # ---------------------------------------------------------------------
        # 9. DOMAIN-SPECIFIC EXPERIENCE & NEGATIVE PENALTIES
        # ---------------------------------------------------------------------
        negative_penalties = 0.0
        penalty_reasons: List[str] = []

        domain_years = self._p.get("domain_years", {})
        if vacancy_family in (RoleFamily.APPLICATION_SUPPORT, RoleFamily.TECH_SUPPORT, RoleFamily.SYSTEM_ADMIN):
            domain_cand_years = domain_years.get("it_support") or domain_years.get("application_support") or cand_years or 3
        elif vacancy_family == RoleFamily.AI_AUTOMATION:
            domain_cand_years = domain_years.get("automation") or domain_years.get("ai_llm") or cand_years or 3
        elif vacancy_family == RoleFamily.PYTHON_BACKEND:
            domain_cand_years = domain_years.get("python") or cand_years or 3
        else:
            domain_cand_years = cand_years if cand_years is not None else 3

        # Negative penalty 1: Seniority / Experience years gap
        req_years = extract_required_years(title, desc)
        if req_years is not None:
            if req_years > domain_cand_years + 1:
                gap_val = min(20.0, (req_years - domain_cand_years) * 5.0)
                negative_penalties += gap_val
                gap_msg = f"Experience gap: required {req_years}+ yrs vs domain experience {domain_cand_years:.1f} yrs (-{gap_val:.0f} pts)"
                penalty_reasons.append(gap_msg)
                gaps.append(gap_msg)

        dimensions["negative_penalties"] = {
            "score": -negative_penalties,
            "reasons": penalty_reasons,
        }

        # ---------------------------------------------------------------------
        # TOTAL SCORE & DECISION
        # ---------------------------------------------------------------------
        base_total = role_score + skills_score + seniority_score + loc_score + salary_score + emp_score + lang_score
        total = base_total - negative_penalties
        score = max(0, min(100, int(round(total))))

        # Decision thresholds:
        # P3 (Stretch) role semantics vs P1/P2 (Primary/Secondary)
        if hard_status == HardRequirementStatus.INELIGIBLE:
            decision = "SKIP"
            decision_class = MatchDecisionClass.REJECT.value
        elif hard_status == HardRequirementStatus.BORDERLINE:
            decision = "REVIEW"
            decision_class = MatchDecisionClass.BORDERLINE.value
        elif role_prio == RolePriority.P3.value:
            if score >= 80:
                decision = "APPLY"
                decision_class = MatchDecisionClass.STRETCH.value
            elif score >= 65:
                decision = "REVIEW"
                decision_class = MatchDecisionClass.STRETCH.value
            else:
                decision = "SKIP"
                decision_class = MatchDecisionClass.REJECT.value
        else:
            # P1 or P2 role
            low_confidence_match = False
            if matched_core:
                avg_conf = sum(_get_conf_wt(s) for s in matched_core) / len(matched_core)
                if avg_conf < 0.7:
                    low_confidence_match = True

            if score >= 90 and not low_confidence_match:
                decision = "APPLY"
                decision_class = MatchDecisionClass.STRONG_MATCH.value
            elif score >= 80:
                decision = "APPLY"
                decision_class = MatchDecisionClass.MATCH.value
            elif score >= 65:
                decision = "REVIEW"
                decision_class = MatchDecisionClass.BORDERLINE.value
            else:
                decision = "SKIP"
                decision_class = MatchDecisionClass.REJECT.value

        breakdown = [role_breakdown, skills_breakdown, seniority_breakdown, loc_breakdown, salary_breakdown, emp_breakdown, lang_breakdown]
        reasons = []
        if decision == "APPLY":
            reasons.append(f"Strong match {score}/100: " + ", ".join(breakdown))
            if strengths:
                reasons.append("Strengths: " + "; ".join(strengths[:3]))
            if gaps:
                reasons.append("Gaps: " + "; ".join(gaps[:2]))
        elif decision == "REVIEW":
            reasons.append(f"Moderate match {score}/100: " + ", ".join(breakdown))
            if gaps:
                reasons.append(f"Needs review: {'; '.join(gaps[:2])}")
            if strengths:
                reasons.append(f"Strengths: {'; '.join(strengths[:2])}")
        else:
            reasons.append(f"Low match {score}/100: " + ", ".join(breakdown))
            if gaps:
                reasons.append(f"Gaps: {'; '.join(gaps[:3])}")

        return MatchResult(
            score=score,
            decision=decision,
            decision_class=decision_class,
            eligibility="BORDERLINE" if hard_status == HardRequirementStatus.BORDERLINE else "ELIGIBLE",
            role_family=vacancy_family.value,
            role_priority=role_prio,
            reasons=reasons,
            strengths=strengths,
            gaps=gaps,
            dimensions=dimensions,
        )
