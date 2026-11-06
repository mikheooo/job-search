"""Candidate knowledge base: storage, merge and status transitions (Stage 0 / T2).

The KB is where a fact stops being "something the parser read" and becomes
"something the candidate has confirmed". The design exists to keep that
distinction honest:

* A claim arrives as ``DERIVED_FROM_SOURCE``. It reaches ``CONFIRMED_BY_USER``
  only through an explicit :meth:`CandidateKnowledgeBase.confirm`, and
  ``REJECTED_BY_USER`` only through an explicit :meth:`CandidateKnowledgeBase.reject`.
  **Nothing in the merge path touches a status.** That is the "no auto-promotion"
  invariant, and a test fails if it is ever broken.
* **Merging never deletes.** A claim that disappears from the source (the resume
  was edited) keeps its record, its status and its version history; it is only
  marked ``in_source = False``. Dropping a confirmed fact because a file changed
  would be worse than carrying a stale one.
* ``claim_id`` is content-derived (T1), so editing the resume produces a **new**
  id. The merge therefore sees a new, unconfirmed claim. It cannot inherit the
  old confirmation, because that confirmation was about the old text.

This module is storage and policy only: it never prints. The command layer lives
in ``confirm.py``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resume_parser import ResumeClaim

__all__ = [
    "DEFAULT_KB_PATH",
    "INTERVIEW_SOURCE",
    "KB_PATH_ENV",
    "PROJECT_SOURCE",
    "CandidateKnowledgeBase",
    "ClaimRecord",
    "ClaimStatus",
    "MergeReport",
    "ProjectSyncReport",
    "is_document_sourced",
    "is_interview_sourced",
    "is_project_sourced",
    "resolve_kb_path",
]


#: ``evidence.source_file`` for a claim the interview established (T4.5). A claim
#: carries this string instead of a path, which is what marks its provenance as
#: "the candidate said so" rather than "a document said so".
INTERVIEW_SOURCE = "interview"

#: ``evidence.source_file`` for a claim read out of one of the candidate's own
#: repositories (T5). Like the interview marker this is not a path, so it cannot
#: be confused with a resume claim; the real file it came from is kept in
#: ``evidence["source_path"]`` together with the line, the quote and the sha256,
#: which is what makes a project claim checkable by hand.
PROJECT_SOURCE = "project"


def is_interview_sourced(record: ClaimRecord) -> bool:
    """Whether a claim came from the interview rather than from a document."""
    return str(record.evidence.get("source_file") or "") == INTERVIEW_SOURCE


def is_project_sourced(record: ClaimRecord) -> bool:
    """Whether a claim came from the project catalogue rather than a document."""
    return str(record.evidence.get("source_file") or "") == PROJECT_SOURCE


def is_document_sourced(record: ClaimRecord) -> bool:
    """Whether a claim belongs to a document, so a merge may mark it absent.

    The two markers above are the only non-document origins. Everything else is
    a claim about a file that a re-extraction is entitled to refresh -- and, if
    the words are gone from that file, to flag as no longer in the source.
    """
    return not (is_interview_sourced(record) or is_project_sourced(record))


class ClaimStatus(StrEnum):
    """The only three states a claim can be in.

    There is no "probably true" and no score: a fact is either read from the
    source, confirmed by the candidate, or rejected by the candidate.
    """

    DERIVED_FROM_SOURCE = "DERIVED_FROM_SOURCE"
    CONFIRMED_BY_USER = "CONFIRMED_BY_USER"
    REJECTED_BY_USER = "REJECTED_BY_USER"


# Repository root, derived from this file rather than from the working
# directory: cron, the CLI and the tests all run from different places, and the
# knowledge base must be the same file for all of them. config.py is not used
# here because it currently carries unrelated uncommitted changes.
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_KB_PATH = PROJECT_ROOT / "candidate_kb.json"
KB_PATH_ENV = "CANDIDATE_KB_FILE"

KB_FORMAT_VERSION = 1


def resolve_kb_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Explicit path wins, then ``CANDIDATE_KB_FILE``, then the repo root."""
    if explicit:
        return Path(explicit)
    from_env = os.getenv(KB_PATH_ENV)
    if from_env:
        return Path(from_env)
    return DEFAULT_KB_PATH


