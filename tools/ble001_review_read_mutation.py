#!/usr/bin/env python
"""Mutation check for finding #56: is a broken review row still invisible?

The fix has two halves in two files, and each half has to be provable on its own:

  * ``application_integrity._check_review_json_records`` gives the auditor a
    check on the review column, the way it already had one on the queue (#33)
    and on the verifications (since before #33).
  * ``hh_submission._check_review_gate`` stops calling a row it cannot read
    "No review found for vacancy".

Mutations, one mistake each:

  * **M0** restores the original gate verbatim - the real pre-fix shape, so the
    pin set is read against the code rather than against my description of it.
  * **M1** keeps the check and drops its registration in ``run_audit``. A
    method nobody calls is the classic "I added the check" that checks nothing.
  * **M2** keeps the check, keeps the registration, and swallows the verdict -
    ``self._err(...)`` becomes ``continue``. This is the exact class-B shape
    the whole triage is about, now inside the fix for it.
  * **M3** (CONTROL) makes the gate branch unconditional, so a *missing* review
    is described as a damaged one. A control that cannot fail is not a control.
  * **M4** (CONTROL) makes the audit raise before comparing anything, so every
    readable row is flagged. Same defect, opposite direction.
  * **M5** (CONTROL) makes ``review_record_is_unreadable`` answer True when
    there is no row at all, which blurs the one distinction the helper exists
    to preserve.

Everything is restored byte-for-byte and the hashes are printed; the tool
refuses to report a verdict it could not restore.

It also refuses to report a verdict it did not measure, and it reads that
verdict from --junitxml rather than from the console. The bulk-delete guard
fires during pytest's own cleanup, after the last test has run and before the
summary line prints, so "no summary" happens on runs that measured everything.
The first version of this tool read the console, saw no FAILED lines, and
reported a clean 5/5 SURVIVED out of a killed run. A pin missing from the junit
is UNKNOWN here, never PASS.

Usage:  ./.venv/Scripts/python.exe tools/ble001_review_read_mutation.py
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
SUBMISSION = REPO / "ai_assistant" / "hh_submission.py"
REVIEW = REPO / "ai_assistant" / "application_review.py"

PIN_AUDIT = "test_an_unreadable_review_record_is_flagged_by_the_audit"
PIN_AUDIT_CTRL = "test_a_readable_review_record_is_not_flagged_by_the_audit"
PIN_GATE = "test_the_review_gate_does_not_call_an_unreadable_record_missing"
PIN_MISSING = "test_a_missing_review_record_is_still_reported_as_missing"
PIN_GATE_CTRL = "test_a_readable_approved_review_still_passes_the_gate"
PIN_REJECTED = "test_a_readable_rejected_review_still_fails_with_its_own_reason"
PIN_AGREE = "test_the_audit_and_the_gate_agree_that_the_record_is_damaged"

SHORT = {
    PIN_AUDIT: "audit_flags",
    PIN_AUDIT_CTRL: "ctrl_audit_quiet",
    PIN_GATE: "gate_says_damaged",
    PIN_MISSING: "ctrl_true_half",
    PIN_GATE_CTRL: "ctrl_gate_passes",
    PIN_REJECTED: "ctrl_rejected",
    PIN_AGREE: "audit_and_gate_agree",
}

AUDIT_PINS = [PIN_AUDIT, PIN_AGREE]
GATE_PINS = [PIN_GATE, PIN_AGREE]

# --- anchors, verbatim. A mismatch means the fix moved, and the tool says so
# rather than reporting a clean run. ------------------------------------------

GATE_FIXED = (
    "        if not review:\n"
    "            # A row that exists but cannot be parsed is not the same as no row\n"
    "            # (BLE001 finding #56). Saying \"No review found\" about a row that is\n"
    "            # there sends a human to create the review that already exists - and\n"
    "            # create_application_review() preserves created_at only when the\n"
    "            # existing row reads, so the repair overwrites what it cannot see.\n"
    "            # The gate keeps refusing either way; only the sentence changes.\n"
    "            if review_obj is None and review_record_is_unreadable(vacancy_stable_id):\n"
    "                return (\n"
    "                    GateCheckResult(\n"
    "                        passed=False,\n"
    "                        failed_gate=GateName.GATE_REVIEW_APPROVED,\n"
    "                        reason=(\n"
    "                            f\"Review record for {vacancy_stable_id} exists but \"\n"
    "                            \"cannot be read - approval state unknown\"\n"
    "                        ),\n"
    "                    ),\n"
    "                    None,\n"
    "                    None,\n"
    "                )\n"
)

GATE_ORIGINAL = "        if not review:\n"

GATE_BRANCH_FIXED = (
    "            if review_obj is None and review_record_is_unreadable(vacancy_stable_id):\n"
)

REGISTRATION_FIXED = (
    "        self._check_queue_json_records(tracked_sids)\n"
    "        self._check_review_json_records(tracked_sids)\n"
)

REPORT_FIXED = (
    "            except Exception as exc:\n"
    "                self._err(\n"
    '                    "INVALID_REVIEW_JSON",\n'
    "                    sid,\n"
    "                    None,\n"
    '                    "Review has unreadable persisted JSON",\n'
    '                    {"version": version, "status": status, "error": str(exc)},\n'
    "                )\n"
)

REPORT_DEFUSED = (
    "            except Exception as exc:\n"
    "                continue\n"
)

COMPARE_FIXED = (
    "                if parsed_status != status or parsed.review_version != version:\n"
    '                    raise ValueError("JSON fields do not match indexed columns")\n'
)

COMPARE_ALWAYS_RAISES = (
    '                raise ValueError("JSON fields do not match indexed columns")\n'
)

ROW_GUARD_FIXED = "    if not row:\n        return False\n"

# (name, description, target file, old, new, pins)
MUTATIONS = [
    (
        "M0",
        "the original gate, restored verbatim - the real red run",
        SUBMISSION,
        GATE_FIXED,
        GATE_ORIGINAL,
        GATE_PINS,
    ),
    (
        "M1",
        "keep the check, drop its registration in run_audit",
        INTEGRITY,
        REGISTRATION_FIXED,
        "        self._check_queue_json_records(tracked_sids)\n",
        AUDIT_PINS,
    ),
    (
        "M2",
        "keep the check and the registration, swallow the verdict",
        INTEGRITY,
        REPORT_FIXED,
        REPORT_DEFUSED,
        AUDIT_PINS,
    ),
    (
        "M3",
        "CONTROL: overcorrect - describe a MISSING review as damaged",
        SUBMISSION,
        GATE_BRANCH_FIXED,
        "            if True:\n",
        # PIN_GATE_CTRL is not listed: the branch sits inside `if not review:`,
        # so a readable review never reaches it and that pin cannot move.
        [PIN_MISSING],
    ),
    (
        "M4",
        "CONTROL: flag every readable row too",
        INTEGRITY,
        COMPARE_FIXED,
        COMPARE_ALWAYS_RAISES,
        [PIN_AUDIT_CTRL],
    ),
    (
        "M5",
        "CONTROL: call a MISSING row unreadable in the helper itself",
        REVIEW,
        ROW_GUARD_FIXED,
        "    if not row:\n        return True\n",
        # PIN_REJECTED is not listed: a readable REJECTED row is truthy, so the
        # gate never consults the helper for it.
        [PIN_MISSING],
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin_tag(node_ids: list[str]) -> str:
    """A stable, filesystem-safe tag for a pin set - one junit file per set.

    Deliberately no unlink(): every deletion this tool performs counts against
    the repo's bulk-delete budget, and that budget is what killed the run in
    the middle of M1 on 2026-09-20. Overwriting a junit is free; deleting one
    is not.
    """
    return hashlib.sha1("|".join(sorted(node_ids)).encode()).hexdigest()[:8]


def run_pins(node_ids: list[str]) -> tuple[dict[str, str], bool]:
    """Return ({pin: PASS/FAIL/UNKNOWN}, every pin was actually measured).

    Verdicts come from --junitxml, not from the console summary. The repo's
    bulk-delete guard fires during pytest's own temp-dir cleanup, after the last
    test has run and before the summary line is printed - so a run can finish
    every test it was asked for and still print no summary at all. The junit
    file is written as the run progresses and survives that. Judging by the
    console alone gave "5/5 SURVIVED" once; judging by "no FAILED line" would
    give the same answer for a run that never started.

    A pin that is missing from the junit is UNKNOWN, never PASS. A test that did
    not run is not a test that passed, and the whole point of this tool is to
    refuse that inference.
    """
    expr = " or ".join(node_ids)
    xml = REPO / f".pytest_q56_{pin_tag(node_ids)}.xml"
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, "--no-header", "-rf", f"--junitxml={xml}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    verdicts = dict.fromkeys(node_ids, "UNKNOWN")
    if xml.exists():
        root = ElementTree.parse(xml).getroot()
        for case in root.iter("testcase"):
            name = case.get("name", "")
            broken = any(child.tag in ("failure", "error") for child in case)
            for pin in node_ids:
                if name == pin or name.startswith(f"{pin}["):
                    verdicts[pin] = "FAIL" if broken else "PASS"
    else:
        out = proc.stdout + proc.stderr
        print(f"     !! no junit written (returncode {proc.returncode}); "
              f"output tail: {(out[-300:] or '<empty>')!r}")
    return verdicts, all(v != "UNKNOWN" for v in verdicts.values())


def main() -> int:
    # Accept mutation names as arguments: `... M0 M3` runs just those. Six
    # pytest runs inside one shell invocation is what exhausted the bulk-delete
    # budget on the first attempt, so running them one call at a time is a
    # supported way to get an honest verdict out of this tool.
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    mutations = [m for m in MUTATIONS if not only or m[0] in only]

    files = [INTEGRITY, SUBMISSION, REVIEW]
    originals = {p: p.read_bytes() for p in files}
    before = {p.name: sha(p) for p in files}

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
                verdicts, reported = run_pins(pins)
            finally:
                target.write_bytes(originals[target])

            if not reported:
                print(f"{name:4s} !! NO VERDICT - mutation left unresolved, not counted")
                failures.append(name)
                continue
            # Caught means SOME listed pin failed, not all of them: the question
            # is whether the pin set is blind to this mistake, and one failing
            # pin answers it. Requiring every pin to fail reported two caught
            # controls as SURVIVED on 2026-09-20 while the evidence line right
            # next to it read ctrl_true_half=FAIL.
            caught = any(verdicts[p] == "FAIL" for p in pins)
            status = "caught" if caught else "SURVIVED"
            print(f"{name:4s} {status:9s} {description}")
            print(f"     evidence: {', '.join(f'{SHORT[p]}={verdicts[p]}' for p in pins)}")
            if not caught:
                failures.append(name)
    finally:
        for path, blob in originals.items():
            path.write_bytes(blob)

    after = {p.name: sha(p) for p in files}
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
