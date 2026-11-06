"""Candidate audit core: findings, severity and the reviewer runner (Stage 0 / T4).

Three reviewers look at the same candidate from three professional angles -- an
ATS parser, an HR screener and a tech lead -- and each one produces **findings**.
This module owns the shared model and the plumbing; the reviewers themselves live
in :mod:`audit_ats`, :mod:`audit_hr` and :mod:`audit_tech`.

WHY THERE IS A FOURTH FILE
--------------------------
The Stage 0 plan lists ``audit_ats.py``, ``audit_hr.py`` and ``audit_tech.py``.
Three modules that all return ``Finding`` need **one** definition of ``Finding``
and **one** ordering of ``Severity``; three copies would drift, and a reviewer
that sorted severities differently from its neighbour would be a bug nobody
notices. So the shared model lives here, and the deviation from the plan's file
list is deliberate and recorded in the T4 report.

WHAT A FINDING IS NOT
---------------------
A finding is an **observation**, never an edit. Nothing in this module writes to
the resume, the profile, the knowledge base or the database. The only file the
command ever writes is its own report, and ``--no-write`` suppresses even that.
The acceptance criterion for T4 is exactly this, and it is checked by hashing the
protected files before and after a run rather than by asserting it in a comment.

WHY A FINDING DOES NOT CHANGE THE EXIT CODE
-------------------------------------------
Findings are the *expected product* of an audit. A ``BLOCKER`` means "the ATS
will struggle with this resume", not "the audit failed to run". Exit 0 therefore
means "the audit ran"; a non-zero code is reserved for being unable to run at
all. A script that needs to gate on findings reads the report or ``--json``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .kb import PROJECT_ROOT, CandidateKnowledgeBase, resolve_kb_path
from .kb_verify import (
    SOURCE_PROFILE,
    SOURCE_RESUME,
    SourceRef,
    VerificationResult,
    load_profile,
    resolve_profile_path,
    verify_all,
)

if TYPE_CHECKING:
    from ..candidate_profile import CandidateProfile
    from .kb import ClaimRecord

__all__ = [
    "ARTIFACTS_DIR",
    "DEFAULT_REPORT_STEM",
    "SEVERITY_ORDER",
    "AuditContext",
    "AuditResult",
    "Finding",
    "Reviewer",
    "Rule",
    "Severity",
    "candidate_audit_cmd",
    "claims_of_kind",
    "first_of_kind",
    "format_audit",
    "line_of",
    "profile_ref",
    "report_path_for",
    "resume_ref",
    "reviewers",
    "run_audit",
    "run_rule_set",
]

ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
DEFAULT_REPORT_STEM = "candidate_audit"

ALL_MODES = "all"


class Severity(StrEnum):
    """How much a finding costs the candidate, worst first.

    Ordered on purpose: ``SEVERITY_ORDER`` is the only place the ranking is
    written down, so the report, the JSON payload and the tests cannot disagree
    about which finding matters more.
    """

    BLOCKER = "BLOCKER"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.BLOCKER,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
)


@dataclass(frozen=True)
class Finding:
    """One reviewer's observation about the candidate.

    ``status`` carries a claim-level verdict when the finding is about a claim
    rather than about a document. The tech reviewer uses it to mark the
    contradictions T3 found as ``CONTRADICTED``, which the plan requires it to
    do; nothing else sets it.
    """

    rule: str
    severity: Severity
    subject: str
    detail: str
    evidence: tuple[SourceRef, ...] = ()
    status: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "severity": str(self.severity),
            "subject": self.subject,
            "detail": self.detail,
            "status": self.status,
            "evidence": [ref.to_dict() for ref in self.evidence],
        }


@dataclass(frozen=True)
class Reviewer:
    """One named reviewer: a mode, a title and the function that does the work."""

    mode: str
    title: str
    check: Callable[[AuditContext], list[Finding]]


@dataclass(frozen=True)
class Rule:
    """One named check inside a reviewer.

    Rules are separate objects rather than one big function so a test can run a
    reviewer with exactly one rule enabled. Without that, "this rule found
    nothing" and "this rule is dead" are indistinguishable -- the same
    false-negative trap T3 guarded against.
    """

    rule_id: str
    description: str
    check: Callable[[AuditContext], list[Finding]]


def run_rule_set(context: AuditContext, rules: tuple[Rule, ...]) -> list[Finding]:
    """Run the given rules. An empty tuple is a valid request and yields nothing."""
    found: list[Finding] = []
    for rule in rules:
        found.extend(rule.check(context))
    return found


@dataclass(frozen=True)
class AuditContext:
    """Everything the reviewers are allowed to look at.

    ``verification`` is T3's own result object, passed through rather than
    re-derived: the plan requires the tech lead to flag the contradictions T3
    found, and re-running the rules here could produce a *different* list than
    the one the candidate was shown by ``candidate-kb verify``.
    """

    claims: tuple[ClaimRecord, ...]
    profile: CandidateProfile
    profile_path: Path | None
    profile_label: str
    resume_label: str
    verification: VerificationResult


@dataclass
class AuditResult:
    mode: str
    kb_path: Path
    profile_path: Path | None
    profile_label: str
    resume_label: str
    findings: list[Finding]
    reviewers_run: tuple[str, ...]
    reviewers_silent: tuple[str, ...]
    stale_note: str | None
    verification: VerificationResult

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [item for item in self.findings if item.severity == severity]

    def by_rule(self, rule: str) -> list[Finding]:
        return [item for item in self.findings if item.rule == rule]

    def counts_by_severity(self) -> dict[str, int]:
        return {str(level): len(self.by_severity(level)) for level in SEVERITY_ORDER}

    def worst(self) -> Severity | None:
        return next((level for level in SEVERITY_ORDER if self.by_severity(level)), None)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "kb_path": str(self.kb_path),
            "profile_path": str(self.profile_path) if self.profile_path else None,
            "resume": self.resume_label,
            "finding_count": len(self.findings),
            "counts_by_severity": self.counts_by_severity(),
            "reviewers_run": list(self.reviewers_run),
            "reviewers_silent": list(self.reviewers_silent),
            "stale_note": self.stale_note,
            "contradictions_from_t3": len(self.verification.contradictions),
            "findings": [item.to_dict() for item in self.findings],
        }


# --------------------------------------------------------------------------
# The reviewer registry
# --------------------------------------------------------------------------


def reviewers() -> tuple[Reviewer, ...]:
    """The three reviewers, imported here so the modules can import this one.

    A module-level import would be circular: each reviewer needs ``Finding`` and
    ``Severity`` from this file.
    """
    from .audit_ats import ATS_REVIEWER
    from .audit_hr import HR_REVIEWER
    from .audit_tech import TECH_REVIEWER

    return (ATS_REVIEWER, HR_REVIEWER, TECH_REVIEWER)


def select_reviewers(mode: str) -> tuple[Reviewer, ...]:
    """``all`` means every reviewer; anything else must name exactly one."""
    available = reviewers()
    if mode == ALL_MODES:
        return available
    chosen = tuple(item for item in available if item.mode == mode)
    if not chosen:
        known = ", ".join([item.mode for item in available] + [ALL_MODES])
        raise ValueError(f"unknown audit mode {mode!r}; expected one of: {known}")
    return chosen


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _staleness_note(kb: CandidateKnowledgeBase, resume_path: str | None) -> str | None:
    """Whether the knowledge base still describes the resume on disk.

    The audit reads the *knowledge base*, not the resume file. If the resume has
    been edited since the last ``candidate-kb extract``, every finding below is
    about a snapshot that no longer exists. Saying so is cheaper than producing a
    confident report about stale text.
    """
    from .resume_parser import resolve_resume_path

    if not kb.source_sha256:
        return "the knowledge base records no source hash, so freshness is unknown"
    path = resolve_resume_path(resume_path)
    if path is None:
        return "the resume file was not found, so freshness could not be checked"
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"the resume file could not be read ({exc}), so freshness is unknown"
    if actual != kb.source_sha256:
        return (
            f"the resume on disk ({path}) has changed since the knowledge base was built "
            f"(kb {kb.source_sha256[:12]} vs file {actual[:12]}). This audit describes the "
            f"snapshot in the knowledge base, not the current file -- re-run "
            f"`candidate-kb extract` to refresh it."
        )
    return None


# --------------------------------------------------------------------------
# Evidence helpers shared by the reviewers
# --------------------------------------------------------------------------


def line_of(record: ClaimRecord) -> int:
    """The 1-based source line of a claim, or 0 when the record carries none."""
    try:
        return int(record.evidence.get("line") or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def claims_of_kind(context: AuditContext, *kinds: str) -> list[ClaimRecord]:
    """Every in-source claim of the given kinds, in document order."""
    wanted = set(kinds)
    records = [record for record in context.claims if record.kind in wanted]
    return sorted(records, key=lambda record: (line_of(record), record.kind, record.value))


def first_of_kind(context: AuditContext, *kinds: str) -> ClaimRecord | None:
    found = claims_of_kind(context, *kinds)
    return found[0] if found else None


def resume_ref(record: ClaimRecord) -> SourceRef:
    """A claim as one side of a finding, pointing back at the resume line."""
    source = str(record.evidence.get("source_file") or "resume.md")
    quote = record.evidence.get("quote")
    return SourceRef(
        source=SOURCE_RESUME,
        location=f"{source}:{line_of(record)}",
        value=record.value,
        quote=str(quote) if quote is not None else None,
    )


def profile_ref(context: AuditContext, field: str, value: object) -> SourceRef:
    """A profile field as the other side of a finding."""
    shown = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    return SourceRef(
        source=SOURCE_PROFILE,
        location=f"{context.profile_label}:{field}",
        value=shown,
    )


def run_audit(
    mode: str = ALL_MODES,
    kb_path: str | None = None,
    profile_path: str | None = None,
    resume_path: str | None = None,
) -> AuditResult:
    """Load the sources, run the chosen reviewers and collect their findings.

    Read-only with respect to every source: the knowledge base is opened for
    reading, the profile is read, and the resume is only hashed.
    """
    chosen = select_reviewers(mode)

    resolved_kb = resolve_kb_path(kb_path)
    knowledge_base = CandidateKnowledgeBase.load(resolved_kb)

    resolved_profile = resolve_profile_path(profile_path)
    if resolved_profile is not None:
        profile = load_profile(resolved_profile)
        profile_label = str(resolved_profile)
    else:
        from .kb_verify import BASELINE_LABEL, load_baseline_profile

        profile = load_baseline_profile()
        profile_label = BASELINE_LABEL

    verification = verify_all(kb_path=resolved_kb, profile_path=resolved_profile)

    context = AuditContext(
        claims=tuple(
            record for record in knowledge_base.claims.values() if record.in_source
        ),
        profile=profile,
        profile_path=resolved_profile,
        profile_label=profile_label,
        resume_label=str(knowledge_base.source_file or "resume.md"),
        verification=verification,
    )

    findings: list[Finding] = []
    for reviewer in chosen:
        findings.extend(reviewer.check(context))

    ranked = {str(level): index for index, level in enumerate(SEVERITY_ORDER)}
    findings.sort(key=lambda item: (ranked[str(item.severity)], item.rule, item.subject))

    return AuditResult(
        mode=mode,
        kb_path=resolved_kb,
        profile_path=resolved_profile,
        profile_label=profile_label,
        resume_label=context.resume_label,
        findings=findings,
        reviewers_run=tuple(item.mode for item in chosen),
        reviewers_silent=tuple(
            item.mode
            for item in chosen
            if not any(finding.rule.startswith(item.mode) for finding in findings)
        ),
        stale_note=_staleness_note(knowledge_base, resume_path),
        verification=verification,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def report_path_for(when: date | None = None) -> Path:
    """``artifacts/candidate_audit_<YYYY-MM-DD>.md``.

    Dated in **UTC**, like every other timestamp this project writes. A report
    dated by local time would disagree with the ``updated_at`` inside the
    knowledge base it describes.
    """
    stamp = (when or datetime.now(timezone.utc).date()).isoformat()
    return ARTIFACTS_DIR / f"{DEFAULT_REPORT_STEM}_{stamp}.md"


def format_audit(result: AuditResult) -> str:
    """The human-readable report, grouped by severity so the worst reads first."""
    lines = [
        f"candidate audit -- mode: {result.mode}",
        f"  knowledge base : {result.kb_path}",
        f"  profile        : {result.profile_path or result.profile_label}",
        f"  resume         : {result.resume_label}",
        f"  reviewers      : {', '.join(result.reviewers_run)}",
    ]
    if result.stale_note:
        lines.append(f"  WARNING        : {result.stale_note}")
    counts = result.counts_by_severity()
    lines.append("")
    lines.append(f"findings : {len(result.findings)}")
    lines.append(
        "  "
        + "  ".join(f"{level}: {counts[str(level)]}" for level in SEVERITY_ORDER)
    )

    current: Severity | None = None
    for finding in result.findings:
        if finding.severity != current:
            current = finding.severity
            lines.append("")
            lines.append(f"=== {current} ({counts[str(current)]}) ===")
        marker = f" [{finding.status}]" if finding.status else ""
        lines.append(f"  {finding.rule}{marker}: {finding.subject}")
        for ref in finding.evidence:
            value = ref.value if len(ref.value) <= 90 else ref.value[:87] + "..."
            lines.append(f"    {ref.source:<8} {ref.location}  ->  {value}")
        lines.append(f"    {finding.detail}")

    if result.reviewers_silent:
        lines.append("")
        lines.append(f"reviewers that found nothing: {', '.join(result.reviewers_silent)}")
    lines.append("")
    lines.append("read-only audit: no finding changed any file.")
    return "\n".join(lines)


def candidate_audit_cmd(
    mode: str = ALL_MODES,
    kb_path: str | None = None,
    profile_path: str | None = None,
    resume_path: str | None = None,
    as_json: bool = False,
    out_path: str | None = None,
    write: bool = True,
) -> int:
    """``candidate-audit`` handler.

    Prints the audit, and -- unless ``--no-write`` was given -- writes it to
    ``artifacts/candidate_audit_<date>.md`` (or ``--out``). The report is the
    command's declared output; **no source file is ever written**, and that is
    pinned by a test that hashes the protected files around a run.
    """
    try:
        select_reviewers(mode)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    resolved_kb = resolve_kb_path(kb_path)
    if not resolved_kb.is_file():
        print(f"ERROR: no knowledge base at {resolved_kb}", file=sys.stderr)
        print("Run `candidate-kb extract` first to build it.", file=sys.stderr)
        return 2

    result = run_audit(
        mode=mode,
        kb_path=kb_path,
        profile_path=profile_path,
        resume_path=resume_path,
    )

    written: Path | None = None
    if write:
        target = Path(out_path) if out_path else report_path_for()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(format_audit(result) + "\n", encoding="utf-8")
        written = target

    if as_json:
        payload = result.to_dict()
        payload["report_path"] = str(written) if written else None
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(format_audit(result))
    print()
    if written is None:
        print("report: not written (--no-write)")
    else:
        print(f"report: {written}")
    return 0
