"""Deterministic resume claim extraction (Candidate Intelligence, Stage 0 / T1).

The parser itself is read-only: :func:`parse_resume` touches no file, and it has
no LLM, no network call and no randomness -- the same text in gives the same
claims out. The one write path in this module is the CLI handler
:func:`candidate_kb_extract_cmd`, added in T2: it merges the extraction into the
knowledge base, and only when ``--dry-run`` is absent. The resume, the candidate
profile and the database are never written by anything here.

Design rules the tests pin:

* **Every claim carries evidence.** The source file, the 1-based line number,
  and the exact substring of that line the claim was derived from. The quote is
  always a literal substring of the cited line, so a claim cannot outrun its
  source -- that is the structural half of "do not invent experience".
* **``normalized`` is filled only when the mapping is unambiguous**: a month
  name to ``YYYY-MM``, a known language to its ISO 639-1 code, a country to its
  ISO 3166-1 alpha-2 code, an email to lower case. It stays ``None`` everywhere
  else.
* **A total stated in prose is never turned into a number.** The resume says
  "15+ years of IT experience"; that survives as ``experience_statement`` with
  ``normalized=None``. Equating a total tenure with tenure in a single
  technology is exactly the inference T1 forbids.
* **Nothing is invented.** If the resume does not say it, there is no claim.
  Where a category is empty (this resume states no quantified achievement) the
  result is an empty category, not a plausible-looking guess.
* **Bullets stay bound to their role.** Every ``duty``/``achievement`` carries
  the ``claim_id`` of the ``role`` claim it was listed under.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "STATUS_DERIVED_FROM_SOURCE",
    "Evidence",
    "ResumeClaim",
    "candidate_kb_extract_cmd",
    "format_claims",
    "make_claim_id",
    "parse_resume",
    "resolve_resume_path",
]

STATUS_DERIVED_FROM_SOURCE = "DERIVED_FROM_SOURCE"

#: Field separator for the content digest behind a claim id. A unit separator is
#: used rather than a printable character so that no value can contain it and
#: two different (kind, scope, value) triples cannot collide by concatenation.
CLAIM_ID_SEPARATOR = "\x1f"


def make_claim_id(kind: str, scope: str, value: str) -> str:
    """The content-derived claim id, shared by every extractor.

    Extracted from :class:`_IdFactory` in T5 so the project catalogue can mint
    ids the same way. Two extractors that disagree about how an id is built
    would produce two records for one fact, and the KB would grow a duplicate
    instead of refreshing the original.
    """
    digest = hashlib.sha256(
        f"{kind}{CLAIM_ID_SEPARATOR}{scope}{CLAIM_ID_SEPARATOR}{value}".encode()
    ).hexdigest()[:10]
    return f"{kind}-{digest}"


# Resume discovery mirrors job_analyzer.RESUME_CANDIDATES. Kept local so this
# module stays import-light (job_analyzer pulls in pydantic); a test asserts the
# two lists are identical, so they cannot drift apart silently.
FALLBACK_RESUME_CANDIDATES: tuple[Path, ...] = (
    Path("resume.md"),
    Path("RESUME.md"),
    Path("Resume.md"),
    Path("ai_assistant/resume.md"),
    Path("ai_assistant/RESUME.md"),
    Path("../resume.md"),
)

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# Only mappings that are fixed and unambiguous. Anything not listed normalises
# to None rather than to a guess: "Moscow" is not silently rewritten to "RU".
LANGUAGE_ISO = {"russian": "ru", "english": "en"}
COUNTRY_ISO = {"thailand": "TH"}

WORK_MODE_MARKERS = {"remote", "hybrid", "on-site", "onsite", "office", "relocation"}

# An achievement has to be *marked* in the source, not inferred from tone.
# Either a quantified outcome, or a bullet that opens with an outcome verb.
# "Automated ..." and "Built ..." are deliberately absent: those describe the
# job, not a result, and promoting them would manufacture achievements.
ACHIEVEMENT_VERBS = frozenset(
    {
        "achieved",
        "accelerated",
        "boosted",
        "cut",
        "doubled",
        "eliminated",
        "grew",
        "improved",
        "increased",
        "reduced",
        "saved",
        "tripled",
    }
)
ACHIEVEMENT_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
ACHIEVEMENT_MULTIPLIER_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:x|×)\b", re.IGNORECASE)

H1_RE = re.compile(r"^#\s+(?P<name>.+?)\s*$")
H2_RE = re.compile(r"^##\s+(?P<section>.+?)\s*$")
H3_RE = re.compile(r"^###\s+(?P<heading>.+?)\s*$")
BOLD_RE = re.compile(r"^\*\*(?P<body>.+?)\*\*\s*$")
BULLET_LABEL_RE = re.compile(r"^\*\*(?P<label>[^*]+?):?\*\*\s*:?\s*(?P<rest>.*)$")
ITALIC_RE = re.compile(r"^\*(?P<body>.+?)\*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MONTH_YEAR_RE = re.compile(r"^(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})$")
YEAR_RE = re.compile(r"^\d{4}$")
DATE_RANGE_SPLIT_RE = re.compile(r"\s+-\s+")
EXPERIENCE_STATEMENT_RE = re.compile(
    r"\d+\+?\s*years?\s+of\s+[A-Za-z][A-Za-z\s]*?experience",
    re.IGNORECASE,
)
EDUCATION_RE = re.compile(r"^\*\*(?P<institution>[^*]+?)\*\*\s*[—–-]\s*(?P<rest>.+)$")
LANGUAGE_LABEL_RE = re.compile(r"^\*\*(?P<label>languages?):?\*\*\s*:?\s*(?P<rest>.+)$", re.IGNORECASE)

_SECTION_OF = {
    "name": "Identity",
    "headline": "Identity",
    "title": "Identity",
    "contact_location": "Contacts",
    "contact_email": "Contacts",
    "contact_telegram": "Contacts",
    "contact_other": "Contacts",
    "work_mode": "Contacts",
    "summary": "Summary",
    "experience_statement": "Summary",
    "skill_group": "Skills",
    "skill": "Skills",
    "education_institution": "Education",
    "education_program": "Education",
    "education_level": "Education",
    "education_year": "Education",
    "education_note": "Education",
    "language": "Languages",
    "language_level": "Languages",
}


@dataclass(frozen=True)
class Evidence:
    """Where a claim came from, precise enough to re-check by hand."""

    source_file: str
    line: int
    quote: str

    def to_dict(self) -> dict[str, object]:
        return {"source_file": self.source_file, "line": self.line, "quote": self.quote}


@dataclass(frozen=True)
class ResumeClaim:
    """One fact read off the resume, with its provenance.

    ``role_id`` binds duties/achievements and the parts of a role heading to the
    ``role`` claim they belong to. ``parent_id`` binds finer groupings (a skill
    to its skill group, a language level to its language).
    """

    claim_id: str
    kind: str
    value: str
    status: str
    evidence: Evidence
    normalized: str | None = None
    role_id: str | None = None
    parent_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind,
            "value": self.value,
            "normalized": self.normalized,
            "status": self.status,
            "evidence": self.evidence.to_dict(),
            "role_id": self.role_id,
            "parent_id": self.parent_id,
        }


class _IdFactory:
    """Stable, content-derived claim ids.

    The id is a digest of (kind, scope, value), so re-parsing the same resume
    yields the same ids and moving an unrelated line does not renumber anything.
    A repeated (kind, scope, value) triple gets a numeric suffix, assigned in
    document order.
    """

    def __init__(self) -> None:
        self._used: set[str] = set()

    def make(self, kind: str, scope: str, value: str) -> str:
        base = make_claim_id(kind, scope, value)
        candidate = base
        suffix = 1
        while candidate in self._used:
            suffix += 1
            candidate = f"{base}-{suffix}"
        self._used.add(candidate)
        return candidate


class _Builder:
    """Collects claims in document order while handing out stable ids."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.ids = _IdFactory()
        self.claims: list[ResumeClaim] = []

    def add(
        self,
        kind: str,
        value: str,
        line: int,
        quote: str,
        *,
        normalized: str | None = None,
        role_id: str | None = None,
        parent_id: str | None = None,
    ) -> ResumeClaim:
        scope = role_id or parent_id or ""
        claim = ResumeClaim(
            claim_id=self.ids.make(kind, scope, value),
            kind=kind,
            value=value,
            status=STATUS_DERIVED_FROM_SOURCE,
            evidence=Evidence(self.source, line, quote),
            normalized=normalized,
            role_id=role_id,
            parent_id=parent_id,
        )
        self.claims.append(claim)
        return claim


