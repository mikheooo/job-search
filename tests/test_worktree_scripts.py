"""Tests for Git worktree management scripts."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_worktree_script_files_exist():
    """Verify all worktree management scripts are present in scripts/."""
    expected_scripts = [
        "scripts/worktree.ps1",
        "scripts/worktree.bat",
        "scripts/setup_worktree.ps1",
        "scripts/new_worktree.ps1",
        "scripts/remove_worktree.ps1",
        "scripts/list_worktrees.ps1",
        "scripts/register_worktree_git_aliases.ps1",
    ]
    for rel_path in expected_scripts:
        script_path = REPO_ROOT / rel_path
        assert script_path.exists(), f"Missing expected script: {rel_path}"
        assert script_path.stat().st_size > 0, f"Script is empty: {rel_path}"


def test_gitignore_contains_worktrees():
    """Verify .worktrees/ directory is ignored in .gitignore."""
    gitignore = REPO_ROOT / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text(encoding="utf-8")
    assert ".worktrees/" in content


def test_powershell_scripts_syntax():
    """Verify that all PowerShell scripts parse without syntax errors."""
    if sys.platform != "win32":
        return

    ps_files = list((REPO_ROOT / "scripts").glob("*worktree*.ps1"))
    assert len(ps_files) >= 5

    for ps_file in ps_files:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"[System.Management.Automation.Language.Parser]::ParseFile('{ps_file}', [ref]$null, [ref]$null) | Out-Null; if (-not $?) {{ exit 1 }}",
        ]
        res = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert res.returncode == 0, f"Syntax error in {ps_file.name}: {res.stderr}"


def test_worktree_help_command():
    """Verify worktree help command runs and shows usage information."""
    if sys.platform != "win32":
        return

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "scripts" / "worktree.ps1"),
        "help",
    ]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0
    assert "Job-Search Worktree Management Utility" in res.stdout
    assert "new" in res.stdout
    assert "setup" in res.stdout
    assert "list" in res.stdout
    assert "remove" in res.stdout


def test_worktree_list_command():
    """Verify worktree list command runs and parses git worktree table."""
    if sys.platform != "win32":
        return

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "scripts" / "worktree.ps1"),
        "list",
    ]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0
    assert "Git Worktrees & Environment Status" in res.stdout
    assert "Branch" in res.stdout
    assert "Venv" in res.stdout
    assert "Status" in res.stdout


def test_paseo_config_worktree_setup():
    """Verify paseo.json exists and defines worktree.setup command."""
    import json

    paseo_file = REPO_ROOT / "paseo.json"
    assert paseo_file.exists(), "paseo.json must exist in repository root"

    with open(paseo_file, encoding="utf-8") as f:
        data = json.load(f)

    assert "worktree" in data, "paseo.json must contain 'worktree' configuration"
    assert "setup" in data["worktree"], "worktree section must contain 'setup' commands"

    setup_cmds = data["worktree"]["setup"]
    assert isinstance(setup_cmds, (list, str)), "worktree.setup must be list or string"
    if isinstance(setup_cmds, list):
        assert len(setup_cmds) > 0, "worktree.setup must not be empty"
        for cmd in setup_cmds:
            assert "setup_worktree" in cmd, f"Setup command should run setup_worktree: {cmd}"

