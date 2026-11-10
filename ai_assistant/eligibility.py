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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .remote_filter import classify_work_format
from .schema import Vacancy


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


# --- T7.4: EMPLOYER-FIRST GEOGRAPHY AND A/B/C BANDS ---
#
# Two failure modes this section exists to fix:
#
# 1. A board's permissive label ("Anywhere in the World") was read as the
#    employer's permission, so a posting whose own text says "Applicants must be
#    based in the USA" came out ELIGIBLE for a candidate in Thailand.
# 2. A board's structured location field carries a country ("... United States of
#    America, Oregon"). That is a restriction signal, and it was ignored.
#
# Rules enforced below:
#   * the employer's own words outrank the board's label;
#   * a board label can never create permission -- it can only produce B;
#   * absent evidence is B, never A;
#   * contradictory evidence is reported as a conflict and never auto-approved.
#
# The band answers the geographic question only. Overall suitability (language,
# work authorization, on-site attendance) is still decided by the status ladder
# in assess_vacancy_eligibility.


class GeoBand(str, Enum):
    """Employer-first geographic verdict for a candidate based in Thailand."""

    A = "A"  # employer explicitly allows Thailand or any country, nothing contradicts
    B = "B"  # remote, but availability from Thailand is not established
    C = "C"  # employer restricts to another country, requires office, or evidence conflicts


class GeoEvidence(str, Enum):
    """Where the strongest geographic statement came from."""

    EMPLOYER = "employer"
    PLATFORM = "platform"
    NONE = "none"


# Place names a board can put in its structured location field. A hit means the
# posting names a place other than Thailand. The bare US abbreviations sit LAST
# in the alternation so that a longer name ("Belarus") wins and the inner "us"
# is never matched on its own.
_PLACE_NAME_ALT = (
    r"(?:the\s+)?(?:united\s+states(?:\s+of\s+america)?|uk|u\.k\.|"
    r"united\s+kingdom(?:\s+of\s+great\s+britain(?:\s+and\s+northern\s+ireland)?)?|"
    r"canada|germany|deutschland|france|spain|portugal|poland|netherlands|ireland|"
    r"italy|sweden|norway|denmark|finland|switzerland|austria|czech\w*|romania|"
    r"ukraine|belarus|russia|india|philippines|indonesia|vietnam|japan|china|korea|"
    r"singapore|malaysia|australia|new\s+zealand|brazil|mexico|argentina|colombia|"
    r"chile|peru|south\s+africa|nigeria|kenya|egypt|israel|turkey|uae|"
    r"saudi\s+arabia|serbia|серби\w*|рф|росси\w+|москв\w*|санкт-петербург\w*|u\.?s\.?a?)"
)

# T7.7: the module knew the abbreviation "РФ" but not the full name, so a posting
# that wrote "удаленный формат работы - строго в пределах Российской Федерации"
# (live row hh:137448071, YCLIENTS) stated a hard geographic restriction and was
# still read as merely "unknown". Both spellings now resolve to one jurisdiction.
_RF_ALT = r"(?:рф|российск\w*\s+федераци\w*|росси[яи])"

# The employer's own restriction sentence. Any hit means the employer named a
# jurisdiction, so the posting cannot be offered as available from Thailand.
_EMPLOYER_RESTRICTION_PATTERNS: list[str] = [
    rf"\bmust\s+be\s+based\s+in\s+{_PLACE_NAME_ALT}\b",
    rf"\bmust\s+(?:reside|live)\s+in\s+{_PLACE_NAME_ALT}\b",
    rf"\bmust\s+be\s+located\s+in\s+{_PLACE_NAME_ALT}\b",
    rf"\bmust\s+be\s+a\s+resident\s+of\s+{_PLACE_NAME_ALT}\b",
    rf"\b(?:candidates?|applicants?|you)\s+(?:must\s+be\s+)?based\s+in\s+{_PLACE_NAME_ALT}\b",
    rf"\beligible\s+for\s+employment\s+in\s+{_PLACE_NAME_ALT}\b",
    rf"\bauthori[sz]ed\s+to\s+work\s+in\s+{_PLACE_NAME_ALT}\b",
    rf"\bright\s+to\s+work\s+in\s+{_PLACE_NAME_ALT}\b",
    r"\b(?:us|u\.s\.|usa|united\s+states|uk|canada|germany|eu)[- ]based\b",
    r"\bus\s+remote\b",
    r"\bu\.s\.\s+remote\b",
    r"\bus\s+only\b",
    r"\buk\s+only\b",
    r"\bcanada\s+only\b",
    r"\beu\s+only\b",
    r"\beurope\s+only\b",
    r"\bresidents?\s+only\b",
    r"\bapplicants?\s+only\b",
    r"\b(?:us|u\.s\.|united\s+states)\s+work\s+authorization\b",
    r"\bwork\s+authorization\s+(?:is\s+)?required\b",
    r"\bno\s+sponsorship\b",
    r"\bwithout\s+sponsorship\b",
    r"\bw-?2\s+only\b",
    r"\bgreen\s+card\b",
    r"\bus\s+citizens?\b",
    rf"\bтолько\s+(?:в\s+)?{_RF_ALT}\b",
    rf"\bпроживани\w*\s+в\s+{_RF_ALT}\b",
    rf"\bнахождени\w*\s+в\s+{_RF_ALT}\b",
    rf"\bтолько\s+на\s+территории\s+{_RF_ALT}\b",
    rf"\bгражданств\w*\s+{_RF_ALT}\b",
    rf"\bналогов\w*\s+резидент\w*\s+{_RF_ALT}\b",
    r"\bтолько\s+для\s+(?:граждан|резидентов)\b",
    # T7.7: the work-TERRITORY form. "в пределах Российской Федерации" bounds
    # where the employee may work; it is not a statement about where the company
    # is registered. Deliberately narrow: the bare "на территории РФ" is left out
    # so that a company description cannot read as a work restriction.
    rf"\bв\s+пределах\s+{_RF_ALT}\b",
]