def normalize_date(raw: str) -> str | None:
    """``May 2026`` -> ``2026-05``, ``2009`` -> ``2009``, ``Present`` -> ``None``.

    ``Present`` has no date to normalise to; returning a sentinel date would
    invent one.
    """
    text = raw.strip()
    month_year = MONTH_YEAR_RE.match(text)
    if month_year:
        month = MONTHS.get(month_year.group("month").lower())
        if month is None:
            return None
        return f"{month_year.group('year')}-{month:02d}"
    if YEAR_RE.match(text):
        return text
    return None


def _is_achievement(bullet: str) -> bool:
    if ACHIEVEMENT_PERCENT_RE.search(bullet) or ACHIEVEMENT_MULTIPLIER_RE.search(bullet):
        return True
    first_word = re.split(r"[\s,]+", bullet.strip().lower(), maxsplit=1)[0]
    return first_word in ACHIEVEMENT_VERBS


def _add_header(builder: _Builder, header_lines: list[str]) -> None:
    for offset, raw in enumerate(header_lines):
        line_no = offset + 1
        text = raw.strip()
        if not text:
            continue
        name = H1_RE.match(text)
        if name:
            builder.add("name", name.group("name"), line_no, name.group("name"))
            continue
        bold = BOLD_RE.match(text)
        if bold:
            headline = bold.group("body").strip()
            builder.add("headline", headline, line_no, headline)
            for title in (part.strip() for part in headline.split("|")):
                if title:
                    builder.add("title", title, line_no, title)
            continue
        if "|" in text or EMAIL_RE.match(text):
            _add_contacts(builder, text, line_no)


