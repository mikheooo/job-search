[CmdletBinding()]
param (
    [switch]$DryRun,
    [switch]$Force,
    [switch]$SendIfSunday
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

$arguments = @("-m", "ai_assistant.cli", "metrics", "week")

if ($SendIfSunday) {
    $arguments += "--send-if-sunday"
} else {
    $arguments += "--send"
}

if ($DryRun) {
    $arguments += "--dry-run"
}
if ($Force) {
    $arguments += "--force"
}

& $PythonExe @arguments
exit $LASTEXITCODE