# An explicit permission in the employer's own words. This is the ONLY route to
# band A -- a board label never gets here.
_EXPLICIT_EMPLOYER_WORLDWIDE_PATTERNS: list[str] = [
    r"\bwork\s+from\s+anywhere\b",
    r"\bwork\s+from\s+any\s+(?:country|location|place)\b",
    r"\banywhere\s+in\s+the\s+world\b",
    r"\bremote\s+worldwide\b",
    r"\bworldwide\s+remote\b",
    r"\bremote\s*\(\s*worldwide\s*\)",
    r"\bremote\s*\(\s*global\s*\)",
    r"\bremote\s*\(\s*anywhere\s*\)",
    r"\bglobal\s+remote\b",
    r"\bno\s+location\s+restrictions?\b",
    r"\ball\s+countries\b",
    r"\bworldwide\s+contractors?\b",
    r"\bhire[sd]?\s+(?:globally|worldwide|anywhere)\b",
    r"\bwe\s+hire\s+globally\b",
    r"\binternational\s+contractors?\b",
    r"\bcontractor\s+anywhere\b",
    # NOT "\bfully distributed\b": that describes how the company is organised
    # ("we're a fully distributed company"), not where the candidate may live.
    # Measured on live data it produced band A for postings that never permitted
    # Thailand, so it was removed rather than kept as a loose signal.
    r"\bвесь\s+мир\b",
    r"\bпо\s+всему\s+миру\b",
    r"\bв\s+любой\s+точке\s+мира\b",
    r"\bиз\s+любой\s+точки\s+мира\b",
    r"\bиз\s+любой\s+страны\b",
    r"\bлюбая\s+страна\b",
]

# --- T7.7: a worldwide phrase is permission only when it is about the CANDIDATE --
#
# The pattern list above reads the *words*, not the grammar. Live row
# hh:137574152 (Servers.com, "Technical Support Engineer (Serbia)") contains
# "администрировать серверы из любой точки мира" / "system administration of
# servers from anywhere in the world". That sentence says where the *servers* can
# be reached from; it says nothing about where the employee may live, and the same
# posting states the position is in Serbia. The pattern lifted the row to band A
# anyway.
#
# The guard therefore looks at the words immediately BEFORE the phrase. If the
# phrase attaches to a thing the work is done on -- equipment, infrastructure,
# clients, systems -- it is an object-of-work phrase and is not permission.
# Genuine permissions ("Work from anywhere", "you may work from any country")
# have no such noun in front of them and keep producing band A.
_OBJECT_OF_WORK_NOUN = (
    r"servers?|systems?|networks?|hardware|software|equipment|devices?|machines?|"
    r"clients?|customers?|databases?|applications?|apps?|services?|environments?|"
    r"websites?|sites?|accounts?|projects?|tasks?|workloads?|hosts?|clusters?|"
    r"containers?|endpoints?|computers?|laptops?|routers?|switches|firewalls?|"
    r"storage|data\s*cent(?:er|re)s?|platforms?|infrastructure|monitoring|"
    r"issues?|problems?|errors?|incidents?|tickets?|requests?|alerts?|cases?|"
    r"сервер\w*|систем\w*|сет\w*|оборудован\w*|устройств\w*|клиент\w*|"
    r"баз\w*\s+данн\w*|приложени\w*|сервис\w*|проект\w*|задач\w*|"
    r"инфраструктур\w*|компьютер\w*|ноутбук\w*|платформ\w*|мониторинг\w*|"
    r"обращени\w*|заявк\w*|инцидент\w*|ошибк\w*"
)

# The noun must sit at the TAIL of the window, optionally followed by a manner
# adverb and a preposition ("servers from", "серверы из"). Anything else in
# between means the phrase is not attached to that noun.
_OBJECT_OF_WORK_TAIL_RE = re.compile(
    rf"\b(?:{_OBJECT_OF_WORK_NOUN})\b[\s,;:]*"
    r"(?:(?:remotely|from|via|at|in|из|с|со)\b[\s,;:]*)*$",
    re.IGNORECASE,
)
_OBJECT_OF_WORK_WINDOW = 70

