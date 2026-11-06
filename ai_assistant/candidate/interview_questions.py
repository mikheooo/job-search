"""The interview plan: what to ask, in what order, and why (Stage 0 / T4.5).

WHAT THIS MODULE IS
-------------------
The plan is **derived, never hardcoded**. Every question carries a
:class:`Grounding` entry naming the T3 contradiction, the T4 finding or the
knowledge-base claim that motivated it. A question with no grounding cannot be
built, which is what makes "the questions are grounded in T3 and T4" a
structural property rather than a claim in a report.

That also means the plan moves when the audit moves. Fix the missing salary
figure and the salary question disappears on its own; add a contradiction and a
question appears for it. There is no second list of questions to keep in sync.

EXPERIENCE IS NOT PREFERENCE
----------------------------
Every question declares one :class:`FactClass`. ``EXPERIENCE`` facts are things
the candidate *did* and may appear in a resume; ``PREFERENCE``, ``GOAL`` and
``CONSTRAINT`` facts are things the candidate *wants* and may not. The Bitrix24
conflict is deliberately split into two neighbouring questions -- one for each
class -- because "I ran Bitrix24 for a living" and "I do not want another
Bitrix24 job" are different facts that the same sentence can blur together.

WHAT A QUESTION IS NOT
----------------------
A question is not a finding and not a defect. The employment gap and the missing
salary figure are the two places where a reviewer is most tempted to write
"problem" -- so those questions say the opposite in their own text: a gap is a
question the candidate already has the answer to, and "I would rather not state
a figure" is a complete answer rather than a failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .audit import line_of

if TYPE_CHECKING:
    from ..candidate_profile import CandidateProfile
    from .audit import Finding
    from .kb import ClaimRecord
    from .kb_verify import Contradiction

__all__ = [
    "ALLOWED_KINDS",
    "KIND_CLASS",
    "SECTION_ORDER",
    "SECTION_TITLE",
    "FactClass",
    "Grounding",
    "PlanContext",
    "Question",
    "Section",
    "build_plan",
    "fact_class_of",
    "format_question",
    "is_experience_kind",
    "questions_for_section",
]


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class Section(StrEnum):
    """The five parts of the interview, in the order they are asked."""

    GOALS = "goals_and_constraints"
    CHRONOLOGY = "career_chronology"
    PROJECTS = "projects_and_achievements"
    TECHNOLOGIES = "technologies_and_depth"
    CONFLICTS = "open_questions_from_audit"


SECTION_ORDER: tuple[Section, ...] = (
    Section.GOALS,
    Section.CHRONOLOGY,
    Section.PROJECTS,
    Section.TECHNOLOGIES,
    Section.CONFLICTS,
)

SECTION_TITLE: dict[Section, str] = {
    Section.GOALS: "Professional goals and constraints",
    Section.CHRONOLOGY: "Career chronology",
    Section.PROJECTS: "Projects and achievements",
    Section.TECHNOLOGIES: "Technologies and depth",
    Section.CONFLICTS: "Open questions from the audit (T3/T4)",
}


class FactClass(StrEnum):
    """What kind of thing a fact is, which decides where it may be used.

    This is the structural half of "distinguish experience from career
    preferences": a later stage cannot accidentally paste a preference into the
    experience section of a resume, because the class is on the record.
    """

    EXPERIENCE = "EXPERIENCE"
    PREFERENCE = "PREFERENCE"
    GOAL = "GOAL"
    CONSTRAINT = "CONSTRAINT"


#: The only kinds an interview fact may carry. Each maps to exactly one class,
#: so a fact's class is never a separate thing that can fall out of sync.
KIND_CLASS: dict[str, FactClass] = {
    "experience_tenure": FactClass.EXPERIENCE,
    "experience_note": FactClass.EXPERIENCE,
    "experience_skill_use": FactClass.EXPERIENCE,
    "experience_achievement": FactClass.EXPERIENCE,
    "chronology_note": FactClass.EXPERIENCE,
    "preference_role": FactClass.PREFERENCE,
    "preference_condition": FactClass.PREFERENCE,
    "goal_direction": FactClass.GOAL,
    "constraint_condition": FactClass.CONSTRAINT,
}

ALLOWED_KINDS: frozenset[str] = frozenset(KIND_CLASS)


def fact_class_of(kind: str) -> FactClass:
    """The class of an allowed kind. Raises ``ValueError`` for anything else."""
    try:
        return KIND_CLASS[kind]
    except KeyError:
        known = ", ".join(sorted(KIND_CLASS))
        raise ValueError(f"unknown fact kind {kind!r}; expected one of: {known}") from None


def is_experience_kind(kind: str) -> bool:
    """Whether a fact of this kind may appear in a resume."""
    return KIND_CLASS.get(kind) is FactClass.EXPERIENCE


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Grounding:
    """Why a question is being asked, traceable to a named source.

    ``source`` is one of ``t3`` (a cross-source contradiction), ``t4`` (an audit
    finding), ``kb`` (a claim in the knowledge base) or ``profile``.
    """

    source: str
    rule: str
    subject: str
    detail: str = ""

    def label(self) -> str:
        return f"{self.source}:{self.rule} -- {self.subject}"

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "rule": self.rule,
            "subject": self.subject,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Grounding:
        return cls(
            source=str(data.get("source") or ""),
            rule=str(data.get("rule") or ""),
            subject=str(data.get("subject") or ""),
            detail=str(data.get("detail") or ""),
        )


@dataclass(frozen=True)
class Question:
    """One interview step: a single question and the fact kind it produces."""

    question_id: str
    section: Section
    prompt: str
    why: str
    fact_kind: str
    scope: str
    grounding: tuple[Grounding, ...] = ()
    role_id: str | None = None
    parent_id: str | None = None

    def __post_init__(self) -> None:
        # Fail at construction rather than at extraction time: a question whose
        # kind is not allowed would silently produce facts nothing can store.
        fact_class_of(self.fact_kind)
        if not self.grounding:
            raise ValueError(f"question {self.question_id!r} has no grounding")

    @property
    def fact_class(self) -> FactClass:
        return fact_class_of(self.fact_kind)

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "section": str(self.section),
            "prompt": self.prompt,
            "why": self.why,
            "fact_kind": self.fact_kind,
            "fact_class": str(self.fact_class),
            "scope": self.scope,
            "role_id": self.role_id,
            "parent_id": self.parent_id,
            "grounding": [item.to_dict() for item in self.grounding],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Question:
        grounding = data.get("grounding") or []
        return cls(
            question_id=str(data["question_id"]),
            section=Section(str(data["section"])),
            prompt=str(data.get("prompt") or ""),
            why=str(data.get("why") or ""),
            fact_kind=str(data.get("fact_kind") or ""),
            scope=str(data.get("scope") or ""),
            grounding=tuple(
                Grounding.from_dict(item) for item in grounding  # type: ignore[union-attr]
            ),
            role_id=_opt_str(data.get("role_id")),
            parent_id=_opt_str(data.get("parent_id")),
        )


@dataclass(frozen=True)
class PlanContext:
    """Everything the plan is allowed to look at.

    ``contradictions`` and ``findings`` are T3's and T4's own objects, passed
    through rather than re-derived -- the same rule the tech reviewer follows, so
    the interview cannot be grounded in a different audit than the one the
    candidate was shown.
    """

    contradictions: tuple[Contradiction, ...]
    findings: tuple[Finding, ...]
    claims: tuple[ClaimRecord, ...]
    profile: CandidateProfile
    profile_label: str
    resume_label: str

    # -- queries -----------------------------------------------------------

    def by_t3_rule(self, rule: str) -> tuple[Contradiction, ...]:
        return tuple(item for item in self.contradictions if item.rule == rule)

    def by_t4_rule(self, rule: str) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.rule == rule)

    def claims_of(self, *kinds: str) -> tuple[ClaimRecord, ...]:
        wanted = set(kinds)
        found = [record for record in self.claims if record.kind in wanted]
        return tuple(sorted(found, key=lambda record: (line_of(record), record.value)))

    def profile_value(self, field: str) -> object | None:
        return getattr(self.profile, field, None)

    def profile_list(self, field: str) -> tuple[str, ...]:
        value = self.profile_value(field)
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        try:
            return tuple(str(item) for item in value)  # type: ignore[union-attr]
        except TypeError:
            return (str(value),)


# --------------------------------------------------------------------------
# Building the plan
# --------------------------------------------------------------------------


def build_plan(context: PlanContext) -> tuple[Question, ...]:
    """The whole interview, section by section, in a stable order.

    Sections are emitted in :data:`SECTION_ORDER`. Within a section the order is
    the order the generators append, and each generator iterates its inputs in
    document order -- so the same sources always produce the same plan, and a
    resumed session cannot land on a different question than it left off on.
    """
    questions: list[Question] = []
    for section in SECTION_ORDER:
        questions.extend(questions_for_section(context, section))
    return tuple(questions)


def questions_for_section(context: PlanContext, section: Section) -> tuple[Question, ...]:
    """One section's questions. An unknown section is a programming error."""
    generator = _GENERATORS.get(section)
    if generator is None:
        known = ", ".join(item.value for item in SECTION_ORDER)
        raise ValueError(f"unknown section {section!r}; expected one of: {known}")
    return tuple(generator(context))


