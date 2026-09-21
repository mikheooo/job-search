"""Attribute the report's advisory flagged-reference count to a cause.

The ratchet in `tools/report_line_refs.py --symbols` is a count, not an
explanation: when it moves, the question "what moved it" has to be answered by
substitution, not by arithmetic on line offsets. This tool answers it two ways,
and both hold one axis fixed so the other is readable.

  * `--by-commit` moves the DOCUMENT axis: every commit's version of the report
    is fed to the checker while the sources stay at the working tree. Use it to
    see which commit's report text raised or lowered the count.
  * default moves the SOURCE axis: the production files that a range of
    findings edited are temporarily replaced with their pre-range versions, the
    count is taken, and everything is written back. Use it to see whether your
    own code edits caused flags -- and which references they exposed.

Nothing is deleted. The source mode restores every byte it touched and prints
the verified hashes before any verdict, and it refuses to print a verdict if
the restore did not match.

    python tools/ble001_ratchet_attribution.py --by-commit
    python tools/ble001_ratchet_attribution.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import report_line_refs as R

DOC = "docs/ble001_triage.md"

# The range whose source edits are under test, and the commit just before it.
BEFORE = "5879f66"
FILES = [
    "ai_assistant/application_integrity.py",
    "ai_assistant/application_prep.py",
    "ai_assistant/application_review.py",
    "ai_assistant/browser_executor.py",
    "ai_assistant/hh_application_runner.py",
    "ai_assistant/hh_controlled_submit.py",
    "ai_assistant/hh_submission.py",
    "ai_assistant/hh_submit_policy.py",
    "ai_assistant/submission_recovery.py",
]

# Report text per commit. The label is what the delta is attributed to.
COMMITS = [
    ("d5b0690", "#50"),
    ("68a1c00", "#51"),
    ("b1f9a49", "#52"),
    ("5879f66", "#53"),
    ("ded0002", "#55"),
    ("9ae1987", "#56"),
    ("2df4602", "#57"),
    ("08a72e1", "#58"),
    ("ea6b2b7", "#59"),
    ("a2311b6", "#60"),
    ("53fb0c2", "#61"),
    ("WORKTREE", "now (working tree)"),
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob(rev: str, path: str) -> bytes:
    if rev == "WORKTREE":
        return (REPO / path).read_bytes()
    return subprocess.run(
        ["git", "show", f"{rev}:{path}"], cwd=REPO,
        capture_output=True, check=True,
    ).stdout


def report_text(rev: str) -> str:
    return blob(rev, DOC).decode("utf-8")


def measure(text: str) -> tuple[int, list[tuple]]:
    return R.symbol_worklist([DOC], {DOC: text}, R.tracked_py())


def by_commit() -> int:
    print(f"{'commit':9s} {'named':>6s} {'flagged':>8s}  delta  what")
    prev = None
    for rev, what in COMMITS:
        checked, flagged = measure(report_text(rev))
        delta = "" if prev is None else f"{len(flagged) - prev:+d}"
        prev = len(flagged)
        print(f"{rev:9s} {checked:6d} {len(flagged):8d}  {delta:>5s}  {what}")
    return 0


def by_source() -> int:
    saved = {f: (REPO / f).read_bytes() for f in FILES}
    before = {f: sha(b) for f, b in saved.items()}
    current_text = (REPO / DOC).read_bytes().decode("utf-8")

    try:
        _, flagged = measure(current_text)
        print(f"current sources : flagged={len(flagged)}")

        for f in FILES:
            (REPO / f).write_bytes(blob(BEFORE, f))
        _, flagged_old = measure(current_text)
        print(f"pre-{BEFORE} sources : flagged={len(flagged_old)}")
    finally:
        for f, data in saved.items():
            (REPO / f).write_bytes(data)

    bad = [f for f, h in before.items() if sha((REPO / f).read_bytes()) != h]
    if bad:
        print("REFUSING A VERDICT: restore failed for")
        for f in bad:
            print("   ", f)
        return 1
    print(f"tree restored: {len(FILES)} files, sha256 verified")

    def key(rows: list[tuple]) -> set[str]:
        return {f"{r[2]}:{r[3]}" for r in rows}

    now, old = key(flagged), key(flagged_old)
    print(f"flagged only with the edits present ({len(now - old)}):")
    for ref in sorted(now - old):
        print("   +", ref)
    print(f"flagged only with pre-{BEFORE} sources ({len(old - now)}):")
    for ref in sorted(old - now):
        print("   -", ref)
    return 0


def main(argv: list[str]) -> int:
    if "--by-commit" in argv:
        return by_commit()
    return by_source()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
