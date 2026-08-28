"""Remote Eligibility and Multi-Dimensional Vacancy Classification.

Evaluates vacancy eligibility across 6 distinct dimensions:
1. REMOTE MODE: remote / hybrid / onsite / unknown
2. GEO ELIGIBILITY: worldwide / thailand / regional / country_specific / unknown
3. WORK AUTHORIZATION: unrestricted / region_restricted / country_restricted / unknown
4. TIMEZONE: unrestricted / specified / strict / unknown
5. LANGUAGE: unrestricted / english / fluent_english / native_english / russian / other
6. EMPLOYMENT MODEL: worldwide_contractor / international / local_only / unknown

Provides overall eligibility determination: eligible / eligible_with_warning / ineligible / unknown
with structured, granular explanation reasons.
"""

from __future__ import annotations

import re
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schema import Vacancy
from .remote_filter import classify_work_format


class RemoteMode(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class GeoScope(str, Enum):
    WORLDWIDE = "worldwide"
    THAILAND = "thailand"
    REGIONAL = "regional"             # e.g. EU, LATAM, APAC, Americas
    COUNTRY_SPECIFIC = "country_specific" # e.g. US only, UK only, Germany only
    UNKNOWN = "unknown"


class WorkAuthorization(str, Enum):
    UNRESTRICTED = "unrestricted"       # Worldwide contractor / B2B / no local visa needed
    REGION_RESTRICTED = "region_restricted" # e.g. EU right to work
    COUNTRY_RESTRICTED = "country_restricted" # e.g. US Citizen / Green Card / W2 only
    UNKNOWN = "unknown"


class TimezoneRequirement(str, Enum):
    UNRESTRICTED = "unrestricted"       # Flexible / async / anywhere
    SPECIFIED = "specified"             # Overlap requested (e.g. UTC-5, EST 4h overlap)
    STRICT = "strict"                   # Must work exact local core hours (e.g. 9-5 PST)
    UNKNOWN = "unknown"


class LanguageRequirement(str, Enum):
    UNRESTRICTED = "unrestricted"       # None specified
    ENGLISH = "english"                 # Working / professional English
    FLUENT_ENGLISH = "fluent_english"   # Fluent / C1 / C2 / Advanced
    NATIVE_ENGLISH = "native_english"   # Native English speaker only
    RUSSIAN = "russian"                 # Russian language required
    OTHER = "other"                     # German, French, etc.


class EmploymentScope(str, Enum):
    WORLDWIDE_CONTRACTOR = "worldwide_contractor" # B2B / Contractor worldwide
    INTERNATIONAL = "international"               # EOR / Deel / Global remote
    LOCAL_ONLY = "local_only"                     # W2 / direct local employee only
    UNKNOWN = "unknown"


class EligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    ELIGIBLE_WITH_WARNING = "eligible_with_warning"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass
class EligibilityAssessment:
    remote_mode: RemoteMode
    geo_scope: GeoScope
    work_authorization: WorkAuthorization
    timezone_requirement: TimezoneRequirement
    language_requirement: LanguageRequirement
    employment_scope: EmploymentScope
    eligibility: EligibilityStatus
    eligibility_reasons: List[str] = field(default_factory=list)
    matched_regions: List[str] = field(default_factory=list)
    matched_countries: List[str] = field(default_factory=list)
    timezone_details: Optional[str] = None
    language_details: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "remote_mode": self.remote_mode.value,
            "geo_scope": self.geo_scope.value,
            "work_authorization": self.work_authorization.value,
            "timezone_requirement": self.timezone_requirement.value,
            "language_requirement": self.language_requirement.value,
            "employment_scope": self.employment_scope.value,
            "eligibility": self.eligibility.value,
            "eligibility_reasons": self.eligibility_reasons,
            "matched_regions": self.matched_regions,
            "matched_countries": self.matched_countries,
            "timezone_details": self.timezone_details,
            "language_details": self.language_details,
        }


# --- REGEX PATTERNS FOR CLASSIFIERS ---

# Exclusively remote platforms
_EXCLUSIVELY_REMOTE_SOURCES = {"remoteok", "weworkremotely", "himalayas"}

