"""Mutation check for the RED-zone guard in tests/test_ble001_zone_policy.py.

Two mutations, both must be caught, and they exercise different branches:

  M1  add a silent broad handler to ai_assistant/submission_recovery.py
      -> the count goes UP. Expect the "new silent failure" branch.

  M2  raise the frozen baseline for submission_recovery.py by one
      -> the count is BELOW the baseline. Expect the "lock it in" branch.
      This is the branch that did not exist before 2026-09-17, and its absence
      is why the guard drifted open by ten slots.

Both mutations are applied to a byte copy, the test is run, the copy is
restored, and the restore is verified by md5. A mutation that cannot be undone
is not an experiment, it is a change.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"
TARGET = REPO / "ai_assistant" / "submission_recovery.py"
TESTFILE = REPO / "tests" / "test_ble001_zone_policy.py"

MUTATION_HANDLER = b"""


def _ble001_mutation_probe() -> bool:
    try:
        return bool(1)
    except Exception:
        pass
    return False
"""


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def run_test() -> tuple[int, str]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", "tests/test_ble001_zone_policy.py", "-q", "--no-header"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return proc.returncode, out


def report(label: str, rc: int, out: str, must_contain: str) -> bool:
    caught = rc != 0 and must_contain in out
    print(f"\n--- {label} ---")
    print(f"    exit code     : {rc}  (non-zero = the guard noticed)")
    print(f"    message match : {must_contain!r} in output -> {must_contain in out}")
    print(f"    VERDICT       : {'CAUGHT' if caught else 'SURVIVED (guard is blind)'}")
    tail = [ln for ln in out.splitlines() if ln.strip()][-4:]
    for ln in tail:
        print(f"      | {ln[:150]}")
    return caught


def main() -> int:
    original = TARGET.read_bytes()
    original_hash = md5(original)
    test_original = TESTFILE.read_bytes()
    test_hash = md5(test_original)
    results: list[bool] = []

    try:
        # --- M1: a new silent handler on the submission path ---
        TARGET.write_bytes(original + MUTATION_HANDLER)
        rc, out = run_test()
        results.append(report("M1  new silent handler added", rc, out, "New silent failure"))
        TARGET.write_bytes(original)

        # --- M2: a baseline that is too high (stale ceiling) ---
        mutated_test = test_original.replace(
            b'"submission_recovery.py": 0,', b'"submission_recovery.py": 1,', 1
        )
        assert mutated_test != test_original, "M2 anchor did not match"
        TESTFILE.write_bytes(mutated_test)
        rc, out = run_test()
        results.append(report("M2  baseline raised above the measured count", rc, out, "lock it in"))
        TESTFILE.write_bytes(test_original)
    finally:
        TARGET.write_bytes(original)
        TESTFILE.write_bytes(test_original)

    print("\n=== restore ===")
    ok_target = md5(TARGET.read_bytes()) == original_hash
    ok_test = md5(TESTFILE.read_bytes()) == test_hash
    print(f"    {TARGET.name}: md5 {md5(TARGET.read_bytes())} (was {original_hash}) -> {ok_target}")
    print(f"    {TESTFILE.name}: md5 {md5(TESTFILE.read_bytes())} (was {test_hash}) -> {ok_test}")
    if not (ok_target and ok_test):
        print("    !! RESTORE FAILED")
        return 2

    print("\n=== sanity: the guard is green again on the restored tree ===")
    rc, out = run_test()
    print(f"    exit code {rc}, {'green' if rc == 0 else 'NOT GREEN'}")
    results.append(rc == 0)

    print("\n=== summary ===")
    print(f"    mutations caught : {sum(results[:2])} of 2")
    print("    tree restored    : yes")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