@dataclass
class ClaimRecord:
    """One claim as the knowledge base holds it.

    ``evidence`` is kept as a plain dict so the KB round-trips through JSON
    without the storage layer depending on the parser.
    """

    claim_id: str
    kind: str
    value: str
    status: str = ClaimStatus.DERIVED_FROM_SOURCE
    normalized: str | None = None
    role_id: str | None = None
    parent_id: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    in_source: bool = True
    first_seen_version: int = 0
    last_seen_version: int = 0
    confirmed_version: int | None = None
    rejected_version: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind,
            "value": self.value,
            "normalized": self.normalized,
            "status": self.status,
            "role_id": self.role_id,
            "parent_id": self.parent_id,
            "evidence": dict(self.evidence),
            "in_source": self.in_source,
            "first_seen_version": self.first_seen_version,
            "last_seen_version": self.last_seen_version,
            "confirmed_version": self.confirmed_version,
            "rejected_version": self.rejected_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ClaimRecord:
        return cls(
            claim_id=str(data["claim_id"]),
            kind=str(data["kind"]),
            value=str(data["value"]),
            status=str(data.get("status") or ClaimStatus.DERIVED_FROM_SOURCE),
            normalized=_opt_str(data.get("normalized")),
            role_id=_opt_str(data.get("role_id")),
            parent_id=_opt_str(data.get("parent_id")),
            evidence=dict(data.get("evidence") or {}),
            in_source=bool(data.get("in_source", True)),
            first_seen_version=int(data.get("first_seen_version") or 0),
            last_seen_version=int(data.get("last_seen_version") or 0),
            confirmed_version=_opt_int(data.get("confirmed_version")),
            rejected_version=_opt_int(data.get("rejected_version")),
        )

    def line_ref(self) -> str:
        source = self.evidence.get("source_file") or "?"
        line = self.evidence.get("line")
        return f"{source}:{line}" if line is not None else str(source)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[arg-type]


@dataclass
class MergeReport:
    """What one merge actually did, so the CLI can print it without guessing."""

    previous_version: int
    kb_version: int
    added: int
    refreshed: int
    marked_absent: int
    still_absent: int
    source_sha256_changed: bool
    #: Claims the merge recognised as belonging to another source (the interview)
    #: and deliberately left alone. Counted rather than silently skipped: an
    #: invisible skip is indistinguishable from a bug.
    skipped_foreign: int = 0
    #: Claims from the project catalogue (T5), left alone for the same reason but
    #: counted apart. Folding them into ``skipped_foreign`` would make its
    #: message -- which says "interview fact(s)" -- quietly untrue.
    skipped_other_origin: int = 0

    def summary(self) -> str:
        parts = [
            f"kb_version {self.previous_version} -> {self.kb_version}",
            f"added {self.added}",
            f"refreshed {self.refreshed}",
            f"marked absent {self.marked_absent}",
            f"still absent {self.still_absent}",
        ]
        if self.source_sha256_changed:
            parts.append("source sha256 changed")
        if self.skipped_foreign:
            parts.append(f"left alone {self.skipped_foreign} interview fact(s)")
        if self.skipped_other_origin:
            parts.append(f"left alone {self.skipped_other_origin} claim(s) from other origins")
        return ", ".join(parts)


@dataclass
class ProjectSyncReport:
    """What one project-catalogue sync did. Nothing else was touched."""

    previous_version: int
    kb_version: int
    added: int
    refreshed: int
    unchanged: int
    #: Project claims the catalogue no longer produces. Marked ``in_source =
    #: False`` and kept, exactly as a resume merge treats a vanished line.
    marked_absent: int = 0
    touched: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"kb_version {self.previous_version} -> {self.kb_version}, "
            f"added {self.added}, refreshed {self.refreshed}, "
            f"unchanged {self.unchanged}, marked absent {self.marked_absent}"
        )


