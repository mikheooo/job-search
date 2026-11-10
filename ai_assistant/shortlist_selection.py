"""T7.27 — which of the discovered vacancies deserve a full read, and why.

The problem this module exists for is small and real. ``run_search_operation``
discovers vacancies query by query and then reads the first ``read_limit`` of the
union — and the union is in discovery order, which is query order. With
``["IT Support", "Bitrix24"]`` and ``read_limit=10`` every read came from
``IT Support`` and ``Bitrix24`` got **zero** full reads, not because it found
nothing but because it was second in the list. The shortlist then depends on the
order of the query list, which is an accident of configuration.

So the read targets are chosen by a policy instead of by position:

1. every card is placed in one of the five directions the candidate actually
   targets, or in ``OFF_PROFILE``;
2. a cheap title-level rank says which cards are worth the expensive page read —
   explicitly *not* a hiring decision, only a budget decision (§3);
3. the selection walks the categories **round-robin**, so a strong direction
   cannot be starved by a weak one that merely came first.

Pure stdlib, no browser, no profile I/O: this is policy, and policy should be
testable without a live board.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# the five directions §4 requires coverage for
# ---------------------------------------------------------------------------


class ShortlistCategory(str, Enum):
    BITRIX24_CRM = "BITRIX24_CRM"
    IT_SUPPORT = "IT_SUPPORT"
    SYSADMIN_INFRA = "SYSADMIN_INFRA"
    APP_INTEGRATION_SUPPORT = "APP_INTEGRATION_SUPPORT"
    AUTOMATION_AI = "AUTOMATION_AI"
    OTHER = "OTHER"
    OFF_PROFILE = "OFF_PROFILE"


#: The order the round-robin walks. The four "HIGH" directions of §3 come first;
#: ``AUTOMATION_AI`` is the §3 "MEDIUM" one, and the two leftovers fill spare slots
#: only after every target direction has had its turn.
CATEGORY_PRIORITY: tuple[ShortlistCategory, ...] = (
    ShortlistCategory.BITRIX24_CRM,
    ShortlistCategory.IT_SUPPORT,
    ShortlistCategory.SYSADMIN_INFRA,
    ShortlistCategory.APP_INTEGRATION_SUPPORT,
    ShortlistCategory.AUTOMATION_AI,
    ShortlistCategory.OTHER,
    ShortlistCategory.OFF_PROFILE,
)

#: §11 asks for the distribution of exactly these five.
COVERAGE_CATEGORIES: tuple[ShortlistCategory, ...] = (
    ShortlistCategory.BITRIX24_CRM,
    ShortlistCategory.IT_SUPPORT,
    ShortlistCategory.SYSADMIN_INFRA,
    ShortlistCategory.APP_INTEGRATION_SUPPORT,
    ShortlistCategory.AUTOMATION_AI,
)

#: The directions the candidate actually targets. ``OTHER`` and ``OFF_PROFILE`` are
#: not directions: they are the leftover bucket and the reject bucket. They may only
#: take a read slot that no target card can use, and only after every target direction
#: has been exhausted.
TARGET_CATEGORIES: tuple[ShortlistCategory, ...] = COVERAGE_CATEGORIES


class CheapTier(str, Enum):
    """How much the *card* is worth reading. Not a fit verdict."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# ---------------------------------------------------------------------------
# P1: Search Streams (REMOTE_GLOBAL, THAILAND_LOCAL, GIGS_FREELANCE)
# ---------------------------------------------------------------------------

class SearchStream(str, Enum):
    REMOTE_GLOBAL = "REMOTE_GLOBAL"
    THAILAND_LOCAL = "THAILAND_LOCAL"
    GIGS_FREELANCE = "GIGS_FREELANCE"


