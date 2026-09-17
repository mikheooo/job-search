"""Census: which tracked files carry bare CR, so `grep -n` and an editor disagree.

The triage report cites `file.py:NNNN`. That number means two different things
depending on who is counting:

  * `grep -n`, and every tool built on LF -- splits on `\\n` only;
  * Python's `str.splitlines()`, VS Code, Sublime, Notepad++ -- treat a bare
    `\\r` as a line break too.

`cli.py` is the famous case: 5946 LF plus **6101 bare CR**, so the same call sits
at 3312 by grep and around 6708 in an editor. The report documented that one file
because that is where the drift bit. It never asked how many other files have the
same property, which is how a reference into `application_queue.py` (769 bare CR)
stayed silently ambiguous.

This tool answers that question over the tracked set -- the set the report can
actually reference.

Exit codes: 0 when the census ran, 2 when it could not look. A census that prints
"nothing found" because git failed is the exact defect this sweep hunts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Text formats worth counting. Binary files can contain 0x0D by accident and
# mean nothing by it.
SUFFIXES = {".py", ".md", ".toml", ".json", ".txt", ".yaml", ".yml", ".js", ".cfg", ".ini"}

CR = b"\r"
LF = b"\n"
CRLF = b"\r\n"


def tracked_text_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        print(f"!! git ls-files failed: {out.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    files = [
        line for line in out.stdout.splitlines()
        if line.strip() and Path(line).suffix in SUFFIXES
    ]
    if not files:
        print("!! git ls-files returned no text files -- refusing to report an empty census",
              file=sys.stderr)
        sys.exit(2)
    return files


def shifting_bare_cr(data: bytes) -> int:
    """Bare CRs that can move a line number: all but a trailing run at EOF.

    A lone `\\r` in the trailing whitespace adds an empty final "line" and
    nothing else -- no reference points past it, so no number shifts.
    Measured: tests/test_submission_verifier.py carries exactly one such CR, and
    that is why its two line counts agree (2477 and 2477) despite it appearing
    in the census.
    """
    body = data.rstrip(b"\r\n \t")
    return body.count(CR) - body.count(CRLF)


def main() -> int:
    rows = []
    missing = []
    for rel in tracked_text_files():
        path = REPO / rel
        if not path.is_file():
            missing.append(rel)
            continue
        data = path.read_bytes()
        crlf = data.count(CRLF)
        bare_cr = data.count(CR) - crlf
        if bare_cr:
            rows.append(
                (shifting_bare_cr(data), bare_cr, crlf, data.count(LF) - crlf, rel, len(data))
            )

    if missing:
        print("!! tracked files not present on disk (cannot count them):", file=sys.stderr)
        for rel in missing:
            print(f"   {rel}", file=sys.stderr)
        return 2

    rows.sort(reverse=True)
    print(f"tracked text files scanned : {len(tracked_text_files())}")
    print(f"files carrying bare CR     : {len(rows)}")
    for shifting, bare_cr, crlf, bare_lf, rel, size in rows:
        grep_lines = crlf + bare_lf + 1
        py_lines = crlf + bare_cr + bare_lf
        print()
        print(f"  {rel}")
        print(f"    bare CR={bare_cr} (of them line-shifting: {shifting})"
              f"  CRLF={crlf}  bare LF={bare_lf}  bytes={size}")
        print(f"    grep -n would see ~{grep_lines} lines, an editor ~{py_lines}")
        if shifting and py_lines > grep_lines:
            print(f"    AMBIGUOUS band: line numbers 1..{grep_lines} are in range for both"
                  f" systems and mean different places; {grep_lines + 1}..{py_lines} are"
                  f" in range only for an editor")
        elif not shifting:
            print("    no line-shifting bare CR: every number means the same in both systems")
    if not rows:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
