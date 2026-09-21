#!/usr/bin/env python
"""Mutation check for finding #54: is the "not made" marker load-bearing?

Finding #54 is the reachable half of the skip finding #53 closed the
unreachable half of. A review whose gate record carries no vacancy confirmed the
submission and reported a comparison that never ran. The fix records the fact:

    if gate_vacancy:
        if gate_vacancy != vacancy_stable_id:
            return {"ok": False, ...}
        reason = "human confirmation recorded"
    else:
        reason = ("human confirmation recorded "
                  "(vacancy comparison not made: no approved vacancy on record)")

and the same note is appended to the sibling's READY reason in
``hh_submission.preflight_submission``.

The mutations that carry the finding:

  * **M0** restores the pre-fix line verbatim, so the pin set is read against
    the real old code rather than against a prediction of it.
  * **M1** is option A from the report - refuse a record without a vacancy. It
    is the tempting strict answer, and it blocks legitimate reviews.
  * **M2** / **M4** over-mark: they add the note where the comparison DID run
    and passed. A control that cannot fail is not a control, so the two
    counter-pins have to be shown able to fire.
  * **M5** marks the empty mapping but not the absent field - a partial fix,
    which is the shape a careless edit would produce.

Verdicts come from ``--junitxml``, not from stdout, and the criterion is
``any()``: the question is whether the pin set is blind to the error, and one
pin that fires answers it. A pin missing from the report is ``UNKNOWN``, never
``PASS`` - "not mentioned" and "passed" are different facts.

Everything is restored byte-for-byte and the hashes are printed; the tool
refuses to report a verdict it could not restore.

Usage:  ./.venv/Scripts/python.exe tools/ble001_vacancy_note_mutation.py
        ./.venv/Scripts/python.exe tools/ble001_vacancy_note_mutation.py M0 M3
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_ble001_fail_closed.py"
HCS = REPO / "ai_assistant" / "hh_human_submission.py"
HS = REPO / "ai_assistant" / "hh_submission.py"

PIN_C1 = "test_a_confirmation_with_no_vacancy_on_record_does_not_claim_the_comparison"
PIN_C1_MATCH = "test_a_matching_vacancy_still_records_a_plain_confirmation"
PIN_C1_REFUSE = "test_a_refused_mismatch_does_not_carry_the_not_made_marker"
PIN_C2 = "test_the_sibling_preflight_reports_the_same_not_made_marker"
PIN_C2_PLAIN = "test_the_sibling_preflight_stays_plain_on_a_real_match"
PIN_Q53_ABSENT = "test_a_review_with_no_vacancy_on_record_still_confirms"

ALL_PINS = [PIN_C1, PIN_C1_MATCH, PIN_C1_REFUSE, PIN_C2, PIN_C2_PLAIN, PIN_Q53_ABSENT]

SHORT = {
    PIN_C1: "c1_marks",
    PIN_C1_MATCH: "c1_ctrl_match",
    PIN_C1_REFUSE: "c1_ctrl_refuse",
    PIN_C2: "c2_marks",
    PIN_C2_PLAIN: "c2_ctrl_plain",
    PIN_Q53_ABSENT: "q53_absent",
}

# The fixed copy-1 shape, verbatim.
C1_FIXED = (
    "    if gate_vacancy:\n"
    "        if gate_vacancy != vacancy_stable_id:\n"
    '            return {"ok": False, "reason": f"vacancy mismatch vs approved review: {gate_vacancy} != {vacancy_stable_id}"}\n'
    "        # The comparison ran and passed, so the record says nothing extra.\n"
    '        reason = "human confirmation recorded"\n'
    "    else:\n"
)

C1_OLD = (
    "    if gate_vacancy and gate_vacancy != vacancy_stable_id:\n"
    '        return {"ok": False, "reason": f"vacancy mismatch vs approved review: {gate_vacancy} != {vacancy_stable_id}"}\n'
)

C1_MATCH_LINE = '        reason = "human confirmation recorded"\n'
C1_ELSE_HEAD = "    else:\n"

C2_READY = (
    '    report.reason = "all gates passed; submit button found and enabled" + vacancy_note\n'
)
C2_NOTE = (
    '        vacancy_note = " (vacancy comparison not made: no approved vacancy on record)"\n'
)

# (name, target, description, old, new, pins)
MUTATIONS = [
    (
        "M0", HCS,
        "the pre-fix line, restored verbatim - the real red run",
        C1_FIXED,
        C1_OLD,
        [PIN_C1, PIN_C1_MATCH],
    ),
    (
        "M1", HCS,
        "option A: refuse a record without a vacancy instead of recording the skip",
        C1_MATCH_LINE + C1_ELSE_HEAD,
        C1_MATCH_LINE
        + "    elif True:\n"
        + '        return {"ok": False, "reason": "no approved vacancy on record"}\n'
        + C1_ELSE_HEAD,
        [PIN_C1, PIN_Q53_ABSENT],
    ),
    (
        "M2", HCS,
        "over-mark: note the comparison as not made even when it ran and passed",
        C1_MATCH_LINE,
        '        reason = "human confirmation recorded (vacancy comparison not made)"\n',
        [PIN_C1_MATCH],
    ),
    (
        "M3", HS,
        "drop the note from the sibling's READY reason",
        C2_READY,
        '    report.reason = "all gates passed; submit button found and enabled"\n',
        [PIN_C2],
    ),
    (
        "M4", HS,
        "over-mark the sibling: set the note in the matched branch too",
        "    if gate_vacancy:\n        if gate_vacancy != report.vacancy_stable_id:\n",
        (
            "    if gate_vacancy:\n"
            '        vacancy_note = " (vacancy comparison not made: no approved vacancy on record)"\n'
            "        if gate_vacancy != report.vacancy_stable_id:\n"
        ),
        [PIN_C2_PLAIN],
    ),
    (
        "M5", HCS,
        "partial fix: mark the empty mapping but not the absent field",
        "    if gate_vacancy:\n        if gate_vacancy != vacancy_stable_id:\n",
        "    if gate_vacancy or gate_data is None:\n        if gate_vacancy != vacancy_stable_id:\n",
        [PIN_C1],
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pins(pins: list[str], xml: Path) -> dict[str, str]:
    """PASS / FAIL / UNKNOWN per pin, read from the junit report.

    UNKNOWN is not PASS: a pin that did not run is a pin that measured nothing,
    and calling that a pass is how a blind set of pins looks green.
    """
    expr = " or ".join(pins)
    subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, f"--junitxml={xml}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    seen: dict[str, str] = {}
    if xml.exists():
        root = ET.parse(xml).getroot()
        for case in root.iter("testcase"):
            name = case.get("name") or ""
            bad = case.find("failure") is not None or case.find("error") is not None
            for pin in pins:
                if name.startswith(pin) and (bad or pin not in seen):
                    # A pin can be parametrized, so several cases carry the same
                    # pin name. FAIL must win over PASS: keeping the last case
                    # seen hides a failing parameter behind a passing one, and
                    # measured, that is exactly what it did -- mutation M5 broke
                    # only [absent] while [empty-dict] and [blank-vacancy] still
                    # passed, and the pin was reported PASS, so a real catch was
                    # written down as SURVIVED.
                    seen[pin] = "FAIL" if bad else "PASS"
    return {pin: seen.get(pin, "UNKNOWN") for pin in pins}


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if not a.startswith("-")]
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    if not selected:
        print(f"no mutation matches {wanted}; known: {[m[0] for m in MUTATIONS]}")
        return 2

    originals = {p: p.read_bytes() for p in (HCS, HS)}
    before = {p: sha(p) for p in originals}
    for path, digest in before.items():
        print(f"{path.name} sha256 before: {digest}")
    print(f"{len(selected)} of {len(MUTATIONS)} mutations\n")

    xml = REPO / ".pytest_q54_mutation.xml"
    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    survived = []
    try:
        for name, target, description, old, new, pins in selected:
            original = originals[target]
            count = original.count(old.encode())
            if count != 1:
                print(f"{name:4s} !! anchor appears {count} times, expected 1:"
                      f" {old.splitlines()[0][:60]!r}")
                survived.append(name)
                continue
            mutated = original.replace(old.encode(), new.encode(), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                survived.append(name)
                continue

            target.write_bytes(mutated)
            verdicts = run_pins(pins, xml)
            fired = [p for p in pins if verdicts[p] == "FAIL"]
            unknown = [p for p in pins if verdicts[p] == "UNKNOWN"]
            caught = bool(fired)
            print(f"{name:4s} {'caught' if caught else 'SURVIVED':9s} {description}")
            print("     evidence: " + ", ".join(
                f"{SHORT[p]}={verdicts[p]}" for p in pins))
            if unknown:
                print(f"     !! never ran (UNKNOWN, not PASS): "
                      f"{', '.join(SHORT[p] for p in unknown)}")
            if not caught:
                survived.append(name)
            target.write_bytes(original)
    finally:
        for path, data in originals.items():
            path.write_bytes(data)

    print()
    ok = True
    for path, digest in before.items():
        after = sha(path)
        same = after == digest
        ok = ok and same
        print(f"{path.name} sha256 after : {after}")
        print(f"  restored byte-for-byte: {same}")
    if not ok:
        return 2

    print()
    if survived:
        print(f"RESULT: {len(survived)} mutation(s) survived: {', '.join(survived)}")
        return 1
    print(f"RESULT: all {len(selected)} mutations caught, tree restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
