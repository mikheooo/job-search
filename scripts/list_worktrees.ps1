<#
.SYNOPSIS
    Display list of git worktrees with environment and configuration status.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = (git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repoRoot)) {
    Write-Host "[ERROR] Current directory is not a git repository." -ForegroundColor Red
    exit 1
}

$rawList = git worktree list --porcelain
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to run 'git worktree list --porcelain'" -ForegroundColor Red
    exit $LASTEXITCODE
}

$worktrees = @()
$currentEntry = @{}

foreach ($line in $rawList) {
    if ([string]::IsNullOrWhiteSpace($line)) {
        if ($currentEntry.Count -gt 0) {
            $worktrees += [PSCustomObject]$currentEntry
            $currentEntry = @{}
        }
        continue
    }

    if ($line -match '^worktree\s+(.+)$') {
        $currentEntry["Worktree"] = $Matches[1].Trim()
    } elseif ($line -match '^HEAD\s+(.+)$') {
        $currentEntry["HEAD"] = $Matches[1].Trim()
    } elseif ($line -match '^branch\s+(.+)$') {
        $currentEntry["Branch"] = ($Matches[1].Trim() -replace '^refs/heads/', '')
    } elseif ($line -match '^bare$') {
        $currentEntry["Bare"] = $true
    } elseif ($line -match '^detached$') {
        $currentEntry["Detached"] = $true
    }
}
if ($currentEntry.Count -gt 0) {
    $worktrees += [PSCustomObject]$currentEntry
}

Write-Host "==========================================================" -ForegroundColor Magenta
Write-Host "  Git Worktrees & Environment Status                      " -ForegroundColor Magenta
Write-Host "==========================================================" -ForegroundColor Magenta

$results = @()
$index = 1

foreach ($wt in $worktrees) {
    $wtPath = $wt.Worktree
    $branchName = "N/A"
    if ($wt.Branch) {
        $branchName = $wt.Branch
    } elseif ($wt.Detached) {
        $shortHead = if ($wt.HEAD.Length -ge 7) { $wt.HEAD.Substring(0, 7) } else { $wt.HEAD }
        $branchName = "(detached: $shortHead)"
    }

    $venvPath = Join-Path $wtPath ".venv"
    $venvStatus = "Missing"
    $hasVenv = $false

    if (Test-Path $venvPath) {
        $item = Get-Item $venvPath -Force
        if ($item.LinkType -eq "Junction" -or $item.Attributes -match "ReparsePoint") {
            $venvStatus = "Junction (Shared)"
            $hasVenv = $true
        } elseif (Test-Path (Join-Path $venvPath "Scripts\python.exe")) {
            $venvStatus = "Isolated (.venv)"
            $hasVenv = $true
        } else {
            $venvStatus = "Corrupted"
        }
    }

    $hasProfile = Test-Path (Join-Path $wtPath "candidate_profile.json")
    $hasAiEnv = Test-Path (Join-Path $wtPath "ai_assistant\.env")
    $hasRootEnv = Test-Path (Join-Path $wtPath ".env")
    $envPresent = ($hasAiEnv -or $hasRootEnv)

    $isMain = ($index -eq 1)
    $status = if ($isMain) {
        "Main Repository"
    } elseif ($hasVenv -and $hasProfile) {
        "Ready"
    } else {
        "Needs Setup"
    }

    $results += [PSCustomObject]@{
        "#" = $index
        "Branch" = $branchName
        "Venv" = $venvStatus
        "Profile" = $(if ($hasProfile) { "Yes" } else { "No" })
        "Env" = $(if ($envPresent) { "Yes" } else { "No" })
        "Status" = $status
        "Path" = $wtPath
    }
    $index++
}

$results | Format-Table -AutoSize
Write-Host "Total worktrees: $($results.Count)`n" -ForegroundColor DarkGray
