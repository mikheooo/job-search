"""T7.5: the manual-review list -- the middle mode between search and submission.

Three modes are kept apart on purpose:

1. **search and save** -- ``cli collect`` / ``cli watch`` fetch from the board
   adapters and write to ``vacancies``;
2. **manual review list** -- this module. Read-only. Bands A and B for remote
   work, plus a separate Thailand-local category;
3. **submission queue** -- ``application_queue.generate_queue``, gated by
   ``matcher._hard_constraints``.

Band B is *visible here* so a human can check the conditions, and is deliberately
never queued. Band C is in neither list. A vacancy that happens to sit in
Thailand is a fourth thing again: the geography is not a barrier for a candidate
who lives there, so it forms its own category rather than being read as C.

The module imports no queue writer at all, so building the review list cannot
approve anything -- the separation is structural, not a matter of discipline.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .db import _row_to_vacancy
from .schema import is_genuine_production_vacancy

# ---------------------------------------------------------------------------
# Bands and categories
# ---------------------------------------------------------------------------

#: Bands that may appear in the manual-review list. C is absent by construction.
REVIEW_BANDS: tuple[str, ...] = ("A", "B")

#: Review categories. ``remote_*`` answers "may I work from Thailand?";
#: ``local_thailand`` is a different question -- the work is *in* Thailand.
CATEGORY_REMOTE_CONFIRMED = "remote_confirmed"      # band A
CATEGORY_REMOTE_UNCLEAR = "remote_unclear"          # band B
CATEGORY_LOCAL_THAILAND = "local_thailand"          # work located in Thailand

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_REMOTE_CONFIRMED: "Удалённо из Таиланда подтверждено (A)",
    CATEGORY_REMOTE_UNCLEAR: "Удалённо, география не подтверждена (B)",
    CATEGORY_LOCAL_THAILAND: "Работа непосредственно в Таиланде",
}

#: The Thailand-local category is never auto-queued either: a local posting
#: still needs its own checks (workplace, language, permit).
AUTO_QUEUE_ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({CATEGORY_REMOTE_CONFIRMED})

#: A place name that means the posting is located in Thailand.
_THAILAND_PLACE_RE = re.compile(
    r"\b(?:thailand|тайланд|таиланд|bangkok|бангкок|phuket|пхукет|"
    r"chiang\s*mai|chiangmai|pattaya|паттайя|khon\s*kaen|hat\s*yai|hua\s*hin|"
    r"samui|krabi|rayong|chonburi|nonthaburi|краби)\b",
    re.IGNORECASE,
)

#: A scope marker that makes the field an openness claim rather than a place.
_SCOPE_OPENNESS_RE = re.compile(
    r"\b(?:anywhere|worldwide|global|world[- ]wide)\b|\bв\s+любой\s+точке\b|\bпо\s+всему\s+миру\b",
    re.IGNORECASE,
)

#: How many comma-separated segments still read as one location. "Bangkok,
#: Thailand" is a location; "Anywhere in the World, <70 countries>, Delaware" is
#: a multi-country remote scope rendered as one field.
_MAX_LOCAL_SEGMENTS = 2

# ---------------------------------------------------------------------------
# Directions (T7.3). Primary first; Python/AI stays as the secondary option.
# ---------------------------------------------------------------------------

#: (direction, priority, title patterns, description patterns). Priority 1 is a
#: confirmed primary direction, 3 the secondary Python/AI option.
DIRECTIONS: list[tuple[str, int, list[str], list[str]]] = [
    (
        "bitrix24",
        1,
        [r"\bbitrix"],
        [r"\bbitrix"],
    ),
    (
        "crm_admin_support",
        1,
        [
            r"\bcrm\b", r"\bcrm\s+(?:admin|administrator|support|manager|specialist)",
            r"\bsalesforce\s+admin", r"\bzoho\b", r"\bhubspot\b", r"\bamo\s?crm\b",
        ],
        [r"\bcrm\s+(?:administration|support|migration|integration)", r"\bcustomer\s+relationship\s+management\b"],
    ),
    (
        "helpdesk",
        1,
        [r"\bhelp\s?desk\b", r"\bservice\s+desk\b", r"\b1st\s+line\b", r"\bfirst\s+line\b", r"\b2nd\s+line\b"],
        [r"\bhelp\s?desk\b", r"\bservice\s+desk\b", r"\bincident\s+(?:management|triage)\b"],
    ),
    (
        "it_support",
        1,
        [
            # Only unambiguously technical phrasing. A bare "support specialist"
            # is *not* here: "Customer Support Specialist" matches it, and that
            # is a different job -- it sits in the customer_support direction
            # below, ranked lower, instead of being read as IT support.
            r"\btechnical\s+support\b", r"\bapplication\s+support\b",
            r"\bit\s+support\b", r"\bproduct\s+support\b",
            r"\bsupport\s+(?:engineer|technician)\b",
            r"\b(?:business\s+)?systems?\s+support\b",
            r"\bспециалист\s+по\s+сопровождению\b", r"\bтехническ\w+\s+поддержк\w*",
            r"\bсопровождени\w*\s+(?:информационн|программн|it)",
        ],
        [r"\bapplication\s+support\b", r"\btechnical\s+support\b", r"\btroubleshoot\w*"],
    ),
    (
        # Adjacent, not the same job: helping end users of a product is not
        # administering IT. The task names six technical directions and keeps
        # Python/AI as *additional*; customer support belongs in the same
        # additional tier, so it is available by name and kept out of the default
        # list instead of crowding out the technical directions.
        "customer_support",
        3,
        [
            r"\bcustomer\s+support\b", r"\bclient\s+support\b",
            r"\bcustomer\s+(?:care|service)\b",
            r"\bsupport\s+(?:specialist|analyst|agent|advisor|assistant)\b",
            r"\bподдержк\w*\s+(?:пользовател|клиент)",
        ],
        [r"\bcustomer\s+support\b", r"\bchat\b", r"\bemail\s+support\b"],
    ),
    (
        "sysadmin",
        1,
        [
            r"\bsystem\s+administrator\b", r"\bsysadmin\b", r"\bсистемн\w+\s+администратор\w*",
            r"\bit\s+administrator\b", r"\bwindows\s+server\b", r"\bactive\s+directory\b",
            r"\bnetwork\s+administrator\b",
        ],
        [r"\bwindows\s+server\b", r"\bactive\s+directory\b", r"\bсистемн\w+\s+администрирован\w*"],
    ),
    (
        "it_infrastructure",
        1,
        [
            r"\bit\s+infrastructure\b", r"\binfrastructure\s+(?:engineer|administrator|specialist)\b",
            r"\bинфраструктур\w*", r"\bit\s+operations\b",
        ],
        [r"\bit\s+infrastructure\b", r"\binfrastructure\s+operations\b"],
    ),
    (
        "integrations",
        1,
        [
            r"\bintegration\s+(?:engineer|specialist|developer|architect)\b",
            r"\btechnical\s+integration", r"\bинтеграц\w*",
        ],
        [r"\brest\s+api\s+integration", r"\bwebhook\w*", r"\bинтеграц\w*"],
    ),
    (
        "it_automation",
        2,
        [r"\bit\s+automation\b", r"\bautomation\s+(?:engineer|specialist)\b", r"\brpa\b"],
        [r"\bn8n\b", r"\bmake\.com\b", r"\bzapier\b", r"\bautomation\s+pipeline"],
    ),
    (
        "python_ai",
        3,
        [
            r"\bpython\s+(?:developer|engineer)\b", r"\bai\s+(?:engineer|developer)\b",
            r"\bai\s+agent\b", r"\bllm\s+(?:engineer|developer)\b",
            r"\bbackend\s+(?:python|engineer)\b", r"\bautomation\s+engineer\b",
        ],
        [r"\bfastapi\b", r"\bllm\b", r"\bai\s+agents?\b"],
    ),
]

#: The direction names, for validating a filter *before* it is applied. An
#: unknown name filters everything out and returns an empty list, which is
#: indistinguishable from "no suitable vacancies" -- so callers that accept a
#: direction from a human or a query string should refuse an unknown one loudly.
DIRECTION_NAMES: frozenset[str] = frozenset(d[0] for d in DIRECTIONS)

#: Titles that name a different profession even though a direction word appears.
NEGATIVE_TITLE_PATTERNS: list[str] = [
    # "Salesforce Developer", and also "Salesforce CRM Developer" -- the profession
    # word can be separated from the vendor by a product word. A Salesforce
    # *Administrator* is deliberately not caught: CRM administration is a target
    # direction, and it is only the developer/consultant roles the task rules out.
    r"\bsalesforce\b[\w\s-]{0,24}?\b(?:developer|engineer|architect|consultant)\b",
    # The same profession written the other way round: "Senior CRM Developer,
    # Salesforce and HubSpot" puts the vendor after the profession word, where the
    # pattern above cannot see it. CRM administration is a target direction; CRM
    # development is not, whatever the vendor.
    r"\bcrm\b[\w\s-]{0,20}?\b(?:developer|engineer|architect|consultant)\b",
    r"\b(?:developer|engineer|architect|consultant)\b[\w\s,-]{0,24}?\bcrm\b",
    r"\bmac\s+(?:msp|managed)\b",
    r"\bmsp\s+help\s?desk\b",
    r"\b(?:devops|sre|site\s+reliability)\s+engineer\b",
    r"\b(?:software|backend|frontend|full[- ]?stack)\s+(?:engineer|developer)\b",
    r"\bdata\s+(?:engineer|scientist)\b",
    # Management, in both languages. The task names "руководящие позиции" as
    # padding to be kept out, and a Russian board writes them with words the
    # English list cannot see: «Руководитель технической поддержки» is exactly a
    # "Head of Technical Support".
    #
    # The English list stops at head/director/vp/chief/president and deliberately
    # does not carry a bare "manager": in English titles "manager" is routinely a
    # senior individual contributor ("CRM Manager" is often an administrator),
    # whereas «руководитель» in a Russian title is a management post. Those
    # English cases stay in the list and are kept out of the shortlist by hand.
    r"\b(?:head\s+of|director|vp|chief|president)\b",
    r"\b(?:руководител\w*|начальник\w*|директор\w*|заведующ\w*)\b",
    r"\b(?:tax|payroll|video|dental|dentist|recruit\w*|marketing|sales)\b",
    r"\b(?:crypto|trader|trading|igaming|casino|betting)\b",
]

#: Titles that are a different profession only when several words appear
#: together. Kept apart from the single-pattern list because "Bitrix24" alone is
#: a direction word and "разработчик" alone is a real title in the Python/AI
#: direction; it is the pair that means the candidate is being offered a
#: development job in a direction he asked for as *administration and support*.
NEGATIVE_TITLE_COMBOS: list[tuple[str, ...]] = [
    (r"\bbitrix", r"\b(?:разработчик|программист|developer|programmer)\b"),
]

#: Domains that are not IT at all. A title naming one of these is not an IT
#: vacancy even when a direction word coincides with it: a "Behavioral Health
#: Integration Specialist" coordinates clinical documentation and is not an
#: integrations engineer.
NON_IT_DOMAIN_PATTERNS: list[str] = [
    r"\b(?:behavioral|mental)\s+health\b",
    r"\bclinical\b",
    r"\bpatient\s+(?:care|charts?|contact|documentation)\b",
    r"\bnursing\b",
    r"\bpharmac\w*\b",
    r"\bmedical\s+(?:billing|coding|records)\b",
    r"\bcare\s+coordination\b",
]

#: Words that make a title an IT vacancy even inside a non-IT industry. The veto
#: above is conditional on this list, or it would delete a whole legitimate
#: sector: "Healthcare IT Support Engineer" is an IT job that happens to serve
#: healthcare, and must survive.
#:
#: A bare "systems" is deliberately absent. It was in an earlier draft and it
#: rescued "Clinical Systems Coordinator", which is a health-information post:
#: the escape has to name something that is actually IT, not merely structural.
IT_CONTEXT_PATTERNS: list[str] = [
    r"\bit\b",
    r"\bsoftware\b",
    r"\btechnical\b",
    r"\bnetwork\b",
    r"\bserver\b",
    r"\bdatabase\b",
    r"\bhelp\s?desk\b",
    r"\bdevops\b",
    r"\bcloud\b",
    r"\binfrastructure\b",
    r"\bdeveloper\b",
    r"\bengineer\b",
    r"\badministrator\b",
]

# ---------------------------------------------------------------------------
# The candidate's confirmed hard exclusions
# ---------------------------------------------------------------------------

#: Which confirmed lists apply. Same three the submission gate reads, with the
#: same substring semantics, so the manual-review list cannot offer a vacancy the
#: queue would refuse on the same grounds.
HARD_EXCLUSION_FIELDS: tuple[str, ...] = (
    "excluded_roles",
    "excluded_industries",
    "excluded_companies",
)


def load_hard_exclusions(profile_path: str | None = None) -> dict[str, Any]:
    """The candidate's confirmed "never show me this" lists, and where they came from.

    ``matcher`` is deliberately *not* imported to borrow its gate: this module
    must not be able to approve or refuse a submission, only to read the lists.
    The semantics are copied on purpose -- substring, case-folded, over the title
    and the description -- because two different readings of the same confirmed
    preference would be worse than a little duplication.

    ``source`` says which profile answered. The loader falls back to a built-in
    default when no file is found, and that default still carries ``bitrix`` in
    ``excluded_roles`` where the live profile does not, so the difference has to
    be visible rather than inferred.
    """
    from .candidate_profile import load_candidate_profile

    profile = load_candidate_profile(profile_path)
    out: dict[str, Any] = {"source": profile_path or "auto-discovered or built-in default"}
    for field in HARD_EXCLUSION_FIELDS:
        values = [str(v).strip().lower() for v in (getattr(profile, field, None) or [])]
        out[field] = [v for v in values if v]
    return out


#: Industry keywords carry synonyms the profile does not spell out. The confirmed
#: preference excludes "gambling"; the posting says "iGaming", and a literal
#: keyword match therefore misses it -- measured on a live row that offers the
#: candidate a customer-support job at a betting company. Expanding the keyword is
#: a reading of the *same* confirmed preference, not a new one, and the profile
#: itself is left untouched.
INDUSTRY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "gambling": (
        "igaming", "i-gaming", "betting", "bookmaker", "bookmaking",
        "sportsbook", "poker", "slots", "lottery", "wagering", "gambl",
    ),
    "casino": ("casino", "igaming", "i-gaming", "poker", "slots"),
    "adult": ("porn", "adult content", "webcam model", "escort service"),
}


def _industry_keywords(exclusions: dict[str, Any]) -> list[str]:
    """The confirmed industry exclusions, each widened with its synonyms."""
    out: list[str] = []
    for keyword in exclusions.get("excluded_industries") or []:
        out.append(keyword)
        out.extend(INDUSTRY_SYNONYMS.get(keyword, ()))
    return out


def _hard_exclusion_hit(
    title: str, description: str, company: str | None, exclusions: dict[str, Any]
) -> str | None:
    """The first confirmed exclusion this vacancy trips, or ``None``.

    Roles are read from the title *and* the body, exactly as the submission gate
    reads them: a "Разработчик Bitrix24" posting does not always name PHP in its
    title, and the two readings disagreeing is how a vacancy ends up offered for
    manual review and then refused by the queue.
    """
    text = f"{title or ''} {description or ''}".lower()
    company_text = (company or "").lower()
    for field, where, keywords in (
        ("excluded_roles", text, exclusions.get("excluded_roles") or []),
        ("excluded_industries", text + " " + company_text, _industry_keywords(exclusions)),
        ("excluded_companies", company_text, exclusions.get("excluded_companies") or []),
    ):
        for keyword in keywords:
            if keyword in where:
                return f"{field}:{keyword}"
    return None


def _title_is_negative(title: str) -> bool:
    """True when the title names something other than a target IT profession."""
    if any(re.search(p, title, re.IGNORECASE) for p in NEGATIVE_TITLE_PATTERNS):
        return True
    for combo in NEGATIVE_TITLE_COMBOS:
        if all(re.search(p, title, re.IGNORECASE) for p in combo):
            return True
    # A non-IT domain only vetoes a title that names no IT context: "Behavioral
    # Health Integration Specialist" is out, "Healthcare IT Support Engineer" is
    # not. Without the escape the rule would take a whole legitimate sector.
    names_non_it_domain = any(
        re.search(p, title, re.IGNORECASE) for p in NON_IT_DOMAIN_PATTERNS
    )
    names_it_context = any(re.search(p, title, re.IGNORECASE) for p in IT_CONTEXT_PATTERNS)
    return names_non_it_domain and not names_it_context


# ---------------------------------------------------------------------------
# Open questions -- derived from concrete gaps, never invented
# ---------------------------------------------------------------------------

def _open_questions(item: dict[str, Any]) -> list[str]:
    """Concrete questions a human must answer before an application is prepared.

    Each entry is derived from a fact that is missing or contradictory in the
    record -- never from a guess about the employer's intent.
    """
    out: list[str] = []
    if item["category"] == CATEGORY_REMOTE_UNCLEAR:
        out.append(
            "Нанимают ли кандидата из Таиланда? Площадка заявляет удалённость, "
            "работодатель о географии не говорит."
        )
    if item.get("geo_conflict"):
        out.append("Источники противоречат друг другу: " + "; ".join(item.get("geo_conflict_details") or [])[:300])
    if item["category"] == CATEGORY_LOCAL_THAILAND:
        out.append("Указано ли место работы в Таиланде и требуется ли знание тайского языка?")
        out.append(
            "Оформляет ли работодатель разрешение на работу? Если он об этом не сообщил, "
            "помощь с оформлением не предполагается."
        )
    if item.get("salary_min") is None and item.get("salary_max") is None:
        out.append("Зарплата в записи не указана — условия оплаты нужно подтвердить у работодателя.")
    if item.get("timezone") in ("specified", "strict"):
        out.append(f"График: {item.get('timezone_details') or 'часовой пояс указан'} — сверить с Таиландом (UTC+7).")
    if item.get("language") == "fluent_english":
        out.append("Уровень английского заявлен как требование — подтвердить, что рабочего достаточно.")
    if item.get("language") == "other":
        out.append(f"Требуется другой язык работы: {item.get('language_details') or 'не указан'}.")
    return out


# ---------------------------------------------------------------------------
# Building the list
# ---------------------------------------------------------------------------

def _direction(title: str, description: str, *, title_only: bool) -> tuple[str, int] | None:
    """Best direction for a vacancy.

    A title match outranks a description match. ``title_only`` drops the
    description pass entirely: measured on the live set, matching the body gave
    113 false hits out of 131 ("Director of Accounting", "Building Maintenance
    Technician", "Course Writer"), because almost any posting mentions
    troubleshooting somewhere. A review list of *suitable* vacancies must not be
    padded that way, so the body is only consulted when asked for explicitly.
    """
    for name, priority, title_patterns, _desc in DIRECTIONS:
        if any(re.search(p, title, re.IGNORECASE) for p in title_patterns):
            return name, priority
    if title_only:
        return None
    for name, priority, _title, desc_patterns in DIRECTIONS:
        if any(re.search(p, description, re.IGNORECASE) for p in desc_patterns):
            return name, priority
    return None


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return None


def _is_thailand_local(location: str | None, country_restrictions: list[str] | None) -> bool:
    """True when the posting is *scoped to Thailand* rather than merely remote.

    Two traps are handled explicitly.

    First, a board renders a multi-country remote scope as one field --
    "Anywhere in the World, <73 countries including Thailand>, Delaware". Reading
    the country list would call that a Thai posting; it is a remote posting, and
    the openness marker plus the segment count both say so.

    Second, a mention of Thailand inside the body is not read at all: on a board
    like Lemon.io that word sits in a list of countries the employer may hire in,
    which is a different statement from "this job is in Thailand".

    An explicit ``country_restrictions == ["Thailand"]`` is the employer stating
    the scope outright, so it counts on its own.
    """
    restrictions = {str(c).strip().lower() for c in (country_restrictions or [])}
    if restrictions == {"thailand"}:
        return True

    loc = (location or "").strip()
    if not loc:
        return False
    if _SCOPE_OPENNESS_RE.search(loc):
        return False
    segments = [s for s in loc.split(",") if s.strip()]
    if len(segments) > _MAX_LOCAL_SEGMENTS:
        return False
    return bool(_THAILAND_PLACE_RE.search(loc))


def _read_records(db_path: str | None) -> tuple[list[Any], dict[str, dict[str, Any]]]:
    """Read vacancies and their stored assessments, read-only.

    The connection is opened with ``mode=ro`` and ``init_db`` is deliberately not
    called: a module whose whole job is to *show* a list must not be able to
    create or change anything, and that includes creating a database file. The
    caller passes a path when it needs a specific one (a test copy, for example);
    otherwise the configured database is used.

    A missing file is reported as such. Without this check SQLite answers
    ``unable to open database file``, which reads like a permissions problem and
    sends the reader looking in the wrong place -- and ``mode=ro`` never creates
    the file, so "no database yet" and "database locked" are not distinguishable
    from that message.
    """
    path = Path(db_path or config.DB_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"review list needs an existing database, and {path} is not there; "
            "this module opens the database read-only and will not create it"
        )
    # ``file:<path>?mode=ro`` with a leading slash and forward slashes is the
    # documented URI form; on Windows a bare drive letter after ``file:`` is
    # parsed inconsistently across SQLite builds.
    uri = "file:/" + path.resolve().as_posix().lstrip("/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM vacancies").fetchall()
        elig: dict[str, dict[str, Any]] = {}
        try:
            cursor = conn.execute(
                "SELECT vacancy_stable_id, status, reasons_json, assessment_json FROM vacancy_eligibility"
            )
        except sqlite3.OperationalError:
            # A database from before the eligibility table existed still yields a
            # usable list: every row simply reads as band-less and is excluded.
            return rows, elig
        for record in cursor:
            try:
                assessment = json.loads(record["assessment_json"]) if record["assessment_json"] else {}
            except (TypeError, ValueError):
                assessment = {}
            try:
                reasons = json.loads(record["reasons_json"]) if record["reasons_json"] else []
            except (TypeError, ValueError):
                reasons = []
            elig[record["vacancy_stable_id"]] = {
                "status": record["status"],
                "reasons": reasons,
                "assessment": assessment,
            }
        return rows, elig
    finally:
        conn.close()


def build_review_list(
    *,
    limit: int = 100,
    categories: tuple[str, ...] | None = None,
    directions: tuple[str, ...] | None = None,
    max_age_days: int | None = 30,
    include_band_c_foreign: bool = False,
    min_priority: int = 2,
    title_only: bool = True,
    db_path: str | None = None,
    now: datetime | None = None,
    exclusions: dict[str, Any] | None = None,
    profile_path: str | None = None,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Return the manual-review list, newest first.

    Read-only. Band C vacancies from abroad are excluded unless
    ``include_band_c_foreign`` is set, which exists only so a test can prove the
    exclusion is real; nothing in the product sets it.

    ``categories`` and ``directions`` narrow the list; ``max_age_days`` drops
    records whose freshness cannot be established. ``title_only`` keeps the list
    to postings whose *title* names the profession; see ``_direction``.
    ``min_priority`` defaults to 2, the tier of the six technical directions the
    task names plus the adjacent automation work. Priority 3 -- customer support
    and Python/AI, both *additional* rather than primary -- is reachable by asking
    for it, and is not in the default list.

    ``exclusions`` are the candidate's confirmed hard exclusions, loaded from the
    profile when not given. ``stats``, when passed, is filled with the reason
    counts of everything dropped -- a list that silently omits rows is
    indistinguishable from a list with nothing to show, and that is the failure
    this project keeps treating as a defect.
    """
    now = now or datetime.now(timezone.utc)
    wanted_categories = set(categories) if categories else {
        CATEGORY_REMOTE_CONFIRMED, CATEGORY_REMOTE_UNCLEAR, CATEGORY_LOCAL_THAILAND,
    }
    wanted_directions = set(directions) if directions else None
    if exclusions is None:
        exclusions = load_hard_exclusions(profile_path)

    rows, elig = _read_records(db_path)
    out: list[dict[str, Any]] = []

    def _drop(reason: str) -> None:
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    for row in rows:
        vac = _row_to_vacancy_safe(row)
        if vac is None:
            _drop("unreadable_row")
            continue
        genuine, why = is_genuine_production_vacancy(vac)
        if not genuine:
            _drop(f"not_genuine:{why}")
            continue

        title = vac.title or ""
        description = vac.description or ""
        if _title_is_negative(title):
            _drop("negative_title")
            continue
        hit = _hard_exclusion_hit(title, description, vac.company, exclusions)
        if hit is not None:
            _drop(f"profile_exclusion:{hit.split(':')[0]}")
            continue

        record = elig.get(vac.stable_id()) or {}
        assessment = record.get("assessment") or {}
        band = str(assessment.get("geo_band") or "").upper()
        status = str(record.get("status") or "unknown")

        # A remote category requires remoteness to have been *established*. Band B
        # is the default of the geographic classifier, not a positive finding: a
        # posting that says nothing about remote work lands there, and its reason
        # line then reads "remote, but the employer states nothing about where the
        # candidate may be located" -- asserting a premise the record does not
        # support. Measured on the live set: two such rows, both plainly office
        # jobs (Moscow and Vladivostok, salaries in roubles), and no band A row is
        # affected -- so this gate costs nothing and stops the list from calling an
        # office job remote.
        remote_established = str(assessment.get("remote_mode") or "").lower() == "remote"

        thailand_local = _is_thailand_local(vac.location, vac.country_restrictions)
        if thailand_local:
            # The local category asks a different question -- the work is *in*
            # Thailand -- so it does not depend on remoteness at all.
            category = CATEGORY_LOCAL_THAILAND
        elif band == "A" and remote_established:
            category = CATEGORY_REMOTE_CONFIRMED
        elif band == "B" and remote_established:
            category = CATEGORY_REMOTE_UNCLEAR
        elif band in ("A", "B"):
            # A or B but remoteness was never established. Kept out of both remote
            # categories; this is not the band C escape hatch, so the flag does not
            # reach it either.
            _drop("remoteness_not_established")
            continue
        else:
            # Band C from abroad, or a record with no band at all. Neither is
            # offered as a suitable vacancy.
            if not include_band_c_foreign:
                _drop("band_c_or_unbanded")
                continue
            category = CATEGORY_REMOTE_UNCLEAR

        if category not in wanted_categories:
            _drop(f"category_not_wanted:{category}")
            continue

        direction = _direction(
            title,
            description,
            # The Thailand-local category is small and its titles are written in
            # a different vocabulary (Thai employers, local phrasing), so the
            # body is consulted there. The remote list stays title-only: it is
            # large, and padding it with body matches was measured to be noise.
            title_only=title_only and not thailand_local,
        )
        if direction is None:
            _drop("no_direction")
            continue
        direction_name, priority = direction
        # The priority tiers are about which *direction* is primary. They do not
        # apply to the Thailand-local category, which asks a different question
        # ("is the work in Thailand?") and is exempt from ``title_only`` for the
        # same reason: it is small, and its postings use local vocabulary.
        if priority > min_priority and not thailand_local:
            _drop(f"priority_above:{priority}")
            continue
        if wanted_directions is not None and direction_name not in wanted_directions:
            _drop(f"direction_not_wanted:{direction_name}")
            continue

        stamp = _as_dt(vac.published_at) or _as_dt(vac.first_seen_at)
        age = (now - stamp).days if stamp is not None else None
        if max_age_days is not None and age is not None and age > max_age_days:
            _drop("too_old")
            continue

        item: dict[str, Any] = {
            "stable_id": vac.stable_id(),
            "source": vac.source,
            "title": title,
            "company": vac.company,
            "url": vac.job_url,
            "location": vac.location,
            "country_restrictions": list(vac.country_restrictions or []),
            "published_at": str(vac.published_at) if vac.published_at else None,
            "first_seen_at": str(vac.first_seen_at) if vac.first_seen_at else None,
            "age_days": age,
            "category": category,
            "category_label": CATEGORY_LABELS[category],
            "band": band or None,
            "band_reasons": list(assessment.get("geo_band_reasons") or []),
            "geo_evidence": assessment.get("geo_evidence"),
            "geo_conflict": bool(assessment.get("geo_conflict")),
            "geo_conflict_details": list(assessment.get("geo_conflict_details") or []),
            "platform_places": list(assessment.get("platform_places") or []),
            "eligibility_status": status,
            "remote_mode": assessment.get("remote_mode"),
            "geo_scope": assessment.get("geo_scope"),
            "timezone": assessment.get("timezone_requirement"),
            "timezone_details": assessment.get("timezone_details"),
            "language": assessment.get("language_requirement"),
            "language_details": assessment.get("language_details"),
            "employment_scope": assessment.get("employment_scope"),
            "salary_min": vac.salary_min,
            "salary_max": vac.salary_max,
            "salary_currency": vac.salary_currency,
            "direction": direction_name,
            "direction_priority": priority,
            # A category that is not on the auto-queue allow-list is exactly the
            # case a human must look at before anything is prepared.
            "manual_review_required": category not in AUTO_QUEUE_ELIGIBLE_CATEGORIES,
            "auto_queue_allowed": category in AUTO_QUEUE_ELIGIBLE_CATEGORIES,
        }
        item["open_questions"] = _open_questions(item)
        out.append(item)

    out.sort(
        key=lambda r: (
            0 if r["category"] == CATEGORY_REMOTE_CONFIRMED else (1 if r["category"] == CATEGORY_REMOTE_UNCLEAR else 2),
            r["direction_priority"],
            -(r["age_days"] if r["age_days"] is not None else 10_000),
        )
    )
    return out[:limit]


def _row_to_vacancy_safe(row: Any):
    """``_row_to_vacancy`` with the positional-tuple caveat handled once.

    A malformed row must not take the whole review list down, so the narrow set
    of failures that mapping a row can raise is caught and the row is skipped.
    """
    try:
        return _row_to_vacancy(row)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


def summarise(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts by category and direction, for a dashboard header or a report."""
    by_category: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    for item in items:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
        by_direction[item["direction"]] = by_direction.get(item["direction"], 0) + 1
    return {
        "total": len(items),
        "by_category": by_category,
        "by_direction": by_direction,
        "needs_manual_review": sum(1 for i in items if i["manual_review_required"]),
    }
