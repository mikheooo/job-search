"""Knowledge base -> master resume (Stage 0 / T5).

The master resume is the first artifact that a human outside this repository
could read, so it is also the first place where a wrong fact would leave the
building. Two rules keep that from happening:

**It reads the knowledge base and nothing else.** Every line carries the
``claim_id`` it came from, and the source map prints them. There is no second
source, so there is no second truth: if a fact is wrong, it is wrong in the KB
and the fix belongs there.

**It cannot promote a fact.** A claim is rendered only if its status is
``DERIVED_FROM_SOURCE`` or ``CONFIRMED_BY_USER`` -- and it is *labelled* with
which. A ``REJECTED_BY_USER`` claim, a claim superseded by a later answer, a
claim no longer present in its source, and a claim flagged as an assumption are
all dropped, each with a reason that :func:`exclusions` will report. Nothing is
quietly dropped and nothing is quietly upgraded.

Three things the generator does that are *not* reading the KB, all declared in
code and all reported as needing the candidate's approval:

* the **headline** is assembled from the candidate's own role lists in
  ``candidate_profile.json`` plus the confirmed Bitrix24 preference;
* the **"own projects, no paid client work"** line and the move of the former
  "Freelance" heading into Independent Projects, both driven by the confirmed
  interview fact named in :data:`INDEPENDENT_ROLE_EVIDENCE`;
* **omitted spans** in quoted text, listed in :data:`OMITTED_SPANS` with the
  reason and the rule that found the problem.

The last one matters most. The resume's summary opens with "15+ years of IT
experience", which T3 already flags against ``domain_years.it_support = 11.0``.
Rather than silently reword a claim, the generator deletes the span, and refuses
to run if the span is no longer there to delete -- so the omission cannot become
invisible by accident.

Every claim in the KB ends up in exactly one of three places, and
``test_every_claim_is_rendered_excluded_or_subsumed`` holds that partition: a
line of the resume, an :func:`exclusions` entry (a fact deliberately withheld,
with the reason), or a :func:`subsumed` entry (a fact that is present but carried
by a different line). A claim that fell out of all three would be a fact nobody
could see was missing, which is the failure this partition exists to catch.

Nothing here writes to the resume, the profile or the database. The only file it
produces is the draft it is asked to produce.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .kb import (
    CandidateKnowledgeBase,
    ClaimRecord,
    ClaimStatus,
)

__all__ = [
    "APPROVED_EDITORIAL_DECISIONS",
    "APPROVED_PROJECTS_PHRASE",
    "APPROVED_SUMMARY_OPENING",
    "APPROVED_TENURE",
    "APPROVED_TEXT_EDITS",
    "CHRONOLOGY_EMPLOYER",
    "DUTY_SOURCED_GROUP_HEADING",
    "DUTY_SOURCED_SKILL_GROUPS",
    "EXCLUDED_CLAIMS",
    "EXCLUDED_KINDS",
    "INDEPENDENT_ROLE_EVIDENCE",
    "POSITIONING_LABELS",
    "PRIVATE_KINDS",
    "PROPOSALS",
    "STATUS_WITHOUT_ANCHOR",
    "SUBSUMED_KINDS",
    "Exclusion",
    "Line",
    "MasterResume",
    "MissingTranslation",
    "build_master_resume",
    "exclusions",
    "render_markdown",
    "render_source_map",
    "subsumed",
    "unaccounted_claims",
]


class MissingTranslation(KeyError):
    """The Russian variant is missing a line.

    Raised rather than falling back to English: a review document that is
    silently half-translated reads as complete, and the point of the Russian
    version is that the candidate can check every line.
    """


# ---------------------------------------------------------------------------
# Declared presentation decisions -- each one is reported, none is inferred
# ---------------------------------------------------------------------------

#: Claims that describe how the candidate searches, not what the candidate has
#: done. A public resume is not the place for a salary floor or a search
#: strategy, so these are excluded by kind rather than by wording.
PRIVATE_KINDS: dict[str, str] = {
    "goal_direction": "search strategy, not resume content",
    "preference_role": "search strategy, not resume content",
    "preference_condition": "salary expectation: excluded from the public resume on purpose",
}

#: Whole kinds whose every claim misstates something and so must not be shown.
EXCLUDED_KINDS: dict[str, str] = {
    "experience_statement": (
        "restates total IT tenure in a form that equates it with tenure in one "
        "domain; contradicted by candidate_profile.json domain_years.it_support "
        "= 11.0 and by the T3 rule total_experience_vs_it_support. The same "
        "phrase is deleted from the summary via OMITTED_SPANS."
    ),
}

#: Individual claims excluded by identity. Kind-level policy cannot express
#: these: one employer name in a block is a misstatement while its siblings are
#: fine, so the reason has to be attached to the claim.
EXCLUDED_CLAIMS: dict[str, str] = {
    "role_company-fcd10805e4": (
        "'Freelance - AI Integration & Automation' names an employer and implies "
        "paid client work, which the candidate confirmed did not happen; the "
        "period is presented as 'own projects' instead"
    ),
}

#: Claims whose content the resume represents through another line instead of
#: quoting them. Recorded so that no fact can quietly disappear: a claim that is
#: neither rendered nor excluded nor subsumed fails
#: ``test_every_claim_is_rendered_excluded_or_subsumed``.
SUBSUMED_KINDS: dict[str, str] = {
    "headline": (
        "represented by the resume headline, which is assembled as a proposal "
        "(POSITIONING_LABELS) rather than quoted verbatim; the candidate's own "
        "wording stays in the KB for per-vacancy versions"
    ),
    "title": (
        "one of the candidate's own target titles, represented by the headline "
        "proposal; kept in the KB as raw material for per-vacancy versions"
    ),
}

#: Roles the candidate has told us are not paid employment, and the confirmed
#: claim that says so. Keyed by role claim id; the value must be a
#: ``CONFIRMED_BY_USER`` claim whose ``role_id`` is that role, which
#: ``test_independent_role_evidence_is_confirmed_and_bound`` enforces. Without
#: that check this table would be an assertion about the candidate's history
#: that nothing verifies.
INDEPENDENT_ROLE_EVIDENCE: dict[str, str] = {
    "role-aee78081ce": "experience_note-7d59a994c6",
}

#: The confirmed chronology correction, and the role block it applies to.
CHRONOLOGY_EVIDENCE: dict[str, str] = {
    "role-7dcdc7ae59": "chronology_note-451eec4688",
}

#: The employer inside the role that the confirmed period belongs to.
#:
#: This is not decoration. The role's own heading names four employers in one
#: block, and the confirmed period is one company's, not the block's: rendering
#: "March 2018 - March 2021" against the combined heading would date Intravision,
#: Slavyanka and Russian Post by a fact nobody confirmed about them. The value
#: must be one of the role's ``role_employer`` claims, which
#: ``test_the_confirmed_period_names_an_employer_of_that_role`` enforces.
CHRONOLOGY_EMPLOYER: dict[str, str] = {
    "role-7dcdc7ae59": "Speedcast / Germsat",
}

#: The period the candidate confirmed, transcribed from the claim above. The
#: Russian month names in that claim are checked against this table, so the two
#: cannot drift apart without a test noticing.
CHRONOLOGY_PERIOD: dict[str, tuple[str, str]] = {
    "role-7dcdc7ae59": ("March 2018", "March 2021"),
}

_RU_MONTHS: dict[str, int] = {
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}

#: Role directions the master resume positions the candidate across. A proposal,
#: not a fact, and marked as one wherever it is reported.
#:
#: Two deliberate properties:
#:
#: * the set is **universal**. The candidate's stated priority is to find any
#:   suitable IT job quickly, with Bitrix24 on a par with everything else, so the
#:   headline must not sell an AI Engineer to an IT-support advert. Support,
#:   systems administration, Bitrix24/CRM, automation and the candidate's own AI
#:   projects are all first-class here.
#: * the order **carries no priority**. It is alphabetical, so no reader can
#:   infer a ranking, and no single direction is made the main one for every
#:   vacancy. Per-vacancy versions (T6/T7) choose their own lead; this is the
#:   source they choose from.
POSITIONING_LABELS: tuple[str, ...] = (
    "Application Support & IT Support",
    "Bitrix24 / CRM Automation",
    "Python Automation & AI Projects",
    "Systems & Network Administration",
)

#: The tenure figure the candidate approved, in the wording he approved. It is
#: the *only* figure the resume states: ``years_experience = 3`` in the profile
#: means something the candidate has not defined, and the brief must not spend
#: the total IT history on a per-technology requirement (see T6.2).
APPROVED_TENURE = "15+ years in IT"

#: The opening the candidate approved for the professional summary.
#:
#: It replaces the quoted summary's first clause, which did two things the
#: master resume must not do: it stated a single-direction identity ("AI
#: Automation Engineer") in a document that positions the candidate across
#: support, systems administration, Bitrix24/CRM and automation, and it stated a
#: tenure figure in wording nobody had approved. The candidate's own words stay
#: untouched in the KB.
APPROVED_SUMMARY_OPENING = (
    "IT specialist with 15+ years in IT: technical support, systems "
    "administration, Bitrix24/CRM, automation, and my own projects built with "
    "AI tools."
)

#: The wording the candidate approved for what used to read "in production".
#:
#: "In production" describes systems that do run, but for personal projects with
#: no client it can read as a claim about a commercial deployment. The approved
#: wording says where the systems are used and claims no client, no revenue and
#: no metric.
APPROVED_PROJECTS_PHRASE = "used in my work and in my own projects"

#: Declared edits to quoted text: ``(span, replacement, reason)``.
#:
#: Both edits are *substitutions*, not silent rewrites: the span being replaced
#: is named here, the replacement is the wording the candidate approved, and a
#: span that is no longer present is an error. That last part matters more than
#: it looks -- an edit that has quietly stopped applying leaves the draft reading
#: as if it were approved, and nothing in the output would say otherwise.
APPROVED_TEXT_EDITS: tuple[tuple[str, str, str], ...] = (
    (
        "AI Automation Engineer with 15+ years of IT experience: ",
        # The trailing space is the sentence separator the span consumed: the
        # next word in the quote is "API", and without it the draft would read
        # "AI tools.API integration".
        APPROVED_SUMMARY_OPENING + " ",
        (
            "the quoted clause names one direction and states an unapproved "
            "tenure wording. Replaced by the opening the candidate approved; "
            "the candidate's own sentence stays untouched in the KB."
        ),
    ),
    (
        "in production",
        APPROVED_PROJECTS_PHRASE,
        (
            "'in production' is the candidate's own wording and describes systems "
            "that do run (Reels Analyzer runs under Docker with PostgreSQL and "
            "Redis; Job Search AI is in daily use), but for personal projects "
            "with no client it can read as a claim about a commercial "
            "deployment. Replaced by the approved wording about where the "
            "systems are used. No client, no revenue and no metric is claimed "
            "anywhere in the draft."
        ),
    ),
)

#: Projects whose status label is **not** backed by a quoted line in the repository.
#:
#: The label the resume prints (``— Implemented``, ``— Prototype``) comes from
#: ``PROJECT_CATALOGUE``: it is the catalogue's reading of the project's state, not
#: a sentence the project wrote about itself. Where the repository does state its
#: status, an anchored ``project_status`` claim carries that sentence verbatim
#: (Repiton does, twice: ``STAGE_1_FINAL_NO_GO`` and ``TRACK BLOCKED``), and such a
#: project must **not** appear here.
#:
#: Listing a project here is a claim that the label is the author's judgement, which
#: is why the reason is required.
#: ``test_every_project_status_is_either_anchored_or_declared`` holds the partition,
#: so a project added without either cannot slip through.
STATUS_WITHOUT_ANCHOR: dict[str, str] = {
    "job_search_ai": (
        "the repository documents its safety gates, its sources and its application "
        "flow; it never states a completion status, so 'Implemented' is the "
        "catalogue's reading"
    ),
    "reels_analyzer": (
        "the repository documents its pipeline and its stack; it never states a "
        "completion status, so 'Implemented' is the catalogue's reading"
    ),
    "whubn": (
        "the repository documents its engine, its map and its segmentation; it never "
        "states a completion status, so 'Implemented' is the catalogue's reading"
    ),
}

#: Heading of the core-skills group sourced from the candidate's duties rather
#: than from ``skill`` claims.
DUTY_SOURCED_GROUP_HEADING = "IT Support & Systems Administration"

#: A core-skills group whose members are named by the candidate's duties:
#: ``(group heading, ((label, claim_id, phrase the claim must contain), ...))``.
#:
#: The KB holds no ``skill`` claim for any of these -- its 32 skill claims are
#: all AI, automation and Python -- yet the candidate's professional history
#: names every one of them: one duty of the 2009-2018 block carries the Windows
#: Server / AD / Exchange / Hyper-V / networking / IP-telephony list, the
#: Avantis duty carries Bitrix24, and the same role's support duty carries
#: technical support. So the group is declared here and sourced from those
#: duties rather than invented as skills.
#:
#: ``phrase`` is not decoration: it is the text the cited claim must literally
#: contain, which is what makes "every skill has a source in the candidate's
#: professional history" checkable rather than a promise. It differs from the
#: label where the history says the thing in other words -- the history says
#: "technical support", the label reads "IT Support" -- and naming the phrase is
#: how that difference stays visible instead of hidden.
DUTY_SOURCED_SKILL_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        DUTY_SOURCED_GROUP_HEADING,
        (
            ("Windows Server", "duty-39d5320cd4", "Windows Server"),
            ("Active Directory", "duty-39d5320cd4", "Active Directory"),
            ("Exchange", "duty-39d5320cd4", "Exchange"),
            ("Hyper-V", "duty-39d5320cd4", "Hyper-V"),
            ("Networking", "duty-39d5320cd4", "networking"),
            ("IP Telephony", "duty-39d5320cd4", "IP telephony"),
            ("IT Support", "duty-94b646ace9", "technical support"),
            ("Bitrix24", "duty-d87a2fdc28", "Bitrix24"),
        ),
    ),
)

#: Text this generator would add, offered for approval rather than published.
#: Each entry is (label, proposed text, why it needs a decision). Nothing here
#: reaches the resume: the resume carries quotes and declared decisions, and
#: new prose about the candidate is the candidate's to approve first.
#:
#: **Empty as of T5.2 completion.** The three entries that used to live here --
#: the summary opening, the tenure figure and the "in production" wording --
#: have all been approved by the candidate, so they moved to
#: :data:`APPROVED_EDITORIAL_DECISIONS` and are now published. An entry belongs
#: in exactly one of the two tables: keeping an approved decision here would make
#: the resume fail ``test_no_proposal_reaches_either_draft``, and keeping it out
#: of both would make the published wording unattributable.
PROPOSALS: tuple[tuple[str, str, str], ...] = ()

#: Editorial decisions the candidate has approved, and the evidence each rests
#: on: ``(label, published text, why it is defensible)``.
#:
#: This is the source map's answer to "who decided this?". The resume publishes
#: new prose about the candidate, which no KB claim contains, so the decision and
#: its reasoning have to be visible next to the text -- otherwise the next reader
#: cannot tell approved wording from invented wording.
APPROVED_EDITORIAL_DECISIONS: tuple[tuple[str, str, str], ...] = (
    (
        "Professional summary — opening",
        APPROVED_SUMMARY_OPENING,
        (
            "Approved by the candidate. Names the four directions the resume "
            "positions him across instead of the single direction the quoted "
            "clause did. The tenure figure inside it is the next entry."
        ),
    ),
    (
        "Tenure — the figure that is published",
        APPROVED_TENURE,
        (
            "Approved by the candidate. Supported by the resume's own timeline "
            "rather than asserted: the earlier block starts in 2009 and the last "
            "employed role ends April 2026, about 17 years. It does not "
            "contradict domain_years.it_support = 11.0, which measures the "
            "support domain rather than the whole career, and it is not "
            "years_experience = 3, which is a third number and is not total "
            "tenure."
        ),
    ),
    (
        "Projects — the phrase 'in production'",
        APPROVED_PROJECTS_PHRASE,
        (
            "Approved by the candidate. Replaces 'in production' in the quoted "
            "summary. The systems do run, but for personal projects with no "
            "client the old phrase can read as a claim about a commercial "
            "deployment. No client, no revenue and no metric is claimed."
        ),
    ),
    (
        "Core skills — the support & systems group",
        DUTY_SOURCED_GROUP_HEADING,
        (
            "Approved by the candidate. The KB's 32 skill claims name no Windows "
            "Server, Active Directory, Exchange, Hyper-V, networking, IP "
            "telephony or Bitrix24, so the group is sourced from the duties that "
            "do name them -- each entry carries the claim it came from, and "
            "test_every_duty_sourced_skill_names_its_source checks that the duty "
            "really contains the technology."
        ),
    ),
)

#: The line that separates paid employment from the candidate's own projects.
#: It carries no dates of its own: the period is stated by the role's own
#: ``role_start`` / ``role_end`` claims, which have their own provenance.
INDEPENDENT_PROJECTS_NOTE = (
    "No paid client work in this period (confirmed by the candidate)."
)

EMPLOYMENT_HEADING = "EMPLOYMENT HISTORY"
INDEPENDENT_HEADING = "INDEPENDENT PROJECTS"

#: Human labels for the origin markers in ``evidence["source_file"]``, used by the
#: source map's brief table. An unlabelled marker is reported as such rather than
#: silently omitted: a new origin appearing is something a reader should see.
SOURCE_LABELS: dict[str, tuple[str, str]] = {
    "resume.md": ("`resume.md` (T1 parser)", "the candidate's own resume, line by line"),
    "interview": (
        "T4.5 interview",
        "answers the candidate confirmed; the only claims above DERIVED_FROM_SOURCE",
    ),
    "project": (
        "project catalogue (T5)",
        "quotes anchored in four repositories, each re-read and hashed on every run",
    ),
}


def _source_counts(kb: CandidateKnowledgeBase) -> dict[str, int]:
    """Claim count per origin marker, computed from the KB rather than written down."""
    counts: dict[str, int] = {}
    for record in kb.by_status():
        marker = str(record.evidence.get("source_file") or "unknown")
        counts[marker] = counts.get(marker, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Rendering primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One rendered line, with the claims behind it.

    ``translate`` is False for proper nouns -- company names, technology names,
    project names -- which are the same in both languages and must not be
    invented a second time.

    ``ru_text`` overrides the Russian rendering. It exists for a claim whose
    source is already Russian: the English draft needs a gloss, while the
    Russian review document should carry the original words.
    """

    text: str
    kind: str
    claim_ids: tuple[str, ...] = ()
    translate: bool = True
    ru_text: str | None = None


