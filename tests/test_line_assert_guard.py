"""Guard against fragile line-number and string-slicing assertions in tests.

Refactoring source modules (e.g. cli.py, runner.py) must not be blocked by tests
that assert hardcoded line numbers or split python source files with naive string
operations instead of AST parsing.
"""
import ast
import re
from pathlib import Path

TESTS_DIR = Path(__file__).parent


def test_no_hardcoded_lineno_equality_assertions():
    """Ensure no test asserts exact line numbers (e.g. node.lineno == 123) on source files."""
    violations = []

    for test_file in TESTS_DIR.glob("*.py"):
        if test_file.name in ("test_line_assert_guard.py", "test_report_line_refs.py"):
            continue
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                # Check for `node.lineno == <number>`
                for op, comparator in zip(node.ops, node.comparators):
                    if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant) and isinstance(comparator.value, int):
                        left = node.left
                        if isinstance(left, ast.Attribute) and left.attr == "lineno":
                            violations.append(f"{test_file.name}:{node.lineno}: {ast.unparse(node)}")

    assert not violations, f"Found fragile exact lineno equality assertions in tests:\n" + "\n".join(violations)


def test_no_naive_source_slicing_on_cli_py():
    """Ensure no test slices cli.py with fragile .split('def ...') patterns."""
    violations = []
    pattern = re.compile(r'\.split\(\s*["\']def\s+')

    for test_file in TESTS_DIR.glob("*.py"):
        if test_file.name == "test_line_assert_guard.py":
            continue
        content = test_file.read_text(encoding="utf-8")
        if "cli" in content and pattern.search(content):
            violations.append(test_file.name)

    assert not violations, (
        f"Found naive string slicing on cli.py (use AST parsing instead):\n" + "\n".join(violations)
    )
