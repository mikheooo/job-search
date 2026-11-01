[CmdletBinding()]
param (
    [switch]$Live = $true,
    [switch]$DryRun,
    [switch]$SendOne,
    [string]$CdpUrl = "http://127.0.0.1:9111"
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

$arguments = @("scripts/run_kwork_inbox_monitor_cycle.py")
if ($Live -and -not $DryRun) {
    $arguments += "--live"
}
if ($SendOne) {
    $arguments += "--send-one"
}
if ($CdpUrl -ne "http://127.0.0.1:9111") {
    $arguments += @("--cdp-url", $CdpUrl)
}

& $PythonExe @arguments
exit $LASTEXITCODE