# 1. Geo Scope Patterns
_WORLDWIDE_GEO_PATTERNS = [
    r"\bworldwide\b",
    r"\bglobal\b",
    r"\banywhere\b",
    r"\bwork\s+from\s+anywhere\b",
    r"\banywhere\s+in\s+the\s+world\b",
    r"\bremote\s+worldwide\b",
    r"\bremote\s*\(worldwide\)\b",
    r"\bremote\s*\(global\)\b",
    r"\bremote\s*\(anywhere\)\b",
    r"\ball\s+countries\b",
    r"\bno\s+location\s+restrictions\b",
    r"\bвесь\s+мир\b",
    r"\bпо\s+всему\s+миру\b",
    r"\bв\s+любой\s+точке\s+мира\b",
    r"\bиз\s+любой\s+точки\s+мира\b",
]

_THAILAND_PATTERNS = [
    r"\bthailand\b",
    r"\bтайланд\b",
    r"\bтаиланд\b",
    r"\bbangkok\b",
    r"\bphuket\b",
]

_COUNTRY_SPECIFIC_PATTERNS: Dict[str, List[str]] = {
    "US": [
        r"\bus\s+only\b",
        r"\bu\.s\.\s+only\b",
        r"\busa\s+only\b",
        r"\bunited\s+states\s+only\b",
        r"\bmust\s+reside\s+in\s+(?:the\s+)?(?:us|usa|united\s+states)\b",
        r"\bmust\s+be\s+located\s+in\s+(?:the\s+)?(?:us|usa|united\s+states)\b",
        r"\bbased\s+in\s+(?:the\s+)?(?:us|usa|united\s+states)\s+only\b",
        r"\bus\s+residents?\s+only\b",
        r"\bus\s+applicants?\s+only\b",
        r"\bus-only\b",
    ],
    "UK": [
        r"\buk\s+only\b",
        r"\bu\.k\.\s+only\b",
        r"\bunited\s+kingdom\s+only\b",
        r"\bmust\s+reside\s+in\s+(?:the\s+)?uk\b",
        r"\bmust\s+be\s+located\s+in\s+(?:the\s+)?uk\b",
        r"\buk\s+residents?\s+only\b",
        r"\buk-only\b",
    ],
    "Germany": [
        r"\bgermany\s+only\b",
        r"\bmust\s+reside\s+in\s+germany\b",
        r"\bmust\s+be\s+located\s+in\s+germany\b",
        r"\bgermany\s+residents?\s+only\b",
        r"\bdeutschland\s+only\b",
    ],
    "Canada": [
        r"\bcanada\s+only\b",
        r"\bmust\s+reside\s+in\s+canada\b",
        r"\bmust\s+be\s+located\s+in\s+canada\b",
        r"\bcanada\s+residents?\s+only\b",
    ],
    "Russia": [
        r"\bтолько\s+(?:в\s+)?рф\b",
        r"\bтолько\s+россия\b",
        r"\bпроживание\s+в\s+рф\b",
        r"\bнахождение\s+в\s+рф\b",
        r"\bлокация\s*:\s*рф\b",
        r"\bтолько\s+на\s+территории\s+рф\b",
    ],
}

_REGIONAL_PATTERNS: Dict[str, List[str]] = {
    "EU": [
        r"\beu\s+only\b",
        r"\beurope\s+only\b",
        r"\beu\/eea\s+only\b",
        r"\bmust\s+reside\s+in\s+(?:the\s+)?eu\b",
        r"\beuropean\s+union\s+only\b",
        r"\beu\s+residents?\s+only\b",
    ],
    "LATAM": [
        r"\blatam\s+only\b",
        r"\blatin\s+america\s+only\b",
    ],
    "APAC": [
        r"\bapac\s+only\b",
        r"\basia\s+pacific\s+only\b",
        r"\basia\s+only\b",
    ],
    "Americas": [
        r"\bamericas\s+only\b",
        r"\bnorth\s+america\s+only\b",
    ],
}

