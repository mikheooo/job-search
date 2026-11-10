"""Turning an answer into atomic facts, without inventing anything (Stage 0 / T4.5).

THE INVARIANT
-------------
Every fact carries ``quote`` -- and ``quote`` must be a **literal substring of the
answer the candidate actually typed**. That is the same structural guarantee the
resume parser gives (``evidence.quote`` is a substring of the resume line), moved
to the interview. A fact that cannot point at the candidate's own words is
rejected before it is ever shown, so "the extractor invented a fact" is not a
thing that can reach the confirmation step.

Three independent nets stand behind it:

1. **verbatim** -- ``quote in answer``, character for character;
2. **numbers** -- every number in ``value`` must appear in the quote, or in the
   question itself (a question may legitimately supply a figure, e.g. "your
   profile records 11 years");
3. **technology** -- any known technology named in ``value`` must also appear in
   the quote, which is what stops "I automated some reports" from becoming
   "knows Python".

WHAT THE NETS DO NOT CATCH
--------------------------
An extractor could still produce a *paraphrase* that adds no number and no known
technology yet is still wrong. That residual risk is why extraction is not
confirmation: every fact is shown to the candidate and only an explicit
per-fact confirmation moves it. The nets exist to keep the thing being shown
honest, not to replace the candidate.

TWO MODES, AND WHY THE DEFAULT IS THE DULL ONE
----------------------------------------------
``deterministic`` mode invents nothing by construction: ``value`` **is** ``quote``,
split out of the answer. ``llm`` mode asks the configured model for structured
facts and then runs the same validation over them.

The default is ``off``, i.e. deterministic. This project has no budget for
accidental spend, and a test suite that silently reached a paid endpoint would be
worse than a duller extractor -- so the model is only consulted when someone asks
for it explicitly (``CANDIDATE_INTERVIEW_LLM=auto`` or ``required``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .interview_questions import ALLOWED_KINDS, FactClass, fact_class_of

if TYPE_CHECKING:
    from .interview_questions import Question

__all__ = [
    "LLM_AUTO",
    "LLM_MODE_ENV",
    "LLM_OFF",
    "LLM_REQUIRED",
    "ExtractionError",
    "ExtractionOutcome",
    "ProposedFact",
    "RejectedFact",
    "extract_facts",
    "make_claim_id",
    "numbers_in",
    "resolve_llm_mode",
    "validate_fact",
]

logger = logging.getLogger(__name__)

#: ``off`` (default) never calls a model; ``auto`` tries and falls back;
#: ``required`` tries and raises. ``JOB_ANALYZER_OFFLINE=1`` forces ``off``.
LLM_MODE_ENV = "CANDIDATE_INTERVIEW_LLM"
LLM_OFF = "off"
LLM_AUTO = "auto"
LLM_REQUIRED = "required"

MODE_DETERMINISTIC = "deterministic"
MODE_LLM = "llm"

SOURCE_INTERVIEW = "interview"

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
#: Bullet and list markers stripped from a deterministic fragment.
_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s*")


class ExtractionError(RuntimeError):
    """Raised when facts could not be extracted at all, and silence would lie."""


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------


def make_claim_id(kind: str, scope: str, value: str) -> str:
    """The content-derived claim id, using T1's scheme.

    T1 computes ``sha256(kind \\x1f scope \\x1f value)[:10]`` and prefixes the
    kind, so that the same fact always gets the same id and editing a source
    produces a *new* id rather than silently rewriting an old record. Interview
    facts must live under the same scheme or the knowledge base would hold two
    incompatible notions of identity -- and a re-run of the same answer would
    duplicate instead of being idempotent.

    A test recomputes a parsed resume claim's id through this function, so the
    two implementations cannot drift apart unnoticed.
    """
    digest = hashlib.sha256(f"{kind}\x1f{scope}\x1f{value}".encode()).hexdigest()[:10]
    return f"{kind}-{digest}"


# --------------------------------------------------------------------------
# The facts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedFact:
    """One atomic fact extracted from an answer, awaiting a decision.

    ``claim_id`` is computed at extraction time and is the id the record will
    occupy in the knowledge base once confirmed -- so a fact has the same name
    before and after confirmation, and the session file and the knowledge base
    cannot disagree about which fact is which.
    """

    claim_id: str
    kind: str
    value: str
    quote: str
    scope: str
    question_id: str
    fact_class: FactClass
    source: str
    role_id: str | None = None
    parent_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind,
            "value": self.value,
            "quote": self.quote,
            "scope": self.scope,
            "question_id": self.question_id,
            "fact_class": str(self.fact_class),
            "source": self.source,
            "role_id": self.role_id,
            "parent_id": self.parent_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ProposedFact:
        return cls(
            claim_id=str(data["claim_id"]),
            kind=str(data["kind"]),
            value=str(data["value"]),
            quote=str(data.get("quote") or ""),
            scope=str(data.get("scope") or ""),
            question_id=str(data.get("question_id") or ""),
            fact_class=fact_class_of(str(data["kind"])),
            source=str(data.get("source") or MODE_DETERMINISTIC),
            role_id=_opt_str(data.get("role_id")),
            parent_id=_opt_str(data.get("parent_id")),
        )

    def evidence(self, *, session_id: str, answered_at: str) -> dict[str, object]:
        """The provenance block written into the knowledge base.

        ``source_file`` is the string ``interview``, never a path: it is what
        marks this claim as interview-sourced, and it is what stops a later
        resume merge from marking it absent or overwriting its provenance.
        """
        return {
            "source_file": SOURCE_INTERVIEW,
            "line": None,
            "quote": self.quote,
            "question_id": self.question_id,
            "session_id": session_id,
            "answered_at": answered_at,
            "extractor": self.source,
        }


@dataclass(frozen=True)
class RejectedFact:
    """A candidate fact the validation refused, kept so the refusal is visible."""

    value: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"value": self.value, "reason": self.reason}


@dataclass(frozen=True)
class ExtractionOutcome:
    """What one answer produced: the facts, the refusals and how they were made."""

    facts: tuple[ProposedFact, ...]
    rejected: tuple[RejectedFact, ...]
    source: str
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "note": self.note,
            "facts": [fact.to_dict() for fact in self.facts],
            "rejected": [item.to_dict() for item in self.rejected],
        }


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# Numbers and technology terms
# --------------------------------------------------------------------------


def numbers_in(text: str) -> frozenset[str]:
    """Every numeric token in ``text``, with the comma-decimal spelling too."""
    found: set[str] = set()
    for raw in _NUMBER_RE.findall(text or ""):
        found.add(raw)
        found.add(raw.replace(",", "."))
    return frozenset(found)


def _terms_in(text: str, known: frozenset[str]) -> frozenset[str]:
    """Which of the known technology terms appear in ``text`` (case-insensitive)."""
    lowered = (text or "").lower()
    return frozenset(term for term in known if term in lowered)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_fact(
    *,
    kind: str,
    value: str,
    quote: str,
    answer: str,
    allowed_numbers: frozenset[str] = frozenset(),
    known_terms: frozenset[str] = frozenset(),
) -> str | None:
    """``None`` when the fact is acceptable, otherwise the reason it is not.

    Returning the reason rather than a bool is deliberate: a rejection that
    cannot say *why* is indistinguishable from a bug, and this project has been
    bitten by silent refusals before.
    """
    if kind not in ALLOWED_KINDS:
        return f"kind {kind!r} is not one of the allowed interview kinds"
    if not value.strip():
        return "the fact is empty"
    if not quote.strip():
        return "the fact carries no quote, so it cannot be traced to the answer"
    if quote not in answer:
        return "the quote is not a verbatim substring of the answer"

    stray_numbers = numbers_in(value) - numbers_in(quote) - allowed_numbers
    if stray_numbers:
        return (
            "the fact states number(s) the answer never gave: "
            + ", ".join(sorted(stray_numbers))
        )

    invented_terms = _terms_in(value, known_terms) - _terms_in(quote, known_terms)
    if invented_terms:
        return (
            "the fact names technolog(y/ies) the answer never mentioned: "
            + ", ".join(sorted(invented_terms))
        )
    return None


# --------------------------------------------------------------------------
# Deterministic extraction
# --------------------------------------------------------------------------


def _fragments(answer: str) -> list[str]:
    """The answer split into the pieces a person wrote as separate statements."""
    pieces: list[str] = []
    for line in (answer or "").splitlines():
        stripped = _BULLET_RE.sub("", line).strip()
        if stripped:
            pieces.append(stripped)
    if not pieces:
        collapsed = (answer or "").strip()
        if collapsed:
            pieces.append(collapsed)
    return pieces


def extract_deterministic(
    question: Question,
    answer: str,
    *,
    allowed_numbers: frozenset[str] = frozenset(),
    known_terms: frozenset[str] = frozenset(),
) -> tuple[list[ProposedFact], list[RejectedFact]]:
    """One fact per statement the candidate wrote, quoted verbatim.

    ``value`` and ``quote`` are the same string here. That is the whole point:
    in this mode there is no step at which a number, a technology or an employer
    could enter a fact that the candidate did not type.
    """
    facts: list[ProposedFact] = []
    rejected: list[RejectedFact] = []
    seen: set[str] = set()
    for fragment in _fragments(answer):
        reason = validate_fact(
            kind=question.fact_kind,
            value=fragment,
            quote=fragment,
            answer=answer,
            allowed_numbers=allowed_numbers,
            known_terms=known_terms,
        )
        if reason is not None:
            rejected.append(RejectedFact(value=fragment, reason=reason))
            continue
        claim_id = make_claim_id(question.fact_kind, question.scope, fragment)
        if claim_id in seen:
            # The same sentence twice is one fact. T1's factory would suffix it;
            # here a verbatim repeat carries no new information, so it collapses.
            continue
        seen.add(claim_id)
        facts.append(
            ProposedFact(
                claim_id=claim_id,
                kind=question.fact_kind,
                value=fragment,
                quote=fragment,
                scope=question.scope,
                question_id=question.question_id,
                fact_class=fact_class_of(question.fact_kind),
                source=MODE_DETERMINISTIC,
                role_id=question.role_id,
                parent_id=question.parent_id,
            )
        )
    return facts, rejected


# --------------------------------------------------------------------------
# LLM extraction
# --------------------------------------------------------------------------

LlmCallable = Callable[[str, str], str]

#: ``__KINDS__`` is substituted with ``str.replace`` rather than ``str.format``:
#: the prompt is mostly literal JSON, and ``format`` would read its braces as
#: placeholders (a bug that cost a debugging round here).
EXTRACT_SYSTEM_PROMPT = """You extract atomic facts from a candidate's answer in a job-search interview.

