"""Paired mutations for BLE001 finding #49.

Finding #49 was measured, not fixed: ``db._row_to_application`` turns an
unreadable ``answers_json`` into ``{}`` with no error marker, and the measurement
in ``tools/ble001_app_answers_probe.py`` showed that every consumer treats that
as "no answers" - which closes the gate. The pins in
``tests/test_ble001_fail_closed.py`` lock that direction, and a pin that cannot
fail is a comment, so this script proves they can.

The pair is on the reader itself, because that is the address under measurement:

  M1 breaks the GUARD - the except-branch fills a permissive default instead of
     an empty dict, which is exactly the shape a careless "helpful" fix takes.
  M2 breaks the POSITIVE DIRECTION - the reader ignores the column entirely, so
     a complete answers row stops reaching the gate. Without this one, "the pins
     are green" would also be satisfied by a reader that always returns {}.

Every mutation is applied to a copy of the real source file, the named pins are
run against it, and the file is restored byte-for-byte in a ``finally`` block.
The script refuses to report success if the file changed, and it prints the
before/after hash.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_app_answers_mutation.py

Exit code is 0 only when every mutation was caught by the pins it names.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
TEST_FILE = "tests/test_ble001_fail_closed.py"
DB = "ai_assistant/db.py"


def pin(name: str) -> str:
    return f"{TEST_FILE}::{name}"


PINS = {
    "bad": pin("test_an_unreadable_answers_column_on_the_application_fails_closed"),
    "ok": pin("test_a_complete_answers_column_on_the_application_still_passes"),
    "fallback": pin("test_the_questionnaires_own_answers_survive_a_corrupt_application_column"),
}

READER = """    answers = {}
    if row[8]:
        try:
            answers = json.loads(row[8])
        except Exception:
            answers = {}
"""

# (name, old, new, pins that must go red)
MUTATIONS: list[tuple[str, str, str, list[str]]] = [
    (
        "M1 the unreadable column is filled with a permissive default",
        READER,
        """    answers = {}
    if row[8]:
        try:
            answers = json.loads(row[8])
        except Exception:
            answers = {"q1": "filled-in-by-a-helpful-fix"}
""",
        [PINS["bad"], PINS["fallback"]],
    ),
    (
        "M2 the reader ignores the column, so a complete row stops reaching the gate",
        READER,
        """    answers = {}
    if row[8] and False:
        try:
            answers = json.loads(row[8])
        except Exception:
            answers = {}
""",
        [PINS["ok"]],
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
    original = (REPO / DB).read_bytes()
    before = digest(REPO / DB)

    print("=== baseline (pins must be green before mutating) ===")
    rc, summary = run_pins(sorted(PINS.values()))
    print(f"    all pins: rc={rc}  {summary}")
    if rc != 0:
        print("    REFUSING TO CONTINUE: the pins are not green on the unmutated tree.")
        return 1

    text = original.decode("utf-8")
    caught = 0
    for name, old, new, expected in MUTATIONS:
        path = REPO / DB
        if text.count(old) != 1:
            print(f"--- {name}")
            print(f"    SKIPPED: anchor occurs {text.count(old)} times in {DB}")
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
    now = (REPO / DB).read_bytes()
    clean = now == original
    print(f"    {DB}: {'restored' if clean else 'CHANGED'} ({before} -> {digest(REPO / DB)})")

    print()
    print("=== summary ===")
    print(f"    mutations caught : {caught} of {len(MUTATIONS)}")
    print(f"    tree restored    : {'yes' if clean else 'NO - inspect before committing'}")
    return 0 if caught == len(MUTATIONS) and clean else 1


if __name__ == "__main__":
    sys.exit(main())
