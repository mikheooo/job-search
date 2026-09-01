[CmdletBinding()]
param (
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
Set-Location $ProjectRoot

# Keep lock handling, logging, exit-code propagation, and health tracking in
# the Python runner so scheduled and interactive production runs share one path.
$arguments = @("-m", "ai_assistant.cli", "production-run")
if ($DryRun) {
    $arguments += "--dry-run"
}

& $PythonExe @arguments
exit $LASTEXITCODE
