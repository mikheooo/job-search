"""ATS reviewer: can a parser read this resume, and can a recruiter reply? (T4).

An ATS is a parser before it is a judge. It splits the header into fields, tries
to match a headline against a title vocabulary, and extracts a way to contact the
person. This reviewer asks only those questions:

* **Is there a contact channel at all?** No email and no messenger handle means
  the application cannot be answered, whatever else the resume says.
* **Does the resume carry the channels the profile declares?** The profile knows
  about two phone numbers and three links; the resume states none of them. That
  is a fact about the two files, not an opinion about resumes.
* **Is the headline parseable?** A headline that lists three titles separated by
  pipes is the classic case where a parser records one long string instead of a
  target role, or records the first title and drops the rest.

WHAT THIS REVIEWER DELIBERATELY DOES NOT DO
-------------------------------------------
It does not judge writing quality, length, formatting or "impact". Those are
opinions, and an audit full of opinions is one the candidate learns to ignore.
Every finding here can be checked by opening the two files and looking.
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
    first_of_kind,
    profile_ref,
    resume_ref,
    run_rule_set,
)

__all__ = [
    "ATS_REVIEWER",
    "ATS_RULES",
    "CONTACT_CHANNEL_KINDS",
    "NON_TITLE_MARKERS",
    "SEPARATOR_CHARS",
]

# An ATS splits a headline on these and then reports several conflicting target
# titles -- or picks one and silently drops the others.
SEPARATOR_CHARS = ("|", "/", ";")

# Words that describe how someone sees themselves rather than what the job is.
# A title vocabulary has nothing to match them against.
NON_TITLE_MARKERS = frozenset(
    {"ninja", "rockstar", "guru", "wizard", "evangelist", "superhero", "unicorn"}
)

# Kinds that mean "a recruiter can reach the candidate through this".
CONTACT_CHANNEL_KINDS = ("contact_email", "contact_telegram")

_HEADLINE_SPLIT_RE = re.compile(r"[|/;]")
_LETTER_RE = re.compile(r"[A-Za-z]")
_NON_DIGIT_RE = re.compile(r"\D")
_URL_SCHEME_RE = re.compile(r"^[a-z]+://")
_URL_WWW_RE = re.compile(r"^www\.")
_PHONE_TAIL_DIGITS = 9


def _resume_blob(context: AuditContext) -> str:
    """Every claim value, joined and lower-cased: the resume as one searchable text."""
    return "\n".join(record.value.lower() for record in context.claims)


def _resume_mentions(blob: str, needle: str) -> bool:
    """Whether the resume text carries ``needle``.

    A numeric needle is compared digit-to-digit so that a phone written with
    spaces or dashes still counts as present. This is the only leniency in the
    reviewer, and it exists to avoid reporting a channel as missing when it is
    merely formatted differently.
    """
    if not needle:
        return False
    if needle.isdigit():
        return needle in _NON_DIGIT_RE.sub("", blob)
    return needle in blob


def _digits_tail(value: str) -> str:
    """The last nine digits of a phone number: enough to identify, short enough to survive formatting."""
    return _NON_DIGIT_RE.sub("", value)[-_PHONE_TAIL_DIGITS:]


def _url_token(url: str) -> str:
    """``https://www.linkedin.com/in/x/`` -> ``linkedin.com/in/x``."""
    token = _URL_SCHEME_RE.sub("", url.strip().lower())
    token = _URL_WWW_RE.sub("", token)
    return token.rstrip("/")


def _declared_channels(
    profile: object,
) -> list[tuple[str, Severity, list[tuple[str, str]]]]:
    """Every contact channel the profile declares, as searchable needles.

    Grouped by channel rather than by field: two phone numbers are one channel,
    and reporting them as two separate findings would double the apparent size of
    a single problem.
    """
    groups: list[tuple[str, Severity, list[tuple[str, str]]]] = []

    email = str(getattr(profile, "email", "") or "").strip()
    if email:
        groups.append(("email", Severity.BLOCKER, [("email", email.lower())]))

    phones: list[tuple[str, str]] = []
    for field in ("phone_ru", "phone_th"):
        value = str(getattr(profile, field, "") or "").strip()
        if value:
            phones.append((field, _digits_tail(value)))
    if phones:
        groups.append(("phone", Severity.HIGH, phones))

    for field in ("linkedin", "github", "portfolio"):
        value = str(getattr(profile, field, "") or "").strip()
        if value:
            groups.append((field, Severity.LOW, [(field, _url_token(value))]))

    return groups


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def _check_no_contact_channel(context: AuditContext) -> list[Finding]:
    """No email and no messenger handle: the application has nowhere to go."""
    if claims_of_kind(context, *CONTACT_CHANNEL_KINDS):
        return []
    return [
        Finding(
            rule="ats_no_contact_channel",
            severity=Severity.BLOCKER,
            subject="the resume states no contact channel",
            detail=(
                "the resume carries neither an email address nor a messenger handle, so a "
                "recruiter reading it has no way to reply. The finding has no line to point "
                "at: what is missing is the evidence."
            ),
            evidence=(),
        )
    ]


