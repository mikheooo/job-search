"""The candidate's own projects, as a catalogue whose every claim is checkable.

This module exists because "Independent Projects" is the one section of a resume
where invention is easiest and least detectable. A project description cannot be
extracted from a resume, so somebody has to write it -- and the moment somebody
writes it, the provenance chain the rest of Stage 0 is built on snaps.

The way out taken here is to make every project claim **a quote**. A claim's
value is the whitespace-normalised text of an :class:`Anchor`, and an anchor
names a real file, a real line and the exact substring found there. Nothing is
paraphrased into the knowledge base, so nothing can drift from its source
without :func:`validate_catalogue` failing first: if a line moves, the quote
stops matching and the sync refuses to run. A silent omission is impossible for
the same reason -- a claim either has a matching anchor or it does not exist.

Three honest buckets, because "done" is not one thing:

``IMPLEMENTED``
    The code is in the repository and the repository's own documentation
    describes it as complete for its stated stage.
``PROTOTYPE``
    Deliberately non-authoritative: a mock, or a research harness whose
    validation failed. Repiton is here, and its ``NO-GO`` is quoted rather than
    hidden -- a measured negative result is still a result.
``PLAN``
    Documented but not built, and explicitly marked as blocked or pending.

The module is deterministic and offline: no LLM, no network, no clock. The only
external call is ``git`` for repository metadata, and that metadata is confined
to the source map -- the resume itself carries no commit counts, so a commit
cannot change the candidate's resume.

Nothing here writes except :func:`candidate_kb_projects_cmd`, and it only ever
reaches the knowledge base through
:meth:`~.kb.CandidateKnowledgeBase.sync_project_claims`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .kb import (
    PROJECT_SOURCE,
    CandidateKnowledgeBase,
    ClaimRecord,
    ClaimStatus,
    resolve_kb_path,
)
from .resume_parser import make_claim_id

__all__ = [
    "KIND_PROJECT",
    "KIND_PROJECT_BULLET",
    "KIND_PROJECT_STACK",
    "KIND_PROJECT_STATUS",
    "KIND_PROJECT_SUMMARY",
    "PROJECTS_ROOT_ENV",
    "PROJECT_CATALOGUE",
    "STATUS_IMPLEMENTED",
    "STATUS_PLAN",
    "STATUS_PROTOTYPE",
    "Anchor",
    "ProjectSourceError",
    "ProjectSpec",
    "RepositoryFacts",
    "candidate_kb_projects_cmd",
    "extract_project_claims",
    "repository_facts",
    "resolve_projects_root",
    "validate_catalogue",
]

#: Where the repositories live. The catalogue stores paths relative to this
#: root, so nothing machine-specific is baked into the repository.
PROJECTS_ROOT_ENV = "CANDIDATE_PROJECTS_ROOT"

KIND_PROJECT = "project"
KIND_PROJECT_SUMMARY = "project_summary"
KIND_PROJECT_STATUS = "project_status"
KIND_PROJECT_STACK = "project_stack"
KIND_PROJECT_BULLET = "project_bullet"

STATUS_IMPLEMENTED = "IMPLEMENTED"
STATUS_PROTOTYPE = "PROTOTYPE"
STATUS_PLAN = "PLAN"


class ProjectSourceError(RuntimeError):
    """An anchor does not match its source file.

    Raised rather than logged. A project claim whose quote cannot be found is a
    claim about nothing, and writing it to the knowledge base would put an
    unverifiable statement into the document that gets sent to employers.
    """


@dataclass(frozen=True)
class Anchor:
    """One verbatim quote, and where to find it.

    ``quote`` may span several lines: the validator joins the lines from
    ``line`` onwards and checks the quote is a substring of that text. Quoting a
    whole paragraph is therefore as checkable as quoting a single word.

    ``kind`` is explicit rather than positional. A bullet accidentally typed as
    a summary would move a paragraph of project documentation into the resume's
    opening line, and nothing downstream could tell that it had happened.
    """

    rel_path: str
    line: int
    quote: str
    kind: str

    def normalized(self) -> str:
        """The quote as one line, which is what the knowledge base stores.

        A line that ends in a hyphen is joined without a space, because that is
        what the hyphen is there for: ``evidence-\\nbacked`` is one word broken
        across two lines, not two words. Getting this wrong puts
        ``evidence- backed`` into the resume, which reads as a typo.
        """
        parts = self.quote.split("\n")
        joined = parts[0]
        for part in parts[1:]:
            if joined.endswith("-"):
                joined += part.lstrip()
            else:
                joined += " " + part.strip()
        return " ".join(joined.split())


@dataclass(frozen=True)
class ProjectSpec:
    """One project, with the quotes that describe it."""

    project_id: str
    name: str
    rel_repo: str
    status: str
    anchors: tuple[Anchor, ...]

    def anchors_of(self, kind: str) -> tuple[Anchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.kind == kind)


@dataclass(frozen=True)
class RepositoryFacts:
    """What ``git`` says about a repository. Source map only, never the resume."""

    path: str
    exists: bool
    remote: str | None = None
    first_commit_date: str | None = None
    last_commit_date: str | None = None
    commit_count: int | None = None


def resolve_projects_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Explicit path wins, then ``CANDIDATE_PROJECTS_ROOT``, then the home directory."""
    if explicit:
        return Path(explicit)
    from_env = os.getenv(PROJECTS_ROOT_ENV)
    if from_env:
        return Path(from_env)
    return Path.home()


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
#
# Every entry below is a quote. Read the anchor, open the file, check the line.
# Adding a project means adding an anchor, not adding a sentence.

