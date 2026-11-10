<#
.SYNOPSIS
    Automated environment setup and dependency installer for job-search worktrees.

.DESCRIPTION
    Prepares a clean or existing git worktree:
    1. Sets up Python virtual environment (.venv) via uv (blazing fast) or python -m venv.
       Supports -SharedVenv (NTFS Junction to parent .venv for instant setup).
    2. Installs requirements.txt and requirements-dev.txt.
    3. Installs Playwright Chromium browser (unless -SkipPlaywright).
    4. Synchronizes local configurations (candidate_profile.json, .env, ai_assistant\.env).
    5. Performs sanity check and optional smoke test.
#>

[CmdletBinding()]
param(
    [string]$Path = "",
    [string]$ParentRepo = "",
    [switch]$SharedVenv,
    [switch]$UsePip,
    [string]$PythonVersion = "3.11",
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

function Write-WarnMsg {
    param([string]$Msg)
    Write-Host "[WARN] $Msg" -ForegroundColor Yellow
}

function Write-ErrMsg {
    param([string]$Msg)
    Write-Host "[ERROR] $Msg" -ForegroundColor Red
}

# 1. Resolve worktree target path
if ([string]::IsNullOrWhiteSpace($Path)) {
    try {
        $gitTopLevel = (git rev-parse --show-toplevel 2>$null)
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($gitTopLevel)) {
            $targetDir = (Resolve-Path $gitTopLevel).Path
        } else {
            $targetDir = (Get-Item -Path ".").FullName
        }
    } catch {
        $targetDir = (Get-Item -Path ".").FullName
    }
} else {
    if (-not (Test-Path $Path)) {
        Write-ErrMsg "Worktree directory '$Path' does not exist."
        exit 1
    }
    $targetDir = (Resolve-Path $Path).Path
}

Write-Host "==========================================================" -ForegroundColor Magenta
Write-Host "  Job-Search Worktree Setup & Dependency Installer        " -ForegroundColor Magenta
Write-Host "==========================================================" -ForegroundColor Magenta
Write-Host "Target worktree: $targetDir" -ForegroundColor White

# 2. Resolve parent / main repository
if ([string]::IsNullOrWhiteSpace($ParentRepo)) {
    if (-not [string]::IsNullOrWhiteSpace($env:PASEO_SOURCE_CHECKOUT_PATH) -and (Test-Path $env:PASEO_SOURCE_CHECKOUT_PATH)) {
        $ParentRepo = (Resolve-Path $env:PASEO_SOURCE_CHECKOUT_PATH).Path
    } elseif (-not [string]::IsNullOrWhiteSpace($env:PASEO_ROOT_PATH) -and (Test-Path $env:PASEO_ROOT_PATH)) {
        $ParentRepo = (Resolve-Path $env:PASEO_ROOT_PATH).Path
    } else {
        try {
            Push-Location $targetDir
            $commonGitDir = (git rev-parse --git-common-dir 2>$null)
            Pop-Location

            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($commonGitDir)) {
                $resolvedCommon = Resolve-Path (Join-Path $targetDir $commonGitDir) -ErrorAction SilentlyContinue
                if ($resolvedCommon) {
                    $ParentRepo = (Get-Item $resolvedCommon.Path).Parent.FullName
                }
            }
        } catch {
            # Fallback below
        }
    }

    if ([string]::IsNullOrWhiteSpace($ParentRepo)) {
        $ParentRepo = (Resolve-Path "$PSScriptRoot\..").Path
    }
} else {
    $ParentRepo = (Resolve-Path $ParentRepo).Path
}

Write-Host "Parent repository: $ParentRepo" -ForegroundColor DarkGray

$targetVenv = Join-Path $targetDir ".venv"
$parentVenv = Join-Path $ParentRepo ".venv"
$reqFile = Join-Path $targetDir "requirements.txt"
$reqDevFile = Join-Path $targetDir "requirements-dev.txt"

# 3. Configure Python Virtual Environment
Write-Step "Configuring Python virtual environment (.venv)..."

