"""Enumerate broad ``except`` handlers and say which of them are silent.

The triage report quotes counts like "RED zone: 55 broad handlers, 25 of them
silent" and "submission_recovery.py: 8 silent out of 9". Those numbers came from
an ad-hoc scan that was never saved, which makes them unverifiable -- the report
itself has a section saying a measurement you cannot repeat is not a measurement.
This is that scan, saved.

Definitions, kept deliberately narrow so the numbers mean something:

  broad    -- ``except Exception``, ``except BaseException`` or a bare
              ``except:``. A typed handler (``except ValueError``) is not
              counted: it is a statement about what can go wrong.
  silent   -- the body contains no ``raise``, no call on a name that looks like
              a logger (``logger.*``, ``log.*``, ``logging.*``), and never uses
              the bound exception name. Such a handler answers "did it work?"
              with "yes" regardless of what happened.

Silent is not the same as wrong. A top-level watchdog that must survive a bad
iteration is legitimately silent. The point of the scan is to produce the list
of places that need reading, not a verdict -- that is why it prints line numbers
and the first statement of each body.

Usage:
    python tools/ble001_silent_scan.py                       # whole package
    python tools/ble001_silent_scan.py ai_assistant/hh_submission.py
    python tools/ble001_silent_scan.py --only-silent <files>

Exit code is 0 when the scan ran. It does not fail on findings: findings are the
normal state of this repository, and a non-zero exit would make the tool useless
in a pipeline.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = [REPO / "ai_assistant"]

LOGGERISH = ("logger", "log", "logging", "_log", "LOG")


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """True for ``except Exception``, ``except BaseException``, bare ``except:``."""
    if handler.type is None:
        return True
    node = handler.type
    if isinstance(node, ast.Name):
        return node.id in {"Exception", "BaseException"}
    if isinstance(node, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id in {"Exception", "BaseException"} for e in node.elts)
    return False


def _calls_logger(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in LOGGERISH:
            return True
    return False


def _uses_exception(handler: ast.ExceptHandler) -> bool:
    name = handler.name
    if not name:
        return False
    for node in ast.walk(handler):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            return True
    return False


def _raises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) for node in ast.walk(handler))


def _first_statement(handler: ast.ExceptHandler) -> str:
    if not handler.body:
        return "<empty>"
    return ast.unparse(handler.body[0])[:70]


def scan_file(path: Path) -> list[tuple[int, bool, str]]:
    """Return ``(lineno, silent, first_statement)`` for every broad handler."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        print(f"  !! {path}: cannot parse: {exc}", file=sys.stderr)
        return []
    out: list[tuple[int, bool, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
            continue
        silent = not (_calls_logger(node) or _uses_exception(node) or _raises(node))
        out.append((node.lineno, silent, _first_statement(node)))
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="files or directories (default: ai_assistant/)")
    ap.add_argument("--only-silent", action="store_true", help="list only silent handlers")
    args = ap.parse_args()

    targets: list[Path] = []
    missing: list[Path] = []
    for raw in args.paths or DEFAULT_TARGETS:
        p = Path(raw)
        if p.is_dir():
            targets.extend(sorted(p.rglob("*.py")))
        elif p.is_file():
            targets.append(p)
        else:
            # Fail loudly. The first version skipped unknown paths in silence and
            # printed "broad handlers: 0" -- a measurer that answers "nothing to
            # report" when it could not look is the exact defect this sweep hunts.
            missing.append(p)
    if missing:
        print("!! these paths do not exist (check MSYS path conversion for python.exe):", file=sys.stderr)
        for p in missing:
            print(f"   {p}", file=sys.stderr)
        return 2
    targets = [p for p in targets if "__pycache__" not in p.parts]
    if not targets:
        print("!! no .py files matched -- refusing to report an empty scan as a clean one", file=sys.stderr)
        return 2

    total_broad = 0
    total_silent = 0
    worst: list[tuple[int, int, Path]] = []

    for path in targets:
        rows = scan_file(path)
        if not rows:
            continue
        broad = len(rows)
        silent = sum(1 for _, s, _ in rows if s)
        total_broad += broad
        total_silent += silent
        if silent:
            worst.append((silent, broad, path))
        shown = [r for r in rows if r[1] or not args.only_silent]
        if not shown:
            continue
        try:
            rel = path.relative_to(REPO)
        except ValueError:
            rel = path
        print(f"\n{rel}  --  broad {broad}, silent {silent}")
        for lineno, is_silent, first in shown:
            mark = "SILENT" if is_silent else "      "
            print(f"  {mark}  {rel}:{lineno}  {first}")

    print("\n=== totals ===")
    print(f"broad handlers : {total_broad}")
    print(f"silent         : {total_silent}")
    print("\nworst files (silent, of broad):")
    for silent, broad, path in sorted(worst, reverse=True)[:12]:
        try:
            rel = path.relative_to(REPO)
        except ValueError:
            rel = path
        print(f"  {silent:3d} of {broad:3d}  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