# 2. Work Authorization Patterns
_WORK_AUTH_COUNTRY_PATTERNS = [
    r"\bus\s+citizens?(?:\s+or\s+green\s+card)?\b",
    r"\bgreen\s+card\b",
    r"\bw2\s+only\b",
    r"\bw-2\s+only\b",
    r"\bus\s+work\s+authorization\s+(?:is\s+)?required\b",
    r"\bauthorized\s+to\s+work\s+in\s+(?:the\s+)?(?:us|usa|united\s+states)\b",
    r"\bno\s+sponsorship\b",
    r"\bwithout\s+sponsorship\b",
    r"\buk\s+right\s+to\s+work\b",
    r"\bright\s+to\s+work\s+in\s+(?:the\s+)?uk\b",
    r"\bналоговый\s+резидент\s+рф\b",
]

_WORK_AUTH_REGION_PATTERNS = [
    r"\beu\s+work\s+permit\b",
    r"\beu\s+citizenship\s+required\b",
    r"\beligible\s+to\s+work\s+in\s+(?:the\s+)?(?:eu|europe)\b",
]

_WORK_AUTH_UNRESTRICTED_PATTERNS = [
    r"\bworldwide\s+contractor\b",
    r"\binternational\s+contractor\b",
    r"\bb2b\s+contract\b",
    r"\bb2b\b",
    r"\bcontractor\s+anywhere\b",
    r"\bwe\s+hire\s+globally\b",
]

# 3. Timezone Patterns
_TIMEZONE_STRICT_PATTERNS = [
    r"\bstrict\s+(?:core\s+hours|9[- ]to[- ]5|hours)\b",
    r"\bmust\s+work\s+(?:exact\s+)?9\s*(?:am)?\s*[-–to]\s*5\s*(?:pm)?\s*(?:est|pst|cst|cet|gmt|msk)\b",
    r"\bобязательно\s+строго\s+с\s+9\s+до\s+18\b",
]

_TIMEZONE_SPECIFIED_PATTERNS = [
    r"\b(?:overlap|hours?\s+overlap)\s+with\s+(?:est|pst|cst|cet|gmt|utc|us|europe|new\s+york)\b",
    r"\b\d+\s*[-–]?\s*\d*\s*hours?\s+(?:of\s+)?overlap\b",
    r"\b(?:utc|gmt)\s*[-+]\s*\d+\b",
    r"\b(?:est|pst|cst|cet|mst|utc|gmt)\s+timezone\b",
    r"\btimezone\s*:\s*[^\n,;]{2,30}\b",
    r"\btime\s+zone\s*:\s*[^\n,;]{2,30}\b",
    r"\bчасовой\s+пояс\s*:\s*[^\n,;]{2,30}\b",
    r"\bпересечение\s+с\s+(?:мск|utc|европ|сша)\b",
]

_TIMEZONE_UNRESTRICTED_PATTERNS = [
    r"\basync\b",
    r"\basynchronous\b",
    r"\bany\s+timezone\b",
    r"\bflexible\s+hours\b",
    r"\bwork\s+whenever\s+you\s+want\b",
    r"\bno\s+timezone\s+restrictions\b",
    r"\bсвободный\s+график\b",
    r"\bгибкие\s+часы\b",
]

# 4. Language Patterns
_LANG_NATIVE_PATTERNS = [
    r"\bnative\s+english\b",
    r"\bnative\s+english\s+speaker\b",
    r"\bnative\s+speaker\s+only\b",
    r"\benglish\s+as\s+a\s+mother\s+tongue\b",
    r"\bmother[- ]tongue\s+english\b",
]

_LANG_FLUENT_PATTERNS = [
    r"\bfluent\s+english\b",
    r"\bfluent\s+in\s+english\b",
    r"\benglish\s*:\s*fluent\b",
    r"\benglish\s*:\s*(?:c1|c2|advanced)\b",
    r"\b(?:c1|c2)\s+english\b",
    r"\badvanced\s+english\b",
    r"\bexcellent\s+(?:written\s+and\s+spoken\s+)?english\b",
    r"\bсвободный\s+английский\b",
    r"\bанглийский\s+(?:c1|c2|advanced|свободный)\b",
]

