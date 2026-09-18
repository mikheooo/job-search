"""Paired mutations for BLE001 finding #48.

Finding #48: in ``hh_submit_policy.evaluate`` gate 3, when a fingerprint could be
computed but no review fingerprint was stored to compare it against, the gate
recorded ``fingerprint_match`` - a *passed* check - so an audit reading
``checks_passed`` saw "the fingerprint was verified" when nothing had been
compared. The record travels into ``SubmitApproval`` and from there into the
evidence of the SUBMITTED transition.

The pins live in ``tests/test_hh_submit_policy.py``; this script proves they can
fail, because a pin that cannot fail is a comment.

Pairs matter more than single mutations. M1 and M5 break the *guard* (the record
starts lying again). M2, M3 and M4 break the *positive direction*: M2 makes the
honest name appear where a real comparison did happen, M3 "fixes" the lie by
refusing, M4 "fixes" it by throwing the computed value away. Without that second
kind, "the tests are green" would also be satisfied by a gate that refuses
everything or by one that stops reporting the fingerprint at all.

Every mutation is applied to a copy of the real source file, the named pins are
run against it, and the file is restored byte-for-byte in a ``finally`` block.
The script refuses to report success if the file changed, and it prints the
before/after hash.

Usage:
    python tools/ble001_submit_policy_fingerprint_mutation.py

Exit code is 0 only when every mutation was caught by the pins it names.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
TEST_FILE = "tests/test_hh_submit_policy.py"
POLICY = "ai_assistant/hh_submit_policy.py"


def pin(name: str) -> str:
    return f"{TEST_FILE}::{name}"


PINS = {
    "bad": pin("test_policy_without_a_stored_fingerprint_does_not_claim_the_check_passed"),
    "ctl": pin("test_policy_still_records_a_real_fingerprint_match"),
    "verdict": pin("test_the_fingerprint_record_change_does_not_change_the_verdict"),
}

HONEST = """        resolved_fp = actual_fp
        checks_passed.append("fingerprint_not_compared")
"""

# (name, old, new, pins that must go red)
MUTATIONS: list[tuple[str, str, str, list[str]]] = [
    (
        "M1 the record starts claiming a comparison that never happened (the hole)",
        '        checks_passed.append("fingerprint_not_compared")\n',
        '        checks_passed.append("fingerprint_match")\n',
        [PINS["bad"], PINS["verdict"]],
    ),
    (
        "M2 a real comparison is recorded as 'not compared' (honest in the wrong branch)",
        """        if expected_fp == actual_fp:
            resolved_fp = actual_fp
            checks_passed.append("fingerprint_match")
""",
        """        if expected_fp == actual_fp:
            resolved_fp = actual_fp
            checks_passed.append("fingerprint_not_compared")
""",
        [PINS["ctl"]],
    ),
    (
        "M3 the lie is 'fixed' by refusing the submission",
        HONEST,
        """        checks_failed.append("fingerprint_match")
        reasons.append("fingerprint_not_comparable")
""",
        [PINS["bad"], PINS["verdict"]],
    ),
    (
        "M4 the lie is 'fixed' by throwing the computed fingerprint away",
        HONEST,
        """        resolved_fp = None
        checks_passed.append("fingerprint_not_compared")
""",
        [PINS["verdict"]],
    ),
    (
        "M5 the honest name is added but the lie is left in place",
        '        checks_passed.append("fingerprint_not_compared")\n',
        """        checks_passed.append("fingerprint_match")
        checks_passed.append("fingerprint_not_compared")
""",
        [PINS["bad"]],
    ),
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def run_pins(node_ids: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", *node_ids],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode, tail[-1] if tail else "(no output)"


def main() -> int:
    original = (REPO / POLICY).read_bytes()
    before = digest(REPO / POLICY)

    print("=== baseline (pins must be green before mutating) ===")
    rc, summary = run_pins(sorted(PINS.values()))
    print(f"    all pins: rc={rc}  {summary}")
    if rc != 0:
        print("    REFUSING TO CONTINUE: the pins are not green on the unmutated tree.")
        return 1

    text = original.decode("utf-8")
    caught = 0
    for name, old, new, expected in MUTATIONS:
        path = REPO / POLICY
        if text.count(old) != 1:
            print(f"--- {name}")
            print(f"    SKIPPED: anchor occurs {text.count(old)} times in {POLICY}")
            continue
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            rc, summary = run_pins(expected)
        finally:
            path.write_bytes(original)

        ok = rc != 0
        caught += 1 if ok else 0
        print(f"--- {name}")
        print(f"    expected red: {len(expected)} pin(s)")
        print(f"    {'CAUGHT' if ok else 'SURVIVED'}: rc={rc}  {summary}")

    print()
    print("=== integrity ===")
    now = (REPO / POLICY).read_bytes()
    clean = now == original
    print(f"    {POLICY}: {'restored' if clean else 'CHANGED'} ({before} -> {digest(REPO / POLICY)})")

    print()
    print("=== summary ===")
    print(f"    mutations caught : {caught} of {len(MUTATIONS)}")
    print(f"    tree restored    : {'yes' if clean else 'NO - inspect before committing'}")
    return 0 if caught == len(MUTATIONS) and clean else 1


if __name__ == "__main__":
    sys.exit(main())
