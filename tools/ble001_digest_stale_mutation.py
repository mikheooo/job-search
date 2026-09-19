"""Paired mutations for BLE001 finding #50.

Finding #50 fixed ``db.list_digest_attempts``: ``age_seconds`` started at 0 and
stayed 0 when the timestamp could not be read, so an ATTEMPTING batch of unknown
age was reported as fresh and never tripped Rule A. The fix makes the age ``None``
and treats an un-ageable ATTEMPTING row as stale, and ``cli.py`` renders the
unknown age as unknown instead of a number.

The pins in ``tests/test_ble001_fail_closed.py`` lock that, and a pin that cannot
fail is a comment, so this script proves each one can. Six mutations, paired so
that breaking the guard and breaking the positive direction are both covered:

  M1 restores the lie          - age_seconds goes back to 0 (the pre-fix shape).
  M2 keeps None but drops the  - the age is reported honestly as unknown and
     fail-closed half             then NOT treated as stale, so nothing fires.
  M3 makes every ATTEMPTING    - the opposite overreach: the stale rule stops
     row stale                    distinguishing old from recent.
  M4 drops the payload         - the documented fallback for an empty column.
     fallback
  M5 puts the zero back in     - the operator line prints a number again.
     the CLI
  M6 drops the reason          - age_error stops carrying why the read failed.

M2 is the one worth naming: it is the shape a "half fix" takes, and only the
fail-closed pin catches it.

Every mutation is applied to a copy of the real source file, the named pins are
run against it, and every touched file is restored byte-for-byte in a ``finally``
block. The script refuses to report success if any file changed, and it prints the
before/after hash of each.

Usage:
    ./.venv/Scripts/python.exe tools/ble001_digest_stale_mutation.py

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
CLI = "ai_assistant/cli.py"


def pin(name: str) -> str:
    return f"{TEST_FILE}::{name}"


PINS = {
    "unreadable": pin("test_an_unreadable_digest_timestamp_fails_closed"),
    "recent": pin("test_a_readable_recent_digest_timestamp_is_still_not_stale"),
    "fallback": pin("test_an_empty_digest_column_still_falls_back_to_the_payload"),
    "cli": pin("test_the_cli_reports_an_unknown_digest_age_as_unknown"),
}

INIT_AGE = "        age_seconds: int | None = None"

STALE_BLOCK = """        age_unknown = age_seconds is None
        is_stale = (
            persisted_status == "ATTEMPTING"
            and (age_unknown or age_seconds >= stale_threshold_seconds)
        )
"""

# (target file, name, old, new, pins that must go red)
MUTATIONS: list[tuple[str, str, str, str, list[str]]] = [
    (
        DB,
        "M1 age_seconds goes back to 0, restoring the pre-fix lie",
        INIT_AGE,
        "        age_seconds = 0",
        [PINS["unreadable"]],
    ),
    (
        DB,
        "M2 unknown age is reported honestly but is not treated as stale",
        STALE_BLOCK,
        """        age_unknown = age_seconds is None
        is_stale = (
            persisted_status == "ATTEMPTING"
            and age_seconds is not None
            and age_seconds >= stale_threshold_seconds
        )
""",
        [PINS["unreadable"]],
    ),
    (
        DB,
        "M3 every ATTEMPTING row is stale, recent ones included",
        STALE_BLOCK,
        """        age_unknown = age_seconds is None
        is_stale = persisted_status == "ATTEMPTING"
""",
        [PINS["recent"]],
    ),
    (
        DB,
        "M4 the payload fallback is dropped from the timestamp lookup",
        '        attempted_at_str = r[3] or payload.get("attempted_at")',
        "        attempted_at_str = r[3]",
        [PINS["fallback"]],
    ),
    (
        DB,
        "M5 the reason for the failed read is not kept",
        '            age_error = f"{type(exc).__name__}: {exc}"',
        "            age_error = None",
        [PINS["unreadable"]],
    ),
    (
        CLI,
        "M6 the CLI prints a number again instead of unknown",
        "_metric_text(age_minutes) if age_minutes is None",
        'f"{age_minutes} min" if age_minutes is None',
        [PINS["cli"]],
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
    originals = {p: (REPO / p).read_bytes() for p in (DB, CLI)}
    before = {p: digest(REPO / p) for p in originals}

    print("=== baseline (pins must be green before mutating) ===")
    rc, summary = run_pins(sorted(PINS.values()))
    print(f"    all pins: rc={rc}  {summary}")
    if rc != 0:
        print("    REFUSING TO CONTINUE: the pins are not green on the unmutated tree.")
        return 1

    caught = 0
    for target, name, old, new, expected in MUTATIONS:
        original = originals[target]
        text = original.decode("utf-8")
        print(f"--- {name}")
        if text.count(old) != 1:
            print(f"    SKIPPED: anchor occurs {text.count(old)} times in {target}")
            continue
        path = REPO / target
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            rc, summary = run_pins(expected)
        finally:
            path.write_bytes(original)

        ok = rc != 0
        caught += 1 if ok else 0
        print(f"    target: {target}   expected red: {len(expected)} pin(s)")
        print(f"    {'CAUGHT' if ok else 'SURVIVED'}: rc={rc}  {summary}")

    print()
    print("=== integrity ===")
    clean = True
    for p, original in originals.items():
        now = (REPO / p).read_bytes()
        ok = now == original
        clean = clean and ok
        print(f"    {p}: {'restored' if ok else 'CHANGED'} ({before[p]} -> {digest(REPO / p)})")

    print()
    print("=== summary ===")
    print(f"    mutations caught : {caught} of {len(MUTATIONS)}")
    print(f"    tree restored    : {'yes' if clean else 'NO - inspect before committing'}")
    return 0 if caught == len(MUTATIONS) and clean else 1


if __name__ == "__main__":
    sys.exit(main())
