#!/usr/bin/env python
"""Mutation check for finding #58: is a review still built out of an unreadable package?

The fix is one guard in ``application_review._get_required_data`` plus one loud
failure where the read used to be silent:

  * ``package_record_is_unreadable(vacancy_stable_id)`` refuses to build a review
    at all when the package row exists but cannot be read - the review used to be
    written with an empty cover letter and ``form_fingerprint == fp({})``, a value
    that every later comparison against it passes.
  * the parse inside the same function raises instead of degrading to ``{}``. It
    is unreachable for every shape measured in #58 (the guard runs the same
    ``json.loads`` on the same bytes); it exists for the race where the row
    changes between the two reads.

Mutations, one mistake each:

  * **M0** drops the guard - the pre-fix shape, so the pins are read against the
    code rather than against my description of it.
  * **M1** makes ``package_record_is_unreadable`` never find anything, which is
    the "the helper is decoration" mistake (the same one #57 checked).
  * **M2** puts the silent ``{}`` back where the loud refusal now is.
  * **M3** (CONTROL) makes the guard refuse everything, healthy packages
    included: the refusal must not spread.
  * **M4** (CONTROL) raises the same refusal as a different exception type. A
    refusal a caller cannot catch as ValueError is a crash, and the pins must
    not accept "any exception" as "refused".

Everything is restored byte-for-byte and the hashes are printed; the tool
refuses to report a verdict it could not restore. Verdicts come from --junitxml,
not from the console: the bulk-delete guard fires during pytest's own cleanup,
after the last test has run and before the summary prints. A parametrized pin is
aggregated over ALL its cases (FAIL if any case failed), and a pin missing from
the junit is UNKNOWN, never PASS.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_review_fingerprint_mutation.py
    ./.venv/Scripts/python.exe tools/ble001_review_fingerprint_mutation.py M0 M1 M2
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_ble001_fail_closed.py"

REVIEW = REPO / "ai_assistant" / "application_review.py"
PREP = REPO / "ai_assistant" / "application_prep.py"

PIN_REFUSE = "test_creating_a_review_against_an_unreadable_package_is_refused"
PIN_NOT_WRITTEN = "test_the_refused_review_is_not_written_at_all"
PIN_NO_EMPTY_FP = "test_the_fingerprint_of_an_empty_package_is_never_recorded"
PIN_READABLE_CTRL = "test_a_readable_package_still_creates_a_review_with_the_real_fingerprint"
PIN_LETTERLESS = "test_a_letterless_package_hashes_to_the_empty_fingerprint_legitimately"
PIN_SECOND_LINE = "test_a_package_that_degrades_between_the_two_reads_is_refused_loudly"
PIN_FIELDS = "test_a_readable_package_still_fills_the_review_from_the_package"
PIN_ABSENT = "test_an_absent_package_row_still_says_not_found"
PIN_IDEMPOTENT = "test_an_existing_review_is_still_returned_idempotently"

SHORT = {
    PIN_REFUSE: "create_refused",
    PIN_NOT_WRITTEN: "nothing_written",
    PIN_NO_EMPTY_FP: "no_empty_fp",
    PIN_READABLE_CTRL: "ctrl_readable_ok",
    PIN_LETTERLESS: "ctrl_letterless",
    PIN_SECOND_LINE: "second_line_loud",
    PIN_FIELDS: "ctrl_fields_filled",
    PIN_ABSENT: "absent_still_not_found",
    PIN_IDEMPOTENT: "idempotent_kept",
}

DAMAGED_PINS = [PIN_REFUSE, PIN_NOT_WRITTEN, PIN_NO_EMPTY_FP]
HEALTHY_PINS = [
    PIN_READABLE_CTRL, PIN_LETTERLESS, PIN_FIELDS, PIN_ABSENT, PIN_IDEMPOTENT,
]

# --- anchors, verbatim. A mismatch means the fix moved, and the tool says so
# rather than reporting a clean run. ------------------------------------------

GUARD_FIXED = (
    "    if package_record_is_unreadable(vacancy_stable_id):\n"
    "        raise ValueError(\n"
    '            f"Application package for {vacancy_stable_id} exists but cannot be read"\n'
    '            " - refusing to build a review out of an unreadable package"\n'
    "        )\n"
)
GUARD_REMOVED = ""
GUARD_ALWAYS_REFUSES = (
    "    if True:\n"
    "        raise ValueError(\n"
    '            "Application package cannot be read"\n'
    "        )\n"
)
GUARD_WRONG_TYPE = (
    "    if package_record_is_unreadable(vacancy_stable_id):\n"
    "        raise RuntimeError(\n"
    '            f"Application package for {vacancy_stable_id} exists but cannot be read"\n'
    '            " - refusing to build a review out of an unreadable package"\n'
    "        )\n"
)

LOUD_FIXED = (
    "        except Exception as exc:\n"
    "            # Unreachable for the shapes measured in #58 (the guard runs the\n"
    "            # same json.loads on the same bytes); kept loud for the race where\n"
    "            # the row changes between the two reads.\n"
    "            raise ValueError(\n"
    '                f"Application package for {vacancy_stable_id} could not be parsed"\n'
    '                f" while building the review ({exc.__class__.__name__})"\n'
    "            ) from exc\n"
)
LOUD_DEFUSED = "        except Exception:\n            pkg_json = {}\n"

HELPER_BODY_FIXED = (
    "    raw = row[2]\n"
    "    if not raw:\n"
    "        return True\n"
    "    try:\n"
    "        payload = json.loads(raw)\n"
    "    except (ValueError, TypeError):\n"
    "        return True\n"
    "    if not isinstance(payload, dict):\n"
    "        return True\n"
    "    return not payload\n"
)
HELPER_BODY_NEVER_FINDS = "    return False\n"

# (name, description, target file, old, new, pins)
MUTATIONS = [
    (
        "M0",
        "drop the guard - the pre-fix shape",
        REVIEW,
        GUARD_FIXED,
        GUARD_REMOVED,
        DAMAGED_PINS,
    ),
    (
        "M1",
        "CONTROL: the helper never finds anything unreadable",
        PREP,
        HELPER_BODY_FIXED,
        HELPER_BODY_NEVER_FINDS,
        DAMAGED_PINS,
    ),
    (
        "M2",
        "put the silent {} back where the loud refusal is",
        REVIEW,
        LOUD_FIXED,
        LOUD_DEFUSED,
        [PIN_SECOND_LINE],
    ),
    (
        "M3",
        "CONTROL: refuse every review, healthy packages included",
        REVIEW,
        GUARD_FIXED,
        GUARD_ALWAYS_REFUSES,
        HEALTHY_PINS,
    ),
    (
        "M4",
        "CONTROL: refuse with the wrong exception type",
        REVIEW,
        GUARD_FIXED,
        GUARD_WRONG_TYPE,
        DAMAGED_PINS,
    ),
]

FILES = [REVIEW, PREP]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin_tag(node_ids: list[str]) -> str:
    """A stable, filesystem-safe tag for a pin set - one junit file per set.

    Deliberately no unlink(): every deletion counts against the repo's
    bulk-delete budget, and that budget is what killed a mutation run in the
    middle of M1 on 2026-09-20. Overwriting a junit is free; deleting one is not.
    """
    return hashlib.sha1("|".join(sorted(node_ids)).encode()).hexdigest()[:8]


def run_pins(node_ids: list[str]) -> tuple[dict[str, str], dict[str, tuple[int, int]], bool]:
    """Return ({pin: PASS/FAIL/UNKNOWN}, {pin: (failed_cases, total)}, measured)."""
    expr = " or ".join(node_ids)
    xml = REPO / f".pytest_q58_{pin_tag(node_ids)}.xml"
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, "--no-header", "-rf", f"--junitxml={xml}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    verdicts = dict.fromkeys(node_ids, "UNKNOWN")
    counts: dict[str, tuple[int, int]] = {p: (0, 0) for p in node_ids}
    if xml.exists():
        root = ElementTree.parse(xml).getroot()
        tally = {p: [0, 0] for p in node_ids}  # [failed, total]
        for case in root.iter("testcase"):
            name = case.get("name", "")
            broken = any(child.tag in ("failure", "error") for child in case)
            for pin in node_ids:
                if name == pin or name.startswith(f"{pin}["):
                    tally[pin][1] += 1
                    if broken:
                        tally[pin][0] += 1
        for pin in node_ids:
            failed, total = tally[pin]
            counts[pin] = (failed, total)
            if failed:
                verdicts[pin] = "FAIL"
            elif total:
                verdicts[pin] = "PASS"
    else:
        out = proc.stdout + proc.stderr
        print(f"     !! no junit written (returncode {proc.returncode}); "
              f"output tail: {(out[-300:] or '<empty>')!r}")
    return verdicts, counts, all(v != "UNKNOWN" for v in verdicts.values())


def main() -> int:
    # Accept mutation names as arguments: `... M0 M1` runs just those. Several
    # pytest runs inside one shell invocation exhaust the bulk-delete budget, so
    # running them a few at a time is a supported way to get an honest verdict.
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    mutations = [m for m in MUTATIONS if not only or m[0] in only]

    originals = {p: p.read_bytes() for p in FILES}
    before = {p.name: sha(p) for p in FILES}

    for name, digest in before.items():
        print(f"{name} sha256 before: {digest}")
    print(f"{len(mutations)} of {len(MUTATIONS)} mutations"
          f"{' (' + ', '.join(only) + ')' if only else ''}\n")
    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    failures = []
    try:
        for name, description, target, old, new, pins in mutations:
            source = originals[target]
            count = source.count(old.encode())
            if count != 1:
                print(
                    f"{name:4s} !! anchor appears {count} times in {target.name}, "
                    f"expected 1: {old.splitlines()[0]!r}"
                )
                failures.append(name)
                continue
            mutated = source.replace(old.encode(), new.encode(), 1)
            if mutated == source:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            target.write_bytes(mutated)
            try:
                verdicts, counts, reported = run_pins(pins)
            finally:
                target.write_bytes(originals[target])

            if not reported:
                print(f"{name:4s} !! NO VERDICT - mutation left unresolved, not counted")
                failures.append(name)
                continue
            # Caught means SOME listed pin failed, not all of them: the question
            # is whether the pin set is blind to this mistake, and one failing
            # pin answers it.
            caught = any(verdicts[p] == "FAIL" for p in pins)
            status = "caught" if caught else "SURVIVED"
            print(f"{name:4s} {status:9s} {description}")
            evidence = []
            for p in pins:
                failed, total = counts[p]
                suffix = f"({failed}/{total})" if total > 1 else ""
                evidence.append(f"{SHORT[p]}={verdicts[p]}{suffix}")
            print(f"     evidence: {', '.join(evidence)}")
            if not caught:
                failures.append(name)
    finally:
        for path, blob in originals.items():
            path.write_bytes(blob)

    after = {p.name: sha(p) for p in FILES}
    print()
    for name, digest in after.items():
        print(f"{name} sha256 after : {digest}")
    restored = after == before
    print(f"restored byte-for-byte: {restored}")
    if not restored:
        return 2

    print()
    if failures:
        print(f"RESULT: {len(failures)} mutation(s) survived: {', '.join(failures)}")
        return 1
    print(f"RESULT: all {len(mutations)} mutations caught, tree restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