_LANG_ENGLISH_PATTERNS = [
    r"\benglish\s+required\b",
    r"\benglish\s*:\s*(?:b1|b2|intermediate|working|professional)\b",
    r"\b(?:b1|b2)\s+english\b",
    r"\bworking\s+english\b",
    r"\bprofessional\s+english\b",
    r"\bproficient\s+in\s+english\b",
    r"\bspoken\s+english\b",
    r"\bзнание\s+английского\b",
    r"\bанглийский\s+язык\b",
    r"\bанглийский\s+(?:b1|b2|intermediate)\b",
]

_LANG_RUSSIAN_PATTERNS = [
    r"\bрусский\s+язык\b",
    r"\bзнание\s+русского\s+языка\b",
    r"\brussian\s+required\b",
    r"\brussian\s+speaking\b",
    r"\brussian\s+language\b",
    r"\bсвободный\s+русский\b",
]

_LANG_OTHER_PATTERNS = [
    r"\bfluent\s+german\b",
    r"\bgerman\s+required\b",
    r"\bfluent\s+french\b",
    r"\bfrench\s+required\b",
    r"\bfluent\s+spanish\b",
    r"\bspanish\s+required\b",
]

# 5. Employment Scope Patterns
_EMP_CONTRACTOR_PATTERNS = [
    r"\bworldwide\s+contractor\b",
    r"\binternational\s+contractor\b",
    r"\bb2b\s+contract\b",
    r"\bb2b\b",
    r"\bcontractor\b",
    r"\bcontract\s+role\b",
    r"\bfreelance\b",
    r"\bип\b",
    r"\bсамозанят\w*",
    r"\bгпх\b",
]

_EMP_INTERNATIONAL_PATTERNS = [
    r"\bglobal\s+remote\b",
    r"\bremote\s+worldwide\b",
    r"\bdeel\b",
    r"\bremote\.com\b",
    r"\beor\b",
    r"\bemployer\s+of\s+record\b",
]

_EMP_LOCAL_ONLY_PATTERNS = [
    r"\bw2\s+only\b",
    r"\bw-2\s+only\b",
    r"\bdirect\s+w2\b",
    r"\blocal\s+payroll\s+only\b",
    r"\bno\s+c2c\b",
    r"\bno\s+corp-to-corp\b",
    r"\bтолько\s+по\s+тк\s+рф\b",
]


# --- CLASSIFIER IMPLEMENTATIONS ---

def classify_remote_mode(title: str, description: str, location: Optional[str], source: Optional[str]) -> RemoteMode:
    is_remote, reason = classify_work_format(title=title, description=description, location=location, source=source)
    if is_remote:
        return RemoteMode.REMOTE

    combined = f"{title} {description} {location or ''}".lower()
    if any(k in combined for k in ("hybrid", "гибрид", "смешанный")):
        return RemoteMode.HYBRID
    if any(k in combined for k in ("onsite", "on-site", "office", "офис", "территории работодателя")):
        return RemoteMode.ONSITE
    return RemoteMode.UNKNOWN


