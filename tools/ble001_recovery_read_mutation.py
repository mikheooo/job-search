#!/usr/bin/env python
"""Mutation check for finding #55: does the recovery report still lie?

The fix in ``submission_recovery.inspect_submission_state`` stops calling an
unreadable verification record missing. The shape it replaces is a single
``if not verification:`` that could not tell two different worlds apart:

    ver = get_verification(...)          # None for "no row" AND for "cannot read"
    ...
    if not verification:
        return (NEEDS_VERIFICATION,
                "Submission exists but no verification performed",
                "Run verification to check submission result on the website")

The fix adds ``_verification_record_is_unreadable()``, which reads the column by
SQL the way ``application_integrity._check_verification_json_records`` already
does, and threads the answer through as ``verification_unreadable``.

Two mutations carry the finding:

  * **M1** computes the flag and then forgets to pass it. The warning is still
    right (it reads the flag directly) while the reason and the advice go back
    to the false sentence - a half-fix that reads as done. Without this
    mutation the pin set would not prove the plumbing matters.
  * **M2** keeps all the plumbing and defuses the probe instead (``except`` ->
    ``pass``). Same verdict, different mistake, and it is the one a reader
    would make by "simplifying" the helper.

M0 restores the original code verbatim, so the pin set is read against the real
pre-fix shape instead of my description of it.

M3 and M4 break CONTROLS, because a control that cannot fail is not a control:
  * **M3** overcorrects the other way - a *missing* record gets called
    unreadable, which hides the genuinely unverified submission.
  * **M4** fires the unreadable warning unconditionally, which must disturb the
    working path.

Everything is restored byte-for-byte and the hashes are printed; the tool
refuses to report a verdict it could not restore.

It also refuses to report a verdict it did not measure: a pytest run that prints
no summary is treated as NO VERDICT, not as "every pin passed". The first run of
this tool after the full suite came back 5/5 SURVIVED for exactly that reason -
the repo's bulk-delete guard had killed pytest before it printed anything, and
"no FAILED line" was read as success.

Usage:  ./.venv/Scripts/python.exe tools/ble001_recovery_read_mutation.py
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_ble001_fail_closed.py"
SOURCE = REPO / "ai_assistant" / "submission_recovery.py"

PIN_REPORT = "test_an_unreadable_verification_record_is_not_reported_as_missing"
PIN_ADVICE = "test_an_unreadable_verification_record_does_not_advise_rechecking_the_site"
PIN_AGREE = "test_the_audit_and_the_recovery_report_agree_that_the_record_is_damaged"
PIN_MISSING = "test_a_missing_verification_record_is_still_reported_as_missing"
PIN_READABLE = "test_a_readable_verification_record_is_still_reported_as_verified"
PIN_RECONCILE = "test_an_unreadable_verification_record_still_fails_reconcile_closed"

SHORT = {
    PIN_REPORT: "report_not_missing",
    PIN_ADVICE: "advice_no_recheck",
    PIN_AGREE: "audit_agrees",
    PIN_MISSING: "ctrl_true_half",
    PIN_READABLE: "ctrl_readable",
    PIN_RECONCILE: "ctrl_reconcile_closed",
}

CONTENT_PINS = [PIN_REPORT, PIN_ADVICE, PIN_AGREE]

# --- the fixed shape, verbatim. An anchor that stops matching means the fix
# moved, and the tool says so rather than reporting a clean run. -------------

FLAG_FIXED = (
    "    verification_unreadable = bool(\n"
    "        ver is None and submission_id\n"
    "        and _verification_record_is_unreadable(vacancy_stable_id, submission_id)\n"
    "    )\n"
)

KWARG_FIXED = (
    "        browser_status,\n"
    "        verification_unreadable=verification_unreadable,\n"
    "    )\n"
)

PROBE_FIXED = (
    "    try:\n"
    "        data = json.loads(row[0]) if row[0] else {}\n"
    "        SubmissionVerification.model_validate(data)\n"
    "    except (ValueError, TypeError):\n"
    "        return True\n"
    "    return False\n"
)

WARNING_FIXED = (
    "    if verification_unreadable:\n"
    "        warnings.append(\n"
    '            "Verification record exists but cannot be read - its content is unknown"\n'
    "        )\n"
)

REASON_FIXED = (
    "        if not verification:\n"
    "            if verification_unreadable:\n"
    "                return (\n"
    "                    RecoveryStatus.NEEDS_VERIFICATION,\n"
    '                    "Submission exists but its verification record cannot be read",\n'
    "                    (\n"
    '                        "Inspect the stored verification record - it exists but cannot "\n'
    '                        "be parsed, so whether the submission succeeded is unknown. "\n'
    '                        "Do not assume it was never verified"\n'
    "                    ),\n"
    "                )\n"
)

# The original code, verbatim: no second case, one false sentence for both.
REASON_ORIGINAL = (
    "        if not verification:\n"
)

ROW_GUARD_FIXED = (
    "    if not row:\n"
    "        return False\n"
)

# (name, description, old, new, pins)
# `pins` lists only what the mutation can actually reach.
MUTATIONS = [
    (
        "M0",
        "the original shape, restored verbatim - the real red run",
        REASON_FIXED,
        REASON_ORIGINAL,
        CONTENT_PINS,
    ),
    (
        "M1",
        "compute the flag, then forget to pass it to the determiner",
        KWARG_FIXED,
        "        browser_status,\n    )\n",
        CONTENT_PINS,
    ),
    (
        "M2",
        "keep the plumbing, defuse the probe: the except swallows and falls through",
        PROBE_FIXED,
        (
            "    try:\n"
            "        data = json.loads(row[0]) if row[0] else {}\n"
            "        SubmissionVerification.model_validate(data)\n"
            "    except (ValueError, TypeError):\n"
            "        pass\n"
            "    return False\n"
        ),
        CONTENT_PINS,
    ),
    (
        "M3",
        "CONTROL: overcorrect - call a MISSING record unreadable",
        ROW_GUARD_FIXED,
        "    if not row:\n        return True\n",
        [PIN_MISSING],
    ),
    (
        "M4",
        "CONTROL: fire the unreadable warning unconditionally",
        WARNING_FIXED,
        WARNING_FIXED.replace("    if verification_unreadable:\n", "    if True:\n"),
        [PIN_READABLE],
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pins(node_ids: list[str]) -> tuple[dict[str, str], bool]:
    """Return ({pin: PASS/FAIL}, pytest_actually_reported).

    The second value exists because of a real trap hit on 2026-09-20: this tool
    was run right after the full suite had been killed by the repo's bulk-delete
    guard, and the leftover temp dir made the guard kill this pytest too. A
    killed run prints no FAILED lines, so every pin read as PASS and all five
    mutations were reported as SURVIVED - a clean verdict produced by having no
    measurement at all. "No FAILED line" is not evidence of passing.
    """
    expr = " or ".join(node_ids)
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, "--no-header", "-rf"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    out = proc.stdout + proc.stderr
    reported = bool(re.search(r"\d+ (passed|failed|error)", out))
    verdicts = {
        pin: ("FAIL" if re.search(rf"^FAILED .*::{pin}\b", out, re.MULTILINE) else "PASS")
        for pin in node_ids
    }
    if not reported:
        print(f"     !! pytest printed no summary (returncode {proc.returncode}); "
              f"output tail: {(out[-300:] or '<empty>')!r}")
    return verdicts, reported


def main() -> int:
    original = SOURCE.read_bytes()
    before = sha(SOURCE)

    print(f"submission_recovery.py sha256 before: {before}")
    print(f"{len(MUTATIONS)} mutations\n")
    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    failures = []
    try:
        for name, description, old, new, pins in MUTATIONS:
            count = original.count(old.encode())
            if count != 1:
                print(
                    f"{name:4s} !! anchor appears {count} times, expected 1: "
                    f"{old.splitlines()[0]!r}"
                )
                failures.append(name)
                continue
            mutated = original.replace(old.encode(), new.encode(), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            SOURCE.write_bytes(mutated)
            verdicts, reported = run_pins(pins)
            if not reported:
                print(f"{name:4s} !! NO VERDICT - mutation left unresolved, not counted")
                failures.append(name)
                continue
            caught = all(verdicts[p] == "FAIL" for p in pins)
            status = "caught" if caught else "SURVIVED"
            print(f"{name:4s} {status:9s} {description}")
            print(f"     evidence: {', '.join(f'{SHORT[p]}={verdicts[p]}' for p in pins)}")
            if not caught:
                failures.append(name)
    finally:
        SOURCE.write_bytes(original)

    after = sha(SOURCE)
    print()
    print(f"submission_recovery.py sha256 after : {after}")
    print(f"restored byte-for-byte: {after == before}")
    if after != before:
        return 2

    print()
    if failures:
        print(f"RESULT: {len(failures)} mutation(s) survived: {', '.join(failures)}")
        return 1
    print(f"RESULT: all {len(MUTATIONS)} mutations caught, tree restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
