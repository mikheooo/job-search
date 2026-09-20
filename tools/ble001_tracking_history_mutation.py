#!/usr/bin/env python
"""Mutation check for finding #59: is an unrecorded status change still invisible?

The fix is one new check in ``application_integrity``:

  * ``_check_unrecorded_status_changes()`` compares the status a row is IN against
    the status its recorded history ENDS AT. ``set_application_status()`` writes
    ``status`` and ``last_status_change_at`` and nothing else, while
    ``application_status_history`` is written only by ``transition_application()``
    - so a write that skips the state machine leaves the transition-legality guard
    with nothing to read.

Mutations, one mistake each:

  * **M0** drops the call from ``run_audit`` - the pre-fix shape, so the pins are
    read against the code rather than against my description of it.
  * **M1** makes the comparison never true ("the check is decoration").
  * **M2** compares TIMESTAMPS instead of the status. This is the mistake the
    score-only control exists for: ``set_application_status`` bumps
    ``last_status_change_at`` even when the status does not move, so a timestamp
    test reports every score refresh as an unrecorded transition.
  * **M3** (CONTROL) flags every row, legal transitions included: the finding must
    not spread over the whole park, or it gets switched off.
  * **M4** (CONTROL) reports the finding under the canonical id instead of the
    vacancy id. A finding that names the wrong row is not actionable, and the pins
    filter by vacancy id on purpose.

Everything is restored byte-for-byte and the hashes are printed; the tool refuses
to report a verdict it could not restore. Verdicts come from --junitxml, not from
the console: the bulk-delete guard fires during pytest's own cleanup, after the
last test has run and before the summary prints. A parametrized pin is aggregated
over ALL its cases (FAIL if any case failed), and a pin missing from the junit is
UNKNOWN, never PASS.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_tracking_history_mutation.py
    ./.venv/Scripts/python.exe tools/ble001_tracking_history_mutation.py M0 M1 M2
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

INTEGRITY = REPO / "ai_assistant" / "application_integrity.py"

PIN_NO_RECORD = "test_a_status_change_that_leaves_no_record_is_reported_by_the_audit"
PIN_LEGAL = "test_a_legal_transition_keeps_the_audit_quiet"
PIN_SCORE_ONLY = "test_a_score_only_refresh_is_not_an_unrecorded_status_change"
PIN_CREATED = "test_a_row_created_directly_in_a_status_is_not_flagged"
PIN_SUBMISSION = "test_the_submission_sync_write_is_reported_too"
PIN_SYNC_FALLBACK = "test_the_sync_fallback_that_writes_the_refused_status_is_reported"

SHORT = {
    PIN_NO_RECORD: "unrecorded_reported",
    PIN_LEGAL: "ctrl_legal_quiet",
    PIN_SCORE_ONLY: "ctrl_score_only",
    PIN_CREATED: "ctrl_created_direct",
    PIN_SUBMISSION: "submission_reported",
    PIN_SYNC_FALLBACK: "sync_fallback_reported",
}

DAMAGED_PINS = [PIN_NO_RECORD, PIN_SUBMISSION, PIN_SYNC_FALLBACK]
HEALTHY_PINS = [PIN_LEGAL, PIN_SCORE_ONLY, PIN_CREATED]

# --- anchors, verbatim. A mismatch means the fix moved, and the tool says so
# rather than reporting a clean run. ------------------------------------------

WIRED = "            self._check_unrecorded_status_changes(cid, als)\n"
WIRED_OUT = ""

COMPARE_FIXED = (
    "            recorded = hist[-1].new_status\n"
    "            current = tr.status.value if hasattr(tr.status, \"value\") else str(tr.status)\n"
    "            if recorded != current:\n"
)
COMPARE_NEVER = (
    "            recorded = hist[-1].new_status\n"
    "            current = tr.status.value if hasattr(tr.status, \"value\") else str(tr.status)\n"
    "            if False:\n"
)
COMPARE_TIMESTAMPS = (
    "            recorded = hist[-1].new_status\n"
    "            current = tr.status.value if hasattr(tr.status, \"value\") else str(tr.status)\n"
    "            if tr.last_status_change_at != hist[-1].changed_at:\n"
)
COMPARE_ALWAYS = (
    "            recorded = hist[-1].new_status\n"
    "            current = tr.status.value if hasattr(tr.status, \"value\") else str(tr.status)\n"
    "            if True:\n"
)

REPORT_ROW_FIXED = '                    "UNRECORDED_STATUS_CHANGE",\n                    sid,\n                    cid,\n'
REPORT_ROW_SWAPPED = '                    "UNRECORDED_STATUS_CHANGE",\n                    cid,\n                    sid,\n'

# (name, description, old, new, pins)
MUTATIONS = [
    (
        "M0",
        "drop the call from run_audit - the pre-fix shape",
        WIRED,
        WIRED_OUT,
        DAMAGED_PINS,
    ),
    (
        "M1",
        "the comparison is never true",
        COMPARE_FIXED,
        COMPARE_NEVER,
        DAMAGED_PINS,
    ),
    (
        "M2",
        "compare timestamps instead of the status",
        COMPARE_FIXED,
        COMPARE_TIMESTAMPS,
        [PIN_SCORE_ONLY],
    ),
    (
        "M3",
        "CONTROL: flag every row, legal transitions included",
        COMPARE_FIXED,
        COMPARE_ALWAYS,
        HEALTHY_PINS,
    ),
    (
        "M4",
        "CONTROL: report the finding under the canonical id, not the vacancy id",
        REPORT_ROW_FIXED,
        REPORT_ROW_SWAPPED,
        DAMAGED_PINS,
    ),
]

FILES = [INTEGRITY]


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
    xml = REPO / f".pytest_q59_{pin_tag(node_ids)}.xml"
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
            source = originals[INTEGRITY]
            count = source.count(old.encode())
            if count != 1:
                print(
                    f"{name:4s} !! anchor appears {count} times in {INTEGRITY.name}, "
                    f"expected 1: {old.splitlines()[0]!r}"
                )
                failures.append(name)
                continue
            mutated = source.replace(old.encode(), new.encode(), 1)
            if mutated == source:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            INTEGRITY.write_bytes(mutated)
            try:
                verdicts, counts, reported = run_pins(pins)
            finally:
                INTEGRITY.write_bytes(originals[INTEGRITY])

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