def _add_contacts(builder: _Builder, line: str, line_no: int) -> None:
    for segment in line.split("|"):
        text = segment.strip()
        if not text:
            continue
        lowered = text.lower()
        if EMAIL_RE.match(text):
            builder.add("contact_email", text, line_no, text, normalized=lowered)
        elif lowered.startswith("telegram:"):
            handle = text.split(":", 1)[1].strip()
            builder.add("contact_telegram", text, line_no, text, normalized=handle or None)
        elif lowered in WORK_MODE_MARKERS:
            builder.add("work_mode", text, line_no, text)
        elif lowered in COUNTRY_ISO:
            builder.add("contact_location", text, line_no, text, normalized=COUNTRY_ISO[lowered])
        else:
            builder.add("contact_other", text, line_no, text)


def _add_role(builder: _Builder, heading: str, line_no: int) -> str:
    parts = [part.strip() for part in heading.split("|")]
    title = parts[0] if parts else heading
    company = parts[1] if len(parts) > 1 else ""
    role = builder.add("role", heading, line_no, heading)
    if title:
        builder.add("role_title", title, line_no, title, role_id=role.claim_id)
    if company:
        builder.add("role_company", company, line_no, company, role_id=role.claim_id)
        if ";" in company:
            for employer in (item.strip() for item in company.split(";")):
                if employer:
                    builder.add("role_employer", employer, line_no, employer, role_id=role.claim_id)
    return role.claim_id


def _add_role_meta(builder: _Builder, role_id: str, meta: str, line_no: int) -> None:
    parts = [part.strip() for part in meta.split("|")]
    date_part = ""
    if len(parts) >= 2:
        location = parts[0]
        date_part = parts[1]
        if location:
            builder.add(
                "role_location",
                location,
                line_no,
                location,
                normalized=COUNTRY_ISO.get(location.lower()),
                role_id=role_id,
            )
    elif parts:
        date_part = parts[0]
    if not date_part:
        return
    bounds = [part.strip() for part in DATE_RANGE_SPLIT_RE.split(date_part) if part.strip()]
    if len(bounds) == 2:
        start_raw, end_raw = bounds[0], bounds[1]
    elif len(bounds) == 1:
        start_raw, end_raw = bounds[0], ""
    else:
        return
    if start_raw:
        builder.add(
            "role_start",
            start_raw,
            line_no,
            start_raw,
            normalized=normalize_date(start_raw),
            role_id=role_id,
        )
    if end_raw:
        builder.add(
            "role_end",
            end_raw,
            line_no,
            end_raw,
            normalized=normalize_date(end_raw),
            role_id=role_id,
        )


def _add_skill_bullet(builder: _Builder, bullet: str, line_no: int) -> None:
    labelled = BULLET_LABEL_RE.match(bullet)
    if not labelled:
        builder.add("skill", bullet, line_no, bullet)
        return
    group = labelled.group("label").strip().rstrip(":").strip()
    group_claim = builder.add("skill_group", group, line_no, group)
    for item in (part.strip() for part in labelled.group("rest").split(",")):
        if item:
            builder.add("skill", item, line_no, item, parent_id=group_claim.claim_id)