PROJECT_CATALOGUE: tuple[ProjectSpec, ...] = (
    ProjectSpec(
        project_id="job_search_ai",
        name="Job Search AI (Job Search Hub)",
        rel_repo="Documents/job-search",
        status=STATUS_IMPLEMENTED,
        anchors=(
            Anchor("README.md", 1, "# job-search", KIND_PROJECT),
            Anchor(
                "README.md",
                3,
                "Personal job-search automation system. Collects remote vacancies, scores them\n"
                "against a candidate profile, prepares tailored applications, and — behind\n"
                "explicit safety gates — can fill and submit HH.ru application forms, reply to\n"
                "HH messages, and prepare email replies.",
                KIND_PROJECT_SUMMARY,
            ),
            Anchor(
                "README.md",
                8,
                "Everything that touches the outside world (browser, HH, email) is gated:\n"
                "default mode is REVIEW (human decision required); AUTO modes are strictly\n"
                "opt-in via environment kill-switches and never guess facts.",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "README.md",
                14,
                "- Collects vacancies from remote and tech job boards (habrcareer, remoteok,\n"
                "  weworkremotely, himalayas; legacy: linkedin, wellfound).",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "README.md",
                26,
                "- Controlled application flow: plan → prefill → human review gate → single\n"
                "  gated submit → read-only verification.",
                KIND_PROJECT_BULLET,
            ),
        ),
    ),
    ProjectSpec(
        project_id="reels_analyzer",
        name="Reels Analyzer",
        rel_repo="reels_bot",
        status=STATUS_IMPLEMENTED,
        anchors=(
            Anchor("README.md", 1, "# Reels Analyzer (reels_bot)", KIND_PROJECT),
            Anchor(
                "README.md",
                3,
                "A Telegram bot and asynchronous worker pipeline that analyzes short-form video\n"
                "(Instagram Reels, TikTok, YouTube Shorts): extracts transcripts and visual\n"
                "evidence, checks claims against sources, and produces structured, evidence-\n"
                "backed analysis results.",
                KIND_PROJECT_SUMMARY,
            ),
            Anchor(
                "README.md",
                10,
                "- Accepts video URLs via a Telegram bot (aiogram) and queues them for\n"
                "  processing (Redis + ARQ async task queue, PostgreSQL job/task store).",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "README.md",
                16,
                "- Fact-checks claims against web sources (Exa) with QA controls.",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "README.md",
                38,
                "Python 3.13, aiogram, ARQ + Redis, PostgreSQL (asyncpg/SQLAlchemy), Pydantic,\n"
                "Google Gemini API, Exa, Docker / docker-compose.",
                KIND_PROJECT_STACK,
            ),
        ),
    ),
    ProjectSpec(
        project_id="repiton",
        name="Repiton",
        rel_repo="Documents/Repiton",
        status=STATUS_PROTOTYPE,
        anchors=(
            Anchor("README.md", 1, "# REPITON", KIND_PROJECT),
            Anchor(
                "README.md",
                3,
                "REPITON — AI-тренер произношения. Он учит говорить так, чтобы носитель языка "
                "правильно понял слово, а не просто подтверждает, что Speech-to-Text угадал текст.",
                KIND_PROJECT_SUMMARY,
            ),
            Anchor(
                "README.md",
                7,
                "`Listen → Repeat → Analyze → Explain → Retry → Master`",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "README.md",
                11,
                "Stage 1 остаётся **NO-GO**: текущая модель неустойчива между дикторами, "
                "а learner/error gold evidence отсутствует.",
                KIND_PROJECT_STATUS,
            ),
            Anchor(
                "PROJECT.md",
                35,
                "**STAGE_1_FINAL_NO_GO.** По итогам однократного sealed-final эксперимента "
                "на 1 218 записях 6 независимых дикторов C-COST (macro-F1 = 0.702 < 0.80, "
                "low recall = 0.318 < 0.65)",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "PROJECT.md",
                39,
                "- **TRACK B — PRONUNCIATION SCORER VALIDATION**: `BLOCKED BY STAGE_1_FINAL_NO_GO`",
                KIND_PROJECT_STATUS,
            ),
        ),
    ),
    ProjectSpec(
        project_id="whubn",
        name="WHUBN",
        rel_repo="Documents/WHUBN",
        status=STATUS_IMPLEMENTED,
        anchors=(
            Anchor("README.md", 1, "# WHUBN", KIND_PROJECT),
            Anchor(
                "README.md",
                5,
                "WHUBN is a local-first iOS app that reconstructs where a person's life happened "
                "from permitted location evidence such as photo metadata and location-history imports.",
                KIND_PROJECT_SUMMARY,
            ),
            Anchor(
                "README.md",
                21,
                "- High-throughput SQLite persistence engine with WAL and Sendable Swift 6 concurrency",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "README.md",
                55,
                "- Interactive 2D life map using Apple MapKit (`AtlasMapView`, `AtlasMKMapView`)",
                KIND_PROJECT_BULLET,
            ),
            Anchor(
                "README.md",
                64,
                "- Deterministic chronological segmentation of `LocationEvent` records into "
                "canonical `Stay` records (`StaySegmentationEngine`)",
                KIND_PROJECT_BULLET,
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check_anchor(repo_dir: Path, spec: ProjectSpec, anchor: Anchor) -> str:
    """Return the anchor's normalised text, or raise if it is not where it says."""
    target = repo_dir / anchor.rel_path
    if not target.is_file():
        raise ProjectSourceError(
            f"{spec.project_id}: {anchor.rel_path} does not exist under {repo_dir}"
        )
    lines = target.read_text(encoding="utf-8").splitlines()
    if anchor.line < 1 or anchor.line > len(lines):
        raise ProjectSourceError(
            f"{spec.project_id}: {anchor.rel_path} has {len(lines)} lines, so line "
            f"{anchor.line} cannot hold {anchor.normalized()!r}"
        )
    span = anchor.quote.count("\n") + 1
    window = "\n".join(lines[anchor.line - 1 : anchor.line - 1 + span])
    if anchor.quote not in window:
        raise ProjectSourceError(
            f"{spec.project_id}: {anchor.rel_path}:{anchor.line} no longer contains "
            f"{anchor.normalized()!r}. The source moved or was reworded; the catalogue "
            f"must be re-anchored before anything is written."
        )
    return anchor.normalized()


def validate_catalogue(
    root: str | os.PathLike[str] | None = None,
    catalogue: tuple[ProjectSpec, ...] = PROJECT_CATALOGUE,
) -> dict[str, int]:
    """Check every anchor against the file it names. Returns project -> anchor count.

    Raises :class:`ProjectSourceError` on the first anchor that does not match.
    """
    projects_root = resolve_projects_root(root)
    counts: dict[str, int] = {}
    for spec in catalogue:
        repo_dir = projects_root / spec.rel_repo
        for anchor in spec.anchors:
            _check_anchor(repo_dir, spec, anchor)
        counts[spec.project_id] = len(spec.anchors)
    return counts


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _claim(
    kind: str,
    value: str,
    *,
    spec: ProjectSpec,
    anchor: Anchor,
    repo_dir: Path,
    scope: str,
    parent_id: str | None,
) -> ClaimRecord:
    """Build one claim whose evidence points at the file the words came from."""
    target = repo_dir / anchor.rel_path
    evidence: dict[str, object] = {
        # The marker, exactly as the interview does it, so this claim can never
        # be mistaken for something the resume parser read.
        "source_file": PROJECT_SOURCE,
        "project_id": spec.project_id,
        # The honest bucket: implemented, prototype or plan.
        "project_status": spec.status,
        # The real path and line, so a human can open the file and check.
        "source_path": str(target),
        "source_rel_path": anchor.rel_path,
        "line": anchor.line,
        "quote": anchor.quote,
        "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    return ClaimRecord(
        claim_id=make_claim_id(kind, scope, value),
        kind=kind,
        value=value,
        status=ClaimStatus.DERIVED_FROM_SOURCE,
        normalized=spec.project_id,
        role_id=None,
        parent_id=parent_id,
        evidence=evidence,
        in_source=True,
    )


def extract_project_claims(
    root: str | os.PathLike[str] | None = None,
    catalogue: tuple[ProjectSpec, ...] = PROJECT_CATALOGUE,
) -> list[ClaimRecord]:
    """Every project claim, in catalogue order, with its anchors already checked.

    Validates first, so a drifted anchor stops the whole extraction rather than
    producing a partial set that looks complete.
    """
    validate_catalogue(root, catalogue)
    projects_root = resolve_projects_root(root)

    records: list[ClaimRecord] = []
    for spec in catalogue:
        repo_dir = projects_root / spec.rel_repo
        identity_anchor = spec.anchors_of(KIND_PROJECT)[0]
        identity = _claim(
            KIND_PROJECT,
            spec.name,
            spec=spec,
            anchor=identity_anchor,
            repo_dir=repo_dir,
            scope=spec.project_id,
            parent_id=None,
        )
        records.append(identity)
        for anchor in spec.anchors:
            if anchor.kind == KIND_PROJECT:
                continue
            records.append(
                _claim(
                    anchor.kind,
                    anchor.normalized(),
                    spec=spec,
                    anchor=anchor,
                    repo_dir=repo_dir,
                    scope=identity.claim_id,
                    parent_id=identity.claim_id,
                )
            )
    return records


# ---------------------------------------------------------------------------
# Repository metadata -- source map only
# ---------------------------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _first_line(text: str | None) -> str | None:
    """``git log`` prints one line per commit; the source map wants the first."""
    if not text:
        return None
    lines = text.splitlines()
    return lines[0] if lines else None


def repository_facts(
    root: str | os.PathLike[str] | None = None,
    catalogue: tuple[ProjectSpec, ...] = PROJECT_CATALOGUE,
) -> list[RepositoryFacts]:
    """Live git metadata for each repository. Never stored, never in the resume.

    A commit count living in the knowledge base would change on every commit and
    make the resume churn for no reason. It is evidence for the source map, which
    is regenerated whenever the resume is.
    """
    projects_root = resolve_projects_root(root)
    facts: list[RepositoryFacts] = []
    for spec in catalogue:
        repo_dir = projects_root / spec.rel_repo
        if not (repo_dir / ".git").exists():
            facts.append(RepositoryFacts(path=str(repo_dir), exists=False))
            continue
        count = _git(repo_dir, "rev-list", "--count", "HEAD")
        facts.append(
            RepositoryFacts(
                path=str(repo_dir),
                exists=True,
                remote=_first_line(_git(repo_dir, "remote", "get-url", "origin")),
                first_commit_date=_first_line(
                    _git(repo_dir, "log", "--reverse", "--format=%ad", "--date=short")
                ),
                last_commit_date=_first_line(
                    _git(repo_dir, "log", "-1", "--format=%ad", "--date=short")
                ),
                commit_count=int(count) if count and count.isdigit() else None,
            )
        )
    return facts


# ---------------------------------------------------------------------------
# Command layer
# ---------------------------------------------------------------------------


def _truncate(value: str, width: int = 58) -> str:
    return value if len(value) <= width else value[: width - 3] + "..."


def candidate_kb_projects_cmd(
    root: str | None = None,
    kb_path: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> int:
    """``candidate-kb projects`` -- fold the project catalogue into the KB.

    Additive by construction: it can add a claim and it can refresh a claim that
    already carries project provenance. It cannot delete, cannot change a status
    and cannot touch a resume claim or an interview answer.
    """
    try:
        records = extract_project_claims(root)
    except ProjectSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 2

    if dry_run:
        if as_json:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "claim_count": len(records),
                        "claims": [record.to_dict() for record in records],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        print(f"project catalogue: {len(records)} claims (dry run, nothing written)")
        for record in records:
            print(
                f"  {record.claim_id:<34} {record.kind:<18} {_truncate(record.value)}  "
                f"[{record.evidence['source_rel_path']}:{record.evidence['line']}]"
            )
        return 0

    kb_file = resolve_kb_path(kb_path)
    if not kb_file.is_file():
        print(f"ERROR: no knowledge base at {kb_file}", file=sys.stderr)
        print("Run `candidate-kb extract` first to build it.", file=sys.stderr)
        return 2

    knowledge_base = CandidateKnowledgeBase.load(kb_file)
    report = knowledge_base.sync_project_claims(records)
    knowledge_base.save(kb_file)

    if as_json:
        print(
            json.dumps(
                {
                    "dry_run": False,
                    "kb_path": str(kb_file),
                    "claim_count": len(records),
                    "added": report.added,
                    "refreshed": report.refreshed,
                    "unchanged": report.unchanged,
                    "kb_version": report.kb_version,
                    "claims": [record.to_dict() for record in records],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(f"knowledge base : {kb_file}")
    print(f"project sync   : {report.summary()}")
    for record in records:
        print(
            f"  {record.claim_id:<34} {record.kind:<18} {_truncate(record.value)}  "
            f"[{record.evidence['source_rel_path']}:{record.evidence['line']}]"
        )
    return 0
