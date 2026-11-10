<#
.SYNOPSIS
    Safely remove a git worktree and clean up its resources.

.DESCRIPTION
    Removes a git worktree by path or branch name:
    - Protects the main repository from accidental removal
    - Safely removes junctions/symlinks if shared .venv was used
    - Executes 'git worktree remove'
    - Executes 'git worktree prune'
    - Optionally deletes the branch if -DeleteBranch is passed.

.PARAMETER Target
    Path to worktree or branch name.

.PARAMETER Force
    Force removal even with untracked or modified files (-f).

.PARAMETER DeleteBranch
    Delete the git branch after worktree is removed (-D).

.EXAMPLE
    .\scripts\remove_worktree.ps1 -Target "t728head" -Force
    .\scripts\remove_worktree.ps1 -Target "feature/my-branch" -DeleteBranch
#>

[CmdletBinding()]
param(
    [Parameter(Position=0, Mandatory=$true)]
    [string]$Target,

    [Alias("f")]
    [switch]$Force,

    [Alias("D")]
    [switch]$DeleteBranch
)

$ErrorActionPreference = "Stop"

$repoRoot = (git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repoRoot)) {
    Write-Host "[ERROR] Current directory is not a git repository." -ForegroundColor Red
    exit 1
}
$repoRoot = (Resolve-Path $repoRoot).Path

# Get worktree list
$rawList = git worktree list --porcelain
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
    } elseif ($line -match '^branch\s+(.+)$') {
        $currentEntry["Branch"] = ($Matches[1].Trim() -replace '^refs/heads/', '')
    }
}
if ($currentEntry.Count -gt 0) {
    $worktrees += [PSCustomObject]$currentEntry
}

if ($worktrees.Count -eq 0) {
    Write-Host "[ERROR] No worktrees found." -ForegroundColor Red
    exit 1
}

$mainWtPath = $worktrees[0].Worktree

# Find matching worktree
$matchedWt = $null
foreach ($wt in $worktrees) {
    $normTarget = $Target.Replace('/', '\').TrimEnd('\')
    $normWtPath = $wt.Worktree.Replace('/', '\').TrimEnd('\')

    if ($normWtPath -like "*\$normTarget" -or $normWtPath -eq $normTarget) {
        $matchedWt = $wt
        break
    }
    if ($wt.Branch -and $wt.Branch -eq $Target) {
        $matchedWt = $wt
        break
    }
}

if (-not $matchedWt) {
    Write-Host "[ERROR] Worktree matching '$Target' not found in git worktree list!" -ForegroundColor Red
    exit 1
}

$targetPath = $matchedWt.Worktree
$branchName = $matchedWt.Branch

if ($targetPath.Replace('/', '\').TrimEnd('\') -eq $mainWtPath.Replace('/', '\').TrimEnd('\')) {
    Write-Host "[ERROR] Cannot remove main repository worktree: $targetPath" -ForegroundColor Red
    exit 1
}

Write-Host "==========================================================" -ForegroundColor Magenta
Write-Host "  Removing Git Worktree: $targetPath                      " -ForegroundColor Magenta
Write-Host "==========================================================" -ForegroundColor Magenta

# Check if .venv is junction, remove junction first to prevent recursing deletion into main repo
$venvPath = Join-Path $targetPath ".venv"
if (Test-Path $venvPath) {
    $item = Get-Item $venvPath -Force
    if ($item.LinkType -eq "Junction" -or $item.Attributes -match "ReparsePoint") {
        Write-Host "Removing .venv junction link..." -ForegroundColor Yellow
        $item.Delete()
    }
}

$removeArgs = @("worktree", "remove", $targetPath)
if ($Force) {
    $removeArgs += "--force"
}

Write-Host "Running: git $($removeArgs -join ' ')" -ForegroundColor DarkGray
& git @removeArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] git worktree remove failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Pruning worktrees: git worktree prune" -ForegroundColor DarkGray
& git worktree prune

if ($DeleteBranch -and $branchName) {
    Write-Host "Deleting branch: git branch -D $branchName" -ForegroundColor Yellow
    & git branch -D $branchName
}

Write-Host "`n[OK] Worktree successfully removed: $targetPath" -ForegroundColor Green