# --------------------------------------------------------------------------
# Section: goals and constraints
# --------------------------------------------------------------------------


def _goals(context: PlanContext) -> list[Question]:
    target = context.profile_list("target_roles")
    avoid = context.profile_list("must_avoid_conditions")
    questions = [
        Question(
            question_id="q.goals.direction",
            section=Section.GOALS,
            prompt=(
                "In your own words: what kind of work do you want to be doing a year from now?"
            ),
            why=(
                "Your profile lists target roles ("
                + ", ".join(target[:4])
                + ("..." if len(target) > 4 else "")
                + "), but that list was written by an earlier step rather than by you. "
                "Your answer here becomes the yardstick every vacancy is measured against."
            ),
            fact_kind="goal_direction",
            scope="goals.direction",
            grounding=(
                Grounding(
                    source="profile",
                    rule="target_roles",
                    subject=", ".join(target) or "(no target roles recorded)",
                    detail="the roles the pipeline currently aims at",
                ),
            ),
        ),
        Question(
            question_id="q.goals.constraints",
            section=Section.GOALS,
            prompt=(
                "What must be true about a job for you to take it, and what must never be true?"
            ),
            why=(
                "Your profile carries hard lists (must_avoid_conditions: "
                + (", ".join(avoid[:6]) or "(empty)")
                + "). Saying them in your own words keeps the filters honest -- "
                "a filter that drops a job you would have taken costs you more than a bad application."
            ),
            fact_kind="constraint_condition",
            scope="goals.constraints",
            grounding=(
                Grounding(
                    source="profile",
                    rule="must_avoid_conditions",
                    subject=", ".join(avoid) or "(no conditions recorded)",
                    detail="the conditions the pipeline currently treats as disqualifying",
                ),
            ),
        ),
    ]
    questions.extend(_salary(context))
    questions.extend(_contact(context))
    return questions