@dataclass(frozen=True)
class Exclusion:
    """A claim that did not reach the resume, and why."""

    claim_id: str
    kind: str
    value: str
    reason: str


@dataclass
class MasterResume:
    """The built resume: lines, the provenance behind them, and what was dropped."""

    lines: list[Line] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)
    subsumed: list[Exclusion] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def rendered_claim_ids(self) -> frozenset[str]:
        return frozenset(self.claim_ids)


# ---------------------------------------------------------------------------
# Exclusion policy
# ---------------------------------------------------------------------------


def _exclusion_reason(record: ClaimRecord) -> str | None:
    """Why this claim must not appear, or ``None`` if it may."""
    if record.status == ClaimStatus.REJECTED_BY_USER:
        return "REJECTED_BY_USER"
    if record.evidence.get("superseded_by"):
        return f"superseded by {record.evidence['superseded_by']}"
    if record.status not in (
        ClaimStatus.DERIVED_FROM_SOURCE,
        ClaimStatus.CONFIRMED_BY_USER,
    ):
        return f"status {record.status} is not a fact"
    if not record.in_source:
        return "no longer present in the source it came from"
    fact_class = str(record.evidence.get("fact_class") or "").upper()
    if fact_class in {"ASSUMPTION", "MISSING", "UNCONFIRMED"}:
        return f"flagged {fact_class}: not an established fact"
    if record.claim_id in EXCLUDED_CLAIMS:
        return EXCLUDED_CLAIMS[record.claim_id]
    if record.kind in EXCLUDED_KINDS:
        return EXCLUDED_KINDS[record.kind]
    if record.kind in PRIVATE_KINDS:
        return PRIVATE_KINDS[record.kind]
    return None


