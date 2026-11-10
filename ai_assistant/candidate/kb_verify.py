"""Cross-source verification: resume KB vs profile vs built-in baseline (Stage 0 / T3).

Three sources describe the same candidate, and they do not fully agree:

* the **resume** -- as extracted into the knowledge base by T1 (``resume.md``);
* the **profile** -- ``candidate_profile.json``, the file the pipeline actually
  loads;
* the **baseline** -- the built-in fallback inside ``candidate_profile.py``,
  which is what the pipeline uses when no profile file is found.

This module compares them and reports **contradictions**, each citing the sources
it came from. It deliberately resolves nothing. A contradiction is a question for
the candidate; a tool that picked a winner would be inventing an answer, and the
whole point of T1/T2 was to stop inventing answers.

WHY THIS FILE IS ALLOWED TO READ A NUMBER OUT OF PROSE
------------------------------------------------------
T1 refuses to turn "15+ years of IT experience" into a number: the claim carries
``normalized = None``, because a prose total is not a measurement. This module
does parse a lower bound out of that quote -- but only in order to *compare* it,
and it never writes the number back. Comparing is not storing, and the KB is
never opened for writing here.

WHAT IS NOT A CONTRADICTION
---------------------------
The built-in baseline intentionally carries no name, email or phone (BLE001
finding #44 documents that). That is a documented design decision, not a
disagreement, so identity fields are excluded from the baseline comparison. A
checker that flagged them would produce noise on a fact that is already known and
already deliberate.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .kb import CandidateKnowledgeBase, resolve_kb_path

if TYPE_CHECKING:
    from ..candidate_profile import CandidateProfile
    from .kb import ClaimRecord

__all__ = [
    "DEFAULT_RULES",
    "SOURCE_BASELINE",
    "SOURCE_PROFILE",
    "SOURCE_RESUME",
    "Contradiction",
    "Rule",
    "SourceRef",
    "VerificationResult",
    "candidate_kb_verify_cmd",
    "format_verification",
    "load_baseline_profile",
    "run_rules",
    "verify_all",
]

SOURCE_RESUME = "resume"
SOURCE_PROFILE = "profile"
SOURCE_BASELINE = "baseline"

BASELINE_LABEL = "built-in fallback in ai_assistant/candidate_profile.py"

# "15+ years", "11 years", "3+ years" -- the lower bound the resume states.
EXPERIENCE_YEARS_RE = re.compile(r"(\d+)\s*\+?\s*years?", re.IGNORECASE)
BITRIX_RE = re.compile(r"bitrix", re.IGNORECASE)

# Kinds whose value can carry a technology the profile might exclude. `summary`
# and `experience_statement` are excluded on purpose: they mention many
# technologies at once, so a hit there says nothing specific.
TECHNOLOGY_BEARING_KINDS = ("role", "role_company", "role_title", "duty", "skill", "skill_group")
_TECHNOLOGY_PREFERENCE = {kind: index for index, kind in enumerate(TECHNOLOGY_BEARING_KINDS)}

# Fields compared against the built-in fallback. Identity fields are left out:
# the fallback is documented as anonymous, so an empty name there is intent.
BASELINE_COMPARED_FIELDS = (
    "target_roles",
    "desired_roles",
    "alternative_roles",
    "role_families",
    "years_experience",
    "years_of_experience",
    "remote_required",
    "minimum_salary",
    "salary_currency",
    "core_skills",
    "secondary_skills",
    "transferable_skills",
    "languages",
    "excluded_roles",
    "excluded_countries",
    "excluded_industries",
    "must_avoid_conditions",
)


# --------------------------------------------------------------------------
# Result model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """One side of a contradiction, with enough context to go and look."""

    source: str
    location: str
    value: str
    quote: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "location": self.location,
            "value": self.value,
            "quote": self.quote,
        }


@dataclass(frozen=True)
class Contradiction:
    """A disagreement between sources. Reporting it is the whole product."""

    rule: str
    subject: str
    detail: str
    sources: tuple[SourceRef, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "subject": self.subject,
            "detail": self.detail,
            "sources": [ref.to_dict() for ref in self.sources],
        }


@dataclass
class VerificationResult:
    kb_path: Path
    profile_path: Path | None
    baseline_label: str
    contradictions: list[Contradiction]
    rules_run: tuple[str, ...]
    rules_silent: tuple[str, ...]

    def by_rule(self, rule: str) -> list[Contradiction]:
        return [item for item in self.contradictions if item.rule == rule]

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_path": str(self.kb_path),
            "profile_path": str(self.profile_path) if self.profile_path else None,
            "baseline": self.baseline_label,
            "contradiction_count": len(self.contradictions),
            "rules_run": list(self.rules_run),
            "rules_silent": list(self.rules_silent),
            "contradictions": [item.to_dict() for item in self.contradictions],
        }


@dataclass(frozen=True)
class VerifyContext:
    """Everything the rules are allowed to look at."""

    claims: tuple[ClaimRecord, ...]
    profile: CandidateProfile
    profile_path: Path | None
    profile_label: str
    baseline: CandidateProfile
    profile_copies: tuple[tuple[Path, CandidateProfile], ...]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    description: str
    check: Callable[[VerifyContext], list[Contradiction]]


# --------------------------------------------------------------------------
# Loading the three sources
# --------------------------------------------------------------------------


def _candidate_profile_module():
    from .. import candidate_profile

    return candidate_profile


def resolve_profile_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Which profile file the pipeline would load, or None if it would fall back.

    ``DEFAULT_PROFILE_PATHS`` is cwd-relative. This resolver tries them as-is
    first -- that is the pipeline's own behaviour -- and then relative to the
    project root, so the command still reports the real file when run from
    elsewhere. The resolved path is printed, so the choice is never hidden.
    """
    from .kb import PROJECT_ROOT

    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None

    env_path = os.getenv("CANDIDATE_PROFILE") or os.getenv("CANDIDATE_PROFILE_FILE")
    if env_path and Path(env_path).is_file():
        return Path(env_path)

    for relative in _candidate_profile_module().DEFAULT_PROFILE_PATHS:
        for candidate in (Path(relative), PROJECT_ROOT / relative):
            if candidate.is_file():
                return candidate
    return None