# --- T7.7: where the EMPLOYER says the position itself is -----------------------
#
# "Technical Support Engineer (Serbia)", "We are looking for a technical support
# engineer in Serbia.", "Ищем в команду инженера технической поддержки в Сербии."
# None of these is a rule ("must be based in X"); they are place-of-work
# statements, and none of the restriction patterns above reads them. A posting
# that names a country other than the candidate's for the position itself is a
# geographic restriction, so it is read here.
_EMPLOYER_ROLE_LOCATION_PATTERNS: list[str] = [
    # "We are looking for a technical support engineer in Serbia."
    (
        rf"\b(?:looking\s+for|seeking|hiring|recruiting|recruit)\b[^.!?]{{0,90}}?"
        rf"\b(?:in|at|based\s+in)\s+{_PLACE_NAME_ALT}\b"
    ),
    # "Ищем в команду инженера технической поддержки в Сербии."
    rf"\bищ(?:ем|ет|ут)\b[^.!?]{{0,90}}?\bв\s+{_PLACE_NAME_ALT}\b",
    # "The position is based in Serbia." / "vacancy located in Serbia"
    rf"\b(?:position|role|vacancy|job)\b[^.!?]{{0,40}}?\b(?:based|located)\s+in\s+{_PLACE_NAME_ALT}\b",
    # "Location: Serbia" / "Место работы: Сербия"
    rf"\b(?:location|office|место\s+работы)\s*[:\-]\s*{_PLACE_NAME_ALT}\b",
]

# The title is matched on its own so that "(Serbia)" is read as the position's
# place and not as an aside in the middle of the description.
_TITLE_PLACE_RE = re.compile(rf"\(\s*{_PLACE_NAME_ALT}\s*\)\s*$", re.IGNORECASE)

# Board labels that claim geographic openness. They are metadata, not permission.
_PLATFORM_PERMISSIVE_GEO_PATTERNS: list[str] = [
    r"anywhere\s+in\s+the\s+world",
    r"\bworldwide\b",
    r"\bglobal\b",
    r"\banywhere\b",
    r"\ball\s+countries\b",
    r"\bno\s+location\s+restrictions?\b",
]

# Values of the location field that name a work FORMAT, not a place. They must
# not be read as geographic evidence in either direction.
_LOCATION_FORMAT_LABELS: frozenset[str] = frozenset(
    {
        "remote",
        "remote worldwide",
        "remote (worldwide)",
        "remote (global)",
        "remote anywhere",
        "remote, worldwide",
        "anywhere",
        "worldwide",
        "global",
        "удаленно",
        "удаленная",
        "удаленная работа",
        "дистанционно",
        "полный рабочий день",
        "полная занятость",
        "full time",
        "part time",
    }
)

_COMPILED_EMPLOYER_RESTRICTION = [
    re.compile(p, re.IGNORECASE) for p in _EMPLOYER_RESTRICTION_PATTERNS
]
_COMPILED_EMPLOYER_WORLDWIDE = [
    re.compile(p, re.IGNORECASE) for p in _EXPLICIT_EMPLOYER_WORLDWIDE_PATTERNS
]
_COMPILED_PLATFORM_PERMISSIVE = [
    re.compile(p, re.IGNORECASE) for p in _PLATFORM_PERMISSIVE_GEO_PATTERNS
]
_COMPILED_EMPLOYER_ROLE_LOCATION = [
    re.compile(p, re.IGNORECASE) for p in _EMPLOYER_ROLE_LOCATION_PATTERNS
]
_COMPILED_PLACE_TAGS = [
    re.compile(
        rf"(?<![A-Za-z])(?:{pattern})(?![A-Za-z])",
        re.IGNORECASE,
    )
    for pattern in (
        _PLACE_NAME_ALT,
        r"🇺🇸",
        r"🇬🇧",
        r"🇨🇦",
        r"🇩🇪",
        r"🇫🇷",
        r"🇪🇸",
        r"🇵🇹",
        r"🇵🇱",
        r"🇳🇱",
        r"🇮🇪",
        r"🇮🇹",
        r"🇸🇪",
        r"🇳🇴",
        r"🇩🇰",
        r"🇫🇮",
        r"🇨🇭",
        r"🇦🇹",
        r"🇨🇿",
        r"🇷🇴",
        r"🇺🇦",
        r"🇧🇾",
        r"🇮🇳",
        r"🇵🇭",
        r"🇮🇩",
        r"🇻🇳",
        r"🇯🇵",
        r"🇨🇳",
        r"🇰🇷",
        r"🇸🇬",
        r"🇲🇾",
        r"🇦🇺",
        r"🇳🇿",
        r"🇧🇷",
        r"🇲🇽",
        r"🇦🇷",
        r"🇨🇴",
        r"🇨🇱",
        r"🇵🇪",
        r"🇿🇦",
        r"🇳🇬",
        r"🇰🇪",
        r"🇪🇬",
        r"🇮🇱",
        r"🇹🇷",
        r"🇦🇪",
        r"🇸🇦",
        r"🇷🇺",
    )
]

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

