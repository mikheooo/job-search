"""Tests for CFG-002: Environment variable inventory & linter test.

Verifies that:
1. Every application environment variable referenced in ai_assistant/ and scripts/
   is documented in .env.example.
2. Every environment variable in .env.example is documented in docs/environment_variables.md.
3. All critical safety variables (OPERATOR_SAFETY_VARS) are tracked and categorized.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ai_assistant import config

REPO_ROOT = Path(__file__).resolve().parents[1]

# OS and runtime variables that are system-managed rather than application config
SYSTEM_EXCLUDES = {
    "COMPUTERNAME",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PYTEST_CURRENT_TEST",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
}


def _get_example_env_keys() -> set[str]:
    """Parse all variable names from .env.example."""
    example_file = REPO_ROOT / ".env.example"
    assert example_file.is_file(), ".env.example is missing"

    keys = set()
    for line in example_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^#?\s*([A-Z0-9_]+)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def _get_docs_env_keys() -> set[str]:
    """Parse all documented variable names from docs/environment_variables.md."""
    doc_file = REPO_ROOT / "docs" / "environment_variables.md"
    assert doc_file.is_file(), "docs/environment_variables.md is missing"

    keys = set()
    for line in doc_file.read_text(encoding="utf-8").splitlines():
        # Match table rows: | `VAR_NAME` | ...
        m = re.match(r"^\|\s*`([A-Z0-9_]+)`\s*\|", line)
        if m:
            keys.add(m.group(1))
    return keys


def _discover_codebase_env_vars() -> dict[str, list[str]]:
    """AST scan of ai_assistant/ and scripts/ for environment lookups."""
    discovered: dict[str, list[str]] = {}

    target_dirs = [REPO_ROOT / "ai_assistant", REPO_ROOT / "scripts"]
    for target_dir in target_dirs:
        for py_path in target_dir.rglob("*.py"):
            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue

            rel_path = str(py_path.relative_to(REPO_ROOT)).replace("\\", "/")

            for node in ast.walk(tree):
                # os.getenv('KEY') or os.environ.get('KEY')
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr in ("getenv", "get"):
                        val = node.func.value
                        is_env = (
                            isinstance(val, ast.Name) and val.id == "os" and node.func.attr == "getenv"
                        ) or (isinstance(val, ast.Attribute) and val.attr == "environ")
                        if is_env and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                            key = node.args[0].value
                            if key not in SYSTEM_EXCLUDES and key.isidentifier() and key.isupper():
                                discovered.setdefault(key, []).append(f"{rel_path}:{node.lineno}")

                # os.environ['KEY']
                elif isinstance(node, ast.Subscript):
                    val = node.value
                    if isinstance(val, ast.Attribute) and val.attr == "environ":
                        slice_node = node.slice
                        if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
                            key = slice_node.value
                            if key not in SYSTEM_EXCLUDES and key.isidentifier() and key.isupper():
                                discovered.setdefault(key, []).append(f"{rel_path}:{node.lineno}")

    return discovered


def test_operator_safety_vars_are_in_example_and_docs():
    """All OPERATOR_SAFETY_VARS must be present in .env.example and docs."""
    example_keys = _get_example_env_keys()
    docs_keys = _get_docs_env_keys()

    for var in config.OPERATOR_SAFETY_VARS:
        assert var in example_keys, f"Safety variable {var} missing from .env.example"
        assert var in docs_keys, f"Safety variable {var} missing from docs/environment_variables.md"


def test_all_codebase_env_vars_documented_in_example():
    """Every os.getenv / os.environ call in the codebase must have an entry in .env.example."""
    discovered = _discover_codebase_env_vars()
    example_keys = _get_example_env_keys()

    undocumented = {k: v for k, v in discovered.items() if k not in example_keys}
    assert not undocumented, (
        f"Found {len(undocumented)} undocumented environment variables in codebase!\n"
        f"Variables: {sorted(undocumented.keys())}\n"
        f"Locations: {undocumented}"
    )


def test_all_example_vars_documented_in_markdown():
    """.env.example and docs/environment_variables.md must be fully synchronized."""
    example_keys = _get_example_env_keys()
    docs_keys = _get_docs_env_keys()

    missing_in_docs = example_keys - docs_keys
    assert not missing_in_docs, (
        f"Variables in .env.example missing from docs/environment_variables.md: {sorted(missing_in_docs)}"
    )