def _check_contact_channel_gap(context: AuditContext) -> list[Finding]:
    """Channels the profile declares that the resume never states."""
    if not claims_of_kind(context, *CONTACT_CHANNEL_KINDS):
        # The resume has no contact channel at all; ats_no_contact_channel already
        # reported that as a blocker. Listing every missing channel on top of it
        # would inflate one problem into five.
        return []

    blob = _resume_blob(context)
    findings: list[Finding] = []
    for channel, severity, needles in _declared_channels(context.profile):
        missing = [(field, needle) for field, needle in needles if not _resume_mentions(blob, needle)]
        if not missing:
            continue
        fields = ", ".join(field for field, _ in missing)
        findings.append(
            Finding(
                rule="ats_contact_channel_gap",
                severity=severity,
                subject=f"the resume omits {fields}, declared in the profile",
                detail=(
                    f"the profile declares a {channel} ({fields}), and the resume never states "
                    f"it. An ATS reading only the resume loses that channel entirely. This is a "
                    f"gap between two files, not a judgement about either -- if the channel is "
                    f"deliberately private, the profile is the file to change. Only the values "
                    f"that are actually missing are named here, so a resume that carries one of "
                    f"two phone numbers is reported for the other one, not for both."
                ),
                evidence=tuple(
                    profile_ref(context, field, getattr(context.profile, field, None))
                    for field, _ in missing
                ),
            )
        )
    return findings


def _check_headline_uses_separators(context: AuditContext) -> list[Finding]:
    """A headline listing several titles is parsed as one string, or loses titles."""
    headline = first_of_kind(context, "headline")
    if headline is None:
        return []
    hits = [char for char in SEPARATOR_CHARS if char in headline.value]
    if not hits:
        return []
    joined = "".join(hits)
    return [
        Finding(
            rule="ats_headline_uses_separators",
            severity=Severity.MEDIUM,
            subject="the headline lists several titles separated by punctuation",
            detail=(
                f"the headline contains {joined!r}. Parsers commonly keep the whole line as "
                f"one string, or take the first title and drop the rest -- so which of the "
                f"titles gets matched against a vacancy is decided by the parser, not by the "
                f"candidate. Splitting the headline into one primary title is the usual fix; "
                f"that is a decision for the candidate, so this is reported, not applied."
            ),
            evidence=(resume_ref(headline),),
        )
    ]


def _check_headline_not_a_job_title(context: AuditContext) -> list[Finding]:
    """A headline segment that names an attitude rather than a job."""
    headline = first_of_kind(context, "headline")
    if headline is None:
        return []
    findings: list[Finding] = []
    for segment in (part.strip() for part in _HEADLINE_SPLIT_RE.split(headline.value)):
        if not segment:
            continue
        lowered = segment.lower()
        marker = next(
            (word for word in sorted(NON_TITLE_MARKERS) if re.search(rf"\b{word}\b", lowered)),
            None,
        )
        if marker is None and _LETTER_RE.search(segment):
            continue
        reason = f"contains {marker!r}" if marker else "contains no words"
        findings.append(
            Finding(
                rule="ats_headline_not_a_job_title",
                severity=Severity.MEDIUM,
                subject=f"headline segment {segment!r} is not a job title",
                detail=(
                    f"the segment {reason}. A title vocabulary has nothing to match against "
                    f"it, so this part of the headline is invisible to keyword screening."
                ),
                evidence=(resume_ref(headline),),
            )
        )
    return findings


def _check_headline_absent(context: AuditContext) -> list[Finding]:
    """No headline at all: the parser has no target role to match."""
    if first_of_kind(context, "headline") is not None:
        return []
    return [
        Finding(
            rule="ats_headline_absent",
            severity=Severity.HIGH,
            subject="the resume states no headline",
            detail=(
                "there is no headline line, so nothing in the document names the role being "
                "applied for. Keyword screening then has to infer the target role from the "
                "body text, and often does not."
            ),
            evidence=(),
        )
    ]


ATS_RULES: tuple[Rule, ...] = (
    Rule("ats_no_contact_channel", "the resume states at least one contact channel", _check_no_contact_channel),
    Rule("ats_contact_channel_gap", "resume contact channels vs the profile's", _check_contact_channel_gap),
    Rule("ats_headline_uses_separators", "the headline holds a single title", _check_headline_uses_separators),
    Rule("ats_headline_not_a_job_title", "every headline segment is a job title", _check_headline_not_a_job_title),
    Rule("ats_headline_absent", "the resume states a headline", _check_headline_absent),
)


def _check_all(context: AuditContext) -> list[Finding]:
    return run_rule_set(context, ATS_RULES)


ATS_REVIEWER = Reviewer(
    mode="ats",
    title="ATS parser and recruiter reachability",
    check=_check_all,
)