def subsumed(kb: CandidateKnowledgeBase) -> list[Exclusion]:
    """Claims the resume represents through another line rather than quoting.

    Same shape as :func:`exclusions` so the report can print both tables with
    one renderer. Kept separate because the meaning differs: an exclusion is a
    fact deliberately withheld, a subsumption is a fact that is present but
    carried by a different line.
    """
    out: list[Exclusion] = []
    for record in kb.by_status():
        if _exclusion_reason(record) is not None:
            continue
        reason = SUBSUMED_KINDS.get(record.kind)
        if reason is not None:
            out.append(
                Exclusion(
                    claim_id=record.claim_id,
                    kind=record.kind,
                    value=record.value,
                    reason=reason,
                )
            )
    return out


def exclusions(kb: CandidateKnowledgeBase) -> list[Exclusion]:
    """Every claim the master resume refuses to carry, with its reason."""
    out: list[Exclusion] = []
    for record in kb.by_status():
        reason = _exclusion_reason(record)
        if reason is not None:
            out.append(
                Exclusion(
                    claim_id=record.claim_id,
                    kind=record.kind,
                    value=record.value,
                    reason=reason,
                )
            )
    return out


def unaccounted_claims(kb: CandidateKnowledgeBase, resume: MasterResume) -> list[ClaimRecord]:
    """Claims in neither the rendered set, the exclusions nor the subsumptions.

    The invariant this enforces is the whole point of the three-way split: a
    claim that is not on a line, not declared withheld and not declared
    represented is a fact that has silently left the resume, and nothing in the
    output would show it. It returns the offenders rather than a bool so the
    caller can name them.
    """
    seen = set(resume.rendered_claim_ids())
    seen.update(entry.claim_id for entry in resume.excluded)
    seen.update(entry.claim_id for entry in resume.subsumed)
    return [record for record in kb.by_status() if record.claim_id not in seen]


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Role:
    """A role claim with its children resolved into named slots."""

    claim: ClaimRecord
    title: str
    company: str
    location: str
    start: str
    end: str
    start_norm: str | None
    end_norm: str | None
    duties: tuple[ClaimRecord, ...]
    notes: tuple[ClaimRecord, ...]
    heading_claims: tuple[str, ...]
    start_claim_id: str | None = None
    end_claim_id: str | None = None
    location_claim_id: str | None = None
    employer_claims: tuple[ClaimRecord, ...] = ()

    def period(self) -> str:
        parts = [part for part in (self.start, self.end) if part]
        return " – ".join(parts)

    def period_claim_ids(self) -> tuple[str, ...]:
        return tuple(
            claim_id
            for claim_id in (self.start_claim_id, self.end_claim_id)
            if claim_id is not None
        )

    def meta_parts(self) -> list[tuple[str, tuple[str, ...]]]:
        """The ``location | period`` fragments, each with the claims it renders.

        Deliberately narrower than ``heading_claims``: the title and company
        claims are rendered by the heading, so crediting them here too would
        make the source map claim one fact twice. A fragment whose claim the KB
        does not hold (an empty location, an undated role) simply carries no ids.
        """
        parts: list[tuple[str, tuple[str, ...]]] = []
        if self.location:
            ids = (self.location_claim_id,) if self.location_claim_id else ()
            parts.append((self.location, ids))
        if self.period():
            parts.append((self.period(), self.period_claim_ids()))
        return parts

    def meta_claim_ids(self) -> tuple[str, ...]:
        return tuple(
            claim_id for _, ids in self.meta_parts() for claim_id in ids
        )


