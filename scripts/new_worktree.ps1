<#
.SYNOPSIS
    Create and automatically prepare a new git worktree for job-search.

.DESCRIPTION
    Creates a new worktree using 'git worktree add' and automatically calls
    scripts/setup_worktree.ps1 to create .venv, install dependencies, download
    Playwright Chromium, and copy candidate_profile.json and .env.

.PARAMETER Branch
    Branch name or commit ref.

.PARAMETER Path
    Destination path for the worktree. Defaults to "..\job-search-<branch>".

.PARAMETER CreateBranch
    Create a new branch with the specified name (-b alias).

.PARAMETER BaseRef
    Base ref for new branch (default: HEAD).

.PARAMETER Detach
    Check out commit in detached HEAD mode.

.PARAMETER SharedVenv
    Link parent .venv via fast NTFS Junction instead of isolated copy.

.PARAMETER UsePip
    Force standard pip instead of uv.

.PARAMETER SkipSetup
    Skip environment and dependency setup.

.PARAMETER SkipDev
    Skip requirements-dev.txt.

.PARAMETER SkipPlaywright
    Skip Playwright Chromium installation.

.PARAMETER SkipConfigCopy
    Do not copy candidate_profile.json and .env.

.PARAMETER RunTests
    Run smoke test after setup.
#>

[CmdletBinding()]
param(
    [Parameter(Position=0, Mandatory=$true)]
    [string]$Branch,

    [Parameter(Position=1)]
    [string]$Path = "",

    [Alias("b")]
    [switch]$CreateBranch,

    [string]$BaseRef = "HEAD",

    [switch]$Detach,
    [switch]$SharedVenv,
    [switch]$UsePip,
    [switch]$SkipSetup,
    [switch]$SkipDev,
    [switch]$SkipPlaywright,
    [switch]$SkipConfigCopy,
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Msg)
    Write-Host "`n==> $Msg" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Msg)
    Write-Host "[OK] $Msg" -ForegroundColor Green
}

function Write-ErrMsg {
    param([string]$Msg)
    Write-Host "[ERROR] $Msg" -ForegroundColor Red
}

# 1. Verify we are inside a git repository
$repoRoot = (git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repoRoot)) {
    Write-ErrMsg "Current directory is not a git repository."
    exit 1
}
$repoRoot = (Resolve-Path $repoRoot).Path

# 2. Determine target path
if ([string]::IsNullOrWhiteSpace($Path)) {
    $cleanBranchName = $Branch -replace '[\/\\:*?"<>|]', '-'
    $parentDir = Split-Path $repoRoot -Parent
    $targetPath = Join-Path $parentDir "job-search-$cleanBranchName"
} else {
    $targetPath = [System.IO.Path]::GetFullPath($Path)
}

if (Test-Path $targetPath) {
    Write-ErrMsg "Target directory '$targetPath' already exists."
    exit 1
}

Write-Host "==========================================================" -ForegroundColor Magenta
Write-Host "  Creating New Git Worktree: $Branch                      " -ForegroundColor Magenta
Write-Host "==========================================================" -ForegroundColor Magenta
Write-Host "Repository: $repoRoot" -ForegroundColor DarkGray
Write-Host "Target Path: $targetPath" -ForegroundColor White
Write-Host "Branch/Ref: $Branch (Create branch: $($CreateBranch.IsPresent))" -ForegroundColor White

# 3. Execute git worktree add
Write-Step "Executing 'git worktree add'..."
$gitArgs = @("worktree", "add")

if ($CreateBranch) {
    $gitArgs += @("-b", $Branch, $targetPath, $BaseRef)
} elseif ($Detach -or ($Branch -eq "HEAD")) {
    $gitArgs += @("--detach", $targetPath, $Branch)
} else {
    $gitArgs += @($targetPath, $Branch)
}

Write-Host "git $($gitArgs -join ' ')" -ForegroundColor DarkGray
& git @gitArgs
if ($LASTEXITCODE -ne 0) {
    Write-ErrMsg "git worktree add failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Success "Git worktree created at: $targetPath"

# 4. Invoke setup_worktree.ps1
if (-not $SkipSetup) {
    Write-Step "Starting automated dependency and environment setup..."
    $setupScript = Join-Path $PSScriptRoot "setup_worktree.ps1"
    
    $setupArgs = @{
        Path = $targetPath
        ParentRepo = $repoRoot
    }
    if ($SharedVenv) { $setupArgs["SharedVenv"] = $true }
    if ($UsePip) { $setupArgs["UsePip"] = $true }
    if ($SkipDev) { $setupArgs["SkipDev"] = $true }
    if ($SkipPlaywright) { $setupArgs["SkipPlaywright"] = $true }
    if ($SkipConfigCopy) { $setupArgs["SkipConfigCopy"] = $true }
    if ($RunTests) { $setupArgs["RunTests"] = $true }

    & $setupScript @setupArgs
    if ($LASTEXITCODE -ne 0) {
        Write-ErrMsg "Worktree setup encountered warnings or errors."
    }
} else {
    Write-Host "`nEnvironment setup skipped (-SkipSetup)." -ForegroundColor Yellow
    Write-Host "To set up later, run:" -ForegroundColor Yellow
    Write-Host "  .\scripts\setup_worktree.ps1 -Path `"$targetPath`"" -ForegroundColor White
}