def is_thailand_local(card: Mapping[str, Any] | Any) -> bool:
    """Check if the vacancy or card represents a local position in Thailand."""
    if isinstance(card, Mapping):
        source = str(card.get("source") or "").lower()
        area = str(card.get("area") or card.get("location") or card.get("city") or "").lower()
        title = str(card.get("title") or "").lower()
        url = str(card.get("url") or card.get("job_url") or "").lower()
        company = str(card.get("employer") or card.get("company") or "").lower()
    else:
        source = str(getattr(card, "source", "") or "").lower()
        area = str(getattr(card, "location", "") or getattr(card, "city", "") or "").lower()
        title = str(getattr(card, "title", "") or "").lower()
        url = str(getattr(card, "url", "") or getattr(card, "job_url", "") or "").lower()
        company = str(getattr(card, "company", "") or getattr(card, "employer", "") or "").lower()

    combined = f"{source} {area} {title} {url} {company}"
    th_keywords = [
        "bangkok", "бангкок", "pattaya", "паттайя", "thailand", "таиланд",
        "phuket", "пхукет", "agoda", "lalamove", "bitkub", "true corp", "scb 10x"
    ]
    return any(k in combined for k in th_keywords)


def is_gig_freelance(card: Mapping[str, Any] | Any) -> bool:
    """Check if the vacancy or card represents a gig or freelance contract."""
    if isinstance(card, Mapping):
        source = str(card.get("source") or "").lower()
        is_gig = bool(card.get("is_gig"))
        url = str(card.get("url") or card.get("job_url") or "").lower()
    else:
        source = str(getattr(card, "source", "") or "").lower()
        is_gig = bool(getattr(card, "is_gig", False))
        url = str(getattr(card, "url", "") or getattr(card, "job_url", "") or "").lower()

    if is_gig:
        return True
    gig_sources = ["tg_zakazy_it", "tg_freelance_orders", "kwork", "freelancer", "contra", "habr_freelance", "fl_ru"]
    return any(s in source or s in url for s in gig_sources)


def resolve_search_stream(card: Mapping[str, Any] | Any) -> SearchStream:
    """Determine the active search stream for a vacancy, card or order."""
    if isinstance(card, Mapping):
        explicit = card.get("search_stream")
    else:
        explicit = getattr(card, "search_stream", None)

    if explicit:
        try:
            return SearchStream(str(explicit).upper())
        except ValueError:
            pass

    if is_gig_freelance(card):
        return SearchStream.GIGS_FREELANCE
    if is_thailand_local(card):
        return SearchStream.THAILAND_LOCAL
    return SearchStream.REMOTE_GLOBAL


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