def _children(kb: CandidateKnowledgeBase, role_id: str) -> dict[str, list[ClaimRecord]]:
    """A role's claims, grouped by kind, with excluded claims already dropped.

    The filter matters as much here as anywhere else: a rejected duty is still
    bound to its role, so grouping without it would put a fact the candidate
    explicitly threw out straight back into the resume.
    """
    grouped: dict[str, list[ClaimRecord]] = {}
    for record in kb.by_status():
        if record.role_id != role_id:
            continue
        if _exclusion_reason(record) is not None:
            continue
        grouped.setdefault(record.kind, []).append(record)
    return grouped


def _first(grouped: dict[str, list[ClaimRecord]], kind: str) -> ClaimRecord | None:
    items = grouped.get(kind) or []
    return items[0] if items else None


def _role(kb: CandidateKnowledgeBase, claim: ClaimRecord) -> _Role:
    grouped = _children(kb, claim.claim_id)

    def value_of(kind: str) -> str:
        record = _first(grouped, kind)
        return record.value if record is not None else ""

    def norm_of(kind: str) -> str | None:
        record = _first(grouped, kind)
        return record.normalized if record is not None else None

    heading_claims = tuple(
        record.claim_id
        for kind in ("role_title", "role_company")
        for record in grouped.get(kind, [])[:1]
    )
    # The role claim itself and the per-employer splits are facts the resume
    # states. They are rendered *through* the title and company slots, but the
    # source map must still be able to point at them, so their ids travel with
    # the heading. Location and dates are deliberately NOT here: they belong to
    # the meta line and are credited there, so no claim is counted twice.
    heading_claims = (claim.claim_id,) + heading_claims
    employer_claims = tuple(grouped.get("role_employer", ()))
    heading_claims = heading_claims + tuple(record.claim_id for record in employer_claims)

    start_record = _first(grouped, "role_start")
    end_record = _first(grouped, "role_end")
    location_record = _first(grouped, "role_location")
    return _Role(
        claim=claim,
        title=value_of("role_title"),
        company=value_of("role_company"),
        location=value_of("role_location"),
        start=value_of("role_start"),
        end=value_of("role_end"),
        start_norm=norm_of("role_start"),
        end_norm=norm_of("role_end"),
        duties=tuple(grouped.get("duty", ())) + tuple(grouped.get("achievement", ())),
        notes=tuple(grouped.get("experience_note", ()))
        + tuple(grouped.get("chronology_note", ())),
        heading_claims=heading_claims,
        start_claim_id=start_record.claim_id if start_record is not None else None,
        end_claim_id=end_record.claim_id if end_record is not None else None,
        location_claim_id=location_record.claim_id if location_record is not None else None,
        employer_claims=employer_claims,
    )


def _apply_approved_edits(text: str) -> str:
    """Apply the declared substitutions, loudly if a span is no longer there.

    A span that has gone missing means the edit has stopped applying, and the
    output would read as if it had: the approved wording would be absent while
    nothing said so. Raising is the only honest response.
    """
    for span, replacement, reason in APPROVED_TEXT_EDITS:
        if span not in text:
            raise ValueError(
                f"the span {span!r} is not in {text!r}, so the approved edit for "
                f"{reason!r} has stopped applying. Fix APPROVED_TEXT_EDITS rather "
                f"than letting the text through unedited."
            )
        text = text.replace(span, replacement)
    return " ".join(text.split())


def _order_roles(roles: list[_Role]) -> list[_Role]:
    """Reverse-chronological, which is what a reader expects. Undated last.

    A role whose start date the resume never stated goes to the end rather than
    being given a position it has no evidence for.
    """
    dated = sorted(
        (role for role in roles if role.start_norm),
        key=lambda role: role.start_norm or "",
        reverse=True,
    )
    undated = [role for role in roles if not role.start_norm]
    return dated + undated


def build_master_resume(kb: CandidateKnowledgeBase) -> MasterResume:
    """Assemble the master resume from the knowledge base.

    The result carries its own ``excluded`` and ``subsumed`` tables, so the
    three-way partition is complete the moment the resume exists. A caller that
    forgot to fill them would render a source map whose accounting does not add
    up, which is exactly the kind of silent gap the partition is there to close.
    """
    resume = MasterResume()
    resume.excluded = exclusions(kb)
    resume.subsumed = subsumed(kb)

    def emit(
        text: str,
        kind: str,
        claim_ids: tuple[str, ...] = (),
        *,
        translate: bool = True,
        ru_text: str | None = None,
    ) -> None:
        resume.lines.append(Line(text, kind, claim_ids, translate, ru_text))
        resume.claim_ids.extend(claim_ids)

    def claim_id_of(kind: str, *, status: str | None = None) -> ClaimRecord | None:
        for record in kb.by_status(status):
            if record.kind == kind and _exclusion_reason(record) is None:
                return record
        return None

    # -- header ---------------------------------------------------------
    name = claim_id_of("name")
    if name is not None:
        emit(name.value, "h1", (name.claim_id,), translate=False)
    emit(" | ".join(POSITIONING_LABELS), "headline", translate=False)
    resume.notes.append(
        "headline assembled from the candidate's own role lists and the confirmed "
        "Bitrix24 preference; it covers four directions and is ordered "
        "alphabetically so that no direction is made the main one for every "
        "vacancy. A proposal, and it needs approval."
    )

    contact_bits: list[tuple[str, str]] = []
    for kind in ("contact_location", "work_mode", "contact_email", "contact_telegram"):
        record = claim_id_of(kind)
        if record is not None:
            contact_bits.append((record.value, record.claim_id))
    if contact_bits:
        emit(
            " · ".join(value for value, _ in contact_bits),
            "contact",
            tuple(claim_id for _, claim_id in contact_bits),
            translate=False,
        )

    # -- summary --------------------------------------------------------
    summary = claim_id_of("summary")
    if summary is not None:
        emit("## PROFESSIONAL SUMMARY", "h2")
        emit(_apply_approved_edits(summary.value), "paragraph", (summary.claim_id,))
        resume.notes.append(
            "summary: the quoted clause 'AI Automation Engineer with 15+ years of "
            "IT experience: ' is replaced by the approved opening, and 'in "
            "production' by the approved wording -- see APPROVED_TEXT_EDITS for "
            "both, and APPROVED_EDITORIAL_DECISIONS for who approved them. The "
            "candidate's own wording is untouched in the KB"
        )

    # -- core skills ----------------------------------------------------
    groups: list[tuple[ClaimRecord, list[ClaimRecord]]] = []
    for group in kb.by_status():
        if group.kind != "skill_group" or _exclusion_reason(group) is not None:
            continue
        members = [
            record
            for record in kb.by_status()
            if record.kind == "skill"
            and record.parent_id == group.claim_id
            and _exclusion_reason(record) is None
        ]
        if members:
            groups.append((group, members))
    if groups or DUTY_SOURCED_SKILL_GROUPS:
        emit("## CORE SKILLS", "h2")
        for group, members in groups:
            ids = (group.claim_id,) + tuple(record.claim_id for record in members)
            emit(
                f"- **{group.value}:** " + ", ".join(record.value for record in members),
                "bullet",
                ids,
                translate=False,
            )
        # The declared group goes last so that the change reads as an addition:
        # reordering the KB's own groups would silently rewrite lines the
        # candidate has already seen.
        for heading, entries in DUTY_SOURCED_SKILL_GROUPS:
            ids = tuple(dict.fromkeys(claim_id for _, claim_id, _ in entries))
            emit(
                f"- **{heading}:** " + ", ".join(label for label, _, _ in entries),
                "bullet",
                ids,
                translate=False,
            )

    # -- employment history ---------------------------------------------
    roles = [
        _role(kb, record)
        for record in kb.by_status()
        if record.kind == "role" and _exclusion_reason(record) is None
    ]
    independent = [role for role in roles if role.claim.claim_id in INDEPENDENT_ROLE_EVIDENCE]
    employment = [role for role in roles if role.claim.claim_id not in INDEPENDENT_ROLE_EVIDENCE]

    if employment:
        emit(f"## {EMPLOYMENT_HEADING}", "h2")
        for role in _order_roles(employment):
            _emit_role(emit, role, resume)

    # -- independent projects -------------------------------------------
    emit(f"## {INDEPENDENT_HEADING}", "h2")
    for role in independent:
        _emit_independent_role(emit, role, resume)

    project_records = _project_claims(kb)
    if project_records:
        for project_id, identity, members in project_records:
            _emit_project(emit, project_id, identity, members, resume)
        resume.notes.append(
            "project status labels (Implemented / Prototype) are the catalogue's "
            "reading of each project's state, not an independent assessment; where a "
            "repository states its own status an anchored line carries that sentence "
            "verbatim, and STATUS_WITHOUT_ANCHOR names the projects that do not. No "
            "project claims a client, a revenue figure or a metric"
        )
    else:
        resume.notes.append(
            "no project claims in the knowledge base: run `candidate-kb projects` "
            "to add them from the catalogue"
        )

    # -- education ------------------------------------------------------
    education: list[tuple[str, str]] = []
    for kind in ("education_institution", "education_program", "education_level", "education_year"):
        record = claim_id_of(kind)
        if record is not None:
            education.append((record.value, record.claim_id))
    if education:
        emit("## EDUCATION", "h2")
        institution = education[0][0]
        rest = " — " + ", ".join(value for value, _ in education[1:])
        emit(
            f"- **{institution}**{rest}",
            "bullet",
            tuple(claim_id for _, claim_id in education),
        )

    # -- languages ------------------------------------------------------
    languages: list[Line] = []
    for language in kb.by_status():
        if language.kind != "language" or _exclusion_reason(language) is not None:
            continue
        levels = [
            record
            for record in kb.by_status()
            if record.kind == "language_level" and record.parent_id == language.claim_id
        ]
        level = levels[0].value if levels else ""
        ids = (language.claim_id,) + tuple(record.claim_id for record in levels[:1])
        languages.append(
            Line(f"- {language.value} — {level}" if level else f"- {language.value}", "bullet", ids)
        )
    if languages:
        emit("## LANGUAGES", "h2")
        for line in languages:
            emit(line.text, line.kind, line.claim_ids, translate=line.translate)

    # -- contact --------------------------------------------------------
    emit("## CONTACT INFORMATION", "h2")
    for kind in ("contact_email", "contact_telegram", "contact_location", "work_mode"):
        record = claim_id_of(kind)
        if record is not None:
            emit(f"- {record.value}", "bullet", (record.claim_id,), translate=False)

    return resume


