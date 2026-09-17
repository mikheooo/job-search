#!/usr/bin/env python
"""Do the report's `file.py:NNNN` references still point inside their files?

A line number past the end of its file is the cheapest possible tell that a
reference has gone stale, and it needs no knowledge of the symbol at all - which
matters here, because the report cites bare basenames ("cli.py:3312") and the
symbol at a location is a separate, noisier question.

The line count is taken the way `grep -n` counts: LF only. That convention is
declared in docs/ble001_triage.md, and it is not pedantry - `cli.py` carries
6101 bare CR on top of 5946 LF, so an editor (VS Code, Sublime) reports roughly
twice as many lines as grep does for the same file.

Deliberately quoted stale references are listed in QUOTED below with a reason:
the report discusses its own past mistakes, and those examples must not fail
the check.

Usage:  ./.venv/Scripts/python.exe tools/report_line_refs.py [doc.md ...]
Exit code is non-zero when a live reference is out of range.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DOCS = ["docs/ble001_triage.md"]

# (path, line) pairs that the report quotes on purpose as examples of a stale
# reference. Keep this list short and keep the reason next to it.
QUOTED = {
    ("cli.py", 6708): "quoted in trap 5 as an editor-coordinate reference",
    ("cli.py", 7011): "quoted in trap 5 as a reference that went past EOF",
    ("application_queue.py", 1142): "quoted in trap 5 / finding 5 as the old number",
}

REF = re.compile(r"`?([A-Za-z0-9_./\\-]+\.py):(\d+)`?")


def tracked_py() -> dict[str, list[str]]:
    out = subprocess.run(
        ["git", "ls-files", "--", "*.py"], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8", check=False,
    ).stdout.splitlines()
    by_base: dict[str, list[str]] = {}
    for p in out:
        by_base.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    return by_base


def grep_lines(path: Path) -> list[str]:
    """Split the way `grep -n` counts: LF only, CRLF normalised to LF."""
    return path.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n").split("\n")


def main(argv: list[str]) -> int:
    docs = argv or DEFAULT_DOCS
    by_base = tracked_py()
    problems, quoted, checked = [], 0, 0
    seen: set[tuple[str, int]] = set()

    for doc in docs:
        text = (REPO / doc).read_text(encoding="utf-8")
        for n, ln in enumerate(text.split("\n"), 1):
            for m in REF.finditer(ln):
                path, num = m.group(1), int(m.group(2))
                if (path, num) in seen:
                    continue
                seen.add((path, num))
                checked += 1

                hits = by_base.get(path.rsplit("/", 1)[-1], [])
                if len(hits) != 1:
                    problems.append(f"{doc}:{n}  {path}:{num}  -> "
                                    f"{'no such file' if not hits else 'ambiguous'}")
                    continue

                lines = grep_lines(REPO / hits[0])
                if num > len(lines):
                    if (path, num) in QUOTED:
                        quoted += 1
                    else:
                        problems.append(
                            f"{doc}:{n}  {path}:{num}  -> file has {len(lines)} lines"
                        )

    print(f"documents: {', '.join(docs)}")
    print(f"distinct file:line references: {checked}")
    print(f"  quoted as deliberate examples: {quoted}")
    if problems:
        print(f"  OUT OF RANGE: {len(problems)}")
        for p in problems:
            print(f"    {p}")
        return 1
    print("  out of range: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