def load_profile(path: Path) -> CandidateProfile:
    return _candidate_profile_module().CandidateProfile.from_json_file(path)


def load_baseline_profile() -> CandidateProfile:
    """The built-in fallback, reached by making every real source unavailable.

    There is no public accessor for it, and ``CandidateProfile()`` is an empty
    profile rather than the baseline, so the only honest way to obtain it is to
    call ``load_candidate_profile()`` with nothing to find. The fallback logs a
    warning by design; it is silenced here because a warning saying "profile not
    found" while verifying a profile that does exist would be actively
    misleading. Module state is restored in ``finally``.
    """
    module = _candidate_profile_module()
    saved_paths = module.DEFAULT_PROFILE_PATHS
    saved_env = {
        name: os.environ.pop(name)
        for name in ("CANDIDATE_PROFILE", "CANDIDATE_PROFILE_FILE")
        if name in os.environ
    }
    saved_disabled = module.logger.disabled
    module.DEFAULT_PROFILE_PATHS = []
    module.logger.disabled = True
    try:
        return module.load_candidate_profile()
    finally:
        module.logger.disabled = saved_disabled
        module.DEFAULT_PROFILE_PATHS = saved_paths
        os.environ.update(saved_env)


def discover_profile_copies() -> tuple[tuple[Path, CandidateProfile], ...]:
    """Every profile file that exists, so a duplicate cannot drift unnoticed."""
    from .kb import PROJECT_ROOT

    seen: list[Path] = []
    found: list[tuple[Path, CandidateProfile]] = []
    for relative in _candidate_profile_module().DEFAULT_PROFILE_PATHS:
        for candidate in (Path(relative), PROJECT_ROOT / relative):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.append(resolved)
            found.append((candidate, load_profile(candidate)))
    return tuple(found)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _fmt_number(value: object) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _claims_of_kind(context: VerifyContext, *kinds: str) -> list[ClaimRecord]:
    wanted = set(kinds)
    records = [record for record in context.claims if record.kind in wanted]
    return sorted(records, key=lambda record: (record.kind, _line_of(record), record.value))


def _first_of_kind(context: VerifyContext, *kinds: str) -> ClaimRecord | None:
    found = _claims_of_kind(context, *kinds)
    return found[0] if found else None