def _add_role_bullet(builder: _Builder, role_id: str, bullet: str, line_no: int) -> None:
    kind = "achievement" if _is_achievement(bullet) else "duty"
    builder.add(kind, bullet, line_no, bullet, role_id=role_id)


def _add_education(builder: _Builder, institution: str, rest: str, line_no: int) -> None:
    builder.add("education_institution", institution, line_no, institution)
    segments = [part.strip() for part in rest.split("|")]
    program = segments[0] if segments else ""
    if program:
        builder.add("education_program", program, line_no, program)
    if len(segments) < 2:
        return
    pieces = [part.strip() for part in segments[-1].split(",") if part.strip()]
    if not pieces:
        return
    year = pieces[-1]
    level = ", ".join(pieces[:-1])
    if level:
        builder.add("education_level", level, line_no, level)
    if year:
        builder.add(
            "education_year",
            year,
            line_no,
            year,
            normalized=year if YEAR_RE.match(year) else None,
        )


def _add_languages(builder: _Builder, rest: str, line_no: int) -> None:
    for segment in rest.split("|"):
        text = segment.strip()
        if not text:
            continue
        parts = [part.strip() for part in DATE_RANGE_SPLIT_RE.split(text) if part.strip()]
        name = parts[0] if parts else text
        level = parts[1] if len(parts) > 1 else ""
        claim = builder.add(
            "language",
            name,
            line_no,
            name,
            normalized=LANGUAGE_ISO.get(name.lower()),
        )
        if level:
            builder.add("language_level", level, line_no, level, parent_id=claim.claim_id)


def _add_summary(builder: _Builder, sentence: str, line_no: int) -> None:
    builder.add("summary", sentence, line_no, sentence)
    statement = EXPERIENCE_STATEMENT_RE.search(sentence)
    if not statement:
        return
    fragment = statement.group(0)
    # Deliberately unnormalised: see the module docstring.
    builder.add("experience_statement", fragment, line_no, fragment)


def _add_education_or_language(builder: _Builder, bullet: str, line_no: int) -> None:
    language = LANGUAGE_LABEL_RE.match(bullet)
    if language:
        _add_languages(builder, language.group("rest"), line_no)
        return
    education = EDUCATION_RE.match(bullet)
    if education:
        _add_education(builder, education.group("institution").strip(), education.group("rest").strip(), line_no)
        return
    builder.add("education_note", bullet, line_no, bullet)


def _parse_sections(builder: _Builder, lines: list[str], start: int) -> None:
    section = ""
    role_id: str | None = None
    awaiting_role_meta = False
    awaiting_summary = False

    for index in range(start, len(lines)):
        raw = lines[index]
        line_no = index + 1
        text = raw.strip()
        if not text:
            continue

        if not text.startswith("###"):
            heading = H2_RE.match(text)
            if heading:
                section = heading.group("section").strip()
                role_id = None
                awaiting_role_meta = False
                awaiting_summary = section.lower().startswith("summary")
                continue

        heading = H3_RE.match(text)
        if heading:
            role_id = _add_role(builder, heading.group("heading"), line_no)
            awaiting_role_meta = True
            awaiting_summary = False
            continue

        if awaiting_summary:
            _add_summary(builder, text, line_no)
            awaiting_summary = False
            continue

        if awaiting_role_meta:
            meta = ITALIC_RE.match(text)
            if meta and role_id:
                _add_role_meta(builder, role_id, meta.group("body").strip(), line_no)
                awaiting_role_meta = False
                continue
            awaiting_role_meta = False

        if not text.startswith("- "):
            continue
        bullet = text[2:].strip()
        lowered_section = section.lower()
        if lowered_section.startswith("core skills"):
            _add_skill_bullet(builder, bullet, line_no)
        elif lowered_section.startswith("professional experience"):
            if role_id:
                _add_role_bullet(builder, role_id, bullet, line_no)
        elif lowered_section.startswith("education"):
            _add_education_or_language(builder, bullet, line_no)


def parse_resume(text: str, source: str) -> list[ResumeClaim]:
    """Extract every claim the resume states, in document order.

    Returns ``[]`` for empty or whitespace-only input: an empty resume states
    nothing, which is not an error. Missing *files* are an error, and that is
    reported by the CLI layer, not here.
    """
    lines = text.splitlines()
    builder = _Builder(source)
    if not any(line.strip() for line in lines):
        return []

    body_start = len(lines)
    for index, raw in enumerate(lines):
        if not raw.strip().startswith("###") and H2_RE.match(raw.strip()):
            body_start = index
            break

    _add_header(builder, lines[:body_start])
    _parse_sections(builder, lines, body_start)
    return builder.claims


