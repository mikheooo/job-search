#!/usr/bin/env python
"""Mutation check for finding #53: does the confirmation gate actually hold?

The fix in ``hh_human_submission.confirm_human_submission`` replaces a
try/except that fell back to ``""`` with the structural shape finding #32
already uses in ``hh_submission.py``:

    gate_data = entry.get("gate")
    if gate_data is None:            # absent -> legitimate skip
        gate_vacancy = ""
    elif isinstance(gate_data, dict):
        gate_vacancy = gate_data.get("vacancy_stable_id") or ""
    else:                            # anything else -> refuse
        return {"ok": False, "reason": "review gate record is unreadable (...)"}

Two mutations carry the finding, and they are the reason this tool exists:

  * **M3** puts ``or {}`` back. That alone reopens door 2 - the falsy
    non-mapping (``""`` / ``[]`` / ``0``) that is coerced to ``{}`` WITHOUT
    raising. Door 1 stays closed, so the pin list is door 2 only.
  * **M4** is the "obvious" fix: the old code with a *narrowed* handler
    instead of a blind one - exactly what finding #52 did to its own shape.
    It closes NEITHER door. A handler that falls back to ``""`` skips the
    comparison whether or not it catches precisely, so narrowing the except
    is not a fix here; the shape has to change. This is the measurement that
    separates #53 from #52, and it would be invisible without this mutation.

M0 reproduces the original bug, so the pin set can be read against the real
pre-fix code instead of against my prediction of it.

M5 breaks a CONTROL, because a control that cannot fail is not a control.

Everything is restored byte-for-byte and the hashes are printed; the tool
refuses to report a verdict it could not restore.

Usage:  ./.venv/Scripts/python.exe tools/ble001_human_confirm_mutation.py
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
SOURCE = REPO / "ai_assistant" / "hh_human_submission.py"

PIN_DOOR1 = "test_a_gate_record_that_is_not_a_mapping_does_not_confirm"
PIN_DOOR2 = "test_a_falsy_gate_record_does_not_confirm"
PIN_RECORD = "test_an_unreadable_gate_record_records_no_confirmation"
PIN_MATCH = "test_a_matching_vacancy_is_still_confirmed"
PIN_MISMATCH = "test_a_mismatched_vacancy_is_still_refused"
PIN_PREDATES = "test_a_review_with_no_vacancy_on_record_still_confirms"

SHORT = {
    PIN_DOOR1: "door1_non_mapping",
    PIN_DOOR2: "door2_falsy",
    PIN_RECORD: "record_not_written",
    PIN_MATCH: "ctrl_match",
    PIN_MISMATCH: "ctrl_mismatch",
    PIN_PREDATES: "ctrl_absent",
}

# The fixed shape, verbatim. Every mutation anchors on a slice of it, so an
# anchor that stops matching means the fix moved and the tool says so.
FIXED = (
    '    gate_data = entry.get("gate")\n'
    "    if gate_data is None:\n"
    '        gate_vacancy = ""\n'
    "    elif isinstance(gate_data, dict):\n"
    '        gate_vacancy = gate_data.get("vacancy_stable_id") or ""\n'
    "    else:\n"
    "        return {\n"
    '            "ok": False,\n'
    '            "reason": (\n'
    '                f"review gate record is unreadable ({type(gate_data).__name__}), "\n'
    '                "so the approved vacancy cannot be confirmed"\n'
    "            ),\n"
    "        }\n"
)

PREFIX_FIXED = (
    '    gate_data = entry.get("gate")\n'
    "    if gate_data is None:\n"
    '        gate_vacancy = ""\n'
    "    elif isinstance(gate_data, dict):\n"
)

ANOMALY_BRANCH = (
    "    else:\n"
    "        return {\n"
    '            "ok": False,\n'
    '            "reason": (\n'
    '                f"review gate record is unreadable ({type(gate_data).__name__}), "\n'
    '                "so the approved vacancy cannot be confirmed"\n'
    "            ),\n"
    "        }\n"
)

OLD_SHAPE = (
    '    gate_vacancy = ""\n'
    "    try:\n"
    '        gate_data = entry.get("gate") or {}\n'
    '        gate_vacancy = gate_data.get("vacancy_stable_id") or ""\n'
    "    except Exception:\n"
    '        gate_vacancy = ""\n'
)

# The "narrowed except" variant of the old shape: the shape finding #52 fixed
# its own copy with. Same fallback to "", precise handler.
NARROWED_SHAPE = (
    '    gate_vacancy = ""\n'
    "    try:\n"
    '        gate_data = entry.get("gate") or {}\n'
    '        gate_vacancy = gate_data.get("vacancy_stable_id") or ""\n'
    "    except (AttributeError, TypeError, KeyError):\n"
    '        gate_vacancy = ""\n'
)

MISMATCH_IF = "    if gate_vacancy and gate_vacancy != vacancy_stable_id:\n"

# (name, description, old, new, pins)
# `pins` lists only what the mutation can actually reach.
MUTATIONS = [
    (
        "M0",
        "the original bug, restored verbatim - the real red run",
        FIXED,
        OLD_SHAPE,
        [PIN_DOOR1, PIN_DOOR2, PIN_RECORD],
    ),
    (
        "M1",
        "keep the shape, but let the anomaly branch skip instead of refusing",
        ANOMALY_BRANCH,
        (
            "    else:\n"
            '        gate_vacancy = ""\n'
        ),
        [PIN_DOOR1, PIN_DOOR2, PIN_RECORD],
    ),
    (
        "M2",
        "drop the `is None` branch, so an ABSENT gate is refused too",
        PREFIX_FIXED,
        "    if isinstance(gate_data, dict):\n",
        [PIN_PREDATES],
    ),
    (
        "M3",
        "put `or {}` back - door 2 reopens on its own, door 1 stays shut",
        '    gate_data = entry.get("gate")\n',
        '    gate_data = entry.get("gate") or {}\n',
        [PIN_DOOR2],
    ),
    (
        "M4",
        "the obvious fix: old shape with a NARROWED handler (#52's move here)",
        FIXED,
        NARROWED_SHAPE,
        [PIN_DOOR1, PIN_DOOR2],
    ),
    (
        "M5",
        "CONTROL: refuse every confirmation, so the match pin must fire",
        MISMATCH_IF,
        "    if True:\n",
        [PIN_MATCH, PIN_PREDATES],
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pins(node_ids: list[str]) -> dict[str, str]:
    """Return {pin: PASS/FAIL} for one pytest run."""
    expr = " or ".join(node_ids)
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, "--no-header", "-rf"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    out = proc.stdout + proc.stderr
    return {
        pin: ("FAIL" if re.search(rf"^FAILED .*::{pin}\b", out, re.MULTILINE) else "PASS")
        for pin in node_ids
    }


def main() -> int:
    original = SOURCE.read_bytes()
    before = sha(SOURCE)

    print(f"hh_human_submission.py sha256 before: {before}")
    print(f"{len(MUTATIONS)} mutations\n")
    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    failures = []
    try:
        for name, description, old, new, pins in MUTATIONS:
            count = original.count(old.encode())
            if count != 1:
                print(f"{name:4s} !! anchor appears {count} times, expected 1: {old.splitlines()[0]!r}")
                failures.append(name)
                continue
            mutated = original.replace(old.encode(), new.encode(), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            SOURCE.write_bytes(mutated)
            verdicts = run_pins(pins)
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
    print(f"hh_human_submission.py sha256 after : {after}")
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