def _hit(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(p.search(text) for p in patterns)


# §3 "низкий/исключать" + the stacks the profile excludes outright. Checked first,
# because a title like "Менеджер по продажам CRM" is a sales role that happens to
# name the system we target - and the Bitrix24-only live run really did return a
# page of sales and logistics vacancies.
_OFF_PROFILE = _rx(
    r"продаж", r"\bsales\b", r"менеджер по работе с клиент", r"account manager",
    r"маркет", r"\bmarketing\b", r"\bseo\b", r"smm",
    r"\bcompliance\b", r"\baml\b", r"\bkyc\b",
    r"локализац", r"\blocalization\b", r"\btranslat", r"переводчик",
    r"\bqa\b", r"\bsdet\b", r"тестировщик", r"тестирование по",
    r"\brecruiter\b", r"рекрутер", r"подбор персонала", r"\bhr\b",
    r"бухгалтер", r"\baccountant\b", r"юрист", r"\blawyer\b", r"\blegal counsel\b",
    r"дизайнер", r"\bdesigner\b", r"копирайтер", r"\bcopywriter\b",
    r"клиентского опыта", r"customer experience", r"customer success",
    r"логистик", r"\blogistics\b", r"таможенн", r"\bcustoms\b", r"декларант",
    r"консультант по карьере", r"карьерный консультант", r"психолог",
    r"менеджер по закупк", r"снабжен", r"секретарь", r"офис-менеджер",
    # Client-facing management that merely says "support". Measured live:
    # «Менеджер по развитию бизнеса (услуга Professional Support)» and
    # «Customer Support Manager» both classified as IT_SUPPORT/HIGH, because
    # _IT_SUPPORT matches the bare word "support". §3 puts sales and client
    # management off-profile, and "business development" is sales by another name.
    r"развити[юя] бизнеса", r"\bbusiness development\b", r"customer support manager",
    r"\bclient support manager\b", r"\bcustomer care\b", r"\bclient care\b",
    r"менеджер клиентск", r"клиентск[а-я]* поддержк", r"менеджер по сопровождению клиент",
    # the profile's own excluded stacks
    r"\bphp\b", r"\bruby\b", r"\b1c\b", r"\b1с\b", r"\bc\+\+\b", r"\bc#\b",
    r"java разработчик", r"java developer", r"разработчик 1с",
)

_BITRIX24_CRM = _rx(
    r"bitrix", r"битрикс", r"\bcrm\b", r"срм",
    r"salesforce", r"amocrm", r"amo crm", r"hubspot",
)

_APP_INTEGRATION_SUPPORT = _rx(
    r"application support", r"поддержк[аи] приложени",
    r"сопровожд", r"внедрен", r"\bintegration\b", r"интеграц",
    r"\bimplementation\b", r"\bimpl[ae]?ment\b",
)

_IT_SUPPORT = _rx(
    r"техническ[а-я]* поддержк", r"\btech\w* support\b", r"\bit support\b",
    r"\bsupport engineer\b", r"\bsupport specialist\b", r"\bsupport manager\b",
    r"\bsupport technician\b", r"\bhelp\s?desk\b", r"\bservice\s?desk\b",
    r"\bсаппорт\b", r"\bsupport\b", r"поддержк",
)

_SYSADMIN_INFRA = _rx(
    r"системн[а-я]* администратор", r"\bsystem administrator\b", r"\bsysadmin\b",
    r"\bit administrator\b", r"\bwindows server\b", r"\bactive directory\b",
    r"инфраструктур", r"\binfrastructure\b", r"\bnetwork administrator\b",
    r"сетево[а-я]* администратор", r"\bserver administrator\b",
    r"администратор (?:windows|linux|серверов|сети)",
)

_AUTOMATION_AI = _rx(
    r"автоматизац", r"\bautomation\b", r"\bautomate\b",
    r"\bn8n\b", r"\bmake\.com\b", r"\bzapier\b",
    r"\bai engineer\b", r"\bai developer\b", r"\bai agent\b", r"\bllm\b",
    r"\bml engineer\b", r"\bmachine learning\b",
    r"\bии\b", r"искусственн[а-я]* интеллект", r"\bai\b",
    r"telegram", r"телеграм", r"ботов", r"\bbot\b", r"\bботы\b",
)


def classify_category(title: str, employer: str = "", description: str = "") -> ShortlistCategory:
    """One card -> one direction. Off-profile is decided before anything else.

    Only the title and the employer are consulted by default: the description is
    not on the card, and a classifier that quietly needed a field the caller does
    not have would silently return ``OTHER`` for everything.
    """
    text = f"{title or ''} {employer or ''}"
    if description:
        text = f"{text} {description}"

    if any(p.search(text) for p in _OFF_PROFILE):
        return ShortlistCategory.OFF_PROFILE
    # Bitrix24/CRM before the generic support patterns: "Специалист по внедрению и
    # сопровождению CRM" is a CRM role that also reads as an application-support one.
    if any(p.search(text) for p in _BITRIX24_CRM):
        return ShortlistCategory.BITRIX24_CRM
    if any(p.search(text) for p in _APP_INTEGRATION_SUPPORT):
        return ShortlistCategory.APP_INTEGRATION_SUPPORT
    if any(p.search(text) for p in _IT_SUPPORT):
        return ShortlistCategory.IT_SUPPORT
    if any(p.search(text) for p in _SYSADMIN_INFRA):
        return ShortlistCategory.SYSADMIN_INFRA
    if any(p.search(text) for p in _AUTOMATION_AI):
        return ShortlistCategory.AUTOMATION_AI
    return ShortlistCategory.OTHER


# ---------------------------------------------------------------------------
# cheap rank - "what is worth the expensive read", not "who do we hire"
# ---------------------------------------------------------------------------

_HIGH_CATEGORIES = frozenset({
    ShortlistCategory.BITRIX24_CRM,
    ShortlistCategory.IT_SUPPORT,
    ShortlistCategory.SYSADMIN_INFRA,
    ShortlistCategory.APP_INTEGRATION_SUPPORT,
})

# §3: penalise what the profile cannot back. These are card-level *signals*; the
# full read is what decides, and a signal is never treated as a verdict.
_HEAVY_SENIOR = _rx(r"\bsenior\b", r"\blead\b", r"\bprincipal\b", r"\bhead of\b",
                    r"\bведущий\b", r"\bруководитель\b", r"\bдиректор\b", r"\bchief\b")
# ``engineer`` is deliberately absent: "Support Engineer" and "Infrastructure
# Engineer" are exactly the roles this profile targets, and a penalty on the word
# would have punished the shortlist for being on target.
_DEV_ROLE = _rx(r"\bdeveloper\b", r"\bразработчик\b", r"\bпрограммист\b",
                r"\barchitect\b", r"\bархитектор\b")
_HEAVY_DEVOPS = _rx(r"\bkubernetes\b", r"\bk8s\b", r"\bterraform\b", r"\bdevops\b",
                    r"\baws\b", r"\bazure\b", r"\bgcp\b", r"\bsre\b")


@dataclass
class CheapRank:
    """The card-level verdict, with the reasons kept so a report can show them."""

    category: ShortlistCategory
    tier: CheapTier
    score: int
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "tier": self.tier.value,
            "score": self.score,
            "reasons": list(self.reasons),
        }


