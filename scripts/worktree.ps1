<#
.SYNOPSIS
    Unified management CLI for Git worktrees in job-search.

.DESCRIPTION
    Provides subcommands to create, setup, list, and remove worktrees with automatic
    Python virtual environment setup, dependency installation, and config synchronization.
#>

$ErrorActionPreference = "Stop"

function Show-Help {
    Write-Host @"
========================================================================
  Job-Search Worktree Management Utility
========================================================================

Usage:
  .\scripts\worktree.ps1 <command> [options...]

Available Commands:
  new, add       Create a new worktree and automatically prepare it
                 Usage: .\scripts\worktree.ps1 new <branch> [-Path <path>] [-b] [-SharedVenv] [-SkipPlaywright] [-RunTests]

  setup, prepare Prepare an existing or current worktree (venv, deps, configs)
                 Usage: .\scripts\worktree.ps1 setup [-Path <path>] [-SharedVenv] [-SkipPlaywright] [-RunTests]

  list, ls       List all worktrees with their environment and readiness status
                 Usage: .\scripts\worktree.ps1 list

  remove, rm     Safely remove a worktree and clean up references
                 Usage: .\scripts\worktree.ps1 remove <branch-or-path> [-Force] [-DeleteBranch]

  help           Show this help message

Options:
  -b, -CreateBranch  Create a new git branch when creating worktree
  -SharedVenv        Use parent .venv via NTFS Junction for instant startup
  -UsePip            Force standard pip/venv instead of uv
  -SkipPlaywright    Skip 'playwright install chromium'
  -SkipDev           Skip requirements-dev.txt (core deps only)
  -RunTests          Run smoke test (pytest) after environment setup
  -Force             Force removal of worktree even if dirty
  -DeleteBranch      Delete branch when removing worktree

Examples:
  # Create a new branch and fully prepare isolated worktree:
  .\scripts\worktree.ps1 new feature/remote-adapter -b

  # Fast lightweight worktree using shared parent .venv:
  .\scripts\worktree.ps1 new test-head -SharedVenv

  # Prepare environment in current directory / worktree:
  .\scripts\worktree.ps1 setup

  # List all worktrees and see which ones need setup:
  .\scripts\worktree.ps1 list

  # Remove worktree when done:
  .\scripts\worktree.ps1 remove feature/remote-adapter -DeleteBranch
========================================================================
"@ -ForegroundColor Cyan
}

if ($args.Count -eq 0) {
    Show-Help
    exit 0
}

$Command = [string]$args[0]
$restArgs = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

function Invoke-SubScript {
    param([string]$ScriptPath, [array]$ArgsList)
    if ($ArgsList -and $ArgsList.Count -gt 0) {
        & $ScriptPath @ArgsList
    } else {
        & $ScriptPath
    }
}

switch ($Command.ToLowerInvariant()) {
    { $_ -in @("new", "add") } {
        Invoke-SubScript "$PSScriptRoot\new_worktree.ps1" $restArgs
    }
    { $_ -in @("setup", "prepare") } {
        Invoke-SubScript "$PSScriptRoot\setup_worktree.ps1" $restArgs
    }
    { $_ -in @("list", "ls") } {
        Invoke-SubScript "$PSScriptRoot\list_worktrees.ps1" $restArgs
    }
    { $_ -in @("remove", "rm") } {
        Invoke-SubScript "$PSScriptRoot\remove_worktree.ps1" $restArgs
    }
    { $_ -in @("help", "-h", "--help", "/?") } {
        Show-Help
    }
    default {
        Write-Host "[ERROR] Unknown command '$Command'`n" -ForegroundColor Red
        Show-Help
        exit 1
    }
}