def _salary(context: PlanContext) -> list[Question]:
    findings = context.by_t4_rule("hr_no_salary_expectation")
    if not findings:
        return []
    grounding = tuple(
        Grounding(
            source="t4",
            rule=finding.rule,
            subject=finding.subject,
            detail=finding.detail,
        )
        for finding in findings
    )
    return [
        Question(
            question_id="q.goals.salary",
            section=Section.GOALS,
            prompt=(
                "Should your resume state a salary expectation? If yes, what figure and currency?"
            ),
            why=(
                "Your resume states no figure, so a screener filtering on budget has nothing "
                "to filter on. **Saying no is a complete answer**: if you would rather not state "
                "a figure, that is a decision about your own document, not a defect, and it will "
                "be recorded as one."
            ),
            fact_kind="preference_condition",
            scope="goals.salary",
            grounding=grounding,
        )
    ]


def _contact(context: PlanContext) -> list[Question]:
    findings = context.by_t4_rule("ats_contact_channel_gap")
    if not findings:
        return []
    channels = sorted({finding.subject for finding in findings})
    grounding = tuple(
        Grounding(
            source="t4",
            rule=finding.rule,
            subject=finding.subject,
            detail=finding.detail,
        )
        for finding in findings
    )
    return [
        Question(
            question_id="q.goals.contact",
            section=Section.GOALS,
            prompt=(
                "Which contact channels should your resume carry? "
                "Your profile declares these and your resume does not repeat them: "
                + "; ".join(channels)
                + "."
            ),
            why=(
                "An ATS reading only the resume loses a channel the profile knows about. "
                "If a channel is deliberately private, say so and it stays out -- that is a "
                "preference, and the resume is the file that should change."
            ),
            fact_kind="preference_condition",
            scope="goals.contact",
            grounding=grounding,
        )
    ]


