#!/usr/bin/env python
"""Mutation check for finding #51: does the narrower handler actually hold?

The fix in ``db.record_digest_attempt`` narrows one handler:

    except sqlite3.IntegrityError:      # was: except Exception:

Each mutation below puts a different plausible mistake back and asks the pins
whether they notice. Two mutations restore the old optimism, and two break the
CONTROLS - because a control that cannot fail is not a control.

Everything is restored byte-for-byte and the hashes are printed; the tool refuses
to report a verdict it could not restore.

Usage:  ./.venv/Scripts/python.exe tools/ble001_digest_writer_mutation.py
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_ble001_fail_closed.py"

DB = REPO / "ai_assistant" / "db.py"

# The anchor is the whole handler head, so a mutation cannot silently match the
# wrong `except` in a 2951-line file.
ANCHOR = "        except sqlite3.IntegrityError:\n"

PIN_WRITE_FAILURE = "test_a_write_failure_is_not_reported_as_another_worker"
PIN_REASON = "test_the_original_reason_survives_the_raise"
PIN_RACE = "test_the_unique_race_still_returns_none"
PIN_HEALTHY = "test_a_healthy_write_still_returns_a_batch_key"

MUTATIONS = [
    (
        "M1",
        "the exact pre-fix shape: `except Exception`",
        "        except Exception:\n",
        [PIN_WRITE_FAILURE, PIN_REASON],
    ),
    (
        "M2",
        "wide enough to swallow the database errors too",
        "        except (sqlite3.IntegrityError, sqlite3.OperationalError):\n",
        [PIN_WRITE_FAILURE, PIN_REASON],
    ),
    (
        "M3",
        "CONTROL CHECK: drop the race branch, so the race pin must fire",
        "        except sqlite3.OperationalError:\n",
        [PIN_RACE],
    ),
    (
        "M4",
        "CONTROL CHECK: never claim the batch, so the healthy pin must fire",
        None,  # sentinel: this mutation rewrites the final `return batch_key`
        [PIN_HEALTHY],
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pins(node_ids: list[str]) -> tuple[dict[str, str], str]:
    """Return {pin: PASS/FAIL} plus the raw tail, for one pytest run."""
    expr = " or ".join(node_ids)
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, "--no-header", "-rf"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    out = proc.stdout + proc.stderr
    verdicts: dict[str, str] = {}
    for pin in node_ids:
        # `-rf` prints "FAILED path::pin" for failures; anything else ran and passed.
        verdicts[pin] = "FAIL" if re.search(rf"^FAILED .*::{pin}\b", out, re.MULTILINE) else "PASS"
    return verdicts, out.strip().splitlines()[-1] if out.strip() else "<no output>"


def main() -> int:
    original = DB.read_bytes()
    before = sha(DB)
    if original.count(ANCHOR.encode()) != 1:
        print(f"!! anchor appears {original.count(ANCHOR.encode())} times, expected 1")
        return 2

    print(f"db.py sha256 before: {before}")
    print(f"anchor: {ANCHOR.strip()!r} (once)\n")
    header = f"{'mut':4s} {'expected to be caught by':34s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    failures = []
    try:
        for name, description, replacement, pins in MUTATIONS:
            if replacement is None:
                # M4 rewrites the tail of the function instead.
                tail = b"    conn.commit()\n    conn.close()\n    return batch_key\n"
                if original.count(tail) != 1:
                    print(f"{name:4s} !! tail appears {original.count(tail)} times, expected 1")
                    failures.append(name)
                    continue
                mutated = original.replace(tail, tail.replace(b"batch_key", b"None"))
            else:
                mutated = original.replace(ANCHOR.encode(), replacement.encode(), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                failures.append(name)
                continue

            DB.write_bytes(mutated)
            verdicts, _ = run_pins(pins)
            caught = [p for p in pins if verdicts[p] == "FAIL"]
            status = "caught" if caught else "SURVIVED"
            print(f"{name:4s} {','.join(pins)[:34]:34s} {status:9s} {description}")
            if not caught:
                failures.append(name)
    finally:
        DB.write_bytes(original)

    after = sha(DB)
    print()
    print(f"db.py sha256 after : {after}")
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
    raise SystemExit(main())