def _emit_role(emit, role: _Role, resume: MasterResume) -> None:
    heading = f"### {role.title}"
    if role.company:
        heading += f" | {role.company}"
    emit(heading, "h3", role.heading_claims, translate=False)
    meta = role.meta_parts()
    if meta:
        emit(
            "*" + " | ".join(text for text, _ in meta) + "*",
            "meta",
            role.meta_claim_ids(),
            translate=False,
        )
    for duty in role.duties:
        emit(f"- {duty.value}", "bullet", (duty.claim_id,))

    note_id = CHRONOLOGY_EVIDENCE.get(role.claim.claim_id)
    if note_id:
        start, end = CHRONOLOGY_PERIOD[role.claim.claim_id]
        employer = CHRONOLOGY_EMPLOYER[role.claim.claim_id]
        emit(
            f"*{employer}: {start} – {end} (period confirmed by the candidate).*",
            "note",
            (note_id,),
        )
        resume.notes.append(
            f"chronology note {note_id} transcribed into the period {start} – {end} "
            f"and attributed to {employer} alone, not to the block's other employers; "
            f"the block keeps its own dates from the resume and no other employer is "
            f"given a date the source does not state"
        )


def _emit_independent_role(emit, role: _Role, resume: MasterResume) -> None:
    """The candidate's own-projects period, stated as such and not as an employer.

    The role's own facts -- its title, location and dates -- are real and are
    rendered. The employer name is not, because "Freelance" would claim paid
    client work the candidate denied; the heading is rephrased instead. The
    confirmed interview note travels with the period line so that the "no paid
    client work" statement is traceable to the fact that established it rather
    than being an assertion this module makes on its own.
    """
    emit("### Own projects in this period", "h3", role.heading_claims)
    note_id = INDEPENDENT_ROLE_EVIDENCE.get(role.claim.claim_id)
    meta = role.meta_parts()
    if meta:
        emit(
            "*" + " | ".join(text for text, _ in meta) + "*",
            "meta",
            role.meta_claim_ids(),
            translate=False,
        )
    emit(INDEPENDENT_PROJECTS_NOTE, "meta", (note_id,) if note_id else ())
    resume.notes.append(
        f"reclassified from the resume's 'Freelance' heading (role "
        f"{role.claim.claim_id}); the employer name is not reproduced because the "
        f"candidate confirmed no paid client work (fact {note_id or '?'})"
    )
    for duty in role.duties:
        emit(f"- {duty.value}", "bullet", (duty.claim_id,))


def _project_claims(
    kb: CandidateKnowledgeBase,
) -> list[tuple[str, ClaimRecord, list[ClaimRecord]]]:
    """Project identities with their members, in KB order."""
    identities = [
        record
        for record in kb.by_status()
        if record.kind == "project" and _exclusion_reason(record) is None
    ]
    out: list[tuple[str, ClaimRecord, list[ClaimRecord]]] = []
    for identity in identities:
        members = [
            record
            for record in kb.by_status()
            if record.parent_id == identity.claim_id and _exclusion_reason(record) is None
        ]
        project_id = str(identity.evidence.get("project_id") or identity.normalized or "")
        out.append((project_id, identity, members))
    return out


_STATUS_LABELS = {
    "IMPLEMENTED": "Implemented",
    "PROTOTYPE": "Prototype",
    "PLAN": "Planned, not built",
}

#: English glosses for claims whose source is written in Russian. A translation
#: is not an invention -- the verbatim quote stays in the claim, and the source
#: map prints it -- but the English draft has to be readable in English. An
#: entry that maps a string to itself marks text that is already
#: language-neutral and should be carried through unchanged.
SOURCE_GLOSS: dict[str, str] = {
    "REPITON — AI-тренер произношения. Он учит говорить так, чтобы носитель языка правильно понял слово, а не просто подтверждает, что Speech-to-Text угадал текст.": (
        "REPITON — an AI pronunciation coach. It teaches you to speak so that a native "
        "speaker understands the word, rather than merely confirming that speech-to-text "
        "guessed the text."
    ),
    "Stage 1 остаётся **NO-GO**: текущая модель неустойчива между дикторами, а learner/error gold evidence отсутствует.": (
        "Stage 1 remains **NO-GO**: the current model is unstable across speakers, and "
        "learner/error gold evidence is missing."
    ),
    "**STAGE_1_FINAL_NO_GO.** По итогам однократного sealed-final эксперимента на 1 218 записях 6 независимых дикторов C-COST (macro-F1 = 0.702 < 0.80, low recall = 0.318 < 0.65)": (
        "**STAGE_1_FINAL_NO_GO.** In a single sealed-final experiment on 1,218 recordings "
        "from 6 independent C-COST speakers (macro-F1 = 0.702 < 0.80, low recall = 0.318 < 0.65)"
    ),
    "**TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO`": (
        "**TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO`"
    ),
    "`Listen → Repeat → Analyze → Explain → Retry → Master`": (
        "`Listen → Repeat → Analyze → Explain → Retry → Master`"
    ),
}

#: Order members so the project reads as prose: what it is, then what it does,
#: then its stack, then how far it got.
_MEMBER_ORDER = {
    "project_summary": 0,
    "project_bullet": 1,
    "project_stack": 2,
    "project_status": 3,
}


def _member_sort_key(record: ClaimRecord) -> tuple[int, int, str]:
    line = record.evidence.get("line")
    return (
        _MEMBER_ORDER.get(record.kind, 9),
        int(line) if isinstance(line, int) else 0,
        record.claim_id,
    )


def _strip_bullet(value: str) -> str:
    """A quoted markdown bullet already starts with '- '; do not double it."""
    return value[2:].strip() if value.startswith("- ") else value