# --------------------------------------------------------------------------
# Section: career chronology
# --------------------------------------------------------------------------


def _chronology(context: PlanContext) -> list[Question]:
    questions: list[Question] = []
    roles = context.claims_of("role")
    for role in roles:
        duties = [
            record
            for record in context.claims_of("duty", "achievement")
            if record.role_id == role.claim_id
        ]
        questions.append(
            Question(
                question_id=f"q.chronology.role.{role.claim_id}",
                section=Section.CHRONOLOGY,
                prompt=(
                    f"Role: {role.value}. In your own words, what did you actually do here "
                    "day to day, and what was the most concrete thing you delivered?"
                ),
                why=(
                    f"Your resume lists {len(duties)} bullet(s) for this role. Recording the "
                    "same work in your own words lets the master resume quote you instead of "
                    "repeating the bullet list -- and it is the only way to find the result "
                    "the bullets leave out."
                ),
                fact_kind="experience_note",
                scope=role.claim_id,
                role_id=role.claim_id,
                grounding=(
                    Grounding(
                        source="kb",
                        rule="role",
                        subject=role.value,
                        detail=f"resume line {line_of(role)}",
                    ),
                ),
            )
        )
    questions.extend(_gap(context))
    return questions


_GAP_SUBJECT_RE = re.compile(r"^(\d+) months unexplained between (.+?) and (.+)$")


def _gap(context: PlanContext) -> list[Question]:
    findings = context.by_t4_rule("hr_employment_gap")
    if not findings:
        return []
    questions: list[Question] = []
    for index, finding in enumerate(findings, start=1):
        match = _GAP_SUBJECT_RE.match(finding.subject)
        if match:
            months, after_role, before_role = match.groups()
            prompt = (
                f"Your resume has a {months}-month gap between '{after_role}' and "
                f"'{before_role}'. What were you doing during that time?"
            )
        else:
            prompt = f"About this period in your resume: {finding.subject}. What were you doing?"
        questions.append(
            Question(
                question_id=f"q.chronology.gap.{index}",
                section=Section.CHRONOLOGY,
                prompt=prompt,
                why=(
                    "**A gap is a question, not a mistake.** You already have the answer; a "
                    "screener will ask it anyway, so the point of recording it here is that the "
                    "answer exists before they ask. 'I was not working' is also an answer."
                ),
                fact_kind="chronology_note",
                scope=f"chronology.gap.{index}",
                grounding=(
                    Grounding(
                        source="t4",
                        rule=finding.rule,
                        subject=finding.subject,
                        detail=finding.detail,
                    ),
                ),
            )
        )
    return questions


# --------------------------------------------------------------------------
# Section: projects and achievements
# --------------------------------------------------------------------------


