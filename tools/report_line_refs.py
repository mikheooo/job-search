#!/usr/bin/env python
"""Do the report's `file.py:NNNN` references still point inside their files?

A line number past the end of its file is the cheapest possible tell that a
reference has gone stale, and it needs no knowledge of the symbol at all - which
matters here, because the report cites bare basenames ("cli.py:3312") and the
symbol at a location is a separate, noisier question.

Two counting systems exist and they disagree on some files:

  * `grep -n` (and everything built on LF) splits on `\\n` only;
  * Python's `splitlines()` and every GUI editor also treat a bare `\\r` as a
    line break.

`cli.py` carries 6101 bare CR on top of 5946 LF, so the same call sits at 3312
by grep and around 6708 in an editor. `application_queue.py` has the same
property (769 bare CR against 737 CRLF) and the report never mentioned it, so a
reference into that file meant two different places while only one was checked.

The counting system is therefore not this tool's private choice: THE DOCUMENT
DECLARES IT, and this tool reads the declaration and applies it. A reference into
a file whose numbering is ambiguous, with no declaration in the document, is a
failure -- that is the state in which the ambiguity went unnoticed. The census
behind these claims is reproducible: `tools/bare_cr_census.py`.

Deliberately quoted stale references are listed in QUOTED below with a reason:
the report discusses its own past mistakes, and those examples must not fail
the check.

Usage:  ./.venv/Scripts/python.exe tools/report_line_refs.py [doc.md ...]
        ./.venv/Scripts/python.exe tools/report_line_refs.py --ambiguous
        ./.venv/Scripts/python.exe tools/report_line_refs.py --symbols
Exit code is non-zero when a live reference is out of range, or when a reference
into a bare-CR file has no declared counting system.

--ambiguous prints, for every reference into a bare-CR file, the line content
under BOTH systems. Range checking cannot tell "correct in the declared system"
from "correct in the other one" -- both are in range for numbers below the grep
length. This mode is how that question gets answered by reading, once, instead of
by hoping.

--symbols is ADVISORY and never changes the exit code. It answers the question
range checking cannot: is the symbol the sentence names actually at the line it
cites? An in-range but rotted anchor is invisible to a range check -- measured
2026-09-17, a full sweep of the report found 26 such anchors (db.py:2173 -> 2232,
hh_submission.py:1436 -> 1518, ...), every one of them "in range".

The first attempt at this was rejected as too noisy ("take the first backticked
token to the right of the reference"): on 109 references it produced 10
mismatches, almost all artefacts, because what sits to the right is prose
(questionnaire_data), a keyword (pass) or a literal (True). The version here
takes names from the OTHER inline-code spans on the same line instead, drops
file names, and searches a window -- measured precision on this report is about
68% (21 real out of 31 flagged), and 21 further real anchors were found only by
reading, because their doc lines name no symbol at all. That is the honest
measure of how much of this is automation and how much is a human reading the
worklist. The false positives are known and listed in
FALSE_POSITIVE_CLASSES; they are the reason this stays a worklist, not a gate.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DOCS = ["docs/ble001_triage.md"]

CR = b"\r"
LF = b"\n"
CRLF = b"\r\n"

# (path, line) pairs that the report quotes on purpose as examples of a stale
# reference. Keep this list short and keep the reason next to it.
QUOTED = {
    ("cli.py", 6708): "quoted in trap 5 as an editor-coordinate reference",
    ("cli.py", 7011): "quoted in trap 5 as a reference that went past EOF",
    ("application_queue.py", 1142): "quoted in trap 5 / finding 5 as the old number",
}

# The declaration is a machine-readable marker, not a phrase. Matching prose was
# the first attempt and it failed immediately: the report *mentions* both systems
# while *declaring* one ("в системе счёта редактора" appears in the sentence that
# explains why grep was chosen), so a substring match declared two systems and
# the check refused to run. An HTML comment is invisible when rendered and
# unambiguous when parsed.
DECLARATIONS = {
    "<!-- line-numbering: grep -->": "grep",
    "<!-- line-numbering: editor -->": "editor",
}

REF = re.compile(r"`?([A-Za-z0-9_./\\-]+\.py):(\d+)`?")
# A reference may be a range (`:625-630`). The symbol window has to cover the
# whole cited range, otherwise a name sitting at the far end reads as absent --
# measured: `application_queue.py:625-630` was flagged although
# `save_vacancy_eligibility` is at 629, inside the range the report names.
REF_RANGE = re.compile(r"`?([A-Za-z0-9_./\\-]+\.py):(\d+)(?:-(\d+))?`?")
CODE = re.compile(r"`([^`]+)`")
FILETOK = re.compile(r"[A-Za-z0-9_./\\-]*\.(?:py|md|js|json|toml|sh|yaml|yml)")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
SYMBOL_WINDOW = 4

# Measured 2026-09-17 on docs/ble001_triage.md: 31 flagged, 26 real. These are
# the five kinds of false positive, kept here so the next sweep does not have to
# re-derive them. Do NOT "fix" them by widening the window -- widening is what
# hides real drift.
FALSE_POSITIVE_CLASSES = (
    "one address, several consumers named in the same sentence",
    ("the name is named precisely to say it is NOT there "
     "(hh_message_watcher.py:369 goes without questionnaire_data)"),
    ("the anchor is a comment that refers back to the finding itself "
     "(hh_application_runner.py:532)"),
    "the name is the caller, not the cited line (db_schema.py:119 + init_db())",
    "a prose name-pattern, not a symbol (discover_*, extract_*)",
    ("the name says what should appear there AFTER the fix "
     "(browser_executor.py:681 + form_detected)"),
)
SYMBOL_STOP = {
    "py", "md", "js", "json", "toml", "sh", "yaml", "yml",
    "True", "False", "None", "pass", "print", "docs", "ble001_triage",
}


def declared_system(text: str) -> tuple[str | None, list[str]]:
    """Return (system, description of what was found).

    The declaration must appear EXACTLY ONCE. Found the hard way: the first
    version of finding #45 quoted the marker verbatim while explaining it, so the
    document carried two, and both mutations of this checker changed meaning --
    deleting the first marker left the quoted one behind (M1 "survived"), and
    flipping the first left a contradiction (M3 stopped being a clean survivor).
    A declaration that can be quoted is not a declaration.
    """
    counts = {phrase: text.count(phrase) for phrase in DECLARATIONS}
    total = sum(counts.values())
    if total == 0:
        return None, []
    found = [f"{phrase!r} x{counts[phrase]}" for phrase in DECLARATIONS if counts[phrase]]
    if total == 1:
        (phrase,) = [p for p, c in counts.items() if c]
        return DECLARATIONS[phrase], found
    return None, found


def tracked_py() -> dict[str, list[str]]:
    out = subprocess.run(
        ["git", "ls-files", "--", "*.py"], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8", check=False,
    ).stdout.splitlines()
    by_base: dict[str, list[str]] = {}
    for p in out:
        by_base.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    return by_base


def line_counts(path: Path) -> tuple[int, int, int]:
    """(grep lines, editor lines, line-shifting bare CRs) for one file."""
    data = path.read_bytes()
    body = data.rstrip(b"\r\n \t")
    shifting = body.count(CR) - body.count(CRLF)
    crlf = data.count(CRLF)
    bare_lf = data.count(LF) - crlf
    grep_lines = crlf + bare_lf + 1
    editor_lines = len(data.decode("utf-8", "replace").splitlines())
    return grep_lines, editor_lines, shifting


def symbol_worklist(
    docs: list[str], texts: dict[str, str], by_base: dict[str, list[str]],
) -> tuple[int, list[tuple[str, int, str, int, list[str], str]]]:
    """Advisory: references whose named symbol is not anywhere near the line.

    Returns (references_that_named_a_symbol, flagged). Never raises and never
    affects the exit code: it is a worklist for a human, not a verdict.
    """
    checked = 0
    flagged: list[tuple[str, int, str, int, list[str], str]] = []
    for doc in docs:
        text = texts.get(doc)
        if text is None:
            continue
        for n, ln in enumerate(text.split("\n"), 1):
            refs = list(REF_RANGE.finditer(ln))
            if not refs:
                continue
            spans = [m.group(1) for m in CODE.finditer(ln)]
            spans = [FILETOK.sub(" ", s) for s in spans]
            names = {
                name
                for span in spans
                if not REF_RANGE.search(span)
                for name in IDENT.findall(span)
                if name not in SYMBOL_STOP
            }
            if not names:
                continue
            for m in refs:
                path, start = m.group(1), int(m.group(2))
                end = int(m.group(3)) if m.group(3) else start
                base = path.rsplit("/", 1)[-1]
                hits = by_base.get(base, [])
                if len(hits) != 1:
                    continue
                wanted = names - {Path(base).stem}
                if not wanted:
                    continue
                checked += 1
                src = (REPO / hits[0]).read_bytes().decode(
                    "utf-8", "replace",
                ).split("\n")
                lo = max(0, start - 1 - SYMBOL_WINDOW)
                hi = min(len(src), end + SYMBOL_WINDOW)
                window = "\n".join(src[lo:hi])
                if any(name in window for name in wanted):
                    continue
                actual = src[start - 1].strip() if start <= len(src) else "<past EOF>"
                flagged.append((doc, n, base, start, sorted(wanted)[:6], actual))
    return checked, flagged


def find_symbol_line(path: Path, symbol: str) -> int | None:
    """First grep line of `path` that mentions `symbol`, or None.

    Exists so the mutation tool can compute the anchor it perturbs instead of
    typing it: a hardcoded line number would rot into a false pass, which is the
    exact failure mode this whole module is about.
    """
    text = path.read_bytes().decode("utf-8", "replace")
    for n, ln in enumerate(text.split("\n"), 1):
        if symbol in ln:
            return n
    return None


def main(argv: list[str]) -> int:
    want_ambiguous = "--ambiguous" in argv
    want_symbols = "--symbols" in argv
    docs = [a for a in argv if not a.startswith("--")] or DEFAULT_DOCS
    by_base = tracked_py()
    problems, quoted, checked, declared = [], 0, 0, 0
    seen: set[tuple[str, int]] = set()
    ambiguous: list[tuple[str, str, str, str, str]] = []
    texts: dict[str, str] = {}

    for doc in docs:
        if not (REPO / doc).is_file():
            print(f"!! {doc} does not exist -- refusing to report a verdict about"
                  f" a document that was never read", file=sys.stderr)
            return 2
        text = (REPO / doc).read_text(encoding="utf-8")
        texts[doc] = text
        system, phrases = declared_system(text)
        if phrases and system is None:
            problems.append(
                f"{doc}  -> the counting-system declaration is not unique"
                f" ({', '.join(phrases)}); it must appear exactly once, and a"
                f" quotation of the marker in prose counts as a second declaration"
            )

        for n, ln in enumerate(text.split("\n"), 1):
            for m in REF.finditer(ln):
                path, num = m.group(1), int(m.group(2))
                if (path, num) in seen:
                    continue
                seen.add((path, num))
                checked += 1

                base = path.rsplit("/", 1)[-1]
                hits = by_base.get(base, [])
                if len(hits) != 1:
                    problems.append(f"{doc}:{n}  {path}:{num}  -> "
                                    f"{'no such file' if not hits else 'ambiguous'}")
                    continue

                grep_lines, editor_lines, shifting = line_counts(REPO / hits[0])

                if not shifting:
                    limit = grep_lines
                elif system is None:
                    problems.append(
                        f"{doc}:{n}  {path}:{num}  -> carries {shifting} line-shifting"
                        f" bare CR: numbers mean different places in grep and in an"
                        f" editor, and the document never declares which it uses"
                    )
                    continue
                else:
                    declared += 1
                    limit = grep_lines if system == "grep" else editor_lines
                    if want_ambiguous:
                        raw = (REPO / hits[0]).read_bytes().decode("utf-8", "replace")
                        by_grep = raw.replace("\r\n", "\n").split("\n")
                        by_editor = raw.splitlines()
                        ambiguous.append((
                            f"{base}:{num}",
                            system,
                            by_grep[num - 1].strip() if num <= len(by_grep) else "<past EOF>",
                            by_editor[num - 1].strip() if num <= len(by_editor) else "<past EOF>",
                        ))

                if num > limit:
                    if (path, num) in QUOTED:
                        quoted += 1
                    else:
                        problems.append(
                            f"{doc}:{n}  {path}:{num}  -> file has {limit} lines"
                        )

    # An exemption that no longer matches anything in the document is a hole
    # nobody will notice: it silently excuses a future reference at that exact
    # line. Found by doing it -- cli.py:88888 was added while mutating M2 and
    # stayed behind after the report stopped quoting it.
    #
    # This check only makes sense for the full document set the exemptions were
    # written for. Checking one ad-hoc document would report every exemption as
    # stale, which is noise: measured by running the checker on a single-line
    # temp document and getting three "stale exemption" problems instead of the
    # one answer that was asked for.
    if docs == DEFAULT_DOCS:
        for path, num in sorted(QUOTED):
            if (path, num) not in seen:
                problems.append(
                    f"QUOTED  {path}:{num}  -> stale exemption ({QUOTED[(path, num)]}):"
                    f" no document references it any more; delete the entry"
                )

    if want_ambiguous:
        print("=== references into bare-CR files, both readings ===")
        print(f"    {'ref':34s} {'declared':9s} verdict")
        for ref, system, g, e in ambiguous:
            mark = "same in both" if g == e else f"DIFFERS (checked under {system})"
            print(f"    {ref:34s} {system:9s} {mark}")
            print(f"        grep   : {g[:96]}")
            if g != e:
                print(f"        editor : {e[:96]}")
        print()

    print(f"documents: {', '.join(docs)}")
    print(f"distinct file:line references: {checked}")
    print(f"  into a bare-CR file, checked under a declared system: {declared}")
    print(f"  quoted as deliberate examples: {quoted}")

    if want_symbols:
        sym_checked, flagged = symbol_worklist(docs, texts, by_base)
        print()
        print("=== advisory: does the named symbol sit at the cited line? ===")
        print(f"    references that name a symbol: {sym_checked}")
        print(f"    flagged for reading: {len(flagged)}"
              f"  (measured precision ~68%: 21 real of 31 flagged on 2026-09-17,")
        print("     and 21 more real anchors were found only by reading --"
              " their doc lines name no symbol at all)")
        if flagged:
            print(f"    {'doc:line':16s} {'ref':28s} names / actual line")
            for doc, n, base, start, names, actual in flagged:
                print(f"    {doc}:{n:<8d} {base}:{start:<10d} "
                      f"{','.join(names)} | {actual[:56]!r}")
            print("    known false-positive classes (do not re-derive):")
            for cls in FALSE_POSITIVE_CLASSES:
                print(f"      - {cls}")
        print("    this section is advisory and does not change the exit code")

    if problems:
        print(f"  PROBLEMS: {len(problems)}")
        for p in problems:
            print(f"    {p}")
        return 1
    print("  out of range: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