def format_claims(claims: list[ResumeClaim], source_label: str | None = None) -> str:
    """Render claims grouped by section, every line pointing back at its source.

    Pure formatting: this writes nothing and says nothing about writes. The
    command handler owns the note about whether a knowledge base was touched.
    """
    out: list[str] = [f"candidate-kb extract — {source_label or '?'}: {len(claims)} claims"]
    role_labels = {claim.claim_id: claim.value for claim in claims if claim.kind == "role"}
    current_key: tuple[str, str] | None = None

    for claim in claims:
        role_key = claim.claim_id if claim.kind == "role" else claim.role_id
        if role_key:
            key = ("role", role_key)
            header = f"Role: {role_labels.get(role_key, role_key)}"
        else:
            key = ("section", _SECTION_OF.get(claim.kind, "Other"))
            header = key[1]
        if key != current_key:
            out.append("")
            out.append(f"[{header}]")
            current_key = key

        value = claim.value if len(claim.value) <= 66 else claim.value[:63] + "..."
        extras: list[str] = []
        if claim.normalized:
            extras.append(f"normalized={claim.normalized}")
        if claim.parent_id:
            extras.append(f"parent={claim.parent_id}")
        suffix = "  " + " ".join(extras) if extras else ""
        out.append(
            f"  {claim.kind:<20} {value:<68} {claim.evidence.source_file}:{claim.evidence.line}{suffix}"
        )

    out.append("")
    out.append("read-only view: formatting only, nothing was written.")
    return "\n".join(out)


def resolve_resume_path(explicit: str | None = None) -> Path | None:
    """Find the resume file, or ``None`` so the caller can report an explicit error."""
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None
    try:
        from ..job_analyzer import RESUME_CANDIDATES as candidates
    except ImportError:
        candidates = FALLBACK_RESUME_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def candidate_kb_extract_cmd(
    resume_path: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
    kb_path: str | None = None,
) -> int:
    """``candidate-kb extract`` handler.

    Parses the resume, prints the claims, and -- unless ``--dry-run`` is given --
    merges them into the knowledge base. Only the knowledge base is written; the
    resume, the candidate profile and the database are never touched.

    The merge happens before anything is printed, so ``--json`` stays pure JSON:
    the merge outcome travels inside the payload rather than as trailing lines.
    """
    path = resolve_resume_path(resume_path)
    if path is None:
        looked_in = ", ".join(str(item) for item in FALLBACK_RESUME_CANDIDATES)
        print(f"ERROR: resume file not found (looked in: {looked_in})", file=sys.stderr)
        print("Pass --resume PATH to point at the file explicitly.", file=sys.stderr)
        return 2
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"ERROR: cannot read resume file {path}: {exc}", file=sys.stderr)
        return 2
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"ERROR: {path} is not valid UTF-8: {exc}", file=sys.stderr)
        return 2

    claims = parse_resume(text, str(path))

    merge_report = None
    kb_file = None
    if not dry_run:
        from .kb import CandidateKnowledgeBase, resolve_kb_path

        kb_file = resolve_kb_path(kb_path)
        knowledge_base = CandidateKnowledgeBase.load(kb_file)
        merge_report = knowledge_base.merge_claims(
            claims,
            source_file=str(path),
            source_sha256=hashlib.sha256(raw).hexdigest(),
            now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        knowledge_base.save(kb_file)

    if as_json:
        payload: dict[str, object] = {
            "source": str(path),
            "claim_count": len(claims),
            "claims": [claim.to_dict() for claim in claims],
        }
        if merge_report is not None and kb_file is not None:
            payload["knowledge_base"] = {
                "path": str(kb_file),
                "kb_version": merge_report.kb_version,
                "added": merge_report.added,
                "refreshed": merge_report.refreshed,
                "marked_absent": merge_report.marked_absent,
                "still_absent": merge_report.still_absent,
                "source_sha256_changed": merge_report.source_sha256_changed,
            }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(format_claims(claims, str(path)))
    print()
    if merge_report is None:
        print("read-only: --dry-run was given, so nothing was written.")
    else:
        print(f"knowledge base : {kb_file}")
        print(f"merge          : {merge_report.summary()}")
    return 0
