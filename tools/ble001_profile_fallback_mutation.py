"""Mutation check for the finding-#44 pins in tests/test_ble001_fail_closed.py.

A pin that cannot fail is decoration. Four mutations, each must be caught by the
guard it targets:

  M1  delete the logger.warning() from candidate_profile.load_candidate_profile()
      -> the fallback is silent again. Expect the "said nothing" assertion.

  M2  append a module-level helper that calls load_candidate_profile() directly
      -> a second owner of the profile load. Expect the "lives outside
      _load_profile()" assertion.

  M3  delete one `profile = _load_profile(profile_path)` call site
      -> the census drops from three to two. Expect the "expected three call
      sites" assertion. Different assertion, different branch of the same test.

  M4  drop load_candidate_profile from the module-level import in
      browser_executor.py -> ruff F821. This is not a pytest pin: it is the
      exact defect that shipped for one edit earlier today (four F821s), caught
      by the ruff parity check rather than by any test. Pinned here because
      "the tests are green" was never the thing that caught it.

Every mutation is applied to a byte copy, verified by md5, and restored. A
mutation that cannot be undone is not an experiment, it is a change.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"
RUFF = Path.home() / ".workbuddy-ai" / "binaries" / "python" / "envs" / "default" / "Scripts" / "ruff.exe"
PROFILE_MOD = REPO / "ai_assistant" / "candidate_profile.py"
EXECUTOR = REPO / "ai_assistant" / "browser_executor.py"
TESTFILE = REPO / "tests" / "test_ble001_fail_closed.py"

WARNING_BLOCK = (
    b'    logger.warning(\n'
    b'        "candidate profile not found: no explicit path, no CANDIDATE_PROFILE/"\n'
    b'        "CANDIDATE_PROFILE_FILE in the environment, and none of %s exists. Using the "\n'
    b'        "built-in default profile, which has no name, email or phone.",\n'
    b'        ", ".join(str(p) for p in DEFAULT_PROFILE_PATHS),\n'
    b'    )\n'
)

OUTSIDE_CALL = (
    b"\n\ndef _ble001_mutation_second_owner():\n"
    b"    return load_candidate_profile()\n"
)

CALL_SITE = b"    profile = _load_profile(profile_path)\n"

MODULE_IMPORT = b"from .candidate_profile import CandidateProfile, load_candidate_profile\n"
MODULE_IMPORT_BROKEN = b"from .candidate_profile import CandidateProfile\n"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    """Write and verify, with retries.

    Measured 2026-09-17: the first run of this tool died on
    `OSError: [Errno 22] Invalid argument` while rewriting browser_executor.py
    right after a pytest subprocess had imported it -- and the same error then
    hit the `finally` restore, so the tool could neither mutate nor put things
    back. A mutator whose restore can fail silently is worse than no mutator:
    it leaves the tree in a state nobody measured. Retry, then verify by bytes,
    then give up loudly.
    """
    last: OSError | None = None
    for attempt in range(6):
        try:
            path.write_bytes(data)
            if path.read_bytes() == data:
                return
            last = OSError("write reported success but the bytes differ")
        except OSError as exc:
            last = exc
        time.sleep(0.4 * (attempt + 1))
    raise SystemExit(f"!! could not write {path} after 6 attempts: {last}")


def run_test(expr: str) -> tuple[int, str]:
    proc = subprocess.run(
        [
            str(PY), "-m", "pytest", "tests/test_ble001_fail_closed.py",
            "-q", "--no-header", "-p", "no:cacheprovider", "-k", expr,
        ],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")


def run_ruff(path: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [str(RUFF), "check", str(path.relative_to(REPO)), "--select", "F821",
         "--output-format=concise", "--no-cache"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")


def report(label: str, caught: bool, detail: str, tail: str) -> bool:
    print(f"\n--- {label} ---")
    print(f"    {detail}")
    print(f"    VERDICT : {'CAUGHT' if caught else 'SURVIVED (the pin is blind)'}")
    for line in [ln for ln in tail.splitlines() if ln.strip()][-3:]:
        print(f"      | {line[:150]}")
    return caught


def main() -> int:
    originals = {p: p.read_bytes() for p in (PROFILE_MOD, EXECUTOR, TESTFILE)}
    hashes = {p: md5(b) for p, b in originals.items()}
    results: list[bool] = []

    try:
        # --- M1: the fallback goes silent again ---
        assert WARNING_BLOCK in originals[PROFILE_MOD], "M1 anchor did not match"
        write_bytes(PROFILE_MOD, originals[PROFILE_MOD].replace(
            WARNING_BLOCK, b"    pass  # M1: warning removed\n", 1))
        rc, out = run_test("missing_profile_is_reported")
        results.append(report(
            "M1  warning removed from the fallback", rc != 0 and "said nothing" in out,
            f"exit {rc}, 'said nothing' in output -> {'said nothing' in out}", out))
        write_bytes(PROFILE_MOD, originals[PROFILE_MOD])

        # --- M2: a second owner of the profile load ---
        write_bytes(EXECUTOR, originals[EXECUTOR] + OUTSIDE_CALL)
        rc, out = run_test("loads_the_profile_in_exactly_one_place")
        results.append(report(
            "M2  extra load_candidate_profile() call outside the helper",
            rc != 0 and "lives outside _load_profile()" in out,
            f"exit {rc}, 'lives outside' in output -> {'lives outside _load_profile()' in out}",
            out))
        write_bytes(EXECUTOR, originals[EXECUTOR])

        # --- M3: one call site disappears ---
        mutated = originals[EXECUTOR].replace(CALL_SITE, b"", 1)
        assert mutated != originals[EXECUTOR], "M3 anchor did not match"
        write_bytes(EXECUTOR, mutated)
        rc, out = run_test("loads_the_profile_in_exactly_one_place")
        results.append(report(
            "M3  one of the three call sites deleted", rc != 0 and "three call sites" in out,
            f"exit {rc}, 'three call sites' in output -> {'three call sites' in out}", out))
        write_bytes(EXECUTOR, originals[EXECUTOR])

        # --- M4: the F821 that ruff caught and no test would have ---
        assert MODULE_IMPORT in originals[EXECUTOR], "M4 anchor did not match"
        write_bytes(EXECUTOR, originals[EXECUTOR].replace(MODULE_IMPORT, MODULE_IMPORT_BROKEN, 1))
        rc, out = run_ruff(EXECUTOR)
        results.append(report(
            "M4  module-level import dropped (the shipped F821 bug)",
            rc != 0 and "F821" in out,
            f"exit {rc}, F821 in output -> {'F821' in out}", out))
        write_bytes(EXECUTOR, originals[EXECUTOR])
    finally:
        for path, data in originals.items():
            write_bytes(path, data)

    print("\n=== restore ===")
    restored = True
    for path, digest in hashes.items():
        now = md5(path.read_bytes())
        same = now == digest
        restored = restored and same
        print(f"    {path.name}: md5 {now} (was {digest}) -> {same}")
    if not restored:
        print("    !! RESTORE FAILED")
        return 2

    print("\n=== sanity: the pins are green again on the restored tree ===")
    rc, out = run_test("profile or built_in_default")
    print(f"    exit code {rc}, {'green' if rc == 0 else 'NOT GREEN'}")
    results.append(rc == 0)

    print("\n=== summary ===")
    print(f"    mutations caught : {sum(results[:4])} of 4")
    print(f"    tree restored    : {'yes' if restored else 'NO'}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
