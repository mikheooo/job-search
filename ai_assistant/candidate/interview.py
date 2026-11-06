"""The candidate interview: session state, decisions and persistence (Stage 0 / T4.5).

WHAT THIS STAGE IS FOR
----------------------
T1-T4 read the candidate's documents and reported what they found. T4.5 is the
first stage that *asks*. It sits between T4 and T5 because the master resume T5
builds cannot be generated from documents that disagree with each other and from
gaps nobody has filled: the three ambiguities T3 detected need a human answer,
and the gaps T4 found need the candidate's own words rather than a rule's guess.

WHAT IT REUSES RATHER THAN REBUILDS
-----------------------------------
* the knowledge base -- there is **one** KB, and interview facts are claims in it;
* :meth:`CandidateKnowledgeBase.confirm` and ``reject`` -- the only status
  transitions, unchanged and still idempotent;
* T1's content-derived claim ids -- see :func:`interview_extract.make_claim_id`;
* T3's and T4's own result objects -- the plan is grounded in the audit that was
  actually shown, not in a re-run of it;
* the project's existing LLM entry point -- see ``interview_extract._default_llm``.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* **It never calls ``merge_claims``.** That method rewrites ``source_file``,
  ``source_sha256`` and ``updated_at``, bumps ``kb_version`` and marks every claim
  it was not handed as ``in_source = False``. Handing it interview facts would
  mark all 96 resume claims absent and relabel the resume snapshot as an
  interview. Interview writes go through their own path, which touches none of
  those three envelope fields.
* **It never writes the profile, the resume or the database.** Only the KB and
  the session file are written, and the session file is the only place an
  unconfirmed fact ever exists.
* **An answer is not a confirmation.** Extracted facts land in the session file
  as ``PENDING``; the KB receives nothing until the candidate explicitly confirms
  or rejects one fact. A test hashes the KB around an ``answer`` call to prove it.
* **It never writes to the live profile.** Unconfirmed data reaching the profile
  would change the pipeline's behaviour without anyone saying so.

WHERE STATE LIVES
-----------------
Two files, with different lifetimes:

* ``artifacts/candidate_interview_state.json`` -- the session: which plan, which
  question, every answer, every pending fact and every decision. This is what
  makes the interview resumable across sessions.
* ``candidate_kb.json`` -- the facts the candidate has ruled on, alongside the
  resume claims. Same file, same format, same statuses.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .audit import ARTIFACTS_DIR, run_audit
from .interview_extract import (
    ExtractionOutcome,
    ProposedFact,
    extract_facts,
    make_claim_id,
    resolve_llm_mode,
)
from .interview_questions import (
    SECTION_ORDER,
    SECTION_TITLE,
    PlanContext,
    Question,
    Section,
    build_plan,
    format_question,
)
from .kb import CandidateKnowledgeBase, ClaimRecord, ClaimStatus, resolve_kb_path
from .kb_verify import (
    BASELINE_LABEL,
    load_baseline_profile,
    load_profile,
    resolve_profile_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..candidate_profile import CandidateProfile
    from .audit import Finding
    from .kb_verify import Contradiction

__all__ = [
    "DEFAULT_SESSION_PATH",
    "SESSION_FORMAT_VERSION",
    "SESSION_PATH_ENV",
    "FactDecision",
    "InterviewEngine",
    "InterviewSession",
    "InterviewSources",
    "ProposalStatus",
    "SessionFormatError",
    "SessionTurn",
    "candidate_interview_cmd",
    "format_progress",
    "load_sources",
    "resolve_session_path",
]

SESSION_FORMAT_VERSION = 1
SESSION_PATH_ENV = "CANDIDATE_INTERVIEW_STATE"
DEFAULT_SESSION_PATH = ARTIFACTS_DIR / "candidate_interview_state.json"

#: The status of a *proposal*, which is not the same thing as the status of a
#: claim. A proposal is a fact waiting for a decision; the claim's status is the
#: decision. ``SUPERSEDED`` means the candidate revised it, so it is no longer
#: the fact on the table.
class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class SessionFormatError(RuntimeError):
    """Raised when a session file cannot be read without guessing."""


def _now() -> str:
    """UTC, like every other timestamp this project writes."""
    return datetime.now(timezone.utc).isoformat()


def resolve_session_path(explicit: str | None = None) -> Path:
    """Explicit path wins, then ``CANDIDATE_INTERVIEW_STATE``, then ``artifacts/``."""
    if explicit:
        return Path(explicit)
    from_env = os.environ.get(SESSION_PATH_ENV)
    if from_env:
        return Path(from_env)
    return DEFAULT_SESSION_PATH


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------


@dataclass
class FactDecision:
    """One extracted fact and what the candidate decided about it."""

    fact: ProposedFact
    status: str = ProposalStatus.PENDING
    extracted_at: str = ""
    decided_at: str | None = None
    #: When this fact replaces an earlier *confirmed* one, the claim id it revises.
    #: Applied only on confirmation, so rejecting a revision leaves the original
    #: fact standing rather than silently retiring it.
    revises: str | None = None
    superseded_by: str | None = None

    @property
    def claim_id(self) -> str:
        return self.fact.claim_id

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "status": str(self.status),
            "extracted_at": self.extracted_at,
            "decided_at": self.decided_at,
            "revises": self.revises,
            "superseded_by": self.superseded_by,
            "fact": self.fact.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FactDecision:
        return cls(
            fact=ProposedFact.from_dict(dict(data["fact"])),  # type: ignore[arg-type]
            status=str(data.get("status") or ProposalStatus.PENDING),
            extracted_at=str(data.get("extracted_at") or ""),
            decided_at=_opt_str(data.get("decided_at")),
            revises=_opt_str(data.get("revises")),
            superseded_by=_opt_str(data.get("superseded_by")),
        )


@dataclass
class SessionTurn:
    """One exchange: the question, what was said, and what came out of it."""

    question_id: str
    asked_at: str
    answer: str | None = None
    answered_at: str | None = None
    skipped: bool = False
    fact_ids: list[str] = field(default_factory=list)
    rejected: list[dict[str, object]] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "asked_at": self.asked_at,
            "answer": self.answer,
            "answered_at": self.answered_at,
            "skipped": self.skipped,
            "fact_ids": list(self.fact_ids),
            "rejected": list(self.rejected),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionTurn:
        return cls(
            question_id=str(data.get("question_id") or ""),
            asked_at=str(data.get("asked_at") or ""),
            answer=_opt_str(data.get("answer")),
            answered_at=_opt_str(data.get("answered_at")),
            skipped=bool(data.get("skipped", False)),
            fact_ids=[str(item) for item in (data.get("fact_ids") or [])],  # type: ignore[union-attr]
            rejected=[dict(item) for item in (data.get("rejected") or [])],  # type: ignore[arg-type,union-attr]
            note=_opt_str(data.get("note")),
        )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


@dataclass
class InterviewSession:
    """The whole interview in one serialisable object."""

    session_id: str
    started_at: str
    updated_at: str
    plan: list[Question]
    cursor: int = 0
    turns: dict[str, SessionTurn] = field(default_factory=dict)
    proposals: dict[str, FactDecision] = field(default_factory=dict)
    plan_fingerprint: str = ""
    source_sha256: str | None = None

    # -- queries -----------------------------------------------------------

    def current(self) -> Question | None:
        if 0 <= self.cursor < len(self.plan):
            return self.plan[self.cursor]
        return None

    def is_complete(self) -> bool:
        return self.cursor >= len(self.plan)

    def decisions(self, status: str | None = None) -> list[FactDecision]:
        items = [
            item for item in self.proposals.values() if status is None or item.status == status
        ]
        return sorted(items, key=lambda item: (item.fact.kind, item.fact.value, item.claim_id))

    def counts_by_status(self) -> dict[str, int]:
        counts = {str(status): 0 for status in ProposalStatus}
        for item in self.proposals.values():
            counts[str(item.status)] = counts.get(str(item.status), 0) + 1
        return counts

    def answered_ids(self) -> set[str]:
        return {
            question_id
            for question_id, turn in self.turns.items()
            if turn.answer is not None or turn.skipped
        }

    def section_counts(self) -> dict[str, tuple[int, int]]:
        """``{section: (answered, total)}`` in section order."""
        counts: dict[str, tuple[int, int]] = {}
        answered = self.answered_ids()
        for section in SECTION_ORDER:
            questions = [item for item in self.plan if item.section is section]
            done = sum(1 for item in questions if item.question_id in answered)
            counts[str(section)] = (done, len(questions))
        return counts

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "session_format_version": SESSION_FORMAT_VERSION,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "plan_fingerprint": self.plan_fingerprint,
            "source_sha256": self.source_sha256,
            "cursor": self.cursor,
            "plan": [item.to_dict() for item in self.plan],
            "turns": {key: value.to_dict() for key, value in self.turns.items()},
            "proposals": {key: value.to_dict() for key, value in self.proposals.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InterviewSession:
        """Read a session file, migrating an older one rather than refusing it.

        The format version is checked *explicitly* and only for the future: a file
        written by a newer build is refused, because guessing at fields this build
        does not know about is how a confirmation gets silently dropped. An older
        file -- including one with no version field at all, which is what version
        1 looks like -- is read with defaults for anything missing, and unknown
        keys are ignored rather than treated as an error.
        """
        raw_version = data.get("session_format_version")
        version = SESSION_FORMAT_VERSION if raw_version is None else int(raw_version)  # type: ignore[arg-type]
        if version > SESSION_FORMAT_VERSION:
            raise SessionFormatError(
                f"session file is format version {version}, but this build understands "
                f"at most {SESSION_FORMAT_VERSION}. Refusing to read it rather than drop "
                f"decisions it may contain."
            )
        plan = [Question.from_dict(dict(item)) for item in (data.get("plan") or [])]  # type: ignore[arg-type,union-attr]
        turns = {
            str(key): SessionTurn.from_dict(dict(value))
            for key, value in dict(data.get("turns") or {}).items()  # type: ignore[arg-type]
        }
        proposals = {
            str(key): FactDecision.from_dict(dict(value))
            for key, value in dict(data.get("proposals") or {}).items()  # type: ignore[arg-type]
        }
        cursor = int(data.get("cursor") or 0)  # type: ignore[arg-type]
        return cls(
            session_id=str(data.get("session_id") or ""),
            started_at=str(data.get("started_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            plan=plan,
            cursor=min(max(cursor, 0), len(plan)),
            turns=turns,
            proposals=proposals,
            plan_fingerprint=str(data.get("plan_fingerprint") or ""),
            source_sha256=_opt_str(data.get("source_sha256")),
        )


def save_session(session: InterviewSession, path: Path) -> None:
    """Write the session atomically: a half-written file would drop decisions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(session.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    os.replace(temp_path, path)


def load_session(path: Path) -> InterviewSession | None:
    """``None`` when there is no session yet; raises when there is a broken one."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionFormatError(f"cannot read session file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SessionFormatError(f"session file {path} does not contain a JSON object")
    return InterviewSession.from_dict(data)


def plan_fingerprint(plan: list[Question], source_sha256: str | None) -> str:
    """A digest of the plan plus the resume snapshot it was built from.

    Used to tell a resumed session that the ground moved underneath it -- the
    resume was re-extracted, or the audit changed -- so the candidate is told
    rather than quietly asked different questions than they started with.
    """
    payload = json.dumps(
        {"plan": [item.to_dict() for item in plan], "source": source_sha256 or ""},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InterviewSources:
    """Everything the plan is built from, loaded once per command."""

    kb_path: Path
    profile_path: Path | None
    profile_label: str
    resume_label: str
    claims: tuple[ClaimRecord, ...]
    profile: CandidateProfile
    findings: tuple[Finding, ...]
    contradictions: tuple[Contradiction, ...]
    source_sha256: str | None

    def plan_context(self) -> PlanContext:
        return PlanContext(
            contradictions=self.contradictions,
            findings=self.findings,
            claims=self.claims,
            profile=self.profile,
            profile_label=self.profile_label,
            resume_label=self.resume_label,
        )


def load_sources(
    kb_path: str | None = None,
    profile_path: str | None = None,
    resume_path: str | None = None,
) -> InterviewSources:
    """Load the KB, the profile and the T3/T4 results, read-only.

    The audit is run through :func:`audit.run_audit` rather than re-derived, so
    the interview is grounded in exactly the contradictions and findings the
    candidate would see from ``candidate-kb verify`` and ``candidate-audit``.
    """
    resolved_kb = resolve_kb_path(kb_path)
    knowledge_base = CandidateKnowledgeBase.load(resolved_kb)

    resolved_profile = resolve_profile_path(profile_path)
    if resolved_profile is not None:
        profile = load_profile(resolved_profile)
        profile_label = str(resolved_profile)
    else:
        profile = load_baseline_profile()
        profile_label = BASELINE_LABEL

    audit = run_audit(
        mode="all",
        kb_path=str(resolved_kb),
        profile_path=profile_path,
        resume_path=resume_path,
    )

    return InterviewSources(
        kb_path=resolved_kb,
        profile_path=resolved_profile,
        profile_label=profile_label,
        resume_label=audit.resume_label,
        claims=tuple(
            record for record in knowledge_base.claims.values() if record.in_source
        ),
        profile=profile,
        findings=tuple(audit.findings),
        contradictions=tuple(audit.verification.contradictions),
        source_sha256=knowledge_base.source_sha256,
    )


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class InterviewEngine:
    """The interview's operations. Every one of them is explicit.

    Nothing here infers a decision. ``answer`` extracts and stages; ``confirm``,
    ``reject`` and ``edit`` are the only things that change a status, and each
    acts on exactly one fact named by id.
    """

    def __init__(
        self,
        *,
        kb_path: str | None = None,
        session_path: str | None = None,
        profile_path: str | None = None,
        resume_path: str | None = None,
        llm: Callable[[str, str], str] | None = None,
        llm_mode: str | None = None,
    ) -> None:
        self.kb_path = resolve_kb_path(kb_path)
        self.session_path = resolve_session_path(session_path)
        self.profile_path = profile_path
        self.resume_path = resume_path
        self.llm = llm
        self.llm_mode = llm_mode
        self.sources: InterviewSources | None = None
        self.session: InterviewSession | None = None
        self.plan_drift: str | None = None

    # -- loading -----------------------------------------------------------

    def _sources(self) -> InterviewSources:
        if self.sources is None:
            self.sources = load_sources(
                str(self.kb_path), self.profile_path, self.resume_path
            )
        return self.sources

    def _require_session(self) -> InterviewSession:
        if self.session is None:
            raise SessionFormatError(
                f"no interview session at {self.session_path}; run `candidate-interview start` first"
            )
        return self.session

    def _knowledge_base(self) -> CandidateKnowledgeBase:
        return CandidateKnowledgeBase.load(self.kb_path)

    def _known_terms(self) -> frozenset[str]:
        """The resume's own technology vocabulary, used by the validation net."""
        terms: set[str] = set()
        for record in self._sources().claims:
            if record.kind == "skill" and record.value.strip():
                terms.add(record.value.strip().lower())
        return frozenset(terms)

    # -- lifecycle ---------------------------------------------------------

    def start(
        self, *, reset: bool = False, rebuild: bool = False, persist: bool = True
    ) -> InterviewSession:
        """Open the interview: resume an existing session, or begin one.

        ``persist=False`` is for the read-only views (``progress``, ``facts``):
        they need the session in memory but must not rewrite its ``updated_at``,
        because a command that only looks should not look like it did something.
        """
        sources = self._sources()
        fresh_plan = list(build_plan(sources.plan_context()))
        fingerprint = plan_fingerprint(fresh_plan, sources.source_sha256)

        existing = None if reset else load_session(self.session_path)
        if existing is None:
            session = InterviewSession(
                # A random suffix rather than a finer timestamp: Windows' clock
                # granularity is ~15ms, so two sessions started in the same tick
                # would otherwise share an id and a reset would look like a resume.
                session_id=(
                    f"interview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                    f"-{os.urandom(3).hex()}"
                ),
                started_at=_now(),
                updated_at=_now(),
                plan=fresh_plan,
                plan_fingerprint=fingerprint,
                source_sha256=sources.source_sha256,
            )
        else:
            session = existing
            if rebuild:
                session.plan = fresh_plan
            if session.plan_fingerprint and session.plan_fingerprint != fingerprint:
                self.plan_drift = (
                    "the resume snapshot or the audit changed since this session began, so the "
                    "question list may have moved. Existing answers and decisions are kept; "
                    "re-run with --rebuild to refresh the question list."
                )
            session.plan_fingerprint = fingerprint
            session.source_sha256 = sources.source_sha256
            session.updated_at = _now()
            session.cursor = min(session.cursor, len(session.plan))

        self.session = session
        if persist:
            save_session(session, self.session_path)
        return session

    def current_question(self) -> Question | None:
        return self._require_session().current()

    def _current_or_error(self) -> Question:
        session = self._require_session()
        question = session.current()
        if question is None:
            raise SessionFormatError(
                "the interview has reached the end of its question list; "
                "`candidate-interview progress` shows what was covered"
            )
        return question

    # -- answering ---------------------------------------------------------

    def answer(self, text: str) -> ExtractionOutcome:
        """Record an answer, extract facts, and stage them as PENDING.

        Writes the session file only. The knowledge base is not touched: an answer
        is not a confirmation, and a test hashes the KB around this call to prove
        it.
        """
        session = self._require_session()
        question = self._current_or_error()
        outcome = extract_facts(
            question,
            text,
            llm=self.llm,
            mode=self.llm_mode,
            known_terms=self._known_terms(),
        )
        now = _now()
        turn = session.turns.get(question.question_id)
        if turn is None:
            turn = SessionTurn(question_id=question.question_id, asked_at=now)
            session.turns[question.question_id] = turn
        turn.answer = text
        turn.answered_at = now
        turn.skipped = False
        turn.note = outcome.note
        turn.rejected = [item.to_dict() for item in outcome.rejected]

        for fact in outcome.facts:
            existing = session.proposals.get(fact.claim_id)
            if existing is None:
                session.proposals[fact.claim_id] = FactDecision(
                    fact=fact, status=ProposalStatus.PENDING, extracted_at=now
                )
            elif existing.status == ProposalStatus.SUPERSEDED:
                # Re-answering with the same words revives the same fact rather
                # than leaving a superseded husk behind.
                existing.status = ProposalStatus.PENDING
                existing.decided_at = None
                existing.superseded_by = None

        turn.fact_ids = [fact.claim_id for fact in outcome.facts]
        session.updated_at = now
        save_session(session, self.session_path)
        return outcome

    # -- decisions ---------------------------------------------------------

    def _decision(self, claim_id: str) -> FactDecision:
        session = self._require_session()
        decision = session.proposals.get(claim_id)
        if decision is None:
            known = ", ".join(sorted(session.proposals)) or "(none)"
            raise SessionFormatError(
                f"no extracted fact with id {claim_id!r} in this session. Known ids: {known}"
            )
        return decision

    def _decidable(self, claim_id: str) -> FactDecision:
        """A fact the candidate can still rule on.

        A superseded fact is not decidable: the candidate already replaced it with
        a revision, so confirming or rejecting it would act on a position they have
        moved off -- and rejecting it would flip a confirmed claim to rejected for
        no reason a reader could reconstruct.
        """
        decision = self._decision(claim_id)
        if decision.status == ProposalStatus.SUPERSEDED:
            raise SessionFormatError(
                f"{claim_id} was superseded by {decision.superseded_by}; decide that fact instead"
            )
        return decision

    def _ensure_claim(
        self,
        knowledge_base: CandidateKnowledgeBase,
        decision: FactDecision,
        *,
        session_id: str,
        answered_at: str,
    ) -> bool:
        """Put the fact in the KB as DERIVED, if it is not there already.

        Touches ``claims`` and ``kb_version`` only. ``source_file``,
        ``source_sha256`` and ``updated_at`` describe the *resume* snapshot and are
        left exactly as the last ``extract`` wrote them -- which is the whole
        reason this is not routed through ``merge_claims``.
        """
        fact = decision.fact
        if knowledge_base.get(fact.claim_id) is not None:
            return False
        new_version = knowledge_base.kb_version + 1
        knowledge_base.claims[fact.claim_id] = ClaimRecord(
            claim_id=fact.claim_id,
            kind=fact.kind,
            value=fact.value,
            status=ClaimStatus.DERIVED_FROM_SOURCE,
            normalized=None,
            role_id=fact.role_id,
            parent_id=fact.parent_id,
            evidence=fact.evidence(session_id=session_id, answered_at=answered_at),
            in_source=True,
            first_seen_version=new_version,
            last_seen_version=new_version,
        )
        knowledge_base.kb_version = new_version
        return True

    def _answered_at(self, question_id: str) -> str:
        session = self._require_session()
        turn = session.turns.get(question_id)
        if turn is not None and turn.answered_at:
            return turn.answered_at
        return session.started_at or _now()

    def confirm(self, claim_id: str) -> tuple[FactDecision, bool]:
        """Move exactly one fact to ``CONFIRMED_BY_USER``.

        Returns ``(decision, changed)``. Idempotent: confirming an already
        confirmed fact writes nothing at all, which is the same promise
        ``candidate-kb confirm`` makes.
        """
        session = self._require_session()
        decision = self._decidable(claim_id)
        if decision.status == ProposalStatus.CONFIRMED:
            return decision, False

        knowledge_base = self._knowledge_base()
        self._ensure_claim(
            knowledge_base,
            decision,
            session_id=session.session_id,
            answered_at=self._answered_at(decision.fact.question_id),
        )
        knowledge_base.confirm(claim_id)

        if decision.revises:
            # The revision takes effect only now, on confirmation: rejecting an
            # edit must leave the original fact standing.
            revised = knowledge_base.get(decision.revises)
            if revised is not None:
                revised.evidence = {
                    **revised.evidence,
                    "superseded_by": claim_id,
                    "superseded_at": _now(),
                }

        knowledge_base.save(self.kb_path)
        decision.status = ProposalStatus.CONFIRMED
        decision.decided_at = _now()
        session.updated_at = decision.decided_at
        save_session(session, self.session_path)
        return decision, True

    def reject(self, claim_id: str) -> tuple[FactDecision, bool]:
        """Move exactly one fact to ``REJECTED_BY_USER``; the record is kept.

        A rejection is a decision, not an absence, so it is recorded in the KB the
        same way ``candidate-kb reject`` records one -- T5 must be able to tell
        "the candidate said no" from "nobody asked".
        """
        session = self._require_session()
        decision = self._decidable(claim_id)
        if decision.status == ProposalStatus.REJECTED:
            return decision, False

        knowledge_base = self._knowledge_base()
        self._ensure_claim(
            knowledge_base,
            decision,
            session_id=session.session_id,
            answered_at=self._answered_at(decision.fact.question_id),
        )
        knowledge_base.reject(claim_id)
        knowledge_base.save(self.kb_path)

        decision.status = ProposalStatus.REJECTED
        decision.decided_at = _now()
        session.updated_at = decision.decided_at
        save_session(session, self.session_path)
        return decision, True

    def edit(self, claim_id: str, new_text: str) -> FactDecision:
        """Revise a fact. The revised fact is PENDING again, by design.

        "A corrected fact requires re-confirmation" is not a policy bolted on
        here: the corrected text has a different ``claim_id`` (ids are derived
        from content), so it is a *different* fact and there is no confirmation
        for it to inherit. The old fact is marked ``SUPERSEDED``; if it had been
        confirmed, the new one carries ``revises`` so that confirming it records
        the supersede link -- and rejecting it leaves the original standing.
        """
        session = self._require_session()
        decision = self._decidable(claim_id)
        text = (new_text or "").strip()
        if not text:
            raise SessionFormatError("the corrected fact is empty")

        new_claim_id = make_claim_id(decision.fact.kind, decision.fact.scope, text)
        if new_claim_id == claim_id:
            # Rewriting a fact with its own words changes nothing, so nothing is
            # superseded and nothing needs re-confirming.
            return decision

        now = _now()
        revised = ProposedFact(
            claim_id=new_claim_id,
            kind=decision.fact.kind,
            value=text,
            quote=text,
            scope=decision.fact.scope,
            question_id=decision.fact.question_id,
            fact_class=decision.fact.fact_class,
            source="edit",
            role_id=decision.fact.role_id,
            parent_id=decision.fact.parent_id,
        )
        session.proposals[new_claim_id] = FactDecision(
            fact=revised,
            status=ProposalStatus.PENDING,
            extracted_at=now,
            revises=claim_id if decision.status == ProposalStatus.CONFIRMED else None,
        )
        decision.status = ProposalStatus.SUPERSEDED
        decision.superseded_by = new_claim_id
        decision.decided_at = now
        session.updated_at = now
        save_session(session, self.session_path)
        return session.proposals[new_claim_id]

    # -- movement ----------------------------------------------------------

    def skip(self) -> Question | None:
        """Mark the current question skipped and move on. Nothing is written to the KB."""
        session = self._require_session()
        question = self._current_or_error()
        now = _now()
        turn = session.turns.get(question.question_id)
        if turn is None:
            turn = SessionTurn(question_id=question.question_id, asked_at=now)
            session.turns[question.question_id] = turn
        turn.skipped = True
        turn.answered_at = now
        session.cursor = min(session.cursor + 1, len(session.plan))
        session.updated_at = now
        save_session(session, self.session_path)
        return session.current()

    def advance(self) -> Question | None:
        """Move to the next question without recording anything about this one."""
        session = self._require_session()
        session.cursor = min(session.cursor + 1, len(session.plan))
        session.updated_at = _now()
        save_session(session, self.session_path)
        return session.current()

    def go_back(self) -> Question | None:
        """Move to the previous question, keeping everything already decided."""
        session = self._require_session()
        session.cursor = max(session.cursor - 1, 0)
        session.updated_at = _now()
        save_session(session, self.session_path)
        return session.current()

    def progress(self) -> dict[str, object]:
        """A read-only summary of where the interview stands."""
        session = self._require_session()
        answered = session.answered_ids()
        current = session.current()
        return {
            "session_id": session.session_id,
            "session_path": str(self.session_path),
            "kb_path": str(self.kb_path),
            "started_at": session.started_at,
            "updated_at": session.updated_at,
            "questions_total": len(session.plan),
            "questions_answered": len(answered),
            "questions_skipped": sum(1 for turn in session.turns.values() if turn.skipped),
            "cursor": session.cursor,
            "current_question_id": current.question_id if current else None,
            "complete": session.is_complete(),
            "sections": {
                key: {"answered": value[0], "total": value[1]}
                for key, value in session.section_counts().items()
            },
            "proposal_counts": session.counts_by_status(),
            "plan_drift": self.plan_drift,
        }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def format_progress(payload: dict[str, object]) -> str:
    """The progress view, grouped by section so an unfinished part is obvious."""
    total = payload["questions_total"]
    answered = payload["questions_answered"]
    lines = [
        "interview progress",
        f"  session        : {payload['session_id']}  (started {payload['started_at']})",
        f"  state file     : {payload['session_path']}",
        f"  knowledge base : {payload['kb_path']}",
        f"  questions      : {answered}/{total} answered, {payload['questions_skipped']} skipped",
    ]
    current = payload["current_question_id"]
    if payload["complete"]:
        lines.append("  current        : (end of the question list)")
    else:
        lines.append(f"  current        : {current}")
    counts = payload["proposal_counts"]
    lines.append(
        "  facts          : "
        + ", ".join(f"{status} {count}" for status, count in counts.items())
    )
    if payload["plan_drift"]:
        lines.append(f"  WARNING        : {payload['plan_drift']}")
    lines.append("")
    lines.append("  sections:")
    for key, value in payload["sections"].items():
        title = SECTION_TITLE[Section(key)]
        lines.append(f"    {title:<38} {value['answered']}/{value['total']}")
    return "\n".join(lines)


def _format_decisions(decisions: list[FactDecision], *, title: str) -> str:
    if not decisions:
        return f"{title}: (none)"
    lines = [f"{title}: {len(decisions)}"]
    for item in decisions:
        lines.append(f"  {item.claim_id}  [{item.status}] {item.fact.kind}")
        lines.append(f"    fact  : {item.fact.value}")
        lines.append(f"    quote : {item.fact.quote}")
        if item.superseded_by:
            lines.append(f"    superseded by: {item.superseded_by}")
        if item.revises:
            lines.append(f"    revises      : {item.revises}")
    return "\n".join(lines)


def _format_proposals(facts: list[ProposedFact]) -> str:
    """Freshly extracted facts, which have no decision yet by definition."""
    if not facts:
        return "extracted facts (PENDING): (none)"
    lines = [f"extracted facts (PENDING): {len(facts)}"]
    for fact in facts:
        lines.append(f"  {fact.claim_id}  [{ProposalStatus.PENDING}] {fact.kind}")
        lines.append(f"    fact  : {fact.value}")
        lines.append(f"    quote : {fact.quote}")
    return "\n".join(lines)


def _format_question_block(
    engine: InterviewEngine, question: Question | None, *, explain: bool = False
) -> str:
    session = engine._require_session()
    if question is None:
        return (
            "the interview has reached the end of its question list.\n"
            "`candidate-interview progress` shows what was covered."
        )
    position = session.plan.index(question) + 1
    return format_question(question, position=position, total=len(session.plan), explain=explain)


def _format_outcome(outcome: ExtractionOutcome) -> str:
    lines: list[str] = []
    if outcome.note:
        lines.append(f"note: {outcome.note}")
    lines.append(_format_proposals(list(outcome.facts)))
    if outcome.rejected:
        lines.append("")
        lines.append(f"refused by validation: {len(outcome.rejected)}")
        for item in outcome.rejected:
            lines.append(f"  {item.value[:70]!r}")
            lines.append(f"    reason: {item.reason}")
    lines.append("")
    lines.append(
        "Nothing has been written to the knowledge base. Confirm each fact you accept "
        "with `candidate-interview confirm --fact <id>`."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------

ACTIONS = (
    "start",
    "answer",
    "confirm",
    "reject",
    "edit",
    "next",
    "back",
    "skip",
    "clarify",
    "progress",
    "facts",
)


def candidate_interview_cmd(
    action: str,
    value: str | None = None,
    *,
    fact_id: str | None = None,
    kb_path: str | None = None,
    session_path: str | None = None,
    profile_path: str | None = None,
    resume_path: str | None = None,
    status: str | None = None,
    as_json: bool = False,
    reset: bool = False,
    rebuild: bool = False,
    llm_mode: str | None = None,
) -> int:
    """``candidate-interview`` handler. Returns a process exit code.

    Exit 0 means the action ran. A non-zero code is reserved for being unable to
    run at all -- an unknown action, a missing session, an unknown fact id -- and
    never for "the candidate's answer was not what we hoped".
    """
    if action not in ACTIONS:
        print(f"ERROR: unknown action {action!r}; expected one of: {', '.join(ACTIONS)}", file=sys.stderr)
        return 2
    if llm_mode is not None:
        try:
            resolve_llm_mode(llm_mode)
        except Exception as exc:  # noqa: BLE001 - surfaced as a usage error
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    engine = InterviewEngine(
        kb_path=kb_path,
        session_path=session_path,
        profile_path=profile_path,
        resume_path=resume_path,
        llm_mode=llm_mode,
    )

    try:
        if action == "start":
            session = engine.start(reset=reset, rebuild=rebuild)
            if as_json:
                payload = engine.progress()
                payload["question"] = (
                    session.current().to_dict() if session.current() else None
                )
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 0
            if engine.plan_drift:
                print(f"WARNING: {engine.plan_drift}")
                print()
            print(
                f"interview {session.session_id}: {len(session.plan)} question(s) in "
                f"{len(SECTION_ORDER)} section(s)."
            )
            print()
            print(_format_question_block(engine, session.current()))
            return 0

        if action == "progress":
            if not engine.session_path.is_file():
                print(
                    f"no interview session at {engine.session_path}; "
                    f"run `candidate-interview start` to begin one."
                )
                return 0
            engine.start(persist=False)
            payload = engine.progress()
            if as_json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(format_progress(payload))
            return 0

        if action == "facts":
            if not engine.session_path.is_file():
                print(
                    f"no interview session at {engine.session_path}; "
                    f"run `candidate-interview start` to begin one."
                )
                return 0
            session = engine.start(persist=False)
            if status is not None:
                known = {str(item) for item in ProposalStatus}
                if status not in known:
                    print(
                        f"ERROR: unknown status {status!r}; expected one of: {', '.join(sorted(known))}",
                        file=sys.stderr,
                    )
                    return 2
            decisions = session.decisions(status)
            if as_json:
                print(
                    json.dumps(
                        {
                            "session_id": session.session_id,
                            "counts": session.counts_by_status(),
                            "facts": [item.to_dict() for item in decisions],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 0
            print(_format_decisions(decisions, title=f"facts ({status or 'all'})"))
            return 0

        # Every remaining action needs an existing session.
        session = load_session(engine.session_path)
        if session is None:
            print(
                f"ERROR: no interview session at {engine.session_path}. "
                f"Run `candidate-interview start` first.",
                file=sys.stderr,
            )
            return 2
        engine.session = session

        if action == "clarify":
            print(_format_question_block(engine, engine.current_question(), explain=True))
            return 0

        if action == "next":
            question = engine.advance()
            print(_format_question_block(engine, question))
            return 0

        if action == "back":
            question = engine.go_back()
            print(_format_question_block(engine, question))
            return 0

        if action == "skip":
            question = engine.skip()
            print("skipped: no fact was recorded, and nothing was written to the knowledge base.")
            print()
            print(_format_question_block(engine, question))
            return 0

        if action == "answer":
            if value is None:
                print("ERROR: `answer` needs the answer text", file=sys.stderr)
                return 2
            outcome = engine.answer(value)
            if as_json:
                print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))
                return 0
            print(_format_outcome(outcome))
            return 0

        if action in ("confirm", "reject"):
            if fact_id is None:
                print(f"ERROR: `{action}` needs --fact <claim_id>", file=sys.stderr)
                return 2
            decision, changed = (
                engine.confirm(fact_id) if action == "confirm" else engine.reject(fact_id)
            )
            if as_json:
                print(json.dumps(decision.to_dict(), indent=2, ensure_ascii=False))
                return 0
            if not changed:
                print(
                    f"{action}: {fact_id} is already {decision.status} "
                    f"(no change, nothing written)"
                )
                return 0
            print(f"{action}: {fact_id}")
            print(f"  kind   : {decision.fact.kind} ({decision.fact.fact_class})")
            print(f"  value  : {decision.fact.value}")
            print(f"  quote  : {decision.fact.quote}")
            print(f"  status : {decision.status}")
            print(f"  written to knowledge base: {engine.kb_path}")
            return 0

        if action == "edit":
            if fact_id is None or value is None:
                print(
                    "ERROR: `edit` needs --fact <claim_id> and the corrected text",
                    file=sys.stderr,
                )
                return 2
            revised = engine.edit(fact_id, value)
            if as_json:
                print(json.dumps(revised.to_dict(), indent=2, ensure_ascii=False))
                return 0
            print(f"edited: {fact_id} -> {revised.claim_id}")
            print(f"  new value : {revised.fact.value}")
            print(f"  status    : {revised.status}")
            print()
            print(
                "The corrected fact is PENDING and needs its own confirmation: "
                "an edited fact is a different fact, so there was no confirmation for it to inherit."
            )
            return 0
    except SessionFormatError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the CLI must not traceback at the user
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"ERROR: action {action!r} was not handled", file=sys.stderr)
    return 2