def cheap_rank(title: str, employer: str = "", *, category: ShortlistCategory | None = None) -> CheapRank:
    """A title-level score in ``[0, 100]``, plus the tier the selector walks.

    Deliberately shallow. It sees a title and a company name; it cannot see a
    mandatory requirement, a geography clause or a salary, so it is never allowed
    to *reject* a vacancy on the candidate's behalf - it only decides what to read.
    """
    cat = category or classify_category(title, employer)
    text = f"{title or ''} {employer or ''}"
    reasons: list[str] = []

    if cat is ShortlistCategory.OFF_PROFILE:
        return CheapRank(cat, CheapTier.LOW, 0, ["off-profile direction"])

    if cat in _HIGH_CATEGORIES:
        score = 70
        tier = CheapTier.HIGH
        reasons.append(f"target direction {cat.value}")
    elif cat is ShortlistCategory.AUTOMATION_AI:
        score = 55
        tier = CheapTier.MEDIUM
        reasons.append("automation/AI — medium priority per §3")
    else:
        score = 35
        tier = CheapTier.LOW
        reasons.append("direction not in the §3 priority list")

    if _hit(_HEAVY_SENIOR, text):
        score -= 10
        reasons.append("seniority marker (senior/lead/head)")
    if _hit(_DEV_ROLE, text) and cat in _HIGH_CATEGORIES:
        # "Support Engineer" is fine; "Senior Software Engineer" is not the same job.
        score -= 10
        reasons.append("engineering-heavy title in a support direction")
    if _hit(_HEAVY_DEVOPS, text):
        score -= 15
        reasons.append("heavy DevOps/cloud marker")

    return CheapRank(cat, tier, max(0, min(100, score)), reasons)


# ---------------------------------------------------------------------------
# balanced selection
# ---------------------------------------------------------------------------

@dataclass
class SelectionResult:
    """The chosen ids, in read order, plus enough to explain the choice."""

    targets: list[str] = field(default_factory=list)
    by_category: dict[str, list[str]] = field(default_factory=dict)
    eligible_total: int = 0
    off_profile_total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "targets": list(self.targets),
            "by_category": {k: list(v) for k, v in self.by_category.items()},
            "eligible_total": self.eligible_total,
            "off_profile_total": self.off_profile_total,
        }


def _card_title(card: Mapping[str, Any]) -> str:
    return str(card.get("title") or "")


def _card_employer(card: Mapping[str, Any]) -> str:
    return str(card.get("employer") or "")