def _emit_project(
    emit,
    project_id: str,
    identity: ClaimRecord,
    members: list[ClaimRecord],
    resume: MasterResume,
) -> None:
    status = _STATUS_LABELS.get(str(identity.evidence.get("project_status") or "").upper(), "")
    heading = f"### {identity.value}"
    if status:
        heading += f" — {status}"
    emit(heading, "h3", (identity.claim_id,), translate=False)

    for record in sorted(members, key=_member_sort_key):
        text = _strip_bullet(record.value)
        gloss = SOURCE_GLOSS.get(text)
        if record.kind == "project_summary":
            emit(
                gloss or text,
                "paragraph",
                (record.claim_id,),
                translate=gloss is None,
                ru_text=text if gloss else None,
            )
        elif record.kind == "project_stack":
            emit(f"**Stack:** {text}", "meta", (record.claim_id,), translate=False)
        elif record.kind == "project_status":
            emit(
                f"- {gloss or text}",
                "bullet",
                (record.claim_id,),
                translate=gloss is None,
                ru_text=f"- {text}" if gloss else None,
            )
        else:
            emit(
                f"- {gloss or text}",
                "bullet",
                (record.claim_id,),
                translate=gloss is None,
                ru_text=f"- {text}" if gloss else None,
            )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

RU_LABELS: dict[str, str] = {
    "EMPLOYMENT HISTORY": "ОПЫТ РАБОТЫ",
    "INDEPENDENT PROJECTS": "СОБСТВЕННЫЕ ПРОЕКТЫ",
    "PROFESSIONAL SUMMARY": "КРАТКОЕ ОПИСАНИЕ",
    "CORE SKILLS": "КЛЮЧЕВЫЕ НАВЫКИ",
    "EDUCATION": "ОБРАЗОВАНИЕ",
    "LANGUAGES": "ЯЗЫКИ",
    "CONTACT INFORMATION": "КОНТАКТЫ",
    "Own projects in this period": "Собственные проекты в этот период",
}

_RU_KIND_LABELS = {
    "Implemented": "Реализовано",
    "Prototype": "Прототип",
    "Planned, not built": "Запланировано, не сделано",
}

#: English -> Russian for every line marked ``translate``. Keyed by the English
#: text so a reworded source line fails the coverage test instead of silently
#: keeping a stale translation.
RU_TRANSLATIONS: dict[str, str] = {
    # -- professional summary
    "IT specialist with 15+ years in IT: technical support, systems administration, Bitrix24/CRM, automation, and my own projects built with AI tools. API integration, workflow automation, and LLM-powered systems used in my work and in my own projects. Builds end-to-end systems combining Python, LLMs (OpenAI, Gemini, Anthropic), REST/webhook integrations, Telegram bots, n8n/Make workflows, and Linux/Docker infrastructure. English B1, fully remote, ICT (+7) timezone.": (
        "IT-специалист с 15+ лет опыта в IT: техническая поддержка, "
        "администрирование систем, Bitrix24/CRM, автоматизация и собственные "
        "проекты, построенные с помощью AI-инструментов. Интеграция API, "
        "автоматизация процессов и LLM-системы, применяемые в работе и в "
        "собственных проектах. Строит сквозные системы на Python, LLM (OpenAI, "
        "Gemini, Anthropic), REST/webhook-интеграциях, Telegram-ботах, "
        "n8n/Make-воркфлоу и инфраструктуре Linux/Docker. Английский B1, "
        "полностью удалённо, часовой пояс ICT (+7)."
    ),
    # -- Sayama Travel
    "Administered Linux and Docker infrastructure (systemd services, logs, monitoring); configured VPN infrastructure and maintained backups and network security.": (
        "Администрировал инфраструктуру Linux и Docker (службы systemd, логи, мониторинг); "
        "настраивал VPN-инфраструктуру, поддерживал резервное копирование и сетевую безопасность."
    ),
    "Automated business processes around Bitrix24, including workflows, robots, triggers, telephony, email, analytics, and lead-generation channels.": (
        "Автоматизировал бизнес-процессы вокруг Bitrix24: воркфлоу, роботы, триггеры, "
        "телефонию, почту, аналитику и каналы лидогенерации."
    ),
    "Built a voice-message pipeline using Whisper for transcription, followed by AI summarization and storage in Bitrix24 deal records.": (
        "Построил пайплайн голосовых сообщений: транскрибация через Whisper, затем "
        "AI-суммаризация и сохранение в сделках Bitrix24."
    ),
    "Developed Telegram bots and asynchronous media-transfer pipelines using Python, aiogram, Telethon, and MTProto.": (
        "Разрабатывал Telegram-ботов и асинхронные пайплайны передачи медиа на Python, "
        "aiogram, Telethon и MTProto."
    ),
    "Developed n8n workflows connecting Bitrix24, Google Sheets, Telegram channels, and external databases.": (
        "Разрабатывал n8n-воркфлоу, связывающие Bitrix24, Google Sheets, Telegram-каналы "
        "и внешние базы данных."
    ),
    "Integrated OpenAI and Google Gemini into corporate chats and CRM workflows for incoming-request processing, lead classification, and multilingual response generation.": (
        "Интегрировал OpenAI и Google Gemini в корпоративные чаты и CRM-процессы: обработка "
        "входящих заявок, классификация лидов, генерация ответов на разных языках."
    ),
    # -- Avantis
    "Administered self-hosted Bitrix24; configured integrations, business processes, automation robots, and system workflows.": (
        "Администрировал self-hosted Bitrix24; настраивал интеграции, бизнес-процессы, "
        "роботов автоматизации и системные воркфлоу."
    ),
    "Provided technical support and troubleshooting for business users.": (
        "Оказывал техническую поддержку и диагностику для бизнес-пользователей."
    ),
    # -- earlier infrastructure block
    "Automated routine administrative tasks with Bash and PowerShell.": (
        "Автоматизировал рутинные административные задачи на Bash и PowerShell."
    ),
    "Worked across Windows Server and Linux infrastructure, Active Directory, Exchange, SQL, Hyper-V, networking, IP telephony, monitoring, API/web-service testing, and ITSM systems.": (
        "Работал с инфраструктурой Windows Server и Linux, Active Directory, Exchange, SQL, "
        "Hyper-V, сетями, IP-телефонией, мониторингом, тестированием API/веб-сервисов "
        "и ITSM-системами."
    ),
    "*Speedcast / Germsat: March 2018 – March 2021 (period confirmed by the candidate).*": (
        "*Speedcast / Germsat: март 2018 – март 2021 (период подтверждён кандидатом).*"
    ),
    # -- independent projects
    "No paid client work in this period (confirmed by the candidate).": (
        "В этот период оплачиваемых заказов не было (подтверждено кандидатом)."
    ),
    "Build asynchronous Python ETL pipelines with asyncio, Telethon/MTProto, and external APIs.": (
        "Строю асинхронные ETL-пайплайны на Python с asyncio, Telethon/MTProto и внешними API."
    ),
    "Build n8n workflows connecting databases, AI agents, Google Workspace APIs, CRM systems, notifications, and external services.": (
        "Строю n8n-воркфлоу, связывающие базы данных, AI-агентов, Google Workspace API, "
        "CRM-системы, уведомления и внешние сервисы."
    ),
    "Design and implement end-to-end AI automation pipelines for content processing and distribution (RAG, prompt engineering, AI agents).": (
        "Проектирую и реализую сквозные пайплайны AI-автоматизации для обработки "
        "и распространения контента (RAG, prompt engineering, AI-агенты)."
    ),
    "Develop Telegram bots with aiogram, external APIs, task queues, and FSM-based state management.": (
        "Разрабатываю Telegram-ботов на aiogram с внешними API, очередями задач "
        "и управлением состоянием на FSM."
    ),
    "Develop media-processing workflows integrating Google Drive, Whisper, LLMs, and Telegram.": (
        "Разрабатываю процессы обработки медиа с интеграцией Google Drive, Whisper, LLM и Telegram."
    ),
    # -- education and languages
    "- **Sakhalin State University** — Technological Institute, Technology and Entrepreneurship, Higher Education, 2005": (
        "- **Сахалинский государственный университет** — Технологический институт, "
        "технология и предпринимательство, высшее образование, 2005"
    ),
    "- English — B1 (Intermediate)": "- Английский — B1 (средний)",
    "- Russian — Native": "- Русский — родной",
    # -- projects whose sources are in English
    "Personal job-search automation system. Collects remote vacancies, scores them against a candidate profile, prepares tailored applications, and — behind explicit safety gates — can fill and submit HH.ru application forms, reply to HH messages, and prepare email replies.": (
        "Персональная система автоматизации поиска работы. Собирает удалённые вакансии, "
        "оценивает их по профилю кандидата, готовит адаптированные отклики и — за явными "
        "предохранителями — может заполнять и отправлять формы отклика на HH.ru, отвечать "
        "на сообщения HH и готовить ответы по email."
    ),
    "- Everything that touches the outside world (browser, HH, email) is gated: default mode is REVIEW (human decision required); AUTO modes are strictly opt-in via environment kill-switches and never guess facts.": (
        "- Всё, что взаимодействует с внешним миром (браузер, HH, email), закрыто "
        "предохранителями: режим по умолчанию — REVIEW (решение принимает человек); режимы "
        "AUTO включаются только явно через kill-switch в окружении и никогда не додумывают факты."
    ),
    "- Collects vacancies from remote and tech job boards (habrcareer, remoteok, weworkremotely, himalayas; legacy: linkedin, wellfound).": (
        "- Собирает вакансии с удалённых и технических джоб-бордов (habrcareer, remoteok, "
        "weworkremotely, himalayas; ранее: linkedin, wellfound)."
    ),
    "- Controlled application flow: plan → prefill → human review gate → single gated submit → read-only verification.": (
        "- Управляемый поток отклика: план → предзаполнение → ворота ручной проверки → "
        "одна отправка под предохранителем → проверка в режиме чтения."
    ),
    "A Telegram bot and asynchronous worker pipeline that analyzes short-form video (Instagram Reels, TikTok, YouTube Shorts): extracts transcripts and visual evidence, checks claims against sources, and produces structured, evidence-backed analysis results.": (
        "Telegram-бот и асинхронный воркер-пайплайн для анализа коротких видео (Instagram Reels, "
        "TikTok, YouTube Shorts): извлекает транскрипты и визуальные доказательства, проверяет "
        "утверждения по источникам и выдаёт структурированный результат с опорой на доказательства."
    ),
    "- Accepts video URLs via a Telegram bot (aiogram) and queues them for processing (Redis + ARQ async task queue, PostgreSQL job/task store).": (
        "- Принимает ссылки на видео через Telegram-бота (aiogram) и ставит их в очередь "
        "на обработку (асинхронная очередь задач Redis + ARQ, хранилище задач на PostgreSQL)."
    ),
    "- Fact-checks claims against web sources (Exa) with QA controls.": (
        "- Проверяет утверждения по веб-источникам (Exa) с контролем качества."
    ),
    "WHUBN is a local-first iOS app that reconstructs where a person's life happened from permitted location evidence such as photo metadata and location-history imports.": (
        "WHUBN — local-first iOS-приложение, которое восстанавливает, где происходила жизнь "
        "человека, по разрешённым данным о местоположении: метаданным фотографий и импорту "
        "истории перемещений."
    ),
    "- High-throughput SQLite persistence engine with WAL and Sendable Swift 6 concurrency": (
        "- Высоконагруженный движок хранения на SQLite с WAL и конкурентностью Swift 6 (Sendable)"
    ),
    "- Interactive 2D life map using Apple MapKit (`AtlasMapView`, `AtlasMKMapView`)": (
        "- Интерактивная 2D-карта жизни на Apple MapKit (`AtlasMapView`, `AtlasMKMapView`)"
    ),
    "- Deterministic chronological segmentation of `LocationEvent` records into canonical `Stay` records (`StaySegmentationEngine`)": (
        "- Детерминированная хронологическая сегментация записей `LocationEvent` "
        "в канонические `Stay` (`StaySegmentationEngine`)"
    ),
}


