"""Command layer for the candidate knowledge base (Stage 0 / T2).

Three commands, all of them explicit acts by the candidate:

* ``show``    -- list claims and their status (read-only);
* ``confirm`` -- mark exactly one claim as true;
* ``reject``  -- mark exactly one claim as rejected (the record is kept).

Nothing here decides anything on its own. In particular there is no path from
"the parser found it again" to "it is confirmed": re-running ``extract`` merges
and refreshes, and leaves every status exactly where the candidate put it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .kb import CandidateKnowledgeBase, ClaimStatus, resolve_kb_path

__all__ = [
    "candidate_kb_confirm_cmd",
    "candidate_kb_reject_cmd",
    "candidate_kb_show_cmd",
    "render_kb_summary",
]

_VALID_STATUSES = tuple(status.value for status in ClaimStatus)


def _load_or_report(kb_path: Path) -> CandidateKnowledgeBase | None:
    if not kb_path.is_file():
        print(f"ERROR: no knowledge base at {kb_path}", file=sys.stderr)
        print("Run `candidate-kb extract` first to build it.", file=sys.stderr)
        return None
    try:
        return CandidateKnowledgeBase.load(kb_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read knowledge base {kb_path}: {exc}", file=sys.stderr)
        return None


def _truncate(value: str, width: int = 62) -> str:
    return value if len(value) <= width else value[: width - 3] + "..."


def render_kb_summary(kb: CandidateKnowledgeBase, kb_path: Path) -> str:
    """Header lines shared by every command, so the numbers always agree."""
    sha = kb.source_sha256[:16] if kb.source_sha256 else "?"
    lines = [
        f"knowledge base : {kb_path}",
        f"kb_version     : {kb.kb_version}",
        f"source         : {kb.source_file or '?'}  (sha256[:16] {sha})",
    ]
    if kb.updated_at:
        lines.append(f"updated_at     : {kb.updated_at}")
    counts = kb.counts()
    lines.append(
        "statuses       : "
        + ", ".join(f"{status} {counts.get(status, 0)}" for status in _VALID_STATUSES)
    )
    absent = sum(1 for record in kb.claims.values() if not record.in_source)
    lines.append(f"absent records : {absent} (kept, no longer present in the source)")
    return "\n".join(lines)


def candidate_kb_show_cmd(
    status: str | None = None,
    as_json: bool = False,
    kb_path: str | None = None,
) -> int:
    """``candidate-kb show`` -- read-only listing. Returns a process exit code."""
    path = resolve_kb_path(kb_path)
    if status is not None and status not in _VALID_STATUSES:
        print(
            f"ERROR: unknown status {status!r}; expected one of {', '.join(_VALID_STATUSES)}",
            file=sys.stderr,
        )
        return 2
    kb = _load_or_report(path)
    if kb is None:
        return 2

    if as_json:
        payload = {
            "kb_path": str(path),
            "kb_version": kb.kb_version,
            "source_file": kb.source_file,
            "source_sha256": kb.source_sha256,
            "updated_at": kb.updated_at,
            "counts": kb.counts(),
            "claims": [record.to_dict() for record in kb.by_status(status)],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(render_kb_summary(kb, path))
    statuses = (status,) if status else _VALID_STATUSES
    for current in statuses:
        records = kb.by_status(current)
        print()
        print(f"[{current}] {len(records)}")
        for record in records:
            marker = "" if record.in_source else "  ABSENT_FROM_SOURCE"
            print(
                f"  {record.claim_id:<28} {record.kind:<20} "
                f"{_truncate(record.value):<64} {record.line_ref()}{marker}"
            )
    return 0


def _act_on_one(claim_id: str, kb_path: str | None, *, confirm: bool) -> int:
    path = resolve_kb_path(kb_path)
    kb = _load_or_report(path)
    if kb is None:
        return 2

    record = kb.get(claim_id)
    if record is None:
        print(f"ERROR: no claim with id {claim_id!r} in {path}", file=sys.stderr)
        print("Run `candidate-kb show` to list claim ids.", file=sys.stderr)
        return 2

    before = record.status
    if confirm:
        kb.confirm(claim_id)
    else:
        kb.reject(claim_id)
    record = kb.get(claim_id)
    # The lookup above already proved the record exists; this assertion only
    # guards a future refactor that might drop it.
    assert record is not None

    verb = "confirm" if confirm else "reject"
    if before == record.status:
        print(f"{verb}: {claim_id} already {record.status} (no change, nothing written)")
        return 0

    kb.save(path)
    print(f"{verb}: {claim_id}")
    print(f"  kind      : {record.kind}")
    print(f"  value     : {record.value}")
    print(f"  evidence  : {record.line_ref()}")
    print(f"  status    : {before} -> {record.status}")
    print(f"  written to: {path}")
    return 0


def candidate_kb_confirm_cmd(claim_id: str, kb_path: str | None = None) -> int:
    """``candidate-kb confirm <id>`` -- the only way a claim becomes CONFIRMED."""
    return _act_on_one(claim_id, kb_path, confirm=True)


def candidate_kb_reject_cmd(claim_id: str, kb_path: str | None = None) -> int:
    """``candidate-kb reject <id>`` -- status changes, the record stays."""
    return _act_on_one(claim_id, kb_path, confirm=False)
