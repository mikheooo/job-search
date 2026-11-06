"""Low hardening: dependency pins, one Telegram poller, no production tools/.t* imports."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (
    ROOT / "ai_assistant",
    ROOT / "scripts",
    ROOT / "integrations",
)


def _python_files(root: Path):
    return [p for p in root.rglob("*.py") if p.is_file()]


def _docstring_nodes(tree: ast.AST) -> set[ast.AST]:
    nodes: set[ast.AST] = set()

    def consider(body: list[ast.stmt]) -> None:
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            nodes.add(body[0].value)

    if isinstance(tree, ast.Module):
        consider(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            consider(node.body)
    return nodes


def test_production_does_not_import_tools_scratch():
    """No production module loads tools/.t* by import or by a path string.

    Historical mentions inside docstrings are not imports.
    """
    offenders = []
    for root in PRODUCTION_ROOTS:
        if not root.exists():
            continue
        for path in _python_files(root):
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                offenders.append(f"{path.relative_to(ROOT)}: syntax")
                continue
            docs = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tools"):
                    offenders.append(f"{path.relative_to(ROOT)}: import {node.module}")
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if node in docs:
                    continue
                value = node.value.replace("\\", "/")
                if node not in docs and (
                    ("tools" in value and "/.t" in value)
                    or (value.startswith(".t") and value[2:3].isdigit())
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{value[:80]}")
    assert offenders == []


def test_brief_js_lives_in_the_package():
    from ai_assistant.hh_cdp_submit import BRIEF_JS, _window_snapshot

    assert "__VID__" in BRIEF_JS
    assert "vacancyView" in BRIEF_JS
    snap = _window_snapshot("t722_brief_before")
    assert snap["label"] == "t722_brief_before"
    assert snap["source"] == "package"


def test_telegram_bot_is_not_a_poller():
    from ai_assistant import telegram_bot

    control = (ROOT / "ai_assistant" / "telegram_control_bot.py").read_text(encoding="utf-8")
    assert "def run_bot_polling" in control
    assert 'if __name__ == "__main__":' in control
    assert telegram_bot.main() == 2

    proc = subprocess.run(
        [sys.executable, "-m", "ai_assistant.telegram_bot"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "telegram_control_bot" in proc.stderr


def test_requirements_pin_majors_and_declare_extras():
    core = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    browser = (ROOT / "requirements-browser.txt").read_text(encoding="utf-8")
    gmail = (ROOT / "requirements-gmail.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in ("pydantic>=2,<3", "requests>=2,<3", "openai>=1,<4", "fastapi>=0.110,<1"):
        assert name in core, name
    assert "playwright>=1,<2" in browser
    assert "google-auth>=2,<3" in gmail
    assert "google-api-python-client>=2,<3" in gmail
    assert 'browser = ["playwright>=1,<2"]' in pyproject
    assert "gmail" in pyproject