def _projects(context: PlanContext) -> list[Question]:
    questions: list[Question] = []
    quantified = context.by_t4_rule("tech_no_quantified_achievement")
    if quantified:
        questions.append(
            Question(
                question_id="q.projects.quantified",
                section=Section.PROJECTS,
                prompt=(
                    "Pick one thing you built or fixed and tell me what changed because of it. "
                    "A number if you have one -- hours saved, volume handled, error rate, "
                    "number of people it replaced."
                ),
                why=(
                    "Every bullet on your resume describes a duty; none states a result. This is "
                    "the single highest-value thing a resume can add, and it has to come from you: "
                    "an invented number would be worse than no number at all."
                ),
                fact_kind="experience_achievement",
                scope="projects.quantified",
                grounding=tuple(
                    Grounding(
                        source="t4",
                        rule=finding.rule,
                        subject=finding.subject,
                        detail=finding.detail,
                    )
                    for finding in quantified
                ),
            )
        )

    duties = context.claims_of("duty", "achievement")
    roles = context.claims_of("role")
    if duties:
        questions.append(
            Question(
                question_id="q.projects.biggest",
                section=Section.PROJECTS,
                prompt=(
                    "Which of these is the project you would most want to be asked about in an "
                    "interview, and why that one?"
                ),
                why=(
                    f"Your resume lists {len(duties)} duty/duties across {len(roles)} role(s). "
                    "Naming the one you want to talk about tells the pipeline what to lead with, "
                    "and the 'why' is usually a stronger sentence than any bullet."
                ),
                fact_kind="experience_note",
                scope="projects.biggest",
                grounding=(
                    Grounding(
                        source="kb",
                        rule="duty",
                        subject=f"{len(duties)} duty/achievement claim(s)",
                        detail="the resume's experience bullets",
                    ),
                ),
            )
        )
    return questions


# --------------------------------------------------------------------------
# Section: technologies and depth
# --------------------------------------------------------------------------


def _technologies(context: PlanContext) -> list[Question]:
    questions: list[Question] = []
    unsupported = context.by_t4_rule("tech_skill_without_project_context")
    if unsupported:
        # The finding subject reads "'Anthropic API' is claimed but no role
        # demonstrates it"; only the quoted skill is wanted in the prompt.
        skills = sorted(
            {
                match.group(1)
                for finding in unsupported
                if (match := re.search(r"'([^']+)' is claimed", finding.subject))
            }
        )
        listed = skills or [finding.subject for finding in unsupported]
        questions.append(
            Question(
                question_id="q.tech.context",
                section=Section.TECHNOLOGIES,
                prompt=(
                    "These are on your resume but no role shows them: "
                    + ", ".join(listed)
                    + ". For each one, which role or project used it? "
                    "If you would rather drop one, say so."
                ),
                why=(
                    "A skill with no role behind it reads as a keyword, and an interviewer asks "
                    "about it first. Naming the role turns it into experience. Note the summary "
                    "is deliberately not counted as evidence -- a mention there says the word was "
                    "written, not that the work was done."
                ),
                fact_kind="experience_skill_use",
                scope="tech.context",
                grounding=tuple(
                    Grounding(
                        source="t4",
                        rule=finding.rule,
                        subject=finding.subject,
                        detail=finding.detail,
                    )
                    for finding in unsupported
                ),
            )
        )

    skills = context.claims_of("skill")
    if skills:
        questions.append(
            Question(
                question_id="q.tech.depth",
                section=Section.TECHNOLOGIES,
                prompt=(
                    "Which parts of your stack are you genuinely deep in, and which are working "
                    "knowledge? Be blunt -- 'I have used it twice' is a useful answer."
                ),
                why=(
                    f"Your resume lists {len(skills)} skill(s) with no depth attached. Depth is "
                    "what a tech lead probes, and 'working knowledge' is a complete answer rather "
                    "than a failure. It is also what stops an interview question you cannot answer."
                ),
                fact_kind="experience_skill_use",
                scope="tech.depth",
                grounding=(
                    Grounding(
                        source="kb",
                        rule="skill",
                        subject=f"{len(skills)} skill claim(s)",
                        detail="the resume's CORE SKILLS section",
                    ),
                ),
            )
        )
    return questions


# --------------------------------------------------------------------------
# Section: open questions from the audit
# --------------------------------------------------------------------------