def _line_of(record: ClaimRecord) -> int:
    try:
        return int(record.evidence.get("line") or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _source_file_of(record: ClaimRecord) -> str:
    return str(record.evidence.get("source_file") or "resume.md")


def _quote_of(record: ClaimRecord) -> str | None:
    quote = record.evidence.get("quote")
    return str(quote) if quote is not None else None


def _resume_ref(record: ClaimRecord) -> SourceRef:
    return SourceRef(
        source=SOURCE_RESUME,
        location=f"{_source_file_of(record)}:{_line_of(record)}",
        value=record.value,
        quote=_quote_of(record),
    )


def _profile_ref(context: VerifyContext, field: str, value: object) -> SourceRef:
    return SourceRef(
        source=SOURCE_PROFILE,
        location=f"{context.profile_label}:{field}",
        value=_fmt_number(value) if not isinstance(value, list) else ", ".join(map(str, value)),
    )


def _baseline_ref(field: str, value: object) -> SourceRef:
    return SourceRef(
        source=SOURCE_BASELINE,
        location=f"{BASELINE_LABEL}:{field}",
        value=_fmt_number(value) if not isinstance(value, list) else ", ".join(map(str, value)),
    )


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def _check_total_experience_vs_it_support(context: VerifyContext) -> list[Contradiction]:
    """The resume's stated total vs the profile's it_support figure."""
    statement = _first_of_kind(context, "experience_statement")
    if statement is None:
        return []
    match = EXPERIENCE_YEARS_RE.search(statement.value)
    if match is None:
        return []
    stated = int(match.group(1))

    domain = context.profile.domain_years or {}
    if "it_support" not in domain:
        return []
    it_support = domain["it_support"]
    try:
        it_support_value = float(it_support)
    except (TypeError, ValueError):
        return []
    if it_support_value >= stated:
        return []

    return [
        Contradiction(
            rule="total_experience_vs_it_support",
            subject="total IT experience",
            detail=(
                f"the resume states {stated}+ years of IT experience; the profile records "
                f"{_fmt_number(it_support)} for domain_years.it_support, which is below the "
                f"resume's own lower bound. Both cannot be right. Note the two are not the "
                f"same measure -- it_support is a domain figure and the resume's number is a "
                f"prose total -- which is exactly why this needs a human answer rather than "
                f"a rule."
            ),
            sources=(
                _resume_ref(statement),
                _profile_ref(context, "domain_years.it_support", it_support),
            ),
        )
    ]


def _check_bitrix_experience_vs_excluded(context: VerifyContext) -> list[Contradiction]:
    """The resume presents Bitrix24 as experience; the profile excludes bitrix."""
    hits = [
        record
        for record in _claims_of_kind(context, *TECHNOLOGY_BEARING_KINDS)
        if BITRIX_RE.search(record.value)
    ]
    if not hits:
        return []

    excluded_fields = [
        field
        for field in ("excluded_roles", "must_avoid_conditions")
        if any(BITRIX_RE.search(str(item)) for item in (getattr(context.profile, field, None) or []))
    ]
    if not excluded_fields:
        return []

    hits.sort(key=lambda record: _TECHNOLOGY_PREFERENCE.get(record.kind, len(TECHNOLOGY_BEARING_KINDS)))
    best = hits[0]

    sources = [_resume_ref(best)]
    sources.extend(
        _profile_ref(context, field, getattr(context.profile, field))
        for field in excluded_fields
    )

    return [
        Contradiction(
            rule="bitrix_experience_vs_excluded",
            subject="Bitrix24: presented experience vs excluded role",
            detail=(
                f"the resume presents Bitrix24 as professional experience ({len(hits)} claim(s) "
                f"mention it, starting at {_source_file_of(best)}:{_line_of(best)}), while the "
                f"profile lists 'bitrix' under {', '.join(excluded_fields)}. Anything that "
                f"filters vacancies by those lists will discard the very experience the resume "
                f"leads with. This is a tension between what to present and what to avoid, not "
                f"a factual error on either side -- so it is a question, not a defect."
            ),
            sources=tuple(sources),
        )
    ]


def _check_years_experience_vs_domain_years(context: VerifyContext) -> list[Contradiction]:
    """A domain figure cannot exceed the total it belongs to."""
    total = context.profile.years_experience
    if total is None:
        total = context.profile.years_of_experience
    if total is None:
        return []

    try:
        total_value = float(total)
    except (TypeError, ValueError):
        return []

    out: list[Contradiction] = []
    for key, raw in sorted((context.profile.domain_years or {}).items()):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= total_value:
            continue
        out.append(
            Contradiction(
                rule="years_experience_vs_domain_years",
                subject=f"years_experience vs domain_years.{key}",
                detail=(
                    f"the profile states years_experience = {_fmt_number(total)}, but "
                    f"domain_years.{key} = {_fmt_number(value)}. A domain cannot exceed the "
                    f"total it belongs to, so either years_experience is not a total or it is "
                    f"stale. The same root cause produces one finding per offending domain."
                ),
                sources=(
                    _profile_ref(context, "years_experience", total),
                    _profile_ref(context, f"domain_years.{key}", raw),
                ),
            )
        )
    return out


def _check_baseline_vs_profile_drift(context: VerifyContext) -> list[Contradiction]:
    """What the fallback would change if the profile file ever went missing."""
    out: list[Contradiction] = []
    for field in BASELINE_COMPARED_FIELDS:
        live = getattr(context.profile, field, None)
        fallback = getattr(context.baseline, field, None)
        if live == fallback:
            continue
        out.append(
            Contradiction(
                rule="baseline_vs_profile_drift",
                subject=f"built-in fallback disagrees on {field}",
                detail=(
                    f"the live profile and the built-in fallback differ on {field}. If the "
                    f"profile file goes missing, load_candidate_profile() falls back silently "
                    f"and the candidate changes underneath the pipeline."
                ),
                sources=(
                    _profile_ref(context, field, live),
                    _baseline_ref(field, fallback),
                ),
            )
        )
    return out


def _check_profile_copy_drift(context: VerifyContext) -> list[Contradiction]:
    """Duplicate profile files must not disagree, since only the first is loaded."""
    if len(context.profile_copies) < 2:
        return []

    reference_path, reference = context.profile_copies[0]
    out: list[Contradiction] = []
    for path, profile in context.profile_copies[1:]:
        for field in BASELINE_COMPARED_FIELDS + ("name", "email"):
            left = getattr(reference, field, None)
            right = getattr(profile, field, None)
            if left == right:
                continue
            out.append(
                Contradiction(
                    rule="profile_copy_drift",
                    subject=f"profile copies disagree on {field}",
                    detail=(
                        f"{reference_path} and {path} disagree on {field}. "
                        f"DEFAULT_PROFILE_PATHS loads the first match, so the second copy is "
                        f"either dead weight or a trap."
                    ),
                    sources=(
                        SourceRef(SOURCE_PROFILE, f"{reference_path}:{field}", _fmt_number(left)),
                        SourceRef(SOURCE_PROFILE, f"{path}:{field}", _fmt_number(right)),
                    ),
                )
            )
    return out


def _check_contact_mismatch(context: VerifyContext) -> list[Contradiction]:
    """Fires only on a real mismatch -- the control for false positives."""
    out: list[Contradiction] = []

    email = _first_of_kind(context, "contact_email")
    if (
        email is not None
        and context.profile.email
        and email.value.strip().lower() != str(context.profile.email).strip().lower()
    ):
        out.append(
            Contradiction(
                rule="contact_mismatch",
                subject="email",
                detail="the resume and the profile state different email addresses.",
                sources=(_resume_ref(email), _profile_ref(context, "email", context.profile.email)),
            )
        )

    name = _first_of_kind(context, "name")
    if (
        name is not None
        and context.profile.name
        and name.value.strip() != str(context.profile.name).strip()
    ):
        out.append(
            Contradiction(
                rule="contact_mismatch",
                subject="name",
                detail="the resume and the profile state different names.",
                sources=(_resume_ref(name), _profile_ref(context, "name", context.profile.name)),
            )
        )

    return out


def _check_language_mismatch(context: VerifyContext) -> list[Contradiction]:
    """Resume languages vs profile languages, compared through ISO 639-1."""
    from .resume_parser import LANGUAGE_ISO

    resume_languages: dict[str, ClaimRecord] = {}
    for record in _claims_of_kind(context, "language"):
        code = str(record.normalized or "").strip().lower()
        if not code:
            code = LANGUAGE_ISO.get(record.value.strip().lower(), record.value.strip().lower())
        if code:
            resume_languages.setdefault(code, record)
    if not resume_languages:
        return []

    profile_codes = set()
    for entry in context.profile.languages or []:
        text = str(entry).strip().lower()
        profile_codes.add(LANGUAGE_ISO.get(text, text))

    missing = sorted(set(resume_languages) - profile_codes)
    if not missing:
        return []

    return [
        Contradiction(
            rule="language_mismatch",
            subject="languages",
            detail=(
                f"the resume states {', '.join(missing)} but the profile's languages list does "
                f"not cover it. Anything filtering on the profile would treat a language the "
                f"candidate actually speaks as absent."
            ),
            sources=(
                _resume_ref(resume_languages[missing[0]]),
                _profile_ref(context, "languages", context.profile.languages),
            ),
        )
    ]


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        "total_experience_vs_it_support",
        "resume's stated total IT experience vs profile domain_years.it_support",
        _check_total_experience_vs_it_support,
    ),
    Rule(
        "bitrix_experience_vs_excluded",
        "Bitrix24 as presented experience vs profile exclusion lists",
        _check_bitrix_experience_vs_excluded,
    ),
    Rule(
        "years_experience_vs_domain_years",
        "profile years_experience vs its own domain_years entries",
        _check_years_experience_vs_domain_years,
    ),
    Rule(
        "baseline_vs_profile_drift",
        "live profile vs the built-in fallback profile",
        _check_baseline_vs_profile_drift,
    ),
    Rule(
        "profile_copy_drift",
        "duplicate profile files vs each other",
        _check_profile_copy_drift,
    ),
    Rule(
        "contact_mismatch",
        "resume contact details vs profile contact details",
        _check_contact_mismatch,
    ),
    Rule(
        "language_mismatch",
        "resume languages vs profile languages",
        _check_language_mismatch,
    ),
)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def run_rules(
    context: VerifyContext, rules: tuple[Rule, ...] = DEFAULT_RULES
) -> list[Contradiction]:
    """Run the given rules. An empty tuple is a valid request and yields nothing."""
    found: list[Contradiction] = []
    for rule in rules:
        found.extend(rule.check(context))
    return sorted(found, key=lambda item: (item.rule, item.subject))