def translation_for(text: str) -> str | None:
    """The Russian text for a line, or ``None`` if the table has no entry.

    A leading ``- `` is presentation, not content, so a bullet may be keyed with
    or without its marker. Both spellings resolve; the marker is put back.
    """
    if text in RU_TRANSLATIONS:
        return RU_TRANSLATIONS[text]
    if text.startswith("- ") and text[2:] in RU_TRANSLATIONS:
        return "- " + RU_TRANSLATIONS[text[2:]]
    return None


def _ru_text(line: Line) -> str:
    """The Russian rendering of one line, or an error naming the missing piece.

    Section headings are translated structurally, project headings keep their
    proper noun and translate only the status label, and prose must be in
    :data:`RU_TRANSLATIONS`. A line that is missing there is an error rather
    than an English fallback: a half-translated review document reads as
    complete, which defeats the point of having one.
    """
    if line.ru_text is not None:
        return line.ru_text
    heading = _translate_headings_only(line.text)
    if heading != line.text:
        return heading
    if not line.translate:
        return line.text
    translated = translation_for(line.text)
    if translated is not None:
        return translated
    raise MissingTranslation(line.text)


def render_markdown(resume: MasterResume, *, lang: str = "en") -> str:
    """Render the resume. ``lang='ru'`` produces the review translation."""
    out: list[str] = []
    for line in resume.lines:
        text = _ru_text(line) if lang == "ru" else line.text
        # "meta" belongs here as well as in the trailing set: a role's meta line
        # is saved by the h3 above it, but a project's **Stack:** line follows a
        # bullet, and without the blank line markdown reads it as part of that
        # bullet. A trailing blank is not enough -- it does not help the line
        # that comes after a list item.
        if line.kind in {"h2", "h3", "paragraph", "meta", "note"} and out and out[-1].strip():
            out.append("")
        out.append(text)
        if line.kind in {"h2", "h3", "paragraph", "meta", "note"}:
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _translate_headings_only(text: str) -> str:
    for en, ru in RU_LABELS.items():
        if text == f"## {en}":
            return f"## {ru}"
        if text == f"### {en}":
            return f"### {ru}"
    if text.startswith("### ") and " — " in text:
        head, _, tail = text.partition(" — ")
        label = _RU_KIND_LABELS.get(tail)
        if label:
            return f"{head} — {label}"
    return text


