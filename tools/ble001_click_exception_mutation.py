#!/usr/bin/env python
"""Mutation check for finding #61: do the pins see the guard and the record?

The fix is two changes at one place, ``hh_controlled_submit.controlled_real_submit()``:

  * the one-shot invariant (``_submitted_reviews.add(review_id)``) used to be armed
    AFTER the click; it is now armed BEFORE it, the way
    ``hh_submission.submit_application()`` arms it at :418. ``_SUBMIT_CLICK_JS``
    runs ``el.click()`` and only then returns its JSON, so a lost CDP response
    arrives as an exception after the button was pressed - and the path that
    armed the invariant afterwards left the review unspent.
  * that exception path used to record ``click_count = 0``, ``submit_count = 0``
    and ``verdict = FAIL_CLOSED``. It now counts the dispatched click and reports
    ``SUBMISSION_UNKNOWN``.

Mutations, one mistake each:

  * **M0** drops the early arming, so the guard never fires in this function.
    This is the defect, not the pre-fix shape - the pre-fix shape also armed it
    after a successful click, and M0 does not need that: three pins must see it.
  * **M1** restores the pre-fix record exactly (0/0 + FAIL_CLOSED). The pins are
    then read against the code rather than against my description of it.
  * **M2** keeps the honest verdict but counts nothing - the subtler version of
    the same lie, and the one a verdict-only pin would miss.
  * **M3** keeps the counts but calls it FAIL_CLOSED - the other half.
  * **M4** arms the invariant BEFORE the gates instead of at the click boundary.
    A refused run then burns the review; the gate control pin holds that it must
    not. This is the control that proves the pins distinguish "armed at the click
    boundary" from "armed earlier", rather than merely "armed somewhere".
  * **M5** makes the healthy path stop counting its click. The healthy control
    pin holds that a confirmed click is still 1/1.

Everything is restored byte-for-byte and the hashes are printed; the tool refuses
to report a verdict it could not restore. Verdicts come from --junitxml, not from
the console: the bulk-delete guard fires during pytest's own cleanup, after the
last test has run and before the summary prints. A pin missing from the junit is
UNKNOWN, never PASS.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_click_exception_mutation.py
    ./.venv/Scripts/python.exe tools/ble001_click_exception_mutation.py M0 M1 M2
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

TARGET = REPO / "ai_assistant" / "hh_controlled_submit.py"

PIN_RECORD = "test_a_lost_click_response_is_not_recorded_as_a_click_that_never_happened"
PIN_INVARIANT = "test_a_lost_click_response_arms_the_one_shot_invariant"
PIN_SECOND = "test_a_lost_click_response_does_not_allow_a_second_click_on_the_same_review"
PIN_HEALTHY = "test_a_confirmed_click_still_reports_submitted_and_still_blocks_a_second_attempt"
PIN_GATE = "test_a_gate_refusal_still_records_zero_clicks"

SHORT = {
    PIN_RECORD: "record",
    PIN_INVARIANT: "invariant",
    PIN_SECOND: "no_2nd_click",
    PIN_HEALTHY: "ctrl_healthy",
    PIN_GATE: "ctrl_gate_zero",
}

LF = chr(10)
CRLF = chr(13) + chr(10)


def L(*lines: str, nl: str = LF) -> str:
    """Join lines with the file's own newline and terminate the last one."""
    return nl.join(lines) + nl


# --- anchors, verbatim. A mismatch means the fix moved, and the tool says so
# rather than reporting a clean run. ------------------------------------------

ARM_EARLY = L(
    "    _submitted_reviews.add(review_id)",
    "    try:",
)
ARM_GONE = L(
    "    try:",
)

RECORD_FIXED = L(
    '        report.click_count = 1',
    '        report.submit_count = 1',
    '        report.verdict = "SUBMISSION_UNKNOWN"',
)
RECORD_PREFIX = L(
    '        report.click_count = 0',
    '        report.submit_count = 0',
    '        report.verdict = "FAIL_CLOSED"',
)
RECORD_ZERO_COUNTS = L(
    '        report.click_count = 0',
    '        report.submit_count = 0',
    '        report.verdict = "SUBMISSION_UNKNOWN"',
)
RECORD_FAIL_CLOSED = L(
    '        report.click_count = 1',
    '        report.submit_count = 1',
    '        report.verdict = "FAIL_CLOSED"',
)

STEP2 = L(
    "    # --- Step 2: Full read-only preflight (all 11 gates + button) ---",
    "    from .hh_submission import preflight_submission",
)
STEP2_ARMED_EARLY = L(
    "    # --- Step 2: Full read-only preflight (all 11 gates + button) ---",
    "    from .hh_submission import _submitted_reviews, preflight_submission",
    "    _submitted_reviews.add(review_id)",
)

HEALTHY_COUNTS = L(
    '    is_pre_click = bool(res.get("refused"))',
    "    report.click_count = 0 if is_pre_click else 1",
    "    report.submit_count = 0 if is_pre_click else 1",
    "",
    '    if not res.get("ok"):',
)
HEALTHY_ZERO = L(
    '    is_pre_click = bool(res.get("refused"))',
    "    report.click_count = 0 if is_pre_click else 0",
    "    report.submit_count = 0 if is_pre_click else 1",
    "",
    '    if not res.get("ok"):',
)

# (name, description, old, new, pins)
MUTATIONS = [
    (
        "M0",
        "the guard never arms in this function",
        ARM_EARLY,
        ARM_GONE,
        [PIN_INVARIANT, PIN_SECOND, PIN_HEALTHY],
    ),
    (
        "M1",
        "restore the pre-fix record: 0/0 + FAIL_CLOSED",
        RECORD_FIXED,
        RECORD_PREFIX,
        [PIN_RECORD],
    ),
    (
        "M2",
        "honest verdict, but count no click",
        RECORD_FIXED,
        RECORD_ZERO_COUNTS,
        [PIN_RECORD],
    ),
    (
        "M3",
        "count the click, but call it FAIL_CLOSED",
        RECORD_FIXED,
        RECORD_FAIL_CLOSED,
        [PIN_RECORD],
    ),
    (
        "M4",
        "arm the invariant before the gates instead of at the click",
        STEP2,
        STEP2_ARMED_EARLY,
        [PIN_GATE],
    ),
    (
        "M5",
        "the healthy path stops counting its click",
        HEALTHY_COUNTS,
        HEALTHY_ZERO,
        [PIN_HEALTHY],
    ),
]

FILES = [TARGET]


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
    xml = REPO / f".pytest_q61_{pin_tag(node_ids)}.xml"
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
        for name, description, old, new, pins in mutations:
            source = originals[TARGET]
            count = source.count(old.encode())
            if count != 1:
                print(
                    f"{name:4s} !! anchor appears {count} times in {TARGET.name}, "
                    f"expected 1: {old.splitlines()[0]!r}"
                )
                failures.append(name)
                continue
            mutated = source.replace(old.encode(), new.encode(), 1)
            if mutated == source:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            TARGET.write_bytes(mutated)
            try:
                verdicts, counts, reported = run_pins(pins)
            finally:
                TARGET.write_bytes(originals[TARGET])

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