def verify_all(
    kb_path: str | os.PathLike[str] | None = None,
    profile_path: str | os.PathLike[str] | None = None,
    *,
    rules: tuple[Rule, ...] = DEFAULT_RULES,
) -> VerificationResult:
    """Load all three sources and report where they disagree."""
    resolved_kb = resolve_kb_path(kb_path)
    knowledge_base = CandidateKnowledgeBase.load(resolved_kb)

    resolved_profile = resolve_profile_path(profile_path)
    if resolved_profile is not None:
        profile = load_profile(resolved_profile)
        profile_label = str(resolved_profile)
    else:
        profile = load_baseline_profile()
        profile_label = BASELINE_LABEL

    context = VerifyContext(
        claims=tuple(record for record in knowledge_base.claims.values() if record.in_source),
        profile=profile,
        profile_path=resolved_profile,
        profile_label=profile_label,
        baseline=load_baseline_profile(),
        profile_copies=discover_profile_copies(),
    )

    contradictions = run_rules(context, rules)
    firing = {item.rule for item in contradictions}
    return VerificationResult(
        kb_path=resolved_kb,
        profile_path=resolved_profile,
        baseline_label=BASELINE_LABEL,
        contradictions=contradictions,
        rules_run=tuple(rule.rule_id for rule in rules),
        rules_silent=tuple(rule.rule_id for rule in rules if rule.rule_id not in firing),
    )


