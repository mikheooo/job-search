"""BLE001 finding #44 probe: is the candidate-profile fallback still silent?

The defect this measures: `load_candidate_profile()` never raises. When no
profile file is found it returns a hard-coded default that carries no name,
email or phone, and the old code did that with no log record at all -- so forms
get prepared with empty contact details and nothing anywhere says why.

This probe pins the before/after in the only way that counts: it loads the
module from two states (the committed HEAD version and the working tree) into a
throwaway directory where no profile can be discovered, calls
`load_candidate_profile()` with no arguments, and counts WARNING records.

    HEAD  -> 0 warnings  (silent fallback)
    WORK  -> 1 warning   (visible fallback)

It also reports how many fields the fallback profile loses relative to the real
one, so the number in the report is reproducible rather than remembered.

Exit codes: 0 = the working tree behaves as documented, 1 = it does not,
2 = the probe could not look (missing path, empty selection) -- a measurer that
reports "fine" when it could not look is the exact defect this sweep hunts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE_REL = "ai_assistant/candidate_profile.py"

# The field-loss comparison imports the package, and `tools/` is not a package
# root, so make the repo importable from here.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Runs in a child process, cwd = an empty scratch dir, no CANDIDATE_PROFILE* in
# the environment, so the default search locations cannot resolve.
CHILD = r'''
import importlib.util, json, logging, sys

spec = importlib.util.spec_from_file_location("cp_probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
# dataclass() looks the class's module up in sys.modules while processing the
# class body, so register it before exec_module or it raises AttributeError on
# NoneType.__dict__.
sys.modules["cp_probe"] = mod
spec.loader.exec_module(mod)

records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


root = logging.getLogger()
root.addHandler(Capture())
root.setLevel(logging.DEBUG)

profile = mod.load_candidate_profile()

print(json.dumps({
    "warnings": [r.getMessage() for r in records if r.levelno >= logging.WARNING],
    "info_or_worse": [r.getMessage() for r in records if r.levelno >= logging.INFO],
    "fields": vars(profile),
}, default=str))
'''


def _state_source(state: str) -> str:
    """Return the source of the module in the requested state, or exit 2."""
    if state == "WORK":
        path = REPO / MODULE_REL
        if not path.is_file():
            print(f"!! {path} does not exist", file=sys.stderr)
            sys.exit(2)
        return path.read_text(encoding="utf-8")
    if state == "HEAD":
        out = subprocess.run(
            ["git", "show", f"HEAD:{MODULE_REL}"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0 or not out.stdout.strip():
            # Empty input makes ruff say "clean" and makes this probe report a
            # silent fallback for the wrong reason. Refuse instead.
            print(f"!! could not read HEAD:{MODULE_REL}: {out.stderr.strip()}", file=sys.stderr)
            sys.exit(2)
        return out.stdout
    print(f"!! unknown state {state!r}", file=sys.stderr)
    sys.exit(2)


def measure(state: str) -> dict:
    scratch = REPO / "_tmp_profile_probe" / state
    scratch.mkdir(parents=True, exist_ok=True)
    module_path = scratch / "candidate_profile.py"
    module_path.write_text(_state_source(state), encoding="utf-8", newline="")

    empty_cwd = REPO / "_tmp_profile_probe" / "empty_cwd"
    empty_cwd.mkdir(parents=True, exist_ok=True)

    env = {
        k: v
        for k, v in __import__("os").environ.items()
        if k not in ("CANDIDATE_PROFILE", "CANDIDATE_PROFILE_FILE")
    }

    out = subprocess.run(
        [sys.executable, "-c", CHILD, str(module_path)],
        cwd=empty_cwd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if out.returncode != 0:
        print(f"!! child failed for {state}: {out.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return json.loads(out.stdout.strip().splitlines()[-1])


def real_profile_field_loss(fallback_fields: dict) -> list[str]:
    """Fields the real profile fills in and the fallback leaves empty.

    The fallback is loaded from a scratch dir; the real profile is loaded from
    the repo, where `candidate_profile.json` is discoverable. Both are dataclass
    instances, so `vars()` gives the comparable shape.
    """
    from ai_assistant.candidate_profile import load_candidate_profile

    real = vars(load_candidate_profile())
    empty = (None, "", [], {})
    lost = []
    for field, value in real.items():
        if value in empty:
            continue
        if fallback_fields.get(field) in empty:
            lost.append(field)
    return lost


def main() -> int:
    head = measure("HEAD")
    work = measure("WORK")
    lost = real_profile_field_loss(work["fields"])

    print("=== state HEAD (committed) ===")
    print(f"  warnings emitted : {len(head['warnings'])}")
    print("=== state WORK (working tree) ===")
    print(f"  warnings emitted : {len(work['warnings'])}")
    for w in work["warnings"]:
        print(f"    {w}")

    print("=== why the fallback matters ===")
    print(f"  fields the real profile fills and the fallback leaves empty: {len(lost)}")
    print(f"    {lost}")

    ok = True
    if head["warnings"]:
        print("!! HEAD already warned -- the baseline is not what the report claims", file=sys.stderr)
        ok = False
    if not work["warnings"]:
        print("!! WORK did not warn -- the fallback is still silent", file=sys.stderr)
        ok = False
    if work["fields"].get("name") is not None or work["fields"].get("email") is not None:
        print("!! WORK returned a profile with contact data -- probe did not hit the fallback", file=sys.stderr)
        ok = False
    if not lost:
        print("!! fallback loses no fields -- the finding's premise does not hold", file=sys.stderr)
        ok = False

    print("VERDICT:", "PASS" if ok else "FAIL")

    scratch = REPO / "_tmp_profile_probe"
    if "--keep" in sys.argv:
        print(f"scratch kept at {scratch}")
    else:
        shutil.rmtree(scratch, ignore_errors=True)
        print("scratch removed (pass --keep to inspect the loaded copies)")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