if ($SharedVenv) {
    if (-not (Test-Path $parentVenv)) {
        Write-ErrMsg "Parent .venv not found at: $parentVenv. Cannot use -SharedVenv."
        exit 1
    }

    if (Test-Path $targetVenv) {
        $item = Get-Item $targetVenv -Force
        if ($item.LinkType -eq "Junction" -or $item.Attributes -match "ReparsePoint") {
            Write-Host "Junction link already present: $targetVenv -> $parentVenv" -ForegroundColor Gray
        } else {
            Write-WarnMsg "Existing .venv is an isolated directory, keeping existing environment."
        }
    } else {
        Write-Host "Creating NTFS Junction: $targetVenv -> $parentVenv" -ForegroundColor Yellow
        New-Item -ItemType Junction -Path $targetVenv -Target $parentVenv | Out-Null
        Write-Success "Shared .venv successfully linked via NTFS Junction."
    }
} else {
    $venvPython = Join-Path $targetVenv "Scripts\python.exe"

    $hasUv = $false
    if (-not $UsePip) {
        $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($uvCmd) {
            $hasUv = $true
        }
    }

    if (-not (Test-Path $venvPython)) {
        if ($hasUv) {
            Write-Host "Creating .venv using uv (Python $PythonVersion)..." -ForegroundColor Green
            try {
                uv venv $targetVenv --python $PythonVersion
                if ($LASTEXITCODE -ne 0) {
                    Write-WarnMsg "uv venv failed with code $LASTEXITCODE. Falling back to system python..."
                    python -m venv $targetVenv
                }
            } catch {
                Write-WarnMsg "uv venv invocation error. Falling back to system python..."
                python -m venv $targetVenv
            }
        } else {
            Write-Host "Creating .venv using python -m venv..." -ForegroundColor Yellow
            python -m venv $targetVenv
        }
    } else {
        Write-Host ".venv already exists at: $targetVenv" -ForegroundColor Gray
    }

    if (-not (Test-Path $venvPython)) {
        Write-ErrMsg "Failed to create .venv: $venvPython not found!"
        exit 1
    }

    # 4. Install Dependencies
    Write-Step "Installing dependencies..."
    if (Test-Path $reqFile) {
        if ($hasUv) {
            Write-Host "Installing requirements.txt via uv pip..." -ForegroundColor Green
            uv pip install -r $reqFile --python $venvPython
            if ($LASTEXITCODE -ne 0) {
                Write-WarnMsg "uv pip exited with code $LASTEXITCODE, falling back to pip.exe..."
                & $venvPython -m pip install -r $reqFile
            }
        } else {
            Write-Host "Upgrading pip and installing requirements.txt..." -ForegroundColor Yellow
            & $venvPython -m pip install --upgrade pip
            & $venvPython -m pip install -r $reqFile
        }
        Write-Success "Core requirements installed."
    } else {
        Write-WarnMsg "requirements.txt not found in $targetDir"
    }

    if (-not $SkipDev -and (Test-Path $reqDevFile)) {
        if ($hasUv) {
            Write-Host "Installing requirements-dev.txt via uv pip..." -ForegroundColor Green
            uv pip install -r $reqDevFile --python $venvPython
            if ($LASTEXITCODE -ne 0) {
                Write-WarnMsg "uv pip exited with code $LASTEXITCODE, falling back to pip.exe..."
                & $venvPython -m pip install -r $reqDevFile
            }
        } else {
            Write-Host "Installing requirements-dev.txt via pip..." -ForegroundColor Yellow
            & $venvPython -m pip install -r $reqDevFile
        }
        Write-Success "Dev / test requirements installed."
    }
}

$venvPython = Join-Path $targetVenv "Scripts\python.exe"
$venvPlaywright = Join-Path $targetVenv "Scripts\playwright.exe"

# 5. Playwright Chromium browser
if (-not $SkipPlaywright -and -not $SkipDev -and -not $SharedVenv) {
    if (Test-Path $venvPlaywright) {
        Write-Step "Checking / installing Playwright Chromium browser..."
        try {
            & $venvPlaywright install chromium
            Write-Success "Playwright Chromium browser installed."
        } catch {
            Write-WarnMsg "Playwright browser installation returned error: $_"
        }
    }
}