def select_read_targets(
    cards: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    read_limit: int,
    *,
    include_off_profile: bool = False,
    stream: SearchStream | str | None = None,
) -> SelectionResult:
    """Choose which discovered vacancies get the expensive full read.

    ``cards`` is either the ``seen`` mapping of ``run_search_operation`` (id ->
    card) or a sequence of cards carrying ``vacancy_id``.

    ``stream`` optionally scopes selection to a single SearchStream (e.g.
    THAILAND_LOCAL, REMOTE_GLOBAL, or GIGS_FREELANCE).

    The walk is **round-robin across the directions**, best card first inside each,
    so the order of the query list stops deciding the shortlist. Slots left over
    after every direction has been served are filled by score, which is what keeps
    a run with one very strong direction from wasting its budget.

    A vacancy is returned at most once: the caller's ``seen`` map is already keyed
    by id, and this function never emits a duplicate.
    """
    limit = max(0, int(read_limit))
    if limit == 0:
        return SelectionResult()

    target_stream: SearchStream | None = None
    if stream:
        try:
            target_stream = SearchStream(str(stream).upper())
        except ValueError:
            target_stream = None

    if isinstance(cards, Mapping):
        items = [(str(k), v) for k, v in cards.items()]
    else:
        items = [(str(c.get("vacancy_id") or ""), c) for c in cards]

    if target_stream is not None:
        items = [(vid, c) for vid, c in items if resolve_search_stream(c) == target_stream]

    ranked: dict[ShortlistCategory, list[tuple[int, int, str, CheapRank]]] = {
        c: [] for c in CATEGORY_PRIORITY
    }
    off_profile = 0

    for index, (vid, card) in enumerate(items):
        if not vid:
            continue
        rank = cheap_rank(_card_title(card), _card_employer(card))
        if rank.category is ShortlistCategory.OFF_PROFILE:
            off_profile += 1
            if not include_off_profile:
                continue
        ranked[rank.category].append((-rank.score, index, vid, rank))

    for bucket in ranked.values():
        bucket.sort()

    eligible_total = sum(len(b) for c, b in ranked.items()
                         if c is not ShortlistCategory.OFF_PROFILE)

    result = SelectionResult(eligible_total=eligible_total, off_profile_total=off_profile)
    cursors = {c: 0 for c in CATEGORY_PRIORITY}
    chosen: set[str] = set()

    def take(cat: ShortlistCategory) -> str | None:
        bucket = ranked[cat]
        while cursors[cat] < len(bucket):
            _, _, vid, _ = bucket[cursors[cat]]
            cursors[cat] += 1
            if vid not in chosen:
                chosen.add(vid)
                result.by_category.setdefault(cat.value, []).append(vid)
                return vid
        return None

    # Lap 1..n over the *target* directions only. One card per direction per lap, so
    # a direction that sits late in the query list is served as early as the first.
    # Stops when the budget is spent or no target direction has anything left.
    progressed = True
    while len(result.targets) < limit and progressed:
        progressed = False
        for cat in TARGET_CATEGORIES:
            if len(result.targets) >= limit:
                break
            vid = take(cat)
            if vid is not None:
                result.targets.append(vid)
                progressed = True

    # Only now the leftovers. Measured live: with 269 eligible target cards and a
    # budget of 30, the plain round-robin still handed 5 slots to OTHER and 3 to
    # OFF_PROFILE, because those buckets took a turn on every lap. They are not
    # directions - they fill spare capacity, they must not compete for it.
    if len(result.targets) < limit and include_off_profile:
        for cat in (ShortlistCategory.OTHER, ShortlistCategory.OFF_PROFILE):
            while len(result.targets) < limit:
                vid = take(cat)
                if vid is None:
                    break
                result.targets.append(vid)

    return result


def category_distribution(ids: Iterable[str],
                          cards: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """Count the ids per direction, for the §11 coverage table."""
    counts = {c.value: 0 for c in CATEGORY_PRIORITY}
    for vid in ids:
        card = cards.get(str(vid))
        if card is None:
            continue
        cat = classify_category(_card_title(card), _card_employer(card))
        counts[cat.value] = counts.get(cat.value, 0) + 1
    return counts
