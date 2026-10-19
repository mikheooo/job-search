[CmdletBinding()]
param (
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$env:PYTHONPATH = "$ProjectRoot\.venv\Lib\site-packages;$ProjectRoot"

$UvPython = "C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
if (Test-Path $UvPython) {
    $PythonExe = $UvPython
} else {
    $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
Set-Location $ProjectRoot

# Keep lock handling, logging, exit-code propagation, and health tracking in
# the Python runner so scheduled and interactive production runs share one path.
$arguments = @("-m", "ai_assistant.cli", "production-run")
if ($DryRun) {
    $arguments += "--dry-run"
}

& $PythonExe @arguments
exit $LASTEXITCODE