Return JSON only, shaped exactly like:
{"facts": [{"kind": "...", "value": "...", "quote": "..."}]}

Rules, all of them absolute:
- "quote" MUST be copied character-for-character out of the candidate's answer. No rewording, no ellipsis, no stitching two sentences together.
- "value" states ONE fact in one sentence, using only words and numbers that appear in that quote.
- Never add a number, duration, employer, technology or achievement that the quote does not contain.
- "kind" MUST be one of: __KINDS__
- The question tells you which kind is expected; use it unless the answer is clearly about something else in the same class.
- If the answer contains no usable fact (a refusal, a joke, "I don't know"), return {"facts": []}.
- Do not speculate about the candidate's skills. A skill counts only if the quote names it.
"""


def _build_user_prompt(question: Question, answer: str) -> str:
    return (
        f"Question id: {question.question_id}\n"
        f"Question: {question.prompt}\n"
        f"Expected fact kind: {question.fact_kind} ({fact_class_of(question.fact_kind)})\n"
        f"Why it is being asked: {question.why}\n"
        f"\nCandidate answer (the only permitted source of quotes):\n{answer}\n"
    )


def _default_llm(system_prompt: str, user_prompt: str) -> str:
    """The project's existing LLM entry point, imported lazily.

    Reused rather than re-implemented: ``job_analyzer._call_llm`` already owns the
    provider configuration, the retry policy for transient 5xx/429 and the
    ``JOB_ANALYZER_OFFLINE`` short-circuit. Importing it here keeps one definition
    of "how this project talks to a model".
    """
    from ..job_analyzer import _call_llm

    return _call_llm(system_prompt, user_prompt)


def parse_llm_payload(raw: str) -> list[dict[str, object]]:
    """Read the model's JSON, refusing anything that is not the promised shape."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ExtractionError(f"the model did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExtractionError("the model returned JSON that is not an object")
    facts = payload.get("facts")
    if facts is None:
        raise ExtractionError("the model's JSON has no 'facts' key")
    if not isinstance(facts, list):
        raise ExtractionError("the model's 'facts' is not a list")
    rows: list[dict[str, object]] = []
    for index, item in enumerate(facts):
        if not isinstance(item, dict):
            raise ExtractionError(f"fact #{index} is not an object")
        rows.append(item)
    return rows