# Russian cities are not enumerated: any Cyrillic location that is not a format
# label names a place in the Russian-language space.
_CYRILLIC_PLACE_LABEL = "RU/CIS"


@dataclass
class GeoVerdict:
    """Employer-first geographic verdict with the evidence that produced it."""

    band: GeoBand
    reasons: list[str] = field(default_factory=list)
    evidence: GeoEvidence = GeoEvidence.NONE
    conflict: bool = False
    conflict_details: list[str] = field(default_factory=list)
    employer_restriction: str | None = None
    employer_allows_thailand: bool = False
    employer_allows_any_country: bool = False
    platform_places: list[str] = field(default_factory=list)
    platform_permissive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "band": self.band.value,
            "reasons": self.reasons,
            "evidence": self.evidence.value,
            "conflict": self.conflict,
            "conflict_details": self.conflict_details,
            "employer_restriction": self.employer_restriction,
            "employer_allows_thailand": self.employer_allows_thailand,
            "employer_allows_any_country": self.employer_allows_any_country,
            "platform_places": self.platform_places,
            "platform_permissive": self.platform_permissive,
        }


def platform_place_tags(location: str | None) -> list[str]:
    """Places named in a board's structured location field (never format labels)."""
    loc = (location or "").strip()
    if not loc:
        return []
    if loc.lower() in _LOCATION_FORMAT_LABELS:
        return []

    found: list[str] = []
    for pattern in _COMPILED_PLACE_TAGS:
        for match in pattern.finditer(loc):
            token = match.group(0).strip()
            if token and token not in found:
                found.append(token)
    if _CYRILLIC_RE.search(loc) and _CYRILLIC_PLACE_LABEL not in found:
        found.append(_CYRILLIC_PLACE_LABEL)
    return found


def _phrase_fragment(text: str, match: Any, pad: int = 45) -> str:
    """The matched phrase with a little context, for an auditable reason line."""
    lo = max(0, match.start() - pad)
    hi = min(len(text), match.end() + pad)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def _object_of_work_worldwide_match(text: str) -> Any | None:
    """The first worldwide phrase that is about a thing, not about the candidate.

    Returns the ``re.Match`` that was rejected, or ``None``. Used only to explain
    the verdict; ``_employee_worldwide_match`` is what grants band A.
    """
    for pattern in _COMPILED_EMPLOYER_WORLDWIDE:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - _OBJECT_OF_WORK_WINDOW):match.start()]
            if _OBJECT_OF_WORK_TAIL_RE.search(window):
                return match
    return None


def _employee_worldwide_match(text: str) -> Any | None:
    """The first worldwide phrase that is genuinely about the candidate.

    A phrase whose object is equipment, infrastructure, clients or systems
    describes what the work is done *on*; it is skipped, and the remaining
    patterns are still tried, so a posting that says both ("administer servers
    from anywhere in the world; work from anywhere") keeps its band A.
    """
    for pattern in _COMPILED_EMPLOYER_WORLDWIDE:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - _OBJECT_OF_WORK_WINDOW):match.start()]
            if not _OBJECT_OF_WORK_TAIL_RE.search(window):
                return match
    return None


