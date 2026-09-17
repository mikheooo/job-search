"""Mutation check for tools/report_line_refs.py.

A checker that cannot fail is decoration. Three mutations:

  M1  delete the `<!-- line-numbering: grep -->` marker from the document
      -> the numbering is ambiguous again and undeclared. Expect the
      "never declares which it uses" refusal.

  M2  append a reference past the declared limit (cli.py:<limit+1000>, chosen so
      it cannot collide with a QUOTED entry)
      -> expect "file has N lines". The number is computed, not hardcoded: the
      first two versions used cli.py:99999 and then cli.py:88888, and finding #45
      ended up quoting both as its own mutation examples. A quoted number lands in
      QUOTED, the checker excuses it, and the mutation "survives" for a reason that
      has nothing to do with the checker. A mutation whose anchor drifts into the
      exemption list is not measuring the guard.

  M3  flip the marker to `editor` while the numbers stay in grep coordinates
      -> EXPECTED SURVIVOR. The checker would verify every reference against
      12047 lines, all of them land in range, and the report would be silently
      wrong. Declaring the system makes the numbering well-defined; it does not
      make the declaration true. That gap is not closable by range checking, and
      the tool says so: `--ambiguous` prints both readings for a human to read
      once. Listed here so "3 mutations, 3 caught" is not reported when the
      honest number is "2 caught, 1 expected survivor".

  M4  quote the marker verbatim in prose, leaving the real one in place
      -> two declarations. Expect the "not unique" refusal. This is not a
      hypothetical: the first draft of finding #45 did exactly that while
      explaining the marker, and it silently inverted M1 and M3.

  M5  inject a reference that is IN RANGE but points at the wrong line, with the
      symbol named in the same sentence (db.py:2500 + count_submitted_transitions_since,
      which lives at 2584)
      -> the range check passes it -- that is the whole point, and the reason 42
      such anchors sat in the report unnoticed. `--symbols` must flag it, and the
      exit code must stay 0: advisory means a worklist, not a failure.

  M6  counter-check for M5: the SAME sentence with the CORRECT number must NOT be
      flagged. Without this, a mode that flagged everything would satisfy M5.

M5 and M6 are a pair on purpose. M1-M4 ask "can the checker fail?"; M5/M6 ask
"can the advisory half tell right from wrong, in both directions?".

Mutations run against a copy of the document in _tmp_refs_mutation/, never the
real one, so there is nothing to restore. The copy is removed at the end.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"
CHECKER = "tools/report_line_refs.py"
SOURCE_DOC = REPO / "docs" / "ble001_triage.md"
SCRATCH = REPO / "_tmp_refs_mutation"
DOC = SCRATCH / "doc.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_line_refs as rlr

MARKER = "<!-- line-numbering: grep -->"

EXPECTED_SURVIVORS = {
    "M3": "declaration flipped to `editor`: in range everywhere, wrong everywhere",
}


def overflow_reference() -> tuple[str, int]:
    """A cli.py reference past its grep length that no QUOTED entry excuses."""
    grep_lines, _, _ = rlr.line_counts(REPO / "ai_assistant" / "cli.py")
    candidate = grep_lines + 1000
    while ("cli.py", candidate) in rlr.QUOTED:
        candidate += 1000
    return f"cli.py:{candidate}", candidate


def write(path: Path, text: str) -> None:
    """Write with retries and verify -- see the finding-#44 mutator for why."""
    data = text.encode("utf-8")
    last: Exception | None = None
    for attempt in range(6):
        try:
            path.write_bytes(data)
            if path.read_bytes() == data:
                return
            last = OSError("write reported success but the bytes differ")
        except OSError as exc:
            last = exc
        time.sleep(0.4 * (attempt + 1))
    raise SystemExit(f"!! could not write {path} after 6 attempts: {last}")


