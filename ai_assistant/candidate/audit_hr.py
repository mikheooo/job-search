"""HR reviewer: would a screener ask a question this resume cannot answer? (T4).

An HR screen is a five-second read followed by one question. This reviewer looks
for the two things that reliably produce that question and that can be checked
against the files rather than argued about:

* **A gap between roles.** The resume's own dates, in order, with the months
  between them counted. A gap is not a defect -- it is a question, and the value
  of reporting it is that the candidate gets to answer it before a stranger asks.
* **No stated expectation.** The profile records a minimum salary; the resume
  states no figure at all. A screener filtering on budget has nothing to filter
  on, and the candidate has no anchor if the conversation starts.

THE THRESHOLD IS A JUDGEMENT, AND IT IS WRITTEN DOWN
----------------------------------------------------
``GAP_THRESHOLD_MONTHS`` decides what counts as a gap. It is a parameter, not a
truth: a two-month gap is ordinary and is deliberately **not** reported, while a
gap of two years is. A test pins both sides of that line, so the threshold cannot
drift into noise without the suite going red.
"""

from __future__ import annotations

import re
from itertools import pairwise

from .audit import (
    AuditContext,
    Finding,
    Reviewer,
    Rule,
    Severity,
    claims_of_kind,
    profile_ref,
    resume_ref,
    run_rule_set,
)

__all__ = [
    "GAP_THRESHOLD_MONTHS",
    "HR_REVIEWER",
    "HR_RULES",
    "OPEN_ENDED_MARKERS",
    "SALARY_RE",
]

# A gap this long is what a screener asks about; anything shorter is ordinary
# between jobs and reporting it would bury the real findings in noise.
GAP_THRESHOLD_MONTHS = 3

# How a resume says "and I am still there". These have no end date to normalise
# to, which is intent, not a parsing failure.
OPEN_ENDED_MARKERS = frozenset({"present", "now", "current", "today", "ongoing"})

# A stated figure, a currency code, or the words a resume uses to promise one.
SALARY_RE = re.compile(
    r"(\$\s*\d"
    r"|\d[\d\s,.]*\s*(?:usd|eur|thb|rub|gbp)\b"
    r"|\bsalary\b"
    r"|\bcompensation\b"
    r"|\bmonthly\s+rate\b"
    r"|\bper\s+month\b)",
    re.IGNORECASE,
)

_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")


def _month_index(year: int, month: int) -> int:
    """A month as a single comparable integer, so gaps are subtraction."""
    return year * 12 + month


def _parse_period(value: str | None, *, is_end: bool) -> tuple[int, int] | None:
    """``2026-05`` -> ``(2026, 5)``; ``2018`` -> ``(2018, 12)`` for an end date.

    A year with no month is deliberately widened to the whole year: reading
    ``2009 - 2018`` as ending in January would manufacture a gap that the source
    does not state.
    """
    if not value:
        return None
    text = value.strip()
    year_month = _YEAR_MONTH_RE.match(text)
    if year_month:
        return int(year_month.group(1)), int(year_month.group(2))
    year_only = _YEAR_RE.match(text)
    if year_only:
        return int(year_only.group(1)), 12 if is_end else 1
    return None


class _Period:
    """One role's dates, with the raw claims kept for evidence."""

    def __init__(self, role: object, start: object | None, end: object | None) -> None:
        self.role = role
        self.start_claim = start
        self.end_claim = end
        self.start = _parse_period(_normalized(start), is_end=False)
        self.end = _parse_period(_normalized(end), is_end=True)
        self.open_ended = bool(end is not None and _is_open_ended(_normalized(end)))

    @property
    def label(self) -> str:
        return str(getattr(self.role, "value", "?"))


def _normalized(record: object | None) -> str | None:
    if record is None:
        return None
    return getattr(record, "normalized", None)


def _is_open_ended(value: str | None) -> bool:
    return bool(value) and str(value).strip().lower() in OPEN_ENDED_MARKERS


def _role_periods(context: AuditContext) -> list[_Period]:
    """Every role with the start/end claims that belong to it, in document order."""
    children: dict[str, dict[str, object]] = {}
    for record in claims_of_kind(context, "role_start", "role_end"):
        if not record.role_id:
            continue
        children.setdefault(record.role_id, {})[record.kind] = record

    periods: list[_Period] = []
    for role in claims_of_kind(context, "role"):
        owned = children.get(role.claim_id, {})
        periods.append(_Period(role, owned.get("role_start"), owned.get("role_end")))
    return periods


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def _check_employment_gap(context: AuditContext) -> list[Finding]:
    """Months between one role ending and the next beginning."""
    dated = [period for period in _role_periods(context) if period.start and period.end]
    dated.sort(key=lambda period: period.start or (0, 0))

    findings: list[Finding] = []
    for previous, current in pairwise(dated):
        assert previous.end is not None and current.start is not None
        gap = _month_index(*current.start) - _month_index(*previous.end) - 1
        if gap < GAP_THRESHOLD_MONTHS:
            continue
        evidence = [resume_ref(previous.role), resume_ref(current.role)]
        evidence.extend(
            resume_ref(claim)
            for claim in (previous.end_claim, current.start_claim)
            if claim is not None
        )
        findings.append(
            Finding(
                rule="hr_employment_gap",
                severity=Severity.HIGH,
                subject=f"{gap} months unexplained between {previous.label} and {current.label}",
                detail=(
                    f"the resume dates one role as ending and the next as beginning {gap} "
                    f"months later, with nothing in between. A gap is a question, not a "
                    f"defect: the fix is an answer the candidate already has, not a change to "
                    f"the dates. This is reported because a screener will ask it anyway."
                ),
                evidence=tuple(evidence),
            )
        )
    return findings