def classify_geo_band(
    *,
    title: str,
    description: str,
    location: str | None,
    remote_mode: RemoteMode,
    matched_regions: list[str] | None = None,
    work_authorization: WorkAuthorization = WorkAuthorization.UNKNOWN,
    employment_scope: EmploymentScope = EmploymentScope.UNKNOWN,
    country_restrictions: list[str] | None = None,
) -> GeoVerdict:
    """Decide the A/B/C geographic band from the employer's words first.

    A board's location label can only ever lower the verdict (to B), never raise
    it to A. Any contradiction between the board's label and a restriction is
    recorded as a conflict and produces C, so it cannot be auto-approved.

    ``country_restrictions`` is the one structured field treated as evidence in
    both directions: an explicit "Worldwide"/"Anywhere" entry is a stated scope,
    exactly as an explicit "United States" entry is a stated restriction. Absence
    of an entry is still not permission -- only an explicit one is.
    """
    employer_text = f"{title} {description}"
    loc_raw = (location or "").strip()

    restriction = None
    for pattern in _COMPILED_EMPLOYER_RESTRICTION:
        match = pattern.search(employer_text)
        if match:
            restriction = match.group(0)
            break

    # T7.7: where the employer says the POSITION is, as opposed to the rule it
    # states. "(Serbia)" in a title, "we are looking for ... in Serbia" in the
    # body: a place of work named for the role itself, which is a restriction on
    # the candidate's location just as much as "must be based in X" is.
    role_place = None
    for pattern in _COMPILED_EMPLOYER_ROLE_LOCATION:
        match = pattern.search(employer_text)
        if match:
            role_place = match.group(0).strip()
            break
    if role_place is None:
        match = _TITLE_PLACE_RE.search(title or "")
        if match:
            role_place = match.group(0).strip()

    # T7.4: a bare "Thailand" mention is not permission -- see
    # ``employer_allows_thailand``. A negated mention leaves the row at band B.
    allows_thailand = employer_allows_thailand(employer_text)
    # T7.7: and a worldwide phrase is permission only when it is about the
    # candidate rather than about the thing the work is done on.
    worldwide_match = _employee_worldwide_match(employer_text)
    allows_any_country = worldwide_match is not None
    suppressed_worldwide = (
        None if allows_any_country else _object_of_work_worldwide_match(employer_text)
    )
    explicit_scope = {str(c).strip().upper() for c in (country_restrictions or [])}
    scope_allows_anywhere = bool(
        explicit_scope & {"WORLDWIDE", "ANYWHERE", "GLOBAL", "ALL COUNTRIES"}
    )
    permissive_label = any(p.search(loc_raw) for p in _COMPILED_PLATFORM_PERMISSIVE)
    places = platform_place_tags(loc_raw)

    verdict = GeoVerdict(
        band=GeoBand.B,
        employer_restriction=restriction,
        employer_allows_thailand=allows_thailand,
        employer_allows_any_country=allows_any_country,
        platform_places=places,
        platform_permissive=permissive_label,
    )

    if suppressed_worldwide is not None:
        # Recorded, never silent: the reader must be able to see that the text
        # does contain a worldwide phrase and why it was not read as permission.
        verdict.reasons.append(
            "GEO_NOTE: the text contains a worldwide phrase, but it describes what "
            "the work is done on rather than where the candidate may live "
            f"('{_phrase_fragment(employer_text, suppressed_worldwide)}'), so it is "
            "not read as permission"
        )

    if (
        restriction
        or role_place
        or allows_thailand
        or allows_any_country
        or scope_allows_anywhere
    ):
        verdict.evidence = GeoEvidence.EMPLOYER
    elif permissive_label or places:
        verdict.evidence = GeoEvidence.PLATFORM

    if permissive_label and restriction:
        verdict.conflict = True
        verdict.conflict_details.append(
            f"board location label claims openness ('{loc_raw}') but the employer text "
            f"states a restriction ('{restriction}')"
        )
    elif allows_any_country and places:
        # T7.4: a real source-vs-source tension. The employer's own prose claims
        # worldwide openness while the board's structured field names countries,
        # so the optimistic reading is not taken automatically.
        #
        # A board field that carries an openness tag AND country tags together
        # ("Anywhere in the World, <countries>, Delaware") is NOT a conflict: that
        # is how a board renders a multi-country scope. It was previously recorded
        # as one, which flagged 58 live rows -- including five Lemon.io postings
        # whose own description lists Thailand -- and would have blocked a genuine
        # band A. Those rows now keep their band with no conflict recorded.
        verdict.conflict = True
        verdict.conflict_details.append(
            f"the employer's own text claims openness while the board's location "
            f"field names {places}"
        )

    # --- C: the employer's own restriction, in priority order ---
    if remote_mode in (RemoteMode.ONSITE, RemoteMode.HYBRID):
        verdict.band = GeoBand.C
        verdict.reasons.append(
            "BAND_C: employer requires on-site or hybrid attendance, so the "
            "candidate's location is not the candidate's to choose"
        )
        return verdict

    if restriction:
        verdict.band = GeoBand.C
        verdict.reasons.append(
            f"BAND_C: the employer's own text restricts the location or work "
            f"authorization ('{restriction}')"
        )
        if verdict.conflict:
            verdict.reasons.append(
                "BAND_C: conflicting evidence -- the board's permissive label does not "
                "override the employer's restriction, so this vacancy is not "
                "auto-approved"
            )
        return verdict

    if role_place:
        # T7.7: a place of work named for the position itself. It sits at the same
        # priority as an explicit restriction because it answers the same question
        # ("where does the employer expect this person to be?"), it is just phrased
        # as a location instead of as a rule.
        verdict.band = GeoBand.C
        verdict.reasons.append(
            f"BAND_C: the employer's own text states where the position is located "
            f"('{role_place}'), which is not the candidate's country"
        )
        return verdict

    if matched_regions:
        verdict.band = GeoBand.C
        verdict.reasons.append(
            f"BAND_C: the vacancy text restricts the region to {', '.join(matched_regions)}"
        )
        return verdict

    if work_authorization in (
        WorkAuthorization.COUNTRY_RESTRICTED,
        WorkAuthorization.REGION_RESTRICTED,
    ):
        verdict.band = GeoBand.C
        verdict.reasons.append(
            "BAND_C: country or regional work authorization is required"
        )
        return verdict

    if employment_scope == EmploymentScope.LOCAL_ONLY:
        verdict.band = GeoBand.C
        verdict.reasons.append("BAND_C: local payroll only, no international contracting")
        return verdict

    if places and remote_mode != RemoteMode.REMOTE:
        verdict.band = GeoBand.C
        verdict.reasons.append(
            f"BAND_C: the board places this vacancy in {places} and the remote format "
            f"is not confirmed, so office attendance there is the default reading"
        )
        return verdict

    # --- A: an explicit permission in the employer's own words ---
    if allows_thailand:
        verdict.band = GeoBand.A
        verdict.reasons.append(
            "BAND_A: the employer's own text allows work from Thailand"
        )
        return verdict

    if allows_any_country and not places and not verdict.conflict:
        verdict.band = GeoBand.A
        verdict.reasons.append(
            "BAND_A: the employer's own text allows work from any country and nothing "
            "contradicts it"
        )
        return verdict

    if scope_allows_anywhere and not places and not verdict.conflict:
        verdict.band = GeoBand.A
        verdict.reasons.append(
            f"BAND_A: the vacancy declares an explicit scope of {sorted(explicit_scope)} "
            f"and nothing contradicts it"
        )
        return verdict

    # --- B: remote, but Thailand availability is not established ---
    verdict.band = GeoBand.B
    if (allows_any_country or scope_allows_anywhere) and (places or verdict.conflict):
        verdict.reasons.append(
            "BAND_B: the vacancy declares an open scope, but the board's own location "
            "field names a place, so this is not treated as confirmed"
        )
    elif places:
        verdict.reasons.append(
            f"BAND_B: remote, but the board's location field names {places} and the "
            f"employer says nothing about Thailand"
        )
    elif permissive_label:
        verdict.reasons.append(
            "BAND_B: remote, but the openness is the board's label rather than the "
            "employer's statement, so availability from Thailand is unconfirmed"
        )
    else:
        verdict.reasons.append(
            "BAND_B: remote, but the employer states nothing about where the candidate "
            "may be located"
        )
    return verdict