class CandidateKnowledgeBase:
    """An in-memory knowledge base, loaded from and saved to one JSON file."""

    def __init__(
        self,
        *,
        kb_version: int = 0,
        source_file: str | None = None,
        source_sha256: str | None = None,
        updated_at: str | None = None,
        claims: dict[str, ClaimRecord] | None = None,
        preferences: dict[str, object] | None = None,
    ) -> None:
        self.kb_version = kb_version
        self.source_file = source_file
        self.source_sha256 = source_sha256
        self.updated_at = updated_at
        self.claims: dict[str, ClaimRecord] = claims if claims is not None else {}
        # Search preferences (T8): which roles to look for, what to exclude, the
        # salary floor, contact details the resume does not carry.
        #
        # They are a separate section rather than claims on purpose. A claim is
        # something read out of a source and checkable against it; a preference
        # is a decision that no source contains, so it has no evidence, no
        # `in_source` flag and nothing for a resume merge to refresh or mark
        # absent. Storing them as claims would drag them into every
        # `merge_claims` invariant, where the answer to "is this still in the
        # resume?" is always "it never was".
        self.preferences: dict[str, object] = dict(preferences or {})

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> CandidateKnowledgeBase:
        """Read the KB. A missing file is an empty KB, not an error."""
        kb_path = Path(path)
        if not kb_path.is_file():
            return cls()
        data = json.loads(kb_path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CandidateKnowledgeBase:
        raw_claims = data.get("claims") or {}
        claims = {
            str(claim_id): ClaimRecord.from_dict(record)
            for claim_id, record in dict(raw_claims).items()  # type: ignore[arg-type]
        }
        return cls(
            kb_version=int(data.get("kb_version") or 0),
            source_file=_opt_str(data.get("source_file")),
            source_sha256=_opt_str(data.get("source_sha256")),
            updated_at=_opt_str(data.get("updated_at")),
            claims=claims,
            # Absent in a KB written before T8. That is not an error and must not
            # be one: an old file simply has no preferences recorded yet.
            preferences=dict(data.get("preferences") or {}),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_format_version": KB_FORMAT_VERSION,
            "kb_version": self.kb_version,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "updated_at": self.updated_at,
            "claims": {claim_id: record.to_dict() for claim_id, record in self.claims.items()},
            "preferences": self.preferences,
        }

    def save(self, path: str | os.PathLike[str]) -> None:
        """Write the KB atomically.

        A half-written knowledge base would silently drop confirmations, so the
        text lands in a sibling temp file and is renamed over the target.
        """
        kb_path = Path(path)
        text = json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        temp_path = kb_path.with_suffix(kb_path.suffix + ".tmp")
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, kb_path)

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def merge_claims(
        self,
        claims: list[ResumeClaim],
        *,
        source_file: str,
        source_sha256: str | None = None,
        now: str | None = None,
    ) -> MergeReport:
        """Fold a fresh extraction into the KB without ever touching a status.

        Claims that are no longer in the source are kept and marked
        ``in_source = False``; they are never removed and their status is never
        changed. A claim whose text changed gets a new ``claim_id`` from the
        parser, so it arrives as a new unconfirmed claim rather than silently
        inheriting an old confirmation.
        """
        previous_version = self.kb_version
        new_version = previous_version + 1
        sha_changed = bool(
            self.source_sha256 and source_sha256 and self.source_sha256 != source_sha256
        )

        incoming: dict[str, ResumeClaim] = {}
        for claim in claims:
            incoming[claim.claim_id] = claim

        added = 0
        refreshed = 0
        skipped_foreign = 0
        skipped_other_origin = 0
        for claim_id, claim in incoming.items():
            evidence = claim.evidence.to_dict()
            existing = self.claims.get(claim_id)
            if existing is not None and not is_document_sourced(existing):
                # A fact the interview established, or one read out of the
                # candidate's own repository, is not this merge's to refresh,
                # even if the resume happens to contain the same words.
                # Refreshing would replace its evidence with resume provenance,
                # and the confirmation it carries would then look like it came
                # from the parser -- which is exactly the confusion the
                # interview exists to avoid.
                if is_interview_sourced(existing):
                    skipped_foreign += 1
                else:
                    skipped_other_origin += 1
                continue
            if existing is None:
                self.claims[claim_id] = ClaimRecord(
                    claim_id=claim_id,
                    kind=claim.kind,
                    value=claim.value,
                    normalized=claim.normalized,
                    role_id=claim.role_id,
                    parent_id=claim.parent_id,
                    evidence=evidence,
                    in_source=True,
                    first_seen_version=new_version,
                    last_seen_version=new_version,
                )
                added += 1
                continue
            existing.kind = claim.kind
            existing.value = claim.value
            existing.normalized = claim.normalized
            existing.role_id = claim.role_id
            existing.parent_id = claim.parent_id
            existing.evidence = evidence
            existing.in_source = True
            existing.last_seen_version = new_version
            # existing.status is deliberately not assigned here.
            refreshed += 1

        marked_absent = 0
        still_absent = 0
        for claim_id, record in self.claims.items():
            if claim_id in incoming:
                continue
            if not is_document_sourced(record):
                # Not "absent from the source". The resume never had this fact to
                # lose, and marking it absent would make every downstream filter
                # that reads ``in_source`` quietly drop a confirmed answer or a
                # project the candidate actually built.
                if is_interview_sourced(record):
                    skipped_foreign += 1
                else:
                    skipped_other_origin += 1
                continue
            if record.in_source:
                record.in_source = False
                marked_absent += 1
            else:
                still_absent += 1

        self.kb_version = new_version
        self.source_file = source_file
        self.source_sha256 = source_sha256
        self.updated_at = now

        return MergeReport(
            previous_version=previous_version,
            kb_version=new_version,
            added=added,
            refreshed=refreshed,
            marked_absent=marked_absent,
            still_absent=still_absent,
            source_sha256_changed=sha_changed,
            skipped_foreign=skipped_foreign,
            skipped_other_origin=skipped_other_origin,
        )

    def sync_project_claims(
        self,
        records: list[ClaimRecord],
        *,
        now: str | None = None,
    ) -> ProjectSyncReport:
        """Fold the project catalogue in: add what is new, refresh what is not.

        Deliberately **not** ``merge_claims``. A merge answers "what does this
        document say now?" and marks anything it did not see as gone. This sync
        answers "which projects do we present?" -- a narrower question with the
        same consequence: a claim the catalogue no longer produces *is* gone
        from its source, so it is marked ``in_source = False``. It is never
        deleted and its status is never changed, so a project dropped from the
        catalogue can be brought back by putting the anchor back.

        That marking is what makes a changed quote safe. Without it, editing one
        line of a README would leave the old claim and the new claim both bound
        to the same project, and the resume would print the same fact twice.

        Takes :class:`ClaimRecord` rather than ``ResumeClaim`` because a project
        claim carries provenance a resume claim has no room for -- the real file
        it came from, the project it belongs to and that file's sha256.

        Only project-origin claims are touched. A status is never changed here:
        a claim arrives ``DERIVED_FROM_SOURCE`` and reaches ``CONFIRMED_BY_USER``
        only through :meth:`confirm`.
        """
        previous_version = self.kb_version
        added = 0
        refreshed = 0
        unchanged = 0
        touched: list[str] = []
        incoming = {record.claim_id for record in records}

        for record in records:
            evidence = dict(record.evidence)
            existing = self.claims.get(record.claim_id)
            if existing is None:
                new_version = self.kb_version + 1
                self.claims[record.claim_id] = ClaimRecord(
                    claim_id=record.claim_id,
                    kind=record.kind,
                    value=record.value,
                    # Forced, not copied: a catalogue claim is read from a file
                    # and is not confirmed by anybody on arrival.
                    status=ClaimStatus.DERIVED_FROM_SOURCE,
                    normalized=record.normalized,
                    role_id=record.role_id,
                    parent_id=record.parent_id,
                    evidence=evidence,
                    in_source=True,
                    first_seen_version=new_version,
                    last_seen_version=new_version,
                )
                self.kb_version = new_version
                added += 1
                touched.append(record.claim_id)
                continue

            if not is_project_sourced(existing):
                # A resume claim or an interview answer happens to hash to the
                # same id. Refreshing it would relabel somebody else's fact.
                continue

            if existing.evidence == evidence:
                unchanged += 1
                continue

            existing.kind = record.kind
            existing.value = record.value
            existing.normalized = record.normalized
            existing.role_id = record.role_id
            existing.parent_id = record.parent_id
            existing.evidence = evidence
            existing.in_source = True
            existing.last_seen_version = self.kb_version
            # existing.status is deliberately not assigned here.
            refreshed += 1
            touched.append(record.claim_id)

        if added or refreshed:
            self.updated_at = now

        marked_absent = 0
        for claim_id, record in self.claims.items():
            if claim_id in incoming or not is_project_sourced(record):
                continue
            if record.in_source:
                record.in_source = False
                marked_absent += 1

        return ProjectSyncReport(
            previous_version=previous_version,
            kb_version=self.kb_version,
            added=added,
            refreshed=refreshed,
            unchanged=unchanged,
            marked_absent=marked_absent,
            touched=tuple(touched),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get(self, claim_id: str) -> ClaimRecord | None:
        return self.claims.get(claim_id)

    def by_status(self, status: str | None = None) -> list[ClaimRecord]:
        """Records for one status, or all of them, in a stable order."""
        records = [
            record
            for record in self.claims.values()
            if status is None or record.status == status
        ]
        return sorted(records, key=lambda record: (record.kind, record.value, record.claim_id))

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ClaimStatus}
        for record in self.claims.values():
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Transitions -- explicit only
    # ------------------------------------------------------------------

    def confirm(self, claim_id: str) -> ClaimRecord:
        """Mark a claim as confirmed by the candidate.

        Idempotent: confirming an already-confirmed claim changes nothing and
        does not bump the version. Raises ``KeyError`` for an unknown id so the
        CLI can report it instead of quietly creating a record.
        """
        record = self._require(claim_id)
        if record.status == ClaimStatus.CONFIRMED_BY_USER:
            return record
        record.status = ClaimStatus.CONFIRMED_BY_USER
        record.confirmed_version = self.kb_version
        return record

    def reject(self, claim_id: str) -> ClaimRecord:
        """Mark a claim as rejected by the candidate.

        The record is kept, never deleted: a rejected fact is still evidence of
        what the source said. Idempotent, and raises ``KeyError`` for an unknown
        id.
        """
        record = self._require(claim_id)
        if record.status == ClaimStatus.REJECTED_BY_USER:
            return record
        record.status = ClaimStatus.REJECTED_BY_USER
        record.rejected_version = self.kb_version
        return record

    def _require(self, claim_id: str) -> ClaimRecord:
        record = self.claims.get(claim_id)
        if record is None:
            raise KeyError(claim_id)
        return record
