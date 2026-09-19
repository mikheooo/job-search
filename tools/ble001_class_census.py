#!/usr/bin/env python
"""Split the remaining BLE001 sites into "kept a trace" and "discarded it".

The triage's own method is one question per handler:

    Does the handler keep ANY trace of the error - a log, a `raise`, or the
    exception's name reaching a report/dict/return value?

Class A ("kept a trace") cannot hide a fail-open: whatever went wrong is
visible somewhere. Only class B ("discarded") can, and that is where the
candidates are. The report asserts the split in prose ("Класс A целиком
(166 шт.)"); this tool measures it, because a count that nobody re-runs is a
count that drifts.

Classification is mechanical and deliberately conservative:

  * TRACE if the body contains a `raise`, a logger call (`.warning/.error/
    .exception/.critical/.info/.debug/.log`), a `print(...)`, or an f-string /
    `.format()` that interpolates the bound exception name.
  * DISCARD otherwise.

`print` counts as a trace but is reported separately, because a trace on stdout
of a batch job is weaker than a log line - saying "kept a trace" without that
distinction would be the kind of rounding this triage keeps catching.

Line numbers are reported in the `grep` system (the one the report declares),
which is also ruff's: ruff splits on `\\n`, and so does this tool. Files that
carry bare CRs are marked, because there an editor and grep disagree.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_class_census.py
    ./.venv/Scripts/python.exe tools/ble001_class_census.py --discard-only
    ./.venv/Scripts/python.exe tools/ble001_class_census.py --discard-only --file hh_submission
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "docs" / "ble001_triage.md"
TARGET = "ai_assistant/"
LOGGER = re.compile(r"\.(warning|error|exception|critical|info|debug|log)$")
ADDRESS = re.compile(r"([A-Za-z0-9_/\\]+\.py):(\d+)")

# A handler whose body is one of these shapes and nothing else discards the
# error by construction. Kept as a label, not as the decision - the decision is
# "no trace found", these just name the common forms.
SHAPES = (
    ("pass", lambda n: isinstance(n, ast.Pass)),
    ("continue", lambda n: isinstance(n, ast.Continue)),
    ("break", lambda n: isinstance(n, ast.Break)),
    ("return-const", lambda n: isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)),
    ("return-none", lambda n: isinstance(n, ast.Return) and n.value is None),
    ("return-empty", lambda n: isinstance(n, ast.Return) and isinstance(n.value, (ast.List, ast.Dict, ast.Tuple)) and not getattr(n.value, "elts", getattr(n.value, "keys", []))),
    ("assign-const", lambda n: isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)),
)


def ruff_sites() -> list[dict]:
    proc = subprocess.run(
        ["uvx", "ruff@0.16.6", "check", "--no-cache", "--select", "BLE001",
         "--output-format", "json", TARGET],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=300,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit("ruff did not return JSON; run it by hand and look") from None


def ast_line_starts(text: str) -> list[int]:
    """Offsets at which the PYTHON tokenizer starts each line.

    Not the same as `text.split("\\n")`: the tokenizer also breaks on a lone
    `\\r`, which is exactly how a bare-CR file gets two different numbering
    systems. This is what lets an ast line be translated back to grep's.

    A start is appended only AFTER a line break - appending one per iteration
    instead gives one entry per character. Measured: 3202 starts for an 86-line
    file, and every address came out as row 3.
    """
    starts, i, n = [0], 0, len(text)
    newline = re.compile(r"\r\n|\r|\n")
    while i < n:
        m = newline.match(text, i)
        if m:
            starts.append(m.end())
            i = m.end()
        else:
            i += 1
    return starts


class TraceFinder(ast.NodeVisitor):
    """Does the handler keep a trace of the error?

    The criterion is the triage's own: a `raise`, a log, or the exception's
    NAME reaching a report/dict/return value. The first version of this class
    only looked for `raise`/logger/`print`/f-string and therefore called
    `events.append(_audit_failure("REVIEW", e))` a discarded error - it is not,
    the name is passed straight into the audit trail. So the binding's USE is
    the primary test, and the specific forms are only labels on top of it.
    """

    def __init__(self) -> None:
        self.raise_ = False
        self.logger = False
        self.print_ = False
        self.named = False
        self.passed_on = False
        self.used = False
        self._call_depth = 0

    def visit_Raise(self, node: ast.Raise) -> None:
        self.raise_ = True
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and LOGGER.search(f".{func.attr}"):
            self.logger = True
        elif isinstance(func, ast.Name) and func.id == "print":
            self.print_ = True
        self._call_depth += 1
        self.generic_visit(node)
        self._call_depth -= 1

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self.named = True
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.used = True
        if self._call_depth:
            self.passed_on = True
        self.generic_visit(node)


def classify(handler: ast.ExceptHandler) -> tuple[str, str, bool]:
    finder = TraceFinder()
    for stmt in handler.body:
        finder.visit(stmt)
    if finder.raise_:
        return "A", "raise", True
    if finder.logger:
        return "A", "log", True
    if finder.print_:
        return "A", "print", True
    # The name of the binding is used somewhere in the body: the error is
    # carried out of the handler, whatever it is carried into.
    if handler.name and finder.used:
        return "A", "passes-on" if finder.passed_on else "uses-name", True
    if finder.named and handler.name:
        return "A", "f-string", True
    label = "other"
    if len(handler.body) == 1:
        for name, pred in SHAPES:
            if pred(handler.body[0]):
                label = name
                break
    return "B", label, False


def named_addresses() -> set[tuple[str, int]]:
    text = REPORT.read_text(encoding="utf-8")
    out = set()
    for path, num in ADDRESS.findall(text):
        out.add((Path(path).name, int(num)))
    return out


def main(argv: list[str]) -> int:
    discard_only = "--discard-only" in argv
    files = [a for a in argv if a.endswith(".py")]

    known = named_addresses()
    by_file: dict[str, list[tuple[int, str, str, bool]]] = {}
    counts = {"A": 0, "B": 0}
    labels: dict[str, int] = {}
    named_b = 0

    for site in ruff_sites():
        path = Path(site["filename"])
        if files and not any(f in str(path) for f in files):
            continue
        # Read the BYTES and decode without newline translation. `read_text()`
        # turns a lone \r into \n, so a bare-CR file silently gains lines and
        # every address drifts away from the grep system the report declares.
        text = path.read_bytes().decode("utf-8", "replace")
        starts = ast_line_starts(text)
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            print(f"!! {path.name}: cannot parse ({exc.msg}) - counted nowhere")
            continue
        ruff_row = site["location"]["row"]
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            handler_row = text[: starts[node.lineno - 1]].count("\n") + 1
            if handler_row != ruff_row:
                continue
            cls, label, _ = classify(node)
            counts[cls] += 1
            labels[label] = labels.get(label, 0) + 1
            is_named = (path.name, handler_row) in known
            if cls == "B" and is_named:
                named_b += 1
            by_file.setdefault(path.name, []).append((handler_row, cls, label, is_named))
            break

    total = counts["A"] + counts["B"]
    print(f"BLE001 handlers measured: {total}")
    print(f"  class A (kept a trace): {counts['A']}")
    print(f"  class B (discarded):    {counts['B']}")
    print()
    print("  class A by what kept the trace:")
    for label in ("raise", "log", "print", "passes-on", "uses-name", "f-string"):
        if labels.get(label):
            note = ""
            if label == "print":
                note = "  (stdout only - weaker than a log line)"
            elif label == "uses-name":
                note = "  (bound, used, but not handed to anything)"
            print(f"    {label:10s} {labels[label]:4d}{note}")
    print()
    print("  class B by shape:")
    for label, n in sorted(labels.items(), key=lambda kv: -kv[1]):
        if label in ("raise", "log", "print", "passes-on", "uses-name", "f-string"):
            continue
        print(f"    {label:12s} {n:4d}")

    if discard_only:
        print()
        print(f"  class B sites: {counts['B']} total, {named_b} already named in the report")
        print()
        header = f"    {'file':34s} {'line':>6s}  {'shape':12s} in report?"
        print(header)
        print("    " + "-" * (len(header) - 4))
        for name in sorted(by_file):
            rows = [r for r in by_file[name] if r[1] == "B"]
            if not rows:
                continue
            for row, _cls, label, is_named in sorted(rows):
                mark = "named" if is_named else "-"
                print(f"    {name:34s} {row:6d}  {label:12s} {mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