@dataclass
class EligibilityAssessment:
    remote_mode: RemoteMode
    geo_scope: GeoScope
    work_authorization: WorkAuthorization
    timezone_requirement: TimezoneRequirement
    language_requirement: LanguageRequirement
    employment_scope: EmploymentScope
    eligibility: EligibilityStatus
    eligibility_reasons: list[str] = field(default_factory=list)
    matched_regions: list[str] = field(default_factory=list)
    matched_countries: list[str] = field(default_factory=list)
    timezone_details: str | None = None
    language_details: str | None = None
    # T7.4: the explicit geographic band, the evidence behind it, and any
    # conflict between the board's label and the employer's own words.
    geo_band: GeoBand = GeoBand.B
    geo_band_reasons: list[str] = field(default_factory=list)
    geo_evidence: GeoEvidence = GeoEvidence.NONE
    geo_conflict: bool = False
    geo_conflict_details: list[str] = field(default_factory=list)
    platform_places: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
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
            "geo_band": self.geo_band.value,
            "geo_band_reasons": self.geo_band_reasons,
            "geo_evidence": self.geo_evidence.value,
            "geo_conflict": self.geo_conflict,
            "geo_conflict_details": self.geo_conflict_details,
            "platform_places": self.platform_places,
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

# T7.4: a bare place name is not permission. "We are unable to hire in Thailand"
# and "excluding Thailand" would otherwise set ``allows_thailand`` and lift the
# row to band A. A mention only counts when no negation or exclusion token sits in
# its neighbourhood; an unclear mention stays False, which keeps the row at band B
# and out of the auto-queue. The window is deliberately wide -- fail-closed.
_THAILAND_COMPILED = [re.compile(p, re.IGNORECASE) for p in _THAILAND_PATTERNS]
_THAILAND_NEGATION_WINDOW_BEFORE = 80
_THAILAND_NEGATION_WINDOW_AFTER = 40
_THAILAND_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|cannot|can't|unable|excluding|excluded|exclude|except|"
    r"outside|without|neither|nor|unavailable)\b"
    r"|\bне\s+можем\b|\bне\s+работа\w*|\bкроме\b|\bисключ\w*|\bза\s+пределами\b"
    r"|\bнедоступ\w*|\bне\s+нанима\w*",
    re.IGNORECASE,
)


def employer_allows_thailand(employer_text: str) -> bool:
    """True only when a Thailand mention is neither negated nor excluded.

    ``_THAILAND_PATTERNS`` matches a bare place name, so on its own it cannot tell
    a permission from a refusal. Only a mention with no negation token in its
    neighbourhood counts; anything ambiguous returns False, so the vacancy stays
    at band B and is never auto-approved on the strength of a place name alone.
    """
    for pattern in _THAILAND_COMPILED:
        for match in pattern.finditer(employer_text):
            lo = max(0, match.start() - _THAILAND_NEGATION_WINDOW_BEFORE)
            hi = min(len(employer_text), match.end() + _THAILAND_NEGATION_WINDOW_AFTER)
            if not _THAILAND_NEGATION_RE.search(employer_text[lo:hi]):
                return True
    return False