# 6. Copy local unversioned configs from parent repo
if (-not $SkipConfigCopy) {
    Write-Step "Synchronizing local configuration files..."

    # candidate_profile.json
    $targetProfile = Join-Path $targetDir "candidate_profile.json"
    $parentProfile = Join-Path $ParentRepo "candidate_profile.json"
    $exampleProfile = Join-Path $targetDir "candidate_profile.example.json"

    if (-not (Test-Path $targetProfile)) {
        if (Test-Path $parentProfile) {
            Copy-Item $parentProfile $targetProfile -Force
            Write-Success "Copied candidate_profile.json from parent repository."
        } elseif (Test-Path $exampleProfile) {
            Copy-Item $exampleProfile $targetProfile -Force
            Write-WarnMsg "Copied template candidate_profile.example.json -> candidate_profile.json"
        }
    } else {
        Write-Host "candidate_profile.json already present in worktree." -ForegroundColor Gray
    }

    # ai_assistant/.env
    $targetAiEnv = Join-Path $targetDir "ai_assistant\.env"
    $parentAiEnv = Join-Path $ParentRepo "ai_assistant\.env"
    if (-not (Test-Path $targetAiEnv) -and (Test-Path $parentAiEnv)) {
        $parentAiEnvDir = Split-Path $targetAiEnv -Parent
        if (-not (Test-Path $parentAiEnvDir)) {
            New-Item -ItemType Directory -Path $parentAiEnvDir -Force | Out-Null
        }
        Copy-Item $parentAiEnv $targetAiEnv -Force
        Write-Success "Copied ai_assistant\.env from parent repository."
    }

    # Root .env
    $targetRootEnv = Join-Path $targetDir ".env"
    $parentRootEnv = Join-Path $ParentRepo ".env"
    if (-not (Test-Path $targetRootEnv) -and (Test-Path $parentRootEnv)) {
        Copy-Item $parentRootEnv $targetRootEnv -Force
        Write-Success "Copied root .env from parent repository."
    }
}

# 7. Environment Sanity Check
Write-Step "Performing environment sanity check..."
try {
    $checkOutput = & $venvPython -c "import ai_assistant; import pytest; import pydantic; print('READY')" 2>&1
    if ($checkOutput -match "READY") {
        Write-Success "Environment sanity check PASSED (ai_assistant, pytest, pydantic loaded)."
    } else {
        Write-WarnMsg "Sanity check output: $checkOutput"
    }
} catch {
    Write-WarnMsg "Sanity check execution failed: $_"
}

# 8. Optional Smoke Test (-RunTests)
if ($RunTests) {
    Write-Step "Running smoke tests (pytest)..."
    Push-Location $targetDir
    try {
        $smokeTest = Join-Path $targetDir "tests\test_application_dashboard.py"
        if (-not (Test-Path $smokeTest)) {
            $anyTest = Get-ChildItem -Path (Join-Path $targetDir "tests") -Filter "test_*.py" | Select-Object -First 1
            if ($anyTest) { $smokeTest = $anyTest.FullName }
        }
        if (Test-Path $smokeTest) {
            Write-Host "Running: pytest $smokeTest -q" -ForegroundColor DarkGray
            & $venvPython -m pytest $smokeTest -q
            if ($LASTEXITCODE -eq 0) {
                Write-Success "Smoke test PASSED!"
            } else {
                Write-WarnMsg "Smoke test finished with exit code $LASTEXITCODE"
            }
        } else {
            Write-WarnMsg "No test files found in $targetDir\tests"
        }
    } finally {
        Pop-Location
    }
}

Write-Host "`n==========================================================" -ForegroundColor Green
Write-Host "  Worktree setup complete and ready for development!      " -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "To navigate and activate environment:" -ForegroundColor Yellow
Write-Host "  cd `"$targetDir`"" -ForegroundColor White
Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "To run tests:" -ForegroundColor Yellow
Write-Host "  pytest tests -q" -ForegroundColor White
Write-Host ""
