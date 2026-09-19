#!/usr/bin/env python
"""Mutation check for finding #52: does the gate-6 fix actually hold?

The fix in ``hh_submit_policy.evaluate`` changes three things in gate 6:

  1. the handler is narrowed from ``except Exception`` to the four exceptions the
     three row readers can actually raise;
  2. a row that was there and could not be read now CLOSES the gate instead of
     leaving an empty description behind;
  3. an empty description no longer earns ``language_match`` - it earns
     ``language_match_without_description``.

Each mutation below puts a different plausible mistake back and asks whether
something notices. Four are caught by the pins; one is caught by ruff instead,
because it changes no behaviour at all - widening the handler back to
``except Exception`` leaves every verdict identical and only reintroduces the
blind except. Saying that plainly is the point: the pins do not cover it, and
pretending otherwise would be the same kind of false record this triage is about.

One mutation breaks a CONTROL, because a control that cannot fail is not a
control.

Everything is restored byte-for-byte and the hashes are printed; the tool refuses
to report a verdict it could not restore.

Usage:  ./.venv/Scripts/python.exe tools/ble001_gate6_mutation.py
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
SOURCE = REPO / "ai_assistant" / "hh_submit_policy.py"

PIN_UNREADABLE = "test_a_vacancy_row_that_cannot_be_read_does_not_claim_a_language_match"
PIN_UNKNOWN_SHAPE = "test_a_vacancy_row_of_an_unknown_shape_does_not_claim_a_language_match"
PIN_NO_ROW = "test_a_vacancy_that_was_never_read_does_not_claim_a_language_match"
PIN_EMPTY_ID = "test_an_empty_vacancy_id_skips_the_lookup_entirely"
PIN_NO_DESCRIPTION = "test_a_vacancy_with_no_description_is_marked_but_does_not_block"
PIN_HEALTHY = "test_a_vacancy_that_was_read_still_records_a_language_match"

DOOR_PINS = [PIN_UNREADABLE, PIN_UNKNOWN_SHAPE]
MARK_PINS = [PIN_NO_ROW, PIN_EMPTY_ID, PIN_NO_DESCRIPTION]

SHORT = {
    PIN_UNREADABLE: "door1_unreadable",
    PIN_UNKNOWN_SHAPE: "door2_shape",
    PIN_NO_ROW: "door3_no_row",
    PIN_EMPTY_ID: "door3_empty_id",
    PIN_NO_DESCRIPTION: "boundary_empty_desc",
    PIN_HEALTHY: "control_read_ok",
}

# (name, description, old, new, pins, detector)
# detector "pins" -> every listed pin must fail; detector "ruff" -> BLE001 must appear.
# `pins` lists only what the mutation can actually reach: M2 touches the exception
# path alone, and door 2 never raises, so door 2 is M3's business and not M2's.
MUTATIONS = [
    (
        "M1",
        "widen back to `except Exception` - the exact BLE001 regression",
        "        except (KeyError, TypeError, IndexError, AttributeError) as exc:\n",
        "        except Exception as exc:\n",
        [],
        "ruff",
    ),
    (
        "M2",
        "keep the narrow handler but swallow it silently (pre-fix consequence)",
        '            vac_row_unreadable = f"{type(exc).__name__}: {exc}"\n',
        "            pass\n",
        # Only door 1 goes through this handler; door 2 is caught by M3.
        [PIN_UNREADABLE],
        "pins",
    ),
    (
        "M3",
        "drop the unrecognised-shape branch, so door 2 goes silent again",
        (
            "            else:\n"
            '                vac_row_unreadable = f"unrecognised row shape: '
            '{type(vac_row).__name__}"\n'
        ),
        "",
        [PIN_UNKNOWN_SHAPE],
        "pins",
    ),
    (
        "M4",
        "an empty description earns `language_match` again - the pre-fix claim",
        '        checks_passed.append("language_match_without_description")\n',
        '        checks_passed.append("language_match")\n',
        MARK_PINS,
        "pins",
    ),
    (
        "M5",
        "CONTROL CHECK: mark every non-mismatch, so the healthy pin must fire",
        "    elif not vac_desc:\n",
        "    elif True:\n",
        [PIN_HEALTHY],
        "pins",
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


def run_ruff() -> tuple[bool, str]:
    """True when ruff flags BLE001 on the source, plus the line it flagged.

    Missing ruff is not a pass: the tool says so instead of quietly agreeing.
    """
    try:
        proc = subprocess.run(
            ["uvx", "ruff@0.16.6", "check", "--no-cache", "--output-format=concise",
             str(SOURCE)],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ruff unavailable: {type(exc).__name__}"
    lines = [ln for ln in (proc.stdout + proc.stderr).splitlines() if "BLE001" in ln]
    if not lines:
        return False, "ruff reported no BLE001"
    return True, lines[0].strip()


def main() -> int:
    original = SOURCE.read_bytes()
    before = sha(SOURCE)

    print(f"hh_submit_policy.py sha256 before: {before}")
    print(f"{len(MUTATIONS)} mutations\n")
    header = f"{'mut':4s} {'detector':9s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    failures = []
    try:
        for name, description, old, new, pins, detector in MUTATIONS:
            count = original.count(old.encode())
            if count != 1:
                print(f"{name:4s} !! anchor appears {count} times, expected 1: {old.strip()!r}")
                failures.append(name)
                continue
            mutated = original.replace(old.encode(), new.encode(), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            SOURCE.write_bytes(mutated)
            if detector == "ruff":
                caught, detail = run_ruff()
            else:
                verdicts = run_pins(pins)
                caught = all(verdicts[p] == "FAIL" for p in pins)
                detail = ", ".join(f"{SHORT[p]}={verdicts[p]}" for p in pins)
            status = "caught" if caught else "SURVIVED"
            print(f"{name:4s} {detector:9s} {status:9s} {description}")
            print(f"     evidence: {detail}")
            if not caught:
                failures.append(name)
    finally:
        SOURCE.write_bytes(original)

    after = sha(SOURCE)
    print()
    print(f"hh_submit_policy.py sha256 after : {after}")
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