def format_verification(result: VerificationResult) -> str:
    """Human-readable rendering. Groups by rule so a shared root cause is visible."""
    if result.profile_path:
        profile_display = str(result.profile_path)
    else:
        profile_display = f"NOT FOUND -> {BASELINE_LABEL}"
    firing = len(result.rules_run) - len(result.rules_silent)
    lines = [
        "cross-source verification",
        f"  knowledge base : {result.kb_path}",
        f"  profile        : {profile_display}",
        f"  baseline       : {result.baseline_label}",
        f"  rules          : {len(result.rules_run)} run, {firing} found something",
        "",
        f"contradictions : {len(result.contradictions)}",
    ]

    current = None
    for item in result.contradictions:
        if item.rule != current:
            current = item.rule
            lines.append("")
            lines.append(f"[{item.rule}] {len(result.by_rule(item.rule))}")
        lines.append(f"  subject: {item.subject}")
        for ref in item.sources:
            value = ref.value if len(ref.value) <= 90 else ref.value[:87] + "..."
            lines.append(f"    {ref.source:<8} {ref.location}  ->  {value}")
        lines.append(f"    detail: {item.detail}")

    if result.rules_silent:
        lines.append("")
        lines.append(f"rules that found nothing: {', '.join(result.rules_silent)}")

    return "\n".join(lines)


def candidate_kb_verify_cmd(
    kb_path: str | None = None,
    profile_path: str | None = None,
    as_json: bool = False,
) -> int:
    """``candidate-kb verify`` -- read-only cross-source report.

    Exit code 0 means the verification ran. Contradictions are the expected
    output of this command, not a failure of it, so they do not change the exit
    code; only being unable to run does.
    """
    path = resolve_kb_path(kb_path)
    if not path.is_file():
        print(f"ERROR: no knowledge base at {path}", file=sys.stderr)
        print("Run `candidate-kb extract` first to build it.", file=sys.stderr)
        return 2

    result = verify_all(kb_path=path, profile_path=profile_path)

    if as_json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0

    print(format_verification(result))
    return 0