def classify_geo_scope(
    title: str,
    description: str,
    location: Optional[str],
    source: Optional[str],
    country_restrictions: Optional[List[str]] = None,
) -> Tuple[GeoScope, List[str], List[str]]:
    """Detects geo scope, matched countries and matched regions."""
    src = (source or "").strip().lower()
    loc = (location or "").strip()
    combined = f"{title} {description} {loc}".lower()
    restrictions = [str(c).upper() for c in (country_restrictions or [])]

    # Explicit country restrictions list check
    if restrictions:
        # Check if Thailand or Worldwide is present
        if "WORLDWIDE" in restrictions or "ANYWHERE" in restrictions or "GLOBAL" in restrictions:
            return GeoScope.WORLDWIDE, [], []
        if "TH" in restrictions or "THAILAND" in restrictions:
            return GeoScope.THAILAND, ["Thailand"], []
        # Region or country specific
        return GeoScope.COUNTRY_SPECIFIC, restrictions, []

    # Check Thailand
    for p in _THAILAND_PATTERNS:
        if re.search(p, combined, re.IGNORECASE):
            return GeoScope.THAILAND, ["Thailand"], []

    # Check Country Specific
    matched_countries: List[str] = []
    for country, patterns in _COUNTRY_SPECIFIC_PATTERNS.items():
        for p in patterns:
            if re.search(p, combined, re.IGNORECASE):
                matched_countries.append(country)
                break

    if matched_countries:
        return GeoScope.COUNTRY_SPECIFIC, matched_countries, []

    # Check Regional
    matched_regions: List[str] = []
    for region, patterns in _REGIONAL_PATTERNS.items():
        for p in patterns:
            if re.search(p, combined, re.IGNORECASE):
                matched_regions.append(region)
                break

    if matched_regions:
        return GeoScope.REGIONAL, [], matched_regions

    # Check Worldwide
    for p in _WORLDWIDE_GEO_PATTERNS:
        if re.search(p, combined, re.IGNORECASE):
            return GeoScope.WORLDWIDE, [], []

    # Exclusively remote sources with clean location default to Worldwide
    if src in _EXCLUSIVELY_REMOTE_SOURCES:
        if not loc or loc.lower() in ("remote", "worldwide", "global", "anywhere"):
            return GeoScope.WORLDWIDE, [], []

    return GeoScope.UNKNOWN, [], []


def classify_work_auth(title: str, description: str) -> Tuple[WorkAuthorization, Optional[str]]:
    text = f"{title} {description}".lower()

    for p in _WORK_AUTH_COUNTRY_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return WorkAuthorization.COUNTRY_RESTRICTED, m.group(0)

    for p in _WORK_AUTH_REGION_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return WorkAuthorization.REGION_RESTRICTED, m.group(0)

    for p in _WORK_AUTH_UNRESTRICTED_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return WorkAuthorization.UNRESTRICTED, m.group(0)

    return WorkAuthorization.UNKNOWN, None


def classify_timezone(
    title: str,
    description: str,
    location: Optional[str],
    timezone_restrictions: Optional[List[Any]] = None,
) -> Tuple[TimezoneRequirement, Optional[str]]:
    text = f"{title} {description} {location or ''}".lower()

    # Check structured tz restrictions
    if timezone_restrictions:
        tz_str = ", ".join(str(x) for x in timezone_restrictions)
        return TimezoneRequirement.SPECIFIED, f"Timezone restrictions: {tz_str}"

    for p in _TIMEZONE_STRICT_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return TimezoneRequirement.STRICT, m.group(0)

    for p in _TIMEZONE_SPECIFIED_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return TimezoneRequirement.SPECIFIED, m.group(0)

    for p in _TIMEZONE_UNRESTRICTED_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return TimezoneRequirement.UNRESTRICTED, m.group(0)

    return TimezoneRequirement.UNKNOWN, None


def classify_language(title: str, description: str) -> Tuple[LanguageRequirement, Optional[str]]:
    text = f"{title} {description}".lower()

    for p in _LANG_NATIVE_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return LanguageRequirement.NATIVE_ENGLISH, m.group(0)

    for p in _LANG_FLUENT_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return LanguageRequirement.FLUENT_ENGLISH, m.group(0)

    for p in _LANG_ENGLISH_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return LanguageRequirement.ENGLISH, m.group(0)

    for p in _LANG_RUSSIAN_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return LanguageRequirement.RUSSIAN, m.group(0)

    for p in _LANG_OTHER_PATTERNS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return LanguageRequirement.OTHER, m.group(0)

    return LanguageRequirement.UNRESTRICTED, None


def classify_employment_scope(title: str, description: str) -> EmploymentScope:
    text = f"{title} {description}".lower()

    for p in _EMP_LOCAL_ONLY_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return EmploymentScope.LOCAL_ONLY

    for p in _EMP_CONTRACTOR_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return EmploymentScope.WORLDWIDE_CONTRACTOR

    for p in _EMP_INTERNATIONAL_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return EmploymentScope.INTERNATIONAL

    return EmploymentScope.UNKNOWN


# --- OVERALL ELIGIBILITY EVALUATOR ---