def _conflicts(context: PlanContext) -> list[Question]:
    """The three T3 ambiguities, plus the headline question T4 raised.

    This is the section the brief singles out: the three items below are the ones
    no rule can settle, because settling them means choosing a meaning for the
    candidate's own words.
    """
    questions: list[Question] = []

    tenure_total = context.by_t3_rule("total_experience_vs_it_support")
    if tenure_total:
        resume_value, profile_value = _two_sides(tenure_total[0])
        questions.append(
            Question(
                question_id="q.conflict.tenure_total",
                section=Section.CONFLICTS,
                prompt=(
                    f"Your resume says '{resume_value}'. Your profile records "
                    f"{profile_value} years for IT support. How many years have you actually "
                    "worked in IT, and what does that number include?"
                ),
                why=(
                    "Both numbers are yours and they disagree, so a screener who reads both "
                    "files finds the conflict. Note the two are not the same measure -- one is a "
                    "prose total, the other a domain figure -- which is exactly why this needs "
                    "your answer rather than a rule."
                ),
                fact_kind="experience_tenure",
                scope="conflict.tenure_total",
                grounding=_ground_t3(tenure_total),
            )
        )

    tenure_meaning = context.by_t3_rule("years_experience_vs_domain_years")
    if tenure_meaning:
        stated = _profile_value_of(tenure_meaning[0], "years_experience")
        domains = ", ".join(_domain_side(item) for item in tenure_meaning)
        questions.append(
            Question(
                question_id="q.conflict.tenure_meaning",
                section=Section.CONFLICTS,
                prompt=(
                    f"Your profile has a field 'years_experience = {stated}' next to domain "
                    f"figures of {domains}. What is the {stated} meant to represent?"
                ),
                why=(
                    "A domain cannot be longer than the total it belongs to, so one of the two "
                    "meanings is wrong. Naming what the figure measures settles "
                    f"{len(tenure_meaning)} finding(s) at once -- and if it is simply stale, "
                    "that is a useful answer too."
                ),
                fact_kind="experience_tenure",
                scope="conflict.tenure_meaning",
                grounding=_ground_t3(tenure_meaning),
            )
        )

    bitrix = context.by_t3_rule("bitrix_experience_vs_excluded")
    if bitrix:
        grounding = _ground_t3(bitrix)
        questions.append(
            Question(
                question_id="q.conflict.bitrix_experience",
                section=Section.CONFLICTS,
                prompt=(
                    "What did you actually do with Bitrix24 -- at Avantis and at Sayama Travel? "
                    "Concretely: what did you build, fix or run?"
                ),
                why=(
                    "This is the **experience** side of a conflict. Your resume leads with "
                    "Bitrix24 work while your profile lists 'bitrix' among the roles to avoid, so "
                    "anything filtering on that list discards the experience your resume opens "
                    "with. Recording what you did is a separate question from deciding whether to "
                    "keep presenting it."
                ),
                fact_kind="experience_note",
                scope="conflict.bitrix_experience",
                grounding=grounding,
            )
        )
        questions.append(
            Question(
                question_id="q.conflict.bitrix_preference",
                section=Section.CONFLICTS,
                prompt=(
                    "Should Bitrix24 work stay on your resume as experience, or be played down? "
                    "And do you want to keep avoiding Bitrix24 vacancies?"
                ),
                why=(
                    "This is the **preference** side of the same conflict. Your experience and "
                    "your preferences are different facts and the pipeline treats them "
                    "differently: experience may go into a resume, a preference may not. Keeping "
                    "them apart is what stops 'I did Bitrix24' from being read as 'I want "
                    "Bitrix24'."
                ),
                fact_kind="preference_role",
                scope="conflict.bitrix_preference",
                grounding=grounding,
            )
        )

    drift = context.by_t3_rule("baseline_vs_profile_drift")
    if drift:
        fields = ", ".join(sorted({_profile_field_of(item) for item in drift}))
        questions.append(
            Question(
                question_id="q.conflict.target_roles",
                section=Section.CONFLICTS,
                prompt=(
                    f"Your profile file and the code's built-in fallback give different values "
                    f"for: {fields}. Which list is current?"
                ),
                why=(
                    "If the profile file ever goes missing, the pipeline silently falls back to "
                    "the built-in version and the candidate changes underneath it. Confirming one "
                    "list removes that risk; nothing here needs your memory, only your decision."
                ),
                fact_kind="goal_direction",
                scope="conflict.target_roles",
                grounding=_ground_t3(drift),
            )
        )

    headline = context.by_t4_rule("ats_headline_uses_separators")
    for index, finding in enumerate(headline, start=1):
        headline_value = _first_evidence_value(finding)
        questions.append(
            Question(
                question_id=f"q.conflict.headline.{index}",
                section=Section.CONFLICTS,
                prompt=(
                    f"Your headline reads '{headline_value}'. Which one title should an ATS see "
                    "first, and may the others go into the summary instead?"
                ),
                why=(
                    "Parsers commonly keep the whole line as one string or take the first title "
                    "and drop the rest -- so which title gets matched against a vacancy is "
                    "currently the parser's decision rather than yours. This is a choice about "
                    "your own document, not a correction."
                ),
                fact_kind="goal_direction",
                scope=f"conflict.headline.{index}",
                grounding=(
                    Grounding(
                        source="t4",
                        rule=finding.rule,
                        subject=finding.subject,
                        detail=finding.detail,
                    ),
                ),
            )
        )
    return questions


