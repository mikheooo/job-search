#!/usr/bin/env python
"""Mutation check for finding #57: is an unreadable package still invisible?

The fix has six halves in six files, because the same damaged row was read six
different ways. Each half has to be provable on its own:

  * ``application_integrity._check_package_json_records`` gives the auditor a
    check on the package column, the way it already had one on the queue (#33),
    on the verifications (#55) and on the reviews (#56).
  * ``application_review.approve_review`` refuses to approve a package nobody
    read, and syncs ``validation_status`` on the idempotent path too.
  * ``application_review._mark_package_valid`` writes that sync BEFORE the
    approval, without the old ``except Exception: pass``.
  * ``application_prep.package_record_is_unreadable`` is the single definition
    of "readable" all six readers now ask.
  * ``browser_executor.prepare_application_in_browser`` stops calling an
    unreadable package READY_FOR_REVIEW.
  * ``hh_submit_policy.evaluate`` names the damaged record instead of crashing
    with a raw JSONDecodeError.

Mutations, one mistake each:

  * **M0** drops the approval guard - the pre-fix shape of the most important
    half, so the pins are read against the code rather than against my
    description of it.
  * **M1** keeps the auditor check and drops its registration in ``run_audit``.
    A method nobody calls is the classic "I added the check" that checks
    nothing.
  * **M2** keeps the check and the registration and swallows the verdict -
    ``self._err(...)`` becomes ``continue``. This is the exact class-B shape the
    whole triage is about, now inside the fix for it.
  * **M3** drops the browser guard, so an unreadable package is ready again.
  * **M4** drops the sync on the idempotent approval path.
  * **M5** drops the policy guard, so the damaged record crashes again.
  * **M6** (CONTROL) makes the browser never ready at all. A control that cannot
    fail is not a control.
  * **M7** (CONTROL) makes the approval refuse everything, so the refusal
    spreads to healthy packages.
  * **M8** (CONTROL) makes ``package_record_is_unreadable`` never find anything,
    which is the "the helper is decoration" mistake.

Everything is restored byte-for-byte and the hashes are printed; the tool
refuses to report a verdict it could not restore.

It also refuses to report a verdict it did not measure, and it reads that
verdict from --junitxml rather than from the console - the bulk-delete guard
fires during pytest's own cleanup, after the last test has run and before the
summary prints, so a run can measure everything and print no summary. A pin
missing from the junit is UNKNOWN here, never PASS.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_package_read_mutation.py
    ./.venv/Scripts/python.exe tools/ble001_package_read_mutation.py M0 M1 M2
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
PREP = REPO / "ai_assistant" / "application_prep.py"
BROWSER = REPO / "ai_assistant" / "browser_executor.py"
POLICY = REPO / "ai_assistant" / "hh_submit_policy.py"

PIN_AUDIT = "test_an_unreadable_package_record_is_flagged_by_the_audit"
PIN_AUDIT_CTRL = "test_a_readable_package_record_is_not_flagged_by_the_audit"
PIN_REFUSE = "test_approving_against_an_unreadable_package_is_refused"
PIN_FP = "test_the_refused_approval_does_not_write_a_fingerprint_of_an_empty_package"
PIN_APPROVE_CTRL = "test_a_readable_package_is_still_approvable_and_gets_marked_valid"
PIN_BROWSER = "test_the_browser_preparer_does_not_call_an_unreadable_package_ready"
PIN_BROWSER_CTRL = "test_a_readable_package_with_an_approved_review_is_still_ready"
PIN_REPEAT = "test_a_repeat_approval_still_marks_the_package_valid"
PIN_POLICY = "test_the_submit_policy_refuses_an_unreadable_package_by_name"
PIN_POLICY_CTRL = "test_the_submit_policy_does_not_invent_an_unreadable_package"
PIN_AGREE = "test_the_audit_and_the_preparer_agree_that_the_package_is_damaged"

SHORT = {
    PIN_AUDIT: "audit_flags",
    PIN_AUDIT_CTRL: "ctrl_audit_quiet",
    PIN_REFUSE: "approve_refused",
    PIN_FP: "no_empty_fingerprint",
    PIN_APPROVE_CTRL: "ctrl_approve_ok",
    PIN_BROWSER: "browser_not_ready",
    PIN_BROWSER_CTRL: "ctrl_browser_ready",
    PIN_REPEAT: "repeat_syncs",
    PIN_POLICY: "policy_names_it",
    PIN_POLICY_CTRL: "ctrl_policy_quiet",
    PIN_AGREE: "audit_and_browser_agree",
}

AUDIT_PINS = [PIN_AUDIT, PIN_AGREE]
APPROVE_PINS = [PIN_REFUSE, PIN_FP]
BROWSER_PINS = [PIN_BROWSER, PIN_AGREE]
POLICY_PINS = [PIN_POLICY]

# --- anchors, verbatim. A mismatch means the fix moved, and the tool says so
# rather than reporting a clean run. ------------------------------------------

GUARD_FIXED = (
    "    from .application_prep import package_record_is_unreadable\n"
    "    if package_record_is_unreadable(vacancy_stable_id):\n"
    "        raise ValueError(\n"
    '            f"Application package for {vacancy_stable_id} exists but cannot be read "\n'
    '            "- refusing to approve a package nobody read"\n'
    "        )\n"
)
GUARD_REMOVED = ""

GUARD_ALWAYS_REFUSES = (
    "    if True:\n"
    "        raise ValueError(\n"
    '            "Application package cannot be read"\n'
    "        )\n"
)

REGISTRATION_FIXED = (
    "        self._check_review_json_records(tracked_sids)\n"
    "        self._check_package_json_records(tracked_sids)\n"
)
REGISTRATION_REMOVED = "        self._check_review_json_records(tracked_sids)\n"

REPORT_FIXED = (
    "            except Exception as exc:\n"
    "                self._err(\n"
    '                    "INVALID_PACKAGE_JSON",\n'
    "                    sid,\n"
    "                    None,\n"
    '                    "Application package has unreadable persisted JSON",\n'
    '                    {"version": version, "error": str(exc)},\n'
    "                )\n"
)
REPORT_DEFUSED = "            except Exception as exc:\n                continue\n"

BROWSER_COND_FIXED = (
    "                    if is_valid and not fields_skipped and not package_unreadable:\n"
)
BROWSER_COND_ORIGINAL = "                    if is_valid and not fields_skipped:\n"
BROWSER_COND_NEVER = "                    if False:\n"

IDEMPOTENT_FIXED = (
    "        if _mark_package_valid(vacancy_stable_id, pkg_row):\n"
    "            modified = True\n"
)
IDEMPOTENT_REMOVED = ""

POLICY_FIXED = (
    "        from .application_prep import package_record_is_unreadable\n"
    "\n"
    "        if package_record_is_unreadable(vac_stable_id):\n"
    '            checks_failed.append("package_json_unreadable")\n'
    '            reasons.append("package_json_unreadable")\n'
    "        elif isinstance(pkg_row, tuple):\n"
)
POLICY_ORIGINAL = "        if isinstance(pkg_row, tuple):\n"

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
        "drop the approval guard - the pre-fix shape",
        REVIEW,
        GUARD_FIXED,
        GUARD_REMOVED,
        APPROVE_PINS,
    ),
    (
        "M1",
        "keep the auditor check, drop its registration in run_audit",
        INTEGRITY,
        REGISTRATION_FIXED,
        REGISTRATION_REMOVED,
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
        "drop the browser guard, so an unreadable package is ready again",
        BROWSER,
        BROWSER_COND_FIXED,
        BROWSER_COND_ORIGINAL,
        BROWSER_PINS,
    ),
    (
        "M4",
        "drop the sync on the idempotent approval path",
        REVIEW,
        IDEMPOTENT_FIXED,
        IDEMPOTENT_REMOVED,
        [PIN_REPEAT],
    ),
    (
        "M5",
        "drop the policy guard, so the damaged record crashes again",
        POLICY,
        POLICY_FIXED,
        POLICY_ORIGINAL,
        POLICY_PINS,
    ),
    (
        "M6",
        "CONTROL: never ready at all, healthy packages included",
        BROWSER,
        BROWSER_COND_FIXED,
        BROWSER_COND_NEVER,
        [PIN_BROWSER_CTRL],
    ),
    (
        "M7",
        "CONTROL: refuse every approval, healthy packages included",
        REVIEW,
        GUARD_FIXED,
        GUARD_ALWAYS_REFUSES,
        [PIN_APPROVE_CTRL],
    ),
    (
        "M8",
        "CONTROL: the helper never finds anything unreadable",
        PREP,
        HELPER_BODY_FIXED,
        HELPER_BODY_NEVER_FINDS,
        [PIN_REFUSE, PIN_BROWSER, PIN_POLICY],
    ),
]

FILES = [INTEGRITY, SUBMISSION, REVIEW, PREP, BROWSER, POLICY]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin_tag(node_ids: list[str]) -> str:
    """A stable, filesystem-safe tag for a pin set - one junit file per set.

    Deliberately no unlink(): every deletion counts against the repo's
    bulk-delete budget, and that budget is what killed a mutation run in the
    middle of M1 on 2026-09-20. Overwriting a junit is free; deleting one is
    not.
    """
    return hashlib.sha1("|".join(sorted(node_ids)).encode()).hexdigest()[:8]


def run_pins(node_ids: list[str]) -> tuple[dict[str, str], dict[str, tuple[int, int]], bool]:
    """Return ({pin: PASS/FAIL/UNKNOWN}, {pin: (failed_cases, total)}, measured).

    Verdicts come from --junitxml, not from the console summary: the bulk-delete
    guard fires during pytest's own temp-dir cleanup, after the last test has run
    and before the summary line is printed, so a run can finish every test it was
    asked for and still print no summary at all. The junit is written as the run
    progresses and survives that.

    A parametrized pin is aggregated over ALL its cases: FAIL if any case failed.
    Taking the last case's verdict - which this tool did at first - reported
    ``no_empty_fingerprint=PASS`` under M0 while three of its five cases had in
    fact failed, because broken-json and json-list are caught by the second line
    of defence rather than by the guard the mutation removed. A pin is only as
    blind as its worst case.

    A pin that is missing from the junit is UNKNOWN, never PASS. A test that did
    not run is not a test that passed.
    """
    expr = " or ".join(node_ids)
    xml = REPO / f".pytest_q57_{pin_tag(node_ids)}.xml"
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
    # Accept mutation names as arguments: `... M0 M3` runs just those. Nine
    # pytest runs inside one shell invocation exhausts the bulk-delete budget,
    # so running them a few at a time is a supported way to get an honest
    # verdict out of this tool.
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
            # pin answers it. Requiring every pin to fail reported two caught
            # controls as SURVIVED on 2026-09-20 while the evidence line right
            # next to it read ctrl_true_half=FAIL.
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