_COUNTRY_SPECIFIC_PATTERNS: dict[str, list[str]] = {
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
        # T7.4: the employer's own phrasings. "Porkbun ... Applicants must be
        # based in the USA" reached the engine as WORLDWIDE before this list was
        # widened, because none of the patterns above cover it.
        r"\bmust\s+be\s+based\s+in\s+(?:the\s+)?(?:us|u\.s\.|usa|united\s+states)\b",
        r"\b(?:candidates?|applicants?|you)\s+(?:must\s+be\s+)?based\s+in\s+(?:the\s+)?(?:us|u\.s\.|usa|united\s+states)\b",
        r"\bmust\s+be\s+a\s+resident\s+of\s+(?:the\s+)?(?:us|u\.s\.|usa|united\s+states)\b",
        r"\beligible\s+for\s+employment\s+in\s+(?:the\s+)?(?:us|u\.s\.|usa|united\s+states)\b",
        r"\bmust\s+be\s+eligible\s+for\s+employment\b",
        r"\bopen\s+only\s+to\s+candidates?\s+(?:based|located|residing|living)\s+in\b",
        r"\bonly\s+(?:open\s+to|considering|accepting)\s+(?:candidates?|applicants?)\s+(?:who\s+are\s+)?(?:based|located|residing|living)\s+in\b",
        # "US Remote" is how the employer titles a US-only posting (VXI).
        r"\bus\s+remote\b",
        r"\bu\.s\.\s+remote\b",
        r"\b(?:us|u\.s\.|usa|united\s+states)[- ]based\b[^.]{0,40}?\b(?:candidates?|applicants?|residents?|employees?|representatives?|workers?|contractors?)\b",
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

_REGIONAL_PATTERNS: dict[str, list[str]] = {
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

def classify_remote_mode(title: str, description: str, location: str | None, source: str | None) -> RemoteMode:
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
    location: str | None,
    source: str | None,
    country_restrictions: list[str] | None = None,
) -> tuple[GeoScope, list[str], list[str]]:
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
    matched_countries: list[str] = []
    for country, patterns in _COUNTRY_SPECIFIC_PATTERNS.items():
        for p in patterns:
            if re.search(p, combined, re.IGNORECASE):
                matched_countries.append(country)
                break

    if matched_countries:
        return GeoScope.COUNTRY_SPECIFIC, matched_countries, []

    # Check Regional
    matched_regions: list[str] = []
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


def classify_work_auth(title: str, description: str) -> tuple[WorkAuthorization, str | None]:
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
    location: str | None,
    timezone_restrictions: list[Any] | None = None,
) -> tuple[TimezoneRequirement, str | None]:
    text = f"{title} {description} {location or ''}".lower()

    # Check structured tz restrictions
    if timezone_restrictions:
        if isinstance(timezone_restrictions, (int, float, str)):
            tz_list = [timezone_restrictions]
        else:
            tz_list = list(timezone_restrictions)
        tz_str = ", ".join(str(x) for x in tz_list)
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