def render_source_map(
    resume: MasterResume,
    kb: CandidateKnowledgeBase,
    *,
    project_evidence: list[dict[str, object]] | None = None,
) -> str:
    """Every rendered line's provenance, plus what was excluded and why."""
    by_id = {record.claim_id: record for record in kb.by_status()}
    total = len(kb.by_status())
    accounted = (
        len(resume.rendered_claim_ids()) + len(resume.excluded) + len(resume.subsumed)
    )
    out: list[str] = [
        "# Master resume — source map",
        "",
        f"Knowledge base version: {kb.kb_version}",
        f"Claims in the knowledge base: {total}",
        f"Claims rendered: {len(resume.rendered_claim_ids())}",
        f"Claims excluded: {len(resume.excluded)}",
        f"Claims subsumed: {len(resume.subsumed)}",
        f"Claims accounted for: {accounted} of {total}",
        "",
        (
            "Every line below names the claim it came from. Open the claim in "
            "`candidate_kb.json` and check `evidence` to see the file, the line and the quote."
        ),
        "",
        "## Brief map",
        "",
        "| where the content comes from | claims | what it is |",
        "| --- | --- | --- |",
    ]
    for marker, count in sorted(_source_counts(kb).items()):
        label, note = SOURCE_LABELS.get(
            marker, (f"`{marker}`", "origin marker with no label declared")
        )
        out.append(f"| {label} | {count} | {note} |")
    out += [
        (
            f"| **total** | **{total}** | {len(resume.rendered_claim_ids())} rendered, "
            f"{len(resume.excluded)} excluded, {len(resume.subsumed)} subsumed |"
        ),
        "",
        (
            "Each claim's `evidence` names its file, its line and its quote, so any "
            "line of the resume can be checked against the document it came from. "
            "The excluded and subsumed tables below say why a claim is not on a line."
        ),
    ]

    if DUTY_SOURCED_SKILL_GROUPS:
        out += [
            "",
            "## Core-skills groups sourced from duties",
            "",
            (
                "The knowledge base holds no `skill` claim for these, so the group "
                "is declared in the generator rather than invented as skills. Every "
                "entry names the duty it came from and the phrase that duty must "
                "literally contain, which is what makes the sourcing checkable."
            ),
            "",
            "| group | skill | duty claim | phrase in the duty |",
            "| --- | --- | --- | --- |",
        ]
        for heading, entries in DUTY_SOURCED_SKILL_GROUPS:
            for label, claim_id, phrase in entries:
                out.append(f"| {heading} | {label} | `{claim_id}` | {phrase} |")

    if APPROVED_EDITORIAL_DECISIONS:
        out += [
            "",
            "## Approved editorial decisions",
            "",
            (
                "Wording the candidate approved. These lines **are** published in "
                "the resume. The quoted text each one replaces is named in "
                "`APPROVED_TEXT_EDITS`; the decision is recorded here so published "
                "prose about the candidate is attributable rather than anonymous."
            ),
            "",
        ]
        for label, text, why in APPROVED_EDITORIAL_DECISIONS:
            out += [f"### {label}", "", f"> {text}", "", why, ""]

    if PROPOSALS:
        out += [
            "",
            "## Proposals awaiting approval",
            "",
            (
                "Nothing below is in the resume. The resume carries quotes and "
                "declared decisions; new prose about the candidate is the "
                "candidate's to approve first."
            ),
            "",
        ]
        for label, text, why in PROPOSALS:
            out += [f"### {label}", "", f"> {text}", "", why, ""]

    out += [
        "## Rendered claims",
        "",
        "| line | claim | status | kind | value |",
        "| --- | --- | --- | --- | --- |",
    ]
    for line in resume.lines:
        for claim_id in line.claim_ids:
            record = by_id.get(claim_id)
            if record is None:
                continue
            value = record.value.replace("|", "\\|")
            if len(value) > 110:
                value = value[:107] + "..."
            out.append(
                f"| {line.kind} | `{claim_id}` | {record.status} | {record.kind} | {value} |"
            )

    if resume.notes:
        out += ["", "## Declared presentation decisions", ""]
        out += [f"- {note}" for note in resume.notes]

    if resume.excluded:
        out += ["", "## Excluded claims", ""]
        out += ["| claim | kind | reason | value |", "| --- | --- | --- | --- |"]
        for item in resume.excluded:
            value = item.value.replace("|", "\\|")
            if len(value) > 90:
                value = value[:87] + "..."
            out.append(f"| `{item.claim_id}` | {item.kind} | {item.reason} | {value} |")

    if resume.subsumed:
        out += ["", "## Subsumed claims", ""]
        out += [
            (
                "Present in the resume but carried by a different line, so they are "
                "not quoted verbatim. Kept here as raw material for per-vacancy versions."
            ),
            "",
            "| claim | kind | reason | value |",
            "| --- | --- | --- | --- |",
        ]
        for item in resume.subsumed:
            value = item.value.replace("|", "\\|")
            if len(value) > 90:
                value = value[:87] + "..."
            out.append(f"| `{item.claim_id}` | {item.kind} | {item.reason} | {value} |")

    if project_evidence:
        out += ["", "## Project repositories", ""]
        out += [
            "| project | path | remote | first commit | last commit | commits |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in project_evidence:
            out.append(
                "| {project} | `{path}` | {remote} | {first} | {last} | {count} |".format(
                    project=item.get("project_id", "?"),
                    path=item.get("path", "?"),
                    remote=item.get("remote") or "—",
                    first=item.get("first_commit_date") or "—",
                    last=item.get("last_commit_date") or "—",
                    count=item.get("commit_count") if item.get("commit_count") is not None else "—",
                )
            )
    return "\n".join(out).rstrip() + "\n"


def project_evidence(root: str | None = None) -> list[dict[str, object]]:
    """Repository facts for the source map. Imported lazily to keep this module light."""
    from .projects import PROJECT_CATALOGUE, repository_facts

    facts = repository_facts(root)
    return [
        {
            "project_id": spec.project_id,
            "path": fact.path,
            "remote": fact.remote,
            "first_commit_date": fact.first_commit_date,
            "last_commit_date": fact.last_commit_date,
            "commit_count": fact.commit_count,
        }
        for spec, fact in zip(PROJECT_CATALOGUE, facts)
    ]


def load_kb(kb_path: str | None = None) -> tuple[CandidateKnowledgeBase, Path]:
    """Load the knowledge base, failing loudly rather than inventing an empty one."""
    from .kb import resolve_kb_path

    path = resolve_kb_path(kb_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"no knowledge base at {path}; the master resume is generated from the KB "
            f"and there is no fallback"
        )
    return CandidateKnowledgeBase.load(path), path


def _json_dump(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def candidate_master_build_cmd(
    kb_path: str | None = None,
    out_path: str | None = None,
    *,
    lang: str = "both",
    projects_root: str | None = None,
    as_json: bool = False,
) -> int:
    """``candidate-master build`` -- KB to ``artifacts/master_resume*.md``.

    Writes only the artifacts it names. The resume, the profile and the database
    are never opened.
    """
    import sys

    from .kb import PROJECT_ROOT

    try:
        knowledge_base, resolved_kb = load_kb(kb_path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    resume = build_master_resume(knowledge_base)

    out_dir = Path(out_path) if out_path else PROJECT_ROOT / "artifacts"
    if out_dir.suffix == ".md":
        out_dir = out_dir.parent

    # Render everything before writing anything. A missing translation in the
    # Russian variant must not leave a half-written artifact set behind, which
    # would look like a successful build with a stale Russian document in it.
    documents: list[tuple[Path, str]] = []
    wanted = ("en", "ru") if lang == "both" else (lang,)
    try:
        for code in wanted:
            suffix = "" if code == "en" else ".ru"
            documents.append(
                (out_dir / f"master_resume{suffix}.md", render_markdown(resume, lang=code))
            )
        documents.append(
            (
                out_dir / "master_resume_sources.md",
                render_source_map(
                    resume, knowledge_base, project_evidence=project_evidence(projects_root)
                ),
            )
        )
    except MissingTranslation as exc:
        print(
            f"ERROR: the Russian variant has no translation for {exc.args[0]!r}. "
            f"Add it to RU_TRANSLATIONS; the English draft is still valid, so "
            f"run with --lang en if that is what you wanted.",
            file=sys.stderr,
        )
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for target, body in documents:
        target.write_text(body, encoding="utf-8")
        written.append(str(target))

    if as_json:
        print(
            _json_dump(
                {
                    "kb_path": str(resolved_kb),
                    "kb_version": knowledge_base.kb_version,
                    "written": written,
                    "rendered_claims": len(resume.rendered_claim_ids()),
                    "excluded_claims": len(resume.excluded),
                    "subsumed_claims": len(resume.subsumed),
                    "unaccounted_claims": [
                        record.claim_id
                        for record in unaccounted_claims(knowledge_base, resume)
                    ],
                    "notes": resume.notes,
                }
            )
        )
        return 0

    missing = unaccounted_claims(knowledge_base, resume)
    print(f"knowledge base : {resolved_kb} (kb_version {knowledge_base.kb_version})")
    print(f"rendered       : {len(resume.rendered_claim_ids())} claims")
    print(f"excluded       : {len(resume.excluded)} claims")
    print(f"subsumed       : {len(resume.subsumed)} claims (carried by another line)")
    total = len(knowledge_base.by_status())
    print(f"accounted      : {total - len(missing)} of {total} claims")
    for path in written:
        print(f"written        : {path}")
    for note in resume.notes:
        print(f"note           : {note}")
    if missing:
        for record in missing:
            print(
                f"WARNING        : {record.claim_id} ({record.kind}) is neither "
                f"rendered, excluded nor subsumed: {record.value[:60]!r}",
                file=sys.stderr,
            )
        print(
            f"WARNING        : {len(missing)} claim(s) would leave the resume without "
            f"a trace; declare them in EXCLUDED_KINDS/EXCLUDED_CLAIMS or render them",
            file=sys.stderr,
        )
        return 1
    return 0
