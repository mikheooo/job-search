import os
import sys

wt = os.path.abspath(os.path.dirname(__file__))
venv_pkgs = os.path.join(wt, ".venv", "Lib", "site-packages")

# Insert worktree root and worktree site-packages at top of sys.path
if venv_pkgs not in sys.path:
    sys.path.insert(0, venv_pkgs)
if wt not in sys.path:
    sys.path.insert(0, wt)

# Set PYTHONPATH in os.environ for child processes if any
os.environ["PYTHONPATH"] = f"{venv_pkgs};{wt}"
os.chdir(wt)

import pytest

args = sys.argv[1:] if len(sys.argv) > 1 else ["tests/test_p0_stabilization.py", "-v"]
print(f"Running pytest with args: {args}")
sys.exit(pytest.main(args))