def classify_language(title: str, description: str) -> tuple[LanguageRequirement, str | None]:
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
    candidate_languages: list[str] | None = None,
) -> EligibilityAssessment:
    """Perform full multi-dimensional eligibility assessment for candidate in Thailand.

    The geographic verdict is employer-first (see classify_geo_band):
    band C is always INELIGIBLE, band B is always UNKNOWN, band A is ELIGIBLE or
    ELIGIBLE_WITH_WARNING when a timezone is specified. Non-geographic blockers
    (on-site attendance, work authorization, language) still force INELIGIBLE and
    keep their own reasons.
    """
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

    # 1b. Employer-first geographic band (T7.4). Computed once, carried on every
    #     return path below so the band can never be silently lost.
    verdict = classify_geo_band(
        title=title,
        description=desc,
        location=loc,
        remote_mode=remote_mode,
        matched_regions=matched_regions,
        work_authorization=work_auth,
        employment_scope=emp_scope,
        country_restrictions=c_restr,
    )

    def _result(status: EligibilityStatus, reasons: list[str]) -> EligibilityAssessment:
        """Build the assessment, always carrying the band, evidence and conflicts."""
        combined = list(reasons)
        # T7.4 invariant: contradictory evidence is never auto-approved, whatever
        # band it produced. Today every conflict path already lands on band C and
        # so returns INELIGIBLE before reaching here; this guard makes the rule
        # structural instead of incidental, so a future band-A path cannot quietly
        # reintroduce the optimistic reading the task forbids.
        if verdict.conflict and status in (
            EligibilityStatus.ELIGIBLE,
            EligibilityStatus.ELIGIBLE_WITH_WARNING,
        ):
            combined.append(
                "UNKNOWN: conflicting geographic evidence -- not auto-approved; "
                "the conflict is reported above and must be resolved by hand"
            )
            status = EligibilityStatus.UNKNOWN
        for line in verdict.reasons:
            if line not in combined:
                combined.append(line)
        for line in verdict.conflict_details:
            marker = f"GEO_CONFLICT: {line}"
            if marker not in combined:
                combined.append(marker)
        return EligibilityAssessment(
            remote_mode=remote_mode,
            geo_scope=geo_scope,
            work_authorization=work_auth,
            timezone_requirement=tz_req,
            language_requirement=lang_req,
            employment_scope=emp_scope,
            eligibility=status,
            eligibility_reasons=combined,
            matched_regions=matched_regions,
            matched_countries=matched_countries,
            timezone_details=tz_detail,
            language_details=lang_detail,
            geo_band=verdict.band,
            geo_band_reasons=list(verdict.reasons),
            geo_evidence=verdict.evidence,
            geo_conflict=verdict.conflict,
            geo_conflict_details=list(verdict.conflict_details),
            platform_places=list(verdict.platform_places),
        )

    reasons: list[str] = []

    # 2. Evaluate Remote Mode
    if remote_mode == RemoteMode.ONSITE:
        reasons.append("INELIGIBLE: Onsite work required")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    if remote_mode == RemoteMode.HYBRID:
        reasons.append("INELIGIBLE: Hybrid work / office attendance required")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    # 3. Evaluate Geo Scope & Country Restrictions
    if geo_scope == GeoScope.COUNTRY_SPECIFIC:
        reasons.append(f"INELIGIBLE: Country restricted to {', '.join(matched_countries)} (candidate in {candidate_country})")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    if geo_scope == GeoScope.REGIONAL:
        reasons.append(f"INELIGIBLE: Region restricted to {', '.join(matched_regions)} (candidate in {candidate_country})")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    # 4. Evaluate Work Authorization & Employment Model
    if work_auth == WorkAuthorization.COUNTRY_RESTRICTED:
        reasons.append(f"INELIGIBLE: Country work authorization required ({auth_detail})")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    if work_auth == WorkAuthorization.REGION_RESTRICTED:
        reasons.append(f"INELIGIBLE: Regional work authorization required ({auth_detail})")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    if emp_scope == EmploymentScope.LOCAL_ONLY:
        reasons.append("INELIGIBLE: Local payroll / W2 only (no international contractor support)")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    # 5. Evaluate Language Requirement
    if lang_req == LanguageRequirement.NATIVE_ENGLISH:
        reasons.append(f"INELIGIBLE: LANGUAGE_RESTRICTION ({lang_detail})")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    if lang_req == LanguageRequirement.OTHER:
        reasons.append(f"INELIGIBLE: Non-English/Russian language required ({lang_detail})")
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    # 6. Band C: never auto-approved, whatever the format looks like. This runs
    #    before the "remote format unknown" branch on purpose -- an office
    #    vacancy in another country is C, not merely B.
    if verdict.band == GeoBand.C:
        reasons.append(
            "INELIGIBLE: Geo band C -- the employer's own evidence rules this out for a "
            "candidate in Thailand"
        )
        return _result(EligibilityStatus.INELIGIBLE, reasons)

    # 7. Remote format itself is not established -> B
    if remote_mode == RemoteMode.UNKNOWN:
        reasons.append("UNKNOWN_REMOTE: Work format is not confirmed remote")
        return _result(EligibilityStatus.UNKNOWN, reasons)

    # 8. Band B: remote, but Thailand availability is not established. Band A is
    #    excluded here on purpose: an explicit employer permission settles the
    #    geography even when no generic worldwide pattern matched the text.
    if verdict.band == GeoBand.B or (geo_scope == GeoScope.UNKNOWN and verdict.band != GeoBand.A):
        reasons.append("GEO_UNKNOWN: Geolocation eligibility not explicitly specified")
        return _result(EligibilityStatus.UNKNOWN, reasons)

    # 9. Check Timezone Warnings
    if tz_req in (TimezoneRequirement.SPECIFIED, TimezoneRequirement.STRICT):
        reasons.append(f"ELIGIBLE_WITH_TIMEZONE_WARNING: {tz_detail} (candidate in Thailand UTC+7)")
        return _result(EligibilityStatus.ELIGIBLE_WITH_WARNING, reasons)

    # 10. Fully Eligible (band A)
    reasons.append("ELIGIBLE: Confirmed remote-eligible for candidate in Thailand")
    return _result(EligibilityStatus.ELIGIBLE, reasons)


# --- T7.7: the geographic gate the send paths consult ---------------------------

def geo_band_permits_auto_send(assessment: Any) -> tuple[bool, str]:
    """Whether the geographic verdict lets this vacancy be sent WITHOUT a human.

    Only band A does. Band B is the classifier's default and means "availability
    from Thailand was never established"; band C means the employer's own evidence
    rules it out. Neither may become a sent application just because the format
    looks remote or because a board labelled the row "worldwide", so both are
    refused here.

    This is deliberately a named predicate rather than a comparison repeated at
    every call site: the queue, the submit executor and any future caller must
    agree on the same rule, and a reader must be able to find it in one place.
    """
    band = getattr(assessment, "geo_band", None)
    if band == GeoBand.A:
        return True, ""
    value = getattr(band, "value", "?")
    reasons = "; ".join(getattr(assessment, "geo_band_reasons", None) or [])
    return False, (
        f"geo band {value} is not auto-sendable (only band A is; band B means the "
        f"geography was never established and band C means it is ruled out): {reasons}"
    )