def extract_with_llm(
    question: Question,
    answer: str,
    *,
    llm: LlmCallable | None = None,
    allowed_numbers: frozenset[str] = frozenset(),
    known_terms: frozenset[str] = frozenset(),
) -> tuple[list[ProposedFact], list[RejectedFact]]:
    """Ask the model for facts, then hold its answer to the same nets."""
    caller = llm or _default_llm
    kinds = ", ".join(sorted(ALLOWED_KINDS))
    raw = caller(
        EXTRACT_SYSTEM_PROMPT.replace("__KINDS__", kinds),
        _build_user_prompt(question, answer),
    )
    rows = parse_llm_payload(raw)

    facts: list[ProposedFact] = []
    rejected: list[RejectedFact] = []
    seen: set[str] = set()
    for row in rows:
        kind = str(row.get("kind") or question.fact_kind)
        value = str(row.get("value") or "")
        quote = str(row.get("quote") or "")
        reason = validate_fact(
            kind=kind,
            value=value,
            quote=quote,
            answer=answer,
            allowed_numbers=allowed_numbers,
            known_terms=known_terms,
        )
        if reason is not None:
            rejected.append(RejectedFact(value=value or quote, reason=reason))
            continue
        claim_id = make_claim_id(kind, question.scope, value)
        if claim_id in seen:
            continue
        seen.add(claim_id)
        facts.append(
            ProposedFact(
                claim_id=claim_id,
                kind=kind,
                value=value,
                quote=quote,
                scope=question.scope,
                question_id=question.question_id,
                fact_class=fact_class_of(kind),
                source=MODE_LLM,
                role_id=question.role_id,
                parent_id=question.parent_id,
            )
        )
    return facts, rejected


