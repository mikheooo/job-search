"""Tech-lead reviewer: is the technology claimed actually demonstrated? (T4).

A tech lead reads a resume looking for the same thing in every line: *where did
you do this, and what did it do?* This reviewer asks that question mechanically.

* **A technology named in the skills section but in no role** is a claim with no
  project behind it. The skills section is a list of words; the experience
  section is the evidence. A skill that appears only in the list is exactly the
  gap an interviewer probes in the first five minutes.
* **No quantified achievement at all.** If nothing in the document states an
  outcome, then no reader can tell whether the work succeeded.
* **A role with no bullets** is a heading with nothing under it.

THE MANDATORY PART
------------------
The Stage 0 plan requires the tech lead to flag the contradictions T3 found, and
this module does it by consuming :func:`kb_verify.verify_all` **rather than
re-running the rules**. Re-deriving them here could produce a different list than
the one ``candidate-kb verify`` showed the candidate, and two different answers to
the same question is worse than one answer with a known limitation.

Every such finding carries ``status="CONTRADICTED"``. Note what that is and is
not: it marks the finding, it does not write a status into the knowledge base.
T3 deliberately kept ``CONTRADICTED`` out of storage because resolving a
contradiction is a decision, and decisions in this project are explicit
transitions with a version trail. This reviewer observes; it does not decide.
"""

from __future__ import annotations

import re

from .audit import (
    AuditContext,
    Finding,
    Reviewer,
    Rule,
    Severity,
    claims_of_kind,
    resume_ref,
    run_rule_set,
)

__all__ = [
    "CONTRADICTED_STATUS",
    "PROJECT_CONTEXT_KINDS",
    "TECH_REVIEWER",
    "TECH_RULES",
]

# Where a technology has to appear to count as demonstrated. `summary` and
# `experience_statement` are deliberately absent: they mention many technologies
# at once, so a hit there says "the candidate wrote this word", not "the
# candidate did this on a project".
PROJECT_CONTEXT_KINDS = ("role", "role_title", "role_company", "duty", "achievement")

# The marker this reviewer puts on findings inherited from T3.
CONTRADICTED_STATUS = "CONTRADICTED"

# A head word this short is too generic to match on ("ai", "ms"), so those
# phrases have to match whole.
_MIN_HEAD_WORD = 3


def _project_context(context: AuditContext) -> str:
    """Every role and bullet, joined and lower-cased: the evidence section."""
    records = claims_of_kind(context, *PROJECT_CONTEXT_KINDS)
    return "\n".join(record.value.lower() for record in records)


def _demonstrated(blob: str, skill: str) -> bool:
    """Whether the evidence section names this skill.

    Two chances, both literal: the phrase as written, or its head word. The head
    word exists because a skills list writes "OpenAI API" where a bullet writes
    "OpenAI", and calling that a missing project would be a false positive. The
    match is on a word boundary at the start only, so "workflow" finds
    "workflows".
    """
    text = skill.strip().lower()
    if not text:
        return True
    if re.search(rf"\b{re.escape(text)}\b", blob) is not None:
        return True
    head = text.split()[0]
    if len(head) < _MIN_HEAD_WORD:
        return False
    return re.search(rf"\b{re.escape(head)}(?:s|es|ing|ed)?\b", blob) is not None


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def _check_contradicted_claim(context: AuditContext) -> list[Finding]:
    """Every contradiction T3 found, marked ``CONTRADICTED``.

    Required by the plan. The contradictions come from T3's own result object, so
    the tech lead and ``candidate-kb verify`` cannot disagree about what was found.
    """
    findings: list[Finding] = []
    for contradiction in context.verification.contradictions:
        findings.append(
            Finding(
                rule="tech_contradicted_claim",
                severity=Severity.HIGH,
                subject=contradiction.subject,
                detail=(
                    f"cross-source verification ({contradiction.rule}) found the sources in "
                    f"conflict here. {contradiction.detail} The tech lead's position is that "
                    f"this must be answered before the fact is used in an application: a "
                    f"reader who checks both sources finds the disagreement, and that costs "
                    f"more than the missing fact would have."
                ),
                evidence=contradiction.sources,
                status=CONTRADICTED_STATUS,
            )
        )
    return findings


