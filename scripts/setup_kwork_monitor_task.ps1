[CmdletBinding()]
param (
    [switch]$Unregister
)

$taskName = 'JobSearch_Kwork_Inbox_Monitor'
$projectRoot = 'C:\Users\Misha\Documents\job-search'
$vbsPath = Join-Path $projectRoot 'scripts\run_kwork_monitor_silent.vbs'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task $taskName unregistered successfully." -ForegroundColor Yellow
    exit 0
}

# Action: run wscript.exe scripts\run_kwork_monitor_silent.vbs (100% silent, no terminal popup)
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbsPath`"" -WorkingDirectory $projectRoot

# Trigger: Start now, repeat every 7 minutes indefinitely
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 7)

# Settings: Ignore new if already running, don't stop on battery, max execution 5 minutes
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

# Register or update task
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Continuous Kwork inbox monitor in safe read-only mode every 7 minutes (silent background execution)' `
    -Force

Write-Host "Task $taskName successfully registered to run silently every 7 minutes!" -ForegroundColor Green