# --------------------------------------------------------------------------
# Mode selection
# --------------------------------------------------------------------------


def resolve_llm_mode(explicit: str | None = None) -> str:
    """``off`` / ``auto`` / ``required``, defaulting to ``off``.

    An unreadable or unknown value is treated as ``off`` rather than as an error:
    the failure mode of a typo should be "a duller extractor", never "a surprise
    invoice".
    """
    if explicit:
        value = explicit.strip().lower()
        if value not in (LLM_OFF, LLM_AUTO, LLM_REQUIRED):
            raise ExtractionError(
                f"unknown LLM mode {explicit!r}; expected one of: {LLM_OFF}, {LLM_AUTO}, {LLM_REQUIRED}"
            )
        return value
    if os.getenv("JOB_ANALYZER_OFFLINE") == "1":
        return LLM_OFF
    value = (os.getenv(LLM_MODE_ENV) or "").strip().lower()
    return value if value in (LLM_OFF, LLM_AUTO, LLM_REQUIRED) else LLM_OFF


def extract_facts(
    question: Question,
    answer: str,
    *,
    llm: LlmCallable | None = None,
    mode: str | None = None,
    known_terms: frozenset[str] = frozenset(),
) -> ExtractionOutcome:
    """Extract the facts from one answer, validated, ready to be shown.

    An injected ``llm`` always wins, whatever the mode says: that is what lets a
    test exercise the whole model path -- prompt, parsing, validation, rejection --
    without a socket and without a bill.
    """
    text = (answer or "").strip()
    if not text:
        return ExtractionOutcome(facts=(), rejected=(), source=MODE_DETERMINISTIC)

    allowed_numbers = numbers_in(question.prompt) | numbers_in(question.why)

    if llm is not None:
        facts, rejected = extract_with_llm(
            question,
            text,
            llm=llm,
            allowed_numbers=allowed_numbers,
            known_terms=known_terms,
        )
        return ExtractionOutcome(facts=tuple(facts), rejected=tuple(rejected), source=MODE_LLM)

    resolved = resolve_llm_mode(mode)
    if resolved == LLM_OFF:
        facts, rejected = extract_deterministic(
            question, text, allowed_numbers=allowed_numbers, known_terms=known_terms
        )
        return ExtractionOutcome(
            facts=tuple(facts), rejected=tuple(rejected), source=MODE_DETERMINISTIC
        )

    try:
        facts, rejected = extract_with_llm(
            question,
            text,
            allowed_numbers=allowed_numbers,
            known_terms=known_terms,
        )
    except Exception as exc:
        # No noqa needed here: ruff's BLE001 does not fire because the handler
        # re-raises when the mode is ``required``.
        if resolved == LLM_REQUIRED:
            raise ExtractionError(
                f"LLM extraction was required but failed ({type(exc).__name__}: {exc}); "
                f"re-run with {LLM_MODE_ENV}={LLM_OFF} to use the deterministic extractor"
            ) from exc
        logger.warning("LLM extraction unavailable (%s); using deterministic mode", type(exc).__name__)
        facts, rejected = extract_deterministic(
            question, text, allowed_numbers=allowed_numbers, known_terms=known_terms
        )
        return ExtractionOutcome(
            facts=tuple(facts),
            rejected=tuple(rejected),
            source=MODE_DETERMINISTIC,
            note=(
                f"the model was unavailable ({type(exc).__name__}), so this answer was read "
                "verbatim instead of paraphrased"
            ),
        )
    return ExtractionOutcome(facts=tuple(facts), rejected=tuple(rejected), source=MODE_LLM)