def run_checker(*extra: str) -> tuple[int, str]:
    proc = subprocess.run(
        [str(PY), CHECKER, *extra, str(DOC.relative_to(REPO)).replace("\\", "/")],
        cwd=REPO, capture_output=True, check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")


def main() -> int:
    if not SOURCE_DOC.is_file():
        print(f"!! {SOURCE_DOC} does not exist", file=sys.stderr)
        return 2
    original = SOURCE_DOC.read_text(encoding="utf-8")
    if MARKER not in original:
        print(f"!! the document no longer carries {MARKER!r} -- nothing to mutate",
              file=sys.stderr)
        return 2

    SCRATCH.mkdir(parents=True, exist_ok=True)
    caught: list[bool] = []

    try:
        # --- M1: the declaration disappears ---
        write(DOC, original.replace(MARKER, "", 1))
        rc, out = run_checker()
        ok = rc != 0 and "never declares which it uses" in out
        caught.append(ok)
        print("\n--- M1  declaration marker removed ---")
        print(f"    exit {rc}, refusal present -> {'never declares which it uses' in out}")
        print(f"    VERDICT : {'CAUGHT' if ok else 'SURVIVED'}")

        # --- M2: a reference past the declared limit ---
        ref, _num = overflow_reference()
        write(DOC, original + f"\n\nСсылка за конец: `{ref}`.\n")
        rc, out = run_checker()
        expected_msg = f"file has {rlr.line_counts(REPO / 'ai_assistant' / 'cli.py')[0]} lines"
        ok = rc != 0 and expected_msg in out
        caught.append(ok)
        print("\n--- M2  reference past the declared limit ---")
        print(f"    reference {ref} (not in QUOTED)")
        print(f"    exit {rc}, out-of-range reported -> {expected_msg in out}")
        print(f"    VERDICT : {'CAUGHT' if ok else 'SURVIVED'}")

        # --- M3: the declaration lies (expected survivor) ---
        write(DOC, original.replace(MARKER, "<!-- line-numbering: editor -->", 1))
        rc, out = run_checker()
        survived = rc == 0
        print("\n--- M3  declaration flipped to editor (EXPECTED SURVIVOR) ---")
        print(f"    exit {rc} -- checker {'passes' if survived else 'fails'}"
              f" while the numbers are in the wrong system")
        print(f"    reason  : {EXPECTED_SURVIVORS['M3']}")

        # --- M4: the marker quoted in prose, so there are two ---
        write(DOC, original + f"\n\nВ тексте цитируется маркер: `{MARKER}`.\n")
        rc, out = run_checker()
        ok = rc != 0 and "not unique" in out
        caught.append(ok)
        print("\n--- M4  marker quoted verbatim in prose (two declarations) ---")
        print(f"    exit {rc}, refusal present -> {'not unique' in out}")
        print(f"    VERDICT : {'CAUGHT' if ok else 'SURVIVED'}")

        # --- M5/M6: the advisory half, in both directions ---
        #
        # The baseline is measured, not assumed: the mutation runs against a copy
        # of the whole report, so it already carries that report's own advisory
        # flags. The first version of M6 asserted "flagged for reading: 0" and
        # "survived" for that reason alone -- a mutation comparing against a
        # hardcoded zero measures the document, not the guard.
        def advisory_flags(text: str) -> tuple[int, int, int, str]:
            write(DOC, text)
            code, out = run_checker("--symbols")
            match = re.search(r"flagged for reading: (\d+)", out)
            named = re.search(r"references that name a symbol: (\d+)", out)
            return (code,
                    int(match.group(1)) if match else -1,
                    int(named.group(1)) if named else -1,
                    out)

        symbol = "count_submitted_transitions_since"
        db_path = REPO / "ai_assistant" / "db.py"
        db_lines = rlr.line_counts(db_path)[0]
        right = rlr.find_symbol_line(db_path, symbol)
        if right is None:
            print(f"\n!! {symbol} is not in db.py any more -- M5/M6 cannot run",
                  file=sys.stderr)
            return 2
        wrong = right - 84 if right - 84 >= 1 else right + 84
        assert wrong <= db_lines, (wrong, db_lines)

        _rc, base_flags, base_named, _ = advisory_flags(original)
        if base_flags < 0:
            print("\n!! the advisory summary line is gone -- M5/M6 cannot run",
                  file=sys.stderr)
            return 2

        rc, flags, named, out = advisory_flags(
            original + f"\n\nСчётчик `{symbol}` (`db.py:{wrong}`).\n"
        )
        flagged = flags == base_flags + 1 and named == base_named + 1
        ok = rc == 0 and flagged
        caught.append(ok)
        print("\n--- M5  in-range reference to the wrong line (advisory must flag) ---")
        print(f"    {symbol} really lives at db.py:{right}; the document says {wrong}")
        print(f"    baseline {base_flags} flags / {base_named} named"
              f"  ->  {flags} flags / {named} named")
        print(f"    exit {rc} (advisory must not fail the run), flagged -> {flagged}")
        print(f"    VERDICT : {'CAUGHT' if ok else 'SURVIVED'}")

        rc, flags, named, out = advisory_flags(
            original + f"\n\nСчётчик `{symbol}` (`db.py:{right}`).\n"
        )
        quiet = flags == base_flags and named == base_named + 1
        ok = rc == 0 and quiet
        caught.append(ok)
        print("\n--- M6  the same sentence with the correct number (must stay quiet) ---")
        print(f"    baseline {base_flags} flags / {base_named} named"
              f"  ->  {flags} flags / {named} named  (named must rise, flags must not)")
        print(f"    exit {rc}, no new flag -> {quiet}")
        print(f"    VERDICT : {'CAUGHT' if ok else 'SURVIVED'}")
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)

    print("\n=== summary ===")
    print(f"    mutations caught        : {sum(caught)} of {len(caught)}"
          f" (plus {len(EXPECTED_SURVIVORS)} expected survivor)")
    print(f"    scratch removed         : {'yes' if not SCRATCH.exists() else 'NO'}")
    return 0 if all(caught) else 1


if __name__ == "__main__":
    sys.exit(main())