def _check_skill_without_project_context(context: AuditContext) -> list[Finding]:
    """A skill that appears in the skills list and in no role."""
    blob = _project_context(context)
    if not blob.strip():
        # No evidence section at all is a different problem, reported by
        # tech_role_without_bullets. Listing every skill as unsupported would
        # restate one finding as thirty.
        return []

    findings: list[Finding] = []
    for record in claims_of_kind(context, "skill"):
        if _demonstrated(blob, record.value):
            continue
        findings.append(
            Finding(
                rule="tech_skill_without_project_context",
                severity=Severity.MEDIUM,
                subject=f"{record.value!r} is claimed but no role demonstrates it",
                detail=(
                    f"the skills section names {record.value!r}, and no role, duty or bullet "
                    f"mentions it. The summary is deliberately not counted as evidence: it "
                    f"lists technologies in prose, so a mention there says the word was "
                    f"written, not that the work was done -- and it is the summary that a "
                    f"reader already believes. That is not the same as the skill being "
                    f"untrue; the resume simply does not show it. An interviewer asks about it "
                    f"first, and an ATS matching on experience rather than keywords does not "
                    f"see it at all."
                ),
                evidence=(resume_ref(record),),
            )
        )
    return findings


def _check_no_quantified_achievement(context: AuditContext) -> list[Finding]:
    """Nothing in the document states an outcome."""
    if claims_of_kind(context, "achievement"):
        return []
    return [
        Finding(
            rule="tech_no_quantified_achievement",
            severity=Severity.LOW,
            subject="no bullet states a quantified outcome",
            detail=(
                "every experience bullet describes a duty; none states a result. A reader "
                "cannot tell from this document whether any of the work succeeded. The parser "
                "marks an achievement only when the source marks one, so this finding is about "
                "the resume, not about the extraction."
            ),
            evidence=(),
        )
    ]


def _check_role_without_bullets(context: AuditContext) -> list[Finding]:
    """A role heading with nothing under it."""
    bullet_counts: dict[str, int] = {}
    for record in claims_of_kind(context, "duty", "achievement"):
        if record.role_id:
            bullet_counts[record.role_id] = bullet_counts.get(record.role_id, 0) + 1

    findings: list[Finding] = []
    for role in claims_of_kind(context, "role"):
        if bullet_counts.get(role.claim_id):
            continue
        findings.append(
            Finding(
                rule="tech_role_without_bullets",
                severity=Severity.MEDIUM,
                subject=f"{role.value} has no bullets",
                detail=(
                    "the role is a heading with no duties or achievements under it, so it "
                    "contributes a title and dates and nothing else. On a technical resume that "
                    "is the line an interviewer skips."
                ),
                evidence=(resume_ref(role),),
            )
        )
    return findings


TECH_RULES: tuple[Rule, ...] = (
    Rule("tech_contradicted_claim", "T3's contradictions are flagged (mandatory)", _check_contradicted_claim),
    Rule(
        "tech_skill_without_project_context",
        "every claimed skill is demonstrated by a role",
        _check_skill_without_project_context,
    ),
    Rule(
        "tech_no_quantified_achievement",
        "at least one bullet states an outcome",
        _check_no_quantified_achievement,
    ),
    Rule("tech_role_without_bullets", "every role has at least one bullet", _check_role_without_bullets),
)


def _check_all(context: AuditContext) -> list[Finding]:
    return run_rule_set(context, TECH_RULES)


TECH_REVIEWER = Reviewer(
    mode="tech",
    title="Tech lead: is the technology demonstrated?",
    check=_check_all,
)
