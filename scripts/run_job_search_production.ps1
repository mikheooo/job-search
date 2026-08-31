# ==============================================================================
# Production Scheduled Wrapper for Unified Job Search Dispatcher (Stage 83)
# ==============================================================================
# Invocation for Windows Task Scheduler:
#   powershell.exe -ExecutionPolicy Bypass -File C:\Users\Misha\Documents\job-search\scripts\run_job_search_production.ps1
# ==============================================================================

[CmdletBinding()]
param (
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"

# 1. Establish project directory and paths
$ProjectRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $ProjectRoot

$LogsDir = "$ProjectRoot\logs\job_search"
if (!(Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

$LogFile = "$LogsDir\job_search_production.log"
$LockFile = "$LogsDir\job_search.lock"
$PythonExe = "$ProjectRoot\.venv\Scripts\python.exe"
$FetcherScript = "$env:LOCALAPPDATA\hermes\profiles\jobs\scripts\job_search_fetcher.py"

# 2. Log rotation if log file exceeds 5MB
if (Test-Path $LogFile) {
    $item = Get-Item $LogFile
    if ($item.Length -gt 5242880) {
        if (Test-Path "$LogFile.1") { Remove-Item "$LogFile.1" -Force }
        Rename-Item $LogFile "$LogFile.1" -Force
    }
}

# 3. Single-Instance Process Lock Check
if (Test-Path $LockFile) {
    try {
        $lockJson = Get-Content $LockFile -Raw | ConvertFrom-Json
        $lockedPid = [int]$lockJson.pid
        $lockedAt = $lockJson.timestamp

        $proc = Get-Process -Id $lockedPid -ErrorAction SilentlyContinue
        if ($proc) {
            $msg = "[INFO] SKIPPED_ALREADY_RUNNING: Active PID $lockedPid owns lock (started at $lockedAt). Exiting safely."
            Write-Host $msg
            Add-Content -Path $LogFile -Value "`n[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg" -Encoding UTF8
            exit 0
        } else {
            Write-Warning "[STAGE 83] Stale lock detected (dead PID $lockedPid). Recovering lock file."
            Remove-Item $LockFile -Force
        }
    } catch {
        Write-Warning "[STAGE 83] Corrupted lock file encountered. Overwriting."
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    }
}

# Acquire lock
$currentPid = $PID
$nowIso = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ")
$lockPayload = @{
    pid = $currentPid
    timestamp = $nowIso
    host = $env:COMPUTERNAME
} | ConvertTo-Json

Set-Content -Path $LockFile -Value $lockPayload -Encoding UTF8

# 4. Execute production fetcher
$startTime = Get-Date
$startIso = $startTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ")

$startHeader = @"

==================== RUN START ====================
timestamp: $startIso
pid: $currentPid
command: $PythonExe $FetcherScript
working_dir: $ProjectRoot
---------------------------------------------------
"@
Add-Content -Path $LogFile -Value $startHeader -Encoding UTF8

if ($DryRun) {
    $env:JOB_SEARCH_DRY_RUN = "1"
}

try {
    # Execute dispatcher capturing output and exit code
    $output = & $PythonExe $FetcherScript 2>&1
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { $exitCode = 0 }
} catch {
    $output = "[CRITICAL RUNNER ERROR] Failed to execute dispatcher: $_"
    $exitCode = 99
} finally {
    # 5. Clean up lock file
    if (Test-Path $LockFile) {
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    }
}

$endTime = Get-Date
$endIso = $endTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ")
$duration = [Math]::Round(($endTime - $startTime).TotalSeconds, 2)

# Sanitize secrets from output
$sanitizedOutput = ($output -join "`n") -replace 'bot\d+:[A-Za-z0-9_-]{25,}', 'bot<MASKED_TELEGRAM_TOKEN>' -replace 'TELEGRAM_BOT_TOKEN=[^\s\r\n]+', 'TELEGRAM_BOT_TOKEN=<MASKED>'

$endFooter = @"
$sanitizedOutput
---------------------------------------------------
RUN END: $endIso | exit_code: $exitCode | duration: ${duration}s
===================================================
"@
Add-Content -Path $LogFile -Value $endFooter -Encoding UTF8

# Echo output to console
Write-Host $sanitizedOutput
Write-Host "[STAGE 83] Production run completed (exit_code: $exitCode, duration: ${duration}s)."

exit $exitCode