def assess_vacancy_eligibility(
    vacancy: Vacancy,
    candidate_country: str = "TH",
    candidate_languages: Optional[List[str]] = None,
) -> EligibilityAssessment:
    """Perform full multi-dimensional eligibility assessment for candidate in Thailand."""
    title = vacancy.title or ""
    desc = vacancy.description or ""
    loc = vacancy.location
    src = vacancy.source
    c_restr = vacancy.country_restrictions
    tz_restr = vacancy.timezone_restrictions

    cand_langs = [l.lower() for l in (candidate_languages or ["en", "ru"])]

    # 1. Classify individual dimensions
    remote_mode = classify_remote_mode(title, desc, loc, src)
    geo_scope, matched_countries, matched_regions = classify_geo_scope(title, desc, loc, src, c_restr)
    work_auth, auth_detail = classify_work_auth(title, desc)
    tz_req, tz_detail = classify_timezone(title, desc, loc, tz_restr)
    lang_req, lang_detail = classify_language(title, desc)
    emp_scope = classify_employment_scope(title, desc)

    reasons: List[str] = []

    # 2. Evaluate Remote Mode
    if remote_mode == RemoteMode.ONSITE:
        reasons.append("INELIGIBLE: Onsite work required")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    if remote_mode == RemoteMode.HYBRID:
        reasons.append("INELIGIBLE: Hybrid work / office attendance required")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    if remote_mode == RemoteMode.UNKNOWN:
        reasons.append("UNKNOWN_REMOTE: Work format is not confirmed remote")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.UNKNOWN,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    # 3. Evaluate Geo Scope & Country Restrictions
    if geo_scope == GeoScope.COUNTRY_SPECIFIC:
        reasons.append(f"INELIGIBLE: Country restricted to {', '.join(matched_countries)} (candidate in {candidate_country})")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    if geo_scope == GeoScope.REGIONAL:
        reasons.append(f"INELIGIBLE: Region restricted to {', '.join(matched_regions)} (candidate in {candidate_country})")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    # 4. Evaluate Work Authorization & Employment Model
    if work_auth == WorkAuthorization.COUNTRY_RESTRICTED:
        reasons.append(f"INELIGIBLE: Country work authorization required ({auth_detail})")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    if work_auth == WorkAuthorization.REGION_RESTRICTED:
        reasons.append(f"INELIGIBLE: Regional work authorization required ({auth_detail})")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    if emp_scope == EmploymentScope.LOCAL_ONLY:
        reasons.append("INELIGIBLE: Local payroll / W2 only (no international contractor support)")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    # 5. Evaluate Language Requirement
    if lang_req == LanguageRequirement.NATIVE_ENGLISH:
        reasons.append(f"INELIGIBLE: LANGUAGE_RESTRICTION ({lang_detail})")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    if lang_req == LanguageRequirement.OTHER:
        reasons.append(f"INELIGIBLE: Non-English/Russian language required ({lang_detail})")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.INELIGIBLE,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    # 6. Evaluate Unknown Geolocation
    if geo_scope == GeoScope.UNKNOWN:
        reasons.append("GEO_UNKNOWN: Geolocation eligibility not explicitly specified")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.UNKNOWN,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    # 7. Check Timezone Warnings
    if tz_req in (TimezoneRequirement.SPECIFIED, TimezoneRequirement.STRICT):
        reasons.append(f"ELIGIBLE_WITH_TIMEZONE_WARNING: {tz_detail} (candidate in Thailand UTC+7)")
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=EligibilityStatus.ELIGIBLE_WITH_WARNING,
            eligibility_reasons=reasons,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
        )

    # 8. Fully Eligible
    reasons.append("ELIGIBLE: Confirmed remote-eligible for candidate in Thailand")
    return EligibilityAssessment(
        remote_mode=remote_mode,
        geo_scope=geo_scope,
        work_authorization=work_auth,
        timezone_requirement=tz_req,
        language_requirement=lang_req,
        employment_scope=emp_scope,
        eligibility=EligibilityStatus.ELIGIBLE,
        eligibility_reasons=reasons,
        matched_regions=matched_regions,
        matched_countries=matched_countries,
        timezone_details=tz_detail,
        language_details=lang_detail,
    )