def _ground_t3(items: tuple[Contradiction, ...]) -> tuple[Grounding, ...]:
    return tuple(
        Grounding(
            source="t3",
            rule=item.rule,
            subject=item.subject,
            detail=item.detail,
        )
        for item in items
    )


def _two_sides(contradiction: Contradiction) -> tuple[str, str]:
    """``(resume side, profile side)`` of a contradiction, as shown values."""
    resume_value = ""
    profile_value = ""
    for ref in contradiction.sources:
        if ref.source == "resume" and not resume_value:
            resume_value = ref.value
        elif ref.source == "profile" and not profile_value:
            profile_value = ref.value
    return resume_value, profile_value


def _profile_value_of(contradiction: Contradiction, field: str) -> str:
    for ref in contradiction.sources:
        if ref.source == "profile" and ref.location.endswith(f":{field}"):
            return ref.value
    return "?"


def _domain_side(contradiction: Contradiction) -> str:
    """``domain_years.<name>=<value>`` -- the domain figure of a tenure conflict.

    The contradiction carries two profile sides (``years_experience`` and the
    domain), so the domain is whichever one is not ``years_experience``.
    """
    for ref in contradiction.sources:
        if ref.source == "profile" and not ref.location.endswith(":years_experience"):
            return f"{ref.location.rsplit(':', 1)[-1]}={ref.value}"
    return "?"


def _profile_field_of(contradiction: Contradiction) -> str:
    """The profile field a contradiction is about, last path segment only."""
    for ref in contradiction.sources:
        if ref.source == "profile":
            return ref.location.rsplit(":", 1)[-1]
    return "?"


def _first_evidence_value(finding: Finding) -> str:
    for ref in finding.evidence:
        return ref.value
    return finding.subject


_GENERATORS = {
    Section.GOALS: _goals,
    Section.CHRONOLOGY: _chronology,
    Section.PROJECTS: _projects,
    Section.TECHNOLOGIES: _technologies,
    Section.CONFLICTS: _conflicts,
}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def format_question(
    question: Question,
    *,
    position: int | None = None,
    total: int | None = None,
    explain: bool = False,
) -> str:
    """One question, plain language, with an optional explanation block.

    ``explain=True`` is the clarification view: it adds the reasoning and the
    grounding so the candidate can see why they are being asked this.
    """
    counter = f"[{position}/{total}] " if position and total else ""
    lines = [
        f"{counter}{SECTION_TITLE[question.section]}",
        "",
        question.prompt,
    ]
    if explain:
        lines.append("")
        lines.append(f"why this is being asked: {question.why}")
        lines.append(
            f"an answer here is recorded as: {question.fact_kind} ({question.fact_class})"
        )
        if question.role_id:
            lines.append(f"attached to role        : {question.role_id}")
        lines.append("grounded in:")
        for item in question.grounding:
            lines.append(f"  - {item.label()}")
    return "\n".join(lines)