def _check_no_salary_expectation(context: AuditContext) -> list[Finding]:
    """The profile records a minimum salary; the resume states no figure."""
    blob = "\n".join(record.value for record in context.claims)
    if SALARY_RE.search(blob):
        return []

    declared = getattr(context.profile, "minimum_salary", None)
    currency = getattr(context.profile, "salary_currency", None)
    if declared:
        detail = (
            f"the resume states no salary expectation anywhere. The profile records a "
            f"minimum of {declared} {currency or ''}".rstrip()
            + ", so the two files disagree about whether an expectation exists at all. "
            "A screener filtering on budget has nothing to filter on."
        )
        evidence = (profile_ref(context, "minimum_salary", f"{declared} {currency or ''}".strip()),)
    else:
        detail = (
            "the resume states no salary expectation and the profile records none either, so "
            "the number does not exist in any file. Nothing here is wrong; the point is that "
            "the first person to name a number will be the recruiter."
        )
        evidence = ()

    return [
        Finding(
            rule="hr_no_salary_expectation",
            severity=Severity.MEDIUM,
            subject="no salary expectation is stated anywhere",
            detail=detail,
            evidence=evidence,
        )
    ]


def _check_role_without_dates(context: AuditContext) -> list[Finding]:
    """A role with no start date at all: it cannot be placed on a timeline."""
    findings: list[Finding] = []
    for period in _role_periods(context):
        if period.start_claim is not None:
            continue
        findings.append(
            Finding(
                rule="hr_role_without_dates",
                severity=Severity.HIGH,
                subject=f"{period.label} carries no start date",
                detail=(
                    "the role heading has no dated line under it, so the role cannot be placed "
                    "on a timeline. An ATS ordering experience by date will either drop it or "
                    "place it arbitrarily."
                ),
                evidence=(resume_ref(period.role),),
            )
        )
    return findings


def _check_unparsed_period(context: AuditContext) -> list[Finding]:
    """A date the parser could read as text but not as a month."""
    findings: list[Finding] = []
    for period in _role_periods(context):
        for claim in (period.start_claim, period.end_claim):
            if claim is None or claim.normalized or _is_open_ended(claim.value):
                continue
            findings.append(
                Finding(
                    rule="hr_unparsed_period",
                    severity=Severity.MEDIUM,
                    subject=f"{claim.value!r} on {period.label} is not a recognisable month",
                    detail=(
                        "the line states a period that is not a month-and-year or a year, so it "
                        "stays unnormalised. A date-based filter cannot order it, and neither "
                        "can a reader skimming the section."
                    ),
                    evidence=(resume_ref(claim),),
                )
            )
    return findings


def _check_overlapping_roles(context: AuditContext) -> list[Finding]:
    """Two roles claiming the same months: a screener reads that as an error."""
    dated = [period for period in _role_periods(context) if period.start and period.end]
    dated.sort(key=lambda period: period.start or (0, 0))

    findings: list[Finding] = []
    for previous, current in pairwise(dated):
        assert previous.end is not None and current.start is not None
        if _month_index(*current.start) >= _month_index(*previous.end):
            continue
        findings.append(
            Finding(
                rule="hr_overlapping_roles",
                severity=Severity.MEDIUM,
                subject=f"{previous.label} and {current.label} overlap",
                detail=(
                    "the second role begins before the first one ends. Concurrent work is "
                    "normal and legitimate, but a screener reads overlapping dates as a typo "
                    "unless the resume says otherwise."
                ),
                evidence=(resume_ref(previous.role), resume_ref(current.role)),
            )
        )
    return findings


HR_RULES: tuple[Rule, ...] = (
    Rule("hr_employment_gap", "no unexplained gap between roles", _check_employment_gap),
    Rule("hr_no_salary_expectation", "a salary expectation exists", _check_no_salary_expectation),
    Rule("hr_role_without_dates", "every role is dated", _check_role_without_dates),
    Rule("hr_unparsed_period", "every stated period is a month or a year", _check_unparsed_period),
    Rule("hr_overlapping_roles", "no two roles claim the same months", _check_overlapping_roles),
)


def _check_all(context: AuditContext) -> list[Finding]:
    return run_rule_set(context, HR_RULES)


HR_REVIEWER = Reviewer(
    mode="hr",
    title="HR screen: timeline and expectations",
    check=_check_all,
)
